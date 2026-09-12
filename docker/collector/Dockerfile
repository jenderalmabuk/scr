# Nexus Collector — multi-stage, non-root
FROM python:3.12-slim AS builder

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc libpq-dev && \
    rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt

FROM python:3.12-slim

RUN groupadd -r nexus && useradd -r -g nexus -d /app -s /usr/sbin/nologin nexus

WORKDIR /app

COPY --from=builder /install /usr/local
COPY --chown=nexus:nexus collector/ ./collector/
COPY --chown=nexus:nexus universe.txt ./

USER nexus

CMD ["python", "-u", "-m", "collector"]
