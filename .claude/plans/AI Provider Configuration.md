# Plan: Provider Configuration Page

## Context

The multi-provider architecture (Claude, OpenAI, Ollama) is already implemented. Currently, provider credentials and settings are configured only via environment variables (`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `OPENAI_MODEL`, `OLLAMA_URL`, `OLLAMA_MODEL`). The user wants a web dashboard page to manage all provider configuration from the UI — set API keys, select models, configure URLs, and set the default provider.

## Files to Modify

- **`server.py`** — DB helpers, provider class changes, API endpoints, dashboard HTML, dashboard JS
- **`static/dashboard.css`** — styles for the new provider cards

## Implementation Steps

### Step 1: DB Helpers for Provider Config

Add after `_save_mcp_env_vars_setting()` (~line 680):

```python
def _get_provider_config() -> dict:
    """Load provider_config from settings table."""
    # Returns: {"claude": {"api_key": "...", "model": "..."}, "openai": {...}, "ollama": {...}}

def _save_provider_config(config: dict) -> None:
    """Save provider_config to settings table."""

def _get_provider_value(provider: str, key: str, env_var: str, default: str = "") -> str:
    """Get a provider config value. Priority: DB > env var > default."""
```

Storage key: `provider_config` in the `settings` table. Schema:
```json
{
  "claude": {"api_key": "sk-ant-...", "model": "claude-sonnet-4-20250514"},
  "openai": {"api_key": "sk-...", "model": "gpt-4o"},
  "ollama": {"url": "http://localhost:11434", "model": "llama3.1"}
}
```

### Step 2: Modify Provider Classes to Use DB-first Config

**ClaudeProvider:**
- `is_available()`: Also check `_get_provider_value("claude", "api_key", "ANTHROPIC_API_KEY")`
- `execute()`: Inject DB key into `os.environ["ANTHROPIC_API_KEY"]` if env var not set (SDK reads env directly)

**OpenAIProvider:**
- `execute()`: Use `_get_provider_value("openai", "api_key", "OPENAI_API_KEY")` and `_get_provider_value("openai", "model", "OPENAI_MODEL", "gpt-4o")`
- `is_available()`: Use `_get_provider_value` instead of just `os.environ.get`

**OllamaProvider:**
- `_get_openai_base_url()`: Use `_get_provider_value("ollama", "url", "OLLAMA_URL", "http://localhost:11434")`
- `execute()`: Use `_get_provider_value` for model
- `is_available()`: Use `_get_provider_value` for URL

### Step 3: Add `_reinit_providers()`

New function that clears `PROVIDERS` dict, injects DB-stored API keys into `os.environ`, then calls `init_providers()`. Called after saving provider config via the API.

### Step 4: New/Enhanced API Endpoints

**Enhance `GET /api/providers` (line 3001):**
Add to each provider's response:
- `has_api_key` (bool)
- `masked_api_key` (`"****abcd"` or `""`)
- `key_source` (`"database"`, `"environment"`, `"none"`)
- `model` (currently configured model)
- `available_models` (from PRICING / OPENAI_PRICING dicts)
- `url` (for ollama only)

Keys are NEVER returned in full. Mask: `"****" + key[-4:]` if len > 4.

**New `POST /api/provider-config`:**
Body: `{"provider": "openai", "api_key": "sk-...", "model": "gpt-4o"}`
- Validates provider name
- Skips `api_key` update if value is `"****..."` (masked placeholder)
- Saves via `_save_provider_config()`
- Calls `_reinit_providers()`
- Returns `{"success": true, "available": bool}`

**New `POST /api/default-provider`:**
Body: `{"provider": "openai"}`
- Saves to settings `default_provider`
- Returns `{"success": true}`

Register in `dashboard_routes` (line ~3887).

### Step 5: Mask Keys in Settings GET

In `api_settings_get_handler` (line 3019), after building the settings dict, mask any `api_key` values in `provider_config` before returning. Prevents the generic endpoint from leaking keys.

### Step 6: Add "Providers" Tab to Dashboard HTML

**Tab button** — insert between "Tool Logs" and "Settings" (line ~1274):
```html
<button class="tab" onclick="showTab('providers')">Providers</button>
```

**Section div** — add between `tool-logs` and `settings` sections (line ~1355):
```html
<div id="providers" class="section">
  <div class="card">
    <h2>AI Provider Configuration</h2>
    <p class="provider-desc">Configure API keys, models, and set the default provider.</p>
    <div id="provider-cards" class="provider-cards-grid"></div>
    <div class="provider-default-section">
      <h3>Default Provider</h3>
      <div class="provider-default-row">
        <select id="default-provider-select"></select>
        <button class="refresh-btn" onclick="saveDefaultProvider()">Set Default</button>
      </div>
    </div>
  </div>
</div>
```

### Step 7: Dashboard JavaScript

All rendering uses **safe DOM methods** (createElement, textContent, appendChild, addEventListener). No innerHTML.

**`loadProviderConfig()`** — fetches `GET /api/providers`, calls `renderProviderCards()`.

**`renderProviderCards(data)`** — for each provider (claude, openai, ollama), creates a card with:
- Header: provider name + status badge (online/offline)
- Key source label ("Database" / "Environment" / "Not configured")
- API Key password input (claude, openai) — pre-filled with masked value or empty
- URL text input (ollama only) — pre-filled with current URL
- Model select dropdown — populated from `available_models` with current selection
- "Save" button → `saveProviderConfig(name)`
- "Test" button → `testProvider(name)` (calls reinit then checks availability)

**`saveProviderConfig(name)`** — reads input values from the card, POSTs to `/api/provider-config`, reloads config + quick-run dropdown.

**`testProvider(name)`** — saves first, then reads availability from the response. Shows toast.

**`populateDefaultSelect(data)`** — populates the default provider dropdown, marks current default.

**`saveDefaultProvider()`** — POSTs to `/api/default-provider`.

Call `loadProviderConfig()` from the existing page load flow (in or after `loadProviders()`).

### Step 8: CSS Styles

Add to `static/dashboard.css` after the MCP Server Cards section (~line 690):

```css
/* Provider Cards */
.provider-cards-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(340px, 1fr)); gap: 16px; margin-bottom: 24px; }
.provider-card { background: var(--bg-secondary); border: 1px solid var(--border-default); padding: 20px; transition: all 0.15s ease; }
.provider-card:hover { border-color: var(--border-hover); }
.provider-card.available { border-color: var(--success); }
.provider-card-header { display: flex; align-items: center; gap: 8px; margin-bottom: 12px; }
.provider-card-name { font-weight: 600; font-size: 16px; flex: 1; }
.provider-card-badge { font-size: 10px; padding: 2px 8px; text-transform: uppercase; letter-spacing: 0.5px; font-weight: 600; }
.provider-card-badge.online { background: rgba(16, 185, 129, 0.15); color: var(--success); }
.provider-card-badge.offline { background: rgba(239, 68, 68, 0.15); color: var(--error); }
.provider-card-source { font-size: 11px; color: var(--text-muted); margin-bottom: 12px; font-family: var(--font-mono); }
.provider-card-fields { display: flex; flex-direction: column; gap: 12px; padding-top: 12px; border-top: 1px solid var(--border-default); }
.provider-field label { display: block; font-size: 10px; font-weight: 600; color: var(--text-muted); margin-bottom: 4px; text-transform: uppercase; letter-spacing: 0.5px; }
.provider-field input, .provider-field select { width: 100%; padding: 8px; border: 1px solid var(--border-default); background: var(--bg-tertiary); color: var(--text-primary); font-size: 13px; }
.provider-field input:focus, .provider-field select:focus { outline: none; border-color: var(--accent-blue); }
.provider-card-actions { display: flex; gap: 8px; margin-top: 16px; }
.provider-save-btn { /* same as refresh-btn */ }
.provider-test-btn { /* same as view-btn */ }
.provider-default-section { margin-top: 24px; padding-top: 24px; border-top: 1px solid var(--border-default); }
.provider-default-row { display: flex; gap: 8px; align-items: center; }
```

Plus responsive rules in the existing `@media (max-width: 768px)` block: `.provider-cards-grid { grid-template-columns: 1fr; }`

### Step 9: Update CLAUDE.md

Document:
- New `provider_config` settings key and its schema
- New API endpoints (`POST /api/provider-config`, `POST /api/default-provider`)
- DB-first config resolution (DB > env var > default)
- Providers tab in dashboard

## Verification

1. Start server with NO env vars for providers → Providers tab shows all 3 cards as "offline"
2. Enter an OpenAI key in the UI, save → card shows "online", Quick Run dropdown includes "openai"
3. Enter an Ollama URL pointing to LM Studio, save → card shows "online"
4. Set default provider to "openai" → new Quick Run prompts default to OpenAI
5. Reload page → masked keys show `****xxxx`, model selections persist
6. Set OPENAI_API_KEY in env AND in DB → `key_source` shows "database" (DB takes priority)
7. Delete DB key (clear field, save) → falls back to env var, `key_source` shows "environment"
8. Run a job via each provider → cost tracking works correctly
