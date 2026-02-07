# Full Production Hardening: MCP Spinner Server

## Context

The server runs everything in a single Python process with 2-3 threads. A production audit revealed issues across concurrency, thread safety, security, reliability, and observability. This plan addresses all of them using stdlib tools — zero new services, zero new dependencies.

**Why not Temporal?** Researched Temporal, Celery, Hatchet, ARQ, Restate. All require external services. Temporal needs a full cluster + PostgreSQL. Our scale (1-50 jobs, 1-10 concurrent runs) is well within asyncio's capability. `sdk_query()` already spawns subprocesses internally. If we outgrow stdlib later, Restate (single binary, no DB) is the best option.

**All changes in:** `server.py` + `CLAUDE.md`

---

## Phase 1: Logging Framework

Replace all 55+ `print(..., file=sys.stderr)` calls with Python's `logging` module.

**Add near top of file (after imports):**
```python
import logging
logger = logging.getLogger("mcp-spinner")
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    stream=sys.stderr,
)
```

**Then replace all print statements** with appropriate levels:
- `logger.debug()` — serialization details (line 261), SDK message repr (line 4926), env key names (line 4878)
- `logger.info()` — startup, run created/completed, webhook triggered
- `logger.warning()` — stale run cleanup, migration fallbacks
- `logger.error()` — scheduler/processor errors, run failures, tool invocation errors

---

## Phase 2: SQLite Robustness

**`init_db()` (line 313)** — Add pragmas before `executescript`:
```python
conn.execute("PRAGMA journal_mode=WAL")
conn.execute("PRAGMA busy_timeout=5000")
conn.execute("PRAGMA synchronous=NORMAL")
```

**`get_db()` (line 466)** — Add timeout and busy_timeout:
```python
def get_db():
    conn = sqlite3.connect(DB_PATH, timeout=10.0)
    conn.execute("PRAGMA busy_timeout=5000")
    conn.row_factory = sqlite3.Row
    return conn
```

**Streaming DB writes in `execute_run()` (lines 4997-5010)** — Throttle to every ~2 seconds instead of every SDK message. Use a single connection for the streaming loop instead of `get_db()`/close per message:
```python
# Before streaming loop:
stream_conn = get_db()
last_db_flush = time.monotonic()
DB_FLUSH_INTERVAL = 2.0

# Inside loop (replace lines 5004-5010):
if time.monotonic() - last_db_flush >= DB_FLUSH_INTERVAL:
    stream_conn.execute("UPDATE runs SET output = ? WHERE id = ?", (current_output, run_id))
    stream_conn.commit()
    last_db_flush = time.monotonic()

# After loop: final flush + close stream_conn
```

**Fix migration `except: pass` blocks (lines 431-462)** — Replace with `except sqlite3.OperationalError: pass` and add `logger.debug()` for visibility.

---

## Phase 3: Concurrency Control

**New globals (near line 77):**
```python
MAX_CONCURRENT_RUNS = int(os.environ.get("MAX_CONCURRENT_RUNS", "3"))
_run_semaphore: asyncio.Semaphore | None = None
```

**New wrapper function:**
```python
async def _guarded_execute_run(run_id: str):
    global _run_semaphore
    if _run_semaphore is None:
        _run_semaphore = asyncio.Semaphore(MAX_CONCURRENT_RUNS)
    async with _run_semaphore:
        await execute_run(run_id)
```

**`scheduler_loop()` (line 5107)** — Remove `asyncio.create_task(execute_run(run_id))`. The `run_processor_loop` picks up pending runs within 5 seconds. This eliminates dual-spawn.

**`run_processor_loop()` (lines 5115-5131)** — Track active run IDs to avoid duplicate task spawning. Use `_guarded_execute_run`:
```python
async def run_processor_loop():
    active_run_ids: set[str] = set()
    while True:
        try:
            active_run_ids = {rid for rid in active_run_ids if rid in RUNNING_TASKS}
            conn = get_db()
            pending = conn.execute("SELECT id FROM runs WHERE state = 'pending'").fetchall()
            conn.close()
            for run in pending:
                rid = run["id"]
                if rid not in active_run_ids:
                    active_run_ids.add(rid)
                    asyncio.create_task(_guarded_execute_run(rid))
        except Exception as e:
            logger.error(f"Run processor error: {e}")
        await asyncio.sleep(5)
```

---

## Phase 4: Thread Safety

**New globals (near line 77):**
```python
_tasks_lock = threading.Lock()
_env_lock = threading.Lock()
_background_loop: asyncio.AbstractEventLoop | None = None
```

**`RUNNING_TASKS` access** — Wrap all reads/writes with `_tasks_lock`:
- Line 4682 (`RUNNING_TASKS[run_id] = ...`)
- Line 5083 (`RUNNING_TASKS.pop(run_id, None)`)
- Line 3813 (`RUNNING_TASKS.get(run_id)`)
- Line 3841 (`RUNNING_TASKS.pop(run_id, None)`)
- Line 2663 (`RUNNING_TASKS.get(run_id)`)
- Line 2686 (`RUNNING_TASKS.pop(run_id, None)`)

**`run_background_loops()` (line 5152)** — Store loop reference:
```python
def run_background_loops():
    global _background_loop
    loop = asyncio.new_event_loop()
    _background_loop = loop
    asyncio.set_event_loop(loop)
    ...
```

**Task cancellation in `kill_run()` (line 3826) and `api_kill_run_handler()` (line 2675)** — Replace `task.cancel()` with:
```python
if _background_loop and _background_loop.is_running():
    _background_loop.call_soon_threadsafe(task.cancel)
else:
    task.cancel()
```

**`invoke_internal_mcp_tool()` (lines 4549-4579)** — Wrap env var mutation with `_env_lock`.

**`_load_mcp_tool_from_file()` (lines 881-888)** — Wrap `sys.path` mutation with `_env_lock`.

---

## Phase 5: Security Fixes

### 5a. Mask credentials in `/api/settings` (line 2780)
In `api_settings_get_handler`, mask values in `mcp_env_vars` before returning:
```python
if 'mcp_env_vars' in settings and isinstance(settings['mcp_env_vars'], dict):
    masked = {}
    for server, vars in settings['mcp_env_vars'].items():
        masked[server] = {k: "****" if v else "" for k, v in vars.items()}
    settings['mcp_env_vars'] = masked
```

### 5b. Fix open CORS (line 3566)
Replace wildcard origin reflection with an allowlist. Use `ALLOWED_ORIGINS` env var:
```python
ALLOWED_ORIGINS = set(filter(None, os.environ.get("ALLOWED_ORIGINS", "").split(",")))
```
In `CORSMiddleware`, only set `Access-Control-Allow-Origin` if origin is in `ALLOWED_ORIGINS` or if the list is empty and the request is same-origin. Remove `Access-Control-Allow-Credentials: true` when using wildcard.

### 5c. Webhook rate limiting (line 3028)
Add simple in-memory rate limiting to `webhook_trigger_handler`:
```python
_webhook_rate: dict[str, list[float]] = {}  # token -> list of timestamps
WEBHOOK_RATE_LIMIT = int(os.environ.get("WEBHOOK_RATE_LIMIT", "10"))  # per minute
WEBHOOK_RATE_WINDOW = 60  # seconds
```
At the start of `webhook_trigger_handler`, check if the token has exceeded the limit in the last 60 seconds. Return 429 if so.

### 5d. Stop printing OAuth secret (lines 5209-5213)
Only print the OAuth secret when it was newly generated (first run), not on subsequent startups. Mask the middle characters.

---

## Phase 6: Reliability

### 6a. Output size cap in `execute_run()`
Add `MAX_OUTPUT_SIZE = int(os.environ.get("MAX_OUTPUT_SIZE_MB", "50")) * 1024 * 1024` and check `len(current_output)` in the streaming loop. When exceeded, stop appending to `output_parts` and add a truncation notice.

### 6b. Retry mechanism for failed runs
Add `max_retries` column to `jobs` table (default 0 = no retry) and `retry_count` column to `runs` table. In `execute_run()`, when a run fails with a transient error (timeout, SDK connection error), check if `retry_count < max_retries`. If so, create a new pending run with `retry_count + 1`.

### 6c. Subprocess timeout kill
In `execute_run()`, after catching `asyncio.TimeoutError` (line 5012), attempt to terminate the SDK subprocess. Check if `sdk_query` exposes a way to stop/cancel. If not, use the `RUNNING_TASKS` cancellation path which at minimum stops the async generator iteration. Add a comment documenting this limitation.

### 6d. Input validation
- `api_run_prompt_handler` (line 2690): Add `MAX_PROMPT_LENGTH = 100_000` check
- `webhook_trigger_handler` (line 3045): Add `MAX_PAYLOAD_SIZE = 1_048_576` (1MB) check on `request.body()`
- `create_job`/`update_job`: Validate `timeout_minutes` is between 1 and 1440 (24h)
- `list_runs`: Cap `limit` parameter at 1000

---

## Phase 7: Observability & Maintenance

### 7a. Health endpoint
Add `/api/health` route returning:
```json
{
  "status": "ok",
  "db": true,
  "scheduler_alive": true,
  "active_runs": 2,
  "pending_runs": 0,
  "uptime_seconds": 3600
}
```
Check DB connectivity and scheduler loop heartbeat.

### 7b. Database retention
Add `RETENTION_DAYS` env var (default 30). In `scheduler_loop`, once per hour, delete runs older than retention period (where state is `finished` or `error`). Also prune disabled one-off jobs (webhook/quick-run) with no recent runs, expired OAuth tokens, and old tool logs.

### 7c. Playground cleanup
After each `execute_run()` completes, optionally clean the `claude_playground` directory. Add `CLEANUP_PLAYGROUND` env var (default: `false`). When enabled, remove all files in the playground after run completion.

### 7d. Fix bare `except` clauses
Replace all 15+ `except: pass` with specific exception types (`except sqlite3.OperationalError`, `except json.JSONDecodeError`, `except ValueError`, etc.) and add `logger.debug()` calls.

### 7e. Graceful shutdown
Register `SIGINT`/`SIGTERM` handlers in `main()` that:
1. Cancel all running tasks via `_background_loop.call_soon_threadsafe(task.cancel)`
2. Wait 5 seconds for cleanup
3. Call `cleanup_stale_runs()`
4. Exit cleanly

---

## Phase 8: Run State & Resume (Interrupt + Resume Pattern)

Enable pausing runs mid-execution (e.g., for human approval) and resuming them later. Uses the Claude Agent SDK's built-in session persistence — conversation transcripts are stored as JSONL files on disk and can be resumed by session ID.

**Note:** Resume is Claude-provider only. OpenAI/Ollama providers don't support session persistence.

### 8a. Add `session_id` to `ProviderResult` and `runs` table

**`ProviderResult` dataclass (line 135)** — Add field:
```python
session_id: str | None = None
```

**`ClaudeProvider.execute()` (line ~5014)** — Capture session_id from ResultMessage:
```python
if hasattr(message, 'session_id') and message.session_id:
    session_id = message.session_id
```
Include it in the returned `ProviderResult(session_id=session_id, ...)`.

**`runs` table migration in `init_db()`** — Add column:
```python
try:
    conn.execute("ALTER TABLE runs ADD COLUMN session_id TEXT")
except sqlite3.OperationalError: pass
```

**`execute_run()` (line ~5640)** — Store session_id in the final DB update:
```sql
UPDATE runs SET ... session_id = ? ... WHERE id = ?
```

### 8b. Add `awaiting_approval` state

Extend the run state machine:
```
pending -> running -> finished
                   -> error
                   -> awaiting_approval -> pending (resume) -> running -> ...
```

### 8c. New MCP tools: `pause_run` and `resume_run`

**`pause_run(run_id: str, reason: str)` MCP tool:**
1. Find the running task in `RUNNING_TASKS`
2. Cancel the asyncio task (via `call_soon_threadsafe`)
3. Wait for the `execute_run` finally block to capture the session_id
4. Update DB: `state = 'awaiting_approval'`, store `reason` in a new `pause_reason` column
5. Return the run info with session_id

**`resume_run(run_id: str, approval_message: str = "")` MCP tool:**
1. Verify run is in `awaiting_approval` state and has a `session_id`
2. Create a new run with:
   - `state = 'pending'`
   - Reference to the original `session_id` for resume
   - Prompt = approval_message (or a default "Continue. The user has approved.")
3. The run processor picks it up within 5 seconds

**`ClaudeProvider.execute()` modifications for resume:**
Add `resume_session_id` parameter to `BaseProvider.execute()` signature (optional, default None). When provided in ClaudeProvider:
```python
sdk_options = ClaudeAgentOptions(
    ...,
    resume=resume_session_id,  # Resume from previous session
)
```

**`execute_run()` modifications:**
Check if the run references a previous session_id for resume. If so, pass it through to `provider.execute(resume_session_id=...)`.

### 8d. Dashboard API endpoints

- `POST /api/run/{run_id}/pause` — Calls `pause_run`
- `POST /api/run/{run_id}/resume` — Calls `resume_run` with `approval_message` from body

### 8e. Webhook-based approval trigger

Add a new webhook type `approval` that, when triggered, calls `resume_run` on the referenced run. This enables external systems (Slack, email, CI/CD) to approve paused runs.

---

## New Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `LOG_LEVEL` | `INFO` | Logging level (DEBUG/INFO/WARNING/ERROR) |
| `MAX_CONCURRENT_RUNS` | `3` | Max simultaneous job executions |
| `MAX_OUTPUT_SIZE_MB` | `50` | Max output size per run before truncation |
| `ALLOWED_ORIGINS` | `` (empty=permissive) | Comma-separated CORS allowed origins |
| `WEBHOOK_RATE_LIMIT` | `10` | Max webhook triggers per minute per token |
| `RETENTION_DAYS` | `30` | Days to keep completed/failed runs |
| `CLEANUP_PLAYGROUND` | `false` | Clean playground directory after each run |

## New DB Columns

| Table | Column | Type | Description |
|-------|--------|------|-------------|
| `runs` | `session_id` | TEXT | Claude CLI session ID for resume |
| `runs` | `pause_reason` | TEXT | Why the run was paused |
| `runs` | `resume_from_session` | TEXT | Session ID to resume from (set on the new resumed run) |
| `runs` | `retry_count` | INTEGER DEFAULT 0 | Current retry attempt number |
| `jobs` | `max_retries` | INTEGER DEFAULT 0 | Max retry attempts for transient failures |

## Files Modified
- `server.py` — All changes (~400-500 lines modified/added across all phases)
- `CLAUDE.md` — Document new env vars, state machine, resume capability, architecture decisions

## Verification
1. `MCP_TRANSPORT=sse python server.py` — Confirm structured log output, WAL mode logged
2. Trigger 5+ jobs simultaneously — Confirm only `MAX_CONCURRENT_RUNS` run concurrently
3. Kill a running job from dashboard — No crash, clean cancellation
4. `curl /api/health` — Returns health JSON
5. `curl /api/settings` — Credentials are masked (`****`)
6. Rapid-fire webhook triggers — 429 returned after rate limit exceeded
7. `Ctrl+C` — Graceful shutdown, stale runs cleaned
8. `sqlite3 jobs.db "PRAGMA journal_mode"` — Returns `wal`
9. Check logs have proper levels: `LOG_LEVEL=DEBUG python server.py`
10. Pause a running Claude job → verify state = `awaiting_approval` and session_id stored
11. Resume a paused job → verify new run created with `resume` and conversation continues
12. `get_run` on a completed Claude run → verify session_id is present
