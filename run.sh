#!/usr/bin/env bash
# Automated deployment helper for the LinkedIn scraping service on Raspberry Pi.
# - Installs system packages, Python venv, and project dependencies.
# - Installs and configures Cloudflare Tunnel (cloudflared) named "linkedin-scrapping".
# - Registers systemd units for the Flask/Gunicorn app and for the Cloudflare tunnel.
#
# The script is idempotent and may be re-run after updating configuration.

set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEFAULT_SERVICE_NAME="linkedin-scrapping"
APP_SERVICE_NAME="${APP_SERVICE_NAME:-${DEFAULT_SERVICE_NAME}}"
CLOUDFLARE_TUNNEL_NAME="${CLOUDFLARE_TUNNEL_NAME:-linkedin-scrapping}"
APP_USER="${APP_USER:-${SUDO_USER:-$USER}}"
APP_GROUP="${APP_GROUP:-$(id -gn "$APP_USER")}"
PYTHON_BIN="${PYTHON_BIN:-python3}"
VENV_DIR="${VENV_DIR:-$PROJECT_DIR/.venv}"
ENV_FILE="${ENV_FILE:-$PROJECT_DIR/.env}"
LOG_DIR="${LOG_DIR:-$PROJECT_DIR/logs}"
SYSTEMD_DIR="/etc/systemd/system"
CLOUDFLARE_BASE_BIN="${CLOUDFLARE_BIN:-}"
CF_CONFIG_DIR="${CF_CONFIG_DIR:-/home/$APP_USER/.cloudflared}"
CLOUDFLARE_HOSTNAME="${CLOUDFLARE_HOSTNAME:-}"
DEFAULT_PORT="5000"
APP_PORT="${APP_PORT:-}"

APT_PACKAGES=(
  python3 python3-venv python3-pip python3-dev build-essential
  curl unzip jq ca-certificates
  libglib2.0-0 libnss3 libatk1.0-0 libatk-bridge2.0-0 libdrm2 libxkbcommon0
  libxcomposite1 libxdamage1 libxfixes3 libxrandr2 libgbm1 libpango-1.0-0
  libcups2 libxshmfence1 libasound2 libxtst6 libgtk-3-0
)

declare -A PACKAGE_ALIASES=(
  [libglib2.0-0]="libglib2.0-0t64"
  [libatk1.0-0]="libatk1.0-0t64"
  [libatk-bridge2.0-0]="libatk-bridge2.0-0t64"
  [libcups2]="libcups2t64"
  [libgtk-3-0]="libgtk-3-0t64"
  [libasound2]="libasound2t64"
)

RESOLVED_APT_PACKAGES=()

log() {
  echo "[run.sh] $*"
}

package_available() {
  local pkg="$1"
  if dpkg -s "$pkg" >/dev/null 2>&1; then
    return 0
  fi
  if apt-cache show "$pkg" >/dev/null 2>&1; then
    return 0
  fi
  local candidate
  candidate="$(apt-cache policy "$pkg" 2>/dev/null | awk '/Candidate:/ {print $2}' | head -n1)"
  if [[ -n "$candidate" && "$candidate" != "(none)" ]]; then
    return 0
  fi
  return 1
}

resolve_package_name() {
  local pkg="$1"
  local alt="${PACKAGE_ALIASES[$pkg]:-}"

  # Prefer architecture-aware alias when defined.
  if [[ -n "$alt" ]] && package_available "$alt"; then
    printf '%s\n' "$alt"
    return 0
  fi

  if package_available "$pkg"; then
    printf '%s\n' "$pkg"
    return 0
  fi

  # If the alias exists but apt-cache cannot find it yet, surface the alias
  # so apt-get tries it anyway (resolves virtual packages such as libasound2).
  if [[ -n "$alt" ]]; then
    printf '%s\n' "$alt"
    return 0
  fi

  return 1
}

resolve_package_list() {
  RESOLVED_APT_PACKAGES=()
  declare -A seen=()
  for pkg in "${APT_PACKAGES[@]}"; do
    local resolved_pkg
    if resolved_pkg="$(resolve_package_name "$pkg")"; then
      if [[ -z "${seen[$resolved_pkg]+x}" ]]; then
        if [[ "$resolved_pkg" != "$pkg" ]]; then
          log "Using fallback apt package '$resolved_pkg' for '$pkg'"
        fi
        RESOLVED_APT_PACKAGES+=("$resolved_pkg")
        seen["$resolved_pkg"]=1
      fi
    else
      echo "Warning: could not find apt package '$pkg' or any defined fallback." >&2
    fi
  done
  if [[ "${#RESOLVED_APT_PACKAGES[@]}" -eq 0 ]]; then
    echo "Error: no apt packages resolved for installation." >&2
    exit 1
  fi
}

require_root() {
  if [[ "$(id -u)" -ne 0 ]]; then
    echo "Error: run.sh must be executed with sudo/root privileges." >&2
    exit 1
  fi
}

ensure_user_exists() {
  if ! id "$APP_USER" >/dev/null 2>&1; then
    echo "Error: target user '$APP_USER' does not exist." >&2
    exit 1
  fi
}

ensure_env_file() {
  if [[ ! -f "$ENV_FILE" ]]; then
    echo "Warning: $ENV_FILE is missing. Create it before starting the service." >&2
  fi
}

detect_app_port() {
  if [[ -n "$APP_PORT" ]]; then
    return
  fi
  if [[ -f "$ENV_FILE" ]]; then
    local env_port
    env_port="$(grep -E '^PORT=' "$ENV_FILE" | tail -n1 | cut -d'=' -f2- || true)"
    if [[ -n "$env_port" ]]; then
      APP_PORT="$env_port"
      return
    fi
  fi
  APP_PORT="$DEFAULT_PORT"
}

install_packages() {
  log "Updating apt metadata and installing required system packages..."
  apt-get update -y
  resolve_package_list
  DEBIAN_FRONTEND=noninteractive apt-get install -y "${RESOLVED_APT_PACKAGES[@]}"
}

ensure_python_venv() {
  if [[ ! -d "$VENV_DIR" ]]; then
    log "Creating Python virtual environment at $VENV_DIR ..."
    sudo -u "$APP_USER" -H "$PYTHON_BIN" -m venv "$VENV_DIR"
  fi

  log "Installing Python dependencies inside the virtual environment..."
  sudo -u "$APP_USER" -H "$VENV_DIR/bin/pip" install --upgrade pip wheel
  sudo -u "$APP_USER" -H "$VENV_DIR/bin/pip" install -r "$PROJECT_DIR/requirements.txt"

  log "Installing Playwright Chromium browser (this may take a while)..."
  sudo -u "$APP_USER" -H "$VENV_DIR/bin/playwright" install chromium
}

detect_cloudflared_arch() {
  local arch
  arch="$(uname -m)"
  case "$arch" in
    aarch64|arm64) echo "arm64" ;;
    armv7l|armhf) echo "arm" ;;
    x86_64|amd64) echo "amd64" ;;
    *)
      echo "Error: unsupported architecture '$arch' for cloudflared installer." >&2
      exit 1
      ;;
  esac
}

install_cloudflared() {
  if command -v cloudflared >/dev/null 2>&1; then
    CLOUDFLARE_BASE_BIN="$(command -v cloudflared)"
    log "cloudflared already installed at $CLOUDFLARE_BASE_BIN"
    return
  fi

  log "Installing cloudflared from the Cloudflare release channel..."
  local pkg_arch
  pkg_arch="$(detect_cloudflared_arch)"
  local tmp_pkg="/tmp/cloudflared.deb"
  curl -fsSL "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-${pkg_arch}.deb" -o "$tmp_pkg"
  dpkg -i "$tmp_pkg" || apt-get install -f -y
  rm -f "$tmp_pkg"
  CLOUDFLARE_BASE_BIN="$(command -v cloudflared)"
  log "cloudflared installed to $CLOUDFLARE_BASE_BIN"
}

ensure_cloudflared_login() {
  mkdir -p "$CF_CONFIG_DIR"
  chown -R "$APP_USER:$APP_GROUP" "$CF_CONFIG_DIR"
  local cert_file="$CF_CONFIG_DIR/cert.pem"
  if [[ ! -f "$cert_file" ]]; then
    cat >&2 <<EOF
Cloudflared is installed but not authenticated.
Please run the following command as $APP_USER to authorize the tunnel, then re-run run.sh:
  sudo -u $APP_USER -H cloudflared tunnel login
EOF
    exit 1
  fi
}

ensure_cloudflare_tunnel() {
  ensure_cloudflared_login

  detect_app_port

  if ! sudo -u "$APP_USER" -H cloudflared tunnel list 2>/dev/null | grep -q "$CLOUDFLARE_TUNNEL_NAME"; then
    log "Creating Cloudflare tunnel '$CLOUDFLARE_TUNNEL_NAME'..."
    sudo -u "$APP_USER" -H cloudflared tunnel create "$CLOUDFLARE_TUNNEL_NAME"
  else
    log "Cloudflare tunnel '$CLOUDFLARE_TUNNEL_NAME' already exists."
  fi

  local tunnel_id
  tunnel_id="$(sudo -u "$APP_USER" -H cloudflared tunnel list --output json \
    | jq -r ".[] | select(.name==\"$CLOUDFLARE_TUNNEL_NAME\") | .id")"
  if [[ -z "$tunnel_id" || "$tunnel_id" == "null" ]]; then
    echo "Error: unable to determine Tunnel ID for '$CLOUDFLARE_TUNNEL_NAME'." >&2
    exit 1
  fi

  if [[ -z "$CLOUDFLARE_HOSTNAME" ]]; then
    read -rp "Enter the Cloudflare hostname (e.g. scrape.example.com) to expose this app: " CLOUDFLARE_HOSTNAME
    if [[ -z "$CLOUDFLARE_HOSTNAME" ]]; then
      echo "Error: hostname is required to configure the tunnel." >&2
      exit 1
    fi
  fi

  local tunnel_config="$CF_CONFIG_DIR/${CLOUDFLARE_TUNNEL_NAME}.yml"
  local credentials_file="$CF_CONFIG_DIR/${tunnel_id}.json"
  cat > "$tunnel_config" <<EOF
tunnel: ${tunnel_id}
credentials-file: ${credentials_file}

ingress:
  - hostname: ${CLOUDFLARE_HOSTNAME}
    service: http://localhost:${APP_PORT}
  - service: http_status:404
EOF
  chown "$APP_USER:$APP_GROUP" "$tunnel_config"
  log "Cloudflare tunnel config written to $tunnel_config"

  if ! sudo -u "$APP_USER" -H cloudflared tunnel route dns "$CLOUDFLARE_TUNNEL_NAME" "$CLOUDFLARE_HOSTNAME"; then
    cat >&2 <<EOF
Warning: cloudflared could not automatically create the DNS route.
Ensure that '$CLOUDFLARE_HOSTNAME' is managed in your Cloudflare account and run:
  sudo -u $APP_USER -H cloudflared tunnel route dns $CLOUDFLARE_TUNNEL_NAME $CLOUDFLARE_HOSTNAME
EOF
  fi
}

write_systemd_units() {
  mkdir -p "$LOG_DIR"
  chown "$APP_USER:$APP_GROUP" "$LOG_DIR"

  detect_app_port

  local app_service_file="$SYSTEMD_DIR/${APP_SERVICE_NAME}.service"
  cat > "$app_service_file" <<EOF
[Unit]
Description=LinkedIn Scraping Flask Service
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=${APP_USER}
Group=${APP_GROUP}
WorkingDirectory=${PROJECT_DIR}
EnvironmentFile=-${ENV_FILE}
Environment=PORT=${APP_PORT}
ExecStart=${VENV_DIR}/bin/gunicorn -b 0.0.0.0:\${PORT} --threads 4 --timeout 360 final_scrapping_script:app
Restart=on-failure
RestartSec=5
StandardOutput=journal
StandardError=inherit

[Install]
WantedBy=multi-user.target
EOF

  local cloudflared_bin
  cloudflared_bin="${CLOUDFLARE_BASE_BIN:-$(command -v cloudflared)}"
  local cloudflare_service_file="$SYSTEMD_DIR/${APP_SERVICE_NAME}-cloudflared.service"
  cat > "$cloudflare_service_file" <<EOF
[Unit]
Description=cloudflared tunnel (${CLOUDFLARE_TUNNEL_NAME})
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=${APP_USER}
Group=${APP_GROUP}
WorkingDirectory=${CF_CONFIG_DIR}
Environment=HOME=/home/${APP_USER}
ExecStart=${cloudflared_bin} --config ${CF_CONFIG_DIR}/${CLOUDFLARE_TUNNEL_NAME}.yml --no-autoupdate tunnel run ${CLOUDFLARE_TUNNEL_NAME}
Restart=on-failure
RestartSec=5
StandardOutput=journal
StandardError=inherit

[Install]
WantedBy=multi-user.target
EOF

  log "Systemd unit files created: $app_service_file and $cloudflare_service_file"
}

enable_services() {
  systemctl daemon-reload
  systemctl enable --now "${APP_SERVICE_NAME}.service"
  systemctl enable --now "${APP_SERVICE_NAME}-cloudflared.service"
  log "Services enabled and started. Use 'systemctl status ${APP_SERVICE_NAME}' for logs."
}

main() {
  require_root
  ensure_user_exists
  ensure_env_file
  detect_app_port
  install_packages
  ensure_python_venv
  install_cloudflared
  ensure_cloudflare_tunnel
  write_systemd_units
  enable_services
  log "Deployment complete."
}

main "$@"
