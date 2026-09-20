# Panduan Restore dan Deploy Bot di VPS Baru

Dokumen ini berisi panduan lengkap untuk memasang dan menjalankan bot Signal Copy Real di VPS baru menggunakan repositori GitHub dan file backup private ini.

---

## 1. Persiapan VPS Baru (Prerequisites)
Pastikan VPS baru menggunakan OS Ubuntu 22.04 LTS atau 24.04 LTS dan telah terinstall **Docker** & **Docker Compose**.

Jika belum terinstall, jalankan perintah berikut di VPS baru:
`ash
# Update sistem
apt-get update && apt-get upgrade -y
apt-get install -y git curl wget unzip

# Install Docker
curl -fsSL https://get.docker.com -o get-docker.sh
sh get-docker.sh

# Verifikasi docker
docker --version
docker compose version
`

---

## 2. Clone Repository dari GitHub
Clone kode bot dari repositori publik/private Anda:
`ash
git clone https://github.com/jenderalmabuk/scr.git /opt/signalcopyreal
cd /opt/signalcopyreal
`

---

## 3. Ekstrak File Backup Private
Upload file signalcopyreal_private_backup_20260920.zip ke VPS (misalnya ke /root/ atau langsung ke /opt/signalcopyreal/), lalu ekstrak:
`ash
# Contoh jika file zip berada di /root/
unzip -o /root/signalcopyreal_private_backup_20260920.zip -d /opt/signalcopyreal/

cd /opt/signalcopyreal
`

File yang dipulihkan meliputi:
1. .env (Kredensial API Bybit Mainnet, Channel Whitelist/Calibration, Dynamic Risk, Notifikasi Telegram)
2. docker/.env (Konfigurasi internal database, port, dan container)
3. untime/ (Sesi Telethon Telegram signal_copy_session.session, State channel performance, Universe list)
4. journal/ (Database riwayat order, posisi aktif open_positions.json, scratch exit audit)

---

## 4. Jalankan Bot (1-Click Launch)
Jalankan script otomatis:
`ash
chmod +x restore_and_launch.sh
./restore_and_launch.sh
`

Atau jalankan langkah manual:
`ash
# 1. Atur izin akses volume runtime & journal untuk docker non-root user
chmod -R 777 runtime journal

# 2. Build dan jalankan seluruh container
docker compose down
docker compose up -d --build
`

---

## 5. Verifikasi Bot Berjalan Normal
Periksa apakah seluruh container aktif dan sehat:
`ash
# Cek container
docker ps

# Cek log Signal Copy Real
docker logs -f --tail 50 nexus_signal_copy

# Cek log Gateway Eksekusi
docker logs -f --tail 50 nexus_gateway

# Cek log Bybit Collector
docker logs -f --tail 50 nexus_bybit_collector
`

---

## 6. Selesai!
Bot akan langsung:
- Terhubung ke Telegram provider tanpa perlu login ulang (menggunakan session yang di-restore).
- Membaca posisi aktif yang tersimpan.
- Menganalisa sinyal live dengan Dynamic TP Slicing, Instant Profit Lock, dan Dynamic Risk Sizing.
