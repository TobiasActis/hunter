#!/usr/bin/env bash
# Prepara un Ubuntu limpio (Oracle Cloud Always Free / cualquier VPS
# parecido) para correr HUNTER 24/7 como servicio systemd -- se
# reinicia solo si se cae, y sobrevive a que cierres la sesión SSH.
#
# Uso: parado en la carpeta del proyecto en la VM (donde está main.py):
#   chmod +x deploy/setup.sh && ./deploy/setup.sh
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SERVICE_USER="$(whoami)"

echo "==> Proyecto en: $PROJECT_DIR"
echo "==> Instalando Python3, pip, venv..."
sudo apt-get update -y
sudo apt-get install -y python3 python3-venv python3-pip

echo "==> Creando entorno virtual en $PROJECT_DIR/venv..."
python3 -m venv "$PROJECT_DIR/venv"
"$PROJECT_DIR/venv/bin/pip" install --upgrade pip --quiet
"$PROJECT_DIR/venv/bin/pip" install -r "$PROJECT_DIR/requirements.txt" --quiet

echo "==> Instalando servicio principal (main.py, corre siempre)..."
sudo cp "$PROJECT_DIR/deploy/hunter.service.template" /etc/systemd/system/hunter.service
sudo sed -i "s#__PROJECT_DIR__#$PROJECT_DIR#g; s#__USER__#$SERVICE_USER#g" /etc/systemd/system/hunter.service

echo "==> Instalando timer de reentrenamiento diario (brain.py)..."
sudo cp "$PROJECT_DIR/deploy/hunter-brain.service.template" /etc/systemd/system/hunter-brain.service
sudo sed -i "s#__PROJECT_DIR__#$PROJECT_DIR#g; s#__USER__#$SERVICE_USER#g" /etc/systemd/system/hunter-brain.service
sudo cp "$PROJECT_DIR/deploy/hunter-brain.timer" /etc/systemd/system/hunter-brain.timer

sudo systemctl daemon-reload
sudo systemctl enable --now hunter.service
sudo systemctl enable --now hunter-brain.timer

echo ""
echo "==================================================================="
echo "Listo. Estado del servicio principal:"
sudo systemctl status hunter.service --no-pager || true
echo ""
echo "Comandos utiles:"
echo "  Ver logs en vivo:         sudo journalctl -u hunter.service -f"
echo "  Ver logs de brain.py:     sudo journalctl -u hunter-brain.service -f"
echo "  Reiniciar:                sudo systemctl restart hunter.service"
echo "  Parar:                    sudo systemctl stop hunter.service"
echo ""
echo "El dashboard escucha SOLO en 127.0.0.1:8000 dentro de la VM (a"
echo "proposito, no lo expongas al puerto publico). Para verlo desde tu"
echo "PC, abri un tunel SSH y dejalo corriendo en otra terminal:"
echo "  ssh -L 8000:localhost:8000 $SERVICE_USER@<IP_DE_LA_VM>"
echo "y despues entra a http://localhost:8000 en tu navegador."
echo "==================================================================="
