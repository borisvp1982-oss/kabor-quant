# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Borislav Vladimirov Petrakiev
"""KaBor Quant 7.0 — optimizador de parámetros con walk-forward.

Cada combinación de la rejilla se prueba UNA vez sobre todo el periodo (los indicadores solo miran hacia atrás,
así que las operaciones de un tramo son las mismas que saldrían probando ese tramo suelto). Con esas operaciones:

* Mapa de la rejilla: la métrica de cada combinación en todo el periodo y la misma métrica suavizada con sus
  vecinas. El «pico» es la mejor casilla suelta; la «zona robusta» es la mejor casilla de media con sus vecinas.
* Walk-forward anclado: el periodo se parte en tramos+1 trozos iguales. En cada tramo se elige la mejor
  combinación con todo lo anterior (dentro de muestra) y se apunta lo que hace en el tramo siguiente, que
  no ha visto (fuera de muestra). La suma de los tramos fuera de muestra es lo que habrías ganado de verdad
  optimizando así.
"""
import math
import threading
import time

import nucleo as K

MAX_COMBOS = 225
MAX_VALORES = 15
EXCLUIR_GRUPOS = ("Mercado", "Ejecución en sandbox", "Guardián (detiene el bot)")
EXCLUIR_TIPOS = ("symbol", "strategy", "cuentas", "text")
METRICAS = {
    "total": "R total",
    "r_dd": "R total / drawdown",
    "pf": "Profit factor",
    "sqn": "SQN",
}


# ---------- métricas sobre una lista de R ----------
def resumen(rs):
    n = len(rs)
    if not n:
        return {"n": 0, "total": 0.0, "pf": None, "dd": 0.0, "sqn": None, "wr": 0.0}
    tot = sum(rs)
    cum = peak = dd = 0.0
    for r in rs:
        cum += r
        peak = max(peak, cum)
        dd = max(dd, peak - cum)
    g = sum(r for r in rs if r > 0)
    p = -sum(r for r in rs if r <= 0)
    avg = tot / n
    std = math.sqrt(sum((r - avg) ** 2 for r in rs) / (n - 1)) if n > 1 else 0.0
    return {"n": n, "total": round(tot, 3), "pf": round(g / p, 3) if p > 0 else None, "dd": round(dd, 3),
            "sqn": round(math.sqrt(n) * avg / std, 3) if std > 0 else None,
            "wr": round(100.0 * sum(1 for r in rs if r > 0) / n, 1)}


def valor(m, metrica, min_ops):
    """Número a maximizar (None si no hay operaciones suficientes)."""
    if m["n"] < min_ops:
        return None
    if metrica == "total":
        return m["total"]
    if metrica == "r_dd":
        return m["total"] / max(m["dd"], 1.0)
    if metrica == "pf":
        return m["pf"] if m["pf"] is not None else (99.0 if m["total"] > 0 else 0.0)
    if metrica == "sqn":
        return m["sqn"] if m["sqn"] is not None else 0.0
    return m["total"]


# ---------- rejilla ----------
def campos_optimizables(P):
    pl = K.plugin_for(P)
    out = []
    for f in pl.schema:
        if f.get("group") in EXCLUIR_GRUPOS or f.get("type") in EXCLUIR_TIPOS:
            continue
        if f["type"] in ("int", "float", "select", "bool"):
            out.append(f)
    return out


def valores_eje(f, eje):
    t = f["type"]
    if t == "bool":
        return [True, False]
    if t == "select":
        vals = [v for v in (eje.get("valores") or f["options"]) if v in f["options"]]
        return vals[:MAX_VALORES]
    try:
        a, b, st = float(eje["desde"]), float(eje["hasta"]), float(eje["paso"])
    except (KeyError, TypeError, ValueError):
        raise ValueError("«{}»: indica desde, hasta y paso".format(f["label"]))
    if st <= 0:
        raise ValueError("«{}»: el paso tiene que ser mayor que 0".format(f["label"]))
    if b < a:
        a, b = b, a
    n = int(math.floor((b - a) / st + 1e-9)) + 1
    if n > MAX_VALORES:
        raise ValueError("«{}»: salen {} valores; el máximo es {}. Sube el paso.".format(f["label"], n, MAX_VALORES))
    lo, hi = f.get("min"), f.get("max")
    vals = []
    for i in range(n):
        v = a + i * st
        v = int(round(v)) if t == "int" else round(v, 6)
        if (lo is not None and v < lo) or (hi is not None and v > hi):
            continue
        if v not in vals:
            vals.append(v)
    if not vals:
        raise ValueError("«{}»: ningún valor dentro de su rango ({} a {})".format(f["label"], lo, hi))
    return vals


class Optimizador:
    def __init__(self, hub):
        self.hub = hub
        self.lock = threading.Lock()
        self.job = None
        self.stop = threading.Event()

    # ---------- API para el panel ----------
    def estado(self):
        with self.lock:
            j = self.job
            if not j:
                return {"ok": True, "job": None}
            out = {k: v for k, v in j.items() if k not in ("_base",)}
            if j["estado"] == "en_marcha" and j["hecho"]:
                el = time.time() - j["t0"]
                out["eta"] = round(el / j["hecho"] * (j["total"] - j["hecho"]))
            return {"ok": True, "job": out}

    def cancelar(self):
        with self.lock:
            if self.job and self.job["estado"] == "en_marcha":
                self.stop.set()
                return {"ok": True}
        return {"ok": False, "errors": ["No hay ninguna optimización en marcha"]}

    def campos(self, raw):
        P, errs = K.validate(raw)
        if errs:
            return {"ok": False, "errors": errs}
        return {"ok": True, "campos": [f["key"] for f in campos_optimizables(P)]}

    def empezar(self, body):
        if self.hub.req is None:
            return {"ok": False, "errors": ["El programa aún está arrancando"]}
        with self.lock:
            if self.job and self.job["estado"] == "en_marcha":
                return {"ok": False, "errors": ["Ya hay una optimización en marcha; cancélala o espera a que acabe"]}
        P, errs = K.validate(body.get("params"))
        if not errs and not self.hub.markets.get(P["symbol"]):
            errs = ["{} no está en tu lista de instrumentos".format(P["symbol"])]
        if errs:
            return {"ok": False, "errors": errs}
        campos = {f["key"]: f for f in campos_optimizables(P)}
        ejes = []
        for e in (body.get("ejes") or [])[:2]:
            f = campos.get(e.get("key"))
            if not f:
                return {"ok": False, "errors": ["«{}» no se puede optimizar".format(e.get("key"))]}
            if any(x["key"] == f["key"] for x in ejes):
                return {"ok": False, "errors": ["Has elegido dos veces el mismo ajuste"]}
            try:
                vals = valores_eje(f, e)
            except ValueError as ex:
                return {"ok": False, "errors": [str(ex)]}
            ejes.append({"key": f["key"], "label": f["label"], "type": f["type"], "valores": vals})
        if not ejes:
            return {"ok": False, "errors": ["Elige al menos un ajuste que optimizar"]}
        total = 1
        for e in ejes:
            total *= len(e["valores"])
        if total > MAX_COMBOS:
            return {"ok": False, "errors": ["Salen {} combinaciones; el máximo es {}. Reduce valores.".format(
                total, MAX_COMBOS)]}
        if total < 2:
            return {"ok": False, "errors": ["Con una sola combinación no hay nada que optimizar"]}
        metrica = body.get("metrica") if body.get("metrica") in METRICAS else "r_dd"
        tramos = max(2, min(int(body.get("tramos") or 4), 8))
        min_ops = max(1, min(int(body.get("min_ops") or 20), 1000))
        days = int(body.get("days") or 0)
        job = {"estado": "en_marcha", "hecho": 0, "total": total, "t0": time.time(), "fase": "Preparando velas…",
               "ejes": ejes, "metrica": metrica, "metrica_nombre": METRICAS[metrica], "tramos": tramos,
               "min_ops": min_ops, "days": days, "symbol": P["symbol"], "timeframe": P["timeframe"],
               "bot": str(body.get("bot") or ""), "base": P, "resultado": None, "errors": [], "_base": P}
        with self.lock:
            self.job = job
            self.stop.clear()
        threading.Thread(target=self._run, args=(job,), daemon=True, name="optimizador").start()
        return {"ok": True, "total": total}

    # ---------- trabajo en segundo plano ----------
    def _combos(self, ejes):
        if len(ejes) == 1:
            return [((i,), {ejes[0]["key"]: v}) for i, v in enumerate(ejes[0]["valores"])]
        a, b = ejes
        return [((i, j), {a["key"]: va, b["key"]: vb})
                for i, va in enumerate(a["valores"]) for j, vb in enumerate(b["valores"])]

    def _run(self, job):
        hub = self.hub
        try:
            base = job["_base"]
            combos = []
            for idx, over in self._combos(job["ejes"]):
                P, errs = K.validate(dict(base, **over))
                combos.append((idx, over, None if errs else P, errs))
            validos = [c for c in combos if c[2] is not None]
            if not validos:
                raise ValueError("Ninguna combinación es válida: " + "; ".join(combos[0][3][:2]))
            warm = max(K.warmup_bars(c[2]) for c in validos)
            cache_htf = {}
            d = hub.bt_datos(base, job["days"], htf_cache=cache_htf, warm_min=warm)
            if not d.get("ok"):
                raise ValueError("; ".join(d.get("errors") or ["No hay velas"]))
            cs, warm = d["cs"], d["warm"]
            t_from, t_to = cs[warm][0], cs[-1][0]
            cortes = [t_from + (t_to - t_from) * k // (job["tramos"] + 1) for k in range(job["tramos"] + 2)]
            cortes[-1] = t_to + 1
            cost = hub.markets.cost(base["symbol"])
            celdas = {}
            job["fase"] = "Probando combinaciones…"
            job["t0"] = time.time()
            for idx, over, P, errs in combos:
                if self.stop.is_set():
                    job["estado"] = "cancelada"
                    return
                if P is None:
                    celdas[idx] = {"over": over, "error": errs[0] if errs else "no válida"}
                else:
                    htf = hub.bt_htf(P, cs, cache_htf)
                    try:
                        trades, _ = K.backtest(cs, P, hub.hour_fn, htf, warm=warm, instrument_cost=cost)
                    except Exception as e:  # una estrategia que falla con unos valores no para todo
                        celdas[idx] = {"over": over, "error": "{}: {}".format(type(e).__name__, e)}
                        trades = None
                    if trades is not None:
                        ops = [(x["t_open"], x["r"]) for x in trades]
                        celdas[idx] = {"over": over, "ops": ops}
                job["hecho"] += 1
                time.sleep(0.01)   # deja respirar a los bots en marcha
            # los ajustes actuales, como referencia (si no están en la rejilla, una prueba más)
            base_ops = next((c["ops"] for c in celdas.values()
                             if "ops" in c and all(base.get(k) == v for k, v in c["over"].items())), None)
            if base_ops is None:
                try:
                    tr, _ = K.backtest(cs, base, hub.hour_fn, hub.bt_htf(base, cs, cache_htf), warm=warm,
                                       instrument_cost=cost)
                    base_ops = [(x["t_open"], x["r"]) for x in tr]
                except Exception:
                    base_ops = None
            job["fase"] = "Calculando el walk-forward…"
            job["resultado"] = self._analizar(job, celdas, cortes, base_ops)
            job["estado"] = "terminada"
            secs = time.time() - job["t0"]
            job["secs"] = round(secs, 1)
            av = hub.avisos
            if av and av.ready() and secs > 60:
                wf = job["resultado"]["wf"]
                av.push("optimizacion", "🧪 Optimización terminada: {} {} · {} combinaciones · fuera de muestra "
                        "{:+.1f} R ({})".format(job["symbol"], job["timeframe"], job["total"], wf["total"],
                                                 wf["veredicto_txt"]))
        except Exception as e:
            job["estado"] = "error"
            job["errors"] = [str(e) or type(e).__name__]
        finally:
            hub.log("Optimizador: {} ({} de {} combinaciones)".format(job["estado"], job["hecho"], job["total"]))

    def _analizar(self, job, celdas, cortes, base_ops):
        met, min_ops, ejes, T = job["metrica"], job["min_ops"], job["ejes"], job["tramos"]

        def tramo(ops, a, b):
            return [r for t, r in ops if cortes[a] <= t < cortes[b]]

        # mapa de todo el periodo
        mapa = []
        for idx, c in celdas.items():
            if "ops" not in c:
                mapa.append({"idx": list(idx), "over": c["over"], "error": c.get("error")})
                continue
            m = resumen([r for _, r in c["ops"]])
            mapa.append({"idx": list(idx), "over": c["over"], "m": m, "v": valor(m, met, min_ops)})
        por_idx = {tuple(x["idx"]): x for x in mapa}
        # suavizado: media de la casilla y sus vecinas válidas (una casilla sin dato cuenta como la peor)
        vs = [x["v"] for x in mapa if x.get("v") is not None]
        peor = min(vs) if vs else 0.0
        for x in mapa:
            if x.get("v") is None:
                x["s"] = None
                continue
            vec = []
            for d0 in (-1, 0, 1):
                for d1 in ((-1, 0, 1) if len(ejes) == 2 else (0,)):
                    k = (x["idx"][0] + d0,) + ((x["idx"][1] + d1,) if len(ejes) == 2 else ())
                    y = por_idx.get(k)
                    if y is not None:
                        vec.append(y["v"] if y.get("v") is not None else peor)
            x["s"] = sum(vec) / len(vec)
        pico = max((x for x in mapa if x.get("v") is not None), key=lambda x: x["v"], default=None)
        zona = max((x for x in mapa if x.get("s") is not None), key=lambda x: x["s"], default=None)

        # walk-forward anclado
        filas, oos_rs, is_dia, oos_dia = [], [], [], []
        dur = lambda a, b: max(1.0, (cortes[b] - cortes[a]) / 86_400_000)   # noqa: E731
        for k in range(1, T + 1):
            best, best_v = None, None
            for idx, c in celdas.items():
                if "ops" not in c:
                    continue
                v = valor(resumen(tramo(c["ops"], 0, k)), met, max(1, min_ops * k // (T + 1)))
                if v is not None and (best_v is None or v > best_v):
                    best, best_v = idx, v
            if best is None:
                filas.append({"tramo": k, "desde": cortes[k], "hasta": cortes[k + 1] - 1, "over": None,
                              "is": None, "oos": None})
                continue
            ins = resumen(tramo(celdas[best]["ops"], 0, k))
            oos_l = tramo(celdas[best]["ops"], k, k + 1)
            oos = resumen(oos_l)
            oos_rs += oos_l
            is_dia.append(ins["total"] / dur(0, k))
            oos_dia.append(oos["total"] / dur(k, k + 1))
            filas.append({"tramo": k, "desde": cortes[k], "hasta": cortes[k + 1] - 1, "over": celdas[best]["over"],
                          "idx": list(best), "is": ins, "oos": oos})
        wf = resumen(oos_rs)
        isd = sum(is_dia) / len(is_dia) if is_dia else 0.0
        oosd = sum(oos_dia) / len(oos_dia) if oos_dia else 0.0
        wf["eficiencia"] = round(100.0 * oosd / isd, 1) if isd > 0 else None
        wf["dias"] = round((cortes[-1] - cortes[1]) / 86_400_000, 1)
        # lo mismo con los ajustes actuales, como referencia
        base_idx = None
        for idx, c in celdas.items():
            if all(job["_base"].get(k) == v for k, v in c["over"].items()):
                base_idx = idx
        actual_oos = resumen(tramo(base_ops, 1, T + 1)) if base_ops is not None else None
        cambios = len({tuple(f["idx"]) for f in filas if f.get("idx")})
        if wf["n"] < min_ops // 2 or wf["total"] <= 0:
            ver, txt = "mal", "sobreajustada: fuera de muestra no gana"
        elif wf["eficiencia"] is not None and wf["eficiencia"] >= 50 and (wf["pf"] or 0) >= 1.15:
            ver, txt = "bien", "robusta: gana también con datos que no ha visto"
        else:
            ver, txt = "regular", "dudosa: gana fuera de muestra, pero mucho menos que dentro"
        wf.update({"veredicto": ver, "veredicto_txt": txt, "cambios": cambios})
        return {"mapa": mapa, "pico": pico and pico["idx"], "zona": zona and zona["idx"], "wf": wf, "filas": filas,
                "actual": base_idx and list(base_idx), "actual_oos": actual_oos, "cortes": cortes,
                "pico_m": pico and pico["m"], "zona_m": zona and zona["m"]}
