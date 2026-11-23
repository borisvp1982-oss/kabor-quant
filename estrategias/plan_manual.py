# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Borislav Vladimirov Petrakiev
"""
Plan manual: el bot no tiene estrategia propia, ejecuta los planes que tú confirmas en el panel → Planes.

Cada plan (ver planes.py) dice: símbolo, temporalidad, lado, condición (cierre por encima/debajo de un nivel, o
toque de un nivel), stop, objetivo, invalidación opcional y hora límite. Este bot vigila los planes ACTIVOS de su
mismo símbolo y temporalidad:

  * al cierre de cada vela comprueba primero la caducidad y la invalidación, luego la condición,
  * si se cumple, da la señal con el stop y el objetivo del plan; el sistema entra a mercado en la apertura de la
    vela siguiente, con el tamaño por riesgo y todos los filtros y límites comunes del bot,
  * el resultado (entrada abierta o descartada por un filtro) queda en el historial del plan.

Condiciones de toque: se evalúan al cierre de la vela que toca el nivel.

Órdenes límite (condición «limite», desde la 1.1): al primer cierre tras confirmar el plan se coloca una orden límite
en el nivel. Se llena en cuanto una vela toca el precio (o en la apertura, si abre ya al otro lado), sin esperar al
cierre. Se retira si el precio llega antes al objetivo, si se cumple la invalidación, si caduca o si cancelas el plan.
Si la misma vela toca el límite y el objetivo, no se cuenta (no se sabe qué fue primero); si toca el límite y el
stop, se cuenta como pérdida. En un bot con cuenta sandbox la orden es «vigilada» (desde la 1.3): no se coloca en
MyFundedPerps; KaBor vigila el precio en vivo y, al tocar el nivel, envía la entrada a mercado con su stop y su
objetivo. El modelo registra la entrada a ese mismo precio.

Seguridad:
  * solo actúa en directo: en el backtest, Monte Carlo o walk-forward no hace nada,
  * solo cuenta velas que cierran DESPUÉS de confirmar el plan y recientes (al recargar el historial no dispara
    planes viejos),
  * un plan se dispara una sola vez; mientras haya una operación abierta, los demás esperan.

Usa un solo bot «Plan manual» por símbolo y temporalidad. Si un bot con estrategia opera el mismo símbolo en la
misma cuenta, sus posiciones pueden compensarse: mejor en símbolos distintos o en otra cuenta.
"""
import time

from nucleo import Base
import planes as PL

ID = "plan_manual"
NOMBRE = "Plan manual"
VERSION = "1.6"
AUTOR = "Boris y Claude"
DESCRIPCION = ("Sin estrategia propia: ejecuta los planes de tu análisis (panel → Planes) del mismo símbolo y "
               "temporalidad, con su stop, objetivo, invalidación y caducidad. Solo actúa en directo.")

PARAMETROS = []
AJUSTES_COMUNES = {
    "max_cost": {"default": 0.3, "help": "Descarta la entrada si la comisión supera esta fracción del riesgo. Con "
                                         "stops muy ajustados sube este valor o el plan se descartará."},
}
LINEAS = [
    {"key": "nivel", "nombre": "Nivel del plan", "color": "gold", "estilo": "discontinua"},
    {"key": "sl", "nombre": "Stop del plan", "color": "loss", "estilo": "puntos"},
    {"key": "tp", "nombre": "Objetivo del plan", "color": "gain", "estilo": "puntos"},
    {"key": "tp1", "nombre": "TP1 del plan", "color": "gain", "estilo": "discontinua"},
    {"key": "inv", "nombre": "Invalidación", "color": "muted", "estilo": "puntos"},
]


def calentamiento(P):
    return 30


class Estrategia(Base):
    def __init__(self, P, motor):
        super().__init__(P, motor)
        self.c = None
        self.esperando = None       # plan disparado en la vela anterior: ¿se abrió la entrada?
        self.racha = {}             # cierres seguidos que cumplen la condición, por plan
        self.vigilados = []
        self.limite = None          # orden límite colocada: {"id", "t", "intentos"}
        self.plan_pos = None        # plan de la operación en curso (para su TP1 con venta parcial y break-even)

    def actualizar(self, vela):
        self.c = vela

    def _activos(self):
        st = PL.STORE
        if st is None or not getattr(self.m, "en_vivo", False):
            return None
        return st

    def senales(self):
        st, c = self._activos(), self.c
        if st is None or c is None:
            return []
        tf = self.m.tf_ms
        cierre = c[0] + tf
        if self.esperando:
            pid, self.esperando = self.esperando, None
            ua = getattr(self.m, "ult_apertura", None)
            if ua and ua[0] == c[0]:
                cerrada = not (self.m.pos and self.m.pos.get("t_open") == c[0])
                st.marcar(pid, None, "Entrada abierta a {}{}".format(
                    PL._fmt(ua[1]), " (y cerrada en la misma vela)" if cerrada else ""),
                    {"entrada": ua[1], "t_entrada": c[0]})
            else:
                st.marcar(pid, None, "La entrada no se abrió (filtro del bot, límite diario, hueco de precio o coste)")
        if cierre < time.time() * 1000 - 3 * tf:
            self.vigilados = st.para(self.P["symbol"], self.P["timeframe"])
            return []                               # vela antigua: recarga del historial
        out = self._seguir_limite(st, c, cierre)
        self.vigilados = st.para(self.P["symbol"], self.P["timeframe"])   # después: puede haber cambiado de estado
        if self.limite or out:
            return out                              # con una orden límite puesta, los demás planes esperan
        for p in self.vigilados:
            pid = p["id"]
            if not p.get("confirmado_ms") or cierre <= p["confirmado_ms"]:
                continue
            if cierre >= p["expira_ms"]:
                st.marcar(pid, "caducado", "Llegó la hora límite sin cumplirse")
                continue
            inv = p.get("invalidacion")
            if inv and PL.cumple(inv["tipo"], inv["nivel"], c):
                st.marcar(pid, "invalidado", "Se cumplió la invalidación ({} {})".format(
                    PL.CONDICIONES[inv["tipo"]], PL._fmt(inv["nivel"])))
                continue
            cond = p["condicion"]
            if cond["tipo"] == "limite":
                if out or self.m.pos or self.m.pending:
                    continue
                out.append({"side": 1 if p["lado"] == "largo" else -1, "sl": p["sl"], "tp": p["tp"],
                            "limite": cond["nivel"], "tamano": p.get("tamano")})
                ya = p.get("limite_t")
                self.limite = {"id": pid, "t": ya or cierre, "intentos": 1}
                self.plan_pos = pid
                if not ya:            # al recargar el historial no se anuncia otra vez
                    st.marcar(pid, None, "Orden límite colocada en {}".format(PL._fmt(cond["nivel"])),
                              {"limite_t": cierre})
                continue
            k = self.racha.get(pid, 0) + 1 if PL.cumple(cond["tipo"], cond["nivel"], c) else 0
            self.racha[pid] = k
            if k < cond.get("velas", 1) or out or self.m.pos or self.m.pending:
                continue
            s = 1 if p["lado"] == "largo" else -1
            if (c[4] - p["sl"]) * s <= 0:
                st.marcar(pid, "invalidado", "El cierre ya estaba al otro lado del stop")
                continue
            if (p["tp"] - c[4]) * s <= 0:
                st.marcar(pid, "invalidado", "El cierre ya había pasado el objetivo")
                continue
            out.append({"side": s, "sl": p["sl"], "tp": p["tp"], "tamano": p.get("tamano")})
            self.plan_pos = pid
            st.marcar(pid, "disparado", "Condición cumplida: cierre {} a las {} UTC".format(
                PL._fmt(c[4]), time.strftime("%H:%M", time.gmtime(cierre / 1000))), {"t_disparo": cierre})
            self.esperando = pid
            self.racha.pop(pid, None)
        return out

    def _seguir_limite(self, st, c, cierre):
        """Estado de la orden límite colocada: llena, retirada por el motor, o anulada por el plan."""
        L = self.limite
        if not L:
            return []
        m, pid = self.m, L["id"]
        p = st.get(pid)
        pend = m.pending if (m.pending or {}).get("limite") is not None else None
        ua = getattr(m, "ult_apertura", None)
        if ua and ua[0] >= L["t"] and not pend:
            self.limite = None
            cerrada = not (m.pos and m.pos.get("t_open") == ua[0])
            st.marcar(pid, "disparado", "Orden límite llena: entrada a {}{}".format(
                PL._fmt(ua[1]), " (y cerrada en la misma vela)" if cerrada else ""),
                {"entrada": ua[1], "t_entrada": ua[0], "t_disparo": ua[0]})
            return []
        fin = getattr(m, "limite_fin", None)
        if not pend:
            self.limite = None
            if fin and fin[0] >= L["t"]:
                st.marcar(pid, "invalidado", "Orden límite retirada: el precio llegó al objetivo sin llenarla")
                return []
            if p and p["estado"] == "activo" and L["intentos"] < 3 and not m.pos:
                # la orden no llegó a quedar puesta (filtro del bot, límite diario, reinicio…): se vuelve a colocar
                self.limite = dict(L, intentos=L["intentos"] + 1)
                return [{"side": 1 if p["lado"] == "largo" else -1, "sl": p["sl"], "tp": p["tp"],
                         "limite": p["condicion"]["nivel"], "tamano": p.get("tamano")}]
            if p and p["estado"] == "activo":
                st.marcar(pid, "invalidado", "La orden límite no se pudo colocar (filtro del bot, límite diario o "
                                             "coste)")
            return []
        # la orden sigue puesta: ¿hay que retirarla?
        if pend.get("forzar") is not None:
            return []           # ya se llenó en la cuenta (orden vigilada): el motor la registra al cierre de la vela
        motivo, estado = None, None
        if not p or p["estado"] != "activo":
            motivo = "plan cancelado"
        elif cierre >= p["expira_ms"]:
            motivo, estado = "Llegó la hora límite sin llenarse", "caducado"
        elif p.get("invalidacion") and PL.cumple(p["invalidacion"]["tipo"], p["invalidacion"]["nivel"], c):
            inv = p["invalidacion"]
            motivo, estado = "Se cumplió la invalidación ({} {}) sin llenarse".format(
                PL.CONDICIONES[inv["tipo"]], PL._fmt(inv["nivel"])), "invalidado"
        if motivo:
            m.pending = None
            self.limite = None
            if estado:
                st.marcar(pid, estado, "Orden límite retirada: " + motivo[0].lower() + motivo[1:])
        return []

    def gestion_be(self, pos):
        """TP1 del plan: vende su porcentaje y, si lo pide, sube el stop a break-even (también en la cuenta)."""
        if not self.plan_pos or PL.STORE is None:
            return None
        p = PL.STORE.get(self.plan_pos)
        if not p or not p.get("tp1"):
            return None
        return {"nivel": p["tp1"], "pct": p.get("tp1_pct") or 0, "off": 0.0 if p.get("tp1_be", True) else None}

    def _refrescar(self):
        """El panel pregunta entre velas: lee los planes activos al momento (no espera al próximo cierre)."""
        if PL.STORE is not None and getattr(self.m, "en_vivo", False):
            self.vigilados = PL.STORE.para(self.P["symbol"], self.P["timeframe"])

    def lineas(self):
        self._refrescar()
        if not self.vigilados:
            return {}
        p = self.vigilados[0]
        d = {"nivel": p["condicion"]["nivel"], "sl": p["sl"], "tp": p["tp"]}
        if p.get("tp1"):
            d["tp1"] = p["tp1"]
        if p.get("invalidacion"):
            d["inv"] = p["invalidacion"]["nivel"]
        return d

    def texto(self):
        if PL.STORE is None:
            return "Solo actúa en directo."
        self._refrescar()
        n = len(self.vigilados)
        if not n:
            return "Sin planes activos para {} {}.".format(self.P["symbol"], self.P["timeframe"])
        if self.limite:
            p = next((x for x in self.vigilados if x["id"] == self.limite["id"]), None)
            if p:
                return "Orden límite puesta: " + PL.describir(p)
        p = self.vigilados[0]
        if p["condicion"]["tipo"] == "limite":
            return "Plan activo; la orden límite se coloca en el próximo cierre de vela: " + PL.describir(p)
        return "Vigilando {} plan{}: {}".format(n, "es" if n > 1 else "", PL.describir(self.vigilados[0]))

    def guardar(self):
        return {"esperando": self.esperando, "racha": self.racha, "limite": self.limite, "plan_pos": self.plan_pos}

    def cargar(self, s):
        self.esperando = s.get("esperando")
        self.racha = dict(s.get("racha") or {})
        self.limite = s.get("limite")
        self.plan_pos = s.get("plan_pos")

    def reiniciar(self):
        self.racha = {}
