# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Borislav Vladimirov Petrakiev
"""
Contexto de cada operación (KaBor Quant 6.8): la base para que los bots aprendan de sus errores.

En cada vela el Motor actualiza unos indicadores de contexto que NO usa ninguna estrategia (no cambian ninguna
decisión). Cuando una señal se convierte en entrada, se hace una «foto» del mercado en la vela de la señal, viaja
con la operación y, al cerrarse, se anota en contexto.csv junto con el resultado (R, motivo, MFE y MAE).

  * Solo se usan la vela de la señal y las anteriores: nada del futuro.
  * Es igual en directo y en backtest, así que un backtest largo ya da cientos de operaciones etiquetadas.
  * Python puro, sin dependencias: corre en la Raspberry.

Analizar (también con ./bot.sh analizar 1, ./bot.sh analizar "Oro 5m" o ./bot.sh analizar ARCHIVO.csv):
    python3 contexto.py analizar BOT|ARCHIVO.csv [...] [--datos data] [--min 20] [--salida informe.txt]
Lista de bots, con el número que usan los comandos (su posición en el menú del panel):
    python3 contexto.py bots [--datos data]
"""
import csv
import json
import math
import os
import sys
import time
from collections import deque

VERSION = "1.0"
ARCHIVO = "contexto.csv"

# Columnas de identificación y resultado (la etiqueta) y columnas de contexto (las variables).
ID_COLS = ["n", "bot", "config", "estrategia", "simbolo", "tf", "lado", "invertida", "senal_utc", "senal_ms", "apertura_ms",
           "R", "gana", "resultado", "mfe_r", "mae_r", "velas"]
CTX_COLS = ["hora", "dia_sem", "sesion", "atr_pct", "atr_rel", "rango_atr", "cuerpo_pct", "cierre_favor",
            "racha_favor", "mom10_atr", "ema50_atr", "ema200_atr", "ema50_pend", "adx", "rsi", "eficiencia",
            "pos_rango50", "stop_atr", "coste_r", "hueco_atr", "velas_desde_ult", "r_ult3", "perd_seguidas",
            "dia_r", "dia_ops", "g_abiertas", "g_dia"]

AYUDA = {
    "hora": "hora local de entrada", "dia_sem": "día de la semana (lunes = 0)",
    "sesion": "sesión (UTC): asia 0-7, londres 7-13, nueva_york 13-21, cierre 21-24",
    "atr_pct": "ATR en % del precio", "atr_rel": "ATR / su media de 100 velas (>1 = más volátil de lo normal)",
    "rango_atr": "rango de la vela de señal en ATR", "cuerpo_pct": "cuerpo / rango de la vela de señal",
    "cierre_favor": "dónde cierra la vela de señal dentro de su rango, a favor (1 = en el extremo a favor)",
    "racha_favor": "velas seguidas del color a favor", "mom10_atr": "avance de 10 velas a favor, en ATR",
    "ema50_atr": "distancia a la EMA 50 a favor, en ATR", "ema200_atr": "distancia a la EMA 200 a favor, en ATR",
    "ema50_pend": "pendiente de la EMA 50 (10 velas) a favor, en ATR", "adx": "ADX 14 (fuerza de tendencia)",
    "rsi": "RSI 14", "eficiencia": "eficiencia de Kaufman 20 (1 = recta, 0 = lateral)",
    "pos_rango50": "posición en el rango de 50 velas, a favor (1 = en el extremo a favor)",
    "stop_atr": "distancia al stop en ATR", "coste_r": "coste de la operación en R",
    "hueco_atr": "apertura de entrada frente al cierre de la señal, a favor, en ATR",
    "velas_desde_ult": "velas desde el cierre de la operación anterior", "r_ult3": "R de las 3 operaciones anteriores",
    "perd_seguidas": "pérdidas seguidas antes de esta", "dia_r": "R del bot en el día", "dia_ops": "operaciones del día",
    "g_abiertas": "operaciones abiertas en los otros bots (solo en directo)",
    "g_dia": "resultado del día del riesgo global (solo en directo)",
}


# ============================ indicadores propios (sin depender de nucleo) ============================
class _EMA:
    def __init__(self, n):
        self.k, self.v = 2 / (n + 1), None

    def update(self, x):
        self.v = x if self.v is None else self.v + self.k * (x - self.v)
        return self.v


class _RMA:
    def __init__(self, n):
        self.n, self.v, self.buf = n, None, []

    def update(self, x):
        if self.v is None:
            self.buf.append(x)
            if len(self.buf) == self.n:
                self.v = sum(self.buf) / self.n
            return self.v
        self.v += (x - self.v) / self.n
        return self.v


class Contexto:
    """Se alimenta con cada vela cerrada; foto() resume el mercado en la vela de una señal."""

    def __init__(self):
        self.e50, self.e200 = _EMA(50), _EMA(200)
        self.e50_hist = deque(maxlen=11)
        self.closes = deque(maxlen=51)
        self.highs, self.lows = deque(maxlen=50), deque(maxlen=50)
        self.atrs = deque(maxlen=100)
        self.rsi_up, self.rsi_dn = _RMA(14), _RMA(14)
        self.pdm, self.mdm, self.trr, self.adx_r = _RMA(14), _RMA(14), _RMA(14), _RMA(14)
        self.adx = self.rsi = None
        self.racha = 0                      # >0 velas verdes seguidas, <0 rojas
        self.prev = None
        self.n = 0
        self.ult_r = deque(maxlen=3)
        self.ult_cierre = None             # nº de vela del último cierre
        self.perd = 0

    def actualizar(self, c, atr):
        t, o, h, l, cl = c
        p = self.prev
        self.n += 1
        self.e50_hist.append(self.e50.update(cl))
        self.e200.update(cl)
        self.closes.append(cl)
        self.highs.append(h)
        self.lows.append(l)
        if atr is not None:
            self.atrs.append(atr)
        self.racha = (max(self.racha, 0) + 1 if cl > o else min(self.racha, 0) - 1 if cl < o else 0)
        if p is not None:
            d = cl - p[4]
            up, dn = self.rsi_up.update(max(d, 0.0)), self.rsi_dn.update(max(-d, 0.0))
            if up is not None:
                self.rsi = 100.0 if dn == 0 else 100 - 100 / (1 + up / dn)
            um, dm = h - p[2], p[3] - l
            pdm = self.pdm.update(um if um > dm and um > 0 else 0.0)
            mdm = self.mdm.update(dm if dm > um and dm > 0 else 0.0)
            tr = self.trr.update(max(h - l, abs(h - p[4]), abs(l - p[4])))
            if tr:
                pdi, mdi = 100 * pdm / tr, 100 * mdm / tr
                dx = 100 * abs(pdi - mdi) / (pdi + mdi) if pdi + mdi else 0.0
                self.adx = self.adx_r.update(dx)
        self.prev = c

    def operacion_cerrada(self, r):
        self.ult_r.append(r)
        self.ult_cierre = self.n
        self.perd = self.perd + 1 if r < 0 else 0

    def foto(self, motor, side, cl, sl):
        """Variables en la vela de la señal, orientadas al lado de la operación cuando tiene sentido."""
        c, atr = self.prev, motor.atr
        if c is None or not atr:
            return {}
        t, o, h, l, _ = c
        s = side
        rng = h - l
        hora, wd = motor.hora(t + motor.tf_ms)
        hu = ((t + motor.tf_ms) // 3_600_000) % 24
        e50, e200 = self.e50.v, self.e200.v
        x = {
            "hora": hora, "dia_sem": wd,
            "sesion": "asia" if hu < 7 else "londres" if hu < 13 else "nueva_york" if hu < 21 else "cierre",
            "atr_pct": 100 * atr / cl if cl else None,
            "atr_rel": atr / (sum(self.atrs) / len(self.atrs)) if len(self.atrs) >= 50 else None,
            "rango_atr": rng / atr,
            "cuerpo_pct": abs(cl - o) / rng if rng else 0.0,
            "cierre_favor": ((cl - l) / rng if s == 1 else (h - cl) / rng) if rng else 0.5,
            "racha_favor": self.racha * s,
            "mom10_atr": (cl - self.closes[-11]) * s / atr if len(self.closes) >= 11 else None,
            "ema50_atr": (cl - e50) * s / atr if self.n >= 100 else None,
            "ema200_atr": (cl - e200) * s / atr if self.n >= 400 else None,
            "ema50_pend": (self.e50_hist[-1] - self.e50_hist[0]) * s / atr
            if self.n >= 100 and len(self.e50_hist) == 11 else None,
            "adx": self.adx, "rsi": self.rsi,
            "stop_atr": abs(cl - sl) / atr,
            "velas_desde_ult": self.n - self.ult_cierre if self.ult_cierre is not None else None,
            "r_ult3": sum(self.ult_r) if len(self.ult_r) == 3 else None,
            "perd_seguidas": self.perd, "dia_r": motor.dr, "dia_ops": motor.dtrades,
            "_t_senal": t,
        }
        if len(self.closes) >= 21:
            cs = list(self.closes)[-21:]
            mov = sum(abs(cs[i] - cs[i - 1]) for i in range(1, 21))
            x["eficiencia"] = abs(cs[-1] - cs[0]) / mov if mov else 0.0
        if len(self.highs) == 50:
            hi, lo = max(self.highs), min(self.lows)
            if hi > lo:
                x["pos_rango50"] = (cl - lo) / (hi - lo) if s == 1 else (hi - cl) / (hi - lo)
        try:                                          # líneas de la estrategia (bandas de Keltner, media…)
            for k, v in (motor.S.lineas() or {}).items():
                if isinstance(v, (int, float)) and math.isfinite(v):
                    x["ln_" + str(k)] = (cl - v) * s / atr
        except Exception:
            pass
        return {k: (round(v, 4) if isinstance(v, float) else v) for k, v in x.items() if v is not None}

    @staticmethod
    def al_abrir(ctx, o, cl, atr, cost_r, side):
        if ctx is None:
            return None
        ctx = dict(ctx)
        ctx["coste_r"] = round(cost_r, 4)
        if atr:
            ctx["hueco_atr"] = round((o - cl) * side / atr, 4)
        return ctx


# ============================ bots (como en el menú del panel) ============================
def lista_bots(datos="."):
    """Bots en el orden del menú del panel. Los activos llevan número (su posición en el menú); los archivados, no."""
    try:
        with open(os.path.join(datos, "bots.json"), encoding="utf-8") as fh:
            meta = json.load(fh)
    except (OSError, ValueError):
        return []
    out, n = [], 0
    for m in meta:
        P = {}
        try:
            with open(os.path.join(datos, "bots", str(m["id"]), "config.json"), encoding="utf-8") as fh:
                P = json.load(fh).get("params") or {}
        except (OSError, ValueError, KeyError):
            pass
        archivado = bool(m.get("archived"))
        if not archivado:
            n += 1
        out.append({"num": None if archivado else n, "id": str(m["id"]), "nombre": m.get("name") or "Bot",
                    "archivado": archivado, "simbolo": P.get("symbol", "?"), "tf": P.get("timeframe", "?"),
                    "estrategia": P.get("estrategia", "?")})
    return out


def _norm(s):
    return " ".join(str(s).lower().split())


def resolver_bot(datos, ref):
    """ref: posición en el menú del panel (1, 2…) o nombre del bot (completo o una parte que solo tenga uno).
    Devuelve el bot o lanza ValueError con un mensaje para la persona."""
    bots = lista_bots(datos)
    if not bots:
        raise ValueError("No encuentro ningún bot (falta {}).".format(os.path.join(datos, "bots.json")))
    r = str(ref).strip()
    if r.isdigit():
        for b in bots:
            if b["num"] == int(r):
                return b
        raise ValueError("No hay un bot activo número {} (en el menú hay {}).\n{}".format(
            r, sum(1 for b in bots if b["num"]), texto_bots(bots)))
    exactos = [b for b in bots if _norm(b["nombre"]) == _norm(r)]
    parciales = [b for b in bots if _norm(r) in _norm(b["nombre"])]
    for grupo in (exactos, parciales):
        activos = [b for b in grupo if not b["archivado"]]
        if len(activos) == 1 or (len(grupo) == 1):
            return activos[0] if len(activos) == 1 else grupo[0]
        if len(grupo) > 1:
            raise ValueError("«{}» coincide con varios bots: {}. Usa el número o el nombre completo.\n{}".format(
                r, ", ".join("«{}»".format(b["nombre"]) for b in grupo), texto_bots(bots)))
    raise ValueError("No hay ningún bot llamado «{}».\n{}".format(r, texto_bots(bots)))


def describir(b):
    return "{}«{}» ({} {} · {}){}".format("bot {} del menú: ".format(b["num"]) if b["num"] else "", b["nombre"],
                                          b["simbolo"], b["tf"], b["estrategia"], " · archivado" if b["archivado"] else "")


def texto_bots(bots):
    L = ["Bots (el número es su posición en el menú del panel):"]
    for b in bots:
        if not b["archivado"]:
            L.append("  {:>2}  {:<24} {} {} · {}".format(b["num"], b["nombre"][:24], b["simbolo"], b["tf"], b["estrategia"]))
    arch = [b for b in bots if b["archivado"]]
    if arch:
        L.append("Archivados (se eligen por su nombre):")
        for b in arch:
            L.append("   -  {:<24} {} {} · {}".format(b["nombre"][:24], b["simbolo"], b["tf"], b["estrategia"]))
    return "\n".join(L)


# ============================ escritura ============================
def _fmt(ms):
    return time.strftime("%Y-%m-%d %H:%M", time.gmtime(ms / 1000)) if ms else ""


def fila(x, bot="", estrategia=""):
    """x: la operación cerrada del Motor (con 'ctx'). Devuelve un dict columna → valor."""
    ctx = x.get("ctx") or {}
    r = x.get("r", 0.0)
    out = {"n": x.get("n", ""), "bot": bot, "config": x.get("cfg", ""), "estrategia": estrategia,
           "simbolo": x.get("symbol", ""), "tf": x.get("tf", ""), "lado": "LONG" if x["side"] == 1 else "SHORT",
           "invertida": x.get("inv") or "no",
           "senal_utc": _fmt(ctx.get("_t_senal")), "senal_ms": ctx.get("_t_senal", ""), "apertura_ms": x["t_open"],
           "R": round(r, 3), "gana": 1 if r > 0 else 0, "resultado": x.get("why", ""),
           "mfe_r": x.get("mfe", ""), "mae_r": x.get("mae", ""), "velas": x.get("bars", "")}
    for k, v in ctx.items():
        if not k.startswith("_"):
            out[k] = v
    return out


def columnas(filas):
    extra = sorted({k for f in filas for k in f if k not in ID_COLS and k not in CTX_COLS})
    return ID_COLS + CTX_COLS + extra


def escribir(path, f):
    """Añade una fila. Si trae columnas que el archivo no tiene (otra estrategia, una línea que aparece), el archivo
    se reescribe con todas las columnas: nunca se aparta ni se pierde nada."""
    if os.path.exists(path):
        with open(path, encoding="utf-8", newline="") as fh:
            head = next(csv.reader(fh), None) or []
        if any(k not in head for k in f):
            with open(path, encoding="utf-8", newline="") as fh:
                filas = list(csv.DictReader(fh))
            cols = columnas(filas + [f])
            tmp = path + ".tmp"
            with open(tmp, "w", newline="", encoding="utf-8") as fh:
                w = csv.writer(fh)
                w.writerow(cols)
                for r in filas + [f]:
                    w.writerow([r.get(c_, "") if r.get(c_) is not None else "" for c_ in cols])
            os.replace(tmp, path)
            return
        cols = head
    else:
        cols = columnas([f])
    new = not os.path.exists(path)
    with open(path, "a", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        if new:
            w.writerow(cols)
        w.writerow([f.get(c_, "") for c_ in cols])


def escribir_todas(path, filas):
    cols = columnas(filas)
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(cols)
        for f in filas:
            w.writerow([f.get(c, "") for c in cols])


# ============================ análisis ============================
def leer(paths):
    rows = []
    for p in paths:
        with open(p, encoding="utf-8") as fh:
            for r in csv.DictReader(fh):
                r["_archivo"] = os.path.basename(p)
                rows.append(r)
    rows.sort(key=lambda r: _num(r.get("apertura_ms")) or 0)
    return rows


def _num(v):
    try:
        x = float(v)
        return x if math.isfinite(x) else None
    except (TypeError, ValueError):
        return None


def _stats(rs):
    n = len(rs)
    if not n:
        return {"n": 0, "acierto": 0.0, "r_medio": 0.0, "r_total": 0.0}
    return {"n": n, "acierto": 100.0 * sum(1 for r in rs if r > 0) / n, "r_medio": sum(rs) / n, "r_total": sum(rs)}


def _es_categoria(rows, col):
    if col in ("sesion", "dia_sem", "lado", "hora"):
        return True
    return any(_num(r.get(col)) is None for r in rows if r.get(col, "") != "")


def _cortes(vals, k):
    """Límites entre tramos por cuantiles (k tramos con el mismo número de operaciones)."""
    vals = sorted(vals)
    n = len(vals)
    cortes = sorted({vals[i * n // k] for i in range(1, k)})
    return cortes


def _tramo(v, cortes):
    i = 0
    while i < len(cortes) and v >= cortes[i]:
        i += 1
    return i


def _agrupar(rows, col, cortes):
    """Devuelve {clave: [filas]}; clave = categoría o índice de tramo."""
    g = {}
    for r in rows:
        v = r.get(col, "")
        if v == "":
            continue
        if cortes is None:
            key = v
        else:
            x = _num(v)
            if x is None:
                continue
            key = _tramo(x, cortes)
        g.setdefault(key, []).append(r)
    return g


def _etiqueta(key, cortes, rows, col):
    if cortes is None:
        return str(key)
    xs = [_num(r[col]) for r in rows]
    return "{:.3g} … {:.3g}".format(min(xs), max(xs))


def _welch(a, b):
    """t de Welch: cuántos «errores típicos» separan la media de a de la de b."""
    if len(a) < 2 or len(b) < 2:
        return 0.0
    ma, mb = sum(a) / len(a), sum(b) / len(b)
    va = sum((x - ma) ** 2 for x in a) / (len(a) - 1)
    vb = sum((x - mb) ** 2 for x in b) / (len(b) - 1)
    se = math.sqrt(va / len(a) + vb / len(b))
    return (ma - mb) / se if se else 0.0


def _R(rows):
    return [_num(r["R"]) or 0.0 for r in rows]


def analizar(rows, minimo=20, k=5):
    """Para cada variable se buscan el mejor y el peor tramo SOLO con la primera mitad del historial (en el tiempo)
    y se comprueban en la segunda mitad, que no se ha mirado: ¿ese tramo sigue siendo mejor (o peor) que el resto?
    t ≥ 3 en la segunda mitad = se confirma (con ~30 variables, t ≥ 2 sale por azar en una o dos). Así una casualidad de la primera mitad no pasa por patrón."""
    base = _stats(_R(rows))
    mitad = len(rows) // 2
    A, B = rows[:mitad], rows[mitad:]
    cols = [c for c in (rows[0].keys() if rows else []) if c not in ID_COLS and not c.startswith("_")]
    res = []
    for col in cols:
        cat = _es_categoria(rows, col)
        vals_a = [_num(r[col]) for r in A if r.get(col, "") != "" and _num(r[col]) is not None]
        if not cat and len(set(vals_a)) < 2:
            continue
        cortes = None if cat else _cortes(vals_a, k)
        ga, gb, gt = _agrupar(A, col, cortes), _agrupar(B, col, cortes), _agrupar(rows, col, cortes)
        cand = {key: _stats(_R(g)) for key, g in ga.items() if len(g) >= max(2, minimo // 2)}
        if len(cand) < 2:
            continue
        mejor = max(cand, key=lambda x: cand[x]["r_medio"])
        peor = min(cand, key=lambda x: cand[x]["r_medio"])
        oos = {}
        for nombre, key, signo in (("mejor", mejor, 1), ("peor", peor, -1)):
            dentro = _R(gb.get(key, []))
            fuera = [x for kk, g in gb.items() if kk != key for x in _R(g)]
            if len(dentro) < max(2, minimo // 2) or len(fuera) < 2:
                oos[nombre] = None
                continue
            t = _welch(dentro, fuera) * signo
            oos[nombre] = {"n": len(dentro), "r_medio": sum(dentro) / len(dentro),
                           "resto": sum(fuera) / len(fuera), "t": t}
        ts = [o["t"] for o in oos.values() if o]
        if not ts:
            veredicto, fuerza = "sin datos", 0.0
        else:
            fuerza = max(ts)
            veredicto = ("CONFIRMADO" if fuerza >= 3 else "probable" if fuerza >= 2 else "apunta" if fuerza >= 1
                         else "no se confirma")
        orden = sorted(gt) if cortes is not None else sorted(gt, key=str)
        tramos = [(_etiqueta(key, cortes, gt[key], col), _stats(_R(gt[key])),
                   "mejor" if key == mejor else "peor" if key == peor else "") for key in orden]
        res.append({"col": col, "tramos": tramos, "oos": oos, "veredicto": veredicto, "fuerza": fuerza,
                    "etq_mejor": _etiqueta(mejor, cortes, gt.get(mejor, ga[mejor]), col),
                    "etq_peor": _etiqueta(peor, cortes, gt.get(peor, ga[peor]), col)})
    res.sort(key=lambda x: x["fuerza"], reverse=True)
    return base, res


def deslizamiento(rows, carpeta):
    """Une con ejecuciones.csv de la misma carpeta (por hora de la señal y lado): R real frente a R del modelo."""
    path = os.path.join(carpeta, "ejecuciones.csv")
    if not os.path.exists(path):
        return None
    modelo = {(r.get("senal_utc"), r.get("lado")): _num(r.get("R")) for r in rows}
    pares = []
    with open(path, encoding="utf-8") as fh:
        for e in csv.DictReader(fh):
            m = modelo.get((e.get("senal_utc"), e.get("lado")))
            real = _num(e.get("R_real"))
            if m is not None and real is not None:
                pares.append((m, real, e.get("cuenta", "")))
    return pares


def informe(paths, minimo=20):
    rows = leer(paths)
    L = ["KaBor Quant · análisis de contexto v{}".format(VERSION), "Archivos: " + ", ".join(paths), ""]
    if not rows:
        return "\n".join(L + ["No hay operaciones todavía."])
    base, res = analizar(rows, minimo)
    L.append("Operaciones {} · acierto {:.1f} % · R medio {:+.3f} · R total {:+.1f}".format(
        base["n"], base["acierto"], base["r_medio"], base["r_total"]))
    if base["n"] < 300:
        L.append("AVISO: con {} operaciones las diferencias son poco fiables. Úsalo para mirar, no para cambiar "
                 "reglas todavía (lo razonable empieza hacia 300-500).".format(base["n"]))
    L += ["",
          "Cómo leerlo: el mejor y el peor tramo de cada variable se eligen con la PRIMERA mitad del historial y se",
          "comprueban en la SEGUNDA, que no se ha usado para elegirlos. En la segunda mitad: «CONFIRMADO» = ese tramo",
          "sigue separado del resto con t ≥ 3; «probable» = t ≥ 2 (con {} variables, una o dos salen así por puro".format(
              len(res)),
          "azar); «apunta» = misma dirección, sin fuerza. Antes de convertir algo en filtro, pruébalo con un",
          "backtest aparte y en otro periodo.", ""]
    for ver in ("CONFIRMADO", "probable"):
        sel = [x["col"] for x in res if x["veredicto"] == ver]
        L.append("{}: {}".format("Confirmadas fuera de muestra" if ver == "CONFIRMADO" else "Probables",
                                 ", ".join(sel) if sel else "ninguna"))
    L.append("")
    for x in res:
        ayuda = AYUDA.get(x["col"], "línea de la estrategia, a favor, en ATR" if x["col"].startswith("ln_") else "")
        L.append("■ {} ({}) · {}".format(x["col"], ayuda, x["veredicto"]))
        for lab, s, marca in x["tramos"]:
            L.append("    {:<24} n {:>4} · acierto {:>5.1f} % · R medio {:+.3f}{}".format(
                str(lab)[:24], s["n"], s["acierto"], s["r_medio"], " ← " + marca if marca else ""))
        for nombre in ("mejor", "peor"):
            o = x["oos"].get(nombre)
            if o:
                L.append("    2ª mitad, tramo {} ({}): R medio {:+.3f} vs resto {:+.3f} · t {:+.1f} · n {}".format(
                    nombre, x["etq_" + nombre], o["r_medio"], o["resto"], o["t"], o["n"]))
        L.append("")
    carpetas = {os.path.dirname(os.path.abspath(p)) for p in paths}
    for d in sorted(carpetas):
        pares = deslizamiento(rows, d)
        if pares:
            dif = [real - m for m, real, _ in pares]
            L.append("Cuenta sandbox ({}): {} operaciones unidas · R real − R modelo: media {:+.3f}, peor {:+.3f}".format(
                os.path.basename(d), len(pares), sum(dif) / len(dif), min(dif)))
    return "\n".join(L)


def main(argv):
    if not argv or argv[0] not in ("analizar", "bots"):
        print(__doc__)
        return 1
    paths, minimo, salida, datos, refs = [], 20, None, ".", []
    it = iter(argv[1:])
    for a in it:
        if a == "--min":
            minimo = int(next(it))
        elif a == "--salida":
            salida = next(it)
        elif a == "--datos":
            datos = next(it)
        else:
            refs.append(a)
    if argv[0] == "bots":
        bots = lista_bots(datos)
        print(texto_bots(bots) if bots else "No encuentro ningún bot en {}".format(os.path.abspath(datos)))
        return 0
    for r in refs:
        for cand in (r, os.path.join(datos, r)):
            if os.path.isfile(cand):
                paths.append(cand)
                break
        else:
            try:
                b = resolver_bot(datos, r)
            except ValueError as err:
                print(err)
                return 1
            p = os.path.join(datos, "bots", b["id"], ARCHIVO)
            print("Analizando el contexto en directo del " + describir(b))
            if not os.path.exists(p):
                print("Este bot aún no tiene operaciones cerradas con contexto (se anotan desde la 6.8). Para tener "
                      "datos ya: ./bot.sh contexto {}".format(b["num"] or '"{}"'.format(b["nombre"])))
                return 1
            paths.append(p)
    if not paths:
        print("Dime qué analizar: el número del bot en el menú, su nombre o un CSV exportado.")
        return 1
    txt = informe(paths, minimo)
    try:
        print(txt)
    except BrokenPipeError:
        pass
    if salida:
        with open(salida, "w", encoding="utf-8") as fh:
            fh.write(txt + "\n")
        print("\nGuardado en " + salida)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
