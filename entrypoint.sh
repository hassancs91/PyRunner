#!/bin/bash
set -e

echo "=========================================="
echo "  PyRunner - Starting up..."
echo "=========================================="

# Auto-generate ENCRYPTION_KEY if not provided
if [ -z "$ENCRYPTION_KEY" ]; then
    echo ""
    echo "[!] ENCRYPTION_KEY not set - generating a new one..."
    export ENCRYPTION_KEY=$(python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())")
    echo ""
    echo "=================================================="
    echo "  Generated ENCRYPTION_KEY:"
    echo "  $ENCRYPTION_KEY"
    echo "=================================================="
    echo ""
    echo "  *** IMPORTANT: Save this key! ***"
    echo "  Set ENCRYPTION_KEY env var to persist across restarts"
    echo ""
fi

# Auto-generate SECRET_KEY if not provided
if [ -z "$SECRET_KEY" ]; then
    export SECRET_KEY=$(python -c "import secrets; print(secrets.token_urlsafe(50))")
fi

# Run setup (migrations + default environment)
echo "[*] Running setup..."
python manage.py setup

echo ""
echo "[*] Starting services..."

# Start qcluster worker in background
echo "    - Starting django-q2 worker..."
python manage.py qcluster &
QCLUSTER_PID=$!

# Handle graceful shutdown
cleanup() {
    echo ""
    echo "[*] Shutting down..."
    kill $QCLUSTER_PID 2>/dev/null || true
    wait $QCLUSTER_PID 2>/dev/null || true
    exit 0
}
trap cleanup SIGTERM SIGINT

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
