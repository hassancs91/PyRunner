#!/bin/bash
set -e

# If a command was passed (e.g. `docker run ... python -c "..."`, as the docs'
# key-generation one-liners do), run it instead of booting the app. Placed
# before the key checks on purpose: generating a key must not require one.
if [ "$#" -gt 0 ]; then
    exec "$@"
fi

echo "=========================================="
echo "  PyRunner - Starting up..."
echo "=========================================="

# Validate required environment variables. Also reject the two literal values
# once baked into published images (≤1.15.0) via Dockerfile ENV: on those
# images the variables are never unset, so an empty-check alone can't catch a
# deployment running on the publicly-known keys.
if [ -z "$SECRET_KEY" ] || [ "$SECRET_KEY" = "build-only-key-not-for-runtime" ]; then
    echo ""
    echo "ERROR: SECRET_KEY is required but not set."
    echo ""
    echo "Generate one with:"
    echo "  python -c \"from django.core.management.utils import get_random_secret_key; print(get_random_secret_key())\""
    echo ""
    exit 1
fi

if [ -z "$ENCRYPTION_KEY" ] || [ "$ENCRYPTION_KEY" = "QUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUE=" ]; then
    echo ""
    echo "ERROR: ENCRYPTION_KEY is required but not set."
    echo ""
    echo "Generate one with:"
    echo "  python -c \"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\""
    echo ""
    exit 1
fi

# Run setup (migrations + default environment) with plugins DISABLED. setup runs
# django.setup(), which would otherwise import every ACTIVE plugin — and a broken
# one (e.g. a throwing ready()) would crash migrations/boot BEFORE the preflight
# step below ever gets a chance to quarantine it. Core migrations don't need
# plugins loaded; each plugin's own migrations are applied per-plugin by the
# preflight step (in isolation), not here.
echo "[*] Running setup..."
PYRUNNER_DISABLE_PLUGINS=1 python manage.py setup

# --- Plugin system ----------------------------------------------------------
# Seed the plugins package so `import plugins.<slug>` resolves even on a fresh
# (empty) data volume, then validate every ACTIVE plugin in its own subprocess
# BEFORE the web server imports anything. Broken plugins are flipped to ERRORED
# (quarantined) so gunicorn only ever loads plugins that just passed in
# isolation. This step is never fatal: the orchestrator always exits 0, and the
# `|| true` is a final backstop so a broken plugin can never block startup.
PLUGINS_DIR="${PLUGINS_DIR:-/app/plugins}"
mkdir -p "$PLUGINS_DIR"
if [ ! -f "$PLUGINS_DIR/__init__.py" ]; then
    echo "# PyRunner plugins package (auto-seeded). Do not delete." > "$PLUGINS_DIR/__init__.py"
fi

# Plugin Dev Mode (Plugin Platform v2, WS1): when PYRUNNER_PLUGIN_DEV points at a
# local plugin folder, there is no installed/active plugin set to seed or
# preflight — the dev plugin loads live under runserver (which sets RUN_MAIN),
# never under this gunicorn boot path. Skip both steps so a dev folder can't
# stall startup. (Note: this container boot still runs gunicorn, which never
# loads the dev plugin; dev iteration uses `manage.py runserver`.)
if [ -n "$PYRUNNER_PLUGIN_DEV" ]; then
    echo "[*] Dev mode active: local plugin '$PYRUNNER_PLUGIN_DEV' — skipping seed + preflight."
else
    # Seed the bundled example plugin on first boot only (INSTALLED, never active),
    # so a fresh deployment has an example to look at / try / delete. Idempotent and
    # never fatal.
    echo "[*] Seeding example plugin (first boot only)..."
    PYRUNNER_DISABLE_PLUGINS=1 python manage.py seed_example_plugin || true

    echo "[*] Preflighting plugins..."
    PYRUNNER_DISABLE_PLUGINS=1 python manage.py plugin_preflight --all --disable-broken || true
fi

echo ""
echo "[*] Starting services..."

# PID file location — `manage.py restart_workers` reads this to SIGTERM the
# worker; the monitor loop below rewrites it when it starts a replacement.
QCLUSTER_PID_FILE="/tmp/qcluster.pid"

# Function to start qcluster worker. pyrunner_qcluster (not plain qcluster):
# it applies the DB-backed Workers settings — unreachable at settings-import
# time — before the cluster starts, so "restart workers to apply" is real.
start_qcluster() {
    echo "    - Starting django-q2 worker..."
    python manage.py pyrunner_qcluster &
    QCLUSTER_PID=$!
    echo $QCLUSTER_PID > "$QCLUSTER_PID_FILE"
    echo "    - Worker started with PID $QCLUSTER_PID"
}

# NOTE: no signal traps here on purpose. The `exec gunicorn` at the bottom
# replaces this shell, so any trap set now would silently die with it — an
# earlier version trapped SIGUSR1 for "restart workers" and SIGTERM for
# cleanup, and both were dead code from the moment gunicorn started. The
# monitor loop below is the one restart mechanism: anything that wants a fresh
# worker (the Settings "Restart workers" button, a crash, an OOM kill) just
# has to make the old process die.

# Start workers initially
start_qcluster

# Worker monitoring loop (runs in background)
monitor_workers() {
    local backoff=1
    local max_backoff=60

    while true; do
        sleep 5

        # Check if worker is still running
        if [ -f "$QCLUSTER_PID_FILE" ]; then
            local pid=$(cat "$QCLUSTER_PID_FILE")
            if ! kill -0 $pid 2>/dev/null; then
                echo "[!] Worker (PID $pid) died unexpectedly, restarting in ${backoff}s..."
                sleep $backoff
                start_qcluster
                # Increase backoff (exponential with max)
                backoff=$((backoff * 2))
                if [ $backoff -gt $max_backoff ]; then
                    backoff=$max_backoff
                fi
            else
                # Worker is running, reset backoff
                backoff=1
            fi
        fi
    done
}
# The monitor is a child process, so it SURVIVES the exec below.
monitor_workers &

# Start gunicorn web server
echo "    - Starting web server on port ${PORT:-8000}..."
echo ""
echo "=========================================="
echo "  PyRunner is ready!"
# PUBLIC_PORT is the host-side published port (set by compose from the host
# PORT). PORT here is the container-internal bind (pinned 8000), so it would be
# wrong to show whenever the host maps a different port. Fall back to it only
# when PUBLIC_PORT is unset (e.g. Coolify, where access is via FQDN anyway).
echo "  Open http://localhost:${PUBLIC_PORT:-${PORT:-8000}}"
echo "=========================================="
echo ""

# exec makes gunicorn PID 1: `docker stop` delivers SIGTERM straight to it for
# a clean web shutdown. The monitor + qcluster children get no TERM and are
# SIGKILLed at Docker's grace deadline — safe by design: django-q2 re-delivers
# interrupted PENDING tasks, execute_run's atomic claim makes duplicate
# deliveries no-ops, and a run whose worker died mid-execution is reconciled
# to failed by Run.reconcile_stale (worker heartbeat / runs page). (A shell
# trap can't forward TERM here — no trap survives exec — and keeping gunicorn
# as PID 1 is the more robust default.)
exec gunicorn pyrunner.wsgi:application \
    --bind 0.0.0.0:${PORT:-8000} \
    --workers ${GUNICORN_WORKERS:-2} \
    --threads ${GUNICORN_THREADS:-4} \
    --timeout ${GUNICORN_TIMEOUT:-120} \
    --access-logfile - \
    --error-logfile -
