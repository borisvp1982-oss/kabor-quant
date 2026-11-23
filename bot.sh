#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Borislav Vladimirov Petrakiev
# KaBor Quant. Uso: ./bot.sh [estado|panel|logs|arrancar|parar|reiniciar|configurar|usuario|certificado|cerrar-sesiones|quitar-2fa USUARIO|importar RUTA|importar-bot CARPETA|comprobar|probar ARCHIVO [opciones]|bots|contexto N|analizar N|informe|conector|desinstalar]
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PY="$DIR/venv/bin/python"
SVC="$(basename "$DIR")"; SVC="${SVC//[^a-zA-Z0-9_-]/-}"
cfg() { grep -E "^$1=" "$DIR/config.env" 2>/dev/null | head -1 | cut -d= -f2-; }
case "${1:-}" in
  estado)      systemctl status "$SVC" --no-pager -l | head -n 12 ;;
  panel)       IP=$(hostname -I | awk '{print $1}'); PORT=$(cfg FP_WEB_PORT)
               PROTO=http; [ "$(cfg FP_WEB_HTTPS)" = 1 ] && PROTO=https
               echo "Panel ($SVC): $PROTO://$IP:${PORT:-8083}/  ·  también $PROTO://$(hostname).local:${PORT:-8083}/" ;;
  logs)        journalctl -u "$SVC" -f -n 60 ;;
  arrancar)    sudo systemctl start "$SVC" && echo "Arrancado" ;;
  parar)       sudo systemctl stop "$SVC" && echo "Parado" ;;
  reiniciar)   sudo systemctl restart "$SVC" && echo "Reiniciado" ;;
  configurar)  "$PY" "$DIR/kcbot.py" --configurar && echo "Aplica los cambios con: ./bot.sh reiniciar" ;;
  importar|importar-bot)
               # se para el servicio para que no pise la lista de bots, se importa y se vuelve a arrancar
               RUN=0; systemctl is-active --quiet "$SVC" && RUN=1 && sudo systemctl stop "$SVC"
               "$PY" "$DIR/kcbot.py" --config "$DIR/config.env" --$1 "$2"
               [ "$RUN" = 1 ] && sudo systemctl start "$SVC" && echo "Servicio arrancado de nuevo con el bot importado." ;;
  usuario)     "$PY" "$DIR/kcbot.py" --config "$DIR/config.env" --usuario ;;
  certificado) "$PY" "$DIR/kcbot.py" --config "$DIR/config.env" --certificado ;;
  cerrar-sesiones) "$PY" "$DIR/kcbot.py" --config "$DIR/config.env" --cerrar-sesiones ;;
  quitar-2fa)  [ -z "${2:-}" ] && { echo "Uso: ./bot.sh quitar-2fa USUARIO"; exit 1; }
               "$PY" "$DIR/kcbot.py" --config "$DIR/config.env" --quitar-2fa "$2" ;;
  comprobar)   "$PY" "$DIR/kcbot.py" --config "$DIR/config.env" --comprobar ;;
  probar)      shift; "$PY" "$DIR/kcbot.py" --config "$DIR/config.env" --probar "$@" ;;
  bots)        # los bots con el número que usan contexto y analizar (su posición en el menú del panel)
               D="$(cfg FP_DATA_DIR)"; D="${D:-data}"; [[ "$D" = /* ]] || D="$DIR/$D"
               "$PY" "$DIR/contexto.py" bots --datos "$D" ;;
  contexto)    # backtest con los ajustes de un bot y exporta el contexto: ./bot.sh contexto 1 [--dias 365]
               #   (1 = primer bot del menú del panel; también vale el nombre: ./bot.sh contexto "Oro 5m")
               [ -z "${2:-}" ] && { echo "Uso: ./bot.sh contexto N|\"NOMBRE\" [--dias 365]  ·  ./bot.sh bots para verlos"; exit 1; }
               N="$2"; shift 2; "$PY" "$DIR/kcbot.py" --config "$DIR/config.env" --bot "$N" --exportar "$@" ;;
  analizar)    # ./bot.sh analizar 1  ·  ./bot.sh analizar "Oro 5m"  ·  ./bot.sh analizar ARCHIVO.csv [--min 20]
               D="$(cfg FP_DATA_DIR)"; D="${D:-data}"; [[ "$D" = /* ]] || D="$DIR/$D"; shift
               "$PY" "$DIR/contexto.py" analizar --datos "$D" "$@" ;;
  informe)     "$PY" "$DIR/kcbot.py" --config "$DIR/config.env" --informe ;;
  conector)    T=$(cfg FP_MCP_TOKEN)
               if [ -z "$T" ]; then T=$("$PY" -c "import secrets; print(secrets.token_urlsafe(32))")
                 echo "FP_MCP_TOKEN=$T" >> "$DIR/config.env"; echo "Token creado. Aplica con: ./bot.sh reiniciar"; fi
               echo "Ruta del conector (con la dirección pública de Funnel delante):"
               echo "  /mcp/$T"
               echo "Para cambiarlo, borra la línea FP_MCP_TOKEN de config.env y repite." ;;
  desinstalar) sudo systemctl disable --now "$SVC"; sudo rm -f "/etc/systemd/system/$SVC.service"; sudo systemctl daemon-reload
               echo "Servicio $SVC eliminado. La carpeta y los datos se conservan." ;;
  *) echo "Uso: ./bot.sh [estado|panel|logs|arrancar|parar|reiniciar|configurar|usuario|certificado|cerrar-sesiones|quitar-2fa USUARIO|importar RUTA|importar-bot CARPETA|comprobar|probar ARCHIVO [--simbolo BTC --tf 5m --dias 30 --ajuste clave=valor --exportar]|bots|contexto N [--dias 365]|analizar N|ARCHIVO.csv|informe|conector|desinstalar]" ;;
esac
