#!/usr/bin/env bash
# Container Agent installer for RHEL 10 + rootless Podman.
# Run on the target host as your normal SSH user (the one who runs podman compose).
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONFIG_DIR="${HOME}/.config/container-agent"
DATA_DIR="${HOME}/.local/share/container-agent"
BIN_DIR="${HOME}/.local/bin"
VENV_DIR="${REPO_DIR}/.venv"
SYSTEMD_USER_DIR="${HOME}/.config/systemd/user"

log() { printf '==> %s\n' "$*"; }
die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }

prompt_secret() {
  local var_name="$1" prompt_text="$2" value=""
  read -r -s -p "${prompt_text}: " value
  echo
  printf -v "$var_name" '%s' "$value"
}

prompt_value() {
  local var_name="$1" prompt_text="$2" default_value="${3:-}"
  local value=""
  if [[ -n "$default_value" ]]; then
    read -r -p "${prompt_text} [${default_value}]: " value
    value="${value:-$default_value}"
  else
    read -r -p "${prompt_text}: " value
  fi
  printf -v "$var_name" '%s' "$value"
}

require_cmd() {
  command -v "$1" >/dev/null 2>&1 || die "Missing required command: $1"
}

install_packages() {
  if command -v dnf >/dev/null 2>&1; then
    log "Installing system packages (sudo may prompt)..."
    sudo dnf install -y python3 python3-pip msmtp lsof podman || true
  else
    log "dnf not found; ensure python3, msmtp, lsof, and podman are installed."
  fi
}

setup_venv() {
  log "Python virtualenv at ${VENV_DIR}"
  if [[ ! -d "$VENV_DIR" ]]; then
    python3 -m venv "$VENV_DIR"
  fi
  # shellcheck disable=SC1091
  source "${VENV_DIR}/bin/activate"
  pip install --upgrade pip
  pip install -r "${REPO_DIR}/requirements.txt"
}

write_config() {
  local compose_path="$1"
  mkdir -p "$CONFIG_DIR" "$DATA_DIR"/{pending,approvals,backups,state}
  if [[ ! -f "${CONFIG_DIR}/config.yaml" ]]; then
    sed "s|/home/digilabs|${compose_path}|g" \
      "${REPO_DIR}/config/config.yaml.example" > "${CONFIG_DIR}/config.yaml"
    log "Wrote ${CONFIG_DIR}/config.yaml"
  else
    log "Keeping existing ${CONFIG_DIR}/config.yaml"
  fi
}

write_secrets() {
  local gemini_key smtp_password smtp_user alert_email
  if [[ -f "${CONFIG_DIR}/secrets.env" ]]; then
    log "Keeping existing ${CONFIG_DIR}/secrets.env"
    return
  fi

  echo
  log "Secrets are stored only on this server (${CONFIG_DIR}/secrets.env), never in git."
  echo "Get a Gemini API key: https://aistudio.google.com/apikey"
  echo "Get a Gmail App Password: https://myaccount.google.com/apppasswords"
  echo

  prompt_secret gemini_key "Gemini API key"
  prompt_value smtp_user "SMTP user (Gmail address)" "drewfert@gmail.com"
  prompt_secret smtp_password "Gmail App Password (not your login password)"
  prompt_value alert_email "Alert email recipient" "$smtp_user"

  cat > "${CONFIG_DIR}/secrets.env" <<EOF
GEMINI_API_KEY=${gemini_key}
SMTP_HOST=smtp.gmail.com
SMTP_PORT=587
SMTP_USER=${smtp_user}
SMTP_PASSWORD=${smtp_password}
ALERT_EMAIL=${alert_email}
EOF
  chmod 600 "${CONFIG_DIR}/secrets.env"
  log "Wrote ${CONFIG_DIR}/secrets.env (chmod 600)"
}

write_msmtp() {
  local secrets_file="${CONFIG_DIR}/secrets.env"
  # shellcheck disable=SC1090
  source "$secrets_file"
  local msmtp_file="${HOME}/.msmtprc"
  cat > "$msmtp_file" <<EOF
defaults
auth           on
tls            on
tls_trust_file /etc/ssl/certs/ca-bundle.crt
logfile        ${DATA_DIR}/msmtp.log

account        gmail
host           ${SMTP_HOST}
port           ${SMTP_PORT}
from           ${SMTP_USER}
user           ${SMTP_USER}
password       ${SMTP_PASSWORD}

account default : gmail
EOF
  chmod 600 "$msmtp_file"
  log "Wrote ${msmtp_file}"
}

install_cli() {
  mkdir -p "$BIN_DIR"
  cat > "${BIN_DIR}/container-agent" <<EOF
#!/usr/bin/env bash
export PYTHONPATH="${REPO_DIR}"
exec "${VENV_DIR}/bin/python" -m agent.cli "\$@"
EOF
  chmod +x "${BIN_DIR}/container-agent"
  log "Installed CLI: ${BIN_DIR}/container-agent"
}

install_systemd_user_units() {
  mkdir -p "$SYSTEMD_USER_DIR"
  sed "s|%REPO_DIR%|${REPO_DIR}|g" "${REPO_DIR}/systemd/container-agent.service" \
    > "${SYSTEMD_USER_DIR}/container-agent.service"
  cp "${REPO_DIR}/systemd/container-agent.timer" "${SYSTEMD_USER_DIR}/container-agent.timer"
  log "Enabling lingering for ${USER} (allows timers when not logged in)..."
  sudo loginctl enable-linger "$USER" || true
  systemctl --user daemon-reload
  systemctl --user enable --now container-agent.timer
  log "Timer active: systemctl --user status container-agent.timer"
}

write_ui_auth() {
  "${VENV_DIR}/bin/python" -c "from agent.ui_auth import ensure_session_secret; from pathlib import Path; ensure_session_secret(Path('${CONFIG_DIR}/secrets.env'))"
  if [[ -f "${CONFIG_DIR}/ui-auth.json" ]]; then
    log "Keeping existing ${CONFIG_DIR}/ui-auth.json (change: container-agent set-ui-password)"
    return
  fi
  echo
  log "Web UI login — password stored as bcrypt hash only (${CONFIG_DIR}/ui-auth.json)"
  local ui_user ui_pass ui_pass2
  prompt_value ui_user "UI username"
  prompt_secret ui_pass "UI password"
  prompt_secret ui_pass2 "Confirm UI password"
  if [[ "$ui_pass" != "$ui_pass2" ]]; then
    die "UI passwords do not match"
  fi
  CONFIG_DIR="${CONFIG_DIR}" UI_USER="${ui_user}" UI_PASS="${ui_pass}" \
    "${VENV_DIR}/bin/python" -c "import os; from pathlib import Path; from agent.ui_auth import write_ui_auth; write_ui_auth(Path(os.environ['CONFIG_DIR']), os.environ['UI_USER'], os.environ['UI_PASS'])"
  chmod 600 "${CONFIG_DIR}/ui-auth.json"
  log "Wrote ${CONFIG_DIR}/ui-auth.json"
}

write_compose_env() {
  local data_dir
  data_dir="$("${VENV_DIR}/bin/python" -c "from agent.config import load_config; print(load_config()['data_dir'])")"
  cat > "${REPO_DIR}/compose/.env" <<EOF
CONTAINER_AGENT_DATA_DIR=${data_dir}
CONTAINER_AGENT_CONFIG_DIR=${CONFIG_DIR}
EOF
  log "Wrote compose/.env → data=${data_dir} config=${CONFIG_DIR}"
}

start_web_ui() {
  write_compose_env
  if [[ ! -f "${REPO_DIR}/web/static/index.html" ]]; then
    die "Missing ${REPO_DIR}/web/static/index.html — run git pull for the full UI"
  fi
  log "Building and starting approval UI container..."
  podman compose -f "${REPO_DIR}/compose/docker-compose.yml" build --no-cache
  podman compose -f "${REPO_DIR}/compose/docker-compose.yml" up -d --force-recreate
  sleep 2
  if ! curl -fsS "http://127.0.0.1:8787/health" >/dev/null; then
    die "Web UI failed health check — run: podman logs container-agent-ui"
  fi
  log "Web UI on http://127.0.0.1:8787 (reverse-proxy this port)"
}

main() {
  require_cmd podman
  require_cmd python3

  local default_compose="/home/digilabs"
  prompt_value COMPOSE_PATH "Root directory to search for compose files (recursive)" "$default_compose"

  install_packages
  setup_venv
  write_config "$COMPOSE_PATH"
  write_secrets
  write_ui_auth
  write_msmtp
  install_cli
  install_systemd_user_units
  start_web_ui

  echo
  log "Install complete."
  echo "  Config:  ${CONFIG_DIR}/config.yaml"
  echo "  Secrets: ${CONFIG_DIR}/secrets.env"
  echo "  Data:    ${DATA_DIR}/incidents.jsonl"
  echo "  Status:  container-agent status"
  echo "  UI login: container-agent set-ui-password"
  echo "  Approve: container-agent approve <incident-id>  OR  web UI /approve/<id>?token=..."
  echo "  Logs:    journalctl --user -u container-agent.service -n 50 --no-pager"
  echo
  echo "Re-run this script safely to upgrade deps or repair systemd/web UI."
}

main "$@"
