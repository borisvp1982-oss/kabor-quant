# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Borislav Vladimirov Petrakiev
"""
cruce_emas.py -- Example strategy for KaBor Quant: EMA crossover with a trend filter.
(Estrategia de ejemplo: cruce de EMAs con filtro de tendencia.)

This is the reference file to learn how a KaBor strategy is written. It is a teaching example, not a tested edge:
backtest it on your own market before trading it.

Rules (long; the short side is the mirror):
  1) Fast EMA (9) crosses above slow EMA (21) at a candle close.
  2) Optional trend filter: the close must be above the trend EMA (200). Signals against it are logged as
     "filtered", not traded.
  3) Entry at the open of the next candle (done by the KaBor engine, same in live and backtest).
  4) Stop: signal close minus N x ATR (1.5 by default). ATR comes from the engine (common "ATR length" setting).
  5) Exit: fixed target at RR x risk (2.0 by default), or "opposite cross": close the trade at the candle close
     when the fast EMA crosses back below the slow one.

Connection with MyFundedPerps: the strategy never calls the API. With "Sandbox execution -> Mode: sandbox",
KaBor sends each signal as a market order with the SL attached (and the TP in RR mode), sized by risk; with the
"opposite cross" exit, the real position is closed at market.

Test it with real MFP candles before installing:
    ./bot.sh probar estrategias/cruce_emas.py --simbolo BTC --tf 15m --dias 60
    ./bot.sh probar estrategias/cruce_emas.py --simbolo BTC --tf 15m --dias 60 --ajuste ce_salida="cruce contrario"
"""
from nucleo import F, EMA, Base

ID = "cruce_emas"
NOMBRE = "Cruce de EMAs"
VERSION = "1.0"
AUTOR = "KaBor Quant"
DESCRIPCION = ("Ejemplo didáctico: largo cuando la EMA rápida cruza por encima de la lenta y corto al revés, "
               "con filtro opcional de EMA de tendencia, stop en ATR y salida a RR fijo o en el cruce contrario.")

PARAMETROS = [
    # ---- EMAs ----
    F("ce_rapida", "EMAs", "EMA rápida", "int", 9, "", min=2, max=500),
    F("ce_lenta", "EMAs", "EMA lenta", "int", 21, "Tiene que ser más larga que la rápida.",
      min=3, max=1000),
    F("ce_tendencia", "EMAs", "EMA de tendencia (0 = sin filtro)", "int", 200,
      "Largos solo con el cierre por encima de esta EMA; cortos, por debajo.", min=0, max=2000),
    # ---- Stop ----
    F("ce_sl_atr", "Stop", "Stop (ATR desde el cierre)", "float", 1.5,
      "Distancia del cierre de la señal al stop, en ATR.", min=0.2, max=20, step=0.1),
    # ---- Exit ----
    F("ce_salida", "Objetivo y salidas", "Salida", "select", "RR",
      "RR: objetivo fijo a un múltiplo del riesgo. Cruce contrario: sin objetivo; cierra al cierre de la vela en "
      "la que las EMAs vuelven a cruzarse.",
      options=["RR", "cruce contrario"]),
    F("ce_rr", "Objetivo y salidas", "RR", "float", 2.0, "Objetivo como múltiplo del riesgo.",
      min=0.3, max=20, step=0.1, show_if={"ce_salida": "RR"}),
]

# Defaults for the common settings (any of them can still be changed in the panel).
AJUSTES_COMUNES = {
    "symbol": {"default": "BTC"},
    "timeframe": {"default": "15m"},
}
FILTROS = {"tendencia_ema": "contra la EMA de tendencia"}
SALIDAS = {"CRUCE": "cruce contrario"}
LINEAS = [
    {"key": "rapida", "nombre": "EMA rápida", "color": "gold", "estilo": "continua"},
    {"key": "lenta", "nombre": "EMA lenta", "color": "blue", "estilo": "continua"},
    {"key": "tendencia", "nombre": "EMA de tendencia", "color": "muted", "estilo": "discontinua"},
]
LADOS = {1: "alcista", -1: "bajista"}


def validar(P):
    errs = []
    if P["ce_rapida"] >= P["ce_lenta"]:
        errs.append("La EMA rápida tiene que ser más corta que la lenta")
    if P["ce_tendencia"] and P["ce_tendencia"] <= P["ce_lenta"]:
        errs.append("La EMA de tendencia tiene que ser más larga que la lenta (o 0 para quitar el filtro)")
    return errs


def calentamiento(P):
    # EMAs need several times their period before they stop depending on the first candles.
    return 4 * max(P["ce_lenta"], P["ce_tendencia"] or 0)


class Estrategia(Base):
    def __init__(self, P, motor):
        super().__init__(P, motor)
        self.e_r, self.e_l = EMA(P["ce_rapida"]), EMA(P["ce_lenta"])
        self.e_t = EMA(P["ce_tendencia"]) if P["ce_tendencia"] else None
        self.c = None
        self.vr = self.vl = self.vt = None
        self.dif_ant = None          # fast - slow at the previous candle (to detect the cross)
        self.nota = ""

    # ---- state that must survive a restart ----
    def guardar(self):
        return {"dif_ant": self.dif_ant, "nota": self.nota}

    def cargar(self, s):
        self.dif_ant = s.get("dif_ant")
        self.nota = s.get("nota", "")

    def reiniciar(self):
        self.nota = ""

    # ---- candle by candle ----
    def actualizar(self, vela):                  # start of each closed candle: update indicators only
        self.c = vela
        cl = vela[4]
        self.vr, self.vl = self.e_r.update(cl), self.e_l.update(cl)
        self.vt = self.e_t.update(cl) if self.e_t else None

    def rr(self):
        return self.P["ce_rr"] if self.P["ce_salida"] == "RR" else None

    def objetivo(self, pos):
        return None                              # "opposite cross" has no target: the exit is in salida()

    def salida(self, pos):
        if self.P["ce_salida"] != "cruce contrario" or None in (self.vr, self.vl):
            return None
        side = pos["side"]
        if (self.vr - self.vl) * side < 0:       # the EMAs are now crossed against the trade
            return "CRUCE"
        return None

    def senales(self):                           # at each candle close
        P, atr = self.P, self.m.atr
        if self.c is None or None in (self.vr, self.vl):
            return []
        dif, ant = self.vr - self.vl, self.dif_ant
        self.dif_ant = dif
        if ant is None or atr is None:
            return []
        if ant <= 0 < dif:
            side = 1
        elif ant >= 0 > dif:
            side = -1
        else:
            return []
        cl = self.c[4]
        sig = {"side": side, "sl": cl - side * P["ce_sl_atr"] * atr}
        if self.e_t is not None and (self.vt is None or (cl - self.vt) * side <= 0):
            sig["rechazo"] = "tendencia_ema"     # counted as filtered: no trade, no order
        self.nota = "Cruce {} {}.".format(
            LADOS[side], "rechazado: contra la EMA de tendencia" if "rechazo" in sig
            else "válido: entra en la apertura de la vela siguiente")
        return [sig]

    # ---- panel and alerts ----
    def lineas(self):
        if self.vl is None:
            return {}
        out = {"rapida": self.vr, "lenta": self.vl}
        if self.vt is not None:
            out["tendencia"] = self.vt
        return out

    def indicadores(self):
        if self.vl is None:
            return ""
        txt = "EMA {} {:.1f} · EMA {} {:.1f}".format(self.P["ce_rapida"], self.vr, self.P["ce_lenta"], self.vl)
        if self.vt is not None:
            txt += " · EMA {} {:.1f}".format(self.P["ce_tendencia"], self.vt)
        return txt

    def texto(self):
        if self.vl is None or (self.e_t is not None and self.vt is None):
            return "Calentando las EMAs."
        if self.m.pos:
            return "Dentro de la operación." if self.P["ce_salida"] == "RR" else \
                "Dentro de la operación: sale si las EMAs se cruzan en contra."
        if self.nota:
            return self.nota
        return "Esperando un cruce. EMA rápida {} la lenta.".format(
            "por encima de" if self.vr > self.vl else "por debajo de")
