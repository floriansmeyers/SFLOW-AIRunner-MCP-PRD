#!/usr/bin/env python3
"""
Email MCP Server - Send emails via SendGrid or Mailgun.

This is a fixed (built-in) MCP server that provides email sending capabilities.
Configuration is via environment variables (managed through set_server_credential).
"""
import json
import os

from fastmcp import FastMCP
import requests

# Try to import SendGrid
try:
    from sendgrid import SendGridAPIClient
    from sendgrid.helpers.mail import Mail
    SENDGRID_AVAILABLE = True
except ImportError:
    SENDGRID_AVAILABLE = False

mcp = FastMCP("Email")

# Configuration from environment variables
# Set these using: set_server_credential("email", "VAR_NAME", "value")
EMAIL_PROVIDER = os.environ.get("EMAIL_PROVIDER", "")  # 'sendgrid' or 'mailgun'
SENDGRID_API_KEY = os.environ.get("SENDGRID_API_KEY", "")
MAILGUN_API_KEY = os.environ.get("MAILGUN_API_KEY", "")
MAILGUN_DOMAIN = os.environ.get("MAILGUN_DOMAIN", "")
EMAIL_FROM_ADDRESS = os.environ.get("EMAIL_FROM_ADDRESS", "")
EMAIL_FROM_NAME = os.environ.get("EMAIL_FROM_NAME", "")


def _send_via_sendgrid(recipients: list, subject: str, body: str, html: bool) -> str:
    """Send email via SendGrid."""
    if not SENDGRID_AVAILABLE:
        return json.dumps({"error": "SendGrid library not installed. Run: pip install sendgrid"})

    if not SENDGRID_API_KEY:
        return json.dumps({"error": "SENDGRID_API_KEY not configured. Use set_server_credential('email', 'SENDGRID_API_KEY', 'your_key')."})

    message = Mail(
        from_email=EMAIL_FROM_ADDRESS,
        to_emails=recipients,
        subject=subject,
        plain_text_content=body if not html else None,
        html_content=body if html else None
    )

    try:
        sg = SendGridAPIClient(SENDGRID_API_KEY)
        response = sg.send(message)
        return json.dumps({
            "success": True,
            "provider": "sendgrid",
            "status_code": response.status_code,
            "message": f"Email sent to {', '.join(recipients)}"
        })
    except Exception as e:
        return json.dumps({
            "error": str(e),
            "provider": "sendgrid",
            "message": "Failed to send email via SendGrid"
        })


def _send_via_mailgun(recipients: list, subject: str, body: str, html: bool) -> str:
    """Send email via Mailgun."""
    if not MAILGUN_API_KEY:
        return json.dumps({"error": "MAILGUN_API_KEY not configured. Use set_server_credential('email', 'MAILGUN_API_KEY', 'your_key')."})
    if not MAILGUN_DOMAIN:
        return json.dumps({"error": "MAILGUN_DOMAIN not configured. Use set_server_credential('email', 'MAILGUN_DOMAIN', 'mg.yourdomain.com')."})

    # Determine API endpoint (EU or US)
    if 'eu' in MAILGUN_DOMAIN.lower():
        api_base = "https://api.eu.mailgun.net/v3"
    else:
        api_base = "https://api.mailgun.net/v3"

    url = f"{api_base}/{MAILGUN_DOMAIN}/messages"

    # Build from field
    from_email = f"{EMAIL_FROM_NAME} <{EMAIL_FROM_ADDRESS}>" if EMAIL_FROM_NAME else EMAIL_FROM_ADDRESS

    data = {
        "from": from_email,
        "to": recipients,
        "subject": subject,
    }

    if html:
        data["html"] = body
    else:
        data["text"] = body

    try:
        response = requests.post(
            url,
            auth=("api", MAILGUN_API_KEY),
            data=data
        )

        if response.status_code == 200:
            result = response.json()
            return json.dumps({
                "success": True,
                "provider": "mailgun",
                "status_code": response.status_code,
                "message": f"Email sent to {', '.join(recipients)}",
                "id": result.get("id", "")
            })
        else:
            return json.dumps({
                "error": response.text,
                "provider": "mailgun",
                "status_code": response.status_code,
                "message": "Failed to send email via Mailgun"
            })
    except Exception as e:
        return json.dumps({
            "error": str(e),
            "provider": "mailgun",
            "message": "Failed to send email via Mailgun"
        })


# === MCP Tools ===

@mcp.tool()
def send_email(to: str, subject: str, body: str, html: bool = False) -> str:
    """
    Send an email via the configured provider (SendGrid or Mailgun).

    Args:
        to: Recipient email address(es), comma-separated for multiple
        subject: Email subject line
        body: Email body content
        html: If True, treat body as HTML; otherwise plain text
    """
    # Validate configuration
    if not EMAIL_PROVIDER:
        return json.dumps({"error": "EMAIL_PROVIDER not configured. Use set_server_credential('email', 'EMAIL_PROVIDER', 'sendgrid' or 'mailgun')."})

    if not EMAIL_FROM_ADDRESS:
        return json.dumps({"error": "EMAIL_FROM_ADDRESS not configured. Use set_server_credential('email', 'EMAIL_FROM_ADDRESS', 'your@email.com')."})

    if EMAIL_PROVIDER not in ['sendgrid', 'mailgun']:
        return json.dumps({"error": f"Invalid EMAIL_PROVIDER '{EMAIL_PROVIDER}'. Must be 'sendgrid' or 'mailgun'."})

    # Build recipient list
    recipients = [email.strip() for email in to.split(',')]

    if EMAIL_PROVIDER == 'mailgun':
        return _send_via_mailgun(recipients, subject, body, html)
    else:
        return _send_via_sendgrid(recipients, subject, body, html)


@mcp.tool()
def get_email_status() -> str:
    """
    Get current email configuration status (credentials are masked).
    """
    status = {
        "provider": EMAIL_PROVIDER or "(not set)",
        "from_address": EMAIL_FROM_ADDRESS or "(not set)",
        "from_name": EMAIL_FROM_NAME or "(not set)",
        "configured": bool(EMAIL_PROVIDER and EMAIL_FROM_ADDRESS)
    }

    if EMAIL_PROVIDER == 'sendgrid':
        status["sendgrid_api_key"] = "***configured***" if SENDGRID_API_KEY else "(not set)"
        status["ready"] = bool(SENDGRID_API_KEY)
    elif EMAIL_PROVIDER == 'mailgun':
        status["mailgun_api_key"] = "***configured***" if MAILGUN_API_KEY else "(not set)"
        status["mailgun_domain"] = MAILGUN_DOMAIN or "(not set)"
        status["ready"] = bool(MAILGUN_API_KEY and MAILGUN_DOMAIN)
    else:
        status["ready"] = False

    return json.dumps(status, indent=2)


if __name__ == "__main__":
    mcp.run()
