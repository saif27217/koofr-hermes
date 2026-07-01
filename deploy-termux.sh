#!/data/data/com.termux/files/usr/bin/env bash
set -euo pipefail

# Koofr Hermes — Termux deploy script
# Idempotent. Run it fresh or re-run to update.

APP_DIR="$HOME/koofr-hermes"
VENV_DIR="$APP_DIR/.venv"
CONFIG_FILE="$APP_DIR/.env"
DATA_DIR="$HOME/.koofr-hermes"

echo "=> Koofr Hermes - Termux Deploy"

# 1. System packages
echo "=> Checking system packages..."
pkg update -y 2>/dev/null | tail -1 || true
pkg install -y python python-pip git 2>/dev/null | tail -1 || true

if ! command -v uv &>/dev/null; then
    echo "=> Installing uv..."
    pip install uv
fi

# 2. Clone or update
if [ -d "$APP_DIR/.git" ]; then
    echo "=> Updating existing repo..."
    cd "$APP_DIR" && git pull --ff-only
else
    echo "=> Cloning repo..."
    git clone https://github.com/saif27217/koofr-hermes.git "$APP_DIR"
    cd "$APP_DIR"
fi

# 3. Venv
if [ ! -f "$VENV_DIR/bin/python" ]; then
    echo "=> Creating virtual environment..."
    uv venv "$VENV_DIR"
fi

# 4. Dependencies
echo "=> Installing Python dependencies..."
uv pip install -r requirements.txt

# 5. Config
if [ ! -f "$CONFIG_FILE" ]; then
    echo ""
    echo "=== FIRST-TIME SETUP ==="
    echo "1. Generate an app password at:"
    echo "   https://app.koofr.net/app/admin/preferences/password"
    echo ""
    echo "2. Enter your Koofr email and the app password:"
    read -rp "Koofr email: " email
    read -rsp "Koofr app password: " password
    echo ""
    cat > "$CONFIG_FILE" << EOF
KOOFR_EMAIL=$email
KOOFR_PASSWORD=$password
# KOOFR_PORT=5000
# KOOFR_HOST=127.0.0.1
# KOOFR_REFRESH_INTERVAL=3600
EOF
    chmod 600 "$CONFIG_FILE"
    echo "=> Config saved to $CONFIG_FILE"
fi

# 6. Data dir
mkdir -p "$DATA_DIR"

# 7. Run script
cat > "$APP_DIR/run.sh" << 'SCRIPT'
#!/data/data/com.termux/files/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
if [ -f .env ]; then
    set -a; source .env; set +a
fi
if [ -z "${KOOFR_EMAIL:-}" ] || [ -z "${KOOFR_PASSWORD:-}" ]; then
    echo "ERROR: KOOFR_EMAIL and KOOFR_PASSWORD must be set"
    exit 1
fi
exec .venv/bin/python server.py
SCRIPT
chmod +x "$APP_DIR/run.sh"

echo ""
echo "=== DEPLOY COMPLETE ==="
echo ""
echo "Start the server:"
echo "  bash ~/koofr-hermes/run.sh"
echo ""
echo "Then tunnel from VPS:"
echo "  ssh -L 5000:localhost:5000 termux"
echo ""
echo "Open browser: http://localhost:5000"
echo ""
