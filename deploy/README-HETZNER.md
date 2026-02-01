# Hetzner VPS Deployment Guide

Complete instructions for deploying the MCP Server with Job Scheduling on a Hetzner VPS running Ubuntu 22.04/24.04.

## Prerequisites

- Hetzner VPS (CX11 or higher recommended - 2GB RAM minimum)
- SSH access to the VPS
- Anthropic account (for Claude CLI authentication)
- GitHub SSH key (for cloning private repos)

**Optional:** Domain name with Cloudflare DNS (for HTTPS and Claude.ai connector)

---

## 1. Initial Server Setup

SSH into your VPS:
```bash
ssh root@your-vps-ip
```

Update system and install packages:
```bash
apt update && apt upgrade -y
apt install -y python3 python3-pip python3-venv git ufw curl

# Install Node.js (required for Claude CLI)
curl -fsSL https://deb.nodesource.com/setup_20.x | bash -
apt install -y nodejs
```

---

## 2. Create Application User

**Important:** Claude CLI won't run as root. Create a dedicated user:

```bash
useradd -m -s /bin/bash mcpuser
mkdir -p /opt/mcpserver
chown mcpuser:mcpuser /opt/mcpserver
```

---

## 3. Setup SSH Key for GitHub

Generate SSH key for the VPS:
```bash
ssh-keygen -t ed25519 -C "vps-deploy"
cat ~/.ssh/id_ed25519.pub
```

Add the public key to GitHub: https://github.com/settings/keys → "New SSH key"

---

## 4. Install Claude CLI

```bash
npm install -g @anthropic-ai/claude-code
claude --version
```

---

## 5. Clone and Setup the Project

```bash
cd /opt/mcpserver

# Clone via SSH (not HTTPS!)
git clone git@github.com:YOUR_USERNAME/SFLOW-AIagents-MCP-Spinner.git app
cd app

# Set ownership to mcpuser
chown -R mcpuser:mcpuser /opt/mcpserver

# Switch to mcpuser for remaining setup
su - mcpuser
cd /opt/mcpserver/app

# Create virtual environment
python3 -m venv venv
source venv/bin/activate

# Install dependencies
pip install --upgrade pip
pip install -r requirements.txt

# Install Playwright browsers (required for browser automation)
playwright install chromium
```

---

## 6. Authenticate Claude CLI

**Important:** Must be done as mcpuser, not root!

```bash
# If not already mcpuser:
su - mcpuser

# Login to Claude (opens URL to authenticate in browser)
claude login
```

Copy the URL shown, open it in your browser, and complete authentication.

---

## 7. Configure Environment Variables

```bash
# Create .env file
cat > /opt/mcpserver/app/.env << 'EOF'
# Server transport mode (sse enables web dashboard)
MCP_TRANSPORT=sse

# Dashboard credentials (change these!)
DASHBOARD_USERNAME=admin
DASHBOARD_PASSWORD=changeme123

# OAuth credentials for Claude.ai connector (auto-generated if not set)
# OAUTH_CLIENT_ID=your-client-id
# OAUTH_CLIENT_SECRET=your-client-secret

# Public URL (required for Claude.ai connector with custom domain)
# PUBLIC_URL=https://your-domain.com
EOF

chmod 600 /opt/mcpserver/app/.env
```

**Note:** On first run, OAuth credentials will be auto-generated and shown in the logs. Copy them to your `.env` file.

---

## 8. Install systemd Services

Exit back to root:
```bash
exit
```

Copy service files:
```bash
cp /opt/mcpserver/app/deploy/mcpserver.service /etc/systemd/system/
cp /opt/mcpserver/app/deploy/mcpserver-deploy.service /etc/systemd/system/
cp /opt/mcpserver/app/deploy/mcpserver-deploy.timer /etc/systemd/system/

# Copy deploy script
cp /opt/mcpserver/app/deploy/deploy.sh /opt/mcpserver/
chmod +x /opt/mcpserver/deploy.sh

# Create log file
touch /var/log/mcpserver-deploy.log
chmod 644 /var/log/mcpserver-deploy.log

# Reload and enable services
systemctl daemon-reload
systemctl enable mcpserver
systemctl enable mcpserver-deploy.timer

# Start services
systemctl start mcpserver
systemctl start mcpserver-deploy.timer

# Verify
systemctl status mcpserver
```

---

## 9. Configure Firewall

For direct access (no domain):
```bash
ufw default deny incoming
ufw default allow outgoing
ufw allow ssh
ufw allow 8080/tcp
ufw enable
```

---

## 10. Verify Deployment

```bash
# Check service status
systemctl status mcpserver

# View logs (look for OAuth credentials on first run!)
journalctl -u mcpserver -n 50

# Test locally
curl http://localhost:8080/
```

Access your dashboard at: **http://YOUR-VPS-IP:8080**

---

## Updating the Server

The server includes an **auto-deploy system** that checks for updates every 5 minutes.

### How Auto-Deploy Works

1. The `mcpserver-deploy.timer` runs every 5 minutes
2. It checks if there are new commits on `origin/main`
3. If updates exist, it pulls changes, installs dependencies, and restarts the service
4. Logs are written to `/var/log/mcpserver-deploy.log`

### Manual Update

To trigger an update immediately:
```bash
systemctl start mcpserver-deploy.service
```

Or update manually:
```bash
su - mcpuser
cd /opt/mcpserver/app
source venv/bin/activate

git pull origin main
pip install -r requirements.txt
playwright install chromium

exit
systemctl restart mcpserver
```

### Check Update Status

```bash
# View auto-deploy logs
tail -f /var/log/mcpserver-deploy.log

# Check timer status
systemctl list-timers | grep mcpserver

# See last update time
journalctl -u mcpserver-deploy.service -n 20
```

---

## Connect from Claude Desktop

Add to `~/.config/claude/claude_desktop_config.json` (Linux) or `~/Library/Application Support/Claude/claude_desktop_config.json` (Mac):

```json
{
  "mcpServers": {
    "job-scheduler": {
      "url": "http://YOUR-VPS-IP:8080/sse"
    }
  }
}
```

---

## Setup Domain with Cloudflare (Required for Claude.ai)

If you want to connect Claude.ai (not Desktop), you need HTTPS with a domain.

### Step 1: Add DNS Record in Cloudflare

Go to Cloudflare → your-domain.com → DNS → Add record:

| Type | Name | Content | Proxy status |
|------|------|---------|--------------|
| A | mcp (or subdomain of choice) | YOUR-VPS-IP | **Proxied** (orange cloud) |

### Step 2: Cloudflare SSL Settings

Go to **SSL/TLS** → **Overview** → Set mode to **Full**

### Step 3: Create Cloudflare Origin Certificate

Go to **SSL/TLS** → **Origin Server** → **Create Certificate**:
- Private key type: RSA (2048)
- Hostnames: `*.your-domain.com`, `your-domain.com`
- Certificate Validity: 15 years
- Click **Create**

**Copy both the Certificate and Private Key** (you can only see the key once!)

### Step 4: Install Certificate on VPS

```bash
# Create certificate file (paste the Certificate)
nano /etc/ssl/cloudflare-cert.pem

# Create key file (paste the Private Key)
nano /etc/ssl/cloudflare-key.pem

# Secure the key
chmod 600 /etc/ssl/cloudflare-key.pem
```

### Step 5: Install and Configure nginx

```bash
apt install -y nginx

nano /etc/nginx/sites-available/mcpserver
```

Paste this config (replace `mcp.your-domain.com` with your subdomain):

```nginx
server {
    listen 80;
    server_name mcp.your-domain.com;
    return 301 https://$server_name$request_uri;
}

server {
    listen 443 ssl;
    server_name mcp.your-domain.com;

    ssl_certificate /etc/ssl/cloudflare-cert.pem;
    ssl_certificate_key /etc/ssl/cloudflare-key.pem;

    location / {
        proxy_pass http://127.0.0.1:8080;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $http_cf_connecting_ip;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_buffering off;
        proxy_cache off;
        proxy_read_timeout 86400;
    }

    location /sse {
        proxy_pass http://127.0.0.1:8080/sse;
        proxy_http_version 1.1;
        proxy_set_header Connection "";
        proxy_buffering off;
        proxy_cache off;
        proxy_read_timeout 86400;
        chunked_transfer_encoding off;
    }
}
```

Enable and start nginx:
```bash
ln -sf /etc/nginx/sites-available/mcpserver /etc/nginx/sites-enabled/
rm -f /etc/nginx/sites-enabled/default
nginx -t
systemctl reload nginx
ufw allow 'Nginx Full'
```

### Step 6: Configure PUBLIC_URL

Edit `.env` to add your public URL:
```bash
nano /opt/mcpserver/app/.env
```

Add:
```
PUBLIC_URL=https://mcp.your-domain.com
```

Restart the service:
```bash
systemctl restart mcpserver
```

### Step 7: Verify OAuth Discovery

```bash
curl https://mcp.your-domain.com/.well-known/oauth-authorization-server
```

Should return JSON with your public URL (not localhost).

---

## Connect Claude.ai

Once HTTPS is set up:

1. Go to **https://claude.ai** → Settings → Integrations
2. Add MCP Server
3. Enter:
   - **URL:** `https://mcp.your-domain.com/sse`
   - **Client ID:** (from your `.env` or startup logs)
   - **Client Secret:** (from your `.env` or startup logs)

To find your OAuth credentials:
```bash
journalctl -u mcpserver | grep -A5 "OAUTH CREDENTIALS"
```

---

## Alternative: Let's Encrypt SSL (without Cloudflare)

If not using Cloudflare proxy, use certbot:

```bash
apt install -y certbot python3-certbot-nginx
certbot --nginx -d mcp.your-domain.com --email your@email.com --agree-tos --non-interactive
```

---

## Useful Commands

```bash
# Service management
systemctl restart mcpserver
systemctl stop mcpserver
journalctl -u mcpserver -f

# Auto-deploy logs
tail -f /var/log/mcpserver-deploy.log

# Manual deploy trigger
systemctl start mcpserver-deploy.service

# Check auto-deploy timer
systemctl list-timers | grep mcpserver

# View OAuth credentials
journalctl -u mcpserver | grep -A5 "OAUTH CREDENTIALS"

# Test OAuth discovery
curl https://your-domain.com/.well-known/oauth-authorization-server
```

---

## Troubleshooting

### Service won't start
```bash
journalctl -u mcpserver -n 100 --no-pager
```

### Claude CLI not authenticated
```bash
su - mcpuser
claude login
exit
systemctl restart mcpserver
```

### "Cannot run as root" error
Make sure the service file has `User=mcpuser` (not `User=root`)

### Git clone fails with password prompt
Use SSH URL (`git@github.com:...`) not HTTPS URL

### Git "dubious ownership" error
If auto-deploy fails with "fatal: detected dubious ownership in repository":
```bash
git config --system --add safe.directory /opt/mcpserver/app
```
This happens when the repository owner differs from the user running the deploy script.

### Playwright browser not found
If Playwright fails with browser not found errors, install manually:
```bash
# As root - install system dependencies (Ubuntu 24.04)
apt install -y libnss3 libnspr4 libatk1.0-0t64 libatk-bridge2.0-0t64 libcups2t64 \
  libxkbcommon0 libxcomposite1 libxdamage1 libxfixes3 libxrandr2 libgbm1 \
  libpango-1.0-0 libcairo2 libasound2t64

# As mcpuser - install browser
su - mcpuser
cd /opt/mcpserver/app
source venv/bin/activate
playwright install chromium
```

Verify it works:
```bash
python -c "from playwright.sync_api import sync_playwright; p = sync_playwright().start(); b = p.chromium.launch(); print('OK'); b.close(); p.stop()"
```

### OAuth returns localhost URLs
Make sure `PUBLIC_URL` is set in `.env` and service is restarted

### Claude.ai connection fails
1. Check OAuth discovery: `curl https://your-domain.com/.well-known/oauth-authorization-server`
2. Verify URLs show your domain, not localhost
3. Check nginx logs: `tail -f /var/log/nginx/access.log`

### nginx SSL errors
1. Verify certificate files exist and are readable
2. Check Cloudflare SSL mode is set to "Full"
3. Test config: `nginx -t`

### Database backup
```bash
cp /opt/mcpserver/app/jobs.db /opt/mcpserver/app/jobs.db.backup
```
