#!/usr/bin/env bash
#
# Run the full PyRunner stack locally in Docker, exactly as it runs in
# production. macOS/Linux counterpart of run-local.ps1.
#
# Builds the production image from the Dockerfile and runs it via
# docker-compose.yml - the same definition used for self-hosting. That single
# container is the whole stack:
#
#   * gunicorn  -> WSGI web server (NOT manage.py runserver)
#   * django-q2 -> background task worker (started by entrypoint.sh, with the
#                  same auto-restart monitor used in production)
#   * WhiteNoise serves the collected/compressed static files baked into the
#     image, so there is no Tailwind watcher and DEBUG is OFF.
#
# On start it ensures a .env with real SECRET_KEY / ENCRYPTION_KEY and pins the
# production-parity flags (DEBUG=False, SECURE_SSL_REDIRECT=False so plain http
# works locally). The SQLite DB and environments live in the named Docker
# volume `pyrunner_data`, so they persist across runs (use --fresh to wipe).
#
# Requires Docker (Desktop) running. The first build is slow (it installs
# Node + the Claude CLI + Python deps); later runs reuse the layer cache.
#
# Usage:
#   ./run-local.sh                 build + run, stream logs here (Ctrl+C stops)
#   ./run-local.sh --detached      run in the background; then --logs / --down
#   ./run-local.sh --fresh         wipe the data volume and start clean
#   ./run-local.sh --port 9000     publish on a different host port
#   ./run-local.sh --rebuild       force a clean, no-cache image rebuild
#   ./run-local.sh --no-build      start the existing image without rebuilding
#   ./run-local.sh --logs          follow logs of the running stack, then exit
#   ./run-local.sh --down          stop and remove the stack (keep the volume)

set -eu

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"
COMPOSE_FILE="$SCRIPT_DIR/docker-compose.yml"
ENV_FILE="$SCRIPT_DIR/.env"

# --- pretty output -----------------------------------------------------------
if [ -t 1 ]; then
    CYAN='\033[0;36m'; GREEN='\033[0;32m'; YELLOW='\033[0;33m'; NC='\033[0m'
else
    CYAN=''; GREEN=''; YELLOW=''; NC=''
fi
step() { printf "\n${CYAN}==> %s${NC}\n" "$1"; }
ok()   { printf "${GREEN}    %s${NC}\n" "$1"; }
warn() { printf "${YELLOW}    %s${NC}\n" "$1"; }
die()  { printf "${YELLOW}ERROR: %s${NC}\n" "$1" >&2; exit 1; }

usage() { sed -n '3,38p' "$0" | sed 's/^# \{0,1\}//'; }

# --- args --------------------------------------------------------------------
PORT=8123
NO_BUILD=0; REBUILD=0; DETACHED=0; DO_LOGS=0; DO_DOWN=0; DO_FRESH=0
while [ $# -gt 0 ]; do
    case "$1" in
        -p|--port)     PORT="$2"; shift 2 ;;
        --port=*)      PORT="${1#*=}"; shift ;;
        --no-build)    NO_BUILD=1; shift ;;
        --rebuild)     REBUILD=1; shift ;;
        -d|--detached) DETACHED=1; shift ;;
        --logs)        DO_LOGS=1; shift ;;
        --down)        DO_DOWN=1; shift ;;
        --fresh)       DO_FRESH=1; shift ;;
        -h|--help)     usage; exit 0 ;;
        *)             echo "Unknown option: $1"; echo; usage; exit 1 ;;
    esac
done

# --- 1. Verify Docker + Compose ---------------------------------------------
command -v docker >/dev/null 2>&1 || die "Docker not found on PATH. Install Docker Desktop and try again."
docker info >/dev/null 2>&1 || die "Docker is installed but the daemon isn't reachable. Start Docker Desktop and retry."

if docker compose version >/dev/null 2>&1; then
    COMPOSE="docker compose"
elif command -v docker-compose >/dev/null 2>&1; then
    COMPOSE="docker-compose"
else
    die "Docker Compose not found (neither 'docker compose' nor 'docker-compose')."
fi

# $COMPOSE is intentionally unquoted so "docker compose" splits into two words.
compose() { $COMPOSE -f "$COMPOSE_FILE" "$@"; }

# --- 2. Lifecycle short-circuits (down / fresh / logs) -----------------------
if [ "$DO_DOWN" -eq 1 ]; then
    step "Stopping PyRunner (keeping data volume)"
    compose down
    ok "Stopped. Data volume 'pyrunner_data' preserved."
    exit 0
fi

if [ "$DO_LOGS" -eq 1 ]; then
    step "Following logs (Ctrl+C to stop watching)"
    compose logs -f
    exit 0
fi

if [ "$DO_FRESH" -eq 1 ]; then
    step "Tearing down stack AND deleting data volume"
    compose down -v
    ok "Clean slate - DB and environments removed."
fi

# --- 3. Ensure .env with production-parity values ----------------------------
# Keys are generated with openssl (ships with macOS) so no local Python needed.
command -v openssl >/dev/null 2>&1 || die "openssl not found; needed to generate SECRET_KEY/ENCRYPTION_KEY."

# SECRET_KEY: base64url, no padding -> safe inside a .env value.
gen_secret() { openssl rand -base64 48 | tr '+/' '-_' | tr -d '=\r\n'; }
# Fernet key: urlsafe-base64 of 32 random bytes, 44 chars INCLUDING '=' padding.
gen_fernet() { openssl rand -base64 32 | tr '+/' '-_' | tr -d '\r\n'; }

get_env_value() {
    [ -f "$ENV_FILE" ] || return 0
    grep -E "^$1=" "$ENV_FILE" 2>/dev/null | head -n1 | cut -d'=' -f2- || true
}

set_env_value() {
    local key="$1" val="$2"
    if [ -f "$ENV_FILE" ] && grep -qE "^$key=" "$ENV_FILE"; then
        local tmp; tmp="$(mktemp "${TMPDIR:-/tmp}/pyrunner.XXXXXX")"
        grep -vE "^$key=" "$ENV_FILE" > "$tmp" || true
        mv "$tmp" "$ENV_FILE"
    fi
    printf '%s=%s\n' "$key" "$val" >> "$ENV_FILE"
}

if [ ! -f "$ENV_FILE" ]; then
    step "No .env found - generating one with fresh keys"
    printf '%s\n' '# Maintained by run-local.sh - PyRunner production-parity local run (Docker).' \
                  '# SECRET_KEY / ENCRYPTION_KEY are preserved across runs; keep them if you store secrets.' \
        > "$ENV_FILE"
fi

case "$(get_env_value SECRET_KEY)" in
    ""|your-*) set_env_value SECRET_KEY "$(gen_secret)"; ok "Generated SECRET_KEY" ;;
esac
case "$(get_env_value ENCRYPTION_KEY)" in
    ""|your-*) set_env_value ENCRYPTION_KEY "$(gen_fernet)"; ok "Generated ENCRYPTION_KEY" ;;
esac
[ -n "$(get_env_value ALLOWED_HOSTS)" ] || set_env_value ALLOWED_HOSTS "localhost,127.0.0.1"

# Force the production-parity flags (this is the whole point of this script).
cur_debug="$(get_env_value DEBUG)"
if [ "$cur_debug" != "False" ]; then
    warn "Setting DEBUG=False for production parity (was '${cur_debug:-unset}')."
fi
set_env_value DEBUG False
set_env_value SECURE_SSL_REDIRECT False   # plain http locally; edge/proxy does TLS in prod
ok ".env ready for production-parity run."

# --- 4. Build + run ----------------------------------------------------------
# Host port for the ${PORT:-8000}:8000 mapping. The container always binds 8000
# internally (compose pins PORT=8000 in the service env), so this only moves the
# host side - no conflict with the literal container port.
export PORT="$PORT"
URL="http://localhost:$PORT"

if [ "$REBUILD" -eq 1 ]; then
    step "Rebuilding image from scratch (--no-cache)"
    compose build --no-cache
fi

UP_ARGS="up"
if [ "$NO_BUILD" -eq 0 ] && [ "$REBUILD" -eq 0 ]; then UP_ARGS="$UP_ARGS --build"; fi
if [ "$DETACHED" -eq 1 ]; then UP_ARGS="$UP_ARGS -d"; fi

step "Starting full PyRunner stack (gunicorn + django-q2 worker) -> $URL"
echo "    image:   built from Dockerfile (production)"
echo "    web:     gunicorn pyrunner.wsgi (DEBUG=False, WhiteNoise static)"
echo "    worker:  django-q2 qcluster (auto-restart monitor)"
echo "    data:    docker volume 'pyrunner_data' (persists across runs)"
echo

if [ "$DETACHED" -eq 1 ]; then
    compose $UP_ARGS
    echo
    ok "PyRunner is starting in the background at $URL"
    warn "First start runs migrations + setup; give it a few seconds."
    echo
    printf "${YELLOW}    Follow logs:  ./run-local.sh --logs${NC}\n"
    printf "${YELLOW}    Stop:         ./run-local.sh --down${NC}\n"
    printf "${YELLOW}    Fresh start:  ./run-local.sh --fresh${NC}\n"
else
    printf "${GREEN}  PyRunner will be available at %s${NC}\n" "$URL"
    printf "${YELLOW}  Press Ctrl+C to stop the stack.${NC}\n"
    echo
    # Foreground: compose streams logs and handles Ctrl+C (graceful stop).
    compose $UP_ARGS
fi
