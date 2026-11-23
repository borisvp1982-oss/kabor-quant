# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Borislav Vladimirov Petrakiev
"""
avisos.py -- Avisos por Telegram con captura del gráfico (entrada, stop y objetivo).

* Solo usa la API de bots de Telegram (HTTPS a api.telegram.org) con urllib: sin dependencias.
* Los gráficos se dibujan en la Raspberry con Pillow. Si Pillow no está instalado, los avisos van sin imagen.
* Todo se envía desde un hilo propio con cola: un fallo de Telegram nunca frena a los bots.
* Comandos desde Telegram (solo desde tu chat): /estado, /grafico N, /pausar N, /reanudar N, /ayuda.
"""
import io
import json
import os
import queue
import threading
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone

try:
    from PIL import Image, ImageDraw, ImageFont
    HAVE_PIL = True
except Exception:                       # pragma: no cover
    HAVE_PIL = False

API = "https://api.telegram.org/bot{}/{}"
EVENTS = [("senal", "Señal de entrada", "En cuanto la estrategia da la señal (entra en la apertura de la vela "
           "siguiente). Es el aviso más rápido."),
          ("apertura", "Operación abierta", "Precio de entrada real, stop y objetivo."),
          ("cierre", "Operación cerrada", "Motivo y resultado en R (y en $ si indicas el riesgo)."),
          ("gestion", "Gestión de la operación", "Break-even y venta parcial, trailing activado, ampliaciones."),
          ("ejecucion", "Cuentas", "Órdenes reales en cada cuenta: apertura, cierre con su resultado en $, señales "
           "sin orden (cuenta bloqueada, trading policy, daily loss, tamaño), errores de la API y envío de órdenes "
           "activado o desactivado."),
          ("limites", "Límites, guardián y bloqueos", "Día bloqueado por la estrategia, bot detenido o reanudado, "
           "cuenta bloqueada o desbloqueada."),
          ("noticias", "Noticias", "Unos minutos antes de cada noticia del filtro (panel → Noticias): qué sale, a qué "
           "hora y qué bots tienen una operación abierta."),
          ("deriva", "Vigilante de deriva", "Cuando un bot en marcha se aparta de lo que prometía su backtest (resultado, "
           "pérdidas seguidas, drawdown o frecuencia), y cuando vuelve a la normalidad."),
          ("problemas", "Problemas", "Sin datos durante un buen rato, errores de la estrategia, programa reiniciado."),
          ("resumen", "Resumen diario", "Resultado del día de todos los bots, a la hora que elijas.")]
CHART_EVENTS = ("senal", "apertura", "cierre", "gestion")
DEFAULTS = {"activo": False, "token": "", "chat_id": "", "chat_name": "",
            "eventos": {"senal": True, "apertura": True, "cierre": True, "gestion": False, "ejecucion": True,
                        "limites": True, "noticias": True, "deriva": True, "problemas": True,
                        "resumen": True},
            "graficos": {"senal": False, "apertura": True, "cierre": True, "gestion": False},
            "sin_sonido": {"gestion": True, "resumen": True},
            "bots_off": [], "resumen_hora": 22, "comandos": True}

# colores (tema oscuro del panel)
C = {"bg": (16, 21, 27), "panel": (24, 32, 42), "ink": (227, 231, 235), "muted": (147, 160, 173),
     "rule": (42, 53, 65), "gold": (212, 172, 69), "gain": (77, 190, 138), "loss": (239, 106, 92),
     "blue": (91, 155, 239), "purple": (180, 140, 242), "warn": (224, 160, 64)}
FONT_PATHS = ["/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", "/usr/share/fonts/TTF/DejaVuSans.ttf",
              "/usr/share/fonts/dejavu/DejaVuSans.ttf"]
FONT_BOLD = ["/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf",
             "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf"]


def esc(s):
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def decimals(p):
    if not p:
        return 2
    p = abs(p)
    return 5 if p < 1 else 4 if p < 10 else 3 if p < 100 else 2


def fpx(v, d):
    if v is None:
        return "–"
    s = "{:,.{}f}".format(v, d)
    return s.replace(",", "X").replace(".", ",").replace("X", ".")


def fr(v, d=2):
    return ("+" if v > 0 else "") + fpx(v, d)


# ============================ gráfico ============================
_fonts = {}


def font(size, bold=False):
    k = (size, bold)
    if k in _fonts:
        return _fonts[k]
    f = None
    for path in (FONT_BOLD if bold else FONT_PATHS):
        if os.path.exists(path):
            try:
                f = ImageFont.truetype(path, size)
                break
            except OSError:
                pass
    if f is None:
        try:
            f = ImageFont.load_default(size=size)
        except TypeError:
            f = ImageFont.load_default()
    _fonts[k] = f
    return f


def _dashed(d, pts, fill, width, dash, gap):
    """Polilínea discontinua."""
    carry, on = 0.0, True
    for (x0, y0), (x1, y1) in zip(pts, pts[1:]):
        seg = ((x1 - x0) ** 2 + (y1 - y0) ** 2) ** 0.5
        if seg == 0:
            continue
        pos = 0.0
        while pos < seg:
            step = (dash if on else gap) - carry
            end = min(seg, pos + step)
            if on:
                a, b = pos / seg, end / seg
                d.line([(x0 + (x1 - x0) * a, y0 + (y1 - y0) * a), (x0 + (x1 - x0) * b, y0 + (y1 - y0) * b)],
                       fill=fill, width=width)
            if end - pos < step:
                carry += end - pos
                break
            carry, on, pos = 0.0, not on, end


def render_chart(pl, tz=None, W=1280, H=720):
    """pl: {title, subtitle, badge, badge_color, candles:[{t,o,h,l,c,v:{}}], lineas:[meta], decimals,
            pos:{side, entry, sl, tp, t_open}, trade:{side, entry, exit, sl, sl_final, tp, t_open, t_close, r},
            signal:{side, price, sl, tp}, note}. Devuelve PNG (bytes) o None sin Pillow."""
    if not HAVE_PIL:
        return None
    K = 2
    w, h = W * K, H * K
    img = Image.new("RGB", (w, h), C["bg"])
    ov = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    d, do = ImageDraw.Draw(img), ImageDraw.Draw(ov)
    cs = list(pl.get("candles") or [])[-90:]
    dec = pl.get("decimals", 2)
    tz = tz or timezone.utc
    # cabecera
    d.text((28 * K, 18 * K), pl.get("title", ""), font=font(26 * K, True), fill=C["ink"])
    d.text((28 * K, 52 * K), pl.get("subtitle", ""), font=font(15 * K), fill=C["muted"])
    badge = pl.get("badge")
    if badge:
        bf = font(18 * K, True)
        bw = d.textlength(badge, font=bf)
        col = C.get(pl.get("badge_color", "gold"), C["gold"])
        x1 = w - 28 * K
        x0 = x1 - bw - 28 * K
        d.rounded_rectangle([x0, 20 * K, x1, 56 * K], radius=8 * K, fill=col)
        d.text((x0 + 14 * K, 38 * K), badge, font=bf, fill=C["bg"], anchor="lm")
    if len(cs) < 3:
        d.text((w // 2, h // 2), "Aún no hay velas para dibujar", font=font(18 * K), fill=C["muted"], anchor="mm")
        return _png(img.resize((W, H), Image.LANCZOS))
    L, R, T, B = 20 * K, w - 168 * K, 86 * K, h - 52 * K
    pos, trade, sig = pl.get("pos"), pl.get("trade"), pl.get("signal")
    lo, hi = min(c["l"] for c in cs), max(c["h"] for c in cs)
    meta = pl.get("lineas") or []
    for c in cs:
        for m in meta:
            v = (c.get("v") or {}).get(m["key"])
            if v is not None:
                lo, hi = min(lo, v), max(hi, v)
    for obj in (pos, trade, sig):
        if obj:
            for k in ("sl", "tp", "entry", "exit", "price", "sl_final"):
                if obj.get(k) is not None:
                    lo, hi = min(lo, obj[k]), max(hi, obj[k])
    pad = (hi - lo) * 0.06 or 1
    lo, hi = lo - pad, hi + pad
    n = len(cs)
    step = (R - L) / (n + 6)                # hueco a la derecha
    X = lambda i: L + step * (i + 0.5)
    Y = lambda v: T + (hi - v) / (hi - lo) * (B - T)
    t_idx = lambda t: max([i for i, c in enumerate(cs) if c["t"] <= t] or [0])
    # rejilla y eje de precios
    af = font(13 * K)
    for k in range(6):
        v = lo + (hi - lo) * k / 5
        y = Y(v)
        d.line([(L, y), (R, y)], fill=C["rule"], width=1 * K)
        d.text((R + 10 * K, y), fpx(v, dec), font=af, fill=C["muted"], anchor="lm")
    # horas
    every = max(1, n // 6)
    last_day = None
    for i in range(0, n, every):
        dt = datetime.fromtimestamp(cs[i]["t"] / 1000, tz)
        lab = dt.strftime("%H:%M") if dt.date() == last_day else dt.strftime("%d/%m %H:%M")
        last_day = dt.date()
        d.line([(X(i), T), (X(i), B)], fill=C["rule"], width=1)
        tw = d.textlength(lab, font=af)
        d.text((max(L + tw / 2, X(i)), B + 16 * K), lab, font=af, fill=C["muted"], anchor="mm")
    # zonas de riesgo y beneficio
    zone = None
    if trade:
        zone = (t_idx(trade["t_open"]), t_idx(trade["t_close"]), trade["entry"], trade.get("sl"), trade.get("tp"))
    elif pos:
        zone = (t_idx(pos["t_open"]) if pos.get("t_open") else n - 1, n + 5, pos["entry"], pos.get("sl"), pos.get("tp"))
    elif sig:
        zone = (n, n + 5, sig["price"], sig.get("sl"), sig.get("tp"))
    if zone:
        i0, i1, e, sl, tp = zone
        xa, xb = X(i0) - step / 2, min(R, X(i1) + step / 2)
        if tp is not None:
            do.rectangle([xa, min(Y(e), Y(tp)), xb, max(Y(e), Y(tp))], fill=C["gain"] + (46,))
        if sl is not None:
            do.rectangle([xa, min(Y(e), Y(sl)), xb, max(Y(e), Y(sl))], fill=C["loss"] + (46,))
    img.paste(ov, (0, 0), ov)
    d = ImageDraw.Draw(img)
    # líneas de la estrategia
    for m in meta:
        col = C.get(m.get("color", "muted"), C["muted"])
        pts = [(X(i), Y(c["v"][m["key"]])) for i, c in enumerate(cs) if (c.get("v") or {}).get(m["key"]) is not None]
        if len(pts) < 2:
            continue
        st = m.get("estilo", "continua")
        if st == "continua":
            d.line(pts, fill=col, width=2 * K, joint="curve")
        else:
            _dashed(d, pts, col, 2 * K, 9 * K if st == "discontinua" else 3 * K, 6 * K)
    # velas
    bw = max(1 * K, step * 0.62)
    for i, c in enumerate(cs):
        col = C["gain"] if c["c"] >= c["o"] else C["loss"]
        x = X(i)
        d.line([(x, Y(c["h"])), (x, Y(c["l"]))], fill=col, width=max(1, int(1.2 * K)))
        y0, y1 = Y(max(c["o"], c["c"])), Y(min(c["o"], c["c"]))
        d.rectangle([x - bw / 2, y0, x + bw / 2, max(y0 + 1, y1)], fill=col)
    # niveles de la operación
    tags = []
    if zone:
        i0, i1, e, sl, tp = zone
        xa = X(i0) - step / 2
        xb = min(R, X(i1) + step / 2) if trade else R
        for v, col, lab, dash in ((tp, C["gain"], "TP", True), (sl, C["loss"], "SL", True),
                                  (e, C["gold"], "Entrada", False)):
            if v is None:
                continue
            if dash:
                _dashed(d, [(xa, Y(v)), (xb, Y(v))], col, 2 * K, 10 * K, 6 * K)
            else:
                d.line([(xa, Y(v)), (xb, Y(v))], fill=col, width=2 * K)
            tags.append((v, col, lab))
        if trade and trade.get("sl_final") is not None and abs(trade["sl_final"] - trade["sl"]) > 1e-12:
            _dashed(d, [(xa, Y(trade["sl_final"])), (xb, Y(trade["sl_final"]))], C["warn"], 2 * K, 4 * K, 5 * K)
            tags.append((trade["sl_final"], C["warn"], "SL final"))
        side = (trade or pos or sig)["side"]
        # flecha de entrada
        xe = X(i0) if not sig or pos or trade else X(n - 1) + step
        ye = Y(e)
        s = 1 if side == 1 else -1
        tri = [(xe, ye + s * 4 * K), (xe - 9 * K, ye + s * 20 * K), (xe + 9 * K, ye + s * 20 * K)]
        d.polygon(tri, fill=C["gold"])
    if trade:
        xe, ye = X(t_idx(trade["t_close"])), Y(trade["exit"])
        col = C["gain"] if trade["r"] > 0 else C["loss"]
        d.ellipse([xe - 8 * K, ye - 8 * K, xe + 8 * K, ye + 8 * K], fill=col, outline=C["bg"], width=2 * K)
        lab = "{} R".format(fr(trade["r"]))
        lf = font(16 * K, True)
        tw = d.textlength(lab, font=lf)
        bx = min(R - tw - 20 * K, xe + 14 * K)
        by = ye - 34 * K if (trade["exit"] - trade["entry"]) * trade["side"] > 0 else ye + 14 * K
        d.rounded_rectangle([bx, by, bx + tw + 16 * K, by + 26 * K], radius=6 * K, fill=col)
        d.text((bx + 8 * K, by + 13 * K), lab, font=lf, fill=C["bg"], anchor="lm")
    # último precio
    last = cs[-1]["c"]
    tags.append((last, C["ink"], None))
    tags.sort(key=lambda x: -x[0])
    placed = []
    tf = font(13 * K, True)
    for v, col, lab in tags:
        y = Y(v)
        for py in placed:                    # que no se pisen las etiquetas
            if abs(y - py) < 22 * K:
                y = py + 22 * K
        placed.append(y)
        text = (lab + " " if lab else "") + fpx(v, dec)
        tw = d.textlength(text, font=tf)
        x0 = R + 4 * K
        d.rounded_rectangle([x0, y - 11 * K, x0 + max(tw + 12 * K, 110 * K), y + 11 * K], radius=4 * K, fill=col)
        d.text((x0 + 6 * K, y), text, font=tf, fill=C["bg"], anchor="lm")
    d.rectangle([L, T, R, B], outline=C["rule"], width=1 * K)
    d.text((28 * K, h - 18 * K), pl.get("note") or "En papel · sin órdenes reales · datos de MyFundedPerps",
           font=font(12 * K), fill=C["muted"], anchor="lm")
    d.text((w - 28 * K, h - 18 * K), datetime.now(tz).strftime("%d/%m/%Y %H:%M"), font=font(12 * K),
           fill=C["muted"], anchor="rm")
    return _png(img.resize((W, H), Image.LANCZOS))


def _png(img):
    buf = io.BytesIO()
    img.save(buf, "PNG", optimize=False)
    return buf.getvalue()


# ============================ API de Telegram ============================
class TelegramError(Exception):
    def __init__(self, msg, code=0, retry=0):
        super().__init__(msg)
        self.code, self.retry = code, retry


class Telegram:
    def __init__(self, token):
        self.token = token

    def call(self, method, params=None, files=None, timeout=30):
        url = API.format(self.token, method)
        if files:
            boundary = uuid.uuid4().hex
            body = io.BytesIO()
            for k, v in (params or {}).items():
                body.write("--{}\r\nContent-Disposition: form-data; name=\"{}\"\r\n\r\n{}\r\n".format(
                    boundary, k, v).encode("utf-8"))
            for k, (fname, data, ctype) in files.items():
                body.write("--{}\r\nContent-Disposition: form-data; name=\"{}\"; filename=\"{}\"\r\n"
                           "Content-Type: {}\r\n\r\n".format(boundary, k, fname, ctype).encode("utf-8"))
                body.write(data)
                body.write(b"\r\n")
            body.write("--{}--\r\n".format(boundary).encode())
            req = urllib.request.Request(url, data=body.getvalue(),
                                         headers={"Content-Type": "multipart/form-data; boundary=" + boundary})
        else:
            req = urllib.request.Request(url, data=json.dumps(params or {}).encode("utf-8"),
                                         headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                j = json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            try:
                j = json.loads(e.read().decode("utf-8"))
            except Exception:
                raise TelegramError("HTTP {}".format(e.code), e.code)
        except (urllib.error.URLError, OSError, ValueError) as e:
            raise TelegramError("sin conexión con Telegram ({})".format(getattr(e, "reason", e)))
        if not j.get("ok"):
            desc = j.get("description", "error")
            if "Unauthorized" in desc:
                desc = "el token no es válido (revísalo en @BotFather)"
            elif "chat not found" in desc:
                desc = "chat no encontrado: escribe /start a tu bot desde Telegram y vuelve a detectarlo"
            raise TelegramError(desc, j.get("error_code", 0), (j.get("parameters") or {}).get("retry_after", 0))
        return j.get("result")

    def send(self, chat, text, png=None, silent=False):
        if png:
            if len(text) > 1000:
                self.call("sendPhoto", {"chat_id": chat, "disable_notification": json.dumps(silent)},
                          {"photo": ("grafico.png", png, "image/png")}, timeout=60)
                return self.call("sendMessage", {"chat_id": chat, "text": text, "parse_mode": "HTML",
                                                 "disable_notification": True, "disable_web_page_preview": True})
            return self.call("sendPhoto", {"chat_id": chat, "caption": text, "parse_mode": "HTML",
                                           "disable_notification": json.dumps(silent)},
                             {"photo": ("grafico.png", png, "image/png")}, timeout=60)
        return self.call("sendMessage", {"chat_id": chat, "text": text[:4000], "parse_mode": "HTML",
                                         "disable_notification": silent, "disable_web_page_preview": True})


# ============================ gestor de avisos ============================
class Avisos:
    def __init__(self, path, log, tz=None, commands=None):
        self.path, self.log, self.tz = path, log, tz or timezone.utc
        self.commands = commands               # función(texto) -> (respuesta, png o None)
        self.cfg = self._load()
        self.q = queue.Queue(maxsize=200)
        self.lock = threading.Lock()
        self.last_error, self.last_ok, self.sent, self.failed, self.dropped = None, 0, 0, 0, 0
        self.bot_name = None
        self.seen_chats = {}
        self.offset = None
        self.poll_err = None
        self.summary_day = None
        self._stop = False
        self.wake = threading.Event()
        threading.Thread(target=self._worker, daemon=True, name="avisos").start()
        threading.Thread(target=self._poller, daemon=True, name="telegram").start()

    # ---------- configuración ----------
    def _load(self):
        c = json.loads(json.dumps(DEFAULTS))
        try:
            with open(self.path, encoding="utf-8") as f:
                saved = json.load(f)
            for k, v in saved.items():
                if isinstance(v, dict) and isinstance(c.get(k), dict):
                    c[k].update(v)
                else:
                    c[k] = v
        except (OSError, ValueError):
            pass
        return c

    def _save(self):
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.cfg, f, indent=1, ensure_ascii=False)
        os.chmod(tmp, 0o600)
        os.replace(tmp, self.path)

    def public(self):
        c = dict(self.cfg)
        tok = c.get("token") or ""
        c["token"] = (tok[:6] + "…" + tok[-4:]) if len(tok) > 12 else ("•" * len(tok))
        c["has_token"] = bool(tok)
        return {"cfg": c, "events": [{"key": k, "label": l, "help": h, "chart": k in CHART_EVENTS} for k, l, h in EVENTS],
                "pil": HAVE_PIL, "bot_name": self.bot_name, "last_error": self.last_error, "last_ok": self.last_ok,
                "sent": self.sent, "failed": self.failed, "dropped": self.dropped, "queue": self.q.qsize(),
                "poll_err": self.poll_err,
                "chats": sorted(self.seen_chats.values(), key=lambda x: -x["t"])[:8]}

    def update(self, body):
        with self.lock:
            c = self.cfg
            if "token" in body and body["token"] is not None and "…" not in str(body["token"]) \
                    and "•" not in str(body["token"]):
                tok = str(body["token"]).strip()
                if tok and ":" not in tok:
                    raise ValueError("El token de Telegram tiene la forma 123456789:ABC… (te lo da @BotFather)")
                if tok != c.get("token"):
                    c["token"], self.bot_name, self.offset, self.seen_chats = tok, None, None, {}
                    self.wake.set()
            if "chat_id" in body:
                c["chat_id"] = str(body["chat_id"] or "").strip()
                c["chat_name"] = str(body.get("chat_name") or c.get("chat_name") or "")[:60]
            for k in ("eventos", "graficos", "sin_sonido"):
                if isinstance(body.get(k), dict):
                    c[k].update({a: bool(b) for a, b in body[k].items()})
            if isinstance(body.get("bots_off"), list):
                c["bots_off"] = [str(x) for x in body["bots_off"]]
            if "resumen_hora" in body:
                hh = int(body["resumen_hora"])
                if not 0 <= hh <= 23:
                    raise ValueError("Hora del resumen entre 0 y 23")
                c["resumen_hora"] = hh
            for k in ("activo", "comandos"):
                if k in body:
                    c[k] = bool(body[k])
            if c["activo"] and not (c["token"] and c["chat_id"]):
                c["activo"] = False
                self._save()
                raise ValueError("Para activar los avisos hacen falta el token y el chat")
            self._save()

    def check_token(self):
        if not self.cfg.get("token"):
            raise ValueError("Falta el token")
        me = Telegram(self.cfg["token"]).call("getMe", timeout=15)
        self.bot_name = me.get("username")
        return me

    # ---------- envío ----------
    def ready(self):
        return bool(self.cfg.get("activo") and self.cfg.get("token") and self.cfg.get("chat_id"))

    def wants(self, kind, bot_id=None):
        c = self.cfg
        if not self.ready() or not c["eventos"].get(kind):
            return False
        return not (bot_id is not None and str(bot_id) in c.get("bots_off", []))

    def wants_chart(self, kind):
        return HAVE_PIL and bool(self.cfg["graficos"].get(kind))

    def push(self, kind, text, chart=None, force=False):
        """chart: payload para render_chart (se dibuja en el hilo de envío)."""
        if not force and not self.ready():
            return
        try:
            self.q.put_nowait({"kind": kind, "text": text, "chart": chart, "t": time.time(),
                               "silent": bool(self.cfg["sin_sonido"].get(kind))})
        except queue.Full:
            self.dropped += 1

    def send_now(self, text, chart=None):
        """Envío directo (prueba desde el panel). Lanza la excepción si falla."""
        c = self.cfg
        if not (c.get("token") and c.get("chat_id")):
            raise ValueError("Faltan el token o el chat")
        png = render_chart(chart, self.tz) if chart else None
        Telegram(c["token"]).send(c["chat_id"], text, png)
        self.last_ok, self.last_error = int(time.time() * 1000), None
        return bool(png)

    def _worker(self):
        while not self._stop:
            item = self.q.get()
            if time.time() - item["t"] > 1800:          # media hora en cola: ya no sirve
                self.dropped += 1
                continue
            png = None
            if item.get("chart"):
                try:
                    png = render_chart(item["chart"], self.tz)
                except Exception as e:
                    self.log("No se pudo dibujar el gráfico del aviso: {}".format(e), "warn")
            for attempt in range(4):
                try:
                    Telegram(self.cfg["token"]).send(self.cfg["chat_id"], item["text"], png, item["silent"])
                    self.sent += 1
                    self.last_ok, self.last_error = int(time.time() * 1000), None
                    break
                except TelegramError as e:
                    self.last_error = str(e)
                    if e.retry:
                        time.sleep(min(60, e.retry + 1))
                        continue
                    if e.code in (400, 401, 403, 404):
                        self.failed += 1
                        self.log("Aviso de Telegram no enviado: {}".format(e), "warn")
                        break
                    time.sleep(5 * (attempt + 1))
            else:
                self.failed += 1
                self.log("Aviso de Telegram no enviado tras varios intentos: {}".format(self.last_error), "warn")
            time.sleep(1.1)                             # límite de Telegram: ~1 mensaje por segundo y chat

    # ---------- comandos y detección del chat ----------
    def _poller(self):
        while not self._stop:
            tok = self.cfg.get("token")
            if not tok:
                self.wake.wait(5)
                self.wake.clear()
                continue
            try:
                params = {"timeout": 45, "allowed_updates": ["message"]}
                if self.offset:
                    params["offset"] = self.offset
                ups = Telegram(tok).call("getUpdates", params, timeout=60)
                self.poll_err = None
                for u in ups or []:
                    self.offset = u["update_id"] + 1
                    msg = u.get("message") or {}
                    chat = msg.get("chat") or {}
                    if not chat:
                        continue
                    name = chat.get("title") or " ".join(x for x in (chat.get("first_name"), chat.get("last_name")) if x) \
                        or chat.get("username") or str(chat.get("id"))
                    self.seen_chats[str(chat["id"])] = {"id": str(chat["id"]), "name": name, "type": chat.get("type"),
                                                        "t": int(time.time() * 1000), "text": (msg.get("text") or "")[:40]}
                    text = (msg.get("text") or "").strip()
                    if text.startswith("/") and str(chat["id"]) == str(self.cfg.get("chat_id")) \
                            and self.cfg.get("comandos") and self.commands:
                        self._command(text)
            except TelegramError as e:
                self.poll_err = str(e)
                time.sleep(600 if e.code in (401, 404) else 30 if e.code == 409 else 15)
            except Exception as e:
                self.poll_err = str(e)
                time.sleep(15)

    def _command(self, text):
        try:
            reply, chart = self.commands(text)
        except Exception as e:
            reply, chart = "Error con el comando: {}".format(esc(e)), None
        if reply:
            try:
                png = render_chart(chart, self.tz) if chart else None
                Telegram(self.cfg["token"]).send(self.cfg["chat_id"], reply, png)
            except Exception as e:
                self.last_error = str(e)

    # ---------- resumen diario ----------
    def due_summary(self):
        if not self.wants("resumen"):
            return False
        now = datetime.now(self.tz)
        if now.hour == int(self.cfg.get("resumen_hora", 22)) and self.summary_day != now.date():
            self.summary_day = now.date()
            return True
        return False
