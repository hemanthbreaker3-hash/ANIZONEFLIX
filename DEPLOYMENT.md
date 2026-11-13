# AniZoneFlix deployment (FAST — no lock delays)

## Ubuntu / Debian VPS (recommended)

```bash
cd ANIZONEFLIX-main
bash deploy-now.sh
# or
bash deploy.sh
```

Run as root (`sudo bash deploy.sh`) for full 24/7 systemd service.

### Required `.env` keys
Create/edit `.env` before first run (or the script will stop and ask):

- `API_ID`
- `API_HASH`
- `BOT_TOKEN`
- `MONGO_URI`

Optional: `ADMIN_IDS`, `BASE_URL`, `PORT`, `SECRET_KEY` (auto-generated if missing), etc.

The script preserves existing `.env` values, creates `SECRET_KEY` if needed, installs deps in `venv`, validates, and installs a **systemd** service `anizoneflix` with `Restart=always` so it runs 24/7 and on reboot.

### Service commands
```bash
systemctl status anizoneflix
journalctl -u anizoneflix -f
systemctl restart anizoneflix
systemctl stop anizoneflix
```

## FAST / no-wait behaviour
- Default: **zero wait** on APT locks (`APT_LOCK_TIMEOUT=0`).
- If packages fail because of a stuck lock, re-run with force unlock:

```bash
sudo FORCE_APT_UNLOCK=1 bash deploy.sh
```

- Optional short wait: `APT_LOCK_TIMEOUT=30 bash deploy.sh`

## Termux
```bash
bash deploy.sh
```
(no sudo; runs in foreground or screen if available)

## Docker (alternative)
```bash
docker compose up -d --build
```

## Notes
- `deploy-now.sh`, `START.sh`, `deploy.vps` all call the same `deploy.sh`.
- Works on Ubuntu/Debian, Alpine, RHEL/Rocky, Termux.
