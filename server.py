#!/usr/bin/env python3
"""
Claude Code Runner - MCP Server with Job Scheduling
A single-file MCP server that schedules and executes Claude Code CLI tasks.
"""

import sqlite3
import subprocess
import json
import asyncio
import uuid
import os
import sys
import time
import threading
import webbrowser
import base64
import secrets
import ast
import importlib.util
import inspect
from datetime import datetime, timezone, timedelta
from pathlib import Path

# Load environment variables from .env file
from dotenv import load_dotenv
load_dotenv()

def utc_now() -> datetime:
    """Get current UTC time (timezone-aware)"""
    return datetime.now(timezone.utc)

def utc_now_iso() -> str:
    """Get current UTC time as ISO string"""
    return datetime.now(timezone.utc).isoformat()
from croniter import croniter
from fastmcp import FastMCP
import re
from sendgrid import SendGridAPIClient
from sendgrid.helpers.mail import Mail
import requests

# Claude Agent SDK for executing prompts
try:
    from claude_agent_sdk import query as sdk_query, ClaudeAgentOptions
    AGENT_SDK_AVAILABLE = True
except ImportError:
    AGENT_SDK_AVAILABLE = False
    print("Warning: claude-agent-sdk not installed. Run: pip install claude-agent-sdk", file=sys.stderr)

# ngrok for remote access
try:
    from pyngrok import ngrok, conf
    NGROK_AVAILABLE = True
except ImportError:
    NGROK_AVAILABLE = False

# JWT for OAuth tokens
try:
    import jwt
    JWT_AVAILABLE = True
except ImportError:
    JWT_AVAILABLE = False
    print("Warning: PyJWT not installed. OAuth will not work. Run: pip install PyJWT", file=sys.stderr)
import hashlib

# === Configuration ===
DB_PATH = Path(__file__).parent / "jobs.db"
DYNAMIC_SERVERS_DIR = Path(__file__).parent / "dynamic_servers"
FIXED_SERVERS_DIR = Path(__file__).parent / "fixed-servers"
CHECK_INTERVAL_SECONDS = 60

# Global ngrok URL (set at startup)
NGROK_PUBLIC_URL = None

# Track running tasks for cancellation (run_id -> asyncio.Task)
RUNNING_TASKS: dict[str, asyncio.Task] = {}

# Dashboard Basic Auth (optional - set env vars to enable)
DASHBOARD_USERNAME = os.environ.get("DASHBOARD_USERNAME")
DASHBOARD_PASSWORD = os.environ.get("DASHBOARD_PASSWORD")
DASHBOARD_AUTH_ENABLED = bool(DASHBOARD_USERNAME and DASHBOARD_PASSWORD)

# OAuth 2.1 Configuration for MCP SSE endpoints
OAUTH_SECRET_KEY = os.environ.get("OAUTH_SECRET_KEY", secrets.token_hex(32))
OAUTH_TOKEN_EXPIRY = 86400  # 24 hours
OAUTH_REFRESH_TOKEN_EXPIRY = 2592000  # 30 days
OAUTH_CODE_EXPIRY = 600    # 10 minutes
ALLOWED_REDIRECT_URIS = [
    "https://claude.ai/api/mcp/auth_callback",
    "https://claude.com/api/mcp/auth_callback",
]
# Static OAuth client credentials (set via env vars for security, or auto-generated)
OAUTH_CLIENT_ID = os.environ.get("OAUTH_CLIENT_ID")
OAUTH_CLIENT_SECRET = os.environ.get("OAUTH_CLIENT_SECRET")

# Pricing per 1M tokens (as of 2024/2025)
PRICING = {
    "claude-sonnet-4-20250514": {"input": 3.00, "output": 15.00},
    "claude-sonnet-4-5-20250514": {"input": 3.00, "output": 15.00},
    "claude-3-5-sonnet-20241022": {"input": 3.00, "output": 15.00},
    "claude-3-opus-20240229": {"input": 15.00, "output": 75.00},
    "claude-opus-4-5-20251101": {"input": 5.00, "output": 25.00},
    "claude-3-haiku-20240307": {"input": 0.25, "output": 1.25},
    "claude-haiku-4-5-20250514": {"input": 1.00, "output": 5.00},
    "default": {"input": 3.00, "output": 15.00}  # Fallback to Sonnet pricing
}

def parse_token_usage(output: str) -> dict:
    """Parse token usage and model info from Claude CLI output"""
    result = {
        "input_tokens": 0,
        "output_tokens": 0,
        "total_tokens": 0,
        "cost_usd": 0.0,
        "model": None
    }

    if not output:
        return result

    # Try to find JSON blocks in the output (Claude CLI may wrap JSON in markdown)
    json_blocks = re.findall(r'\{[^{}]*"(?:usage|model)"[^{}]*\}', output, re.DOTALL)
    for block in json_blocks:
        try:
            data = json.loads(block)
            if isinstance(data, dict):
                if "usage" in data:
                    usage = data["usage"]
                    result["input_tokens"] = usage.get("input_tokens", 0)
                    result["output_tokens"] = usage.get("output_tokens", 0)
                if "model" in data:
                    result["model"] = data["model"]
        except json.JSONDecodeError:
            pass

    # Try to parse the whole output as JSON
    try:
        data = json.loads(output.strip())
        if isinstance(data, dict):
            if "usage" in data:
                usage = data["usage"]
                result["input_tokens"] = usage.get("input_tokens", 0)
                result["output_tokens"] = usage.get("output_tokens", 0)
            if "model" in data:
                result["model"] = data["model"]
    except json.JSONDecodeError:
        pass

    # Parse verbose output patterns
    # Look for patterns like "Input tokens: 1234", "input_tokens: 1234", "Tokens in: 1234"
    input_patterns = [
        r'[Ii]nput[_\s]*[Tt]okens[:\s]+(\d[\d,]*)',
        r'[Tt]okens[_\s]*[Ii]n[:\s]+(\d[\d,]*)',
        r'"input_tokens"[:\s]+(\d[\d,]*)',
    ]
    for pattern in input_patterns:
        match = re.search(pattern, output)
        if match:
            result["input_tokens"] = int(match.group(1).replace(',', ''))
            break

    output_patterns = [
        r'[Oo]utput[_\s]*[Tt]okens[:\s]+(\d[\d,]*)',
        r'[Tt]okens[_\s]*[Oo]ut[:\s]+(\d[\d,]*)',
        r'"output_tokens"[:\s]+(\d[\d,]*)',
    ]
    for pattern in output_patterns:
        match = re.search(pattern, output)
        if match:
            result["output_tokens"] = int(match.group(1).replace(',', ''))
            break

    # Look for total tokens
    total_match = re.search(r'[Tt]otal[_\s]*[Tt]okens[:\s]+(\d[\d,]*)', output)
    if total_match:
        result["total_tokens"] = int(total_match.group(1).replace(',', ''))

    # Look for model info
    model_patterns = [
        r'[Mm]odel[:\s]+"?(claude-[a-z0-9-]+)"?',
        r'"model"[:\s]+"(claude-[a-z0-9-]+)"',
        r'Using (claude-[a-z0-9-]+)',
    ]
    for pattern in model_patterns:
        match = re.search(pattern, output)
        if match and not result["model"]:
            result["model"] = match.group(1)
            break

    # Look for direct cost in output (e.g., "Cost: $0.0123" or "Total cost: $0.0123")
    cost_match = re.search(r'[Cc]ost[:\s]+\$?([\d.]+)', output)
    if cost_match:
        try:
            result["cost_usd"] = float(cost_match.group(1))
        except ValueError:
            pass

    # Calculate total if not found
    if result["total_tokens"] == 0:
        result["total_tokens"] = result["input_tokens"] + result["output_tokens"]

    # Calculate cost if not already parsed from output
    if result["cost_usd"] == 0.0 and (result["input_tokens"] > 0 or result["output_tokens"] > 0):
        model = result["model"] or "default"
        pricing = PRICING.get(model, PRICING["default"])

        input_cost = (result["input_tokens"] / 1_000_000) * pricing["input"]
        output_cost = (result["output_tokens"] / 1_000_000) * pricing["output"]
        result["cost_usd"] = round(input_cost + output_cost, 6)

    return result

def render_webhook_template(template: str, payload: dict) -> str:
    """
    Substitute {{payload}} and {{payload.field.subfield}} placeholders in template.

    - {{payload}} -> Full JSON payload as string
    - {{payload.field}} -> Value of payload["field"]
    - {{payload.field.subfield}} -> Nested access payload["field"]["subfield"]
    """
    import re

    def get_nested_value(data: dict, path: str):
        """Get nested value from dict using dot notation"""
        keys = path.split('.')
        value = data
        for key in keys:
            if isinstance(value, dict) and key in value:
                value = value[key]
            else:
                return f"{{{{payload.{path}}}}}"  # Return original if not found
        return value if not isinstance(value, dict) else json.dumps(value)

    # Replace {{payload}} with full JSON
    result = template.replace('{{payload}}', json.dumps(payload, indent=2))

    # Replace {{payload.field.subfield}} patterns
    pattern = r'\{\{payload\.([^}]+)\}\}'
    def replacer(match):
        path = match.group(1)
        return str(get_nested_value(payload, path))

    result = re.sub(pattern, replacer, result)
    return result

def serialize_content_block(block) -> dict:
    """Serialize a Claude Agent SDK content block to JSON-friendly dict"""
    # Handle string input (already serialized or raw text)
    if isinstance(block, str):
        return {"type": "text", "text": block}

    # Handle dict input (already serialized)
    if isinstance(block, dict):
        return block

    # Get block type from .type attribute or class name
    block_type = getattr(block, 'type', None)
    class_name = type(block).__name__

    print(f"[serialize] class_name={class_name}, block_type={block_type}, has_text={hasattr(block, 'text')}", file=sys.stderr)

    # Check by class name (e.g., TextBlock)
    if 'TextBlock' in class_name or block_type == 'text':
        return {
            "type": "text",
            "text": getattr(block, 'text', str(block))
        }
    elif 'ToolUseBlock' in class_name or block_type == 'tool_use':
        # Ensure input is JSON-serializable
        raw_input = getattr(block, 'input', {})
        try:
            # Test if it's serializable
            json.dumps(raw_input)
            safe_input = raw_input
        except (TypeError, ValueError):
            safe_input = str(raw_input)
        return {
            "type": "tool_use",
            "id": getattr(block, 'id', ''),
            "name": getattr(block, 'name', 'unknown'),
            "input": safe_input
        }
    elif 'ToolResultBlock' in class_name or block_type == 'tool_result':
        content = getattr(block, 'content', '')
        # Handle content that might be a list of blocks
        if isinstance(content, list):
            content = '\n'.join(str(c) for c in content)
        return {
            "type": "tool_result",
            "tool_use_id": getattr(block, 'tool_use_id', ''),
            "content": content,
            "is_error": getattr(block, 'is_error', False)
        }
    elif 'ThinkingBlock' in class_name or block_type == 'thinking':
        return {
            "type": "thinking",
            "thinking": getattr(block, 'thinking', str(block))
        }
    else:
        # Fallback for unknown block types - still try to extract text if available
        if hasattr(block, 'text'):
            return {
                "type": "text",
                "text": block.text
            }
        return {
            "type": str(block_type) if block_type else class_name.lower(),
            "raw": str(block)
        }

# === Database Setup ===
def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS jobs (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            cron TEXT NOT NULL,
            prompt TEXT NOT NULL,
            command TEXT DEFAULT 'claude',
            tools TEXT DEFAULT '[]',
            environment TEXT DEFAULT '{}',
            timeout_minutes INTEGER DEFAULT 30,
            enabled INTEGER DEFAULT 1,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            last_executed_at TEXT,
            last_error TEXT
        );
        
        CREATE TABLE IF NOT EXISTS runs (
            id TEXT PRIMARY KEY,
            job_id TEXT NOT NULL,
            started_at TEXT NOT NULL,
            finished_at TEXT,
            prompt TEXT NOT NULL,
            command TEXT NOT NULL,
            output TEXT,
            error TEXT,
            exit_code INTEGER,
            state TEXT DEFAULT 'pending',
            input_tokens INTEGER DEFAULT 0,
            output_tokens INTEGER DEFAULT 0,
            total_tokens INTEGER DEFAULT 0,
            cost_usd REAL DEFAULT 0.0,
            model TEXT,
            FOREIGN KEY (job_id) REFERENCES jobs(id)
        );
        
        CREATE INDEX IF NOT EXISTS idx_runs_job_id ON runs(job_id);
        CREATE INDEX IF NOT EXISTS idx_runs_state ON runs(state);

        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS webhooks (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            description TEXT,
            secret_token TEXT UNIQUE NOT NULL,
            prompt_template TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            last_triggered_at TEXT,
            trigger_count INTEGER DEFAULT 0,
            enabled INTEGER DEFAULT 1
        );

        CREATE INDEX IF NOT EXISTS idx_webhooks_token ON webhooks(secret_token);

        CREATE TABLE IF NOT EXISTS tool_logs (
            id TEXT PRIMARY KEY,
            server_name TEXT NOT NULL,
            tool_name TEXT NOT NULL,
            payload TEXT,
            result TEXT,
            error TEXT,
            status TEXT DEFAULT 'success',
            duration_ms REAL DEFAULT 0,
            created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_tool_logs_created ON tool_logs(created_at);

        -- OAuth 2.1 tables for MCP authentication
        CREATE TABLE IF NOT EXISTS oauth_clients (
            client_id TEXT PRIMARY KEY,
            client_secret_hash TEXT NOT NULL,
            client_name TEXT,
            redirect_uris TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS oauth_codes (
            code TEXT PRIMARY KEY,
            client_id TEXT NOT NULL,
            redirect_uri TEXT NOT NULL,
            code_challenge TEXT,
            code_challenge_method TEXT,
            expires_at TEXT NOT NULL,
            used INTEGER DEFAULT 0
        );

        CREATE INDEX IF NOT EXISTS idx_oauth_codes_client ON oauth_codes(client_id);

        CREATE TABLE IF NOT EXISTS oauth_refresh_tokens (
            token TEXT PRIMARY KEY,
            client_id TEXT NOT NULL,
            issued_at TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            revoked INTEGER DEFAULT 0
        );

        CREATE INDEX IF NOT EXISTS idx_oauth_refresh_tokens_client ON oauth_refresh_tokens(client_id);

        -- Insert default settings if not exists
        INSERT OR IGNORE INTO settings (key, value) VALUES
            ('allowed_tools', '["WebSearch","WebFetch","Read","Write","Edit","Bash"]'),
            ('mcp_servers', '[]'),
            ('mcp_env_vars', '{}'),
            ('custom_mcp_paths', '[]'),
            ('webhook_base_url', '"http://localhost:8080"'),
            ('sandbox_mode', 'true');
    """)

    # Migration: Add new columns to existing tables if they don't exist
    # Jobs table migrations
    try:
        conn.execute("ALTER TABLE jobs ADD COLUMN timeout_minutes INTEGER DEFAULT 30")
    except: pass

    # Runs table migrations
    try:
        conn.execute("ALTER TABLE runs ADD COLUMN input_tokens INTEGER DEFAULT 0")
    except: pass
    try:
        conn.execute("ALTER TABLE runs ADD COLUMN output_tokens INTEGER DEFAULT 0")
    except: pass
    try:
        conn.execute("ALTER TABLE runs ADD COLUMN total_tokens INTEGER DEFAULT 0")
    except: pass
    try:
        conn.execute("ALTER TABLE runs ADD COLUMN cost_usd REAL DEFAULT 0.0")
    except: pass
    try:
        conn.execute("ALTER TABLE runs ADD COLUMN model TEXT")
    except: pass
    try:
        conn.execute("ALTER TABLE runs ADD COLUMN webhook_id TEXT")
    except: pass
    try:
        conn.execute("ALTER TABLE runs ADD COLUMN cache_read_tokens INTEGER DEFAULT 0")
    except: pass
    try:
        conn.execute("ALTER TABLE runs ADD COLUMN cache_creation_tokens INTEGER DEFAULT 0")
    except: pass
    try:
        conn.execute("ALTER TABLE runs ADD COLUMN web_search_requests INTEGER DEFAULT 0")
    except: pass
    conn.commit()
    return conn

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

# === Dynamic MCP Server Helper Functions ===

def _get_dynamic_server_path(name: str) -> Path:
    """Get the directory path for a dynamic MCP server."""
    return DYNAMIC_SERVERS_DIR / name

def _get_dynamic_server_metadata(name: str) -> dict | None:
    """Load metadata for a dynamic MCP server."""
    metadata_path = _get_dynamic_server_path(name) / "metadata.json"
    if metadata_path.exists():
        try:
            return json.loads(metadata_path.read_text())
        except:
            return None
    return None

def _save_dynamic_server_metadata(name: str, metadata: dict) -> None:
    """Save metadata for a dynamic MCP server."""
    server_dir = _get_dynamic_server_path(name)
    server_dir.mkdir(parents=True, exist_ok=True)
    metadata_path = server_dir / "metadata.json"
    metadata_path.write_text(json.dumps(metadata, indent=2))

def _get_mcp_servers_setting() -> list:
    """Load the mcp_servers setting from database."""
    conn = get_db()
    row = conn.execute("SELECT value FROM settings WHERE key = 'mcp_servers'").fetchone()
    conn.close()
    if row:
        try:
            return json.loads(row['value'])
        except:
            return []
    return []

def _save_mcp_servers_setting(servers: list) -> None:
    """Save the mcp_servers setting to database."""
    conn = get_db()
    conn.execute(
        "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
        ('mcp_servers', json.dumps(servers))
    )
    conn.commit()
    conn.close()

def _add_dynamic_server_to_config(name: str) -> None:
    """Add a dynamic server to the mcp_servers configuration."""
    servers = _get_mcp_servers_setting()
    # Remove existing entry with same name if any
    servers = [s for s in servers if s.get('name') != name]
    # Add new entry
    server_path = str(_get_dynamic_server_path(name) / "server.py")
    servers.append({
        "name": name,
        "config": {
            "command": "python",
            "args": [server_path]
        },
        "dynamic": True
    })
    _save_mcp_servers_setting(servers)

def _remove_dynamic_server_from_config(name: str) -> None:
    """Remove a dynamic server from the mcp_servers configuration."""
    servers = _get_mcp_servers_setting()
    servers = [s for s in servers if s.get('name') != name]
    _save_mcp_servers_setting(servers)

def _is_dynamic_server_enabled(name: str) -> bool:
    """Check if a dynamic server is currently enabled in mcp_servers config."""
    servers = _get_mcp_servers_setting()
    return any(s.get('name') == name and s.get('dynamic') for s in servers)

def _list_all_dynamic_servers() -> list:
    """List all dynamic servers from the file system."""
    if not DYNAMIC_SERVERS_DIR.exists():
        return []

    servers = []
    for server_dir in DYNAMIC_SERVERS_DIR.iterdir():
        if server_dir.is_dir() and (server_dir / "server.py").exists():
            name = server_dir.name
            metadata = _get_dynamic_server_metadata(name) or {}
            env_var_defs = metadata.get("env_vars", [])
            cred_status = _check_server_credentials_configured(name)
            servers.append({
                "name": name,
                "description": metadata.get("description", ""),
                "created_at": metadata.get("created_at", ""),
                "file_path": str(server_dir / "server.py"),
                "enabled": _is_dynamic_server_enabled(name),
                "env_vars": env_var_defs,
                "credentials_configured": cred_status["configured"],
                "missing_credentials": cred_status["missing"]
            })
    return servers

# === Credential Management Helper Functions ===

def _get_mcp_env_vars_setting() -> dict:
    """Load the mcp_env_vars setting from database."""
    conn = get_db()
    row = conn.execute("SELECT value FROM settings WHERE key = 'mcp_env_vars'").fetchone()
    conn.close()
    if row:
        try:
            return json.loads(row['value'])
        except:
            return {}
    return {}

def _get_allowed_tools_setting() -> list:
    """Load the allowed_tools setting from database."""
    conn = get_db()
    row = conn.execute("SELECT value FROM settings WHERE key = 'allowed_tools'").fetchone()
    conn.close()
    if row:
        try:
            return json.loads(row['value'])
        except:
            return []
    return []

def _save_mcp_env_vars_setting(env_vars: dict) -> None:
    """Save the mcp_env_vars setting to database."""
    conn = get_db()
    conn.execute(
        "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
        ('mcp_env_vars', json.dumps(env_vars))
    )
    conn.commit()
    conn.close()

def _get_server_credential(server_name: str, var_name: str) -> str | None:
    """Get a specific credential for a server from mcp_env_vars."""
    env_vars = _get_mcp_env_vars_setting()
    # Look for server-prefixed key first, then global
    prefixed_key = f"{server_name}_{var_name}"
    if prefixed_key in env_vars:
        return env_vars[prefixed_key]
    if var_name in env_vars:
        return env_vars[var_name]
    return None

def _set_server_credential(server_name: str, var_name: str, value: str) -> None:
    """Set a credential for a server in mcp_env_vars."""
    env_vars = _get_mcp_env_vars_setting()
    # Store with server prefix for isolation
    prefixed_key = f"{server_name}_{var_name}"
    env_vars[prefixed_key] = value
    _save_mcp_env_vars_setting(env_vars)

def _get_server_env_vars(server_name: str) -> dict:
    """Get all configured env vars for a specific server."""
    env_vars = _get_mcp_env_vars_setting()
    result = {}
    prefix = f"{server_name}_"
    for key, value in env_vars.items():
        if key.startswith(prefix):
            var_name = key[len(prefix):]
            result[var_name] = value
    return result

def _check_server_credentials_configured(server_name: str) -> dict:
    """Check if all required credentials are configured for a server."""
    metadata = _get_dynamic_server_metadata(server_name)
    if not metadata:
        return {"configured": True, "missing": [], "required": []}

    env_var_defs = metadata.get("env_vars", [])
    required_vars = [v["name"] for v in env_var_defs if v.get("required", True)]

    missing = []
    for var_name in required_vars:
        if _get_server_credential(server_name, var_name) is None:
            missing.append(var_name)

    return {
        "configured": len(missing) == 0,
        "missing": missing,
        "required": required_vars,
        "env_vars": env_var_defs
    }

# === Fixed Server Helper Functions ===

def _get_fixed_server_path(name: str) -> Path:
    """Get the directory path for a fixed MCP server."""
    return FIXED_SERVERS_DIR / name

def _get_fixed_server_metadata(name: str) -> dict | None:
    """Load metadata for a fixed MCP server."""
    metadata_path = _get_fixed_server_path(name) / "metadata.json"
    if metadata_path.exists():
        try:
            return json.loads(metadata_path.read_text())
        except:
            return None
    return None

def _is_fixed_server_enabled(name: str) -> bool:
    """Check if a fixed server is currently enabled in mcp_servers config."""
    servers = _get_mcp_servers_setting()
    return any(s.get('name') == name and s.get('fixed') for s in servers)

def _add_fixed_server_to_config(name: str) -> None:
    """Add a fixed server to the mcp_servers configuration."""
    servers = _get_mcp_servers_setting()
    # Remove existing entry with same name if any
    servers = [s for s in servers if s.get('name') != name]
    # Add new entry
    server_path = str(_get_fixed_server_path(name) / "server.py")
    servers.append({
        "name": name,
        "config": {
            "command": "python",
            "args": [server_path]
        },
        "fixed": True
    })
    _save_mcp_servers_setting(servers)

def _remove_fixed_server_from_config(name: str) -> None:
    """Remove a fixed server from the mcp_servers configuration."""
    servers = _get_mcp_servers_setting()
    servers = [s for s in servers if s.get('name') != name]
    _save_mcp_servers_setting(servers)

def _list_all_fixed_servers() -> list:
    """List all fixed servers from the file system."""
    if not FIXED_SERVERS_DIR.exists():
        return []

    servers = []
    for server_dir in FIXED_SERVERS_DIR.iterdir():
        if server_dir.is_dir() and (server_dir / "server.py").exists():
            name = server_dir.name
            metadata = _get_fixed_server_metadata(name) or {}
            env_var_defs = metadata.get("env_vars", [])
            # For fixed servers, check credentials using the server name
            cred_status = _check_fixed_server_credentials(name, env_var_defs)
            servers.append({
                "name": name,
                "description": metadata.get("description", ""),
                "built_in": metadata.get("built_in", True),
                "file_path": str(server_dir / "server.py"),
                "enabled": _is_fixed_server_enabled(name),
                "env_vars": env_var_defs,
                "credentials_configured": cred_status["configured"],
                "missing_credentials": cred_status["missing"]
            })
    return servers

def _check_fixed_server_credentials(name: str, env_var_defs: list) -> dict:
    """Check if credentials are configured for a fixed server."""
    required_vars = [v["name"] for v in env_var_defs if v.get("required", True)]
    missing = []
    for var_name in required_vars:
        if _get_server_credential(name, var_name) is None:
            missing.append(var_name)

    return {
        "configured": len(missing) == 0,
        "missing": missing,
        "required": required_vars
    }

def _auto_register_fixed_servers() -> None:
    """Auto-register all fixed servers on startup."""
    if not FIXED_SERVERS_DIR.exists():
        return

    for server_dir in FIXED_SERVERS_DIR.iterdir():
        if server_dir.is_dir() and (server_dir / "server.py").exists():
            name = server_dir.name
            if not _is_fixed_server_enabled(name):
                _add_fixed_server_to_config(name)
                print(f"[startup] Auto-registered fixed server: {name}", file=sys.stderr)

# === Tool Name Validation Helper ===

# Built-in Claude Code tools that don't need normalization
BUILTIN_TOOLS = {
    "Bash", "Read", "Write", "Edit", "Glob", "Grep",
    "WebFetch", "WebSearch", "Task", "TaskOutput", "TodoWrite",
    "NotebookEdit", "KillShell", "AskUserQuestion", "Skill",
    "EnterPlanMode", "ExitPlanMode"
}

def _normalize_tool_name(tool: str) -> tuple[str, str | None]:
    """
    Validate and normalize a tool name.

    Returns:
        (normalized_name, warning_message)
        - If valid, returns (name, None)
        - If normalized, returns (normalized_name, warning about conversion)
        - If invalid, returns (original, error message)
    """
    tool = tool.strip()

    # Built-in Claude Code tools - no normalization needed
    if tool in BUILTIN_TOOLS:
        return tool, None

    # Already correct MCP format: mcp__<server>__<tool>
    if tool.startswith("mcp__") and "__" in tool[5:]:
        return tool, None

    # Common mistake: server:tool format -> auto-normalize
    if ":" in tool and not tool.startswith("mcp__"):
        parts = tool.split(":", 1)
        if len(parts) == 2 and parts[0] and parts[1]:
            normalized = f"mcp__{parts[0]}__{parts[1]}"
            return normalized, f"Normalized '{tool}' to '{normalized}'"

    # Common mistake: server.tool format -> auto-normalize
    if "." in tool and not tool.startswith("mcp__"):
        parts = tool.split(".", 1)
        if len(parts) == 2 and parts[0] and parts[1]:
            normalized = f"mcp__{parts[0]}__{parts[1]}"
            return normalized, f"Normalized '{tool}' to '{normalized}'"

    # Unknown format - return as-is but warn
    return tool, f"Unknown tool format '{tool}' - expected 'mcp__<server>__<tool>' or built-in tool name"


def _normalize_tools_list(tools_json: str) -> tuple[str, list[str]]:
    """
    Normalize a JSON array of tool names.

    Returns:
        (normalized_json, list_of_warnings)
    """
    try:
        tools = json.loads(tools_json)
        if not isinstance(tools, list):
            return tools_json, [f"Tools must be a JSON array, got {type(tools).__name__}"]
    except json.JSONDecodeError as e:
        return tools_json, [f"Invalid JSON: {e}"]

    normalized_tools = []
    warnings = []

    for tool in tools:
        if not isinstance(tool, str):
            warnings.append(f"Skipped non-string tool: {tool}")
            continue
        normalized, warning = _normalize_tool_name(tool)
        normalized_tools.append(normalized)
        if warning:
            warnings.append(warning)

    return json.dumps(normalized_tools), warnings


def _merge_tool_lists(base_tools: list[str], extra_tools: list[str]) -> list[str]:
    merged = list(base_tools)
    for tool in extra_tools:
        if tool not in merged:
            merged.append(tool)
    return merged


def _filter_builtin_tools(tools: list[str]) -> list[str]:
    return [tool for tool in tools if not tool.startswith("mcp__")]


def _discover_python_mcp_tools(server_path: Path) -> list[str]:
    try:
        source = server_path.read_text(encoding="utf-8")
    except Exception as e:
        print(f"[mcp] Failed reading {server_path}: {e}", file=sys.stderr)
        return []

    try:
        tree = ast.parse(source)
    except SyntaxError as e:
        print(f"[mcp] Failed parsing {server_path}: {e}", file=sys.stderr)
        return []

    tool_names = []
    for node in tree.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for decorator in node.decorator_list:
            target = decorator.func if isinstance(decorator, ast.Call) else decorator
            if (
                isinstance(target, ast.Attribute)
                and isinstance(target.value, ast.Name)
                and target.value.id == "mcp"
                and target.attr == "tool"
            ):
                tool_names.append(node.name)
                break

    return tool_names


def _load_mcp_tool_from_file(server_name: str, server_path: Path, tool_name: str):
    """Load a tool callable from a server.py file."""
    if not server_path.exists():
        return None, f"Server file not found: {server_path}"

    module_name = f"_mcp_tool_{server_name}_{uuid.uuid4().hex}"
    spec = importlib.util.spec_from_file_location(module_name, server_path)
    if spec is None or spec.loader is None:
        return None, f"Failed to load module for {server_name}"

    module = importlib.util.module_from_spec(spec)
    sys.path.insert(0, str(server_path.parent))
    try:
        spec.loader.exec_module(module)
    except Exception as e:
        return None, f"Failed importing server '{server_name}': {e}"
    finally:
        if sys.path and sys.path[0] == str(server_path.parent):
            sys.path.pop(0)

    if not hasattr(module, tool_name):
        return None, f"Tool '{tool_name}' not found in server '{server_name}'"

    tool_obj = getattr(module, tool_name)

    # FastMCP's @mcp.tool() decorator wraps functions in FunctionTool objects.
    # Extract the original callable via .fn attribute.
    if hasattr(tool_obj, 'fn') and callable(getattr(tool_obj, 'fn', None)):
        return tool_obj.fn, None

    if callable(tool_obj):
        return tool_obj, None

    return None, f"Tool '{tool_name}' in server '{server_name}' is not callable"


def _discover_mcp_tool_allowlist(mcp_config: dict[str, dict]) -> list[str]:
    allowlist = []

    for server_name, config in mcp_config.items():
        if not isinstance(config, dict):
            continue

        command = config.get("command")
        args = config.get("args") or []
        server_path = None

        if isinstance(command, str) and command.startswith("python") and isinstance(args, list):
            for arg in args:
                if isinstance(arg, str) and arg.endswith(".py"):
                    server_path = Path(arg)
                    if not server_path.is_absolute():
                        server_path = (Path(__file__).parent / server_path).resolve()
                    break

        if server_path and server_path.exists():
            tool_names = _discover_python_mcp_tools(server_path)
            allowlist.extend([f"mcp__{server_name}__{name}" for name in tool_names])
        else:
            print(f"[mcp] Tool discovery skipped for '{server_name}' (non-python or missing path)", file=sys.stderr)

    return allowlist


# === OAuth 2.1 Helper Functions ===

def hash_client_secret(secret: str) -> str:
    """Hash a client secret using SHA256."""
    return hashlib.sha256(secret.encode()).hexdigest()

def verify_client_secret(secret: str, secret_hash: str) -> bool:
    """Verify a client secret against its hash."""
    return secrets.compare_digest(hash_client_secret(secret), secret_hash)

def generate_oauth_jwt(client_id: str, scopes: list[str] = None) -> str:
    """Generate a signed JWT access token."""
    if not JWT_AVAILABLE:
        raise RuntimeError("PyJWT not installed")

    now = datetime.now(timezone.utc)
    payload = {
        "iss": "claude-runner",
        "sub": client_id,
        "aud": "claude-runner-mcp",
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(seconds=OAUTH_TOKEN_EXPIRY)).timestamp()),
        "scope": " ".join(scopes or ["mcp"]),
    }
    return jwt.encode(payload, OAUTH_SECRET_KEY, algorithm="HS256")

def generate_refresh_token(client_id: str) -> tuple[str, str]:
    """Generate a refresh token. Returns (token, expires_at)."""
    token = secrets.token_urlsafe(64)
    now = datetime.now(timezone.utc)
    expires_at = (now + timedelta(seconds=OAUTH_REFRESH_TOKEN_EXPIRY)).isoformat()

    conn = get_db()
    conn.execute("""
        INSERT INTO oauth_refresh_tokens (token, client_id, issued_at, expires_at)
        VALUES (?, ?, ?, ?)
    """, (token, client_id, now.isoformat(), expires_at))
    conn.commit()
    conn.close()

    return token, expires_at

def verify_oauth_jwt(token: str) -> dict | None:
    """Verify and decode a JWT access token. Returns claims or None if invalid."""
    if not JWT_AVAILABLE:
        return None

    try:
        claims = jwt.decode(
            token,
            OAUTH_SECRET_KEY,
            algorithms=["HS256"],
            audience="claude-runner-mcp",
        )
        return claims
    except jwt.ExpiredSignatureError:
        return None
    except jwt.InvalidTokenError:
        return None

def verify_pkce(code_verifier: str, code_challenge: str, method: str = "S256") -> bool:
    """Verify PKCE code_verifier against code_challenge."""
    if method == "S256":
        # SHA256 hash, then base64url encode (no padding)
        digest = hashlib.sha256(code_verifier.encode("ascii")).digest()
        computed = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
        return secrets.compare_digest(computed, code_challenge)
    elif method == "plain":
        return secrets.compare_digest(code_verifier, code_challenge)
    return False

def get_server_url() -> str:
    """Get the public server URL (PUBLIC_URL, ngrok, or localhost)."""
    public_url = os.environ.get("PUBLIC_URL")
    if public_url:
        return public_url.rstrip("/")
    if NGROK_PUBLIC_URL:
        return NGROK_PUBLIC_URL
    return "http://localhost:8080"

def get_webhook_base_url() -> str:
    """Get base URL for webhook endpoints (PUBLIC_URL > ngrok > DB setting > localhost)."""
    public_url = os.environ.get("PUBLIC_URL")
    if public_url:
        return public_url.rstrip("/")
    if NGROK_PUBLIC_URL:
        return NGROK_PUBLIC_URL
    # Fall back to DB setting
    try:
        conn = get_db()
        row = conn.execute("SELECT value FROM settings WHERE key = 'webhook_base_url'").fetchone()
        conn.close()
        if row:
            return json.loads(row['value'])
    except:
        pass
    return "http://localhost:8080"

def ensure_oauth_client() -> tuple[str, str]:
    """Ensure the OAuth client exists. Returns (client_id, client_secret).

    If OAUTH_CLIENT_ID and OAUTH_CLIENT_SECRET env vars are set, uses those.
    Otherwise, generates new credentials and stores them in the database.
    Returns the plaintext secret (only shown once at startup).
    """
    global OAUTH_CLIENT_ID, OAUTH_CLIENT_SECRET

    conn = get_db()

    # Check if we have env vars
    if OAUTH_CLIENT_ID and OAUTH_CLIENT_SECRET:
        # Use provided credentials - ensure they're in the database
        existing = conn.execute("SELECT * FROM oauth_clients WHERE client_id = ?", (OAUTH_CLIENT_ID,)).fetchone()
        if not existing:
            conn.execute("""
                INSERT INTO oauth_clients (client_id, client_secret_hash, client_name, redirect_uris, created_at)
                VALUES (?, ?, ?, ?, ?)
            """, (OAUTH_CLIENT_ID, hash_client_secret(OAUTH_CLIENT_SECRET), "Claude (env)",
                  json.dumps(ALLOWED_REDIRECT_URIS), utc_now_iso()))
            conn.commit()
        conn.close()
        return OAUTH_CLIENT_ID, OAUTH_CLIENT_SECRET

    # Check if we already have a client in the database
    existing = conn.execute("SELECT * FROM oauth_clients WHERE client_name = 'Claude (auto)'").fetchone()
    if existing:
        conn.close()
        # We can't recover the secret, user needs to check their .env or regenerate
        OAUTH_CLIENT_ID = existing["client_id"]
        return existing["client_id"], None

    # Generate new credentials
    client_id = secrets.token_urlsafe(16)
    client_secret = secrets.token_urlsafe(32)

    conn.execute("""
        INSERT INTO oauth_clients (client_id, client_secret_hash, client_name, redirect_uris, created_at)
        VALUES (?, ?, ?, ?, ?)
    """, (client_id, hash_client_secret(client_secret), "Claude (auto)",
          json.dumps(ALLOWED_REDIRECT_URIS), utc_now_iso()))
    conn.commit()
    conn.close()

    OAUTH_CLIENT_ID = client_id
    OAUTH_CLIENT_SECRET = client_secret
    return client_id, client_secret

# === MCP Server ===
mcp = FastMCP("Claude Runner")

# === Web Dashboard ===
from starlette.responses import HTMLResponse, JSONResponse, Response, FileResponse
from starlette.routing import Route
from functools import wraps

def check_basic_auth(request) -> Response | None:
    """Check basic auth if enabled. Returns 401 Response if auth fails, None if OK."""
    if not DASHBOARD_AUTH_ENABLED:
        return None

    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Basic "):
        return Response(
            "Authentication required",
            status_code=401,
            headers={"WWW-Authenticate": 'Basic realm="Claude Runner Dashboard"'}
        )

    try:
        encoded = auth_header[6:]  # Remove "Basic " prefix
        decoded = base64.b64decode(encoded).decode("utf-8")
        username, password = decoded.split(":", 1)

        # Use constant-time comparison to prevent timing attacks
        username_ok = secrets.compare_digest(username, DASHBOARD_USERNAME)
        password_ok = secrets.compare_digest(password, DASHBOARD_PASSWORD)

        if username_ok and password_ok:
            return None  # Auth successful
    except Exception:
        pass

    return Response(
        "Invalid credentials",
        status_code=401,
        headers={"WWW-Authenticate": 'Basic realm="Claude Runner Dashboard"'}
    )

def require_auth(handler):
    """Decorator to require basic auth for a route handler."""
    @wraps(handler)
    async def wrapper(request, *args, **kwargs):
        auth_response = check_basic_auth(request)
        if auth_response:
            return auth_response
        return await handler(request, *args, **kwargs)
    return wrapper

DASHBOARD_HTML = """
<!DOCTYPE html>
<html>
<head>
    <title>Claude Runner Dashboard</title>
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <script src="https://cdn.jsdelivr.net/npm/marked/marked.min.js"></script>
    <link rel="stylesheet" href="/static/dashboard.css">
</head>
<body>
    <div class="container">
        <h1>Claude Runner Dashboard</h1>

        <div class="cost-overview" id="cost-overview">
            <div class="cost-card highlight">
                <h4>Today's Cost</h4>
                <p class="value" id="cost-today">$0.00</p>
                <p class="subtext" id="runs-today">0 runs</p>
            </div>
            <div class="cost-card">
                <h4>This Week</h4>
                <p class="value" id="cost-week">$0.00</p>
                <p class="subtext" id="runs-week">0 runs</p>
            </div>
            <div class="cost-card">
                <h4>Total Spent</h4>
                <p class="value" id="cost-total">$0.00</p>
                <p class="subtext" id="runs-total">0 runs</p>
            </div>
            <div class="cost-card">
                <h4>Total Tokens</h4>
                <p class="value" id="tokens-total">0</p>
                <div class="token-bar"><div class="token-bar-fill" id="token-bar-fill" style="width: 0%"></div></div>
                <p class="subtext"><span id="tokens-input">0</span> in / <span id="tokens-output">0</span> out</p>
            </div>
            <div class="cost-card" id="ngrok-card" style="display: none;">
                <h4>Remote Access</h4>
                <p class="value" style="font-size: 14px; word-break: break-all;" id="ngrok-url">-</p>
                <p class="subtext"><a href="#" id="ngrok-copy" onclick="copyNgrokUrl(event)" style="color: var(--accent-blue); cursor: pointer;">Copy SSE URL</a></p>
            </div>
        </div>

        <div class="quick-run">
            <h3>Run Custom Prompt</h3>
            <div class="quick-run-form">
                <input type="text" id="quick-prompt" class="quick-run-input" placeholder="Enter a prompt to run immediately..." onkeydown="if(event.key==='Enter')runQuickPrompt()">
                <button class="quick-run-btn" id="quick-run-btn" onclick="runQuickPrompt()">Run Now</button>
            </div>
        </div>

        <div style="display: flex; gap: var(--spacing-sm); align-items: center; margin-bottom: var(--spacing-lg);">
            <button class="refresh-btn" onclick="refresh()">Refresh</button>
            <span id="last-update" style="color: var(--text-muted); font-size: 12px;"></span>
        </div>

        <div class="tabs">
            <button class="tab active" onclick="showTab('manual-runs')">Manual Runs</button>
            <button class="tab" onclick="showTab('scheduled')">Scheduled</button>
            <button class="tab" onclick="showTab('webhooks')">Webhooks</button>
            <button class="tab" onclick="showTab('tool-logs')">Tool Logs</button>
            <button class="tab" onclick="showTab('settings')">Settings</button>
        </div>

        <div id="manual-runs" class="section active">
            <div class="card">
                <h2>Manual Runs</h2>
                <p style="color: var(--text-secondary); margin-bottom: var(--spacing-md); font-size: 13px;">User-triggered runs from the quick run form above.</p>
                <div class="run-cards" id="manual-runs-list">
                    <!-- Manual runs will be populated by JavaScript -->
                </div>
            </div>
        </div>

        <div id="scheduled" class="section">
            <div class="card">
                <h2>Scheduled Jobs</h2>
                <p style="color: var(--text-secondary); margin-bottom: var(--spacing-md); font-size: 13px;">Cron-based automated tasks.</p>
                <div class="table-wrapper">
                    <table>
                        <thead>
                            <tr><th>ID</th><th>Name</th><th>Cron</th><th>Status</th><th>Last Run</th><th>Prompt</th><th>Actions</th></tr>
                        </thead>
                        <tbody id="jobs-table"></tbody>
                    </table>
                </div>
            </div>
            <div class="card" style="margin-top: var(--spacing-lg);">
                <h2>Scheduled Run History</h2>
                <div class="table-wrapper">
                    <table>
                        <thead>
                            <tr><th>ID</th><th>Job</th><th>Started</th><th>Duration</th><th>Tokens</th><th>Cost</th><th>State</th><th>Output</th></tr>
                        </thead>
                        <tbody id="scheduled-runs-table"></tbody>
                    </table>
                </div>
            </div>
        </div>

        <div id="webhooks" class="section">
            <div class="card">
                <h2>Webhook Endpoints</h2>
                <p style="color: var(--text-secondary); margin-bottom: var(--spacing-md); font-size: 13px;">Webhooks allow external services to trigger Claude prompts via HTTP POST requests.</p>
                <div class="table-wrapper">
                    <table>
                        <thead>
                            <tr><th>Name</th><th>URL</th><th>Triggers</th><th>Last Triggered</th><th>Status</th><th>Actions</th></tr>
                        </thead>
                        <tbody id="webhooks-table"></tbody>
                    </table>
                </div>
            </div>
            <div class="card" style="margin-top: var(--spacing-lg);">
                <h2>Webhook Run History</h2>
                <p style="color: var(--text-secondary); margin-bottom: var(--spacing-md); font-size: 13px;">
                    Runs triggered by incoming webhook HTTP POST requests.
                </p>
                <div class="table-wrapper">
                    <table>
                        <thead>
                            <tr><th>ID</th><th>Webhook</th><th>Started</th><th>Duration</th><th>Tokens</th><th>Cost</th><th>State</th><th>Output</th></tr>
                        </thead>
                        <tbody id="webhook-runs-table"></tbody>
                    </table>
                </div>
            </div>
        </div>

        <div id="tool-logs" class="section">
            <div class="card">
                <h2>Tool Invocation Logs</h2>
                <p style="color: var(--text-secondary); margin-bottom: var(--spacing-md); font-size: 13px;">Recent invocations of internal MCP tools via invoke_internal_mcp_tool.</p>
                <div class="table-wrapper">
                    <table>
                        <thead>
                            <tr><th>Server</th><th>Tool</th><th>Status</th><th>Duration</th><th>Time</th><th>Actions</th></tr>
                        </thead>
                        <tbody id="tool-logs-table"></tbody>
                    </table>
                </div>
            </div>
        </div>

        <div id="settings" class="section">
            <div class="card">
                <h2>Tool & MCP Settings</h2>
                <div class="settings-grid">
                    <div class="settings-section">
                        <h3>Built-in Tools</h3>
                        <div class="tool-grid" id="tools-grid">
                            <!-- Tools will be populated by JavaScript -->
                        </div>
                    </div>
                    <div class="settings-section" style="grid-column: 1 / -1;">
                        <h3>Fixed Servers</h3>
                        <p style="font-size: 13px; color: var(--text-secondary); margin-bottom: var(--spacing-md);">Built-in MCP servers that provide core functionality. Configure credentials as needed.</p>
                        <div class="mcp-cards-grid" id="fixed-servers-grid">
                            <div class="mcp-loading">Loading fixed servers...</div>
                        </div>
                    </div>
                    <div class="settings-section" style="grid-column: 1 / -1;">
                        <h3>Dynamic Servers</h3>
                        <p style="font-size: 13px; color: var(--text-secondary); margin-bottom: var(--spacing-md);">User-created MCP servers. Configure credentials for servers that require them.</p>
                        <div class="mcp-cards-grid" id="dynamic-servers-grid">
                            <div class="mcp-loading">Loading dynamic servers...</div>
                        </div>
                    </div>
                    <div class="settings-section" style="grid-column: 1 / -1;">
                        <h3>External MCP Plugins</h3>
                        <p style="font-size: 13px; color: var(--text-secondary); margin-bottom: var(--spacing-md);">Enable MCP servers to extend Claude's capabilities. Servers are auto-discovered from your Claude Code installation and custom paths.</p>
                        <div class="mcp-cards-grid" id="mcp-cards">
                            <div class="mcp-loading">Loading available MCP servers...</div>
                        </div>
                        <div style="margin-top: var(--spacing-md); padding-top: var(--spacing-md); border-top: 1px solid var(--border-default);">
                            <h4 style="font-size: 13px; font-weight: 600; margin-bottom: var(--spacing-sm); color: var(--text-primary);">Custom MCP Paths</h4>
                            <p style="font-size: 12px; color: var(--text-muted); margin-bottom: var(--spacing-sm);">Add paths to local MCP server directories (containing mcp.json).</p>
                            <div id="custom-paths-list" style="display: flex; flex-direction: column; gap: var(--spacing-sm); margin-bottom: var(--spacing-sm);"></div>
                            <div style="display: flex; gap: var(--spacing-sm);">
                                <button class="add-mcp-btn" onclick="addCustomPath()" style="font-size: 12px; padding: 6px 12px;">+ Add Custom Path</button>
                                <button class="add-mcp-btn" onclick="reloadMcpServers()" style="font-size: 12px; padding: 6px 12px; background: var(--bg-elevated);">Reload MCP Servers</button>
                            </div>
                        </div>
                    </div>
                    <div class="settings-section" style="grid-column: 1 / -1;">
                        <h3>Security</h3>
                        <p style="font-size: 13px; color: var(--text-secondary); margin-bottom: var(--spacing-md);">Control how scheduled jobs access files and system resources.</p>
                        <div style="display: flex; align-items: center; gap: var(--spacing-sm);">
                            <label style="display: flex; align-items: center; gap: var(--spacing-sm); cursor: pointer;">
                                <input type="checkbox" id="sandbox-mode" style="width: 18px; height: 18px; accent-color: var(--accent-blue);" onchange="toggleSandbox()">
                                <span style="font-weight: 500; color: var(--text-primary);">Sandbox Mode</span>
                            </label>
                            <span style="font-size: 13px; color: var(--text-muted);">(Restrict file access to claude_playground directory only)</span>
                        </div>
                    </div>
                </div>
                <div style="margin-top: var(--spacing-lg);">
                    <button class="save-settings-btn" onclick="saveSettings()">Save Settings</button>
                </div>
            </div>
        </div>
    </div>

    <!-- Output Modal -->
    <div id="output-modal" class="modal">
        <div class="modal-content">
            <div class="modal-header">
                <h3 id="modal-title">Run Details</h3>
                <button class="modal-close" onclick="closeModal()">&times;</button>
            </div>
            <div class="modal-body" id="modal-body"></div>
        </div>
    </div>

    <!-- Toast Container -->
    <div class="toast-container" id="toast-container"></div>

    <!-- Confirmation Modal -->
    <div id="confirm-modal" class="confirm-modal">
        <div class="confirm-content">
            <h4 class="confirm-title" id="confirm-title">Confirm Action</h4>
            <p class="confirm-message" id="confirm-message">Are you sure?</p>
            <div class="confirm-buttons">
                <button class="confirm-btn confirm-btn-cancel" onclick="closeConfirm()">Cancel</button>
                <button class="confirm-btn confirm-btn-danger" id="confirm-action-btn">Confirm</button>
            </div>
        </div>
    </div>

    <script>
        let jobsData = [];
        let runsData = [];
        let webhooksData = [];
        let toolLogsData = [];
        let livePollingInterval = null;
        let currentRunId = null;
        let confirmCallback = null;

        // ========================================
        // TOAST NOTIFICATIONS
        // ========================================
        function showToast(message, type = 'info', duration = 4000) {
            const container = document.getElementById('toast-container');
            const toast = document.createElement('div');
            toast.className = `toast toast-${type}`;
            toast.innerHTML = `
                <span class="toast-message">${escapeHtml(message)}</span>
                <button class="toast-close" onclick="this.parentElement.remove()">&times;</button>
            `;
            container.appendChild(toast);

            if (duration > 0) {
                setTimeout(() => {
                    toast.classList.add('hiding');
                    setTimeout(() => toast.remove(), 300);
                }, duration);
            }
        }

        // ========================================
        // CONFIRMATION MODAL
        // ========================================
        function showConfirm(title, message, callback) {
            document.getElementById('confirm-title').textContent = title;
            document.getElementById('confirm-message').textContent = message;
            confirmCallback = callback;
            document.getElementById('confirm-modal').classList.add('active');
        }

        function closeConfirm() {
            document.getElementById('confirm-modal').classList.remove('active');
            confirmCallback = null;
        }

        document.getElementById('confirm-action-btn').addEventListener('click', () => {
            if (confirmCallback) {
                confirmCallback();
            }
            closeConfirm();
        });

        // ========================================
        // TAB NAVIGATION
        // ========================================
        function showTab(tab) {
            document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
            document.querySelectorAll('.section').forEach(s => s.classList.remove('active'));
            document.querySelector(`[onclick="showTab('${tab}')"]`).classList.add('active');
            document.getElementById(tab).classList.add('active');
        }

        function escapeHtml(text) {
            const div = document.createElement('div');
            div.textContent = text;
            return div.innerHTML;
        }

        function copyToClipboard(text) {
            navigator.clipboard.writeText(text).then(() => {
                showToast('Copied to clipboard!', 'success');
            }).catch(err => {
                console.error('Failed to copy:', err);
                prompt('Copy this URL:', text);
            });
        }

        function deleteJob(jobId, jobName) {
            showConfirm('Delete Job', `Are you sure you want to delete "${jobName}"? This will also delete all associated runs.`, async () => {
                try {
                    const res = await fetch(`/api/job/${jobId}`, { method: 'DELETE' });
                    const data = await res.json();
                    if (data.success) {
                        showToast('Job deleted successfully', 'success');
                        refresh();
                    } else {
                        showToast('Failed to delete job: ' + (data.error || 'Unknown error'), 'error');
                    }
                } catch (e) {
                    showToast('Error deleting job: ' + e.message, 'error');
                }
            });
        }

        function killRun(runId) {
            showConfirm('Kill Run', 'Are you sure you want to kill this running job?', async () => {
                try {
                    const res = await fetch(`/api/run/${runId}/kill`, { method: 'POST' });
                    const data = await res.json();
                    if (data.success) {
                        showToast('Run terminated', 'success');
                        closeModal();
                        refresh();
                    } else {
                        showToast('Failed to kill run: ' + (data.error || 'Unknown error'), 'error');
                    }
                } catch (e) {
                    showToast('Error killing run: ' + e.message, 'error');
                }
            });
        }

        function viewWebhookPrompt(webhookId) {
            const webhook = webhooksData.find(w => w.id === webhookId);
            if (!webhook) {
                showToast('Webhook not found', 'error');
                return;
            }
            showWebhookPrompt(webhookId, webhook.prompt_template);
        }

        function showWebhookPrompt(webhookId, promptTemplate) {
            document.getElementById('modal-title').textContent = 'Webhook Prompt Template';
            document.getElementById('modal-body').innerHTML = `
                <div class="modal-section">
                    <h4>Prompt Template</h4>
                    <pre style="white-space: pre-wrap; max-height: 400px; overflow-y: auto;">${escapeHtml(promptTemplate)}</pre>
                </div>
                <div style="margin-top: var(--spacing-md); padding: var(--spacing-md); background: rgba(59, 130, 246, 0.1); border-left: 3px solid var(--accent-blue);">
                    <strong style="color: var(--text-primary);">Template Variables:</strong><br>
                    <code style="color: var(--accent-cyan);">{{payload}}</code> <span style="color: var(--text-secondary);">- Full JSON payload</span><br>
                    <code style="color: var(--accent-cyan);">{{payload.field}}</code> <span style="color: var(--text-secondary);">- Specific field from payload</span><br>
                    <code style="color: var(--accent-cyan);">{{payload.field.subfield}}</code> <span style="color: var(--text-secondary);">- Nested field access</span>
                </div>
            `;
            document.getElementById('output-modal').classList.add('active');
        }

        function showWebhookRuns(webhookId) {
            // Switch to webhooks tab if not already there
            document.querySelectorAll('.nav-item').forEach(n => n.classList.remove('active'));
            document.querySelectorAll('.section').forEach(s => s.classList.remove('active'));
            const webhooksNav = document.querySelector('.nav-item[onclick*="webhooks"]');
            if (webhooksNav) webhooksNav.classList.add('active');
            document.getElementById('webhooks').classList.add('active');

            // Scroll to the webhook run history table
            const table = document.getElementById('webhook-runs-table');
            if (table) {
                table.closest('.card').scrollIntoView({ behavior: 'smooth', block: 'start' });
            }

            // Highlight matching rows briefly
            const rows = table ? table.querySelectorAll('tr[data-webhook-id]') : [];
            rows.forEach(row => {
                row.style.transition = 'background-color 0.3s';
                if (row.getAttribute('data-webhook-id') === webhookId) {
                    row.style.backgroundColor = 'rgba(59, 130, 246, 0.15)';
                    setTimeout(() => { row.style.backgroundColor = ''; }, 2000);
                }
            });
        }

        function renderMarkdown(text) {
            if (!text) return '';
            try {
                marked.setOptions({
                    breaks: true,
                    gfm: true,
                });
                return marked.parse(text);
            } catch {
                return escapeHtml(text);
            }
        }

        function parseOutput(output) {
            if (!output) return null;
            try {
                return JSON.parse(output);
            } catch {
                return null;
            }
        }

        function renderExecutionTrace(outputJson) {
            if (!outputJson) return '<div class="text-muted">No output</div>';

            let blocks;
            try {
                blocks = JSON.parse(outputJson);
            } catch {
                // Fallback for old format or plain text
                return '<pre>' + escapeHtml(outputJson) + '</pre>';
            }

            if (!Array.isArray(blocks)) {
                return '<pre>' + escapeHtml(outputJson) + '</pre>';
            }

            const html = blocks.map((block) => {
                switch (block.type) {
                    case 'text':
                        return '<div class="trace-block text">' +
                            '<div class="block-header">' +
                            '<svg viewBox="0 0 16 16" fill="currentColor"><path d="M8 0a8 8 0 100 16A8 8 0 008 0zM4.5 7.5a.5.5 0 000 1h5.793l-2.147 2.146a.5.5 0 00.708.708l3-3a.5.5 0 000-.708l-3-3a.5.5 0 10-.708.708L10.293 7.5H4.5z"/></svg>' +
                            'Claude</div>' +
                            '<div class="trace-content markdown-content">' + renderMarkdown(block.text) + '</div>' +
                            '</div>';

                    case 'tool_use':
                        const inputHtml = Object.entries(block.input || {})
                            .map(([key, val]) => {
                                const valStr = typeof val === 'object' ? JSON.stringify(val, null, 2) : String(val);
                                const isLong = valStr.length > 200;
                                const displayVal = isLong ? valStr.substring(0, 200) + '...' : valStr;
                                return '<div class="tool-input-key">' + escapeHtml(key) + ':</div>' +
                                    '<div class="tool-input-value' + (isLong ? ' long' : '') + '">' + escapeHtml(displayVal) + '</div>';
                            }).join('');

                        return '<div class="trace-block tool-use">' +
                            '<div class="tool-header">' +
                            '<svg viewBox="0 0 16 16"><path d="M1 0L0 1l2.2 3.081a1 1 0 00.815.419h.07a1 1 0 01.708.293l2.675 2.675-2.617 2.654A3.003 3.003 0 000 13a3 3 0 105.878-.851l2.654-2.617.968.968-.305.914a1 1 0 00.242 1.023l3.27 3.27a.997.997 0 001.414 0l1.586-1.586a.997.997 0 000-1.414l-3.27-3.27a1 1 0 00-1.023-.242l-.914.305-.968-.968 2.617-2.654A3.003 3.003 0 0013 0a3 3 0 10-.851 5.878L9.495 8.53 6.82 5.854a1 1 0 01-.293-.708v-.07a1 1 0 00-.419-.815L3.082 2l2.183-2.183L3.851-1.597 1 0z"/></svg>' +
                            'Tool: ' + escapeHtml(block.name) +
                            '</div>' +
                            '<div class="tool-params"><div class="tool-input-grid">' + inputHtml + '</div></div>' +
                            '</div>';

                    case 'tool_result':
                        const isError = block.is_error === true;
                        const content = typeof block.content === 'object'
                            ? JSON.stringify(block.content, null, 2)
                            : String(block.content || '');

                        return '<div class="trace-block tool-result' + (isError ? ' error' : '') + '">' +
                            '<details class="tool-result-details">' +
                            '<summary class="block-header">' + (isError ? 'Error (click to expand)' : 'Result (click to expand)') + '</summary>' +
                            '<div class="trace-content"><pre>' + escapeHtml(content) + '</pre></div>' +
                            '</details>' +
                            '</div>';

                    case 'result':
                        return '<div class="trace-block result">' +
                            '<div class="block-header">' +
                            '<svg viewBox="0 0 16 16" fill="currentColor"><path d="M13.854 3.646a.5.5 0 010 .708l-7 7a.5.5 0 01-.708 0l-3.5-3.5a.5.5 0 11.708-.708L6.5 10.293l6.646-6.647a.5.5 0 01.708 0z"/></svg>' +
                            'Final Result</div>' +
                            '<div class="trace-content markdown-content">' + renderMarkdown(block.result) + '</div>' +
                            '</div>';

                    case 'thinking':
                        return '<div class="trace-block thinking">' +
                            '<div class="block-header">Thinking</div>' +
                            '<div class="trace-content">' + escapeHtml(block.thinking || '') + '</div>' +
                            '</div>';

                    default:
                        return '<div class="trace-block text">' +
                            '<pre>' + escapeHtml(JSON.stringify(block, null, 2)) + '</pre>' +
                            '</div>';
                }
            }).join('');

            return '<div class="execution-trace">' + html + '</div>';
        }

        function getResultSummary(output, error) {
            if (error) return { type: 'error', text: error.substring(0, 100) };
            if (!output) return { type: 'none', text: 'No output' };

            const parsed = parseOutput(output);

            // New format: array of blocks
            if (Array.isArray(parsed)) {
                // Look for result block first
                const resultBlock = parsed.find(b => b.type === 'result');
                if (resultBlock && resultBlock.result) {
                    const text = String(resultBlock.result);
                    return { type: 'success', text: text.substring(0, 100) + (text.length > 100 ? '...' : '') };
                }
                // Fallback to last text block
                const textBlocks = parsed.filter(b => b.type === 'text');
                if (textBlocks.length > 0) {
                    const lastText = textBlocks[textBlocks.length - 1].text || '';
                    return { type: 'success', text: lastText.substring(0, 100) + (lastText.length > 100 ? '...' : '') };
                }
                // Show tool count if only tools
                const toolCount = parsed.filter(b => b.type === 'tool_use').length;
                if (toolCount > 0) {
                    return { type: 'raw', text: toolCount + ' tool call(s)' };
                }
            }

            // Old format: object with result field
            if (parsed && parsed.result) {
                const text = typeof parsed.result === 'string' ? parsed.result : JSON.stringify(parsed.result);
                return { type: 'success', text: text.substring(0, 100) + (text.length > 100 ? '...' : '') };
            }

            return { type: 'raw', text: output.substring(0, 100) + (output.length > 100 ? '...' : '') };
        }

        function formatDuration(started, finished) {
            if (!finished) return 'Running...';
            const ms = new Date(finished) - new Date(started);
            if (ms < 1000) return ms + 'ms';
            if (ms < 60000) return (ms / 1000).toFixed(1) + 's';
            return (ms / 60000).toFixed(1) + 'm';
        }

        function renderRunModal(run) {
            const job = jobsData.find(j => j.id === run.job_id);
            const webhook = run.webhook_id ? webhooksData.find(w => w.id === run.webhook_id) : null;
            const parsed = parseOutput(run.output);
            const isLive = run.state === 'running' || run.state === 'pending';

            // Build error section if there's an error
            let errorHtml = '';
            if (run.error) {
                errorHtml = `<div class="modal-section">
                    <h4>Error</h4>
                    <div class="result-text error-text">${escapeHtml(run.error)}</div>
                </div>`;
            }

            const liveIndicator = isLive ? `<span class="live-badge"><span class="live-dot"></span>LIVE</span>` : '';
            const stateDisplay = isLive ? `<span class="spinner"></span>${run.state}` : run.state;

            const tokens = run.total_tokens || 0;
            const cost = run.cost_usd || 0;
            const model = run.model || '-';
            const inputTokens = run.input_tokens || 0;
            const outputTokens = run.output_tokens || 0;
            const cacheReadTokens = run.cache_read_tokens || 0;
            const cacheCreationTokens = run.cache_creation_tokens || 0;
            const webSearchRequests = run.web_search_requests || 0;

            const modalTitle = document.getElementById('modal-title');
            modalTitle.textContent = '';
            modalTitle.appendChild(document.createTextNode((job ? job.name : 'Run ' + run.id) + ' '));
            if (isLive) {
                const liveBadge = document.createElement('span');
                liveBadge.className = 'live-badge';
                const liveDot = document.createElement('span');
                liveDot.className = 'live-dot';
                liveBadge.appendChild(liveDot);
                liveBadge.appendChild(document.createTextNode('LIVE'));
                modalTitle.appendChild(liveBadge);
            }
            document.getElementById('modal-body').innerHTML = `
                <div class="modal-section">
                    <div class="meta-grid">
                        <div class="meta-item">
                            <label>Run ID</label>
                            <span>${run.id}</span>
                        </div>
                        <div class="meta-item">
                            <label>State</label>
                            <span class="status ${run.state}">${stateDisplay}</span>
                        </div>
                        <div class="meta-item">
                            <label>Started</label>
                            <span>${new Date(run.started_at).toLocaleString()}</span>
                        </div>
                        <div class="meta-item">
                            <label>Duration</label>
                            <span id="duration-display">${formatDuration(run.started_at, run.finished_at)}</span>
                        </div>
                        <div class="meta-item">
                            <label>Exit Code</label>
                            <span>${run.exit_code ?? '-'}</span>
                        </div>
                        <div class="meta-item">
                            <label>Model</label>
                            <span>${escapeHtml(model)}</span>
                        </div>
                        <div class="meta-item">
                            <label>Tokens</label>
                            <span>${tokens > 0 ? tokens.toLocaleString() : '-'} ${tokens > 0 ? '(' + inputTokens.toLocaleString() + ' in / ' + outputTokens.toLocaleString() + ' out)' : ''}</span>
                        </div>
                        <div class="meta-item">
                            <label>Cache Tokens</label>
                            <span>${(cacheReadTokens > 0 || cacheCreationTokens > 0) ? cacheReadTokens.toLocaleString() + ' read / ' + cacheCreationTokens.toLocaleString() + ' created' : '-'}</span>
                        </div>
                        <div class="meta-item">
                            <label>Web Searches</label>
                            <span>${webSearchRequests > 0 ? webSearchRequests.toLocaleString() : '-'}</span>
                        </div>
                        <div class="meta-item">
                            <label>Cost</label>
                            <span style="color: ${cost > 0 ? '#28a745' : 'inherit'}; font-weight: ${cost > 0 ? '600' : 'inherit'};">${cost > 0 ? '$' + cost.toFixed(4) : '-'}</span>
                        </div>
                        ${webhook ? `<div class="meta-item">
                            <label>Webhook</label>
                            <span>${escapeHtml(webhook.name)}</span>
                        </div>` : ''}
                    </div>
                </div>
                <div class="modal-section">
                    <h4>Prompt</h4>
                    <pre>${escapeHtml(run.prompt)}</pre>
                </div>
                ${errorHtml}
                ${isLive ? `<div class="modal-section">
                    <h4>Live Output</h4>
                    <div id="live-output">${run.output ? renderExecutionTrace(run.output) : '<div class="text-muted">Waiting for output...</div>'}</div>
                </div>
                <div class="modal-section" style="text-align: center;">
                    <button class="view-btn" style="background: #dc3545; padding: 10px 20px; font-size: 14px;" onclick="killRun('${run.id}')">Kill This Run</button>
                </div>` : ''}
                ${!isLive && run.output ? `<div class="modal-section">
                    <h4>Execution Trace</h4>
                    ${renderExecutionTrace(run.output)}
                </div>` : ''}
            `;

            // Auto-scroll live output to bottom
            if (isLive) {
                const liveOutput = document.getElementById('live-output');
                if (liveOutput) liveOutput.scrollTop = liveOutput.scrollHeight;
            }
        }

        async function pollRunUpdate() {
            if (!currentRunId) return;
            try {
                const res = await fetch(`/api/run/${currentRunId}`);
                const run = await res.json();
                if (run.error) return;

                renderRunModal(run);

                // Stop polling if run is complete
                if (run.state !== 'running' && run.state !== 'pending') {
                    stopLivePolling();
                    refresh(); // Refresh the main list
                }
            } catch (e) {
                console.error('Poll error:', e);
            }
        }

        function startLivePolling(runId) {
            stopLivePolling();
            currentRunId = runId;
            livePollingInterval = setInterval(pollRunUpdate, 2000);
        }

        function stopLivePolling() {
            if (livePollingInterval) {
                clearInterval(livePollingInterval);
                livePollingInterval = null;
            }
            currentRunId = null;
        }

        function showRunDetails(runId) {
            const run = runsData.find(r => r.id === runId);
            if (!run) return;

            renderRunModal(run);
            document.getElementById('output-modal').classList.add('active');

            // Start live polling if run is active
            if (run.state === 'running' || run.state === 'pending') {
                startLivePolling(runId);
            }
        }

        function showToolLogDetails(logId) {
            const log = toolLogsData.find(l => l.id === logId);
            if (!log) return;

            const modalTitle = document.getElementById('modal-title');
            modalTitle.textContent = 'Tool Log: ' + log.server_name + '.' + log.tool_name;

            let payloadFormatted = '';
            try { payloadFormatted = JSON.stringify(JSON.parse(log.payload || '{}'), null, 2); }
            catch(e) { payloadFormatted = log.payload || '(none)'; }

            let resultSection = '';
            if (log.status === 'error' && log.error) {
                resultSection = '<div class="modal-section"><h4>Error</h4><div class="result-text error-text">' + escapeHtml(log.error) + '</div></div>';
            } else if (log.result) {
                let resultFormatted = '';
                try { resultFormatted = JSON.stringify(JSON.parse(log.result), null, 2); }
                catch(e) { resultFormatted = log.result; }
                resultSection = '<div class="modal-section"><h4>Result</h4><pre class="result-text">' + escapeHtml(resultFormatted) + '</pre></div>';
            }

            document.getElementById('modal-body').innerHTML = '<div class="modal-section"><div class="meta-grid">'
                + '<div class="meta-item"><label>Server</label><span>' + escapeHtml(log.server_name) + '</span></div>'
                + '<div class="meta-item"><label>Tool</label><span>' + escapeHtml(log.tool_name) + '</span></div>'
                + '<div class="meta-item"><label>Status</label><span class="status ' + (log.status === 'success' ? 'finished' : 'error') + '">' + escapeHtml(log.status) + '</span></div>'
                + '<div class="meta-item"><label>Duration</label><span>' + log.duration_ms.toFixed(0) + 'ms</span></div>'
                + '<div class="meta-item"><label>Timestamp</label><span>' + new Date(log.created_at).toLocaleString() + '</span></div>'
                + '</div></div>'
                + '<div class="modal-section"><h4>Payload</h4><pre class="result-text">' + escapeHtml(payloadFormatted) + '</pre></div>'
                + resultSection;

            document.getElementById('output-modal').classList.add('active');
        }

        function closeModal() {
            stopLivePolling();
            document.getElementById('output-modal').classList.remove('active');
        }

        document.getElementById('output-modal').addEventListener('click', (e) => {
            if (e.target.id === 'output-modal') closeModal();
        });

        document.addEventListener('keydown', (e) => {
            if (e.key === 'Escape') closeModal();
        });

        async function refresh() {
            const [jobsRes, runsRes, webhooksRes, toolLogsRes] = await Promise.all([
                fetch('/api/jobs').then(r => r.json()),
                fetch('/api/runs').then(r => r.json()),
                fetch('/api/webhooks').then(r => r.json()),
                fetch('/api/tool-logs').then(r => r.json())
            ]);
            jobsData = jobsRes;
            runsData = runsRes;
            webhooksData = webhooksRes;
            toolLogsData = toolLogsRes;

            // Helper to determine run type based on job cron field
            function getRunType(run) {
                const job = jobsData.find(j => j.id === run.job_id);
                if (!job) return 'manual';
                if (job.cron === 'manual') return 'manual';
                if (job.cron === 'webhook') return 'webhook';
                return 'scheduled';
            }

            // Helper for relative time
            function timeAgo(dateStr) {
                const seconds = Math.floor((new Date() - new Date(dateStr)) / 1000);
                if (seconds < 60) return 'just now';
                const minutes = Math.floor(seconds / 60);
                if (minutes < 60) return `${minutes}m ago`;
                const hours = Math.floor(minutes / 60);
                if (hours < 24) return `${hours}h ago`;
                const days = Math.floor(hours / 24);
                return `${days}d ago`;
            }

            // Filter jobs - only show scheduled jobs (not manual or webhook)
            const scheduledJobs = jobsRes.filter(j => j.cron !== 'manual' && j.cron !== 'webhook');

            // Render scheduled jobs table
            document.getElementById('jobs-table').innerHTML = scheduledJobs.length === 0
                ? '<tr><td colspan="7" style="text-align: center; color: var(--text-muted); padding: var(--spacing-lg);">No scheduled jobs. Create one using the MCP tool.</td></tr>'
                : scheduledJobs.map(j => `
                <tr>
                    <td><code style="color: var(--accent-cyan);">${j.id.substring(0, 8)}</code></td>
                    <td>${escapeHtml(j.name)}</td>
                    <td><span class="cron">${j.cron}</span></td>
                    <td><span class="status ${j.enabled ? 'enabled' : 'disabled'}">${j.enabled ? 'Enabled' : 'Disabled'}</span></td>
                    <td style="color: var(--text-secondary);">${j.last_executed_at ? new Date(j.last_executed_at).toLocaleString() : 'Never'}</td>
                    <td style="color: var(--text-secondary);">${escapeHtml(j.prompt.substring(0, 50))}${j.prompt.length > 50 ? '...' : ''}</td>
                    <td><button class="delete-btn" onclick="deleteJob('${j.id}', '${escapeHtml(j.name).replace(/'/g, "\\'")}')">Delete</button></td>
                </tr>
            `).join('');

            // Separate runs by type
            const manualRuns = runsRes.filter(r => getRunType(r) === 'manual');
            const scheduledRuns = runsRes.filter(r => getRunType(r) === 'scheduled');
            const webhookRuns = runsRes.filter(r => getRunType(r) === 'webhook');

            // Render manual runs as cards
            document.getElementById('manual-runs-list').innerHTML = manualRuns.length === 0
                ? '<div style="text-align: center; color: var(--text-muted); padding: var(--spacing-lg);">No manual runs yet. Use the quick run form above to start.</div>'
                : manualRuns.map(r => {
                    const job = jobsData.find(j => j.id === r.job_id);
                    const prompt = job ? job.prompt : 'Unknown';
                    const tokens = r.total_tokens || 0;
                    const cost = r.cost_usd || 0;
                    const isRunning = r.state === 'running' || r.state === 'pending';
                    return `
                    <div class="run-card run-card--manual" onclick="showRunDetails('${r.id}')">
                        <div class="run-card__prompt">
                            <span class="run-card__icon">&gt;</span>
                            <span class="run-card__text">${escapeHtml(prompt.substring(0, 150))}${prompt.length > 150 ? '...' : ''}</span>
                        </div>
                        <div class="run-card__meta">
                            <span class="status badge-manual">Manual</span>
                            <span class="status ${r.state}">${isRunning ? '<span class="live-dot"></span>' : ''}${r.state}</span>
                            <span class="run-card__time">${timeAgo(r.started_at)}</span>
                            <span class="run-card__stats">${tokens > 0 ? tokens.toLocaleString() + ' tokens' : ''}${cost > 0 ? ' · $' + cost.toFixed(4) : ''}</span>
                        </div>
                    </div>
                `}).join('');

            // Render scheduled runs in table
            document.getElementById('scheduled-runs-table').innerHTML = scheduledRuns.length === 0
                ? '<tr><td colspan="8" style="text-align: center; color: var(--text-muted); padding: var(--spacing-lg);">No scheduled runs yet.</td></tr>'
                : scheduledRuns.map(r => {
                const job = jobsData.find(j => j.id === r.job_id);
                const summary = getResultSummary(r.output, r.error);
                const tokens = r.total_tokens || 0;
                const cost = r.cost_usd || 0;
                const isRunning = r.state === 'running' || r.state === 'pending';
                return `
                <tr>
                    <td><code style="color: var(--accent-cyan);">${r.id.substring(0, 8)}</code></td>
                    <td>${job ? escapeHtml(job.name) : r.job_id.substring(0, 8)}</td>
                    <td style="color: var(--text-secondary);">${new Date(r.started_at).toLocaleString()}</td>
                    <td>${formatDuration(r.started_at, r.finished_at)}</td>
                    <td>${tokens > 0 ? tokens.toLocaleString() : '-'}</td>
                    <td>${cost > 0 ? '$' + cost.toFixed(4) : '-'}</td>
                    <td><span class="status ${r.state}">${isRunning ? '<span class="live-dot"></span>' : ''}${r.state}</span></td>
                    <td>
                        <div class="output-summary">${escapeHtml(summary.text)}</div>
                        <button class="view-btn" onclick="event.stopPropagation(); showRunDetails('${r.id}')">View</button>
                        ${isRunning ? `<button class="delete-btn" style="margin-left: 5px;" onclick="event.stopPropagation(); killRun('${r.id}')">Kill</button>` : ''}
                    </td>
                </tr>
            `}).join('');

            // Render webhook runs in table (follows same escapeHtml pattern as scheduled runs above)
            document.getElementById('webhook-runs-table').innerHTML = webhookRuns.length === 0
                ? '<tr><td colspan="8" style="text-align: center; color: var(--text-muted); padding: var(--spacing-lg);">No webhook-triggered runs yet.</td></tr>'
                : webhookRuns.map(r => {
                const job = jobsData.find(j => j.id === r.job_id);
                const webhook = webhooksData.find(w => w.id === r.webhook_id);
                const webhookName = webhook ? webhook.name : (job ? job.name : r.webhook_id ? r.webhook_id.substring(0, 8) : 'Unknown');
                const summary = getResultSummary(r.output, r.error);
                const tokens = r.total_tokens || 0;
                const cost = r.cost_usd || 0;
                const isRunning = r.state === 'running' || r.state === 'pending';
                return `
                <tr data-webhook-id="${r.webhook_id || ''}">
                    <td><code style="color: var(--accent-cyan);">${r.id.substring(0, 8)}</code></td>
                    <td>${escapeHtml(webhookName)}</td>
                    <td style="color: var(--text-secondary);">${new Date(r.started_at).toLocaleString()}</td>
                    <td>${formatDuration(r.started_at, r.finished_at)}</td>
                    <td>${tokens > 0 ? tokens.toLocaleString() : '-'}</td>
                    <td>${cost > 0 ? '$' + cost.toFixed(4) : '-'}</td>
                    <td><span class="status ${r.state}">${isRunning ? '<span class="live-dot"></span>' : ''}${r.state}</span></td>
                    <td>
                        <div class="output-summary">${escapeHtml(summary.text)}</div>
                        <button class="view-btn" onclick="event.stopPropagation(); showRunDetails('${r.id}')">View</button>
                        ${isRunning ? `<button class="delete-btn" style="margin-left: 5px;" onclick="event.stopPropagation(); killRun('${r.id}')">Kill</button>` : ''}
                    </td>
                </tr>
            `}).join('');

            // Render webhooks
            document.getElementById('webhooks-table').innerHTML = webhooksRes.length === 0
                ? '<tr><td colspan="6" style="text-align: center; color: var(--text-muted); padding: var(--spacing-lg);">No webhooks configured. Create one using the MCP tool.</td></tr>'
                : webhooksRes.map(w => `
                <tr>
                    <td>
                        <strong style="color: var(--text-primary);">${escapeHtml(w.name)}</strong>
                        ${w.description ? '<br><small style="color: var(--text-muted);">' + escapeHtml(w.description) + '</small>' : ''}
                    </td>
                    <td>
                        <code style="font-size: 11px; word-break: break-all; color: var(--accent-cyan);">${escapeHtml(w.url)}</code>
                        <button class="view-btn" style="margin-left: 8px;" onclick="copyToClipboard('${escapeHtml(w.url)}')">Copy</button>
                    </td>
                    <td>${w.trigger_count}</td>
                    <td style="color: var(--text-secondary);">${w.last_triggered_at ? new Date(w.last_triggered_at).toLocaleString() : 'Never'}</td>
                    <td><span class="status ${w.enabled ? 'enabled' : 'disabled'}">${w.enabled ? 'Active' : 'Disabled'}</span></td>
                    <td>
                        <button class="view-btn" onclick="viewWebhookPrompt('${w.id}')">View Prompt</button>
                        <button class="view-btn" style="margin-left: 5px;" onclick="showWebhookRuns('${w.id}')">View Runs</button>
                    </td>
                </tr>
            `).join('');

            // Render tool logs table
            const toolLogsTable = document.getElementById('tool-logs-table');
            if (toolLogsRes.length === 0) {
                toolLogsTable.textContent = '';
                const emptyRow = document.createElement('tr');
                const emptyCell = document.createElement('td');
                emptyCell.colSpan = 6;
                emptyCell.style.cssText = 'text-align: center; color: var(--text-muted); padding: var(--spacing-lg);';
                emptyCell.textContent = 'No tool invocations logged yet.';
                emptyRow.appendChild(emptyCell);
                toolLogsTable.appendChild(emptyRow);
            } else {
                toolLogsTable.textContent = '';
                toolLogsRes.forEach(l => {
                    const row = document.createElement('tr');

                    const serverCell = document.createElement('td');
                    const serverStrong = document.createElement('strong');
                    serverStrong.style.color = 'var(--text-primary)';
                    serverStrong.textContent = l.server_name;
                    serverCell.appendChild(serverStrong);
                    row.appendChild(serverCell);

                    const toolCell = document.createElement('td');
                    const toolCode = document.createElement('code');
                    toolCode.style.cssText = 'font-size: 12px; color: var(--accent-cyan);';
                    toolCode.textContent = l.tool_name;
                    toolCell.appendChild(toolCode);
                    row.appendChild(toolCell);

                    const statusCell = document.createElement('td');
                    const statusSpan = document.createElement('span');
                    statusSpan.className = 'status ' + (l.status === 'success' ? 'finished' : 'error');
                    statusSpan.textContent = l.status;
                    statusCell.appendChild(statusSpan);
                    row.appendChild(statusCell);

                    const durationCell = document.createElement('td');
                    durationCell.style.color = 'var(--text-secondary)';
                    durationCell.textContent = l.duration_ms.toFixed(0) + 'ms';
                    row.appendChild(durationCell);

                    const timeCell = document.createElement('td');
                    timeCell.style.color = 'var(--text-secondary)';
                    timeCell.textContent = timeAgo(l.created_at);
                    row.appendChild(timeCell);

                    const actionsCell = document.createElement('td');
                    const viewBtn = document.createElement('button');
                    viewBtn.className = 'view-btn';
                    viewBtn.textContent = 'View';
                    viewBtn.addEventListener('click', () => showToolLogDetails(l.id));
                    actionsCell.appendChild(viewBtn);
                    row.appendChild(actionsCell);

                    toolLogsTable.appendChild(row);
                });
            }

            document.getElementById('last-update').textContent = 'Updated ' + new Date().toLocaleTimeString();

            // Update cost overview
            await updateCostOverview();
        }

        async function updateCostOverview() {
            try {
                const res = await fetch('/api/stats');
                const stats = await res.json();

                document.getElementById('cost-today').textContent = '$' + (stats.today_cost || 0).toFixed(2);
                document.getElementById('runs-today').textContent = (stats.today_runs || 0) + ' runs';

                document.getElementById('cost-week').textContent = '$' + (stats.week_cost || 0).toFixed(2);
                document.getElementById('runs-week').textContent = (stats.week_runs || 0) + ' runs';

                document.getElementById('cost-total').textContent = '$' + (stats.total_cost || 0).toFixed(2);
                document.getElementById('runs-total').textContent = (stats.total_runs || 0) + ' runs';

                const totalTokens = stats.total_tokens || 0;
                document.getElementById('tokens-total').textContent = totalTokens.toLocaleString();
                document.getElementById('tokens-input').textContent = (stats.total_input_tokens || 0).toLocaleString();
                document.getElementById('tokens-output').textContent = (stats.total_output_tokens || 0).toLocaleString();

                // Token bar: show ratio of input vs output
                const inputRatio = totalTokens > 0 ? (stats.total_input_tokens / totalTokens) * 100 : 50;
                document.getElementById('token-bar-fill').style.width = inputRatio + '%';
            } catch (e) {
                console.error('Failed to load stats:', e);
            }
        }

        let ngrokSseUrl = null;

        async function updateNgrokStatus() {
            try {
                const res = await fetch('/api/ngrok');
                const data = await res.json();

                if (data.url) {
                    ngrokSseUrl = data.sse_endpoint;
                    document.getElementById('ngrok-card').style.display = 'block';
                    document.getElementById('ngrok-url').textContent = data.sse_endpoint;
                }
            } catch (e) {
                console.error('Failed to load ngrok status:', e);
            }
        }

        function copyNgrokUrl(event) {
            event.preventDefault();
            if (ngrokSseUrl) {
                navigator.clipboard.writeText(ngrokSseUrl).then(() => {
                    const link = document.getElementById('ngrok-copy');
                    const originalText = link.textContent;
                    link.textContent = 'Copied!';
                    setTimeout(() => { link.textContent = originalText; }, 2000);
                });
            }
        }

        async function runQuickPrompt() {
            const input = document.getElementById('quick-prompt');
            const btn = document.getElementById('quick-run-btn');
            const prompt = input.value.trim();

            if (!prompt) {
                input.focus();
                return;
            }

            btn.disabled = true;
            btn.textContent = 'Starting...';

            try {
                const res = await fetch('/api/run-prompt', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ prompt })
                });
                const data = await res.json();

                if (data.error) {
                    showToast('Error: ' + data.error, 'error');
                    return;
                }

                // Clear input
                input.value = '';
                showToast('Run started', 'success');

                // Refresh background data
                refresh();

                // Small delay then fetch the new run and show modal
                await new Promise(r => setTimeout(r, 500));
                const runRes = await fetch(`/api/run/${data.run_id}`);
                const run = await runRes.json();

                if (run && !run.error) {
                    renderRunModal(run);
                    document.getElementById('output-modal').classList.add('active');
                    startLivePolling(data.run_id);
                } else {
                    console.error('Run fetch error:', run);
                    showToast('Run started but could not load details', 'warning');
                }
            } catch (e) {
                showToast('Error: ' + e.message, 'error');
            } finally {
                btn.disabled = false;
                btn.textContent = 'Run Now';
            }
        }

        // === Settings Management ===
        const AVAILABLE_TOOLS = [
            { id: 'WebSearch', name: 'Web Search', icon: '🔍', desc: 'Search the web' },
            { id: 'WebFetch', name: 'Web Fetch', icon: '🌐', desc: 'Fetch web pages' },
            { id: 'Read', name: 'Read Files', icon: '📖', desc: 'Read file contents' },
            { id: 'Write', name: 'Write Files', icon: '📝', desc: 'Write to files' },
            { id: 'Edit', name: 'Edit Files', icon: '✏️', desc: 'Edit existing files' },
            { id: 'Bash', name: 'Bash/Shell', icon: '💻', desc: 'Run shell commands' },
            { id: 'Glob', name: 'Glob Search', icon: '📁', desc: 'Find files by pattern' },
            { id: 'Grep', name: 'Grep Search', icon: '🔎', desc: 'Search file contents' },
        ];

        let settingsData = { allowed_tools: [], mcp_servers: [], mcp_env_vars: {}, custom_mcp_paths: [], sandbox_mode: true };
        let availableMcpServers = [];
        let fixedServers = [];
        let dynamicServers = [];

        async function loadSettings() {
            try {
                const res = await fetch('/api/settings');
                settingsData = await res.json();
                if (!settingsData.mcp_env_vars) settingsData.mcp_env_vars = {};
                if (!settingsData.custom_mcp_paths) settingsData.custom_mcp_paths = [];
                renderTools();
                renderCustomPaths();
                await Promise.all([
                    loadAvailableMcpServers(),
                    loadFixedServers(),
                    loadDynamicServers()
                ]);
                renderSecuritySettings();
            } catch (e) {
                console.error('Failed to load settings:', e);
            }
        }

        async function loadFixedServers() {
            try {
                const res = await fetch('/api/fixed-servers');
                fixedServers = await res.json();
                renderFixedServers();
            } catch (e) {
                console.error('Failed to load fixed servers:', e);
                document.getElementById('fixed-servers-grid').innerHTML = '<div class="mcp-empty">Failed to load fixed servers</div>';
            }
        }

        async function loadDynamicServers() {
            try {
                const res = await fetch('/api/dynamic-servers');
                dynamicServers = await res.json();
                renderDynamicServers();
            } catch (e) {
                console.error('Failed to load dynamic servers:', e);
                document.getElementById('dynamic-servers-grid').innerHTML = '<div class="mcp-empty">Failed to load dynamic servers</div>';
            }
        }

        function renderServerCard(server, type) {
            const enabled = server.enabled;
            const hasEnvVars = server.env_vars && server.env_vars.length > 0;
            const credConfigured = server.credentials_configured;
            const status = !enabled ? 'disabled' : (credConfigured ? 'ready' : 'needs-config');
            const cardClass = enabled ? (status === 'ready' ? 'enabled' : 'needs-config') : '';
            const statusBadge = enabled
                ? (status === 'ready'
                    ? '<span class="mcp-card-status ready">Ready</span>'
                    : '<span class="mcp-card-status needs-vars">Needs Config</span>')
                : '<span class="mcp-card-status" style="background: #6c757d;">Disabled</span>';

            let configHtml = '';
            if (hasEnvVars) {
                configHtml = '<div class="mcp-card-config">' +
                    server.env_vars.map(v => {
                        const key = server.name + '_' + v.name;
                        const hasValue = settingsData.mcp_env_vars && settingsData.mcp_env_vars[key];
                        const inputType = v.sensitive ? 'password' : 'text';
                        return '<div class="mcp-var-row">' +
                            '<label>' + escapeHtml(v.name) + (v.required ? ' <span style="color:#dc3545">*</span>' : '') + '</label>' +
                            '<input type="' + inputType + '" placeholder="' + escapeHtml(v.description || '') + '" ' +
                            'value="' + (hasValue ? '••••••••' : '') + '" ' +
                            'onchange="setServerCredential(\\'' + escapeHtml(server.name) + '\\', \\'' + escapeHtml(v.name) + '\\', this.value)" ' +
                            'style="width: 100%; padding: 6px; border: 1px solid var(--border-default); background: var(--bg-tertiary); color: var(--text-primary); font-size: 12px;">' +
                            '</div>';
                    }).join('') +
                    '</div>';
            }

            return '<div class="mcp-card ' + cardClass + '">' +
                '<div class="mcp-card-header">' +
                '<div class="mcp-card-title">' + escapeHtml(server.name) + '</div>' +
                statusBadge +
                '</div>' +
                '<div class="mcp-card-desc">' + escapeHtml(server.description || '') + '</div>' +
                configHtml +
                '<div class="mcp-card-toggle">' +
                '<label class="toggle-switch">' +
                '<input type="checkbox" ' + (enabled ? 'checked' : '') + ' onchange="toggleServerEnabled(\\'' + type + '\\', \\'' + escapeHtml(server.name) + '\\', this.checked)">' +
                '<span class="toggle-slider"></span>' +
                '</label>' +
                '<span style="font-size: 12px; color: var(--text-secondary);">' + (enabled ? 'Enabled' : 'Disabled') + '</span>' +
                '</div>' +
                '</div>';
        }

        function renderFixedServers() {
            const container = document.getElementById('fixed-servers-grid');
            if (fixedServers.length === 0) {
                container.innerHTML = '<div class="mcp-empty">No fixed servers installed.</div>';
                return;
            }
            container.innerHTML = fixedServers.map(s => renderServerCard(s, 'fixed')).join('');
        }

        function renderDynamicServers() {
            const container = document.getElementById('dynamic-servers-grid');
            if (dynamicServers.length === 0) {
                container.innerHTML = '<div class="mcp-empty">No dynamic servers created. Use create_mcp_server tool to create one.</div>';
                return;
            }
            container.innerHTML = dynamicServers.map(s => renderServerCard(s, 'dynamic')).join('');
        }

        async function setServerCredential(serverName, varName, value) {
            if (!value || value === '••••••••') return;
            try {
                const res = await fetch('/api/server-credential', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ server_name: serverName, var_name: varName, value: value })
                });
                if (res.ok) {
                    showToast('Credential saved', 'success');
                    // Update local state
                    if (!settingsData.mcp_env_vars) settingsData.mcp_env_vars = {};
                    settingsData.mcp_env_vars[serverName + '_' + varName] = value;
                    await Promise.all([loadFixedServers(), loadDynamicServers()]);
                } else {
                    showToast('Failed to save credential', 'error');
                }
            } catch (e) {
                showToast('Error saving credential: ' + e.message, 'error');
            }
        }

        async function toggleServerEnabled(type, name, enabled) {
            try {
                const endpoint = type === 'fixed' ? '/api/fixed-server-toggle' : '/api/dynamic-server-toggle';
                const res = await fetch(endpoint, {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ name: name, enabled: enabled })
                });
                if (res.ok) {
                    showToast(name + ' ' + (enabled ? 'enabled' : 'disabled'), 'success');
                    if (type === 'fixed') await loadFixedServers();
                    else await loadDynamicServers();
                } else {
                    showToast('Failed to toggle server', 'error');
                }
            } catch (e) {
                showToast('Error: ' + e.message, 'error');
            }
        }

        function renderCustomPaths() {
            const container = document.getElementById('custom-paths-list');
            if (!settingsData.custom_mcp_paths || settingsData.custom_mcp_paths.length === 0) {
                container.innerHTML = '<div style="font-size: 12px; color: #999;">No custom paths configured.</div>';
                return;
            }
            container.innerHTML = settingsData.custom_mcp_paths.map((path, i) =>
                '<div style="display: flex; gap: 8px; align-items: center;">' +
                '<input type="text" value="' + escapeHtml(path) + '" style="flex: 1; padding: 6px 10px; border: 1px solid #ddd; border-radius: 4px; font-size: 12px; font-family: monospace;" onchange="updateCustomPath(' + i + ', this.value)">' +
                '<button onclick="removeCustomPath(' + i + ')" style="padding: 6px 10px; background: #dc3545; color: white; border: none; border-radius: 4px; cursor: pointer; font-size: 11px;">Remove</button>' +
                '</div>'
            ).join('');
        }

        function addCustomPath() {
            const path = prompt('Enter the path to a local MCP server directory:');
            if (path && path.trim()) {
                settingsData.custom_mcp_paths.push(path.trim());
                renderCustomPaths();
            }
        }

        function updateCustomPath(index, value) {
            settingsData.custom_mcp_paths[index] = value;
        }

        function removeCustomPath(index) {
            settingsData.custom_mcp_paths.splice(index, 1);
            renderCustomPaths();
        }

        async function reloadMcpServers() {
            // Save custom paths first, then reload
            try {
                await fetch('/api/settings', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ custom_mcp_paths: settingsData.custom_mcp_paths })
                });
                document.getElementById('mcp-cards').innerHTML = '<div class="mcp-loading">Reloading MCP servers...</div>';
                await loadAvailableMcpServers();
            } catch (e) {
                console.error('Failed to reload MCP servers:', e);
                showToast('Failed to reload MCP servers', 'error');
            }
        }

        async function loadAvailableMcpServers() {
            try {
                const res = await fetch('/api/mcp-available');
                availableMcpServers = await res.json();
                renderMcpCards();
            } catch (e) {
                console.error('Failed to load MCP servers:', e);
                document.getElementById('mcp-cards').innerHTML = '<div class="mcp-empty">Failed to load MCP servers</div>';
            }
        }

        function isMcpServerEnabled(serverId) {
            return settingsData.mcp_servers.some(s => s.name === serverId);
        }

        function getMcpServerStatus(server) {
            if (!isMcpServerEnabled(server.id)) return 'disabled';
            const hasEnvVars = server.envVars && server.envVars.length > 0;
            const hasInputVars = server.inputVars && server.inputVars.length > 0;
            if (!hasEnvVars && !hasInputVars) return 'ready';
            const allEnvVarsSet = !hasEnvVars || server.envVars.every(v => settingsData.mcp_env_vars[v]);
            const allInputVarsSet = !hasInputVars || server.inputVars.every(v => settingsData.mcp_env_vars['input_' + v]);
            return (allEnvVarsSet && allInputVarsSet) ? 'ready' : 'needs-vars';
        }

        function toggleMcpServer(serverId) {
            const server = availableMcpServers.find(s => s.id === serverId);
            if (!server) return;

            const isEnabled = isMcpServerEnabled(serverId);
            if (isEnabled) {
                settingsData.mcp_servers = settingsData.mcp_servers.filter(s => s.name !== serverId);
            } else {
                settingsData.mcp_servers.push({
                    name: serverId,
                    config: server.config
                });
            }
            renderMcpCards();
        }

        function updateMcpEnvVar(varName, value) {
            if (value) {
                settingsData.mcp_env_vars[varName] = value;
            } else {
                delete settingsData.mcp_env_vars[varName];
            }
            renderMcpCards();
        }

        function renderMcpCards() {
            const container = document.getElementById('mcp-cards');
            if (availableMcpServers.length === 0) {
                container.innerHTML = '<div class="mcp-empty">No MCP servers found. Install MCP plugins in Claude Code or add custom paths below.</div>';
                return;
            }

            container.innerHTML = availableMcpServers.map(server => {
                const enabled = isMcpServerEnabled(server.id);
                const status = getMcpServerStatus(server);
                const cardClass = enabled ? (status === 'ready' ? 'enabled' : 'needs-config') : '';
                const isCustom = server.source === 'custom';
                const statusBadge = enabled
                    ? (status === 'ready'
                        ? '<span class="mcp-card-status ready">Ready</span>'
                        : '<span class="mcp-card-status needs-vars">Needs Config</span>')
                    : '';
                const customBadge = isCustom ? '<span style="font-size: 10px; background: #6f42c1; color: white; padding: 2px 6px; border-radius: 3px; margin-left: 6px;">Custom</span>' : '';

                // Build env vars HTML
                let configHtml = '';
                if (enabled) {
                    const envVarsList = (server.envVars || []).map(varName => {
                        const hasValue = !!settingsData.mcp_env_vars[varName];
                        return '<div class="mcp-env-var">' +
                            '<label>' + escapeHtml(varName) + '</label>' +
                            '<input type="password" class="' + (hasValue ? 'has-value' : '') + '" ' +
                            'placeholder="Enter value..." ' +
                            'value="' + (settingsData.mcp_env_vars[varName] || '') + '" ' +
                            'onchange="updateMcpEnvVar(\\''+varName+'\\', this.value)">' +
                            '</div>';
                    });

                    // Also show input vars (like ado_org for Azure DevOps)
                    const inputVarsList = (server.inputVars || []).map(varName => {
                        const key = 'input_' + varName;
                        const hasValue = !!settingsData.mcp_env_vars[key];
                        return '<div class="mcp-env-var">' +
                            '<label>' + escapeHtml(varName) + '</label>' +
                            '<input type="text" class="' + (hasValue ? 'has-value' : '') + '" ' +
                            'placeholder="Enter value..." ' +
                            'value="' + (settingsData.mcp_env_vars[key] || '') + '" ' +
                            'onchange="updateMcpEnvVar(\\''+key+'\\', this.value)">' +
                            '</div>';
                    });

                    const allVars = [...envVarsList, ...inputVarsList];
                    if (allVars.length > 0) {
                        configHtml = '<div class="mcp-env-vars">' + allVars.join('') + '</div>';
                    }
                }

                return '<div class="mcp-card ' + cardClass + '">' +
                    '<div class="mcp-card-header">' +
                    '<input type="checkbox" ' + (enabled ? 'checked' : '') + ' onchange="toggleMcpServer(\\''+server.id+'\\')">' +
                    '<span class="mcp-card-name">' + escapeHtml(server.name) + customBadge + '</span>' +
                    statusBadge +
                    '</div>' +
                    '<div class="mcp-card-desc">' + escapeHtml(server.description) + '</div>' +
                    configHtml +
                    '</div>';
            }).join('');
        }

        function renderSecuritySettings() {
            const checkbox = document.getElementById('sandbox-mode');
            if (checkbox) {
                checkbox.checked = settingsData.sandbox_mode === true || settingsData.sandbox_mode === 'true';
            }
        }

        function toggleSandbox() {
            settingsData.sandbox_mode = document.getElementById('sandbox-mode').checked;
        }

        function renderTools() {
            const grid = document.getElementById('tools-grid');
            grid.innerHTML = AVAILABLE_TOOLS.map(tool => {
                const enabled = settingsData.allowed_tools.includes(tool.id);
                return `
                    <div class="tool-item ${enabled ? 'enabled' : ''}" onclick="toggleTool('${tool.id}')">
                        <span class="tool-icon">${tool.icon}</span>
                        <input type="checkbox" id="tool-${tool.id}" ${enabled ? 'checked' : ''} onclick="event.stopPropagation(); toggleTool('${tool.id}')">
                        <label for="tool-${tool.id}">${tool.name}</label>
                    </div>
                `;
            }).join('');
        }

        function toggleTool(toolId) {
            const idx = settingsData.allowed_tools.indexOf(toolId);
            if (idx >= 0) {
                settingsData.allowed_tools.splice(idx, 1);
            } else {
                settingsData.allowed_tools.push(toolId);
            }
            renderTools();
        }

        async function saveSettings() {
            try {
                const res = await fetch('/api/settings', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(settingsData)
                });
                if (res.ok) {
                    showToast('Settings saved', 'success');
                } else {
                    showToast('Failed to save settings', 'error');
                }
            } catch (e) {
                showToast('Error saving settings: ' + e.message, 'error');
            }
        }

        // Load settings on page load
        loadSettings();

        // Check ngrok status once on load
        updateNgrokStatus();

        refresh();
        setInterval(refresh, 10000);
    </script>
</body>
</html>
"""

@require_auth
async def dashboard_handler(request):
    return HTMLResponse(DASHBOARD_HTML)

async def static_file_handler(request):
    """Serve static files (CSS, JS) from the static directory."""
    filename = request.path_params.get("filename", "")
    # Security: only allow specific file extensions
    allowed_extensions = {'.css', '.js', '.png', '.jpg', '.ico', '.svg', '.woff', '.woff2'}
    import os
    ext = os.path.splitext(filename)[1].lower()
    if ext not in allowed_extensions:
        return Response("Not found", status_code=404)

    static_dir = os.path.join(os.path.dirname(__file__), "static")
    file_path = os.path.join(static_dir, filename)

    # Security: prevent path traversal
    if not os.path.abspath(file_path).startswith(os.path.abspath(static_dir)):
        return Response("Not found", status_code=404)

    if os.path.isfile(file_path):
        content_types = {
            '.css': 'text/css',
            '.js': 'application/javascript',
            '.png': 'image/png',
            '.jpg': 'image/jpeg',
            '.ico': 'image/x-icon',
            '.svg': 'image/svg+xml',
            '.woff': 'font/woff',
            '.woff2': 'font/woff2',
        }
        return FileResponse(file_path, media_type=content_types.get(ext, 'application/octet-stream'))
    return Response("Not found", status_code=404)

@require_auth
async def api_jobs_handler(request):
    conn = get_db()
    jobs = conn.execute("SELECT * FROM jobs ORDER BY created_at DESC").fetchall()
    conn.close()
    return JSONResponse([dict(j) for j in jobs])

@require_auth
async def api_runs_handler(request):
    conn = get_db()
    runs = conn.execute("SELECT * FROM runs ORDER BY started_at DESC LIMIT 50").fetchall()
    conn.close()
    return JSONResponse([dict(r) for r in runs])

@require_auth
async def api_run_detail_handler(request):
    run_id = request.path_params['run_id']
    conn = get_db()
    run = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
    conn.close()
    if not run:
        return JSONResponse({"error": "Run not found"}, status_code=404)
    return JSONResponse(dict(run))

@require_auth
async def api_kill_run_handler(request):
    """Kill a running task"""
    run_id = request.path_params['run_id']

    # Check if task is in our tracking dict
    task = RUNNING_TASKS.get(run_id)
    if not task:
        conn = get_db()
        run = conn.execute("SELECT state FROM runs WHERE id = ?", (run_id,)).fetchone()
        conn.close()
        if not run:
            return JSONResponse({"error": "Run not found"}, status_code=404)
        if run["state"] != "running":
            return JSONResponse({"error": f"Run is not running (state: {run['state']})"}, status_code=400)
        return JSONResponse({"error": "Run not found in active tasks"}, status_code=400)

    # Cancel the task
    task.cancel()

    # Update database
    conn = get_db()
    conn.execute("""
        UPDATE runs SET state = 'error', error = 'Killed by user', finished_at = ?
        WHERE id = ?
    """, (utc_now_iso(), run_id))
    conn.commit()
    conn.close()

    RUNNING_TASKS.pop(run_id, None)
    return JSONResponse({"success": True, "message": f"Run {run_id} killed"})

@require_auth
async def api_run_prompt_handler(request):
    """Run a custom prompt immediately (one-off execution)"""
    try:
        body = await request.json()
        prompt = body.get('prompt', '').strip()
        if not prompt:
            return JSONResponse({"error": "Prompt is required"}, status_code=400)

        # Create a one-off job
        job_id = str(uuid.uuid4())[:8]
        now = utc_now_iso()

        conn = get_db()
        conn.execute("""
            INSERT INTO jobs (id, name, cron, prompt, command, tools, environment, enabled, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (job_id, f"Manual Run ({now[:16]})", "manual", prompt, "claude", "[]", "{}", 0, now, now))
        conn.commit()

        # Create and trigger the run
        run_id = str(uuid.uuid4())[:8]
        conn.execute("""
            INSERT INTO runs (id, job_id, started_at, prompt, command, state)
            VALUES (?, ?, ?, ?, ?, 'pending')
        """, (run_id, job_id, now, prompt, "claude"))
        conn.commit()
        conn.close()

        # Run will be picked up by run_processor_loop (within 5 seconds)
        return JSONResponse({"success": True, "run_id": run_id, "job_id": job_id})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)

@require_auth
async def api_stats_handler(request):
    """Get cost and usage statistics"""
    conn = get_db()

    # Get today's date at midnight
    today = utc_now().date().isoformat()

    # Get date 7 days ago
    week_ago = (utc_now().date() - timedelta(days=7)).isoformat()

    # Today's stats
    today_stats = conn.execute("""
        SELECT COUNT(*) as runs, COALESCE(SUM(cost_usd), 0) as cost,
               COALESCE(SUM(total_tokens), 0) as tokens
        FROM runs WHERE date(started_at) = date(?)
    """, (today,)).fetchone()

    # This week's stats
    week_stats = conn.execute("""
        SELECT COUNT(*) as runs, COALESCE(SUM(cost_usd), 0) as cost,
               COALESCE(SUM(total_tokens), 0) as tokens
        FROM runs WHERE date(started_at) >= date(?)
    """, (week_ago,)).fetchone()

    # Total stats
    total_stats = conn.execute("""
        SELECT COUNT(*) as runs, COALESCE(SUM(cost_usd), 0) as cost,
               COALESCE(SUM(total_tokens), 0) as tokens,
               COALESCE(SUM(input_tokens), 0) as input_tokens,
               COALESCE(SUM(output_tokens), 0) as output_tokens
        FROM runs
    """).fetchone()

    conn.close()

    return JSONResponse({
        "today_runs": today_stats["runs"],
        "today_cost": today_stats["cost"],
        "week_runs": week_stats["runs"],
        "week_cost": week_stats["cost"],
        "total_runs": total_stats["runs"],
        "total_cost": total_stats["cost"],
        "total_tokens": total_stats["tokens"],
        "total_input_tokens": total_stats["input_tokens"],
        "total_output_tokens": total_stats["output_tokens"]
    })

@require_auth
async def api_ngrok_handler(request):
    """Get ngrok tunnel URL if available"""
    return JSONResponse({
        "url": NGROK_PUBLIC_URL,
        "sse_endpoint": f"{NGROK_PUBLIC_URL}/sse" if NGROK_PUBLIC_URL else None
    })

@require_auth
async def api_settings_get_handler(request):
    """Get current settings"""
    conn = get_db()
    settings = {}
    for row in conn.execute("SELECT key, value FROM settings").fetchall():
        try:
            settings[row['key']] = json.loads(row['value'])
        except:
            settings[row['key']] = row['value']
    conn.close()
    return JSONResponse(settings)

@require_auth
async def api_settings_post_handler(request):
    """Save settings"""
    try:
        body = await request.json()
        conn = get_db()

        # Save allowed_tools
        if 'allowed_tools' in body:
            conn.execute(
                "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
                ('allowed_tools', json.dumps(body['allowed_tools']))
            )

        # Save mcp_servers
        if 'mcp_servers' in body:
            conn.execute(
                "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
                ('mcp_servers', json.dumps(body['mcp_servers']))
            )

        # Save mcp_env_vars
        if 'mcp_env_vars' in body:
            conn.execute(
                "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
                ('mcp_env_vars', json.dumps(body['mcp_env_vars']))
            )

        # Save custom_mcp_paths
        if 'custom_mcp_paths' in body:
            conn.execute(
                "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
                ('custom_mcp_paths', json.dumps(body['custom_mcp_paths']))
            )

        # Save sandbox_mode
        if 'sandbox_mode' in body:
            conn.execute(
                "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
                ('sandbox_mode', json.dumps(body['sandbox_mode']))
            )

        conn.commit()
        conn.close()
        return JSONResponse({"success": True})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)

@require_auth
async def api_mcp_available_handler(request):
    """Discover available MCP servers from Claude Code's plugin directory and custom paths"""
    from pathlib import Path
    import re

    servers = []

    # Load custom paths from settings
    custom_paths = []
    try:
        conn = get_db()
        row = conn.execute("SELECT value FROM settings WHERE key = 'custom_mcp_paths'").fetchone()
        if row:
            custom_paths = json.loads(row['value'])
        conn.close()
    except:
        pass

    # Scan Claude Code plugins directory
    plugins_dir = Path.home() / ".claude" / "plugins" / "marketplaces" / "claude-plugins-official" / "external_plugins"
    if plugins_dir.exists():
        for plugin_dir in plugins_dir.iterdir():
            if not plugin_dir.is_dir():
                continue

            mcp_file = plugin_dir / ".mcp.json"
            if not mcp_file.exists():
                continue

            try:
                mcp_config = json.loads(mcp_file.read_text())

                # Read plugin metadata for description
                description = ""
                plugin_json = plugin_dir / ".claude-plugin" / "plugin.json"
                if plugin_json.exists():
                    try:
                        plugin_meta = json.loads(plugin_json.read_text())
                        description = plugin_meta.get("description", "")
                    except:
                        pass

                # Extract env var placeholders from config
                config_str = json.dumps(mcp_config)
                env_vars = list(set(re.findall(r'\$\{([A-Z_][A-Z0-9_]*)\}', config_str)))

                # Get the first server config (most plugins have one)
                server_name = plugin_dir.name
                server_config = mcp_config.get(server_name, mcp_config)

                servers.append({
                    "id": server_name,
                    "name": server_name.replace("-", " ").title(),
                    "description": description[:200] if description else f"{server_name} MCP server",
                    "config": server_config,
                    "envVars": env_vars,
                    "source": "plugin"
                })
            except Exception as e:
                print(f"[api_mcp_available] Error reading {plugin_dir}: {e}", file=sys.stderr)
                continue

    # Scan custom paths for local MCP servers
    for custom_path in custom_paths:
        try:
            path = Path(custom_path).expanduser()
            if not path.exists():
                continue

            # Look for mcp.json (local format) or .mcp.json (plugin format)
            mcp_file = path / "mcp.json"
            if not mcp_file.exists():
                mcp_file = path / ".mcp.json"
            if not mcp_file.exists():
                continue

            mcp_config = json.loads(mcp_file.read_text())

            # Read server.json for metadata if available
            description = ""
            server_json = path / "server.json"
            if server_json.exists():
                try:
                    server_meta = json.loads(server_json.read_text())
                    description = server_meta.get("description", "")
                except:
                    pass

            # Check if there's a package.json with npm package info
            npm_package_name = None
            package_json = path / "package.json"
            if package_json.exists():
                try:
                    pkg = json.loads(package_json.read_text())
                    npm_package_name = pkg.get("name")
                except:
                    pass

            # Handle local MCP format: {"servers": {"name": {...}}, "inputs": [...]}
            if "servers" in mcp_config:
                for server_name, server_config in mcp_config["servers"].items():
                    # Extract env vars and input placeholders
                    config_str = json.dumps(server_config)
                    env_vars = list(set(re.findall(r'\$\{([A-Z_][A-Z0-9_]*)\}', config_str)))

                    # Also extract input placeholders like ${input:ado_org}
                    input_vars = list(set(re.findall(r'\$\{input:([a-z_][a-z0-9_]*)\}', config_str)))

                    # If command isn't in PATH and we have an npm package, use npx
                    if npm_package_name and server_config.get("command"):
                        original_cmd = server_config["command"]
                        # Check if command is in PATH
                        import shutil
                        if not shutil.which(original_cmd):
                            # Transform to npx command
                            server_config = server_config.copy()
                            original_args = server_config.get("args", [])
                            server_config["command"] = "npx"
                            server_config["args"] = ["-y", "-p", npm_package_name, original_cmd] + original_args

                    servers.append({
                        "id": f"custom-{server_name}",
                        "name": server_name.replace("-", " ").replace("_", " ").title(),
                        "description": description[:200] if description else f"{server_name} (custom)",
                        "config": server_config,
                        "envVars": env_vars,
                        "inputVars": input_vars,
                        "source": "custom",
                        "path": str(path)
                    })
            else:
                # Standard plugin format
                server_name = path.name
                config_str = json.dumps(mcp_config)
                env_vars = list(set(re.findall(r'\$\{([A-Z_][A-Z0-9_]*)\}', config_str)))

                server_config = mcp_config.get(server_name, mcp_config)

                # If command isn't in PATH and we have an npm package, use npx
                if npm_package_name and server_config.get("command"):
                    original_cmd = server_config["command"]
                    import shutil
                    if not shutil.which(original_cmd):
                        server_config = server_config.copy()
                        original_args = server_config.get("args", [])
                        server_config["command"] = "npx"
                        server_config["args"] = ["-y", "-p", npm_package_name, original_cmd] + original_args

                servers.append({
                    "id": f"custom-{server_name}",
                    "name": server_name.replace("-", " ").title(),
                    "description": description[:200] if description else f"{server_name} (custom)",
                    "config": server_config,
                    "envVars": env_vars,
                    "source": "custom",
                    "path": str(path)
                })
        except Exception as e:
            print(f"[api_mcp_available] Error reading custom path {custom_path}: {e}", file=sys.stderr)
            continue

    return JSONResponse(servers)

@require_auth
async def api_webhooks_handler(request):
    """List all webhooks for dashboard"""
    conn = get_db()
    webhooks = conn.execute("SELECT * FROM webhooks ORDER BY created_at DESC").fetchall()
    conn.close()

    base_url = get_webhook_base_url()
    result = []
    for w in webhooks:
        webhook_dict = dict(w)
        webhook_dict['url'] = f"{base_url}/webhook/{w['secret_token']}"
        result.append(webhook_dict)

    return JSONResponse(result)

@require_auth
async def api_tool_logs_handler(request):
    """List recent tool invocation logs for dashboard"""
    conn = get_db()
    logs = conn.execute("SELECT * FROM tool_logs ORDER BY created_at DESC LIMIT 100").fetchall()
    conn.close()
    return JSONResponse([dict(l) for l in logs])

async def webhook_trigger_handler(request):
    """Handle incoming webhook POST requests"""
    token = request.path_params.get("token")

    conn = get_db()
    try:
        webhook = conn.execute("SELECT * FROM webhooks WHERE secret_token = ?", (token,)).fetchone()

        if not webhook:
            return JSONResponse({"error": "Webhook not found"}, status_code=404)

        if not webhook["enabled"]:
            return JSONResponse({"error": "Webhook is disabled"}, status_code=403)

        print(f"[webhook] Trigger received for '{webhook['name']}' (id={webhook['id']})", file=sys.stderr)

        # Parse JSON payload
        try:
            payload = await request.json()
        except:
            payload = {}

        # Render the prompt template with payload data
        prompt = render_webhook_template(webhook["prompt_template"], payload)

        # Create a disabled job for this webhook run (follows quick-run pattern)
        job_id = str(uuid.uuid4())[:8]
        now = utc_now_iso()

        conn.execute("""
            INSERT INTO jobs (id, name, cron, prompt, command, tools, environment, enabled, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (job_id, f"Webhook: {webhook['name']} ({now[:16]})", "webhook", prompt, "claude", "[]", "{}", 0, now, now))

        # Create the run with webhook_id
        run_id = str(uuid.uuid4())[:8]
        conn.execute("""
            INSERT INTO runs (id, job_id, started_at, prompt, command, state, webhook_id)
            VALUES (?, ?, ?, ?, ?, 'pending', ?)
        """, (run_id, job_id, now, prompt, "claude", webhook["id"]))

        # Update webhook stats
        conn.execute("""
            UPDATE webhooks SET
                last_triggered_at = ?,
                trigger_count = trigger_count + 1
            WHERE id = ?
        """, (now, webhook["id"]))

        conn.commit()

        print(f"[webhook] Created run {run_id} for webhook '{webhook['name']}' (job_id={job_id})", file=sys.stderr)

        # Run will be picked up by run_processor_loop (within 5 seconds)
        return JSONResponse({
            "success": True,
            "run_id": run_id,
            "webhook_id": webhook["id"],
            "message": "Webhook triggered successfully"
        })
    except Exception as e:
        print(f"[webhook] Error triggering webhook (token={token[:8]}...): {e}", file=sys.stderr)
        return JSONResponse({"error": str(e)}, status_code=500)
    finally:
        conn.close()

@require_auth
async def api_delete_job_handler(request):
    """Delete a job and its runs"""
    job_id = request.path_params['job_id']
    conn = get_db()
    # Delete associated runs first
    conn.execute("DELETE FROM runs WHERE job_id = ?", (job_id,))
    result = conn.execute("DELETE FROM jobs WHERE id = ?", (job_id,))
    conn.commit()
    deleted = result.rowcount > 0
    conn.close()
    if deleted:
        return JSONResponse({"success": True})
    else:
        return JSONResponse({"error": "Job not found"}, status_code=404)

# === Fixed and Dynamic Servers API Endpoints ===

@require_auth
async def api_fixed_servers_handler(request):
    """List all fixed MCP servers with credential status"""
    servers = _list_all_fixed_servers()
    return JSONResponse(servers)

@require_auth
async def api_dynamic_servers_handler(request):
    """List all dynamic MCP servers with credential status"""
    servers = _list_all_dynamic_servers()
    return JSONResponse(servers)

@require_auth
async def api_server_credential_handler(request):
    """Set a credential for an MCP server"""
    try:
        body = await request.json()
        server_name = body.get('server_name')
        var_name = body.get('var_name')
        value = body.get('value')

        if not server_name or not var_name or not value:
            return JSONResponse({"error": "Missing required fields"}, status_code=400)

        _set_server_credential(server_name, var_name, value)
        return JSONResponse({"success": True})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)

@require_auth
async def api_fixed_server_toggle_handler(request):
    """Enable or disable a fixed server"""
    try:
        body = await request.json()
        name = body.get('name')
        enabled = body.get('enabled', True)

        if not name:
            return JSONResponse({"error": "Missing server name"}, status_code=400)

        if enabled:
            _add_fixed_server_to_config(name)
        else:
            _remove_fixed_server_from_config(name)

        return JSONResponse({"success": True, "enabled": enabled})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)

@require_auth
async def api_dynamic_server_toggle_handler(request):
    """Enable or disable a dynamic server"""
    try:
        body = await request.json()
        name = body.get('name')
        enabled = body.get('enabled', True)

        if not name:
            return JSONResponse({"error": "Missing server name"}, status_code=400)

        if enabled:
            _add_dynamic_server_to_config(name)
        else:
            _remove_dynamic_server_from_config(name)

        return JSONResponse({"success": True, "enabled": enabled})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)

# === OAuth 2.1 Endpoints ===

async def oauth_protected_resource_handler(request):
    """Return OAuth Protected Resource Metadata (RFC 9728)."""
    server_url = get_server_url()
    return JSONResponse({
        "resource": f"{server_url}/mcp",
        "authorization_servers": [server_url],
        "scopes_supported": ["mcp"],
    })

async def oauth_server_metadata_handler(request):
    """Return OAuth Authorization Server Metadata (RFC 8414)."""
    server_url = get_server_url()
    return JSONResponse({
        "issuer": server_url,
        "authorization_endpoint": f"{server_url}/oauth/authorize",
        "token_endpoint": f"{server_url}/oauth/token",
        "registration_endpoint": f"{server_url}/oauth/register",
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code", "refresh_token"],
        "code_challenge_methods_supported": ["S256"],
        "scopes_supported": ["mcp"],
    })

async def oauth_register_handler(request):
    """Dynamic Client Registration (RFC 7591) - DISABLED for security.

    This server uses static client credentials instead of DCR.
    Get your client_id and client_secret from the server startup logs
    or set OAUTH_CLIENT_ID and OAUTH_CLIENT_SECRET environment variables.
    """
    return JSONResponse({
        "error": "registration_not_supported",
        "error_description": "Dynamic client registration is disabled. Use the client_id and client_secret from the server startup logs or set OAUTH_CLIENT_ID and OAUTH_CLIENT_SECRET environment variables."
    }, status_code=400)

async def oauth_authorize_get_handler(request):
    """OAuth Authorization Endpoint - GET (show consent page or auto-approve)."""
    params = dict(request.query_params)

    client_id = params.get("client_id")
    redirect_uri = params.get("redirect_uri")
    response_type = params.get("response_type")
    state = params.get("state", "")
    code_challenge = params.get("code_challenge")
    code_challenge_method = params.get("code_challenge_method", "S256")

    # Validate required params
    if not client_id or not redirect_uri or response_type != "code":
        return JSONResponse({"error": "invalid_request"}, status_code=400)

    # Validate redirect URI
    if redirect_uri not in ALLOWED_REDIRECT_URIS:
        return JSONResponse({"error": "invalid_redirect_uri"}, status_code=400)

    # Validate client exists
    conn = get_db()
    client = conn.execute("SELECT * FROM oauth_clients WHERE client_id = ?", (client_id,)).fetchone()
    conn.close()

    if not client:
        return JSONResponse({"error": "invalid_client"}, status_code=400)

    # PKCE is required
    if not code_challenge:
        return JSONResponse({"error": "invalid_request", "error_description": "code_challenge required"}, status_code=400)

    # Auto-approve: generate authorization code immediately
    code = secrets.token_urlsafe(32)
    expires_at = (datetime.now(timezone.utc) + timedelta(seconds=OAUTH_CODE_EXPIRY)).isoformat()

    conn = get_db()
    conn.execute("""
        INSERT INTO oauth_codes (code, client_id, redirect_uri, code_challenge, code_challenge_method, expires_at)
        VALUES (?, ?, ?, ?, ?, ?)
    """, (code, client_id, redirect_uri, code_challenge, code_challenge_method, expires_at))
    conn.commit()
    conn.close()

    # Redirect back with authorization code
    from urllib.parse import urlencode
    redirect_params = {"code": code}
    if state:
        redirect_params["state"] = state
    redirect_url = f"{redirect_uri}?{urlencode(redirect_params)}"

    return Response(
        status_code=302,
        headers={"Location": redirect_url}
    )

async def oauth_token_handler(request):
    """OAuth Token Endpoint - exchange code for access token."""
    try:
        # Support both form-encoded and JSON
        content_type = request.headers.get("content-type", "")
        if "application/json" in content_type:
            body = await request.json()
        else:
            form = await request.form()
            body = dict(form)
    except Exception:
        return JSONResponse({"error": "invalid_request"}, status_code=400)

    grant_type = body.get("grant_type")
    client_id = body.get("client_id")
    client_secret = body.get("client_secret")

    # Handle refresh_token grant type
    if grant_type == "refresh_token":
        refresh_token = body.get("refresh_token")
        if not refresh_token or not client_id:
            return JSONResponse({"error": "invalid_request"}, status_code=400)

        conn = get_db()

        # Verify client exists
        client = conn.execute("SELECT * FROM oauth_clients WHERE client_id = ?", (client_id,)).fetchone()
        if not client:
            conn.close()
            return JSONResponse({"error": "invalid_client"}, status_code=401)

        # Verify refresh token
        token_row = conn.execute(
            "SELECT * FROM oauth_refresh_tokens WHERE token = ? AND client_id = ?",
            (refresh_token, client_id)
        ).fetchone()

        if not token_row or token_row["revoked"]:
            conn.close()
            return JSONResponse({"error": "invalid_grant"}, status_code=400)

        # Check expiry
        expires_at = datetime.fromisoformat(token_row["expires_at"])
        if datetime.now(timezone.utc) > expires_at:
            conn.close()
            return JSONResponse({"error": "invalid_grant", "error_description": "Refresh token expired"}, status_code=400)

        conn.close()

        # Issue new access token
        access_token = generate_oauth_jwt(client_id, ["mcp"])

        return JSONResponse({
            "access_token": access_token,
            "token_type": "Bearer",
            "expires_in": OAUTH_TOKEN_EXPIRY,
            "scope": "mcp",
        })

    # Handle authorization_code grant type
    elif grant_type == "authorization_code":
        code = body.get("code")
        redirect_uri = body.get("redirect_uri")
        code_verifier = body.get("code_verifier")

        if not code or not redirect_uri or not client_id or not code_verifier:
            return JSONResponse({"error": "invalid_request"}, status_code=400)

        conn = get_db()

        # Verify client
        client = conn.execute("SELECT * FROM oauth_clients WHERE client_id = ?", (client_id,)).fetchone()
        if not client:
            conn.close()
            return JSONResponse({"error": "invalid_client"}, status_code=401)

        # Verify client secret if provided
        if client_secret and not verify_client_secret(client_secret, client["client_secret_hash"]):
            conn.close()
            return JSONResponse({"error": "invalid_client"}, status_code=401)

        # Verify authorization code
        auth_code = conn.execute("SELECT * FROM oauth_codes WHERE code = ?", (code,)).fetchone()
        if not auth_code:
            conn.close()
            return JSONResponse({"error": "invalid_grant"}, status_code=400)

        # Check code is not used
        if auth_code["used"]:
            conn.close()
            return JSONResponse({"error": "invalid_grant", "error_description": "Code already used"}, status_code=400)

        # Check code is not expired
        expires_at = datetime.fromisoformat(auth_code["expires_at"])
        if datetime.now(timezone.utc) > expires_at:
            conn.close()
            return JSONResponse({"error": "invalid_grant", "error_description": "Code expired"}, status_code=400)

        # Verify redirect_uri matches
        if auth_code["redirect_uri"] != redirect_uri:
            conn.close()
            return JSONResponse({"error": "invalid_grant"}, status_code=400)

        # Verify client_id matches
        if auth_code["client_id"] != client_id:
            conn.close()
            return JSONResponse({"error": "invalid_grant"}, status_code=400)

        # Verify PKCE
        if not verify_pkce(code_verifier, auth_code["code_challenge"], auth_code["code_challenge_method"] or "S256"):
            conn.close()
            return JSONResponse({"error": "invalid_grant", "error_description": "PKCE verification failed"}, status_code=400)

        # Mark code as used
        conn.execute("UPDATE oauth_codes SET used = 1 WHERE code = ?", (code,))
        conn.commit()
        conn.close()

        # Generate access token and refresh token
        access_token = generate_oauth_jwt(client_id, ["mcp"])
        refresh_token, _ = generate_refresh_token(client_id)

        return JSONResponse({
            "access_token": access_token,
            "token_type": "Bearer",
            "expires_in": OAUTH_TOKEN_EXPIRY,
            "refresh_token": refresh_token,
            "scope": "mcp",
        })

    else:
        return JSONResponse({"error": "unsupported_grant_type"}, status_code=400)

def get_resource_metadata_url() -> str:
    """Get the URL for the OAuth protected resource metadata."""
    return f"{get_server_url()}/.well-known/oauth-protected-resource"

async def check_oauth_token(request) -> Response | None:
    """Check OAuth Bearer token. Returns 401 Response if auth fails, None if OK."""
    auth_header = request.headers.get("Authorization", "")

    if not auth_header.startswith("Bearer "):
        return JSONResponse(
            {"error": "unauthorized"},
            status_code=401,
            headers={
                "WWW-Authenticate": f'Bearer realm="mcp", resource_metadata="{get_resource_metadata_url()}"'
            }
        )

    token = auth_header[7:]
    claims = verify_oauth_jwt(token)

    if not claims:
        return JSONResponse(
            {"error": "invalid_token"},
            status_code=401,
            headers={
                "WWW-Authenticate": f'Bearer realm="mcp", error="invalid_token", resource_metadata="{get_resource_metadata_url()}"'
            }
        )

    return None  # Auth successful

def require_oauth(handler):
    """Decorator to require OAuth Bearer token for a route handler."""
    @wraps(handler)
    async def wrapper(request, *args, **kwargs):
        auth_response = await check_oauth_token(request)
        if auth_response:
            return auth_response
        return await handler(request, *args, **kwargs)
    return wrapper

class OAuthMiddleware:
    """Starlette middleware to protect /sse and /mcp endpoints with OAuth."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            path = scope.get("path", "")
            # Protect SSE and MCP endpoints
            if path.startswith("/sse") or path.startswith("/mcp"):
                # Check for Authorization header
                headers = dict(scope.get("headers", []))
                auth_header = headers.get(b"authorization", b"").decode("utf-8")

                if not auth_header.startswith("Bearer "):
                    # Return 401 with WWW-Authenticate header
                    response = JSONResponse(
                        {"error": "unauthorized"},
                        status_code=401,
                        headers={
                            "WWW-Authenticate": f'Bearer realm="mcp", resource_metadata="{get_resource_metadata_url()}"'
                        }
                    )
                    await response(scope, receive, send)
                    return

                token = auth_header[7:]
                claims = verify_oauth_jwt(token)

                if not claims:
                    response = JSONResponse(
                        {"error": "invalid_token"},
                        status_code=401,
                        headers={
                            "WWW-Authenticate": f'Bearer realm="mcp", error="invalid_token", resource_metadata="{get_resource_metadata_url()}"'
                        }
                    )
                    await response(scope, receive, send)
                    return

        # Continue to the actual app
        await self.app(scope, receive, send)

@require_auth
async def api_create_job_handler(request):
    """Create a new job via REST API"""
    try:
        body = await request.json()
        name = body.get('name', '').strip()
        cron = body.get('cron', '').strip()
        prompt = body.get('prompt', '').strip()
        command = body.get('command', 'claude')
        tools = body.get('tools', '[]')
        environment = body.get('environment', '{}')
        timeout_minutes = body.get('timeout_minutes', 30)

        if not name:
            return JSONResponse({"error": "name is required"}, status_code=400)
        if not cron:
            return JSONResponse({"error": "cron is required"}, status_code=400)
        if not prompt:
            return JSONResponse({"error": "prompt is required"}, status_code=400)

        if isinstance(tools, list):
            tools = json.dumps(tools)
        if isinstance(environment, dict):
            environment = json.dumps(environment)

        result = json.loads(create_job(name, cron, prompt, command, tools, environment, timeout_minutes))
        if "error" in result:
            return JSONResponse(result, status_code=400)
        return JSONResponse(result)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)

@require_auth
async def api_get_job_handler(request):
    """Get a single job by ID"""
    job_id = request.path_params['job_id']
    conn = get_db()
    job = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    conn.close()
    if not job:
        return JSONResponse({"error": "Job not found"}, status_code=404)
    return JSONResponse(dict(job))

@require_auth
async def api_update_job_handler(request):
    """Update a job via REST API"""
    job_id = request.path_params['job_id']
    try:
        body = await request.json()
        name = body.get('name')
        cron = body.get('cron')
        prompt = body.get('prompt')
        enabled = body.get('enabled')
        timeout_minutes = body.get('timeout_minutes')
        tools = body.get('tools')

        if tools is not None and isinstance(tools, list):
            tools = json.dumps(tools)

        result = json.loads(update_job(job_id, name=name, cron=cron, prompt=prompt, enabled=enabled, timeout_minutes=timeout_minutes, tools=tools))
        if "error" in result:
            return JSONResponse(result, status_code=400)
        return JSONResponse(result)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)

@require_auth
async def api_trigger_job_handler(request):
    """Trigger a job to run immediately via REST API"""
    job_id = request.path_params['job_id']
    result = json.loads(trigger_job(job_id))
    if "error" in result:
        return JSONResponse(result, status_code=404)
    return JSONResponse(result)

class CORSMiddleware:
    """Simple CORS middleware for cross-origin requests from the SFLOW demo app."""
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        from starlette.requests import Request
        request = Request(scope, receive)
        origin = request.headers.get("origin", "")

        if request.method == "OPTIONS":
            response = Response(
                status_code=204,
                headers={
                    "Access-Control-Allow-Origin": origin or "*",
                    "Access-Control-Allow-Methods": "GET, POST, PUT, DELETE, OPTIONS",
                    "Access-Control-Allow-Headers": "Authorization, Content-Type, ngrok-skip-browser-warning",
                    "Access-Control-Max-Age": "86400",
                },
            )
            await response(scope, receive, send)
            return

        async def send_with_cors(message):
            if message["type"] == "http.response.start":
                headers = dict(message.get("headers", []))
                cors_headers = [
                    (b"access-control-allow-origin", (origin or "*").encode()),
                    (b"access-control-allow-credentials", b"true"),
                ]
                message["headers"] = list(message.get("headers", [])) + cors_headers
            await send(message)

        await self.app(scope, receive, send_with_cors)

dashboard_routes = [
    Route("/", dashboard_handler),
    Route("/dashboard", dashboard_handler),
    Route("/static/{filename}", static_file_handler),
    Route("/api/jobs", api_jobs_handler, methods=["GET"]),
    Route("/api/jobs", api_create_job_handler, methods=["POST"]),
    Route("/api/runs", api_runs_handler),
    Route("/api/run/{run_id}", api_run_detail_handler),
    Route("/api/run/{run_id}/kill", api_kill_run_handler, methods=["POST"]),
    Route("/api/run-prompt", api_run_prompt_handler, methods=["POST"]),
    Route("/api/job/{job_id}", api_get_job_handler, methods=["GET"]),
    Route("/api/job/{job_id}", api_update_job_handler, methods=["PUT"]),
    Route("/api/job/{job_id}", api_delete_job_handler, methods=["DELETE"]),
    Route("/api/job/{job_id}/trigger", api_trigger_job_handler, methods=["POST"]),
    Route("/api/stats", api_stats_handler),
    Route("/api/ngrok", api_ngrok_handler),
    Route("/api/settings", api_settings_get_handler, methods=["GET"]),
    Route("/api/settings", api_settings_post_handler, methods=["POST"]),
    Route("/api/mcp-available", api_mcp_available_handler, methods=["GET"]),
    Route("/api/fixed-servers", api_fixed_servers_handler, methods=["GET"]),
    Route("/api/dynamic-servers", api_dynamic_servers_handler, methods=["GET"]),
    Route("/api/server-credential", api_server_credential_handler, methods=["POST"]),
    Route("/api/fixed-server-toggle", api_fixed_server_toggle_handler, methods=["POST"]),
    Route("/api/dynamic-server-toggle", api_dynamic_server_toggle_handler, methods=["POST"]),
    Route("/api/webhooks", api_webhooks_handler),
    Route("/api/tool-logs", api_tool_logs_handler),
    Route("/webhook/{token}", webhook_trigger_handler, methods=["POST"]),
    # OAuth 2.1 endpoints
    Route("/.well-known/oauth-protected-resource", oauth_protected_resource_handler),
    Route("/.well-known/oauth-authorization-server", oauth_server_metadata_handler),
    Route("/oauth/register", oauth_register_handler, methods=["POST"]),
    Route("/oauth/authorize", oauth_authorize_get_handler, methods=["GET"]),
    Route("/oauth/token", oauth_token_handler, methods=["POST"]),
]

@mcp.tool()
def list_jobs() -> str:
    """List all scheduled jobs"""
    conn = get_db()
    jobs = conn.execute("SELECT * FROM jobs ORDER BY created_at DESC").fetchall()
    conn.close()
    return json.dumps([dict(j) for j in jobs], indent=2)

@mcp.tool()
def get_job(job_id: str) -> str:
    """Get a specific job by ID"""
    conn = get_db()
    job = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    conn.close()
    if not job:
        return json.dumps({"error": "Job not found"})
    return json.dumps(dict(job), indent=2)

@mcp.tool()
def create_job(name: str, cron: str, prompt: str, command: str = "claude", tools: str = "[]", environment: str = "{}", timeout_minutes: int = 30) -> str:
    """
    Create a new scheduled job.

    Args:
        name: Human-readable job name
        cron: Cron expression (e.g., "0 9 * * 1-5" for weekdays at 9am)
        prompt: The prompt to send to Claude Code
        command: CLI command (default: "claude")
        tools: JSON array of MCP tools to enable. Tool names are auto-normalized:
               - "email:send_email" -> "mcp__email__send_email"
               - "email.send_email" -> "mcp__email__send_email"
               Built-in tools (Bash, Read, etc.) and mcp__* format are unchanged.
        environment: JSON object of environment variables
        timeout_minutes: Max runtime before killing the process (default: 30)

    Returns:
        JSON with success status, job_id, and optional tool_warnings if any tools were normalized.
    """
    # Validate cron expression
    try:
        croniter(cron)
    except Exception as e:
        return json.dumps({"error": f"Invalid cron expression: {e}"})

    # Normalize tool names (e.g., "email:send_email" -> "mcp__email__send_email")
    normalized_tools, tool_warnings = _normalize_tools_list(tools)

    job_id = str(uuid.uuid4())[:8]
    now = utc_now_iso()

    conn = get_db()
    conn.execute("""
        INSERT INTO jobs (id, name, cron, prompt, command, tools, environment, timeout_minutes, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (job_id, name, cron, prompt, command, normalized_tools, environment, timeout_minutes, now, now))
    conn.commit()
    conn.close()

    result = {"success": True, "job_id": job_id}
    if tool_warnings:
        result["tool_warnings"] = tool_warnings
    return json.dumps(result)

@mcp.tool()
def update_job(job_id: str, name: str = None, cron: str = None, prompt: str = None, enabled: bool = None, timeout_minutes: int = None, tools: str = None) -> str:
    """Update an existing job.

    Args:
        job_id: The job ID to update
        name: New job name
        cron: New cron expression
        prompt: New prompt
        enabled: Enable/disable the job
        timeout_minutes: Max runtime in minutes
        tools: JSON array of tools (e.g., '["mcp__email__send_email"]')
    """
    conn = get_db()
    job = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    if not job:
        conn.close()
        return json.dumps({"error": "Job not found"})

    updates = []
    params = []
    tool_warnings = []

    if name is not None:
        updates.append("name = ?")
        params.append(name)
    if cron is not None:
        try:
            croniter(cron)
        except Exception as e:
            conn.close()
            return json.dumps({"error": f"Invalid cron expression: {e}"})
        updates.append("cron = ?")
        params.append(cron)
    if prompt is not None:
        updates.append("prompt = ?")
        params.append(prompt)
    if enabled is not None:
        updates.append("enabled = ?")
        params.append(1 if enabled else 0)
    if timeout_minutes is not None:
        updates.append("timeout_minutes = ?")
        params.append(timeout_minutes)
    if tools is not None:
        # Normalize tool names (e.g., "email:send_email" -> "mcp__email__send_email")
        normalized_tools, tool_warnings = _normalize_tools_list(tools)
        updates.append("tools = ?")
        params.append(normalized_tools)

    if updates:
        updates.append("updated_at = ?")
        params.append(utc_now_iso())
        params.append(job_id)

        conn.execute(f"UPDATE jobs SET {', '.join(updates)} WHERE id = ?", params)
        conn.commit()

    conn.close()
    result = {"success": True}
    if tool_warnings:
        result["tool_warnings"] = tool_warnings
    return json.dumps(result)

@mcp.tool()
def delete_job(job_id: str) -> str:
    """Delete a job and all its runs"""
    conn = get_db()
    conn.execute("DELETE FROM runs WHERE job_id = ?", (job_id,))
    result = conn.execute("DELETE FROM jobs WHERE id = ?", (job_id,))
    conn.commit()
    deleted = result.rowcount > 0
    conn.close()
    return json.dumps({"success": deleted})

@mcp.tool()
def list_runs(job_id: str = None, webhook_id: str = None, limit: int = 20) -> str:
    """List recent runs, optionally filtered by job_id or webhook_id"""
    conn = get_db()
    if webhook_id:
        runs = conn.execute(
            "SELECT * FROM runs WHERE webhook_id = ? ORDER BY started_at DESC LIMIT ?",
            (webhook_id, limit)
        ).fetchall()
    elif job_id:
        runs = conn.execute(
            "SELECT * FROM runs WHERE job_id = ? ORDER BY started_at DESC LIMIT ?",
            (job_id, limit)
        ).fetchall()
    else:
        runs = conn.execute(
            "SELECT * FROM runs ORDER BY started_at DESC LIMIT ?",
            (limit,)
        ).fetchall()
    conn.close()
    return json.dumps([dict(r) for r in runs], indent=2)

@mcp.tool()
def get_run(run_id: str) -> str:
    """Get details of a specific run including full output"""
    conn = get_db()
    run = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
    conn.close()
    if not run:
        return json.dumps({"error": "Run not found"})
    return json.dumps(dict(run), indent=2)

@mcp.tool()
def kill_run(run_id: str) -> str:
    """Force-kill a running job/run by its ID. Only works for runs in 'running' state."""
    # Check if task is in our tracking dict
    task = RUNNING_TASKS.get(run_id)
    if not task:
        # Check if the run exists and its current state
        conn = get_db()
        run = conn.execute("SELECT state FROM runs WHERE id = ?", (run_id,)).fetchone()
        conn.close()
        if not run:
            return json.dumps({"success": False, "error": "Run not found"})
        if run["state"] != "running":
            return json.dumps({"success": False, "error": f"Run is not running (state: {run['state']})"})
        return json.dumps({"success": False, "error": "Run not found in active tasks (may have already finished)"})

    # Cancel the asyncio task
    task.cancel()

    # Update database state
    conn = get_db()
    conn.execute("""
        UPDATE runs SET
            state = 'error',
            error = 'Killed by user',
            finished_at = ?
        WHERE id = ?
    """, (utc_now_iso(), run_id))
    conn.commit()
    conn.close()

    # Remove from tracking
    RUNNING_TASKS.pop(run_id, None)

    return json.dumps({"success": True, "message": f"Run {run_id} killed"})

@mcp.tool()
def trigger_job(job_id: str) -> str:
    """Manually trigger a job to run immediately"""
    conn = get_db()
    job = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    if not job:
        conn.close()
        return json.dumps({"error": "Job not found"})

    run_id = create_run(dict(job))
    conn.close()
    return json.dumps({"success": True, "run_id": run_id})

# === Webhook Tools ===

@mcp.tool()
def create_webhook(name: str, prompt_template: str, description: str = "") -> str:
    """
    Create a webhook endpoint that executes a prompt when triggered via HTTP POST.

    The prompt_template can use placeholders:
    - {{payload}} - Full JSON payload as string
    - {{payload.field}} - Specific field from payload
    - {{payload.field.subfield}} - Nested field access

    Example template:
    "A new work item was created: {{payload.resource.fields.System.Title}}"

    Returns the full webhook URL with security token.
    """
    webhook_id = str(uuid.uuid4())[:8]
    secret_token = uuid.uuid4().hex  # 32-char hex token
    now = utc_now_iso()

    conn = get_db()
    conn.execute("""
        INSERT INTO webhooks (id, name, description, secret_token, prompt_template, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (webhook_id, name, description, secret_token, prompt_template, now, now))
    conn.commit()

    conn.close()

    base_url = get_webhook_base_url()
    webhook_url = f"{base_url}/webhook/{secret_token}"

    return json.dumps({
        "success": True,
        "webhook_id": webhook_id,
        "url": webhook_url,
        "name": name,
        "message": f"Webhook created! Configure this URL in your external service: {webhook_url}"
    }, indent=2)

@mcp.tool()
def list_webhooks() -> str:
    """List all webhook endpoints"""
    conn = get_db()
    webhooks = conn.execute("SELECT * FROM webhooks ORDER BY created_at DESC").fetchall()
    conn.close()

    base_url = get_webhook_base_url()
    result = []
    for w in webhooks:
        webhook_dict = dict(w)
        webhook_dict['url'] = f"{base_url}/webhook/{w['secret_token']}"
        result.append(webhook_dict)

    return json.dumps(result, indent=2)

@mcp.tool()
def get_webhook(webhook_id: str) -> str:
    """Get a specific webhook by ID"""
    conn = get_db()
    webhook = conn.execute("SELECT * FROM webhooks WHERE id = ?", (webhook_id,)).fetchone()
    if not webhook:
        conn.close()
        return json.dumps({"error": "Webhook not found"})

    conn.close()

    base_url = get_webhook_base_url()
    result = dict(webhook)
    result['url'] = f"{base_url}/webhook/{webhook['secret_token']}"
    return json.dumps(result, indent=2)

@mcp.tool()
def update_webhook(webhook_id: str, name: str = None, prompt_template: str = None, description: str = None, enabled: bool = None) -> str:
    """Update an existing webhook"""
    conn = get_db()
    webhook = conn.execute("SELECT * FROM webhooks WHERE id = ?", (webhook_id,)).fetchone()
    if not webhook:
        conn.close()
        return json.dumps({"error": "Webhook not found"})

    updates = []
    params = []

    if name is not None:
        updates.append("name = ?")
        params.append(name)
    if prompt_template is not None:
        updates.append("prompt_template = ?")
        params.append(prompt_template)
    if description is not None:
        updates.append("description = ?")
        params.append(description)
    if enabled is not None:
        updates.append("enabled = ?")
        params.append(1 if enabled else 0)

    if updates:
        updates.append("updated_at = ?")
        params.append(utc_now_iso())
        params.append(webhook_id)

        conn.execute(f"UPDATE webhooks SET {', '.join(updates)} WHERE id = ?", params)
        conn.commit()

    conn.close()
    return json.dumps({"success": True})

@mcp.tool()
def delete_webhook(webhook_id: str) -> str:
    """Delete a webhook endpoint"""
    conn = get_db()
    result = conn.execute("DELETE FROM webhooks WHERE id = ?", (webhook_id,))
    conn.commit()
    deleted = result.rowcount > 0
    conn.close()
    return json.dumps({"success": deleted})

# === Email Tools ===
# NOTE: Email tools have been moved to fixed-servers/email/server.py
# The email fixed server is auto-registered on startup and provides:
# - set_email_config: Configure email provider settings
# - get_email_config: Get current email configuration
# - send_email: Send an email via SendGrid or Mailgun

# === Credential Management MCP Tools ===

@mcp.tool()
def set_server_credential(server_name: str, var_name: str, value: str) -> str:
    """
    Set a credential/environment variable for an MCP server.

    Credentials are stored securely in the database and injected when the server runs.

    Args:
        server_name: The MCP server identifier (e.g., "email", "azure-devops-workitems")
        var_name: The environment variable name (e.g., "API_KEY", "SENDGRID_API_KEY")
        value: The value to set
    """
    _set_server_credential(server_name, var_name, value)
    return json.dumps({
        "success": True,
        "server_name": server_name,
        "var_name": var_name,
        "message": f"Credential '{var_name}' set for server '{server_name}'"
    })

@mcp.tool()
def get_server_credentials(server_name: str) -> str:
    """
    Get configured credentials for an MCP server (values are masked for security).

    Args:
        server_name: The MCP server identifier
    """
    metadata = _get_dynamic_server_metadata(server_name)
    env_var_defs = metadata.get("env_vars", []) if metadata else []
    configured_vars = _get_server_env_vars(server_name)

    # Build response with masked values
    credentials = []
    for var_def in env_var_defs:
        var_name = var_def["name"]
        value = configured_vars.get(var_name)
        credentials.append({
            "name": var_name,
            "description": var_def.get("description", ""),
            "required": var_def.get("required", True),
            "sensitive": var_def.get("sensitive", False),
            "configured": value is not None,
            "value_preview": (value[:4] + "..." + value[-4:] if len(value) > 12 else "***") if value and var_def.get("sensitive", False) else value
        })

    cred_status = _check_server_credentials_configured(server_name)
    return json.dumps({
        "server_name": server_name,
        "credentials": credentials,
        "all_configured": cred_status["configured"],
        "missing": cred_status["missing"]
    }, indent=2)

@mcp.tool()
def list_required_credentials(server_name: str) -> str:
    """
    List what credentials/environment variables an MCP server requires.

    Args:
        server_name: The MCP server identifier
    """
    metadata = _get_dynamic_server_metadata(server_name)
    if not metadata:
        return json.dumps({"error": f"Server '{server_name}' not found"})

    env_var_defs = metadata.get("env_vars", [])
    cred_status = _check_server_credentials_configured(server_name)

    return json.dumps({
        "server_name": server_name,
        "env_vars": env_var_defs,
        "all_configured": cred_status["configured"],
        "missing": cred_status["missing"]
    }, indent=2)

@mcp.tool()
def get_unconfigured_servers() -> str:
    """
    List all MCP servers (both dynamic and fixed) that have missing required credentials.

    Returns servers that need configuration before they can be used.
    """
    # Check both dynamic and fixed servers
    dynamic_servers = _list_all_dynamic_servers()
    fixed_servers = _list_all_fixed_servers()
    all_servers = dynamic_servers + fixed_servers

    unconfigured = []
    for server in all_servers:
        if not server.get("credentials_configured", True):
            unconfigured.append({
                "name": server["name"],
                "description": server.get("description", ""),
                "type": "fixed" if server.get("built_in") else "dynamic",
                "missing_credentials": server.get("missing_credentials", []),
                "env_vars": server.get("env_vars", [])
            })

    return json.dumps({
        "unconfigured_servers": unconfigured,
        "count": len(unconfigured),
        "message": "All servers configured!" if len(unconfigured) == 0 else f"{len(unconfigured)} server(s) need credential configuration"
    }, indent=2)

@mcp.tool()
def delete_server_credential(server_name: str, var_name: str) -> str:
    """
    Delete a credential/environment variable for an MCP server.

    Args:
        server_name: The MCP server identifier
        var_name: The environment variable name to delete
    """
    env_vars = _get_mcp_env_vars_setting()
    prefixed_key = f"{server_name}_{var_name}"

    if prefixed_key in env_vars:
        del env_vars[prefixed_key]
        _save_mcp_env_vars_setting(env_vars)
        return json.dumps({
            "success": True,
            "server_name": server_name,
            "var_name": var_name,
            "message": f"Credential '{var_name}' deleted for server '{server_name}'"
        })
    else:
        return json.dumps({
            "success": False,
            "error": f"Credential '{var_name}' not found for server '{server_name}'"
        })

# === Fixed MCP Server Management Tools ===

@mcp.tool()
def list_fixed_mcp_servers() -> str:
    """
    List all fixed (built-in) MCP servers.

    Fixed servers are pre-installed servers that provide core functionality
    like email sending. They are auto-registered on startup.
    """
    servers = _list_all_fixed_servers()
    return json.dumps({
        "servers": servers,
        "count": len(servers)
    }, indent=2)

@mcp.tool()
def enable_fixed_server(name: str) -> str:
    """
    Enable a fixed MCP server.

    Args:
        name: The server identifier (e.g., "email")
    """
    server_dir = _get_fixed_server_path(name)
    if not server_dir.exists():
        return json.dumps({
            "error": f"Fixed server '{name}' does not exist."
        })

    if _is_fixed_server_enabled(name):
        return json.dumps({
            "success": True,
            "name": name,
            "message": f"Fixed server '{name}' is already enabled."
        })

    _add_fixed_server_to_config(name)
    return json.dumps({
        "success": True,
        "name": name,
        "message": f"Fixed server '{name}' enabled. Its tools will be available in subsequent job runs."
    })

@mcp.tool()
def disable_fixed_server(name: str) -> str:
    """
    Disable a fixed MCP server.

    The server files remain on disk but its tools won't be available in job runs.

    Args:
        name: The server identifier (e.g., "email")
    """
    server_dir = _get_fixed_server_path(name)
    if not server_dir.exists():
        return json.dumps({
            "error": f"Fixed server '{name}' does not exist."
        })

    if not _is_fixed_server_enabled(name):
        return json.dumps({
            "success": True,
            "name": name,
            "message": f"Fixed server '{name}' is already disabled."
        })

    _remove_fixed_server_from_config(name)
    return json.dumps({
        "success": True,
        "name": name,
        "message": f"Fixed server '{name}' disabled. Its tools will no longer be available in job runs."
    })

# === Dynamic MCP Server Management Tools ===

@mcp.tool()
def create_mcp_server(name: str, code: str, description: str = "", env_vars: str = "") -> str:
    """
    Create a new MCP server from Python code.

    Your code will have access to a DATA_DIR variable (Path object) pointing to
    the server's directory - use this for any data files to ensure portability.

    Example:
        from fastmcp import FastMCP
        mcp = FastMCP("My Tool")

        # Use DATA_DIR for any data files
        CONFIG_FILE = DATA_DIR / "config.json"

        @mcp.tool()
        def my_function(arg: str) -> str:
            return f"Result: {arg}"

        if __name__ == "__main__":
            mcp.run()

    Args:
        name: Unique identifier for the server (e.g., "weather-tool")
        code: Python code defining the MCP server
        description: Optional description of what this server does
        env_vars: Optional JSON array of environment variable declarations, e.g.:
            [{"name": "API_KEY", "description": "API key for service", "required": true, "sensitive": true}]
            If not provided, env vars will be auto-detected from os.environ.get() calls in the code.
    """
    import re as regex_module

    # Validate name (alphanumeric, hyphens, underscores only)
    if not regex_module.match(r'^[a-zA-Z][a-zA-Z0-9_-]*$', name):
        return json.dumps({
            "error": "Invalid name. Must start with a letter and contain only letters, numbers, hyphens, and underscores."
        })

    # Check if server already exists
    server_dir = _get_dynamic_server_path(name)
    if server_dir.exists():
        return json.dumps({
            "error": f"Server '{name}' already exists. Use update_mcp_server to modify it."
        })

    # Create server directory and save code
    server_dir.mkdir(parents=True, exist_ok=True)
    server_file = server_dir / "server.py"

    # Auto-inject DATA_DIR at the top of server code for portable data storage
    header = f'''# Auto-generated by create_mcp_server
# DATA_DIR: Safe directory for storing data files for this server
from pathlib import Path as _Path
DATA_DIR = _Path("{server_dir}")

'''
    final_code = header + code
    server_file.write_text(final_code)

    # Parse or auto-detect environment variables
    parsed_env_vars = []
    if env_vars:
        try:
            parsed_env_vars = json.loads(env_vars)
        except json.JSONDecodeError:
            return json.dumps({"error": "Invalid env_vars JSON format"})
    else:
        # Auto-detect env vars from os.environ.get() or os.getenv() calls
        detected_vars = set(regex_module.findall(r'os\.(?:environ\.get|getenv)\(["\']([A-Z_][A-Z0-9_]*)["\']', code))
        parsed_env_vars = [
            {"name": var, "description": f"Environment variable {var}", "required": True, "sensitive": "KEY" in var or "SECRET" in var or "TOKEN" in var or "PASSWORD" in var}
            for var in sorted(detected_vars)
        ]

    # Save metadata
    metadata = {
        "description": description,
        "created_at": utc_now_iso(),
        "updated_at": utc_now_iso(),
        "enabled": True,
        "env_vars": parsed_env_vars
    }
    _save_dynamic_server_metadata(name, metadata)

    # Add to mcp_servers config
    _add_dynamic_server_to_config(name)

    return json.dumps({
        "success": True,
        "name": name,
        "description": description,
        "file_path": str(server_file),
        "env_vars": parsed_env_vars,
        "message": f"MCP server '{name}' created and enabled. Its tools will be available in subsequent job runs." + (f" Detected {len(parsed_env_vars)} environment variable(s)." if parsed_env_vars else "")
    })

@mcp.tool()
def update_mcp_server(name: str, code: str, description: str = None) -> str:
    """
    Update an existing dynamic MCP server's code.

    Your code will have access to a DATA_DIR variable (Path object) pointing to
    the server's directory - use this for any data files to ensure portability.

    Args:
        name: The server identifier to update
        code: New Python code for the server
        description: Optional new description (keeps existing if not provided)
    """
    server_dir = _get_dynamic_server_path(name)
    if not server_dir.exists():
        return json.dumps({
            "error": f"Server '{name}' does not exist. Use create_mcp_server to create it."
        })

    # Update code with DATA_DIR injection
    server_file = server_dir / "server.py"
    header = f'''# Auto-generated by create_mcp_server
# DATA_DIR: Safe directory for storing data files for this server
from pathlib import Path as _Path
DATA_DIR = _Path("{server_dir}")

'''
    final_code = header + code
    server_file.write_text(final_code)

    # Update metadata
    metadata = _get_dynamic_server_metadata(name) or {}
    metadata["updated_at"] = utc_now_iso()
    if description is not None:
        metadata["description"] = description
    _save_dynamic_server_metadata(name, metadata)

    return json.dumps({
        "success": True,
        "name": name,
        "description": metadata.get("description", ""),
        "file_path": str(server_file),
        "message": f"MCP server '{name}' updated. Changes will take effect in subsequent job runs."
    })

@mcp.tool()
def delete_mcp_server(name: str) -> str:
    """
    Delete a dynamic MCP server completely.

    This removes the server from configuration and deletes its files.

    Args:
        name: The server identifier to delete
    """
    import shutil

    server_dir = _get_dynamic_server_path(name)
    if not server_dir.exists():
        return json.dumps({
            "error": f"Server '{name}' does not exist."
        })

    # Remove from config first
    _remove_dynamic_server_from_config(name)

    # Delete files
    shutil.rmtree(server_dir)

    return json.dumps({
        "success": True,
        "name": name,
        "message": f"MCP server '{name}' deleted."
    })

@mcp.tool()
def list_dynamic_mcp_servers() -> str:
    """
    List all dynamically created MCP servers.

    Returns a list of servers with their name, description, status, and file path.
    """
    servers = _list_all_dynamic_servers()
    return json.dumps({
        "servers": servers,
        "count": len(servers)
    }, indent=2)

@mcp.tool()
def get_dynamic_mcp_server(name: str) -> str:
    """
    Get details and code of a specific dynamic MCP server.

    Args:
        name: The server identifier to retrieve
    """
    server_dir = _get_dynamic_server_path(name)
    server_file = server_dir / "server.py"

    if not server_file.exists():
        return json.dumps({
            "error": f"Server '{name}' does not exist."
        })

    metadata = _get_dynamic_server_metadata(name) or {}
    code = server_file.read_text()

    return json.dumps({
        "name": name,
        "description": metadata.get("description", ""),
        "created_at": metadata.get("created_at", ""),
        "updated_at": metadata.get("updated_at", ""),
        "enabled": _is_dynamic_server_enabled(name),
        "file_path": str(server_file),
        "code": code
    }, indent=2)

@mcp.tool()
def enable_mcp_server(name: str) -> str:
    """
    Enable a disabled dynamic MCP server.

    The server's tools will be available in subsequent job runs.

    Args:
        name: The server identifier to enable
    """
    server_dir = _get_dynamic_server_path(name)
    if not server_dir.exists():
        return json.dumps({
            "error": f"Server '{name}' does not exist."
        })

    if _is_dynamic_server_enabled(name):
        return json.dumps({
            "success": True,
            "name": name,
            "message": f"MCP server '{name}' is already enabled."
        })

    # Add to config
    _add_dynamic_server_to_config(name)

    # Update metadata
    metadata = _get_dynamic_server_metadata(name) or {}
    metadata["enabled"] = True
    metadata["updated_at"] = utc_now_iso()
    _save_dynamic_server_metadata(name, metadata)

    return json.dumps({
        "success": True,
        "name": name,
        "message": f"MCP server '{name}' enabled. Its tools will be available in subsequent job runs."
    })

@mcp.tool()
def disable_mcp_server(name: str) -> str:
    """
    Disable a dynamic MCP server without deleting it.

    The server's files are kept on disk but its tools won't be available in job runs.

    Args:
        name: The server identifier to disable
    """
    server_dir = _get_dynamic_server_path(name)
    if not server_dir.exists():
        return json.dumps({
            "error": f"Server '{name}' does not exist."
        })

    if not _is_dynamic_server_enabled(name):
        return json.dumps({
            "success": True,
            "name": name,
            "message": f"MCP server '{name}' is already disabled."
        })

    # Remove from config
    _remove_dynamic_server_from_config(name)

    # Update metadata
    metadata = _get_dynamic_server_metadata(name) or {}
    metadata["enabled"] = False
    metadata["updated_at"] = utc_now_iso()
    _save_dynamic_server_metadata(name, metadata)

    return json.dumps({
        "success": True,
        "name": name,
        "message": f"MCP server '{name}' disabled. Its tools will no longer be available in job runs."
    })

@mcp.tool()
async def invoke_internal_mcp_tool(tool: str, payload: str = "{}") -> str:
    """
    Invoke an internal MCP tool from a dynamic or fixed server directly.

    Args:
        tool: Tool name (mcp__server__tool, server:tool, or server.tool)
        payload: JSON object or array of arguments to pass to the tool
    """
    normalized_tool, warning = _normalize_tool_name(tool)
    if not normalized_tool.startswith("mcp__"):
        error = "Only MCP tools are supported (mcp__server__tool)."
        if warning:
            return json.dumps({"error": error, "warning": warning})
        return json.dumps({"error": error})

    parts = normalized_tool.split("__", 2)
    if len(parts) != 3:
        return json.dumps({"error": f"Invalid MCP tool format '{normalized_tool}'"})

    server_name, tool_name = parts[1], parts[2]
    start_time = time.time()
    log_result = None
    log_error = None
    log_status = "success"

    enabled_servers = _get_mcp_servers_setting()
    if not any(s.get("name") == server_name for s in enabled_servers):
        return json.dumps({"error": f"MCP server '{server_name}' is not enabled."})

    server_path = _get_dynamic_server_path(server_name) / "server.py"
    if not server_path.exists():
        server_path = _get_fixed_server_path(server_name) / "server.py"
    if not server_path.exists():
        return json.dumps({"error": f"Server '{server_name}' is not a local dynamic/fixed server."})

    tool_names = _discover_python_mcp_tools(server_path)
    if tool_name not in tool_names:
        return json.dumps({
            "error": f"Tool '{tool_name}' not found in server '{server_name}'.",
            "available_tools": tool_names
        })

    allowed_tools = _get_allowed_tools_setting()
    allowlist = set(allowed_tools)
    allowlist.update([f"mcp__{server_name}__{name}" for name in tool_names])
    if allowlist and normalized_tool not in allowlist:
        return json.dumps({"error": f"Tool '{normalized_tool}' is not allowed by settings."})

    try:
        payload_data = json.loads(payload) if payload else {}
    except json.JSONDecodeError as e:
        return json.dumps({"error": f"Invalid payload JSON: {e}"})

    if payload_data is None:
        payload_data = {}

    if isinstance(payload_data, dict):
        args = []
        kwargs = payload_data
    elif isinstance(payload_data, list):
        args = payload_data
        kwargs = {}
    else:
        return json.dumps({"error": "Payload must be a JSON object or array."})

    env_vars = _get_server_env_vars(server_name)
    previous_env = {}
    for key, value in env_vars.items():
        previous_env[key] = os.environ.get(key)
        os.environ[key] = str(value)

    try:
        tool_func, error = _load_mcp_tool_from_file(server_name, server_path, tool_name)
        if error:
            log_error = error
            log_status = "error"
            return json.dumps({"error": error})

        result = tool_func(*args, **kwargs)
        if inspect.isawaitable(result):
            result = await result
        log_result = result
    except TypeError as e:
        log_error = f"Tool invocation failed: {e}"
        log_status = "error"
        return json.dumps({"error": log_error})
    except Exception as e:
        log_error = f"Tool execution error: {e}"
        log_status = "error"
        return json.dumps({"error": log_error})
    finally:
        for key, prev in previous_env.items():
            if prev is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = prev

        # Log tool invocation to database
        duration_ms = (time.time() - start_time) * 1000
        try:
            result_str = json.dumps(log_result, default=str) if log_result is not None else None
            if result_str and len(result_str) > 10240:
                result_str = result_str[:10240] + "... (truncated)"
            conn = get_db()
            conn.execute(
                "INSERT INTO tool_logs (id, server_name, tool_name, payload, result, error, status, duration_ms, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (str(uuid.uuid4()), server_name, tool_name, payload, result_str, log_error, log_status, round(duration_ms, 2), datetime.now(timezone.utc).isoformat())
            )
            conn.commit()
            conn.close()
        except Exception:
            pass

    response = {"success": True, "tool": normalized_tool, "result": result}
    if warning:
        response["warning"] = warning
    return json.dumps(response, indent=2)

# === Email Helper Functions ===
# NOTE: Email helper functions have been moved to fixed-servers/email/server.py

# === Scheduler Logic ===
def should_run(job: dict, now: datetime) -> bool:
    """Check if a job should run based on its cron schedule"""
    if not job["enabled"]:
        return False

    # Prevent duplicate runs: skip if there's already a pending/running run
    conn = get_db()
    active_run = conn.execute(
        "SELECT id FROM runs WHERE job_id = ? AND state IN ('pending', 'running') LIMIT 1",
        (job["id"],)
    ).fetchone()
    conn.close()
    if active_run:
        return False

    cron = croniter(job["cron"], now)
    prev_run = cron.get_prev(datetime)

    # Make prev_run timezone-aware (UTC) for proper comparison
    if prev_run.tzinfo is None:
        prev_run = prev_run.replace(tzinfo=timezone.utc)

    # If never executed, run it
    if job["last_executed_at"] is None:
        return True

    last_executed = datetime.fromisoformat(job["last_executed_at"])
    # Ensure last_executed is also timezone-aware
    if last_executed.tzinfo is None:
        last_executed = last_executed.replace(tzinfo=timezone.utc)

    return last_executed < prev_run

def create_run(job: dict) -> str:
    """Create a new run record and return its ID"""
    run_id = str(uuid.uuid4())[:8]
    now = utc_now_iso()
    
    conn = get_db()
    conn.execute("""
        INSERT INTO runs (id, job_id, started_at, prompt, command, state)
        VALUES (?, ?, ?, ?, ?, 'pending')
    """, (run_id, job["id"], now, job["prompt"], job["command"]))
    conn.commit()
    conn.close()
    
    return run_id

async def execute_run(run_id: str):
    """Execute a run using the Claude Agent SDK with streaming output"""
    if not AGENT_SDK_AVAILABLE:
        raise RuntimeError("Claude Agent SDK not installed. Run: pip install claude-agent-sdk")

    conn = get_db()

    # Atomically claim this run - only succeeds if state is still 'pending'
    cursor = conn.execute(
        "UPDATE runs SET state = 'running', output = '' WHERE id = ? AND state = 'pending'",
        (run_id,)
    )
    conn.commit()

    # Check if we actually claimed it (rowcount = 0 means another process got it first)
    if cursor.rowcount == 0:
        conn.close()
        return

    # Now fetch the run data
    run = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
    if not run:
        conn.close()
        return

    job = conn.execute("SELECT * FROM jobs WHERE id = ?", (run["job_id"],)).fetchone()

    # Register this task for potential cancellation
    RUNNING_TASKS[run_id] = asyncio.current_task()

    try:
        # Working directory for Claude operations
        cwd = str(Path(__file__).parent / "claude_playground")
        os.makedirs(cwd, exist_ok=True)

        # Load settings from database
        settings_conn = get_db()
        allowed_tools = ["WebSearch", "WebFetch", "Read", "Write", "Edit", "Bash"]  # defaults
        mcp_servers = []
        mcp_env_vars = {}
        sandbox_mode = True  # Default to sandboxed for safety
        for row in settings_conn.execute("SELECT key, value FROM settings").fetchall():
            try:
                if row['key'] == 'allowed_tools':
                    allowed_tools = json.loads(row['value'])
                elif row['key'] == 'mcp_servers':
                    mcp_servers = json.loads(row['value'])
                elif row['key'] == 'mcp_env_vars':
                    mcp_env_vars = json.loads(row['value'])
                elif row['key'] == 'sandbox_mode':
                    sandbox_mode = json.loads(row['value'])
            except:
                pass
        settings_conn.close()

        # Merge environment variables from .env (takes precedence over database)
        # Supports: MCP_VARNAME -> VARNAME, and input_varname -> input_varname
        for env_key, env_value in os.environ.items():
            if env_key.startswith('MCP_'):
                # MCP_AZURE_DEVOPS_PAT -> AZURE_DEVOPS_PAT
                var_key = env_key[4:]  # Remove 'MCP_' prefix
                mcp_env_vars[var_key] = env_value
            elif env_key.startswith('input_'):
                # input_ado_org stays as input_ado_org
                mcp_env_vars[env_key] = env_value

        # Build MCP servers config dict for SDK
        # SDK expects: {"server-name": {"command": "...", "args": [...]}}
        mcp_config = {}
        for server in mcp_servers:
            if server.get('name') and server.get('config'):
                try:
                    # Parse config if it's a JSON string
                    config = server['config']
                    if isinstance(config, str):
                        config = json.loads(config)

                    # Substitute environment variable placeholders
                    # e.g., ${GITHUB_PERSONAL_ACCESS_TOKEN} -> actual value
                    # Also handle ${input:varname} -> looks for input_varname in mcp_env_vars
                    config_str = json.dumps(config)
                    for var_name, var_value in mcp_env_vars.items():
                        if var_name.startswith('input_'):
                            # Handle input vars: ${input:ado_org} -> value from input_ado_org
                            input_key = var_name[6:]  # Remove 'input_' prefix
                            config_str = config_str.replace(f'${{input:{input_key}}}', var_value)
                        else:
                            config_str = config_str.replace(f'${{{var_name}}}', var_value)
                    config = json.loads(config_str)

                    # Transform production paths to local paths if running locally
                    # Production paths: /opt/mcpserver/app/...
                    # Local paths: <current_dir>/...
                    production_prefix = "/opt/mcpserver/app/"
                    local_base = str(Path(__file__).parent)
                    if config.get('args'):
                        transformed_args = []
                        for arg in config['args']:
                            if isinstance(arg, str) and arg.startswith(production_prefix):
                                # Check if production path exists
                                if not Path(arg).exists():
                                    # Transform to local path
                                    relative_path = arg[len(production_prefix):]
                                    local_path = str(Path(local_base) / relative_path)
                                    if Path(local_path).exists():
                                        print(f"[execute_run] Transformed path: {arg} -> {local_path}", file=sys.stderr)
                                        arg = local_path
                            transformed_args.append(arg)
                        config['args'] = transformed_args

                    # Handle command not in PATH for custom MCP servers
                    # Transform to npx if we can find the npm package name
                    if config.get('command') and config.get('command') != 'npx':
                        import shutil
                        cmd = config['command']
                        if not shutil.which(cmd):
                            # Try to find npm package name from custom paths
                            npm_package = None
                            server_name = server['name']

                            # Load custom paths
                            custom_paths = []
                            try:
                                cp_conn = get_db()
                                cp_row = cp_conn.execute("SELECT value FROM settings WHERE key = 'custom_mcp_paths'").fetchone()
                                if cp_row:
                                    custom_paths = json.loads(cp_row['value'])
                                cp_conn.close()
                            except:
                                pass

                            # Search custom paths for matching server
                            for cp in custom_paths:
                                try:
                                    cp_path = Path(cp)
                                    pkg_json = cp_path / 'package.json'
                                    mcp_json = cp_path / 'mcp.json'
                                    if not mcp_json.exists():
                                        mcp_json = cp_path / '.mcp.json'

                                    if pkg_json.exists() and mcp_json.exists():
                                        mcp_data = json.loads(mcp_json.read_text())
                                        # Check if this MCP defines the server we're looking for
                                        if 'servers' in mcp_data:
                                            for srv_name in mcp_data['servers'].keys():
                                                if f'custom-{srv_name}' == server_name:
                                                    pkg_data = json.loads(pkg_json.read_text())
                                                    npm_package = pkg_data.get('name')
                                                    break
                                        if npm_package:
                                            break
                                except Exception as e:
                                    print(f"[execute_run] Error checking custom path {cp}: {e}", file=sys.stderr)

                            if npm_package:
                                print(f"[execute_run] Transforming {cmd} to npx for package {npm_package}", file=sys.stderr)
                                original_args = config.get('args', [])
                                config['command'] = 'npx'
                                config['args'] = ['-y', '-p', npm_package, cmd] + original_args

                    # Ensure stdio type is set explicitly for all command-based servers
                    # This helps the Claude CLI properly identify and start the MCP server
                    if 'command' in config and 'type' not in config:
                        config['type'] = 'stdio'

                    mcp_config[server['name']] = config
                except (json.JSONDecodeError, TypeError) as e:
                    print(f"[execute_run] Error parsing MCP config for {server.get('name')}: {e}", file=sys.stderr)

        # Inject server-specific env vars into each MCP server config
        # This ensures fixed servers (like email) receive their credentials
        for server_name, config in mcp_config.items():
            server_env = {}
            prefix = f"{server_name}_"
            for key, value in mcp_env_vars.items():
                if key.startswith(prefix):
                    # Strip the server prefix to get the actual env var name
                    env_var_name = key[len(prefix):]
                    server_env[env_var_name] = value
            if server_env:
                # Merge with any existing env vars in config
                existing_env = config.get('env', {})
                existing_env.update(server_env)
                config['env'] = existing_env
                print(f"[execute_run] Injected {len(server_env)} env vars for {server_name}", file=sys.stderr)

        # Build the prompt
        full_prompt = run['prompt']

        # Prepend sandbox notice if sandbox mode is enabled
        if sandbox_mode:
            sandbox_notice = """[SANDBOX MODE ENABLED]
You are restricted to working only within the claude_playground directory.
- DO NOT read, write, or access files outside claude_playground
- DO NOT access home directory (~), .claude.json, .ssh, .aws, or system files
- Only create/modify files in: ./claude_playground/
- Use relative paths within the sandbox directory
---

"""
            full_prompt = sandbox_notice + full_prompt

        # Get timeout from job settings (default 30 minutes)
        timeout_minutes = job["timeout_minutes"] if job else 30
        timeout_seconds = timeout_minutes * 60

        # Define stderr handler to capture CLI debug output (helps diagnose MCP server issues)
        def stderr_handler(line: str):
            print(f"[CLI stderr] {line}", file=sys.stderr)

        # Build SDK options
        if mcp_config:
            mcp_tool_allowlist = _discover_mcp_tool_allowlist(mcp_config)
            if mcp_tool_allowlist:
                allowed_tools = _merge_tool_lists(allowed_tools, mcp_tool_allowlist)
            else:
                print("[execute_run] No MCP tools discovered; MCP tool access may be restricted by allowed_tools", file=sys.stderr)
            builtin_allowed_tools = _filter_builtin_tools(allowed_tools)

            # Debug: print actual MCP config being passed to SDK
            for server_name, server_config in mcp_config.items():
                cmd = server_config.get('command', 'N/A')
                args = server_config.get('args', [])
                env_keys = list(server_config.get('env', {}).keys())
                print(f"[execute_run] MCP server '{server_name}': cmd={cmd}, args={args[:2]}..., env_keys={env_keys}", file=sys.stderr)
            sdk_options = ClaudeAgentOptions(
                tools=builtin_allowed_tools,
                allowed_tools=allowed_tools,
                permission_mode="bypassPermissions",  # Automated execution, no prompts
                cwd=cwd,
                stderr=stderr_handler,  # Capture CLI stderr for debugging
                mcp_servers=mcp_config,
            )
        else:
            builtin_allowed_tools = _filter_builtin_tools(allowed_tools)
            sdk_options = ClaudeAgentOptions(
                tools=builtin_allowed_tools,
                allowed_tools=allowed_tools,
                permission_mode="bypassPermissions",  # Automated execution, no prompts
                cwd=cwd,
                stderr=stderr_handler,  # Capture CLI stderr for debugging
            )

        # Execute via Agent SDK with streaming
        output_parts = []
        usage = {
            "input_tokens": 0,
            "output_tokens": 0,
            "total_tokens": 0,
            "cost_usd": 0.0,
            "model": None,
            "cache_read_tokens": 0,
            "cache_creation_tokens": 0,
            "web_search_requests": 0,
        }
        processed_ids = set()  # Deduplicate messages (parallel tool uses share IDs)
        final_result = None

        print(f"[execute_run] Starting SDK query for run {run_id} (v2 - JSON output)", file=sys.stderr)

        # Use asyncio.timeout for timeout handling (Python 3.11+)
        try:
            async with asyncio.timeout(timeout_seconds):
                async for message in sdk_query(prompt=full_prompt, options=sdk_options):
                    # Capture different message types
                    msg_type = getattr(message, 'type', None)
                    msg_subtype = getattr(message, 'subtype', None)

                    # Capture assistant messages (the actual responses)
                    if hasattr(message, 'content') and message.content:
                        content = message.content
                        # Debug: log what we're receiving
                        print(f"[execute_run] content type: {type(content).__name__}, content: {repr(content)[:200]}", file=sys.stderr)

                        # Handle different content formats
                        if isinstance(content, str):
                            # Content is already a string - store as text block
                            output_parts.append({"type": "text", "text": content})
                        elif isinstance(content, list):
                            # Content is a list of blocks - serialize each
                            for block in content:
                                output_parts.append(serialize_content_block(block))
                        else:
                            # Single block object
                            output_parts.append(serialize_content_block(content))

                    # Capture tool use and results from nested message structure
                    if msg_type == 'assistant' and hasattr(message, 'message'):
                        nested_content = getattr(message.message, 'content', [])
                        if isinstance(nested_content, list):
                            for block in nested_content:
                                output_parts.append(serialize_content_block(block))

                    # Capture token usage from system messages (deduplicate by message ID)
                    msg_id = getattr(message, 'id', None)
                    if hasattr(message, 'usage') and message.usage:
                        if msg_id is None or msg_id not in processed_ids:
                            if msg_id is not None:
                                processed_ids.add(msg_id)
                            u = message.usage
                            if hasattr(u, 'input_tokens'):
                                usage["input_tokens"] = u.input_tokens
                            elif isinstance(u, dict):
                                usage["input_tokens"] = u.get("input_tokens", usage["input_tokens"])

                            if hasattr(u, 'output_tokens'):
                                usage["output_tokens"] = u.output_tokens
                            elif isinstance(u, dict):
                                usage["output_tokens"] = u.get("output_tokens", usage["output_tokens"])

                            # Capture cache token usage
                            cache_read = getattr(u, 'cache_read_input_tokens', None) or (u.get('cache_read_input_tokens') if isinstance(u, dict) else None)
                            if cache_read:
                                usage["cache_read_tokens"] = cache_read
                            cache_creation = getattr(u, 'cache_creation_input_tokens', None) or (u.get('cache_creation_input_tokens') if isinstance(u, dict) else None)
                            if cache_creation:
                                usage["cache_creation_tokens"] = cache_creation

                    # Capture model info
                    if hasattr(message, 'model') and message.model:
                        usage["model"] = message.model

                    # Capture authoritative cost from SDK ResultMessage
                    if hasattr(message, 'total_cost_usd') and message.total_cost_usd:
                        usage["cost_usd"] = message.total_cost_usd
                    if hasattr(message, 'model_usage') and message.model_usage:
                        # model_usage may contain per-model breakdown; extract web search count if available
                        mu = message.model_usage
                        if isinstance(mu, dict):
                            for model_info in mu.values():
                                if isinstance(model_info, dict):
                                    usage["web_search_requests"] += model_info.get('web_search_requests', 0)
                        elif hasattr(mu, '__iter__'):
                            for model_info in mu:
                                ws = getattr(model_info, 'web_search_requests', 0)
                                if ws:
                                    usage["web_search_requests"] += ws

                    # Capture final result
                    if hasattr(message, 'result') and message.result:
                        final_result = message.result

                    # Update DB with streaming output (every message)
                    try:
                        current_output = json.dumps(output_parts)
                    except (TypeError, ValueError) as e:
                        print(f"[execute_run] JSON serialization failed: {e}", file=sys.stderr)
                        print(f"[execute_run] output_parts types: {[type(p).__name__ for p in output_parts]}", file=sys.stderr)
                        # Fallback: convert to string
                        current_output = str(output_parts)
                    conn_update = get_db()
                    conn_update.execute(
                        "UPDATE runs SET output = ? WHERE id = ?",
                        (current_output, run_id)
                    )
                    conn_update.commit()
                    conn_update.close()

        except asyncio.TimeoutError:
            raise Exception(f"Timeout after {timeout_minutes} minutes")

        # Build final output - add final result as a special block
        if final_result:
            output_parts.append({
                "type": "result",
                "result": final_result
            })

        output = json.dumps(output_parts)

        # Calculate total tokens
        usage["total_tokens"] = usage["input_tokens"] + usage["output_tokens"]

        # Only calculate cost manually if SDK didn't provide authoritative total_cost_usd
        if usage["cost_usd"] == 0.0 and (usage["input_tokens"] > 0 or usage["output_tokens"] > 0):
            model = usage["model"] or "default"
            pricing = PRICING.get(model, PRICING["default"])
            input_cost = (usage["input_tokens"] / 1_000_000) * pricing["input"]
            output_cost = (usage["output_tokens"] / 1_000_000) * pricing["output"]
            usage["cost_usd"] = round(input_cost + output_cost, 6)

        # Update run record with final output and token stats
        now = utc_now_iso()
        conn.execute("""
            UPDATE runs SET
                finished_at = ?,
                output = ?,
                exit_code = ?,
                state = ?,
                input_tokens = ?,
                output_tokens = ?,
                total_tokens = ?,
                cost_usd = ?,
                model = ?,
                cache_read_tokens = ?,
                cache_creation_tokens = ?,
                web_search_requests = ?
            WHERE id = ?
        """, (now, output, 0, "finished",
              usage["input_tokens"], usage["output_tokens"], usage["total_tokens"],
              usage["cost_usd"], usage["model"],
              usage["cache_read_tokens"], usage["cache_creation_tokens"],
              usage["web_search_requests"], run_id))

        # Update job's last_executed_at
        conn.execute("""
            UPDATE jobs SET
                last_executed_at = ?,
                last_error = ?
            WHERE id = ?
        """, (now, None, job["id"]))

        conn.commit()

        print(f"[execute_run] Completed run {run_id}: {usage['total_tokens']} tokens, ${usage['cost_usd']:.4f}", file=sys.stderr)

    except Exception as e:
        print(f"[execute_run] Error in run {run_id}: {e}", file=sys.stderr)
        conn.execute("""
            UPDATE runs SET
                finished_at = ?,
                error = ?,
                state = 'error'
            WHERE id = ?
        """, (utc_now_iso(), str(e), run_id))
        conn.commit()

    finally:
        # Unregister task from cancellation tracking
        RUNNING_TASKS.pop(run_id, None)
        conn.close()

async def scheduler_loop():
    """Main scheduler loop - checks for jobs to run every minute"""
    while True:
        try:
            now = utc_now()
            conn = get_db()
            jobs = conn.execute("SELECT * FROM jobs WHERE enabled = 1").fetchall()
            conn.close()

            for job in jobs:
                job_dict = dict(job)
                if should_run(job_dict, now):
                    run_id = create_run(job_dict)
                    # Update last_executed_at immediately to prevent duplicate triggers
                    conn2 = get_db()
                    conn2.execute(
                        "UPDATE jobs SET last_executed_at = ? WHERE id = ?",
                        (utc_now_iso(), job_dict["id"])
                    )
                    conn2.commit()
                    conn2.close()
                    asyncio.create_task(execute_run(run_id))
                    print(f"[{now.isoformat()}] Started run {run_id} for job {job_dict['name']}", file=sys.stderr)

        except Exception as e:
            print(f"Scheduler error: {e}", file=sys.stderr)

        await asyncio.sleep(CHECK_INTERVAL_SECONDS)

async def run_processor_loop():
    """Process any pending runs (handles restarts gracefully)"""
    while True:
        try:
            conn = get_db()
            pending_runs = conn.execute(
                "SELECT id FROM runs WHERE state = 'pending'"
            ).fetchall()
            conn.close()
            
            for run in pending_runs:
                asyncio.create_task(execute_run(run["id"]))
        
        except Exception as e:
            print(f"Run processor error: {e}", file=sys.stderr)
        
        await asyncio.sleep(5)

# === Main Entry Point ===
import threading

def cleanup_stale_runs():
    """Mark any 'running' state runs as error (stale from previous crash)"""
    conn = get_db()
    stale = conn.execute("SELECT COUNT(*) FROM runs WHERE state = 'running'").fetchone()[0]
    if stale > 0:
        conn.execute("""
            UPDATE runs
            SET state = 'error',
                error = 'Killed: stale from server restart',
                finished_at = ?
            WHERE state = 'running'
        """, (utc_now_iso(),))
        conn.commit()
        print(f"Cleaned up {stale} stale run(s)", file=sys.stderr)
    conn.close()

def run_background_loops():
    """Run scheduler loops in a separate thread with its own event loop"""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.create_task(scheduler_loop())
    loop.create_task(run_processor_loop())
    loop.run_forever()

def start_ngrok(port: int = 8080) -> str | None:
    """Start ngrok tunnel and return the public URL."""
    global NGROK_PUBLIC_URL

    if not NGROK_AVAILABLE:
        print("[ngrok] pyngrok not installed. Run: pip install pyngrok", file=sys.stderr)
        return None

    # Check for auth token in environment
    auth_token = os.environ.get("NGROK_AUTHTOKEN")
    if auth_token:
        ngrok.set_auth_token(auth_token)

    # Kill any existing ngrok tunnels to avoid conflicts
    try:
        ngrok.kill()
        print("[ngrok] Killed existing ngrok processes", file=sys.stderr)
    except Exception:
        pass  # No existing processes to kill

    try:
        # Start ngrok tunnel
        tunnel = ngrok.connect(port, "http")
        public_url = tunnel.public_url
        NGROK_PUBLIC_URL = public_url  # Store globally for dashboard
        print(f"[ngrok] Tunnel established!", file=sys.stderr)
        print(f"[ngrok] Public URL: {public_url}", file=sys.stderr)
        print(f"[ngrok] MCP SSE endpoint: {public_url}/sse", file=sys.stderr)
        return public_url
    except Exception as e:
        print(f"[ngrok] Failed to start tunnel: {e}", file=sys.stderr)
        if "authentication" in str(e).lower() or "authtoken" in str(e).lower():
            print("[ngrok] Set NGROK_AUTHTOKEN environment variable or run: ngrok authtoken <token>", file=sys.stderr)
        return None

def main():
    init_db()
    cleanup_stale_runs()
    _auto_register_fixed_servers()
    print(f"Database initialized at {DB_PATH}", file=sys.stderr)

    # Initialize OAuth client for Claude connector
    oauth_client_id, oauth_client_secret = ensure_oauth_client()
    print("", file=sys.stderr)
    print("=" * 60, file=sys.stderr)
    print("OAUTH CREDENTIALS FOR CLAUDE CONNECTOR", file=sys.stderr)
    print("=" * 60, file=sys.stderr)
    print(f"Client ID:     {oauth_client_id}", file=sys.stderr)
    if oauth_client_secret:
        print(f"Client Secret: {oauth_client_secret}", file=sys.stderr)
        print("", file=sys.stderr)
        print("Save these to your .env file:", file=sys.stderr)
        print(f"  OAUTH_CLIENT_ID={oauth_client_id}", file=sys.stderr)
        print(f"  OAUTH_CLIENT_SECRET={oauth_client_secret}", file=sys.stderr)
    else:
        print("Client Secret: <already configured - check your .env file>", file=sys.stderr)
        print("", file=sys.stderr)
        print("If you lost your secret, delete jobs.db to regenerate.", file=sys.stderr)
    print("=" * 60, file=sys.stderr)
    print("", file=sys.stderr)

    # Start background tasks in separate thread
    bg_thread = threading.Thread(target=run_background_loops, daemon=True)
    bg_thread.start()
    print("Scheduler started", file=sys.stderr)

    transport = os.environ.get("MCP_TRANSPORT", "both")

    if transport == "both":
        # Run BOTH SSE (web dashboard) and stdio (Claude Desktop) simultaneously
        from starlette.routing import Mount
        import uvicorn
        import urllib.request
        import urllib.error

        base_app = mcp.http_app(path='/sse')  # Serve MCP at /sse for Claude connector
        base_app.routes.extend(dashboard_routes)
        # Wrap with OAuth middleware to protect /sse endpoint, then add CORS
        app = CORSMiddleware(OAuthMiddleware(base_app))

        def run_sse_server():
            """Run SSE server in its own event loop"""
            try:
                print("[SSE] Starting server thread...", file=sys.stderr)
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                config = uvicorn.Config(app, host="0.0.0.0", port=8080, log_level="info")
                server = uvicorn.Server(config)
                print("[SSE] Running uvicorn server...", file=sys.stderr)
                loop.run_until_complete(server.serve())
            except Exception as e:
                print(f"[SSE] Server error: {e}", file=sys.stderr)
                import traceback
                traceback.print_exc()

        # Start SSE server in background thread
        sse_thread = threading.Thread(target=run_sse_server, daemon=True)
        sse_thread.start()

        dashboard_url = "http://localhost:8080/"

        # Wait for server to be ready before proceeding (up to 10 seconds)
        print("[SSE] Waiting for server to be ready...", file=sys.stderr)
        server_ready = False
        for i in range(20):
            try:
                urllib.request.urlopen(dashboard_url, timeout=0.5)
                server_ready = True
                print("[SSE] Server is ready!", file=sys.stderr)
                break
            except urllib.error.HTTPError as e:
                # 401 means server is running (just requires auth)
                if e.code == 401:
                    server_ready = True
                    print("[SSE] Server is ready!", file=sys.stderr)
                    break
                time.sleep(0.5)
            except Exception:
                time.sleep(0.5)

        if server_ready:
            # Start ngrok tunnel for remote access BEFORE opening browser
            ngrok_url = start_ngrok(8080)
            if ngrok_url:
                print(f"[ngrok] Connect from claude.ai using: {ngrok_url}/sse", file=sys.stderr)
            # Open browser after ngrok is ready
            webbrowser.open(dashboard_url)
        else:
            print("[SSE] Warning: Server may not be ready", file=sys.stderr)
        print(f"Dashboard available at {dashboard_url}", file=sys.stderr)
        if DASHBOARD_AUTH_ENABLED:
            print(f"[Auth] Basic auth enabled for dashboard (user: {DASHBOARD_USERNAME})", file=sys.stderr)
        else:
            print("[Auth] Dashboard auth not configured. Set DASHBOARD_USERNAME and DASHBOARD_PASSWORD to enable.", file=sys.stderr)
        print("[OAuth] OAuth 2.1 enabled for /sse and /mcp endpoints", file=sys.stderr)
        if JWT_AVAILABLE:
            print("[OAuth] JWT tokens will be used for authentication", file=sys.stderr)
        else:
            print("[OAuth] WARNING: PyJWT not installed, OAuth will not work!", file=sys.stderr)

        # Run stdio in main thread (required for proper stdin/stdout handling)
        print("stdio MCP ready for Claude Desktop", file=sys.stderr)
        mcp.run()

    elif transport == "sse":
        # SSE only - web dashboard without Claude Desktop
        from starlette.routing import Mount
        import uvicorn

        base_app = mcp.http_app(path='/sse')  # Serve MCP at /sse for Claude connector
        base_app.routes.extend(dashboard_routes)
        # Wrap with OAuth middleware to protect /sse endpoint, then add CORS
        app = CORSMiddleware(OAuthMiddleware(base_app))

        dashboard_url = "http://localhost:8080/"
        print(f"Dashboard available at {dashboard_url}", file=sys.stderr)
        if DASHBOARD_AUTH_ENABLED:
            print(f"[Auth] Basic auth enabled for dashboard (user: {DASHBOARD_USERNAME})", file=sys.stderr)
        else:
            print("[Auth] Dashboard auth not configured. Set DASHBOARD_USERNAME and DASHBOARD_PASSWORD to enable.", file=sys.stderr)
        print("[OAuth] OAuth 2.1 enabled for /sse and /mcp endpoints", file=sys.stderr)
        if JWT_AVAILABLE:
            print("[OAuth] JWT tokens will be used for authentication", file=sys.stderr)
        else:
            print("[OAuth] WARNING: PyJWT not installed, OAuth will not work!", file=sys.stderr)

        # Start ngrok first, then open browser after a short delay
        def on_server_ready():
            ngrok_url = start_ngrok(8080)
            if ngrok_url:
                print(f"[ngrok] Connect from claude.ai using: {ngrok_url}/sse", file=sys.stderr)
            webbrowser.open(dashboard_url)

        threading.Timer(1.0, on_server_ready).start()

        uvicorn.run(app, host="0.0.0.0", port=8080, log_level="warning")

    else:
        # stdio only - Claude Desktop without web dashboard
        mcp.run()

if __name__ == "__main__":
    main()
