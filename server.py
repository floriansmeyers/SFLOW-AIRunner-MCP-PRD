#!/usr/bin/env python3
"""
SFLOW Agentic AI - MCP Server with Job Scheduling
A single-file MCP server that schedules and executes AI tasks via multiple providers.
Supports Claude (Agent SDK), OpenAI (Responses API), and Ollama (local inference).
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
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Callable, Optional

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

# Anthropic SDK for admin chat (always available via claude-agent-sdk dependency)
try:
    import anthropic as anthropic_module
    ANTHROPIC_SDK_AVAILABLE = True
except ImportError:
    ANTHROPIC_SDK_AVAILABLE = False

# OpenAI SDK for OpenAI provider
try:
    import openai as openai_module
    OPENAI_SDK_AVAILABLE = True
except ImportError:
    OPENAI_SDK_AVAILABLE = False

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

# OpenAI pricing per 1M tokens
OPENAI_PRICING = {
    "gpt-4o": {"input": 2.50, "output": 10.00},
    "gpt-4o-mini": {"input": 0.15, "output": 0.60},
    "gpt-4o-2024-11-20": {"input": 2.50, "output": 10.00},
    "o3": {"input": 10.00, "output": 40.00},
    "o3-mini": {"input": 1.10, "output": 4.40},
    "o4-mini": {"input": 1.10, "output": 4.40},
    "default": {"input": 2.50, "output": 10.00}
}


# === Provider Abstraction ===

@dataclass
class ProviderResult:
    """Result from a provider execution"""
    output_parts: list = field(default_factory=list)  # [{type: "text", text: "..."}, ...]
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    model: str | None = None
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0
    web_search_requests: int = 0
    final_result: str | None = None


class BaseProvider(ABC):
    """Abstract base class for AI providers"""

    @abstractmethod
    async def execute(
        self,
        prompt: str,
        cwd: str,
        allowed_tools: list[str],
        mcp_config: dict,
        timeout_seconds: int,
        on_progress: Optional[Callable] = None,
    ) -> ProviderResult:
        """Execute a prompt and return results"""
        ...

    @abstractmethod
    def get_pricing(self) -> dict[str, dict[str, float]]:
        """Return pricing dict for this provider's models"""
        ...

    @abstractmethod
    def is_available(self) -> bool:
        """Check if this provider is available (SDK installed, API key set, etc.)"""
        ...

    async def chat(
        self,
        messages: list,
        system_prompt: str,
        tool_defs: list,
        tool_callables: dict,
    ) -> dict:
        """Multi-turn conversation with tool calling.

        Args:
            messages: Conversation history in Anthropic format [{role, content}]
            system_prompt: System instructions for the assistant
            tool_defs: Tool definitions (provider-agnostic internal format)
            tool_callables: Dict mapping tool name -> callable

        Returns:
            {messages: [...], response_text: str, tool_calls_made: [{name, input, result}]}
        """
        raise NotImplementedError(f"{type(self).__name__} does not support chat()")

    def get_capabilities(self) -> dict:
        """Return provider capabilities"""
        return {"streaming": False, "mcp_tools": False, "tool_use": False}


# Provider registry - populated at startup
PROVIDERS: dict[str, BaseProvider] = {}


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
            command TEXT DEFAULT 'claude',
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
    try:
        conn.execute("ALTER TABLE webhooks ADD COLUMN command TEXT DEFAULT 'claude'")
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

    # Auto-configure scheduler DB path so it points to our jobs.db
    if (FIXED_SERVERS_DIR / "scheduler" / "server.py").exists():
        db_path = str(DB_PATH.resolve())
        existing = _get_server_credential("scheduler", "SCHEDULER_DB_PATH")
        if existing != db_path:
            _set_server_credential("scheduler", "SCHEDULER_DB_PATH", db_path)
            print(f"[startup] Auto-configured scheduler DB path: {db_path}", file=sys.stderr)

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
mcp = FastMCP("SFLOW Agentic AI")

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
            headers={"WWW-Authenticate": 'Basic realm="SFLOW Agentic AI Dashboard"'}
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
        headers={"WWW-Authenticate": 'Basic realm="SFLOW Agentic AI Dashboard"'}
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

def _load_dashboard_html():
    path = os.path.join(os.path.dirname(__file__), "static", "dashboard.html")
    with open(path, "r") as f:
        return f.read()

DASHBOARD_HTML = _load_dashboard_html()

@require_auth
async def dashboard_handler(request):
    return HTMLResponse(DASHBOARD_HTML)

async def static_file_handler(request):
    """Serve static files (CSS, JS) from the static directory."""
    filename = request.path_params.get("filename", "")
    # Security: only allow specific file extensions
    allowed_extensions = {'.css', '.js', '.html', '.png', '.jpg', '.ico', '.svg', '.woff', '.woff2'}
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

        # Get provider from request (default to first available or "claude")
        command = body.get('command', _get_default_provider())

        # Validate provider is registered
        if command not in PROVIDERS:
            available = list(PROVIDERS.keys())
            return JSONResponse({"error": f"Provider '{command}' not available. Available: {available}"}, status_code=400)

        # Create a one-off job
        job_id = str(uuid.uuid4())[:8]
        now = utc_now_iso()

        conn = get_db()
        conn.execute("""
            INSERT INTO jobs (id, name, cron, prompt, command, tools, environment, enabled, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (job_id, f"Manual Run ({now[:16]})", "manual", prompt, command, "[]", "{}", 0, now, now))
        conn.commit()

        # Create and trigger the run
        run_id = str(uuid.uuid4())[:8]
        conn.execute("""
            INSERT INTO runs (id, job_id, started_at, prompt, command, state)
            VALUES (?, ?, ?, ?, ?, 'pending')
        """, (run_id, job_id, now, prompt, command))
        conn.commit()
        conn.close()

        # Run will be picked up by run_processor_loop (within 5 seconds)
        return JSONResponse({"success": True, "run_id": run_id, "job_id": job_id})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)

@require_auth
async def api_admin_chat_handler(request):
    """Admin chat endpoint — multi-turn conversation with direct tool calling"""
    global ADMIN_TOOL_DEFINITIONS, ADMIN_TOOL_CALLABLES
    try:
        body = await request.json()
        messages = body.get("messages", [])
        command = body.get("command", _get_default_provider())

        if not messages:
            return JSONResponse({"error": "No messages provided"}, status_code=400)

        provider = PROVIDERS.get(command)
        if not provider:
            available = list(PROVIDERS.keys())
            return JSONResponse(
                {"error": f"Provider '{command}' not available. Available: {available}"},
                status_code=400,
            )

        # Build tool definitions and callables lazily on first call
        if not ADMIN_TOOL_DEFINITIONS:
            ADMIN_TOOL_DEFINITIONS = _build_admin_tool_definitions()
            ADMIN_TOOL_CALLABLES = _build_admin_tool_callables()
            print(f"[admin-chat] Built {len(ADMIN_TOOL_DEFINITIONS)} tool definitions", file=sys.stderr)

        result = await provider.chat(
            messages=messages,
            system_prompt=ADMIN_SYSTEM_PROMPT,
            tool_defs=ADMIN_TOOL_DEFINITIONS,
            tool_callables=ADMIN_TOOL_CALLABLES,
        )
        return JSONResponse(result)
    except Exception as e:
        import traceback
        traceback.print_exc()
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
async def api_providers_handler(request):
    """Get available AI providers and their capabilities"""
    providers = {}
    for name, provider in PROVIDERS.items():
        providers[name] = {
            "available": provider.is_available(),
            "capabilities": provider.get_capabilities(),
        }
    # Also include providers that are known but not registered
    for name in ["claude", "openai", "ollama"]:
        if name not in providers:
            providers[name] = {"available": False, "capabilities": {}}
    return JSONResponse({
        "providers": providers,
        "default": _get_default_provider(),
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

@require_auth
async def api_webhook_provider_handler(request):
    """Update a webhook's AI provider"""
    webhook_id = request.path_params['webhook_id']
    try:
        body = await request.json()
        command = body.get('command', 'claude')
        conn = get_db()
        conn.execute(
            "UPDATE webhooks SET command = ?, updated_at = ? WHERE id = ?",
            (command, utc_now_iso(), webhook_id)
        )
        conn.commit()
        conn.close()
        return JSONResponse({"success": True})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)

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

        # Determine provider: webhook setting > payload override > default
        command = webhook.get("command") or "claude"
        if command not in PROVIDERS:
            command = _get_default_provider()
        if isinstance(payload, dict) and payload.get('_provider'):
            req_provider = payload['_provider']
            if req_provider in PROVIDERS:
                command = req_provider

        # Create a disabled job for this webhook run (follows quick-run pattern)
        job_id = str(uuid.uuid4())[:8]
        now = utc_now_iso()

        conn.execute("""
            INSERT INTO jobs (id, name, cron, prompt, command, tools, environment, enabled, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (job_id, f"Webhook: {webhook['name']} ({now[:16]})", "webhook", prompt, command, "[]", "{}", 0, now, now))

        # Create the run with webhook_id
        run_id = str(uuid.uuid4())[:8]
        conn.execute("""
            INSERT INTO runs (id, job_id, started_at, prompt, command, state, webhook_id)
            VALUES (?, ?, ?, ?, ?, 'pending', ?)
        """, (run_id, job_id, now, prompt, command, webhook["id"]))

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
    Route("/api/admin-chat", api_admin_chat_handler, methods=["POST"]),
    Route("/api/job/{job_id}", api_get_job_handler, methods=["GET"]),
    Route("/api/job/{job_id}", api_update_job_handler, methods=["PUT"]),
    Route("/api/job/{job_id}", api_delete_job_handler, methods=["DELETE"]),
    Route("/api/job/{job_id}/trigger", api_trigger_job_handler, methods=["POST"]),
    Route("/api/stats", api_stats_handler),
    Route("/api/ngrok", api_ngrok_handler),
    Route("/api/providers", api_providers_handler, methods=["GET"]),
    Route("/api/settings", api_settings_get_handler, methods=["GET"]),
    Route("/api/settings", api_settings_post_handler, methods=["POST"]),
    Route("/api/mcp-available", api_mcp_available_handler, methods=["GET"]),
    Route("/api/fixed-servers", api_fixed_servers_handler, methods=["GET"]),
    Route("/api/dynamic-servers", api_dynamic_servers_handler, methods=["GET"]),
    Route("/api/server-credential", api_server_credential_handler, methods=["POST"]),
    Route("/api/fixed-server-toggle", api_fixed_server_toggle_handler, methods=["POST"]),
    Route("/api/dynamic-server-toggle", api_dynamic_server_toggle_handler, methods=["POST"]),
    Route("/api/webhooks", api_webhooks_handler),
    Route("/api/webhook/{webhook_id}/provider", api_webhook_provider_handler, methods=["PUT"]),
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
        prompt: The prompt to send to the AI provider
        command: AI provider to use: "claude" (default), "openai", or "ollama".
                 Claude uses the Agent SDK, OpenAI uses the chat completions API,
                 and Ollama uses the local HTTP API.
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
def create_webhook(name: str, prompt_template: str, description: str = "", command: str = "claude") -> str:
    """
    Create a webhook endpoint that executes a prompt when triggered via HTTP POST.

    The prompt_template can use placeholders:
    - {{payload}} - Full JSON payload as string
    - {{payload.field}} - Specific field from payload
    - {{payload.field.subfield}} - Nested field access

    Example template:
    "A new work item was created: {{payload.resource.fields.System.Title}}"

    Args:
        name: Human-readable webhook name
        prompt_template: Template with {{payload}} placeholders
        description: Optional description
        command: AI provider to use: "claude" (default), "openai", or "ollama"

    Returns the full webhook URL with security token.
    """
    webhook_id = str(uuid.uuid4())[:8]
    secret_token = uuid.uuid4().hex  # 32-char hex token
    now = utc_now_iso()

    conn = get_db()
    conn.execute("""
        INSERT INTO webhooks (id, name, description, secret_token, prompt_template, command, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """, (webhook_id, name, description, secret_token, prompt_template, command, now, now))
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
def update_webhook(webhook_id: str, name: str = None, prompt_template: str = None, description: str = None, enabled: bool = None, command: str = None) -> str:
    """Update an existing webhook.

    Args:
        webhook_id: The webhook ID to update
        name: New webhook name
        prompt_template: New prompt template
        description: New description
        enabled: Enable/disable the webhook
        command: AI provider to use: "claude", "openai", or "ollama"
    """
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
    if command is not None:
        updates.append("command = ?")
        params.append(command)

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


# === Provider Implementations ===

def _extract_tool_schema(func) -> dict:
    """Extract JSON Schema for function parameters using inspect.signature() and docstrings."""
    sig = inspect.signature(func)
    properties = {}
    required = []

    type_map = {
        str: "string",
        int: "integer",
        float: "number",
        bool: "boolean",
        list: "array",
        dict: "object",
    }

    for name, param in sig.parameters.items():
        if name in ("self", "cls"):
            continue
        prop = {}
        annotation = param.annotation
        if annotation != inspect.Parameter.empty:
            prop["type"] = type_map.get(annotation, "string")
        else:
            prop["type"] = "string"

        if param.default != inspect.Parameter.empty:
            prop["default"] = param.default
        else:
            required.append(name)

        properties[name] = prop

    schema = {"type": "object", "properties": properties}
    if required:
        schema["required"] = required
    return schema


def _convert_mcp_tools_to_functions(mcp_config: dict) -> tuple[list[dict], dict]:
    """
    Convert MCP server tools to OpenAI-compatible function definitions.
    Returns (tool_definitions, tool_callables) where:
      - tool_definitions: list of OpenAI tool dicts
      - tool_callables: dict mapping function name -> callable
    """
    tool_definitions = []
    tool_callables = {}

    for server_name, config in mcp_config.items():
        # Only handle Python-based MCP servers (stdio with python/python3 command)
        command = config.get("command", "")
        args = config.get("args", [])

        # Find the server.py path from args
        server_path = None
        if "python" in command or "python3" in command:
            for arg in args:
                if arg.endswith(".py") or arg.endswith("/server.py"):
                    server_path = Path(arg)
                    break
        elif args:
            # Try the first arg as a potential Python file
            candidate = Path(args[0]) if args else None
            if candidate and candidate.exists() and candidate.suffix == ".py":
                server_path = candidate

        if not server_path:
            # Try standard location for dynamic/fixed servers
            for base_dir in [DYNAMIC_SERVERS_DIR, FIXED_SERVERS_DIR]:
                candidate = base_dir / server_name / "server.py"
                if candidate.exists():
                    server_path = candidate
                    break

        if not server_path or not server_path.exists():
            print(f"[providers] Skipping MCP server '{server_name}': cannot find server.py", file=sys.stderr)
            continue

        # Inject server env vars so in-process module imports see them via os.environ
        server_env = config.get('env', {})
        old_env = {}
        for k, v in server_env.items():
            old_env[k] = os.environ.get(k)
            os.environ[k] = v

        try:
            # Discover tools from the server file
            tool_names = _discover_python_mcp_tools(server_path)
            if not tool_names:
                continue

            for tool_name in tool_names:
                func, error = _load_mcp_tool_from_file(server_name, server_path, tool_name)
                if error or not func:
                    print(f"[providers] Skipping tool '{tool_name}' from '{server_name}': {error}", file=sys.stderr)
                    continue

                # Build OpenAI-compatible function definition
                qualified_name = f"mcp__{server_name}__{tool_name}"
                schema = _extract_tool_schema(func)
                description = (func.__doc__ or f"Tool '{tool_name}' from MCP server '{server_name}'").strip()

                tool_definitions.append({
                    "type": "function",
                    "function": {
                        "name": qualified_name,
                        "description": description[:1024],  # OpenAI limit
                        "parameters": schema,
                    }
                })
                tool_callables[qualified_name] = func
                print(f"[providers] Loaded tool: {qualified_name}", file=sys.stderr)
        finally:
            # Restore original env vars
            for k, prev in old_env.items():
                if prev is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = prev

    return tool_definitions, tool_callables


async def _openai_compatible_chat(messages, system_prompt, tool_defs, tool_callables,
                                   client_factory, model) -> dict:
    """Shared chat implementation for OpenAI-compatible APIs (OpenAI + Ollama)."""
    if not OPENAI_SDK_AVAILABLE:
        raise RuntimeError("OpenAI SDK not installed. Run: pip install openai")

    client = client_factory()

    # Convert tool_defs to OpenAI format
    openai_tools = []
    for td in tool_defs:
        openai_tools.append({
            "type": "function",
            "function": {
                "name": td["name"],
                "description": td.get("description", "")[:1024],
                "parameters": td["parameters"],
            }
        })

    # Prepend system message
    api_messages = [{"role": "system", "content": system_prompt}]
    # Convert messages from Anthropic format to OpenAI format
    for msg in messages:
        role = msg.get("role", "user")
        content = msg.get("content", "")
        if isinstance(content, str):
            api_messages.append({"role": role, "content": content})
        elif isinstance(content, list):
            if role == "assistant":
                # Assistant messages may contain text + tool_use blocks
                text_parts = []
                tool_calls_list = []
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "text":
                        text_parts.append(block["text"])
                    elif isinstance(block, dict) and block.get("type") == "tool_use":
                        tool_calls_list.append({
                            "id": block.get("id", ""),
                            "type": "function",
                            "function": {
                                "name": block["name"],
                                "arguments": json.dumps(block.get("input", {})),
                            }
                        })
                assistant_msg = {"role": "assistant", "content": "\n".join(text_parts) if text_parts else None}
                if tool_calls_list:
                    assistant_msg["tool_calls"] = tool_calls_list
                api_messages.append(assistant_msg)
            else:
                # User messages with tool_result blocks
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "tool_result":
                        api_messages.append({
                            "role": "tool",
                            "tool_call_id": block["tool_use_id"],
                            "content": str(block.get("content", "")),
                        })
                    elif isinstance(block, dict) and block.get("type") == "text":
                        api_messages.append({"role": role, "content": block["text"]})
                    else:
                        api_messages.append({"role": role, "content": str(block)})

    tool_calls_made = []
    response_text = ""
    max_iterations = 25

    for _ in range(max_iterations):
        kwargs = {"model": model, "messages": api_messages}
        if openai_tools:
            kwargs["tools"] = openai_tools

        response = await client.chat.completions.create(**kwargs)
        choice = response.choices[0]
        message = choice.message

        if message.content:
            response_text += message.content

        if not message.tool_calls:
            # Append final assistant message to conversation in Anthropic format
            messages.append({"role": "assistant", "content": response_text})
            break

        # Build assistant content in Anthropic format
        assistant_content = []
        if message.content:
            assistant_content.append({"type": "text", "text": message.content})

        # Process tool calls
        api_messages.append(message.model_dump())
        tool_results_anthropic = []

        for tool_call in message.tool_calls:
            fn_name = tool_call.function.name
            try:
                fn_input = json.loads(tool_call.function.arguments) if tool_call.function.arguments else {}
            except (json.JSONDecodeError, TypeError):
                fn_input = {}

            assistant_content.append({
                "type": "tool_use",
                "id": tool_call.id,
                "name": fn_name,
                "input": fn_input,
            })

            result_str = ""
            is_error = False
            if fn_name in tool_callables:
                try:
                    func = tool_callables[fn_name]
                    if asyncio.iscoroutinefunction(func):
                        result = await func(**fn_input)
                    else:
                        result = func(**fn_input)
                    result_str = str(result) if result is not None else ""
                except Exception as e:
                    result_str = f"Error: {e}"
                    is_error = True
            else:
                result_str = f"Tool '{fn_name}' not available"
                is_error = True

            tool_calls_made.append({
                "name": fn_name,
                "input": fn_input,
                "result": result_str[:5000],
            })

            api_messages.append({
                "role": "tool",
                "tool_call_id": tool_call.id,
                "content": result_str[:10000],
            })
            tool_results_anthropic.append({
                "type": "tool_result",
                "tool_use_id": tool_call.id,
                "content": result_str[:10000],
                "is_error": is_error,
            })

        messages.append({"role": "assistant", "content": assistant_content})
        messages.append({"role": "user", "content": tool_results_anthropic})

    await client.close()

    return {
        "messages": messages,
        "response_text": response_text,
        "tool_calls_made": tool_calls_made,
    }


class ClaudeProvider(BaseProvider):
    """Provider that executes via the Claude Agent SDK (Claude Code CLI)"""

    async def execute(self, prompt, cwd, allowed_tools, mcp_config,
                      timeout_seconds, on_progress=None) -> ProviderResult:
        if not AGENT_SDK_AVAILABLE:
            raise RuntimeError("Claude Agent SDK not installed. Run: pip install claude-agent-sdk")

        # Define stderr handler
        def stderr_handler(line: str):
            print(f"[CLI stderr] {line}", file=sys.stderr)

        # Build SDK options
        if mcp_config:
            mcp_tool_allowlist = _discover_mcp_tool_allowlist(mcp_config)
            if mcp_tool_allowlist:
                merged_tools = _merge_tool_lists(allowed_tools, mcp_tool_allowlist)
            else:
                merged_tools = allowed_tools
                print("[ClaudeProvider] No MCP tools discovered; MCP tool access may be restricted", file=sys.stderr)
            builtin_allowed_tools = _filter_builtin_tools(merged_tools)

            for server_name, server_config in mcp_config.items():
                cmd = server_config.get('command', 'N/A')
                args = server_config.get('args', [])
                env_keys = list(server_config.get('env', {}).keys())
                print(f"[ClaudeProvider] MCP server '{server_name}': cmd={cmd}, args={args[:2]}..., env_keys={env_keys}", file=sys.stderr)

            sdk_options = ClaudeAgentOptions(
                tools=builtin_allowed_tools,
                allowed_tools=merged_tools,
                permission_mode="bypassPermissions",
                cwd=cwd,
                stderr=stderr_handler,
                mcp_servers=mcp_config,
            )
        else:
            builtin_allowed_tools = _filter_builtin_tools(allowed_tools)
            sdk_options = ClaudeAgentOptions(
                tools=builtin_allowed_tools,
                allowed_tools=allowed_tools,
                permission_mode="bypassPermissions",
                cwd=cwd,
                stderr=stderr_handler,
            )

        # Execute via Agent SDK with streaming
        output_parts = []
        usage = {
            "input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0,
            "model": None, "cache_read_tokens": 0, "cache_creation_tokens": 0,
            "web_search_requests": 0,
        }
        processed_ids = set()
        final_result = None

        print(f"[ClaudeProvider] Starting SDK query (timeout={timeout_seconds}s)", file=sys.stderr)

        try:
            async with asyncio.timeout(timeout_seconds):
                async for message in sdk_query(prompt=prompt, options=sdk_options):
                    msg_type = getattr(message, 'type', None)
                    msg_subtype = getattr(message, 'subtype', None)

                    # Capture assistant messages
                    if hasattr(message, 'content') and message.content:
                        content = message.content
                        if isinstance(content, str):
                            output_parts.append({"type": "text", "text": content})
                        elif isinstance(content, list):
                            for block in content:
                                output_parts.append(serialize_content_block(block))
                        else:
                            output_parts.append(serialize_content_block(content))

                    # Capture from nested message structure
                    if msg_type == 'assistant' and hasattr(message, 'message'):
                        nested_content = getattr(message.message, 'content', [])
                        if isinstance(nested_content, list):
                            for block in nested_content:
                                output_parts.append(serialize_content_block(block))

                    # Capture token usage (deduplicated)
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
                            cache_read = getattr(u, 'cache_read_input_tokens', None) or (u.get('cache_read_input_tokens') if isinstance(u, dict) else None)
                            if cache_read:
                                usage["cache_read_tokens"] = cache_read
                            cache_creation = getattr(u, 'cache_creation_input_tokens', None) or (u.get('cache_creation_input_tokens') if isinstance(u, dict) else None)
                            if cache_creation:
                                usage["cache_creation_tokens"] = cache_creation

                    if hasattr(message, 'model') and message.model:
                        usage["model"] = message.model

                    if hasattr(message, 'total_cost_usd') and message.total_cost_usd:
                        usage["cost_usd"] = message.total_cost_usd
                    if hasattr(message, 'model_usage') and message.model_usage:
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

                    if hasattr(message, 'result') and message.result:
                        final_result = message.result

                    # Progress callback for streaming DB updates
                    if on_progress:
                        on_progress(output_parts)

        except asyncio.TimeoutError:
            raise Exception(f"Timeout after {timeout_seconds // 60} minutes")

        # Calculate cost if not provided by SDK
        total_tokens = usage["input_tokens"] + usage["output_tokens"]
        if usage["cost_usd"] == 0.0 and total_tokens > 0:
            model = usage["model"] or "default"
            pricing = PRICING.get(model, PRICING["default"])
            input_cost = (usage["input_tokens"] / 1_000_000) * pricing["input"]
            output_cost = (usage["output_tokens"] / 1_000_000) * pricing["output"]
            usage["cost_usd"] = round(input_cost + output_cost, 6)

        return ProviderResult(
            output_parts=output_parts,
            input_tokens=usage["input_tokens"],
            output_tokens=usage["output_tokens"],
            cost_usd=usage["cost_usd"],
            model=usage["model"],
            cache_read_tokens=usage["cache_read_tokens"],
            cache_creation_tokens=usage["cache_creation_tokens"],
            web_search_requests=usage["web_search_requests"],
            final_result=final_result,
        )

    def get_pricing(self) -> dict[str, dict[str, float]]:
        return PRICING

    def is_available(self) -> bool:
        return AGENT_SDK_AVAILABLE

    async def chat(self, messages, system_prompt, tool_defs, tool_callables) -> dict:
        if not ANTHROPIC_SDK_AVAILABLE:
            raise RuntimeError("Anthropic SDK not installed. Run: pip install anthropic")
        if not os.environ.get("ANTHROPIC_API_KEY"):
            raise RuntimeError("ANTHROPIC_API_KEY not set. Required for Claude admin chat.")

        client = anthropic_module.Anthropic()
        model = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-5-20250514")

        # Convert tool_defs to Anthropic format
        anthropic_tools = []
        for td in tool_defs:
            anthropic_tools.append({
                "name": td["name"],
                "description": td.get("description", ""),
                "input_schema": td["parameters"],
            })

        tool_calls_made = []
        max_iterations = 25

        for _ in range(max_iterations):
            response = client.messages.create(
                model=model,
                max_tokens=4096,
                system=system_prompt,
                tools=anthropic_tools if anthropic_tools else [],
                messages=messages,
            )

            # Build assistant message content
            assistant_content = []
            response_text = ""
            has_tool_use = False

            for block in response.content:
                if block.type == "text":
                    response_text += block.text
                    assistant_content.append({"type": "text", "text": block.text})
                elif block.type == "tool_use":
                    has_tool_use = True
                    assistant_content.append({
                        "type": "tool_use",
                        "id": block.id,
                        "name": block.name,
                        "input": block.input,
                    })

            messages.append({"role": "assistant", "content": assistant_content})

            if response.stop_reason != "tool_use" or not has_tool_use:
                break

            # Execute tool calls and append results
            tool_results = []
            for block in response.content:
                if block.type != "tool_use":
                    continue
                fn_name = block.name
                fn_input = block.input or {}
                result_str = ""
                is_error = False

                if fn_name in tool_callables:
                    try:
                        func = tool_callables[fn_name]
                        if asyncio.iscoroutinefunction(func):
                            result = await func(**fn_input)
                        else:
                            result = func(**fn_input)
                        result_str = str(result) if result is not None else ""
                    except Exception as e:
                        result_str = f"Error: {e}"
                        is_error = True
                else:
                    result_str = f"Tool '{fn_name}' not available"
                    is_error = True

                tool_calls_made.append({
                    "name": fn_name,
                    "input": fn_input,
                    "result": result_str[:5000],
                })
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": result_str[:10000],
                    "is_error": is_error,
                })

            messages.append({"role": "user", "content": tool_results})

        return {
            "messages": messages,
            "response_text": response_text,
            "tool_calls_made": tool_calls_made,
        }

    def get_capabilities(self) -> dict:
        return {"streaming": True, "mcp_tools": True, "tool_use": True}


class OpenAIProvider(BaseProvider):
    """Provider that executes via the OpenAI API with function calling"""

    def __init__(self):
        self.model = os.environ.get("OPENAI_MODEL", "gpt-4o")
        self.max_tool_iterations = 20

    async def execute(self, prompt, cwd, allowed_tools, mcp_config,
                      timeout_seconds, on_progress=None) -> ProviderResult:
        if not OPENAI_SDK_AVAILABLE:
            raise RuntimeError("OpenAI SDK not installed. Run: pip install openai")

        api_key = os.environ.get("OPENAI_API_KEY")
        if not api_key:
            raise RuntimeError("OPENAI_API_KEY environment variable not set")

        client = openai_module.AsyncOpenAI(api_key=api_key)
        model = os.environ.get("OPENAI_MODEL", self.model)

        # Convert MCP tools to OpenAI function calling format
        tool_definitions, tool_callables = _convert_mcp_tools_to_functions(mcp_config)
        print(f"[OpenAIProvider] model={model}, tools={len(tool_definitions)}", file=sys.stderr)

        output_parts = []
        total_input_tokens = 0
        total_output_tokens = 0

        messages = [{"role": "user", "content": prompt}]

        try:
            async with asyncio.timeout(timeout_seconds):
                for iteration in range(self.max_tool_iterations + 1):
                    # Build API call kwargs
                    kwargs = {"model": model, "messages": messages}
                    if tool_definitions and iteration < self.max_tool_iterations:
                        kwargs["tools"] = tool_definitions

                    response = await client.chat.completions.create(**kwargs)
                    choice = response.choices[0]
                    message = choice.message

                    # Track tokens
                    if response.usage:
                        total_input_tokens += response.usage.prompt_tokens or 0
                        total_output_tokens += response.usage.completion_tokens or 0

                    # Capture text content
                    if message.content:
                        output_parts.append({"type": "text", "text": message.content})

                    # Check for tool calls
                    if not message.tool_calls:
                        break  # No tool calls — we're done

                    # Process tool calls
                    messages.append(message.model_dump())  # Add assistant message with tool_calls

                    for tool_call in message.tool_calls:
                        fn_name = tool_call.function.name
                        try:
                            parsed_input = json.loads(tool_call.function.arguments) if tool_call.function.arguments else {}
                        except (json.JSONDecodeError, TypeError):
                            parsed_input = {"raw": tool_call.function.arguments}
                        output_parts.append({
                            "type": "tool_use",
                            "id": tool_call.id,
                            "name": fn_name,
                            "input": parsed_input,
                        })

                        # Execute the tool
                        tool_result = ""
                        is_error = False
                        if fn_name in tool_callables:
                            try:
                                args = parsed_input if isinstance(parsed_input, dict) else {}
                                func = tool_callables[fn_name]
                                if asyncio.iscoroutinefunction(func):
                                    result = await func(**args)
                                else:
                                    result = func(**args)
                                tool_result = str(result) if result is not None else ""
                            except Exception as e:
                                tool_result = f"Error: {e}"
                                is_error = True
                        else:
                            tool_result = f"Tool '{fn_name}' not found"
                            is_error = True

                        output_parts.append({
                            "type": "tool_result",
                            "tool_use_id": tool_call.id,
                            "content": tool_result[:10000],  # Limit result size
                            "is_error": is_error,
                        })

                        messages.append({
                            "role": "tool",
                            "tool_call_id": tool_call.id,
                            "content": tool_result[:10000],
                        })

                        print(f"[OpenAIProvider] Tool {fn_name}: {'error' if is_error else 'ok'}", file=sys.stderr)

                    if on_progress:
                        on_progress(output_parts)

        except asyncio.TimeoutError:
            raise Exception(f"Timeout after {timeout_seconds // 60} minutes")

        # Calculate cost
        pricing = OPENAI_PRICING.get(model, OPENAI_PRICING["default"])
        input_cost = (total_input_tokens / 1_000_000) * pricing["input"]
        output_cost = (total_output_tokens / 1_000_000) * pricing["output"]
        cost = round(input_cost + output_cost, 6)

        return ProviderResult(
            output_parts=output_parts,
            input_tokens=total_input_tokens,
            output_tokens=total_output_tokens,
            cost_usd=cost,
            model=model,
        )

    def get_pricing(self) -> dict[str, dict[str, float]]:
        return OPENAI_PRICING

    def is_available(self) -> bool:
        return OPENAI_SDK_AVAILABLE and bool(os.environ.get("OPENAI_API_KEY"))

    async def chat(self, messages, system_prompt, tool_defs, tool_callables) -> dict:
        return await _openai_compatible_chat(
            messages=messages,
            system_prompt=system_prompt,
            tool_defs=tool_defs,
            tool_callables=tool_callables,
            client_factory=lambda: openai_module.AsyncOpenAI(api_key=os.environ.get("OPENAI_API_KEY")),
            model=os.environ.get("OPENAI_MODEL", self.model),
        )

    def get_capabilities(self) -> dict:
        return {"streaming": True, "mcp_tools": True, "tool_use": True}


class OllamaProvider(BaseProvider):
    """Provider for local AI servers (Ollama, LM Studio, etc.) via OpenAI-compatible API"""

    def __init__(self):
        self.model = os.environ.get("OLLAMA_MODEL", "llama3.1")
        self.base_url = os.environ.get("OLLAMA_URL", "http://localhost:11434")
        self.max_tool_iterations = 15

    def _get_openai_base_url(self) -> str:
        """Get the OpenAI-compatible base URL (append /v1 if needed)"""
        url = os.environ.get("OLLAMA_URL", self.base_url).rstrip("/")
        if not url.endswith("/v1"):
            url = url + "/v1"
        return url

    async def execute(self, prompt, cwd, allowed_tools, mcp_config,
                      timeout_seconds, on_progress=None) -> ProviderResult:
        if not OPENAI_SDK_AVAILABLE:
            raise RuntimeError("OpenAI SDK not installed (needed for local provider). Run: pip install openai")

        model = os.environ.get("OLLAMA_MODEL", self.model)
        base_url = self._get_openai_base_url()

        # Use OpenAI SDK pointed at the local server
        client = openai_module.AsyncOpenAI(api_key="local", base_url=base_url)

        # Convert MCP tools to OpenAI function calling format
        tool_definitions, tool_callables = _convert_mcp_tools_to_functions(mcp_config)
        print(f"[OllamaProvider] model={model}, url={base_url}, tools={len(tool_definitions)}", file=sys.stderr)

        output_parts = []
        total_input_tokens = 0
        total_output_tokens = 0

        messages = [{"role": "user", "content": prompt}]

        try:
            async with asyncio.timeout(timeout_seconds):
                for iteration in range(self.max_tool_iterations + 1):
                    kwargs = {"model": model, "messages": messages}
                    if tool_definitions and iteration < self.max_tool_iterations:
                        kwargs["tools"] = tool_definitions

                    try:
                        response = await client.chat.completions.create(**kwargs)
                    except openai_module.APIConnectionError:
                        raw_url = os.environ.get("OLLAMA_URL", self.base_url)
                        raise RuntimeError(f"Cannot connect to local server at {raw_url}. Is it running?")

                    choice = response.choices[0]
                    message = choice.message

                    # Track tokens
                    if response.usage:
                        total_input_tokens += response.usage.prompt_tokens or 0
                        total_output_tokens += response.usage.completion_tokens or 0

                    # Capture text content
                    if message.content:
                        output_parts.append({"type": "text", "text": message.content})

                    # Check for tool calls
                    if not message.tool_calls:
                        break  # Done

                    messages.append(message.model_dump())

                    for tool_call in message.tool_calls:
                        fn_name = tool_call.function.name
                        try:
                            parsed_input = json.loads(tool_call.function.arguments) if tool_call.function.arguments else {}
                        except (json.JSONDecodeError, TypeError):
                            parsed_input = {"raw": tool_call.function.arguments}
                        output_parts.append({
                            "type": "tool_use",
                            "id": tool_call.id,
                            "name": fn_name,
                            "input": parsed_input,
                        })

                        # Execute the tool
                        tool_result = ""
                        is_error = False
                        if fn_name in tool_callables:
                            try:
                                args = json.loads(tool_call.function.arguments) if isinstance(tool_call.function.arguments, str) else tool_call.function.arguments
                                func = tool_callables[fn_name]
                                if asyncio.iscoroutinefunction(func):
                                    result = await func(**args)
                                else:
                                    result = func(**args)
                                tool_result = str(result) if result is not None else ""
                            except Exception as e:
                                tool_result = f"Error: {e}"
                                is_error = True
                        else:
                            tool_result = f"Tool '{fn_name}' not found"
                            is_error = True

                        output_parts.append({
                            "type": "tool_result",
                            "tool_use_id": tool_call.id,
                            "content": tool_result[:10000],
                            "is_error": is_error,
                        })

                        messages.append({
                            "role": "tool",
                            "tool_call_id": tool_call.id,
                            "content": tool_result[:10000],
                        })

                        print(f"[OllamaProvider] Tool {fn_name}: {'error' if is_error else 'ok'}", file=sys.stderr)

                    if on_progress:
                        on_progress(output_parts)

        except asyncio.TimeoutError:
            raise Exception(f"Timeout after {timeout_seconds // 60} minutes")

        # Local inference — zero cost
        return ProviderResult(
            output_parts=output_parts,
            input_tokens=total_input_tokens,
            output_tokens=total_output_tokens,
            cost_usd=0.0,
            model=model,
        )

    def get_pricing(self) -> dict[str, dict[str, float]]:
        return {"default": {"input": 0.0, "output": 0.0}}

    def is_available(self) -> bool:
        """Check if the local server is reachable"""
        try:
            raw_url = os.environ.get("OLLAMA_URL", self.base_url).rstrip("/")
            # Try OpenAI-compatible /v1/models endpoint first
            resp = requests.get(f"{raw_url}/v1/models", timeout=2)
            if resp.status_code == 200:
                return True
            # Fall back to Ollama-native /api/tags
            resp = requests.get(f"{raw_url}/api/tags", timeout=2)
            return resp.status_code == 200
        except Exception:
            return False

    async def chat(self, messages, system_prompt, tool_defs, tool_callables) -> dict:
        base_url = self._get_openai_base_url()
        return await _openai_compatible_chat(
            messages=messages,
            system_prompt=system_prompt,
            tool_defs=tool_defs,
            tool_callables=tool_callables,
            client_factory=lambda: openai_module.AsyncOpenAI(api_key="local", base_url=base_url),
            model=os.environ.get("OLLAMA_MODEL", self.model),
        )

    def get_capabilities(self) -> dict:
        return {"streaming": True, "mcp_tools": True, "tool_use": True}


# === Admin Chat Configuration ===

ADMIN_SYSTEM_PROMPT = """You are an AI assistant managing the SFLOW Agentic AI Spinner system. You help administrators configure and manage scheduled jobs, webhooks, MCP servers, and credentials through natural language.

Available capabilities:
- **Jobs**: List, create, update, delete, and trigger scheduled AI jobs
- **Runs**: View run history and kill running tasks
- **Webhooks**: Create, list, update, and delete webhook endpoints
- **MCP Servers**: List, enable/disable, create, update, and delete dynamic MCP servers
- **Credentials**: Configure server credentials and check configuration status

Guidelines:
- Always explain what you're about to do before calling a tool
- For destructive operations (delete, disable), confirm the target name/ID with the user
- When creating jobs, validate cron expressions and suggest common patterns
- Show results in a clear, readable format
- If a tool returns an error, explain what went wrong and suggest a fix

When creating MCP servers with `create_mcp_server`, write the code following this pattern:
- Use `from fastmcp import FastMCP` and `mcp = FastMCP("Server Name")`
- Define tools with `@mcp.tool()` decorator
- Use `httpx` (async) for HTTP calls
- Use `os.environ.get("VAR_NAME")` for any credentials so they are auto-detected
- Do NOT import or define `DATA_DIR` — it is auto-injected at the top of the file
- Always end with `if __name__ == "__main__": mcp.run()`

Example server code:
```python
from fastmcp import FastMCP
import httpx

mcp = FastMCP("My API")

@mcp.tool()
async def fetch_data(query: str) -> dict:
    \"\"\"Fetch data from the API.\"\"\"
    async with httpx.AsyncClient() as client:
        response = await client.get(f"https://api.example.com/data?q={query}")
        response.raise_for_status()
        return response.json()

if __name__ == "__main__":
    mcp.run()
```
"""

ADMIN_TOOLS = {
    "list_jobs": list_jobs,
    "create_job": create_job,
    "update_job": update_job,
    "delete_job": delete_job,
    "trigger_job": trigger_job,
    "get_job": get_job,
    "list_runs": list_runs,
    "get_run": get_run,
    "kill_run": kill_run,
    "list_webhooks": list_webhooks,
    "create_webhook": create_webhook,
    "update_webhook": update_webhook,
    "delete_webhook": delete_webhook,
    "get_webhook": get_webhook,
    "list_fixed_mcp_servers": list_fixed_mcp_servers,
    "list_dynamic_mcp_servers": list_dynamic_mcp_servers,
    "enable_fixed_server": enable_fixed_server,
    "disable_fixed_server": disable_fixed_server,
    "enable_mcp_server": enable_mcp_server,
    "disable_mcp_server": disable_mcp_server,
    "set_server_credential": set_server_credential,
    "get_server_credentials": get_server_credentials,
    "list_required_credentials": list_required_credentials,
    "get_unconfigured_servers": get_unconfigured_servers,
    "invoke_internal_mcp_tool": invoke_internal_mcp_tool,
    "create_mcp_server": create_mcp_server,
    "update_mcp_server": update_mcp_server,
    "delete_mcp_server": delete_mcp_server,
    "get_dynamic_mcp_server": get_dynamic_mcp_server,
}

def _unwrap_mcp_tool(func):
    """Unwrap a FastMCP FunctionTool to get the original callable, or return as-is."""
    # FastMCP's @mcp.tool() returns FunctionTool objects with .fn attribute
    if hasattr(func, 'fn') and callable(getattr(func, 'fn')):
        return func.fn
    # Standard functools.wraps pattern
    while hasattr(func, '__wrapped__'):
        func = func.__wrapped__
    return func


def _build_admin_tool_definitions() -> list:
    """Build tool definitions from ADMIN_TOOLS using function signatures and docstrings."""
    definitions = []
    for name, func in ADMIN_TOOLS.items():
        try:
            # FunctionTool has .parameters and .description built-in
            if hasattr(func, 'parameters') and hasattr(func, 'description'):
                schema = func.parameters
                description = (func.description or f"Tool: {name}").strip()
            else:
                original = _unwrap_mcp_tool(func)
                schema = _extract_tool_schema(original)
                description = (original.__doc__ or f"Tool: {name}").strip()

            if len(description) > 1024:
                description = description[:1021] + "..."

            definitions.append({
                "name": name,
                "description": description,
                "parameters": schema,
            })
        except Exception as e:
            print(f"[admin-chat] Failed to build definition for '{name}': {e}", file=sys.stderr)
    return definitions


def _build_admin_tool_callables() -> dict:
    """Build a dict of actual callables from ADMIN_TOOLS (unwrapping FunctionTool objects)."""
    callables = {}
    for name, func in ADMIN_TOOLS.items():
        callables[name] = _unwrap_mcp_tool(func)
    return callables


# Built lazily on first use (after @mcp.tool decorators have run)
ADMIN_TOOL_DEFINITIONS: list = []
ADMIN_TOOL_CALLABLES: dict = {}


def init_providers():
    """Register available providers at startup"""
    providers_to_check = [
        ("claude", ClaudeProvider()),
        ("openai", OpenAIProvider()),
        ("ollama", OllamaProvider()),
    ]
    for name, provider in providers_to_check:
        try:
            if provider.is_available():
                PROVIDERS[name] = provider
                print(f"[startup] Provider '{name}' registered", file=sys.stderr)
            else:
                print(f"[startup] Provider '{name}' not available (missing deps or config)", file=sys.stderr)
        except Exception as e:
            print(f"[startup] Provider '{name}' check failed: {e}", file=sys.stderr)


def _get_default_provider() -> str:
    """Get the default provider name. Checks DB setting, then falls back to first available."""
    try:
        conn = get_db()
        row = conn.execute("SELECT value FROM settings WHERE key = 'default_provider'").fetchone()
        conn.close()
        if row:
            provider_name = json.loads(row['value'])
            if provider_name in PROVIDERS:
                return provider_name
    except Exception:
        pass
    # Fall back to first available provider (claude preferred)
    for name in ["claude", "openai", "ollama"]:
        if name in PROVIDERS:
            return name
    return "claude"  # Ultimate fallback


def create_run(job: dict) -> str:
    """Create a new run record and return its ID"""
    run_id = str(uuid.uuid4())[:8]
    now = utc_now_iso()
    
    conn = get_db()
    conn.execute("""
        INSERT INTO runs (id, job_id, started_at, prompt, command, state)
        VALUES (?, ?, ?, ?, ?, 'pending')
    """, (run_id, job["id"], now, job["prompt"], job.get("command") or "claude"))
    conn.commit()
    conn.close()
    
    return run_id

async def execute_run(run_id: str):
    """Execute a run using the configured provider (Claude, OpenAI, or Ollama)"""
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

        # Determine which provider to use
        command = run["command"] or "claude"
        provider = PROVIDERS.get(command)
        if not provider:
            raise RuntimeError(f"Provider '{command}' not registered. Available: {list(PROVIDERS.keys())}")
        if not provider.is_available():
            raise RuntimeError(f"Provider '{command}' is not available (missing dependencies or configuration)")

        print(f"[execute_run] Using provider '{command}' for run {run_id}", file=sys.stderr)

        # Progress callback to stream output to DB
        def on_progress(output_parts):
            try:
                current_output = json.dumps(output_parts)
            except (TypeError, ValueError) as e:
                print(f"[execute_run] JSON serialization failed: {e}", file=sys.stderr)
                current_output = str(output_parts)
            conn_update = get_db()
            conn_update.execute(
                "UPDATE runs SET output = ? WHERE id = ?",
                (current_output, run_id)
            )
            conn_update.commit()
            conn_update.close()

        # Execute via the provider
        result = await provider.execute(
            prompt=full_prompt,
            cwd=cwd,
            allowed_tools=allowed_tools,
            mcp_config=mcp_config,
            timeout_seconds=timeout_seconds,
            on_progress=on_progress,
        )

        # Build final output
        output_parts = result.output_parts
        if result.final_result:
            output_parts.append({
                "type": "result",
                "result": result.final_result,
            })
        output = json.dumps(output_parts)

        total_tokens = result.input_tokens + result.output_tokens

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
              result.input_tokens, result.output_tokens, total_tokens,
              result.cost_usd, result.model,
              result.cache_read_tokens, result.cache_creation_tokens,
              result.web_search_requests, run_id))

        # Update job's last_executed_at
        conn.execute("""
            UPDATE jobs SET
                last_executed_at = ?,
                last_error = ?
            WHERE id = ?
        """, (now, None, job["id"]))

        conn.commit()

        print(f"[execute_run] Completed run {run_id} ({command}): {total_tokens} tokens, ${result.cost_usd:.4f}", file=sys.stderr)

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
    init_providers()
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
