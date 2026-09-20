#!/usr/bin/env bash
# Configura el oyente de alertas de Telegram (SOLO lee @kotte_memescan_bot) en UN paso. Correr en el servidor, dentro de ~/hunter:
#   ssh -t -i D:/Descargas/hunter_deploy_key ubuntu@152.67.63.253 'cd ~/hunter && bash setup_tg.sh'
# Te pide: api_id, api_hash (oculto), y luego el TELEFONO (con codigo de pais, ej. +54911...) y el codigo que te llega a Telegram (y la clave de doble factor si la tenes).
# Las credenciales quedan solo en ~/hunter/.tg_env (chmod 600) y en la sesion ~/.hunter_tg; nadie mas las ve. La clave del dashboard se toma de ~/hunter/.env sin que la escribas.
set -e
cd "$(dirname "$0")"
umask 077
[ -f .env ] || { echo "No encuentro .env en $(pwd): corre esto dentro de ~/hunter"; exit 1; }
DU=$(grep -E "^DASHBOARD_USER" .env | head -1 | cut -d= -f2- | tr -d '"' | tr -d "'")
DP=$(grep -E "^DASHBOARD_PASSWORD" .env | head -1 | cut -d= -f2- | tr -d '"' | tr -d "'")
[ -n "$DU" ] || { echo "No encuentro DASHBOARD_USER en .env"; exit 1; }
read -r -p "TG_API_ID (solo numeros): " TG_API_ID
case "$TG_API_ID" in ''|*[!0-9]*) echo "El api_id debe ser solo numeros"; exit 1;; esac
read -r -s -p "TG_API_HASH (no se muestra al escribir): " TG_API_HASH; echo
[ ${#TG_API_HASH} -ge 20 ] || { echo "El api_hash parece muy corto"; exit 1; }
{ echo "TG_API_ID=$TG_API_ID"; echo "TG_API_HASH=$TG_API_HASH"; echo "HUNTER_USER=$DU"; echo "HUNTER_PASSWORD=$DP"; } > .tg_env
chmod 600 .tg_env
echo "Credenciales guardadas en .tg_env (permisos 600)."
echo "Ahora el login de Telegram: escribi tu TELEFONO con codigo de pais (NO un token de bot), despues el codigo que te llega a Telegram."
set -a; . ./.tg_env; set +a
./venv/bin/python tg_alerts.py --login
echo "Instalando el servicio para que quede corriendo siempre..."
sudo cp hunter-tg.service.example /etc/systemd/system/hunter-tg.service
sudo systemctl daemon-reload
sudo systemctl enable --now hunter-tg
sleep 5
systemctl is-active hunter-tg
journalctl -u hunter-tg --since "-1min" --no-pager | tail -5
echo "Listo. Cuando llegue una alerta SLOW GRADUATION aparece en la pestana 'Memes: graduaciones lentas' como 'alerta del bot'."
