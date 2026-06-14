# PyRunner

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Docker](https://img.shields.io/badge/Docker-Ready-2496ED?logo=docker&logoColor=white)](https://hub.docker.com/r/hasanaboulhasan/pyrunner)
[![Version](https://img.shields.io/badge/Version-1.1.1-green.svg)](https://github.com/hassancs91/PyRunner/releases)
[![Discord](https://img.shields.io/badge/Discord-Join%20Server-5865F2?logo=discord&logoColor=white)](https://discord.gg/BjkmTn7XSd)

A self-hosted Python script automation platform. Upload a script, schedule it, monitor it — nothing else to configure.

## Features

- **Script Management** — Create, edit, and organize Python scripts from your browser
- **Flexible Scheduling** — Run scripts manually, at intervals, or daily at specific times
- **Virtual Environments** — Isolated Python environments with custom pip packages per script
- **Run History & Logs** — Track every execution with stdout/stderr capture
- **Secrets Management** — Store encrypted environment variables and secrets
- **Claude AI for Scripts** — Call Claude from your scripts (web search, fetch & more) using your Claude subscription or an Anthropic API key
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
| `SECRET_KEY` | **Required** | Django secret key (container exits if unset — see [.env.example](.env.example)) |
| `ENCRYPTION_KEY` | **Required** | Fernet key for encrypting stored secrets — save this somewhere safe |
| `DEBUG` | `False` | Debug mode (disable in production) |
| `ALLOWED_HOSTS` | `localhost,127.0.0.1` | Allowed hostnames |
| `Q_WORKERS` | `2` | Background task workers |

See [.env.example](.env.example) for all options.

## Claude AI in your scripts

PyRunner can run Claude directly inside your Python scripts — with **web search**
and **web fetch** enabled by default — so your automations can research, summarize,
and reason. It reuses **your own** Claude account: either a **Claude subscription**
(via a Claude Code OAuth token) or an **Anthropic API key**.

> **A note on subscription auth:** this is intended for *self-hosters using their
> own Claude subscription for their own automations* — equivalent to running
> Claude Code headless on your own machine. Anthropic's Agent SDK terms restrict
> *offering* claude.ai login to other people as part of a product/SaaS; don't use
> a shared or pooled subscription to serve multiple end-users.

### Setup

1. **Configure it** under **Services → Claude AI**:
   - Choose **Claude subscription** and paste a token, or **Anthropic API key**.
   - To get a subscription token, run `claude setup-token` on a machine where
     you're logged into Claude, then paste the result.
   - Tick **Enable Claude AI for scripts** and **Save**, then **Test Connection**
     (it runs a real web search to confirm everything works end-to-end).
2. **Install the SDK** into the Environment your script uses:
   Environments → *(your env)* → Packages → add `claude-agent-sdk`.
   (The Claude Code CLI itself ships with the PyRunner Docker image.)

### Use it

```python
from pyrunner_ai import ask_claude

# Web search + fetch are on by default
answer = ask_claude("Search the web for today's top AI story and summarize it")
print(answer)

# Restrict tools, pick a model, add a system prompt
summary = ask_claude(
    "Summarize https://peps.python.org/pep-0008/",
    tools=["WebFetch"],
    model="claude-sonnet-4-6",
)

# Full details (tools used, cost, turns)
result = ask_claude("Research the latest Django release", raw=True)
print(result.text, result.tools_used, result.cost_usd)

# Stream the answer
from pyrunner_ai import stream_claude
for chunk in stream_claude("Write a short poem about automation"):
    print(chunk, end="", flush=True)

# Lean mode: only load the tools you ask for (cuts ~50k cached tokens/call)
answer = ask_claude("Search the web for today's AI news", lean=True)
```

Available tools: `WebSearch`, `WebFetch`, `Read`, `Glob`, `Grep`. File-writing
and shell tools (`Write`, `Edit`, `Bash`) are **off by default** for safety —
scripts run with full access to the PyRunner host, so only enable those if you
fully trust the prompt.

**A note on token counts:** an agentic call carries the agent's tool definitions
as context, sent once and **prompt-cached** — that's the large "cache tokens"
figure on the usage page, not your content. Pass `lean=True` to define only the
tools you requested and slash that overhead. The connection test always runs
lean.

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
