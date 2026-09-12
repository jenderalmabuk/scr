# Nexus Bot — multi-stage, non-root
FROM python:3.12-slim AS builder

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc libpq-dev && \
    rm -rf /var/lib/apt/lists/*

COPY bots/requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt

FROM python:3.12-slim

RUN groupadd -r nexus && useradd -r -g nexus -d /app -s /usr/sbin/nologin nexus

WORKDIR /app

COPY --from=builder /install /usr/local
# Copy fusionnew engine code (adversarial bull/bear only)
COPY --chown=nexus:nexus fusionnew/ ./fusionnew/
# Copy gateway for execution routing
COPY --chown=nexus:nexus gateway/ ./gateway/
# Copy execution layer (BinanceTestnetTrader)
COPY --chown=nexus:nexus execution/ ./execution/
# Copy notifications (Telegram trade alerts)
COPY --chown=nexus:nexus notifications/ ./notifications/
# Copy signal_copy config (ExecutionConfig, RiskManager)
COPY --chown=nexus:nexus signal_copy/ ./signal_copy/
# Copy risk engine (RiskManager) + shared utils (persistence_paths for journal).
# Required so this image can ALSO run the Execution Gateway as a container.
COPY --chown=nexus:nexus risk/ ./risk/
COPY --chown=nexus:nexus utils/ ./utils/
# Copy nexus bot files
COPY --chown=nexus:nexus bots/ ./bots/
COPY --chown=nexus:nexus analyst_bot/ ./analyst_bot/

# Normalize read perms: host sources may be 0600 (owner-only). Baked in, those
# become readable only by uid 999, but the gateway service runs as uid 1000 (to
# write the host-owned journal/). Make all code world-readable + dirs traversable
# so the image runs under ANY uid regardless of host file modes (portable clone).
RUN chmod -R a+rX /app

ENV PYTHONPATH=/app:/app/fusionnew
ENV NEXUS_API_URL=http://fastapi:8000

USER nexus

CMD ["python", "-u", "bots/run_bot.py"]
