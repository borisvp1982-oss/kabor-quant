# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Borislav Vladimirov Petrakiev
"""
externos.py -- Velas de fuera de MyFundedPerps para alargar los backtests.

Los datos externos son de un INSTRUMENTO (no de un bot ni de una estrategia): todos los bots y estrategias que
operan ese instrumento los pueden usar. Tienen que ser del MISMO mercado que opera el bot (mismo proveedor y mismo
tipo: perpetuo de Hyperliquid, futuros de Binance…). Para comprobarlo, al importar un archivo:

  1. se leen las columnas solas (Binance, TradingView, MetaTrader, Dukascopy o cualquier CSV con fecha y OHLC),
  2. se deduce la temporalidad y se generan las mayores (de 1m salen 3m, 5m, 15m, 30m, 1h, 2h y 4h),
  3. se compara con las velas reales de MFP en el tramo en que coinciden: desfase horario, diferencia de precio,
     correlación de los movimientos y velas que faltan,
  4. veredicto: «coincide» (mismo mercado), «aproximado» (mercado parecido: otra fuente, spot frente a perpetuo…),
     «sin verificar» (no hay tramo común) o «no coincide» (no se deja usar).

Cómo se usan (por instrumento, en el panel → Instrumentos → Datos externos):
  no usar     -> solo velas de MFP (como siempre),
  completar   -> velas externas ANTES de la primera vela de MFP y, desde ahí, MFP,
  solo        -> solo las externas (para comparar).
El vigilante de deriva usa siempre MFP: compara el directo con su backtest de referencia.

Archivos: data/externos/<SÍMBOLO>_<TF>.csv (t_ms,o,h,l,c en UTC), data/externos/externos.json (verificación y modo)
y data/externos/entrada/ (deja ahí CSV grandes por SCP y pulsa «Releer»).
"""
import csv
import io
import json
import math
import os
import re
import threading
import time
from datetime import datetime, timezone

TF_MS = {"1m": 60_000, "3m": 180_000, "5m": 300_000, "15m": 900_000, "30m": 1_800_000,
         "1h": 3_600_000, "2h": 7_200_000, "4h": 14_400_000, "1d": 86_400_000, "1w": 604_800_000}
SEMANA, LUNES = 604_800_000, 4 * 86_400_000     # semanas de lunes a domingo (UTC)


def inicio_vela(t, ms):
    return t - (t - LUNES) % SEMANA if ms == SEMANA else t - t % ms
MODOS = ("no", "completar", "solo")
VEREDICTOS = {"coincide": "Coincide", "aproximado": "Aproximado", "sin_verificar": "Sin verificar",
              "no_coincide": "No coincide"}
MAX_FILAS = 1_000_000          # ~2 años de 1m o ~9 de 5m (memoria de la Raspberry)
MAX_SUBIDA = 250 * 1024 * 1024
NOMBRE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._ -]{0,120}\.(csv|txt)$")


# ============================ lectura ============================
def _a_ms(v, fecha_extra=None):
    """Fecha de cualquier formato habitual -> milisegundos UTC (sin zona = UTC)."""
    s = str(v).strip().strip('"')
    if fecha_extra is not None:
        s = "{} {}".format(str(fecha_extra).strip(), s)
    if re.fullmatch(r"-?\d+(\.\d+)?", s):
        x = float(s)
        if x > 1e17:
            return int(x / 1e6)        # nanosegundos
        if x > 1e14:
            return int(x / 1e3)        # microsegundos (Binance desde 2025)
        if x > 1e11:
            return int(x)              # milisegundos
        if x > 1e8:
            return int(x * 1000)       # segundos
        if 19000101 <= x <= 21001231:
            s = "{:.0f}".format(x)     # 20240105
        else:
            raise ValueError("número de fecha no reconocido")
    s = re.sub(r"(\d)T(\d)", r"\1 \2", s)
    s = re.sub(r"Z$", "+00:00", s)
    s = re.sub(r"^(\d{4})(\d{2})(\d{2})(?=\s|$)", r"\1-\2-\3", s)                       # 20240105
    s = re.sub(r"^(\d{4})[./-](\d{1,2})[./-](\d{1,2})", lambda m: "{}-{:0>2}-{:0>2}".format(*m.groups()), s)
    s = re.sub(r"^(\d{1,2})[./-](\d{1,2})[./-](\d{4})",                                   # día primero
               lambda m: "{}-{:0>2}-{:0>2}".format(m.group(3), m.group(2), m.group(1)), s)
    s = re.sub(r" (\d{2})(\d{2})(\d{2})$", r" \1:\2:\3", s)                               # 093000
    s = re.sub(r"\s*(?:GMT|UTC)\s*$", "", s)
    s = re.sub(r"\s*(?:GMT|UTC)?([+-])(\d{2}):?(\d{2})$", r"\1\2:\3", s)                   # GMT+0100
    s = re.sub(r"(\d{2}:\d{2}(?::\d{2})?)\.\d+", r"\1", s)                                  # milisegundos
    dt = datetime.fromisoformat(s.strip())
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def _num(v):
    return float(str(v).strip().strip('"').replace(",", "."))


def leer(texto_o_ruta):
    """Lee un CSV de velas. Devuelve (velas ordenadas [(t,o,h,l,c)], info) o lanza ValueError con la causa."""
    if os.path.exists(str(texto_o_ruta)[:4096]):
        if os.path.getsize(texto_o_ruta) > MAX_SUBIDA:
            raise ValueError("El archivo pasa de {} MB".format(MAX_SUBIDA // 1024 // 1024))
        f = open(texto_o_ruta, encoding="utf-8-sig", errors="replace", newline="")
    else:
        f = io.StringIO(str(texto_o_ruta).lstrip("\ufeff"))
    with f:
        muestra = f.read(20000)
        f.seek(0)
        lineas = [x for x in muestra.splitlines() if x.strip()][:6]
        if not lineas:
            raise ValueError("El archivo está vacío")
        sep = max([";", ",", "\t", "|"], key=lambda c: sum(x.count(c) for x in lineas))
        if sum(x.count(sep) for x in lineas) < len(lineas) * 4:
            sep = None
        rd = csv.reader(f, delimiter=sep) if sep else (re.split(r"\s+", x.strip()) for x in f)
        primera = next(rd)
        cab = [re.sub(r"[^a-z]", "", c.lower()) for c in primera]
        tiene_cab = sum(1 for c in cab if c) >= 3 and not re.match(r"^\d", primera[0].strip().strip('"'))
        col = _columnas(cab if tiene_cab else None, primera)
        filas = [] if tiene_cab else [primera]
        out, malas = {}, 0
        for fila in _encadenar(filas, rd):
            if not fila or not "".join(fila).strip():
                continue
            try:
                t = _a_ms(fila[col["t"]], fila[col["d"]] if col.get("d") is not None else None)
                o, h, l, c = (_num(fila[col[k]]) for k in "ohlc")
                if not (min(o, h, l, c) > 0 and h >= max(o, c) - 1e-9 and l <= min(o, c) + 1e-9):
                    raise ValueError
                out[t] = (t, o, h, l, c)
            except (ValueError, IndexError, OverflowError):
                malas += 1
                if malas > 1000 and malas > len(out):
                    raise ValueError("No se entienden las filas (¿columnas o separador distintos?). Ejemplo: {}"
                                     .format(sep.join(fila)[:120] if sep else " ".join(fila)[:120]))
            if len(out) > MAX_FILAS:
                raise ValueError("Más de {:,} velas: divide el archivo".format(MAX_FILAS).replace(",", "."))
    velas = [out[k] for k in sorted(out)]
    if len(velas) < 100:
        raise ValueError("Solo hay {} velas válidas".format(len(velas)))
    return velas, {"filas_malas": malas, "columnas": {k: v for k, v in col.items() if v is not None},
                   "separador": sep or "espacios", "cabecera": tiene_cab}


def _encadenar(a, b):
    for x in a:
        yield x
    for x in b:
        yield x


def _columnas(cab, fila):
    """Índices de fecha (t, y d si la hora va aparte), apertura, máximo, mínimo y cierre."""
    if cab:
        def busca(*nombres):
            for n in nombres:
                for i, c in enumerate(cab):
                    if c == n:
                        return i
            for n in nombres:
                for i, c in enumerate(cab):
                    if c.startswith(n):
                        return i
            return None
        d = busca("date", "fecha", "day")
        hora = busca("time", "hora")
        t = busca("timestamp", "opentime", "datetime", "localtime", "gmttime", "unix", "ts", "time", "date",
                  "fecha", "open_time")
        if d is not None and hora is not None and d != hora:
            t, d = hora, d
        else:
            d = None
        col = {"t": t, "d": d, "o": busca("open", "apertura", "o"), "h": busca("high", "max", "maximo", "h"),
               "l": busca("low", "min", "minimo", "l"), "c": busca("close", "cierre", "last", "c")}
        if None in (col["t"], col["o"], col["h"], col["l"], col["c"]):
            raise ValueError("No encuentro las columnas de fecha, apertura, máximo, mínimo y cierre en la cabecera: "
                             + ", ".join(x for x in cab if x))
        return col
    # sin cabecera: Binance (open_time,o,h,l,c,…) o MetaTrader (fecha,hora,o,h,l,c,…)
    try:
        _num(fila[1])
        return {"t": 0, "d": None, "o": 1, "h": 2, "l": 3, "c": 4}
    except (ValueError, IndexError):
        return {"t": 1, "d": 0, "o": 2, "h": 3, "l": 4, "c": 5}


# ============================ temporalidades ============================
def temporalidad(velas):
    difs = sorted(velas[i + 1][0] - velas[i][0] for i in range(min(len(velas) - 1, 5000)))
    paso = difs[len(difs) // 4] if difs else 0         # cuartil bajo: los huecos de fin de semana no cuentan
    for tf, ms in TF_MS.items():
        if paso == ms:
            return tf
    raise ValueError("Las velas son de {:g} min; se admiten de 1m a 1 semana".format(paso / 60000))


def agrupar(velas, tf_ms, solo_completas=True, base_ms=None):
    """Velas de una temporalidad mayor (UTC), sin la última si está incompleta."""
    out, cur, k = [], None, None
    for t, o, h, l, c in velas:
        b = inicio_vela(t, tf_ms)
        if b != k:
            if cur:
                out.append(tuple(cur))
            k, cur = b, [b, o, h, l, c]
        else:
            cur[2], cur[3], cur[4] = max(cur[2], h), min(cur[3], l), c
    if cur:
        if not solo_completas or (base_ms and velas[-1][0] + base_ms >= k + tf_ms):
            out.append(tuple(cur))
    return out


# ============================ verificación ============================
def _mediana(xs):
    xs = sorted(xs)
    n = len(xs)
    return (xs[n // 2] if n % 2 else (xs[n // 2 - 1] + xs[n // 2]) / 2) if n else None


def verificar(ext, mfp, tf_ms):
    """Compara velas externas con las de MFP. Devuelve el informe con el desfase horario que mejor encaja."""
    if not mfp:
        return {"veredicto": "sin_verificar", "motivo": "No se pudieron leer velas de MFP para comparar",
                "desfase_h": 0, "comunes": 0}
    ref = {c[0]: c[4] for c in mfp}
    t0, t1 = mfp[0][0], mfp[-1][0]
    import bisect
    ts = [c[0] for c in ext]
    ventana = ext[bisect.bisect_left(ts, t0 - 15 * 3_600_000):bisect.bisect_right(ts, t1 + 15 * 3_600_000)]
    pruebas = []
    for h in range(-14, 15):
        dt = h * 3_600_000
        pares = [(c[4], ref[c[0] + dt]) for c in ventana if c[0] + dt in ref]
        if len(pares) >= 50:
            pruebas.append((h, len(pares), _mediana([abs(a / b - 1) for a, b in pares]), pares))
    if not pruebas:
        return {"veredicto": "sin_verificar", "desfase_h": 0, "comunes": 0,
                "motivo": "No hay un tramo común con las velas de MFP (del {} al {}) para comparar".format(
                    _dia(t0), _dia(t1))}
    top = max(p[1] for p in pruebas)
    mejor = min((p for p in pruebas if p[1] >= 0.9 * top), key=lambda p: p[2])
    h, n, dif, pares = mejor
    ra = [math.log(pares[i + 1][0] / pares[i][0]) for i in range(len(pares) - 1)]
    rb = [math.log(pares[i + 1][1] / pares[i][1]) for i in range(len(pares) - 1)]
    corr = _correlacion(ra, rb)
    nivel = _mediana([a / b - 1 for a, b in pares])
    esperadas = int((min(ext[-1][0] + h * 3_600_000, t1) - max(ext[0][0] + h * 3_600_000, t0)) / tf_ms) + 1
    if dif <= 0.0005 and corr >= 0.97:
        ver, motivo = "coincide", "Mismo mercado: diferencia típica de precio {:.3f} %".format(dif * 100)
    elif corr >= 0.8 and dif <= 0.03:
        ver, motivo = "aproximado", ("Mercado parecido pero no idéntico (diferencia típica {:.2f} %, nivel {:+.2f} %): "
                                     "otra fuente, spot frente a perpetuo o futuros con base").format(
            dif * 100, nivel * 100)
    else:
        ver, motivo = "no_coincide", ("No parece el mismo mercado: diferencia típica {:.2f} % y correlación de los "
                                      "movimientos {:.2f}").format(dif * 100, corr)
    if h and ver != "no_coincide":
        motivo += ". Desfase horario corregido: {:+d} h (el archivo no estaba en UTC)".format(h)
    return {"veredicto": ver, "motivo": motivo, "desfase_h": h, "comunes": n, "esperadas": max(esperadas, n),
            "dif_tipica_pct": round(dif * 100, 4), "nivel_pct": round(nivel * 100, 4), "correlacion": round(corr, 4),
            "tramo": [max(ext[0][0] + h * 3_600_000, t0), min(ext[-1][0] + h * 3_600_000, t1)]}


def _correlacion(a, b):
    n = len(a)
    if n < 10:
        return 0.0
    ma, mb = sum(a) / n, sum(b) / n
    sa = math.sqrt(sum((x - ma) ** 2 for x in a))
    sb = math.sqrt(sum((x - mb) ** 2 for x in b))
    if not sa or not sb:
        return 0.0
    return sum((x - ma) * (y - mb) for x, y in zip(a, b)) / (sa * sb)


def _dia(ms):
    return time.strftime("%d-%m-%Y", time.gmtime(ms / 1000))


def huecos(velas, tf_ms):
    """% de velas que faltan dentro del rango (fines de semana incluidos)."""
    if len(velas) < 2:
        return 0.0
    esperadas = (velas[-1][0] - velas[0][0]) // tf_ms + 1
    return round(100.0 * (1 - len(velas) / esperadas), 1)


# ============================ almacén ============================
class Externos:
    """Datos externos en data/externos. `mfp_velas(simbolo, tf, n)` da velas recientes de MFP para comparar."""

    def __init__(self, folder, mfp_velas, log=print):
        self.folder, self.mfp_velas, self.log = folder, mfp_velas, log
        self.entrada = os.path.join(folder, "entrada")
        self.subidas = os.path.join(folder, "subidas")
        for d in (folder, self.entrada, self.subidas):
            os.makedirs(d, exist_ok=True)
        self.lock = threading.RLock()
        self.trabajo = None                         # importación en curso (una a la vez)
        self.meta = {}
        try:
            with open(os.path.join(folder, "externos.json"), encoding="utf-8") as f:
                self.meta = json.load(f)
        except (OSError, ValueError):
            pass
        self._memo = {}

    def _guardar(self):
        tmp = os.path.join(self.folder, "externos.json.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.meta, f, indent=1, ensure_ascii=False)
        os.replace(tmp, os.path.join(self.folder, "externos.json"))

    def ruta(self, sym, tf):
        return os.path.join(self.folder, "{}_{}.csv".format(sym, tf))

    # ---------- consultas ----------
    def publico(self):
        with self.lock:
            archivos = []
            for n in sorted(os.listdir(self.entrada)):
                p = os.path.join(self.entrada, n)
                if os.path.isfile(p) and NOMBRE_RE.match(n):
                    archivos.append({"nombre": n, "mb": round(os.path.getsize(p) / 1048576, 1)})
            return {"ok": True, "simbolos": self.meta, "trabajo": self.trabajo, "entrada": archivos,
                    "modos": MODOS, "veredictos": VEREDICTOS}

    def modo(self, sym):
        m = self.meta.get(sym)
        return m.get("modo", "no") if m else "no"

    def velas(self, sym, tf):
        """Velas externas de sym y tf (con el desfase ya corregido), memorizadas mientras no cambie el archivo."""
        p = self.ruta(sym, tf)
        try:
            mt = os.path.getmtime(p)
        except OSError:
            return []
        k = (sym, tf)
        if k in self._memo and self._memo[k][0] == mt:
            return self._memo[k][1]
        out = []
        with open(p, newline="") as f:
            for r in csv.reader(f):
                try:
                    out.append((int(r[0]), float(r[1]), float(r[2]), float(r[3]), float(r[4])))
                except (ValueError, IndexError):
                    pass
        self._memo = {kk: v for kk, v in self._memo.items() if kk[0] == sym}      # una sola serie grande en memoria
        self._memo[k] = (mt, out)
        return out

    def combinar(self, sym, tf, mfp_rows, modo=None):
        """Velas para un backtest según el modo del instrumento. Devuelve (velas, fuente)."""
        modo = modo or self.modo(sym)
        m = self.meta.get(sym) or {}
        if modo == "no" or m.get("veredicto") == "no_coincide" or tf not in (m.get("tfs") or {}):
            return mfp_rows, {"tipo": "mfp"}
        ext = self.velas(sym, tf)
        if not ext:
            return mfp_rows, {"tipo": "mfp"}
        fuente = {"tipo": modo, "veredicto": m.get("veredicto"), "archivo": m.get("archivo"),
                  "nivel_pct": (m.get("verificacion") or {}).get("nivel_pct")}
        if modo == "solo" or not mfp_rows:
            return ext, dict(fuente, externas=len(ext))
        t0 = mfp_rows[0][0]
        antes = [c for c in ext if c[0] < t0]
        return antes + list(mfp_rows), dict(fuente, externas=len(antes), union=t0)

    # ---------- cambios ----------
    def poner_modo(self, sym, modo):
        if modo not in MODOS:
            raise ValueError("Modo no válido")
        with self.lock:
            m = self.meta.get(sym)
            if not m:
                raise ValueError("{} no tiene datos externos".format(sym))
            if modo != "no" and m.get("veredicto") == "no_coincide":
                raise ValueError("Estos datos no coinciden con el mercado de MFP: no se pueden usar")
            m["modo"] = modo
            self._guardar()

    def borrar(self, sym):
        with self.lock:
            m = self.meta.pop(sym, None)
            for tf in TF_MS:
                try:
                    os.remove(self.ruta(sym, tf))
                except OSError:
                    pass
            self._memo.clear()
            self._guardar()
            return bool(m)

    def subir_parte(self, sid, parte, total, datos):
        """Subida por partes desde el panel (cada parte < 600 KB)."""
        if not re.fullmatch(r"[a-z0-9]{8,32}", str(sid)) or not 0 <= int(parte) < int(total) <= 2000:
            raise ValueError("Subida no válida")
        p = os.path.join(self.subidas, sid + ".part")
        if int(parte) == 0:
            for n in os.listdir(self.subidas):             # restos de subidas viejas
                q = os.path.join(self.subidas, n)
                if time.time() - os.path.getmtime(q) > 3600:
                    os.remove(q)
            open(p, "w").close()
        elif not os.path.exists(p):
            raise ValueError("Falta el principio de la subida: vuelve a empezar")
        if os.path.getsize(p) + len(datos) > MAX_SUBIDA:
            raise ValueError("El archivo pasa de {} MB".format(MAX_SUBIDA // 1024 // 1024))
        with open(p, "a", encoding="utf-8", newline="") as f:
            f.write(datos)
        return p

    def importar(self, sym, ruta, nombre, borrar_despues=False):
        """Lanza la importación en segundo plano (una a la vez)."""
        with self.lock:
            if self.trabajo and self.trabajo.get("estado") == "en curso":
                raise ValueError("Ya hay una importación en curso")
            self.trabajo = {"simbolo": sym, "archivo": nombre, "estado": "en curso", "paso": "Leyendo el archivo",
                            "inicio": int(time.time() * 1000)}
        threading.Thread(target=self._importar, args=(sym, ruta, nombre, borrar_despues), daemon=True,
                         name="externos").start()

    def _paso(self, texto):
        with self.lock:
            self.trabajo["paso"] = texto

    def _importar(self, sym, ruta, nombre, borrar_despues):
        try:
            velas, info = leer(ruta)
            tf = temporalidad(velas)
            tf_ms = TF_MS[tf]
            self._paso("Comparando {:,} velas de {} con MFP".format(len(velas), tf).replace(",", "."))
            n_mfp = int(min(20000, max(3000, (time.time() * 1000 - velas[-1][0]) / tf_ms + 3000)))
            try:
                mfp = self.mfp_velas(sym, tf, n_mfp)
            except Exception as e:
                mfp, info["error_mfp"] = [], str(e)
            ver = verificar(velas, mfp, tf_ms)
            dt = ver.get("desfase_h", 0) * 3_600_000 if ver["veredicto"] != "no_coincide" else 0
            if dt:
                velas = [(t + dt, o, h, l, c) for t, o, h, l, c in velas]
            self._paso("Guardando y generando temporalidades")
            tfs = {}
            for t2, ms in TF_MS.items():
                if ms < tf_ms or ms % tf_ms:
                    continue
                serie = velas if ms == tf_ms else agrupar(velas, ms, True, tf_ms)
                if len(serie) < 100:
                    continue
                tmp = self.ruta(sym, t2) + ".tmp"
                with open(tmp, "w", newline="") as f:
                    w = csv.writer(f)
                    for c in serie:
                        w.writerow((c[0], repr(c[1]), repr(c[2]), repr(c[3]), repr(c[4])))
                os.replace(tmp, self.ruta(sym, t2))
                tfs[t2] = {"n": len(serie), "desde": serie[0][0], "hasta": serie[-1][0]}
            with self.lock:
                for t2 in TF_MS:
                    if t2 not in tfs:
                        try:
                            os.remove(self.ruta(sym, t2))
                        except OSError:
                            pass
                antes = self.meta.get(sym) or {}
                modo = antes.get("modo", "no") if ver["veredicto"] != "no_coincide" else "no"
                self.meta[sym] = {"archivo": nombre, "importado": int(time.time() * 1000), "tf_base": tf,
                                  "tfs": tfs, "huecos_pct": huecos(velas, tf_ms), "veredicto": ver["veredicto"],
                                  "verificacion": ver, "lectura": info, "modo": modo}
                self._memo.clear()
                self._guardar()
                self.trabajo.update(estado="hecho", paso="", fin=int(time.time() * 1000), veredicto=ver["veredicto"])
            self.log("Datos externos de {}: {} velas de {} ({}) · {}".format(
                sym, len(velas), tf, nombre, VEREDICTOS[ver["veredicto"]]))
        except Exception as e:
            with self.lock:
                self.trabajo.update(estado="error", paso="", error=str(e)[:400], fin=int(time.time() * 1000))
        finally:
            if borrar_despues:
                try:
                    os.remove(ruta)
                except OSError:
                    pass
