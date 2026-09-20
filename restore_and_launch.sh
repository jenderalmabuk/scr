#!/usr/bin/env bash
set -e

echo '=================================================='
echo '  SIGNAL COPY REAL - 1-CLICK RESTORE & LAUNCH'
echo '=================================================='

# 1. Pastikan file .env tersedia
if [ ! -f .env ]; then
  echo '[ERROR] File .env tidak ditemukan! Harap ekstrak backup private terlebih dahulu.'
  exit 1
fi

if [ ! -f docker/.env ]; then
  echo '[ERROR] File docker/.env tidak ditemukan! Harap ekstrak backup private terlebih dahulu.'
  exit 1
fi

echo '[1/4] Mempersiapkan folder data & permissions...'
mkdir -p runtime/state runtime/revo runtime/whales runtime/logs journal
chmod -R 777 runtime journal

echo '[2/4] Menghentikan container lama jika ada...'
docker compose down --remove-orphans || true

echo '[3/4] Melakukan build dan memulai container...'
docker compose up -d --build

echo '[4/4] Menunggu inisialisasi container...'
sleep 5

echo '=================================================='
echo '  STATUS CONTAINER:'
echo '=================================================='
docker ps --format 'table {{.Names}}\t{{.Status}}\t{{.Ports}}'

echo ''
echo '=================================================='
echo '  LOG INISIALISASI SIGNAL COPY:'
echo '=================================================='
docker logs --tail 25 nexus_signal_copy || true

echo ''
echo 'Restore dan peluncuran selesai! Pantau live logs dengan:'
echo '  docker logs -f --tail 50 nexus_signal_copy'
