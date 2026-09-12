# Signal Copy Pipeline — Dockerfile
FROM python:3.12-slim AS builder

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc libpq-dev libmagic1 && \
    rm -rf /var/lib/apt/lists/*

# Install dependencies
COPY signal_copy/requirements.txt ./signal_copy_requirements.txt
RUN pip install --no-cache-dir --prefix=/install -r signal_copy_requirements.txt

FROM python:3.12-slim

RUN groupadd -r nexus && useradd -r -g nexus -d /app -s /usr/sbin/nologin nexus

WORKDIR /app

COPY --from=builder /install /usr/local
# Copy nexus package (data_bridge)
COPY --chown=nexus:nexus nexus/ ./nexus/
# Copy signal_copy package
COPY --chown=nexus:nexus signal_copy/ ./signal_copy/
# Copy nexus package (data bridge)
COPY --chown=nexus:nexus nexus/ ./nexus/
# Copy nexus bot files for data bridge
COPY --chown=nexus:nexus bots/ ./bots/
# Copy fusionnew engine code for adversarial
COPY --chown=nexus:nexus fusionnew/ ./fusionnew/
# Copy shared modules
COPY --chown=nexus:nexus notifications/ ./notifications/
COPY --chown=nexus:nexus risk/ ./risk/
COPY --chown=nexus:nexus execution/ ./execution/
COPY --chown=nexus:nexus utils/ ./utils/
# Copy gateway package (Execution Gateway client + adapters for live/testnet routing)
COPY --chown=nexus:nexus gateway/ ./gateway/
# Copy runtime whales for whale data
COPY --chown=nexus:nexus runtime/whales/ ./runtime/whales/

ENV PYTHONPATH=/app:/app/fusionnew:/app/signal_copy
ENV NEXUS_API_URL=http://fastapi:8000
ENV PYTHONUNBUFFERED=1

USER nexus

CMD ["python", "-u", "signal_copy/run.py"]