# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Overview

Single-file MCP server that schedules and executes Claude Code CLI tasks via cron expressions. Built with FastMCP and stores jobs/runs in SQLite. Features a web dashboard, webhook support, dynamic MCP server creation, and token/cost tracking.

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

Jobs execute by spawning `claude -p <prompt> --output-format json` as a subprocess. Token usage and costs are parsed from CLI output.

## Directory Structure

```
├── server.py           # Main MCP server
├── jobs.db             # SQLite database
├── fixed-servers/      # Built-in MCP servers (e.g., email)
│   └── email/
│       ├── server.py
│       └── metadata.json
└── dynamic_servers/    # User-created MCP servers via create_mcp_server tool
    ├── facebook-nieuwsbalen/
    └── azure-devops-workitems/
```

## Database

SQLite at `./jobs.db` with tables:
- `jobs` - Scheduled tasks with cron expressions, prompts, timeout settings
- `runs` - Execution history with output, tokens, cost, state (pending/running/finished/error)
- `webhooks` - HTTP endpoints that trigger prompts with payload templating
- `settings` - Configuration for allowed tools, MCP servers, credentials

## MCP Tools

**Job Management:** `list_jobs`, `get_job`, `create_job`, `update_job`, `delete_job`, `trigger_job`

**Run Management:** `list_runs`, `get_run`, `cancel_run`

**Webhooks:** `create_webhook`, `list_webhooks`, `get_webhook`, `update_webhook`, `delete_webhook`

**Dynamic MCP Servers:** `create_mcp_server`, `list_dynamic_mcp_servers`, `get_dynamic_mcp_server`, `update_dynamic_mcp_server`, `delete_dynamic_mcp_server`

**Fixed MCP Servers:** `list_fixed_mcp_servers`, `enable_fixed_server`, `disable_fixed_server`

**Credential Management:** `set_server_credential`, `get_server_credentials`, `list_required_credentials`, `get_unconfigured_servers`, `delete_server_credential`

**Settings:** `get_settings`, `update_settings`

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

- Cost overview (today/week/total)
- Quick run for ad-hoc prompts
- Job/run/webhook management
- Fixed and dynamic MCP server management
- Credential configuration per server
- Live run output streaming

## Key Dependencies

- `fastmcp` - MCP protocol implementation
- `croniter` - Cron expression parsing
- `sendgrid`, `requests` - Email provider integrations
- `uvicorn` - ASGI server for SSE transport
- `pyngrok` - ngrok tunnel for remote access from claude.ai
