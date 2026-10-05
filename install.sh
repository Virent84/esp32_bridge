#!/usr/bin/env bash
# Install on Raspberry Pi 5 (Raspberry Pi OS 64-bit). ESP32-S3 version: no I2S overlay needed.  Run:  bash install.sh
set -e
DIR="$(cd "$(dirname "$0")" && pwd)"

echo "[1/3] Installing packages (system numpy/scipy - no pip, no venv, no libopenblas problems)"
sudo apt-get update
sudo apt-get install -y python3-numpy python3-scipy python3-flask python3-serial alsa-utils
sudo usermod -aG dialout,audio "$USER" || true      # serial access to /dev/ttyACM0 (log out/in or reboot afterwards)

echo "[2/3] Installing service (not started yet)"
sed "s#__INSTALL_DIR__#$DIR#g; s#__USER__#$USER#g" "$DIR/systemd/hearing-aid.service" | sudo tee /etc/systemd/system/hearing-aid.service >/dev/null
sudo systemctl daemon-reload
echo "   (enable at boot later with:  sudo systemctl enable --now hearing-aid)"

echo "[3/3] Offline self-test (no hardware needed)"
python3 "$DIR/tools/selftest.py" || echo "WARNING: selftest failed"

cat <<MSG

Done. LOG OUT AND IN (or reboot) so the 'dialout' group applies, then:
  ls /dev/ttyACM*                                    # ESP32 native USB port must appear
  python3 $DIR/tools/esp32_check.py                  # link + sync check (clap above the array)
  python3 $DIR/tools/esp32_record.py --seconds 10    # record, then tools/offline_wav.py
  python3 $DIR/run.py                                # live dashboard on http://<pi-ip>:8080
MSG
