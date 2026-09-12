# Archive contents

GitHub repo contains code only, no secrets.

Private archive contains:
- `.env`, `docker/.env`
- Telegram sessions: `*.session`, `runtime/state/signal_copy_session.session`
- `journal/` trade history/open positions
- `runtime/state/` lifecycle/outcomes/pending state
- `backups/nexus_db.dump` TimescaleDB dump to avoid warmup

Never commit private archive to GitHub.
