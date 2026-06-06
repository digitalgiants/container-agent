# Container Agent

Automated Podman Compose guardian for a single RHEL host. Monitors every compose project, restarts unhealthy services, attempts lock-file fixes (with backup), emails you only when auto-fix fails, and records incidents on disk.

Designed for **rootless Podman** — the agent runs as **your SSH login user** (the same user who runs `podman compose`). A separate Linux user cannot see your rootless containers.

## What you need before install

| Item | How to get it |
|------|----------------|
| **Gemini API key** | [Google AI Studio](https://aistudio.google.com/apikey) → Create API key |
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
- Prompt for compose search root (default `/home/digilabs`), Gemini key, Gmail app password
- Write config to `~/.config/container-agent/`
- Install a **systemd user timer** (every 5 minutes)
- Start the **approval web UI** container on `127.0.0.1:8787`

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
- Gemini root cause + recommended commands

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
        → Gemini analysis if still broken
        → email if unresolved

podman compose → container-agent-ui :8787
    → shared volume ~/.local/share/container-agent
    → approve pending fixes
```

## Why not a separate Linux user?

Rootless Podman stores containers per user. Your compose stacks run under your SSH account, so the agent must run as that same user (systemd **user** units, not a separate system account).

## Troubleshooting install

| Problem | Fix |
|---------|-----|
| `podman compose` not found | `sudo dnf install podman-compose` or Podman 4+ compose plugin |
| Timer not firing | `sudo loginctl enable-linger $USER` then `systemctl --user enable --now container-agent.timer` |
| Email fails | Test: `echo test \| msmtp drewfert@gmail.com` |
| No Gemini analysis | Check `GEMINI_API_KEY` in `~/.config/container-agent/secrets.env` |
| UI container cannot see incidents | Ensure compose volume uses same `DATA_DIR` as config (`~/.local/share/container-agent`) |
