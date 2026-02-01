# Security Policy

## Reporting Vulnerabilities

If you discover a security vulnerability, please report it responsibly by emailing **floriansmeyers@gmail.com**. Do not open a public GitHub issue for security vulnerabilities.

Please include:
- A description of the vulnerability
- Steps to reproduce
- Potential impact

You can expect an initial response within 72 hours.

## Security Considerations

### Authentication

- **Change default credentials immediately.** The `.env.example` ships with `admin` / `change-me-to-a-strong-password`. These must be replaced before any deployment.
- OAuth 2.1 credentials are auto-generated with cryptographically secure random values if not explicitly set. You can override them via environment variables.
- The dashboard uses HTTP Basic Auth. Always run behind HTTPS in production (see the nginx reverse proxy setup in `deploy/`).

### Credentials & Secrets

- All secrets (API keys, tokens, passwords) are loaded from environment variables -- never hardcoded.
- MCP server credentials are stored in the SQLite database. Protect `jobs.db` with appropriate file permissions.
- Sensitive credential values are masked in API responses.

### Deployment

- Run the server as a non-root user (the deploy scripts create a dedicated `mcpuser`).
- Use the provided nginx reverse proxy configuration to terminate TLS.
- Restrict network access to the dashboard and SSE endpoints as appropriate for your environment.
- When using ngrok, be aware that the tunnel URL is publicly accessible. Use authentication to restrict access.

### Job Execution

- Jobs run via the Claude Agent SDK with `permission_mode: bypassPermissions`, meaning Claude executes tools without interactive approval. The `allowed_tools` setting controls which tools are available during execution -- review and restrict this list for your use case.
- Job prompts are user-defined. In multi-user environments, validate and sanitize prompt inputs.
- Set appropriate `timeout` values on jobs to prevent runaway processes.
