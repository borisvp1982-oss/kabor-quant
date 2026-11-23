# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Borislav Vladimirov Petrakiev
"""
cuentas.py -- Cuentas de MyFundedPerps configurables desde el panel (v6.3).

  * Varias cuentas, cada una con su clave de API (fp_test_: solo sandbox), el ID de la cuenta en MyFundedPerps (vacío =
    la primera activa que vea la clave), un multiplicador de riesgo y un interruptor propio.
  * Interruptor general «Enviar órdenes» (sustituye a FP_EXEC de config.env, que solo se lee una vez al actualizar).
  * Cada bot elige en qué cuentas opera (ajuste «Cuentas» del bot): una, o varias a la vez con la misma señal.
  * Bloqueo de la cuenta:
      - a mano desde el panel o por Telegram, con duración (horas o «hasta mañana»);
      - automático al llegar a un límite del día: pérdida o ganancia en $, % del daily loss de MyFundedPerps
        consumido u operaciones abiertas en el día;
      - opcionalmente cierra posiciones y cancela órdenes al bloquear (como el Trader Lockout de la web);
      - «estricto»: no se puede quitar antes de que venza (como en MyFundedPerps).
    El bloqueo es del programa: impide que los bots abran operaciones nuevas en esa cuenta. La API de MyFundedPerps no
    permite activar su Trader Lockout (solo la web), pero si lo activas allí el programa lo detecta (error
    account_trader_lockout_active) y deja la cuenta bloqueada hasta la hora de desbloqueo que devuelve la API.
  * El día de la cuenta empieza a las 00:00 de Nueva York (como el daily loss de MyFundedPerps), con horario de verano.

Se guarda en data/cuentas.json (permisos 600). Las claves nunca salen hacia el panel: solo sus últimos caracteres.
"""
import json
import os
import re
import threading
import time
from datetime import datetime, timedelta

import ejecucion as E

FILE = "cuentas.json"
MAX_CUENTAS = 10
ID_RE = re.compile(r"^c[0-9]{1,4}$")
MFP_ID_RE = re.compile(r"^[A-Za-z0-9_.:-]{0,80}$")
BLOQUEO_DEF = {"auto": False, "perdida_usd": 0.0, "ganancia_usd": 0.0, "dll_pct": 0.0, "max_ops": 0,
               "duracion": "mañana", "horas": 4.0, "cerrar": "bots", "estricto": True, "medir": "cuenta"}
DURACIONES = ("mañana", "horas")
CIERRES = ("nada", "todo", "bots")        # qué se cierra al bloquear
MEDIDAS = ("cuenta", "bots")              # qué cuenta para los límites en $ del día
PAUSA_S = 3                 # entre peticiones de cierre/cancelación (el cortafuegos corta las ráfagas)
REINTENTO_MS = 60_000       # reintento de cancelar órdenes tras un bloqueo
MAX_REINTENTOS = 10
NY = "America/New_York"


def now_ms():
    return int(time.time() * 1000)


def _tz():
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(NY)
    except Exception:            # sin tzdata: aproximación con UTC-5 (sin horario de verano)
        from datetime import timezone
        return timezone(timedelta(hours=-5))


TZ_NY = _tz()


def dia_ny(ms=None):
    """Día de trading (fecha de Nueva York) como 'AAAA-MM-DD'."""
    return datetime.fromtimestamp((ms or now_ms()) / 1000, TZ_NY).strftime("%Y-%m-%d")


def medianoche_ny(ms=None):
    """Próxima medianoche de Nueva York, en ms (lo que MyFundedPerps llama «Until Tomorrow»)."""
    d = datetime.fromtimestamp((ms or now_ms()) / 1000, TZ_NY)
    nxt = datetime(d.year, d.month, d.day, tzinfo=TZ_NY) + timedelta(days=1)
    nxt = datetime(nxt.year, nxt.month, nxt.day, tzinfo=TZ_NY)       # recalcula el desfase (cambio de hora)
    return int(nxt.timestamp() * 1000)


def mascara(clave):
    return "fp_test_…" + clave[-4:] if clave and len(clave) > 12 else ("…" if clave else "")


def _num(raw, k, lo, hi, errs, nombre, entero=False):
    try:
        v = float(raw.get(k) or 0)
        if v != v or v < lo or v > hi:
            raise ValueError
        return int(round(v)) if entero else v
    except (TypeError, ValueError):
        errs.append("{}: entre {:g} y {:g}".format(nombre, lo, hi))
        return None


def cierre_de(v):
    """«cerrar» del bloqueo: True/False de versiones anteriores o uno de CIERRES."""
    if v is True:
        return "todo"
    if v is False or v is None:
        return "nada"
    return v if v in CIERRES else None


def validar_bloqueo(raw, base=None):
    c, errs = dict(BLOQUEO_DEF), []
    c.update(base or {})
    raw = raw or {}
    if not base:                              # configuración guardada: hasta la 6.3.1 «cerrar» era sí/no
        c["cerrar"] = cierre_de(raw.get("cerrar", c["cerrar"]))
        c.setdefault("medir", "cuenta")
    for k in ("auto", "estricto"):
        if k in raw:
            c[k] = bool(raw[k])
    if "cerrar" in raw:
        v = cierre_de(raw["cerrar"])
        if v is None:
            errs.append("Al bloquear: valor no válido")
        else:
            c["cerrar"] = v
    if "medir" in raw:
        if raw["medir"] in MEDIDAS:
            c["medir"] = raw["medir"]
        else:
            errs.append("Qué cuenta para el límite: valor no válido")
    for k, lo, hi, nombre, ent in (("perdida_usd", 0, 1e7, "Pérdida del día", False),
                                   ("ganancia_usd", 0, 1e7, "Ganancia del día", False),
                                   ("dll_pct", 0, 100, "Daily loss consumido", False),
                                   ("max_ops", 0, 200, "Operaciones al día", True),
                                   ("horas", 0.25, 168, "Horas de bloqueo", False)):
        if k in raw:
            v = _num(raw, k, lo, hi, errs, nombre, ent)
            if v is not None:
                c[k] = v
    if "duracion" in raw:
        if raw["duracion"] in DURACIONES:
            c["duracion"] = raw["duracion"]
        else:
            errs.append("Duración del bloqueo: valor no válido")
    return c, errs


# ============================ una cuenta ============================
class Cuenta:
    def __init__(self, gestor, d, estado=None):
        self.g = gestor
        self.id = d["id"]
        self.nombre = d.get("nombre") or self.id
        self.clave = d.get("clave", "")
        self.cuenta_mfp = d.get("cuenta_mfp", "")
        self.activa = bool(d.get("activa", True))
        self.mult = float(d.get("mult", 1.0) or 1.0)
        self.bloqueo_cfg, _ = validar_bloqueo(d.get("bloqueo"))
        self.creada = d.get("creada") or now_ms()
        s = estado or {}
        self.bloqueo = s.get("bloqueo")          # {"desde","hasta","motivo","origen","estricto"} o None
        self.dia = s.get("dia")                  # {"key","base","ops","ignorar"}
        self.ultimo = s.get("ultimo")            # último bloqueo, con cuándo y cómo terminó
        self.pend = s.get("pend")                # órdenes por cancelar tras un bloqueo: {"desde","n","prox"}
        self.lock = threading.RLock()
        self.trader, self.err, self.account_id = None, None, None
        self.snap, self.snap_t, self.snap_err = None, None, None
        self.pol, self.pol_t = None, 0
        self.flot, self.flot_t = 0.0, None      # resultado flotante de las posiciones de los bots
        self.next_resolve = 0
        self._cliente()

    # ---------- disco ----------
    def dump(self):
        return {"id": self.id, "nombre": self.nombre, "clave": self.clave, "cuenta_mfp": self.cuenta_mfp,
                "activa": self.activa, "mult": self.mult, "bloqueo": self.bloqueo_cfg, "creada": self.creada}

    def estado(self):
        return {"bloqueo": self.bloqueo, "dia": self.dia, "account_id": self.account_id, "ultimo": self.ultimo,
                "pend": self.pend}

    # ---------- conexión ----------
    def _cliente(self):
        self.trader, self.err = None, None
        if not self.clave:
            self.err = "falta la clave de API"
            return
        try:
            self.trader = E.MFPTrading(self.g.host, self.clave, self.g.timeout, "kcbot-pi/" + self.g.version)
        except ValueError as e:
            self.err = str(e)

    def resolver(self, force=False):
        """ID de la cuenta en MyFundedPerps: el indicado o la primera cuenta activa que ve la clave."""
        if self.account_id and not force:
            return self.account_id
        if not self.trader:
            return None
        if time.time() < self.next_resolve and not force:
            return None
        self.next_resolve = time.time() + 60
        try:
            if self.cuenta_mfp:
                a = self.trader.account(self.cuenta_mfp)
                acc = a.get("id") or self.cuenta_mfp
            else:
                act = [a for a in self.trader.accounts() if a.get("status") == "active"]
                if not act:
                    raise RuntimeError("la clave no ve ninguna cuenta sandbox activa")
                acc = act[0]["id"]
            dup = self.g.duplicada(self, acc)
            if dup:
                raise RuntimeError("«{}» ya usa esa misma cuenta de MyFundedPerps".format(dup.nombre))
            self.account_id, self.err = acc, None
            return acc
        except E.ExecError as e:
            self.err = "No se puede leer la cuenta: {}".format(e) + (
                " (el acceso al sandbox ha caducado)" if e.code == "sandbox_access_expired" else "")
        except Exception as e:
            self.err = str(e)
        return None

    def listo(self):
        """Puede recibir órdenes: interruptor general, cuenta activa, clave válida y cuenta resuelta."""
        return bool(self.g.ordenes and self.activa and self.trader and (self.account_id or self.resolver()))

    def problema(self):
        if not self.g.ordenes:
            return "el envío de órdenes está desactivado (Cuentas → Enviar órdenes)"
        if not self.activa:
            return "la cuenta «{}» está desactivada".format(self.nombre)
        if self.err:
            return "«{}»: {}".format(self.nombre, self.err)
        if not self.account_id:
            return "aún no se ha leído la cuenta «{}»".format(self.nombre)
        return None

    # ---------- bloqueo ----------
    def bloqueada(self, t=None):
        """(True, motivo) si la cuenta no admite operaciones nuevas."""
        with self.lock:
            b = self.bloqueo
            if not b:
                return False, ""
            if b.get("hasta") and (t or now_ms()) >= b["hasta"]:
                return False, ""
            return True, b.get("motivo") or "cuenta bloqueada"

    def texto_hasta(self):
        b = self.bloqueo or {}
        if not b.get("hasta"):
            return "hasta que la desbloquees"
        d = datetime.fromtimestamp(b["hasta"] / 1000, self.g.tz_local)
        return "hasta el {} a las {}".format(d.strftime("%d/%m"), d.strftime("%H:%M"))

    # ---------- día ----------
    def _dia(self, a=None):
        k = dia_ny()
        if not self.dia or self.dia.get("key") != k:
            base = None
            if a:
                r = a.get("risk") or {}
                base = r.get("equity") if r.get("equity") is not None else a.get("balance")
            self.dia = {"key": k, "base": base, "ops": 0, "ignorar": False, "bots_real": 0.0}
            return True
        if self.dia.get("base") is None and a:
            r = a.get("risk") or {}
            self.dia["base"] = r.get("equity") if r.get("equity") is not None else a.get("balance")
            return True
        return False

    def valores(self):
        """Resultado del día y daily loss consumido, con la última lectura de la cuenta."""
        medir = self.bloqueo_cfg.get("medir", "cuenta")
        d = self.dia or {}
        out = {"pnl_dia": None, "pnl_cuenta": None, "dll_usado_pct": None, "ops": d.get("ops", 0), "medir": medir,
               "pnl_bots": round(float(d.get("bots_real") or 0) + float(self.flot or 0), 2),
               "bots_real": round(float(d.get("bots_real") or 0), 2), "bots_flot": round(float(self.flot or 0), 2)}
        if medir == "bots":
            out["pnl_dia"] = out["pnl_bots"]
        a = self.snap
        if not a:
            return out
        r = a.get("risk") or {}
        eq = r.get("equity")
        base = d.get("base")
        if eq is not None and base is not None:
            out["pnl_cuenta"] = round(float(eq) - float(base), 2)
            if medir == "cuenta":
                out["pnl_dia"] = out["pnl_cuenta"]
        pct = (r.get("requirements") or {}).get("daily_loss_pct")
        room = r.get("daily_loss_room")
        if pct and room is not None and a.get("starting_balance"):
            lim = float(a["starting_balance"]) * (pct / 100 if pct > 1 else pct)
            if lim > 0:
                out["dll_usado_pct"] = round(max(0.0, min(100.0, (1 - float(room) / lim) * 100)), 1)
                out["dll_limite"] = round(lim, 2)
                out["dll_queda"] = round(float(room), 2)
        return out

    def public(self, bots):
        v = self.valores()
        b, _ = self.bloqueada()
        a = self.snap or {}
        r = a.get("risk") or {}
        return {"id": self.id, "nombre": self.nombre, "clave": mascara(self.clave), "tiene_clave": bool(self.clave),
                "cuenta_mfp": self.cuenta_mfp, "account_id": self.account_id, "activa": self.activa,
                "mult": self.mult, "bloqueo_cfg": dict(self.bloqueo_cfg), "listo": self.listo(),
                "problema": self.problema(), "error": self.err, "snap_err": self.snap_err, "snap_t": self.snap_t,
                "cuenta": {"nombre": a.get("name"), "numero": a.get("account_number"), "estado": a.get("status"),
                           "fase": a.get("stage"), "saldo": a.get("balance"), "inicial": a.get("starting_balance"),
                           "equity": r.get("equity"), "no_realizado": r.get("unrealized_pnl"),
                           "dd_queda": r.get("max_drawdown_room")} if a else None,
                "dia": v, "bloqueo": self.bloqueo if b else None, "bloqueo_texto": self.texto_hasta() if b else "",
                "ultimo_bloqueo": self.ultimo, "ordenes_pendientes": bool(self.pend), "bots": bots}


# ============================ el gestor ============================
class Gestor:
    def __init__(self, hub, cfg, version):
        self.hub, self.c, self.version = hub, cfg, version
        self.host, self.timeout = cfg.host, cfg.http_timeout
        self.tz_local = hub.tz
        self.lock = threading.RLock()
        self.cuentas = {}
        self.orden = []
        self.ordenes = False
        self.migrada = None
        self._cargar()

    # ---------- disco ----------
    def _cargar(self):
        try:
            with open(FILE, encoding="utf-8") as f:
                d = json.load(f)
        except (OSError, ValueError):
            d = None
        if d is None:
            d = self._migrar()
        self.ordenes = bool(d.get("ordenes"))
        est = d.get("estado") or {}
        for x in d.get("cuentas") or []:
            if ID_RE.match(str(x.get("id", ""))):
                c = Cuenta(self, x, est.get(x["id"]))
                self.cuentas[c.id] = c
                self.orden.append(c.id)
        self.guardar()

    def _migrar(self):
        """Primera vez: crea la cuenta «Principal» con FP_MFP_KEY / FP_MFP_ACCOUNT y toma FP_EXEC."""
        d = {"ordenes": bool(self.c.exec), "cuentas": [], "estado": {}}
        if self.c.key:
            d["cuentas"].append({"id": "c1", "nombre": "Principal", "clave": self.c.key, "cuenta_mfp": self.c.account,
                                 "activa": True, "mult": 1.0, "bloqueo": dict(BLOQUEO_DEF), "creada": now_ms()})
            self.migrada = "c1"
        return d

    def guardar(self):
        with self.lock:
            d = {"ordenes": self.ordenes, "cuentas": [self.cuentas[i].dump() for i in self.orden],
                 "estado": {i: self.cuentas[i].estado() for i in self.orden}}
            tmp = FILE + ".tmp"
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(d, f, indent=1, ensure_ascii=False)
            os.replace(tmp, FILE)
            try:
                os.chmod(FILE, 0o600)
            except OSError:
                pass

    # ---------- consultas ----------
    def get(self, cid):
        return self.cuentas.get(cid)

    def lista(self):
        return [self.cuentas[i] for i in self.orden if i in self.cuentas]

    def por_defecto(self):
        """La que se usa si un bot en modo sandbox de antes de la 6.3 no tiene cuentas elegidas."""
        ls = self.lista()
        return ls[0].id if ls else None

    def para_bot(self, P):
        """Cuentas en las que debe operar un bot con estos ajustes."""
        if P.get("ex_modo") != "sandbox":
            return []
        ids = [i for i in (P.get("ex_cuentas") or []) if i in self.cuentas]
        if not ids and not P.get("ex_cuentas") and self.por_defecto():
            ids = [self.por_defecto()]
        return ids

    def duplicada(self, cta, acc):
        for c in self.lista():
            if c is not cta and c.account_id == acc:
                return c
        return None

    def nombre(self, cid):
        c = self.cuentas.get(cid)
        return c.nombre if c else cid

    def listo_alguna(self):
        return any(c.listo() for c in self.lista())

    def problema(self):
        if not self.ordenes:
            return "el envío de órdenes está desactivado (Cuentas → Enviar órdenes)"
        if not self.cuentas:
            return "no hay ninguna cuenta configurada (menú Cuentas)"
        return None

    def bots_de(self, cid, incluir_archivados=True):
        out = []
        for b in list(self.hub.bots.values()):
            if b.meta.get("archived") and not incluir_archivados:
                continue
            if cid in self.para_bot(b.P) or cid in b.ex.cuentas_con_posicion():
                out.append(b)
        return out

    def public(self):
        out = []
        for c in self.lista():
            bots = [{"id": b.id, "nombre": b.name, "archivado": bool(b.meta.get("archived")),
                     "posicion": bool(b.ex.pos_en(c.id))} for b in self.bots_de(c.id)]
            out.append(c.public(bots))
        return {"ordenes": self.ordenes, "cuentas": out, "max": MAX_CUENTAS, "por_defecto": self.por_defecto(),
                "dia": dia_ny(), "proxima_medianoche": medianoche_ny(), "problema": self.problema()}

    # ---------- cambios desde el panel ----------
    def set_ordenes(self, on):
        with self.lock:
            self.ordenes = bool(on)
            self.guardar()
        self.hub.log("Envío de órdenes {} desde el panel.".format("ACTIVADO" if on else "desactivado"), "warn")
        self._avisar("ejecucion", "{} <b>Envío de órdenes {}</b>".format("▶️" if on else "⏸",
                                                                         "activado" if on else "desactivado"))

    def guardar_cuenta(self, raw):
        """Crea (sin id) o actualiza una cuenta. Devuelve (cuenta, errores)."""
        raw = raw or {}
        errs = []
        with self.lock:
            cid = str(raw.get("id") or "")
            cur = self.cuentas.get(cid) if cid else None
            if cid and not cur:
                return None, ["No existe esa cuenta"]
            if not cur and len(self.cuentas) >= MAX_CUENTAS:
                return None, ["Máximo {} cuentas".format(MAX_CUENTAS)]
            nombre = str(raw.get("nombre", cur.nombre if cur else "")).strip()[:40]
            if not nombre:
                errs.append("Ponle un nombre a la cuenta")
            elif any(c.nombre.lower() == nombre.lower() and c is not cur for c in self.lista()):
                errs.append("Ya hay una cuenta con ese nombre")
            clave = str(raw.get("clave") or "").strip()
            if not clave:
                clave = cur.clave if cur else ""
            if not clave:
                errs.append("Pega la clave de API (fp_test_…)")
            elif not clave.startswith("fp_test_"):
                errs.append("La clave tiene que ser de sandbox (fp_test_). Por seguridad no se aceptan claves fp_live_.")
            elif not re.match(r"^fp_test_[A-Za-z0-9_\-]{8,200}$", clave):
                errs.append("La clave no tiene el formato de una clave de MyFundedPerps")
            mfp = str(raw.get("cuenta_mfp", cur.cuenta_mfp if cur else "") or "").strip()
            if not MFP_ID_RE.match(mfp):
                errs.append("ID de cuenta de MyFundedPerps no válido")
            for c in self.lista():
                if c is cur:
                    continue
                if mfp and c.cuenta_mfp == mfp:
                    errs.append("La cuenta «{}» ya usa ese ID de MyFundedPerps".format(c.nombre))
                elif not mfp and not c.cuenta_mfp and c.clave == clave:
                    errs.append("«{}» ya usa esa clave con «la primera cuenta activa»: elige aquí otra cuenta "
                                "de MyFundedPerps".format(c.nombre))
            try:
                mult = float(raw.get("mult", cur.mult if cur else 1.0) or 1.0)
                if not 0.1 <= mult <= 5:
                    raise ValueError
            except (TypeError, ValueError):
                errs.append("Multiplicador de riesgo: entre 0,1 y 5")
                mult = 1.0
            blq, e2 = validar_bloqueo(raw.get("bloqueo"), cur.bloqueo_cfg if cur else None)
            errs += e2
            if errs:
                return None, errs
            activa = bool(raw.get("activa", cur.activa if cur else True))
            if cur:
                cambia_conexion = clave != cur.clave or mfp != cur.cuenta_mfp
                if cambia_conexion and any(b.ex.pos_en(cur.id) for b in self.bots_de(cur.id)):
                    return None, ["Hay bots con posiciones abiertas en esta cuenta: ciérralas antes de cambiar "
                                  "la clave o la cuenta de MyFundedPerps"]
                cur.nombre, cur.mult, cur.bloqueo_cfg, cur.activa = nombre, mult, blq, activa
                if cambia_conexion:
                    cur.clave, cur.cuenta_mfp = clave, mfp
                    cur.account_id, cur.snap, cur.pol, cur.next_resolve = None, None, None, 0
                    cur._cliente()
                c = cur
            else:
                n = 1
                while "c{}".format(n) in self.cuentas:
                    n += 1
                c = Cuenta(self, {"id": "c{}".format(n), "nombre": nombre, "clave": clave, "cuenta_mfp": mfp,
                                  "activa": activa, "mult": mult, "bloqueo": blq, "creada": now_ms()})
                self.cuentas[c.id] = c
                self.orden.append(c.id)
            self.guardar()
        self.hub.log("Cuenta «{}» guardada ({}).".format(c.nombre, mascara(c.clave)))
        threading.Thread(target=self.leer, args=(c,), daemon=True).start()
        return c, []

    def borrar(self, cid):
        with self.lock:
            c = self.cuentas.get(cid)
            if not c:
                raise ValueError("No existe esa cuenta")
            usan = self.bots_de(cid)
            if usan:
                raise ValueError("La usan estos bots: {}. Quítala de su configuración (Ejecución → Cuentas) "
                                 "antes de borrarla.".format(", ".join(b.name for b in usan)))
            del self.cuentas[cid]
            self.orden.remove(cid)
            self.guardar()
        self.hub.log("Cuenta «{}» borrada del programa (en MyFundedPerps no cambia nada).".format(c.nombre), "warn")

    def cuentas_visibles(self, clave=None, cid=None):
        """Las cuentas sandbox que ve una clave (para elegirla en el panel antes de guardar)."""
        if not clave and cid and cid in self.cuentas:
            clave = self.cuentas[cid].clave
        clave = (clave or "").strip()
        if not clave.startswith("fp_test_"):
            raise ValueError("Pega una clave de sandbox (fp_test_…)")
        tr = E.MFPTrading(self.host, clave, self.timeout, "kcbot-pi/" + self.version)
        try:
            accs = tr.accounts()
        except E.ExecError as e:
            raise ValueError("La clave no funciona: {}".format(e.msg or e))
        return [{"id": a.get("id"), "nombre": a.get("name"), "numero": a.get("account_number"),
                 "estado": a.get("status"), "saldo": a.get("balance"), "inicial": a.get("starting_balance"),
                 "fase": a.get("stage")} for a in accs]

    # ---------- bloqueo ----------
    def bloquear(self, cid, motivo, hasta=None, origen="manual", cerrar=None, estricto=None):
        """hasta: ms, «mañana» o None (sin fecha: hasta desbloquear a mano)."""
        c = self.cuentas.get(cid)
        if not c:
            raise ValueError("No existe esa cuenta")
        if hasta == "mañana":
            hasta = medianoche_ny()
        cfg = c.bloqueo_cfg
        cerrar = cierre_de(cfg["cerrar"] if cerrar is None else cerrar) or "nada"
        estricto = cfg["estricto"] if estricto is None else bool(estricto)
        with c.lock:
            ya, _ = c.bloqueada()
            prev = c.bloqueo if ya else None
            if prev and prev.get("hasta") and hasta and hasta < prev["hasta"]:
                hasta = prev["hasta"]               # un bloqueo nuevo nunca acorta el que ya hay
            if prev and not prev.get("hasta"):
                hasta = None
            c.bloqueo = {"desde": now_ms(), "hasta": hasta, "motivo": motivo[:200], "origen": origen,
                         "estricto": bool(estricto or (prev or {}).get("estricto"))}
            c.ultimo = dict(c.bloqueo, pnl_dia=c.valores().get("pnl_dia"), cierre=None, fin=None, fin_como=None)
            self.guardar()
        texto = "🔒 <b>Cuenta «{}» bloqueada</b> {}\n{}".format(_esc(c.nombre), _esc(c.texto_hasta()), _esc(motivo))
        self.hub.log("Cuenta «{}» bloqueada {}: {}.".format(c.nombre, c.texto_hasta(), motivo), "warn")
        cerrado = ""
        if cerrar in ("todo", "bots") and origen != "web":
            cerrado = self._cerrar_todo(c) if cerrar == "todo" else self._cerrar_bots(c)
            texto += "\n" + _esc(cerrado)
            with c.lock:
                if c.ultimo:
                    c.ultimo["cierre"] = cerrado[:300]
                self.guardar()
        self._avisar("limites", texto + "\nLos bots no abrirán operaciones nuevas en ella. Para bloquear también el "
                     "trading manual, usa Trader Lockout en la web de MyFundedPerps.")
        return cerrado

    def desbloquear(self, cid, forzar=False):
        c = self.cuentas.get(cid)
        if not c:
            raise ValueError("No existe esa cuenta")
        with c.lock:
            b, _ = c.bloqueada()
            if not b:
                c.bloqueo = None
                self.guardar()
                return "La cuenta no estaba bloqueada."
            if c.bloqueo.get("origen") == "web":
                raise ValueError("Es un Trader Lockout de MyFundedPerps: ni la API ni el soporte lo pueden quitar "
                                 "antes de tiempo. Seguirá bloqueada {}.".format(c.texto_hasta()))
            if c.bloqueo.get("estricto") and not forzar:
                raise ValueError("Bloqueo estricto: no se puede quitar antes de tiempo. Seguirá bloqueada {}.".format(
                    c.texto_hasta()))
            c.bloqueo = None
            self._fin(c, "manual")
            if c.dia and c.dia.get("key") == dia_ny():
                c.dia["ignorar"] = True              # los límites automáticos no vuelven a saltar hoy
            self.guardar()
        self.hub.log("Cuenta «{}» desbloqueada a mano.".format(c.nombre), "warn")
        self._avisar("limites", "🔓 <b>Cuenta «{}» desbloqueada</b> a mano. Los límites automáticos no vuelven a "
                     "bloquearla hasta mañana.".format(_esc(c.nombre)))
        return "Cuenta desbloqueada."

    def lockout_web(self, cid, expira_ms):
        """MyFundedPerps rechazó una orden por Trader Lockout (activado en la web)."""
        c = self.cuentas.get(cid)
        if not c:
            return
        b, _ = c.bloqueada()
        if b and (c.bloqueo or {}).get("origen") == "web" and c.bloqueo.get("hasta") == expira_ms:
            return
        self.bloquear(cid, "Trader Lockout activado en la web de MyFundedPerps", hasta=expira_ms or medianoche_ny(),
                      origen="web", cerrar=False, estricto=True)

    # ---------- lo de los bots ----------
    def _ejecutores(self, c):
        """(bot, ejecutor) de cada bot con una posición real abierta en la cuenta."""
        out = []
        for b in list(self.hub.bots.values()):
            x = getattr(b.ex, "ex", {}).get(c.id)
            if x and x.pos:
                out.append((b, x))
        return out

    def resultado_bot(self, cid, pnl):
        """Un bot cerró una operación real en la cuenta: suma al resultado del día de los bots."""
        c = self.cuentas.get(cid)
        if not c:
            return
        with c.lock:
            c._dia(c.snap)
            c.dia["bots_real"] = round(float(c.dia.get("bots_real") or 0) + float(pnl or 0), 2)
            self.guardar()

    def _flotante(self, c):
        """Resultado sin cerrar de las posiciones de los bots en la cuenta (las tuyas a mano no cuentan)."""
        xs = self._ejecutores(c)
        if not xs:
            c.flot, c.flot_t = 0.0, now_ms()
            return
        try:
            pos = {p.get("id"): p for p in (c.trader.data(c.trader.get("/v1/positions", account_id=c.account_id))
                                            or [])}
        except E.ExecError:
            return                                   # se conserva el último valor
        tot = 0.0
        for b, x in xs:
            p = pos.get(x.pos.get("id"))
            if p is None:
                continue                             # ya cerrada: entrará en bots_real al registrarse
            u = next((p[k] for k in ("unrealized_pnl", "unrealised_pnl", "upnl", "pnl") if p.get(k) is not None),
                     None)
            if u is None:
                try:
                    px = p.get("mark_price") or b.live_price()[0]
                    u = (float(px) - float(x.pos["entry"])) * float(x.pos["size"]) * (1 if x.pos["side"] == 1 else -1)
                except Exception:
                    u = 0.0
            tot += float(u)
        c.flot, c.flot_t = round(tot, 2), now_ms()

    def _cerrar_bots(self, c):
        """Cierra solo las posiciones que abrieron los bots en la cuenta, una a una. Lo abierto a mano no se toca."""
        xs = self._ejecutores(c)
        if not xs:
            txt = "Los bots no tenían posiciones abiertas en la cuenta. Lo que tengas abierto a mano sigue igual."
            self.hub.log("Cuenta «{}»: {}".format(c.nombre, txt), "warn")
            return txt
        ok, mal = [], []
        for i, (b, x) in enumerate(xs):
            if i:
                time.sleep(PAUSA_S)
            err0 = x.last_error
            with b.lock:
                x.cerrar_todo("bloqueo de la cuenta")
            if x.pos is None or (x.pos or {}).get("closing"):
                ok.append(b.name)
            else:
                e = x.last_error if x.last_error is not err0 else None
                mal.append("{}{}".format(b.name, " ({})".format(e["msg"]) if e else ""))
        partes = []
        if ok:
            partes.append("Cerradas las posiciones de los bots: {}".format(", ".join(ok)))
        if mal:
            partes.append("⚠️ No se pudieron cerrar: {}. Siguen con su stop y su objetivo".format("; ".join(mal)))
        partes.append("Lo que tengas abierto a mano sigue igual")
        txt = ". ".join(partes) + "."
        self.hub.log("Cuenta «{}»: {}".format(c.nombre, txt), "warn")
        return txt

    def _cerrar_todo(self, c):
        """Cierra todas las posiciones y cancela todas las órdenes de la cuenta (como Close All + Cancel All).

        Entre una petición y otra hay una pausa (el cortafuegos de MyFundedPerps corta las ráfagas) y al final se
        comprueba leyendo las órdenes vivas. Si queda alguna, se reintenta cada minuto mientras dure el bloqueo."""
        if not (c.trader and c.account_id):
            return "No se pudo cerrar nada: la cuenta no está conectada."
        partes = []
        clave = "kq-blq-{}-{}".format(c.id, int((c.bloqueo or {}).get("desde") or now_ms()))
        cancel_ok = False
        for qué, fn in (("posiciones", c.trader.close_all), ("órdenes", c.trader.cancel_all)):
            if qué == "órdenes":
                time.sleep(PAUSA_S)
            try:
                op = {}
                for _ in range(12):
                    op = fn(c.account_id, "{}-{}".format(clave, qué[0])) or {}
                    if op.get("status") != "running":
                        break
                    time.sleep(3)
                ok, mal = op.get("succeeded_count", 0), op.get("failed_count", 0)
                partes.append("{} {}: {}{}".format("Cerradas" if qué == "posiciones" else "Canceladas", qué, ok,
                                                   " ({} con error)".format(mal) if mal else ""))
                if qué == "órdenes":
                    cancel_ok = not mal
            except E.ExecError as e:
                partes.append("No se pudieron {} las {} ({})".format("cerrar" if qué == "posiciones" else "cancelar",
                                                                     qué, e))
        for b in list(self.hub.bots.values()):          # los bots anotan el cierre con este motivo
            b.ex.marcar_cierre(c.id, "bloqueo de la cuenta")
        # comprobación: ¿quedan órdenes vivas?
        time.sleep(PAUSA_S)
        quedan = self._vivas(c)
        if quedan == 0:
            if not cancel_ok:
                partes.append("Comprobado: no queda ninguna orden viva")
        elif quedan is None and cancel_ok:
            pass                                          # cancelación correcta; la lectura de control falló
        else:
            with c.lock:
                c.pend = {"desde": now_ms(), "n": 0, "prox": now_ms() + REINTENTO_MS}
                self.guardar()
            partes.append("⚠️ {} sin cancelar: se reintenta cada minuto mientras dure el bloqueo".format(
                "No se pudo comprobar si quedan órdenes" if quedan is None else
                "Queda {} orden viva".format(quedan) if quedan == 1 else "Quedan {} órdenes vivas".format(quedan)))
        txt = ". ".join(partes) + "."
        self.hub.log("Cuenta «{}»: {}".format(c.nombre, txt), "warn")
        return txt

    @staticmethod
    def _vivas(c):
        """Número de órdenes vivas en la cuenta, o None si no se pudo leer."""
        try:
            return len(c.trader.working_orders(c.account_id))
        except Exception:
            return None

    def _reintentar_cancelar(self, c):
        """Tras un bloqueo con órdenes sin cancelar: un intento más (llamado desde el bucle)."""
        p = c.pend
        if not p or now_ms() < p.get("prox", 0):
            return
        if not c.bloqueada()[0]:
            # Sin bloqueo ya no se cancela nada solo: podría llevarse órdenes nuevas tuyas o de los bots.
            with c.lock:
                c.pend = None
                self.guardar()
            quedan = self._vivas(c) if c.trader and c.account_id else None
            if quedan != 0:
                self.hub.log("Cuenta «{}»: termina el bloqueo sin haber podido cancelar las órdenes.".format(
                    c.nombre), "warn")
                self._avisar("limites", "⚠️ <b>Cuenta «{}»</b>: terminó el bloqueo sin poder cancelar las órdenes "
                             "pendientes. Revísalas en la web de MyFundedPerps.".format(_esc(c.nombre)))
            return
        n = p.get("n", 0) + 1
        err = ""
        try:
            c.trader.cancel_all(c.account_id, "kq-blq-{}-{}-r{}".format(c.id, p.get("desde", 0), n))
            time.sleep(PAUSA_S)
        except E.ExecError as e:
            err = str(e)
        quedan = self._vivas(c)
        with c.lock:
            if quedan == 0:
                c.pend = None
            elif n >= MAX_REINTENTOS:
                c.pend = None
            else:
                c.pend = {"desde": p.get("desde", 0), "n": n, "prox": now_ms() + REINTENTO_MS * min(n, 5)}
            self.guardar()
        if quedan == 0:
            self.hub.log("Cuenta «{}»: órdenes canceladas en el reintento {}.".format(c.nombre, n))
            self._avisar("limites", "✅ <b>Cuenta «{}»</b>: canceladas las órdenes pendientes del bloqueo "
                         "(reintento {}).".format(_esc(c.nombre), n))
        elif n >= MAX_REINTENTOS:
            self.hub.log("Cuenta «{}»: no se pudieron cancelar las órdenes tras {} reintentos{}.".format(
                c.nombre, n, ": " + err if err else ""), "error")
            self._avisar("limites", "⚠️ <b>Cuenta «{}»</b>: no se han podido cancelar las órdenes pendientes tras {} "
                         "reintentos. Cancélalas en la web de MyFundedPerps.{}".format(
                             _esc(c.nombre), n, "\n" + _esc(err) if err else ""))
        else:
            self.hub.log("Cuenta «{}»: reintento {} de cancelar órdenes sin éxito{}.".format(
                c.nombre, n, ": " + err if err else ""), "warn")

    def _fin(self, c, como):
        """Anota cómo terminó el bloqueo (se llama con c.lock tomado)."""
        if c.ultimo and not c.ultimo.get("fin"):
            c.ultimo["fin"], c.ultimo["fin_como"] = now_ms(), como

    # ---------- lectura periódica y límites automáticos ----------
    def leer(self, c):
        if not c.trader:
            return
        acc = c.resolver()
        if not acc:
            c.snap_err = c.err
            return
        try:
            a = c.trader.account(acc)
            c.snap, c.snap_t, c.snap_err = a, now_ms(), None
            self._flotante(c)
            with c.lock:
                if c._dia(a):
                    self.guardar()
            if c.activa:
                self._revisar(c)
        except E.ExecError as e:
            c.snap_err = str(e) + (" (el acceso al sandbox ha caducado)" if e.code == "sandbox_access_expired" else "")
        except Exception as e:
            c.snap_err = str(e)

    def _revisar(self, c):
        cfg = c.bloqueo_cfg
        if not cfg["auto"] or (c.dia or {}).get("ignorar"):
            return
        if c.bloqueada()[0]:
            return
        v = c.valores()
        motivo = None
        de = " de los bots" if v["medir"] == "bots" else ""
        if cfg["perdida_usd"] and v["pnl_dia"] is not None and v["pnl_dia"] <= -cfg["perdida_usd"]:
            motivo = "pérdida del día{} de {:+.2f} $ (límite {:g} $)".format(de, v["pnl_dia"], cfg["perdida_usd"])
        elif cfg["ganancia_usd"] and v["pnl_dia"] is not None and v["pnl_dia"] >= cfg["ganancia_usd"]:
            motivo = "ganancia del día{} de {:+.2f} $ (objetivo {:g} $)".format(de, v["pnl_dia"], cfg["ganancia_usd"])
        elif cfg["dll_pct"] and v["dll_usado_pct"] is not None and v["dll_usado_pct"] >= cfg["dll_pct"]:
            motivo = "consumido el {:.0f} % del daily loss (límite {:g} %)".format(v["dll_usado_pct"], cfg["dll_pct"])
        if motivo:
            hasta = medianoche_ny() if cfg["duracion"] == "mañana" else now_ms() + int(cfg["horas"] * 3_600_000)
            self.bloquear(c.id, "Límite automático: " + motivo, hasta=hasta, origen="auto")

    def contar_apertura(self, cid):
        """Un bot abrió una operación real en la cuenta: cuenta para «operaciones al día»."""
        c = self.cuentas.get(cid)
        if not c:
            return
        with c.lock:
            c._dia(c.snap)
            c.dia["ops"] = c.dia.get("ops", 0) + 1
            n = c.dia["ops"]
            self.guardar()
        cfg = c.bloqueo_cfg
        if cfg["auto"] and cfg["max_ops"] and n >= cfg["max_ops"] and not c.dia.get("ignorar"):
            hasta = medianoche_ny() if cfg["duracion"] == "mañana" else now_ms() + int(cfg["horas"] * 3_600_000)
            # la operación recién abierta sigue: el bloqueo solo impide las siguientes
            self.bloquear(cid, "Límite automático: {} {} hoy (máximo {})".format(
                n, "operación" if n == 1 else "operaciones", cfg["max_ops"]), hasta=hasta, origen="auto", cerrar=False)

    def bucle(self):
        while True:
            for c in self.lista():
                try:
                    b = c.bloqueo
                    if b and b.get("hasta") and now_ms() >= b["hasta"]:
                        with c.lock:
                            c.bloqueo = None
                            self._fin(c, "vencido")
                            self.guardar()
                        self.hub.log("Cuenta «{}»: termina el bloqueo.".format(c.nombre))
                        self._avisar("limites", "🔓 <b>Cuenta «{}»</b>: termina el bloqueo, los bots vuelven a "
                                     "operar en ella.".format(_esc(c.nombre)))
                    if c.pend:
                        self._reintentar_cancelar(c)
                    self.leer(c)
                except Exception as e:
                    self.hub.log("Cuenta «{}»: {}".format(c.nombre, e), "error")
            time.sleep(max(10, self.c.account_poll))

    def _avisar(self, kind, texto):
        av = getattr(self.hub, "avisos", None)
        if av and av.wants(kind):
            av.push(kind, texto)


def _esc(s):
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
