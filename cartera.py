# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Borislav Vladimirov Petrakiev
"""KaBor Quant 7.1 — backtest de cartera: varios bots juntos, como si operasen en la misma cuenta.

Cada bot se prueba con sus ajustes guardados (o se usan sus operaciones reales en papel) y se juntan todas las
operaciones en una sola cuenta. Así se ve lo que no se ve bot a bot: el drawdown conjunto, el peor día de la cuenta,
cuántas operaciones llegan a estar abiertas a la vez y si los bots ganan y pierden los mismos días (correlación).
Todo en R: 1 R es el riesgo de una operación; el «peso» de cada bot multiplica sus R (0,5 = la mitad de riesgo).
"""
import math
import threading
import time

import nucleo as K
from optimizador import resumen

MAX_BOTS = 12
DIA = 86_400_000


def _bajar(pts, n=400):
    """Reduce una curva a unos n puntos conservando el primero, el último y los extremos de cada trozo."""
    if len(pts) <= n:
        return pts
    paso = len(pts) / (n / 2)
    out, i = [], 0.0
    while int(i) < len(pts):
        trozo = pts[int(i):int(i + paso)] or pts[int(i):int(i) + 1]
        a, b = min(trozo, key=lambda p: p[1]), max(trozo, key=lambda p: p[1])
        out += sorted({a, b}, key=lambda p: p[0])
        i += paso
    if out[-1] != pts[-1]:
        out.append(pts[-1])
    return out


def _corr(a, b):
    n = len(a)
    if n < 3:
        return None
    ma, mb = sum(a) / n, sum(b) / n
    va = sum((x - ma) ** 2 for x in a)
    vb = sum((y - mb) ** 2 for y in b)
    if va <= 0 or vb <= 0:
        return None
    return round(sum((x - ma) * (y - mb) for x, y in zip(a, b)) / math.sqrt(va * vb), 3)


class Cartera:
    def __init__(self, hub):
        self.hub = hub
        self.lock = threading.Lock()
        self.job = None
        self.stop = threading.Event()

    # ---------- API para el panel ----------
    def estado(self):
        with self.lock:
            return {"ok": True, "job": dict(self.job) if self.job else None}

    def cancelar(self):
        with self.lock:
            if self.job and self.job["estado"] == "en_marcha":
                self.stop.set()
                return {"ok": True}
        return {"ok": False, "errors": ["No hay ningún cálculo de cartera en marcha"]}

    def empezar(self, body):
        hub = self.hub
        if hub.req is None:
            return {"ok": False, "errors": ["El programa aún está arrancando"]}
        with self.lock:
            if self.job and self.job["estado"] == "en_marcha":
                return {"ok": False, "errors": ["Ya hay un cálculo de cartera en marcha"]}
        fuente = "real" if body.get("fuente") == "real" else "historico"
        sel, vistos = [], set()
        for x in (body.get("bots") or [])[:MAX_BOTS]:
            bid = str(x.get("id"))
            b = hub.bots.get(bid)
            if b is None or b.meta.get("archived") or bid in vistos:
                continue
            try:
                peso = float(x.get("peso", 1))
            except (TypeError, ValueError):
                peso = 1.0
            if not 0 < peso <= 10:
                return {"ok": False, "errors": ["El peso de «{}» tiene que estar entre 0 y 10".format(b.name)]}
            vistos.add(bid)
            sel.append({"id": bid, "peso": peso})
        if len(sel) < 2:
            return {"ok": False, "errors": ["Elige al menos dos bots"]}
        days = int(body.get("days") or 0)
        job = {"estado": "en_marcha", "fuente": fuente, "days": days, "bots": sel, "hecho": 0, "total": len(sel),
               "fase": "Preparando…", "t0": time.time(), "resultado": None, "errors": []}
        with self.lock:
            self.job = job
            self.stop.clear()
        threading.Thread(target=self._run, args=(job,), daemon=True, name="cartera").start()
        return {"ok": True}

    # ---------- trabajo en segundo plano ----------
    def _ops_bot(self, b, fuente, days):
        """[(t_open, t_close, r, lado)], desde, hasta, aviso."""
        hub = self.hub
        if fuente == "real":
            with b.lock:
                ops = [(x["t_open"], x["t_close"], x["r"], x.get("side", 0)) for x in b.trades]
            desde = min([o[0] for o in ops] + [int(b.started_at)])
            return ops, desde, int(time.time() * 1000), None
        P = dict(b.P)
        d = hub.bt_datos(P, days)
        if not d.get("ok"):
            raise ValueError("{}: {}".format(b.name, "; ".join(d.get("errors") or ["sin velas"])))
        cs, warm, htf = d["cs"], d["warm"], d["htf"]
        try:
            trades, _ = K.backtest(cs, P, hub.hour_fn, htf, warm=warm, instrument_cost=hub.markets.cost(P["symbol"]))
        except Exception as e:
            raise ValueError("{}: la estrategia falla en el backtest ({}: {})".format(b.name, type(e).__name__, e))
        return [(x["t_open"], x["t_close"], x["r"], x.get("side", 0)) for x in trades], cs[warm][0], cs[-1][0], None

    def _run(self, job):
        hub = self.hub
        try:
            series = []
            for s in job["bots"]:
                if self.stop.is_set():
                    job["estado"] = "cancelada"
                    return
                b = hub.bots.get(s["id"])
                if b is None:
                    raise ValueError("Uno de los bots ya no existe")
                job["fase"] = "Probando «{}»…".format(b.name) if job["fuente"] == "historico" else "Leyendo operaciones…"
                ops, desde, hasta, _ = self._ops_bot(b, job["fuente"], job["days"])
                series.append({"id": s["id"], "name": b.name, "symbol": b.P["symbol"], "tf": b.P["timeframe"],
                               "peso": s["peso"], "ops": ops, "desde": desde, "hasta": hasta,
                               "off": int(b.P.get("utc_off", 0) * 3_600_000)})
                job["hecho"] += 1
            job["fase"] = "Juntando operaciones…"
            job["resultado"] = self._analizar(job, series)
            job["estado"] = "terminada"
            job["secs"] = round(time.time() - job["t0"], 1)
        except Exception as e:
            job["estado"] = "error"
            job["errors"] = [str(e) or type(e).__name__]
        finally:
            hub.log("Cartera: {} ({} de {} bots)".format(job["estado"], job["hecho"], job["total"]))

    def _analizar(self, job, series):
        real = job["fuente"] == "real"
        # periodo común: en el histórico, el tramo que tienen todos; con operaciones reales, desde el primero
        desde = min(s["desde"] for s in series) if real else max(s["desde"] for s in series)
        hasta = max(s["hasta"] for s in series) if real else min(s["hasta"] for s in series)
        if hasta <= desde:
            raise ValueError("Los bots no tienen historial en común: sus velas no se solapan")
        off = series[0]["off"]
        dia = lambda t: (t + off) // DIA  # noqa: E731
        d0, d1 = dia(desde), dia(hasta)
        nd = d1 - d0 + 1
        todas = []
        for s in series:
            s["ops"] = sorted([(a, c, r * s["peso"], lado) for a, c, r, lado in s["ops"] if desde <= a <= hasta],
                              key=lambda o: o[1])
            for o in s["ops"]:
                todas.append(o + (s["id"],))
        todas.sort(key=lambda o: o[1])
        rs = [o[2] for o in todas]
        comb = resumen(rs)
        # curvas
        def curva(ops):
            cum, pts = 0.0, [(desde, 0.0)]
            for o in ops:
                cum += o[2]
                pts.append((o[1], round(cum, 3)))
            return pts
        eq = _bajar(curva(todas), 500)
        # días
        diario = {s["id"]: [0.0] * nd for s in series}
        dias_r = [[] for _ in range(nd)]
        for o in todas:
            k = dia(o[1]) - d0
            if 0 <= k < nd:
                diario[o[4]][k] += o[2]
                dias_r[k].append(round(o[2], 4))
        dtot = [sum(diario[s["id"]][k] for s in series) for k in range(nd)]
        con = [v for k, v in enumerate(dtot) if dias_r[k]]
        peor_k = min(range(nd), key=lambda k: dtot[k]) if nd else None
        mejor_k = max(range(nd), key=lambda k: dtot[k]) if nd else None
        # a la vez: barrido de aperturas y cierres
        ev = sorted([(o[0], 1, o[2]) for o in todas] + [(o[1], -1, 0) for o in todas], key=lambda e: (e[0], e[1]))
        abiertas = maxa = 0
        t_prev, t2 = None, 0
        for t, dlt, _ in ev:
            if t_prev is not None and abiertas >= 2:
                t2 += t - t_prev
            abiertas += dlt
            maxa = max(maxa, abiertas)
            t_prev = t
        # riesgo abierto a la vez (suma de pesos)
        evp = sorted([(o[0], 1, s["peso"]) for s in series for o in s["ops"]] +
                     [(o[1], -1, s["peso"]) for s in series for o in s["ops"]], key=lambda e: (e[0], e[1]))
        rab = rmax = 0.0
        for _, dlt, p in evp:
            rab += dlt * p
            rmax = max(rmax, rab)
        # bot a bot
        bots = []
        for s in series:
            m = resumen([o[2] for o in s["ops"]])
            bots.append({"id": s["id"], "name": s["name"], "symbol": s["symbol"], "tf": s["tf"], "peso": s["peso"],
                         "m": m, "eq": _bajar(curva(s["ops"]), 250),
                         "aporte": round(100.0 * m["total"] / comb["total"], 1) if comb["total"] > 0 else None})
        suma_dd = sum(b["m"]["dd"] for b in bots)
        ids = [s["id"] for s in series]
        corr = [[1.0 if i == j else _corr(diario[a], diario[b]) for j, b in enumerate(ids)] for i, a in enumerate(ids)]
        pares = [corr[i][j] for i in range(len(ids)) for j in range(i + 1, len(ids)) if corr[i][j] is not None]
        return {"desde": desde, "hasta": hasta, "dias": nd, "m": comb, "eq": eq, "bots": bots, "corr": corr,
                "corr_media": round(sum(pares) / len(pares), 3) if pares else None,
                "dias_r": dias_r, "dias_con": len(con), "dias_pos": sum(1 for v in con if v > 0),
                "peor_dia": {"r": round(dtot[peor_k], 2), "t": (peor_k + d0) * DIA} if con else None,
                "mejor_dia": {"r": round(dtot[mejor_k], 2), "t": (mejor_k + d0) * DIA} if con else None,
                "suma_dd": round(suma_dd, 2),
                "diversif": round(100.0 * (1 - comb["dd"] / suma_dd), 1) if suma_dd > 0 else None,
                "max_abiertas": maxa, "riesgo_max": round(rmax, 2),
                "pct_varias": round(100.0 * t2 / max(1, hasta - desde), 1)}
