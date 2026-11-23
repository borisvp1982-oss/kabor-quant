# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Borislav Vladimirov Petrakiev
"""KaBor Quant 7.3 — diario de operaciones.

Junta en una lista las operaciones de todos los bots (también las de bots archivados) y las que el usuario apunta a
mano, como un swing manual. A cada una se le puede poner una nota, etiquetas y si siguió el plan o no. Las
operaciones de los bots no se copian: se leen de cada bot y aquí solo se guarda lo que añade el usuario, por bot y
número de operación. Archivo: diario.json en la carpeta de datos.
"""
import json
import os
import re
import secrets
import threading
import time

DIA = 86_400_000
MAX_NOTA = 2000
MAX_ETIQ = 8
ETIQ_RE = re.compile(r"^[\w áéíóúüñÁÉÍÓÚÜÑ+\-.#/]{1,24}$")


def _etiquetas(v):
    out = []
    for e in (v or [])[:MAX_ETIQ]:
        e = re.sub(r"\s+", " ", str(e)).strip().lower()
        if e and ETIQ_RE.match(e) and e not in out:
            out.append(e)
    return out


def _plan(v):
    return v if v in ("si", "no") else None


def _num(v, nombre, lo=-1e9, hi=1e9):
    if v in (None, ""):
        return None
    try:
        x = float(str(v).replace(",", "."))
    except ValueError:
        raise ValueError("«{}» tiene que ser un número".format(nombre))
    if not lo <= x <= hi:
        raise ValueError("«{}» fuera de rango".format(nombre))
    return round(x, 4)


class Diario:
    def __init__(self, hub, path="diario.json"):
        self.hub = hub
        self.path = path
        self.lock = threading.Lock()
        self.notas, self.manuales = {}, []
        try:
            with open(path, encoding="utf-8") as f:
                d = json.load(f)
            self.notas = d.get("notas") or {}
            self.manuales = d.get("manuales") or []
        except (OSError, ValueError):
            pass

    def _save(self):
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"notas": self.notas, "manuales": self.manuales}, f, ensure_ascii=False)
        os.replace(tmp, self.path)

    # ---------- lectura ----------
    def listar(self, dias):
        now = int(time.time() * 1000)
        desde = now - int(dias) * DIA if dias and int(dias) > 0 else 0
        ops = []
        for bid, b in list(self.hub.bots.items()):
            with b.lock:
                trades = [dict(x) for x in b.trades if (x.get("t_close") or 0) >= desde]
                riesgo = b.P.get("risk_usd") or 0
                nombre, archivado = b.name, bool(b.meta.get("archived"))
            for x in trades:
                k = "b:{}:{}".format(bid, x.get("n"))
                ops.append({"k": k, "tipo": "bot", "bot_id": bid, "bot": nombre, "archivado": archivado,
                            "simbolo": x.get("symbol"), "tf": x.get("tf"),
                            "lado": "LONG" if x.get("side") in (1, "LONG") else "SHORT",
                            "t_open": x.get("t_open"), "t_close": x.get("t_close"), "r": x.get("r"),
                            "usd": round(x["r"] * riesgo, 2) if riesgo else None, "motivo": x.get("why"),
                            "dur": x.get("dur"), "cfg": x.get("cfg")})
        with self.lock:
            for m in self.manuales:
                if m["t_close"] >= desde:
                    ops.append(dict(m, k="m:" + m["id"], tipo="manual", bot="A mano"))
            notas = dict(self.notas)
        for o in ops:
            n = notas.get(o["k"]) if o["tipo"] == "bot" else o
            o["nota"] = (n or {}).get("nota", "")
            o["etiquetas"] = (n or {}).get("etiquetas", [])
            o["plan"] = (n or {}).get("plan")
        ops.sort(key=lambda o: o["t_close"] or 0, reverse=True)
        freq = {}
        for n in list(notas.values()) + self.manuales:
            for e in n.get("etiquetas") or []:
                freq[e] = freq.get(e, 0) + 1
        return {"ok": True, "ops": ops, "etiquetas": sorted(freq, key=lambda e: (-freq[e], e))}

    # ---------- escritura ----------
    def anotar(self, body):
        k = str(body.get("k") or "")
        nota = str(body.get("nota") or "")[:MAX_NOTA].strip()
        etiq, plan = _etiquetas(body.get("etiquetas")), _plan(body.get("plan"))
        if k.startswith("m:"):
            return self.manual(dict(body, id=k[2:]))
        m = re.fullmatch(r"b:([^:]+):(\d+)", k)
        if not m or m.group(1) not in self.hub.bots:
            return {"ok": False, "errors": ["Esa operación no existe"]}
        with self.lock:
            if nota or etiq or plan:
                self.notas[k] = {"nota": nota, "etiquetas": etiq, "plan": plan, "t": int(time.time() * 1000)}
            else:
                self.notas.pop(k, None)
            self._save()
        return {"ok": True}

    def manual(self, body):
        """Crea (sin id) o cambia (con id) una operación apuntada a mano."""
        try:
            simbolo = re.sub(r"[^A-Za-z0-9._/-]", "", str(body.get("simbolo") or ""))[:20].upper()
            if not simbolo:
                raise ValueError("Escribe el instrumento")
            lado = "LONG" if str(body.get("lado")).upper() in ("LONG", "COMPRA") else "SHORT"
            t_close = int(body.get("t_close") or 0)
            t_open = int(body.get("t_open") or 0) or None
            if not 946_684_800_000 < t_close < int(time.time() * 1000) + DIA:
                raise ValueError("La fecha de cierre no es válida")
            if t_open and t_open > t_close:
                raise ValueError("La apertura no puede ser después del cierre")
            r = _num(body.get("r"), "Resultado en R", -100, 100)
            usd = _num(body.get("usd"), "Resultado en $", -1e7, 1e7)
            if r is None and usd is None:
                raise ValueError("Indica el resultado en R, en $ o en los dos")
        except (TypeError, ValueError) as e:
            return {"ok": False, "errors": [str(e)]}
        reg = {"simbolo": simbolo, "lado": lado, "t_open": t_open, "t_close": t_close, "r": r, "usd": usd,
               "nota": str(body.get("nota") or "")[:MAX_NOTA].strip(), "etiquetas": _etiquetas(body.get("etiquetas")),
               "plan": _plan(body.get("plan")), "t": int(time.time() * 1000)}
        with self.lock:
            mid = str(body.get("id") or "")
            if mid:
                for m in self.manuales:
                    if m["id"] == mid:
                        m.update(reg)
                        break
                else:
                    return {"ok": False, "errors": ["Esa operación ya no existe"]}
            else:
                reg["id"] = mid = secrets.token_hex(6)
                self.manuales.append(reg)
            self._save()
        return {"ok": True, "id": mid}

    def borrar(self, k):
        k = str(k or "")
        with self.lock:
            if k.startswith("m:"):
                n = len(self.manuales)
                self.manuales = [m for m in self.manuales if m["id"] != k[2:]]
                if len(self.manuales) == n:
                    return {"ok": False, "errors": ["Esa operación ya no existe"]}
            else:
                self.notas.pop(k, None)
            self._save()
        return {"ok": True}
