# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Borislav Vladimirov Petrakiev
"""
planes.py -- Planes manuales de KaBor Quant y conector MCP para Claude.

Un plan es tu análisis convertido en reglas fijas: «si una vela de 5m cierra por encima de 2648,5, largo con
stop en 2641 y objetivo en 2662; se anula si cierra por debajo de 2639; vale hasta las 21:00».

Ciclo de vida:
  pendiente  -> lo ha creado Claude (conector) o el formulario del panel. NO se opera.
  activo     -> lo has confirmado TÚ en el panel. El bot «Plan manual» de ese símbolo y temporalidad lo vigila.
  disparado  -> se cumplió la condición; la entrada va a la apertura de la vela siguiente.
  invalidado -> se cumplió la condición de invalidación antes de dispararse.
  caducado   -> llegó la hora límite sin dispararse.
  cancelado  -> lo cancelaste (un plan activo solo tras la espera de seguridad).

Freno contra las emociones: un plan activo no se puede editar. Cancelarlo pide una espera (FP_PLAN_ESPERA_MIN,
5 minutos por defecto) y vuelve a pedir confirmación. El stop y el objetivo nunca se tocan desde aquí.

El conector MCP solo puede LEER contexto, velas y los dibujos del gráfico, CREAR planes pendientes y cancelar planes pendientes. No puede
confirmar planes, operar ni cambiar ajustes.
"""
import hmac
import json
import os
import re
import secrets
import threading
import time
import uuid
from datetime import datetime

TF_MS = {"1m": 60_000, "3m": 180_000, "5m": 300_000, "15m": 900_000, "30m": 1_800_000, "1h": 3_600_000,
         "2h": 7_200_000, "4h": 14_400_000, "1d": 86_400_000, "1w": 604_800_000}
CONDICIONES = {
    "cierre_encima": "una vela cierra por encima de",
    "cierre_debajo": "una vela cierra por debajo de",
    "toca_arriba": "el precio sube y toca",
    "toca_abajo": "el precio baja y toca",
    "limite": "orden límite en",
}
INVALIDACIONES = ("cierre_encima", "cierre_debajo", "toca_arriba", "toca_abajo")
ESTADOS = ("pendiente", "activo", "disparado", "invalidado", "caducado", "cancelado")
VIVOS = ("pendiente", "activo")
MAX_DIAS = 7
MAX_PLANES = 300
ESTRATEGIA_ID = "plan_manual"

STORE = None          # lo crea kcbot.py al arrancar; en backtests y simulaciones queda en None
DIBUJOS = None        # análisis dibujado en el gráfico de Planes (niveles, zonas, tendencias)


def ahora():
    return int(time.time() * 1000)


def _num(v, nombre, errs):
    try:
        x = float(v)
        if x != x or x <= 0:
            raise ValueError
        return x
    except (TypeError, ValueError):
        errs.append("{}: tiene que ser un precio positivo".format(nombre))
        return None


def _fmt(x):
    return "{:,.5f}".format(x).rstrip("0").rstrip(".").replace(",", " ").replace(".", ",").replace(" ", ".")


def describir(p):
    c = p["condicion"]
    lado = "LARGO" if p["lado"] == "largo" else "CORTO"
    if c["tipo"] == "limite":
        s = "{} {} {}: {} con orden límite en {}".format(p["simbolo"], p["temporalidad"], p["id"], lado, _fmt(c["nivel"]))
    else:
        s = "{} {} {}: {} si {} {}".format(p["simbolo"], p["temporalidad"], p["id"], lado, CONDICIONES[c["tipo"]],
                                         _fmt(c["nivel"]))
    if c.get("velas", 1) > 1:
        s += " ({} cierres seguidos)".format(c["velas"])
    if p.get("tamano"):
        s += " · {} contratos".format(_fmt(p["tamano"]))
    if p.get("tp1"):
        s += "; SL {} · TP1 {} ({:g} %{}) · TP2 {}".format(_fmt(p["sl"]), _fmt(p["tp1"]), p.get("tp1_pct") or 0,
                                                          ", SL a BE" if p.get("tp1_be", True) else "", _fmt(p["tp"]))
    else:
        s += "; SL {} · TP {}".format(_fmt(p["sl"]), _fmt(p["tp"]))
    inv = p.get("invalidacion")
    if inv:
        s += "; se anula si {} {}".format(CONDICIONES[inv["tipo"]], _fmt(inv["nivel"]))
    return s


def tamano_info(hub, p):
    """Vista previa del tamaño y del riesgo de un plan en cada bot «Plan manual» que lo vigilaría."""
    out = []
    nivel, sl = p["condicion"]["nivel"], p["sl"]
    dist = abs(nivel - sl)
    if not dist:
        return out
    for b in hub.bots.values():
        P = b.P
        if P.get("estrategia") != ESTRATEGIA_ID or P.get("symbol") != p["simbolo"] or \
                P.get("timeframe") != p["temporalidad"] or b.meta.get("archived"):
            continue
        cuenta = bool(b.ex.activo()) if hasattr(b.ex, "activo") else False
        avisos, contratos, riesgo = [], None, None
        ref = float(P.get("ex_riesgo_usd") or 0) if cuenta and P.get("ex_riesgo_modo") == "importe fijo ($)" \
            else float(P.get("risk_usd") or 0)
        if p.get("tamano"):
            modo, contratos = "tamaño fijo del plan", float(p["tamano"])
        elif cuenta and P.get("ex_riesgo_modo") == "tamaño fijo (contratos)":
            modo, contratos = "tamaño fijo del bot", float(P.get("ex_tamano") or 0)
        elif cuenta and P.get("ex_riesgo_modo") == "importe fijo ($)":
            modo, riesgo = "riesgo fijo del bot", float(P.get("ex_riesgo_usd") or 0)
        elif cuenta:
            modo = "{:g} % del balance".format(float(P.get("ex_riesgo_pct") or 0))
        else:
            modo, riesgo = "riesgo del bot", float(P.get("risk_usd") or 0)
        if contratos is not None:
            riesgo = contratos * dist
        elif riesgo:
            contratos = riesgo / dist
        nominal = contratos * nivel if contratos else None
        cap = float(P.get("ex_nocional_max") or 0) if cuenta else 0
        if cap and nominal and nominal > cap:
            avisos.append("El nominal ({:,.0f} $) supera el máximo del bot ({:,.0f} $): se reducirá el tamaño".format(
                nominal, cap).replace(",", "."))
        if p.get("tamano") and ref and riesgo and riesgo > 1.5 * ref:
            m = lambda x: "{:,.0f}".format(x).replace(",", ".")
            avisos.append("Con ese tamaño arriesgas {} $, más de lo habitual en este bot ({} $)".format(m(riesgo), m(ref)))
        if cuenta and not p.get("tamano") and P.get("ex_riesgo_modo") == "% del balance":
            avisos.append("El tamaño se calcula al entrar con el balance de cada cuenta")
        out.append({"bot": b.name, "cuenta": cuenta, "modo": modo, "distancia": round(dist, 6),
                    "contratos": round(contratos, 4) if contratos else None,
                    "riesgo_usd": round(riesgo, 2) if riesgo else None,
                    "nominal_usd": round(nominal, 0) if nominal else None, "avisos": avisos})
    return out


def cumple(tipo, nivel, vela):
    _, o, h, l, cl = vela[:5]
    if tipo == "cierre_encima":
        return cl > nivel
    if tipo == "cierre_debajo":
        return cl < nivel
    if tipo == "toca_arriba":
        return h >= nivel
    if tipo == "toca_abajo":
        return l <= nivel
    return False                    # «limite» no es una condición de vela: la orden la llena el motor


def validar(d, instrumentos, precio=None, t=None):
    """Comprueba un plan recibido (dict). Devuelve (plan_normalizado, errores, avisos)."""
    errs, avisos = [], []
    t = t or ahora()
    sim = str(d.get("simbolo", "")).strip().upper()
    if sim not in instrumentos:
        errs.append("Símbolo «{}» no está en tus instrumentos ({})".format(sim, ", ".join(instrumentos)))
    tf = str(d.get("temporalidad", "")).strip()
    if tf not in TF_MS:
        errs.append("Temporalidad no válida: usa una de " + ", ".join(TF_MS))
    lado = str(d.get("lado", "")).strip().lower()
    lado = {"long": "largo", "short": "corto", "compra": "largo", "venta": "corto"}.get(lado, lado)
    if lado not in ("largo", "corto"):
        errs.append("Lado: «largo» o «corto»")
    c = d.get("condicion") or {}
    tipo = str(c.get("tipo", "")).strip()
    if tipo not in CONDICIONES:
        errs.append("Condición: usa una de " + ", ".join(CONDICIONES))
    nivel = _num(c.get("nivel"), "Nivel de la condición", errs)
    try:
        velas = int(c.get("velas", 1) or 1)
    except (TypeError, ValueError):
        velas = 0
    if tipo == "limite":
        velas = 1
    if not 1 <= velas <= 5:
        errs.append("Cierres seguidos: de 1 a 5")
    sl, tp = _num(d.get("sl"), "Stop", errs), _num(d.get("tp"), "Objetivo", errs)
    inv = d.get("invalidacion") or None
    if inv:
        itipo = str(inv.get("tipo", "")).strip()
        if itipo not in INVALIDACIONES:
            errs.append("Invalidación: usa una de " + ", ".join(INVALIDACIONES))
        inv = {"tipo": itipo, "nivel": _num(inv.get("nivel"), "Nivel de invalidación", errs)}
    try:
        exp = int(d.get("expira_ms") or 0)
    except (TypeError, ValueError):
        exp = 0
    if not t < exp <= t + MAX_DIAS * 86_400_000:
        errs.append("Caducidad: entre ahora y {} días (milisegundos UTC)".format(MAX_DIAS))
    s = 1 if lado == "largo" else -1
    if None not in (nivel, sl, tp) and lado in ("largo", "corto"):
        if (nivel - sl) * s <= 0:
            errs.append("El stop tiene que quedar {} del nivel de entrada".format("por debajo" if s == 1 else
                                                                              "por encima"))
        if (tp - nivel) * s <= 0:
            errs.append("El objetivo tiene que quedar {} del nivel de entrada".format("por encima" if s == 1 else
                                                                                  "por debajo"))
        if not errs:
            rr = (tp - nivel) * s / ((nivel - sl) * s)
            if rr < 1:
                avisos.append("R:R aproximado {:.2f} (menos de 1)".format(rr))
            if inv and inv["nivel"] is not None and inv["tipo"] in CONDICIONES and \
                    cumple(inv["tipo"], inv["nivel"], (0, nivel, nivel, nivel, nivel)):
                errs.append("La invalidación se cumpliría en el propio nivel de entrada: el plan nunca se dispararía")
    if precio and None not in (nivel, sl, tp) and tipo == "limite" and not errs:
        if (precio - nivel) * s <= 0:          # largo: precio en el límite o por debajo; corto: en él o por encima
            avisos.append("El precio actual ({}) ya está al otro lado del límite: se llenaría en cuanto se coloque, al "
                          "precio de ese momento".format(_fmt(precio)))
    if precio and None not in (nivel, sl, tp) and tipo in CONDICIONES and tipo != "limite" and not errs:
        if cumple(tipo, nivel, (0, precio, precio, precio, precio)):
            avisos.append("Con el precio actual ({}) la condición ya se cumple: se dispararía en el próximo "
                          "cierre".format(_fmt(precio)))
        if (precio - sl) * s <= 0:
            errs.append("El precio actual ({}) ya está al otro lado del stop".format(_fmt(precio)))
    tp1, tp1_pct, tp1_be = None, None, True
    if d.get("tp1") not in (None, ""):
        tp1 = _num(d.get("tp1"), "TP1", errs)
        try:
            tp1_pct = float(d.get("tp1_pct") if d.get("tp1_pct") not in (None, "") else 50)
        except (TypeError, ValueError):
            tp1_pct = -1
        if not 0 <= tp1_pct <= 90:
            errs.append("Porcentaje del TP1: de 0 a 90 (0 = solo mover el stop a BE)")
        tp1_be = bool(d.get("tp1_be", True))
        if tp1_pct == 0 and not tp1_be:
            errs.append("En el TP1 hay que vender algo o mover el stop a BE")
        if None not in (tp1, nivel, tp) and lado in ("largo", "corto") and not ((tp1 - nivel) * s > 0 and (tp - tp1) * s > 0):
            errs.append("El TP1 tiene que quedar entre la entrada y el TP final")
    tamano = None
    if d.get("tamano") not in (None, "", 0):
        tamano = _num(d.get("tamano"), "Tamaño (contratos)", errs)
        if tamano is not None and tamano > 1e6:
            errs.append("Tamaño (contratos): demasiado grande")
    analisis = str(d.get("analisis", "")).strip()[:2000]
    plan = {"simbolo": sim, "temporalidad": tf, "lado": lado,
            "condicion": {"tipo": tipo, "nivel": nivel, "velas": velas},
            "sl": sl, "tp": tp, "tp1": tp1, "tamano": tamano, "tp1_pct": tp1_pct, "tp1_be": tp1_be,
            "invalidacion": inv, "expira_ms": exp, "analisis": analisis,
            "notas_verificacion": [str(x)[:300] for x in (d.get("notas_verificacion") or [])][:10]}
    return plan, errs, avisos


ROLES_NIVEL = {"entrada": "Entrada", "stop": "Stop", "tp1": "TP1", "tp": "TP final", "invalidacion": "Invalidación",
               "nivel": "Nivel"}
ROLES_ZONA = {"fvg_alcista": "FVG alcista", "fvg_bajista": "FVG bajista", "ob_alcista": "Order block alcista",
              "ob_bajista": "Order block bajista", "liquidez": "Liquidez", "zona": "Zona"}
MAX_DIBUJOS = 80


def _limpiar_dibujo(o, tfs):
    """Normaliza un objeto dibujado. Devuelve (dict, error)."""
    if not isinstance(o, dict):
        return None, "Objeto no válido"
    tipo = o.get("tipo")
    out = {"id": re.sub(r"[^A-Za-z0-9_-]", "", str(o.get("id") or ""))[:24] or uuid.uuid4().hex[:8],
           "tipo": tipo, "texto": str(o.get("texto") or "").strip()[:120],
           "tf": str(o.get("tf") or "") if str(o.get("tf") or "") in tfs else None, "auto": bool(o.get("auto"))}

    def num(k, req=True):
        v = o.get(k)
        if v in (None, "") and not req:
            return None
        try:
            v = float(v)
        except (TypeError, ValueError):
            raise ValueError("{}: falta {}".format(tipo, k))
        if v != v or abs(v) > 1e15:
            raise ValueError("{}: {} no válido".format(tipo, k))
        return v
    try:
        if tipo == "nivel":
            out["rol"] = o.get("rol") if o.get("rol") in ROLES_NIVEL else "nivel"
            out["precio"] = num("precio")
            out["t"] = num("t", False)
        elif tipo == "zona":
            out["rol"] = o.get("rol") if o.get("rol") in ROLES_ZONA else "zona"
            a, b = num("p1"), num("p2")
            out["p1"], out["p2"] = max(a, b), min(a, b)
            t1, t2 = num("t1"), num("t2", False)
            out["t1"], out["t2"] = (min(t1, t2), max(t1, t2)) if t2 is not None else (t1, None)
        elif tipo == "tendencia":
            for k in ("t1", "p1", "t2", "p2"):
                out[k] = num(k)
        elif tipo == "posicion":
            out["lado"] = "corto" if o.get("lado") == "corto" else "largo"
            for k in ("entrada", "stop", "tp", "t1", "t2"):
                out[k] = num(k)
            out["tp1"], out["riesgo"], out["tamano"] = num("tp1", False), num("riesgo", False), num("tamano", False)
            for k in ("riesgo", "tamano"):
                if out[k] is not None and out[k] <= 0:
                    out[k] = None
        else:
            return None, "Tipo de dibujo desconocido: {}".format(tipo)
    except ValueError as e:
        return None, str(e)
    return out, None


class Dibujos:
    """Análisis dibujado por el usuario en el gráfico de Planes, por instrumento (dibujos.json)."""

    def __init__(self, path):
        self.path, self.lock = path, threading.Lock()
        try:
            with open(path, encoding="utf-8") as f:
                d = json.load(f)
            self.d = d if isinstance(d, dict) else {}
        except (OSError, ValueError):
            self.d = {}

    def de(self, simbolo):
        with self.lock:
            return [dict(o) for o in self.d.get(simbolo, {}).get("objetos", [])]

    def actualizado(self, simbolo):
        return self.d.get(simbolo, {}).get("t")

    def guardar(self, simbolo, objetos, tfs):
        if not isinstance(objetos, list):
            raise ValueError("Lista de dibujos no válida")
        if len(objetos) > MAX_DIBUJOS:
            raise ValueError("Demasiados dibujos en {} (máximo {})".format(simbolo, MAX_DIBUJOS))
        limpios, vistos = [], set()
        for o in objetos:
            c, err = _limpiar_dibujo(o, tfs)
            if err:
                raise ValueError(err)
            while c["id"] in vistos:
                c["id"] = uuid.uuid4().hex[:8]
            vistos.add(c["id"])
            limpios.append(c)
        with self.lock:
            if limpios:
                self.d[simbolo] = {"t": ahora(), "objetos": limpios}
            else:
                self.d.pop(simbolo, None)
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.d, f, ensure_ascii=False, indent=1)
            os.replace(tmp, self.path)
        return limpios


class Planes:
    """Almacén de planes (planes.json en la carpeta de datos). Seguro entre hilos."""

    def __init__(self, path, espera_min=5, aviso=None):
        self.path, self.espera_ms = path, int(espera_min) * 60_000
        self.aviso = aviso or (lambda texto: None)
        self.lock = threading.RLock()
        self.planes = []
        try:
            with open(path, encoding="utf-8") as f:
                self.planes = json.load(f).get("planes", [])
        except (OSError, ValueError):
            pass

    def _guardar(self):
        vivos = [p for p in self.planes if p["estado"] in VIVOS]
        resto = [p for p in self.planes if p["estado"] not in VIVOS][-(MAX_PLANES - len(vivos)):]
        self.planes = sorted(vivos + resto, key=lambda p: p["creado_ms"])
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"planes": self.planes}, f, indent=1, ensure_ascii=False)
        os.replace(tmp, self.path)

    def _get(self, pid):
        for p in self.planes:
            if p["id"] == pid:
                return p
        raise KeyError("No existe el plan " + str(pid))

    def _hist(self, p, texto):
        p.setdefault("historial", []).append({"t": ahora(), "texto": texto})

    # ---------- consultas ----------
    def lista(self, estado=None):
        with self.lock:
            return [dict(p) for p in self.planes if estado in (None, "", p["estado"])][::-1]

    def get(self, pid):
        with self.lock:
            try:
                return dict(self._get(pid))
            except KeyError:
                return None

    def para(self, simbolo, tf):
        with self.lock:
            return [dict(p) for p in self.planes if p["estado"] == "activo" and p["simbolo"] == simbolo
                    and p["temporalidad"] == tf]

    # ---------- cambios ----------
    def crear(self, plan, origen):
        with self.lock:
            if sum(1 for p in self.planes if p["estado"] in VIVOS) >= 20:
                raise ValueError("Ya hay 20 planes pendientes o activos: cancela alguno antes")
            p = dict(plan, id="P" + secrets.token_hex(3).upper(), estado="pendiente", origen=origen,
                     creado_ms=ahora(), confirmado_ms=None, cancelar_desde=None)
            self._hist(p, "Creado ({})".format(origen))
            self.planes.append(p)
            self._guardar()
        self.aviso("📝 <b>Plan pendiente de confirmar</b>\n{}\nConfírmalo en el panel → Planes.".format(describir(p)))
        return dict(p)

    def confirmar(self, pid):
        with self.lock:
            p = self._get(pid)
            if p["estado"] != "pendiente":
                raise ValueError("Solo se confirman planes pendientes")
            if p["expira_ms"] <= ahora():
                p["estado"] = "caducado"
                self._guardar()
                raise ValueError("El plan ya había caducado")
            p["estado"], p["confirmado_ms"] = "activo", ahora()
            self._hist(p, "Confirmado en el panel")
            self._guardar()
        self.aviso("✅ <b>Plan activo</b>\n" + describir(p))
        return dict(p)

    def cancelar(self, pid, desde_panel):
        """Pendiente: se cancela al momento. Activo (solo panel): primero se pide y, pasada la espera, se confirma."""
        with self.lock:
            p = self._get(pid)
            if p["estado"] == "pendiente":
                p["estado"] = "cancelado"
                self._hist(p, "Cancelado ({})".format("panel" if desde_panel else "Claude"))
                self._guardar()
                return dict(p), None
            if p["estado"] != "activo":
                raise ValueError("Ese plan ya no está vivo")
            if not desde_panel:
                raise ValueError("Un plan activo solo se puede cancelar desde el panel")
            t = ahora()
            if not p.get("cancelar_desde"):
                p["cancelar_desde"] = t + self.espera_ms
                self._hist(p, "Cancelación pedida")
                self._guardar()
                return dict(p), "Cancelación pedida. Podrás confirmarla a partir de las {} (espera de seguridad)." \
                    .format(time.strftime("%H:%M", time.localtime(p["cancelar_desde"] / 1000)))
            if t < p["cancelar_desde"]:
                falta = int((p["cancelar_desde"] - t) / 60_000) + 1
                return dict(p), "Aún faltan {} min para poder cancelarlo.".format(falta)
            p["estado"] = "cancelado"
            self._hist(p, "Cancelado tras la espera")
            self._guardar()
            return dict(p), None

    def retirar_cancelacion(self, pid):
        with self.lock:
            p = self._get(pid)
            p["cancelar_desde"] = None
            self._hist(p, "Cancelación retirada")
            self._guardar()
            return dict(p)

    def marcar(self, pid, estado, texto, extra=None):
        """Lo usa el bot «Plan manual»."""
        with self.lock:
            try:
                p = self._get(pid)
            except KeyError:
                return
            if estado:
                p["estado"] = estado
            p.update(extra or {})
            self._hist(p, texto)
            self._guardar()
        emoji = {"disparado": "🎯", "invalidado": "⛔", "caducado": "⌛"}.get(estado, "ℹ️")
        self.aviso("{} <b>Plan {}</b>: {}\n{}".format(emoji, pid, texto, describir(p)))


# ============================ conector MCP ============================
PROTOCOLOS = ("2025-06-18", "2025-03-26", "2024-11-05")
INSTRUCCIONES = (
    "Conector de KaBor Quant, un sistema de bots de trading (cuenta sandbox de MyFundedPerps). "
    "Flujo: 1) kabor_contexto para ver instrumentos, bots «Plan manual» y la hora del servidor; 2) kabor_velas del "
    "símbolo y temporalidad para comprobar que los niveles del análisis existen en los precios reales (FVG, máximos, "
    "mínimos); 3) señala al usuario incoherencias (stop al lado equivocado, R:R bajo, condición ambigua, niveles que no "
    "coinciden con las velas) y pide aclaración; 4) solo cuando el usuario lo apruebe en el chat, kabor_crear_plan. "
    "Si el planteamiento es vender o comprar en un nivel (rechazo de un swing, de un FVG, toque de un SSL o BSL), "
    "propón la condición «limite» (orden límite en ese precio): se llena cuando el precio lo toca, sin esperar al "
    "cierre, y se retira sola si el precio llega antes al objetivo, si se cumple la invalidación o si caduca. Órdenes "
    "límite: en papel se llenan al tocar el nivel; en un bot con cuenta sandbox KaBor vigila el precio en vivo y "
    "envía la entrada a mercado al tocarlo (puede haber un poco de deslizamiento). Gestión: un TP final (tp) y, "
    "opcional, un TP1 (tp1) donde se vende tp1_pct % y, si tp1_be, el stop pasa a break-even; también en la cuenta. "
    "Tamaño: por defecto el bot dimensiona por su riesgo (contratos = riesgo / distancia al stop); si el usuario quiere "
    "unos contratos concretos usa tamano, y dile el riesgo que resulta (tamano_y_riesgo en la respuesta). "
    "Si el usuario ha marcado su análisis en el gráfico de Planes (niveles de entrada, stop, TP, invalidación, zonas FVG u order blocks, tendencias), kabor_dibujos los lee con el estado de cada zona respecto al precio: úsalos como base del plan, compruébalos con kabor_velas y señala lo que no cuadre antes de proponerlo. "
    "Para revisar resultados tienes kabor_bots, kabor_operaciones y kabor_estadisticas (solo lectura): da al usuario una "
    "opinión honesta y concreta, con números, separando la calidad de su análisis de la de su ejecución, y sin "
    "sacar conclusiones fuertes con pocas operaciones. "
    "El plan queda PENDIENTE: el usuario lo confirma en el panel. Tú no puedes confirmar, operar ni cancelar planes activos.")


def _tool(name, desc, props, req):
    return {"name": name, "description": desc,
            "inputSchema": {"type": "object", "properties": props, "required": req, "additionalProperties": False}}


_COND = {"type": "object", "properties": {
    "tipo": {"type": "string", "enum": list(CONDICIONES),
             "description": "cierre_encima/cierre_debajo: cierre de vela y entrada a mercado en la apertura "
                            "siguiente; toca_arriba: máximo >= nivel; toca_abajo: mínimo <= nivel; limite: orden "
                            "límite en el nivel (se llena al tocarlo; en cuentas sandbox se vigila en vivo y entra a "
                            "mercado al tocarlo; no vale como invalidación)"},
    "nivel": {"type": "number"},
    "velas": {"type": "integer", "minimum": 1, "maximum": 5, "description": "Cierres seguidos (solo condiciones de "
                                                                             "cierre). Por defecto 1"}},
    "required": ["tipo", "nivel"], "additionalProperties": False}

HERRAMIENTAS = [
    _tool("kabor_contexto", "Instrumentos, temporalidades, bots «Plan manual» en marcha, planes vivos y hora del "
                            "servidor (UTC y Madrid). Llámala primero.", {}, []),
    _tool("kabor_velas", "Últimas velas cerradas reales (UTC) de un instrumento: [hora, apertura, máximo, mínimo, "
                         "cierre]. Úsala para verificar los niveles de la captura. 1d y 1w son días UTC y semanas de lunes "
                         "(construidos con velas de 4h), como en Binance y TradingView.",
          {"simbolo": {"type": "string"}, "temporalidad": {"type": "string", "enum": list(TF_MS)},
           "n": {"type": "integer", "minimum": 10, "maximum": 300}}, ["simbolo", "temporalidad"]),
    _tool("kabor_crear_plan", "Crea un plan PENDIENTE (el usuario lo confirma en el panel). La entrada es a mercado en la "
                              "apertura de la vela siguiente a la que cumple la condición; el tamaño lo calcula el "
                              "riesgo del bot. Llámala solo cuando el usuario haya aprobado el plan en el chat.",
          {"simbolo": {"type": "string"}, "temporalidad": {"type": "string", "enum": list(TF_MS)},
           "lado": {"type": "string", "enum": ["largo", "corto"]}, "condicion": _COND,
           "sl": {"type": "number", "description": "Stop (precio)"},
           "tp": {"type": "number", "description": "Objetivo final (precio)"},
           "tp1": {"type": "number", "description": "Opcional: objetivo parcial entre la entrada y el final"},
           "tamano": {"type": "number", "description": "Opcional: tamaño fijo en contratos/unidades para la cuenta. "
                      "Sin él, el bot dimensiona por su riesgo. Riesgo = contratos × distancia al stop × valor del punto "
                      "(NAS100 en MFP: 1 $ por punto y unidad)"},
           "tp1_pct": {"type": "number", "minimum": 0, "maximum": 90,
                       "description": "Porcentaje que se cierra en el TP1 (0 = solo mover el stop). Por defecto 50"},
           "tp1_be": {"type": "boolean", "description": "Mover el stop a break-even al tocar el TP1. Por defecto sí"},
           "invalidacion": dict(_COND, properties=dict(_COND["properties"], tipo={
               "type": "string", "enum": list(INVALIDACIONES)}),
               description="Opcional: si se cumple antes de dispararse (o antes de llenarse la orden límite), el plan "
                           "se anula"),
           "expira_ms": {"type": "integer", "description": "Caducidad en milisegundos UTC (máximo 7 días)"},
           "analisis": {"type": "string", "description": "Resumen del análisis del usuario"},
           "notas_verificacion": {"type": "array", "items": {"type": "string"},
                                  "description": "Lo que comprobaste y lo que no cuadraba"}},
          ["simbolo", "temporalidad", "lado", "condicion", "sl", "tp", "expira_ms", "analisis"]),
    _tool("kabor_dibujos", "Análisis que el usuario ha dibujado en el gráfico de Planes de un instrumento: niveles con "
                           "su papel (entrada, stop, tp1, tp, invalidación, nivel), posiciones largas o cortas "
                           "(entrada, stop, TP1, TP, riesgo), zonas (FVG, order blocks, liquidez) "
                           "con su estado respecto al precio (sin tocar, tocada, rellenada) y líneas de tendencia. Las "
                           "horas van en UTC. Sin símbolo, dice qué instrumentos tienen dibujos.",
          {"simbolo": {"type": "string"}}, []),
    _tool("kabor_planes", "Lista de planes (opcional: filtrar por estado).",
          {"estado": {"type": "string", "enum": list(ESTADOS)}}, []),
    _tool("kabor_cancelar_pendiente", "Cancela un plan PENDIENTE (los activos solo desde el panel).",
          {"id": {"type": "string"}}, ["id"]),
    _tool("kabor_bots", "Estado de los bots (solo lectura): en marcha o no, posición abierta y su R latente, R de hoy, "
                        "de la semana y total, % de acierto, profit factor, drawdown, si operan en cuentas, y el "
                        "estado del riesgo global y del vigilante de deriva.",
          {"incluir_archivados": {"type": "boolean"}}, []),
    _tool("kabor_operaciones", "Operaciones cerradas (solo lectura) de los bots y las apuntadas a mano, con la nota, "
                               "las etiquetas y si siguió el plan según el diario. Horas en hora de España.",
          {"dias": {"type": "integer", "minimum": 1, "maximum": 365},
           "bot": {"type": "string", "description": "Opcional: parte del nombre del bot o del símbolo"},
           "max": {"type": "integer", "minimum": 1, "maximum": 300}}, []),
    _tool("kabor_estadisticas", "Estadísticas (solo lectura) por bot en un periodo: métricas, largos frente a cortos, "
                                "motivos de salida, horas y días de la semana, MFE/MAE medios y, del diario, R de las "
                                "operaciones que siguieron el plan frente a las que no. Úsala para revisar resultados.",
          {"dias": {"type": "integer", "minimum": 1, "maximum": 365},
           "bot": {"type": "string", "description": "Opcional: parte del nombre del bot o del símbolo"}}, []),
]


class ConectorMCP:
    """Servidor MCP mínimo (HTTP con respuestas JSON, sin estado). `hub` aporta datos de KaBor."""

    def __init__(self, hub, token, version):
        self.hub, self.token, self.version = hub, token, version

    def autorizado(self, tok):
        return bool(self.token) and len(self.token) >= 24 and hmac.compare_digest(tok.encode(), self.token.encode())

    def atender(self, msg):
        """Un mensaje JSON-RPC -> respuesta (dict) o None si es una notificación."""
        if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0":
            return {"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "Petición no válida"}}
        mid, met, par = msg.get("id"), msg.get("method"), msg.get("params") or {}
        if mid is None:
            return None
        try:
            if met == "initialize":
                pv = par.get("protocolVersion")
                res = {"protocolVersion": pv if pv in PROTOCOLOS else PROTOCOLOS[0],
                       "capabilities": {"tools": {"listChanged": False}},
                       "serverInfo": {"name": "kabor-quant", "version": self.version},
                       "instructions": INSTRUCCIONES}
            elif met == "ping":
                res = {}
            elif met == "tools/list":
                res = {"tools": HERRAMIENTAS}
            elif met == "tools/call":
                res = self._llamar(par.get("name"), par.get("arguments") or {})
            else:
                return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": "Método no disponible"}}
        except Exception as e:
            res = {"content": [{"type": "text", "text": "Error: {}".format(e)}], "isError": True}
        return {"jsonrpc": "2.0", "id": mid, "result": res}

    def _ok(self, obj, error=False):
        return {"content": [{"type": "text", "text": json.dumps(obj, ensure_ascii=False, indent=1)}],
                "isError": error}

    def _llamar(self, nombre, a):
        h = self.hub
        if nombre == "kabor_contexto":
            bots = [{"bot": b.name, "simbolo": b.P["symbol"], "temporalidad": b.P["timeframe"],
                     "riesgo_usd": b.P.get("risk_usd")} for b in h.bots.values()
                    if b.P.get("estrategia") == ESTRATEGIA_ID and not b.meta.get("archived")]
            t = h.req.server_now() if h.req else ahora()
            return self._ok({"hora_utc_ms": t, "hora_utc": time.strftime("%Y-%m-%d %H:%M", time.gmtime(t / 1000)),
                             "hora_local": datetime.fromtimestamp(t / 1000, h.tz).strftime("%Y-%m-%d %H:%M %Z"), "instrumentos": [i["symbol"] for i in h.markets.list()],
                             "temporalidades": list(TF_MS), "condiciones": CONDICIONES,
                             "bots_plan_manual": bots, "planes_vivos": [describir(p) for p in STORE.lista()
                                                                         if p["estado"] in VIVOS],
                             "aviso": None if bots else "No hay ningún bot «Plan manual» en marcha: un plan activo "
                                                        "no se vigilará hasta que el usuario cree uno con ese símbolo y "
                                                        "temporalidad."})
        if nombre == "kabor_velas":
            sim, tf = str(a.get("simbolo", "")).upper(), a.get("temporalidad")
            if tf not in TF_MS:
                raise ValueError("Temporalidad no válida")
            n = max(10, min(int(a.get("n", 120)), 300))
            cs = h.req.candles(h.markets.resolve(sim), tf, n)
            fmt = "%Y-%m-%d" if TF_MS[tf] >= 86_400_000 else "%m-%d %H:%M"
            filas = [[time.strftime(fmt, time.gmtime(c[0] / 1000))] + [c[k] for k in range(1, 5)]
                     for c in cs[-n:]]
            return self._ok({"simbolo": sim, "temporalidad": tf, "zona": "UTC", "columnas": ["hora", "o", "h", "l", "c"],
                             "velas": filas, "ultimo_cierre": cs[-1][4] if cs else None})
        if nombre == "kabor_crear_plan":
            inst = [i["symbol"] for i in h.markets.list()]
            sim = str(a.get("simbolo", "")).upper()
            precio = None
            if sim in inst and a.get("temporalidad") in TF_MS:
                try:
                    cs = h.req.candles(h.markets.resolve(sim), a["temporalidad"], 3)
                    precio = cs[-1][4] if cs else None
                except Exception:
                    pass
            plan, errs, avisos = validar(a, inst, precio, h.req.server_now() if h.req else None)
            if errs:
                return self._ok({"creado": False, "errores": errs, "avisos": avisos}, error=True)
            p = STORE.crear(plan, "Claude")
            return self._ok({"creado": True, "id": p["id"], "estado": "pendiente", "resumen": describir(p),
                             "tamano_y_riesgo": tamano_info(h, p),
                             "avisos": avisos, "siguiente_paso": "El usuario tiene que confirmarlo en el panel → Planes."})
        if nombre == "kabor_dibujos":
            return self._ok(self._dibujos(str(a.get("simbolo") or "").upper()))
        if nombre == "kabor_planes":
            return self._ok([dict({"id": p["id"], "estado": p["estado"], "resumen": describir(p),
                                   "historial": p.get("historial", [])[-6:],
                                   "tamano_y_riesgo": tamano_info(self.hub, p) if p["estado"] in VIVOS else None},
                                  **self._resultado_plan(p))
                             for p in STORE.lista(a.get("estado"))[:30]])
        if nombre == "kabor_bots":
            return self._ok(self._bots(bool(a.get("incluir_archivados"))))
        if nombre == "kabor_operaciones":
            return self._ok(self._operaciones(int(a.get("dias") or 30), a.get("bot"), int(a.get("max") or 100)))
        if nombre == "kabor_estadisticas":
            return self._ok(self._estadisticas(int(a.get("dias") or 30), a.get("bot")))
        if nombre == "kabor_cancelar_pendiente":
            p, _ = STORE.cancelar(str(a.get("id", "")), desde_panel=False)
            return self._ok({"id": p["id"], "estado": p["estado"]})
        raise ValueError("Herramienta desconocida: {}".format(nombre))

    # ---------- dibujos del gráfico (9.0) ----------
    def _dibujos(self, sim):
        h = self.hub
        if DIBUJOS is None:
            raise ValueError("Los dibujos no están disponibles")
        if not sim:
            return {"instrumentos_con_dibujos": {k: len(v.get("objetos", [])) for k, v in DIBUJOS.d.items()},
                    "siguiente_paso": "Llama a kabor_dibujos con uno de esos símbolos."}
        objs = DIBUJOS.de(sim)
        if not objs:
            return {"simbolo": sim, "objetos": [], "aviso": "No hay nada dibujado en {}.".format(sim)}
        hora = lambda t: time.strftime("%Y-%m-%d %H:%M", time.gmtime(t / 1000)) if t else None
        velas, precio = {}, None
        try:
            mk = h.markets.resolve(sim)
            for tf in {o.get("tf") or "15m" for o in objs if o["tipo"] == "zona"} | {"1m"}:
                velas[tf] = h.req.candles(mk, tf, 300)
            precio = velas["1m"][-1][4] if velas.get("1m") else None
        except Exception:
            pass
        out = []
        for o in objs:
            d = {"id": o["id"], "tipo": o["tipo"], "texto": o.get("texto") or None, "tf_dibujado": o.get("tf"),
                 "automatico": o.get("auto") or None}
            if o["tipo"] == "nivel":
                d.update(papel=ROLES_NIVEL.get(o["rol"], o["rol"]), precio=o["precio"])
                if precio:
                    d["distancia_pct"] = round((o["precio"] - precio) / precio * 100, 3)
            elif o["tipo"] == "zona":
                d.update(papel=ROLES_ZONA.get(o["rol"], o["rol"]), arriba=o["p1"], abajo=o["p2"],
                         desde=hora(o["t1"]), hasta=hora(o["t2"]) if o.get("t2") else "se extiende hasta ahora")
                ztf = o.get("tf") or "15m"
                desde = o["t2"] if o.get("t2") else o["t1"] + TF_MS.get(ztf, 0)   # sin la vela que forma el hueco
                cs = [c for c in velas.get(ztf, []) if c[0] > desde]
                if cs:
                    alc = o["rol"] in ("fvg_alcista", "ob_alcista")
                    baj = o["rol"] in ("fvg_bajista", "ob_bajista")
                    toca = any(c[3] <= o["p1"] and c[2] >= o["p2"] for c in cs)
                    rell = any(c[3] <= o["p2"] for c in cs) if alc else any(c[2] >= o["p1"] for c in cs) if baj else False
                    d["estado"] = "rellenada" if rell else "tocada" if toca else "sin tocar"
                    d["velas_comprobadas"] = "{} de {}".format(len(cs), o.get("tf") or "15m")
                if precio:
                    d["precio_actual_respecto"] = "dentro" if o["p2"] <= precio <= o["p1"] else \
                        "por encima" if precio > o["p1"] else "por debajo"
            elif o["tipo"] == "posicion":
                s = 1 if o["lado"] == "largo" else -1
                dist = abs(o["entrada"] - o["stop"])
                d.update(lado=o["lado"], entrada=o["entrada"], stop=o["stop"], tp1=o.get("tp1"), tp=o["tp"],
                         rr=round((o["tp"] - o["entrada"]) * s / dist, 2) if dist else None,
                         riesgo_usd=o.get("riesgo"), contratos=o.get("tamano"), desde=hora(o["t1"]))
                if (o["entrada"] - o["stop"]) * s <= 0 or (o["tp"] - o["entrada"]) * s <= 0:
                    d["aviso"] = "Stop u objetivo al lado equivocado de la entrada"
            else:
                d.update(desde=[hora(o["t1"]), o["p1"]], hasta=[hora(o["t2"]), o["p2"]])
            out.append(d)
        roles = {o["rol"]: o["precio"] for o in objs if o["tipo"] == "nivel"}
        pos = [o for o in objs if o["tipo"] == "posicion"]
        if pos:   # la herramienta Posición manda sobre los niveles sueltos
            o = pos[-1]
            roles.update(entrada=o["entrada"], stop=o["stop"], tp=o["tp"])
            if o.get("tp1") is not None:
                roles["tp1"] = o["tp1"]
        res = {"simbolo": sim, "precio_actual": precio, "actualizado": hora(DIBUJOS.actualizado(sim)), "objetos": out}
        if {"entrada", "stop", "tp"} <= set(roles):
            s = 1 if roles["tp"] > roles["entrada"] else -1
            riesgo = (roles["entrada"] - roles["stop"]) * s
            res["propuesta_desde_niveles"] = {
                "lado": "largo" if s == 1 else "corto", "entrada": roles["entrada"], "stop": roles["stop"],
                "tp": roles["tp"], "tp1": roles.get("tp1"), "invalidacion": roles.get("invalidacion"),
                "rr": round((roles["tp"] - roles["entrada"]) * s / riesgo, 2) if riesgo > 0 else None,
                "aviso": None if riesgo > 0 else "El stop está al lado equivocado de la entrada"}
        return res

    # ---------- lectura de resultados (7.8) ----------
    def _hora(self, ms):
        return datetime.fromtimestamp(ms / 1000, self.hub.tz).strftime("%d/%m %H:%M") if ms else None

    @staticmethod
    def _coincide(b, filtro):
        f = str(filtro or "").strip().lower()
        return not f or f in b.name.lower() or f == str(b.P.get("symbol", "")).lower() or f == str(b.id)

    def _resultado_plan(self, p):
        """R de la operación de un plan disparado (se busca en los bots «Plan manual» de su símbolo y temporalidad)."""
        te = p.get("t_entrada")
        if not te:
            return {}
        for b in self.hub.bots.values():
            if b.P.get("estrategia") != ESTRATEGIA_ID or b.P.get("symbol") != p["simbolo"] or \
                    b.P.get("timeframe") != p["temporalidad"]:
                continue
            with b.lock:
                x = next((dict(y) for y in b.trades if y.get("t_open") == te), None)
                abierta = bool(b.st and b.st.pos and b.st.pos.get("t_open") == te)
            if x:
                return {"resultado": {"r": round(x["r"], 2), "salida": x.get("exit"), "motivo": x.get("why"),
                                      "cierre": self._hora(x.get("t_close"))}}
            if abierta:
                return {"resultado": {"abierta": True}}
        return {}

    def _bots(self, archivados):
        h = self.hub
        out = []
        for b in h.bots.values():
            if b.meta.get("archived") and not archivados:
                continue
            s = b.summary()
            out.append({"id": s["id"], "bot": s["name"], "estrategia": s["strategy"], "simbolo": s["symbol"],
                        "tf": s["tf"], "estado": s["status_text"], "precio": s["price"],
                        "posicion": s["position"], "hoy_r": s["today_r"], "semana_r": s["week_r"],
                        "total_r": s["total_r"], "operaciones": s["n"], "acierto_pct": round(s["winrate"] or 0, 1),
                        "pf": round(s["pf"], 2) if s["pf"] is not None else None,
                        "max_dd_r": s["max_dd"], "dias_en_marcha": s["days"], "archivado": s["archived"],
                        "cuentas": (s.get("exec") or {}).get("cuentas") if (s.get("exec") or {}).get("activo") else [],
                        "tendencia": [t.get("texto") for t in (s.get("trend") or [])] or None})
        riesgo = None
        try:
            r = h.riesgo.public()
            riesgo = {"hoy": r.get("hoy"), "semana": r.get("semana"), "abiertas": r.get("abiertas"),
                      "bloqueo": r.get("guard")}
        except Exception:
            pass
        try:
            deriva = {b.name: e for bid, e in h.vigil.resumen().items() for b in [h.bots.get(bid)] if b}
        except Exception:
            deriva = None
        return {"hora": self._hora(int(time.time() * 1000)), "bots": out, "riesgo_global": riesgo,
                "vigilante_deriva": deriva, "unidad": "R (1 R = el riesgo de cada operación)"}

    def _operaciones(self, dias, filtro, maximo):
        ops = self.hub.diario.listar(dias)["ops"]
        f = str(filtro or "").strip().lower()
        if f:
            ops = [o for o in ops if f in str(o.get("bot", "")).lower() or f == str(o.get("simbolo", "")).lower()]
        filas = [{"bot": o.get("bot"), "simbolo": o.get("simbolo"), "tf": o.get("tf"), "lado": o.get("lado"),
                  "apertura": self._hora(o.get("t_open")), "cierre": self._hora(o.get("t_close")),
                  "r": round(o["r"], 2) if o.get("r") is not None else None, "usd": o.get("usd"),
                  "motivo": o.get("motivo"), "min": round(o["dur"]) if o.get("dur") else None,
                  "nota": o.get("nota") or None, "etiquetas": o.get("etiquetas") or None,
                  "siguio_plan": o.get("plan")} for o in ops[:maximo]]
        return {"dias": dias, "total": len(ops), "mostradas": len(filas), "operaciones": filas}

    def _estadisticas(self, dias, filtro):
        import nucleo as K
        h = self.hub
        desde = int(time.time() * 1000) - dias * 86_400_000
        dias_sem = ["lun", "mar", "mié", "jue", "vie", "sáb", "dom"]
        diario = {}
        for o in h.diario.listar(dias)["ops"]:
            if o.get("tipo") == "bot":
                diario[(o["bot_id"], o.get("t_open"))] = o.get("plan")

        def resumen(m):
            return {"n": m["n"], "total_r": round(m["total_r"], 2), "acierto_pct": round(m["winrate"], 1),
                    "r_medio": round(m["avg_r"], 3), "pf": round(m["pf"], 2) if m.get("pf") is not None else None,
                    "max_dd_r": round(m["max_dd"], 2), "sqn": round(m["sqn"], 2) if m.get("sqn") is not None else None,
                    "mejor": round(m["best"], 2), "peor": round(m["worst"], 2),
                    "racha_perdedora_max": m["max_loss_streak"]}

        def grupo(trades, clave):
            g = {}
            for x in trades:
                g.setdefault(clave(x), []).append(x["r"])
            return {k: {"n": len(v), "total_r": round(sum(v), 2), "acierto_pct": round(100 * sum(1 for r in v if r > 0)
                                                                                    / len(v), 1)}
                    for k, v in sorted(g.items(), key=lambda kv: str(kv[0]))}

        out = []
        for b in h.bots.values():
            if not self._coincide(b, filtro):
                continue
            with b.lock:
                tr = [dict(x) for x in b.trades if (x.get("t_close") or 0) >= desde]
                nombre, P = b.name, dict(b.P)
            if not tr:
                continue
            loc = lambda ms: datetime.fromtimestamp(ms / 1000, h.tz)
            plan = {"si": [], "no": []}
            for x in tr:
                v = diario.get((b.id, x.get("t_open")))
                if v in plan:
                    plan[v].append(x["r"])
            mfe = [x.get("mfe") for x in tr if x.get("mfe") is not None]
            mae = [x.get("mae") for x in tr if x.get("mae") is not None]
            out.append({"bot": nombre, "estrategia": P.get("estrategia"), "simbolo": P.get("symbol"),
                        "tf": P.get("timeframe"), "metricas": resumen(K.metrics(tr)),
                        "largos": resumen(K.metrics([x for x in tr if x.get("side") == 1])),
                        "cortos": resumen(K.metrics([x for x in tr if x.get("side") == -1])),
                        "por_motivo": grupo(tr, lambda x: x.get("why")),
                        "por_hora_espana": grupo(tr, lambda x: loc(x["t_open"]).hour),
                        "por_dia": grupo(tr, lambda x: dias_sem[loc(x["t_open"]).weekday()]),
                        "mfe_medio_r": round(sum(mfe) / len(mfe), 2) if mfe else None,
                        "mae_medio_r": round(sum(mae) / len(mae), 2) if mae else None,
                        "diario_siguio_plan": {k: {"n": len(v), "total_r": round(sum(v), 2)} for k, v in plan.items()}})
        return {"dias": dias, "bots": out, "nota": "R por operación; las horas van en hora de España"}
