# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Borislav Vladimirov Petrakiev
"""
Ruptura del canal de Keltner a favor, con salida al volver a la media, como archivo de estrategia.

Es la estrategia que salió de las pruebas de 2019 a 2026 con velas reales de MyFundedPerps (proyecto mfp_bot):
la reversión al canal perdía en todas las temporalidades; la ruptura del canal exterior, dejando correr la
ganancia hasta que el precio vuelve a la media, era la única familia positiva fuera de muestra, y el filtro de
ADX bajo reducía el peor drawdown a menos de la mitad. Configuración de referencia: BTC 30m, ADX ≤ 28.

Regla base (largo; el corto es el espejo):
  1) Canal de Keltner: media EMA(n) ± ATR(n) x multiplicador (exterior 3.75 por defecto; n = 50).
  2) Señal: una vela cierra por encima de la banda exterior superior y la vela anterior cerraba por debajo
     (es un cruce, no basta con seguir fuera). Opcional: ADX(14) de esa vela por debajo de un máximo
     (ruptura desde un mercado sin tendencia) y/o por encima de un mínimo.
  3) Entrada en la apertura de la vela siguiente. Stop en el mínimo de la vela de ruptura (o en la banda
     interior), con un colchón opcional en ATR.
  4) Salida: al cierre de la primera vela que cierra por debajo de la media (salida «media»), o a RR fijo.
  Opcional (v1.1): filtro de FVG de temporalidad alta (1h o 4h, construidas con las velas del bot). FVG alcista:
  el mínimo de una vela queda por encima del máximo de dos velas antes (bajista, el espejo). Cuenta desde que
  cierra la vela que lo forma y deja de contar cuando el precio atraviesa su borde lejano.
  En las pruebas (BTC 30m 2018-2026, Bitstamp, ADX ≤ 28, colchón 0,25 ATR):
    sin filtro              752 op. +301R  +0,40R/op  PF 1,68  DD -18,0R
    4h último FVG a favor   536 op. +298R  +0,56R/op  PF 1,93  DD -18,0R  (mejora sobre todo 2023-2026)
    4h más FVG a favor      412 op. +224R  +0,55R/op  PF 1,92  DD -14,4R
    1h último FVG a favor   670 op. +290R  +0,43R/op  PF 1,73  DD -17,3R
  El colchón del stop de 0,25-0,5 ATR también bajaba el drawdown (0,5 ATR: DD -15,7R).

Conexión con MyFundedPerps: no llama a la API. Con «Ejecución en sandbox → Modo: sandbox», la señal se envía
como orden a mercado con el SL adjunto y sin TP; la salida por la media cierra la posición real a mercado.

Resultados de referencia, BTC 30m, sept-2019 → oct-2026, con el backtest de este sistema (comisión 0,06 % ida y
vuelta, sin swap): con ADX ≤ 28, 513 operaciones, +266R, peor drawdown 15,2R, 7 de 8 años positivos; sin filtro
ADX, 1092 operaciones, +434R, peor drawdown 36,7R. (En mfp_bot, con swap: +257R / 15,6R y +419R / 36,9R.) Acierta ~1 de cada 3: el resultado depende de unos pocos movimientos grandes y hay
rachas de 12-16 pérdidas seguidas. Ajusta el riesgo para que esa racha quepa en el drawdown de tu cuenta.
"""
from datetime import datetime, timezone

from nucleo import F, EMA, RMA, Base

ID = "keltner_ruptura"
NOMBRE = "Ruptura de Keltner"
VERSION = "1.1"
AUTOR = "Boris y Claude"
DESCRIPCION = ("Ruptura a favor del canal de Keltner exterior, con filtro de ADX bajo, stop en la vela de ruptura y "
               "salida cuando el precio vuelve a cerrar al otro lado de la media. Referencia: BTC 30m, ADX ≤ 28.")

PARAMETROS = [
    # ---- Canal ----
    F("kr_len", "Canal de Keltner", "Periodo (EMA y ATR del canal)", "int", 50,
      "La media es una EMA y el ancho un ATR de Wilder, los dos con este periodo.", min=5, max=500),
    F("kr_out", "Canal de Keltner", "Multiplicador del canal exterior", "float", 3.75,
      "La señal es el cruce de esta banda.", min=0.5, max=10, step=0.05),
    F("kr_in", "Canal de Keltner", "Multiplicador del canal interior", "float", 2.75,
      "Solo se usa si el stop va en la banda interior o la ruptura es de la interior.", min=0.5, max=10, step=0.05),
    F("kr_banda", "Canal de Keltner", "Banda de ruptura", "select", "exterior",
      "Exterior: la probada. Interior: más señales, peor resultado en las pruebas.", options=["exterior", "interior"]),
    # ---- Filtro ADX ----
    F("kr_adx_len", "Filtro ADX", "Periodo del ADX", "int", 14, "ADX de Wilder.", min=2, max=100),
    F("kr_adx_max", "Filtro ADX", "Solo si el ADX es como mucho", "float", 28,
      "Rupturas desde un mercado sin tendencia. 0 = sin este filtro. En las pruebas, 25 a 30 en 30m.",
      min=0, max=100, step=1),
    F("kr_adx_min", "Filtro ADX", "Solo si el ADX es al menos", "float", 0,
      "0 = sin este filtro. En las pruebas, exigir tendencia (ADX alto) empeoraba.", min=0, max=100, step=1),
    # ---- Filtro FVG ----
    F("kr_fvg", "Filtro FVG", "Filtro de FVG", "select", "desactivado",
      "Último a favor: el FVG activo más reciente de la temporalidad alta apunta en el sentido de la ruptura "
      "(4h: mismo resultado total con un 30% menos de operaciones). Más a favor: hay más FVG activos a favor que en "
      "contra (4h: menos drawdown, pero menos ganancia).",
      options=["desactivado", "último a favor", "más a favor"]),
    F("kr_fvg_tf", "Filtro FVG", "Temporalidad del FVG", "select", "4h",
      "Velas de 1h o 4h construidas en hora UTC con las velas del bot. 4h fue la que mejor resultado dio.",
      options=["1h", "4h"], show_if={"kr_fvg": ["último a favor", "más a favor"]}),
    # ---- Stop ----
    F("kr_sl", "Stop", "Colocación del stop", "select", "vela de ruptura",
      "Vela de ruptura: tras su mínimo (largos) o máximo (cortos). Banda interior: en la banda interior de esa vela "
      "(o en la media si la ruptura es de la interior).", options=["vela de ruptura", "banda interior"]),
    F("kr_sl_buf", "Stop", "Colchón del stop (ATR del canal)", "float", 0.0, "", min=0, max=5, step=0.05),
    # ---- Salida ----
    F("kr_salida", "Objetivo y salidas", "Salida", "select", "media",
      "Media: sin objetivo; cierra al cierre de la vela que vuelve al otro lado de la media del canal. "
      "RR: objetivo fijo a un múltiplo del riesgo (en las pruebas daba ~0R).", options=["media", "RR"]),
    F("kr_rr", "Objetivo y salidas", "RR", "float", 2.0, "", min=0.5, max=20, step=0.1, show_if={"kr_salida": "RR"}),
]

# Valores por defecto de los ajustes comunes con los que se probó (los límites diarios no estaban en la prueba).
AJUSTES_COMUNES = {
    "symbol": {"default": "BTC"},
    "timeframe": {"default": "30m"},
    "max_cost": {"default": 0.0, "help": "0 = sin filtro (como en la prueba). Con 0,15R se descartan las rupturas de "
                                          "stop más ajustado: menos drawdown, pero se pierden algunas de las mejores."},
    "max_trades": {"default": 0},
    "max_consec": {"default": 0, "help": "0 = sin límite. Ojo: esta estrategia tiene rachas largas de pérdidas pequeñas "
                                          "antes de cada movimiento grande; cortar el día puede perder ese movimiento."},
    "max_profit_r": {"default": 0},
    "max_loss_r": {"default": 0},
    "be_trigger": {"options": ["R"], "default": "R"},
}
FILTROS = {"adx_alto": "ADX por encima del máximo", "adx_bajo": "ADX por debajo del mínimo",
           "fvg": "FVG de temporalidad alta en contra"}
SALIDAS = {"MEDIA": "vuelta a la media"}
LINEAS = [
    {"key": "media", "nombre": "Media", "color": "muted", "estilo": "continua"},
    {"key": "ext_sup", "nombre": "Exterior superior", "color": "gold", "estilo": "continua"},
    {"key": "ext_inf", "nombre": "Exterior inferior", "color": "gold", "estilo": "continua"},
    {"key": "int_sup", "nombre": "Interior superior", "color": "blue", "estilo": "discontinua"},
    {"key": "int_inf", "nombre": "Interior inferior", "color": "blue", "estilo": "discontinua"},
]
LADOS = {1: "alcista", -1: "bajista"}


def a_utc(t):
    """Hora de la vela en UTC. Acepta epoch en segundos o milisegundos, datetime o texto ISO (sin zona = UTC)."""
    if isinstance(t, datetime):
        dt = t
    elif isinstance(t, (int, float)):
        dt = datetime.fromtimestamp(t / 1000 if t > 1e11 else t, tz=timezone.utc)
    else:
        dt = datetime.fromisoformat(str(t).replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


class FVG:
    """FVG de una temporalidad alta (en horas, alineada a UTC) a partir de las velas del bot.
    Un FVG cuenta desde que cierra la vela que lo forma y se borra cuando el precio atraviesa su borde lejano."""

    def __init__(self, horas):
        self.seg = int(horas * 3600)
        self.bucket = None              # inicio (epoch) de la vela alta en curso
        self.cur = None                 # [máximo, mínimo] de la vela alta en curso
        self.velas = []                 # últimas 3 velas altas cerradas [máximo, mínimo]
        self.activos = []               # [dirección, borde inferior, borde superior, número]
        self.n = 0

    def update(self, t, h, l):
        b = int(a_utc(t).timestamp()) // self.seg * self.seg
        if self.bucket is not None and b != self.bucket and self.cur is not None:
            self.velas = (self.velas + [self.cur])[-3:]
            if len(self.velas) == 3:
                (h0, l0), _, (h2, l2) = self.velas
                if l2 > h0:
                    self.n += 1
                    self.activos.append([1, h0, l2, self.n])
                elif h2 < l0:
                    self.n += 1
                    self.activos.append([-1, h2, l0, self.n])
            self.cur = None
        self.bucket = b
        self.cur = [h, l] if self.cur is None else [max(self.cur[0], h), min(self.cur[1], l)]
        # mitigación: el precio atraviesa el borde lejano
        self.activos = [g for g in self.activos if not ((g[0] == 1 and l <= g[1]) or (g[0] == -1 and h >= g[2]))]

    def ultimo(self):
        return max(self.activos, key=lambda g: g[3])[0] if self.activos else 0

    def neto(self):
        return sum(g[0] for g in self.activos)

    def guardar(self):
        return {"bucket": self.bucket, "cur": self.cur, "velas": self.velas, "activos": self.activos, "n": self.n}

    def cargar(self, s):
        if not s:
            return
        self.bucket, self.cur = s.get("bucket"), s.get("cur")
        self.velas, self.activos, self.n = s.get("velas", []), s.get("activos", []), s.get("n", 0)


def validar(P):
    errs = []
    if P["kr_in"] >= P["kr_out"]:
        errs.append("El multiplicador interior tiene que ser menor que el exterior")
    if P["kr_adx_max"] and P["kr_adx_min"] and P["kr_adx_min"] >= P["kr_adx_max"]:
        errs.append("El ADX mínimo tiene que ser menor que el máximo")
    return errs


def calentamiento(P):
    need = 4 * max(P["kr_len"], 2 * P["kr_adx_len"])
    if P.get("kr_fvg", "desactivado") != "desactivado":
        # 12 velas de la temporalidad del FVG para que haya FVG detectados al arrancar (en 1m-5m el canal
        # se calienta mucho antes que el FVG de 4h, y «último a favor» rechazaría todo hasta entonces)
        from nucleo import TF_MS
        horas = 4 if P.get("kr_fvg_tf") == "4h" else 1
        need = max(need, 12 * horas * 3_600_000 // TF_MS[P["timeframe"]])
    return need


class ADX:
    """ADX de Wilder incremental. update(h, l, c) devuelve el valor o None mientras calienta."""

    def __init__(self, n):
        self.tr, self.pdm, self.mdm, self.dx = RMA(n), RMA(n), RMA(n), RMA(n)
        self.prev = None
        self.v = None

    def update(self, h, l, c):
        if self.prev is None:
            self.prev = (h, l, c)
            return None
        ph, pl, pc = self.prev
        self.prev = (h, l, c)
        up, dn = h - ph, pl - l
        pdm = up if up > dn and up > 0 else 0.0
        mdm = dn if dn > up and dn > 0 else 0.0
        tr = max(h - l, abs(h - pc), abs(l - pc))
        a, p, m = self.tr.update(tr), self.pdm.update(pdm), self.mdm.update(mdm)
        if a is None or a <= 0:
            return None
        pdi, mdi = 100 * p / a, 100 * m / a
        dx = 100 * abs(pdi - mdi) / (pdi + mdi) if pdi + mdi > 0 else 0.0
        self.v = self.dx.update(dx)
        return self.v


class Estrategia(Base):
    def __init__(self, P, motor):
        super().__init__(P, motor)
        self.ema, self.atr = EMA(P["kr_len"]), RMA(P["kr_len"])
        self.adx_ind = ADX(P["kr_adx_len"])
        self.fvg = FVG(4 if P["kr_fvg_tf"] == "4h" else 1)
        self.c = None
        self.mid = self.a = self.adx = None
        self.prev_bands = None          # (cierre, banda superior, banda inferior) de la vela anterior
        self.nota = ""

    # --- estado ---
    def guardar(self):
        return {"prev_bands": self.prev_bands, "nota": self.nota, "fvg": self.fvg.guardar()}

    def cargar(self, s):
        pb = s.get("prev_bands")
        self.prev_bands = tuple(pb) if pb else None
        self.nota = s.get("nota", "")
        self.fvg.cargar(s.get("fvg"))

    def reiniciar(self):
        self.nota = ""

    # --- vela a vela ---
    def actualizar(self, c):
        prev = self.c
        self.c = c
        t, o, h, l, cl = c
        tr = h - l if prev is None else max(h - l, abs(h - prev[4]), abs(l - prev[4]))
        self.mid, self.a = self.ema.update(cl), self.atr.update(tr)
        self.adx = self.adx_ind.update(h, l, cl)
        if self.P["kr_fvg"] != "desactivado":
            self.fvg.update(t, h, l)

    def _bandas(self, mult):
        return self.mid + mult * self.a, self.mid - mult * self.a

    def rr(self):
        return self.P["kr_rr"] if self.P["kr_salida"] == "RR" else None

    def objetivo(self, pos):
        return None                     # salida «media»: sin objetivo; se cierra en salida()

    def salida(self, pos):
        if self.P["kr_salida"] != "media" or self.mid is None or self.c is None:
            return None
        side = pos.get("side") if isinstance(pos, dict) else getattr(pos, "side", None)
        if side and (self.c[4] - self.mid) * side < 0:
            return "MEDIA"
        return None

    def senales(self):
        P = self.P
        if self.c is None or self.mid is None or self.a is None:
            return []
        t, o, h, l, cl = self.c
        mult = P["kr_out"] if P["kr_banda"] == "exterior" else P["kr_in"]
        up, lo = self._bandas(mult)
        prev, self.prev_bands = self.prev_bands, (cl, up, lo)
        if prev is None:
            return []
        pcl, pup, plo = prev
        if pcl <= pup and cl > up:
            side = 1
        elif pcl >= plo and cl < lo:
            side = -1
        else:
            return []
        # stop
        if P["kr_sl"] == "banda interior":
            iu, il = self._bandas(P["kr_in"])
            stop = (self.mid if P["kr_banda"] == "interior" else (iu if side == 1 else il))
        else:
            stop = l if side == 1 else h
        stop -= side * P["kr_sl_buf"] * self.a
        if (cl - stop) * side <= 0:
            self.nota = "Ruptura {} descartada: el stop no queda al otro lado del cierre.".format(LADOS[side])
            return []
        sig = {"side": side, "sl": stop}
        if P["kr_adx_max"] and (self.adx is None or self.adx > P["kr_adx_max"]):
            sig["rechazo"] = "adx_alto"
        elif P["kr_adx_min"] and (self.adx is None or self.adx < P["kr_adx_min"]):
            sig["rechazo"] = "adx_bajo"
        elif P["kr_fvg"] == "último a favor" and self.fvg.ultimo() != side:
            sig["rechazo"] = "fvg"
        elif P["kr_fvg"] == "más a favor" and self.fvg.neto() * side <= 0:
            sig["rechazo"] = "fvg"
        adx_txt = "ADX {:.0f}".format(self.adx) if self.adx is not None else "ADX calentando"
        self.nota = "Ruptura {} {} ({}): {}.".format(
            LADOS[side], "rechazada" if "rechazo" in sig else "válida", adx_txt,
            FILTROS[sig["rechazo"]] if "rechazo" in sig else "entra en la apertura de la siguiente vela")
        return [sig]

    # --- panel ---
    def lineas(self):
        if self.mid is None or self.a is None:
            return {}
        es, ei = self._bandas(self.P["kr_out"])
        is_, ii = self._bandas(self.P["kr_in"])
        return {"media": self.mid, "ext_sup": es, "ext_inf": ei, "int_sup": is_, "int_inf": ii}

    def indicadores(self):
        partes = []
        if self.mid is not None:
            partes.append("media {:.1f}".format(self.mid))
        if self.adx is not None:
            partes.append("ADX {:.0f}".format(self.adx))
        if self.P["kr_fvg"] != "desactivado":
            u = self.fvg.ultimo()
            partes.append("FVG {}: {} activos, último {}".format(
                self.P["kr_fvg_tf"], len(self.fvg.activos), {1: "alcista", -1: "bajista", 0: "ninguno"}[u]))
        return " | ".join(partes)

    def texto(self):
        if self.mid is None or self.a is None:
            return "Calentando el canal de Keltner."
        pos = self.m.pos
        if pos:
            return "Dentro de la operación: sale al cerrar una vela al otro lado de la media ({:.1f}).".format(self.mid)
        if self.nota:
            return self.nota
        cl = self.c[4]
        es, ei = self._bandas(self.P["kr_out"] if self.P["kr_banda"] == "exterior" else self.P["kr_in"])
        lim = "" if not self.P["kr_adx_max"] or self.adx is None else \
            " ADX {:.0f} ({} {:.0f}).".format(self.adx, "por debajo de" if self.adx <= self.P["kr_adx_max"]
                                              else "por encima de", self.P["kr_adx_max"])
        return "Esperando un cierre fuera del canal (arriba {:.1f}, abajo {:.1f}; último cierre {:.1f}).{}".format(
            es, ei, cl, lim)
