# PyRunner

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Docker](https://img.shields.io/badge/Docker-Ready-2496ED?logo=docker&logoColor=white)](https://hub.docker.com/r/hasanaboulhasan/pyrunner)
[![Version](https://img.shields.io/badge/Version-1.1.1-green.svg)](https://github.com/hassancs91/PyRunner/releases)

A self-hosted Python script automation platform. Upload a script, schedule it, monitor it — nothing else to configure.

## Features

- **Script Management** — Create, edit, and organize Python scripts from your browser
- **Flexible Scheduling** — Run scripts manually, at intervals, or daily at specific times
- **Virtual Environments** — Isolated Python environments with custom pip packages per script
- **Run History & Logs** — Track every execution with stdout/stderr capture
- **Secrets Management** — Store encrypted environment variables and secrets
- **Notifications** — Get alerts via email, webhook, or Telegram on script completion/failure
- **Magic Link Auth** — Passwordless authentication via email
- **Single Container** — Deploy with one Docker command

## Quick Start

### Using Docker Compose

```bash
# Clone the repository
git clone https://github.com/hassancs91/PyRunner.git
cd PyRunner

# Copy environment template
cp .env.example .env

# Start PyRunner
docker compose up -d

```

Open `http://localhost:8000` in your browser.

### Using Docker Hub Image

```bash
docker run -d \
  --name pyrunner \
  -p 8000:8000 \
  -v pyrunner_data:/app/data \
  -e DEBUG=False \
  -e ALLOWED_HOSTS=localhost \
  hasanaboulhasan/pyrunner:latest
```

## Configuration

Copy `.env.example` to `.env` and configure:

| Variable | Default | Description |
|----------|---------|-------------|
| `SECRET_KEY` | Auto-generated | Django secret key |
| `DEBUG` | `False` | Debug mode (disable in production) |
| `ALLOWED_HOSTS` | `localhost,127.0.0.1` | Allowed hostnames |
| `ENCRYPTION_KEY` | Key for encrypting secrets (save this!) |
| `Q_WORKERS` | `2` | Background task workers |

See [.env.example](.env.example) for all options.

## Tech Stack

- **Backend**: Django, django-q2
- **Frontend**: Tailwind CSS
- **Database**: SQLite
- **Deployment**: Docker

## Requirements

- Docker Engine 20.10+
- Docker Compose v2.0+
- 1GB RAM minimum (2GB recommended)

## License

This project is licensed under the MIT License — see the [LICENSE](LICENSE) file for details.
