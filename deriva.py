# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Borislav Vladimirov Petrakiev
"""KaBor Quant 7.2 — vigilante de deriva.

Compara lo que hace cada bot en marcha con lo que prometía su backtest con los mismos ajustes. La referencia son las
operaciones del backtest de los últimos días (90 por defecto). Con ellas se fabrican miles de «rachas posibles» del
mismo número de operaciones que lleva el bot en real y se mira dónde cae lo real:

* Resultado: si el R acumulado está por debajo de casi todas las rachas posibles, el bot rinde peor de lo esperado.
* Pérdidas seguidas y drawdown: si nunca o casi nunca salen tan largas en el backtest.
* Frecuencia: si opera mucho más o mucho menos de lo esperado (señales que no llegan, datos que fallan…).

Estados: «pocas» (aún no hay operaciones suficientes), «ok», «vigilar» y «deriva». Al empeorar avisa por Telegram y, si
se activa, pausa las entradas del bot (no cierra la operación abierta).
"""
import json
import math
import os
import random
import threading
import time

import nucleo as K

DIA = 86_400_000
DEFAULTS = {"activo": True, "pausar": False, "dias_ref": 90, "min_ops": 10}
NIVEL = {"pocas": 0, "ok": 0, "vigilar": 1, "deriva": 2, "error": 0}
SIMS = 2000
REF_CADUCA = 7 * DIA
CADA = 10 * 60          # segundos entre revisiones


def _poisson_cdf(k, lam):
    """P(X <= k) para una Poisson de media lam (sumando en logaritmos)."""
    if lam <= 0:
        return 1.0
    s, lp = 0.0, -lam
    for i in range(0, k + 1):
        if i:
            lp += math.log(lam) - math.log(i)
        s += math.exp(lp)
    return min(1.0, s)


def _racha(rs):
    m = c = 0
    for r in rs:
        c = c + 1 if r < 0 else 0
        m = max(m, c)
    return m


def _dd(rs):
    cum = peak = dd = 0.0
    for r in rs:
        cum += r
        peak = max(peak, cum)
        dd = max(dd, peak - cum)
    return dd


class Vigilante:
    def __init__(self, hub, path="vigilante.json"):
        self.hub = hub
        self.path = path
        self.lock = threading.Lock()
        self.cfg = dict(DEFAULTS)
        self.refs = {}       # bot -> {"cfg_id", "t", "rs", "tpd", "dias", "error"}
        self.ultimo = {}     # bot -> último estado avisado (sobrevive a reinicios)
        self.eval = {}       # bot -> última evaluación
        self.pend = set()    # bots con la referencia pedida a mano
        self.wake = threading.Event()
        self._load()

    # ---------- disco ----------
    def _load(self):
        try:
            with open(self.path, encoding="utf-8") as f:
                d = json.load(f)
            self.cfg.update({k: v for k, v in (d.get("cfg") or {}).items() if k in DEFAULTS})
            self.refs = d.get("refs") or {}
            self.ultimo = d.get("ultimo") or {}
        except (OSError, ValueError):
            pass

    def _save(self):
        with self.lock:
            d = {"cfg": self.cfg, "refs": self.refs, "ultimo": self.ultimo}
        tmp = self.path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(d, f, ensure_ascii=False)
            os.replace(tmp, self.path)
        except OSError as e:
            self.hub.log("Vigilante: no se pudo guardar ({})".format(e), "warn")

    # ---------- API para el panel ----------
    def public(self):
        with self.lock:
            return {"ok": True, "cfg": dict(self.cfg), "bots": dict(self.eval), "pendientes": sorted(self.pend)}

    def resumen(self):
        with self.lock:
            return {k: v.get("estado") for k, v in self.eval.items()}

    def configurar(self, c):
        c = c or {}
        try:
            return self._configurar(c)
        except (TypeError, ValueError):
            return {"ok": False, "errors": ["Valores no válidos"]}

    def _configurar(self, c):
        with self.lock:
            if "activo" in c:
                self.cfg["activo"] = bool(c["activo"])
            if "pausar" in c:
                self.cfg["pausar"] = bool(c["pausar"])
            if "dias_ref" in c:
                d = int(c["dias_ref"])
                if d not in (30, 60, 90, 180, 365):
                    return {"ok": False, "errors": ["Días de referencia no válidos"]}
                if d != self.cfg["dias_ref"]:
                    self.refs = {}          # cambia la referencia de todos
                self.cfg["dias_ref"] = d
            if "min_ops" in c:
                m = int(c["min_ops"])
                if not 5 <= m <= 200:
                    return {"ok": False, "errors": ["El mínimo de operaciones va de 5 a 200"]}
                self.cfg["min_ops"] = m
        self._save()
        self.wake.set()
        return {"ok": True}

    def recalcular(self, bid):
        bid = str(bid)
        if bid not in self.hub.bots:
            return {"ok": False, "errors": ["No existe ese bot"]}
        with self.lock:
            self.refs.pop(bid, None)
            self.pend.add(bid)
        self.wake.set()
        return {"ok": True}

    # ---------- bucle ----------
    def start(self):
        threading.Thread(target=self._loop, daemon=True, name="deriva").start()

    def _loop(self):
        time.sleep(90)          # deja que los bots carguen
        while True:
            try:
                self.revisar()
            except Exception as e:
                self.hub.log("Vigilante: {}".format(e), "error")
            self.wake.wait(CADA)
            self.wake.clear()

    def revisar(self):
        hub = self.hub
        vivos = set()
        for b in hub.active_bots():
            vivos.add(b.id)
            ref = self._ref_de(b)
            ev = self.evaluar(b, ref)
            with self.lock:
                self.eval[b.id] = ev
                self.pend.discard(b.id)
            if self.cfg["activo"]:
                self._transicion(b, ev)
        with self.lock:
            for k in list(self.eval):
                if k not in vivos:
                    del self.eval[k]
        self._save()

    def _ref_de(self, b):
        now = int(time.time() * 1000)
        with self.lock:
            r = self.refs.get(b.id)
        if r and r.get("cfg_id") == b.cfg_id and now - r.get("t", 0) < REF_CADUCA and not r.get("error"):
            return r
        if r and r.get("error") and now - r.get("t", 0) < 3600_000 and r.get("cfg_id") == b.cfg_id:
            return r            # tras un error, reintenta cada hora
        try:
            P = dict(b.P)
            d = self.hub.bt_datos(P, self.cfg["dias_ref"], fuente="no")   # siempre MFP: compara con el directo
            if not d.get("ok"):
                raise ValueError("; ".join(d.get("errors") or ["sin velas"]))
            cs, warm = d["cs"], d["warm"]
            trades, _ = K.backtest(cs, P, self.hub.hour_fn, d["htf"], warm=warm,
                                   instrument_cost=self.hub.markets.cost(P["symbol"]))
            dias = max(1.0, (cs[-1][0] - cs[warm][0]) / DIA)
            r = {"cfg_id": b.cfg_id, "t": now, "rs": [round(x["r"], 4) for x in trades], "dias": round(dias, 1),
                 "tpd": round(len(trades) / dias, 4), "error": None}
        except Exception as e:
            r = {"cfg_id": b.cfg_id, "t": now, "rs": [], "dias": 0, "tpd": 0, "error": str(e) or type(e).__name__}
        with self.lock:
            self.refs[b.id] = r
        return r

    # ---------- la comparación ----------
    def evaluar(self, b, ref):
        now = int(time.time() * 1000)
        with b.lock:
            vivas = sorted([x for x in b.trades if x.get("cfg") == b.cfg_id], key=lambda x: x["t_close"])
            desde = max(int(b.cfg_saved or 0), int(b.started_at or 0))
        rs = [x["r"] for x in vivas]
        n, total = len(rs), round(sum(rs), 3)
        base = {"id": b.id, "name": b.name, "cfg_id": b.cfg_id, "t": now, "n": n, "total": total,
                "ref": {"n": len(ref.get("rs") or []), "dias": ref.get("dias"), "t": ref.get("t"),
                        "error": ref.get("error")}, "min_ops": self.cfg["min_ops"], "dias_ref": self.cfg["dias_ref"]}
        if ref.get("error"):
            return dict(base, estado="error", motivos=["No se pudo hacer el backtest de referencia: " + ref["error"]])
        R = ref["rs"]
        if len(R) < 20:
            return dict(base, estado="pocas", motivos=["El backtest de referencia solo tiene {} operaciones: amplía los "
                                                       "días de referencia".format(len(R))])
        nR = len(R)
        exp_avg = sum(R) / nR
        exp_wr = 100.0 * sum(1 for r in R if r > 0) / nR
        dias = max(0.5, (now - desde) / DIA) if desde else None
        esperadas = ref["tpd"] * dias if dias else None
        base.update({"exp": {"avg": round(exp_avg, 3), "wr": round(exp_wr, 1), "tpd": ref["tpd"],
                             "n": round(esperadas, 1) if esperadas is not None else None},
                     "vivo": {"avg": round(total / n, 3) if n else None,
                              "wr": round(100.0 * sum(1 for r in rs if r > 0) / n, 1) if n else None,
                              "racha": _racha(rs), "dd": round(_dd(rs), 2), "dias": round(dias, 1) if dias else None,
                              "path": [round(v, 3) for v in self._cum(rs)]}})
        # rachas posibles: H operaciones sacadas del backtest (con reposición), mismas para todo
        H = max(n, 20)
        rnd = random.Random("{}-{}-{}".format(b.id, b.cfg_id, len(R)))
        puntos = sorted({max(1, round(H * k / 40)) for k in range(1, 41)} | ({n} if n else set()))
        cols = {k: [] for k in puntos}
        le = racha_ge = dd_ge = 0
        rac_v, dd_v = _racha(rs), _dd(rs)
        sims = SIMS if H <= 150 else max(500, 300_000 // H)
        for _ in range(sims):
            cum = peak = dd = 0.0
            c = m = 0
            for i in range(1, H + 1):
                r = R[int(rnd.random() * nR)]
                cum += r
                if cum > peak:
                    peak = cum
                if peak - cum > dd:
                    dd = peak - cum
                c = c + 1 if r < 0 else 0
                if c > m:
                    m = c
                if i in cols:
                    cols[i].append(cum)
                if i == n:
                    if cum <= total + 1e-9:
                        le += 1
                    if m >= rac_v:
                        racha_ge += 1
                    if dd >= dd_v - 1e-9:
                        dd_ge += 1
        q = lambda a, p: a[min(len(a) - 1, int(p * (len(a) - 1) + .5))]  # noqa: E731
        cono = []
        for k in puntos:
            a = sorted(cols[k])
            cono.append([k, round(q(a, .05), 3), round(q(a, .25), 3), round(q(a, .5), 3), round(q(a, .75), 3),
                         round(q(a, .95), 3)])
        base["cono"] = cono
        if n < self.cfg["min_ops"]:
            base.update(estado="pocas", motivos=["Lleva {} operaciones con estos ajustes; el vigilante opina a partir "
                                                 "de {}.".format(n, self.cfg["min_ops"])])
            return base
        p_res, p_rac, p_dd = le / sims, racha_ge / sims, dd_ge / sims
        p_frec = None
        if esperadas is not None and esperadas >= 3:
            lo = _poisson_cdf(n, esperadas)
            hi = 1 - _poisson_cdf(n - 1, esperadas) if n > 0 else 1.0
            p_frec = min(1.0, 2 * min(lo, hi))
        base["p"] = {"resultado": round(p_res, 4), "racha": round(p_rac, 4), "dd": round(p_dd, 4),
                     "frecuencia": None if p_frec is None else round(p_frec, 4)}
        motivos, nivel = [], 0
        pct = lambda p: "{:.0f}".format(100 * p) if p >= .01 else "menos de 1"  # noqa: E731
        if p_res < .10:
            nivel = max(nivel, 2 if p_res < .02 else 1)
            motivos.append("Resultado de {:+.1f} R en {} operaciones: solo {} % de las rachas del backtest con las mismas "
                           "operaciones salen tan mal o peor.".format(total, n, pct(p_res)))
        if p_rac < .01:
            nivel = max(nivel, 2)
            motivos.append("{} pérdidas seguidas: en el backtest pasa en {} % de los casos.".format(rac_v, pct(p_rac)))
        elif p_rac < .05:
            nivel = max(nivel, 1)
            motivos.append("{} pérdidas seguidas: poco habitual en el backtest ({} %).".format(rac_v, pct(p_rac)))
        if p_dd < .01:
            nivel = max(nivel, 2)
            motivos.append("Drawdown de {:.1f} R: en el backtest pasa en {} % de los casos.".format(dd_v, pct(p_dd)))
        elif p_dd < .05:
            nivel = max(nivel, 1)
            motivos.append("Drawdown de {:.1f} R: poco habitual en el backtest ({} %).".format(dd_v, pct(p_dd)))
        if p_frec is not None and p_frec < .01:
            nivel = max(nivel, 1 if p_frec >= .001 else 2)
            motivos.append("Opera {} de lo esperado: {} operaciones en {:.0f} días, cuando el backtest daba unas "
                           "{:.0f}. Mira si llegan bien los datos o si el mercado ha cambiado.".format(
                               "mucho más" if n > esperadas else "mucho menos", n, dias, esperadas))
        if not motivos:
            motivos.append("Se comporta como en el backtest." + (" Incluso va por la parte alta." if p_res > .9 else ""))
        base.update(estado=["ok", "vigilar", "deriva"][nivel], motivos=motivos)
        return base

    @staticmethod
    def _cum(rs):
        out, c = [0.0], 0.0
        for r in rs:
            c += r
            out.append(c)
        return out

    # ---------- avisos y pausa ----------
    def _transicion(self, b, ev):
        est = ev["estado"]
        if est not in ("ok", "vigilar", "deriva"):
            return
        clave = "{}:{}".format(b.id, b.cfg_id)
        antes = self.ultimo.get(clave, "ok")
        with self.lock:
            self.ultimo[clave] = est
        if NIVEL[est] <= NIVEL.get(antes, 0):
            if NIVEL[est] < NIVEL.get(antes, 0) and est == "ok":
                self._avisar(b, "✅ <b>{}</b>: vuelve a comportarse como en el backtest.".format(K_esc(b.name)))
            return
        pausado = False
        if est == "deriva" and self.cfg["pausar"] and not b.paused:
            ok, _ = b.control("pausar")
            pausado = ok
            self.hub.log("Vigilante: «{}» en deriva, entradas pausadas".format(b.name), "warn")
        icono = "🟠" if est == "vigilar" else "🔴"
        txt = "{} <b>{}</b>: {}\n{}".format(icono, K_esc(b.name), "a vigilar" if est == "vigilar" else
                                            "se aparta de su backtest", "\n".join("• " + K_esc(m) for m in ev["motivos"]))
        if pausado:
            txt += "\n⏸ Entradas pausadas. Reanúdalas desde el panel cuando lo revises."
        self._avisar(b, txt)

    def _avisar(self, b, txt):
        av = self.hub.avisos
        if av and av.wants("deriva", b.id):
            av.push("deriva", txt)


def K_esc(s):
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
