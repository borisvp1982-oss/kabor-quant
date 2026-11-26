# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Borislav Vladimirov Petrakiev
"""
nucleo.py -- El sistema común de los bots v4. NO contiene ninguna estrategia.

Una estrategia es un archivo .py en la carpeta estrategias/. El núcleo la carga, junta sus ajustes con
los comunes y se encarga de todo lo demás, igual en directo y en el backtest:

  * entrada en la apertura de la vela siguiente a la señal, comisión y coste máximo,
  * filtros comunes (dirección, espera tras cerrar, horario, tendencia de una temporalidad mayor,
    stop demasiado cerca o lejos),
  * gestión de la operación: stop, objetivo fijo / RR / dinámico, break-even y venta parcial,
    trailing stop, ampliación en velas grandes, duración máxima,
  * límites diarios de la estrategia y guardián (estos dos los usa kcbot.py).

La estrategia solo decide CUÁNDO entrar, DÓNDE va el stop y, si quiere, el objetivo y salidas propias.
Cómo escribir una: ESTRATEGIAS.md.

Supuestos: solo velas cerradas; ATR de Wilder; si una vela toca el SL y otro nivel, primero el SL.
"""
import hashlib
import importlib.util
import math
import os
import random
import re
import shutil
import threading
import time
import traceback

from contexto import Contexto

NUCLEO_VERSION = "5.1"
API_VERSION = 1                    # versión de la interfaz de estrategias
TF_MS = {"1m": 60_000, "3m": 180_000, "5m": 300_000, "15m": 900_000, "30m": 1_800_000,
         "1h": 3_600_000, "2h": 7_200_000, "4h": 14_400_000, "1d": 86_400_000, "1w": 604_800_000}
TF_BOT = ["1m", "3m", "5m", "15m", "30m", "1h", "2h", "4h", "1d", "1w"]   # temporalidades para un bot
DIA, SEMANA = 86_400_000, 604_800_000
LUNES = 4 * DIA                    # el 1-1-1970 fue jueves: las semanas empiezan el lunes a las 00:00 UTC
AGREGADAS = {"1d": "4h", "1w": "4h"}   # se construyen en KaBor con velas de 4h (días UTC, semanas de lunes)
MIN_VELAS = {"1d": 120, "1w": 40}      # velas mínimas para arrancar un bot (en el resto, 200)
CALENTAR = {"1d": 400, "1w": 150}      # velas que descarga un bot al arrancar (en el resto, 600)


def inicio_vela(t, ms):
    """Apertura de la vela de duración ms que contiene t (UTC). Las semanas empiezan el lunes."""
    if ms == SEMANA:
        return t - (t - LUNES) % SEMANA
    return t - t % ms


def agrupar_velas(velas, ms, base_ms, ahora=None, margen=15 * 60_000):
    """Velas de ms construidas con velas (t, o, h, l, c) de base_ms. Quita la primera si empieza a medias y, con
    «ahora», las que aún no han cerrado. Una vela ya cerrada se da por completa si llegó su última vela de base o si
    han pasado 15 min desde el cierre (mercado parado en ese tramo)."""
    out, cur, k, ult = [], None, None, None
    for t, o, h, l, c in velas:
        b = inicio_vela(t, ms)
        if b != k:
            if cur:
                out.append((cur, ult))
            cur, k = [b, o, h, l, c], b
            cur.append(t == b)       # empieza en su apertura
        else:
            cur[2], cur[3], cur[4] = max(cur[2], h), min(cur[3], l), c
        ult = t
    if cur:
        out.append((cur, ult))
    res = []
    for i, (v, ult) in enumerate(out):
        if i == 0 and not v[5]:
            continue
        fin = v[0] + ms
        if ahora is not None and (fin > ahora or (ult + base_ms < fin and ahora < fin + margen)):
            continue
        res.append(tuple(v[:5]))
    return res
DEFAULT_SYMBOLS = ["GOLD", "NAS100", "BTC", "CL", "BRENT"]
AUTO_COST = {"BTC": 0.06}
SYMBOL_RE = re.compile(r"^[A-Z0-9][A-Z0-9._:-]{0,39}$")
ID_RE = re.compile(r"^[a-z][a-z0-9_]{1,39}$")
DEFAULT_STRATEGY = "keltner_rsi"
TYPES = ("bool", "int", "float", "select", "symbol", "strategy", "cuentas", "combinar")
MAX_COMBINADAS = 4         # estrategias extra en un mismo bot
TF_DEL_BOT = "la del bot"
DIRECCIONES = ("ambos", "solo largos", "solo cortos")
DIR_AYUDA = ("Lados que opera esta estrategia: si funciona mejor solo en compras o solo en ventas, déjala en ese lado. Con señales invertidas, cuenta el lado que se opera.")
GRUPO_TFDIR = "Temporalidad y dirección"   # grupo de la pestaña de cada estrategia con su temporalidad y su dirección
TF_ESTR = [TF_DEL_BOT, "1m", "3m", "5m", "15m", "30m", "1h", "2h", "4h", "1d"]   # temporalidad propia de una estrategia
TF_AYUDA = ("Velas que mira esta estrategia. Tiene que ser igual o mayor que la del bot y múltiplo de ella (bot de 5m: "
            "15m, 30m, 1h, 4h…). El bot construye estas velas con las suyas; las señales entran en la apertura de la "
            "siguiente vela del bot y el stop y el objetivo se vigilan en cada vela del bot.")
NO_COMBINABLES = ("plan_manual",)   # planes de SMT: siempre en su propio bot
CUENTA_RE = re.compile(r"^c[0-9]{1,4}$")
MAX_CUENTAS_BOT = 5


def F(key, group, label, typ, default, help="", **kw):
    """Define un ajuste. Lo usan el núcleo y las estrategias."""
    d = {"key": key, "group": group, "label": label, "type": typ, "default": default, "help": help}
    d.update(kw)
    return d


def COMUN(key, **overrides):
    """En la lista de ajustes de una estrategia: coloca aquí un ajuste común, con cambios opcionales
    (por ejemplo otro grupo o valor por defecto)."""
    return {"comun": key, **overrides}


# ============================ filtro de tendencia (6.5) ============================
TR_MODES = ["no", "RSI", "EMA", "RSI y EMA", "EMA con pendiente", "Dos EMAs", "ADX + DI", "Estructura"]
TR_TFS = ["15m", "30m", "1h", "2h", "4h", "1d", "1w"]
HTF_MS = dict(TF_MS)
FETCH_TF = dict(AGREGADAS)         # el diario y el semanal se construyen con velas de 4h
TR_NEUTRAL = ["sin entradas", "ambos lados"]


def _tr_fields(px, grupo, defaults):
    """Ajustes de un filtro de tendencia. px = "tr_" (principal) o "tr2_" (confirmación)."""
    d = defaults
    if px == "tr_":
        cond = lambda modos: {"tr_mode": modos}
        out = [F("tr_mode", grupo, "Filtro", "select", "no",
                 "Solo opera a favor de la tendencia de una temporalidad mayor. EMA con pendiente: precio sobre la EMA y "
                 "la EMA subiendo. Dos EMAs: la rápida sobre la lenta. ADX + DI: tendencia con fuerza (ADX) y su "
                 "dirección (DI). Estructura: máximos y mínimos crecientes (alcista) o decrecientes (bajista).",
                 options=TR_MODES),
               F("tr_tf", grupo, "Temporalidad mayor", "select", "1h",
                 "1d: días UTC y 1w: semanas de lunes a domingo, construidos con velas de 4h.", options=TR_TFS, show_if=cond(TR_MODES[1:]))]
    else:
        modos2 = TR_MODES[1:]
        cond = lambda modos: {"tr_mode": TR_MODES[1:], "tr2_tf": TR_TFS, "tr2_mode": modos}
        out = [F("tr2_tf", grupo, "Confirmar con otra temporalidad", "select", "no",
                 "Las dos tienen que estar de acuerdo: un largo necesita tendencia alcista (o neutra, si dejas «ambos "
                 "lados») en las dos. Puede ser la misma temporalidad con otro método.",
                 options=["no"] + TR_TFS, show_if={"tr_mode": TR_MODES[1:]}),
               F("tr2_mode", grupo, "Método", "select", "EMA", "", options=modos2,
                 show_if={"tr_mode": TR_MODES[1:], "tr2_tf": TR_TFS})]
    rsi = ["RSI", "RSI y EMA"]
    ema = ["EMA", "RSI y EMA", "EMA con pendiente", "Dos EMAs"]
    banda = ["EMA", "RSI y EMA", "EMA con pendiente", "Dos EMAs"]
    out += [
        F(px + "rsi_len", grupo, "Periodo del RSI", "int", d["rsi_len"], "", min=2, max=200, show_if=cond(rsi)),
        F(px + "rsi_long", grupo, "Largos solo si el RSI mayor es al menos", "float", 50, "", min=0, max=100,
          show_if=cond(rsi)),
        F(px + "rsi_short", grupo, "Cortos solo si el RSI mayor es como mucho", "float", 50, "", min=0, max=100,
          show_if=cond(rsi)),
        F(px + "ema_len", grupo, "Periodo de la EMA", "int", d["ema_len"],
          "Largos solo con el cierre de la temporalidad mayor por encima de la EMA; cortos, por debajo. En «Dos "
          "EMAs», la lenta.", min=2, max=500, show_if=cond(ema)),
        F(px + "ema_fast", grupo, "EMA rápida", "int", d["ema_fast"], "Tiene que ser menor que la lenta.",
          min=2, max=499, show_if=cond(["Dos EMAs"])),
        F(px + "slope", grupo, "Pendiente: velas atrás", "int", 5,
          "Alcista si la EMA de ahora está por encima de la de hace estas velas (bajista, por debajo).",
          min=1, max=100, show_if=cond(["EMA con pendiente"])),
        F(px + "band", grupo, "Zona neutra (ATR)", "float", 0.0,
          "El precio (o la EMA rápida) tiene que estar al menos esta distancia de la EMA, en ATR de la temporalidad "
          "mayor. Más cerca es zona neutra. 0 = sin zona neutra.", min=0, max=10, step=0.05, show_if=cond(banda)),
        F(px + "adx_len", grupo, "Periodo del ADX", "int", 14, "ADX y DI de Wilder.", min=2, max=100,
          show_if=cond(["ADX + DI"])),
        F(px + "adx_min", grupo, "ADX mínimo para haber tendencia", "float", 20,
          "Por debajo, zona neutra. Lo habitual: 20 a 25.", min=0, max=100, step=1, show_if=cond(["ADX + DI"])),
        F(px + "piv", grupo, "Velas a cada lado del pivote", "int", 3,
          "Un máximo (o mínimo) es pivote si es el más alto (o bajo) de estas velas a cada lado. Se confirma "
          "estas velas después. Alcista: último máximo y último mínimo más altos que los anteriores, y el precio "
          "sin perder el último mínimo.", min=1, max=20, show_if=cond(["Estructura"])),
    ]
    if px == "tr_":
        out.append(F("tr_neutral", grupo, "En zona neutra", "select", "sin entradas",
                     "Sin tendencia clara (lateral, ADX bajo, estructura rota o precio pegado a la EMA): no operar o "
                     "dejar pasar largos y cortos. Vale para las dos temporalidades.",
                     options=TR_NEUTRAL, show_if=cond(TR_MODES[1:])))
    return out


TREND_FIELDS = (_tr_fields("tr_", "Filtro de tendencia", {"rsi_len": 14, "ema_len": 50, "ema_fast": 20})
                + _tr_fields("tr2_", "Tendencia: confirmación", {"rsi_len": 14, "ema_len": 200, "ema_fast": 50}))
TREND_KEYS = tuple(f["key"] for f in TREND_FIELDS)


# ============================ ajustes comunes ============================
# Orden en el formulario: los de PRINCIPIO, luego los de la estrategia, luego el resto.
PRINCIPIO = [
    F("estrategia", "Mercado", "Estrategia", "strategy", DEFAULT_STRATEGY,
      "Las estrategias son archivos de la carpeta estrategias (pestaña «Estrategias»). Cambiarla recarga el "
      "historial y cierra la operación abierta."),
    F("combinar", "Mercado", "Más estrategias en este bot", "combinar", [],
      "Estrategias que vigilan el mismo instrumento a la vez. Una sola operación abierta: la primera señal válida "
      "abre y bloquea al resto hasta que se cierra (TP, stop o salida de esa estrategia, o a mano). Si dan señal "
      "en la misma vela, manda la de mejor puntuación en su backtest. Cada una gestiona la salida de sus "
      "operaciones con sus propios ajustes."),
    F("tf_principal", "Mercado", "Temporalidad de la estrategia principal", "select", "la del bot", TF_AYUDA,
      options=["la del bot", "1m", "3m", "5m", "15m", "30m", "1h", "2h", "4h", "1d"]),
    F("symbol", "Mercado", "Símbolo", "symbol", "GOLD",
      "Uno de tus instrumentos (se añaden en «Instrumentos»). Al cambiarlo se recarga el historial y se cierra la "
      "operación abierta.", env="SYMBOL"),
    F("timeframe", "Mercado", "Temporalidad", "select", "5m", "Igual que el símbolo: recarga el historial.",
      options=TF_BOT, env="TIMEFRAME"),
    F("direction", "Mercado", "Dirección", "select", "ambos", DIR_AYUDA, options=list(DIRECCIONES)),
    F("inv", "Mercado", "Invertir señales", "select", "no",
      "Para probar si una señal que falla tiene valor al revés. Espejo exacto: lado contrario, el stop donde estaría "
      "el objetivo y el objetivo donde estaría el stop, sin break-even, trailing, ampliación ni salidas de la "
      "estrategia (solo con objetivo fijo o RR). Solo dirección: lado contrario con el stop a la misma distancia "
      "y la gestión normal (ya es otra estrategia). Los filtros se aplican al lado que se opera.",
      options=["no", "espejo exacto", "solo dirección"]),
    F("cost_auto", "Mercado", "Comisión del instrumento", "bool", True,
      "Usa la comisión guardada en la lista de instrumentos (0,01 % ida y vuelta en oro, índices y petróleo; "
      "0,06 % en cripto, salvo que la cambies). Desactívalo para fijar una aquí."),
    F("cost_pct", "Mercado", "Comisión ida y vuelta (%)", "float", 0.01, "% del nominal.", min=0, max=1, step=0.001,
      show_if={"cost_auto": False}),
    F("max_cost", "Mercado", "Coste máximo por operación (R)", "float", 0.15,
      "Descarta la entrada si la comisión supera esta fracción del riesgo. 0 = sin filtro.", min=0, max=5, step=0.01,
      env="MAX_COST_R"),
]
RESTO = [
    F("atr_len", "Volatilidad", "Longitud del ATR", "int", 14,
      "ATR de Wilder. Lo usan los filtros de stop, el trailing y la ampliación, y la estrategia si lo necesita.",
      min=1, max=200, env="KC_ATR_LEN"),
    *TREND_FIELDS,
    # ---- Horario ----
    F("hr_on", "Horario", "Limitar horario de entrada", "bool", False, "Hora local de la Raspberry (Madrid)."),
    F("hr_from", "Horario", "Desde las (h)", "int", 8, "", min=0, max=23, show_if={"hr_on": True}),
    F("hr_to", "Horario", "Hasta las (h)", "int", 22, "Sin incluir. Si es menor que «desde», cruza la medianoche.",
      min=0, max=24, show_if={"hr_on": True}),
    F("hr_weekdays", "Horario", "Solo de lunes a viernes", "bool", False, "", show_if={"hr_on": True}),
    # ---- Stop ----
    F("sl_min_atr", "Stop", "Stop mínimo (ATR)", "float", 0.0, "Descarta señales con el stop más cerca. 0 = sin mínimo.",
      min=0, max=20, step=0.05),
    F("sl_max_atr", "Stop", "Stop máximo (ATR)", "float", 0.0, "Descarta señales con el stop más lejos. 0 = sin máximo.",
      min=0, max=50, step=0.05),
    # ---- Salidas comunes ----
    F("max_bars", "Objetivo y salidas", "Duración máxima (velas)", "int", 0,
      "Cierra al cierre tras este número de velas. 0 = sin límite.", min=0, max=5000),
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
      "Porcentaje de la distancia actual entre la entrada y el objetivo.", min=1, max=500, step=5,
      show_if={"xp_on": True}),
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
      "La operación solo se cierra por el trailing o por las salidas al cierre.", show_if={"ts_on": True}),
    # ---- Break-even ----
    F("be_on", "Break-even y venta parcial", "Activar", "bool", False,
      "Al llegar al disparador vende una parte y sube el stop a la entrada.", env="BE_ON"),
    F("be_trigger", "Break-even y venta parcial", "Disparador", "select", "R",
      "R: a un múltiplo del riesgo. Nivel de la estrategia: el que marque la estrategia (si lo tiene).",
      options=["R", "nivel de la estrategia"], env="BE_TRIGGER", show_if={"be_on": True}),
    F("be_r", "Break-even y venta parcial", "Disparador en R", "float", 1.0, "", min=0.1, max=20, step=0.1, env="BE_R",
      show_if={"be_on": True, "be_trigger": "R"}),
    F("be_pct", "Break-even y venta parcial", "Porcentaje que se vende", "float", 10, "0 = solo mover el stop.",
      min=0, max=100, step=1, env="BE_PCT", show_if={"be_on": True}),
    F("be_off", "Break-even y venta parcial", "Stop nuevo: entrada + (R)", "float", 0.0,
      "0 = break-even exacto.", min=-1, max=5, step=0.05, env="BE_OFFSET_R", show_if={"be_on": True}),
    # ---- Límites de la estrategia ----
    F("max_trades", "Límites diarios de la estrategia", "Operaciones al día", "int", 10,
      "0 = sin límite. Pausan el resto del día.", min=0, max=1000, env="MAX_TRADES_DAY"),
    F("max_consec", "Límites diarios de la estrategia", "Pérdidas seguidas", "int", 3, "0 = sin límite.", min=0,
      max=100, env="MAX_CONSEC_LOSSES"),
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
    F("g_unit", "Guardián (detiene el bot)", "Unidad de los límites", "select", "R", "", options=["R", "USD"],
      env="G_UNIT"),
    F("g_day_loss", "Guardián (detiene el bot)", "Pérdida diaria", "float", 0.0, "0 = sin límite.", min=0, max=1e7,
      step=0.5, env="G_DAY_LOSS"),
    F("g_day_profit", "Guardián (detiene el bot)", "Ganancia diaria", "float", 0.0, "0 = sin límite.", min=0, max=1e7,
      step=0.5, env="G_DAY_PROFIT"),
    F("g_week_loss", "Guardián (detiene el bot)", "Pérdida semanal", "float", 0.0, "0 = sin límite.", min=0, max=1e7,
      step=0.5, env="G_WEEK_LOSS"),
    F("g_week_profit", "Guardián (detiene el bot)", "Ganancia semanal", "float", 0.0, "0 = sin límite.", min=0,
      max=1e7, step=0.5, env="G_WEEK_PROFIT"),
    F("g_action", "Guardián (detiene el bot)", "Al alcanzar un límite", "select", "parar",
      "Parar: sin entradas hasta que lo reactives. Pausa: sigue al empezar el día o la semana siguiente.",
      options=["parar", "pausa"], env="G_ACTION"),
    # ---- Ejecución en la cuenta sandbox (ejecucion.py). No cambia nada del modelo en papel. ----
    F("ex_modo", "Ejecución en sandbox", "Modo", "select", "papel",
      "Papel: solo simula. Sandbox: además envía las órdenes a las cuentas elegidas abajo (se dan de alta en el "
      "menú Cuentas, con su clave). En cada cuenta, un solo bot por mercado.",
      options=["papel", "sandbox"], env="EX_MODO"),
    F("ex_cuentas", "Ejecución en sandbox", "Cuentas", "cuentas", [],
      "En qué cuentas opera. Con varias, la misma señal abre una orden en cada una, con su tamaño (y el "
      "multiplicador de riesgo de cada cuenta). Si no eliges ninguna, usa la primera de la lista.",
      show_if={"ex_modo": "sandbox"}),
    F("ex_riesgo_modo", "Ejecución en sandbox", "Tamaño de la posición", "select", "% del balance",
      "% del balance o importe fijo: lo que se pierde si salta el stop inicial (el tamaño se calcula). Tamaño fijo: "
      "siempre los mismos contratos (lo que se arriesga depende de la distancia al stop).",
      options=["% del balance", "importe fijo ($)", "tamaño fijo (contratos)"],
      show_if={"ex_modo": "sandbox"}, env="EX_RIESGO_MODO"),
    F("ex_riesgo_pct", "Ejecución en sandbox", "Riesgo por operación (% del balance)", "float", 0.25,
      "Máximo 2 %: con rachas de pérdidas normales, más riesgo rompe el drawdown de la cuenta.",
      min=0.01, max=2, step=0.01, show_if={"ex_modo": "sandbox", "ex_riesgo_modo": "% del balance"},
      env="EX_RIESGO_PCT"),
    F("ex_riesgo_usd", "Ejecución en sandbox", "Riesgo por operación ($)", "float", 250.0,
      "", min=1, max=1e6, step=10, show_if={"ex_modo": "sandbox", "ex_riesgo_modo": "importe fijo ($)"},
      env="EX_RIESGO_USD"),
    F("ex_tamano", "Ejecución en sandbox", "Tamaño fijo (contratos o unidades)", "float", 1.0,
      "Lo que arriesgas = contratos × distancia al stop × valor del punto. Se respetan igualmente el nominal máximo y "
      "las comprobaciones de la cuenta.", min=0.0001, max=1e6, step=0.01,
      show_if={"ex_modo": "sandbox", "ex_riesgo_modo": "tamaño fijo (contratos)"}, env="EX_TAMANO"),
    F("ex_dll_min_pct", "Ejecución en sandbox", "No operar si tras el SL quedaría menos de (% del daily loss)",
      "float", 40, "Consulta la trading policy y la cuenta antes de cada orden. 0 = sin esta comprobación.",
      min=0, max=100, step=5, show_if={"ex_modo": "sandbox"}, env="EX_DLL_MIN_PCT"),
    F("ex_apalancamiento", "Ejecución en sandbox", "Apalancamiento máximo", "int", 10,
      "Se usa el mínimo necesario para el tamaño, sin pasar de este valor ni del máximo del mercado.",
      min=1, max=100, show_if={"ex_modo": "sandbox"}, env="EX_APALANCAMIENTO"),
    F("ex_margen", "Ejecución en sandbox", "Margen", "select", "cross", "", options=["cross", "isolated"],
      show_if={"ex_modo": "sandbox"}, env="EX_MARGEN"),
    F("ex_slippage_bps", "Ejecución en sandbox", "Tolerancia de deslizamiento (pb)", "float", 0,
      "Puntos básicos (10 = 0,10 %). 0 = la que tengas guardada en MyFundedPerps.", min=0, max=500, step=1,
      show_if={"ex_modo": "sandbox"}, env="EX_SLIPPAGE_BPS"),
    F("ex_nocional_max", "Ejecución en sandbox", "Nominal máximo por operación ($)", "float", 0,
      "Si el tamaño por riesgo lo supera, se reduce. 0 = sin límite propio (se respeta el de la cuenta).",
      min=0, max=1e8, step=1000, show_if={"ex_modo": "sandbox"}, env="EX_NOCIONAL_MAX"),
    F("ex_alinear", "Ejecución en sandbox", "Cerrar la posición real si el modelo no la tiene", "bool", True,
      "Si el modelo cierra (o descarta la entrada) y la posición real sigue abierta, se cierra a mercado.",
      show_if={"ex_modo": "sandbox"}, env="EX_ALINEAR"),
    F("ex_cerrar_al_archivar", "Ejecución en sandbox", "Cerrar la posición real al archivar el bot", "bool", True,
      "Si no, queda abierta en la cuenta con su stop y su objetivo.", show_if={"ex_modo": "sandbox"},
      env="EX_CERRAR_ARCHIVAR"),
]
COMMON = PRINCIPIO + RESTO
COMMON_BY_KEY = {f["key"]: f for f in COMMON}
COMMON_RELOAD = ("estrategia", "combinar", "tf_principal", "symbol", "timeframe") + TREND_KEYS        # la tendencia recarga su historial
MARKET_KEYS = ("estrategia", "symbol", "timeframe")          # cambiarlos cierra la operación abierta
FILTER_NAMES = {"direction": "dirección", "cooldown": "espera tras cerrar", "hours": "horario",
                "trend": "tendencia en contra", "trend_neutral": "tendencia: zona neutra",
                "trend_wait": "tendencia: calentando", "trend2": "tendencia en contra (confirmación)",
                "trend2_neutral": "tendencia: zona neutra (confirmación)",
                "sl_min": "stop demasiado cerca", "sl_max": "stop demasiado lejos",
                "espejo_sin_objetivo": "espejo sin objetivo (la estrategia no tiene objetivo fijo ni RR)"}
EXIT_NAMES = {"TP": "objetivo", "SL": "stop", "BE": "break-even", "TIEMPO": "tiempo", "AJUSTE": "cambio de ajustes",
              "TRAILING": "trailing", "SL MOVIDO": "stop ampliado", "ESTRATEGIA": "salida de la estrategia",
              "MANUAL": "cierre manual"}
LEGACY_COMMON = {"be_trigger": {"r": "R"}}


# ============================ indicadores (para las estrategias) ============================
class EMA:
    """Media exponencial incremental. update(x) devuelve el valor o None mientras calienta."""

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


class RMA:
    """Media de Wilder."""

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


class SMA:
    def __init__(s, n):
        from collections import deque
        s.n, s.buf, s.sum, s.v = n, deque(), 0.0, None

    def update(s, x):
        s.buf.append(x)
        s.sum += x
        if len(s.buf) > s.n:
            s.sum -= s.buf.popleft()
        s.v = s.sum / s.n if len(s.buf) == s.n else None
        return s.v


class RSI:
    """RSI de Wilder."""

    def __init__(s, n):
        s.g, s.l, s.prev = RMA(n), RMA(n), None

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


class Ventana:
    """Últimos n valores (por ejemplo velas) con máximo y mínimo."""

    def __init__(s, n):
        from collections import deque
        s.d = deque(maxlen=n)

    def add(s, x):
        s.d.append(x)

    def lleno(s):
        return len(s.d) == s.d.maxlen

    def __len__(s):
        return len(s.d)

    def __getitem__(s, i):
        return s.d[i]


# nombres antiguos (v3)
EMAInc, RMAInc, RSIInc = EMA, RMA, RSI


class ADX:
    """ADX, DI+ y DI- de Wilder. update(h, l, c) devuelve (adx, di_mas, di_menos) o None mientras calienta."""

    def __init__(s, n):
        s.tr, s.pdm, s.mdm, s.dx = RMA(n), RMA(n), RMA(n), RMA(n)
        s.prev = None

    def update(s, h, l, c):
        if s.prev is None:
            s.prev = (h, l, c)
            return None
        ph, pl, pc = s.prev
        s.prev = (h, l, c)
        up, dn = h - ph, pl - l
        pdm = up if up > dn and up > 0 else 0.0
        mdm = dn if dn > up and dn > 0 else 0.0
        tr = max(h - l, abs(h - pc), abs(l - pc))
        a, b, t = s.pdm.update(pdm), s.mdm.update(mdm), s.tr.update(tr)
        if t is None:
            return None
        dip, dim = (100 * a / t, 100 * b / t) if t else (0.0, 0.0)
        dx = 100 * abs(dip - dim) / (dip + dim) if dip + dim else 0.0
        adx = s.dx.update(dx)
        return None if adx is None else (adx, dip, dim)


class TendenciaTF:
    """Tendencia de UNA temporalidad mayor, construida con las velas del bot (sin mirar el futuro).

    estado() -> (lado, texto): lado 1 alcista, -1 bajista, 0 neutra, None calentando.
    lados()  -> (largos_ok, cortos_ok) o None calentando. Con RSI y «RSI y EMA» se respeta el comportamiento de
    siempre (umbrales independientes para largos y cortos)."""

    def __init__(self, P, px):
        g = lambda k: P[px + k]
        self.px, self.mode, self.tf = px, g("mode"), g("tf")
        self.ms = HTF_MS[self.tf]
        self.src = FETCH_TF.get(self.tf, self.tf)
        self.rsi_long, self.rsi_short = g("rsi_long"), g("rsi_short")
        self.band, self.adx_min, self.piv = g("band"), g("adx_min"), g("piv")
        self.rsi, self.ema, self.fast = RSI(g("rsi_len")), EMA(g("ema_len")), EMA(g("ema_fast"))
        self.ema_len, self.fast_len = g("ema_len"), g("ema_fast")
        self.ema_hist = Ventana(g("slope") + 1)
        self._atr, self._adx = RMA(14), ADX(g("adx_len"))
        self.win = Ventana(2 * self.piv + 1)
        self.highs, self.lows = [], []          # últimos pivotes confirmados (como mucho 2 de cada)
        self.bucket = self.agg = self.last_bucket = None
        self.prev_c = None
        self.v_rsi = self.v_ema = self.v_fast = self.v_close = self.v_atr = self.v_adx = None
        self.bars = 0

    # --- velas ---
    def add(self, c, bar_ms):
        t, o, h, l, cl = c[:5]
        b = inicio_vela(t, self.ms)
        if self.last_bucket is not None and b <= self.last_bucket:
            return
        if self.agg is not None and b != self.bucket:
            self._feed(*self.agg)
            self.last_bucket, self.agg = self.bucket, None
        if self.agg is None:
            self.bucket, self.agg = b, [o, h, l, cl]
        else:
            a = self.agg
            a[1], a[2], a[3] = max(a[1], h), min(a[2], l), cl
        if t + bar_ms >= b + self.ms:
            self._feed(*self.agg)
            self.last_bucket, self.agg = b, None

    def _feed(self, o, h, l, c):
        pc = self.prev_c
        tr = h - l if pc is None else max(h - l, abs(h - pc), abs(l - pc))
        self.prev_c = c
        self.v_atr = self._atr.update(tr)
        self.v_rsi, self.v_ema, self.v_fast, self.v_close = self.rsi.update(c), self.ema.update(c), \
            self.fast.update(c), c
        if self.v_ema is not None:
            self.ema_hist.add(self.v_ema)
        self.v_adx = self._adx.update(h, l, c)
        self.win.add((h, l))
        if self.win.lleno():
            mid = self.win[self.piv]
            if all(mid[0] >= x[0] for x in self.win.d) and any(mid[0] > x[0] for x in self.win.d):
                self.highs = (self.highs + [mid[0]])[-2:]
            if all(mid[1] <= x[1] for x in self.win.d) and any(mid[1] < x[1] for x in self.win.d):
                self.lows = (self.lows + [mid[1]])[-2:]
        self.bars += 1

    # --- lectura ---
    def _banda(self):
        return self.band * self.v_atr if self.band and self.v_atr is not None else 0.0

    def estado(self):
        m, c = self.mode, self.v_close
        if c is None:
            return None, "calentando"
        if m in ("EMA", "EMA con pendiente", "RSI y EMA"):
            if self.v_ema is None or (self.band and self.v_atr is None):
                return None, "calentando"
            d, bd = c - self.v_ema, self._banda()
            lado = 1 if d > bd else -1 if d < -bd else 0
            txt = "{} la EMA {}{}".format("sobre" if d > 0 else "bajo", self.ema_len,
                                          " ({:+.1f} ATR)".format(d / self.v_atr) if self.v_atr else "")
            if m == "EMA con pendiente":
                if not self.ema_hist.lleno():
                    return None, "calentando"
                sl = self.ema_hist[-1] - self.ema_hist[0]
                txt += ", EMA {}".format("subiendo" if sl > 0 else "bajando" if sl < 0 else "plana")
                if lado == 1 and sl <= 0 or lado == -1 and sl >= 0:
                    lado = 0
            if m == "RSI y EMA" and self.v_rsi is not None:
                txt = "RSI {:.0f}, {}".format(self.v_rsi, txt)
            return lado, txt
        if m == "RSI":
            if self.v_rsi is None:
                return None, "calentando"
            lo, sh = self.v_rsi >= self.rsi_long, self.v_rsi <= self.rsi_short
            return (1 if lo and not sh else -1 if sh and not lo else 0), "RSI {:.0f}".format(self.v_rsi)
        if m == "Dos EMAs":
            if self.v_ema is None or self.v_fast is None or (self.band and self.v_atr is None):
                return None, "calentando"
            d, bd = self.v_fast - self.v_ema, self._banda()
            return (1 if d > bd else -1 if d < -bd else 0), "EMA {} {} EMA {}".format(
                self.fast_len, "sobre" if d > 0 else "bajo", self.ema_len)
        if m == "ADX + DI":
            if self.v_adx is None:
                return None, "calentando"
            adx, dp, dm = self.v_adx
            lado = 0 if adx < self.adx_min else 1 if dp > dm else -1 if dm > dp else 0
            return lado, "ADX {:.0f}, DI+ {:.0f} / DI− {:.0f}".format(adx, dp, dm)
        if m == "Estructura":
            if len(self.highs) < 2 or len(self.lows) < 2:
                return None, "calentando"
            hh, hl = self.highs[1] > self.highs[0], self.lows[1] > self.lows[0]
            lh, ll = self.highs[1] < self.highs[0], self.lows[1] < self.lows[0]
            if hh and hl:
                return (1, "máximos y mínimos crecientes") if c >= self.lows[1] else (0, "pierde el último mínimo")
            if lh and ll:
                return (-1, "máximos y mínimos decrecientes") if c <= self.highs[1] else (0, "supera el último máximo")
            return 0, "estructura mixta"
        return None, ""

    def lados(self):
        m = self.mode
        if m in ("RSI", "RSI y EMA"):                    # comportamiento de siempre
            if self.v_rsi is None or (m == "RSI y EMA" and self.v_ema is None):
                return None
            lo, sh = self.v_rsi >= self.rsi_long, self.v_rsi <= self.rsi_short
            if m == "RSI y EMA":
                bd = self._banda()
                lo = lo and self.v_close - self.v_ema > bd
                sh = sh and self.v_close - self.v_ema < -bd
            return lo, sh
        lado, _ = self.estado()
        if lado is None:
            return None
        return lado == 1, lado == -1


class Tendencia:
    """El filtro de tendencia completo: principal (tr_) y, si se pide, confirmación (tr2_)."""

    def __init__(self, P):
        self.P = P
        self.tfs = [TendenciaTF(P, "tr_")]
        if P.get("tr2_tf", "no") != "no":
            self.tfs.append(TendenciaTF(P, "tr2_"))
        self.ambos = P.get("tr_neutral") == "ambos lados"

    # compatibilidad: el panel lee el RSI y la EMA de la principal
    @property
    def v_rsi(self):
        return self.tfs[0].v_rsi

    @property
    def v_ema(self):
        return self.tfs[0].v_ema

    @property
    def v_close(self):
        return self.tfs[0].v_close

    def seed(self, bars, hasta=None):
        """bars: {temporalidad descargada: velas} (o una lista, de la principal, como antes)."""
        if isinstance(bars, (list, tuple)):
            bars = {self.tfs[0].src: bars}
        for x in self.tfs:
            ms = TF_MS[x.src]
            for b in bars.get(x.src) or []:
                if hasta is None or b[0] + ms <= hasta:
                    x.add(b, ms)

    def on_base(self, c, base_ms):
        for x in self.tfs:
            x.add(c, base_ms)

    def rechazo(self, side):
        """None si la tendencia deja entrar en ese lado; si no, el código del filtro."""
        for k, x in enumerate(self.tfs):
            ok = x.lados()
            if ok is None:
                return "trend_wait"
            lo, sh = ok
            if not lo and not sh:
                if self.ambos:
                    continue
                return "trend_neutral" if k == 0 else "trend2_neutral"
            if side == 1 and not lo or side == -1 and not sh:
                return "trend" if k == 0 else "trend2"
        return None

    def resumen(self):
        """Lista de {tf, lado, texto, metodo} para el panel, /estado y los avisos."""
        out = []
        for x in self.tfs:
            lado, txt = x.estado()
            if x.mode in ("RSI", "RSI y EMA") and lado == 0:
                ok = x.lados()
                if ok and ok[0] and ok[1]:
                    txt += " (deja los dos lados)"
            out.append({"tf": x.tf, "lado": lado, "texto": txt, "metodo": x.mode})
        return out

    def texto_corto(self):
        f = {1: "▲", -1: "▼", 0: "◆", None: "…"}
        return " · ".join("{} {}".format(r["tf"], f[r["lado"]]) for r in self.resumen())

    def texto(self):
        n = {1: "alcista", -1: "bajista", 0: "neutra", None: "calentando"}
        return " · ".join("{} {} ({})".format(r["tf"], n[r["lado"]], r["texto"]) if r["lado"] is not None
                          else "{} calentando".format(r["tf"]) for r in self.resumen())


HTF = Tendencia          # nombre anterior


def tendencia_fuentes(P):
    """{temporalidad a descargar: velas para calentar} de los filtros de tendencia activos."""
    out = {}
    if P.get("tr_mode", "no") == "no":
        return out
    for px in ("tr_", "tr2_"):
        if px == "tr2_" and P.get("tr2_tf", "no") == "no":
            continue
        tf = P[px + "tf"]
        src = FETCH_TF.get(tf, tf)
        k = HTF_MS[tf] // TF_MS[src]
        lens = [14]
        m = P[px + "mode"]
        if m in ("RSI", "RSI y EMA"):
            lens.append(P[px + "rsi_len"])
        if m in ("EMA", "RSI y EMA", "EMA con pendiente", "Dos EMAs"):
            lens.append(P[px + "ema_len"])
        if m == "ADX + DI":
            lens.append(2 * P[px + "adx_len"])
        need = 3 * max(lens) + P[px + "slope"] + 6 * P[px + "piv"] + 20
        if m == "Estructura":
            need = max(need, 60 * P[px + "piv"])
        out[src] = max(out.get(src, 0), need * k)
    return out


# ============================ clase base de las estrategias ============================
class Estrategia:
    """Base de todas las estrategias. Solo hace falta escribir senales(); el resto es opcional.

    Dentro de la estrategia:
      self.P      ajustes (los comunes y los de la estrategia)
      self.m      el motor: self.m.atr (ATR de la vela actual o None), self.m.prev (vela anterior),
                  self.m.i (nº de vela), self.m.pos (operación abierta o None, solo lectura),
                  self.m.hora(t) -> (hora local, día de la semana 0=lunes), self.m.tf_ms
    Una vela es una tupla (t_ms, open, high, low, close).
    """

    def __init__(self, P, motor):
        self.P, self.m = P, motor

    # --- obligatorio ---
    def senales(self):
        """Al cierre de cada vela (siempre, también mientras calienta). Devuelve una lista de señales:
        {"side": 1 (largo) o -1 (corto), "sl": precio del stop,
         opcionales: "tp": objetivo fijo en precio, "rechazo": código de filtro propio}."""
        return []

    # --- opcionales ---
    def actualizar(self, vela):
        """Al principio de cada vela cerrada, antes de gestionar la operación: actualiza indicadores."""

    def rr(self):
        """Objetivo a un múltiplo del riesgo (p. ej. 2.0) o None si el objetivo es dinámico o no hay."""
        return None

    def objetivo(self, pos):
        """Objetivo dinámico (precio o None). Se pide en cada vela si rr() es None y la señal no trae tp."""
        return None

    def gestion_be(self, pos):
        """Venta parcial y break-even propios de ESTA operación (aunque el bot no tenga activado el break-even):
        {"nivel": precio, "pct": % que se vende, "off": stop nuevo en R desde la entrada (None = no lo mueve)}.
        None = se usa la configuración del bot."""
        return None

    def nivel_be(self, pos):
        """Precio que dispara el break-even si el disparador no es «R»."""
        return None

    def salida(self, pos):
        """Al cierre de la vela: un texto corto (motivo) para cerrar la operación, o None."""
        return None

    def lineas(self):
        """Valores de las líneas a dibujar en el gráfico de esta vela: {clave: precio}. Ver LINEAS."""
        return {}

    def texto(self):
        """Una frase sobre lo que está esperando la estrategia (se ve en el panel)."""
        return ""

    def indicadores(self):
        """Texto corto con los indicadores actuales (p. ej. «RSI 42»)."""
        return ""

    def guardar(self):
        """Estado propio a conservar en un reinicio (JSON)."""
        return {}

    def cargar(self, s):
        """Recupera lo que devolvió guardar()."""

    def reiniciar(self):
        """Olvida preparaciones en curso (al terminar el calentamiento del backtest o al arrancar de cero)."""


Base = Estrategia


# ============================ registro de estrategias ============================
class StrategyError(Exception):
    pass


class Plugin:
    def __init__(self, path):
        self.path = path
        self.file = os.path.basename(path)
        self.id = os.path.splitext(self.file)[0]
        self.mod = None
        self.error = None
        self.sha = ""
        self.loaded_at = 0
        self.nombre, self.version, self.descripcion, self.autor = self.id, "", "", ""
        self.schema, self.defaults, self.by_key = [], {}, {}
        self.reload_keys = COMMON_RELOAD
        self.filtros, self.salidas, self.lineas, self.legacy = {}, {}, [], {}

    def info(self, used=0):
        return {"id": self.id, "nombre": self.nombre, "version": self.version, "descripcion": self.descripcion,
                "autor": self.autor, "archivo": self.file, "sha": self.sha[:10], "error": self.error,
                "cargada": self.loaded_at, "bots": used, "lineas": self.lineas}


def _check_field(f, where):
    for k in ("key", "group", "label", "type"):
        if k not in f:
            raise StrategyError("{}: a un ajuste le falta «{}»".format(where, k))
    if f["type"] not in TYPES or f["type"] in ("symbol", "strategy", "cuentas", "combinar"):
        raise StrategyError("{}: tipo «{}» no válido en {}".format(where, f["type"], f["key"]))
    if f["type"] == "select" and (not f.get("options") or f["default"] not in f["options"]):
        raise StrategyError("{}: {} necesita opciones y un valor por defecto que esté entre ellas".format(where, f["key"]))
    if not re.match(r"^[a-z][a-z0-9_]{0,39}$", f["key"]):
        raise StrategyError("{}: clave no válida «{}» (minúsculas, números y _)".format(where, f["key"]))


def build_schema(mod, where):
    """Junta los ajustes comunes con los de la estrategia."""
    params = list(getattr(mod, "PARAMETROS", []))
    overrides = dict(getattr(mod, "AJUSTES_COMUNES", {}) or {})
    placed, own = [], []
    seen = set()
    for item in params:
        if "comun" in item:
            k = item["comun"]
            if k not in COMMON_BY_KEY or k in [f["key"] for f in PRINCIPIO]:
                raise StrategyError("{}: «{}» no es un ajuste común que se pueda colocar".format(where, k))
            ov = {a: b for a, b in item.items() if a != "comun"}
            overrides[k] = dict(overrides.get(k, {}), **ov)
            placed.append(("comun", k))
            seen.add(k)
            continue
        _check_field(item, where)
        if item["key"] in COMMON_BY_KEY:
            raise StrategyError("{}: «{}» ya es un ajuste común; usa AJUSTES_COMUNES o COMUN()".format(where, item["key"]))
        if item["key"] in seen:
            raise StrategyError("{}: el ajuste «{}» está repetido".format(where, item["key"]))
        seen.add(item["key"])
        own.append(item)
        placed.append(("own", item))

    def common(k):
        f = dict(COMMON_BY_KEY[k])
        f.update(overrides.get(k, {}))
        f["common"] = True
        if f["type"] == "select" and f["default"] not in f["options"]:
            raise StrategyError("{}: valor por defecto no válido para {}".format(where, k))
        return f

    for k in overrides:
        if k not in COMMON_BY_KEY:
            raise StrategyError("{}: AJUSTES_COMUNES nombra «{}», que no es un ajuste común".format(where, k))
    schema = [common(f["key"]) for f in PRINCIPIO]
    for kind, x in placed:
        schema.append(common(x) if kind == "comun" else dict(x))
    schema += [common(f["key"]) for f in RESTO if f["key"] not in seen]
    return schema


def load_plugin(path):
    """Carga un archivo de estrategia y comprueba que cumple la interfaz. Nunca lanza: deja .error."""
    p = Plugin(path)
    try:
        with open(path, "rb") as fh:
            raw = fh.read()
        p.sha = hashlib.sha256(raw).hexdigest()
        name = "estrategia_{}_{}".format(re.sub(r"\W", "_", p.id), p.sha[:8])
        spec = importlib.util.spec_from_file_location(name, path)
        mod = importlib.util.module_from_spec(spec)
        import sys
        old, sys.dont_write_bytecode = sys.dont_write_bytecode, True     # sin carpetas __pycache__ en estrategias/
        try:
            spec.loader.exec_module(mod)
        finally:
            sys.dont_write_bytecode = old
        sid = getattr(mod, "ID", p.id)
        if not ID_RE.match(str(sid)):
            raise StrategyError("ID «{}» no válido (minúsculas, números y _; empieza por letra)".format(sid))
        p.id = sid
        api = getattr(mod, "API", 1)
        if api > API_VERSION:
            raise StrategyError("pide la interfaz v{} y este núcleo tiene la v{}: actualiza el programa".format(
                api, API_VERSION))
        cls = getattr(mod, "Estrategia", None)
        if cls is None or not isinstance(cls, type) or not issubclass(cls, Estrategia):
            raise StrategyError("falta la clase Estrategia (class Estrategia(Base): …)")
        if cls.senales is Estrategia.senales:
            raise StrategyError("la clase Estrategia no define senales()")
        p.nombre = str(getattr(mod, "NOMBRE", sid))[:60]
        p.version = str(getattr(mod, "VERSION", ""))[:20]
        p.descripcion = str(getattr(mod, "DESCRIPCION", "")).strip()[:2000]
        p.autor = str(getattr(mod, "AUTOR", ""))[:60]
        p.schema = build_schema(mod, p.file)
        p.by_key = {f["key"]: f for f in p.schema}
        p.defaults = {f["key"]: f["default"] for f in p.schema}
        p.defaults["estrategia"] = p.id
        p.by_key["estrategia"] = dict(p.by_key["estrategia"], default=p.id)
        p.reload_keys = tuple(COMMON_RELOAD) + tuple(getattr(mod, "RECARGA", ()))
        p.filtros = dict(getattr(mod, "FILTROS", {}) or {})
        p.salidas = dict(getattr(mod, "SALIDAS", {}) or {})
        p.lineas = list(getattr(mod, "LINEAS", []) or [])
        p.legacy = dict(getattr(mod, "VALORES_ANTIGUOS", {}) or {})
        p.mod = mod
        P, errs = _validate_with(p, {})
        if errs:
            raise StrategyError("los valores por defecto no pasan la validación: " + "; ".join(errs))
        p.loaded_at = int(time.time() * 1000)
    except StrategyError as e:
        p.error = str(e)
    except SyntaxError as e:
        p.error = "error de sintaxis en la línea {}: {}".format(e.lineno, e.msg)
    except Exception as e:
        tb = traceback.extract_tb(e.__traceback__)
        line = next((fr.lineno for fr in reversed(tb) if fr.filename == path), None)
        p.error = "{}: {}{}".format(type(e).__name__, e, " (línea {})".format(line) if line else "")
    if p.error:
        p.mod = None
    return p


def synthetic_candles(n=3000, tf_ms=300_000, seed=7, start=2600.0):
    """Velas inventadas para comprobar que una estrategia funciona (no para medirla)."""
    rnd = random.Random(seed)
    t = 1_700_000_000_000 // tf_ms * tf_ms
    px, vol, out = start, start * 0.0012, []
    for i in range(n):
        if i % 400 == 0:
            vol = start * rnd.uniform(0.0005, 0.003)
        o = px
        c = o + rnd.gauss(0, vol) + (rnd.choice([-1, 1]) * vol * 3 if rnd.random() < 0.02 else 0)
        h = max(o, c) + abs(rnd.gauss(0, vol * 0.6))
        l = min(o, c) - abs(rnd.gauss(0, vol * 0.6))
        out.append((t + i * tf_ms, round(o, 2), round(h, 2), round(l, 2), round(c, 2)))
        px = c
    return out


def smoke_test(p):
    """Ejecuta la estrategia con sus valores por defecto sobre velas inventadas. Devuelve (ok, texto)."""
    P = dict(p.defaults)
    cs = synthetic_candles(3000, TF_MS[P["timeframe"]])
    t0 = time.time()
    try:
        trades, info = backtest(cs, P, warm=min(600, warmup_bars(P, p)), plugin=p)
        st = Motor(P, plugin=p)
        for c in cs[:400]:
            st.on_candle(c)
        st.S.lineas(), st.S.texto(), st.S.indicadores()
        import json
        json.dumps(st.dump())
    except Exception as e:
        tb = traceback.extract_tb(e.__traceback__)
        line = next((fr.lineno for fr in reversed(tb) if fr.filename == p.path), None)
        return False, "falla al ejecutarse: {}: {}{}".format(type(e).__name__, e, " (línea {})".format(line) if line else "")
    secs = time.time() - t0
    return True, "funciona con velas de prueba: {} señales y {} operaciones en 3.000 velas ({:.2f} s)".format(
        info["signals"], len(trades), secs)


class Registry:
    """Las estrategias instaladas: un archivo .py por estrategia en la carpeta estrategias/."""

    def __init__(self, folder):
        self.folder = folder
        self.items = {}
        self.lock = threading.RLock()
        os.makedirs(folder, exist_ok=True)
        os.makedirs(os.path.join(folder, "anteriores"), exist_ok=True)

    def scan(self):
        found = {}
        for name in sorted(os.listdir(self.folder)):
            if not name.endswith(".py") or name.startswith(("_", ".")):
                continue
            p = load_plugin(os.path.join(self.folder, name))
            if p.id in found and not found[p.id].error:
                p.error = "ID «{}» repetido (también en {})".format(p.id, found[p.id].file)
                p.id = os.path.splitext(name)[0] + "__repetido"
            found[p.id] = p
        with self.lock:
            self.items = found
        return found

    def get(self, sid):
        p = self.items.get(sid)
        return p if p and not p.error else None

    def any(self, sid):
        return self.items.get(sid)

    def list(self):
        return sorted(self.items.values(), key=lambda p: (bool(p.error), p.nombre.lower()))

    def default_id(self):
        if self.get(DEFAULT_STRATEGY):
            return DEFAULT_STRATEGY
        ok = [p.id for p in self.list() if not p.error]
        return ok[0] if ok else DEFAULT_STRATEGY

    def install(self, filename, code, in_use_ids=()):
        """Comprueba un archivo nuevo y, si funciona, lo instala. Devuelve (plugin, mensaje, reemplazada)."""
        filename = os.path.basename(filename or "estrategia.py")
        if not filename.endswith(".py"):
            raise ValueError("El archivo tiene que ser .py")
        if len(code) > 400_000:
            raise ValueError("Archivo demasiado grande")
        tmpdir = os.path.join(self.folder, ".comprobando")
        os.makedirs(tmpdir, exist_ok=True)
        tmp = os.path.join(tmpdir, re.sub(r"[^A-Za-z0-9_.-]", "_", filename))
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(code)
        try:
            p = load_plugin(tmp)
            if p.error:
                raise ValueError("No se instala: " + p.error)
            ok, msg = smoke_test(p)
            if not ok:
                raise ValueError("No se instala: " + msg)
            with self.lock:
                old = self.items.get(p.id)
                replaced = bool(old)
                if old and os.path.exists(old.path):
                    self._archive(old.path, old.id)
                dest = os.path.join(self.folder, p.id + ".py")
                if os.path.exists(dest):
                    self._archive(dest, p.id)
                shutil.move(tmp, dest)
                final = load_plugin(dest)
                self.items[final.id] = final
            return final, msg, replaced
        finally:
            if os.path.exists(tmp):
                os.remove(tmp)

    def remove(self, sid):
        with self.lock:
            p = self.items.get(sid)
            if not p:
                raise ValueError("No existe esa estrategia")
            if os.path.exists(p.path):
                self._archive(p.path, sid, move=True)
            del self.items[sid]

    def _archive(self, path, sid, move=False):
        dest = os.path.join(self.folder, "anteriores", "{}_{}.py".format(sid, time.strftime("%Y%m%d_%H%M%S")))
        (shutil.move if move else shutil.copy2)(path, dest)


REGISTRY = None            # lo fija el programa al arrancar (Registry de la carpeta estrategias)


class VistaTF:
    """Lo que ve una estrategia que trabaja en una temporalidad mayor que la del bot: sus propias velas (hechas con
    las del bot), su ATR, su vela anterior y su contador. La operación, la orden pendiente y el resto, los del motor."""

    def __init__(self, motor, tf_ms):
        self._m, self.tf_ms = motor, tf_ms
        self._atr = RMA(motor.P["atr_len"])
        self.atr = self.prev = self.vela = self.cur = None
        self.i = -1

    def __getattr__(self, k):            # pos, pending, hora, P, htf, ctx… del motor
        return getattr(self._m, k)

    def alimentar(self, c):
        """Añade una vela del bot. Devuelve las velas propias que se cierran con ella (normalmente 0 o 1)."""
        t, o, h, l, cl = c
        b, out = inicio_vela(t, self.tf_ms), []
        if self.cur is not None and self.cur[0] != b:      # faltan velas del bot: la anterior se cierra tal cual
            out.append(tuple(self.cur))
            self.cur = None
        if self.cur is None:
            self.cur = [b, o, h, l, cl]
        else:
            self.cur[2], self.cur[3], self.cur[4] = max(self.cur[2], h), min(self.cur[3], l), cl
        if t + self._m.tf_ms >= b + self.tf_ms:
            out.append(tuple(self.cur))
            self.cur = None
        return out

    def cerrar(self, v):
        """Una vela propia cerrada: ATR y contador (antes de que la estrategia la vea)."""
        _t, _o, h, l, _c = v
        pv = self.vela
        tr = h - l if pv is None else max(h - l, abs(h - pv[4]), abs(l - pv[4]))
        self.prev, self.vela = pv, v
        self.atr = self._atr.update(tr)
        self.i += 1


def tf_de(P, sid, principal):
    """Temporalidad (ms) con la que trabaja una de las estrategias de un bot."""
    v = P.get("tf_principal") if principal else P.get(clave_sub(sid, "tf_estr"))
    return TF_MS.get(v) if v and v != TF_DEL_BOT else TF_MS[P["timeframe"]]


class Compuesta:
    """Varias estrategias en un mismo bot (un solo instrumento, una operación a la vez).

    Todas actualizan sus indicadores y miran señales en cada vela. El motor abre con la primera señal válida en el
    orden de prioridad (puntuación) y, mientras esa operación o su orden siga viva, ignora las demás: se cuentan
    como bloqueadas. La estrategia que abrió (la «dueña») decide objetivo, break-even y salida de su operación."""

    def __init__(self, P, motor, partes, mapas=None):
        self.P, self.m = P, motor
        self.ids = [p.id for p in partes]
        self.nombres = {p.id: p.nombre for p in partes}
        mapas = mapas or {}
        base_ms = TF_MS[P["timeframe"]]
        self.vistas, self.tfs = {}, {}
        for k, p in enumerate(partes):
            ms = tf_de(P, p.id, k == 0)
            self.tfs[p.id] = ms
            if ms != base_ms:
                self.vistas[p.id] = VistaTF(motor, ms)
        self.toca = {sid: sid not in self.vistas for sid in self.ids}   # estrategias con vela propia cerrada ahora
        self.subs = {p.id: p.mod.Estrategia(ajustes_sub(P, mapas.get(p.id)), self.vistas.get(p.id, motor))
                     for p in partes}
        self.lmap = {sid: (mapas.get("_lineas") or {}).get(sid, {}) for sid in self.ids}
        self.orden = list(self.ids)
        self.activa = None
        self.bloqueadas = {}
        self._vela_bloq = {}

    # --- prioridad (la fija el programa con las puntuaciones) ---
    def ordenar(self, ids):
        ids = [x for x in (ids or []) if x in self.subs]
        self.orden = ids + [x for x in self.ids if x not in ids]

    def usar(self, sid):
        if sid in self.subs:
            self.activa = sid

    def duena(self, pos=None):
        sid = (pos or self.m.pos or self.m.pending or {}).get("origen") or self.activa or self.ids[0]
        return self.subs.get(sid) or self.subs[self.ids[0]]

    def bloquear(self, sid):
        """Una señal de `sid` ignorada porque ya hay una operación u orden de otra estrategia (una por vela)."""
        if sid in self.subs and self._vela_bloq.get(sid) != self.m.i:
            self._vela_bloq[sid] = self.m.i
            self.bloqueadas[sid] = self.bloqueadas.get(sid, 0) + 1

    # --- interfaz de Estrategia ---
    def actualizar(self, vela):
        for sid in self.ids:
            v = self.vistas.get(sid)
            if v is None:
                self.subs[sid].actualizar(vela)
                continue
            cerradas = v.alimentar(vela)
            self.toca[sid] = bool(cerradas)
            for k, c in enumerate(cerradas):
                v.cerrar(c)
                self.subs[sid].actualizar(c)
                if k < len(cerradas) - 1:      # vela atrasada (faltaban velas del bot): se ve pero no da señales
                    self.subs[sid].senales()

    def senales(self):
        out = []
        for sid in self.orden:
            if not self.toca.get(sid):
                continue
            for sig in self.subs[sid].senales() or ():
                if isinstance(sig, dict):
                    out.append(dict(sig, origen=sid))
        return out

    def rr(self):
        return self.duena().rr()

    def objetivo(self, pos):
        return self.duena(pos).objetivo(pos)

    def gestion_be(self, pos):
        d = self.duena(pos)
        return d.gestion_be(pos) if hasattr(d, "gestion_be") else None

    def nivel_be(self, pos):
        return self.duena(pos).nivel_be(pos)

    def salida(self, pos):
        sid = (pos or {}).get("origen") or self.activa or self.ids[0]
        if not self.toca.get(sid, True):        # su salida se mira al cierre de sus propias velas
            return None
        return self.duena(pos).salida(pos)

    def lineas(self):
        out = {}
        for sid in self.ids:
            ren = self.lmap.get(sid) or {}
            for k, v in (self.subs[sid].lineas() or {}).items():
                out[ren.get(k, k)] = v
        return out

    def texto(self):
        if self.m.pos:
            sid = self.m.pos.get("origen") or self.ids[0]
            return "{}: {}".format(self.nombres[sid], self.subs[sid].texto() or "gestionando la operación")
        partes = ["{}: {}".format(self.nombres[sid], self.subs[sid].texto()) for sid in self.orden if self.subs[sid].texto()]
        return " · ".join(partes)

    def indicadores(self):
        return " | ".join(x for x in (self.subs[sid].indicadores() for sid in self.ids) if x)

    def guardar(self):
        return {"_compuesta": {sid: self.subs[sid].guardar() for sid in self.ids}, "_bloqueadas": dict(self.bloqueadas),
                "_activa": self.activa, "_tfs": {sid: self.tfs[sid] for sid in self.ids}}

    def cargar(self, s):
        s = s or {}
        if "_compuesta" in s:
            tfs = s.get("_tfs") or {}
            for sid, st in (s.get("_compuesta") or {}).items():
                if sid in tfs and tfs[sid] != self.tfs.get(sid):
                    continue                    # le han cambiado la temporalidad: empieza con lo que ve el historial
                if sid in self.subs and st is not None:
                    self.subs[sid].cargar(st)
            self.bloqueadas = {k: int(v) for k, v in (s.get("_bloqueadas") or {}).items() if k in self.subs}
            self.activa = s.get("_activa") if s.get("_activa") in self.subs else None
        else:                                   # estado de cuando el bot tenía una sola estrategia
            self.subs[self.ids[0]].cargar(s)

    def reiniciar(self):
        for sid in self.ids:
            self.subs[sid].reiniciar()
        self.bloqueadas, self._vela_bloq, self.activa = {}, {}, None


class PluginCompuesto:
    """Se comporta como el Plugin de la estrategia principal, con los ajustes, filtros, salidas y líneas de todas."""

    def __init__(self, base, extras):
        self.partes = [base] + list(extras)
        self.path, self.file, self.id = base.path, base.file, base.id
        self.nombre = " + ".join(p.nombre for p in self.partes)[:120]
        self.version, self.autor, self.error = base.version, base.autor, None
        self.descripcion = base.descripcion
        self.sha = hashlib.sha256("".join(p.sha for p in self.partes).encode()).hexdigest()
        self.loaded_at = max(p.loaded_at for p in self.partes)
        schema, claves = [dict(f) for f in base.schema], {f["key"] for f in base.schema}
        lineas, lkeys = list(base.lineas), {str(x.get("key")) for x in base.lineas if isinstance(x, dict)}
        self.filtros, self.salidas, self.legacy = dict(base.filtros), dict(base.salidas), dict(base.legacy)
        defaults = dict(base.defaults)
        self.mapas = {"_lineas": {}}
        for ex in extras:
            # los ajustes propios de cada estrategia añadida llevan su id delante (así nunca chocan con los de otra);
            # al crearla se le pasan con su nombre original (ajustes_sub)
            mapa = {f["key"]: clave_sub(ex.id, f["key"]) for f in ex.schema if not f.get("common")}
            self.mapas[ex.id] = mapa
            ktf, kdir = clave_sub(ex.id, "tf_estr"), clave_sub(ex.id, "direction")
            gtd = "{} · {}".format(ex.nombre, GRUPO_TFDIR)
            schema.append({"key": ktf, "group": gtd, "label": "Temporalidad",
                           "type": "select", "default": TF_DEL_BOT, "options": list(TF_ESTR), "help": TF_AYUDA,
                           "estrategia": ex.id})
            schema.append({"key": kdir, "group": gtd, "label": "Dirección", "type": "select", "default": "ambos",
                           "options": list(DIRECCIONES), "help": DIR_AYUDA, "estrategia": ex.id})
            defaults[ktf], defaults[kdir] = TF_DEL_BOT, "ambos"
            claves.update((ktf, kdir))
            for f in ex.schema:
                if f.get("common"):
                    continue
                g = dict(f, key=mapa[f["key"]], group="{} · {}".format(ex.nombre, f["group"]), estrategia=ex.id)
                if f.get("show_if"):
                    g["show_if"] = {mapa.get(k, k): v for k, v in f["show_if"].items()}
                claves.add(g["key"])
                schema.append(g)
                defaults[g["key"]] = ex.defaults[f["key"]]
            for ln in ex.lineas:
                if not isinstance(ln, dict):
                    continue
                k = str(ln.get("key"))
                if k in lkeys:              # misma línea en dos estrategias: la de la añadida cambia de nombre
                    nk = clave_sub(ex.id, k)
                    self.mapas["_lineas"].setdefault(ex.id, {})[k] = nk
                    ln = dict(ln, key=nk)
                    k = nk
                lineas.append(ln)
                lkeys.add(k)
            self.filtros.update(ex.filtros)
            self.salidas.update(ex.salidas)
            self.legacy.update(ex.legacy)
        self.schema, self.lineas, self.defaults = schema, lineas, defaults
        self.by_key = {f["key"]: f for f in schema}
        self.reload_keys = tuple(dict.fromkeys(
            [self.mapas.get(p.id, {}).get(k, k) for p in self.partes for k in p.reload_keys]
            + [clave_sub(p.id, "tf_estr") for p in extras]))
        partes, mapas = self.partes, self.mapas

        class _Mod:
            NOMBRE = self.nombre

            @staticmethod
            def Estrategia(P, motor):
                return Compuesta(P, motor, partes, mapas)

            @staticmethod
            def calentamiento(P):
                base = TF_MS[P["timeframe"]]
                return max(max(int(getattr(p.mod, "calentamiento", lambda _P: 200)(ajustes_sub(P, mapas.get(p.id)))),
                               4 * P["atr_len"]) * tf_de(P, p.id, k == 0) // base for k, p in enumerate(partes))

            @staticmethod
            def validar(P):
                errs = []
                base = TF_MS[P["timeframe"]]
                for k, p in enumerate(partes):
                    ms = tf_de(P, p.id, k == 0)
                    if ms < base or ms % base:
                        errs.append("{}: su temporalidad tiene que ser igual o mayor que la del bot ({}) y múltiplo "
                                    "de ella".format(p.nombre, P["timeframe"]))
                for p in partes:
                    fn = getattr(p.mod, "validar", None)
                    if fn:
                        errs += ["{}: {}".format(p.nombre, e) for e in (fn(ajustes_sub(P, mapas.get(p.id))) or [])]
                return errs
        self.mod = _Mod

    def info(self, used=0):
        return {"id": self.id, "nombre": self.nombre, "version": self.version, "descripcion": self.descripcion,
                "autor": self.autor, "archivo": self.file, "sha": self.sha[:10], "error": None,
                "cargada": self.loaded_at, "bots": used, "lineas": self.lineas}


_COMPUESTOS = {}


def clave_sub(sid, key):
    """Nombre interno de un ajuste propio de una estrategia añadida a un bot (p. ej. supertrend__ema_on)."""
    return "{}__{}".format(sid, key)


def ajustes_sub(P, mapa):
    """Los ajustes del bot tal como los ve una estrategia añadida: sus ajustes propios con su nombre original."""
    if not mapa:
        return P
    Q = dict(P)
    for k, pk in mapa.items():
        if pk in P:
            Q[k] = P[pk]
    return Q


def ajustes_de(P, sid):
    """Ajustes para probar una de las estrategias de un bot combinado ella sola (puntuación), en su temporalidad."""
    if sid == P.get("estrategia") or not P.get("combinar"):
        return dict(P, estrategia=sid, combinar=[])
    pl = plugin_for(P)
    Q = ajustes_sub(P, getattr(pl, "mapas", {}).get(sid))
    return dict(Q, estrategia=sid, combinar=[], tf_principal=P.get(clave_sub(sid, "tf_estr")) or TF_DEL_BOT,
                direction=P.get(clave_sub(sid, "direction")) or "ambos")


def sin_estrategia(P, sid):
    """Los ajustes del bot sin una de sus estrategias (para comparar). Si se quita la principal, la primera añadida
    pasa a principal con sus ajustes, su temporalidad y su dirección."""
    partes = partes_de(P)
    if sid not in partes or len(partes) < 2:
        return dict(P)
    if sid != partes[0]:
        Q = {k: v for k, v in P.items() if not k.startswith(sid + "__")}
        Q["combinar"] = [x for x in partes[1:] if x != sid]
        return Q
    nueva, resto = partes[1], partes[2:]
    Q = ajustes_de(P, nueva)
    Q = {k: v for k, v in Q.items() if not k.startswith(nueva + "__")}
    Q["combinar"] = list(resto)
    return Q


def direccion_de(P, sid):
    """Lados que opera una de las estrategias del bot (la principal usa «direction»)."""
    if not sid or sid == P.get("estrategia"):
        return P.get("direction") or "ambos"
    return P.get(clave_sub(sid, "direction")) or P.get("direction") or "ambos"


def _compuesto(sid, extras):
    base = plugin_for(sid)
    if sid in NO_COMBINABLES:
        raise StrategyError("«{}» va siempre en su propio bot: no se combina con otras estrategias".format(base.nombre))
    ps = []
    for x in extras:
        if x == sid:
            continue
        if x in NO_COMBINABLES:
            raise StrategyError("«{}» no se puede combinar con otras estrategias".format(x))
        ps.append(plugin_for(x))
    key = tuple((p.id, p.sha) for p in [base] + ps)
    if key not in _COMPUESTOS:
        if len(_COMPUESTOS) > 50:
            _COMPUESTOS.clear()
        _COMPUESTOS[key] = PluginCompuesto(base, ps)
    return _COMPUESTOS[key]


def partes_de(P):
    """Ids de las estrategias de un bot (la principal primero)."""
    return [P.get("estrategia") or DEFAULT_STRATEGY] + [x for x in (P.get("combinar") or []) if x != P.get("estrategia")]


def usa_compuesta(P):
    return bool(P.get("combinar")) or (P.get("tf_principal") or TF_DEL_BOT) != TF_DEL_BOT


def plugin_for(P_or_id, plugin=None):
    if plugin is not None:
        return plugin
    if isinstance(P_or_id, dict) and usa_compuesta(P_or_id):
        return _compuesto(P_or_id.get("estrategia") or DEFAULT_STRATEGY, list(P_or_id.get("combinar") or []))
    sid = P_or_id if isinstance(P_or_id, str) else (P_or_id or {}).get("estrategia") or DEFAULT_STRATEGY
    p = REGISTRY.get(sid) if REGISTRY else None
    if p is None:
        why = REGISTRY.any(sid).error if REGISTRY and REGISTRY.any(sid) else "no está instalada"
        raise StrategyError("La estrategia «{}» no se puede usar: {}".format(sid, why))
    return p


# ============================ validación ============================
def _coerce(f, v, legacy):
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
    if t == "strategy":
        return str(v)
    if t == "combinar":
        if isinstance(v, str):
            v = [x.strip() for x in v.split(",") if x.strip()]
        if not isinstance(v, (list, tuple)):
            raise ValueError
        out = []
        for x in v:
            x = str(x)
            if not ID_RE.match(x):
                raise ValueError
            if x not in out:
                out.append(x)
        if len(out) > MAX_COMBINADAS:
            raise ValueError
        return out
    if t == "cuentas":
        if isinstance(v, str):
            v = [x.strip() for x in v.split(",") if x.strip()]
        if not isinstance(v, (list, tuple)):
            raise ValueError
        out = []
        for x in v:
            x = str(x)
            if not CUENTA_RE.match(x):
                raise ValueError
            if x not in out:
                out.append(x)
        if len(out) > MAX_CUENTAS_BOT:
            raise ValueError
        return out
    v = str(v)
    v = legacy.get(f["key"], {}).get(v, v)
    if v not in f["options"]:
        raise ValueError
    return v


def _validate_with(p, raw):
    P, errs = _copia(p.defaults), []
    legacy = dict(LEGACY_COMMON, **p.legacy)
    for k, v in (raw or {}).items():
        f = p.by_key.get(k)
        if f is None or k == "estrategia":
            continue
        try:
            x = _coerce(f, v, legacy)
        except (TypeError, ValueError):
            errs.append("{}: valor no válido".format(f["label"]))
            continue
        if f["type"] in ("int", "float"):
            if "min" in f and x < f["min"] or "max" in f and x > f["max"]:
                errs.append("{}: debe estar entre {:g} y {:g}".format(f["label"], f.get("min", -1e18), f.get("max", 1e18)))
                continue
        P[k] = x
    if P["tr_mode"] != "no" and HTF_MS[P["tr_tf"]] <= TF_MS[P["timeframe"]]:
        errs.append("La temporalidad del filtro de tendencia tiene que ser mayor que la del bot")
    if P["tr_mode"] != "no" and P["tr2_tf"] != "no":
        if HTF_MS[P["tr2_tf"]] <= TF_MS[P["timeframe"]]:
            errs.append("La temporalidad de la confirmación tiene que ser mayor que la del bot")
        if P["tr2_tf"] == P["tr_tf"] and P["tr2_mode"] == P["tr_mode"]:
            errs.append("La confirmación con la misma temporalidad tiene que usar otro método")
    for px, nom in (("tr_", "filtro de tendencia"), ("tr2_", "confirmación")):
        if P[px + "mode"] == "Dos EMAs" and P[px + "ema_fast"] >= P[px + "ema_len"]:
            errs.append("En el {} con dos EMAs, la rápida tiene que ser menor que la lenta".format(nom))
    if P["g_unit"] == "USD" and P["risk_usd"] <= 0:
        errs.append("Para límites en USD indica el riesgo por operación")
    if P["hr_on"] and P["hr_from"] == P["hr_to"] % 24:
        errs.append("El horario no puede empezar y terminar a la misma hora")
    if P.get("combinar"):
        P["combinar"] = [x for x in P["combinar"] if x != P.get("estrategia")]
    fn = getattr(p.mod, "validar", None) if p.mod else None
    if fn:
        try:
            errs += [str(e) for e in (fn(P) or [])]
        except Exception as e:
            errs.append("La validación de la estrategia ha fallado: {}".format(e))
    return P, errs


def validate(raw):
    """Devuelve (params completos, errores). Si la estrategia no está disponible, conserva los ajustes tal cual
    para no perderlos y devuelve el error."""
    raw = dict(raw or {})
    sid = str(raw.get("estrategia") or DEFAULT_STRATEGY)
    try:
        comb = raw.get("combinar")
        if isinstance(comb, str):
            comb = [x.strip() for x in comb.split(",") if x.strip()]
        p = _compuesto(sid, [str(x) for x in (comb or [])][:MAX_COMBINADAS + 1]) if comb or usa_compuesta(raw) \
            else plugin_for(sid)
        base_own = {f["key"] for f in plugin_for(sid).schema}
        for x, mapa in (getattr(p, "mapas", None) or {}).items():
            if x == "_lineas":
                continue
            for k, pk in mapa.items():          # ajustes guardados por la 9.1 antes de los nombres internos
                if pk not in raw and k in raw and k not in base_own:
                    raw[pk] = raw[k]
            kd = clave_sub(x, "direction")       # hasta la 9.2 la dirección era del bot y valía para todas
            if kd not in raw and clave_sub(x, "tf_estr") in raw and raw.get("direction") in DIRECCIONES:
                raw[kd] = raw["direction"]
    except StrategyError as e:
        P = dict(raw)
        P["estrategia"] = sid
        for f in COMMON:
            P.setdefault(f["key"], f["default"])
        P["estrategia"] = sid
        return P, [str(e)]
    return _validate_with(p, raw)


def _copia(P):
    return {k: list(v) if isinstance(v, list) else v for k, v in P.items()}


def defaults(sid=None):
    p = plugin_for(sid or (REGISTRY.default_id() if REGISTRY else DEFAULT_STRATEGY))
    return _copia(p.defaults)


def schema_for(P):
    try:
        return plugin_for(P).schema
    except StrategyError:
        return [dict(f, common=True) for f in COMMON]


def from_env(vals):
    """Convierte un config.env de la versión 1.x (FP_*) en ajustes (con la estrategia Keltner + RSI)."""
    p = plugin_for(DEFAULT_STRATEGY)
    raw = {"estrategia": DEFAULT_STRATEGY}
    for f in p.schema:
        name = f.get("env")
        if name and ("FP_" + name) in vals and vals["FP_" + name] != "":
            raw[f["key"]] = vals["FP_" + name]
    P, errs = validate(raw)
    return P, errs, sorted(k for k in raw if k != "estrategia")


def cost_pct(P, instrument_cost=None):
    if not P["cost_auto"]:
        return P["cost_pct"]
    return instrument_cost if instrument_cost is not None else AUTO_COST.get(P["symbol"], 0.01)


def warmup_bars(P, plugin=None):
    p = plugin_for(P, plugin)
    fn = getattr(p.mod, "calentamiento", None)
    need = int(fn(P)) if fn else 200
    return max(need, 4 * P["atr_len"])


# ============================ motor: una estrategia + la gestión común ============================
class Motor:
    """Recibe velas cerradas una a una (igual en backtest y en directo)."""

    def __init__(self, P, hour_fn=None, instrument_cost=None, plugin=None):
        self.P = P
        self.plugin = plugin_for(P, plugin)
        self.cost = cost_pct(P, instrument_cost)
        self.tf_ms = TF_MS[P["timeframe"]]
        self.hour_fn = hour_fn or (lambda t: ((t // 3_600_000) % 24, ((t // 86_400_000) + 3) % 7))
        self._atr = RMA(P["atr_len"])
        self.atr = None
        self.htf = Tendencia(P) if P["tr_mode"] != "no" else None
        self.prev = None
        self.vela = None
        self.pending = None
        self.registro = None      # lista en el backtest: (t, estrategia, lado, resultado, detalle)
        self.pos = None
        self.i = -1
        self.cool = 0
        self.reset_day_state()
        self.block_days = {}
        self.days_seen = 0
        self.skipped = 0
        self.filtered = {}
        self.filter_names = dict(FILTER_NAMES, **self.plugin.filtros)
        self.S = self.plugin.mod.Estrategia(P, self)
        self.ctx = Contexto()           # contexto de cada operación (solo se anota; no cambia ninguna decisión)

    def hora(self, t):
        return self.hour_fn(t)

    @property
    def last(self):
        """Líneas de la última vela (compatibilidad)."""
        return self.S.lineas() or {}

    def reset_day_state(self):
        self.day, self.dtrades, self.dconsec, self.dr, self.blocked = None, 0, 0, 0.0, None

    # --- estado ---
    def dump(self):
        return {"pos": self.pos, "pending": self.pending, "day": self.day, "dtrades": self.dtrades,
                "dconsec": self.dconsec, "dr": self.dr, "blocked": self.blocked, "cool": self.cool,
                "estrategia": self.S.guardar(), "estrategia_id": self.plugin.id, "partes": partes_de(self.P)}

    def load(self, s, strategy_state=True):
        self.pos, self.pending = s.get("pos"), s.get("pending")
        self.day, self.dtrades, self.dconsec = s.get("day"), s.get("dtrades", 0), s.get("dconsec", 0)
        self.dr, self.blocked, self.cool = s.get("dr", 0.0), s.get("blocked"), s.get("cool", 0)
        if strategy_state:
            st = s.get("estrategia") if "estrategia" in s else s       # estados v3: todo en la raíz
            if st is not None:
                self.S.cargar(st)
        if self.pos:
            self.fix_pos()

    def fix_pos(self):
        p = self.pos
        for k, v in (("part", None), ("be", False), ("ext", 0.0), ("ext_n", 0), ("trail", False),
                     ("no_tp", False), ("sl_moved", False)):
            p.setdefault(k, v)
        if p.get("modo_tp") != "fijo":
            p["modo_tp"] = "rr" if self.S.rr() is not None else "dinamico"

    def reset_setups(self):
        self.pos = self.pending = None
        self.S.reiniciar()

    def seed_htf(self, bars, hasta=None):
        """bars: {temporalidad: velas} de tendencia_fuentes() (o una lista de la principal). Solo usa las que
        cierran antes de `hasta` (la primera vela del bot)."""
        if self.htf and bars:
            self.htf.seed(bars, hasta)

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

    def _filter(self, t, side, cl, sl, atr, own, origen=None):
        P = self.P
        d = direccion_de(P, origen)
        if d == "solo largos" and side == -1 or d == "solo cortos" and side == 1:
            return "direction"
        if self.cool > 0:
            return "cooldown"
        if P["hr_on"]:
            hour, wd = self.hour_fn(t + self.tf_ms)
            f, to = P["hr_from"], P["hr_to"] % 24
            inside = f <= hour < to if f < to else (hour >= f or hour < to)
            if not inside or (P["hr_weekdays"] and wd >= 5):
                return "hours"
        if self.htf:
            why = self.htf.rechazo(side)
            if why:
                return why
        if own:
            return str(own)
        dist = (cl - sl) * side
        if P["sl_min_atr"] and atr is not None and dist < P["sl_min_atr"] * atr:
            return "sl_min"
        if P["sl_max_atr"] and atr is not None and dist > P["sl_max_atr"] * atr:
            return "sl_max"
        return None

    @staticmethod
    def _gross(p, px):
        part = p.get("part")
        if part:
            f, pp = part
            return (f * (pp - p["entry"]) + (1 - f) * (px - p["entry"])) * p["side"]
        return (px - p["entry"]) * p["side"]

    def _r(self, p, px):
        return (self._gross(p, px) - p["entry"] * self.cost / 100) / p["dist"]

    @staticmethod
    def _sl_why(p):
        if p.get("trail_moved"):
            return "TRAILING"
        if p.get("sl_moved"):
            return "SL MOVIDO"
        return "BE" if p["be"] else "SL"

    def _dynamic(self, t, h, l, cl, atr, ev):
        P, p = self.P, self.pos
        if p.get("espejo"):
            return
        s = p["side"]
        if P["ts_on"]:
            if not p.get("trail") and (cl - p["entry"]) * s / p["dist"] >= P["ts_start_r"]:
                p["trail"] = True
                if P["ts_no_tp"] and not p.get("tp_manual"):
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
        xr = (px - p["entry"]) * p["side"] / p["dist"] if p.get("dist") else 0.0
        part = p.get("part")
        ev.append(("exit", t, {
            "side": p["side"], "t_open": p["t_open"], "t_close": t, "entry": p["entry"], "exit": px,
            "sl": p["sl0"], "sl_final": p["sl"], "tp": p.get("tp"), "r": r, "why": why,
            "stop_pct": 100 * p["dist"] / p["entry"], "bars": p["bars"],
            "part_pct": round(part[0] * 100, 2) if part else 0, "part_px": part[1] if part else None,
            "ext_n": p.get("ext_n", 0), "trail": bool(p.get("trail")),
            "mfe": round(max(p.get("mfe", 0.0), xr), 3), "mae": round(min(p.get("mae", 0.0), xr), 3),
            "path": list(p.get("path") or []), "trend": p.get("trend"), "inv": p.get("inv"),
            "r_bruto": round(self._gross(p, px) / p["dist"], 4) if p.get("dist") else 0.0, "ctx": p.get("ctx"),
            "estrategia": p.get("origen") or self.plugin.id}))
        self.pos = None
        self.ctx.operacion_cerrada(r)
        self.dr += r
        self.dconsec = self.dconsec + 1 if r < 0 else 0
        self.cool = self.P["cooldown"] + 1 if self.P["cooldown"] else 0
        self._check_limits(t, ev)

    def cerrar_manual(self, t, px):
        """Cierre manual desde el panel, al precio dado. Devuelve los eventos (la salida)."""
        ev = []
        if self.pos:
            self._close(t, px, "MANUAL", ev)
        return ev

    def _target(self, p):
        tp = self.S.objetivo(p)
        if tp is not None:
            tp += p["side"] * p.get("ext", 0.0)
        ok = tp is not None and (tp - p["entry"]) * p["side"] > 0 and not p.get("no_tp")
        return tp if ok else None

    # --- vela a vela ---
    def on_candle(self, c):
        P, S = self.P, self.S
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
        atr = self.atr = self._atr.update(tr)
        self.ctx.actualizar(c, atr)
        if self.htf:
            self.htf.on_base(c, self.tf_ms)
        self.vela = c
        S.actualizar(c)

        # 1) entrada pendiente: apertura de esta vela (o, si es una orden límite, cuando el precio la toca)
        entrar, px0 = bool(self.pending) and not self.pos, o
        if entrar and self.pending.get("forzar") is not None:
            px0 = float(self.pending["forzar"])          # orden límite ya llenada en la cuenta (vigilada en vivo)
        elif entrar and self.pending.get("limite") is not None:
            pend = self.pending
            lim, s_l, tp_l = float(pend["limite"]), pend["side"], pend.get("tp")
            hueco = (o - lim) * s_l <= 0             # abre ya al otro lado del límite: se llena en la apertura
            toca = h >= lim if s_l == -1 else l <= lim
            toca_tp = tp_l is not None and (l <= tp_l if s_l == -1 else h >= tp_l)
            if hueco:
                px0 = o
            elif toca and not toca_tp:
                px0 = lim
            else:
                # sin tocar: sigue esperando. Si llega antes al objetivo, o toca límite y objetivo en la misma vela
                # (no se sabe qué fue primero), la orden se retira: lo conservador es no contar esa operación
                entrar = False
                if toca_tp:
                    self.pending = None
                    self.limite_fin = (t, "objetivo")
                    ev.append(("limit_cancel", t, s_l, "objetivo"))
        if entrar:
            pend = self.pending
            if hasattr(S, "usar"):
                S.usar(pend.get("origen"))          # estrategias combinadas: la que dio la señal manda en esta operación
            side, sl = pend["side"], pend["sl"]
            self.pending = None
            o_real, o = o, px0                       # precio de entrada: la apertura o el límite
            fixed = pend.get("tp")
            esp = pend.get("espejo")
            if esp:
                # espejo exacto: la operación original (lado contrario) con su stop y su objetivo, intercambiados
                s0 = -side
                d0 = (o - esp["sl"]) * s0
                rr0 = S.rr()
                tp0 = esp["tp"] if esp["tp"] is not None else (o + s0 * rr0 * d0 if rr0 is not None and d0 > 0 else None)
                fixed = esp["sl"]
                sl = tp0 if tp0 is not None and (tp0 - o) * s0 > 0 else o     # sin objetivo válido: se descarta
            dist = (o - sl) if side == 1 else (sl - o)
            cost_r = self.cost / 100 * o / dist if dist > 0 else 0
            if fixed is not None and (fixed - o) * side <= 0:
                ev.append(("skip_gap", t, side))
            elif dist > 0 and P["max_cost"] and cost_r > P["max_cost"]:
                self.skipped += 1
                ev.append(("skip", t, cost_r))
            elif dist > 0:
                rr = S.rr()
                modo = "fijo" if fixed is not None else "rr" if rr is not None else "dinamico"
                tp = fixed if modo == "fijo" else o + side * rr * dist if modo == "rr" else None
                self.pos = {"side": side, "entry": o, "sl": sl, "sl0": sl, "tp": tp, "dist": dist, "i": self.i,
                            "t_open": t, "bars": 0, "be": False, "part": None, "ext": 0.0, "ext_n": 0,
                            "trail": False, "no_tp": False, "sl_moved": False, "modo_tp": modo,
                            "trend": self.htf.texto_corto() if self.htf else None,
                            "inv": pend.get("inv"), "espejo": bool(esp), "origen": pend.get("origen")}
                if pend.get("ctx") is not None:
                    self.pos["ctx"] = Contexto.al_abrir(pend["ctx"], o, pend.get("cl", o), pend.get("atr"), cost_r, side)
                if modo == "dinamico":
                    self.pos["tp"] = S.objetivo(self.pos)
                self.dtrades += 1
                self.ult_apertura = (t, o, side)         # aunque cierre en esta misma vela (planes manuales)
                ev.append(("open", t, side, o, sl, self.pos["tp"]))
            else:
                ev.append(("skip_gap", t, side))
            o = o_real

        # 2) gestión intravela (si la vela toca el SL y otro nivel, se asume el SL)
        gb = None
        if self.pos and not self.pos.get("be") and hasattr(S, "gestion_be"):
            try:
                gb = S.gestion_be(self.pos)
            except Exception:
                gb = None
        if self.pos:
            p = self.pos
            s = p["side"]
            if p["modo_tp"] == "dinamico":
                p["tp"] = self._target(p)
            hit_sl = l <= p["sl"] if s == 1 else h >= p["sl"]
            hit_tp = p["tp"] is not None and (h >= p["tp"] if s == 1 else l <= p["tp"])
            if hit_sl:
                self._close(t, p["sl"], self._sl_why(p), ev)
            elif hit_tp:
                px = p["tp"]
                if p["modo_tp"] == "dinamico" and (o - px) * s > 0:
                    px = o
                self._close(t, px, "TP", ev)
            elif (P["be_on"] or gb) and not p["be"] and not p.get("espejo"):
                if gb:            # gestión propia de esta operación (p. ej. el TP1 de un plan manual)
                    trig, f, off = gb.get("nivel"), max(0.0, min(0.95, float(gb.get("pct") or 0) / 100)), gb.get("off", 0.0)
                else:
                    trig = p["entry"] + s * P["be_r"] * p["dist"] if P["be_trigger"] == "R" else S.nivel_be(p)
                    f, off = P["be_pct"] / 100, P["be_off"]
                if trig is not None and (trig - p["entry"]) * s > 0 and (h >= trig if s == 1 else l <= trig):
                    px = o if (o - trig) * s > 0 else trig
                    if f > 0 and not p.get("part"):      # si ya vendiste una parte a mano, no se vende otra
                        p["part"] = (f, px)
                    p["be"] = True
                    if off is not None:                  # None: vende la parte pero deja el stop donde estaba
                        p["sl"] = p["entry"] + s * off * p["dist"]
                    ev.append(("partial", t, s, px, f, p["sl"]))
                    if (l <= p["sl"] if s == 1 else h >= p["sl"]) and (px - p["sl"]) * s > 0:
                        self._close(t, p["sl"], "BE", ev)

        # 3) salidas al cierre de la vela
        if self.pos:
            p = self.pos
            why = None if p.get("espejo") else S.salida(p)
            if not why and P["max_bars"] and p["bars"] + 1 >= P["max_bars"]:
                why = "TIEMPO"
            if why:
                self._close(t, cl, str(why)[:30], ev)
        if self.pos and self.pos.get("dist"):
            # Recorrido máximo a favor (MFE) y en contra (MAE), en R de precio, con las velas que la operación
            # sobrevive entera. La vela de salida se cuenta solo hasta el precio de salida (ver _close).
            p = self.pos
            fav = ((h if p["side"] == 1 else l) - p["entry"]) * p["side"] / p["dist"]
            adv = ((l if p["side"] == 1 else h) - p["entry"]) * p["side"] / p["dist"]
            p["mfe"] = max(p.get("mfe", 0.0), fav)
            p["mae"] = min(p.get("mae", 0.0), adv)
        if self.pos and atr is not None:
            self._dynamic(t, h, l, cl, atr, ev)
        if self.pos:
            p = self.pos
            p["bars"] += 1
            pa = p.setdefault("path", [])           # recorrido del stop y del objetivo (para el gráfico)
            if not pa or pa[-1][1] != p["sl"] or pa[-1][2] != p.get("tp"):
                pa.append([t, p["sl"], p.get("tp")])
                if len(pa) > 400:
                    del pa[1]

        # 4) señales de la estrategia
        reg, tcl = self.registro, t + self.tf_ms
        for sig in S.senales() or ():
            side, sl = sig.get("side"), sig.get("sl")
            if side not in (1, -1) or sl is None:
                continue
            org = sig.get("origen") or self.plugin.id
            if self.pos or self.pending:
                due = (self.pos or self.pending or {}).get("origen")
                if sig.get("origen") and sig["origen"] != due and hasattr(S, "bloquear"):
                    S.bloquear(sig["origen"])
                if reg is not None:
                    reg.append((tcl, org, side, "ocupada", due or self.plugin.id))
                continue
            self._check_limits(t, ev)
            if self.blocked:
                if reg is not None:
                    reg.append((tcl, org, side, "limite", self.blocked))
                continue
            inv, tp_sig, esp = P.get("inv", "no"), sig.get("tp"), None
            if inv != "no":
                s0, sl0 = side, float(sl)
                side, sl = -side, cl + (cl - sl0)            # lado contrario, stop a la misma distancia
                if inv == "espejo exacto":
                    rr0 = S.rr()
                    tp0 = float(tp_sig) if tp_sig is not None else (
                        cl + rr0 * (cl - sl0) if rr0 is not None else None)
                    esp = {"sl": sl0, "tp": float(tp_sig) if tp_sig is not None else None}
                    tp_sig = None
                    if tp0 is None:
                        self.filtered["espejo_sin_objetivo"] = self.filtered.get("espejo_sin_objetivo", 0) + 1
                        ev.append(("filtered", t, side, "espejo_sin_objetivo"))
                        if reg is not None:
                            reg.append((tcl, org, side, "filtro", "espejo_sin_objetivo"))
                        continue
                    sl = tp0                                  # el stop, donde estaría el objetivo
                elif tp_sig is not None:
                    tp_sig = cl - (float(tp_sig) - cl)
            lim = sig.get("limite") if inv == "no" else None      # orden límite (solo sin señales invertidas)
            why = self._filter(t, side, float(lim) if lim is not None else cl, sl, atr, sig.get("rechazo"),
                               sig.get("origen"))
            if reg is not None:
                reg.append((tcl, org, side, "filtro" if why else "entra", why or ("limite" if lim is not None else "")))
            if why:
                self.filtered[why] = self.filtered.get(why, 0) + 1
                ev.append(("filtered", t, side, why))
            else:
                self.pending = {"side": side, "sl": float(sl)}
                try:
                    foto = self.ctx.foto(self, side, cl, float(sl))
                except Exception:                  # el contexto nunca puede parar al bot
                    foto = None
                if foto:
                    self.pending.update({"ctx": foto, "cl": cl, "atr": atr})
                if esp:
                    self.pending["espejo"] = esp
                elif tp_sig is not None:
                    self.pending["tp"] = float(tp_sig)
                if inv != "no":
                    self.pending["inv"] = inv
                if lim is not None:
                    self.pending["limite"] = float(lim)
                if sig.get("tamano"):
                    self.pending["tamano"] = float(sig["tamano"])     # tamaño fijo para la cuenta (planes manuales)
                if sig.get("origen"):
                    self.pending["origen"] = sig["origen"]
                ev.append(("signal", t, side, cl, sl))
        self.prev = c
        return ev


Strat = Motor      # nombre antiguo


# ============================ métricas y backtest ============================
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


def backtest(cs, P, hour_fn=None, htf_seed=None, warm=0, instrument_cost=None, plugin=None, contexto=False, orden=None,
             registro=False):
    """cs: velas (t, o, h, l, c). Las primeras `warm` solo calientan indicadores. Con contexto=True cada operación
    conserva su foto de contexto en "ctx" (para exportarla); si no, se quita para no cargar el panel."""
    st = Motor(P, hour_fn, instrument_cost, plugin=plugin)
    if registro:
        st.registro = []
    if orden and hasattr(st.S, "ordenar"):
        st.S.ordenar(orden)
    if htf_seed:
        st.seed_htf(htf_seed, cs[0][0] if cs else None)
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
                if not contexto:
                    tr.pop("ctx", None)
                tr["dur"] = round((tr["t_close"] + tf - tr["t_open"]) / 60000, 1)
                trades.append(tr)
        if k == warm - 1:
            st.reset_setups()
            st.reset_day_state()
            st.block_days, st.filtered, st.skipped = {}, {}, 0
            if st.registro is not None:
                st.registro = []
            if hasattr(st.S, "bloqueadas"):
                st.S.bloqueadas = {}
    return trades, {"signals": signals, "registro": st.registro or [], "filtered": dict(st.filtered), "skipped_cost": st.skipped,
                    "block_days": dict(st.block_days), "days": st.days_seen,
                    "bloqueadas": dict(getattr(st.S, "bloqueadas", {}) or {}),
                    "orden": list(getattr(st.S, "orden", []) or [])}
