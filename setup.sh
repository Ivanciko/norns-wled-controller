#!/usr/bin/env bash
# setup.sh — norns-wled-controller installer
# Raspberry Pi OS Lite (64-bit, Debian Bookworm/Trixie) + norns shield CS4270
#
# Usage:
#   git clone https://github.com/Ivanciko/norns-wled-controller ~/wled-controller
#   cd ~/wled-controller
#   chmod +x setup.sh
#   ./setup.sh
#
# After setup: edit config.json (change wled_host to your ESP32 IP), then reboot.

set -euo pipefail

GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; NC='\033[0m'
info()  { echo -e "${GREEN}[+]${NC} $*"; }
warn()  { echo -e "${YELLOW}[!]${NC} $*"; }
die()   { echo -e "${RED}[✗]${NC} $*"; exit 1; }

[[ $EUID -eq 0 ]] && die "Ejecuta como usuario normal, no como root."

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WLED_DIR="$HOME/wled-controller"
CONFIG=/boot/firmware/config.txt

echo
info "=== norns-wled-controller setup ==="
echo

# ── 1. Paquetes del sistema ───────────────────────────────────────────────────
info "Instalando paquetes del sistema..."
sudo apt-get update -qq
sudo apt-get install -y -qq \
    python3 python3-pip python3-venv python3-dev \
    libasound2-dev build-essential \
    raspberrypi-kernel-headers device-tree-compiler \
    libjpeg-dev libopenjp2-7 i2c-tools git

# ── 2. Modulo kernel CS4270 ───────────────────────────────────────────────────
info "Compilando modulo CS4270 (puede tardar 1-2 min)..."
MODULE_DIR="$REPO_DIR/hardware"
cd "$MODULE_DIR"
make clean 2>/dev/null || true
make -j$(nproc)

KERNEL=$(uname -r)
MOD_PATH="/lib/modules/$KERNEL/kernel/sound/soc/codecs"
sudo mkdir -p "$MOD_PATH"
sudo cp snd-soc-cs4270.ko "$MOD_PATH/"
sudo depmod -a
info "Modulo instalado → $MOD_PATH"

# ── 3. Device tree overlay ────────────────────────────────────────────────────
info "Compilando overlay de device tree..."
dtc -@ -I dts -O dtb -o norns-shield-cs4270.dtbo norns-shield-cs4270-overlay.dts
sudo cp norns-shield-cs4270.dtbo /boot/firmware/overlays/
info "Overlay instalado → /boot/firmware/overlays/"

# ── 4. /boot/firmware/config.txt ──────────────────────────────────────────────
info "Configurando /boot/firmware/config.txt..."

add_if_missing() {
    grep -qF "$1" "$CONFIG" || echo "$1" | sudo tee -a "$CONFIG" > /dev/null
}

add_if_missing "dtparam=i2c_arm=on"
add_if_missing "dtparam=i2c_vc=on"
add_if_missing "dtparam=i2s=on"
add_if_missing "dtparam=spi=on"
add_if_missing "dtparam=audio=on"
add_if_missing "dtoverlay=norns-shield-cs4270"
add_if_missing "enable_uart=1"

# Desactivar audio HDMI: evita que PortAudio falle al enumerar cards ALSA
if grep -q "dtoverlay=vc4-kms-v3d$" "$CONFIG"; then
    sudo sed -i 's/dtoverlay=vc4-kms-v3d$/dtoverlay=vc4-kms-v3d,audio=off/' "$CONFIG"
    info "Audio HDMI desactivado en vc4-kms-v3d"
elif ! grep -q "vc4-kms-v3d,audio=off" "$CONFIG"; then
    echo "dtoverlay=vc4-kms-v3d,audio=off" | sudo tee -a "$CONFIG" > /dev/null
    info "Audio HDMI desactivado (vc4-kms-v3d,audio=off añadido)"
fi

# ── 5. Serial console ─────────────────────────────────────────────────────────
info "Desactivando serial-getty (libera UART)..."
sudo systemctl disable --now serial-getty@ttyS0 2>/dev/null || true

# ── 6. Sudoers ────────────────────────────────────────────────────────────────
info "Configurando sudoers..."
echo "$USER ALL=(root) NOPASSWD: /usr/sbin/poweroff, /usr/bin/nmcli" | \
    sudo tee /etc/sudoers.d/wled-controller > /dev/null
sudo chmod 440 /etc/sudoers.d/wled-controller

# ── 7. Archivos del proyecto ──────────────────────────────────────────────────
info "Preparando directorio del proyecto..."
if [[ "$REPO_DIR" != "$WLED_DIR" ]]; then
    mkdir -p "$WLED_DIR"
    cp "$REPO_DIR"/*.py "$WLED_DIR/"
    [[ -f "$WLED_DIR/config.json" ]] || cp "$REPO_DIR/config.json" "$WLED_DIR/"
    cp "$REPO_DIR/wled-controller.service" "$WLED_DIR/"
fi

# ── 8. Python venv ────────────────────────────────────────────────────────────
info "Creando entorno Python y instalando dependencias..."
python3 -m venv "$WLED_DIR/venv"
"$WLED_DIR/venv/bin/pip" install --quiet --upgrade pip
"$WLED_DIR/venv/bin/pip" install --quiet -r "$REPO_DIR/requirements.txt"
info "Dependencias Python instaladas"

# ── 9. .asoundrc ──────────────────────────────────────────────────────────────
info "Configurando ALSA..."
cat > "$HOME/.asoundrc" << 'ASOUNDRC'
pcm.norns {
    type hw
    card "nornsshieldcs42"
    device 0
}
ctl.norns {
    type hw
    card "nornsshieldcs42"
}
ASOUNDRC

# ── 10. Servicio systemd ──────────────────────────────────────────────────────
info "Instalando servicio systemd..."
SERVICE_DIR="$HOME/.config/systemd/user"
mkdir -p "$SERVICE_DIR"
cp "$WLED_DIR/wled-controller.service" "$SERVICE_DIR/"

systemctl --user daemon-reload
systemctl --user enable wled-controller
loginctl enable-linger "$USER"
info "Servicio habilitado (arrancara solo al encender)"

# ── 11. WLED host ─────────────────────────────────────────────────────────────
echo
warn "IMPORTANTE: edita $WLED_DIR/config.json"
warn "Cambia 'wled_host' a la IP de tu ESP32 con WLED."
echo
info "=== Instalacion completada ==="
echo
echo "  Reinicia la Pi para activar el hardware:"
echo "  sudo reboot"
echo
echo "  Tras el primer arranque, si el volumen de audio es bajo ejecuta:"
echo "  amixer -c nornsshieldcs42 sset Master 192,192 on && sudo alsactl store"
echo
