# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Borislav Vladimirov Petrakiev
"""
fiscal.py -- Retiros y gastos de las prop firms, en euros, para la declaración de la renta.

Cada movimiento es un retiro (payout), un gasto (challenge, reseteo, suscripción, formación…) o un reembolso (la
firma devuelve el precio del challenge). Se guarda:
  - el importe en dólares, la comisión y, si la plataforma ya te paga en euros (Rise, Wise…), los euros que recibiste
    de verdad: ese es el importe que vale, y de él sale el tipo de cambio aplicado,
  - si no hay euros recibidos, se convierte con el tipo de referencia del BCE de ese día (o del último día hábil),
  - el tipo de referencia del BCE se guarda siempre como comprobación,
  - los justificantes (PDF o capturas) en data/fiscal/justificantes.

Resumen por año y trimestre, por entidad, exportación a CSV (para Excel o el gestor) e informe imprimible.

Esto organiza tus datos; no es asesoramiento fiscal. Cómo tributan los payouts lo decide tu gestor.
"""
import base64
import csv
import io
import json
import os
import re
import secrets
import threading
import time
import urllib.request
from datetime import date, datetime, timedelta

TIPOS = {"retiro": "Retiro", "gasto": "Gasto", "reembolso": "Reembolso"}
METODOS = ("Rise", "Wise", "Revolut", "Transferencia", "Cripto", "Tarjeta", "PayPal", "Otro")
CATEGORIAS_GASTO = ("Challenge", "Reseteo", "Suscripción", "Datos y plataformas", "Formación", "Equipo", "Otro")
MAX_ADJUNTO = 10 * 1024 * 1024
NOMBRE_RE = re.compile(r"^[\w .()-]{1,100}\.(pdf|png|jpe?g|webp)$", re.I)
BCE_URL = ("https://data-api.ecb.europa.eu/service/data/EXR/D.USD.EUR.SP00.A"
           "?startPeriod={}&endPeriod={}&format=csvdata")


def _f(v, nombre, errs, obligatorio=False, minimo=0.0):
    if v in (None, ""):
        if obligatorio:
            errs.append(nombre + ": falta el importe")
        return None
    try:
        x = float(str(v).replace(",", "."))
        if x != x or x < minimo:
            raise ValueError
        return round(x, 6)
    except ValueError:
        errs.append(nombre + ": importe no válido")
        return None


class Fiscal:
    def __init__(self, folder, log=print):
        self.folder = folder
        self.adj = os.path.join(folder, "justificantes")
        os.makedirs(self.adj, exist_ok=True)
        self.path = os.path.join(folder, "movimientos.json")
        self.bce_path = os.path.join(folder, "cambios_bce.json")
        self.log = log
        self.lock = threading.RLock()
        datos = self._leer(self.path, {})
        self.movs = datos.get("movimientos", [])
        self.entidades = datos.get("entidades", {})        # prop firms y proveedores guardados
        for m in sorted(self.movs, key=lambda x: x["fecha"]):   # las que ya usaste quedan en la lista, con lo
            e = self.entidades.setdefault(m["entidad"], {})        # habitual de cada tipo (lo más reciente)
            if not isinstance(e.get(m["tipo"]), dict):
                e[m["tipo"]] = {}
            if m.get("metodo"):
                e[m["tipo"]]["metodo"] = m["metodo"]
            e[m["tipo"]]["comision_usd"] = m.get("comision_usd") or 0.0
            if m.get("cuenta"):
                e["cuenta"] = m["cuenta"]
        for e in self.entidades.values():                   # formato de la 8.1 (sin método suelto)
            e.pop("metodo", None)
            e.pop("comision_usd", None)
        self.bce = self._leer(self.bce_path, {})

    @staticmethod
    def _leer(p, defecto):
        try:
            with open(p, encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            return defecto

    def _escribir(self, p, obj):
        tmp = p + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(obj, f, indent=1, ensure_ascii=False)
        os.replace(tmp, p)

    def _guardar(self):
        self.movs.sort(key=lambda m: (m["fecha"], m["creado"]))
        self._escribir(self.path, {"movimientos": self.movs, "entidades": self.entidades})

    # ---------- tipo de cambio del BCE ----------
    def cambio_bce(self, dia):
        """Dólares por euro del BCE para ese día (o el último día hábil anterior). None si no se puede obtener."""
        dia = str(dia)[:10]
        with self.lock:
            if dia in self.bce:
                return self.bce[dia]
        d = date.fromisoformat(dia)
        if d > date.today():
            return None
        url = BCE_URL.format((d - timedelta(days=10)).isoformat(), d.isoformat())
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "KaBor-Quant"})
            with urllib.request.urlopen(req, timeout=15) as r:
                texto = r.read().decode("utf-8", "replace")
        except Exception as e:
            self.log("Fiscal: no se pudo leer el cambio del BCE ({})".format(e))
            return None
        filas = list(csv.DictReader(io.StringIO(texto)))
        datos = sorted((f.get("TIME_PERIOD", ""), f.get("OBS_VALUE", "")) for f in filas
                       if f.get("TIME_PERIOD") and f.get("OBS_VALUE"))
        datos = [(t, v) for t, v in datos if t <= dia]
        if not datos:
            return None
        t, v = datos[-1]
        res = {"usd_por_eur": float(v), "fecha_bce": t}
        with self.lock:
            self.bce[dia] = res
            self._escribir(self.bce_path, self.bce)
        return res

    # ---------- movimientos ----------
    def _normalizar(self, d, previo=None):
        errs = []
        tipo = str(d.get("tipo", "")).strip()
        if tipo not in TIPOS:
            errs.append("Tipo: retiro, gasto o reembolso")
        try:
            fecha = date.fromisoformat(str(d.get("fecha", ""))[:10])
            if fecha > date.today() + timedelta(days=1):
                errs.append("La fecha no puede ser futura")
        except ValueError:
            errs.append("Fecha no válida")
            fecha = None
        entidad = str(d.get("entidad", "")).strip()[:80]
        if not entidad:
            errs.append("Falta la entidad (prop firm o proveedor)")
        usd = _f(d.get("usd"), "Importe en dólares", errs)
        com = _f(d.get("comision_usd"), "Comisión", errs) or 0.0
        eur_rec = _f(d.get("eur_recibido"), "Euros", errs)
        if usd is None and eur_rec is None:
            errs.append("Indica el importe en dólares o en euros")
        if usd is not None and com > usd:
            errs.append("La comisión no puede ser mayor que el importe")
        if errs:
            return None, errs
        m = dict(previo or {})
        m.update({"tipo": tipo, "fecha": fecha.isoformat(), "entidad": entidad,
                  "cuenta": str(d.get("cuenta", "")).strip()[:80],
                  "categoria": str(d.get("categoria", "")).strip()[:40] if tipo == "gasto" else "",
                  "metodo": str(d.get("metodo", "")).strip()[:30], "usd": usd, "comision_usd": round(com, 6),
                  "eur_recibido": eur_rec, "nota": str(d.get("nota", "")).strip()[:1000]})
        bce = self.cambio_bce(m["fecha"])
        m["bce"] = bce
        neto_usd = (usd - com) if usd is not None else None
        if eur_rec is not None:
            m["eur"] = round(eur_rec, 2)
            m["fuente_cambio"] = "importe recibido"
            m["tipo_cambio"] = round(neto_usd / eur_rec, 6) if neto_usd and eur_rec else None
        elif bce and neto_usd is not None:
            m["eur"] = round(neto_usd / bce["usd_por_eur"], 2)
            m["fuente_cambio"] = "BCE {}".format(bce["fecha_bce"])
            m["tipo_cambio"] = bce["usd_por_eur"]
        else:
            m["eur"], m["fuente_cambio"], m["tipo_cambio"] = None, "sin cambio: falta el importe en euros", None
        m["comision_eur"] = (round(com / m["tipo_cambio"], 2) if com and m.get("tipo_cambio") else 0.0)
        return m, []

    def guardar(self, d):
        with self.lock:
            mid = d.get("id")
            previo = next((m for m in self.movs if m["id"] == mid), None) if mid else None
            if mid and not previo:
                raise ValueError("Ese movimiento no existe")
        m, errs = self._normalizar(d, previo)
        if errs:
            return None, errs
        with self.lock:
            e = self.entidades.setdefault(m["entidad"], {})
            pt = e.setdefault(m["tipo"], {})                 # lo habitual de cada tipo: el método de cobro de los
            if m.get("metodo"):                              # retiros no es el de pago de los gastos
                pt["metodo"] = m["metodo"]
            pt["comision_usd"] = m.get("comision_usd") or 0.0
            if m["tipo"] == "gasto" and m.get("categoria"):
                pt["categoria"] = m["categoria"]
            if m.get("cuenta"):
                e["cuenta"] = m["cuenta"]
            if previo:
                self.movs = [m if x["id"] == mid else x for x in self.movs]
            else:
                m.update(id="M" + secrets.token_hex(4).upper(), creado=int(time.time() * 1000), justificantes=[])
                self.movs.append(m)
            self._guardar()
        return m, []

    def borrar(self, mid):
        with self.lock:
            m = next((x for x in self.movs if x["id"] == mid), None)
            if not m:
                raise ValueError("Ese movimiento no existe")
            for n in m.get("justificantes", []):
                try:
                    os.remove(os.path.join(self.adj, n))
                except OSError:
                    pass
            self.movs = [x for x in self.movs if x["id"] != mid]
            self._guardar()

    def guardar_entidad(self, nombre, datos=None, borrar=False):
        nombre = str(nombre).strip()[:80]
        if not nombre:
            raise ValueError("Falta el nombre")
        with self.lock:
            if borrar:
                self.entidades.pop(nombre, None)
            else:
                e = self.entidades.setdefault(nombre, {})
                for k in ("metodo", "cuenta"):
                    if datos and k in datos:
                        e[k] = str(datos[k] or "").strip()[:80]
                if datos and "comision_usd" in datos:
                    errs = []
                    e["comision_usd"] = _f(datos["comision_usd"], "Comisión", errs) or 0.0
                    if errs:
                        raise ValueError(errs[0])
            self._guardar()

    def mensual(self, anio):
        """Por mes: retiros (+reembolsos), gastos y neto acumulado del año, en euros."""
        ms = self.del_anio(anio)
        out, acum = [], 0.0
        for mes in range(1, 13):
            xs = [m for m in ms if int(m["fecha"][5:7]) == mes]
            ing = round(sum(m["eur"] or 0 for m in xs if m["tipo"] in ("retiro", "reembolso")), 2)
            gas = round(sum(m["eur"] or 0 for m in xs if m["tipo"] == "gasto"), 2)
            acum = round(acum + ing - gas, 2)
            out.append({"mes": mes, "ingresos": ing, "gastos": gas, "acumulado": acum})
        return out

    # ---------- justificantes ----------
    def adjuntar_parte(self, mid, nombre, parte, total, b64):
        with self.lock:
            m = next((x for x in self.movs if x["id"] == mid), None)
        if not m:
            raise ValueError("Ese movimiento no existe")
        nombre = os.path.basename(str(nombre))
        if not NOMBRE_RE.match(nombre):
            raise ValueError("Justificante: PDF, PNG, JPG o WEBP")
        parte, total = int(parte), int(total)
        if not 0 <= parte < total <= 100:
            raise ValueError("Subida no válida")
        final = "{}_{}".format(mid, nombre)
        tmp = os.path.join(self.adj, final + ".part")
        datos = base64.b64decode(b64)
        if parte == 0:
            open(tmp, "wb").close()
        elif not os.path.exists(tmp):
            raise ValueError("Falta el principio del archivo: vuelve a subirlo")
        if os.path.getsize(tmp) + len(datos) > MAX_ADJUNTO:
            os.remove(tmp)
            raise ValueError("El justificante pasa de 10 MB")
        with open(tmp, "ab") as f:
            f.write(datos)
        if parte == total - 1:
            os.replace(tmp, os.path.join(self.adj, final))
            with self.lock:
                if final not in m.setdefault("justificantes", []):
                    m["justificantes"].append(final)
                self._guardar()
        return m

    def quitar_adjunto(self, mid, nombre):
        with self.lock:
            m = next((x for x in self.movs if x["id"] == mid), None)
            if not m or nombre not in m.get("justificantes", []):
                raise ValueError("Ese justificante no existe")
            m["justificantes"].remove(nombre)
            try:
                os.remove(os.path.join(self.adj, nombre))
            except OSError:
                pass
            self._guardar()

    def ruta_adjunto(self, nombre):
        nombre = os.path.basename(str(nombre))
        with self.lock:
            if not any(nombre in m.get("justificantes", []) for m in self.movs):
                return None
        p = os.path.join(self.adj, nombre)
        return p if os.path.isfile(p) else None

    # ---------- resumen y exportación ----------
    def anios(self):
        with self.lock:
            return sorted({int(m["fecha"][:4]) for m in self.movs} | {date.today().year}, reverse=True)

    def del_anio(self, anio):
        with self.lock:
            return [dict(m) for m in self.movs if m["fecha"].startswith(str(anio))]

    @staticmethod
    def _suma(ms, tipo):
        return round(sum(m["eur"] or 0 for m in ms if m["tipo"] == tipo), 2)

    def resumen(self, anio):
        ms = self.del_anio(anio)

        def bloque(xs):
            ret, gas, ree = self._suma(xs, "retiro"), self._suma(xs, "gasto"), self._suma(xs, "reembolso")
            return {"retiros": ret, "gastos": gas, "reembolsos": ree, "neto": round(ret + ree - gas, 2),
                    "comisiones": round(sum(m.get("comision_eur") or 0 for m in xs), 2), "n": len(xs)}
        trimestres = {"T{}".format(q): bloque([m for m in ms if (int(m["fecha"][5:7]) - 1) // 3 + 1 == q])
                      for q in (1, 2, 3, 4)}
        entidades = {e: bloque([m for m in ms if m["entidad"] == e]) for e in sorted({m["entidad"] for m in ms})}
        pendientes = [m["id"] for m in ms if m["eur"] is None]
        sin_just = [m["id"] for m in ms if not m.get("justificantes")]
        return {"anio": anio, "total": bloque(ms), "trimestres": trimestres, "entidades": entidades,
                "sin_euros": pendientes, "sin_justificante": sin_just}

    def csv(self, anio):
        def n(x, d=2):
            return "" if x is None else ("{:." + str(d) + "f}").format(x).replace(".", ",")
        out = io.StringIO()
        w = csv.writer(out, delimiter=";")
        w.writerow(["Fecha", "Tipo", "Categoría", "Entidad", "Cuenta", "Método", "Importe USD", "Comisión USD",
                    "Neto USD", "Importe EUR", "Comisión EUR", "Tipo de cambio (USD por EUR)", "Origen del cambio",
                    "Cambio BCE de referencia", "Justificantes", "Nota"])
        for m in self.del_anio(anio):
            neto = (m["usd"] - (m["comision_usd"] or 0)) if m.get("usd") is not None else None
            w.writerow([m["fecha"], TIPOS[m["tipo"]], m.get("categoria", ""), m["entidad"], m.get("cuenta", ""),
                        m.get("metodo", ""), n(m.get("usd")), n(m.get("comision_usd")), n(neto), n(m.get("eur")),
                        n(m.get("comision_eur")), n(m.get("tipo_cambio"), 6), m.get("fuente_cambio", ""),
                        n((m.get("bce") or {}).get("usd_por_eur"), 4), len(m.get("justificantes", [])),
                        m.get("nota", "")])
        r = self.resumen(anio)["total"]
        w.writerow([])
        w.writerow(["Total {}".format(anio), "", "", "", "", "", "", "", "", "", "", "", "", "", "", ""])
        for k, t in (("retiros", "Retiros (EUR)"), ("reembolsos", "Reembolsos (EUR)"), ("gastos", "Gastos (EUR)"),
                     ("neto", "Neto (EUR)"), ("comisiones", "Comisiones incluidas (EUR)")):
            w.writerow([t, n(r[k])])
        return "\ufeff" + out.getvalue()

    def informe_html(self, anio, nombre_app="KaBor Quant"):
        """Informe imprimible (el navegador lo guarda como PDF)."""
        esc = lambda s: (str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))
        e = lambda x: "" if x is None else "{:,.2f} €".format(x).replace(",", "X").replace(".", ",").replace("X", ".")
        u = lambda x: "" if x is None else "{:,.2f} $".format(x).replace(",", "X").replace(".", ",").replace("X", ".")
        r = self.resumen(anio)
        filas = "".join(
            "<tr><td>{}</td><td>{}</td><td>{}</td><td>{}</td><td class=n>{}</td><td class=n>{}</td><td class=n>{}</td>"
            "<td>{}</td><td class=c>{}</td></tr>".format(
                m["fecha"], TIPOS[m["tipo"]] + (" · " + esc(m["categoria"]) if m.get("categoria") else ""),
                esc(m["entidad"]) + (" · " + esc(m["cuenta"]) if m.get("cuenta") else ""), esc(m.get("metodo", "")),
                u(m.get("usd")), u(m.get("comision_usd") or None), e(m.get("eur")), esc(m.get("fuente_cambio", "")),
                len(m.get("justificantes", [])))
            for m in self.del_anio(anio))
        tri = "".join("<tr><td>{}</td><td class=n>{}</td><td class=n>{}</td><td class=n>{}</td><td class=n>{}</td>"
                      "</tr>".format(k, e(v["retiros"]), e(v["reembolsos"]), e(v["gastos"]), e(v["neto"]))
                      for k, v in r["trimestres"].items())
        ent = "".join("<tr><td>{}</td><td class=n>{}</td><td class=n>{}</td><td class=n>{}</td><td class=n>{}</td>"
                      "</tr>".format(esc(k), e(v["retiros"]), e(v["reembolsos"]), e(v["gastos"]), e(v["neto"]))
                      for k, v in r["entidades"].items())
        t = r["total"]
        return """<!doctype html><html lang="es"><head><meta charset="utf-8"><title>Retiros y gastos {a}</title>
<style>body{{font:13px/1.45 system-ui,sans-serif;color:#111;margin:28px}}h1{{font-size:20px;margin:0 0 4px}}
h2{{font-size:15px;margin:22px 0 6px}}table{{border-collapse:collapse;width:100%}}th,td{{border-bottom:1px solid #ddd;
padding:5px 6px;text-align:left;vertical-align:top}}th{{font-size:11px;text-transform:uppercase;color:#555}}
.n{{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}}.c{{text-align:center}}
.tot td{{font-weight:700;border-top:2px solid #111}}.muted{{color:#666;font-size:11px}}
@media print{{button{{display:none}}body{{margin:12mm}}}}</style></head><body>
<button onclick="print()" style="float:right;padding:8px 14px">Imprimir o guardar en PDF</button>
<h1>Retiros y gastos de trading · {a}</h1><div class=muted>Generado por {app} el {hoy}. Importes en euros: los
recibidos de la plataforma cuando paga en euros y, si no, convertidos con el tipo de referencia del BCE.</div>
<h2>Total del año</h2><table><tr><th>Retiros</th><th>Reembolsos</th><th>Gastos</th><th>Neto</th><th>Comisiones
incluidas</th></tr><tr class=tot><td class=n>{ret}</td><td class=n>{ree}</td><td class=n>{gas}</td><td class=n>{net}
</td><td class=n>{com}</td></tr></table>
<h2>Por trimestre</h2><table><tr><th>Trimestre</th><th class=n>Retiros</th><th class=n>Reembolsos</th>
<th class=n>Gastos</th><th class=n>Neto</th></tr>{tri}</table>
<h2>Por entidad</h2><table><tr><th>Entidad</th><th class=n>Retiros</th><th class=n>Reembolsos</th>
<th class=n>Gastos</th><th class=n>Neto</th></tr>{ent}</table>
<h2>Movimientos</h2><table><tr><th>Fecha</th><th>Tipo</th><th>Entidad</th><th>Método</th><th class=n>USD</th>
<th class=n>Comisión</th><th class=n>EUR</th><th>Cambio</th><th class=c>Just.</th></tr>{filas}</table>
<p class=muted>Este informe organiza tus datos; no es asesoramiento fiscal.</p></body></html>""".format(
            a=anio, app=esc(nombre_app), hoy=datetime.now().strftime("%d/%m/%Y"), ret=e(t["retiros"]),
            ree=e(t["reembolsos"]), gas=e(t["gastos"]), net=e(t["neto"]), com=e(t["comisiones"]), tri=tri, ent=ent,
            filas=filas or "<tr><td colspan=9 class=muted>Sin movimientos</td></tr>")
