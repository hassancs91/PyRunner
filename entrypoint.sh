#!/bin/bash
set -e

echo "=========================================="
echo "  PyRunner - Starting up..."
echo "=========================================="

# Validate required environment variables
if [ -z "$SECRET_KEY" ]; then
    echo ""
    echo "ERROR: SECRET_KEY is required but not set."
    echo ""
    echo "Generate one with:"
    echo "  python -c \"from django.core.management.utils import get_random_secret_key; print(get_random_secret_key())\""
    echo ""
    exit 1
fi

if [ -z "$ENCRYPTION_KEY" ]; then
    echo ""
    echo "ERROR: ENCRYPTION_KEY is required but not set."
    echo ""
    echo "Generate one with:"
    echo "  python -c \"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\""
    echo ""
    exit 1
fi

# Run setup (migrations + default environment)
echo "[*] Running setup..."
python manage.py setup

echo ""
echo "[*] Starting services..."

# PID file location
QCLUSTER_PID_FILE="/tmp/qcluster.pid"

# Function to start qcluster worker
start_qcluster() {
    echo "    - Starting django-q2 worker..."
    python manage.py qcluster &
    QCLUSTER_PID=$!
    echo $QCLUSTER_PID > "$QCLUSTER_PID_FILE"
    echo "    - Worker started with PID $QCLUSTER_PID"
}

# Function to stop qcluster gracefully
stop_qcluster() {
    if [ -f "$QCLUSTER_PID_FILE" ]; then
        local pid=$(cat "$QCLUSTER_PID_FILE")
        if kill -0 $pid 2>/dev/null; then
            echo "[*] Stopping worker (PID $pid)..."
            kill -TERM $pid 2>/dev/null || true
            # Wait up to 30 seconds for graceful shutdown
            local count=0
            while kill -0 $pid 2>/dev/null && [ $count -lt 30 ]; do
                sleep 1
                count=$((count + 1))
            done
            # Force kill if still running
            if kill -0 $pid 2>/dev/null; then
                echo "[!] Worker did not stop gracefully, force killing..."
                kill -9 $pid 2>/dev/null || true
            fi
        fi
        rm -f "$QCLUSTER_PID_FILE"
    fi
}

# Signal handler for restart request (SIGUSR1)
handle_restart() {
    echo ""
    echo "[*] Restart signal received, restarting workers..."
    stop_qcluster
    start_qcluster
    echo "[*] Workers restarted successfully"
}
trap handle_restart SIGUSR1

# Handle graceful shutdown
cleanup() {
    echo ""
    echo "[*] Shutting down..."
    stop_qcluster
    # Kill monitor if running
    if [ -n "$MONITOR_PID" ]; then
        kill $MONITOR_PID 2>/dev/null || true
    fi
    exit 0
}
trap cleanup SIGTERM SIGINT

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
monitor_workers &
MONITOR_PID=$!

# Start gunicorn web server
echo "    - Starting web server on port ${PORT:-8000}..."
echo ""
echo "=========================================="
echo "  PyRunner is ready!"
echo "  Open http://localhost:${PORT:-8000}"
echo "=========================================="
echo ""

exec gunicorn pyrunner.wsgi:application \
    --bind 0.0.0.0:${PORT:-8000} \
    --workers ${GUNICORN_WORKERS:-2} \
    --threads ${GUNICORN_THREADS:-4} \
    --timeout ${GUNICORN_TIMEOUT:-120} \
    --access-logfile - \
    --error-logfile -
