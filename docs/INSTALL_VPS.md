# SignalCopy Real VPS install

## 1. Install Docker
```bash
sudo apt update
sudo apt install -y git docker.io docker-compose-plugin postgresql-client
sudo usermod -aG docker $USER
```
Logout/login ulang.

## 2. Clone repo
```bash
git clone https://github.com/jenderalmabuk/signalcopyreal.git
cd signalcopyreal
```

## 3. Extract private archive
Upload `signalcopyreal_private_*.tar.gz` ke folder repo, lalu:
```bash
tar -xzf signalcopyreal_private_*.tar.gz
```
Archive mengisi `.env`, `docker/.env`, `journal/`, `runtime/state/`, Telegram session, dan `backups/nexus_db.dump`.

## 4. Start DB then restore
```bash
docker compose -f docker-compose.full.yml up -d timescaledb
sleep 30
docker cp backups/nexus_db.dump nexus_timescaledb:/tmp/nexus_db.dump
docker exec nexus_timescaledb bash -lc 'psql -U nexus -d nexus -c "SELECT timescaledb_pre_restore();" || true'
docker exec nexus_timescaledb bash -lc 'pg_restore -U nexus -d nexus --no-owner --clean --if-exists /tmp/nexus_db.dump || true'
docker exec nexus_timescaledb bash -lc 'psql -U nexus -d nexus -c "SELECT timescaledb_post_restore();" || true'
```

## 5. Start minimal clean stack
```bash
docker compose -f docker-compose.full.yml up -d binance_collector bybit_collector fastapi nexus_scanner gateway signal_copy
```

## 6. Verify
```bash
docker ps
docker logs --tail 50 nexus_signal_copy
docker logs --tail 50 nexus_gateway
docker exec nexus_signal_copy env | grep SIGNAL_COPY_DRY_RUN
docker exec nexus_gateway env | grep GATEWAY_TRADER_MODE
```
Expected: `SIGNAL_COPY_DRY_RUN=false`, `GATEWAY_TRADER_MODE=bybit`, `LIVE mode: Execution Gateway active`.
