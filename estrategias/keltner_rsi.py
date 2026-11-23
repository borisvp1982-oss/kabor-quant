# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Borislav Vladimirov Petrakiev
"""
Doble canal de Keltner + RSI (la estrategia de Gold5mKC), como archivo de estrategia.

Regla base (largo; el corto es el espejo):
  1) Una vela sale del canal EXTERIOR con el RSI en sobreventa: el lado queda "armado".
  2) La primera vela posterior que cierra dentro del canal INTERIOR da la señal
     (o dentro del EXTERIOR, si se elige la reentrada en el canal exterior).
  3) Entrada en la apertura de la vela siguiente. SL bajo la vela anterior. TP = RR x riesgo.
Opcional: confirmación RSI, divergencia, TP dinámico en el canal, salidas por RSI.

Novedades de la 4.1:
  - Detección de mercado lateral con las N velas anteriores: eficiencia del movimiento (Kaufman),
    anchura del rango en ATR y pendiente de la línea central. El rango (máximo y mínimo de esas velas)
    se congela cuando el lado se arma y puede usarse como filtro, barrido, stop u objetivo.
  - Entrada configurable: al cerrar dentro del canal interior (como hasta ahora) o del exterior.

Novedades de la 4.2 (armado alternativo por liquidez, desactivado por defecto):
  - Swing highs/lows (pivotes de N velas a cada lado) y FVG (en la vela del sistema o en una temporalidad
    mayor agrupada internamente).
  - Un lado también puede armarse SIN salir del canal exterior ("O" lógico con la regla de siempre): la vela
    llega al canal interior (con tolerancia en ATR), toca o barre un swing low (largos) / swing high (cortos)
    o entra en un FVG a favor, y el RSI está en el lado débil. La señal llega cuando una vela cierra de vuelta
    por encima del nivel tocado (largos), a favor y dentro del canal de entrada; puede ser la propia vela del
    barrido. El stop "vela anterior" o "extremo" queda bajo el mínimo del barrido.
  - Cada swing o FVG arma una sola vez. Un swing se borra si un cierre lo rompe; un FVG si un cierre lo atraviesa.

Novedades de la 4.3 (órdenes límite, opcionales):
  - "Límite en retroceso": cada señal, en vez de entrar a mercado, deja una orden límite entre el cierre y el
    stop (una fracción del riesgo). Mismo stop, mejor precio y más R si llena; se cancela si caduca o si el
    precio llega antes al objetivo.
  - "Límite en el nivel": con el armado por swing/FVG activo, la orden se coloca ANTES del barrido, en el swing
    o en el borde del FVG, cuando el nivel está junto al canal interior y a poca distancia del precio. Se
    renueva vela a vela. Stop a X ATR más allá del nivel.
  Las señales llevan dos claves nuevas: "limite" (precio) y "validez" (velas). El motor tiene que soportarlas;
  sin ellas la señal es la de siempre (a mercado en la apertura siguiente).

Con los ajustes por defecto da exactamente las mismas operaciones que la versión 4.0, 4.1 y 4.2.
"""
from collections import deque
from datetime import datetime, timezone

from nucleo import COMUN, F, EMA, RSI, Base

ID = "keltner_rsi"
NOMBRE = "Keltner + RSI"
VERSION = "4.3"
AUTOR = "Boris y Claude"
DESCRIPCION = ("Doble canal de Keltner con RSI. El precio sale del canal exterior con el RSI extremo y la señal "
               "llega cuando vuelve a cerrar dentro del canal interior. Opcional: armado alternativo al barrer un swing "
               "o tocar un FVG cerca del canal interior. Stop bajo la vela anterior (o el extremo, "
               "o ATR); objetivo en RR, en el canal interior opuesto o en la línea central.")

PARAMETROS = [
    # ---- Canales ----
    F("kc_len", "Canales de Keltner", "Longitud de la media", "int", 50, "EMA central.", min=2, max=500, env="KC_LEN"),
    COMUN("atr_len", group="Canales de Keltner", default=10, help="ATR de Wilder de los canales (lo usan también el "
          "stop, el trailing y la ampliación)."),
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
    F("entrada", "Canales de Keltner", "Señal al volver a cerrar dentro del", "select", "canal interior",
      "Canal interior: la versión de siempre. Canal exterior: entra antes, en cuanto una vela vuelve a cerrar "
      "dentro del canal exterior (más operaciones, stop más corto, más señales falsas).",
      options=["canal interior", "canal exterior"], env="ENTRY_CHANNEL"),
    # ---- Mercado lateral ----
    F("lat_on", "Mercado lateral", "Operar solo en mercado lateral", "bool", False,
      "Se mide con las velas anteriores a la salida del canal. Si no hay lateral, la señal sale rechazada.",
      env="LAT_ON"),
    F("lat_len", "Mercado lateral", "Velas anteriores", "int", 30,
      "Velas que se miran antes de la salida del canal. Su máximo y mínimo forman el rango.",
      min=5, max=500, env="LAT_LEN"),
    F("lat_er", "Mercado lateral", "Eficiencia máxima", "float", 0.30,
      "Desplazamiento neto del cierre entre recorrido total (0 = lateral puro, 1 = tendencia limpia).",
      min=0.01, max=1, step=0.01, env="LAT_ER"),
    F("lat_ancho", "Mercado lateral", "Anchura máxima del rango (ATR)", "float", 0,
      "0 = sin límite.", min=0, max=50, step=0.5, env="LAT_ANCHO"),
    F("lat_pend", "Mercado lateral", "Pendiente máxima de la línea central (ATR)", "float", 0,
      "Cuánto se ha movido la EMA central en esas velas. 0 = sin límite.", min=0, max=20, step=0.1,
      env="LAT_PEND"),
    F("lat_barrido", "Mercado lateral", "Exigir barrido del rango", "bool", False,
      "Largos: la salida del canal tiene que perforar el mínimo del rango. Cortos: el máximo.",
      env="LAT_BARRIDO"),
    # ---- Swing y FVG (armado alternativo) ----
    F("alt_on", "Swing y FVG", "Armar también por swing/FVG", "bool", False,
      "Además de la salida del canal exterior (regla de siempre), un lado se arma si el precio llega al canal "
      "interior y toca o barre un swing o un FVG a favor. Desactivado = versión 4.1.", env="ALT_ON"),
    F("alt_modo", "Swing y FVG", "Qué tiene que tocar", "select", "swing o FVG",
      "Swing y FVG: los dos en la misma vela (confluencia, menos señales).",
      options=["swing o FVG", "swing y FVG", "solo swing", "solo FVG"], show_if={"alt_on": True}, env="ALT_MODO"),
    F("alt_canal", "Swing y FVG", "Exigir llegar al canal", "select", "canal interior",
      "Canal interior: la mecha tiene que llegar a la banda interior (con la tolerancia de abajo).",
      options=["canal interior", "sin exigir"], show_if={"alt_on": True}, env="ALT_CANAL"),
    F("alt_cerca", "Swing y FVG", "Tolerancia al canal interior (ATR)", "float", 0.25,
      "0 = la mecha tiene que tocar la banda interior.", min=0, max=5, step=0.05,
      show_if={"alt_canal": "canal interior"}, env="ALT_CERCA"),
    F("alt_rsi", "Swing y FVG", "RSI máximo (largos)", "float", 45,
      "Largos: RSI por debajo de este valor en la vela del toque. Cortos: por encima de 100 menos este valor. "
      "100 = sin filtro.", min=1, max=100, show_if={"alt_on": True}, env="ALT_RSI"),
    F("alt_misma", "Swing y FVG", "Señal en la vela del barrido", "bool", True,
      "Si la propia vela que barre el nivel cierra de vuelta a favor, da la señal sin esperar.",
      show_if={"alt_on": True}, env="ALT_MISMA"),
    F("alt_velas", "Swing y FVG", "Velas de espera de la confirmación", "int", 3,
      "Velas tras el toque para que una cierre de vuelta por encima (largos) del nivel.", min=0, max=50,
      show_if={"alt_on": True}, env="ALT_VELAS"),
    F("sw_len", "Swing y FVG", "Fuerza del swing (velas a cada lado)", "int", 5, "", min=1, max=50,
      show_if={"alt_on": True}, env="SW_LEN"),
    F("sw_tol", "Swing y FVG", "Tolerancia del toque al swing (ATR)", "float", 0.15,
      "0 = tiene que barrerlo. Con tolerancia también vale el doble suelo/techo.", min=0, max=3, step=0.05,
      show_if={"alt_on": True}, env="SW_TOL"),
    F("sw_max", "Swing y FVG", "Vida máxima del swing (velas)", "int", 288, "", min=10, max=5000,
      show_if={"alt_on": True}, env="SW_MAX"),
    F("fvg_tf", "Swing y FVG", "Temporalidad del FVG (min)", "select", "60",
      "Se agrupan las velas del sistema; solo cuentan FVG de velas mayores ya cerradas.",
      options=["5", "15", "60", "240"], show_if={"alt_on": True}, env="FVG_TF"),
    F("fvg_min", "Swing y FVG", "Tamaño mínimo del FVG (ATR)", "float", 0.5, "ATR de la vela del sistema.",
      min=0, max=20, step=0.1, show_if={"alt_on": True}, env="FVG_MIN"),
    F("fvg_toque", "Swing y FVG", "Toque del FVG", "select", "borde",
      "Borde: basta con entrar en el hueco. Mitad: tiene que llegar al 50 %.", options=["borde", "mitad"],
      show_if={"alt_on": True}, env="FVG_TOQUE"),
    F("fvg_max", "Swing y FVG", "Vida máxima del FVG (velas del sistema)", "int", 576, "", min=10, max=10000,
      show_if={"alt_on": True}, env="FVG_MAX"),
    # ---- Orden de entrada ----
    F("orden", "Orden de entrada", "Tipo de orden", "select", "a mercado",
      "A mercado: apertura de la vela siguiente. Límite en retroceso: orden límite entre el cierre de la señal y "
      "el stop. Límite en el nivel: orden colocada de antemano en el swing / FVG (necesita el armado por "
      "swing/FVG). Las órdenes límite necesitan que el motor admita las claves 'limite' y 'validez'.",
      options=["a mercado", "límite en retroceso", "límite en el nivel"], env="ORDEN"),
    F("lim_ret", "Orden de entrada", "Retroceso de la orden (fracción del riesgo)", "float", 0.3,
      "0,3 = la orden queda al 30 % del camino entre el cierre y el stop.", min=0.05, max=0.9, step=0.05,
      show_if={"orden": "límite en retroceso"}, env="LIM_RET"),
    F("lim_velas", "Orden de entrada", "Validez de la orden (velas)", "int", 3, "", min=1, max=50,
      show_if={"orden": "límite en retroceso"}, env="LIM_VELAS"),
    F("lim_dist", "Orden de entrada", "Distancia máxima al nivel (ATR)", "float", 2.0,
      "Solo se coloca la orden si el nivel está a esta distancia del cierre o menos.", min=0.1, max=20, step=0.1,
      show_if={"orden": "límite en el nivel"}, env="LIM_DIST"),
    F("lim_sl", "Orden de entrada", "Stop más allá del nivel (ATR)", "float", 1.0, "", min=0.1, max=10, step=0.1,
      show_if={"orden": "límite en el nivel"}, env="LIM_SL"),
    # ---- RSI ----
    F("rsi_len", "RSI de la señal", "Periodo", "int", 14, "", min=2, max=200, env="RSI_LEN"),
    F("rsi_os", "RSI de la señal", "Sobreventa (arma largos)", "float", 25, "", min=1, max=99, env="RSI_OS"),
    F("rsi_ob", "RSI de la señal", "Sobrecompra (arma cortos)", "float", 75, "", min=1, max=99, env="RSI_OB"),
    # ---- Confirmación ----
    F("rc_on", "Confirmación RSI", "Exigir confirmación", "bool", False,
      "Además de cerrar dentro del canal interior, el RSI tiene que haber salido de la zona extrema. "
      "Si aún no ha salido, el lado sigue armado esperando."),
    F("rc_long", "Confirmación RSI", "Largos: RSI por encima de", "float", 30, "", min=1, max=99, show_if={"rc_on": True}),
    F("rc_short", "Confirmación RSI", "Cortos: RSI por debajo de", "float", 70, "", min=1, max=99,
      show_if={"rc_on": True}),
    # ---- Divergencia ----
    F("dv_on", "Divergencia RSI", "Exigir divergencia", "bool", False,
      "Largos: el mínimo de esta salida del canal es más bajo que el de la salida anterior, pero el RSI marca "
      "un mínimo más alto. Cortos, al revés."),
    F("dv_lookback", "Divergencia RSI", "Distancia máxima a la salida anterior (velas)", "int", 120, "",
      min=5, max=2000, show_if={"dv_on": True}),
    F("dv_min_rsi", "Divergencia RSI", "Diferencia mínima de RSI", "float", 2.0, "Puntos de RSI.", min=0, max=50,
      step=0.5, show_if={"dv_on": True}),
    # ---- Stop ----
    F("sl_mode", "Stop", "Colocación del stop", "select", "vela anterior",
      "Vela anterior: bajo el mínimo de la vela anterior (o de la de señal si llegó más lejos). "
      "Extremo: bajo el extremo de la salida del canal. ATR: a un múltiplo de ATR del cierre de la señal. "
      "Rango: bajo el mínimo del rango lateral o el extremo de la salida, el que quede más lejos.",
      options=["vela anterior", "extremo", "ATR", "rango"], env="SL_MODE"),
    F("sl_buf", "Stop", "Margen extra (ATR)", "float", 0.0, "", min=0, max=10, step=0.05, env="SL_BUFFER_ATR",
      show_if={"sl_mode": ["vela anterior", "extremo", "rango"]}),
    F("sl_atr", "Stop", "Distancia (ATR)", "float", 1.5, "", min=0.1, max=20, step=0.1, show_if={"sl_mode": "ATR"}),
    # ---- Objetivo ----
    F("tp_mode", "Objetivo y salidas", "Objetivo", "select", "RR",
      "RR: fijo a un múltiplo del riesgo. Canal interior / línea central: dinámico, se mueve con el canal "
      "(se usan los niveles de la vela anterior, como una orden límite). Extremo / mitad del rango: fijo en el "
      "rango lateral congelado al armarse el lado.",
      options=["RR", "canal interior", "línea central", "extremo del rango", "mitad del rango"], env="TP_MODE"),
    F("rr", "Objetivo y salidas", "RR", "float", 2.5, "", min=0.1, max=20, step=0.1, env="RR",
      show_if={"tp_mode": "RR"}),
    F("tp_rsi", "Objetivo y salidas", "Cerrar en el RSI extremo opuesto", "bool", False,
      "Cierra al cierre de una vela con el RSI en sobrecompra (largos) o sobreventa (cortos).", env="TP_RSI"),
    F("ex_on", "Objetivo y salidas", "Salida anticipada por RSI", "bool", False,
      "Cierra al cierre de la vela en la que el RSI cruza el nivel en contra (largos: hacia abajo)."),
    F("ex_long", "Objetivo y salidas", "Nivel para largos", "float", 50, "", min=1, max=99, show_if={"ex_on": True}),
    F("ex_short", "Objetivo y salidas", "Nivel para cortos", "float", 50, "", min=1, max=99, show_if={"ex_on": True}),
]

# El disparador del break-even puede ser la línea central del canal.
AJUSTES_COMUNES = {"be_trigger": {"options": ["línea central", "R"], "default": "línea central",
                                  "help": "Línea central: cuando el precio llega a la EMA del canal."}}
VALORES_ANTIGUOS = {"sl_mode": {"prev": "vela anterior", "excursion": "extremo"},
                    "outside": {"wick": "mecha", "close": "cierre"},
                    "tp_mode": {"rr": "RR", "canal": "canal interior"},
                    "be_trigger": {"basis": "línea central", "r": "R"}}
FILTROS = {"divergence": "sin divergencia", "lateral": "mercado sin lateral", "barrido": "sin barrido del rango",
           "objetivo": "objetivo del rango ya superado"}
SALIDAS = {"RSI": "RSI extremo", "RSI EN CONTRA": "RSI en contra"}
# Líneas del gráfico. color: gain, loss, gold, muted, blue, purple. estilo: continua, discontinua, puntos.
LINEAS = [
    {"key": "up_o", "nombre": "Exterior superior", "color": "loss", "estilo": "discontinua"},
    {"key": "lo_o", "nombre": "Exterior inferior", "color": "loss", "estilo": "discontinua"},
    {"key": "up_i", "nombre": "Interior superior", "color": "gain", "estilo": "continua"},
    {"key": "lo_i", "nombre": "Interior inferior", "color": "gain", "estilo": "continua"},
    {"key": "basis", "nombre": "Línea central", "color": "muted", "estilo": "puntos"},
    {"key": "rng_hi", "nombre": "Máximo del rango lateral", "color": "blue", "estilo": "continua"},
    {"key": "rng_lo", "nombre": "Mínimo del rango lateral", "color": "purple", "estilo": "continua"},
    {"key": "sw_lo", "nombre": "Swing low más cercano", "color": "gain", "estilo": "puntos"},
    {"key": "sw_hi", "nombre": "Swing high más cercano", "color": "loss", "estilo": "puntos"},
    {"key": "fvg_alc", "nombre": "FVG alcista (borde superior)", "color": "gold", "estilo": "discontinua"},
    {"key": "fvg_baj", "nombre": "FVG bajista (borde inferior)", "color": "gold", "estilo": "discontinua"},
]
CLAVES_LINEAS = [x["key"] for x in LINEAS]
LADO = {1: "Largo", -1: "Corto"}


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


def validar(P):
    errs = []
    if P["m_in"] >= P["m_out"]:
        errs.append("El canal interior tiene que ser más estrecho que el exterior")
    if P["rsi_os"] >= P["rsi_ob"]:
        errs.append("La sobreventa tiene que ser menor que la sobrecompra")
    if P["orden"] == "límite en el nivel" and not P["alt_on"]:
        errs.append("La orden límite en el nivel necesita activar el armado por swing/FVG")
    return errs


def calentamiento(P):
    n = 4 * max(P["kc_len"], P["atr_len"], P["rsi_len"], P["lat_len"])
    if P.get("alt_on"):
        n = max(n, P["sw_max"] + 2 * P["sw_len"], P["fvg_max"])
    return n


class Estrategia(Base):
    def __init__(self, P, motor):
        super().__init__(P, motor)
        self.basis_ind, self.rsi_ind = EMA(P["kc_len"]), RSI(P["rsi_len"])
        self.basis = self.rsi = None
        self.prev_rsi = None
        self.lv = None                       # niveles de la vela anterior (objetivo dinámico y BE)
        self.armed = {1: None, -1: None}
        self.exc = {1: None, -1: None}       # salida del canal en curso (para la divergencia)
        self.exc_hist = {1: deque(maxlen=50), -1: deque(maxlen=50)}
        self.last = {}
        self.c = None
        self.hist = deque(maxlen=P["lat_len"] + 1)   # (máximo, mínimo, cierre, línea central) de velas anteriores
        self.lat = None                      # foto del mercado lateral con las velas anteriores
        self.tp_fijo = None                  # objetivo fijo en el rango de la última señal
        # Armado alternativo (swing / FVG)
        self.n = 0                           # contador propio de velas
        self.sw = {1: [], -1: []}            # 1: swing lows (para largos), -1: swing highs (para cortos)
        self.win = deque(maxlen=2 * P["sw_len"] + 1)
        self.fvg = {1: [], -1: []}           # 1: FVG alcistas, -1: bajistas
        self.barras = deque(maxlen=3)        # últimas velas cerradas de la temporalidad del FVG
        self.vmayor = None                   # vela de la temporalidad del FVG en curso
        self.nota = ""
        self.orden_txt = ""

    # --- estado ---
    def guardar(self):
        return {"armed": {"1": self.armed[1], "-1": self.armed[-1]}, "tp_fijo": self.tp_fijo}

    def cargar(self, s):
        a = s.get("armed") or {}
        self.armed = {1: a.get("1"), -1: a.get("-1")}
        self.tp_fijo = s.get("tp_fijo")

    def reiniciar(self):
        self.armed = {1: None, -1: None}
        self.tp_fijo = None

    # --- vela a vela ---
    def actualizar(self, c):
        self.c = c
        self.n += 1
        self.basis = self.basis_ind.update(c[4])
        self.rsi = self.rsi_ind.update(c[4])
        if self.P["alt_on"] and self.P["alt_modo"] != "solo swing":
            self._agregar(c)

    # --- swing y FVG ---
    def _agregar(self, c):
        """Agrupa las velas del sistema en la temporalidad del FVG y busca FVG al cerrar cada vela mayor."""
        b = int(a_utc(c[0]).timestamp()) // (int(self.P["fvg_tf"]) * 60)
        _, o, h, l, cl = c[:5]
        v = self.vmayor
        if v is None or b != v[0]:
            if v is not None:
                self.barras.append(v)
                self._buscar_fvg()
            self.vmayor = [b, o, h, l, cl]
        else:
            v[2], v[3], v[4] = max(v[2], h), min(v[3], l), cl

    def _buscar_fvg(self):
        if len(self.barras) < 3 or not self.m.atr:
            return
        v0, v1, v2 = self.barras
        if v2[0] - v1[0] != 1 or v1[0] - v0[0] != 1:      # velas seguidas (sin cierre de mercado en medio)
            return
        mn = self.P["fvg_min"] * self.m.atr
        gap = v2[3] - v0[2]                               # alcista: mínimo de la 3ª sobre el máximo de la 1ª
        if gap > 0 and gap >= mn:
            self.fvg[1].append({"top": v2[3], "bot": v0[2], "i": self.n})
        gap = v0[3] - v2[2]                               # bajista: máximo de la 3ª bajo el mínimo de la 1ª
        if gap > 0 and gap >= mn:
            self.fvg[-1].append({"top": v0[3], "bot": v2[2], "i": self.n})
        for s in (1, -1):
            del self.fvg[s][:-30]

    def _estructura(self, h, l, cl, atr):
        """Al cierre de cada vela: borra swings/FVG rotos o caducados y añade los swings recién confirmados."""
        P, n = self.P, self.n
        tol = P["sw_tol"] * atr if atr else 0.0
        if P["orden"] == "límite en el nivel":
            # Un nivel tocado ya ha llenado (o habría llenado) su orden: no se vuelve a usar
            ext = {1: l, -1: h}
            for s in (1, -1):
                self.sw[s] = [x for x in self.sw[s] if (ext[s] - x["p"]) * s > tol]
                self.fvg[s] = [f for f in self.fvg[s] if (ext[s] - self._borde(f, s)) * s > 0]
        for s in (1, -1):
            self.sw[s] = [x for x in self.sw[s] if n - x["i"] <= P["sw_max"] and (cl - x["p"]) * s >= -tol]
            self.fvg[s] = [f for f in self.fvg[s] if n - f["i"] <= P["fvg_max"] and
                           (cl - (f["bot"] if s == 1 else f["top"])) * s >= 0]
        self.win.append((h, l, n))
        k = P["sw_len"]
        if len(self.win) == self.win.maxlen:
            w = list(self.win)
            ch, cl_, ci = w[k]
            izq, der = w[:k], w[k + 1:]
            if cl_ < min(x[1] for x in izq) and cl_ <= min(x[1] for x in der):
                self.sw[1].append({"p": cl_, "i": ci})
            if ch > max(x[0] for x in izq) and ch >= max(x[0] for x in der):
                self.sw[-1].append({"p": ch, "i": ci})
            for s in (1, -1):
                del self.sw[s][:-30]

    def _borde(self, f, side):
        if self.P["fvg_toque"] == "mitad":
            return (f["top"] + f["bot"]) / 2
        return f["top"] if side == 1 else f["bot"]

    def _niveles(self, side, atr):
        """Niveles candidatos para la orden límite: (precio de la orden, nivel, swings, FVG)."""
        P, modo = self.P, self.P["alt_modo"]
        tol = P["sw_tol"] * atr
        borde = lambda f: self._borde(f, side)
        out = []
        if modo in ("swing o FVG", "solo swing"):
            out += [(x["p"] + side * tol, x["p"], [x], []) for x in self.sw[side]]
        if modo in ("swing o FVG", "solo FVG"):
            out += [(borde(f), borde(f), [], [f]) for f in self.fvg[side]]
        if modo == "swing y FVG":
            for x in self.sw[side]:
                for f in self.fvg[side]:
                    if f["bot"] - tol <= x["p"] <= f["top"] + tol:
                        out.append((x["p"] + side * tol, x["p"], [x], [f]))
        return out

    def _orden_nivel(self, cl, rsi, atr):
        """Orden límite colocada de antemano en el nivel más cercano junto al canal interior."""
        P, lv = self.P, self.lv
        if atr is None or rsi is None:
            return None
        mejor = None
        for side in (1, -1):
            if not (rsi < P["alt_rsi"] if side == 1 else rsi > 100 - P["alt_rsi"]):
                continue
            banda = lv["lo_i"] if side == 1 else lv["up_i"]
            for lim, niv, sw, fv in self._niveles(side, atr):
                dist = (cl - lim) * side
                if dist <= 0 or dist > P["lim_dist"] * atr:
                    continue
                if P["alt_canal"] == "canal interior" and (niv - banda) * side > P["alt_cerca"] * atr:
                    continue
                if mejor is None or dist < mejor[0]:
                    mejor = (dist, side, lim, niv)
        if mejor is None:
            return None
        _, side, lim, niv = mejor
        self.orden_txt = "Orden límite {} en {:.2f}.".format("de compra" if side == 1 else "de venta", lim)
        return {"side": side, "sl": niv - side * P["lim_sl"] * atr, "limite": lim, "validez": 1}

    def _toque_alt(self, side, l, h, lo_i, up_i, rsi, atr):
        """¿Toca esta vela un swing o un FVG a favor, cerca del canal interior y con RSI en el lado débil?"""
        P = self.P
        if P["alt_canal"] == "canal interior":
            if side == 1 and l > lo_i + P["alt_cerca"] * atr:
                return None
            if side == -1 and h < up_i - P["alt_cerca"] * atr:
                return None
        # RSI en el toque: el más débil entre esta vela y la anterior (la vela del barrido suele cerrar fuerte)
        r = rsi if self.prev_rsi is None else (min(rsi, self.prev_rsi) if side == 1 else max(rsi, self.prev_rsi))
        if not (r < P["alt_rsi"] if side == 1 else r > 100 - P["alt_rsi"]):
            return None
        ext, modo = (l if side == 1 else h), P["alt_modo"]
        sw = []
        if modo != "solo FVG":
            tol = P["sw_tol"] * atr
            sw = [x for x in self.sw[side] if (ext - x["p"]) * side <= tol]
        fv = []
        if modo != "solo swing":
            for f in self.fvg[side]:
                borde = f["top"] if side == 1 else f["bot"]
                if P["fvg_toque"] == "mitad":
                    borde = (f["top"] + f["bot"]) / 2
                if (ext - borde) * side <= 0:
                    fv.append(f)
        if not ((sw and fv) if modo == "swing y FVG" else (sw or fv)):
            return None
        niveles = [x["p"] for x in sw] + [(f["top"] if side == 1 else f["bot"]) for f in fv]
        tipo = "swing+FVG" if sw and fv else ("swing" if sw else "FVG")
        # Para confirmar, el cierre tiene que recuperar todos los niveles tocados
        return {"tipo": tipo, "nivel": max(niveles) if side == 1 else min(niveles), "sw": sw, "fv": fv}

    def _arm_alt(self, side, t, l, h, rsi):
        for x in t["sw"]:
            self.sw[side].remove(x)          # cada swing / FVG arma una sola vez
        for f in t["fv"]:
            self.fvg[side].remove(f)
        return {"ext": l if side == 1 else h, "age": 0, "rx": rsi, "start": self.m.i, "rng": self.lat,
                "tipo": t["tipo"], "nivel": t["nivel"]}

    @staticmethod
    def _es_alt(a):
        return bool(a) and a.get("tipo", "canal") != "canal"

    def _back(self, side, a, back, cl, o):
        """Condición de señal. Armado por canal: la de siempre. Armado alternativo: además cierra a favor y
        recupera el nivel tocado."""
        if not self._es_alt(a):
            return back
        return back and (cl - a["nivel"]) * side > 0 and (cl - o) * side > 0

    def rr(self):
        return self.P["rr"] if self.P["tp_mode"] == "RR" else None

    def objetivo(self, pos):
        if self.P["tp_mode"] in ("extremo del rango", "mitad del rango"):
            return self.tp_fijo
        lv = self.lv
        if not lv:
            return None
        if self.P["tp_mode"] == "canal interior":
            return lv["up_i"] if pos["side"] == 1 else lv["lo_i"]
        return lv["basis"]

    def nivel_be(self, pos):
        return self.lv["basis"] if self.lv else None

    def salida(self, pos):
        P, rsi, s = self.P, self.rsi, pos["side"]
        if rsi is None:
            return None
        if P["tp_rsi"] and (rsi >= P["rsi_ob"] if s == 1 else rsi <= P["rsi_os"]):
            return "RSI"
        if P["ex_on"] and self.prev_rsi is not None and (
                (self.prev_rsi >= P["ex_long"] > rsi) if s == 1 else (self.prev_rsi <= P["ex_short"] < rsi)):
            return "RSI EN CONTRA"
        return None

    def _lateral(self, atr):
        """Foto del mercado con las velas anteriores (sin la actual)."""
        P, hs = self.P, self.hist
        if atr is None or atr <= 0 or len(hs) < hs.maxlen:
            return None
        ult = list(hs)[1:]                   # las N velas anteriores
        hi, lo = max(x[0] for x in ult), min(x[1] for x in ult)
        cls = [x[2] for x in hs]             # N+1 cierres para N movimientos
        neto = abs(cls[-1] - cls[0])
        total = sum(abs(b - a) for a, b in zip(cls, cls[1:]))
        er = neto / total if total > 0 else 0.0
        ancho = (hi - lo) / atr
        pend = abs(hs[-1][3] - hs[0][3]) / atr if None not in (hs[-1][3], hs[0][3]) else 0.0
        ok = er <= P["lat_er"]
        if P["lat_ancho"] > 0:
            ok = ok and ancho <= P["lat_ancho"]
        if P["lat_pend"] > 0:
            ok = ok and pend <= P["lat_pend"]
        return {"hi": hi, "lo": lo, "er": round(er, 3), "ancho": round(ancho, 2), "pend": round(pend, 2),
                "lateral": ok}

    def _arm(self, side, a, out, rsi_ok, l, h, rsi):
        ext = l if side == 1 else h
        alt = self._es_alt(a)
        if out:
            if a:
                a["ext"] = min(a["ext"], ext) if side == 1 else max(a["ext"], ext)
                a["rx"] = min(a.get("rx", rsi), rsi) if side == 1 else max(a.get("rx", rsi), rsi)
                a["age"] = 0
                if alt and rsi_ok:           # el armado alternativo pasa a ser un armado normal del canal
                    a["tipo"] = "canal"
                    a.pop("nivel", None)
            elif rsi_ok:
                a = {"ext": ext, "age": 0, "rx": rsi, "start": self.m.i, "rng": self.lat}
        elif a:
            if alt:
                a["ext"] = min(a["ext"], ext) if side == 1 else max(a["ext"], ext)
            a["age"] += 1
            if a["age"] > (self.P["alt_velas"] if alt else self.P["max_armed"]):
                a = None
        return a

    def _stop(self, side, a, l, h, cl, atr):
        P, prev = self.P, self.m.prev
        if P["sl_mode"] == "ATR":
            return cl - side * P["sl_atr"] * atr
        if P["sl_mode"] == "extremo":
            base = a["ext"]
        elif P["sl_mode"] == "rango":
            r = a.get("rng")
            base = a["ext"] if not r else (min(r["lo"], a["ext"]) if side == 1 else max(r["hi"], a["ext"]))
        else:
            base = min(prev[3], l) if side == 1 else max(prev[2], h)
        return base - side * P["sl_buf"] * atr

    def _divergence_ok(self, side, a):
        P, i = self.P, self.m.i
        prev = None
        for e in reversed(self.exc_hist[side]):
            if e[0] < a.get("start", i):
                prev = e
                break
        if prev is None or a.get("start", i) - prev[0] > P["dv_lookback"]:
            return False
        _, p_ext, p_rx = prev
        rx = a.get("rx", 50.0)
        if side == 1:
            return a["ext"] < p_ext and rx >= p_rx + P["dv_min_rsi"]
        return a["ext"] > p_ext and rx <= p_rx - P["dv_min_rsi"]

    def senales(self):
        P, m = self.P, self.m
        t, o, h, l, cl = self.c
        basis, atr, rsi = self.basis, m.atr, self.rsi
        out_sig = []
        self.lat = self._lateral(atr)
        if None not in (basis, atr, rsi) and m.prev is not None:
            up_o, lo_o = basis + P["m_out"] * atr, basis - P["m_out"] * atr
            up_i, lo_i = basis + P["m_in"] * atr, basis - P["m_in"] * atr
            px_lo = l if P["outside"] == "mecha" else cl
            px_hi = h if P["outside"] == "mecha" else cl
            self.last = {"close": cl, "rsi": rsi, "lo_o": lo_o, "up_o": up_o, "lo_i": lo_i, "up_i": up_i,
                         "basis": basis, "atr": atr}
            alt_mkt = P["alt_on"] and P["orden"] != "límite en el nivel"
            for side in (1, -1):
                a = self.armed[side]
                out = px_lo < lo_o if side == 1 else px_hi > up_o
                rsi_ok = rsi < P["rsi_os"] if side == 1 else rsi > P["rsi_ob"]
                if P["entrada"] == "canal exterior":
                    back = cl > lo_o if side == 1 else cl < up_o
                else:
                    back = cl > lo_i if side == 1 else cl < up_i
                if P["rc_on"]:
                    back = back and (rsi > P["rc_long"] if side == 1 else rsi < P["rc_short"])
                fired = False
                if P["same_candle"]:
                    a = self._arm(side, a, out, rsi_ok, l, h, rsi)
                    if a is None and alt_mkt:
                        t_alt = self._toque_alt(side, l, h, lo_i, up_i, rsi, atr)
                        if t_alt:
                            a = self._arm_alt(side, t_alt, l, h, rsi)
                    fired = bool(a and self._back(side, a, back, cl, o))
                elif a and self._back(side, a, back, cl, o):
                    fired = True
                else:
                    a = self._arm(side, a, out, rsi_ok, l, h, rsi)
                    if a is None and alt_mkt:
                        t_alt = self._toque_alt(side, l, h, lo_i, up_i, rsi, atr)
                        if t_alt:
                            a = self._arm_alt(side, t_alt, l, h, rsi)
                            fired = P["alt_misma"] and self._back(side, a, back, cl, o)
                if fired:
                    sig = {"side": side, "sl": self._stop(side, a, l, h, cl, atr)}
                    r = a.get("rng")
                    if self._es_alt(a):
                        self.nota = "{} por {} en {:.2f}.".format(LADO[side], a["tipo"], a["nivel"])
                    # La divergencia compara salidas del canal exterior: no se aplica al armado alternativo
                    if P["dv_on"] and not self._es_alt(a) and not self._divergence_ok(side, a):
                        sig["rechazo"] = "divergence"
                    elif P["lat_on"] and not (r and r["lateral"]):
                        sig["rechazo"] = "lateral"
                    elif P["lat_barrido"] and not (r and (a["ext"] < r["lo"] if side == 1 else a["ext"] > r["hi"])):
                        sig["rechazo"] = "barrido"
                    elif P["tp_mode"] in ("extremo del rango", "mitad del rango"):
                        tp = None
                        if r:
                            tp = (r["hi"] if side == 1 else r["lo"]) if P["tp_mode"] == "extremo del rango" \
                                else (r["hi"] + r["lo"]) / 2
                        if tp is None:
                            sig["rechazo"] = "lateral"
                        elif (tp - cl) * side <= 0:
                            sig["rechazo"] = "objetivo"
                        else:
                            self.tp_fijo = tp
                    if P["orden"] == "límite en retroceso":
                        riesgo = (cl - sig["sl"]) * side
                        if riesgo > 0:
                            sig["limite"] = cl - side * P["lim_ret"] * riesgo
                            sig["validez"] = P["lim_velas"]
                    out_sig.append(sig)
                    a = None
                self.armed[side] = a
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
                    self.exc_hist[side].append((m.i - 1, cur["ext"], cur["rx"]))
                    self.exc[side] = None
            self.lv = {"up_i": up_i, "lo_i": lo_i, "basis": basis}
            if self.lat and self.lat["lateral"]:
                self.last["rng_hi"], self.last["rng_lo"] = self.lat["hi"], self.lat["lo"]
        if P["alt_on"]:
            self._estructura(h, l, cl, atr)
            if self.last:
                self._lineas_alt(cl)
            self.orden_txt = ""
            if P["orden"] == "límite en el nivel" and not out_sig and self.lv:
                o_niv = self._orden_nivel(cl, rsi, atr)
                if o_niv:
                    out_sig.append(o_niv)
        self.hist.append((h, l, cl, basis))
        self.prev_rsi = rsi
        return out_sig

    def _lineas_alt(self, cl):
        bajo = [x["p"] for x in self.sw[1] if x["p"] < cl]
        sobre = [x["p"] for x in self.sw[-1] if x["p"] > cl]
        f_alc = [f["top"] for f in self.fvg[1] if f["top"] < cl]
        f_baj = [f["bot"] for f in self.fvg[-1] if f["bot"] > cl]
        for k, v in (("sw_lo", max(bajo) if bajo else None), ("sw_hi", min(sobre) if sobre else None),
                     ("fvg_alc", max(f_alc) if f_alc else None), ("fvg_baj", min(f_baj) if f_baj else None)):
            if v is not None:
                self.last[k] = v

    # --- panel ---
    def lineas(self):
        lv = self.last
        return {k: lv[k] for k in CLAVES_LINEAS if k in lv}

    def indicadores(self):
        partes = []
        if self.rsi is not None:
            partes.append("RSI {:.0f}".format(self.rsi))
        if self.lat:
            partes.append("{} (ef. {:.2f}, rango {:.1f} ATR)".format(
                "Lateral" if self.lat["lateral"] else "Con dirección", self.lat["er"], self.lat["ancho"]))
        if self.P["alt_on"]:
            partes.append("Swings {}/{} | FVG {}/{} (alc./baj.)".format(
                len(self.sw[1]), len(self.sw[-1]), len(self.fvg[1]), len(self.fvg[-1])))
        return " | ".join(partes)

    def texto(self):
        P, parts = self.P, []
        for side, name, cond in ((1, "Largo", " con RSI > {:g}".format(P["rc_long"])),
                                 (-1, "Corto", " con RSI < {:g}".format(P["rc_short"]))):
            a = self.armed[side]
            if self._es_alt(a):
                parts.append("{} armado por {} en {:.2f} (hace {} velas); espera cierre {} del nivel y dentro "
                             "del {}.".format(name, a["tipo"], a["nivel"], a["age"],
                                              "por encima" if side == 1 else "por debajo", P["entrada"]))
            elif a:
                parts.append("{} armado (hace {} velas); espera cierre dentro del {}{}.".format(
                    name, a["age"], P["entrada"], cond if P["rc_on"] else ""))
        if self.orden_txt:
            parts.append(self.orden_txt)
        if parts:
            return " ".join(parts)
        base = "Ningún lado armado: el precio no ha salido del canal exterior con RSI extremo"
        if P["alt_on"]:
            base += " ni ha barrido un swing o tocado un FVG junto al canal interior"
        return base + "." + (" Última: " + self.nota if self.nota else "")
