#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Borislav Vladimirov Petrakiev
# Instala los bots v5 (datos de MyFundedPerps, estrategias como archivos, avisos por Telegram y, si se activa,
# ejecución en la cuenta sandbox) como servicio en la Raspberry Pi.
# Un solo servicio ejecuta todos los bots; se crean y configuran desde el panel.
# Ejecútalo como tu usuario normal (no con sudo): ./install.sh
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
USER_NAME="$(id -un)"
SVC="$(basename "$DIR")"; SVC="${SVC//[^a-zA-Z0-9_-]/-}"

echo "== Instalando el servicio «$SVC» desde $DIR"
if [ -f "/etc/systemd/system/$SVC.service" ] && ! grep -q "WorkingDirectory=$DIR\$" "/etc/systemd/system/$SVC.service"; then
  echo "ERROR: ya existe un servicio «$SVC» de otra carpeta. Renombra esta carpeta y vuelve a ejecutar ./install.sh"; exit 1
fi

echo "== 1/5 Paquetes del sistema"
sudo apt-get update -qq
sudo apt-get install -y python3 python3-venv fonts-dejavu-core >/dev/null
# por si pip tiene que compilar Pillow (normalmente no: hay paquetes ya hechos para la Raspberry)
sudo apt-get install -y libjpeg-dev zlib1g-dev >/dev/null 2>&1 || true

echo "== 2/5 Entorno de Python"
[ -d "$DIR/venv" ] || python3 -m venv "$DIR/venv"
"$DIR/venv/bin/pip" install -q --upgrade pip
"$DIR/venv/bin/pip" install -q "websocket-client>=1.2"
if ! "$DIR/venv/bin/pip" install -q "pillow>=9.0"; then
  echo "AVISO: no se pudo instalar Pillow. Todo funciona igual, pero los avisos de Telegram irán sin gráfico."
fi

echo "== 3/5 Ajustes del panel"
if [ ! -f "$DIR/config.env" ]; then
  "$DIR/venv/bin/python" "$DIR/kcbot.py" --configurar
  if [ ! -f "$DIR/data/bots.json" ] && [ ! -f "$DIR/data/config.json" ]; then
    echo "Puedes traer bots de instalaciones anteriores (o hacerlo luego con ./bot.sh importar-bot)."
    while true; do
      read -r -p "Carpeta de un bot 2.x para importarlo con su historial (Intro = ninguno): " OLD
      [ -z "$OLD" ] && break
      "$DIR/venv/bin/python" "$DIR/kcbot.py" --config "$DIR/config.env" --importar-bot "${OLD/#\~/$HOME}" || true
    done
  fi
else
  echo "Ya existe config.env (para cambiarlo: ./bot.sh configurar)"
fi

echo "== 4/5 Comprobación de conexión y datos"
if ! "$DIR/venv/bin/python" "$DIR/kcbot.py" --config "$DIR/config.env" --comprobar; then
  echo "La comprobación ha fallado. Corrige lo indicado y vuelve a ejecutar ./install.sh"; exit 1
fi

echo "== 5/5 Servicio systemd «$SVC»"
sudo tee /etc/systemd/system/$SVC.service >/dev/null <<UNIT
[Unit]
Description=Bots MyFundedPerps v5 ($SVC, papel / sandbox)
Wants=network-online.target time-sync.target
After=network-online.target time-sync.target

[Service]
Type=simple
User=$USER_NAME
WorkingDirectory=$DIR
ExecStart=$DIR/venv/bin/python -u $DIR/kcbot.py --config $DIR/config.env
Restart=always
RestartSec=20
Nice=5

[Install]
WantedBy=multi-user.target
UNIT
sudo systemctl daemon-reload
sudo systemctl enable --now $SVC
chmod +x "$DIR/bot.sh"
sleep 3
"$DIR/bot.sh" panel
echo "Listo. Servicio «$SVC». Los bots se crean y configuran desde el panel."
echo "Para los avisos por Telegram: panel → Avisos."
echo "Para operar en la cuenta sandbox: LEEME.md, sección «Operar en la cuenta sandbox»."
