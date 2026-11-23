# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Borislav Vladimirov Petrakiev
"""
Rebote en EMA a favor de la tendencia, como archivo de estrategia.

Idea: con la tendencia definida por una EMA lenta, esperar a que el precio retroceda hasta la EMA de rebote
y operar el rebote en la dirección de la tendencia.

Regla base (pensada para velas de 5 minutos, vale para 1-15m):
  1) Tendencia alcista: cierre por encima de la EMA lenta, EMA de rebote por encima de la lenta y (opcional)
     EMA lenta subiendo. Bajista al revés.
  2) Armado: el precio tiene que separarse de la EMA de rebote al menos X·ATR a favor de la tendencia. Así solo
     cuenta como retroceso una vuelta a la EMA después de un impulso, no el precio pegado a la media.
  3) Toque: una vela llega a la EMA de rebote (con su mecha, más una tolerancia en ATR).
  4) Rebote: en esa misma vela o en las N siguientes, una vela confirma a favor de la tendencia.
     Si antes cierra demasiado al otro lado de la EMA o se pierde la tendencia, el retroceso se descarta.
  5) Entrada en la apertura de la vela siguiente. SL más allá del extremo del retroceso + colchón en ATR.
     TP = RR x riesgo. Después de cada señal (o descarte) el precio tiene que volver a separarse para rearmar.
Filtros opcionales: EMA de temporalidad mayor, RSI a favor, ventana horaria, límite de señales por día.

Conexión con MyFundedPerps (v1.1): la estrategia no llama a la API. El programa (v5) le da las velas cerradas del
feed de MFP y, si el bot está en «Ejecución en sandbox → Modo: sandbox», replica sus decisiones en la cuenta:
  * la señal ({"side", "sl"}) se convierte en una orden a mercado con el SL adjunto y el TP a RR x riesgo,
    con el tamaño por riesgo de la configuración del bot y tras consultar la trading policy;
  * la salida «Cerrar si se pierde la EMA lenta» (salida()) cierra la posición real a mercado;
  * el break-even por R, el trailing y las ampliaciones comunes mueven el SL/TP de la cuenta.
Para que eso funcione, la estrategia cumple tres reglas: el stop siempre queda al otro lado del cierre de la
señal (si no, la descarta), el riesgo está acotado en ATR (min_riesgo / max_riesgo) para que el tamaño no
salga absurdo, y todo su estado se guarda en guardar()/cargar() para continuar tras un reinicio.

Sin backtest todavía: los valores por defecto son un punto de partida, no una versión validada.
Pruébala con datos reales de MFP antes de activarla: python3 kcbot.py --probar estrategias/ema_rebote.py
"""
from collections import deque
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from nucleo import COMUN, F, EMA, RSI, TF_MS, Base

ID = "ema_rebote"
NOMBRE = "Rebote en EMA"
VERSION = "1.1"
AUTOR = "Boris y Claude"
DESCRIPCION = ("Retroceso a la EMA de rebote dentro de una tendencia marcada por la EMA lenta. Se arma cuando el "
               "precio se separa de la media, espera el toque y entra cuando una vela confirma el rebote a favor "
               "de la tendencia. Stop tras el extremo del retroceso, objetivo en RR.")

ZONAS = ["America/New_York", "Europe/Madrid", "Europe/London", "UTC"]
HORAS = ["{:02d}:{:02d}".format(h, m) for h in range(24) for m in (0, 30)]

PARAMETROS = [
    # ---- Medias ----
    F("ema_r", "Medias", "EMA de rebote", "int", 20, "La media donde se espera el retroceso.",
      min=2, max=200, env="REB_EMA"),
    F("ema_l", "Medias", "EMA lenta (tendencia)", "int", 200,
      "El cierre y la EMA de rebote tienen que estar del lado de la tendencia.", min=10, max=500,
      env="REB_EMA_LENTA"),
    F("pend_on", "Medias", "Exigir pendiente de la EMA lenta", "bool", True,
      "Largos solo con la EMA lenta subiendo, cortos solo bajando.", env="REB_PEND"),
    F("pend_velas", "Medias", "Velas para medir la pendiente", "int", 10,
      "Compara la EMA lenta con su valor de hace N velas.", min=1, max=100, show_if={"pend_on": True}),
    F("htf_on", "Medias", "Filtro EMA de temporalidad mayor", "bool", False,
      "Largos solo con el cierre por encima, cortos por debajo. Se calcula con velas cerradas.", env="REB_HTF"),
    F("htf_tf", "Medias", "Temporalidad mayor (min)", "select", "60", "", options=["15", "60", "240"],
      show_if={"htf_on": True}, env="REB_HTF_TF"),
    F("htf_len", "Medias", "Periodo EMA de temporalidad mayor", "int", 200, "", min=10, max=500,
      show_if={"htf_on": True}, env="REB_HTF_LEN"),
    # ---- Retroceso ----
    F("sep_atr", "Retroceso", "Separación mínima para armar (ATR)", "float", 1.0,
      "Distancia del cierre a la EMA de rebote antes de que cuente un retroceso.",
      min=0, max=10, step=0.1, env="REB_SEP"),
    F("tol_atr", "Retroceso", "Tolerancia del toque (ATR)", "float", 0.1,
      "0 = la mecha tiene que llegar a la EMA. Con más tolerancia vale quedarse cerca.",
      min=0, max=2, step=0.05, env="REB_TOL"),
    F("pen_atr", "Retroceso", "Penetración máxima (ATR)", "float", 0.5,
      "Si una vela cierra al otro lado de la EMA de rebote más allá de esto, el retroceso se descarta.",
      min=0, max=5, step=0.1, env="REB_PEN"),
    F("max_velas", "Retroceso", "Velas para confirmar tras el toque", "int", 3,
      "0 = solo vale la propia vela del toque.", min=0, max=20, env="REB_VELAS"),
    F("confirm", "Retroceso", "Confirmación del rebote", "select", "vela a favor",
      "Vela a favor: cierra en la dirección de la tendencia y del lado bueno de la EMA. "
      "Rompe la vela anterior: además cierra más allá del máximo (largos) o mínimo (cortos) de la vela previa.",
      options=["vela a favor", "rompe la vela anterior"], env="REB_CONFIRM"),
    # ---- Filtros ----
    COMUN("atr_len", group="Filtros", default=14, help="ATR de Wilder para separación, tolerancia y stop."),
    F("rsi_on", "Filtros", "Exigir RSI a favor", "bool", False,
      "En la vela de rebote: largos con RSI por encima del nivel, cortos por debajo.", env="REB_RSI"),
    F("rsi_len", "Filtros", "Periodo RSI", "int", 14, "", min=2, max=100, show_if={"rsi_on": True}),
    F("rsi_long", "Filtros", "Largos: RSI por encima de", "float", 50, "", min=1, max=99, show_if={"rsi_on": True}),
    F("rsi_short", "Filtros", "Cortos: RSI por debajo de", "float", 50, "", min=1, max=99, show_if={"rsi_on": True}),
    F("reb_tz", "Filtros", "Zona horaria de la ventana y del día", "select", "America/New_York",
      "La usan la ventana horaria y el límite de señales por día. Sigue los cambios de horario de verano.",
      options=ZONAS, env="REB_TZ"),
    F("ses_on", "Filtros", "Solo dentro de una ventana horaria", "bool", False,
      "En la zona horaria elegida. Admite ventanas que cruzan la medianoche.", env="REB_SESION"),
    F("ses_ini", "Filtros", "Inicio de la ventana", "select", "09:30", "", options=HORAS,
      show_if={"ses_on": True}, env="REB_SES_INI"),
    F("ses_fin", "Filtros", "Fin de la ventana", "select", "16:00", "", options=HORAS,
      show_if={"ses_on": True}, env="REB_SES_FIN"),
    F("max_dia", "Filtros", "Señales válidas por día", "int", 0, "0 = sin límite. Día en la zona horaria elegida.",
      min=0, max=20, env="REB_MAX_DIA"),
    # ---- Stop y objetivo ----
    F("sl_mode", "Stop y objetivo", "Colocación del stop", "select", "extremo del retroceso",
      "Extremo del retroceso: tras el mínimo (largos) o máximo (cortos) desde el toque. ATR: distancia fija.",
      options=["extremo del retroceso", "ATR"], env="REB_SL"),
    F("sl_buf", "Stop y objetivo", "Colchón del stop (ATR)", "float", 0.2, "", min=0, max=3, step=0.05,
      show_if={"sl_mode": "extremo del retroceso"}),
    F("sl_atr", "Stop y objetivo", "Stop en ATR", "float", 1.5, "", min=0.2, max=10, step=0.1,
      show_if={"sl_mode": "ATR"}),
    F("min_riesgo", "Stop y objetivo", "Riesgo mínimo (ATR)", "float", 0.3,
      "0 = sin límite. Evita stops tan ajustados que el spread se coma la operación.", min=0, max=5, step=0.1),
    F("max_riesgo", "Stop y objetivo", "Riesgo máximo (ATR)", "float", 3.0,
      "0 = sin límite. Si el retroceso fue muy profundo, el stop queda demasiado lejos.", min=0, max=20, step=0.1),
    F("rr", "Stop y objetivo", "RR", "float", 2.0, "", min=0.5, max=10, step=0.1, env="RR"),
    F("salida_tend", "Stop y objetivo", "Cerrar si se pierde la EMA lenta", "bool", False,
      "Cierra la posición si una vela cierra al otro lado de la EMA lenta.", env="REB_SALIDA_TEND"),
]

AJUSTES_COMUNES = {"be_trigger": {"options": ["R"], "default": "R", "help": "Break-even al alcanzar X R."}}
FILTROS = {"tendencia": "contra la EMA de temporalidad mayor", "rsi": "RSI sin fuerza a favor",
           "sesion": "fuera de la ventana horaria", "riesgo_min": "stop demasiado ajustado",
           "riesgo_max": "stop demasiado amplio", "max_dia": "límite de señales del día"}
SALIDAS = {"TENDENCIA": "cierre al otro lado de la EMA lenta"}
LINEAS = [
    {"key": "ema_r", "nombre": "EMA de rebote", "color": "blue", "estilo": "continua"},
    {"key": "ema_l", "nombre": "EMA lenta", "color": "purple", "estilo": "continua"},
    {"key": "zona", "nombre": "Zona de toque", "color": "muted", "estilo": "discontinua"},
    {"key": "ema_htf", "nombre": "EMA temporalidad mayor", "color": "gold", "estilo": "puntos"},
]
LADOS = {1: "alcista", -1: "bajista"}


def _hm(s):
    h, m = s.split(":")
    return int(h) * 60 + int(m)


def a_utc(t):
    """Hora de la vela en UTC. Acepta epoch en segundos o milisegundos, datetime o texto ISO.
    Sin zona horaria se asume UTC."""
    if isinstance(t, datetime):
        dt = t
    elif isinstance(t, (int, float)):
        dt = datetime.fromtimestamp(t / 1000 if t > 1e11 else t, tz=timezone.utc)
    else:
        dt = datetime.fromisoformat(str(t).replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def validar(P):
    errs = []
    if P["ema_r"] >= P["ema_l"]:
        errs.append("La EMA de rebote tiene que ser más rápida (periodo menor) que la EMA lenta")
    if P["rsi_short"] > P["rsi_long"]:
        errs.append("El nivel de RSI para cortos no puede ser mayor que el de largos")
    if P["ses_on"] and P["ses_ini"] == P["ses_fin"]:
        errs.append("La ventana horaria no puede empezar y terminar a la misma hora")
    if P["min_riesgo"] > 0 and P["max_riesgo"] > 0 and P["min_riesgo"] >= P["max_riesgo"]:
        errs.append("El riesgo mínimo tiene que ser menor que el máximo")
    if P["htf_on"] and int(P["htf_tf"]) * 60_000 <= TF_MS.get(P.get("timeframe"), 300_000):
        errs.append("La temporalidad mayor del filtro tiene que ser mayor que la del bot")
    return errs


def calentamiento(P):
    n = 3 * max(P["ema_r"], P["ema_l"], P["atr_len"], P["rsi_len"]) + P["pend_velas"]
    if P["htf_on"]:
        # La EMA mayor necesita unas tres veces su periodo en velas mayores, contadas en velas del bot.
        base_min = TF_MS.get(P.get("timeframe"), 300_000) // 60_000
        n = max(n, max(1, int(P["htf_tf"]) // base_min) * 3 * P["htf_len"])
    return n


class Estrategia(Base):
    def __init__(self, P, motor):
        super().__init__(P, motor)
        self.er_ind, self.el_ind = EMA(P["ema_r"]), EMA(P["ema_l"])
        self.htf_ind, self.rsi_ind = EMA(P["htf_len"]), RSI(P["rsi_len"])
        self.er = self.el = self.htf = self.rsi = None
        self.hist_el = deque(maxlen=P["pend_velas"] + 1)   # EMA lenta de las últimas velas, para la pendiente
        self.bloque = None                  # vela de temporalidad mayor en curso
        self.cierre_bloque = None
        self.c = self.prev = None
        self.zona = ZoneInfo(P.get("reb_tz") or "America/New_York")
        self.ny = None                      # hora de la vela en la zona horaria elegida
        self.last = {}
        self.reiniciar()

    # --- estado ---
    def reiniciar(self):
        self.lado = 0                       # tendencia: 1 alcista, -1 bajista, 0 sin tendencia
        self.armado = False                 # el precio se ha separado de la EMA y se espera el retroceso
        self.toque = None                   # {"n": velas desde el toque, "ext": extremo del retroceso}
        self.dia = None
        self.n_dia = 0
        self.nota = ""

    def guardar(self):
        return {"lado": self.lado, "armado": self.armado, "toque": self.toque, "dia": self.dia,
                "n_dia": self.n_dia, "nota": self.nota}

    def cargar(self, s):
        self.lado = s.get("lado", 0)
        self.armado = s.get("armado", False)
        self.toque = s.get("toque")
        self.dia = s.get("dia")
        self.n_dia = s.get("n_dia", 0)
        self.nota = s.get("nota", "")

    # --- vela a vela ---
    def actualizar(self, c):
        self.prev, self.c = self.c, c
        utc = a_utc(c[0])
        self.ny = utc.astimezone(self.zona)
        cl = c[4]
        self.er = self.er_ind.update(cl)
        self.el = self.el_ind.update(cl)
        self.rsi = self.rsi_ind.update(cl)
        if self.el is not None:
            self.hist_el.append(self.el)
        # EMA mayor: al empezar una vela mayor nueva se cierra la anterior con su último cierre
        bloque = int(utc.timestamp()) // (int(self.P["htf_tf"]) * 60)
        if self.bloque is not None and bloque != self.bloque and self.cierre_bloque is not None:
            self.htf = self.htf_ind.update(self.cierre_bloque)
        self.bloque, self.cierre_bloque = bloque, cl

    def rr(self):
        return self.P["rr"]

    def objetivo(self, pos):
        return None

    def nivel_be(self, pos):
        return None

    def salida(self, pos):
        if not self.P["salida_tend"] or self.el is None or self.c is None:
            return None
        side = pos.get("side") if isinstance(pos, dict) else getattr(pos, "side", None)
        if side and (self.c[4] - self.el) * side < 0:
            return "TENDENCIA"
        return None

    def _tendencia(self, cl):
        if None in (self.er, self.el):
            return 0
        for side in (1, -1):
            if (cl - self.el) * side <= 0 or (self.er - self.el) * side <= 0:
                continue
            if self.P["pend_on"] and (len(self.hist_el) < self.hist_el.maxlen or
                                      (self.el - self.hist_el[0]) * side <= 0):
                continue
            return side
        return 0

    def _en_sesion(self):
        ahora = self.ny.hour * 60 + self.ny.minute
        ini, fin = _hm(self.P["ses_ini"]), _hm(self.P["ses_fin"])
        return ini <= ahora < fin if ini < fin else (ahora >= ini or ahora < fin)

    def _descartar(self, motivo):
        self.toque, self.armado = None, False
        self.nota = motivo + " Esperando nueva separación."
        return []

    def senales(self):
        P, m = self.P, self.m
        if self.c is None:
            return []
        t, o, h, l, cl = self.c
        atr = m.atr
        lado = self._tendencia(cl)

        # Líneas del panel
        self.last = {}
        if self.er is not None:
            self.last["ema_r"] = self.er
        if self.el is not None:
            self.last["ema_l"] = self.el
        if P["htf_on"] and self.htf is not None:
            self.last["ema_htf"] = self.htf
        if lado and atr is not None and self.er is not None:
            self.last["zona"] = self.er + lado * P["tol_atr"] * atr

        if atr is None or self.er is None or self.el is None:
            return []
        dia = self.ny.date().isoformat()
        if dia != self.dia:
            self.dia, self.n_dia = dia, 0

        # 0) Cambio de tendencia: se olvida cualquier retroceso a medias
        if lado != self.lado:
            self.lado, self.armado, self.toque, self.nota = lado, False, None, ""
        if lado == 0:
            return []
        dist = (cl - self.er) * lado        # > 0: cierre del lado de la tendencia respecto a la EMA de rebote

        # 1) Armado: separación mínima a favor
        if not self.armado:
            if dist >= P["sep_atr"] * atr:
                self.armado, self.nota = True, ""
            return []

        # 2) Toque de la EMA (o seguimiento del retroceso si ya tocó)
        ext = l if lado == 1 else h
        if self.toque is None:
            if (ext - self.er) * lado > P["tol_atr"] * atr:
                return []
            self.toque = {"n": 0, "ext": ext}
        else:
            self.toque["n"] += 1
            if (ext - self.toque["ext"]) * lado < 0:
                self.toque["ext"] = ext

        # 3) Invalidación: cierre demasiado lejos al otro lado de la EMA
        if -dist > P["pen_atr"] * atr:
            return self._descartar("Retroceso descartado: cierre demasiado al otro lado de la EMA {}.".format(
                P["ema_r"]))

        # 4) Confirmación del rebote
        ok = (cl - o) * lado > 0 and dist > 0
        if ok and P["confirm"] == "rompe la vela anterior":
            ref = None if self.prev is None else (self.prev[2] if lado == 1 else self.prev[3])
            ok = ref is not None and (cl - ref) * lado > 0
        if not ok:
            if self.toque["n"] >= P["max_velas"]:
                return self._descartar("Retroceso descartado: sin rebote en {} velas.".format(P["max_velas"] + 1))
            return []

        # 5) Señal
        ext = self.toque["ext"]
        self.toque, self.armado = None, False
        if P["sl_mode"] == "ATR":
            sl = cl - lado * P["sl_atr"] * atr
        else:
            sl = ext - lado * P["sl_buf"] * atr
        riesgo = (cl - sl) * lado
        if riesgo <= 0:
            return self._descartar("Retroceso descartado: stop inválido.")
        sig = {"side": lado, "sl": sl}

        rechazo = None
        if P["htf_on"] and (self.htf is None or (cl - self.htf) * lado <= 0):
            rechazo = "tendencia"
        elif P["rsi_on"] and (self.rsi is None or
                              not (self.rsi > P["rsi_long"] if lado == 1 else self.rsi < P["rsi_short"])):
            rechazo = "rsi"
        elif P["ses_on"] and not self._en_sesion():
            rechazo = "sesion"
        elif P["min_riesgo"] > 0 and riesgo < P["min_riesgo"] * atr:
            rechazo = "riesgo_min"
        elif P["max_riesgo"] > 0 and riesgo > P["max_riesgo"] * atr:
            rechazo = "riesgo_max"
        elif P["max_dia"] > 0 and self.n_dia >= P["max_dia"]:
            rechazo = "max_dia"

        if rechazo:
            sig["rechazo"] = rechazo
            self.nota = "Rebote {} rechazado: {}.".format(LADOS[lado], FILTROS[rechazo])
        else:
            self.n_dia += 1
            self.nota = "Rebote {} válido: entrada en la apertura de la siguiente vela (riesgo {:.1f} pts).".format(
                LADOS[lado], riesgo)
        return [sig]

    # --- panel ---
    def lineas(self):
        return dict(self.last)

    def indicadores(self):
        partes = []
        if self.er is not None:
            partes.append("EMA{} {:.1f}".format(self.P["ema_r"], self.er))
        if self.el is not None:
            partes.append("EMA{} {:.1f}".format(self.P["ema_l"], self.el))
        if self.P["htf_on"] and self.htf is not None:
            partes.append("EMA{}m {:.1f}".format(self.P["htf_tf"], self.htf))
        if self.rsi is not None:
            partes.append("RSI {:.0f}".format(self.rsi))
        return " | ".join(partes)

    def texto(self):
        if self.lado == 0:
            return "Sin tendencia clara respecto a la EMA {}.".format(self.P["ema_l"])
        tend = "Tendencia " + LADOS[self.lado]
        if self.toque is not None:
            return "{}. Retroceso en la EMA {}: esperando el rebote (vela {} de {}).".format(
                tend, self.P["ema_r"], self.toque["n"] + 1, self.P["max_velas"] + 1)
        if self.armado:
            return "{}. Armado: esperando el retroceso a la EMA {}.".format(tend, self.P["ema_r"])
        if self.nota:
            return self.nota
        return "{}. Esperando que el precio se separe de la EMA {}.".format(tend, self.P["ema_r"])
