# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Borislav Vladimirov Petrakiev
"""
RSI por niveles: ejemplo completo de archivo de estrategia (léelo junto con ESTRATEGIAS.md).

Largos (los cortos son el espejo):
  * Entrada «al salir de la zona»: el RSI cruza hacia arriba el nivel de sobreventa.
    Entrada «al entrar en la zona»: el RSI cruza hacia abajo el nivel de sobreventa (contra la caída).
  * Filtro opcional: solo largos con el cierre por encima de una EMA (y cortos por debajo).
  * Stop: a N × ATR del cierre o bajo el mínimo de las últimas N velas.
  * Objetivo: RR fijo, o sin objetivo y cierre cuando el RSI llega al nivel opuesto.
"""
from nucleo import F, EMA, RSI, Ventana, Base

ID = "rsi_niveles"
NOMBRE = "RSI por niveles"
VERSION = "1.0"
AUTOR = "Boris y Claude"
DESCRIPCION = ("Entra cuando el RSI cruza un nivel extremo (al salir de la zona o al entrar), con filtro opcional "
               "de EMA. Stop por ATR o por el mínimo/máximo de las últimas velas; objetivo en RR o salida en el "
               "RSI del nivel opuesto.")

PARAMETROS = [
    F("rn_len", "RSI", "Periodo del RSI", "int", 14, "", min=2, max=200),
    F("rn_os", "RSI", "Sobreventa", "float", 30, "", min=1, max=99),
    F("rn_ob", "RSI", "Sobrecompra", "float", 70, "", min=1, max=99),
    F("rn_entry", "RSI", "Entrada", "select", "al salir de la zona",
      "Al salir: cruce de vuelta hacia el centro. Al entrar: en cuanto el RSI entra en la zona extrema.",
      options=["al salir de la zona", "al entrar en la zona"]),
    F("rn_ema", "Filtro EMA", "Periodo de la EMA (0 = sin filtro)", "int", 0,
      "Largos solo con el cierre por encima de la EMA; cortos, por debajo. Se dibuja en el gráfico.",
      min=0, max=1000),
    F("rn_sl", "Stop", "Colocación del stop", "select", "ATR", "",
      options=["ATR", "mínimo/máximo de N velas"]),
    F("rn_sl_atr", "Stop", "Distancia (ATR)", "float", 1.5, "", min=0.1, max=20, step=0.1, show_if={"rn_sl": "ATR"}),
    F("rn_sl_bars", "Stop", "Velas para el mínimo/máximo", "int", 5, "", min=1, max=200,
      show_if={"rn_sl": "mínimo/máximo de N velas"}),
    F("rn_tp", "Objetivo y salidas", "Objetivo", "select", "RR",
      "RR: múltiplo del riesgo. RSI opuesto: sin objetivo; cierra cuando el RSI llega a la zona contraria.",
      options=["RR", "RSI opuesto"]),
    F("rn_rr", "Objetivo y salidas", "RR", "float", 2.0, "", min=0.1, max=20, step=0.1, show_if={"rn_tp": "RR"}),
]
SALIDAS = {"RSI OPUESTO": "RSI en la zona opuesta"}
FILTROS = {"ema": "contra la EMA"}
LINEAS = [{"key": "ema", "nombre": "EMA", "color": "blue", "estilo": "continua"}]


def validar(P):
    return ["La sobreventa tiene que ser menor que la sobrecompra"] if P["rn_os"] >= P["rn_ob"] else []


def calentamiento(P):
    return 4 * max(P["rn_len"], P["rn_ema"], P["atr_len"])


class Estrategia(Base):
    def __init__(self, P, motor):
        super().__init__(P, motor)
        self.rsi_ind = RSI(P["rn_len"])
        self.ema_ind = EMA(P["rn_ema"]) if P["rn_ema"] else None
        self.velas = Ventana(P["rn_sl_bars"])
        self.rsi = self.prev_rsi = self.ema = None
        self.c = None

    def actualizar(self, c):
        self.c = c
        self.rsi = self.rsi_ind.update(c[4])
        if self.ema_ind:
            self.ema = self.ema_ind.update(c[4])
        self.velas.add(c)

    def rr(self):
        return self.P["rn_rr"] if self.P["rn_tp"] == "RR" else None

    def salida(self, pos):
        P = self.P
        if P["rn_tp"] == "RSI opuesto" and self.rsi is not None:
            if pos["side"] == 1 and self.rsi >= P["rn_ob"] or pos["side"] == -1 and self.rsi <= P["rn_os"]:
                return "RSI OPUESTO"
        return None

    def senales(self):
        P, out = self.P, []
        rsi, prev, atr = self.rsi, self.prev_rsi, self.m.atr
        cl = self.c[4]
        if rsi is not None and prev is not None and atr is not None and self.velas.lleno():
            if P["rn_entry"] == "al salir de la zona":
                long_ = prev <= P["rn_os"] < rsi
                short = prev >= P["rn_ob"] > rsi
            else:
                long_ = prev >= P["rn_os"] > rsi
                short = prev <= P["rn_ob"] < rsi
            for side, go in ((1, long_), (-1, short)):
                if not go:
                    continue
                if P["rn_sl"] == "ATR":
                    sl = cl - side * P["rn_sl_atr"] * atr
                else:
                    sl = min(v[3] for v in self.velas.d) if side == 1 else max(v[2] for v in self.velas.d)
                sig = {"side": side, "sl": sl}
                if self.ema_ind and (self.ema is None or (cl - self.ema) * side <= 0):
                    sig["rechazo"] = "ema"
                out.append(sig)
        self.prev_rsi = rsi
        return out

    def lineas(self):
        return {"ema": self.ema} if self.ema is not None else {}

    def indicadores(self):
        return "RSI {:.0f}".format(self.rsi) if self.rsi is not None else ""

    def texto(self):
        P, r = self.P, self.rsi
        if r is None:
            return "Calentando el RSI."
        if r <= P["rn_os"]:
            return "RSI {:.0f} en sobreventa: {}.".format(r, "esperando que salga para comprar"
                                                         if P["rn_entry"] == "al salir de la zona" else "dentro de la zona")
        if r >= P["rn_ob"]:
            return "RSI {:.0f} en sobrecompra: {}.".format(r, "esperando que salga para vender"
                                                          if P["rn_entry"] == "al salir de la zona" else "dentro de la zona")
        return "RSI {:.0f}, entre niveles ({:g}–{:g}).".format(r, P["rn_os"], P["rn_ob"])
