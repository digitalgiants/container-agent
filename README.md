# Container Agent

Automated Podman Compose guardian for a single RHEL host. Monitors every compose project, restarts unhealthy services, attempts lock-file fixes (with backup), emails you only when auto-fix fails, and records incidents on disk.

Designed for **rootless Podman** — the agent runs as **your SSH login user** (the same user who runs `podman compose`). A separate Linux user cannot see your rootless containers.

## What you need before install

| Item | How to get it |
|------|----------------|
| **RAM for Ollama** | 8 GB+ recommended for `qwen2.5:7b-instruct` (runs in a Podman container) |
| **Gmail App Password** | [Google App Passwords](https://myaccount.google.com/apppasswords) (2FA required on the account) |
| **Git access** | SSH to the RHEL box from your dev machine |
| **Compose search root** | Default: `/home/digilabs` (recursive — every git repo underneath) |

Secrets live only in `~/.config/container-agent/secrets.env` on the server — **never committed to git**.

## Remote install (you are not on the RHEL box)

### 1. Push this repo to GitHub/GitLab

On your dev machine (where Cursor runs):

```bash
cd container-agent
git init
git add .
git commit -m "Initial container-agent"
git remote add origin git@github.com:YOU/container-agent.git
git push -u origin main
```

### 2. SSH to the RHEL server and clone

```bash
ssh you@your-rhel-host

git clone git@github.com:YOU/container-agent.git ~/container-agent
cd ~/container-agent
chmod +x install.sh uninstall.sh
./install.sh
```

The installer will:

- `dnf install` python3, msmtp, lsof, podman (sudo once)
- `pip install podman-compose` into `~/.local/bin` (not in RHEL 10 repos — handled by `install.sh`)
- Prompt for compose search root (default `/home/digilabs`), Gmail app password, UI login
- Write config to `~/.config/container-agent/`
- Install a **systemd user timer** (every 5 minutes)
- Start **Ollama** on `127.0.0.1:11434` and the **approval web UI** on `127.0.0.1:8787`

### 3. Verify

```bash
container-agent status
systemctl --user status container-agent.timer
container-agent run          # manual scan
journalctl --user -u container-agent.service -n 30 --no-pager
curl -s http://127.0.0.1:8787/api/pending
```

## Reverse proxy (approval UI)

The UI binds to localhost only. Point your reverse proxy at the host:

```
http://127.0.0.1:8787
```

Example Caddy:

```
container-agent.yourdomain.net {
    reverse_proxy 127.0.0.1:8787
}
```

Approve fixes in the browser: `https://container-agent.yourdomain.net/approve/<incident-id>`

## Human-in-the-loop vs full auto

Edit `~/.config/container-agent/config.yaml`:

```yaml
automation_mode: human_in_loop   # or full_auto
```

- **human_in_loop** — destructive fixes on protected/DB paths require approval (web UI or `container-agent approve <id>`).
- **full_auto** — after backup, applies fixes without approval.

## Email behavior

Email is sent **only when** auto-fix did not restore health. Includes:

- Container / project name
- Issue summary
- `app_version` (when HTTP health returns it)
- Actions taken (“ran X → result Y”)
- Local LLM root cause + recommended commands (when analysis runs)

## Incident history

Append-only log on the server:

```
~/.local/share/container-agent/incidents.jsonl
```

## Commands

```bash
container-agent status
container-agent run
container-agent approve <incident-id>
container-agent pull-llm
./uninstall.sh
```

## Re-run install safely

```bash
cd ~/container-agent
git pull
./install.sh
```

Keeps existing `config.yaml`, `secrets.env`, and `incidents.jsonl`; upgrades venv, systemd units, and web UI.

### Already installed? Update the search path

Edit `~/.config/container-agent/config.yaml`:

```yaml
compose_search_paths:
  - /home/digilabs
```

The agent walks every subdirectory (each git repo) and picks up any `compose.yaml` or `docker-compose.yml`. Preview what it finds:

```bash
find /home/digilabs \( -name compose.yml -o -name compose.yaml -o -name docker-compose.yml -o -name docker-compose.yaml \) 2>/dev/null
container-agent run
```

## Architecture

```
systemd user timer (5 min)
    → python -m agent
        → discover compose files under /home/digilabs (all git repos)
        → health + logs (tail 80)
        → graceful restart (max 5/hour per service)
        → lock-file analysis (lsof, backup, optional delete)
        → Ollama LLM analysis if still broken
        → email if approval required

podman compose
    → container-agent-ollama :11434 (local LLM)
    → container-agent-ui :8787
        → shared volume ~/.local/share/container-agent
        → approve pending fixes
```

## Why not a separate Linux user?

Rootless Podman stores containers per user. Your compose stacks run under your SSH account, so the agent must run as that same user (systemd **user** units, not a separate system account).

## Troubleshooting install

| Problem | Fix |
|---------|-----|
| `podman compose` / compose provider errors | RHEL 10 has no `podman-compose` RPM — run `./install.sh` or `pip install --user podman-compose`, then verify: `podman compose version` |
| UI shows container down but `podman ps` shows it running | Compose provider missing — agent cannot run `podman compose ps`; ensure `~/.local/bin/podman-compose` exists and systemd `PATH` includes it, then re-run `container-agent run` |
| Timer not firing | `sudo loginctl enable-linger $USER` then `systemctl --user enable --now container-agent.timer` |
| Email fails | Test: `echo test \| msmtp drewfert@gmail.com` |
| No LLM analysis | `container-agent status` (ollama up?); `container-agent pull-llm`; check `ollama_model` in `config.yaml` |
| UI container cannot see incidents | Ensure compose volume uses same `DATA_DIR` as config (`~/.local/share/container-agent`) |

## Instant scan trigger (optional systemd path unit)

`POST /api/scan` writes `$DATA_DIR/scan_requested`. To make this fire the agent **immediately** instead of waiting up to 5 minutes, add a systemd path unit alongside the existing timer:

```ini
# ~/.config/systemd/user/container-agent-scan-trigger.path
[Unit]
Description=Fire container-agent scan when web UI requests it

[Path]
PathExists=%h/.local/share/container-agent/scan_requested
Unit=container-agent.service

[Install]
WantedBy=default.target
```

```bash
systemctl --user enable --now container-agent-scan-trigger.path
```

---

## Optional future enhancements (Tier 3)

The items below are **not implemented**. They require mounting the Podman socket into the UI container or a separate privileged helper process, which adds complexity and a larger attack surface. They are recorded here for future consideration.

### Mount the Podman socket into the UI container

Add to `compose/docker-compose.yml` under `container-agent-ui`:

```yaml
volumes:
  - /run/user/1000/podman/podman.sock:/run/podman/podman.sock:ro,z
```

Replace `1000` with your actual UID (`id -u`). With the socket mounted, the web process can call `podman` directly, enabling the endpoints below.

### `POST /api/force-restart/{project}/{service}`

Runs `podman compose stop` then `podman compose start` — harder on the service than a graceful restart. Recommended safeguard: require a confirmation token in the request body so double-clicks can't trigger it.

```json
POST /api/force-restart/myproject/webapp
{ "confirm": "force-restart" }
```

### `GET /api/inspect/{project}/{service}`

Returns the full `podman inspect` JSON for the running container. Read-only but exposes mount paths, environment variables, and network config — keep strictly behind the login wall and consider redacting `Env` fields that may contain secrets.

### Considerations before enabling Tier 3

- The Podman socket grants equivalent root-level access to all rootless containers owned by the user. Even a read-only mount (`ro`) allows listing and inspecting all containers.
- All Tier 3 endpoints should be rate-limited and audit-logged.
- Consider adding a separate `admin` role (vs read-only `viewer`) before exposing force-restart or inspect.
