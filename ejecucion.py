# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Borislav Vladimirov Petrakiev
"""
ejecucion.py -- Ejecución real en la cuenta SANDBOX de MyFundedPerps (opcional, desde la v5).

El motor (nucleo.Motor) sigue siendo el modelo: decide las señales, el stop, el objetivo y la gestión, exactamente
igual que en papel. Este módulo replica sus decisiones en la cuenta sandbox:

  * señal del modelo        -> orden a mercado con el SL (y el TP, si lo hay) adjuntos. Tamaño por riesgo.
                               Antes, consulta la trading policy y el daily loss que queda.
  * stop u objetivo nuevos  -> PUT /v1/positions/{id}/exit-orders (break-even, trailing, ampliación,
                               objetivo dinámico).
  * venta parcial           -> cierre parcial de la posición.
  * el modelo cierra        -> cierre a mercado si la posición real sigue abierta.
  * la cuenta cierra sola   -> (SL o TP en el exchange) se registra el resultado real.

Todo se anota en ejecuciones.csv (una fila por operación real, con la cuenta) y en eventos.csv.

Desde la 6.3 un bot puede operar en varias cuentas a la vez (cuentas.py): hay un Ejecutor por cuenta y
Ejecuciones los agrupa. Cada cuenta lleva su clave, su tamaño (con su multiplicador de riesgo) y su bloqueo.

Solo sandbox: el cliente rechaza cualquier host que no sea el de sandbox (o localhost, para pruebas) y cualquier
clave que no empiece por fp_test_. Referencia de la API: https://docs.myfundedperpetuals.com
"""
import json
import math
import os
import random
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import OrderedDict, deque
from http.client import HTTPException

SANDBOX_HOST = "https://sandbox.myfundedperpetuals.com"
EXEC_COLS = ["n", "bot", "simbolo", "mercado", "lado", "senal_utc", "apertura_utc", "cierre_utc", "orden_entrada",
             "posicion", "tamano", "apalancamiento", "entrada_modelo", "entrada_real", "sl_inicial", "tp_inicial",
             "salida_real", "riesgo_usd", "pnl_usd", "R_real", "comisiones", "funding", "motivo", "notas", "cuenta",
             "cuenta_id"]


def migrar_csv(path, nombre, cid):
    """ejecuciones.csv anterior a la 6.3: añade las columnas de la cuenta (eran todas de la única cuenta)."""
    import csv as _csv
    if not os.path.exists(path):
        return False
    with open(path, encoding="utf-8") as f:
        if "cuenta" in f.readline():
            return False
    with open(path, encoding="utf-8", newline="") as f:
        rows = list(_csv.reader(f))
    if not rows or "cuenta" in rows[0]:
        return False
    head = rows[0]
    out = [head + ["cuenta", "cuenta_id"]] + [r + [nombre or "", cid or ""] for r in rows[1:] if r]
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="") as f:
        _csv.writer(f).writerows(out)
    os.replace(tmp, path)
    return True


class ExecError(Exception):
    def __init__(self, status, code, msg, details=None):
        msg = resumir_respuesta(msg)
        super().__init__("HTTP {} {}: {}".format(status, code, msg) if code and code != "cloudflare" else "HTTP {}: {}".format(status, msg))
        self.status, self.code, self.msg, self.details = status, code, msg, details

    @property
    def cloudflare(self):
        return self.code == "cloudflare"


_RAY = re.compile(r"Ray ID:?\s*(?:<[^>]*>\s*)*([0-9a-f]{12,20})", re.I)
_TITLE = re.compile(r"<title>\s*(.*?)\s*</title>", re.I | re.S)


def es_html(text):
    t = (text or "").lstrip()[:200].lower()
    return t.startswith("<!doctype html") or t.startswith("<html") or "<head>" in t


def es_cloudflare(text):
    t = (text or "").lower()
    return es_html(text) and ("cloudflare" in t or "cf-error-details" in t or "/cdn-cgi/" in t)


def resumir_respuesta(text):
    """Una página HTML (de Cloudflare o de un proxy) no se copia entera en registros ni avisos: se resume."""
    text = str(text or "")
    if es_html(text):
        ray = _RAY.search(text)
        ray = " · Ray ID {}".format(ray.group(1)) if ray else ""
        if es_cloudflare(text):
            if "1015" in text or "rate limited" in text.lower():
                return "Cloudflare ha frenado la petición por exceso de peticiones" + ray
            return "el cortafuegos de Cloudflare de MyFundedPerps ha bloqueado la petición" + ray
        ti = _TITLE.search(text)
        return "respuesta HTML inesperada ({}){}".format(ti.group(1)[:80] if ti else "sin título", ray)
    text = " ".join(text.split())
    return text if len(text) <= 300 else text[:297] + "…"


LOCKOUT = "account_trader_lockout_active"


def lockout_expira(err):
    """Hora de desbloqueo (ms) de un rechazo por Trader Lockout, o None."""
    d = getattr(err, "details", None)
    if isinstance(d, dict):
        v = d.get("expiresAt") or d.get("expires_at")
    elif isinstance(d, list):
        v = next((x.get("expiresAt") for x in d if isinstance(x, dict) and x.get("expiresAt")), None)
    else:
        v = None
    try:
        return int(float(v)) if v else None
    except (TypeError, ValueError):
        return None


def host_permitido(host):
    u = urllib.parse.urlparse(host or "")
    return host.rstrip("/") == SANDBOX_HOST or u.hostname in ("localhost", "127.0.0.1")


# ============================ límite de lecturas por clave (desde la 9.4) ============================
# MyFundedPerps limita las GET a /v1/* por clave: 30 por minuto con claves de sandbox (las órdenes, cierres y
# cambios de stop no cuentan). KaBor se queda por debajo con su propio límite, deja sitio a lo de operar, respeta
# el Retry-After de un 429 y anota el aviso X-RateLimit-Warning del periodo de gracia.
GET_LIMITE_MFP = 30
GET_TOPE = {"alta": 24, "baja": 14}   # «baja»: lecturas del panel y la lectura periódica de las cuentas
_PRIORIDAD = threading.local()


class prioridad_baja:
    """with prioridad_baja(): las GET de dentro esperan si la clave va cargada, dejando sitio a lo de operar."""

    def __enter__(self):
        self.antes = getattr(_PRIORIDAD, "v", "alta")
        _PRIORIDAD.v = "baja"

    def __exit__(self, *a):
        _PRIORIDAD.v = self.antes


class Limitador:
    def __init__(self, fin):
        self.fin = fin                      # últimos 4 caracteres de la clave, para los registros
        self.t = deque()                    # GET del último minuto
        self.lock = threading.Lock()
        self.hasta = 0.0                    # MFP pidió esperar (429) hasta esta hora
        self.picos = deque()                # (minuto, máximo de GET en 60 s) de las últimas 24 h
        self.avisos = deque()               # horas de los X-RateLimit-Warning (24 h)
        self.r429 = deque()                 # horas de los 429 (24 h)
        self.esperas = deque()              # veces que KaBor esperó por su propio límite (24 h)
        self.ultimo_aviso = None            # (hora, ruta) del último aviso de MFP
        self.total = 0

    def _purgar(self, now):
        while self.t and now - self.t[0] >= 60:
            self.t.popleft()
        for d in (self.avisos, self.r429, self.esperas):
            while d and now - (d[0][0] if isinstance(d[0], tuple) else d[0]) > 86400:
                d.popleft()
        while self.picos and now - self.picos[0][0] * 60 > 86400:
            self.picos.popleft()

    def pedir(self, espera_max=75):
        """Espera hasta que haya sitio para una GET. Devuelve False si tendría que esperar más de espera_max."""
        prio = getattr(_PRIORIDAD, "v", "alta")
        tope = GET_TOPE.get(prio, GET_TOPE["alta"])
        t0, espero = time.time(), False
        while True:
            with self.lock:
                now = time.time()
                self._purgar(now)
                if now >= self.hasta and len(self.t) < tope:
                    self.t.append(now)
                    self.total += 1
                    m = int(now // 60)
                    if self.picos and self.picos[-1][0] == m:
                        self.picos[-1] = (m, max(self.picos[-1][1], len(self.t)))
                    else:
                        self.picos.append((m, len(self.t)))
                    if espero:
                        self.esperas.append(now)
                    return True
                wait = max(self.hasta - now, (self.t[0] + 60 - now) if len(self.t) >= tope else 0, 0.05)
            if time.time() - t0 + wait > espera_max:
                return False
            espero = True
            time.sleep(min(wait, 5) + random.random() * 0.2)

    def frenar(self, segundos):
        with self.lock:
            now = time.time()
            self.hasta = max(self.hasta, now + segundos)
            self.r429.append(now)

    def aviso(self, ruta):
        """MFP marcó una GET con X-RateLimit-Warning. Devuelve True si es el primero de la última hora."""
        with self.lock:
            now = time.time()
            nuevo = not self.avisos or now - self.avisos[-1] > 3600
            self.avisos.append(now)
            self.ultimo_aviso = (int(now * 1000), ruta)
            return nuevo

    def public(self):
        with self.lock:
            now = time.time()
            self._purgar(now)
            hora = [c for m, c in self.picos if now - m * 60 <= 3600]
            return {"minuto": len(self.t), "max_hora": max(hora) if hora else 0,
                    "max_dia": max((c for m, c in self.picos), default=0), "limite_mfp": GET_LIMITE_MFP,
                    "tope": GET_TOPE["alta"], "avisos_dia": len(self.avisos), "r429_dia": len(self.r429),
                    "esperas_dia": len(self.esperas), "frenado_hasta": int(self.hasta * 1000) if self.hasta > now else None,
                    "ultimo_aviso": self.ultimo_aviso, "total": self.total}


_LIMITADORES = {}
_LIM_LOCK = threading.Lock()
AL_AVISO = None        # función(fin de clave, texto) que pone el gestor de cuentas: registro y Telegram


def limitador(key):
    with _LIM_LOCK:
        if key not in _LIMITADORES:
            _LIMITADORES[key] = Limitador(str(key)[-4:])
        return _LIMITADORES[key]


def _avisar(lim, texto):
    if AL_AVISO:
        try:
            AL_AVISO(lim.fin, texto)
        except Exception:
            pass


# ============================ stream de la cuenta (desde la 9.4) ============================
# Un WebSocket por clave con las cuentas que ve: instantánea al conectar y luego cada cambio de órdenes, llenados,
# posiciones y saldo. Con él, KaBor ya no consulta por REST la posición en cada vela, el estado de una orden hasta
# que se llena ni las órdenes de salida. Si el stream no está al día, se usa la API REST como antes.
STREAM_SANDBOX = "wss://account-stream.myfundedperpetuals.com/v1/sandbox/stream"
STREAM_PRUEBAS = None    # solo pruebas en local: ws://127.0.0.1:PUERTO/v1/sandbox/stream
NO = object()            # el stream no puede responder: hay que preguntar a la API
TERMINALES = {"filled", "canceled", "cancelled", "rejected", "expired"}
VIVAS = ("filled", "cancelled", "canceled", "rejected", "expired")


def url_stream(host):
    if (host or "").rstrip("/") == SANDBOX_HOST:
        return STREAM_SANDBOX
    if STREAM_PRUEBAS and urllib.parse.urlparse(host or "").hostname in ("localhost", "127.0.0.1"):
        return STREAM_PRUEBAS
    return None


class StreamCuenta:
    SILENCIO = 45        # sin nada en 45 s: conexión muerta (lo indica MFP)
    PING = 20

    def __init__(self, key, url, ua="kcbot-pi"):
        self.key, self.url, self.ua = key, url, ua
        self.fin = str(key)[-4:]
        self.cv = threading.Condition()
        self.cuentas = {}            # id -> {"account", "ordenes": {id: orden}, "posiciones": {id: posición}}
        self.terminadas = OrderedDict()   # órdenes terminadas recientes (para esperar una orden)
        self.cerradas = OrderedDict()     # posiciones cerradas recientes
        self.pend = {}               # (cuenta, mercado) -> hora de un llenado aún sin su evento de posición
        self.listo = False
        self.upstream = True
        self.ultimo = 0.0
        self.seq = self.t_seq = self.stream_id = None
        self.estado, self.err, self.desde = "conectando", None, None
        self.conexiones, self.eventos, self.reanudadas = 0, 0, 0
        self.parar = False
        self.ws = None
        self.hilo = threading.Thread(target=self._bucle, name="stream-cuenta-" + self.fin, daemon=True)
        self.hilo.start()

    # ---------- consultas (las usa MFPTrading) ----------
    def al_dia(self):
        return self.listo and self.upstream and time.time() - self.ultimo < self.SILENCIO

    def posicion(self, acc, market_id, espera=3.0):
        fin = time.time() + espera
        with self.cv:
            while True:
                if not self.al_dia() or acc not in self.cuentas:
                    return NO
                t = self.pend.get((acc, market_id))
                if t is None or time.time() - t > 4:
                    self.pend.pop((acc, market_id), None)
                    break
                if time.time() >= fin:
                    return NO        # llenado sin su posición: mejor preguntar a la API
                self.cv.wait(0.25)
            return next((dict(p) for p in self.cuentas[acc]["posiciones"].values()
                         if p.get("market_id") == market_id and p.get("status", "open") == "open"), None)

    def ordenes(self, acc):
        with self.cv:
            if not self.al_dia() or acc not in self.cuentas:
                return NO
            return [dict(o) for o in self.cuentas[acc]["ordenes"].values()]

    def orden(self, oid):
        with self.cv:
            o = self.terminadas.get(oid)
            if o is None:
                for c in self.cuentas.values():
                    if oid in c["ordenes"]:
                        o = c["ordenes"][oid]
                        break
            return dict(o) if o else None

    def esperar_orden(self, oid, max_s):
        """La orden ya resuelta (llena, rechazada…) según el stream, o None si no se sabe en max_s segundos."""
        fin = time.time() + max_s
        with self.cv:
            while time.time() < fin and self.al_dia():
                o = self.terminadas.get(oid)
                if o is not None:
                    return dict(o)
                self.cv.wait(min(0.5, max(0.05, fin - time.time())))
        return None

    def cerrada(self, pid, espera=0.0):
        """La posición cerrada según el stream (espera unos segundos a su evento si se acaba de cerrar)."""
        fin = time.time() + espera
        with self.cv:
            while True:
                p = self.cerradas.get(pid)
                if p or time.time() >= fin or not self.al_dia():
                    return dict(p) if p else None
                self.cv.wait(0.25)

    def bloqueada(self, acc):
        with self.cv:
            c = self.cuentas.get(acc)
            return bool(c and c.get("locked_out"))

    def public(self):
        return {"estado": self.estado, "al_dia": self.al_dia(), "desde": self.desde, "error": self.err,
                "conexiones": self.conexiones, "eventos": self.eventos, "cuentas": len(self.cuentas),
                "silencio_s": round(time.time() - self.ultimo, 1) if self.ultimo else None}

    def cerrar(self):
        self.parar = True
        try:
            if self.ws:
                self.ws.close()
        except Exception:
            pass

    # ---------- conexión ----------
    def _bucle(self):
        try:
            import websocket
        except ImportError:
            self.estado, self.err = "sin websocket-client", "falta el paquete websocket-client"
            return
        espera = 2
        while not self.parar:
            url = self.url
            if self.stream_id and self.seq:
                url += "?" + urllib.parse.urlencode({"resume": self.stream_id, "last_seq": self.seq,
                                                     "since": self.t_seq or int((time.time() - 60) * 1000)})
            retry = None
            try:
                self.estado = "conectando"
                self.ws = websocket.create_connection(url, timeout=20, header=[
                    "Authorization: Bearer " + self.key, "User-Agent: " + self.ua])
                self.ws.settimeout(5)
                self.conexiones += 1
                self.ultimo = time.time()
                espera = 2
                self._leer()
            except websocket.WebSocketBadStatusException as e:
                code = getattr(e, "status_code", None)
                h = getattr(e, "resp_headers", None) or {}
                ra = h.get("retry-after") or h.get("Retry-After")
                if code == 401:
                    self.err, retry = "la clave no vale para el stream (401)", 300
                elif code == 403:
                    self.err, retry = "sin acceso al sandbox (403: hace más de 90 días sin comprar un challenge)", 900
                elif code in (429, 503):
                    self.err = "MyFundedPerps pide esperar ({})".format(code)
                    retry = float(ra) if ra and str(ra).replace(".", "", 1).isdigit() else 30
                else:
                    self.err = "HTTP {}".format(code)
                self.stream_id = None if code in (401, 403) else self.stream_id
            except Exception as e:
                if not self.parar:
                    self.err = "{}: {}".format(type(e).__name__, e)[:200]
            finally:
                with self.cv:
                    self.listo = False
                    self.cv.notify_all()
                try:
                    if self.ws:
                        self.ws.close()
                except Exception:
                    pass
                self.ws = None
            if self.parar:
                break
            self.estado = "reconectando"
            time.sleep((retry if retry is not None else espera) + random.random() * 2)
            espera = min(espera * 2, 60)

    def _leer(self):
        import websocket
        ping_t, n = time.time(), 0
        while not self.parar:
            if time.time() - self.ultimo > self.SILENCIO:
                raise RuntimeError("{} s sin noticias del stream".format(self.SILENCIO))
            if time.time() - ping_t > self.PING:
                n += 1
                self.ws.send(json.dumps({"op": "ping", "id": n}))
                ping_t = time.time()
            try:
                raw = self.ws.recv()
            except websocket.WebSocketTimeoutException:
                continue
            if raw is None or raw == "":
                raise RuntimeError("el stream se ha cerrado")
            self.ultimo = time.time()
            try:
                fr = json.loads(raw)
            except ValueError:
                continue
            if self._frame(fr) == "reconectar":
                return

    # ---------- mensajes ----------
    def _cuenta(self, acc):
        return self.cuentas.setdefault(acc, {"account": {}, "ordenes": {}, "posiciones": {}, "locked_out": False})

    @staticmethod
    def _lista(fr, *claves):
        for k in claves:
            v = fr.get(k)
            if isinstance(v, list):
                return v
        return []

    def _frame(self, fr):
        tipo = fr.get("type")
        with self.cv:
            if fr.get("seq") is not None:
                self.seq = fr["seq"]
            if fr.get("time") is not None and fr.get("seq") is not None:
                self.t_seq = fr["time"]
            if tipo == "hello":
                if not fr.get("resumed"):
                    self.cuentas, self.pend = {}, {}
                else:
                    self.reanudadas += 1
                self.stream_id = fr.get("stream_id") or self.stream_id
                self.listo = bool(fr.get("resumed"))
                self.estado = "al día" if self.listo else "recibiendo la instantánea"
            elif tipo == "snapshot":
                acc = fr.get("account_id") or (fr.get("account") or {}).get("id")
                if acc:
                    c = {"account": fr.get("account") or {}, "ordenes": {}, "posiciones": {},
                         "locked_out": bool((fr.get("account") or {}).get("locked_out") or fr.get("locked_out"))}
                    for o in self._lista(fr, "orders", "working_orders"):
                        if o.get("id"):
                            c["ordenes"][o["id"]] = o
                    for p in self._lista(fr, "positions", "open_positions"):
                        if p.get("id"):
                            c["posiciones"][p["id"]] = p
                    self.cuentas[acc] = c
                    for k in [k for k in self.pend if k[0] == acc]:
                        self.pend.pop(k, None)
            elif tipo == "snapshot_end":
                self.listo, self.err = True, None
                self.estado = "al día"
                if not self.desde:
                    self.desde = int(time.time() * 1000)
            elif tipo == "heartbeat":
                self.upstream = fr.get("upstream_connected") is not False
            elif tipo == "order":
                o = dict(fr.get("order") or {})
                acc = fr.get("account_id") or o.get("account_id")
                ev = fr.get("event")
                if o.get("id") and acc:
                    for k in ("cancel_reason", "reject_reason", "reject_rule_id"):
                        if fr.get(k) is not None and o.get(k) is None:
                            o[k] = fr[k]
                    if ev in TERMINALES:
                        o["status"] = "canceled" if ev == "cancelled" else ev if o.get("status") not in TERMINALES \
                            else o["status"]
                    c = self._cuenta(acc)
                    if ev in TERMINALES or o.get("status") in TERMINALES:
                        c["ordenes"].pop(o["id"], None)
                        self.terminadas[o["id"]] = o
                        while len(self.terminadas) > 500:
                            self.terminadas.popitem(last=False)
                    else:
                        c["ordenes"][o["id"]] = o
                self.eventos += 1
            elif tipo == "fill":
                f = fr.get("fill") or {}
                acc = fr.get("account_id")
                if acc and f.get("market_id") and not fr.get("replay"):
                    self.pend[(acc, f["market_id"])] = time.time()
                self.eventos += 1
            elif tipo == "position":
                p = dict(fr.get("position") or {})
                acc = fr.get("account_id") or p.get("account_id")
                if p.get("id") and acc:
                    c = self._cuenta(acc)
                    if fr.get("event") == "closed" or p.get("status") == "closed":
                        if fr.get("close_reason") and not p.get("close_reason"):
                            p["close_reason"] = fr["close_reason"]
                        p["status"] = "closed"
                        c["posiciones"].pop(p["id"], None)
                        self.cerradas[p["id"]] = p
                        while len(self.cerradas) > 300:
                            self.cerradas.popitem(last=False)
                    else:
                        c["posiciones"][p["id"]] = p
                    if p.get("market_id"):
                        self.pend.pop((acc, p["market_id"]), None)
                self.eventos += 1
            elif tipo == "account":
                acc = fr.get("account_id")
                if acc and fr.get("event") == "removed":
                    self.cuentas.pop(acc, None)
                elif acc:
                    c = self._cuenta(acc)
                    if fr.get("account"):
                        c["account"] = fr["account"]
                    if fr.get("locked_out") is not None:
                        c["locked_out"] = bool(fr["locked_out"])
                self.eventos += 1
            elif tipo == "reconnect":
                return "reconectar"
            elif tipo in ("error", "notice"):
                self.err = "{}: {}".format(fr.get("code") or tipo, fr.get("message") or "")[:200]
            self.cv.notify_all()
        return None


_STREAMS = {}


def stream_para(key, host, ua="kcbot-pi"):
    """El stream de una clave (uno por clave, aunque varias cuentas la compartan). None si no hay stream."""
    url = url_stream(host)
    if not url or not key:
        return None
    with _LIM_LOCK:
        s = _STREAMS.get(key)
        if s is None or s.parar or s.url != url:
            s = _STREAMS[key] = StreamCuenta(key, url, ua)
        return s


def streams_en_uso(claves):
    """Cierra los streams de las claves que ya no usa ninguna cuenta."""
    with _LIM_LOCK:
        for k in [k for k in _STREAMS if k not in claves]:
            _STREAMS.pop(k).cerrar()


def estado_clave(key):
    s = _STREAMS.get(key)
    return {"lecturas": limitador(key).public(), "stream": s.public() if s else None}


class MFPTrading:
    """Cliente REST de la cuenta sandbox con escritura (órdenes, cierres y stops). Desde la 9.4 lee del stream de
    la cuenta lo que puede (posiciones, órdenes) y pasa sus GET por el límite de la clave."""

    def __init__(self, host, key, timeout=20, user_agent="kcbot-pi"):
        if not host_permitido(host):
            raise ValueError("Solo se puede operar en el sandbox ({}), no en {}".format(SANDBOX_HOST, host))
        if not str(key).startswith("fp_test_"):
            raise ValueError("Para operar hace falta una clave de sandbox (fp_test_)")
        self.host, self.key, self.timeout, self.ua = host.rstrip("/"), key, timeout, user_agent
        self.lim = limitador(key)
        self.stream = None          # StreamCuenta, si la cuenta lo usa (lo pone cuentas.Cuenta)
        self.pol_cache = {}         # cuenta -> (trading policy, hora): cambia muy poco

    def _call(self, method, path, body=None, idem=None, params=None):
        url = self.host + path
        if params:
            url += "?" + urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
        data = json.dumps(body).encode() if body is not None else None
        # Solo se reintenta lo que no puede duplicar nada: lecturas y escrituras con clave de idempotencia.
        tries = 3 if method == "GET" or idem else 1
        for i in range(tries):
            if method == "GET" and not self.lim.pedir():
                raise ExecError(429, "kabor_limite", "demasiadas lecturas seguidas en esta clave: se deja para luego")
            req = urllib.request.Request(url, data=data, method=method)
            req.add_header("Accept", "application/json")
            req.add_header("User-Agent", self.ua)
            req.add_header("Authorization", "Bearer " + self.key)
            if data is not None:
                req.add_header("Content-Type", "application/json")
            if idem:
                req.add_header("Idempotency-Key", idem)
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as r:
                    raw = r.read()
                    if method == "GET" and r.headers.get("X-RateLimit-Warning"):
                        if self.lim.aviso(path):
                            _avisar(self.lim, "MyFundedPerps avisa de que la clave …{} supera su límite de lecturas "
                                    "(GET {}).".format(self.lim.fin, path))
                    return json.loads(raw) if raw else {}
            except urllib.error.HTTPError as e:
                text = e.read().decode(errors="replace")[:8000]
                code, msg, details = "", text, None
                try:
                    err = json.loads(text).get("error") or {}
                    code, msg = err.get("code", ""), err.get("message", "") or text
                    details = err.get("details")
                except Exception:
                    if es_cloudflare(text):
                        code = "cloudflare"
                if e.code == 429:
                    wait = e.headers.get("Retry-After") or "60"
                    wait = min(float(wait) if wait.replace(".", "", 1).isdigit() else 60, 120)
                    if method == "GET" or e.headers.get("X-RateLimit-Scope") == "api_key":
                        self.lim.frenar(wait)       # todas las lecturas de la clave esperan
                        _avisar(self.lim, "MyFundedPerps ha frenado la clave …{} ({} {}): se espera {:.0f} s.".format(
                            self.lim.fin, method, path, wait))
                    if i < tries - 1:
                        if method != "GET":
                            time.sleep(wait + random.random())
                        continue
                if e.code in (500, 502, 503, 504) and i < tries - 1:
                    wait = e.headers.get("Retry-After") or str(2 * (i + 1))
                    time.sleep(min(float(wait) if wait.replace(".", "", 1).isdigit() else 5, 30) + random.random())
                    continue
                if code == "cloudflare" and i < tries - 1:
                    # El cortafuegos suele cortar ráfagas: se espera más antes de repetir (solo lecturas e
                    # idempotentes, que no pueden duplicar nada).
                    time.sleep(6 * (i + 1) + 2 * random.random())
                    continue
                raise ExecError(e.code, code, msg, details)
            except (OSError, ValueError, HTTPException) as e:
                if i == tries - 1:
                    raise ExecError("red", type(e).__name__, str(e))
                time.sleep(2 * (i + 1))

    def get(self, path, **params):
        return self._call("GET", path, params=params or None)

    @staticmethod
    def data(res):
        return res.get("data", res) if isinstance(res, dict) else res

    # ---- lecturas ----
    def accounts(self):
        return self.data(self.get("/v1/accounts")) or []

    def account(self, acc):
        return self.data(self.get("/v1/accounts/" + acc))

    def policy(self, acc, max_s=600):
        c = self.pol_cache.get(acc)
        if c and time.time() - c[1] < max_s:
            return c[0]
        pol = self.data(self.get("/v1/accounts/{}/trading-policy".format(acc)))
        self.pol_cache[acc] = (pol, time.time())
        return pol

    def market(self, market_id):
        return self.data(self.get("/v1/markets/" + urllib.parse.quote(market_id, safe="")))

    def mid(self, market_id):
        q = self.data(self.get("/v1/markets/{}/quote".format(urllib.parse.quote(market_id, safe=""))))
        return float(q["mid"])

    def _st(self):
        s = self.stream
        return s if s is not None and s.al_dia() else None

    def position(self, acc, market_id):
        s = self._st()
        if s:
            p = s.posicion(acc, market_id)
            if p is not NO:
                return p
        pos = self.data(self.get("/v1/positions", account_id=acc)) or []
        return next((p for p in pos if p.get("market_id") == market_id), None)

    def positions(self, acc):
        """Posiciones abiertas de la cuenta (todas)."""
        s = self._st()
        if s:
            with s.cv:
                if acc in s.cuentas:
                    return [dict(p) for p in s.cuentas[acc]["posiciones"].values()]
        return self.data(self.get("/v1/positions", account_id=acc)) or []

    def closed_position(self, acc, position_id):
        s = self.stream
        if s is not None:
            p = s.cerrada(position_id, espera=3.0 if s.al_dia() else 0.0)
            if p is not None:
                return p
        res = self.data(self.get("/v1/positions", account_id=acc, status="closed", limit=50)) or []
        return next((p for p in res if p.get("id") == position_id), None)

    def _working(self, acc):
        s = self._st()
        if s:
            r = s.ordenes(acc)
            if r is not NO:
                return r
        return self.data(self.get("/v1/orders", account_id=acc, status="working")) or []

    def working_exits(self, acc, market_id):
        return [o for o in self._working(acc) if o.get("market_id") == market_id and o.get("reduce_only")
                and o.get("status") in ("resting", "pending")]

    def order(self, oid):
        return self.data(self.get("/v1/orders/" + oid))

    # ---- escrituras ----
    def place(self, body, idem):
        return self.data(self._call("POST", "/v1/orders", body, idem))

    def close(self, position_id, body, idem):
        return self.data(self._call("POST", "/v1/positions/{}/close".format(position_id), body, idem))

    def put_exits(self, position_id, body):
        return self.data(self._call("PUT", "/v1/positions/{}/exit-orders".format(position_id), body))

    def close_all(self, acc, idem):
        """Cierra todas las posiciones de la cuenta (como «Close All» de la web)."""
        return self.data(self._call("POST", "/v1/accounts/{}/close-all-positions".format(acc), None, idem))

    def cancel_all(self, acc, idem):
        """Cancela todas las órdenes en espera de la cuenta."""
        return self.data(self._call("POST", "/v1/accounts/{}/cancel-all-orders".format(acc), None, idem))

    def working_orders(self, acc):
        """Órdenes vivas de la cuenta (cualquier mercado)."""
        return [o for o in self._working(acc) if o.get("status") not in VIVAS]

    def wait_order(self, order, max_s=20):
        """Una orden aceptada empieza en pending: se espera a filled / rejected. Con el stream al día, se espera a
        su evento sin consultar la API; si no llega en unos segundos, se pregunta como antes."""
        t0, i = time.time(), 0
        if order.get("status") == "pending" and order.get("id") and self._st():
            o = self.stream.esperar_orden(order["id"], min(8, max_s))
            if o is not None:
                return dict(order, **o)
        while order.get("status") == "pending" and time.time() - t0 < max_s:
            time.sleep(min(0.6 * 2 ** i, 4))
            i += 1
            o = self.stream.orden(order["id"]) if self.stream is not None else None
            order = o if o is not None and o.get("status") in TERMINALES else self.order(order["id"])
        return order


def round_step(x, step, decimals=None):
    v = math.floor(x / step + 1e-9) * step
    return round(v, decimals if decimals is not None else max(0, -int(math.floor(math.log10(step))) if step < 1 else 0))


def es_sl(o, side):
    """¿Es esta orden de salida el stop? (si no, es el objetivo)."""
    role = o.get("bracket_role")
    if role:
        return role == "stop_loss"
    typ = str(o.get("type") or o.get("mode") or "")
    if typ.startswith("stop"):
        return True
    if typ.startswith("take"):
        return False
    return None


class Ejecutor:
    """Uno por bot y cuenta. Replica en esa cuenta lo que hace el modelo del bot."""

    def __init__(self, engine, cid, saved=None, grupo=None):
        self.e, self.cid, self.grupo = engine, cid, grupo
        s = saved or {}
        self.pos = s.get("pos")             # posición real gestionada por este bot (o None)
        self.n = s.get("n", 0)              # operaciones reales abiertas por este bot
        self.last_error = s.get("last_error")
        self.last_action = s.get("last_action")
        self.mkt_cache = {}

    # ---------- estado ----------
    def dump(self):
        return {"pos": self.pos, "n": self.n, "last_error": self.last_error, "last_action": self.last_action}

    @property
    def hub(self):
        return self.e.hub

    @property
    def cta(self):
        g = getattr(self.hub, "cuentas", None)
        return g.get(self.cid) if g else None

    @property
    def tr(self):
        return self.cta.trader

    @property
    def acc(self):
        return self.cta.account_id

    @property
    def cname(self):
        c = self.cta
        return c.nombre if c else self.cid

    def configurado(self):
        """El bot quiere operar en esta cuenta (si se quitó de sus cuentas, solo gestiona lo que tenga abierto)."""
        return self.e.P.get("ex_modo") == "sandbox" and (self.grupo is None or self.cid in self.grupo.deseadas())

    def listo(self):
        c = self.cta
        return bool(c and c.listo())

    def activo(self):
        """Opera de verdad: el bot lo pide y el programa y la cuenta lo permiten."""
        return self.configurado() and self.listo()

    def problema(self):
        c = self.cta
        if not c:
            return "la cuenta {} ya no existe".format(self.cid)
        return c.problema()

    def public(self):
        why = None
        if self.configurado() and not self.listo():
            why = self.problema()
        c = self.cta
        blq, bmot = c.bloqueada() if c else (False, "")
        return {"cuenta_id": self.cid, "cuenta_nombre": self.cname, "configurado": self.configurado(),
                "activo": self.activo(), "motivo_inactivo": why, "cuenta": c.account_id if c else None,
                "bloqueada": blq, "bloqueo_motivo": bmot, "bloqueo_texto": c.texto_hasta() if blq else "",
                "posicion": self.pos, "operaciones": self.n, "ultimo_error": self.last_error,
                "ultima_accion": self.last_action}

    def _log(self, msg, level="info", t=None, kind="ejecucion", **ev):
        self.e.log("[{}] ".format(self.cname) + msg, level)
        self.e._event(t or self.e.last_t or int(time.time() * 1000), kind, info=("[{}] ".format(self.cname) + msg)[:300],
                      **ev)

    def _fail(self, what, err, t=None):
        msg = "{}: {}".format(what, err)
        self.last_error = {"t": int(time.time() * 1000), "msg": msg[:400]}
        self.e.cnt["errors"] += 1
        self._log(msg, "error", t, kind="ejecucion_error")
        self.e.notify("ejecucion", "⚠️ <b>{}</b> · {}\n{}".format(self._esc(self.e.name), self._esc(self.cname),
                                                                   self._esc(msg)))

    def _lockout(self, err, t=None):
        """Rechazo por Trader Lockout de la web: la cuenta queda bloqueada hasta la hora que da la API."""
        exp = lockout_expira(err)
        self._log("MyFundedPerps rechaza órdenes: Trader Lockout activo en la cuenta{}.".format(
            " hasta " + time.strftime("%d/%m %H:%M UTC", time.gmtime(exp / 1000)) if exp else ""), "warn", t,
            kind="ejecucion_bloqueada")
        try:
            self.hub.cuentas.lockout_web(self.cid, exp)
        except Exception as e:
            self.e.log("No se pudo anotar el bloqueo: {}".format(e), "error")

    @staticmethod
    def _esc(s):
        return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    PRECIO_VIVO_S = 5

    def _precio(self, market_id):
        """Precio actual: el del flujo de mercado que ya recibe el bot si es de hace menos de 5 s (no gasta
        lecturas de la clave); si no, la cotización de la API."""
        try:
            m = self.e.market
            if m and m.get("market_id") == market_id:
                px, t, kind = self.e.live_price()
                if px and t and kind not in ("cierre", "candle") and time.time() * 1000 - t < self.PRECIO_VIVO_S * 1000:
                    return float(px)
        except Exception:
            pass
        return self.tr.mid(market_id)

    def _market(self, market_id):
        m = self.mkt_cache.get(market_id)
        if not m or time.time() - m[1] > 3600:
            m = (self.tr.market(market_id), time.time())
            self.mkt_cache[market_id] = m
        return m[0]

    # ---------- comprobaciones previas ----------
    def _policy_ok(self, pol, a, risk_usd):
        """Trading policy y daily loss antes de una orden. Devuelve (ok, motivo)."""
        P = self.e.P
        if a.get("status") != "active":
            return False, "la cuenta está {}".format(a.get("status"))
        for flag, txt in (("manual_trading_blocked", "trading bloqueado en la cuenta"),
                          ("opening_exposure_restricted", "la cuenta no puede abrir exposición ahora")):
            if pol.get(flag):
                return False, txt
        if pol.get("restriction"):
            return False, "restricción en la cuenta: {}".format(pol["restriction"].get("reason"))
        halt = pol.get("trading_halt") or {}
        cat = (self.e.market or {}).get("category")
        if halt.get("platform") or (cat and cat in (halt.get("categories") or [])):
            return False, "trading detenido por la plataforma"
        comp = pol.get("competition") or {}
        allow = comp.get("allowlist") or comp.get("symbols") or []
        if allow and (self.e.market or {}).get("coin") not in allow and (self.e.market or {}).get("symbol") not in allow:
            return False, "mercado no permitido en la competición"
        if P["ex_dll_min_pct"] > 0:
            risk = a.get("risk") or {}
            pct = (risk.get("requirements") or {}).get("daily_loss_pct")
            room = risk.get("daily_loss_room")
            if pct:
                limit = float(a["starting_balance"]) * (pct / 100 if pct > 1 else pct)
                if room is None:
                    return False, "no se conoce el daily loss que queda (marcas incompletas)"
                if float(room) - risk_usd < P["ex_dll_min_pct"] / 100 * limit:
                    return False, "tras un SL quedaría {:.0f} $ de daily loss, menos del {:g} % de {:.0f} $".format(
                        float(room) - risk_usd, P["ex_dll_min_pct"], limit)
        return True, ""

    def _riesgo_usd(self, a):
        P = self.e.P
        mult = self.cta.mult if self.cta else 1.0
        if P["ex_riesgo_modo"] == "importe fijo ($)":
            return float(P["ex_riesgo_usd"]) * mult
        return float(a.get("balance") or 0) * P["ex_riesgo_pct"] / 100 * mult

    # ---------- señal: abrir ----------
    def on_signal(self, t, side, cl, sl, tp_fixed):
        if not self.activo():
            return
        e = self.e
        S = "LONG" if side == 1 else "SHORT"
        if e._late(t):
            self._log("Señal {} de una vela antigua: no se envía orden.".format(S), t=t)
            return
        if self.pos:
            self._log("Señal {} ignorada: ya hay una posición real abierta.".format(S), t=t)
            return
        blq, why = self.cta.bloqueada()
        if blq:
            self._log("Señal {} sin orden: cuenta bloqueada {} ({}).".format(S, self.cta.texto_hasta(), why), "warn", t,
                      kind="ejecucion_bloqueada")
            self.e.notify("ejecucion", "🔒 <b>{}</b> · señal {} sin orden en «{}»\nCuenta bloqueada {}.".format(
                self._esc(e.name), S, self._esc(self.cname), self._esc(self.cta.texto_hasta())))
            return
        try:
            self._abrir(t, side, cl, sl, tp_fixed)
        except ExecError as err:
            if err.code == LOCKOUT:
                self._lockout(err, t)
                return
            self._fail("Orden {} no enviada".format(S), err, t)
        except Exception as err:
            self._fail("Orden {} no enviada".format(S), "{}: {}".format(type(err).__name__, err), t)

    def _abrir(self, t, side, cl, sl, tp_fixed):
        e, P, tr = self.e, self.e.P, self.tr
        acc, mk = self.acc, e.market
        S = "LONG" if side == 1 else "SHORT"
        if tr.position(acc, mk["market_id"]):
            self._log("Señal {} ignorada: ya hay una posición en {} en la cuenta (no la abrió este bot).".format(
                S, mk["market_id"]), "warn", t)
            return
        m = self._market(mk["market_id"])
        if m.get("trading_enabled") is False or m.get("reduce_only"):
            self._log("Señal {} ignorada: el mercado {} no admite posiciones nuevas.".format(S, mk["market_id"]), "warn", t)
            return
        mid = self._precio(mk["market_id"])
        dist = (mid - sl) * side
        if dist <= 0:
            self._log("Señal {} ignorada: el precio ({:.6g}) ya está al otro lado del stop ({:.6g}).".format(
                S, mid, sl), "warn", t)
            return
        cost_r = e.st.cost / 100 * mid / dist if e.st else 0
        if P["max_cost"] and cost_r > P["max_cost"]:
            self._log("Señal {} ignorada: la comisión serían {:.2f}R.".format(S, cost_r), t=t)
            return
        rr = e.st.S.rr() if e.st else None
        if tp_fixed is not None:
            tp = tp_fixed if (tp_fixed - mid) * side > 0 else None
            if tp is None:
                self._log("Señal {} ignorada: el precio ya pasó el objetivo.".format(S), t=t)
                return
        else:
            tp = mid + side * rr * dist if rr is not None else None     # dinámico: lo pone el modelo al abrir
        pol, a = tr.policy(acc), tr.account(acc)
        mult = self.cta.mult if self.cta else 1.0
        fijo = (e.st.pending or {}).get("tamano") if e.st else None          # tamaño fijo del plan manual
        if not fijo and P["ex_riesgo_modo"] == "tamaño fijo (contratos)":
            fijo = float(P.get("ex_tamano") or 0)
        if fijo:
            fijo = float(fijo) * mult
            risk = fijo * dist                         # con tamaño fijo, el riesgo sale de la distancia al stop
        else:
            risk = self._riesgo_usd(a)
        ok, why = self._policy_ok(pol, a, risk)
        if not ok:
            self._log("Señal {} sin orden: {}.".format(S, why), "warn", t, kind="ejecucion_bloqueada")
            self.e.notify("ejecucion", "⏸ <b>{}</b> · señal {} sin orden en «{}»\n{}".format(
                self._esc(e.name), S, self._esc(self.cname), self._esc(why)))
            return
        step, dec = float(m["size_step"]), m.get("size_decimals")
        qty = fijo if fijo else risk / dist
        cap = [x for x in (P["ex_nocional_max"], (pol.get("limits") or {}).get("max_position_value_usd")) if x]
        if cap and qty * mid > min(cap):
            qty = min(cap) / mid
        size = round_step(qty, step, dec)
        notional = size * mid
        if size < float(m.get("min_size") or 0) or notional < float(m.get("min_notional") or 0):
            self._log("Señal {} sin orden: el tamaño ({}) no llega al mínimo del mercado.".format(S, size), "warn", t)
            return
        avail = float((a.get("risk") or {}).get("available_balance") or a.get("balance") or 0)
        lev_need = max(1, math.ceil(notional / avail * 1.25)) if avail > 0 else 999
        lev_max = min(int(P["ex_apalancamiento"]), int(float(m.get("max_leverage") or 1)))
        if lev_need > lev_max:
            self._log("Señal {} sin orden: necesitaría apalancamiento {}x y el máximo es {}x.".format(
                S, lev_need, lev_max), "warn", t)
            return
        cid = "kc{}-{}-{}-{}".format(e.id, self.cid, t, "L" if side == 1 else "S")   # único por bot, cuenta y señal
        body = {"client_order_id": cid, "type": "market", "account_id": acc, "market_id": mk["market_id"],
                "side": "buy" if side == 1 else "sell", "size": size, "expected_price": mid,
                "leverage": lev_need, "margin_mode": P["ex_margen"], "stop_loss_price": round(sl, 8)}
        if tp is not None:
            body["take_profit_price"] = round(tp, 8)
        if P["ex_slippage_bps"]:
            body["slippage_tolerance_bps"] = P["ex_slippage_bps"]
        self._log("Enviando orden {} {} {} (riesgo {:.2f} $, SL {:.6g}{}).".format(
            S, size, mk["market_id"], size * dist, sl, ", TP {:.6g}".format(tp) if tp is not None else ""), t=t,
            kind="orden_enviada")
        order = tr.wait_order(tr.place(body, cid))
        st = order.get("status")
        if st != "filled":
            why = order.get("reject_reason") or st
            if order.get("reject_reason") == LOCKOUT:
                self._lockout(ExecError(422, LOCKOUT, why, order.get("details")), t)
                return
            self._fail("Orden {} {}".format(S, "rechazada" if st == "rejected" else "sin ejecutar"), why, t)
            return
        fill = float(order.get("fill_price") or mid)
        rp = tr.position(acc, mk["market_id"])
        self.n += 1
        self.pos = {"id": rp["id"] if rp else None, "order_id": order["id"], "client_id": cid, "side": side,
                    "size": float(rp["size"]) if rp else float(order.get("size") or size), "entry": fill,
                    "entry_model": None, "sl": sl, "tp": tp, "sl0": sl, "tp0": tp, "risk_usd": abs(fill - sl) * size,
                    "t_signal": t, "t_open": int(time.time() * 1000), "leverage": lev_need, "partial": 0,
                    "closing": None}
        self.last_error = None
        self.last_action = {"t": int(time.time() * 1000), "msg": "{} abierto a {:.6g}".format(S, fill)}
        self._log("{} abierto: {} a {:.6g} (posición {}).".format(S, size, fill, self.pos["id"]), "warn", t,
                  kind="apertura_real", side=S, price=fill, sl=sl, tp=tp if tp is not None else "")
        self.e.notify("ejecucion", "{} <b>{} abierto en «{}»</b> · {}\n{} {} a {}\nSL {}{}\nRiesgo {:.0f} $".format(
            "🟢" if side == 1 else "🔴", S, self._esc(self.cname), self._esc(e.name), size, self._esc(mk["market_id"]),
            self._fmt(fill), self._fmt(sl), " · TP " + self._fmt(tp) if tp is not None else " · TP dinámico",
            self.pos["risk_usd"]))
        try:
            self.hub.cuentas.contar_apertura(self.cid)
        except Exception as err:
            e.log("Cuenta «{}»: {}".format(self.cname, err), "error")

    @staticmethod
    def _fmt(v):
        return "{:.6g}".format(v) if v is not None else "–"

    # ---------- después de cada vela ----------
    def after_candle(self, evs, t):
        """Se llama tras procesar cada vela cerrada, con los eventos del modelo."""
        if not self.pos:
            return
        c = self.cta
        if not (c and c.trader and (c.account_id or c.resolver())):
            return              # sin conexión con la cuenta: la posición conserva su SL/TP en ella
        if not self.hub.cuentas.ordenes:
            return              # órdenes desactivadas: no se toca la cuenta (sigue con su SL/TP)
        if (c.bloqueo or {}).get("origen") == "web" and c.bloqueada()[0]:
            return              # Trader Lockout de la web: MFP no deja tocar nada; la posición sigue con su SL/TP
        try:
            self._gestionar(evs, t)
        except ExecError as err:
            if err.code == LOCKOUT:
                self._lockout(err, t)
                return
            self._fail("Gestión de la posición real", err, t)
        except Exception as err:
            self._fail("Gestión de la posición real", "{}: {}".format(type(err).__name__, err), t)

    def _gestionar(self, evs, t):
        e, P, tr = self.e, self.e.P, self.tr
        acc, p = self.acc, self.pos
        mk = e.market
        rp = tr.position(acc, mk["market_id"]) if mk else None
        if rp is None or (p.get("id") and rp.get("id") != p["id"]):
            self._registrar_cierre(t)
            return
        p["id"], p["size"] = rp["id"], float(rp["size"])
        mpos = e.st.pos if e.st else None
        pending = bool(e.st and e.st.pending)        # señal dada, el modelo entra en la próxima apertura
        for ev in evs:
            if ev[0] == "open" and mpos:
                p["entry_model"] = ev[3]
            if ev[0] == "partial" and ev[4] > 0 and not p.get("partial"):
                self._cerrar(rp, "venta parcial del modelo", fraction=ev[4], t=t)
                rp = tr.position(acc, mk["market_id"])
                if rp is None:
                    self._registrar_cierre(t)
                    return
                p["size"], p["partial"] = float(rp["size"]), ev[4]
        if mpos is None:
            if pending:
                return
            if P["ex_alinear"]:
                why = next((x[2]["why"] for x in evs if x[0] == "exit"), None)
                txt = "el modelo cerró ({})".format(e.why_names().get(why, why)) if why else \
                    "el modelo no tiene posición"
                self._cerrar(rp, txt, t=t)
                self._registrar_cierre(t)
            return
        # el modelo sigue dentro: el stop y el objetivo de la cuenta siguen a los del modelo
        sl, tp = mpos["sl"], mpos.get("tp")
        if self._distinto(sl, p.get("sl")) or self._distinto(tp, p.get("tp")) or (tp is None) != (p.get("tp") is None):
            self._sync_exits(rp, sl, tp, t)

    def _distinto(self, a, b):
        if a is None or b is None:
            return a is not b
        return abs(a - b) > max(abs(a) * 2e-5, 1e-9)

    def _sync_exits(self, rp, sl, tp, t, retry=True):
        tr, p = self.tr, self.pos
        side = 1 if rp.get("side") == "long" else -1
        acc, mid = self.acc, self.e.market["market_id"]
        # el stop nunca puede quedar al otro lado del precio
        px = self._precio(mid)
        if (px - sl) * side <= 0:
            self._log("Stop nuevo {:.6g} ya superado por el precio ({:.6g}): se cierra a mercado.".format(sl, px), "warn", t)
            self._cerrar(rp, "stop del modelo ya superado", t=t)
            self._registrar_cierre(t)
            return
        if tp is not None and (tp - px) * side <= 0:
            self._cerrar(rp, "objetivo del modelo ya alcanzado", t=t)
            self._registrar_cierre(t)
            return
        work = tr.working_exits(acc, mid)
        size = float(rp["size"])
        expected, ops = [], []
        sl_o = tp_o = None
        for o in work:
            kind = es_sl(o, side)
            price = o.get("trigger_price") or o.get("limit_price")
            item = {"order_id": o["id"], "price": float(price), "size": float(o["size"])}
            if str(o.get("type", "")).endswith("limit"):
                item["execution_type"] = "limit"
            expected.append(item)
            if kind is True and sl_o is None:
                sl_o = o
            elif kind is False and tp_o is None:
                tp_o = o
            else:
                ops.append({"kind": "cancel", "order_id": o["id"]})       # salidas sobrantes
        if sl_o:
            ops.append({"kind": "modify", "order_id": sl_o["id"], "price": round(sl, 8), "size": size})
        else:
            ops.append({"kind": "place", "group": "sl", "execution_type": "market", "price": round(sl, 8), "size": size})
        if tp is None and tp_o:
            ops.append({"kind": "cancel", "order_id": tp_o["id"]})
        elif tp is not None and tp_o:
            ops.append({"kind": "modify", "order_id": tp_o["id"], "price": round(tp, 8), "size": size})
        elif tp is not None:
            op = {"kind": "place", "group": "tp", "execution_type": "market", "price": round(tp, 8), "size": size}
            if not sl_o:
                op["oco_pair_with_operation_index"] = len(ops) - 1
                ops[-1]["oco_pair_with_operation_index"] = len(ops)
            ops.append(op)
        try:
            tr.put_exits(rp["id"], {"expected_position_size": size, "expected_orders": expected, "operations": ops})
        except ExecError as err:
            if err.status == 409 and retry:          # alguien cambió algo entre medias: se relee y se repite
                return self._sync_exits(tr.position(acc, mid) or rp, sl, tp, t, retry=False)
            raise
        old = (p.get("sl"), p.get("tp"))
        p["sl"], p["tp"] = sl, tp
        self.last_action = {"t": int(time.time() * 1000), "msg": "SL {} · TP {}".format(self._fmt(sl), self._fmt(tp))}
        self._log("Salidas actualizadas: SL {} → {} · TP {} → {}.".format(
            self._fmt(old[0]), self._fmt(sl), self._fmt(old[1]), self._fmt(tp)), t=t, kind="salidas_real",
            sl=sl, tp=tp if tp is not None else "")

    def _cerrar(self, rp, motivo, fraction=None, t=None):
        tr, p = self.tr, self.pos
        size = float(rp["size"])
        body = {"type": "market", "expected_price": self._precio(rp["market_id"])}
        if fraction:
            m = self._market(rp["market_id"])
            part = round_step(size * fraction, float(m["size_step"]), m.get("size_decimals"))
            if part < float(m.get("min_size") or 0) or part >= size:
                self._log("Venta parcial del {:.0f} % demasiado pequeña para el mercado: no se hace.".format(
                    fraction * 100), t=t)
                return
            body["size"] = part
        if self.e.P["ex_slippage_bps"]:
            body["slippage_tolerance_bps"] = self.e.P["ex_slippage_bps"]
        cid = "{}-x{}".format(p["client_id"], "p" if fraction else "c")[:128]
        body["client_order_id"] = cid
        order = tr.wait_order(tr.close(rp["id"], body, cid))
        if order.get("status") != "filled":
            raise ExecError("orden", order.get("status"), order.get("reject_reason") or "cierre sin ejecutar")
        if not fraction:
            p["closing"] = motivo
        txt = "{} a mercado: {}{}.".format("Venta parcial" if fraction else "Cierre", motivo,
                                                     " ({} a {})".format(body.get("size"), self._fmt(order.get("fill_price")))
                                                     if fraction else "")
        self.last_action = {"t": int(time.time() * 1000), "msg": txt}
        self._log(txt, "warn", t, kind="cierre_enviado")

    def _registrar_cierre(self, t):
        """La posición real ya no está: se busca su cierre y se anota el resultado."""
        tr, p, e = self.tr, self.pos, self.e
        acc = self.acc
        closed = tr.closed_position(acc, p["id"]) if p.get("id") else None
        if closed is None:
            p["missing"] = p.get("missing", 0) + 1
            if p["missing"] < 3:
                self._log("La posición real ya no está abierta; su cierre aún no aparece en el historial.", "warn", t)
                return
            closed = {}
        pnl = float(closed.get("realized_pnl") or 0)
        reason = closed.get("close_reason")
        motivo = p.get("closing") or {"stop_loss": "stop en la cuenta", "take_profit": "objetivo en la cuenta",
                                      "liquidation": "liquidación", "system": "cierre del sistema (fin de semana)"
                                      }.get(reason, reason or "desconocido")
        r = pnl / p["risk_usd"] if p.get("risk_usd") else 0.0
        S = "LONG" if p["side"] == 1 else "SHORT"
        exit_px = closed.get("exit_price")
        notas = []
        if e.st and e.st.pos and not p.get("closing"):
            notas.append("el modelo sigue con la posición abierta")
        if p.get("partial"):
            notas.append("parcial {:.0f} %".format(p["partial"] * 100))
        import csv as _csv
        path = e.p("ejecuciones.csv")
        new = not os.path.exists(path)
        fmt = lambda ms: time.strftime("%Y-%m-%d %H:%M", time.gmtime(ms / 1000)) if ms else ""
        with open(path, "a", newline="", encoding="utf-8") as f:
            w = _csv.writer(f)
            if new:
                w.writerow(EXEC_COLS)
            w.writerow([self.n, e.name, e.P["symbol"], (e.market or {}).get("market_id", ""), S, fmt(p["t_signal"]),
                        fmt(p["t_open"]), fmt(closed.get("closed_at") or int(time.time() * 1000)), p.get("order_id"),
                        p.get("id"), p.get("size"), p.get("leverage"), p.get("entry_model") or "", p["entry"], p["sl0"],
                        p["tp0"] if p.get("tp0") is not None else "", exit_px if exit_px is not None else "",
                        round(p.get("risk_usd") or 0, 2), round(pnl, 2), round(r, 3), closed.get("fees", ""),
                        closed.get("funding", ""), motivo, "; ".join(notas), self.cname, self.cid])
        self.last_action = {"t": int(time.time() * 1000), "msg": "{} cerrado: {:+.2f} $ ({:+.2f} R)".format(S, pnl, r)}
        self._log("{} cerrado ({}): {:+.2f} $ ({:+.2f} R real){}.".format(
            S, motivo, pnl, r, " · " + "; ".join(notas) if notas else ""), "warn", t, kind="cierre_real",
            side=S, price=exit_px if exit_px is not None else "", r=round(r, 3))
        e.notify("ejecucion", "{} <b>{} cerrado en «{}»</b> · {}\n{}\nResultado {:+.2f} $ ({:+.2f} R){}".format(
            "✅" if pnl > 0 else "❌", S, self._esc(self.cname), self._esc(e.name), self._esc(motivo), pnl, r,
            "\n" + self._esc("; ".join(notas)) if notas else ""))
        self.pos = None
        try:
            self.hub.cuentas.resultado_bot(self.cid, pnl)
        except Exception as err:
            e.log("Cuenta «{}»: {}".format(self.cname, err), "error")
        rg = getattr(self.hub, "riesgo", None)
        if rg:
            try:
                rg.real_cerrada(e)
            except Exception as err:
                e.log("Riesgo global: {}".format(err), "error")

    # ---------- arranque y cierre ----------
    def al_arrancar(self):
        """Tras calentar el bot: comprueba que la posición guardada sigue en la cuenta."""
        if not (self.configurado() or self.pos) or not self.listo() or not self.e.market:
            return
        try:
            rp = self.tr.position(self.acc, self.e.market["market_id"])
            if self.pos and rp is None:
                self._registrar_cierre(self.e.last_t)
            elif self.pos and rp is not None:
                self._log("Posición real recuperada tras el reinicio ({} {}).".format(rp.get("side"), rp.get("size")))
            elif rp is not None:
                self._log("Hay una posición en {} en la cuenta que no abrió este bot: no se gestiona y no se abrirán "
                          "otras en ese mercado mientras siga.".format(self.e.market["market_id"]), "warn")
        except Exception as err:
            self._fail("Comprobación de la cuenta al arrancar", err)

    def cerrar_todo(self, motivo):
        if not self.pos or not self.listo():
            return
        try:
            rp = self.tr.position(self.acc, self.e.market["market_id"])
            if rp is not None:
                self._cerrar(rp, motivo)
            self._registrar_cierre(self.e.last_t)
        except Exception as err:
            self._fail("Cierre de la posición real", err)


class Ejecuciones:
    """Las cuentas en las que opera un bot: un Ejecutor por cuenta, con la misma interfaz que tenía el Ejecutor
    único hasta la 6.2 (pos, activo(), configurado(), on_signal, after_candle, al_arrancar, cerrar_todo...)."""

    def __init__(self, engine, saved=None):
        self.e = engine
        self.ex = {}                         # id de la cuenta -> Ejecutor
        s = saved or {}
        g = getattr(engine.hub, "cuentas", None)
        if s and "cuentas" not in s:         # estado de la 6.2: un solo ejecutor, en la cuenta por defecto
            cid = g.por_defecto() if g else None
            if cid and (s.get("pos") or s.get("n")):
                self.ex[cid] = Ejecutor(engine, cid, s, self)
        else:
            for cid, d in (s.get("cuentas") or {}).items():
                self.ex[cid] = Ejecutor(engine, cid, d, self)
        cid0 = (g.migrada or g.por_defecto()) if g else None
        if g:
            try:
                if migrar_csv(engine.p("ejecuciones.csv"), g.nombre(cid0) if cid0 else "", cid0 or ""):
                    engine.log("ejecuciones.csv: añadida la columna de la cuenta («{}»).".format(
                        g.nombre(cid0) if cid0 else "sin cuenta"))
            except OSError as err:
                engine.log("No se pudo actualizar ejecuciones.csv: {}".format(err), "error")
        self.sincronizar()

    # ---------- qué cuentas ----------
    def deseadas(self):
        g = getattr(self.e.hub, "cuentas", None)
        return g.para_bot(self.e.P) if g else []

    def sincronizar(self):
        """Crea los ejecutores de las cuentas nuevas y quita los de las cuentas retiradas sin posición."""
        want = self.deseadas()
        for cid in want:
            if cid not in self.ex:
                self.ex[cid] = Ejecutor(self.e, cid, None, self)
        for cid in list(self.ex):
            x = self.ex[cid]
            if cid not in want and not x.pos:
                del self.ex[cid]
        return want

    def lista(self):
        return [self.ex[c] for c in sorted(self.ex, key=lambda k: (k not in self.deseadas(), k))]

    # ---------- interfaz común ----------
    @property
    def pos(self):
        """Alguna posición real abierta (en cualquier cuenta)."""
        return next((x.pos for x in self.ex.values() if x.pos), None)

    def pos_en(self, cid):
        x = self.ex.get(cid)
        return x.pos if x else None

    def cuentas_con_posicion(self):
        return [c for c, x in self.ex.items() if x.pos]

    def configurado(self):
        return self.e.P.get("ex_modo") == "sandbox"

    def activo(self):
        return any(x.activo() for x in self.ex.values())

    def dump(self):
        return {"cuentas": {c: x.dump() for c, x in self.ex.items()}}

    def public(self):
        cs = [x.public() for x in self.lista()]
        act = [c for c in cs if c["activo"]]
        why = None
        if self.configurado() and not act:
            g = getattr(self.e.hub, "cuentas", None)
            why = (g.problema() if g else None) or next((c["motivo_inactivo"] for c in cs if c["motivo_inactivo"]),
                                                       None) or "el bot no tiene ninguna cuenta elegida"
        return {"configurado": self.configurado(), "activo": bool(act), "motivo_inactivo": why, "cuentas": cs,
                "posicion": self.pos, "operaciones": sum(c["operaciones"] for c in cs)}

    def marcar_cierre(self, cid, motivo):
        x = self.ex.get(cid)
        if x and x.pos and not x.pos.get("closing"):
            x.pos["closing"] = motivo

    def _cada(self, fn):
        for x in list(self.ex.values()):
            try:
                fn(x)
            except Exception as err:         # una cuenta con problemas no frena a las demás
                x._fail("Cuenta «{}»".format(x.cname), "{}: {}".format(type(err).__name__, err))

    def on_signal(self, t, side, cl, sl, tp_fixed):
        self._cada(lambda x: x.on_signal(t, side, cl, sl, tp_fixed))

    def after_candle(self, evs, t):
        self._cada(lambda x: x.after_candle(evs, t))
        self.sincronizar()

    def al_arrancar(self):
        self._cada(lambda x: x.al_arrancar())

    def cerrar_todo(self, motivo):
        self._cada(lambda x: x.cerrar_todo(motivo))
