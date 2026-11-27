#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Borislav Vladimirov Petrakiev
"""
kcbot.py -- KaBor Quant. Varios bots en un solo programa, con datos de MyFundedPerps en tiempo real, un panel web central
(una pestaña por bot), estrategias que se cargan como archivos (carpeta estrategias/, ver ESTRATEGIAS.md) y
avisos por Telegram con captura del gráfico. Pensado para la Raspberry Pi.

Cada bot funciona en PAPEL (como hasta la 4.0) o, si se activa, también en una o varias CUENTAS SANDBOX:
  * Velas y precios: WebSocket público de datos de mercado de MyFundedPerps (no lleva clave).
  * Cuentas (cuentas.py, desde la 6.3): se dan de alta en el panel, cada una con su clave fp_test_. Órdenes solo si
    «Enviar órdenes» está activado en el panel y el bot tiene «Ejecución en sandbox → Modo: sandbox» con sus
    cuentas elegidas (ejecucion.py). Cada cuenta se puede bloquear a mano o al llegar a un límite del día.
    Nunca opera en cuentas reales: solo acepta claves fp_test_ y el host del sandbox.
  * Telegram (opcional): solo envía mensajes a tu chat y lee los comandos que le escribes.

Uso:
  python3 kcbot.py --configurar              puerto, HTTPS, token de API y clave (opcional) del panel
  python3 kcbot.py --usuario                 crea el usuario del panel o le cambia la contraseña
  python3 kcbot.py --certificado             crea (o renueva) el certificado HTTPS autofirmado
  python3 kcbot.py --cerrar-sesiones         cierra todas las sesiones abiertas del panel
  python3 kcbot.py --importar RUTA           crea un bot con la estrategia de un config.env de la versión 1.x
  python3 kcbot.py --importar-bot CARPETA    trae un bot de otra instalación 2.x (carpeta data) con su historial
  python3 kcbot.py --comprobar               prueba conexión y datos, y sale
  python3 kcbot.py                           arranca todos los bots y el panel
  python3 kcbot.py --informe                 crea un ZIP con los informes de todos los bots, y sale
"""
import argparse
import csv
import hmac
import io
import json
import math
import os
import queue
import random
import re
import shutil
import socket
import ssl
import sys
import threading
import time
import urllib.error
import urllib.request
import zipfile
from collections import deque
from datetime import datetime, timedelta, timezone
from http.client import HTTPException
from http.cookies import CookieError, SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import nucleo as K  # noqa: E402
import avisos as A  # noqa: E402
import ejecucion as E  # noqa: E402
import riesgo as RG  # noqa: E402
import acceso as AC  # noqa: E402
import cuentas as CU  # noqa: E402
import noticias as NT  # noqa: E402
import contexto as CX  # noqa: E402
import optimizador as OP  # noqa: E402
import cartera as CT  # noqa: E402
import deriva as DV  # noqa: E402
import diario as DI  # noqa: E402
import planes as PL  # noqa: E402
import externos as EX  # noqa: E402
import fiscal as FI  # noqa: E402
import temas as TM  # noqa: E402

APP = "KaBor Quant"
VERSION = "9.4.4"
WEB_DIR = os.path.join(HERE, "web")
ESTATICOS = {"/favicon.svg": ("favicon.svg", "image/svg+xml"), "/icono-180.png": ("icono-180.png", "image/png"),
             "/icono-512.png": ("icono-512.png", "image/png"), "/icono-192.png": ("icono-192.png", "image/png"),
             "/manifest.webmanifest": ("manifest.webmanifest", "application/manifest+json"),
             "/sw.js": ("sw.js", "text/javascript; charset=utf-8"),
             "/i18n.js": ("i18n.js", "text/javascript; charset=utf-8"),
             "/vendor/lightweight-charts.js": ("vendor/lightweight-charts.standalone.production.js",
                                               "text/javascript; charset=utf-8"),
             "/i18n/en.json": ("i18n/en.json", "application/json; charset=utf-8"),
             "/i18n/fr.json": ("i18n/fr.json", "application/json; charset=utf-8")}
SANDBOX_HOST = "https://sandbox.myfundedperpetuals.com"
STREAM_URL = "wss://api-stream.myfundedperpetuals.com/v1/market-data"
HIST_N = 3000            # velas en memoria por bot para recalcular al cambiar ajustes
BT_MAX = 60000           # velas máximas en un backtest del panel
MAX_BOTS = 10
ALIASES = {"GOLD": ["GOLD", "XAU", "XAUUSD"], "NAS100": ["NAS100", "US100", "XYZ100", "NDX", "NASDAQ"],
           "BTC": ["BTC", "BTCUSDT"], "CL": ["CL", "WTI", "USOIL", "CLUSDT"], "BRENT": ["BRENT", "BRENTOIL"]}
PUNT_DIAS = 90            # backtest de cada estrategia de un bot combinado para su puntuación
PUNT_MIN_OPS = 10         # con menos operaciones no hay puntuación (va al final de la prioridad)
PUNT_CADA_MS = 24 * 3_600_000
DEFAULT_CNT = {"signals": 0, "ignored": 0, "skipped_cost": 0, "day_blocks": 0, "guard_stops": 0, "errors": 0,
               "reconnects": 0, "gaps": 0, "recovered": 0, "partials": 0, "extensions": 0, "filtered": {}}
WHY_ES = dict(K.EXIT_NAMES, **{"RSI": "RSI extremo", "RSI EN CONTRA": "RSI en contra"})
STRAT_DIR = os.path.join(HERE, "estrategias")
V2_FILES = ("config.json", "configs", "estado.json", "operaciones.csv", "eventos.csv", "velas.csv", "informes", "logs")


# ============================ utilidades ============================
def load_env_file(path):
    vals = {}
    if not os.path.exists(path):
        return vals
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            vals[k.strip()] = v.strip().strip('"').strip("'")
    return vals


def env(name, default):
    v = os.environ.get("FP_" + name)
    if v is None or v.strip() == "":
        return default
    return type(default)(v.strip())


class Cfg:
    """Solo la infraestructura (config.env). Cada bot guarda su estrategia y su ejecución en
    data/bots/<id>/config.json. Todas las claves admiten también variable de entorno FP_<NOMBRE>."""

    def __init__(self):
        self.key = env("MFP_KEY", "")
        self.host = env("MFP_HOST", SANDBOX_HOST).rstrip("/")
        self.account = env("MFP_ACCOUNT", "")
        self.exec = env("EXEC", 0) == 1                   # interruptor general de órdenes en el sandbox
        d = env("DATA_DIR", "data")
        self.data_dir = d if os.path.isabs(d) else os.path.join(HERE, d)
        self.bind = env("WEB_BIND", "0.0.0.0")
        self.port = env("WEB_PORT", 8083)
        self.token = env("WEB_TOKEN", "")                 # token de API: solo por cabecera X-Token
        self.login = env("WEB_LOGIN", 1) == 1               # 0 = panel sin inicio de sesión (no recomendado)
        self.https = env("WEB_HTTPS", 0) == 1
        self.proxy_https = env("WEB_PROXY_HTTPS", 0) == 1   # 1 = detrás de un proxy con HTTPS (Tailscale, Caddy…)
        tls = os.path.join(self.data_dir, "tls")
        c, k = env("WEB_CERT", ""), env("WEB_KEY", "")
        self.cert = (c if os.path.isabs(c) else os.path.join(HERE, c)) if c else os.path.join(tls, "cert.pem")
        self.cert_key = (k if os.path.isabs(k) else os.path.join(HERE, k)) if k else os.path.join(tls, "clave.pem")
        self.sesion_horas = env("SESION_HORAS", 12)         # cierre por inactividad sin «mantener la sesión»
        self.sesion_dias = env("SESION_DIAS", 30)           # duración con «mantener la sesión»
        self.test_days = env("TEST_DAYS", 14)
        self.local_tz = env("LOCAL_TZ", "Europe/Madrid")
        self.hist_n = env("HIST_N", HIST_N)               # velas en memoria por bot
        self.bt_max = env("BT_MAX", BT_MAX)               # velas máximas en un backtest del panel
        self.max_bots = env("MAX_BOTS", MAX_BOTS)
        self.account_poll = env("ACCOUNT_POLL_S", 60)     # cada cuánto se lee la cuenta para el panel
        self.http_timeout = env("HTTP_TIMEOUT_S", 20)
        self.mcp_token = env("MCP_TOKEN", "")              # conector MCP para Claude: vacío = desactivado
        self.plan_espera = env("PLAN_ESPERA_MIN", 5)       # espera para cancelar un plan activo

    def problems(self):
        out = []
        if self.key and not self.key.startswith("fp_test_"):
            out.append("La clave debe ser de sandbox (fp_test_). Por seguridad no se aceptan claves fp_live_.")
        if not E.host_permitido(self.host):
            out.append("FP_MFP_HOST solo puede ser {} (o localhost para pruebas).".format(SANDBOX_HOST))
        if self.mcp_token and len(self.mcp_token) < 24:
            out.append("FP_MCP_TOKEN tiene que tener al menos 24 caracteres (./bot.sh conector lo genera)")
        for name, v, lo in (("FP_HIST_N", self.hist_n, 500), ("FP_BT_MAX", self.bt_max, 1000),
                            ("FP_MAX_BOTS", self.max_bots, 1), ("FP_ACCOUNT_POLL_S", self.account_poll, 10),
                            ("FP_SESION_HORAS", self.sesion_horas, 1), ("FP_SESION_DIAS", self.sesion_dias, 1)):
            if v < lo:
                out.append("{} tiene que ser al menos {}".format(name, lo))
        return out


def get_tz(name):
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(name)
    except Exception:
        return timezone.utc


def now_ms():
    return int(time.time() * 1000)


def fmt_utc(ms, f="%Y-%m-%d %H:%M"):
    return datetime.fromtimestamp(ms / 1000, timezone.utc).strftime(f)


def day_label(d):
    return (datetime(1970, 1, 1) + timedelta(days=d)).strftime("%Y-%m-%d")


def wkey(d):
    return (d + 3) // 7


def week_label(w):
    return "semana del " + day_label(w * 7 - 3)


def atomic_write(path, text):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, path)


def append_csv(path, cols, row):
    new = not os.path.exists(path)
    with open(path, "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if new:
            w.writerow(cols)
        w.writerow(row)


def r2(x, nd=2):
    return None if x is None else round(x, nd)


def make_hour_fn(tz):
    cache = {}

    def f(t):
        k = t // 3_600_000
        v = cache.get(k)
        if v is None:
            d = datetime.fromtimestamp(t / 1000, tz)
            v = (d.hour, d.weekday())
            if len(cache) > 20000:
                cache.clear()
            cache[k] = v
        return v
    return f


class Log:
    def __init__(self, folder, prefix=""):
        self.folder, self.prefix = folder, prefix
        os.makedirs(folder, exist_ok=True)
        self.recent = deque(maxlen=300)
        self.lock = threading.Lock()

    def __call__(self, msg, level="info"):
        now = datetime.now(timezone.utc)
        line = "{:%Y-%m-%d %H:%M:%S}Z [{:5}] {}".format(now, level.upper(), msg)
        print(("[{}] ".format(self.prefix) if self.prefix else "") + line, flush=True)
        with self.lock:
            self.recent.append({"t": int(now.timestamp() * 1000), "level": level, "msg": msg})
            try:
                with open(os.path.join(self.folder, "registro_{:%Y-%m-%d}.log".format(now)), "a",
                          encoding="utf-8") as f:
                    f.write(line + "\n")
            except OSError:
                pass


# ============================ API REST (solo GET) ============================
class APIError(Exception):
    def __init__(self, status, code, msg):
        super().__init__("HTTP {} {}: {}".format(status, code, msg))
        self.status, self.code = status, code


class MFPRest:
    """Cliente REST de SOLO LECTURA: el único método que existe es get()."""

    def __init__(self, host, key):
        self.host, self.key = host, key

    def get(self, path, auth=True):
        req = urllib.request.Request(self.host + path, method="GET")
        req.add_header("Accept", "application/json")
        req.add_header("User-Agent", "kcbot-pi/" + VERSION)
        if auth:
            req.add_header("Authorization", "Bearer " + self.key)
        for i in range(3):
            try:
                with urllib.request.urlopen(req, timeout=20) as r:
                    return json.load(r)
            except urllib.error.HTTPError as e:
                body = e.read().decode(errors="replace")[:400]
                code, msg = "", body
                try:
                    err = json.loads(body)["error"]
                    code, msg = err.get("code", ""), err.get("message", "")
                except Exception:
                    pass
                if e.code in (429, 500, 502, 503) and i < 2:
                    wait = e.headers.get("Retry-After") or str(2 * (i + 1))
                    time.sleep(min(int(wait) if wait.isdigit() else 5, 30) + random.random())
                    continue
                raise APIError(e.code, code, msg)
            except (OSError, ValueError, HTTPException) as e:
                if i == 2:
                    raise RuntimeError("Red: {}: {}".format(type(e).__name__, e))
                time.sleep(2 * (i + 1))


def as_list(res):
    if isinstance(res, dict):
        res = res.get("data", [])
    return res if isinstance(res, list) else []


def _wsmod():
    try:
        import websocket  # paquete websocket-client
        return websocket
    except ImportError:
        sys.exit("Falta el paquete websocket-client: ejecuta install.sh")


# ============================ WebSocket: consultas de historial ============================
class MFPStream:
    """Consultas puntuales (historial de velas y hora del servidor) por una conexión propia.
    El WebSocket de mercado es público: nunca lleva la clave."""

    def __init__(self, log, on_reconnect=None):
        self.wsmod, self.log, self.on_reconnect = _wsmod(), log, on_reconnect
        self.ws, self.next_id, self.draining, self.connected_once = None, 1, False, False
        self.lock = threading.Lock()
        self.offset_ms, self.last_ping = 0, 0

    def _close(self):
        try:
            if self.ws:
                self.ws.close()
        except Exception:
            pass
        self.ws = None

    def _connect(self):
        self._close()
        self.ws = self.wsmod.create_connection(STREAM_URL, timeout=20)
        self.draining = False
        if self.connected_once and self.on_reconnect:
            self.on_reconnect()
        self.connected_once = True

    def _req(self, method, payload=None):
        if self.ws is None or self.draining:
            self._connect()
        rid = self.next_id
        self.next_id = rid + 1 if rid < 1_000_000 else 1
        msg = {"op": "req", "id": rid, "method": method}
        if payload is not None:
            msg["payload"] = payload
        self.ws.send(json.dumps(msg))
        deadline = time.time() + 25
        while time.time() < deadline:
            fr = json.loads(self.ws.recv())
            op = fr.get("op")
            if op == "draining":
                self.draining = True
                continue
            if fr.get("id") != rid:
                continue
            if op == "res":
                return fr.get("result")
            if op == "err":
                err = fr.get("error") or {}
                raise APIError("ws", err.get("code") or err.get("reason") or "",
                               err.get("message") or json.dumps(err)[:200])
        raise TimeoutError("sin respuesta del WebSocket")

    def request(self, method, payload=None):
        with self.lock:
            last = None
            for i in range(3):
                try:
                    return self._req(method, payload)
                except APIError as e:
                    last = e
                    if "transport" not in str(e).lower() and "HistoryFetchFailed" not in str(e):
                        raise
                except Exception as e:
                    last = e
                    self._close()
                time.sleep(min(2 ** i * 2, 20) + random.random())
            raise RuntimeError("WebSocket: {}: {}".format(type(last).__name__, last))

    def server_now(self):
        if time.time() - self.last_ping > 600:
            try:
                t0 = time.time()
                srv = self.request("ping")
                t1 = time.time()
                self.offset_ms = int(float(srv) - (t0 + t1) / 2 * 1000)
                self.last_ping = t1
            except Exception as e:
                self.log("No se pudo sincronizar la hora con el servidor: {}".format(e), "warn")
                self.last_ping = time.time() - 540
        return now_ms() + self.offset_ms

    def candles(self, market, tf, n):
        """Últimas n velas CERRADAS (t, o, h, l, c) de un mercado y temporalidad. El diario y el semanal se
        construyen aquí con velas de 4h (días UTC y semanas de lunes), igual con cualquier proveedor."""
        if tf in K.AGREGADAS:
            src = K.AGREGADAS[tf]
            ms, sms = K.TF_MS[tf], K.TF_MS[src]
            k = ms // sms
            base = self._candles(market, src, int(min(n * k + 2 * k, 40_000)))
            return K.agrupar_velas(base, ms, sms, self.server_now())[-int(n):]
        return self._candles(market, tf, n)

    def _candles(self, market, tf, n):
        tf_ms = K.TF_MS[tf]
        now = self.server_now()
        out, end, rounds = {}, None, 0
        while len(out) < n and rounds < 70:
            rounds += 1
            lim = int(min(n - len(out) + 2, 1000))
            p = {"provider": market["provider"], "symbol": market["coin"], "interval": tf, "limit": lim}
            if end is not None:
                p["endTime"] = end
            res = self.request("candles.history", p) or []
            rows = []
            for c in res:
                if c.get("interval", tf) != tf:
                    continue
                rows.append((int(c["openTime"]), float(c["open"]), float(c["high"]), float(c["low"]),
                             float(c["close"]), int(c.get("closeTime") or 0)))
            fresh = [r for r in rows if r[0] not in out]
            for r in rows:
                out[r[0]] = r
            if not fresh:
                break
            end = min(r[0] for r in rows) - 1
        closed = [r for r in out.values() if (r[5] and r[5] < now) or (not r[5] and r[0] + tf_ms <= now)]
        return [r[:5] for r in sorted(closed)]


# ============================ WebSocket: datos en directo ============================
class LiveFeed:
    """Suscripción en directo a velas y precios. Avisa a los bots en cuanto cierra cada vela.
    Si se cae, los bots siguen funcionando consultando el historial (más lento, pero sin perder velas)."""

    PRICE_ORDER = ("mid", "last", "mark")

    def __init__(self, hub, log):
        self.hub, self.log, self.wsmod = hub, log, _wsmod()
        self.lock, self.send_lock = threading.Lock(), threading.Lock()
        self.want = set()
        self.active, self.by_id, self.sub_map, self.failed = {}, {}, {}, {}
        self.ws, self.next_id = None, 1
        self.connected, self.last_frame, self.since = False, 0, 0
        self.reconnects, self.err, self.events = 0, None, 0
        self.prices = {}      # (provider, coin) -> {kind: (precio, ms)}
        self.bars = {}        # (provider, coin, tf) -> {"bar": (t,o,h,l,c), "final": bool, "rx": ms}
        threading.Thread(target=self._run, daemon=True, name="feed").start()

    def set_wanted(self, keys):
        with self.lock:
            self.want = set(keys)
        if self.connected:
            try:
                self._sync()
            except Exception as e:
                self.log("Feed: no se pudo actualizar la suscripción: {}".format(e), "warn")

    def _nid(self):
        self.next_id += 1
        return self.next_id

    def _send(self, obj):
        with self.send_lock:
            self.ws.send(json.dumps(obj))

    def _sync(self):
        with self.lock:
            want, have = set(self.want), set(self.active)
        for k in sorted(want - have):
            if self.failed.get(k, (0,))[0] > time.time():
                continue
            rid = self._nid()
            if k[0] == "candles":
                payload = {"symbols": [k[2]], "providers": [k[1]], "intervals": [k[3]], "historyLimit": 0}
            else:
                payload = {"symbols": [k[2]], "providers": [k[1]]}
            with self.lock:
                self.active[k], self.by_id[rid] = rid, k
            self._send({"op": "sub", "id": rid, "channel": k[0], "payload": payload})
        for k in have - want:
            with self.lock:
                rid = self.active.pop(k)
                self.by_id.pop(rid, None)
            self._send({"op": "unsub", "id": rid})

    def _run(self):
        backoff = 2
        while True:
            try:
                self.ws = self.wsmod.create_connection(STREAM_URL, timeout=40, enable_multithread=True)
                with self.lock:
                    self.active, self.by_id, self.sub_map = {}, {}, {}
                self.connected, self.since, self.err = True, now_ms(), None
                self.last_frame = now_ms()
                self._sync()
                backoff = 2
                last_sync = time.time()
                while True:
                    try:
                        raw = self.ws.recv()
                    except self.wsmod.WebSocketTimeoutException:
                        if now_ms() - self.last_frame > 75_000:
                            raise ConnectionError("sin datos del servidor en 75 s")
                        self._send({"op": "req", "id": self._nid(), "method": "ping"})
                        continue
                    if not raw:
                        raise ConnectionError("conexión cerrada por el servidor")
                    self.last_frame = now_ms()
                    fr = json.loads(raw)
                    op = fr.get("op")
                    if op == "events":
                        for ev in fr.get("events") or []:
                            self._event(ev)
                    elif op == "sub_ok":
                        with self.lock:
                            self.sub_map[fr.get("sub")] = self.by_id.get(fr.get("id"))
                    elif op == "sub_err":
                        with self.lock:
                            k = self.by_id.pop(fr.get("id"), None)
                            if k:
                                self.active.pop(k, None)
                                self.failed[k] = (time.time() + 600, json.dumps(fr.get("error"))[:200])
                        self.err = "suscripción rechazada {}: {}".format(k, json.dumps(fr.get("error"))[:160])
                        self.log("Feed: " + self.err, "warn")
                    elif op == "end":
                        with self.lock:
                            k = self.by_id.pop(fr.get("id"), None)
                            if k:
                                self.active.pop(k, None)
                    elif op == "draining":
                        raise ConnectionError("el servidor renueva la conexión")
                    if time.time() - last_sync > 30:
                        self._sync()
                        last_sync = time.time()
            except Exception as e:
                was = self.connected
                self.connected = False
                self.err = str(e)
                try:
                    if self.ws:
                        self.ws.close()
                except Exception:
                    pass
                if was:
                    self.reconnects += 1
                    self.log("Feed en directo desconectado ({}). Reconectando…".format(e), "warn")
                time.sleep(backoff + random.random() * 2)
                backoff = min(backoff * 2, 60)

    def _event(self, ev):
        self.events += 1
        prov, sym = ev.get("provider"), ev.get("symbol")
        if "interval" in ev and "openTime" in ev:
            key = (prov, sym, ev["interval"])
            try:
                bar = (int(ev["openTime"]), float(ev["open"]), float(ev["high"]), float(ev["low"]), float(ev["close"]))
            except (KeyError, TypeError, ValueError):
                return
            prev = self.bars.get(key)
            final = bool(ev.get("isFinal"))
            self.bars[key] = {"bar": bar, "final": final, "rx": now_ms()}
            p = self.prices.setdefault((prov, sym), {})
            p["candle"] = (bar[4], now_ms())
            if final:
                self.hub.on_bar(key, bar)
            elif prev and prev["bar"][0] < bar[0] and not prev["final"]:
                self.hub.on_rollover(key)       # vela anterior cerrada sin aviso final: se lee del historial
        elif ev.get("kind") in self.PRICE_ORDER and "price" in ev:
            try:
                self.prices.setdefault((prov, sym), {})[ev["kind"]] = (float(ev["price"]), int(ev.get("time") or now_ms()))
            except (TypeError, ValueError):
                pass

    def price(self, prov, coin, max_age=120_000):
        d = self.prices.get((prov, coin)) or {}
        now = now_ms()
        for k in self.PRICE_ORDER + ("candle",):
            v = d.get(k)
            if v and now - v[1] < max_age:
                return v[0], v[1], k
        return None

    def live_bar(self, prov, coin, tf):
        b = self.bars.get((prov, coin, tf))
        if b and not b["final"] and now_ms() - b["rx"] < 120_000:
            return b["bar"]
        return None

    def status(self):
        return {"connected": self.connected, "since": self.since, "last_frame": self.last_frame,
                "reconnects": self.reconnects, "err": self.err, "subs": len(self.active), "events": self.events}


# ============================ catálogo e instrumentos ============================
MKT_KEYS = ("market_id", "provider", "symbol", "coin", "category", "trading_enabled")


def fetch_trend(req, market, P, span_ms, cap=20000):
    """Velas que necesitan los filtros de tendencia: {temporalidad: velas} (vacío si no hay filtro)."""
    out = {}
    for src, extra in K.tendencia_fuentes(P).items():
        out[src] = req.candles(market, src, min(int(span_ms / K.TF_MS[src]) + extra, cap))
    return out


def is_crypto(m):
    cat = str((m or {}).get("category") or "").lower()
    return "crypto" in cat or "cripto" in cat or (m or {}).get("provider") == "binance"


TV_RE = re.compile(r"^[A-Z0-9_]{1,20}:[A-Z0-9_.!&-]{1,40}$")
# Instrumentos de MFP sin el mismo mercado en TradingView: se muestra uno parecido (precio aproximado).
TV_APROX = {"veranta": "OANDA:{coin}", "ostium": {"DJI": "OANDA:US30USD"},
            "composite": {"XYZ100": "HIP3XYZ:XYZ100USDC.P"}}


def tv_auto(m):
    """Símbolo de TradingView del mismo mercado que usa MyFundedPerps. Devuelve (símbolo, exacto)."""
    prov = str((m or {}).get("provider") or "").lower()
    coin = str((m or {}).get("coin") or "").upper()
    if not coin:
        return None, False
    if prov == "binance":
        if not coin.endswith(("USDT", "USDC")):
            coin += "USDT"
        return "BINANCE:{}.P".format(coin), True
    if prov == "hyperliquid":
        dex, _, base = coin.rpartition(":")
        if dex == "XYZ":
            return "HIP3XYZ:{}USDC.P".format(base), True
        if not dex:
            return "HYPERLIQUID:{}USDC.P".format(base), True
        return None, False
    regla = TV_APROX.get(prov)
    if isinstance(regla, str):
        return regla.format(coin=coin), False
    if isinstance(regla, dict) and coin in regla:
        return regla[coin], False
    return None, False


class Markets:
    """Catálogo de MyFundedPerps (público) + lista de instrumentos añadidos por el usuario."""

    def __init__(self, rest):
        self.rest, self.data, self.t = rest, [], 0
        self.lock = threading.Lock()
        self.cambios = []          # mercados que MFP ha movido en su catálogo (se avisan desde la vigilancia)
        self.instr = self._load()

    def catalog(self, force=False):
        if force or not self.data or time.time() - self.t > 3600:
            data = as_list(self.rest.get("/v1/markets", auth=False))
            if data:
                self.data, self.t = data, time.time()
                try:
                    atomic_write("mercados.json", json.dumps(data, indent=1, ensure_ascii=False))
                except OSError:
                    pass
        return self.data

    def search(self, text):
        text = (text or "").strip().upper()
        out = []
        for m in self.catalog():
            hay = " ".join(str(m.get(k, "")) for k in ("market_id", "symbol", "coin", "category")).upper()
            if not text or text in hay:
                out.append({k: m.get(k) for k in MKT_KEYS})
        out.sort(key=lambda m: (str(m.get("symbol", "")).upper() != text, str(m.get("symbol", ""))))
        return out[:60]

    def _load(self):
        try:
            with open("instrumentos.json", encoding="utf-8") as f:
                d = json.load(f)
            if isinstance(d, list) and d:
                return d
        except (OSError, ValueError):
            pass
        return [{"symbol": s, "market_id": None, "cost_pct": K.AUTO_COST.get(s, 0.01), "builtin": True}
                for s in K.DEFAULT_SYMBOLS]

    def _save(self):
        atomic_write("instrumentos.json", json.dumps(self.instr, indent=1, ensure_ascii=False))

    def list(self):
        with self.lock:
            return [dict(i) for i in self.instr]

    def get(self, symbol):
        for i in self.instr:
            if i["symbol"] == symbol:
                return i
        return None

    def cost(self, symbol):
        i = self.get(symbol)
        return i.get("cost_pct") if i else None

    def add(self, market_id, symbol=None, cost=None):
        m = next((x for x in self.catalog(force=True) if x.get("market_id") == market_id), None)
        if m is None:
            raise ValueError("{} no está en el catálogo de MyFundedPerps".format(market_id))
        sym = (symbol or str(m.get("symbol") or m.get("coin") or "").split(":")[-1]).strip().upper()
        if not K.SYMBOL_RE.match(sym):
            raise ValueError("Nombre de instrumento no válido: {}".format(sym))
        with self.lock:
            if self.get(sym):
                raise ValueError("Ya tienes un instrumento llamado {}".format(sym))
            if any(i.get("market_id") == market_id for i in self.instr):
                raise ValueError("Ese mercado ya está en tu lista")
            c = float(cost) if cost not in (None, "") else (0.06 if is_crypto(m) else 0.01)
            if not 0 <= c <= 1:
                raise ValueError("La comisión debe estar entre 0 y 1 %")
            item = dict({k: m.get(k) for k in MKT_KEYS}, symbol=sym, cost_pct=c, builtin=False)
            item["market_symbol"] = m.get("symbol")
            self.instr.append(item)
            self._save()
            return dict(item)

    def remove(self, symbol, in_use):
        with self.lock:
            if symbol in in_use:
                raise ValueError("No puedes quitar un instrumento que está usando un bot")
            n = len(self.instr)
            self.instr = [i for i in self.instr if i["symbol"] != symbol]
            if len(self.instr) == n:
                raise ValueError("No existe {}".format(symbol))
            self._save()

    def set_cost(self, symbol, cost):
        c = float(cost)
        if not 0 <= c <= 1:
            raise ValueError("La comisión debe estar entre 0 y 1 %")
        with self.lock:
            i = self.get(symbol)
            if not i:
                raise ValueError("No existe {}".format(symbol))
            i["cost_pct"] = c
            self._save()

    def resolve(self, symbol):
        cat = self.catalog()
        i = self.get(symbol)
        if i is None:
            raise RuntimeError("{} no está en tu lista de instrumentos".format(symbol))
        def names(x):
            s_, c = str(x.get("symbol", "")).upper(), str(x.get("coin", "")).upper()
            return {s_, c, c.split(":")[-1]}
        cands = set(ALIASES.get(symbol, [symbol]))
        if i.get("market_id"):
            m = next((x for x in cat if x.get("market_id") == i["market_id"]), None)
            if m is not None:
                return {k: m.get(k) for k in MKT_KEYS}
            # MyFundedPerps ha cambiado el mercado de sitio (p. ej. el Nasdaq pasó de hyperliquid|xyz:XYZ100 a
            # composite|XYZ100): se busca el mismo instrumento por su nombre y se actualiza solo
            viejo = i["market_id"]
            cands |= {str(i.get("coin") or "").upper().split(":")[-1]} - {""}
            found = [x for x in cat if names(x) & cands]
            found.sort(key=lambda x: (x.get("trading_enabled") is False, x.get("provider") != "hyperliquid"))
            if not found:
                raise RuntimeError("El mercado {} ({}) ya no aparece en el catálogo de MyFundedPerps".format(viejo, symbol))
            m = found[0]
            with self.lock:
                i.update({k: m.get(k) for k in MKT_KEYS})
                i["market_id_anterior"] = viejo
                try:
                    self._save()
                except OSError:
                    pass
            self.cambios.append((symbol, viejo, m.get("market_id")))
            return {k: m.get(k) for k in MKT_KEYS}
        found = [x for x in cat if names(x) & cands]
        if not found:
            raise RuntimeError("No encuentro {} en el catálogo de MyFundedPerps (ver data/mercados.json)".format(symbol))
        found.sort(key=lambda x: (x.get("trading_enabled") is False, x.get("provider") != "hyperliquid"))
        m = found[0]
        with self.lock:
            i.update({k: m.get(k) for k in MKT_KEYS})
            i["symbol"] = symbol
            try:
                self._save()
            except OSError:
                pass
        return {k: m.get(k) for k in MKT_KEYS}


    # ---- TradingView: símbolo equivalente de cada instrumento (automático o elegido por el usuario) ----
    def _tv_load(self):
        try:
            with open("tradingview.json", encoding="utf-8") as f:
                d = json.load(f)
            return {str(k): str(v) for k, v in d.items()} if isinstance(d, dict) else {}
        except (OSError, ValueError):
            return {}

    def tradingview(self):
        propios = self._tv_load()
        out = []
        for i in self.list():
            sym = i["symbol"]
            try:
                m = self.resolve(sym) if not i.get("provider") else i
            except Exception:
                m = None
            auto, exacto = tv_auto(m)
            tv = propios.get(sym) or auto
            out.append({"symbol": sym, "tv": tv, "auto": auto, "propio": sym in propios,
                        "exacto": bool(exacto) and sym not in propios, "provider": (m or {}).get("provider"),
                        "coin": (m or {}).get("coin")})
        return out

    def set_tradingview(self, symbol, tv):
        if not self.get(symbol):
            raise ValueError("No existe {}".format(symbol))
        tv = str(tv or "").strip().upper()
        if tv and not TV_RE.match(tv):
            raise ValueError("Escribe el símbolo como en TradingView, con la bolsa delante: BINANCE:BTCUSDT.P")
        with self.lock:
            d = self._tv_load()
            if tv:
                d[symbol] = tv
            else:
                d.pop(symbol, None)
            atomic_write("tradingview.json", json.dumps(d, indent=1, ensure_ascii=False))


class Cache:
    """Velas cerradas guardadas en disco por símbolo y temporalidad (para los backtests del panel)."""

    def __init__(self, folder="cache"):
        self.folder = folder
        os.makedirs(folder, exist_ok=True)
        self.lock = threading.Lock()

    def path(self, sym, tf):
        return os.path.join(self.folder, "{}_{}.csv".format(sym, tf))

    def load(self, sym, tf):
        out = {}
        p = self.path(sym, tf)
        if os.path.exists(p):
            with open(p) as f:
                for row in csv.reader(f):
                    try:
                        out[int(row[0])] = (int(row[0]), float(row[1]), float(row[2]), float(row[3]), float(row[4]))
                    except (ValueError, IndexError):
                        pass
        return out

    def merge(self, sym, tf, candles):
        with self.lock:
            d = self.load(sym, tf)
            for c in candles:
                d[c[0]] = tuple(c)
            rows = [d[k] for k in sorted(d)][-200000:]
            tmp = self.path(sym, tf) + ".tmp"
            with open(tmp, "w", newline="") as f:
                csv.writer(f).writerows(rows)
            os.replace(tmp, self.path(sym, tf))
            return rows

    def append(self, sym, tf, c):
        with self.lock:
            with open(self.path(sym, tf), "a", newline="") as f:
                csv.writer(f).writerow(c)


class ConfigStore:
    def __init__(self, folder):
        self.folder = folder
        os.makedirs(os.path.join(folder, "configs"), exist_ok=True)

    def current(self):
        try:
            with open(os.path.join(self.folder, "config.json"), encoding="utf-8") as f:
                c = json.load(f)
            P, _ = K.validate(c.get("params"))
            c["params"] = P
            return c
        except (OSError, ValueError):
            return None

    def history(self):
        out, d = [], os.path.join(self.folder, "configs")
        for name in sorted(os.listdir(d)):
            if name.endswith(".json"):
                try:
                    with open(os.path.join(d, name), encoding="utf-8") as f:
                        out.append(json.load(f))
                except (OSError, ValueError):
                    pass
        return out

    def save(self, P, note=""):
        ids = [c.get("id", 0) for c in self.history()]
        c = {"id": (max(ids) if ids else 0) + 1, "saved_at": now_ms(), "note": note[:200], "params": P}
        atomic_write(os.path.join(self.folder, "configs", "{:04d}.json".format(c["id"])),
                     json.dumps(c, indent=1, ensure_ascii=False))
        atomic_write(os.path.join(self.folder, "config.json"), json.dumps(c, indent=1, ensure_ascii=False))
        return c


# ============================ un bot ============================
TRADE_COLS = ["n", "config", "simbolo", "tf", "lado", "apertura_utc", "cierre_utc", "apertura_ms", "cierre_ms",
              "entrada", "salida", "sl", "sl_final", "tp", "R", "resultado", "stop_pct", "duracion_min", "velas",
              "parcial_pct", "parcial_precio", "ampliaciones", "trailing", "dia", "usd", "mfe_r", "mae_r", "estrategia"]
EVENT_COLS = ["ts_vela_utc", "ms", "evento", "lado", "precio", "sl", "tp", "R", "detalle", "config"]
CANDLE_COLS = ["ts_vela_utc", "ms", "open", "high", "low", "close", "posicion", "dia_bloqueado", "config",
               "estrategia", "indicadores"]


class Engine:
    def __init__(self, hub, meta):
        self.hub, self.c, self.meta = hub, hub.c, meta
        self.id = meta["id"]
        self.dir = os.path.join("bots", self.id)
        os.makedirs(self.dir, exist_ok=True)
        self.log = Log(os.path.join(self.dir, "logs"), meta.get("name", self.id))
        self.store = ConfigStore(self.dir)
        cur = self.store.current()
        if cur is None:
            cur = self.store.save(K.defaults(), "valores por defecto")
        self.P, self.cfg_id, self.cfg_saved = cur["params"], cur["id"], cur.get("saved_at")
        self.strat_err = None               # error de la estrategia al procesar velas
        self.watch = {"nodata_since": None, "nodata_sent": False}
        self.tz = hub.tz
        self.hour_fn = hub.hour_fn
        self.lock = threading.RLock()
        self.q = queue.Queue()
        self.st = None
        self.ready = False
        self.reload = None
        self.stopping = False
        self.thread = None
        self.market = None
        self.trades = self._load_trades()
        self.events = deque(maxlen=150)
        self._load_events()
        self.chart = deque(maxlen=150)
        self.hist = deque(maxlen=hub.c.hist_n)
        self.htf_seed = {}
        s = self._load_state()
        self.saved = s
        self.started_at = s.get("started_at") or now_ms()
        self.restarts = (s.get("restarts", -1) + 1) if s else 0
        self.paused = bool(s.get("paused"))
        self.guard = s.get("guard")
        self.guard_base = s.get("guard_base", 0)
        self.cnt = dict(DEFAULT_CNT, **s.get("cnt", {}))
        self.cnt["filtered"] = dict(self.cnt.get("filtered") or {})
        self.last_t = s.get("last_t", 0)
        self.last_price = None
        self.data_ok_t, self.data_err, self.last_err_logged = 0, None, ("", 0)
        self.live_t = 0                     # última vela recibida en directo
        self.next_backfill = 0
        self.ex = E.Ejecuciones(self, s.get("exec"))   # réplica en sus cuentas (si el bot la tiene activada)

    def p(self, name):
        return os.path.join(self.dir, name)

    @property
    def name(self):
        return self.meta.get("name", self.id)

    def instr_cost(self, P):
        return self.hub.markets.cost(P["symbol"])

    @property
    def tf_ms(self):
        return K.TF_MS[self.P["timeframe"]]

    def dkey(self, t):
        return (t + int(self.P["utc_off"] * 3_600_000)) // 86_400_000

    def local(self, ms, f="%Y-%m-%d %H:%M"):
        return datetime.fromtimestamp(ms / 1000, self.tz).strftime(f)

    def feed_tf(self):
        return K.AGREGADAS.get(self.P["timeframe"], self.P["timeframe"])

    def feed_key(self):
        m = self.market
        return (m["provider"], m["coin"], self.feed_tf()) if m else None

    # ---------- hilo del bot ----------
    def start_thread(self):
        self.stopping = False
        self.thread = threading.Thread(target=self._loop, daemon=True, name="bot-" + self.id)
        self.thread.start()

    def stop(self):
        self.stopping = True
        self.q.put(("stop", None))

    def _loop(self):
        carry = None
        while not self.stopping:
            try:
                if self.reload:
                    carry = self.carry_for_reload()
                if not self.ready:
                    self.hub.feed_changed()
                    self.warm_up(carry)
                    carry = None
                    self.data_ok()
                    self.hub.feed_changed()
                    continue
                try:
                    item = self.q.get(timeout=20)
                except queue.Empty:
                    item = ("tick", None)
                items = [item]
                while True:
                    try:
                        items.append(self.q.get_nowait())
                    except queue.Empty:
                        break
                if any(i[0] == "stop" for i in items):
                    break
                bars = sorted({i[1][0]: i[1] for i in items if i[0] == "bar"}.values())
                need_hist = any(i[0] == "check" for i in items)
                for b in bars:
                    if b[0] <= self.last_t:
                        continue
                    if b[0] != self.last_t + self.tf_ms:
                        need_hist = True          # falta alguna vela: se rellena del historial
                        break
                    if self.reload:
                        break
                    self.live_t = b[0]
                    self.process(b)
                    self.data_ok()
                overdue = now_ms() - (self.last_t + 2 * self.tf_ms) > 25_000 and time.time() >= self.next_backfill
                if (need_hist or overdue) and not self.reload:
                    if self.backfill() == 0 and not need_hist:
                        self.next_backfill = time.time() + 60     # mercado cerrado o feed parado: no insistir
                    else:
                        self.next_backfill = 0
            except Exception as e:
                self.data_error(e)
                time.sleep(10)
        self.log("Bot detenido.")

    def warm_up(self, carry=None):
        P, hub = self.P, self.hub
        K.plugin_for(P)                      # si la estrategia no está instalada, el error se ve en el panel
        market = hub.markets.resolve(P["symbol"])
        tf_ms = K.TF_MS[P["timeframe"]]
        n = max(K.CALENTAR.get(P["timeframe"], 600), K.warmup_bars(P))
        last = self.saved.get("last_t") or 0
        if carry is None and last:
            missed = (hub.req.server_now() - last) // tf_ms
            if 0 < missed < 4000:
                n += int(missed)
        cs = hub.req.candles(market, P["timeframe"], n)
        if len(cs) < K.MIN_VELAS.get(P["timeframe"], 200):
            raise RuntimeError("solo hay {} velas de historial de {} {}".format(len(cs), P["symbol"], P["timeframe"]))
        htf = fetch_trend(hub.req, market, P, n * tf_ms)
        hub.cache.merge(P["symbol"], P["timeframe"], cs)
        self.start(cs, market, htf, carry)
        self.log("{} {}: calentamiento con {} velas{}. Bot en marcha{}.".format(
            P["symbol"], P["timeframe"], len(cs), "".join(" y {} velas de {}".format(len(v), k) for k, v in htf.items()),
            " (opera en: {})".format(", ".join(x["cuenta_nombre"] for x in self.ex.public()["cuentas"] if x["activo"]))
            if self.ex.activo() else
            " (en papel: {})".format(self.ex.public()["motivo_inactivo"]) if self.ex.configurado() else ""))
        with self.lock:
            self.ex.al_arrancar()

    def backfill(self):
        missed = (self.hub.req.server_now() - self.last_t) // self.tf_ms
        if missed < 2:
            return 0
        cs = self.hub.req.candles(self.market, self.P["timeframe"], int(max(5, min(missed + 3, 4500))))
        n = 0
        for c in cs:
            if c[0] > self.last_t:
                if self.reload:
                    break
                self.process(c)
                n += 1
        self.data_ok()
        return n

    # ---------- persistencia ----------
    def _load_state(self):
        try:
            with open(self.p("estado.json"), encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            return {}

    def _save_state(self):
        s = {"version": VERSION, "saved_at": now_ms(), "started_at": self.started_at, "restarts": self.restarts,
             "paused": self.paused, "guard": self.guard, "guard_base": self.guard_base, "cnt": self.cnt,
             "last_t": self.last_t, "cfg_id": self.cfg_id,
             "market": self.market.get("market_id") if self.market else self.saved.get("market"),
             "tf": self.P["timeframe"]}
        if self.st:
            s["strat"] = self.st.dump()
        elif self.saved.get("strat"):
            s["strat"] = self.saved["strat"]
        s["exec"] = self.ex.dump()
        try:
            atomic_write(self.p("estado.json"), json.dumps(s, indent=1, ensure_ascii=False))
        except OSError as e:
            self.log("No se pudo guardar el estado: {}".format(e), "error")

    def _load_events(self):
        if not os.path.exists(self.p("eventos.csv")):
            return
        try:
            with open(self.p("eventos.csv"), encoding="utf-8") as f:
                rows = deque(csv.DictReader(f), maxlen=self.events.maxlen)
            for r in rows:
                self.events.append({"t": int(r["ms"]), "kind": r["evento"], "side": r["lado"], "price": r["precio"],
                                    "sl": r["sl"], "tp": r["tp"], "r": r["R"], "info": r["detalle"]})
        except (OSError, KeyError, ValueError):
            pass

    def _load_trades(self):
        out = []
        if not os.path.exists(self.p("operaciones.csv")):
            return out
        with open(self.p("operaciones.csv"), encoding="utf-8") as f:
            for row in csv.DictReader(f):
                try:
                    tc = int(row["cierre_ms"])
                    d = (tc + int(self.P["utc_off"] * 3_600_000)) // 86_400_000
                    out.append({"n": int(row["n"]), "cfg": int(row["config"]), "symbol": row["simbolo"],
                                "tf": row["tf"], "side": 1 if row["lado"] == "LONG" else -1,
                                "t_open": int(row["apertura_ms"]), "t_close": tc, "entry": float(row["entrada"]),
                                "exit": float(row["salida"]), "sl": float(row["sl"]), "sl_final": float(row["sl_final"]),
                                "tp": float(row["tp"]) if row["tp"] else None, "r": float(row["R"]),
                                "why": row["resultado"], "stop_pct": float(row["stop_pct"]),
                                "dur": float(row["duracion_min"]), "bars": int(row["velas"]),
                                "part_pct": float(row["parcial_pct"] or 0),
                                "part_px": float(row["parcial_precio"]) if row["parcial_precio"] else None,
                                "ext_n": int(row.get("ampliaciones") or 0), "trail": row.get("trailing") == "1",
                                "mfe": float(row["mfe_r"]) if row.get("mfe_r") else None,
                                "mae": float(row["mae_r"]) if row.get("mae_r") else None,
                                "usd": float(row["usd"]) if row.get("usd") else None,
                                "estrategia": row.get("estrategia") or None,
                                "day": d, "week": wkey(d)})
                except (KeyError, ValueError):
                    continue
        return out

    def _migrate_trades_csv(self):
        """Los CSV de la 2.x no tienen las columnas nuevas: se reescriben una vez."""
        path = self.p("operaciones.csv")
        if not os.path.exists(path):
            return
        with open(path, encoding="utf-8") as f:
            header = f.readline().strip().split(",")
        if header == TRADE_COLS:
            return
        with open(path, encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
        tmp = path + ".tmp"
        with open(tmp, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(TRADE_COLS)
            for r in rows:
                w.writerow([r.get(c, "0" if c in ("ampliaciones", "trailing") else "") for c in TRADE_COLS])
        os.replace(tmp, path)

    # ---------- arranque, recarga y recálculo ----------
    def informe_estrategias(self):
        """Rendimiento de cada estrategia del bot: en directo (sus operaciones) y en su backtest de puntuación."""
        partes = K.partes_de(self.P)
        pu = self.meta.get("puntuaciones") or {}
        with self.lock:
            trades = list(self.trades)
            bloq = dict(self.cnt.get("bloqueadas") or {})
        cfgs = {c["id"]: (c.get("params") or {}).get("estrategia") for c in self.store.history()}
        de = lambda x: x.get("estrategia") or cfgs.get(x.get("cfg")) or self.P.get("estrategia")
        ids = self.prioridad() + sorted({de(x) for x in trades} - set(partes))
        out = []
        for sid in ids:
            tr = [x for x in trades if de(x) == sid]
            m = K.metrics(tr)
            usd = [x["usd"] for x in tr if x.get("usd") is not None]
            cum, eq = 0.0, []
            for x in tr:
                cum += x["r"]
                eq.append([x["t_close"], round(cum, 3)])
            bt = (pu.get("por") or {}).get(sid) if sid in (pu.get("partes") or []) else None
            out.append({"id": sid, "nombre": self.nombre_estrategia(sid), "activa": sid in partes, "tf": self.tf_estrategia(sid),
                        "prioridad": self.prioridad().index(sid) + 1 if sid in partes else None,
                        "vivo": {"n": m["n"], "winrate": round(m["winrate"], 1), "pf": round(m["pf"], 2) if m["pf"] else None,
                                 "total_r": round(m["total_r"], 2), "avg_r": round(m["avg_r"], 3),
                                 "max_dd": round(m["max_dd"], 2), "usd": round(sum(usd), 2) if usd else None,
                                 "best": round(m["best"], 2), "worst": round(m["worst"], 2)},
                        "equity": eq[-400:], "backtest": bt, "bloqueadas": bloq.get(sid, 0)})
        return {"ok": True, "combinada": bool(self.P.get("combinar")), "estrategias": out, "puntuado": pu.get("t"),
                "dias": pu.get("dias", PUNT_DIAS), "min_ops": PUNT_MIN_OPS,
                "calculando": self.hub.puntuando == self.id, "riesgo_usd": self.P.get("risk_usd")}

    def prioridad(self):
        """Orden de las estrategias combinadas: mejor puntuación primero (las que no tienen, al final)."""
        partes = K.partes_de(self.P)
        pu = (self.meta.get("puntuaciones") or {}).get("por") or {}
        val = lambda sid: (pu.get(sid) or {}).get("puntuacion")
        return sorted(partes, key=lambda sid: (val(sid) is None, -(val(sid) or 0), partes.index(sid)))

    def tf_estrategia(self, sid):
        """Temporalidad con la que trabaja una de las estrategias del bot (texto, p. ej. «30m»)."""
        partes = K.partes_de(self.P)
        if sid not in partes:
            return None
        ms = K.tf_de(self.P, sid, sid == partes[0])
        return next((k for k, v in K.TF_MS.items() if v == ms), self.P["timeframe"])

    def nombre_estrategia(self, sid):
        try:
            return K.plugin_for(sid).nombre
        except K.StrategyError:
            return sid or "?"

    def _new_strat(self, candles, htf):
        st = K.Motor(self.P, self.hour_fn, self.instr_cost(self.P))
        if hasattr(st.S, "ordenar"):
            st.S.ordenar(self.prioridad())
        st.en_vivo = True                    # el bot «Plan manual» solo actúa en directo, nunca en backtests
        if htf and candles:
            st.seed_htf(htf, candles[0][0])
        self.chart.clear()
        for c in candles:
            st.on_candle(c)
            self._chart_add(c, st)
        return st

    def start(self, candles, market, htf, carry=None):
        with self.lock:
            s = self.saved
            self.market = market
            self.htf_seed = htf or {}
            last = s.get("last_t") or 0
            resume = (carry is None and bool(last) and s.get("market") == market["market_id"]
                      and s.get("tf") == self.P["timeframe"] and s.get("strat")
                      and (s["strat"].get("estrategia_id") or K.DEFAULT_STRATEGY) == self.P["estrategia"]
                      and (s["strat"].get("partes") or [self.P["estrategia"]]) == K.partes_de(self.P))
            warm = [c for c in candles if c[0] <= last] if resume else candles
            if resume and len(warm) < 200:
                resume = False
                warm = candles
            st = self._new_strat(warm, htf)
            if resume:
                st.load(s["strat"])
                self.log("Estado recuperado (reinicio nº {}). Última vela: {} UTC".format(self.restarts, fmt_utc(last)))
            else:
                if carry:
                    # un bot «Plan manual» conserva su entrada pendiente (o su orden límite) si no cambian el
                    # símbolo ni la temporalidad: la decisión es del plan, no de los ajustes del bot
                    sigue = (self.P.get("estrategia") == PL.ESTRATEGIA_ID and carry.get("estrategia_id") == PL.ESTRATEGIA_ID
                             and carry.get("_simbolo_tf") == [self.P["symbol"], self.P["timeframe"]])
                    if not sigue:
                        carry["pending"] = None
                        carry["estrategia"] = st.S.guardar()      # la estrategia sigue con lo que ve el historial
                    elif carry.get("pending"):
                        self.log("Cambio de configuración: se conserva la entrada pendiente del plan manual.")
                    st.load(carry)
                else:
                    if last and s.get("strat", {}).get("pos"):
                        self.log("El estado guardado no se puede continuar: se descarta la operación abierta.", "warn")
                    st.reset_setups()
                    st.reset_day_state()
                    st.block_days = {}
                st.filtered, st.skipped = {}, 0
            self.st = st
            need = K.warmup_bars(self.P) + 200      # lo que pide la estrategia, aunque supere FP_HIST_N
            if self.hist.maxlen < need:
                self.hist = deque(maxlen=need)
            self.hist.clear()
            self.hist.extend(warm)
            self.last_t, self.last_price = warm[-1][0], warm[-1][4]
            self.saved = {}
            self.ready = True
            live = [c for c in candles if c[0] > self.last_t]
            if live:
                self.log("Procesando {} velas que llegaron mientras el bot estaba parado".format(len(live)))
                self.cnt["recovered"] += len(live)
            for c in live:
                self.process(c)
            self._save_state()

    def rebuild(self):
        old = self.st.dump()
        st = self._new_strat(list(self.hist), self.htf_seed)
        old["pending"] = None
        old["estrategia"] = st.S.guardar()
        st.load(old)
        p = st.pos
        if p and p["modo_tp"] == "rr":
            p["tp"] = p["entry"] + p["side"] * (st.S.rr() * p["dist"] + p.get("ext", 0.0))
        st.filtered, st.skipped = {}, 0
        self.st = st
        self.strat_err = None

    def apply_config(self, P, note):
        with self.lock:
            old = self.P
            c = self.store.save(P, note)
            seen, changed = set(), []
            for f in K.schema_for(P) + K.schema_for(old):
                if f["key"] not in seen and old.get(f["key"]) != P.get(f["key"]):
                    changed.append(f["label"])
                seen.add(f["key"])
            self.P, self.cfg_id, self.cfg_saved = P, c["id"], c["saved_at"]
            self.ex.sincronizar()
            t = self.last_t or now_ms()
            msg = "Configuración nº {} aplicada. Cambios: {}".format(c["id"], ", ".join(changed) or "ninguno")
            self.log(msg, "warn")
            self._event(t, "configuracion", info=msg[:300])
            reload_keys = K.plugin_for(P).reload_keys
            if any(old.get(k) != P.get(k) for k in reload_keys) or not self.st:
                market_change = any(old.get(k) != P.get(k) for k in K.MARKET_KEYS)
                self.reload = {"close": market_change}
                self.log("Recargando historial para la nueva configuración…")
                self.q.put(("check", None))
            elif self.st:
                self.rebuild()
            self._save_state()
            return c

    def carry_for_reload(self):
        with self.lock:
            st, info = self.st, self.reload or {}
            self.reload = None
            self.ready = False
            if st is None:
                return None
            if info.get("close") and st.pos:
                ev = []
                st._close(self.last_t, self.last_price, "AJUSTE", ev)
                for e in ev:
                    self._handle(e, self.last_t)
            d = st.dump()
            d["_simbolo_tf"] = [st.P.get("symbol"), st.P.get("timeframe")]     # con qué mercado se calculó
            if info.get("close"):
                d.update({"day": None, "dtrades": 0, "dconsec": 0, "dr": 0.0, "blocked": None, "cool": 0})
            return d

    def instrument_changed(self, symbol):
        with self.lock:
            if symbol == self.P["symbol"] and self.st and self.ready:
                self.rebuild()
                self.log("Comisión de {} actualizada: {:g} % ida y vuelta".format(symbol, self.instr_cost(self.P)))

    # ---------- vela a vela ----------
    def _chart_add(self, c, st):
        try:
            v = {k: round(x, 8) for k, x in (st.S.lineas() or {}).items() if isinstance(x, (int, float))}
        except Exception:
            v = {}
        self.chart.append({"t": c[0], "o": c[1], "h": c[2], "l": c[3], "c": c[4], "v": v})

    def process(self, c):
        with self.lock:
            st, t = self.st, c[0]
            if self.last_t:
                d0, d1 = self.dkey(self.last_t), self.dkey(t)
                if d1 > d0:
                    self._period_reports(d0, d1)
                if t - self.last_t > self.tf_ms * 3:
                    self.cnt["gaps"] += 1
            self._guard_autoresume(t)
            b0 = dict(getattr(st.S, "bloqueadas", None) or {})
            try:
                evs = st.on_candle(c)
                bq = self.cnt.setdefault("bloqueadas", {})
                for k, v in (getattr(st.S, "bloqueadas", None) or {}).items():
                    if v > b0.get(k, 0):
                        bq[k] = bq.get(k, 0) + v - b0.get(k, 0)
                if self.strat_err:
                    self.strat_err = None
                    self.log("La estrategia vuelve a funcionar.")
            except Exception as e:
                evs = []
                self._strategy_failed(e, t)
            self.hist.append(c)
            self.last_t, self.last_price = t, c[4]
            self._chart_add(c, st)
            for e in evs:
                self._handle(e, t)
            self.ex.after_candle(evs, t)
            self._write_candle(c, st)
            self._save_state()
            try:
                self.hub.cache.append(self.P["symbol"], self.P["timeframe"], c)
            except OSError:
                pass

    def _strategy_failed(self, e, t):
        import traceback
        tb = traceback.extract_tb(e.__traceback__)
        where = next(("línea {} de {}".format(fr.lineno, os.path.basename(fr.filename)) for fr in reversed(tb)
                      if os.path.dirname(os.path.abspath(fr.filename)) == STRAT_DIR), "")
        msg = "{}: {}{}".format(type(e).__name__, e, " ({})".format(where) if where else "")[:300]
        self.cnt["errors"] += 1
        if msg != self.strat_err:
            self.strat_err = msg
            self.log("ERROR EN LA ESTRATEGIA (vela {} UTC): {}".format(fmt_utc(t), msg), "error")
            self._event(t, "error_estrategia", info=msg)
            self.notify("problemas", "⚠️ <b>{}</b>: error en la estrategia\n<code>{}</code>\nEl bot sigue con las "
                        "velas siguientes; revisa el archivo de la estrategia.".format(A.esc(self.name), A.esc(msg)))

    # ---------- avisos ----------
    def info_line(self):
        st = self.st
        name = st.plugin.nombre if st else self.P.get("estrategia", "")
        return "{} {} · {}".format(self.P["symbol"], self.P["timeframe"], name)

    def decimals(self):
        return A.decimals(self.last_price)

    def chart_payload(self, badge, color, **kw):
        st = self.st
        sub = self.info_line()
        if st:
            try:
                extra = st.S.indicadores()
            except Exception:
                extra = ""
            if extra:
                sub += " · " + extra
        pl = {"title": self.name, "subtitle": sub, "badge": badge, "badge_color": color,
              "candles": list(self.chart)[-90:], "lineas": st.plugin.lineas if st else [],
              "decimals": self.decimals()}
        pl.update(kw)
        return pl

    def notify(self, kind, text, chart=None):
        av = self.hub.avisos
        if av and av.wants(kind, self.id):
            av.push(kind, text, chart if chart and av.wants_chart(kind) else None)

    def _late(self, t):
        """Texto de retraso si la vela se procesa tarde (por ejemplo, al recuperar velas tras un reinicio)."""
        close = t + self.tf_ms
        if now_ms() - close > max(90_000, self.tf_ms // 2):
            return "⏱ <i>Con retraso: vela de las {}</i>\n".format(self.local(t, "%H:%M"))
        return ""

    def _notify_event(self, kind, e, t, extra=None):
        av = self.hub.avisos
        if not av or not av.ready():
            return
        D = self.decimals()
        px = lambda v: A.fpx(v, D) if v is not None else "–"
        nm, line = A.esc(self.name), A.esc(self.info_line())
        if self.P.get("combinar") and self.st:
            ori = (extra or {}).get("estrategia") if kind == "exit" else (self.st.pending or self.st.pos or {}).get("origen")
            if ori:
                nm += " · 🧩 {} ({})".format(A.esc(self.nombre_estrategia(ori)), self.tf_estrategia(ori) or self.P["timeframe"])
        risk = self.P["risk_usd"]
        pct = lambda a, b: A.fpx(100 * a / b, 2) + " %" if b else "–"
        if kind == "signal":
            if self._late(t):
                return                      # una señal antigua (recuperada tras un reinicio) ya no sirve
            side, cl, sl = e[2], e[3], e[4]
            S = "LONG" if side == 1 else "SHORT"
            dist = abs(cl - sl)
            rr = self.st.S.rr() if self.st else None
            pend_tp = (self.st.pending or {}).get("tp") if self.st else None
            tp = pend_tp if pend_tp is not None else (cl + side * rr * dist if rr is not None else None)
            tp_txt = "TP ≈ {}{}".format(px(tp), " (RR {})".format(("%g" % rr).replace(".", ",")) if rr is not None
                                        and pend_tp is None else "") \
                if tp is not None else "TP dinámico"
            text = ("{}🔔 <b>Señal {}</b> · {}\n{}\nEntra en la apertura de la vela de las {}\n"
                    "Precio ≈ {} · SL {} ({} · {})\n{}{}").format(
                self._late(t), S, nm, line, self.local(t + self.tf_ms, "%H:%M"), px(cl), px(sl), px(dist),
                pct(dist, cl), tp_txt, ("\n📈 " + A.esc(self.htf_text()) if self.htf_text() else "") +
                ("\n🔄 Señal invertida ({})".format(self.P.get("inv")) if self.P.get("inv", "no") != "no" else ""))
            chart = self.chart_payload(("▲ " if side == 1 else "▼ ") + "SEÑAL " + S, "gold",
                                       signal={"side": side, "price": cl, "sl": sl, "tp": tp})
            self.notify("senal", text, chart)
        elif kind == "open":
            side, o, sl, tp = e[2], e[3], e[4], e[5]
            S = "LONG" if side == 1 else "SHORT"
            dist = abs(o - sl)
            p = self.st.pos or {}
            tp_txt = "TP {} (+{} R)".format(px(tp), A.fpx(abs(tp - o) / dist, 2)) if tp is not None and dist else "TP –"
            if p.get("modo_tp") == "dinamico":
                tp_txt += " · dinámico"
            text = ("{}{} <b>{} abierto</b> · {}\n{}\nEntrada {}\nSL {} (−{} · {})\n{}{}").format(
                self._late(t), "🟢" if side == 1 else "🔴", S, nm, line, px(o), px(sl), px(dist),
                pct(dist, o), tp_txt, "\nRiesgo: {:,.0f} $".format(risk).replace(",", ".") if risk else "")
            chart = self.chart_payload(("▲ " if side == 1 else "▼ ") + S + " ABIERTO", "gain" if side == 1 else "loss",
                                       pos={"side": side, "entry": o, "sl": sl, "tp": tp, "t_open": t})
            self.notify("apertura", text, chart)
        elif kind == "exit":
            x = extra
            S = "LONG" if x["side"] == 1 else "SHORT"
            why = self.why_names().get(x["why"], x["why"])
            d = self.dkey(t)
            today = sum(y["r"] for y in self.trades if y["day"] == d)
            week = sum(y["r"] for y in self.trades if y["week"] == wkey(d))
            usd = " ({} $)".format(A.fr(x["r"] * risk, 0)) if risk else ""
            text = ("{}{} <b>{} cerrado: {}</b> · {}\n{}\nEntrada {} → salida {}\n<b>Resultado {} R</b>{}\n"
                    "Duración {:.0f} min{}\nHoy {} R · semana {} R · total {} R").format(
                self._late(t), "✅" if x["r"] > 0 else "❌", S, A.esc(why), nm, line, px(x["entry"]), px(x["exit"]),
                A.fr(x["r"]), usd, x["dur"], " · parcial {} % antes".format(A.fpx(x["part_pct"], 0)) if x.get("part_pct") else "",
                A.fr(today), A.fr(week), A.fr(sum(y["r"] for y in self.trades)))
            chart = self.chart_payload("{} {} R".format(why.upper(), A.fr(x["r"])), "gain" if x["r"] > 0 else "loss",
                                       trade=dict(x))
            self.notify("cierre", text, chart)
        elif kind in ("partial", "extend", "trail_on"):
            p = self.st.pos
            S = "LONG" if e[2] == 1 else "SHORT"
            if kind == "partial":
                text = "🛡 <b>Break-even</b> · {} ({})\n{}vende {:.0f} % a {} y sube el SL a {}".format(
                    nm, S, "" if e[4] else "", e[4] * 100, px(e[3]), px(e[5])) if e[4] else \
                    "🛡 <b>Break-even</b> · {} ({})\nSL a {}".format(nm, S, px(e[5]))
            elif kind == "extend":
                text = "📈 <b>Ampliación nº {}</b> · {} ({})\nTP {} · SL {}".format(e[5], nm, S, px(e[3]), px(e[4]))
            else:
                text = "🎯 <b>Trailing activado</b> · {} ({})".format(nm, S)
            chart = self.chart_payload(text.split("</b>")[0].split("<b>")[1].upper(), "gold",
                                       pos={"side": p["side"], "entry": p["entry"], "sl": p["sl"], "tp": p.get("tp"),
                                            "t_open": p["t_open"]}) if p else None
            self.notify("gestion", self._late(t) + text, chart)
        elif kind == "blocked":
            self.notify("limites", "⏸ <b>{}</b>: límite diario de la estrategia ({}).\nSin entradas hasta el día "
                        "siguiente.".format(nm, A.esc(e[2])))

    def why_names(self):
        out = dict(WHY_ES)
        if self.st:
            out.update(self.st.plugin.salidas)
        return out

    def _event(self, t, kind, side="", price="", sl="", tp="", r="", info=""):
        ev = {"t": t, "kind": kind, "side": side, "price": price, "sl": sl, "tp": tp, "r": r, "info": info}
        self.events.append(ev)
        append_csv(self.p("eventos.csv"), EVENT_COLS, [fmt_utc(t), t, kind, side, price, sl, tp, r, info, self.cfg_id])

    def _handle(self, e, t):
        st, kind = self.st, e[0]
        S = lambda s: "LONG" if s == 1 else "SHORT"
        if kind == "skip":
            self.cnt["skipped_cost"] += 1
            self._event(t, "descartada_coste", info="coste {:.2f}R > {}R".format(e[2], self.P["max_cost"]))
            self.log("Señal descartada: el coste serían {:.2f}R".format(e[2]))
        elif kind == "blocked":
            self.cnt["day_blocks"] += 1
            self._event(t, "limite_estrategia", info=e[2])
            self.log("Límite diario de la estrategia: {}. Sin entradas hasta el día siguiente.".format(e[2]), "warn")
            self._notify_event("blocked", e, t)
        elif kind == "filtered":
            why = st.filter_names.get(e[3], e[3])
            self.cnt["filtered"][e[3]] = self.cnt["filtered"].get(e[3], 0) + 1
            self._event(t, "señal_filtrada", S(e[2]), info=why)
            self.log("Señal {} filtrada: {}".format(S(e[2]), why))
        elif kind == "signal":
            self.cnt["signals"] += 1
            why = "entradas pausadas a mano" if self.paused else "bot detenido por límite" if self.guard else None
            if not why:
                ok, why_g = self.hub.riesgo.permitir(self)
                why = None if ok else why_g
            if not why:
                ok, why_n = self.hub.noticias.permitir(self, t + self.tf_ms)   # la entrada es en la apertura siguiente
                why = None if ok else why_n
            if why:
                st.pending = None
                self.cnt["ignored"] += 1
                self._event(t, "señal_ignorada", S(e[2]), e[3], e[4], info=why)
                self.log("Señal {} ignorada ({})".format(S(e[2]), why))
            elif (st.pending or {}).get("limite") is not None:
                # orden límite (planes manuales): queda puesta hasta que el precio la toque. Solo en papel por ahora
                pend = st.pending
                self._event(t, "orden_limite", S(e[2]), pend["limite"], e[4], pend.get("tp") or "")
                self.log("ORDEN LÍMITE {} en {:.5g} SL {:.5g} TP {}".format(
                    S(e[2]), pend["limite"], e[4], "{:.5g}".format(pend["tp"]) if pend.get("tp") is not None else "–"))
                D = self.decimals()
                px = lambda v: A.fpx(v, D) if v is not None else "–"
                en_cuenta = self.ex.activo() if hasattr(self.ex, "activo") else False
                self.notify("senal", "📌 <b>Orden límite {}</b> · {}\n{}\nLímite {} · SL {} · TP {}{}".format(
                    S(e[2]), A.esc(self.name), A.esc(self.info_line()), px(pend["limite"]), px(e[4]),
                    px(pend.get("tp")), "\n👁 Vigilada por KaBor: al tocar el límite se envía a mercado a la cuenta."
                    if en_cuenta else ""))
            else:
                self._event(t, "señal", S(e[2]), e[3], e[4])
                self._ctx_global(st)
                self.log("SEÑAL {} (entra en la próxima apertura) SL {:.5g}".format(S(e[2]), e[4]))
                self._notify_event("signal", e, t)
                self.ex.on_signal(t, e[2], e[3], e[4], (st.pending or {}).get("tp"))
        elif kind == "open":
            tp = "{:.5g}".format(e[5]) if e[5] is not None else "dinámico"
            self._event(t, "apertura", S(e[2]), e[3], e[4], e[5] if e[5] is not None else "")
            self.log("APERTURA {} @ {:.5g} SL {:.5g} TP {}".format(S(e[2]), e[3], e[4], tp))
            self._notify_event("open", e, t)
        elif kind == "partial":
            self.cnt["partials"] += 1
            self._event(t, "parcial", S(e[2]), e[3], e[5], info="vende {:.0f}% y SL a {:.5g}".format(e[4] * 100, e[5]))
            self.log("PARCIAL {} @ {:.5g}: vende {:.0f}% y sube el SL a {:.5g}".format(S(e[2]), e[3], e[4] * 100, e[5]))
            self._notify_event("partial", e, t)
        elif kind == "extend":
            self.cnt["extensions"] += 1
            self._event(t, "ampliacion", S(e[2]), sl=e[4], tp=e[3], info="ampliación nº {}".format(e[5]))
            self.log("AMPLIACIÓN nº {} {}: TP {:.5g}, SL {:.5g}".format(e[5], S(e[2]), e[3], e[4]))
            self._notify_event("extend", e, t)
        elif kind == "trail_on":
            self._event(t, "trailing_activo", S(e[2]))
            self.log("Trailing stop activado ({})".format(S(e[2])))
            self._notify_event("trail_on", e, t)
        elif kind == "exit":
            x = dict(e[2])
            ctx = x.pop("ctx", None)
            d = self.dkey(t)
            x.update({"n": len(self.trades) + 1, "cfg": self.cfg_id, "symbol": self.P["symbol"],
                      "tf": self.P["timeframe"], "r": round(x["r"], 3), "stop_pct": round(x["stop_pct"], 4),
                      "dur": round((t + self.tf_ms - x["t_open"]) / 60000, 1), "day": d, "week": wkey(d)})
            usd = round(x["r"] * self.P["risk_usd"], 2) if self.P["risk_usd"] else ""
            x["usd"] = usd if usd != "" else None
            self.trades.append(x)
            append_csv(self.p("operaciones.csv"), TRADE_COLS, [
                x["n"], x["cfg"], x["symbol"], x["tf"], S(x["side"]), fmt_utc(x["t_open"]), fmt_utc(t), x["t_open"], t,
                x["entry"], x["exit"], x["sl"], x["sl_final"], x["tp"] if x["tp"] is not None else "", x["r"], x["why"],
                x["stop_pct"], x["dur"], x["bars"], x["part_pct"], x["part_px"] if x["part_px"] is not None else "",
                x.get("ext_n", 0), 1 if x.get("trail") else 0, day_label(d), usd,
                x["mfe"] if x.get("mfe") is not None else "", x["mae"] if x.get("mae") is not None else "",
                x.get("estrategia") or self.P.get("estrategia", "")])
            if ctx:
                try:
                    CX.escribir(self.p(CX.ARCHIVO), CX.fila(dict(x, ctx=ctx), self.name, self.P.get("estrategia", "")))
                except Exception as err:          # el registro de contexto nunca puede parar al bot
                    self.log("No se pudo anotar el contexto de la operación: {}".format(err), "error")
            self._event(t, "cierre", S(x["side"]), x["exit"], r=x["r"], info=x["why"])
            self.log("CIERRE {} @ {:.5g} ({}) {:+.2f}R".format(S(x["side"]), x["exit"], x["why"], x["r"]))
            self._notify_event("exit", e, t, x)
            self._guard_check(t)
            self.hub.riesgo.revisar()

    def _ctx_global(self, st):
        """Añade a la foto de la señal lo que solo se sabe en directo: el riesgo global."""
        ctx = (st.pending or {}).get("ctx")
        if ctx is None:
            return
        try:
            rg = self.hub.riesgo
            ctx["g_abiertas"] = rg.abiertas(excluir=self)
            ctx["g_dia"] = rg.valores().get("día")
        except Exception:
            pass

    def _write_candle(self, c, st):
        ind = dict(self.chart[-1]["v"]) if self.chart and self.chart[-1]["t"] == c[0] else {}
        if st.htf:
            ind.update({"rsi_mayor": st.htf.v_rsi, "ema_mayor": st.htf.v_ema})
        ind = {k: (round(v, 6) if isinstance(v, float) else v) for k, v in ind.items()}
        pos = ("LONG" if st.pos["side"] == 1 else "SHORT") if st.pos else ""
        append_csv(self.p("velas.csv"), CANDLE_COLS, [
            fmt_utc(c[0]), c[0], c[1], c[2], c[3], c[4], pos, st.blocked or "", self.cfg_id, st.plugin.id,
            json.dumps(ind, separators=(",", ":"))])

    def _migrate_candles_csv(self):
        """velas.csv de la 3.x tiene otras columnas: se guarda aparte y se empieza uno nuevo."""
        path = self.p("velas.csv")
        if not os.path.exists(path):
            return
        with open(path, encoding="utf-8") as f:
            header = f.readline().strip().split(",")
        if header != CANDLE_COLS:
            os.replace(path, self.p("velas_v3.csv" if "kc_ext_sup" in header else
                                    "velas_{}.csv".format(datetime.now().strftime("%Y%m%d_%H%M"))))

    # ---------- guardián ----------
    def _unit(self, r):
        return r * self.P["risk_usd"] if self.P["g_unit"] == "USD" else r

    def limits(self):
        P = self.P
        return [("semana", "pérdida", P["g_week_loss"]), ("semana", "ganancia", P["g_week_profit"]),
                ("día", "pérdida", P["g_day_loss"]), ("día", "ganancia", P["g_day_profit"])]

    def period_value(self, scope, key):
        k = "week" if scope == "semana" else "day"
        return self._unit(sum(x["r"] for x in self.trades if x[k] == key and x["t_close"] >= self.guard_base))

    def _guard_check(self, t):
        if self.guard:
            return
        d = self.dkey(t)
        for scope, kind, lim in self.limits():
            if not lim:
                continue
            key = wkey(d) if scope == "semana" else d
            v = self.period_value(scope, key)
            if (kind == "pérdida" and v <= -lim) or (kind == "ganancia" and v >= lim):
                u = self.P["g_unit"]
                self.guard = {"scope": scope, "kind": kind, "key": key, "value": round(v, 2), "limit": lim, "unit": u,
                              "t": t, "action": self.P["g_action"]}
                if self.st:
                    self.st.pending = None
                self.cnt["guard_stops"] += 1
                what = ("hasta el día siguiente" if scope == "día" else "hasta la semana siguiente") \
                    if self.P["g_action"] == "pausa" else "hasta que lo reactives desde el panel"
                msg = "BOT DETENIDO: {} {} de {:+.2f}{} (límite {}{}). Sin entradas {}.".format(
                    kind, "diaria" if scope == "día" else "semanal", v, u, lim, u, what)
                self.log(msg, "warn")
                self._event(t, "bot_detenido", info=msg)
                self.notify("limites", "🛑 <b>{}</b>\n{}".format(A.esc(self.name), A.esc(msg)))
                self.write_report_file("parada_" + fmt_utc(t, "%Y-%m-%d_%H%M"), "Informe en la parada por límite",
                                       self.trades)
                return

    def _guard_autoresume(self, t):
        g = self.guard
        if not g or g.get("action") != "pausa":
            return
        d = self.dkey(t)
        if (wkey(d) if g["scope"] == "semana" else d) > g["key"]:
            self.guard = None
            self.log("Nuevo periodo: el bot vuelve a buscar entradas.")
            self._event(t, "bot_reanudado", info="reanudación automática")
            self.notify("limites", "▶️ <b>{}</b>: nuevo periodo, vuelve a buscar entradas.".format(A.esc(self.name)))

    def control(self, action):
        with self.lock:
            t = self.last_t or now_ms()
            if action == "pausar":
                self.paused = True
                if self.st:
                    self.st.pending = None
                self._event(t, "pausa_manual")
                self.log("Entradas pausadas desde el panel.")
            elif action == "reanudar":
                self.paused = False
                self._event(t, "reanudar_manual")
                self.log("Entradas reanudadas desde el panel.")
            elif action == "reactivar":
                if not self.guard:
                    return False, "El bot no está detenido por límite."
                self.guard = None
                self.guard_base = self.last_t + 1
                self._event(t, "bot_reactivado", info="reactivado a mano; los límites cuentan desde ahora")
                self.log("Bot reactivado desde el panel. Los límites del periodo cuentan desde ahora.", "warn")
            else:
                return False, "Acción desconocida"
            self._save_state()
            return True, "ok"

    # ---------- gestión manual de la posición ----------
    def gestion_manual(self, op, valor=None):
        """Desde el panel: stop a break-even, mover el stop o el objetivo, venta parcial o cierre. Se aplica al modelo
        del bot y, si opera en cuenta, a la posición real (stop y objetivo en la cuenta, cierre a mercado)."""
        with self.lock:
            st = self.st
            p = st.pos if st else None
            if not p:
                return False, "El bot no tiene ninguna posición abierta"
            lp = self.live_price()
            px = float(lp[0]) if lp and lp[0] else float(self.last_price or 0)
            if not px:
                return False, "Sin precio actual: inténtalo en unos segundos"
            s, t, D = p["side"], now_ms(), self.decimals()
            fx = lambda v: A.fpx(v, D)
            lado = "por debajo" if s == 1 else "por encima"
            if op == "be":
                if (px - p["entry"]) * s <= 0:
                    return False, "El precio está en contra de la entrada: el stop a break-even saltaría al momento"
                p["sl"] = p["entry"]
                txt = "Stop a break-even ({})".format(fx(p["entry"]))
            elif op == "sl":
                v = float(valor)
                if (px - v) * s <= 0:
                    return False, "El stop tiene que quedar {} del precio actual ({})".format(lado, fx(px))
                p["sl"] = v
                txt = "Stop movido a {}".format(fx(v))
            elif op == "tp":
                v = None if valor in (None, "") else float(valor)
                if v is not None and (v - px) * s <= 0:
                    return False, "El objetivo tiene que quedar {} del precio actual ({})".format(
                        "por encima" if s == 1 else "por debajo", fx(px))
                p["tp"], p["modo_tp"], p["tp_manual"], p["no_tp"] = v, "fijo", True, v is None
                txt = "Objetivo movido a {}".format(fx(v)) if v is not None else "Objetivo quitado"
            elif op == "parcial":
                f = float(valor) / 100
                if not 0.05 <= f <= 0.9:
                    return False, "La venta parcial va del 5 al 90 %"
                if p.get("part"):
                    return False, "En esta operación ya se hizo una venta parcial"
                p["part"] = (f, px)
                txt = "Venta parcial del {:.0f} % a {}".format(f * 100, fx(px))
            elif op == "cerrar":
                txt = "Cierre manual a {}".format(fx(px))
            else:
                return False, "Acción desconocida"
            errores = []
            if self.ex.activo():
                for x in list(getattr(self.ex, "ex", {}).values()):
                    if not getattr(x, "pos", None):
                        continue
                    try:
                        rp = x.tr.position(x.acc, self.market["market_id"])
                        if not rp:
                            continue
                        if op in ("be", "sl", "tp"):
                            x._sync_exits(rp, p["sl"], p.get("tp"), t)
                        elif op == "parcial":
                            x._cerrar(rp, "venta parcial manual", fraction=f, t=t)
                            x.pos["partial"] = f
                        elif op == "cerrar":
                            x._cerrar(rp, "cierre manual", t=t)
                            x._registrar_cierre(t)
                    except Exception as e:
                        errores.append(str(e))
            if op == "cerrar":
                for e in st.cerrar_manual(t, px):
                    self._handle(e, t)
            self._event(t, "manual", "LONG" if s == 1 else "SHORT", px, p.get("sl", ""), p.get("tp") or "", info=txt)
            self.log("Gestión manual: {}.".format(txt) + (" Cuenta: " + "; ".join(errores) if errores else ""),
                     "warn" if errores else "info")
            if op != "cerrar" or errores:             # el cierre ya lo avisa la salida normal del bot
                self.notify("cierre" if op == "cerrar" else "senal", "✋ <b>{}</b> · {}{}".format(
                    txt, A.esc(self.name), "\n⚠️ Cuenta: " + A.esc("; ".join(errores)) if errores else ""))
            self._save_state()
            if errores:
                return False, "Hecho en el bot, pero la cuenta dio error: " + "; ".join(errores)
            return True, txt

    # ---------- informes ----------
    def _period_reports(self, d0, d1):
        tr = [x for x in self.trades if x["day"] == d0]
        self.write_report_file("diario_" + day_label(d0), "Informe diario {} · {}".format(self.name, day_label(d0)), tr)
        if wkey(d1) > wkey(d0):
            trw = [x for x in self.trades if x["week"] == wkey(d0)]
            self.write_report_file("semanal_" + day_label(wkey(d0) * 7 - 3),
                                   "Informe semanal {} · {}".format(self.name, week_label(wkey(d0))), trw)

    def write_report_file(self, name, title, trades):
        try:
            os.makedirs(self.p("informes"), exist_ok=True)
            atomic_write(os.path.join(self.p("informes"), name + ".md"), self.report_md(title, trades))
        except Exception as e:
            self.log("No se pudo escribir el informe {}: {}".format(name, e), "error")

    def report_md(self, title, trades):
        P = self.P
        m = K.metrics(trades)
        mk = self.market or {}
        try:
            pl = K.plugin_for(P)
            strat = "{} v{} ({})".format(pl.nombre, pl.version, pl.file)
        except K.StrategyError:
            strat = P.get("estrategia", "?") + " (no instalada)"
        L = ["# " + title, "", "Generado: {} (hora local {})".format(self.local(now_ms()), self.c.local_tz),
             "Bot «{}» (en papel, programa v{}). Estrategia: {}. Mercado actual: {} {} (MyFundedPerps {} / {}). "
             "Configuración activa: nº {}.".format(self.name, VERSION, strat, P["symbol"], P["timeframe"],
                                                   mk.get("provider", "?"), mk.get("coin", "?"), self.cfg_id),
             "Prueba iniciada: {}.".format(self.local(self.started_at)), "", "## Configuración activa", "",
             "| Grupo | Ajuste | Valor |", "|---|---|---|"]
        for f in K.schema_for(P):
            v = P.get(f["key"])
            if v != f["default"] or f["group"] in ("Mercado",):
                L.append("| {} | {} | {} |".format(f["group"], f["label"], "sí" if v is True else "no" if v is False else v))
        L += ["", "Solo se listan los ajustes distintos del valor por defecto (y el mercado).", "",
              "## Resultados", "", "| Métrica | Valor |", "|---|---|"]
        rows = [("Operaciones", m["n"]), ("Ganadoras / perdedoras", "{} / {}".format(m["wins"], m["losses"])),
                ("% acierto", "{:.1f}%".format(m["winrate"])), ("R total", "{:+.2f}".format(m["total_r"])),
                ("R medio", "{:+.3f}".format(m["avg_r"])),
                ("Ganancia media / pérdida media", "{:+.2f}R / {:+.2f}R".format(m["avg_win"], m["avg_loss"])),
                ("Profit factor", "{:.2f}".format(m["pf"]) if m["pf"] is not None else "n/d"),
                ("Drawdown máx. / actual", "{:.2f}R / {:.2f}R".format(m["max_dd"], m["cur_dd"])),
                ("Mejor / peor", "{:+.2f}R / {:+.2f}R".format(m["best"], m["worst"])),
                ("Rachas máx. ganadora / perdedora", "{} / {}".format(m["max_win_streak"], m["max_loss_streak"])),
                ("SQN", "{:.2f}".format(m["sqn"]) if m["sqn"] is not None else "n/d"),
                ("Duración media", "{:.0f} min".format(m["avg_dur"])),
                ("Salidas", ", ".join("{} {}".format(self.why_names().get(k, k), v)
                                      for k, v in sorted(m["why"].items())) or "-"),
                ("Con venta parcial / con ampliación", "{} / {}".format(m["partials"], m["extended"]))]
        if P["risk_usd"]:
            rows.append(("Resultado en dinero", "{:+,.2f} $".format(m["total_r"] * P["risk_usd"])))
        L += ["| {} | {} |".format(a, b) for a, b in rows]
        if trades:
            cfgs = sorted({t.get("cfg", 0) for t in trades})
            if len(cfgs) > 1:
                L += ["", "## Por configuración", "", "| Config | Ops | % acierto | R |", "|---|---|---|---|"]
                for cid in cfgs:
                    x = K.metrics([t for t in trades if t.get("cfg") == cid])
                    L.append("| {} | {} | {:.0f}% | {:+.2f} |".format(cid, x["n"], x["winrate"], x["total_r"]))
            L += ["", "## Largos y cortos", "", "| Lado | Ops | % acierto | R |", "|---|---|---|---|"]
            for side, v in (("LONG", 1), ("SHORT", -1)):
                x = K.metrics([t for t in trades if t["side"] == v])
                L.append("| {} | {} | {:.0f}% | {:+.2f} |".format(side, x["n"], x["winrate"], x["total_r"]))
            L += ["", "## Por día", "", "| Día | Ops | % acierto | R |", "|---|---|---|---|"]
            for d in sorted({t["day"] for t in trades}):
                x = K.metrics([t for t in trades if t["day"] == d])
                L.append("| {} | {} | {:.0f}% | {:+.2f} |".format(day_label(d), x["n"], x["winrate"], x["total_r"]))
            L += ["", "## Operaciones", "",
                  "| # | Cfg | Símbolo | Lado | Apertura (local) | Entrada | SL inicial | SL final | Salida | Motivo | "
                  "Parcial | Ampl. | R | Min |", "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
            for t in trades:
                L.append("| {} | {} | {} {} | {} | {} | {:.5g} | {:.5g} | {:.5g} | {:.5g} | {} | {} | {} | {:+.2f} | "
                         "{:.0f} |".format(
                             t["n"], t.get("cfg", ""), t.get("symbol", ""), t.get("tf", ""),
                             "LONG" if t["side"] == 1 else "SHORT", self.local(t["t_open"]), t["entry"], t["sl"],
                             t.get("sl_final", t["sl"]), t["exit"], self.why_names().get(t["why"], t["why"]),
                             "{:g}% @ {:.5g}".format(t["part_pct"], t["part_px"]) if t.get("part_pct") else "-",
                             t.get("ext_n", 0) or "-", t["r"], t["dur"]))
        k = self.cnt
        L += ["", "## Señales y calidad de datos", "",
              "Señales: {} | ignoradas por pausa o parada: {} | descartadas por coste: {} | días bloqueados por la "
              "estrategia: {} | paradas del guardián: {}".format(k["signals"], k["ignored"], k["skipped_cost"],
                                                                k["day_blocks"], k["guard_stops"]),
              "Señales filtradas: " + (", ".join("{} {}".format(self.filter_names().get(a, a), b)
                                                 for a, b in sorted(k["filtered"].items())) or "ninguna"),
              "Reinicios: {} | huecos de datos: {} | velas recuperadas: {} | errores: {}".format(
                  self.restarts, k["gaps"], k["recovered"], k["errors"]), ""]
        return "\n".join(L)

    def zip_into(self, z, prefix):
        with self.lock:
            z.writestr(prefix + "informe_completo.md", self.report_md("Informe completo · " + self.name, self.trades))
            z.writestr(prefix + "resumen.json", json.dumps(self.summary(), indent=1, ensure_ascii=False, default=str))
        for f in ("operaciones.csv", "ejecuciones.csv", CX.ARCHIVO, "eventos.csv", "velas.csv", "velas_v3.csv", "estado.json",
                  "config.json"):
            if os.path.exists(self.p(f)):
                z.write(self.p(f), prefix + f)
        for folder in ("informes", "logs", "configs"):
            d = self.p(folder)
            if os.path.isdir(d):
                for f in sorted(os.listdir(d)):
                    z.write(os.path.join(d, f), prefix + folder + "/" + f)

    # ---------- estado ----------
    def data_ok(self):
        self.data_ok_t, self.data_err = now_ms(), None

    def data_error(self, e):
        with self.lock:
            self.cnt["errors"] += 1
            msg = str(e)
            self.data_err = msg
            last_msg, last_t = self.last_err_logged
            if msg != last_msg or time.time() - last_t > 600:
                self.log("Error de datos: " + msg, "error")
                self.last_err_logged = (msg, time.time())

    def status(self, now):
        if self.meta.get("archived"):
            return "archivado", "Archivado: no busca entradas"
        if self.strat_err:
            return "error", "Error en la estrategia: " + self.strat_err
        if not self.ready:
            if self.data_err and "estrategia" in self.data_err:
                return "error", self.data_err
            return "arrancando", "Cargando historial"
        if self.guard:
            g = self.guard
            return "detenido", "Límite {} de {} alcanzado ({:+.2f}{})".format(
                "diario" if g["scope"] == "día" else "semanal", g["kind"], g["value"], g["unit"])
        if self.paused:
            return "pausado", "Entradas pausadas a mano"
        if now - (self.last_t + self.tf_ms) > 3 * self.tf_ms + 60_000:
            return "sin_datos", "Sin velas nuevas desde hace {:.0f} min".format((now - self.last_t - self.tf_ms) / 60000)
        if self.st and self.st.blocked:
            return "dia_bloqueado", "Regla de la estrategia: " + self.st.blocked
        return "en_marcha", "Buscando señales"

    def vigilar_limite(self):
        """Orden límite «vigilada» (bots Plan manual en sandbox): no se coloca en la cuenta; cuando el precio en vivo
        toca el nivel, se envía a mercado con su stop y su objetivo. Así nunca queda una orden huérfana ni una
        posición sin protección. Lo llama el hilo de vigilancia cada segundo."""
        st = self.st
        if not st or not self.ready or self.meta.get("archived") or self.paused or self.guard:
            return
        pend = st.pending
        if not pend or pend.get("limite") is None or pend.get("enviada") or not self.ex.activo():
            return
        lp = self.live_price()
        if not lp or not lp[0] or lp[2] in ("cierre", "candle") or not lp[1] or now_ms() - lp[1] > 30_000:
            return                                        # sin precio en vivo reciente: no se decide nada
        px = float(lp[0])
        lim, side = float(pend["limite"]), pend["side"]
        if (px - lim) * side > 0:
            return                                        # aún no ha tocado el nivel
        with self.lock:
            pend = self.st.pending if self.st else None
            if not pend or pend.get("limite") is None or pend.get("enviada"):
                return
            pend["enviada"] = now_ms()
            S = "LONG" if side == 1 else "SHORT"
            t = now_ms() - self.tf_ms                     # para la cuenta es una señal de ahora mismo
            self.log("Orden límite vigilada: el precio ({:.6g}) tocó {:.6g}. Se envía {} a mercado a la cuenta.".format(
                px, lim, S))
            self.ex.on_signal(t, side, px, pend["sl"], pend.get("tp"))
            abierta = any(getattr(x, "pos", None) for x in getattr(self.ex, "ex", {}).values())
            D = self.decimals()
            if abierta:
                pend["forzar"] = px                        # el modelo entra al mismo precio que la cuenta
                desl = (px - lim) * side * -1
                self._event(now_ms(), "limite_vigilada", S, lim, px)
                self.notify("senal", "🎯 <b>Orden límite vigilada · {}</b> · {}\nTocó {} · enviada a mercado a {}{}".format(
                    S, A.esc(self.name), A.fpx(lim, D), A.fpx(px, D),
                    " (deslizamiento {})".format(A.fpx(abs(desl), D)) if abs(desl) > 0 else ""))
            else:
                self.log("Orden límite vigilada: la cuenta no abrió la posición (ver avisos de ejecución). El modelo en "
                         "papel sigue con su orden límite.", "warn")
            self._save_state()

    def vigilar_tp1(self):
        """TP1 de un plan manual en la cuenta: al tocarlo en vivo vende su parte y sube el stop a break-even, sin esperar
        al cierre de la vela. El modelo se actualiza igual para que el panel y la cuenta cuadren."""
        st = self.st
        if not st or not self.ready or not self.ex.activo() or not hasattr(st.S, "gestion_be"):
            return
        reales = [x for x in getattr(self.ex, "ex", {}).values()
                  if getattr(x, "pos", None) and not x.pos.get("partial") and not x.pos.get("tp1_vivo")]
        if not reales:
            return
        gb = st.S.gestion_be(st.pos)
        if not gb or not gb.get("nivel"):
            return
        lp = self.live_price()
        if not lp or not lp[0] or lp[2] in ("cierre", "candle") or not lp[1] or now_ms() - lp[1] > 30_000:
            return
        px, nivel = float(lp[0]), float(gb["nivel"])
        f = max(0.0, min(0.95, float(gb.get("pct") or 0) / 100))
        with self.lock:
            for x in reales:
                p, side = x.pos, x.pos["side"]
                if (px - nivel) * side < 0 or p.get("tp1_vivo"):
                    continue
                p["tp1_vivo"] = now_ms()
                t = now_ms()
                try:
                    rp = x.tr.position(x.acc, self.market["market_id"])
                    if not rp:
                        continue
                    if f > 0:
                        x._cerrar(rp, "TP1 del plan", fraction=f, t=t)
                        rp = x.tr.position(x.acc, self.market["market_id"])
                        p["partial"] = f
                        if rp:
                            p["size"] = float(rp["size"])
                    if gb.get("off") is not None and rp:
                        x._sync_exits(rp, p["entry"] + side * gb["off"] * abs(p["entry"] - p["sl0"]), p.get("tp"), t)
                    self.log("TP1 del plan tocado en vivo ({:.6g}): {}{} en la cuenta.".format(
                        px, "vendido el {:.0f} %".format(f * 100) if f > 0 else "",
                        " y stop a break-even" if gb.get("off") is not None else ""))
                except Exception as e:
                    self.log("TP1 del plan: no se pudo gestionar en la cuenta ({}). Se hará al cierre de la vela.".format(e),
                             "warn")
                    p.pop("tp1_vivo", None)
                    continue
            mp = st.pos                                   # el modelo hace lo mismo para cuadrar con la cuenta
            if mp and not mp.get("be") and (px - nivel) * mp["side"] >= 0:
                if f > 0:
                    mp["part"] = (f, nivel)
                mp["be"] = True
                if gb.get("off") is not None:
                    mp["sl"] = mp["entry"] + mp["side"] * gb["off"] * mp["dist"]
            self._save_state()

    def live_price(self):
        m = self.market
        if m and self.hub.feed:
            v = self.hub.feed.price(m["provider"], m["coin"])
            if v:
                return v[0], v[1], v[2]
        return self.last_price, self.last_t + self.tf_ms if self.last_t else None, "cierre"

    def unreal(self, price):
        st = self.st
        if not (st and st.pos and price):
            return None
        p = st.pos
        return round(st._r(p, price) + p["entry"] * st.cost / 100 / p["dist"], 2)

    def summary(self):
        with self.lock:
            now = now_ms()
            P = self.P
            d = self.dkey(self.last_t or now)
            w = wkey(d)
            code, text = self.status(now)
            price, pt, kind = self.live_price()
            m = K.metrics(self.trades)
            pos = None
            if self.st and self.st.pos:
                p = self.st.pos
                pos = {"side": "LONG" if p["side"] == 1 else "SHORT", "unreal_r": self.unreal(price)}
            return {"id": self.id, "name": self.name, "archived": bool(self.meta.get("archived")),
                    "strategy": self.strategy_name(), "strategy_id": P.get("estrategia"),
                    "symbol": P["symbol"], "tf": P["timeframe"], "status": code, "status_text": text,
                    "price": price, "price_t": pt, "price_kind": kind, "cfg_id": self.cfg_id,
                    "today_r": round(sum(x["r"] for x in self.trades if x["day"] == d), 2),
                    "week_r": round(sum(x["r"] for x in self.trades if x["week"] == w), 2),
                    "total_r": round(m["total_r"], 2), "n": m["n"], "winrate": round(m["winrate"], 1),
                    "pf": r2(m["pf"]), "max_dd": round(m["max_dd"], 2), "position": pos, "trend": self.trend(),
                    "days": round((now - self.started_at) / 86_400_000, 1), "g_unit": P["g_unit"],
                    "exec": {"modo": P.get("ex_modo"), "activo": self.ex.activo(), "real": bool(self.ex.pos),
                             "cuentas": [x["cuenta_nombre"] for x in self.ex.public()["cuentas"] if x["activo"]]},
                    "equity": [e[1] for e in equity_curve(self.trades)][-120:], "parada": self.parada_info()}

    def parada_info(self):
        """Por qué no busca entradas y cómo vuelve: para el aviso de bots detenidos del panel."""
        if self.meta.get("archived"):
            return None
        if self.guard:
            g = self.guard
            off = int(self.P["utc_off"] * 3_600_000)
            if g.get("action") == "pausa":
                if g["scope"] == "semana":
                    reanuda = ((g["key"] + 1) * 7 - 3) * 86_400_000 - off
                else:
                    reanuda = (g["key"] + 1) * 86_400_000 - off
            else:
                reanuda = None
            return {"tipo": "limite", "proteccion": True, "reanuda": reanuda}
        if self.paused:
            return {"tipo": "manual", "proteccion": False}
        if self.strat_err:
            return {"tipo": "error", "proteccion": False}
        return None

    def strategy_name(self):
        if self.st:
            return self.st.plugin.nombre
        p = K.REGISTRY.any(self.P.get("estrategia")) if K.REGISTRY else None
        return p.nombre if p else self.P.get("estrategia", "")

    def filter_names(self):
        if self.st:
            return self.st.filter_names
        p = K.REGISTRY.get(self.P.get("estrategia")) if K.REGISTRY else None
        return dict(K.FILTER_NAMES, **(p.filtros if p else {}))

    def htf_text(self):
        st = self.st
        if not st or not st.htf:
            return ""
        return "Tendencia " + st.htf.texto()

    def trend(self):
        """Estado de la tendencia para el panel y Telegram: lista de {tf, lado, texto, metodo} o None."""
        st = self.st
        return st.htf.resumen() if st and st.htf else None

    def snapshot(self, scope="all"):
        with self.lock:
            now = now_ms()
            P, st = self.P, self.st
            d = self.dkey(self.last_t or now)
            w = wkey(d)
            tr = [x for x in self.trades if x.get("cfg") == self.cfg_id] if scope == "cfg" else self.trades
            code, text = self.status(now)
            m = K.metrics(tr)
            price, pt, kind = self.live_price()
            lim = []
            for sc, kd, v in self.limits():
                key = w if sc == "semana" else d
                lim.append({"scope": sc, "kind": kd, "limit": v, "value": round(self.period_value(sc, key), 2)})
            today = [x for x in self.trades if x["day"] == d]
            week = [x for x in self.trades if x["week"] == w]
            pos = None
            if st and st.pos:
                p = st.pos
                pos = {"side": "LONG" if p["side"] == 1 else "SHORT", "entry": p["entry"], "sl": p["sl"], "sl0": p["sl0"],
                       "tp": p.get("tp"), "t_open": p.get("t_open"), "be": p.get("be"), "part": p.get("part"),
                       "modo_tp": p.get("modo_tp"),
                       "bars": p.get("bars"), "ext_n": p.get("ext_n", 0), "trail": p.get("trail"),
                       "trail_moved": p.get("trail_moved"), "unreal_r": self.unreal(price)}
                part = p.get("part")
                if part and p.get("dist"):
                    pos["asegurado_r"] = round(part[0] * (part[1] - p["entry"]) * p["side"] / p["dist"], 2)
                pos["si_stop_r"] = round(st._r(p, p["sl"]), 2) if p.get("dist") else None
            chart = list(self.chart)
            lb = self.hub.feed.live_bar(self.market["provider"], self.market["coin"], P["timeframe"]) \
                if self.market and self.hub.feed else None
            if lb and lb[0] > self.last_t and chart:
                chart.append({"t": lb[0], "o": lb[1], "h": lb[2], "l": lb[3], "c": lb[4], "v": chart[-1]["v"],
                              "live": True})
            try:
                s_text = st.S.texto() if st else ""
                s_ind = st.S.indicadores() if st else ""
            except Exception as e:
                s_text, s_ind = "La estrategia falla al describir su estado: {}".format(e), ""
            out = {
                "version": VERSION, "now": now, "id": self.id, "name": self.name, "archived": bool(self.meta.get("archived")),
                "status": code, "status_text": text, "symbol": P["symbol"], "tf": P["timeframe"], "market": self.market,
                "price": price, "price_t": pt, "price_kind": kind, "last_candle": self.last_t, "live_t": self.live_t,
                "paused": self.paused, "guard": self.guard, "cfg_id": self.cfg_id, "cfg_saved": self.cfg_saved,
                "scope": scope,
                "today": {"label": day_label(d), "n": len(today), "r": round(sum(x["r"] for x in today), 2)},
                "week": {"label": week_label(w), "n": len(week), "r": round(sum(x["r"] for x in week), 2)},
                "metrics": {k: (r2(v, 3) if isinstance(v, float) else v) for k, v in m.items()},
                "risk_usd": P["risk_usd"], "g_unit": P["g_unit"], "g_action": P["g_action"], "limits": lim,
                "progress": {"started_at": self.started_at, "days": round((now - self.started_at) / 86_400_000, 2),
                             "target": self.c.test_days},
                "counters": self.cnt, "restarts": self.restarts, "position": pos,
                "pending": ({"side": "LONG" if st.pending["side"] == 1 else "SHORT", "sl": st.pending["sl"]}
                            if st and st.pending else None),
                "strategy": {"id": P.get("estrategia"), "nombre": self.strategy_name(),
                             "version": st.plugin.version if st else "", "error": self.strat_err},
                "strategy_text": s_text, "ind_short": s_ind,
                "ind_text": " · ".join(x for x in (s_ind, self.htf_text()) if x), "trend": self.trend(),
                "lineas": st.plugin.lineas if st else [], "why_names": self.why_names(),
                "strategy_day": {"trades": st.dtrades, "consec": st.dconsec, "r": round(st.dr, 2),
                                 "blocked": st.blocked, "cool": st.cool} if st else None,
                "equity": equity_curve(tr), "daily": [], "hourly": [[h, 0, 0.0] for h in range(24)],
                "sides": {s: K.metrics([x for x in tr if x["side"] == v]) for s, v in (("LONG", 1), ("SHORT", -1))},
                "trades": [dict(x, day_label=day_label(x["day"])) for x in tr[-60:]][::-1],
                "events": list(self.events)[-60:][::-1], "log": list(self.log.recent)[-80:][::-1],
                "chart": chart, "health": {"data_ok_t": self.data_ok_t, "data_err": self.data_err},
                "params": P, "local_tz": self.c.local_tz, "filter_names": self.filter_names(),
                "notify_off": str(self.id) in (self.hub.avisos.cfg.get("bots_off", []) if self.hub.avisos else []),
                "exec": self.ex.public(),
            }
            days = {}
            for x in tr:
                days.setdefault(x["day"], []).append(x)
            for dd in sorted(days)[-30:]:
                xs = days[dd]
                out["daily"].append([day_label(dd), len(xs), round(sum(y["r"] for y in xs), 2)])
            for x in tr:
                h = self.hour_fn(x["t_open"])[0]
                out["hourly"][h][1] += 1
                out["hourly"][h][2] = round(out["hourly"][h][2] + x["r"], 2)
            return out


DESG_MAX_SENALES = 6000


def desglose_estrategias(P, trades, info, hour_fn):
    """Detalle de una prueba por estrategia (ventana «Desglose» y script del portátil): métricas por lado, curva,
    horas, días, motivos de salida, qué pasó con cada señal (entra, la bloquea otra operación, filtro, límite del
    día) y la línea de tiempo de señales y operaciones."""
    reg = info.get("registro") or []
    partes = info.get("orden") or K.partes_de(P)
    partes = [x for x in partes if x] or [P.get("estrategia")]
    for sid in [x for x in K.partes_de(P) if x not in partes]:
        partes.append(sid)
    ix = {sid: k for k, sid in enumerate(partes)}

    def nombre(sid):
        try:
            return K.plugin_for(sid).nombre
        except Exception:
            return sid

    def tf(sid):
        ms = K.tf_de(P, sid, sid == P.get("estrategia"))
        return next((k for k, v in K.TF_MS.items() if v == ms), P["timeframe"])

    out = []
    for sid in partes:
        tr = [x for x in trades if (x.get("estrategia") or P.get("estrategia")) == sid]
        rs = [x for x in reg if x[1] == sid]
        sen = {"total": len(rs), "largos": sum(1 for x in rs if x[2] == 1), "cortos": sum(1 for x in rs if x[2] == -1),
               "entra": 0, "ocupada": 0, "propia": 0, "limite": 0, "filtro": {}}
        ocupada_por, bloquea_a = {}, {}
        for x in rs:
            if x[3] == "ocupada":
                if x[4] == sid:
                    sen["propia"] += 1
                else:
                    sen["ocupada"] += 1
                    ocupada_por[x[4]] = ocupada_por.get(x[4], 0) + 1
            elif x[3] == "filtro":
                sen["filtro"][x[4]] = sen["filtro"].get(x[4], 0) + 1
            else:
                sen[x[3]] = sen.get(x[3], 0) + 1
        for x in reg:
            if x[3] == "ocupada" and x[4] == sid and x[1] != sid:
                bloquea_a[x[1]] = bloquea_a.get(x[1], 0) + 1
        horas = [[0, 0.0] for _ in range(24)]
        dias = [[0, 0.0] for _ in range(7)]
        for x in tr:
            h, wd = hour_fn(x["t_open"])
            horas[h][0] += 1
            horas[h][1] = round(horas[h][1] + x["r"], 3)
            dias[wd][0] += 1
            dias[wd][1] = round(dias[wd][1] + x["r"], 3)
        cum, curva = 0.0, []
        for x in tr:
            cum += x["r"]
            curva.append([x["t_close"], round(cum, 3)])
        out.append({"id": sid, "nombre": nombre(sid), "tf": tf(sid), "prioridad": ix[sid] + 1,
                    "direccion": K.direccion_de(P, sid), "metrics": K.metrics(tr),
                    "lados": {s: K.metrics([x for x in tr if x["side"] == v]) for s, v in (("LONG", 1), ("SHORT", -1))},
                    "curva": curva, "horas": horas, "dias": dias, "senales": sen,
                    "ocupada_por": ocupada_por, "bloquea_a": bloquea_a})
    # señales de dos estrategias en la misma vela: mismo lado o lados contrarios
    por_vela = {}
    for x in reg:
        por_vela.setdefault(x[0], {})[x[1]] = x[2]
    coinciden = {"mismo": 0, "contrario": 0}
    for v in por_vela.values():
        if len(v) > 1:
            coinciden["mismo" if len(set(v.values())) == 1 else "contrario"] += 1
    lin = [[x[0], ix.get(x[1], 0), x[2], x[3], x[4] if x[3] != "ocupada" else ix.get(x[4], -1)]
           for x in reg[-DESG_MAX_SENALES:]]
    ops = [[x["t_open"], x["t_close"], ix.get(x.get("estrategia") or P.get("estrategia"), 0), x["side"], round(x["r"], 3)]
           for x in trades]
    return {"estrategias": out, "coinciden": coinciden, "senales": lin, "recortadas": max(0, len(reg) - DESG_MAX_SENALES),
            "operaciones": ops}


def equity_curve(tr):
    out, cum = [], 0.0
    for x in tr:
        cum += x["r"]
        out.append([x["t_close"], round(cum, 3)])
    return out


def system_stats():
    s = {}
    try:
        with open("/sys/class/thermal/thermal_zone0/temp") as f:
            s["temp_c"] = round(int(f.read().strip()) / 1000, 1)
    except (OSError, ValueError):
        s["temp_c"] = None
    try:
        s["load"] = [round(x, 2) for x in os.getloadavg()]
        s["cpus"] = os.cpu_count()
    except OSError:
        pass
    try:
        mem = {}
        with open("/proc/meminfo") as f:
            for line in f:
                k, v = line.split(":", 1)
                mem[k] = int(v.strip().split()[0])
        s["mem_used_pct"] = round(100 * (1 - mem["MemAvailable"] / mem["MemTotal"]), 1)
        s["mem_total_mb"] = round(mem["MemTotal"] / 1024)
    except (OSError, KeyError, ValueError):
        pass
    try:
        du = shutil.disk_usage(".")
        s["disk_free_gb"] = round(du.free / 1e9, 1)
    except OSError:
        pass
    return s


# ============================ el conjunto de bots ============================
class Hub:
    def __init__(self, cfg, log):
        self.c, self.log = cfg, log
        self.tz = get_tz(cfg.local_tz)
        self.hour_fn = make_hour_fn(self.tz)
        self.rest = MFPRest(cfg.host, cfg.key)
        self.bots = {}
        self.cuentas = CU.Gestor(self, cfg, VERSION)   # cuentas con su clave, bloqueos y envío de órdenes
        if self.cuentas.migrada:
            log("Cuentas: creada «Principal» con la clave de config.env; envío de órdenes {} (FP_EXEC). Desde "
                "ahora se gestionan en el panel → Cuentas.".format("activado" if self.cuentas.ordenes else
                                                                  "desactivado"), "warn")
        self.markets = Markets(self.rest)
        self.cache = Cache()
        self.lock = threading.RLock()
        self.req = None
        self.feed = None
        self.order = []
        self.deep_fetch = {}
        self._punt_lock = threading.Lock()
        self.puntuando = None
        self.proc_start = now_ms()
        self.req_reconnects = 0
        self.avisos = None
        self.riesgo = RG.RiesgoGlobal(self)     # límites del conjunto de bots y operaciones a la vez
        self.externos = EX.Externos("externos", self._velas_mfp, log)   # velas de fuera de MFP para backtests
        self.fiscal = FI.Fiscal("fiscal", log)                            # retiros y gastos para la renta
        self.temas = TM.Temas("temas")                                    # temas del panel cargados desde archivo
        self.noticias = NT.Noticias(self)       # sin entradas alrededor de las noticias del calendario (6.4)
        self.optim = OP.Optimizador(self)       # optimizador con walk-forward (7.0)
        self.cartera = CT.Cartera(self)         # backtest de cartera (7.1)
        self.vigil = DV.Vigilante(self)         # vigilante de deriva (7.2)
        self.diario = DI.Diario(self)           # diario de operaciones (7.3)
        self.migrate()
        self.meta = self._load_meta()

    # ---------- registro ----------
    def _load_meta(self):
        try:
            with open("bots.json", encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            return []

    def _save_meta(self):
        atomic_write("bots.json", json.dumps(self.meta, indent=1, ensure_ascii=False))

    def migrate(self):
        """Convierte una instalación 2.x (un bot en data/) en el bot nº 1 de la 3.0."""
        if os.path.exists("bots.json"):
            return
        os.makedirs("bots", exist_ok=True)
        meta = []
        if os.path.exists("config.json"):
            dest = os.path.join("bots", "1")
            os.makedirs(dest, exist_ok=True)
            for f in V2_FILES:
                if os.path.exists(f):
                    shutil.move(f, os.path.join(dest, f))
            try:
                with open(os.path.join(dest, "config.json"), encoding="utf-8") as fh:
                    P = json.load(fh)["params"]
                name = "{} {}".format(P.get("symbol", "Bot"), P.get("timeframe", ""))
            except (OSError, ValueError, KeyError):
                name = "Bot 1"
            meta.append({"id": "1", "name": name, "created": now_ms(), "archived": False, "migrated": "2.x"})
            self.log("Instalación 2.x convertida: su bot pasa a ser «{}» con todo su historial.".format(name))
        atomic_write("bots.json", json.dumps(meta, indent=1, ensure_ascii=False))

    def next_id(self):
        ids = [int(m["id"]) for m in self.meta if str(m["id"]).isdigit()]
        return str(max(ids) + 1 if ids else 1)

    # ---------- arranque ----------
    def _vigilar_limites(self):
        """Cada segundo: órdenes límite de los planes manuales que operan en cuenta (ver Engine.vigilar_limite)."""
        while True:
            time.sleep(1)
            for b in list(self.bots.values()):
                try:
                    b.vigilar_limite()
                    b.vigilar_tp1()
                except Exception as e:
                    b.log("Error vigilando la orden límite: {}".format(e), "error")

    def start(self):
        self.avisos = A.Avisos("avisos.json", self.log, self.tz, commands=self.telegram_command)
        self.req = MFPStream(self.log, self._count_req_reconnect)
        self.feed = LiveFeed(self, self.log)
        if not self.meta:
            self.create("GOLD 5m", K.defaults(), "primer bot (valores por defecto)", start=False)
        for m in self.meta:
            self._spawn(m, run=not m.get("archived"))
        threading.Thread(target=self._monitor, daemon=True, name="vigilancia").start()
        threading.Thread(target=self._vigilar_limites, daemon=True, name="limites").start()
        self.vigil.start()
        act = [m for m in self.meta if not m.get("archived")]
        if self.avisos.wants("problemas"):
            self.avisos.push("problemas", "▶️ <b>Programa en marcha</b> (v{}) · {} bot{} activo{}".format(
                VERSION, len(act), "" if len(act) == 1 else "s", "" if len(act) == 1 else "s"))

    def _count_req_reconnect(self):
        self.req_reconnects += 1

    # ---------- ejecución en las cuentas ----------
    def exec_ready(self):
        return self.cuentas.listo_alguna()

    def exec_problem(self):
        return self.cuentas.problema()

    def exec_conflicts(self, P, bot_id=None):
        """Las cuentas elegidas tienen que existir, y en cada cuenta solo un bot por mercado (compartirían la
        posición)."""
        if P.get("ex_modo") != "sandbox":
            return []
        g = self.cuentas
        if not g.lista():
            return ["Para operar en sandbox da de alta antes una cuenta con su clave (menú Cuentas)."]
        malas = [c for c in (P.get("ex_cuentas") or []) if not g.get(c)]
        if malas:
            return ["Cuentas que ya no existen: {}. Revisa «Cuentas» en Ejecución.".format(", ".join(malas))]
        mias = set(g.para_bot(P))
        for b in self.bots.values():
            if b.id == bot_id or b.meta.get("archived") or b.P.get("symbol") != P.get("symbol"):
                continue
            comun = mias & (set(g.para_bot(b.P)) | set(b.ex.cuentas_con_posicion()))
            if comun:
                return ["El bot «{}» ya opera {} en {}. En cada cuenta, un solo bot por mercado: elige otra cuenta, "
                        "ponle uno de los dos en papel o cambia de instrumento.".format(
                            b.name, P.get("symbol"), ", ".join("«{}»".format(g.nombre(c)) for c in sorted(comun)))]
        return []

    def _spawn(self, meta, run=True):
        eng = Engine(self, meta)
        eng._migrate_trades_csv()
        eng._migrate_candles_csv()
        eng.trades = eng._load_trades()
        with self.lock:
            self.bots[meta["id"]] = eng
            if meta["id"] not in self.order:
                self.order.append(meta["id"])
        if run:
            eng.start_thread()
        return eng

    def active_symbols(self):
        return {b.P["symbol"] for b in self.bots.values() if not b.meta.get("archived")}

    def feed_changed(self):
        keys = set()
        for b in list(self.bots.values()):
            if b.meta.get("archived") or not b.market:
                continue
            m = b.market
            keys.add(("candles", m["provider"], m["coin"], b.feed_tf()))
            keys.add(("ticks", m["provider"], m["coin"]))
        if self.feed:
            self.feed.set_wanted(keys)

    def on_bar(self, key, bar):
        for b in list(self.bots.values()):
            if not b.meta.get("archived") and b.ready and b.feed_key() == key:
                # diario/semanal: la vela de 4h no es la del bot; se mira el historial, que la construye
                b.q.put(("check", None) if b.P["timeframe"] in K.AGREGADAS else ("bar", bar))

    def on_rollover(self, key):
        for b in list(self.bots.values()):
            if not b.meta.get("archived") and b.ready and b.feed_key() == key:
                b.q.put(("check", None))

    # ---------- gestión de bots ----------
    def create(self, name, P, note="", start=True):
        with self.lock:
            if len([m for m in self.meta if not m.get("archived")]) >= self.c.max_bots:
                raise ValueError("Máximo {} bots activos a la vez".format(self.c.max_bots))
            errs = self.exec_conflicts(P)
            if errs:
                raise ValueError(errs[0])
            name = (name or "").strip()[:40] or "{} {}".format(P["symbol"], P["timeframe"])
            meta = {"id": self.next_id(), "name": name, "created": now_ms(), "archived": False}
            ConfigStore(os.path.join("bots", meta["id"])).save(P, note or "creado desde el panel")
            self.meta.append(meta)
            self._save_meta()
        self.log("Bot nuevo «{}» ({} {}).".format(name, P["symbol"], P["timeframe"]))
        if start:
            self._spawn(meta)
        return meta

    def archive(self, bid):
        b = self.bots.get(bid)
        if not b or b.meta.get("archived"):
            raise ValueError("Ese bot no está activo")
        if b.ex.pos and b.P.get("ex_cerrar_al_archivar"):
            with b.lock:
                b.ex.cerrar_todo("bot archivado")
        b.stop()
        with self.lock:
            b.meta["archived"] = True
            b.meta["archived_at"] = now_ms()
            self._save_meta()
        b.ready = False
        self.feed_changed()
        self.log("Bot «{}» archivado.".format(b.name))

    def unarchive(self, bid):
        b = self.bots.get(bid)
        if not b or not b.meta.get("archived"):
            raise ValueError("Ese bot no está archivado")
        if len([m for m in self.meta if not m.get("archived")]) >= self.c.max_bots:
            raise ValueError("Máximo {} bots activos a la vez".format(self.c.max_bots))
        errs = self.exec_conflicts(b.P, b.id)
        if errs:
            raise ValueError(errs[0])
        with self.lock:
            b.meta["archived"] = False
            b.meta.pop("archived_at", None)
            self._save_meta()
            del self.bots[bid]
        self._spawn(b.meta)
        self.log("Bot «{}» reactivado.".format(b.meta["name"]))

    def rename(self, bid, name):
        b = self.bots.get(bid)
        name = (name or "").strip()[:40]
        if not b or not name:
            raise ValueError("Nombre no válido")
        with self.lock:
            b.meta["name"] = name
            b.log.prefix = name
            self._save_meta()

    def import_v2(self, folder):
        """Trae un bot de otra instalación 2.x: su carpeta data (con config.json) y todo su historial."""
        folder = os.path.abspath(os.path.expanduser(folder))
        if os.path.isdir(os.path.join(folder, "data")) and not os.path.exists(os.path.join(folder, "config.json")):
            folder = os.path.join(folder, "data")
        if not os.path.exists(os.path.join(folder, "config.json")):
            raise ValueError("No encuentro config.json en {} (¿es la carpeta data de un bot 2.x?)".format(folder))
        with self.lock:
            bid = self.next_id()
            dest = os.path.join("bots", bid)
            os.makedirs(dest, exist_ok=True)
            for f in V2_FILES:
                src = os.path.join(folder, f)
                if os.path.isdir(src):
                    if not os.path.exists(os.path.join(dest, f)):
                        shutil.copytree(src, os.path.join(dest, f))
                elif os.path.exists(src):
                    shutil.copy2(src, os.path.join(dest, f))
            with open(os.path.join(dest, "config.json"), encoding="utf-8") as fh:
                P = json.load(fh)["params"]
            sym = str(P.get("symbol", ""))
            if sym and not self.markets.get(sym):
                src_instr = os.path.join(folder, "instrumentos.json")
                if os.path.exists(src_instr):
                    with open(src_instr, encoding="utf-8") as fh:
                        for i in json.load(fh):
                            if i.get("symbol") == sym:
                                self.markets.instr.append(i)
                                self.markets._save()
            meta = {"id": bid, "name": "{} {}".format(sym, P.get("timeframe", "")), "created": now_ms(),
                    "archived": False, "imported_from": folder}
            self.meta.append(meta)
            self._save_meta()
        return meta

    # ---------- estrategias ----------
    def strategy_users(self):
        used = {}
        for b in self.bots.values():
            for sid in K.partes_de(b.P):
                u = used.setdefault(sid, {"active": [], "archived": []})
                u["archived" if b.meta.get("archived") else "active"].append(b.name)
        return used

    def strategies(self):
        used = self.strategy_users()
        out = []
        for p in K.REGISTRY.list():
            u = used.get(p.id, {"active": [], "archived": []})
            d = p.info(len(u["active"]) + len(u["archived"]))
            d["bots_active"], d["bots_archived"] = u["active"], u["archived"]
            out.append(d)
        missing = [sid for sid in used if not K.REGISTRY.any(sid)]
        return {"items": out, "missing": missing, "folder": STRAT_DIR, "api": K.API_VERSION,
                "default": K.REGISTRY.default_id()}

    def _after_strategy_change(self, sid):
        """Aplica una estrategia instalada o actualizada a los bots que la usan (sin reiniciar el programa)."""
        n = 0
        for b in list(self.bots.values()):
            if sid not in K.partes_de(b.P) or b.meta.get("archived"):
                continue
            P2, errs = K.validate(b.P)
            try:
                if P2 != b.P:
                    b.apply_config(P2, "ajustes adaptados a la versión nueva de la estrategia")
                    self.feed_changed()
                else:
                    with b.lock:
                        if b.st and b.ready:
                            b.rebuild()
                        else:
                            b.q.put(("check", None))
                n += 1
                b.log("Estrategia «{}» actualizada{}.".format(sid, ": " + "; ".join(errs) if errs else ""), "warn")
            except Exception as e:
                b.data_error(e)
        return n

    def install_strategy(self, filename, code):
        p, msg, replaced = K.REGISTRY.install(filename, code)
        n = self._after_strategy_change(p.id) if replaced or self.strategy_users().get(p.id) else 0
        self.log("Estrategia «{}» v{} {} ({}).".format(p.nombre, p.version, "actualizada" if replaced else "instalada",
                                                       p.file))
        txt = "«{}» v{} {}. {}.".format(p.nombre, p.version or "?", "actualizada" if replaced else "instalada", msg)
        if n:
            txt += " {} bot{} la usa{} ya.".format(n, "" if n == 1 else "s", "" if n == 1 else "n")
        return p, txt

    def remove_strategy(self, sid):
        u = self.strategy_users().get(sid)
        if u and (u["active"] or u["archived"]):
            raise ValueError("La usan estos bots: {}. Cámbiales la estrategia antes de quitarla.".format(
                ", ".join(u["active"] + u["archived"])))
        K.REGISTRY.remove(sid)
        self.log("Estrategia «{}» retirada (se guarda una copia en estrategias/anteriores).".format(sid))

    def rescan_strategies(self):
        before = {p.id: p.sha for p in K.REGISTRY.list()}
        K.REGISTRY.scan()
        changed = [p.id for p in K.REGISTRY.list() if not p.error and before.get(p.id) != p.sha]
        for sid in changed:
            self._after_strategy_change(sid)
        errs = [p for p in K.REGISTRY.list() if p.error]
        for p in errs:
            self.log("Estrategia {} con error: {}".format(p.file, p.error), "error")
        return changed

    # ---------- backtest ----------
    def _velas_mfp(self, sym, tf, n):
        if self.req is None:
            raise RuntimeError("el programa aún está arrancando")
        return self.req.candles(self.markets.resolve(sym), tf, n)

    def _htf_externo(self, P, cs):
        """Velas de tendencia construidas con las del backtest cuando incluye datos externos. Se agrupan también las
        velas ANTERIORES al tramo probado (de la última serie combinada) para calentar el filtro, como hace MFP."""
        tf_ms = K.TF_MS[P["timeframe"]]
        previas = []
        memo = getattr(self, "_ext_rows", None)
        if memo and memo[0] == (P["symbol"], P["timeframe"]) and cs:
            import bisect
            ts = memo[2]
            i0 = bisect.bisect_left(ts, cs[0][0])
            extra = max([n * (K.TF_MS.get(src, tf_ms) // tf_ms) for src, n in K.tendencia_fuentes(P).items()] or [0])
            previas = memo[1][max(0, i0 - extra):i0]
        serie = list(previas) + list(cs)
        out = {}
        for src in K.tendencia_fuentes(P):
            ms = K.TF_MS.get(src)
            if ms and ms >= tf_ms and ms % tf_ms == 0:
                out[src] = EX.agrupar(serie, ms, True, tf_ms)
            else:
                out[src] = fetch_trend(self.req, self.markets.resolve(P["symbol"]), P,
                                       cs[-1][0] - cs[0][0]).get(src, [])
        return out

    def bt_datos(self, P, days, htf_cache=None, warm_min=0, fuente=None):
        """Velas (y las de los filtros de tendencia) para un backtest de P en los últimos `days` días."""
        tf_ms = K.TF_MS[P["timeframe"]]
        warm = max(400, K.warmup_bars(P), int(warm_min or 0))
        if P["timeframe"] in K.AGREGADAS:          # en diario y semanal no hay tanta historia
            warm = max(K.MIN_VELAS[P["timeframe"]], K.warmup_bars(P) + 10, int(warm_min or 0))
        days = max(1, min(int(days or 0) or 3650, 3650))
        need = min(self.c.bt_max, warm + int(days * 86_400_000 / tf_ms))
        rows = sorted(self.cache.load(P["symbol"], P["timeframe"]).values())
        key = (P["symbol"], P["timeframe"])
        deep_recent = time.time() - self.deep_fetch.get(key, 0) < 6 * 3600
        if len(rows) < need and not deep_recent:
            mk = self.markets.resolve(P["symbol"])
            rows = self.cache.merge(P["symbol"], P["timeframe"], self.req.candles(mk, P["timeframe"], min(need, 20000)))
            self.deep_fetch[key] = time.time()
        elif not rows or now_ms() - rows[-1][0] > 3 * tf_ms:
            mk = self.markets.resolve(P["symbol"])
            gap = int((now_ms() - rows[-1][0]) / tf_ms) + 5 if rows else 1000
            rows = self.cache.merge(P["symbol"], P["timeframe"], self.req.candles(mk, P["timeframe"], min(gap, 20000)))
        rows, origen = self.externos.combinar(P["symbol"], P["timeframe"], rows, fuente)
        origen = dict(origen, limitado=len(rows) > need)
        if origen["tipo"] != "mfp":
            self._ext_rows = ((P["symbol"], P["timeframe"]), rows, [r[0] for r in rows])
        cs = [tuple(r) for r in rows[-need:]]
        if len(cs) < warm + 50:
            return {"ok": False, "errors": ["Solo hay {} velas de {} {}; hacen falta al menos {}".format(
                len(cs), P["symbol"], P["timeframe"], warm + 50)]}
        fuentes = K.tendencia_fuentes(P)
        htf = None
        if fuentes and origen["tipo"] != "mfp":
            htf = self._htf_externo(P, cs)
        elif fuentes:
            ck = json.dumps(sorted(fuentes.items()))      # el optimizador reutiliza las velas de tendencia
            if htf_cache is not None and ck in htf_cache:
                htf = htf_cache[ck]
            else:
                htf = fetch_trend(self.req, self.markets.resolve(P["symbol"]), P, cs[-1][0] - cs[0][0])
                if htf_cache is not None:
                    htf_cache[ck] = htf
        return {"ok": True, "cs": cs, "warm": warm, "htf": htf, "fuente": origen}

    def bt_htf(self, P, cs, cache):
        """Velas de tendencia de P para el periodo de cs, reutilizando las ya descargadas en `cache`."""
        fuentes = K.tendencia_fuentes(P)
        if not fuentes:
            return None
        if self.externos.modo(P["symbol"]) != "no" and cs and cs[0][0] < (self.cache_desde(P) or 0):
            return self._htf_externo(P, cs)
        ck = json.dumps(sorted(fuentes.items()))
        if ck not in cache:
            cache[ck] = fetch_trend(self.req, self.markets.resolve(P["symbol"]), P, cs[-1][0] - cs[0][0])
        return cache[ck]

    def cache_desde(self, P):
        """Primera vela de MFP guardada en la caché de P (para saber si un backtest incluye datos externos)."""
        try:
            with open(self.cache.path(P["symbol"], P["timeframe"])) as f:
                return int(f.readline().split(",")[0])
        except (OSError, ValueError, IndexError):
            return None

    def backtest(self, raw, days, orden=None, variante=None):
        """variante (para comparar, la usa analizar_estrategias.py): {"sola": id} prueba una estrategia del bot ella
        sola, con sus ajustes, su temporalidad y su dirección; {"sin": id}, el bot sin esa estrategia."""
        P, errs = K.validate(raw)
        if not errs and isinstance(variante, dict) and (variante.get("sola") or variante.get("sin")):
            sid = str(variante.get("sola") or variante.get("sin"))
            if sid not in K.partes_de(P):
                return {"ok": False, "errors": ["«{}» no es una de las estrategias de este bot".format(sid)]}
            try:
                P, errs = K.validate(K.ajustes_de(P, sid) if variante.get("sola") else K.sin_estrategia(P, sid))
            except K.StrategyError as e:
                errs = [str(e)]
        if not errs and not self.markets.get(P["symbol"]):
            errs = ["{} no está en tu lista de instrumentos".format(P["symbol"])]
        if errs:
            return {"ok": False, "errors": errs}
        d = self.bt_datos(P, days)
        if not d.get("ok"):
            return d
        cs, warm, htf = d["cs"], d["warm"], d["htf"]
        t0 = time.time()
        try:
            orden = [str(x) for x in (orden or [])] if isinstance(orden, list) else None
            trades, info = K.backtest(cs, P, self.hour_fn, htf, warm=warm,
                                      instrument_cost=self.markets.cost(P["symbol"]), orden=orden, registro=True)
        except Exception as e:
            import traceback
            tb = traceback.extract_tb(e.__traceback__)
            where = next(("línea {} de {}".format(fr.lineno, os.path.basename(fr.filename)) for fr in reversed(tb)
                          if os.path.dirname(os.path.abspath(fr.filename)) == STRAT_DIR), "")
            return {"ok": False, "errors": ["La estrategia falla en el backtest: {}: {}{}".format(
                type(e).__name__, e, " ({})".format(where) if where else "")]}
        for x in trades:
            x["day"] = (x["t_close"] + int(P["utc_off"] * 3_600_000)) // 86_400_000
        m = K.metrics(trades)
        days_tab = {}
        for x in trades:
            days_tab.setdefault(x["day"], []).append(x["r"])
        hours = [[h, 0, 0.0] for h in range(24)]
        for x in trades:
            h = self.hour_fn(x["t_open"])[0]
            hours[h][1] += 1
            hours[h][2] = round(hours[h][2] + x["r"], 3)
        # día a día (también los días sin operaciones) con el R de cada operación en orden: para el Monte Carlo
        off = int(P["utc_off"] * 3_600_000)
        d0, d1 = (cs[warm][0] + off) // 86_400_000, (cs[-1][0] + off) // 86_400_000
        dias_r = [[round(r, 4) for r in days_tab.get(d, [])] for d in range(d0, d1 + 1)] if d1 - d0 < 4000 else []
        pl = K.plugin_for(P)
        return {"ok": True, "params": P, "candles": len(cs) - warm, "dias_r": dias_r, "from": cs[warm][0], "to": cs[-1][0],
                "filter_names": dict(K.FILTER_NAMES, **pl.filtros), "why_names": dict(WHY_ES, **pl.salidas),
                "strategy": {"id": pl.id, "nombre": pl.nombre, "version": pl.version},
                "secs": round(time.time() - t0, 2), "metrics": m, "info": {k: v for k, v in info.items() if k != "registro"}, "fuente": d.get("fuente"),
                "sides": {s: K.metrics([x for x in trades if x["side"] == v]) for s, v in (("LONG", 1), ("SHORT", -1))},
                "por_estrategia": [{"id": sid, "nombre": K.plugin_for(sid).nombre, "bloqueadas": info.get("bloqueadas", {}).get(sid, 0),
                                    "tf": next((k for k, v in K.TF_MS.items() if v == K.tf_de(P, sid, sid == P["estrategia"])), P["timeframe"]),
                                    "metrics": K.metrics([x for x in trades if x.get("estrategia") == sid])}
                                   for sid in (info.get("orden") or [])] if P.get("combinar") else None,
                "desglose": desglose_estrategias(P, trades, info, self.hour_fn),
                "equity": equity_curve(trades),
                "daily": [[day_label(d), len(v), round(sum(v), 2)] for d, v in sorted(days_tab.items())],
                "hourly": hours, "lineas": pl.lineas, "n_trades": len(trades),
                "bruto": round(sum(x.get("r_bruto", 0.0) for x in trades), 2),
                "trades": [dict(x, n=k) for k, x in enumerate(trades, 1)][-1000:][::-1]}

    def puntuar(self, b):
        """Backtest de cada estrategia del bot por separado (últimos PUNT_DIAS días, ajustes comunes del bot). La
        puntuación es el SQN (calidad del sistema: media de R / desviación × √operaciones, hasta 100 operaciones) y
        decide qué estrategia manda si varias dan señal en la misma vela."""
        if not self._punt_lock.acquire(blocking=False):
            return False
        self.puntuando = b.id
        try:
            partes, por, cache = K.partes_de(b.P), {}, {}
            for sid in partes:
                P1, errs = K.validate(K.ajustes_de(b.P, sid))
                if errs:
                    por[sid] = {"error": errs[0]}
                    continue
                try:
                    d = self.bt_datos(P1, PUNT_DIAS, htf_cache=cache)
                    if not d.get("ok"):
                        por[sid] = {"error": (d.get("errors") or ["sin datos"])[0]}
                        continue
                    trades, _info = K.backtest(d["cs"], P1, self.hour_fn, d["htf"], warm=d["warm"],
                                               instrument_cost=self.markets.cost(P1["symbol"]))
                    m = K.metrics(trades)
                    sqn = m["sqn"] * math.sqrt(min(m["n"], 100) / m["n"]) if m["sqn"] is not None and m["n"] else None
                    por[sid] = {"puntuacion": round(sqn, 2) if sqn is not None and m["n"] >= PUNT_MIN_OPS else None,
                                "n": m["n"], "winrate": round(m["winrate"], 1), "pf": round(m["pf"], 2) if m["pf"] else None,
                                "total_r": round(m["total_r"], 2), "avg_r": round(m["avg_r"], 3),
                                "max_dd": round(m["max_dd"], 2),
                                "equity": [e[1] for e in equity_curve(trades)][-300:]}
                except Exception as e:
                    por[sid] = {"error": "{}: {}".format(type(e).__name__, e)[:200]}
            b.meta["puntuaciones"] = {"t": now_ms(), "dias": PUNT_DIAS, "partes": partes, "cfg": b.cfg_id, "por": por}
            self._save_meta()
            with b.lock:
                if b.st and hasattr(b.st.S, "ordenar"):
                    b.st.S.ordenar(b.prioridad())
            txt = ", ".join("{} ({}) {}".format(b.nombre_estrategia(sid), b.tf_estrategia(sid), "–" if (por.get(sid) or {}).get("puntuacion") is None
                                           else por[sid]["puntuacion"]) for sid in b.prioridad())
            b.log("Puntuación de las estrategias ({} días de backtest): {}. Prioridad en este orden.".format(PUNT_DIAS, txt))
            return True
        finally:
            self.puntuando = None
            self._punt_lock.release()

    def puntuar_pendientes(self):
        """Bots combinados sin puntuación, con estrategias o ajustes nuevos, o con la puntuación de hace más de un día."""
        if self.req is None or self.puntuando:
            return
        for b in self.active_bots():
            if not b.P.get("combinar") or not b.ready:
                continue
            pu = b.meta.get("puntuaciones") or {}
            if pu.get("partes") != K.partes_de(b.P) or pu.get("cfg") != b.cfg_id or now_ms() - (pu.get("t") or 0) > PUNT_CADA_MS:
                threading.Thread(target=self._puntuar_seguro, args=(b,), daemon=True, name="puntuacion").start()
                return

    def _puntuar_seguro(self, b):
        try:
            self.puntuar(b)
        except Exception as e:
            b.log("No se pudo calcular la puntuación de las estrategias: {}".format(e), "error")

    def backtest_chart(self, raw, t_open, t_close, before=80, after=40):
        """Velas y líneas de la estrategia alrededor de una operación del backtest (para verla en el panel)."""
        P, errs = K.validate(raw)
        if errs:
            return {"ok": False, "errors": errs}
        try:
            t_open, t_close = int(t_open), int(t_close)
        except (TypeError, ValueError):
            return {"ok": False, "errors": ["Operación no válida"]}
        key = (P["symbol"], P["timeframe"])
        path = self.cache.path(*key)
        try:
            mt = os.path.getmtime(path)
        except OSError:
            return {"ok": False, "errors": ["No hay velas guardadas de {} {}".format(*key)]}
        memo = getattr(self, "_chart_rows", None)
        mt = (mt, self.externos.modo(P["symbol"]), (self.externos.meta.get(P["symbol"]) or {}).get("importado"))
        if not memo or memo[0] != key or memo[1] != mt:
            rows, _ = self.externos.combinar(P["symbol"], P["timeframe"], sorted(self.cache.load(*key).values()))
            memo = self._chart_rows = (key, mt, rows, [r[0] for r in rows])
        rows, ts = memo[2], memo[3]
        import bisect
        i0, i1 = bisect.bisect_left(ts, t_open), bisect.bisect_left(ts, t_close)
        if i0 >= len(ts) or ts[i0] != t_open or i1 >= len(ts):
            return {"ok": False, "errors": ["Las velas de esa operación ya no están guardadas en la Raspberry"]}
        warm = max(400, K.warmup_bars(P))
        v0 = max(0, i0 - before)
        v1 = min(len(rows), i1 + after + 1)
        if v1 - v0 > 2500:                        # operaciones muy largas: se recorta el principio del contexto
            v0 = max(0, min(v0, v1 - 2500))
        Pq = dict(P, tr_mode="no")                # solo para dibujar: sin filtros ni tendencia
        try:
            m = K.Motor(Pq, self.hour_fn, self.markets.cost(P["symbol"]))
            out = []
            for k in range(max(0, v0 - warm), v1):
                c = rows[k]
                m.on_candle(c)
                if k >= v0:
                    try:
                        v = {a: round(b, 8) for a, b in (m.S.lineas() or {}).items() if isinstance(b, (int, float))}
                    except Exception:
                        v = {}
                    out.append([c[0], c[1], c[2], c[3], c[4], v])
        except Exception as e:
            return {"ok": False, "errors": ["No se pudo dibujar: {}".format(e)]}
        return {"ok": True, "candles": out, "lineas": K.plugin_for(P).lineas, "tf_ms": K.TF_MS[P["timeframe"]]}

    # ---------- resumen ----------
    def estadisticas(self):
        """Todas las operaciones cerradas de todos los bots (también los archivados), en filas compactas, para la
        sección de estadísticas del panel. Los cálculos y gráficos se hacen en el navegador."""
        cols = ["bot", "t_open", "t_close", "side", "r", "why", "dur", "stop_pct", "mfe", "mae", "usd", "cfg"]
        bots, rows = [], []
        for i in self.order:
            b = self.bots.get(i)
            if not b:
                continue
            with b.lock:
                P = b.P
                bots.append({"id": i, "name": b.name, "symbol": P["symbol"], "tf": P["timeframe"],
                             "archived": bool(b.meta.get("archived")), "strategy": b.strategy_name(),
                             "risk_usd": P.get("risk_usd") or 0, "utc_off": P.get("utc_off", 0)})
                for x in b.trades:
                    rows.append([i, x["t_open"], x["t_close"], x["side"], x["r"], x.get("why", ""),
                                 x.get("dur", 0), round(x.get("stop_pct") or 0, 4), x.get("mfe"), x.get("mae"),
                                 x.get("usd"), x.get("cfg")])
        why = {}
        for i in self.order:
            b = self.bots.get(i)
            if b:
                try:
                    why.update(b.why_names())
                except Exception:
                    pass
        rows.sort(key=lambda r: r[2])
        return {"ok": True, "now": now_ms(), "version": VERSION, "cols": cols, "bots": bots, "trades": rows,
                "why": why}

    def overview(self):
        bots = [self.bots[i].summary() for i in self.order if i in self.bots]
        act = [b for b in bots if not b["archived"]]
        return {"version": VERSION, "now": now_ms(), "bots": bots,
                "planes_pendientes": len(PL.STORE.lista("pendiente")) if PL.STORE else 0,
                "totals": {"today_r": round(sum(b["today_r"] for b in act), 2),
                           "week_r": round(sum(b["week_r"] for b in act), 2),
                           "total_r": round(sum(b["total_r"] for b in act), 2), "n": sum(b["n"] for b in act),
                           "open": sum(1 for b in act if b["position"])},
                "feed": self.feed.status() if self.feed else None, "req_reconnects": self.req_reconnects,
                "clock_skew_ms": self.req.offset_ms if self.req else None, "system": system_stats(),
                "proc_start": self.proc_start, "max_bots": self.c.max_bots, "cuentas": self.cuentas.public(),
                "exec": {"enabled": self.cuentas.ordenes, "ready": self.exec_ready(), "problem": self.exec_problem(),
                         "bots": [b["name"] for b in act if (b.get("exec") or {}).get("activo")]},
                "avisos": {"ready": self.avisos.ready(), "last_error": self.avisos.last_error,
                           "sent": self.avisos.sent} if self.avisos else None,
                "riesgo": self.riesgo.public(), "noticias": self.noticias.public(),
                "deriva": self.vigil.resumen(),
                "strategy_errors": [p.file + ": " + p.error for p in K.REGISTRY.list() if p.error],
                "log": list(self.log.recent)[-40:][::-1]}

    def live(self):
        out = {}
        for i in self.order:
            b = self.bots.get(i)
            if not b or b.meta.get("archived"):
                continue
            price, pt, kind = b.live_price()
            lb = self.feed.live_bar(b.market["provider"], b.market["coin"], b.P["timeframe"]) if b.market else None
            out[i] = {"price": price, "t": pt, "kind": kind, "unreal_r": b.unreal(price),
                      "bar": list(lb) if lb else None, "status": b.status(now_ms())[0]}
        return {"now": now_ms(), "bots": out, "feed": self.feed.status() if self.feed else None,
                "ticker": self.ticker(out)}

    def ticker(self, vivo):
        """Cinta de precios (7.4): un elemento por instrumento de los bots activos. El precio sale del mismo flujo en
        directo que ya usan los bots y la variación de las velas que tienen en memoria: no hace peticiones nuevas."""
        now = now_ms()
        cache = getattr(self, "_tk_cache", None)
        if not cache or now - cache["t"] > 30_000:
            grupos = {}
            for b in self.active_bots():
                g = grupos.setdefault(b.P["symbol"], {"bots": [], "hist": None, "tf": None})
                g["bots"].append(b.id)
                h = list(b.hist)
                if h and (g["hist"] is None or (h[-1][0] - h[0][0]) > (g["hist"][-1][0] - g["hist"][0][0])):
                    g["hist"], g["tf"] = h, b.P["timeframe"]
            items = {}
            for sym, g in grupos.items():
                h, ref, spark = g["hist"] or [], None, []
                if h:
                    t24 = h[-1][0] - 86_400_000
                    dentro = [c for c in h if c[0] >= t24]
                    antes = [c for c in h if c[0] <= t24]
                    ref = (antes[-1] if antes else h[0])[4]
                    paso = max(1, len(dentro) // 48)
                    spark = [round(c[4], 8) for c in dentro[::paso]]
                items[sym] = {"symbol": sym, "bots": g["bots"], "ref": ref, "spark": spark,
                              "parcial": bool(h) and (h[-1][0] - h[0][0]) < 86_400_000}
            cache = self._tk_cache = {"t": now, "items": items}
        out = []
        for sym, it in cache["items"].items():
            price = next((vivo[i]["price"] for i in it["bots"] if i in vivo and vivo[i]["price"]), None)
            pos = []
            for i in it["bots"]:
                b = self.bots.get(i)
                st = b.st if b else None
                if st and st.pos:
                    pos.append("L" if st.pos["side"] == 1 else "S")
            ref = it["ref"]
            out.append(dict(it, price=price, pct=round(100.0 * (price - ref) / ref, 3) if price and ref else None,
                            pos=pos))
        return out

    # ---------- vigilancia y Telegram ----------
    def _monitor(self):
        while True:
            time.sleep(30)
            try:
                self.riesgo.revisar()
                self.puntuar_pendientes()
                while self.markets.cambios:
                    sym, viejo, nuevo = self.markets.cambios.pop(0)
                    self.log("{}: MyFundedPerps ha cambiado su mercado de {} a {}. Actualizado solo; los bots de {} "
                             "recargan el historial.".format(sym, viejo, nuevo, sym), "warn")
                    if self.avisos and self.avisos.wants("problemas"):
                        self.avisos.push("problemas", "🔁 <b>{}</b>: MyFundedPerps ha cambiado su mercado ({} → {}). "
                                         "KaBor lo ha actualizado solo.".format(A.esc(sym), A.esc(viejo), A.esc(nuevo)))
                av = self.avisos
                if av.due_summary():
                    av.push("resumen", self.summary_text(daily=True))
                now = now_ms()
                for b in list(self.bots.values()):
                    if b.meta.get("archived") or not b.ready:
                        continue
                    w = b.watch
                    stale = now - (b.last_t + b.tf_ms) > max(30 * 60_000, 6 * b.tf_ms)
                    if stale and not w["nodata_since"]:
                        w["nodata_since"] = now
                        if av.wants("problemas", b.id):
                            av.push("problemas", "⚠️ <b>{}</b>: sin velas nuevas desde las {} ({}). Puede ser cierre "
                                    "de mercado.{}".format(A.esc(b.name), b.local(b.last_t + b.tf_ms, "%H:%M"),
                                                           A.esc(b.info_line()),
                                                           "\nÚltimo error: " + A.esc(b.data_err) if b.data_err else ""))
                            w["nodata_sent"] = True
                    elif not stale and w["nodata_since"]:
                        if w["nodata_sent"] and av.wants("problemas", b.id):
                            av.push("problemas", "✅ <b>{}</b>: vuelven a llegar velas.".format(A.esc(b.name)))
                        w["nodata_since"], w["nodata_sent"] = None, False
            except Exception as e:
                self.log("Vigilancia: {}".format(e), "error")

    def active_bots(self):
        return [self.bots[i] for i in self.order if i in self.bots and not self.bots[i].meta.get("archived")]

    def summary_text(self, daily=False):
        bots = self.active_bots()
        if not bots:
            return "No hay bots activos."
        L = ["📊 <b>{}</b> · {}".format("Resumen del día" if daily else "Estado de los bots",
                                        datetime.now(self.tz).strftime("%d/%m %H:%M"))]
        tot_d = tot_w = 0.0
        for k, b in enumerate(bots, 1):
            s = b.summary()
            tot_d += s["today_r"]
            tot_w += s["week_r"]
            pos = s["position"]
            ptxt = " · {} {} R".format("▲" if pos["side"] == "LONG" else "▼",
                                         A.fr(pos["unreal_r"]) if pos["unreal_r"] is not None else "?") if pos else ""
            flag = "" if s["status"] == "en_marcha" else " ⚠️ " + A.esc(s["status_text"])
            if s.get("trend"):
                flag += " · " + A.esc(b.st.htf.texto_corto() if b.st and b.st.htf else "")
            L.append("\n<b>{}. {}</b> ({} {}){}{}\nhoy {} R · semana {} R · total {} R ({} op.)".format(
                k, A.esc(s["name"]), s["symbol"], s["tf"], ptxt, flag, A.fr(s["today_r"]), A.fr(s["week_r"]),
                A.fr(s["total_r"]), s["n"]))
        L.append("\n<b>Todos: hoy {} R · semana {} R</b>".format(A.fr(tot_d), A.fr(tot_w)))
        rg = self.riesgo.public()
        if rg["config"]["activo"]:
            c, u = rg["config"], rg["config"]["unidad"]
            L.append("🛡 Riesgo global ({}): hoy {} {} · semana {} {}{}".format(
                "sandbox real" if c["ambito"] == "sandbox" else "todos los bots", A.fr(rg["hoy"]), u,
                A.fr(rg["semana"]), u, " · <b>DETENIDO</b>" if rg["guard"] else ""))
        nl = self.noticias.linea_estado()
        if nl:
            L.append(nl)
        blq = [c for c in self.cuentas.lista() if c.bloqueada()[0]]
        if blq:
            L.append("🔒 Bloqueadas: {}".format(", ".join("{} ({})".format(A.esc(c.nombre), A.esc(c.texto_hasta()))
                                                         for c in blq)))
        return "\n".join(L)

    def _find_cuenta(self, arg):
        cs = self.cuentas.lista()
        if arg.isdigit() and 1 <= int(arg) <= len(cs):
            return cs[int(arg) - 1]
        arg = arg.lower()
        return next((c for c in cs if arg and arg in c.nombre.lower()), None)

    def cuentas_text(self):
        g = self.cuentas
        if not g.lista():
            return "No hay cuentas. Dalas de alta en el panel → Cuentas."
        L = ["🏦 <b>Cuentas</b> · envío de órdenes {}".format("activado" if g.ordenes else "<b>desactivado</b>")]
        for k, c in enumerate(g.lista(), 1):
            p = c.public([])
            a, v = p["cuenta"] or {}, p["dia"]
            est = "🔒 bloqueada {}".format(p["bloqueo_texto"]) if p["bloqueo"] else (
                "✅ operativa" if p["listo"] else "⚠️ " + (p["problema"] or "sin conexión"))
            L.append("\n<b>{}. {}</b> · {}\nequity {} $ · hoy {} $ · daily loss usado {} · {} op. hoy".format(
                k, A.esc(c.nombre), A.esc(est),
                A.fpx(a["equity"], 0) if a.get("equity") is not None else "–",
                A.fr(v["pnl_dia"]) if v["pnl_dia"] is not None else "–",
                "{:.0f} %".format(v["dll_usado_pct"]) if v["dll_usado_pct"] is not None else "–", v["ops"]))
        return "\n".join(L)

    def _find_bot(self, arg):
        bots = self.active_bots()
        if not arg:
            return bots[0] if len(bots) == 1 else None
        if arg.isdigit() and 1 <= int(arg) <= len(bots):
            return bots[int(arg) - 1]
        a = arg.lower()
        for b in bots:
            if a in b.name.lower():
                return b
        return None

    def telegram_command(self, text):
        parts = text.split(None, 1)
        cmd = parts[0].split("@")[0].lower()
        arg = parts[1].strip() if len(parts) > 1 else ""
        if cmd in ("/start", "/ayuda", "/help"):
            return ("🤖 <b>KaBor Quant</b>\n/estado — resumen de todos los bots\n/grafico N — gráfico del bot N\n"
                    "/pausar N — pausa las entradas del bot N\n/reanudar N — las reanuda\n/cuentas — estado de las "
                    "cuentas\n/bloquear C [horas|mañana] — bloquea la cuenta C (por defecto hasta mañana)\n/noticias — "
                    "noticias de las próximas 48 h y su bloqueo\n\nN es el "
                    "número que sale en /estado y C el de /cuentas (o parte del nombre)."), None
        if cmd == "/estado":
            return self.summary_text(), None
        if cmd == "/cuentas":
            return self.cuentas_text(), None
        if cmd == "/noticias":
            return self.noticias.texto(), None
        if cmd == "/bloquear":
            bits = arg.split()
            if not bits:
                return "¿Qué cuenta? Escribe por ejemplo <code>/bloquear 1</code> o <code>/bloquear 1 4</code> (horas).", None
            c = self._find_cuenta(bits[0])
            if not c:
                return "No encuentro esa cuenta. Mira los números con /cuentas.", None
            hasta = "mañana"
            if len(bits) > 1 and bits[1].lower() not in ("mañana", "manana"):
                try:
                    h = float(bits[1].replace(",", "."))
                    if not 0.25 <= h <= 168:
                        raise ValueError
                    hasta = now_ms() + int(h * 3_600_000)
                except ValueError:
                    return "Las horas tienen que ser un número entre 0,25 y 168 (o «mañana»).", None
            txt = self.cuentas.bloquear(c.id, "Bloqueo manual desde Telegram", hasta=hasta, origen="manual")
            return "🔒 <b>{}</b> bloqueada {}.{}".format(A.esc(c.nombre), A.esc(c.texto_hasta()),
                                                        "\n" + A.esc(txt) if txt else ""), None
        if cmd in ("/grafico", "/gráfico", "/pausar", "/reanudar"):
            b = self._find_bot(arg)
            if not b:
                return "¿Qué bot? Escribe el número que sale en /estado, por ejemplo <code>{} 1</code>.".format(cmd), None
            if cmd in ("/pausar", "/reanudar"):
                ok, msg = b.control("pausar" if cmd == "/pausar" else "reanudar")
                return ("{} <b>{}</b>: entradas {}.".format("⏸" if cmd == "/pausar" else "▶️", A.esc(b.name),
                                                            "pausadas" if cmd == "/pausar" else "reanudadas")
                        if ok else A.esc(msg)), None
            with b.lock:
                p = b.st.pos if b.st else None
                s = b.summary()
                pos = {"side": p["side"], "entry": p["entry"], "sl": p["sl"], "tp": p.get("tp"),
                       "t_open": p["t_open"]} if p else None
                chart = b.chart_payload(("▲ LONG " if p["side"] == 1 else "▼ SHORT ") + A.fr(s["position"]["unreal_r"] or 0)
                                        + " R" if p else "SIN OPERACIÓN", ("gain" if p["side"] == 1 else "loss") if p
                                        else "muted", pos=pos)
            txt = "<b>{}</b> · {}\n{}\nHoy {} R · semana {} R · total {} R".format(
                A.esc(b.name), A.esc(b.info_line()), A.esc(s["status_text"]), A.fr(s["today_r"]), A.fr(s["week_r"]),
                A.fr(s["total_r"]))
            return txt, chart
        return "No conozco ese comando. Escribe /ayuda.", None

    def zip_all(self):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr("resumen_bots.json", json.dumps(self.overview(), indent=1, ensure_ascii=False, default=str))
            for f in ("bots.json", "instrumentos.json"):
                if os.path.exists(f):
                    z.write(f, f)
            for i in self.order:
                b = self.bots.get(i)
                if b:
                    safe = "".join(ch if ch.isalnum() else "_" for ch in b.name)
                    b.zip_into(z, "bot{}_{}/".format(b.id, safe))
            if os.path.isdir("logs"):
                for f in sorted(os.listdir("logs")):
                    z.write(os.path.join("logs", f), "programa/" + f)
        return buf.getvalue()


# ============================ panel web ============================
LOGIN_CSP = ("default-src 'none'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; "
             "img-src 'self' data:; connect-src 'self'; manifest-src 'self'; form-action 'self'; frame-ancestors 'none'; "
             "base-uri 'none'")


def make_handler(hub, cfg, acc):
    html_path = os.path.join(HERE, "panel.html")
    login_path = os.path.join(WEB_DIR, "login.html")
    mcp = PL.ConectorMCP(hub, cfg.mcp_token, VERSION) if len(cfg.mcp_token) >= 24 else None

    class Handler(BaseHTTPRequestHandler):
        server_version = "KaBorQuant"
        sys_version = ""
        timeout = 60

        def log_message(self, *a):
            pass

        # ---------- acceso ----------
        def _ip(self):
            ip = self.client_address[0]
            if cfg.proxy_https and ip in ("127.0.0.1", "::1"):
                fwd = self.headers.get("X-Forwarded-For", "").split(",")[0].strip()
                if fwd:
                    ip = fwd
            return ip

        def _secure(self):
            return cfg.https or cfg.proxy_https

        def _cookie(self):
            try:
                m = SimpleCookie(self.headers.get("Cookie", "")).get(AC.COOKIE)
            except CookieError:
                return ""
            return m.value if m else ""

        def _user(self):
            """Quién hace la petición: el usuario de su sesión, «api» con el token de API, o None."""
            if not cfg.login:
                return "local"
            got = self.headers.get("X-Token", "")
            if cfg.token and got and hmac.compare_digest(got.encode(), cfg.token.encode()):
                return "api"
            return acc.sesion(self._cookie())

        def _same_origin(self):
            """Contra CSRF: las escrituras desde el navegador llevan la cabecera X-KQ (otra web no puede ponerla
            sin permiso CORS) y, si el navegador manda Origin, tiene que ser este mismo servidor."""
            if self.headers.get("X-KQ") != "1":
                return False
            o = self.headers.get("Origin")
            return not o or urlparse(o).netloc == self.headers.get("Host", "")

        def _set_cookie(self, token, max_age):
            parts = ["{}={}".format(AC.COOKIE, token), "Path=/", "HttpOnly", "SameSite=Strict"]
            if self._secure():
                parts.append("Secure")
            if max_age is not None:
                parts.append("Max-Age={}".format(max_age))
            return "; ".join(parts)

        def _redirect(self, where):
            self._send(303, "", "text/plain; charset=utf-8", {"Location": where})

        def _login(self):
            if not cfg.login:
                return self._json({"ok": True})
            if not self._same_origin():
                return self._json({"ok": False, "error": "Petición no permitida"}, 403)
            try:
                body = self._body()
            except (ValueError, UnicodeDecodeError) as e:
                return self._json({"ok": False, "error": str(e)}, 400)
            if not acc.hay_usuarios():
                return self._json({"ok": False, "error": "Aún no hay ningún usuario. Créalo en la Raspberry con "
                                                         "./bot.sh usuario"}, 409)
            ip, nombre = self._ip(), str(body.get("usuario", ""))[:64]
            tok, err, wait, nuevo_bloqueo = acc.entrar(nombre, str(body.get("clave", ""))[:256], ip,
                                        self.headers.get("User-Agent", ""), bool(body.get("recordar")),
                                        codigo=str(body.get("codigo") or "")[:32])
            if err == AC.PIDE_CODIGO:
                return self._json({"ok": False, "codigo": True, "error": "Escribe el código de tu app de verificación"},
                                  401)
            if tok is None:
                if nuevo_bloqueo:
                    hub.log("Panel: acceso bloqueado 15 min para {} tras varios intentos fallidos".format(ip), "warn")
                    if hub.avisos:
                        threading.Thread(target=self._alerta, daemon=True, args=(
                            "🔒 <b>{}: acceso bloqueado</b>\nVarios intentos fallidos de entrar al panel desde "
                            "<code>{}</code>. Esa dirección queda bloqueada 15 minutos.".format(APP, A.esc(ip)),)).start()
                elif not wait:
                    hub.log("Panel: inicio de sesión fallido desde {}".format(ip), "warn")
                    time.sleep(0.5)
                return self._json({"ok": False, "error": err, "espera": wait}, 429 if wait else 401)
            hub.log("Panel: sesión iniciada por {} desde {}".format(nombre.strip(), ip))
            return self._json({"ok": True}, extra={"Set-Cookie": self._set_cookie(tok, acc.max_age(tok))})

        def _cuenta(self, user, body):
            if user in ("api", "local"):
                return self._json({"ok": False, "errors": ["Solo disponible con una sesión de usuario"]}, 400)
            op, tok = body.get("op"), self._cookie()
            if op == "clave":
                recordar = acc.max_age(tok) is not None
                try:
                    acc.cambiar_clave(user, str(body.get("actual", ""))[:256], str(body.get("nueva", ""))[:256])
                except ValueError as e:
                    time.sleep(0.5)
                    return self._json({"ok": False, "errors": [str(e)]})
                hub.log("Panel: {} ha cambiado su contraseña (se cierran sus sesiones)".format(user))
                nuevo, _, _, _ = acc.entrar(user, str(body.get("nueva", ""))[:256], self._ip(),
                                            self.headers.get("User-Agent", ""), recordar, sin_codigo=True)
                extra = {"Set-Cookie": self._set_cookie(nuevo, acc.max_age(nuevo))} if nuevo else None
                return self._json({"ok": True, "msg": "Se han cerrado las sesiones de los demás dispositivos. "
                                                      "Este sigue dentro."}, extra=extra)
            if op == "totp_iniciar":
                secreto, uri = acc.totp_iniciar(user)
                return self._json({"ok": True, "secreto": secreto, "uri": uri})
            if op == "totp_confirmar":
                try:
                    rec = acc.totp_confirmar(user, str(body.get("codigo", ""))[:32])
                except ValueError as e:
                    time.sleep(0.5)
                    return self._json({"ok": False, "errors": [str(e)]})
                hub.log("Panel: {} ha activado la verificación en dos pasos".format(user))
                return self._json({"ok": True, "recuperacion": rec})
            if op == "totp_quitar":
                try:
                    acc.totp_quitar(user, str(body.get("clave", ""))[:256], str(body.get("codigo", ""))[:32])
                except ValueError as e:
                    time.sleep(0.5)
                    return self._json({"ok": False, "errors": [str(e)]})
                hub.log("Panel: {} ha quitado la verificación en dos pasos".format(user), "warn")
                return self._json({"ok": True})
            if op == "cerrar_otras":
                n = acc.cerrar_otras(user, tok)
                hub.log("Panel: {} ha cerrado {} sesión(es) de otros dispositivos".format(user, n))
                return self._json({"ok": True, "n": n})
            if op == "cerrar":
                n = acc.cerrar_sesion_id(user, str(body.get("id", ""))[:64])
                return self._json({"ok": bool(n), "n": n, "errors": [] if n else ["Esa sesión ya no existe"]})
            return self._json({"ok": False, "errors": ["Operación desconocida"]}, 400)

        # ---------- conector MCP (Claude) ----------
        def _mcp(self, metodo):
            parts = urlparse(self.path).path.strip("/").split("/")
            tok = parts[1] if len(parts) == 2 else ""
            if mcp is None or not mcp.autorizado(tok):
                time.sleep(0.5)
                return self._send(404, "No existe", "text/plain; charset=utf-8")
            if metodo != "POST":
                return self._send(405, "", "text/plain; charset=utf-8", {"Allow": "POST"})
            try:
                msg = self._body()
            except (ValueError, UnicodeDecodeError):
                return self._json({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "JSON no válido"}},
                                  400)
            if PL.STORE is None:
                return self._send(503, "No disponible", "text/plain; charset=utf-8")
            res = [r for r in (mcp.atender(m) for m in msg) if r] if isinstance(msg, list) else mcp.atender(msg)
            if not res:
                return self._send(202, "", "text/plain; charset=utf-8")
            return self._json(res)

        def _precio(self, sym, tf):
            try:
                cs = hub.req.candles(hub.markets.resolve(sym), tf, 3)
                return cs[-1][4] if cs else None
            except Exception:
                return None

        def _externos_payload(self):
            out = hub.externos.publico()
            out["instrumentos"] = [{k: i.get(k) for k in ("symbol", "provider", "coin", "category", "market_id")}
                                   for i in hub.markets.list()]
            out["bt_max"] = cfg.bt_max
            return out

        def _externos_post(self, body):
            ex, op = hub.externos, body.get("op")
            sym = str(body.get("simbolo", "")).upper()
            if op in ("subir", "importar", "modo", "borrar") and not hub.markets.get(sym):
                raise ValueError("{} no está en tu lista de instrumentos".format(sym or "Ese instrumento"))
            if op == "subir":
                nombre = str(body.get("nombre", ""))[:120]
                if not EX.NOMBRE_RE.match(nombre):
                    raise ValueError("El archivo tiene que ser .csv o .txt")
                parte, total = int(body.get("parte", -1)), int(body.get("total", 0))
                ruta = ex.subir_parte(str(body.get("sid", "")), parte, total, str(body.get("datos", "")))
                if parte == total - 1:
                    ex.importar(sym, ruta, nombre, borrar_despues=True)
                    hub.log("Datos externos: importando {} para {}".format(nombre, sym))
            elif op == "importar":
                nombre = os.path.basename(str(body.get("nombre", "")))
                ruta = os.path.join(ex.entrada, nombre)
                if not EX.NOMBRE_RE.match(nombre) or not os.path.isfile(ruta):
                    raise ValueError("No existe ese archivo en data/externos/entrada")
                ex.importar(sym, ruta, nombre)
            elif op == "modo":
                ex.poner_modo(sym, str(body.get("modo", "")))
            elif op == "borrar":
                ex.borrar(sym)
            elif op != "estado":
                return self._json({"ok": False, "errors": ["Operación desconocida"]}, 400)
            return self._json(self._externos_payload())

        # ---------- retiros y gastos (solo panel, nunca por el conector) ----------
        def _fiscal_payload(self, anio=None):
            fi = hub.fiscal
            anios = fi.anios()
            try:
                anio = int(anio or anios[0])
            except ValueError:
                anio = anios[0]
            return {"ok": True, "anios": anios, "anio": anio, "movimientos": fi.del_anio(anio)[::-1],
                    "resumen": fi.resumen(anio), "mensual": fi.mensual(anio), "entidades": fi.entidades,
                    "tipos": FI.TIPOS, "metodos": FI.METODOS, "categorias": FI.CATEGORIAS_GASTO}

        def _fiscal_get(self, u):
            q = parse_qs(u.query)
            anio = (q.get("anio") or [""])[0]
            if u.path == "/api/fiscal":
                return self._json(self._fiscal_payload(anio))
            if u.path == "/api/fiscal/csv":
                a = int(anio or time.strftime("%Y"))
                return self._send(200, hub.fiscal.csv(a), "text/csv; charset=utf-8",
                                  {"Content-Disposition": 'attachment; filename="retiros_y_gastos_{}.csv"'.format(a)})
            if u.path == "/api/fiscal/informe":
                a = int(anio or time.strftime("%Y"))
                return self._send(200, hub.fiscal.informe_html(a), "text/html; charset=utf-8")
            if u.path == "/api/fiscal/justificante":
                p = hub.fiscal.ruta_adjunto((q.get("n") or [""])[0])
                if not p:
                    return self._send(404, "No existe", "text/plain; charset=utf-8")
                ext = p.rsplit(".", 1)[-1].lower()
                ct = {"pdf": "application/pdf", "png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg",
                      "webp": "image/webp"}.get(ext, "application/octet-stream")
                with open(p, "rb") as f:
                    return self._send(200, f.read(), ct, {"Content-Disposition": 'inline; filename="{}"'.format(
                        os.path.basename(p))})
            return self._send(404, "No existe", "text/plain; charset=utf-8")

        def _fiscal_post(self, body):
            fi, op = hub.fiscal, body.get("op")
            anio = body.get("anio")
            if op == "guardar":
                m, errs = fi.guardar(body.get("mov") or {})
                if errs:
                    return self._json({"ok": False, "errors": errs})
                return self._json(dict(self._fiscal_payload(anio or m["fecha"][:4]), msg="Guardado", id=m["id"]))
            if op == "borrar":
                fi.borrar(str(body.get("id", "")))
                return self._json(dict(self._fiscal_payload(anio), msg="Movimiento borrado"))
            if op == "adjuntar":
                fi.adjuntar_parte(str(body.get("id", "")), body.get("nombre", ""), body.get("parte", -1),
                                  body.get("total", 0), str(body.get("datos", "")))
                return self._json(self._fiscal_payload(anio))
            if op == "quitar":
                fi.quitar_adjunto(str(body.get("id", "")), str(body.get("nombre", "")))
                return self._json(self._fiscal_payload(anio))
            if op == "bce":
                return self._json({"ok": True, "bce": fi.cambio_bce(str(body.get("fecha", "")))})
            if op in ("entidad", "entidad_borrar"):
                fi.guardar_entidad(body.get("nombre", ""), body.get("datos"), borrar=op == "entidad_borrar")
                return self._json(self._fiscal_payload(anio))
            return self._json({"ok": False, "errors": ["Operación desconocida"]}, 400)

        def _planes_payload(self):
            bots = [{"name": b.name, "symbol": b.P["symbol"], "tf": b.P["timeframe"],
                     "riesgo": float(b.P.get("ex_riesgo_usd") or 0) if b.P.get("ex_riesgo_modo") == "importe fijo ($)"
                     and getattr(b.ex, "activo", lambda: False)() else float(b.P.get("risk_usd") or 0)}
                    for b in hub.bots.values()
                    if b.P.get("estrategia") == PL.ESTRATEGIA_ID and not b.meta.get("archived")]
            planes = PL.STORE.lista() if PL.STORE else []
            for p in planes:
                if p["estado"] in PL.VIVOS:
                    try:
                        p["tamano_info"] = PL.tamano_info(hub, p)
                    except Exception:
                        p["tamano_info"] = []
            return {"ok": True, "planes": planes, "condiciones": PL.CONDICIONES,
                    "instrumentos": [i["symbol"] for i in hub.markets.list()], "tfs": list(PL.TF_MS),
                    "bots": bots, "mcp": mcp is not None, "espera_min": cfg.plan_espera, "ahora": PL.ahora()}

        def _planes_post(self, body):
            st, op, pid = PL.STORE, body.get("op"), str(body.get("id", ""))
            if st is None:
                raise ValueError("Los planes no están disponibles")
            msg = None
            if op == "crear":
                d = body.get("plan") or {}
                inst = [i["symbol"] for i in hub.markets.list()]
                sim = str(d.get("simbolo", "")).upper()
                precio = self._precio(sim, d.get("temporalidad")) if sim in inst and d.get("temporalidad") in PL.TF_MS \
                    else None
                plan, errs, avisos = PL.validar(d, inst, precio)
                if errs:
                    return self._json({"ok": False, "errors": errs})
                p = st.crear(plan, "panel")
                msg = "Plan {} creado como pendiente".format(p["id"]) + (". " + " · ".join(avisos) if avisos else "")
            elif op == "confirmar":
                st.confirmar(pid)
                msg = "Plan activo: el bot lo vigila desde el próximo cierre"
            elif op == "cancelar":
                _, msg = st.cancelar(pid, desde_panel=True)
                msg = msg or "Plan cancelado"
            elif op == "retirar":
                st.retirar_cancelacion(pid)
                msg = "Cancelación retirada: el plan sigue activo"
            else:
                return self._json({"ok": False, "errors": ["Operación desconocida"]}, 400)
            return self._json(dict(self._planes_payload(), msg=msg))

        def _alerta(self, text):
            try:
                hub.avisos.send_now(text)
            except Exception:
                pass

        # ---------- respuestas ----------
        def _send(self, code, body, ctype, extra=None, cache="no-store"):
            if isinstance(body, str):
                body = body.encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", cache)
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Referrer-Policy", "no-referrer")
            if cfg.proxy_https:
                self.send_header("Strict-Transport-Security", "max-age=31536000")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def _json(self, obj, code=200, extra=None):
            self._send(code, json.dumps(obj, ensure_ascii=False, default=str), "application/json; charset=utf-8",
                       extra)

        def _body(self):
            n = int(self.headers.get("Content-Length") or 0)
            if n > 600_000:
                raise ValueError("petición demasiado grande")
            raw = self.rfile.read(n) if n else b""
            return json.loads(raw.decode("utf-8")) if raw else {}

        def _bot(self, parts):
            b = hub.bots.get(parts[2]) if len(parts) > 2 else None
            if b is None:
                raise KeyError("No existe ese bot")
            return b

        def _config_payload(self, b=None):
            strats = {}
            for p in K.REGISTRY.list():
                if p.error:
                    continue
                strats[p.id] = {"id": p.id, "nombre": p.nombre, "version": p.version, "descripcion": p.descripcion,
                                "schema": p.schema, "defaults": p.defaults, "reload_keys": list(p.reload_keys),
                                "filtros": dict(K.FILTER_NAMES, **p.filtros)}
            out = {"estrategias": strats, "default_strategy": K.REGISTRY.default_id(),
                   "market_keys": list(K.MARKET_KEYS),
                   "instruments": hub.markets.list(), "in_use": sorted(hub.active_symbols()),
                   "cuentas": [{"id": c.id, "nombre": c.nombre, "activa": c.activa, "listo": c.listo(),
                                "bloqueada": c.bloqueada()[0], "mult": c.mult} for c in hub.cuentas.lista()],
                   "cuenta_defecto": hub.cuentas.por_defecto(), "ordenes": hub.cuentas.ordenes}
            if b is not None:
                hist = []
                for c in b.store.history():
                    tr = [x for x in b.trades if x.get("cfg") == c["id"]]
                    hist.append({"id": c["id"], "saved_at": c.get("saved_at"), "note": c.get("note", ""),
                                 "params": c["params"], "n": len(tr), "r": round(sum(x["r"] for x in tr), 2)})
                out.update({"id": b.cfg_id, "params": b.P, "history": hist[::-1]})
            else:
                out.update({"id": None, "params": K.defaults(), "history": []})
            if out["params"].get("estrategia") not in strats:
                out["strategy_missing"] = out["params"].get("estrategia")
            return out

        def do_GET(self):
            u = urlparse(self.path)
            if u.path.startswith("/mcp/"):
                return self._mcp("GET")
            if u.path in ESTATICOS:
                name, ctype = ESTATICOS[u.path]
                try:
                    with open(os.path.join(WEB_DIR, name), "rb") as fh:
                        return self._send(200, fh.read(), ctype, cache="no-cache" if name in (
                            "sw.js", "manifest.webmanifest", "i18n.js") or name.startswith("i18n/") else "public, max-age=86400")
                except OSError:
                    return self._send(404, "No existe", "text/plain; charset=utf-8")
            if u.path == "/api/sesion":
                user = self._user()
                return self._json({"ok": True, "usuario": user, "login": cfg.login, "app": APP, "version": VERSION,
                                   "hay_usuarios": acc.hay_usuarios() if cfg.login else True,
                                   "https": self._secure()})
            if u.path in ("/login", "/login.html"):
                if not cfg.login or self._user():
                    return self._redirect("/")
                with open(login_path, encoding="utf-8") as f:
                    return self._send(200, f.read(), "text/html; charset=utf-8", {"Content-Security-Policy": LOGIN_CSP})
            user = self._user()
            if user is None:
                if u.path in ("/", "/index.html"):
                    return self._redirect("/login")
                return self._json({"ok": False, "login": True, "errors": ["Inicia sesión de nuevo"]}, 401)
            if u.path == "/api/cuenta":
                real = user not in ("api", "local")
                return self._json({"ok": True, "usuario": user, "real": real, "app": APP, "version": VERSION,
                                   "https": self._secure(), "login": cfg.login,
                                   "sesion_horas": cfg.sesion_horas, "sesion_dias": cfg.sesion_dias,
                                   "sesiones": acc.sesiones_de(user, self._cookie()) if real else [],
                                   "totp": acc.totp_estado(user) if real else None})
            parts = u.path.strip("/").split("/")
            qs = parse_qs(u.query)
            try:
                if u.path in ("/", "/index.html"):
                    with open(html_path, encoding="utf-8") as f:
                        return self._send(200, f.read(), "text/html; charset=utf-8")
                if u.path.startswith("/ayuda/"):         # guía del panel (Ayuda): texto e imágenes de web/ayuda
                    nombre = u.path[len("/ayuda/"):]
                    m = re.match(r"^(?:img/(?:en/|fr/)?)?[a-z0-9_.-]{1,60}\.(html|jpg|png|webp)$", nombre)
                    if m and ".." in nombre:
                        m = None
                    ruta = os.path.join(WEB_DIR, "ayuda", nombre)
                    if not m or not os.path.isfile(ruta):
                        return self._send(404, "No existe", "text/plain; charset=utf-8")
                    tipo = {"html": "text/html; charset=utf-8", "jpg": "image/jpeg", "png": "image/png",
                            "webp": "image/webp"}[m.group(1)]
                    with open(ruta, "rb") as fh:
                        return self._send(200, fh.read(), tipo, cache="no-cache" if m.group(1) == "html" else "public, max-age=86400")
                if u.path == "/api/tradingview":
                    return self._json({"ok": True, "instrumentos": hub.markets.tradingview()})
                if u.path == "/api/velas":
                    sim, tf = str(qs.get("simbolo", [""])[0]).upper(), qs.get("tf", ["15m"])[0]
                    if tf not in K.TF_MS or not hub.markets.get(sim):
                        return self._json({"ok": False, "errors": ["Instrumento o temporalidad no válidos"]}, 400)
                    n = max(50, min(int(qs.get("n", ["500"])[0] or 500), 1500))
                    cs = hub.req.candles(hub.markets.resolve(sim), tf, n)
                    return self._json({"ok": True, "simbolo": sim, "tf": tf, "tf_ms": K.TF_MS[tf],
                                       "velas": [list(c[:5]) for c in cs]})
                if u.path == "/api/dibujos":
                    sim = str(qs.get("simbolo", [""])[0]).upper()
                    return self._json({"ok": True, "simbolo": sim, "objetos": PL.DIBUJOS.de(sim) if PL.DIBUJOS else [],
                                       "roles_nivel": PL.ROLES_NIVEL, "roles_zona": PL.ROLES_ZONA})
                if u.path == "/api/resumen":
                    return self._json(hub.overview())
                if u.path == "/api/estadisticas":
                    return self._json(hub.estadisticas())
                if u.path == "/api/optimizar":
                    return self._json(hub.optim.estado())
                if u.path == "/api/cartera":
                    return self._json(hub.cartera.estado())
                if u.path == "/api/vigilante":
                    return self._json(hub.vigil.public())
                if u.path == "/api/diario":
                    try:
                        dias = int((qs.get("dias") or ["30"])[0])
                    except ValueError:
                        dias = 30
                    return self._json(hub.diario.listar(dias))
                if u.path == "/api/vivo":
                    return self._json(hub.live())
                if u.path == "/api/esquema":
                    return self._json(self._config_payload())
                if u.path == "/api/estrategias":
                    return self._json(hub.strategies())
                if len(parts) == 3 and parts[:2] == ["api", "estrategias"] and parts[2].endswith(".py"):
                    p = K.REGISTRY.any(parts[2][:-3])
                    if not p or not os.path.exists(p.path):
                        raise KeyError("No existe esa estrategia")
                    with open(p.path, "rb") as fh:
                        return self._send(200, fh.read(), "text/x-python; charset=utf-8",
                                          {"Content-Disposition": 'attachment; filename="{}"'.format(p.file)})
                if u.path == "/api/avisos":
                    return self._json(hub.avisos.public() if hub.avisos else {"cfg": None})
                if u.path == "/api/cuentas":
                    return self._json(hub.cuentas.public())
                if u.path == "/api/planes":
                    return self._json(self._planes_payload())
                if u.path.startswith("/api/fiscal"):
                    return self._fiscal_get(u)
                if u.path == "/api/temas":
                    return self._json({"ok": True, "temas": hub.temas.lista(), "claves": list(TM.CLAVES),
                                       "bases": list(TM.BASES)})
                if u.path == "/api/externos":
                    return self._json(self._externos_payload())
                if u.path == "/api/mercados":
                    return self._json({"ok": True, "results": hub.markets.search(qs.get("q", [""])[0])})
                if u.path == "/api/informe.zip":
                    name = "informe_bots_{}.zip".format(datetime.now().strftime("%Y%m%d_%H%M"))
                    return self._send(200, hub.zip_all(), "application/zip",
                                      {"Content-Disposition": 'attachment; filename="{}"'.format(name)})
                if len(parts) >= 4 and parts[:2] == ["api", "b"]:
                    b = self._bot(parts)
                    what = parts[3]
                    if what == "estado":
                        return self._json(b.snapshot(scope="cfg" if qs.get("ambito", [""])[0] == "cfg" else "all"))
                    if what == "config":
                        return self._json(self._config_payload(b))
                    if what == "estrategias":
                        return self._json(b.informe_estrategias())
                    if what == "informe.md":
                        with b.lock:
                            md = b.report_md("Informe completo · " + b.name, b.trades)
                        return self._send(200, md, "text/markdown; charset=utf-8")
                    if what == "informe.zip":
                        buf = io.BytesIO()
                        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
                            b.zip_into(z, "")
                        name = "informe_bot{}_{}.zip".format(b.id, datetime.now().strftime("%Y%m%d_%H%M"))
                        return self._send(200, buf.getvalue(), "application/zip",
                                          {"Content-Disposition": 'attachment; filename="{}"'.format(name)})
                self._send(404, "No existe", "text/plain; charset=utf-8")
            except KeyError as e:
                self._json({"ok": False, "errors": [str(e).strip("'")]}, 404)
            except Exception as e:
                self._json({"ok": False, "error": str(e)}, 500)

        def do_DELETE(self):
            if urlparse(self.path).path.startswith("/mcp/"):
                return self._mcp("DELETE")
            self._send(405, "", "text/plain; charset=utf-8")

        def do_POST(self):
            u = urlparse(self.path)
            if u.path.startswith("/mcp/"):
                return self._mcp("POST")
            if u.path == "/api/login":
                return self._login()
            user = self._user()
            if user is None:
                return self._json({"ok": False, "login": True, "errors": ["Inicia sesión de nuevo"]}, 401)
            if user != "api" and cfg.login and not self._same_origin():
                return self._json({"ok": False, "errors": ["Petición no permitida"]}, 403)
            if u.path == "/api/logout":
                tok = self._cookie()
                acc.salir(tok)
                hub.log("Panel: sesión cerrada por {}".format(user))
                return self._json({"ok": True}, extra={"Set-Cookie": self._set_cookie("", 0)})
            try:
                body = self._body()
            except (ValueError, UnicodeDecodeError) as e:
                return self._json({"ok": False, "errors": [str(e)]}, 400)
            if u.path == "/api/cuenta":
                return self._cuenta(user, body)
            parts = u.path.strip("/").split("/")
            try:
                if u.path == "/api/backtest/grafico":
                    res = hub.backtest_chart(body.get("params"), body.get("t_open"), body.get("t_close"))
                    return self._json(res, 200 if res.get("ok") else 400)
                if u.path == "/api/diario":
                    op = body.get("op")
                    res = hub.diario.anotar(body) if op == "anotar" else hub.diario.manual(body) if op == "manual" \
                        else hub.diario.borrar(body.get("k")) if op == "borrar" else \
                        {"ok": False, "errors": ["Operación desconocida"]}
                    return self._json(res, 200 if res.get("ok") else 400)
                if u.path == "/api/vigilante":
                    op = body.get("op")
                    res = hub.vigil.configurar(body.get("cfg")) if op == "config" else \
                        hub.vigil.recalcular(body.get("id")) if op == "recalcular" else \
                        {"ok": False, "errors": ["Operación desconocida"]}
                    return self._json(res, 200 if res.get("ok") else 400)
                if u.path == "/api/cartera":
                    op = body.get("op")
                    res = hub.cartera.cancelar() if op == "cancelar" else hub.cartera.empezar(body) if op == "empezar" \
                        else {"ok": False, "errors": ["Operación desconocida"]}
                    return self._json(res, 200 if res.get("ok") else 400)
                if u.path == "/api/optimizar":
                    op = body.get("op")
                    res = hub.optim.cancelar() if op == "cancelar" else hub.optim.empezar(body) if op == "empezar" \
                        else {"ok": False, "errors": ["Operación desconocida"]}
                    return self._json(res, 200 if res.get("ok") else 400)
                if u.path == "/api/backtest":
                    if hub.req is None:
                        return self._json({"ok": False, "errors": ["El programa aún está arrancando"]}, 503)
                    res = hub.backtest(body.get("params"), body.get("days"), body.get("orden"), body.get("variante"))
                    return self._json(res, 200 if res.get("ok") else 400)
                if u.path == "/api/riesgo":
                    if body.get("op") == "reactivar":
                        ok, msg = hub.riesgo.reactivar()
                        return self._json({"ok": ok, "msg": msg}, 200 if ok else 400)
                    ok, errs = hub.riesgo.configurar(body.get("config"))
                    return self._json({"ok": ok, "errors": errs, "riesgo": hub.riesgo.public()}, 200 if ok else 400)
                if u.path == "/api/noticias":
                    if body.get("op") == "actualizar":
                        ok, msg = hub.noticias.actualizar_ya()
                        return self._json({"ok": ok, "msg": msg, "noticias": hub.noticias.public()},
                                          200 if ok else 400)
                    ok, errs = hub.noticias.configurar(body.get("config"))
                    return self._json({"ok": ok, "errors": errs, "noticias": hub.noticias.public()},
                                      200 if ok else 400)
                if u.path == "/api/cuentas":
                    g, op = hub.cuentas, body.get("op")
                    if op == "guardar":
                        c, errs = g.guardar_cuenta(body.get("cuenta"))
                        if errs:
                            return self._json({"ok": False, "errors": errs}, 400)
                        return self._json({"ok": True, "id": c.id, **g.public()})
                    if op == "borrar":
                        g.borrar(str(body.get("id", "")))
                        return self._json({"ok": True, **g.public()})
                    if op == "visibles":
                        lst = g.cuentas_visibles(body.get("clave"), str(body.get("id") or ""))
                        return self._json({"ok": True, "cuentas": lst})
                    if op == "ordenes":
                        g.set_ordenes(bool(body.get("activo")))
                        return self._json({"ok": True, **g.public()})
                    if op == "bloquear":
                        cid = str(body.get("id", ""))
                        dur = body.get("duracion")
                        if dur == "mañana":
                            hasta = "mañana"
                        else:
                            h = float(body.get("horas") or 0)
                            if not 0.25 <= h <= 168:
                                raise ValueError("Duración: entre 15 minutos y 7 días")
                            hasta = now_ms() + int(h * 3_600_000)
                        txt = g.bloquear(cid, str(body.get("motivo") or "Bloqueo manual desde el panel ({})".format(
                            user))[:200], hasta=hasta, origen="manual", cerrar=bool(body.get("cerrar")),
                            estricto=bool(body.get("estricto")))
                        return self._json({"ok": True, "msg": txt, **g.public()})
                    if op == "desbloquear":
                        msg = g.desbloquear(str(body.get("id", "")))
                        return self._json({"ok": True, "msg": msg, **g.public()})
                    if op == "leer":
                        c = g.get(str(body.get("id", "")))
                        if not c:
                            raise KeyError("No existe esa cuenta")
                        c.next_resolve = 0
                        g.leer(c)
                        return self._json({"ok": True, **g.public()})
                    return self._json({"ok": False, "errors": ["Operación desconocida"]}, 400)
                if u.path == "/api/bots":
                    op = body.get("op")
                    if op == "create":
                        P, errs = K.validate(body.get("params"))
                        if not errs and not hub.markets.get(P["symbol"]):
                            errs = ["{} no está en tu lista de instrumentos".format(P["symbol"])]
                        errs = errs or hub.exec_conflicts(P)
                        if errs:
                            return self._json({"ok": False, "errors": errs}, 400)
                        meta = hub.create(str(body.get("name", "")), P, str(body.get("note", "")))
                        return self._json({"ok": True, "id": meta["id"], "name": meta["name"]})
                    bid = str(body.get("id", ""))
                    if op == "archive":
                        hub.archive(bid)
                    elif op == "unarchive":
                        hub.unarchive(bid)
                    elif op == "rename":
                        hub.rename(bid, str(body.get("name", "")))
                    else:
                        return self._json({"ok": False, "errors": ["Operación desconocida"]}, 400)
                    return self._json({"ok": True})
                if u.path == "/api/dibujos":
                    sim = str(body.get("simbolo", "")).upper()
                    if not hub.markets.get(sim):
                        return self._json({"ok": False, "errors": ["{} no está en tus instrumentos".format(sim)]}, 400)
                    objs = PL.DIBUJOS.guardar(sim, body.get("objetos"), K.TF_MS)
                    return self._json({"ok": True, "simbolo": sim, "objetos": objs})
                if u.path == "/api/tradingview":
                    hub.markets.set_tradingview(str(body.get("symbol", "")), body.get("tv"))
                    return self._json({"ok": True, "instrumentos": hub.markets.tradingview()})
                if u.path == "/api/instrumentos":
                    op = body.get("op")
                    if op == "add":
                        item = hub.markets.add(str(body.get("market_id", "")), body.get("symbol") or None,
                                               body.get("cost_pct"))
                        msg = "{} añadido".format(item["symbol"])
                        try:
                            n = len(hub.req.candles(hub.markets.resolve(item["symbol"]), "5m", 300))
                            msg += ": el feed devuelve {} velas de 5m".format(n)
                            if n < 200:
                                msg += " (pocas: el historial para probar será corto)"
                        except Exception as e:
                            msg += ", pero no se pudieron leer velas: {}".format(e)
                        hub.log("Instrumento {} ({}) añadido. {}".format(item["symbol"], item["market_id"], msg))
                        return self._json({"ok": True, "msg": msg, "instruments": hub.markets.list()})
                    if op == "remove":
                        hub.markets.remove(str(body.get("symbol", "")), hub.active_symbols())
                        return self._json({"ok": True, "instruments": hub.markets.list()})
                    if op == "cost":
                        sym = str(body.get("symbol", ""))
                        hub.markets.set_cost(sym, body.get("cost_pct"))
                        for b in list(hub.bots.values()):
                            b.instrument_changed(sym)
                        return self._json({"ok": True, "instruments": hub.markets.list()})
                    return self._json({"ok": False, "errors": ["Operación desconocida"]}, 400)
                if u.path == "/api/estrategias":
                    op = body.get("op")
                    if op == "subir":
                        p, msg = hub.install_strategy(str(body.get("filename", "")), str(body.get("code", "")))
                        return self._json({"ok": True, "msg": msg, "id": p.id})
                    if op == "borrar":
                        hub.remove_strategy(str(body.get("id", "")))
                        return self._json({"ok": True})
                    if op == "recargar":
                        changed = hub.rescan_strategies()
                        st = hub.strategies()
                        bad = [x for x in st["items"] if x["error"]]
                        msg = "{} estrategias cargadas{}{}".format(
                            len(st["items"]) - len(bad), ", {} con error".format(len(bad)) if bad else "",
                            ", {} actualizada{}".format(len(changed), "s" if len(changed) > 1 else "") if changed else "")
                        return self._json({"ok": True, "msg": msg})
                    return self._json({"ok": False, "errors": ["Operación desconocida"]}, 400)
                if u.path == "/api/avisos":
                    op = body.get("op")
                    av = hub.avisos
                    if op == "guardar":
                        av.update(body.get("cfg") or {})
                        return self._json({"ok": True, **av.public()})
                    if op == "token":
                        if body.get("token"):
                            av.update({"token": body["token"]})
                        me = av.check_token()
                        return self._json({"ok": True, "msg": "Token correcto: tu bot es @{}. Ahora escríbele "
                                           "/start desde Telegram y pulsa «Detectar mi chat».".format(me.get("username")),
                                           **av.public()})
                    if op == "probar":
                        bots = hub.active_bots()
                        chart = None
                        if bots and bots[0].chart:
                            b = bots[0]
                            with b.lock:
                                p = b.st.pos if b.st else None
                                chart = b.chart_payload("PRUEBA", "gold", pos={
                                    "side": p["side"], "entry": p["entry"], "sl": p["sl"], "tp": p.get("tp"),
                                    "t_open": p["t_open"]} if p else None)
                        img = av.send_now("✅ <b>Avisos activos</b>\nAsí verás los avisos de tus bots. Escribe /ayuda "
                                          "para ver los comandos." + ("\n\nGráfico de ejemplo: {}.".format(
                                              A.esc(bots[0].name)) if chart else ""), chart)
                        msg = "Mensaje de prueba enviado" + (" con gráfico" if img else
                                                             " (sin gráfico: instala Pillow)" if chart else "")
                        return self._json({"ok": True, "msg": msg, **av.public()})
                    return self._json({"ok": False, "errors": ["Operación desconocida"]}, 400)
                if u.path == "/api/planes":
                    return self._planes_post(body)
                if u.path == "/api/fiscal":
                    return self._fiscal_post(body)
                if u.path == "/api/temas":
                    op = body.get("op")
                    if op == "subir":
                        t, errs = hub.temas.guardar(body.get("tema"))
                        if errs:
                            return self._json({"ok": False, "errors": errs})
                        hub.log("Panel: tema «{}» cargado".format(t["nombre"]))
                    elif op == "borrar":
                        hub.temas.borrar(str(body.get("id", "")))
                    else:
                        return self._json({"ok": False, "errors": ["Operación desconocida"]}, 400)
                    return self._json({"ok": True, "temas": hub.temas.lista(), "id": t["id"] if op == "subir" else None})
                if u.path == "/api/externos":
                    return self._externos_post(body)
                if u.path == "/api/reiniciar":
                    hub.log("Reinicio pedido desde el panel.", "warn")
                    self._json({"ok": True})
                    threading.Timer(1.0, lambda: os._exit(3)).start()
                    return
                if len(parts) >= 4 and parts[:2] == ["api", "b"]:
                    b = self._bot(parts)
                    action = parts[3]
                    if action == "config":
                        P, errs = K.validate(body.get("params"))
                        if not errs and not hub.markets.get(P["symbol"]):
                            errs = ["{} no está en tu lista de instrumentos".format(P["symbol"])]
                        errs = errs or hub.exec_conflicts(P, b.id)
                        if errs:
                            return self._json({"ok": False, "errors": errs}, 400)
                        if b.meta.get("archived"):
                            return self._json({"ok": False, "errors": ["El bot está archivado: reactívalo primero"]}, 400)
                        c = b.apply_config(P, str(body.get("note", "")))
                        hub.feed_changed()
                        return self._json({"ok": True, "id": c["id"]})
                    if action == "puntuar":
                        if not b.P.get("combinar"):
                            return self._json({"ok": False, "errors": ["Este bot tiene una sola estrategia"]}, 400)
                        if hub.puntuando:
                            return self._json({"ok": False, "errors": ["Ya se está calculando una puntuación; espera un momento"]}, 409)
                        threading.Thread(target=hub._puntuar_seguro, args=(b,), daemon=True, name="puntuacion").start()
                        return self._json({"ok": True, "msg": "Calculando la puntuación de cada estrategia ({} días de backtest)…".format(PUNT_DIAS)})
                    if action == "gestion":
                        ok, msg = b.gestion_manual(str(body.get("op", "")), body.get("valor"))
                        return self._json({"ok": ok, "msg": msg}, 200 if ok else 400)
                    ok, msg = b.control(action)
                    return self._json({"ok": ok, "msg": msg}, 200 if ok else 400)
                return self._json({"ok": False, "errors": ["No existe"]}, 404)
            except KeyError as e:
                return self._json({"ok": False, "errors": [str(e).strip("'")]}, 404)
            except (ValueError, A.TelegramError, K.StrategyError) as e:
                return self._json({"ok": False, "errors": [str(e)]}, 400)
            except Exception as e:
                hub.log("Error en el panel ({}): {}".format(u.path, e), "error")
                return self._json({"ok": False, "errors": [str(e)]}, 500)

    return Handler


# ============================ arranque ============================
class PanelServer(ThreadingHTTPServer):
    """Servidor del panel. Con HTTPS, el cifrado se negocia en el hilo de cada conexión (un cliente lento no
    frena a los demás) y quien entra por http:// recibe una redirección a https://."""
    daemon_threads = True
    allow_reuse_address = True
    tls = None

    def finish_request(self, request, client_address):
        if self.tls is None:
            return super().finish_request(request, client_address)
        request.settimeout(20)
        try:
            first = request.recv(1, socket.MSG_PEEK)
        except OSError:
            return
        if not first:
            return
        if first[0] != 0x16:                  # no empieza por un saludo TLS: es http:// en claro
            return self._a_https(request)
        try:
            tls_sock = self.tls.wrap_socket(request, server_side=True)
        except (ssl.SSLError, OSError):
            return
        try:
            super().finish_request(tls_sock, client_address)
        finally:
            try:
                tls_sock.close()
            except OSError:
                pass

    def _a_https(self, request):
        try:
            data = request.recv(4096).decode("latin-1", "replace")
            lines = data.split("\r\n")
            host = next((ln.split(":", 1)[1].strip() for ln in lines[1:] if ln.lower().startswith("host:")), "")
            first = lines[0].split(" ")
            path = first[1] if len(first) >= 3 and first[1].startswith("/") else "/"
            if re.match(r"^[A-Za-z0-9.\-]+(:\d+)?$|^\[[0-9a-fA-F:]+\](:\d+)?$", host):
                body = "Este panel usa HTTPS: https://{}{}".format(host, path).encode("utf-8", "replace")
                head = "HTTP/1.1 301 Moved Permanently\r\nLocation: https://{}{}\r\n".format(
                    host, re.sub(r"[^\x21-\x7e]", "", path))
            else:
                body, head = b"Este panel usa HTTPS.", "HTTP/1.1 400 Bad Request\r\n"
            request.sendall((head + "Content-Type: text/plain; charset=utf-8\r\nContent-Length: {}\r\n"
                             "Connection: close\r\n\r\n".format(len(body))).encode("latin-1") + body)
        except OSError:
            pass


def tls_context(cfg, log):
    if not (os.path.exists(cfg.cert) and os.path.exists(cfg.cert_key)):
        names = AC.crear_certificado(cfg.cert, cfg.cert_key)
        log("Certificado HTTPS autofirmado creado para: {}".format(", ".join(names)))
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.load_cert_chain(cfg.cert, cfg.cert_key)
    return ctx


def _silencio(fn, *a):
    try:
        fn(*a)
    except Exception:
        pass


def run(cfg, log):
    hub = Hub(cfg, log)
    n = len([m for m in hub.meta if not m.get("archived")]) or 1
    g = hub.cuentas
    if g.ordenes and g.lista():
        log("{} v{} | {} bots | ÓRDENES ACTIVADAS para los bots en modo sandbox · cuentas: {}".format(
            APP, VERSION, n, ", ".join(c.nombre for c in g.lista())), "warn")
    else:
        log("{} v{} | {} bots | solo papel ({})".format(APP, VERSION, n, g.problema() or "sin cuentas"))
    acc = AC.Acceso(cfg.data_dir, cfg.sesion_horas, cfg.sesion_dias)

    def aviso_plan(texto):
        hub.log("Planes: " + re.sub(r"<[^>]+>", "", texto).replace("\n", " · "))
        if hub.avisos:
            threading.Thread(target=lambda: _silencio(hub.avisos.send_now, texto), daemon=True).start()
    PL.STORE = PL.Planes(os.path.join(cfg.data_dir, "planes.json"), cfg.plan_espera, aviso_plan)
    PL.DIBUJOS = PL.Dibujos(os.path.join(cfg.data_dir, "dibujos.json"))
    PL.EQUIVALENTES = {n: set(g) for g in ALIASES.values() for n in g}
    if cfg.mcp_token:
        log("Conector MCP para Claude activo en /mcp/<token> (solo crea planes pendientes)")
    if not cfg.login:
        log("Panel SIN inicio de sesión (FP_WEB_LOGIN=0): cualquiera en tu red puede usarlo", "warn")
    elif not acc.hay_usuarios():
        log("Aún no hay usuario del panel: créalo con ./bot.sh usuario (no hace falta reiniciar)", "warn")
    try:
        srv = PanelServer((cfg.bind, cfg.port), make_handler(hub, cfg, acc))
    except OSError as e:
        log("No se puede abrir el panel en el puerto {}: {}. ¿Otro programa usa ese puerto?".format(cfg.port, e),
            "error")
        sys.exit(2)
    if cfg.https:
        try:
            srv.tls = tls_context(cfg, log)
        except (RuntimeError, OSError, ssl.SSLError) as e:
            log("No se puede activar HTTPS: {}. Revisa el certificado o pon FP_WEB_HTTPS=0.".format(e), "error")
            sys.exit(2)
    hub.start()
    threading.Thread(target=srv.serve_forever, daemon=True, name="web").start()
    log("Panel en {}://<ip>:{}".format("https" if cfg.https else "http", cfg.port))
    threading.Thread(target=hub.cuentas.bucle, daemon=True, name="cuentas").start()
    threading.Thread(target=hub.noticias.bucle, daemon=True, name="noticias").start()
    while True:
        time.sleep(3600)


def check(cfg, log):
    print("Comprobación (no envía órdenes)\n")
    for p in K.REGISTRY.list():
        if p.error:
            print("[!!] Estrategia {}: {}".format(p.file, p.error))
        else:
            ok_s, msg = K.smoke_test(p)
            print("[{}] Estrategia «{}» v{} ({}): {}".format("ok" if ok_s else "!!", p.nombre, p.version, p.file, msg))
    if not K.REGISTRY.list():
        print("[!!] No hay ninguna estrategia en {}".format(STRAT_DIR))
    print("[{}] Gráficos para Telegram: {}".format("ok" if A.HAVE_PIL else "--", "Pillow instalado" if A.HAVE_PIL else
                                                  "falta Pillow (los avisos irán sin imagen): "
                                                  "./venv/bin/pip install pillow"))
    acc = AC.Acceso(cfg.data_dir, cfg.sesion_horas, cfg.sesion_dias)
    if not cfg.login:
        print("[!!] Panel sin inicio de sesión (FP_WEB_LOGIN=0): cualquiera en tu red puede usarlo.")
    elif acc.hay_usuarios():
        print("[ok] Inicio de sesión del panel: {}".format(", ".join(acc.lista())))
    else:
        print("[!!] Aún no hay usuario del panel: créalo con ./bot.sh usuario")
    if cfg.https:
        if os.path.exists(cfg.cert) and os.path.exists(cfg.cert_key):
            try:
                tls_context(cfg, lambda *a: None)
                print("[ok] HTTPS activado con el certificado de {}".format(cfg.cert))
            except (ssl.SSLError, OSError, RuntimeError) as e:
                print("[!!] El certificado HTTPS no sirve: {}. Renuévalo con ./bot.sh certificado".format(e))
        else:
            print("[ok] HTTPS activado: el certificado se creará al arrancar (o ya con ./bot.sh certificado)")
    elif cfg.proxy_https:
        print("[ok] HTTPS a cargo del proxy (FP_WEB_PROXY_HTTPS=1)")
    else:
        print("[--] Panel sin HTTPS: actívalo con ./bot.sh configurar (o FP_WEB_HTTPS=1)")
    try:
        hub_min = type("HubMin", (), {"tz": get_tz(cfg.local_tz), "log": staticmethod(lambda *a: None)})()
        nt = NT.Noticias(hub_min)
        if nt.actualizado and now_ms() - nt.actualizado < NT.REFRESCO_S * 1000:
            ok_n, info = True, len(nt.eventos)          # descargado hace poco: no se vuelve a pedir
        else:
            ok_n, info = nt.descargar()
        if ok_n:
            print("[ok] Calendario de noticias: {} eventos esta semana; filtro {}".format(
                info, "activo" if nt.cfg["activo"] else "desactivado (actívalo en el panel → Noticias)"))
        else:
            print("[!!] Calendario de noticias: {}".format(info))
    except Exception as e:
        print("[!!] Calendario de noticias: {}".format(e))
    rest = MFPRest(cfg.host, cfg.key)
    ok = True
    sandbox_bots = []
    try:
        with open("bots.json", encoding="utf-8") as f:
            for m in json.load(f):
                c = ConfigStore(os.path.join("bots", m["id"])).current() if not m.get("archived") else None
                if c and c["params"].get("ex_modo") == "sandbox":
                    sandbox_bots.append((m.get("name"), c["params"]["symbol"], c["params"].get("ex_cuentas") or []))
    except (OSError, ValueError):
        pass
    try:
        with open(CU.FILE, encoding="utf-8") as f:
            cdata = json.load(f)
    except (OSError, ValueError):
        cdata = None
    if cdata is not None:
        ok = check_cuentas(cfg, cdata, sandbox_bots) and ok
    elif not cfg.exec:
        print("[--] Órdenes en sandbox desactivadas (FP_EXEC=0): todos los bots funcionan en papel{}.".format(
            " aunque {} bot{} tenga{} el modo sandbox".format(len(sandbox_bots), "" if len(sandbox_bots) == 1 else "s",
                                                             "" if len(sandbox_bots) == 1 else "n")
            if sandbox_bots else ""))
    elif not cfg.key:
        ok = False
        print("[!!] FP_EXEC=1 pero falta la clave de sandbox")
    else:
        try:
            tr = E.MFPTrading(cfg.host, cfg.key, cfg.http_timeout)
            accs = tr.accounts()
            act = [a for a in accs if a.get("status") == "active"]
            acc = cfg.account or (act[0]["id"] if act else None)
            if not acc:
                raise RuntimeError("no hay ninguna cuenta sandbox activa")
            a = tr.account(acc)
            pol = tr.policy(acc)
            blocked = [k for k in ("manual_trading_blocked", "opening_exposure_restricted") if pol.get(k)]
            print("[{}] Órdenes en sandbox ACTIVADAS. Cuenta {} ({}), saldo {} $, trading policy: {}".format(
                "!!" if blocked else "ok", a.get("name") or acc, a.get("status"), a.get("balance"),
                ", ".join(blocked) if blocked else "sin bloqueos"))
            print("     Bots que operan en ella: {}".format(", ".join("{} ({})".format(*x[:2]) for x in sandbox_bots)
                                                         if sandbox_bots else "ninguno (actívalo en la configuración del bot)"))
        except Exception as e:
            ok = False
            print("[!!] Órdenes en sandbox: no se puede usar la cuenta: {}".format(e))
    markets = Markets(rest)
    syms = []
    try:
        with open("bots.json", encoding="utf-8") as f:
            for m in json.load(f):
                if not m.get("archived"):
                    c = ConfigStore(os.path.join("bots", m["id"])).current()
                    if c:
                        syms.append((c["params"]["symbol"], c["params"]["timeframe"]))
                        sid = c["params"].get("estrategia")
                        if not K.REGISTRY.get(sid):
                            ok = False
                            print("[!!] El bot «{}» usa la estrategia «{}», que no está disponible".format(
                                m.get("name"), sid))
    except (OSError, ValueError):
        pass
    if not syms and os.path.exists("config.json"):
        with open("config.json", encoding="utf-8") as f:
            P = json.load(f)["params"]
        syms.append((P["symbol"], P["timeframe"]))
    syms = sorted(set(syms)) or [("GOLD", "5m")]
    st = MFPStream(log)
    for sym, tf in syms:
        try:
            mk = markets.resolve(sym)
            cs = st.candles(mk, tf, 400)
            print("[{}] {} {}: {} (feed {} / {}), {} velas".format("ok" if len(cs) >= 200 else "!!", sym, tf,
                                                                     mk["market_id"], mk["provider"], mk["coin"], len(cs)))
            ok = ok and len(cs) >= 200
        except Exception as e:
            ok = False
            print("[!!] {} {}: {}".format(sym, tf, e))
    print("[{}] Diferencia de reloj con el servidor: {:.1f} s".format("ok" if abs(st.offset_ms) < 5000 else "!!",
                                                                     st.offset_ms / 1000))
    try:
        ws = _wsmod().create_connection(STREAM_URL, timeout=20)
        mk = markets.resolve(syms[0][0])
        ws.send(json.dumps({"op": "sub", "id": 1, "channel": "candles", "payload": {
            "symbols": [mk["coin"]], "providers": [mk["provider"]], "intervals": ["1m"], "historyLimit": 1}}))
        got, t0 = None, time.time()
        while time.time() - t0 < 20 and got is None:
            fr = json.loads(ws.recv())
            if fr.get("op") in ("sub_ok", "events", "sub_err"):
                got = fr.get("op")
        ws.close()
        print("[{}] Suscripción en directo: {}".format("ok" if got in ("sub_ok", "events") else "!!",
                                                      {"sub_ok": "aceptada", "events": "recibiendo datos",
                                                       "sub_err": "rechazada", None: "sin respuesta"}[got]))
    except Exception as e:
        print("[!!] Suscripción en directo: {} (los bots funcionarán consultando el historial)".format(e))
    print("\nComprobación superada." if ok else "\nHay problemas: revisa los mensajes [!!].")
    return ok


def check_cuentas(cfg, data, sandbox_bots):
    """./bot.sh comprobar con las cuentas del panel (6.3)."""
    ok = True
    cs = data.get("cuentas") or []
    if not cs:
        print("[--] No hay cuentas: todos los bots funcionan en papel. Dalas de alta en el panel → Cuentas.")
        return True
    print("[{}] Envío de órdenes: {}".format("ok" if data.get("ordenes") else "--", "ACTIVADO" if data.get("ordenes")
                                             else "desactivado (todos los bots en papel; se activa en el panel)"))
    first = cs[0]["id"]
    for c in cs:
        usan = [x for x in sandbox_bots if c["id"] in x[2] or (not x[2] and c["id"] == first)]
        try:
            tr = E.MFPTrading(cfg.host, c.get("clave", ""), cfg.http_timeout)
            if c.get("cuenta_mfp"):
                acc = c["cuenta_mfp"]
            else:
                act = [a for a in tr.accounts() if a.get("status") == "active"]
                if not act:
                    raise RuntimeError("la clave no ve ninguna cuenta sandbox activa")
                acc = act[0]["id"]
            a, pol = tr.account(acc), tr.policy(acc)
            blocked = [k for k in ("manual_trading_blocked", "opening_exposure_restricted") if pol.get(k)]
            est = (data.get("estado") or {}).get(c["id"]) or {}
            b = est.get("bloqueo")
            blq = " · BLOQUEADA en el programa" if b and (not b.get("hasta") or b["hasta"] > now_ms()) else ""
            print("[{}] Cuenta «{}»{}: {} ({}), saldo {} $, trading policy: {}{}".format(
                "!!" if blocked else "ok", c.get("nombre"), "" if c.get("activa", True) else " (desactivada)",
                a.get("name") or acc, a.get("status"), a.get("balance"), ", ".join(blocked) if blocked else
                "sin bloqueos", blq))
        except Exception as e:
            ok = False
            print("[!!] Cuenta «{}»: no se puede usar: {}".format(c.get("nombre"), e))
        print("     Bots que operan en ella: {}".format(", ".join("{} ({})".format(*x[:2]) for x in usan)
                                                     if usan else "ninguno"))
    return ok


def probar(cfg, log, path, symbol, tf, days, extra, exportar=False, bot=None):
    """Prueba un archivo de estrategia con velas reales de MyFundedPerps sin instalarlo ni crear un bot.
    Con bot=N usa la estrategia y los ajustes de ese bot. Con exportar=True guarda el contexto de cada operación
    del backtest en un CSV para analizarlo (./bot.sh analizar)."""
    if bot:
        try:
            b = CX.resolver_bot(".", bot)
        except ValueError as err:
            print("[!!] {}".format(err))
            return False
        print("[ok] Usando el " + CX.describir(b))
        bot = b["num"] or b["nombre"]
        c = ConfigStore(os.path.join("bots", b["id"])).current()
        if not c:
            print("[!!] No encuentro la configuración de «{}» (data/bots/{}/config.json)".format(b["nombre"], b["id"]))
            return False
        raw = dict(c["params"])
        for kv in extra or []:
            k, _, v = kv.partition("=")
            raw[k.strip()] = v.strip()
        P, errs = K.validate(raw)
        p = K.REGISTRY.get(P.get("estrategia"))
        if not p:
            print("[!!] La estrategia «{}» del bot {} no está instalada o tiene un error".format(P.get("estrategia"), bot))
            return False
        symbol, tf = P["symbol"], P["timeframe"]
        print("[ok] Estrategia «{}» v{} · {} {}".format(p.nombre, p.version, symbol, tf))
    else:
        p = K.load_plugin(os.path.abspath(path))
        if p.error:
            print("[!!] {}: {}".format(path, p.error))
            return False
        ok, msg = K.smoke_test(p)
        print("[{}] «{}» v{}: {}".format("ok" if ok else "!!", p.nombre, p.version, msg))
        if not ok:
            return False
        K.REGISTRY.items.setdefault(p.id, p)
        raw = dict(p.defaults, symbol=symbol, timeframe=tf)
        for kv in extra or []:
            k, _, v = kv.partition("=")
            raw[k.strip()] = v.strip()
        P, errs = K.validate(raw)
    if errs:
        print("[!!] Ajustes: " + "; ".join(errs))
        return False
    rest = MFPRest(cfg.host, cfg.key)
    markets = Markets(rest)
    if markets.get(symbol):
        mk = markets.resolve(symbol)
    else:                                   # no está en tu lista: se busca directamente en el catálogo de MFP
        found = [m for m in markets.catalog() if symbol in (str(m.get("symbol", "")).upper(),
                                                            str(m.get("coin", "")).upper().split(":")[-1])]
        if not found:
            print("[!!] {} no está en tu lista de instrumentos ni en el catálogo de MyFundedPerps".format(symbol))
            return False
        mk = {k: found[0].get(k) for k in MKT_KEYS}
    st = MFPStream(log)
    tf_ms = K.TF_MS[tf]
    warm = max(400, K.warmup_bars(P))
    need = min(cfg.bt_max, warm + int(days * 86_400_000 / tf_ms))
    cs = st.candles(mk, tf, need)
    htf = fetch_trend(st, mk, P, len(cs) * tf_ms)
    if len(cs) < warm + 50:
        print("[!!] Solo hay {} velas de {} {}".format(len(cs), symbol, tf))
        return False
    trades, info = K.backtest(cs, P, make_hour_fn(get_tz(cfg.local_tz)), htf, warm=warm,
                              instrument_cost=markets.cost(symbol), contexto=exportar)
    if len(cs) >= cfg.bt_max and days * 86_400_000 / tf_ms > cfg.bt_max - warm:
        print("Aviso: el backtest se ha recortado a {} velas (FP_BT_MAX). Súbelo en config.env para más días.".format(
            cfg.bt_max))
    m = K.metrics(trades)
    print("\n{} {} ({} / {}) · {} → {} UTC · {} velas tras {} de calentamiento".format(
        symbol, tf, mk["market_id"], mk["coin"], fmt_utc(cs[warm][0]), fmt_utc(cs[-1][0]), len(cs) - warm, warm))
    print("Señales {} · filtradas {} · descartadas por coste {}".format(
        info["signals"], sum(info["filtered"].values()), info["skipped_cost"]))
    print("Operaciones {} · % acierto {:.1f} · R total {:+.2f} · R medio {:+.3f} · PF {} · DD máx {:.2f}R · SQN {}".format(
        m["n"], m["winrate"], m["total_r"], m["avg_r"], "{:.2f}".format(m["pf"]) if m["pf"] else "n/d", m["max_dd"],
        "{:.2f}".format(m["sqn"]) if m["sqn"] is not None else "n/d"))
    if m["why"]:
        print("Salidas: " + ", ".join("{} {}".format(k, v) for k, v in sorted(m["why"].items())))
    if m["n"] < 30:
        print("Aviso: con menos de 30 operaciones el resultado dice poco.")
    if exportar:
        filas = []
        for i, tr in enumerate(trades, 1):
            tr.update({"n": i, "cfg": "backtest", "symbol": symbol, "tf": tf})
            filas.append(CX.fila(tr, b["nombre"] if bot else "probar", P.get("estrategia", "")))
        etiqueta = re.sub(r"[^A-Za-z0-9]+", "_", b["nombre"]).strip("_")[:30] + "_" if bot else ""
        nombre = "contexto_backtest_{}{}_{}_{}.csv".format(etiqueta, symbol, tf, datetime.now().strftime("%Y%m%d_%H%M"))
        CX.escribir_todas(nombre, filas)
        print("\nContexto de {} operaciones guardado en {}".format(len(filas), os.path.abspath(nombre)))
        print("Analízalo con: ./bot.sh analizar {}".format(nombre))
    return True


def wizard(path):
    old = load_env_file(path)
    print("\n=== {} v{}: ajustes del programa ===".format(APP, VERSION))
    print("Los bots, sus mercados y sus estrategias se crean y configuran luego desde el panel.\n")

    def ask(label, key, default):
        cur = old.get(key, str(default))
        shown = cur[:8] + "…" + cur[-4:] if key == "FP_MFP_KEY" and len(cur) > 12 else cur
        v = input("{} [{}]: ".format(label, shown)).strip()
        return v or cur

    port = ask("Puerto del panel", "FP_WEB_PORT", 8083)
    while not port.isdigit():
        port = ask("Puerto (número)", "FP_WEB_PORT", 8083)
    exe, acc, key = old.get("FP_EXEC", "0"), old.get("FP_MFP_ACCOUNT", ""), old.get("FP_MFP_KEY", "")
    d = old.get("FP_DATA_DIR", "data")
    if os.path.exists(os.path.join(d if os.path.isabs(d) else os.path.join(HERE, d), CU.FILE)):
        print("Las cuentas de MyFundedPerps, sus claves y el envío de órdenes se gestionan en el panel → Cuentas.\n")
    else:
        print("Clave de sandbox fp_test_ (opcional). Al arrancar se convierte en la cuenta «Principal»; después las")
        print("cuentas y sus claves se gestionan en el panel → Cuentas.")
        while True:
            key = ask("Clave", "FP_MFP_KEY", "")
            if not key or key.startswith("fp_test_"):
                break
            print("Tiene que empezar por fp_test_.")
    if key and not os.path.exists(os.path.join(d if os.path.isabs(d) else os.path.join(HERE, d), CU.FILE)):
        print("¿Permitir que los bots en modo «sandbox» envíen órdenes a la cuenta sandbox? Cada bot sigue en papel")
        print("hasta que lo actives en su configuración (Ejecución en sandbox → Modo).")
        while True:
            exe = ask("Órdenes en sandbox (1 = sí, 0 = no)", "FP_EXEC", 0)
            if exe in ("0", "1"):
                break
        acc = ask("ID de la cuenta sandbox (Intro = la primera activa)", "FP_MFP_ACCOUNT", "")
    days = ask("Duración prevista de las pruebas en días", "FP_TEST_DAYS", 14)
    print("Conexión segura: con HTTPS el panel va cifrado (certificado autofirmado: el navegador avisará la primera")
    print("vez). Si entras con Tailscale o un proxy con HTTPS propio, déjalo en 0 y pon FP_WEB_PROXY_HTTPS=1.")
    while True:
        https = ask("Panel por HTTPS (1 = sí, 0 = no)", "FP_WEB_HTTPS", 1)
        if https in ("0", "1"):
            break
    print("El panel se abre con usuario y contraseña (./bot.sh usuario). El token de API es opcional y solo sirve")
    print("para scripts que llamen al panel con la cabecera X-Token.")
    token = old.get("FP_WEB_TOKEN", "")
    v = input("Token de API (Intro = {}; 'nuevo' = generar uno; 'no' = quitarlo): ".format(
        "mantener" if token else "sin token")).strip().lower()
    if v == "nuevo":
        token = secrets_token()
    elif v == "no":
        token = ""
    keep = lambda k, d: old.get(k, str(d))
    L = ["# {} v{}. Generado el {}.".format(APP, VERSION, datetime.now().strftime("%Y-%m-%d %H:%M")),
         "# Los bots, sus estrategias y su ejecución se configuran desde el panel. Aquí, la infraestructura.", "",
         "# --- Panel ---", "FP_WEB_PORT={}".format(port), "FP_WEB_BIND={}".format(keep("FP_WEB_BIND", "0.0.0.0")),
         "# 1 = panel por HTTPS (certificado en data/tls; ./bot.sh certificado lo renueva)",
         "FP_WEB_HTTPS={}".format(https),
         "# 1 = el panel está detrás de un proxy que ya pone HTTPS (Tailscale serve, Caddy…)",
         "FP_WEB_PROXY_HTTPS={}".format(keep("FP_WEB_PROXY_HTTPS", 0)),
         "# 1 = pedir usuario y contraseña (recomendado). Usuarios: ./bot.sh usuario",
         "FP_WEB_LOGIN={}".format(keep("FP_WEB_LOGIN", 1)),
         "# horas sin actividad hasta cerrar la sesión / días que dura «Mantener la sesión iniciada»",
         "FP_SESION_HORAS={}".format(keep("FP_SESION_HORAS", 12)), "FP_SESION_DIAS={}".format(keep("FP_SESION_DIAS", 30)),
         "# token de API opcional para scripts (cabecera X-Token); el panel no lo usa",
         "FP_WEB_TOKEN={}".format(token)] + (["FP_WEB_CERT={}".format(old["FP_WEB_CERT"])] if old.get("FP_WEB_CERT") else []) + (
        ["FP_WEB_KEY={}".format(old["FP_WEB_KEY"])] if old.get("FP_WEB_KEY") else []) + ["",
         "# --- MyFundedPerps (solo sandbox) ---",
         "# Desde la 6.3 las cuentas, sus claves y el envío de órdenes están en el panel → Cuentas (data/cuentas.json).",
         "# Estas tres solo se leen la primera vez, para crear la cuenta «Principal».",
         "FP_MFP_KEY={}".format(key), "FP_EXEC={}".format(exe), "FP_MFP_ACCOUNT={}".format(acc),
         "FP_MFP_HOST={}".format(keep("FP_MFP_HOST", SANDBOX_HOST)), "",
         "# --- Programa ---", "FP_TEST_DAYS={}".format(days), "FP_LOCAL_TZ={}".format(keep("FP_LOCAL_TZ", "Europe/Madrid")),
         "FP_DATA_DIR={}".format(keep("FP_DATA_DIR", "data")),
         "# velas en memoria por bot / velas máximas de un backtest / bots activos a la vez",
         "FP_HIST_N={}".format(keep("FP_HIST_N", HIST_N)), "FP_BT_MAX={}".format(keep("FP_BT_MAX", BT_MAX)),
         "FP_MAX_BOTS={}".format(keep("FP_MAX_BOTS", MAX_BOTS)),
         "# segundos entre lecturas de la cuenta para el panel / espera máxima de cada petición a la API",
         "FP_ACCOUNT_POLL_S={}".format(keep("FP_ACCOUNT_POLL_S", 60)),
         "FP_HTTP_TIMEOUT_S={}".format(keep("FP_HTTP_TIMEOUT_S", 20))]
    atomic_write(path, "\n".join(L) + "\n")
    os.chmod(path, 0o600)
    print("\nGuardado en {}. Panel: {}://IP-DE-LA-RASPBERRY:{}/".format(path, "https" if https == "1" else "http", port))
    print("Si aún no tienes usuario del panel, créalo con: ./bot.sh usuario\n")


def secrets_token():
    return "".join(random.SystemRandom().choice("abcdefghjkmnpqrstuvwxyz23456789") for _ in range(24))


def gestionar_usuario(cfg):
    import getpass
    acc = AC.Acceso(cfg.data_dir, cfg.sesion_horas, cfg.sesion_dias)
    hay = acc.lista()
    print("\n=== {}: usuario del panel ===".format(APP))
    print("Usuarios actuales: {}".format(", ".join(hay) if hay else "ninguno"))
    print("Escribe uno existente para cambiarle la contraseña o uno nuevo para crearlo.\n")
    defecto = hay[0] if len(hay) == 1 else ""
    while True:
        nombre = input("Usuario{}: ".format(" [{}]".format(defecto) if defecto else "")).strip() or defecto
        if AC.NOMBRE_RE.match(nombre):
            break
        print("De 3 a 32 caracteres: letras, números, punto, guion o guion bajo.")
    for _ in range(3):
        c1 = getpass.getpass("Contraseña (mínimo {} caracteres, no se ve al escribir): ".format(AC.MIN_CLAVE))
        c2 = getpass.getpass("Repítela: ")
        if c1 != c2:
            print("No coinciden. Otra vez.")
            continue
        try:
            nuevo = acc.poner_usuario(nombre, c1)
        except ValueError as e:
            print(e)
            continue
        print("\n[ok] Usuario «{}» {}.".format(nombre, "creado" if nuevo else
                                               "actualizado; sus sesiones abiertas se han cerrado"))
        print("Ya puedes entrar al panel; no hace falta reiniciar.\n")
        return True
    print("No se ha cambiado nada.")
    return False


def main():
    ap = argparse.ArgumentParser(description="{} (papel y cuenta sandbox de MyFundedPerps)".format(APP))
    ap.add_argument("--config", default=os.path.join(HERE, "config.env"))
    ap.add_argument("--configurar", action="store_true")
    ap.add_argument("--importar", metavar="RUTA")
    ap.add_argument("--importar-bot", metavar="CARPETA")
    ap.add_argument("--comprobar", action="store_true")
    ap.add_argument("--informe", action="store_true")
    ap.add_argument("--usuario", action="store_true", help="crea el usuario del panel o le cambia la contraseña")
    ap.add_argument("--certificado", action="store_true", help="crea o renueva el certificado HTTPS")
    ap.add_argument("--cerrar-sesiones", action="store_true", help="cierra todas las sesiones del panel")
    ap.add_argument("--quitar-2fa", metavar="USUARIO", help="quita la verificación en dos pasos de ese usuario")
    ap.add_argument("--probar", metavar="ARCHIVO", help="prueba un archivo de estrategia con velas reales de MFP")
    ap.add_argument("--simbolo", default="BTC")
    ap.add_argument("--tf", default="5m", choices=sorted(K.TF_MS))
    ap.add_argument("--dias", type=float, default=30)
    ap.add_argument("--ajuste", action="append", metavar="CLAVE=VALOR", help="cambia un ajuste en --probar")
    ap.add_argument("--exportar", action="store_true", help="en --probar: guarda el contexto de cada operación en CSV")
    ap.add_argument("--bot", metavar="N|NOMBRE", help="usa la estrategia y los ajustes de ese bot (posición en el menú "
                                                       "del panel o nombre)")
    args = ap.parse_args()
    cfg_path = os.path.abspath(args.config)
    K.REGISTRY = K.Registry(STRAT_DIR)
    K.REGISTRY.scan()
    if args.configurar:
        wizard(cfg_path)
        return
    for k, v in load_env_file(cfg_path).items():
        os.environ.setdefault(k, v)
    cfg = Cfg()
    probs = cfg.problems()
    if probs:
        sys.exit("Configuración incorrecta:\n  - " + "\n  - ".join(probs))
    for sub in ("", "logs", "cache", "bots"):
        os.makedirs(os.path.join(cfg.data_dir, sub), exist_ok=True)
    os.chdir(cfg.data_dir)
    if args.usuario:
        sys.exit(0 if gestionar_usuario(cfg) else 1)
    if args.quitar_2fa:
        try:
            AC.Acceso(cfg.data_dir, cfg.sesion_horas, cfg.sesion_dias).totp_quitar_forzado(args.quitar_2fa)
        except KeyError as e:
            sys.exit(str(e).strip("'\""))
        print("[ok] Verificación en dos pasos quitada a {}. Ya entra solo con la contraseña; vuelve a activarla desde "
              "Ajustes del panel.".format(args.quitar_2fa))
        return
    if args.cerrar_sesiones:
        n = AC.Acceso(cfg.data_dir, cfg.sesion_horas, cfg.sesion_dias).cerrar_todas()
        print("[ok] {} sesión{} cerrada{}. Habrá que volver a entrar en el panel.".format(
            n, "" if n == 1 else "es", "" if n == 1 else "s"))
        return
    if args.certificado:
        try:
            names = AC.crear_certificado(cfg.cert, cfg.cert_key)
        except RuntimeError as e:
            sys.exit(str(e))
        print("[ok] Certificado creado en {}\n     Válido para: {}".format(cfg.cert, ", ".join(names)))
        if not cfg.https:
            print("     HTTPS está desactivado: pon FP_WEB_HTTPS=1 en config.env (o ./bot.sh configurar).")
        print("     Aplícalo con: ./bot.sh reiniciar")
        return
    log = Log(os.path.join(cfg.data_dir, "logs"))
    for p in K.REGISTRY.list():
        if p.error:
            log("Estrategia {} con error, no se puede usar: {}".format(p.file, p.error), "error")
    if args.importar or args.importar_bot:
        hub = Hub(cfg, log)
        if args.importar:
            vals = load_env_file(os.path.abspath(os.path.expanduser(args.importar)))
            if not vals:
                sys.exit("No encuentro ajustes en {}".format(args.importar))
            P, errs, keys = K.from_env(vals)
            if not hub.markets.get(P["symbol"]):
                sys.exit("{} no está en la lista de instrumentos; añádelo desde el panel primero".format(P["symbol"]))
            meta = hub.create("", P, "importado de " + args.importar, start=False)
            print("Bot «{}» creado con {} ajustes de {}.".format(meta["name"], len(keys), args.importar))
            for e in errs:
                print("  aviso: " + e)
        else:
            meta = hub.import_v2(args.importar_bot)
            print("Bot «{}» importado de {} con su historial.".format(meta["name"], args.importar_bot))
        return
    if args.informe:
        hub = Hub(cfg, log)
        for m in hub.meta:
            hub._spawn(m, run=False)
        name = "informe_bots_{}.zip".format(datetime.now().strftime("%Y%m%d_%H%M"))
        with open(name, "wb") as f:
            f.write(hub.zip_all())
        print(os.path.join(cfg.data_dir, name))
        return
    if args.comprobar:
        sys.exit(0 if check(cfg, log) else 1)
    if args.probar or args.bot:
        path = args.probar or ""
        sys.exit(0 if probar(cfg, log, os.path.join(HERE, path) if path and not os.path.isabs(path) else path,
                             args.simbolo.upper(), args.tf, args.dias, args.ajuste, args.exportar, args.bot) else 1)
    try:
        run(cfg, log)
    except KeyboardInterrupt:
        log("Parado por el usuario.")


if __name__ == "__main__":
    main()
