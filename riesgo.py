# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Borislav Vladimirov Petrakiev
"""
Gestor de riesgo global (v5.1): manda sobre todos los bots a la vez.

  * Límites de ganancia y pérdida diaria y semanal del CONJUNTO de bots. Al alcanzar uno, ningún bot abre
    operaciones nuevas (las abiertas siguen con su stop y su objetivo). Acción «pausa»: vuelve solo al empezar
    el día o la semana siguiente; «parar»: hasta que lo reactives desde el panel.
  * Máximo de operaciones abiertas a la vez entre todos los bots (cuenta también las entradas ya aprobadas que
    esperan a la apertura de la vela siguiente).
  * Ámbito «todos»: resultados del modelo de todos los bots (en R, o en $ con el «Riesgo por operación ($)» de
    cada bot). Ámbito «sandbox»: solo los bots que operan en la cuenta sandbox y su resultado REAL
    (ejecuciones.csv: pnl en $ y R real).

Se guarda en data/riesgo_global.json y se configura desde el Resumen del panel. Los límites de cada bot siguen
funcionando igual: el global es una capa más, por encima.

v5.2: los bots archivados ya no cuentan para los límites, y tras alcanzar un límite solo se puede reactivar a mano
cuando ha empezado el día o la semana siguiente (el panel muestra desde cuándo).
"""
import csv
import json
import os
import threading
import time
from datetime import datetime, timezone

FILE = "riesgo_global.json"
DEFAULTS = {"activo": False, "ambito": "todos", "unidad": "R", "dia_perdida": 0.0, "dia_ganancia": 0.0,
            "semana_perdida": 0.0, "semana_ganancia": 0.0, "accion": "pausa", "utc_off": -4.0, "max_abiertas": 0}
OPCIONES = {"ambito": ("todos", "sandbox"), "unidad": ("R", "USD"), "accion": ("pausa", "parar")}
LIMITES = (("semana", "pérdida", "semana_perdida"), ("semana", "ganancia", "semana_ganancia"),
           ("día", "pérdida", "dia_perdida"), ("día", "ganancia", "dia_ganancia"))


def now_ms():
    return int(time.time() * 1000)


def wkey(d):
    return (d + 3) // 7


def validar(raw, base=None):
    """Devuelve (config, errores)."""
    c = dict(base or DEFAULTS)
    errs = []
    raw = raw or {}
    if "activo" in raw:
        c["activo"] = bool(raw["activo"])
    for k, ops in OPCIONES.items():
        if k in raw:
            if raw[k] in ops:
                c[k] = raw[k]
            else:
                errs.append("{}: valor no válido".format(k))
    for k in ("dia_perdida", "dia_ganancia", "semana_perdida", "semana_ganancia"):
        if k in raw:
            try:
                v = float(raw[k] or 0)
                if v < 0 or v > 1e7:
                    raise ValueError
                c[k] = v
            except (TypeError, ValueError):
                errs.append("{}: tiene que ser un número de 0 en adelante".format(k))
    if "utc_off" in raw:
        try:
            v = float(raw["utc_off"])
            if not -12 <= v <= 14:
                raise ValueError
            c["utc_off"] = v
        except (TypeError, ValueError):
            errs.append("utc_off: entre -12 y 14")
    if "max_abiertas" in raw:
        try:
            v = int(float(raw["max_abiertas"] or 0))
            if not 0 <= v <= 50:
                raise ValueError
            c["max_abiertas"] = v
        except (TypeError, ValueError):
            errs.append("max_abiertas: entre 0 y 50")
    return c, errs


class RiesgoGlobal:
    def __init__(self, hub):
        self.hub = hub
        self.lock = threading.RLock()
        s = self._leer()
        self.cfg, _ = validar(s.get("config"))
        self.guard = s.get("guard")          # límite alcanzado (o None)
        self.base = s.get("base", 0)         # tras reactivar a mano, los límites cuentan desde aquí
        self.reales = {}                     # id del bot -> [(cierre_ms, pnl_usd, r_real)] (de ejecuciones.csv)

    # ---------- disco ----------
    def _leer(self):
        try:
            with open(FILE, encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            return {}

    def _guardar(self):
        tmp = FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"config": self.cfg, "guard": self.guard, "base": self.base}, f, indent=1, ensure_ascii=False)
        os.replace(tmp, FILE)

    # ---------- periodos ----------
    def dkey(self, t):
        return (t + int(self.cfg["utc_off"] * 3_600_000)) // 86_400_000

    def inicio_siguiente(self, g):
        """Milisegundos en los que empieza el periodo siguiente al del límite alcanzado."""
        off = int(self.cfg["utc_off"] * 3_600_000)
        dia = g["key"] + 1 if g["scope"] == "día" else 7 * (g["key"] + 1) - 3      # la semana empieza en lunes
        return dia * 86_400_000 - off

    def _periodo_nuevo(self, g, t=None):
        d = self.dkey(t or now_ms())
        return (wkey(d) if g["scope"] == "semana" else d) > g["key"]

    def _hora_local(self, ms):
        tz = getattr(self.hub, "tz", None) or timezone.utc
        return datetime.fromtimestamp(ms / 1000, tz).strftime("%d/%m a las %H:%M")

    def _reales_de(self, b):
        if b.id not in self.reales:
            out = []
            try:
                with open(b.p("ejecuciones.csv"), encoding="utf-8") as f:
                    for row in csv.DictReader(f):
                        try:
                            tc = int(datetime.strptime(row["cierre_utc"], "%Y-%m-%d %H:%M")
                                     .replace(tzinfo=timezone.utc).timestamp() * 1000)
                            out.append((tc, float(row["pnl_usd"] or 0), float(row["R_real"] or 0)))
                        except (KeyError, ValueError):
                            continue
            except OSError:
                pass
            self.reales[b.id] = out
        return self.reales[b.id]

    def _operaciones(self):
        """(cierre_ms, valor en la unidad elegida) de todo lo que cuenta para los límites."""
        c, out = self.cfg, []
        for b in list(self.hub.bots.values()):
            if b.meta.get("archived"):
                continue                      # un bot archivado no opera: su resultado no cuenta
            if c["ambito"] == "sandbox":
                for tc, pnl, r in self._reales_de(b):
                    out.append((tc, pnl if c["unidad"] == "USD" else r))
            else:
                k = b.P.get("risk_usd") or 0
                for x in list(b.trades):
                    out.append((x["t_close"], x["r"] * k if c["unidad"] == "USD" else x["r"]))
        return out

    def valores(self, t=None):
        t = t or now_ms()
        d = self.dkey(t)
        hoy = semana = 0.0
        for tc, v in self._operaciones():
            if tc < self.base:
                continue
            dd = self.dkey(tc)
            if dd == d:
                hoy += v
            if wkey(dd) == wkey(d):
                semana += v
        return {"día": round(hoy, 2), "semana": round(semana, 2), "key_día": d, "key_semana": wkey(d)}

    # ---------- operaciones abiertas ----------
    def _en_ambito(self, b):
        if b.meta.get("archived"):
            return False
        return self.cfg["ambito"] != "sandbox" or b.ex.activo()

    @staticmethod
    def _ocupa(b):
        st = b.st
        if st is not None and (st.pos or (st.pending and st.pending.get("rg"))):
            return True
        return bool(b.ex.pos)

    def abiertas(self, excluir=None):
        return sum(1 for b in list(self.hub.bots.values())
                   if b is not excluir and self._en_ambito(b) and self._ocupa(b))

    # ---------- la consulta de cada bot ----------
    def permitir(self, bot):
        """Antes de aceptar una señal. Devuelve (ok, motivo). Si la acepta, la marca como entrada aprobada para
        que cuente en el máximo de operaciones a la vez."""
        with self.lock:
            st = bot.st
            c = self.cfg
            if c["activo"] and self._en_ambito(bot):
                if self.guard:
                    g = self.guard
                    return False, "límite global: {} {} de {:+.2f} {}".format(
                        g["kind"], "diaria" if g["scope"] == "día" else "semanal", g["value"], g["unit"])
                if c["max_abiertas"]:
                    n = self.abiertas(excluir=bot)
                    if n >= c["max_abiertas"]:
                        return False, "límite global: {} operación{} abierta{} (máximo {})".format(
                            n, "" if n == 1 else "es", "" if n == 1 else "s", c["max_abiertas"])
            if st is not None and st.pending is not None:
                st.pending["rg"] = True
            return True, ""

    # ---------- comprobación de los límites ----------
    def _avisar(self, texto):
        av = self.hub.avisos
        if av and av.wants("limites"):
            av.push("limites", texto)

    def revisar(self, t=None):
        with self.lock:
            c = self.cfg
            if not c["activo"]:
                return
            t = t or now_ms()
            v = self.valores(t)
            g = self.guard
            if g:
                if g.get("action") == "pausa" and v["key_" + g["scope"]] > g["key"]:
                    self.guard = None
                    self._guardar()
                    self.hub.log("Riesgo global: nuevo periodo, los bots vuelven a buscar entradas.")
                    self._avisar("▶️ <b>Riesgo global</b>: nuevo periodo, los bots vuelven a buscar entradas.")
                    v = self.valores(t)
                else:
                    return
            for scope, kind, k in LIMITES:
                lim = c[k]
                if not lim:
                    continue
                val = v[scope]
                if (kind == "pérdida" and val <= -lim) or (kind == "ganancia" and val >= lim):
                    self.guard = {"scope": scope, "kind": kind, "key": v["key_" + scope], "value": val, "limit": lim,
                                  "unit": c["unidad"], "t": t, "action": c["accion"]}
                    self._guardar()
                    hasta = ("hasta el día siguiente" if scope == "día" else "hasta la semana siguiente") \
                        if c["accion"] == "pausa" else "hasta que lo reactives desde el panel"
                    msg = "RIESGO GLOBAL: {} {} de {:+.2f} {} (límite {:g} {}). Ningún bot abre operaciones nuevas {}. " \
                          "Las abiertas siguen con su stop y su objetivo.".format(
                              kind, "diaria" if scope == "día" else "semanal", val, c["unidad"], lim, c["unidad"],
                              hasta)
                    self.hub.log(msg, "warn")
                    self._avisar("🛑 <b>Riesgo global</b>\n" + msg)
                    return

    def real_cerrada(self, bot):
        """La ejecución anotó un cierre real en ejecuciones.csv: se relee y se revisan los límites."""
        with self.lock:
            self.reales.pop(bot.id, None)
        self.revisar()

    # ---------- panel ----------
    def configurar(self, raw):
        with self.lock:
            c, errs = validar(raw, self.cfg)
            if errs:
                return False, errs
            self.cfg = c
            if not c["activo"]:
                self.guard = None
            self._guardar()
            self.hub.log("Riesgo global {}.".format("activado y guardado" if c["activo"] else "desactivado"))
        self.revisar()
        return True, []

    def reactivar(self):
        with self.lock:
            if not self.guard:
                return False, "No hay ningún límite global alcanzado."
            if not self._periodo_nuevo(self.guard):
                return False, "Todavía no: el límite es {} en curso. Podrás reactivar los bots a partir del {}.".format(
                    "del día" if self.guard["scope"] == "día" else "de la semana",
                    self._hora_local(self.inicio_siguiente(self.guard)))
            self.guard = None
            self.base = now_ms()
            self._guardar()
            self.hub.log("Riesgo global reactivado a mano: los límites del periodo cuentan desde ahora.", "warn")
            return True, "ok"

    def public(self):
        with self.lock:
            v = self.valores()
            g = dict(self.guard) if self.guard else None
            if g:
                g["desde"] = self.inicio_siguiente(g)                 # cuándo se puede reactivar
                g["reactivable"] = self._periodo_nuevo(g)
            return {"config": dict(self.cfg), "guard": g, "hoy": v["día"], "semana": v["semana"],
                    "abiertas": self.abiertas(), "base": self.base, "opciones": {k: list(o) for k, o in OPCIONES.items()}}
