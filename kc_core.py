# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Borislav Vladimirov Petrakiev
"""
kc_core.py -- Doble canal de Keltner + RSI (v2), con TODO configurable.

Lógica pura, sin red: la usan igual el bot en directo y el backtest del panel, así que
los dos dan las mismas operaciones con las mismas velas.

Regla base (long; el short es el espejo), idéntica a Gold5mKC.py con los valores por defecto:
  1) Una vela sale del canal EXTERIOR con el RSI en sobreventa: el lado queda "armado".
  2) La primera vela posterior que cierra dentro del canal INTERIOR da la señal.
  3) Entrada en la apertura de la vela siguiente. SL bajo la vela anterior. TP = RR x riesgo.
Todo lo demás (confirmación RSI, divergencia, tendencia, horario, TP dinámico, break-even,
salidas por RSI, tiempo, enfriamiento...) está desactivado por defecto.

Supuestos: EMA central, ATR y RSI de Wilder; trabaja solo con velas cerradas; si una vela
toca el SL y otro nivel, se asume primero el SL.
"""
import math
import re
from collections import deque

TF_MS = {"1m": 60_000, "3m": 180_000, "5m": 300_000, "15m": 900_000, "30m": 1_800_000,
         "1h": 3_600_000, "2h": 7_200_000, "4h": 14_400_000}
DEFAULT_SYMBOLS = ["GOLD", "NAS100", "BTC", "CL", "BRENT"]   # instrumentos de partida; se añaden más desde el panel
AUTO_COST = {"BTC": 0.06}           # % del nominal ida y vuelta; el resto 0,01 % (0,005 % por lado)
SYMBOL_RE = re.compile(r"^[A-Z0-9][A-Z0-9._:-]{0,39}$")
RELOAD_KEYS = ("symbol", "timeframe", "tr_mode", "tr_tf", "tr_rsi_len", "tr_ema_len")


def F(key, group, label, typ, default, help="", **kw):
    d = {"key": key, "group": group, "label": label, "type": typ, "default": default, "help": help}
    d.update(kw)
    return d


# Esquema de TODOS los ajustes. El panel construye el formulario a partir de aquí.
# env = nombre de la variable FP_ de versiones anteriores (para importar una configuración vieja).
SCHEMA = [
    # ---- Mercado ----
    F("symbol", "Mercado", "Símbolo", "symbol", "GOLD",
      "Uno de tus instrumentos (se añaden en «Instrumentos»). Al cambiarlo se recarga el historial y se cierra la "
      "operación abierta.", env="SYMBOL"),
    F("timeframe", "Mercado", "Temporalidad", "select", "5m", "Igual que el símbolo: recarga el historial.",
      options=["1m", "3m", "5m", "15m", "30m", "1h"], env="TIMEFRAME"),
    F("direction", "Mercado", "Operar", "select", "ambos", "", options=["ambos", "solo largos", "solo cortos"]),
    F("cost_auto", "Mercado", "Comisión del instrumento", "bool", True,
      "Usa la comisión guardada en la lista de instrumentos (0,01 % ida y vuelta en oro, índices y petróleo; "
      "0,06 % en cripto, salvo que la cambies). Desactívalo para fijar una aquí."),
    F("cost_pct", "Mercado", "Comisión ida y vuelta (%)", "float", 0.01, "% del nominal.", min=0, max=1, step=0.001,
      show_if={"cost_auto": False}),
    F("max_cost", "Mercado", "Coste máximo por operación (R)", "float", 0.15,
      "Descarta la entrada si la comisión supera esta fracción del riesgo. 0 = sin filtro.", min=0, max=5, step=0.01,
      env="MAX_COST_R"),
    # ---- Canales ----
    F("kc_len", "Canales de Keltner", "Longitud de la media", "int", 50, "EMA central.", min=2, max=500, env="KC_LEN"),
    F("atr_len", "Canales de Keltner", "Longitud del ATR", "int", 10, "", min=1, max=200, env="KC_ATR_LEN"),
    F("m_out", "Canales de Keltner", "Multiplicador canal exterior", "float", 3.75, "", min=0.1, max=20, step=0.05,
      env="KC_OUT"),
    F("m_in", "Canales de Keltner", "Multiplicador canal interior", "float", 2.75, "Debe ser menor que el exterior.",
      min=0.1, max=20, step=0.05, env="KC_IN"),
    F("outside", "Canales de Keltner", "Salida del canal exterior por", "select", "mecha",
      "Mecha: basta con tocarlo. Cierre: la vela tiene que cerrar fuera.", options=["mecha", "cierre"], env="OUTSIDE_BY"),
    F("same_candle", "Canales de Keltner", "Señal en la misma vela", "bool", False,
      "La vela que sale del exterior y cierra dentro del interior ya da señal.", env="ALLOW_SAME_CANDLE"),
    F("max_armed", "Canales de Keltner", "Velas de espera de la reentrada", "int", 10,
      "Si no reentra en este número de velas, se descarta.", min=1, max=500, env="MAX_ARMED_BARS"),
    # ---- RSI de la señal ----
    F("rsi_len", "RSI de la señal", "Periodo", "int", 14, "", min=2, max=200, env="RSI_LEN"),
    F("rsi_os", "RSI de la señal", "Sobreventa (arma largos)", "float", 25, "", min=1, max=99, env="RSI_OS"),
    F("rsi_ob", "RSI de la señal", "Sobrecompra (arma cortos)", "float", 75, "", min=1, max=99, env="RSI_OB"),
    # ---- Confirmación ----
    F("rc_on", "Confirmación RSI", "Exigir confirmación", "bool", False,
      "Además de cerrar dentro del canal interior, el RSI tiene que haber salido de la zona extrema. "
      "Si aún no ha salido, el lado sigue armado esperando."),
    F("rc_long", "Confirmación RSI", "Largos: RSI por encima de", "float", 30, "", min=1, max=99, show_if={"rc_on": True}),
    F("rc_short", "Confirmación RSI", "Cortos: RSI por debajo de", "float", 70, "", min=1, max=99, show_if={"rc_on": True}),
    # ---- Divergencia ----
    F("dv_on", "Divergencia RSI", "Exigir divergencia", "bool", False,
      "Largos: el mínimo de esta salida del canal es más bajo que el de la salida anterior, pero el RSI marca "
      "un mínimo más alto. Cortos, al revés."),
    F("dv_lookback", "Divergencia RSI", "Distancia máxima a la salida anterior (velas)", "int", 120, "",
      min=5, max=2000, show_if={"dv_on": True}),
    F("dv_min_rsi", "Divergencia RSI", "Diferencia mínima de RSI", "float", 2.0, "Puntos de RSI.", min=0, max=50,
      step=0.5, show_if={"dv_on": True}),
    # ---- Tendencia ----
    F("tr_mode", "Filtro de tendencia", "Filtro", "select", "no",
      "Solo opera a favor de la tendencia de una temporalidad mayor.", options=["no", "RSI", "EMA", "RSI y EMA"]),
    F("tr_tf", "Filtro de tendencia", "Temporalidad mayor", "select", "1h", "",
      options=["15m", "30m", "1h", "2h", "4h"], show_if={"tr_mode": ["RSI", "EMA", "RSI y EMA"]}),
    F("tr_rsi_len", "Filtro de tendencia", "Periodo del RSI", "int", 14, "", min=2, max=200,
      show_if={"tr_mode": ["RSI", "RSI y EMA"]}),
    F("tr_rsi_long", "Filtro de tendencia", "Largos solo si el RSI mayor es al menos", "float", 50, "", min=0, max=100,
      show_if={"tr_mode": ["RSI", "RSI y EMA"]}),
    F("tr_rsi_short", "Filtro de tendencia", "Cortos solo si el RSI mayor es como mucho", "float", 50, "", min=0,
      max=100, show_if={"tr_mode": ["RSI", "RSI y EMA"]}),
    F("tr_ema_len", "Filtro de tendencia", "Periodo de la EMA", "int", 50,
      "Largos solo con el cierre de la temporalidad mayor por encima de la EMA; cortos, por debajo.",
      min=2, max=500, show_if={"tr_mode": ["EMA", "RSI y EMA"]}),
    # ---- Horario ----
    F("hr_on", "Horario", "Limitar horario de entrada", "bool", False, "Hora local de la Raspberry (Madrid)."),
    F("hr_from", "Horario", "Desde las (h)", "int", 8, "", min=0, max=23, show_if={"hr_on": True}),
    F("hr_to", "Horario", "Hasta las (h)", "int", 22, "Sin incluir. Si es menor que «desde», cruza la medianoche.",
      min=0, max=24, show_if={"hr_on": True}),
    F("hr_weekdays", "Horario", "Solo de lunes a viernes", "bool", False, "", show_if={"hr_on": True}),
    # ---- Stop ----
    F("sl_mode", "Stop", "Colocación del stop", "select", "vela anterior",
      "Vela anterior: bajo el mínimo de la vela anterior (o de la de señal si llegó más lejos). "
      "Extremo: bajo el extremo de la salida del canal. ATR: a un múltiplo de ATR del cierre de la señal.",
      options=["vela anterior", "extremo", "ATR"], env="SL_MODE"),
    F("sl_buf", "Stop", "Margen extra (ATR)", "float", 0.0, "", min=0, max=10, step=0.05, env="SL_BUFFER_ATR",
      show_if={"sl_mode": ["vela anterior", "extremo"]}),
    F("sl_atr", "Stop", "Distancia (ATR)", "float", 1.5, "", min=0.1, max=20, step=0.1, show_if={"sl_mode": "ATR"}),
    F("sl_min_atr", "Stop", "Stop mínimo (ATR)", "float", 0.0, "Descarta señales con el stop más cerca. 0 = sin mínimo.",
      min=0, max=20, step=0.05),
    F("sl_max_atr", "Stop", "Stop máximo (ATR)", "float", 0.0, "Descarta señales con el stop más lejos. 0 = sin máximo.",
      min=0, max=50, step=0.05),
    # ---- Objetivo ----
    F("tp_mode", "Objetivo y salidas", "Objetivo", "select", "RR",
      "RR: fijo a un múltiplo del riesgo. Canal interior / línea central: dinámico, se mueve con el canal "
      "(se usan los niveles de la vela anterior, como una orden límite).",
      options=["RR", "canal interior", "línea central"], env="TP_MODE"),
    F("rr", "Objetivo y salidas", "RR", "float", 2.5, "", min=0.1, max=20, step=0.1, env="RR",
      show_if={"tp_mode": "RR"}),
    F("tp_rsi", "Objetivo y salidas", "Cerrar en el RSI extremo opuesto", "bool", False,
      "Cierra al cierre de una vela con el RSI en sobrecompra (largos) o sobreventa (cortos).", env="TP_RSI"),
    F("ex_on", "Objetivo y salidas", "Salida anticipada por RSI", "bool", False,
      "Cierra al cierre de la vela en la que el RSI cruza el nivel en contra (largos: hacia abajo)."),
    F("ex_long", "Objetivo y salidas", "Nivel para largos", "float", 50, "", min=1, max=99, show_if={"ex_on": True}),
    F("ex_short", "Objetivo y salidas", "Nivel para cortos", "float", 50, "", min=1, max=99, show_if={"ex_on": True}),
    F("max_bars", "Objetivo y salidas", "Duración máxima (velas)", "int", 0, "Cierra al cierre tras este número de velas. 0 = sin límite.",
      min=0, max=5000),
    F("cooldown", "Objetivo y salidas", "Velas de espera tras cerrar", "int", 0, "Sin señales nuevas durante esas velas.",
      min=0, max=1000),
    # ---- Salida dinámica ----
    F("xp_on", "Salida dinámica", "Ampliar en velas grandes", "bool", False,
      "Cuando una vela grande cierra cerca del objetivo sin tocarlo, aleja el objetivo y sube el stop. "
      "Se decide al cierre de la vela: si la vela toca el objetivo, cierra en el objetivo."),
    F("xp_progress", "Salida dinámica", "Recorrido mínimo hasta el objetivo (%)", "float", 75,
      "La vela tiene que cerrar al menos a este % del camino entre la entrada y el objetivo.", min=10, max=99, step=1,
      show_if={"xp_on": True}),
    F("xp_atr", "Salida dinámica", "Tamaño mínimo de la vela (ATR)", "float", 1.5,
      "Del máximo al mínimo, en múltiplos del ATR.", min=0.1, max=20, step=0.1, show_if={"xp_on": True}),
    F("xp_pct", "Salida dinámica", "El objetivo se aleja (%)", "float", 50,
      "Porcentaje de la distancia actual entre la entrada y el objetivo.", min=1, max=500, step=5, show_if={"xp_on": True}),
    F("xp_sl_pct", "Salida dinámica", "El stop sube (%)", "float", 50,
      "Misma referencia que el objetivo. 0 = el stop no se mueve. Nunca se acerca a menos de 0,25 ATR del cierre.",
      min=0, max=500, step=5, show_if={"xp_on": True}),
    F("xp_max", "Salida dinámica", "Ampliaciones máximas por operación", "int", 3, "", min=1, max=50,
      show_if={"xp_on": True}),
    F("ts_on", "Salida dinámica", "Trailing stop por ATR", "bool", False,
      "Con la operación en beneficio, el stop persigue al precio a una distancia fija en ATR. Solo se mueve a favor."),
    F("ts_start_r", "Salida dinámica", "Se activa con un beneficio de (R)", "float", 1.0, "0 = desde la entrada.",
      min=0, max=50, step=0.1, show_if={"ts_on": True}),
    F("ts_atr", "Salida dinámica", "Distancia del stop (ATR)", "float", 2.0, "", min=0.1, max=20, step=0.1,
      show_if={"ts_on": True}),
    F("ts_ref", "Salida dinámica", "Medido desde", "select", "extremo",
      "Extremo: máximo de la vela (largos) o mínimo (cortos). Cierre: el cierre de la vela.",
      options=["extremo", "cierre"], show_if={"ts_on": True}),
    F("ts_no_tp", "Salida dinámica", "Quitar el objetivo al activarse", "bool", False,
      "La operación solo se cierra por el trailing o por las salidas al cierre (RSI, tiempo).", show_if={"ts_on": True}),
    # ---- Break-even ----
    F("be_on", "Break-even y venta parcial", "Activar", "bool", False,
      "Al llegar al disparador vende una parte y sube el stop a la entrada.", env="BE_ON"),
    F("be_trigger", "Break-even y venta parcial", "Disparador", "select", "línea central", "",
      options=["línea central", "R"], env="BE_TRIGGER", show_if={"be_on": True}),
    F("be_r", "Break-even y venta parcial", "Disparador en R", "float", 1.0, "", min=0.1, max=20, step=0.1, env="BE_R",
      show_if={"be_on": True, "be_trigger": "R"}),
    F("be_pct", "Break-even y venta parcial", "Porcentaje que se vende", "float", 10, "0 = solo mover el stop.",
      min=0, max=100, step=1, env="BE_PCT", show_if={"be_on": True}),
    F("be_off", "Break-even y venta parcial", "Stop nuevo: entrada + (R)", "float", 0.0,
      "0 = break-even exacto.", min=-1, max=5, step=0.05, env="BE_OFFSET_R", show_if={"be_on": True}),
    # ---- Límites de la estrategia ----
    F("max_trades", "Límites diarios de la estrategia", "Operaciones al día", "int", 10, "0 = sin límite. Pausan el resto del día.",
      min=0, max=1000, env="MAX_TRADES_DAY"),
    F("max_consec", "Límites diarios de la estrategia", "Pérdidas seguidas", "int", 3, "0 = sin límite.", min=0, max=100,
      env="MAX_CONSEC_LOSSES"),
    F("max_profit_r", "Límites diarios de la estrategia", "Ganancia diaria (R)", "float", 10, "0 = sin límite.", min=0,
      max=1000, step=0.5, env="MAX_DAILY_PROFIT_R"),
    F("max_loss_r", "Límites diarios de la estrategia", "Pérdida diaria (R)", "float", 3.0, "0 = sin límite.", min=0,
      max=1000, step=0.5, env="MAX_DAILY_LOSS_R"),
    F("utc_off", "Límites diarios de la estrategia", "El día empieza a las 00:00 UTC +", "float", -4,
      "-4 = Nueva York en horario de verano; -5 en invierno (desde el 1 de noviembre).", min=-12, max=14, step=1,
      env="DAY_RESET_UTC_OFFSET"),
    # ---- Guardián ----
    F("risk_usd", "Guardián (detiene el bot)", "Riesgo por operación ($)", "float", 0.0,
      "Para ver resultados en dinero. 0 = solo R.", min=0, max=1e7, step=10, env="RISK_USD"),
    F("g_unit", "Guardián (detiene el bot)", "Unidad de los límites", "select", "R", "", options=["R", "USD"], env="G_UNIT"),
    F("g_day_loss", "Guardián (detiene el bot)", "Pérdida diaria", "float", 0.0, "0 = sin límite.", min=0, max=1e7, step=0.5,
      env="G_DAY_LOSS"),
    F("g_day_profit", "Guardián (detiene el bot)", "Ganancia diaria", "float", 0.0, "0 = sin límite.", min=0, max=1e7,
      step=0.5, env="G_DAY_PROFIT"),
    F("g_week_loss", "Guardián (detiene el bot)", "Pérdida semanal", "float", 0.0, "0 = sin límite.", min=0, max=1e7,
      step=0.5, env="G_WEEK_LOSS"),
    F("g_week_profit", "Guardián (detiene el bot)", "Ganancia semanal", "float", 0.0, "0 = sin límite.", min=0, max=1e7,
      step=0.5, env="G_WEEK_PROFIT"),
    F("g_action", "Guardián (detiene el bot)", "Al alcanzar un límite", "select", "parar",
      "Parar: sin entradas hasta que lo reactives. Pausa: sigue al empezar el día o la semana siguiente.",
      options=["parar", "pausa"], env="G_ACTION"),
]
BY_KEY = {f["key"]: f for f in SCHEMA}
# Valores antiguos de los archivos de configuración -> valores actuales
LEGACY_VALUES = {"sl_mode": {"prev": "vela anterior", "excursion": "extremo"},
                 "outside": {"wick": "mecha", "close": "cierre"},
                 "tp_mode": {"rr": "RR", "canal": "canal interior"},
                 "be_trigger": {"basis": "línea central", "r": "R"}}


def defaults():
    return {f["key"]: f["default"] for f in SCHEMA}


def _coerce(f, v):
    t = f["type"]
    if t == "bool":
        if isinstance(v, str):
            return v.strip().lower() in ("1", "true", "si", "sí", "yes", "on")
        return bool(v)
    if t == "int":
        return int(round(float(v)))
    if t == "float":
        x = float(v)
        if math.isnan(x) or math.isinf(x):
            raise ValueError
        return x
    if t == "symbol":
        v = str(v).strip().upper()
        if not SYMBOL_RE.match(v):
            raise ValueError
        return v
    v = str(v)
    v = LEGACY_VALUES.get(f["key"], {}).get(v, v)
    if v not in f["options"]:
        raise ValueError
    return v


def validate(raw):
    """Devuelve (params completos, lista de errores en texto)."""
    P, errs = defaults(), []
    for k, v in (raw or {}).items():
        f = BY_KEY.get(k)
        if f is None:
            continue
        try:
            x = _coerce(f, v)
        except (TypeError, ValueError):
            errs.append("{}: valor no válido".format(f["label"]))
            continue
        if f["type"] in ("int", "float"):
            if "min" in f and x < f["min"] or "max" in f and x > f["max"]:
                errs.append("{}: debe estar entre {:g} y {:g}".format(f["label"], f.get("min", -1e18), f.get("max", 1e18)))
                continue
        P[k] = x
    if P["m_in"] >= P["m_out"]:
        errs.append("El canal interior tiene que ser más estrecho que el exterior")
    if P["rsi_os"] >= P["rsi_ob"]:
        errs.append("La sobreventa tiene que ser menor que la sobrecompra")
    if P["tr_mode"] != "no" and TF_MS[P["tr_tf"]] <= TF_MS[P["timeframe"]]:
        errs.append("La temporalidad del filtro de tendencia tiene que ser mayor que la del bot")
    if P["g_unit"] == "USD" and P["risk_usd"] <= 0:
        errs.append("Para límites en USD indica el riesgo por operación")
    if P["hr_on"] and P["hr_from"] == P["hr_to"] % 24:
        errs.append("El horario no puede empezar y terminar a la misma hora")
    return P, errs


def from_env(vals):
    """Convierte un config.env antiguo (FP_*) en ajustes v2."""
    raw = {}
    for f in SCHEMA:
        name = f.get("env")
        if name and ("FP_" + name) in vals and vals["FP_" + name] != "":
            raw[f["key"]] = vals["FP_" + name]
    P, errs = validate(raw)
    return P, errs, sorted(raw)


def cost_pct(P, instrument_cost=None):
    if not P["cost_auto"]:
        return P["cost_pct"]
    return instrument_cost if instrument_cost is not None else AUTO_COST.get(P["symbol"], 0.01)


# ---------------------------- indicadores incrementales ----------------------------
class EMAInc:
    def __init__(s, n):
        s.n, s.k, s.buf, s.v = n, 2 / (n + 1), [], None

    def update(s, x):
        if s.v is None:
            s.buf.append(x)
            if len(s.buf) == s.n:
                s.v = sum(s.buf) / s.n
        else:
            s.v = x * s.k + s.v * (1 - s.k)
        return s.v


class RMAInc:  # media de Wilder
    def __init__(s, n):
        s.n, s.buf, s.v = n, [], None

    def update(s, x):
        if s.v is None:
            s.buf.append(x)
            if len(s.buf) == s.n:
                s.v = sum(s.buf) / s.n
        else:
            s.v = (s.v * (s.n - 1) + x) / s.n
        return s.v


class RSIInc:
    def __init__(s, n):
        s.g, s.l, s.prev = RMAInc(n), RMAInc(n), None

    def update(s, x):
        if s.prev is None:
            s.prev = x
            return None
        d, s.prev = x - s.prev, x
        g, l = s.g.update(max(d, 0)), s.l.update(max(-d, 0))
        if g is None:
            return None
        if l == 0:
            return 50.0 if g == 0 else 100.0
        return 100 - 100 / (1 + g / l)


class HTF:
    """Temporalidad mayor construida a partir de las velas del bot (sin mirar el futuro):
    sus valores solo cambian cuando una vela mayor está completa."""

    def __init__(self, P, base_ms):
        self.ms, self.base_ms = TF_MS[P["tr_tf"]], base_ms
        self.rsi, self.ema = RSIInc(P["tr_rsi_len"]), EMAInc(P["tr_ema_len"])
        self.bucket, self.agg, self.last_bucket = None, None, None
        self.v_rsi = self.v_ema = self.v_close = None
        self.bars = 0

    def _feed(self, close):
        self.v_rsi, self.v_ema, self.v_close = self.rsi.update(close), self.ema.update(close), close
        self.bars += 1

    def seed(self, bars):
        """Velas mayores ya cerradas (t, o, h, l, c), en orden."""
        for b in bars:
            self._feed(b[4])
            self.last_bucket = b[0] // self.ms

    def on_base(self, c):
        t = c[0]
        b = t // self.ms
        if self.last_bucket is not None and b <= self.last_bucket:
            return
        if self.agg is not None and b != self.bucket:      # hueco: se cierra la vela mayor anterior
            self._feed(self.agg)
            self.last_bucket, self.agg = self.bucket, None
        if self.agg is None:
            self.bucket = b
        self.agg = c[4]                                      # solo importa el cierre de la vela mayor
        if t + self.base_ms >= (b + 1) * self.ms:            # última vela de la vela mayor
            self._feed(self.agg)
            self.last_bucket, self.agg = b, None

    def ok(self, P, side):
        m = P["tr_mode"]
        if m in ("RSI", "RSI y EMA"):
            if self.v_rsi is None:
                return False
            if side == 1 and self.v_rsi < P["tr_rsi_long"] or side == -1 and self.v_rsi > P["tr_rsi_short"]:
                return False
        if m in ("EMA", "RSI y EMA"):
            if self.v_ema is None:
                return False
            if side == 1 and self.v_close <= self.v_ema or side == -1 and self.v_close >= self.v_ema:
                return False
        return True


# ---------------------------- estrategia ----------------------------
FILTER_NAMES = {"direction": "dirección", "cooldown": "espera tras cerrar", "hours": "horario",
                "trend": "tendencia", "divergence": "sin divergencia", "sl_min": "stop demasiado cerca",
                "sl_max": "stop demasiado lejos"}


class Strat:
    """Recibe velas cerradas una a una (igual en backtest y en directo)."""

    def __init__(self, P, hour_fn=None, instrument_cost=None):
        self.P = P
        self.cost = cost_pct(P, instrument_cost)
        self.tf_ms = TF_MS[P["timeframe"]]
        self.hour_fn = hour_fn or (lambda t: ((t // 3_600_000) % 24, ((t // 86_400_000) + 3) % 7))
        self.basis, self.atr, self.rsi = EMAInc(P["kc_len"]), RMAInc(P["atr_len"]), RSIInc(P["rsi_len"])
        self.htf = HTF(P, self.tf_ms) if P["tr_mode"] != "no" else None
        self.prev = None
        self.prev_rsi = None
        self.lv = None                       # niveles de la vela anterior (para TP dinámico y BE)
        self.armed = {1: None, -1: None}
        self.pending = None
        self.pos = None
        self.i = -1
        self.last = {}
        self.cool = 0
        self.exc = {1: None, -1: None}       # salida del canal en curso
        self.exc_hist = {1: deque(maxlen=50), -1: deque(maxlen=50)}
        self.reset_day_state()
        self.block_days = {}
        self.days_seen = 0
        self.skipped = 0
        self.filtered = {}

    def reset_day_state(self):
        self.day, self.dtrades, self.dconsec, self.dr, self.blocked = None, 0, 0, 0.0, None

    # --- estado para guardar / recuperar ---
    def dump(self):
        return {"pos": self.pos, "pending": self.pending,
                "armed": {"1": self.armed[1], "-1": self.armed[-1]}, "day": self.day,
                "dtrades": self.dtrades, "dconsec": self.dconsec, "dr": self.dr, "blocked": self.blocked,
                "cool": self.cool}

    def load(self, s):
        self.pos, self.pending = s.get("pos"), s.get("pending")
        a = s.get("armed") or {}
        self.armed = {1: a.get("1"), -1: a.get("-1")}
        self.day, self.dtrades, self.dconsec = s.get("day"), s.get("dtrades", 0), s.get("dconsec", 0)
        self.dr, self.blocked, self.cool = s.get("dr", 0.0), s.get("blocked"), s.get("cool", 0)

    def seed_htf(self, bars):
        if self.htf:
            self.htf.seed(bars)

    # --- internos ---
    def _block(self, t, cat, text, ev):
        if not self.blocked:
            self.blocked = text
            self.block_days[cat] = self.block_days.get(cat, 0) + 1
            ev.append(("blocked", t, text))

    def _check_limits(self, t, ev):
        P = self.P
        if P["max_loss_r"] and self.dr <= -P["max_loss_r"]:
            self._block(t, "pérdida diaria", "pérdida diaria {:+.1f}R".format(self.dr), ev)
        elif P["max_profit_r"] and self.dr >= P["max_profit_r"]:
            self._block(t, "ganancia diaria", "ganancia diaria {:+.1f}R".format(self.dr), ev)
        elif P["max_consec"] and self.dconsec >= P["max_consec"]:
            self._block(t, "pérdidas seguidas", "{} pérdidas seguidas".format(self.dconsec), ev)
        elif P["max_trades"] and self.dtrades >= P["max_trades"]:
            self._block(t, "máx. operaciones", "{} operaciones del día".format(self.dtrades), ev)

    def _arm(self, side, a, out, rsi_ok, l, h, rsi):
        ext = l if side == 1 else h
        if out:
            if a:
                a["ext"] = min(a["ext"], ext) if side == 1 else max(a["ext"], ext)
                a["rx"] = min(a.get("rx", rsi), rsi) if side == 1 else max(a.get("rx", rsi), rsi)
                a["age"] = 0
            elif rsi_ok:
                a = {"ext": ext, "age": 0, "rx": rsi, "start": self.i}
        elif a:
            a["age"] += 1
            if a["age"] > self.P["max_armed"]:
                a = None
        return a

    def _stop(self, side, a, l, h, cl, atr):
        P = self.P
        if P["sl_mode"] == "ATR":
            return cl - side * P["sl_atr"] * atr
        if P["sl_mode"] == "extremo":
            base = a["ext"]
        else:
            base = min(self.prev[3], l) if side == 1 else max(self.prev[2], h)
        return base - side * P["sl_buf"] * atr

    def _filter(self, t, side, a, cl, sl, atr):
        P = self.P
        if P["direction"] == "solo largos" and side == -1 or P["direction"] == "solo cortos" and side == 1:
            return "direction"
        if self.cool > 0:
            return "cooldown"
        if P["hr_on"]:
            hour, wd = self.hour_fn(t + self.tf_ms)
            f, to = P["hr_from"], P["hr_to"] % 24
            inside = f <= hour < to if f < to else (hour >= f or hour < to)
            if not inside or (P["hr_weekdays"] and wd >= 5):
                return "hours"
        if self.htf and not self.htf.ok(P, side):
            return "trend"
        if P["dv_on"]:
            prev = None
            for e in reversed(self.exc_hist[side]):
                if e[0] < a.get("start", self.i):
                    prev = e
                    break
            if prev is None or a.get("start", self.i) - prev[0] > P["dv_lookback"]:
                return "divergence"
            _, p_ext, p_rx = prev
            rx = a.get("rx", 50.0)
            if side == 1 and not (a["ext"] < p_ext and rx >= p_rx + P["dv_min_rsi"]):
                return "divergence"
            if side == -1 and not (a["ext"] > p_ext and rx <= p_rx - P["dv_min_rsi"]):
                return "divergence"
        dist = (cl - sl) * side
        if P["sl_min_atr"] and dist < P["sl_min_atr"] * atr:
            return "sl_min"
        if P["sl_max_atr"] and dist > P["sl_max_atr"] * atr:
            return "sl_max"
        return None

    def _r(self, p, px):
        part = p.get("part")
        if part:
            f, pp = part
            gross = (f * (pp - p["entry"]) + (1 - f) * (px - p["entry"])) * p["side"]
        else:
            gross = (px - p["entry"]) * p["side"]
        return (gross - p["entry"] * self.cost / 100) / p["dist"]

    @staticmethod
    def _sl_why(p):
        if p.get("trail_moved"):
            return "TRAILING"
        if p.get("sl_moved"):
            return "SL MOVIDO"
        return "BE" if p["be"] else "SL"

    def _dynamic(self, t, h, l, cl, atr, ev):
        """Al cierre de la vela: trailing stop y ampliación en velas grandes (afectan desde la vela siguiente)."""
        P, p = self.P, self.pos
        s = p["side"]
        if P["ts_on"]:
            if not p.get("trail") and (cl - p["entry"]) * s / p["dist"] >= P["ts_start_r"]:
                p["trail"] = True
                if P["ts_no_tp"]:
                    p["no_tp"], p["tp"] = True, None
                ev.append(("trail_on", t, s))
            if p.get("trail"):
                ref = (h if s == 1 else l) if P["ts_ref"] == "extremo" else cl
                nsl = ref - s * P["ts_atr"] * atr
                if (nsl - p["sl"]) * s > 0 and (cl - nsl) * s > 0:
                    p["sl"], p["trail_moved"] = nsl, True
        if P["xp_on"] and p.get("tp") is not None and p.get("ext_n", 0) < P["xp_max"]:
            D = (p["tp"] - p["entry"]) * s
            if D > 0 and (cl - p["entry"]) * s >= P["xp_progress"] / 100 * D and (h - l) >= P["xp_atr"] * atr:
                add = P["xp_pct"] / 100 * D
                p["ext"] = p.get("ext", 0.0) + add
                p["ext_n"] = p.get("ext_n", 0) + 1
                p["tp"] += s * add
                if P["xp_sl_pct"]:
                    nsl = p["sl"] + s * P["xp_sl_pct"] / 100 * D
                    lim = cl - s * 0.25 * atr
                    if (nsl - lim) * s > 0:
                        nsl = lim
                    if (nsl - p["sl"]) * s > 0:
                        p["sl"], p["sl_moved"] = nsl, True
                ev.append(("extend", t, s, p["tp"], p["sl"], p["ext_n"]))

    def _close(self, t, px, why, ev):
        p = self.pos
        r = self._r(p, px)
        part = p.get("part")
        ev.append(("exit", t, {
            "side": p["side"], "t_open": p["t_open"], "t_close": t, "entry": p["entry"], "exit": px,
            "sl": p["sl0"], "sl_final": p["sl"], "tp": p.get("tp"), "r": r, "why": why,
            "stop_pct": 100 * p["dist"] / p["entry"], "bars": p["bars"],
            "part_pct": round(part[0] * 100, 2) if part else 0, "part_px": part[1] if part else None,
            "ext_n": p.get("ext_n", 0), "trail": bool(p.get("trail"))}))
        self.pos = None
        self.dr += r
        self.dconsec = self.dconsec + 1 if r < 0 else 0
        self.cool = self.P["cooldown"] + 1 if self.P["cooldown"] else 0
        self._check_limits(t, ev)

    def _dyn_tp(self, side):
        P, lv = self.P, self.lv
        if not lv:
            return None
        if P["tp_mode"] == "canal interior":
            return lv["up_i"] if side == 1 else lv["lo_i"]
        return lv["basis"]

    # --- vela a vela ---
    def on_candle(self, c):
        P = self.P
        t, o, h, l, cl = c
        self.i += 1
        ev = []
        day = (t + int(P["utc_off"] * 3_600_000)) // 86_400_000
        if day != self.day:
            self.day, self.dtrades, self.dconsec, self.dr, self.blocked = day, 0, 0, 0.0, None
            self.days_seen += 1
        if self.cool > 0 and not self.pos:
            self.cool -= 1
        prev = self.prev
        tr = h - l if prev is None else max(h - l, abs(h - prev[4]), abs(l - prev[4]))
        basis, atr, rsi = self.basis.update(cl), self.atr.update(tr), self.rsi.update(cl)
        if self.htf:
            self.htf.on_base(c)

        # 1) entrada pendiente: apertura de esta vela
        if self.pending and not self.pos:
            side, sl = self.pending["side"], self.pending["sl"]
            self.pending = None
            dist = (o - sl) if side == 1 else (sl - o)
            cost_r = self.cost / 100 * o / dist if dist > 0 else 0
            if dist > 0 and P["max_cost"] and cost_r > P["max_cost"]:
                self.skipped += 1
                ev.append(("skip", t, cost_r))
            elif dist > 0:
                tp = o + side * P["rr"] * dist if P["tp_mode"] == "RR" else None
                self.pos = {"side": side, "entry": o, "sl": sl, "sl0": sl, "tp": tp, "dist": dist, "i": self.i,
                            "t_open": t, "bars": 0, "be": False, "part": None, "ext": 0.0, "ext_n": 0,
                            "trail": False, "no_tp": False, "sl_moved": False}
                if tp is None:
                    self.pos["tp"] = self._dyn_tp(side)
                self.dtrades += 1
                ev.append(("open", t, side, o, sl, self.pos["tp"]))
            else:
                ev.append(("skip_gap", t, side))

        # 2) gestión intravela (si la vela toca el SL y otro nivel, se asume el SL)
        if self.pos:
            p = self.pos
            s = p["side"]
            if P["tp_mode"] != "RR":
                tp = self._dyn_tp(s)
                if tp is not None:
                    tp += s * p.get("ext", 0.0)
                p["tp"] = tp if tp is not None and (tp - p["entry"]) * s > 0 and not p.get("no_tp") else None
            hit_sl = l <= p["sl"] if s == 1 else h >= p["sl"]
            hit_tp = p["tp"] is not None and (h >= p["tp"] if s == 1 else l <= p["tp"])
            if hit_sl:
                self._close(t, p["sl"], self._sl_why(p), ev)
            elif hit_tp:
                px = p["tp"]
                if P["tp_mode"] != "RR" and (o - px) * s > 0:
                    px = o                       # abrió más allá del objetivo: se llena a la apertura
                self._close(t, px, "TP", ev)
            elif P["be_on"] and not p["be"]:
                trig = (self.lv["basis"] if self.lv else None) if P["be_trigger"] == "línea central" \
                    else p["entry"] + s * P["be_r"] * p["dist"]
                if trig is not None and (trig - p["entry"]) * s > 0 and (h >= trig if s == 1 else l <= trig):
                    px = o if (o - trig) * s > 0 else trig
                    f = P["be_pct"] / 100
                    if f > 0:
                        p["part"] = (f, px)
                    p["be"] = True
                    p["sl"] = p["entry"] + s * P["be_off"] * p["dist"]
                    ev.append(("partial", t, s, px, f, p["sl"]))
                    if (l <= p["sl"] if s == 1 else h >= p["sl"]) and (px - p["sl"]) * s > 0:
                        self._close(t, p["sl"], "BE", ev)

        # 3) salidas al cierre de la vela
        if self.pos and rsi is not None:
            p = self.pos
            s = p["side"]
            why = None
            if P["tp_rsi"] and (rsi >= P["rsi_ob"] if s == 1 else rsi <= P["rsi_os"]):
                why = "RSI"
            elif P["ex_on"] and self.prev_rsi is not None and (
                    (self.prev_rsi >= P["ex_long"] > rsi) if s == 1 else (self.prev_rsi <= P["ex_short"] < rsi)):
                why = "RSI EN CONTRA"
            elif P["max_bars"] and p["bars"] + 1 >= P["max_bars"]:
                why = "TIEMPO"
            if why:
                self._close(t, cl, why, ev)
        if self.pos and atr is not None:
            self._dynamic(t, h, l, cl, atr, ev)
        if self.pos:
            self.pos["bars"] += 1

        # 4) señales
        if None not in (basis, atr, rsi) and prev is not None:
            up_o, lo_o = basis + P["m_out"] * atr, basis - P["m_out"] * atr
            up_i, lo_i = basis + P["m_in"] * atr, basis - P["m_in"] * atr
            px_lo = l if P["outside"] == "mecha" else cl
            px_hi = h if P["outside"] == "mecha" else cl
            self.last = {"close": cl, "rsi": rsi, "lo_o": lo_o, "up_o": up_o, "lo_i": lo_i, "up_i": up_i,
                         "basis": basis, "atr": atr}
            if self.htf:
                self.last.update({"htf_rsi": self.htf.v_rsi, "htf_ema": self.htf.v_ema, "htf_close": self.htf.v_close})
            for side in (1, -1):
                a = self.armed[side]
                out = px_lo < lo_o if side == 1 else px_hi > up_o
                rsi_ok = rsi < P["rsi_os"] if side == 1 else rsi > P["rsi_ob"]
                back = cl > lo_i if side == 1 else cl < up_i
                if P["rc_on"]:
                    back = back and (rsi > P["rc_long"] if side == 1 else rsi < P["rc_short"])
                fired = False
                if P["same_candle"]:
                    a = self._arm(side, a, out, rsi_ok, l, h, rsi)
                    fired = bool(a and back)
                elif a and back:
                    fired = True
                else:
                    a = self._arm(side, a, out, rsi_ok, l, h, rsi)
                if fired:
                    sl = self._stop(side, a, l, h, cl, atr)
                    if not self.pos and not self.pending:
                        self._check_limits(t, ev)
                        if not self.blocked:
                            why = self._filter(t, side, a, cl, sl, atr)
                            if why:
                                self.filtered[why] = self.filtered.get(why, 0) + 1
                                ev.append(("filtered", t, side, why))
                            else:
                                self.pending = {"side": side, "sl": sl}
                                ev.append(("signal", t, side, cl, sl))
                    a = None
                self.armed[side] = a
                # salidas del canal (para la divergencia)
                ext = l if side == 1 else h
                cur = self.exc[side]
                if out:
                    if cur is None:
                        cur = {"ext": ext, "rx": rsi}
                    else:
                        cur["ext"] = min(cur["ext"], ext) if side == 1 else max(cur["ext"], ext)
                        cur["rx"] = min(cur["rx"], rsi) if side == 1 else max(cur["rx"], rsi)
                    self.exc[side] = cur
                elif cur is not None:
                    self.exc_hist[side].append((self.i - 1, cur["ext"], cur["rx"]))
                    self.exc[side] = None
            self.lv = {"up_i": up_i, "lo_i": lo_i, "basis": basis}
        self.prev = c
        self.prev_rsi = rsi
        return ev


# ---------------------------- métricas y backtest ----------------------------
def metrics(trades):
    n = len(trades)
    m = {"n": n, "wins": 0, "losses": 0, "winrate": 0.0, "total_r": 0.0, "avg_r": 0.0, "avg_win": 0.0,
         "avg_loss": 0.0, "pf": None, "max_dd": 0.0, "cur_dd": 0.0, "best": 0.0, "worst": 0.0,
         "max_win_streak": 0, "max_loss_streak": 0, "cur_streak": 0, "std": 0.0, "sqn": None,
         "avg_dur": 0.0, "why": {}, "partials": 0, "extended": 0}
    if not n:
        return m
    rs = [x["r"] for x in trades]
    wins = [r for r in rs if r > 0]
    losses = [r for r in rs if r <= 0]
    tot = sum(rs)
    avg = tot / n
    cum = peak = dd = 0.0
    ws = ls = mws = mls = 0
    for r in rs:
        cum += r
        peak = max(peak, cum)
        dd = max(dd, peak - cum)
        if r > 0:
            ws, ls = ws + 1, 0
        else:
            ls, ws = ls + 1, 0
        mws, mls = max(mws, ws), max(mls, ls)
    std = math.sqrt(sum((r - avg) ** 2 for r in rs) / (n - 1)) if n > 1 else 0.0
    why = {}
    for x in trades:
        why[x["why"]] = why.get(x["why"], 0) + 1
    m.update({
        "wins": len(wins), "losses": len(losses), "winrate": 100.0 * len(wins) / n, "total_r": tot, "avg_r": avg,
        "avg_win": sum(wins) / len(wins) if wins else 0.0, "avg_loss": sum(losses) / len(losses) if losses else 0.0,
        "pf": (sum(wins) / -sum(losses)) if losses and sum(losses) < 0 else None,
        "max_dd": dd, "cur_dd": peak - cum, "best": max(rs), "worst": min(rs),
        "max_win_streak": mws, "max_loss_streak": mls, "cur_streak": ws if ws else -ls,
        "std": std, "sqn": (math.sqrt(n) * avg / std) if std > 0 else None,
        "avg_dur": sum(x.get("dur", 0) for x in trades) / n, "why": why,
        "partials": sum(1 for x in trades if x.get("part_pct")),
        "extended": sum(1 for x in trades if x.get("ext_n"))})
    return m


def backtest(cs, P, hour_fn=None, htf_seed=None, warm=0, instrument_cost=None):
    """cs: velas (t, o, h, l, c). Las primeras `warm` solo calientan indicadores."""
    st = Strat(P, hour_fn, instrument_cost)
    if htf_seed:
        st.seed_htf([b for b in htf_seed if b[0] + TF_MS[P["tr_tf"]] <= cs[0][0]] if cs else htf_seed)
    tf = TF_MS[P["timeframe"]]
    trades, signals = [], 0
    for k, c in enumerate(cs):
        for e in st.on_candle(c):
            if k < warm:
                continue
            if e[0] == "signal":
                signals += 1
            elif e[0] == "exit":
                tr = dict(e[2])
                tr["dur"] = round((tr["t_close"] + tf - tr["t_open"]) / 60000, 1)
                trades.append(tr)
        if k == warm - 1:
            st.pos = st.pending = None
            st.armed = {1: None, -1: None}
            st.reset_day_state()
            st.block_days, st.filtered, st.skipped = {}, {}, 0
    return trades, {"signals": signals, "filtered": dict(st.filtered), "skipped_cost": st.skipped,
                    "block_days": dict(st.block_days), "days": st.days_seen}
