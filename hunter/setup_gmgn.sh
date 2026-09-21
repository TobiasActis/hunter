#!/usr/bin/env bash
# Carga la clave de API de GMGN (SOLO LECTURA: tendencias y smart money; no opera) en el servidor y reinicia el laboratorio de atencion. Correr en el servidor, dentro de ~/hunter:
#   ssh -t -i D:/Descargas/hunter_deploy_key ubuntu@152.67.63.253 'cd ~/hunter && bash setup_gmgn.sh'
# Te pide la clave de API (oculta al escribir; la copias de gmgn.ai/ai -> GMGN API Management -> icono de copiar). NO hace falta la clave privada: no se sube nunca.
# La clave queda solo en ~/hunter/.gmgn_env (chmod 600).
set -e
cd "$(dirname "$0")"
umask 077
read -r -s -p "GMGN_API_KEY (no se muestra al escribir): " K; echo
[ ${#K} -ge 16 ] || { echo "La clave parece muy corta: no se guardo nada"; exit 1; }
case "$K" in *BEGIN*|*PRIVATE*) echo "Eso parece una clave PRIVADA: no la pegues aca. Necesito la clave de API (gmgn_...)"; exit 1;; esac
echo "GMGN_API_KEY=$K" > .gmgn_env
chmod 600 .gmgn_env
echo "Clave guardada en .gmgn_env (permisos 600)."
sudo systemctl restart hunter-attention
sleep 6
systemctl is-active hunter-attention
journalctl -u hunter-attention --since "-1min" --no-pager | tail -5
echo "Listo. En unos minutos aparecen fuentes GMGN_TREND / GMGN_SMART en la seccion 9 del reporte nocturno."
