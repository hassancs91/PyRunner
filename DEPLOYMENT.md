# PyRunner Deployment Guide

This guide covers deploying PyRunner to a production server.

## Prerequisites

- **Server**: Linux VPS (Ubuntu 22.04+ recommended)
- **Docker**: Docker Engine 20.10+
- **Docker Compose**: v2.0+
- **Domain**: Optional, for SSL/HTTPS setup
- **RAM**: Minimum 1GB (2GB recommended)
- **Storage**: 10GB+ for scripts and environments

### Install Docker (Ubuntu)

```bash
# Update packages
sudo apt update && sudo apt upgrade -y

# Install Docker
curl -fsSL https://get.docker.com | sudo sh

# Add your user to docker group
sudo usermod -aG docker $USER

# Log out and back in, then verify
docker --version
docker compose version
```

---

## Quick Start

### 1. Clone or Copy Files

```bash
# Create directory
mkdir -p /opt/pyrunner
cd /opt/pyrunner

# Copy your docker-compose.yml and Dockerfile
# Or clone from your repository
```

### 2. Create Environment File

```bash
cat > .env << 'EOF'
# Django Settings
SECRET_KEY=your-secure-secret-key-here
DEBUG=False
ALLOWED_HOSTS=your-domain.com,your-server-ip

# Encryption (SAVE THIS KEY!)
ENCRYPTION_KEY=

# Workers
Q_WORKERS=2
GUNICORN_WORKERS=2
GUNICORN_THREADS=4
GUNICORN_TIMEOUT=120

# Email (optional)
EMAIL_BACKEND=django.core.mail.backends.console.EmailBackend
DEFAULT_FROM_EMAIL=noreply@your-domain.com
EOF
```

### 3. Deploy

```bash
# Build and start
docker compose up -d --build

# Check logs for auto-generated ENCRYPTION_KEY
docker compose logs pyrunner | grep -A5 "ENCRYPTION_KEY"

# Save the key to your .env file!
```

### 4. Access PyRunner

Open `http://your-server-ip:8000` in your browser.

---

## One-Click Docker Deployment

Deploy PyRunner on platforms like **Coolify**, **Railway**, **Render**, or any Docker-based hosting with just the image and environment variables.

### Docker Image

```
hasanaboulhasan/pyrunner:latest
```

Available on [Docker Hub](https://hub.docker.com/r/hasanaboulhasan/pyrunner).

### Required Configuration

| Setting | Value |
|---------|-------|
| **Port** | `8000` |
| **Volume Mount** | `/app/data` (for persistence) |
| **Health Check** | `http://localhost:8000/` |

### Environment Variables

Set these in your platform's environment configuration:

```env
# Required
DEBUG=False
ALLOWED_HOSTS=your-app-domain.com

# Auto-generated if empty (check logs and save!)
SECRET_KEY=
ENCRYPTION_KEY=

# Optional - Worker tuning
Q_WORKERS=2
GUNICORN_WORKERS=2
GUNICORN_THREADS=4
GUNICORN_TIMEOUT=120
```

### Coolify

1. Create new **Docker** resource
2. Set **Image**: `hasanaboulhasan/pyrunner:latest`
3. Set **Port**: `8000`
4. Add **Volume** with these settings:
   | Field | Value |
   |-------|-------|
   | **Name** | `pyrunner-data` |
   | **Source Path** | Leave empty (Coolify manages it) |
   | **Destination Path** | `/app/data` |
5. Add environment variables (see above)
6. Deploy

### Railway

1. Create new project from **Docker Image**
2. Set image: `hasanaboulhasan/pyrunner:latest`
3. Add environment variables in **Variables** tab
4. Add **Volume** mount to `/app/data`
5. Railway auto-detects port from Dockerfile (`8000`)
6. Deploy

### Render

1. Create new **Web Service**
2. Select **Docker** and set image: `hasanaboulhasan/pyrunner:latest`
3. Set environment variables
4. Add **Disk** mounted at `/app/data`
5. Deploy

### CapRover

1. Create new app
2. Deploy via **Dockerfile** from repo
3. Set environment variables in **App Configs**
4. Add persistent volume at `/app/data`
5. Enable HTTPS

### Important Notes

- **Save your ENCRYPTION_KEY**: On first run, check container logs for the auto-generated key. Save it immediately - you need it to decrypt stored secrets.
- **Persistent storage is required**: Mount a volume at `/app/data` to persist your database, scripts, and Python environments.
- **ALLOWED_HOSTS**: Set this to your app's domain (e.g., `myapp.railway.app`).

---

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `SECRET_KEY` | Auto-generated | Django secret key |
| `DEBUG` | `False` | Debug mode (set False in production) |
| `ALLOWED_HOSTS` | `localhost,127.0.0.1` | Comma-separated allowed hostnames |
| `ENCRYPTION_KEY` | Auto-generated | Fernet key for secrets encryption |
| `Q_WORKERS` | `2` | Number of background task workers |
| `GUNICORN_WORKERS` | `2` | Number of web server workers |
| `GUNICORN_THREADS` | `4` | Threads per worker |
| `GUNICORN_TIMEOUT` | `120` | Worker timeout in seconds |
| `PORT` | `8000` | External port mapping |

### Email Configuration (Optional)

| Variable | Default | Description |
|----------|---------|-------------|
| `EMAIL_BACKEND` | Console backend | Django email backend |
| `DEFAULT_FROM_EMAIL` | `noreply@pyrunner.local` | Sender email address |
| `USE_RESEND` | `False` | Enable Resend email service |
| `RESEND_API_KEY` | - | Resend API key |

---

## Reverse Proxy Setup

### Option 1: Caddy (Recommended - Auto SSL)

```bash
# Install Caddy
sudo apt install -y debian-keyring debian-archive-keyring apt-transport-https
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | sudo gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' | sudo tee /etc/apt/sources.list.d/caddy-stable.list
sudo apt update && sudo apt install caddy
```

Create `/etc/caddy/Caddyfile`:

```
pyrunner.your-domain.com {
    reverse_proxy localhost:8000
}
```

```bash
# Reload Caddy
sudo systemctl reload caddy
```

Caddy automatically provisions SSL certificates.

### Option 2: Nginx + Certbot

```bash
# Install Nginx and Certbot
sudo apt install nginx certbot python3-certbot-nginx -y
```

Create `/etc/nginx/sites-available/pyrunner`:

```nginx
server {
    listen 80;
    server_name pyrunner.your-domain.com;

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_read_timeout 300s;
        proxy_connect_timeout 75s;
    }
}
```

```bash
# Enable site
sudo ln -s /etc/nginx/sites-available/pyrunner /etc/nginx/sites-enabled/
sudo nginx -t && sudo systemctl reload nginx

# Get SSL certificate
sudo certbot --nginx -d pyrunner.your-domain.com
```

### Update ALLOWED_HOSTS

After setting up your domain, update `.env`:

```bash
ALLOWED_HOSTS=pyrunner.your-domain.com,localhost,127.0.0.1
```

Then restart:

```bash
docker compose down && docker compose up -d
```

---

## Firewall Configuration

```bash
# Allow SSH (important - don't lock yourself out!)
sudo ufw allow ssh

# Allow HTTP/HTTPS
sudo ufw allow 80/tcp
sudo ufw allow 443/tcp

# If accessing PyRunner directly (no reverse proxy)
sudo ufw allow 8000/tcp

# Enable firewall
sudo ufw enable
```

---

## Backup & Restore

### Create Backup

**Via Web Interface:**
1. Go to **Settings > Backup**
2. Click **Download Backup**
3. Save the JSON file securely

**Via Command Line:**
```bash
# Backup the entire data volume
docker run --rm -v pyrunner_data:/data -v $(pwd):/backup alpine \
    tar czf /backup/pyrunner-backup-$(date +%Y%m%d).tar.gz -C /data .
```

### Restore Backup

**Via Web Interface:**
1. Go to **Settings > Backup**
2. Upload your backup file
3. Confirm restore

**Via Command Line:**
```bash
# Stop container
docker compose down

# Restore volume
docker run --rm -v pyrunner_data:/data -v $(pwd):/backup alpine \
    sh -c "rm -rf /data/* && tar xzf /backup/pyrunner-backup-YYYYMMDD.tar.gz -C /data"

# Start container
docker compose up -d
```

---

## Monitoring & Logs

### View Logs

```bash
# All logs
docker compose logs -f

# Last 100 lines
docker compose logs --tail 100

# Follow specific service
docker compose logs -f pyrunner
```

### Check Container Status

```bash
# Container health
docker compose ps

# Resource usage
docker stats pyrunner
```

### Check Migration Status

```bash
docker exec pyrunner python manage.py showmigrations
```

---

## Updating PyRunner

```bash
cd /opt/pyrunner

# Stop container
docker compose down

# Pull latest changes (if using git)
git pull

# Rebuild and start
docker compose up -d --build

# Check logs
docker compose logs -f
```

See [UPDATE_GUIDE.md](UPDATE_GUIDE.md) for detailed update instructions.

---

## Troubleshooting

### Container Won't Start

```bash
# Check logs
docker compose logs pyrunner

# Check if port is in use
sudo lsof -i :8000
```

### Secrets Not Decrypting

Your `ENCRYPTION_KEY` may have changed. Restore the original key from your `.env` file or backup.

### 502 Bad Gateway (Nginx/Caddy)

```bash
# Check if container is running
docker compose ps

# Check container logs
docker compose logs pyrunner

# Verify port binding
docker port pyrunner
```

### Slow Script Execution

Increase worker timeout:

```bash
# In .env
GUNICORN_TIMEOUT=300
Q_WORKERS=4
```

### Permission Denied Errors

```bash
# Fix volume permissions
docker exec pyrunner chown -R root:root /app/data
```

---

## Production Checklist

- [ ] `DEBUG=False` in `.env`
- [ ] Strong `SECRET_KEY` set (not auto-generated)
- [ ] `ENCRYPTION_KEY` saved securely
- [ ] `ALLOWED_HOSTS` configured with your domain
- [ ] Reverse proxy configured (Caddy/Nginx)
- [ ] SSL/HTTPS enabled
- [ ] Firewall configured
- [ ] Backup schedule established
- [ ] Monitoring/alerting set up

---

## Data Locations

All persistent data is stored in the `pyrunner_data` Docker volume:

```
/app/data/
├── db.sqlite3          # Database
├── environments/       # Python virtual environments
│   └── default/        # Default environment
└── workdir/            # Script execution workspace
```

---

## Support

- GitHub Issues: Report bugs and feature requests
- Documentation: Check the in-app docs at `/docs/`
