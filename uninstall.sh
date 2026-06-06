#!/usr/bin/env bash
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONFIG_DIR="${HOME}/.config/container-agent"
DATA_DIR="${HOME}/.local/share/container-agent"
BIN_DIR="${HOME}/.local/bin"
SYSTEMD_USER_DIR="${HOME}/.config/systemd/user"

log() { printf '==> %s\n' "$*"; }

stop_services() {
  systemctl --user disable --now container-agent.timer 2>/dev/null || true
  systemctl --user stop container-agent.service 2>/dev/null || true
  rm -f "${SYSTEMD_USER_DIR}/container-agent.service" "${SYSTEMD_USER_DIR}/container-agent.timer"
  systemctl --user daemon-reload
  log "Removed systemd user timer"
}

stop_web_ui() {
  podman compose -f "${REPO_DIR}/compose/docker-compose.yml" down 2>/dev/null || true
  log "Stopped container-agent-ui"
}

remove_cli() {
  rm -f "${BIN_DIR}/container-agent"
  log "Removed CLI wrapper"
}

prompt_purge_data() {
  local answer=""
  read -r -p "Delete incident history and config in ${DATA_DIR} and ${CONFIG_DIR}? [y/N] " answer
  if [[ "$answer" =~ ^[Yy]$ ]]; then
    rm -rf "$DATA_DIR" "$CONFIG_DIR" "${HOME}/.msmtprc"
    log "Removed local config and data"
  else
    log "Kept config and data"
  fi
}

main() {
  stop_services
  stop_web_ui
  remove_cli
  prompt_purge_data
  log "Uninstall complete. Repo directory was not deleted: ${REPO_DIR}"
}

main "$@"
