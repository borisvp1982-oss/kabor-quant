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
import time
import urllib.error
import urllib.parse
import urllib.request
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


class MFPTrading:
    """Cliente REST de la cuenta sandbox con escritura (órdenes, cierres y stops)."""

    def __init__(self, host, key, timeout=20, user_agent="kcbot-pi"):
        if not host_permitido(host):
            raise ValueError("Solo se puede operar en el sandbox ({}), no en {}".format(SANDBOX_HOST, host))
        if not str(key).startswith("fp_test_"):
            raise ValueError("Para operar hace falta una clave de sandbox (fp_test_)")
        self.host, self.key, self.timeout, self.ua = host.rstrip("/"), key, timeout, user_agent

    def _call(self, method, path, body=None, idem=None, params=None):
        url = self.host + path
        if params:
            url += "?" + urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
        data = json.dumps(body).encode() if body is not None else None
        # Solo se reintenta lo que no puede duplicar nada: lecturas y escrituras con clave de idempotencia.
        tries = 3 if method == "GET" or idem else 1
        for i in range(tries):
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
                if e.code in (429, 500, 502, 503, 504) and i < tries - 1:
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

    def policy(self, acc):
        return self.data(self.get("/v1/accounts/{}/trading-policy".format(acc)))

    def market(self, market_id):
        return self.data(self.get("/v1/markets/" + urllib.parse.quote(market_id, safe="")))

    def mid(self, market_id):
        q = self.data(self.get("/v1/markets/{}/quote".format(urllib.parse.quote(market_id, safe=""))))
        return float(q["mid"])

    def position(self, acc, market_id):
        pos = self.data(self.get("/v1/positions", account_id=acc)) or []
        return next((p for p in pos if p.get("market_id") == market_id), None)

    def closed_position(self, acc, position_id):
        res = self.data(self.get("/v1/positions", account_id=acc, status="closed", limit=50)) or []
        return next((p for p in res if p.get("id") == position_id), None)

    def working_exits(self, acc, market_id):
        res = self.data(self.get("/v1/orders", account_id=acc, status="working")) or []
        return [o for o in res if o.get("market_id") == market_id and o.get("reduce_only")
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
        res = self.data(self.get("/v1/orders", account_id=acc, status="working")) or []
        return [o for o in res if o.get("status") not in ("filled", "cancelled", "canceled", "rejected", "expired")]

    def wait_order(self, order, max_s=20):
        """Una orden aceptada empieza en pending: se espera a filled / rejected."""
        t0, i = time.time(), 0
        while order.get("status") == "pending" and time.time() - t0 < max_s:
            time.sleep(min(0.4 * 2 ** i, 4))
            i += 1
            order = self.order(order["id"])
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
        mid = tr.mid(mk["market_id"])
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
        px = tr.mid(mid)
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
        body = {"type": "market", "expected_price": tr.mid(rp["market_id"])}
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
