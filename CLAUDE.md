# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Rules

- **Always update CLAUDE.md** when making changes that affect architecture, conventions, URL resolution logic, helper functions, or any other information documented here. Keep this file in sync with the codebase.

## Overview

Single-file MCP server that schedules and executes AI tasks via multiple providers (Claude, OpenAI, Ollama) with cron expressions. Built with FastMCP and stores jobs/runs in SQLite. Features a web dashboard, webhook support, dynamic MCP server creation, and token/cost tracking.

## Commands

```bash
# Install dependencies
pip install -r requirements.txt

# Run server (stdio transport for Claude Desktop)
python server.py

# Run with SSE transport (enables web dashboard + remote access)
MCP_TRANSPORT=sse python server.py
# Dashboard available at http://localhost:8080/

# Run with ngrok for remote access from claude.ai
NGROK_AUTHTOKEN=your_token python server.py
# ngrok tunnel URL will be printed on startup
```

## Architecture

The server runs three concurrent async components in separate threads:

1. **MCP Server** - Exposes 25+ tools via FastMCP (jobs, runs, webhooks, credentials, settings)
2. **Scheduler Loop** - Checks every 60 seconds for jobs due based on cron expressions
3. **Run Processor Loop** - Picks up pending runs and executes them (handles restarts gracefully)

### Provider Architecture

Jobs execute via a **provider abstraction layer**. The `command` field on jobs/runs selects the provider:

| Provider | `command` value | Execution method | MCP tool support |
|----------|----------------|------------------|-----------------|
| **ClaudeProvider** | `"claude"` | Claude Agent SDK (`sdk_query()` streaming) | Native MCP via CLI |
| **OpenAIProvider** | `"openai"` | OpenAI Chat Completions API with function calling | MCP tools converted to OpenAI functions |
| **OllamaProvider** | `"ollama"` | Ollama HTTP API (`/api/chat`) | MCP tools converted to OpenAI-compatible functions |

**Key classes:**
- `BaseProvider` (ABC) - Abstract interface with `execute()`, `get_pricing()`, `is_available()`, `get_capabilities()`
- `ProviderResult` (dataclass) - Standardized result with `output_parts`, tokens, cost, model info
- `PROVIDERS` (dict) - Registry populated at startup by `init_providers()`
- `_get_default_provider()` - Returns default provider (DB setting `default_provider`, then first available)

**MCP-to-function-calling conversion** (used by OpenAI and Ollama providers):
- `_extract_tool_schema(func)` - Builds JSON Schema from `inspect.signature()`
- `_convert_mcp_tools_to_functions(mcp_config)` - Discovers tools via `_discover_python_mcp_tools()`, loads via `_load_mcp_tool_from_file()`, converts to OpenAI function format. Temporarily injects each server's `config['env']` vars into `os.environ` before loading so that in-process module imports see credentials, then restores the original env afterwards.
- Tool execution in agentic loop: parse arguments from provider response, call MCP tool callable, return result

### Environment Variables for Providers

| Variable | Default | Description |
|----------|---------|-------------|
| `OPENAI_API_KEY` | (required for OpenAI) | OpenAI API key |
| `OPENAI_MODEL` | `gpt-4o` | OpenAI model to use |
| `OLLAMA_URL` | `http://localhost:11434` | Local server URL (Ollama, LM Studio, etc.) |
| `OLLAMA_MODEL` | `llama3.1` | Local model to use (e.g. `openai/gpt-oss-20b` for LM Studio) |

## Directory Structure

```
├── server.py           # Main MCP server
├── jobs.db             # SQLite database
├── static/             # Static assets served by the dashboard
│   ├── dashboard.html  # Dashboard HTML (loaded at startup by _load_dashboard_html())
│   └── dashboard.css   # Dashboard styles
├── fixed-servers/      # Built-in MCP servers (e.g., email)
│   └── email/
│       ├── server.py
│       └── metadata.json
├── dynamic_servers/    # User-created MCP servers via create_mcp_server tool
│   ├── cat-facts/
│   └── r2-images/
└── deploy/             # Deployment configs (systemd, nginx, deploy script)
```

## Database

SQLite at `./jobs.db` with tables:
- `jobs` - Scheduled tasks with cron expressions, prompts, timeout settings. The `command` field selects the AI provider (`"claude"`, `"openai"`, `"ollama"`)
- `runs` - Execution history with output, tokens, cost, state (pending/running/finished/error)
- `webhooks` - HTTP endpoints that trigger prompts with payload templating
- `settings` - Configuration for allowed tools, MCP servers, credentials, `default_provider`

## MCP Tools

**Job Management:** `list_jobs`, `get_job`, `create_job`, `update_job`, `delete_job`, `trigger_job`

**Run Management:** `list_runs`, `get_run`, `kill_run`

**Webhooks:** `create_webhook`, `list_webhooks`, `get_webhook`, `update_webhook`, `delete_webhook`

**Dynamic MCP Servers:** `create_mcp_server`, `list_dynamic_mcp_servers`, `get_dynamic_mcp_server`, `update_mcp_server`, `delete_mcp_server`, `enable_mcp_server`, `disable_mcp_server`

**Fixed MCP Servers:** `list_fixed_mcp_servers`, `enable_fixed_server`, `disable_fixed_server`

**Credential Management:** `set_server_credential`, `get_server_credentials`, `list_required_credentials`, `get_unconfigured_servers`, `delete_server_credential`

**Internal MCP:** `invoke_internal_mcp_tool`

## URL Resolution

Two helpers in `server.py` resolve the public-facing server URL. Both follow the same priority order:

1. `PUBLIC_URL` env var (highest priority)
2. `NGROK_PUBLIC_URL` global (set when an ngrok tunnel is active)
3. Fallback

| Helper | Fallback | Used by |
|--------|----------|---------|
| `get_server_url()` | `http://localhost:8080` | OAuth, general server links |
| `get_webhook_base_url()` | DB setting `webhook_base_url`, then `http://localhost:8080` | All webhook URL generation (`create_webhook`, `list_webhooks`, `get_webhook`, `api_webhooks_handler`) |

When adding new code that builds user-facing URLs, use one of these helpers instead of hardcoding `localhost` or reading from the DB directly.

## Credential Management

All MCP server credentials are stored in the database (`settings.mcp_env_vars`). Each server declares required environment variables in its `metadata.json`:

```json
{
  "env_vars": [
    {"name": "API_KEY", "description": "API key for service", "required": true, "sensitive": true},
    {"name": "ORG_ID", "description": "Organization identifier", "required": true, "sensitive": false}
  ]
}
```

Use MCP tools to manage credentials:
- `set_server_credential("server-name", "VAR_NAME", "value")` - Set a credential
- `get_server_credentials("server-name")` - View configured credentials (values masked)
- `list_required_credentials("server-name")` - See what a server needs
- `get_unconfigured_servers()` - Find servers missing required credentials

When creating dynamic servers with `create_mcp_server`, env vars can be auto-detected from code patterns like `os.environ.get("VAR_NAME")`.

## Web Dashboard (SSE mode only)

The dashboard HTML lives in `static/dashboard.html` and is loaded once at import time into the `DASHBOARD_HTML` variable via `_load_dashboard_html()`. CSS is in `static/dashboard.css`.

- Cost overview (today/week/total)
- Quick run for ad-hoc prompts with provider selection dropdown
- Job/run/webhook management
- Fixed and dynamic MCP server management
- Credential configuration per server
- Live run output streaming
- Provider/model shown on run cards

### Dashboard API Endpoints

- `GET /api/providers` - Returns available providers with capabilities and default
- `POST /api/run-prompt` - Accepts `{prompt, command}` where `command` is the provider name

## Key Dependencies

- `fastmcp` - MCP protocol implementation
- `claude-agent-sdk` - Claude provider execution engine
- `openai` - OpenAI provider (optional, gracefully skipped if not installed)
- `croniter` - Cron expression parsing
- `sendgrid`, `requests` - Email provider integrations
- `uvicorn` - ASGI server for SSE transport
- `pyngrok` - ngrok tunnel for remote access from claude.ai
- `python-dotenv` - `.env` file loading
- `PyJWT` - JWT token handling for OAuth
- `beautifulsoup4` - Web scraping
- `boto3` - AWS integration
- `playwright` - Browser automation
