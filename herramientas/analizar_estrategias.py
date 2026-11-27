#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""KaBor Quant · analizar las estrategias de un bot desde el portátil.

Pide a la Raspberry varias pruebas en el histórico del mismo bot (cada estrategia sola, el bot sin cada una, cada
dirección por estrategia y cada orden de prioridad) y escribe un informe HTML con:

  * qué variante sale mejor y cuánto cambia respecto a la configuración actual;
  * cuándo da señal cada estrategia, en qué dirección y qué pasó con cada señal;
  * cómo se pisan: qué señales bloquea cada una y cuánto habrían dado esas señales bloqueadas;
  * señales contrarias a la operación abierta y cómo acabó esa operación;
  * a qué horas da señales y entra cada estrategia.

Solo usa la biblioteca estándar de Python 3.8 o posterior (Windows, macOS o Linux). Las pruebas se hacen en la
Raspberry, con sus velas y sus estrategias; los bots siguen funcionando mientras tanto.

Ejemplos:
  python analizar_estrategias.py --pi https://DIRECCION-DE-TU-PANEL:8083 --usuario TU_USUARIO --bot 3
  python analizar_estrategias.py --pi https://192.168.1.50:8083 --sin-verificar --usuario TU_USUARIO --bot 3 --dias 90
  python analizar_estrategias.py --pi https://... --token "$KQ_TOKEN" --bot 3 --rapido
  python analizar_estrategias.py --archivo desglose_BTC_15m_2026-11-25.json     (sin conectar: lo descargado
                                                                               en el panel, «Descargar datos»)

Nada de lo que muestra es un consejo de inversión: son resultados pasados de pruebas en el histórico. Con pocas
operaciones, las diferencias pueden ser casualidad; y probar muchas variantes y quedarse con la mejor sobreajusta.
"""

import argparse
import csv
import getpass
import html
import itertools
import json
import math
import os
import ssl
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime

VERSION = "9.4.4"
TF_MS = {"1m": 60_000, "3m": 180_000, "5m": 300_000, "15m": 900_000, "30m": 1_800_000, "1h": 3_600_000,
         "2h": 7_200_000, "4h": 14_400_000, "1d": 86_400_000, "1w": 604_800_000}
DIRECCIONES = ("ambos", "solo largos", "solo cortos")
COLORES = ["#B08A2E", "#3D7BD9", "#8E5CD9", "#1E8A5A", "#C77700", "#C2453A"]


# ----------------------------------------------------------------------------- conexión con la Raspberry
class Pi:
    def __init__(self, url, token=None, verificar=True, certificado=None, espera=300):
        self.url = url.rstrip("/")
        self.token, self.espera = token, espera
        if certificado:
            ctx = ssl.create_default_context(cafile=certificado)
            ctx.check_hostname = False          # el certificado propio de la Raspberry no lleva su nombre de red
        elif verificar:
            ctx = ssl.create_default_context()
        else:
            ctx = ssl._create_unverified_context()
        self.op = urllib.request.build_opener(urllib.request.HTTPSHandler(context=ctx))
        self.cookie = None      # la sesión, solo en memoria

    def llamar(self, ruta, cuerpo=None):
        req = urllib.request.Request(self.url + ruta, method="POST" if cuerpo is not None else "GET",
                                     data=json.dumps(cuerpo).encode() if cuerpo is not None else None)
        req.add_header("X-KQ", "1")
        req.add_header("Content-Type", "application/json")
        if self.token:
            req.add_header("X-Token", self.token)
        if self.cookie:
            req.add_header("Cookie", self.cookie)
        try:
            with self.op.open(req, timeout=self.espera) as r:
                for c in r.headers.get_all("Set-Cookie") or []:
                    self.cookie = c.split(";", 1)[0]
                return json.load(r)
        except urllib.error.HTTPError as e:
            try:
                return json.loads(e.read().decode("utf-8", "replace"))
            except ValueError:
                return {"ok": False, "errors": ["HTTP {}".format(e.code)]}

    def entrar(self, usuario, clave):
        j = self.llamar("/api/login", {"usuario": usuario, "clave": clave, "recordar": False})
        if j.get("codigo"):
            codigo = input("Código de tu app de verificación: ").strip()
            j = self.llamar("/api/login", {"usuario": usuario, "clave": clave, "recordar": False, "codigo": codigo})
        if not j.get("ok"):
            raise SystemExit("No se pudo entrar: {}".format(j.get("error") or j))

    def salir(self):
        if not self.token:
            try:
                self.llamar("/api/logout", {})
            except Exception:
                pass


def error_tls(e):
    t = str(e)
    if "CERTIFICATE_VERIFY_FAILED" in t or "certificate verify failed" in t:
        raise SystemExit(
            "El certificado de la Raspberry no es de una autoridad conocida (es el suyo propio).\n"
            "  · Mejor: copia data/tls/cert.pem de la Raspberry y usa --certificado cert.pem\n"
            "  · O entra por Tailscale (certificado válido)\n"
            "  · O, solo en tu red de casa, --sin-verificar")
    raise SystemExit("Sin conexión con la Raspberry: {}".format(t))


# ----------------------------------------------------------------------------- pruebas
def prueba(pi, P, dias, orden=None, variante=None):
    j = pi.llamar("/api/backtest", {"params": P, "days": dias, "orden": orden, "variante": variante})
    if not j.get("ok"):
        return None, "; ".join(j.get("errors") or [str(j.get("error") or "error")])
    return j, None


def resumen(j):
    m = j["metrics"]
    return {"n": m.get("n", 0), "r": m.get("total_r", 0.0) or 0.0, "pf": m.get("pf"), "winrate": m.get("winrate"),
            "dd": m.get("max_dd"), "sqn": m.get("sqn")}


def variantes(P, partes, nombres, rapido=False):
    """Lista de (grupo, nombre, params, orden, variante) a probar."""
    out = [("Actual", "Configuración actual", P, None, None)]
    if len(partes) > 1:
        for sid in partes:
            out.append(("Sola", "{} sola".format(nombres[sid]), P, None, {"sola": sid}))
        for sid in partes:
            out.append(("Sin", "Sin {}".format(nombres[sid]), P, None, {"sin": sid}))
    for k, sid in enumerate(partes):
        clave = "direction" if k == 0 else "{}__direction".format(sid)
        actual = P.get(clave) or "ambos"
        for d in DIRECCIONES:
            if d != actual:
                out.append(("Dirección", "{}: {}".format(nombres[sid], d), dict(P, **{clave: d}), None, None))
    if len(partes) > 1 and not rapido:
        perms = list(itertools.permutations(partes))
        if len(perms) > 24:
            perms = perms[:24]
        for o in perms:
            out.append(("Prioridad", " → ".join(nombres[s] for s in o), P, list(o), None))
        out = [x for x in out]
    return out


# ----------------------------------------------------------------------------- análisis
def fmt(v, d=2, signo=False):
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return "–"
    s = "{:+,.{}f}".format(v, d) if signo else "{:,.{}f}".format(v, d)
    return s.replace(",", "X").replace(".", ",").replace("X", ".")


def hora(ms, zona=None):
    return datetime.fromtimestamp(ms / 1000).strftime("%d/%m %H:%M")


def coste_oportunidad(base, solas, partes, tf_bot):
    """Para cada señal bloqueada de A por una operación de B: lo que dio esa señal en la prueba de A sola (si allí
    llegó a operar) y lo que dio la operación de B que la bloqueó."""
    D = base["desglose"]
    ops_b = D["operaciones"]
    res = {}
    for t, ka, side, que, kb in D["senales"]:
        if que != "ocupada" or kb == ka or kb < 0:
            continue
        a, b = partes[ka], partes[kb]
        x = res.setdefault((a, b), {"n": 0, "casan": 0, "r_a": 0.0, "gana_a": 0, "r_b": 0.0, "contrarias": 0,
                                    "contrarias_b_pierde": 0, "ops_b": set()})
        x["n"] += 1
        ob = next((o for o in ops_b if o[2] == kb and o[0] <= t <= o[1]), None)
        if ob is None:     # orden pendiente o la operación entra en la vela siguiente
            ob = next((o for o in ops_b if o[2] == kb and t <= o[0] <= t + tf_bot), None)
        if ob is not None:
            if ob[3] != side:
                x["contrarias"] += 1
                if ob[4] < 0:
                    x["contrarias_b_pierde"] += 1
            if ob[0] not in x["ops_b"]:
                x["ops_b"].add(ob[0])
                x["r_b"] += ob[4]
        sola = solas.get(a)
        if sola:
            oa = next((o for o in sola["desglose"]["operaciones"] if o[3] == side and t <= o[0] <= t + tf_bot), None)
            if oa is not None:
                x["casan"] += 1
                x["r_a"] += oa[4]
                x["gana_a"] += oa[4] > 0
    return res


def por_hora(D, partes):
    tab = {sid: [[0, 0] for _ in range(24)] for sid in partes}
    for t, k, side, que, det in D["senales"]:
        h = datetime.fromtimestamp(t / 1000).hour
        tab[partes[k]][h][0] += 1
        tab[partes[k]][h][1] += que == "entra"
    return tab


# ----------------------------------------------------------------------------- informe HTML
CSS = """
body{font:14px/1.5 system-ui,-apple-system,Segoe UI,Roboto,sans-serif;color:#17212B;background:#F4F6F8;margin:0}
main{max-width:1180px;margin:0 auto;padding:24px 20px 60px}
h1{font-size:1.5rem;margin:0 0 4px}h2{font-size:1.15rem;margin:34px 0 10px;padding-top:8px;border-top:1px solid #D5DBE1}
h3{font-size:1rem;margin:18px 0 6px}.muted{color:#5B6672}.small{font-size:.85rem}
table{border-collapse:collapse;width:100%;background:#fff;border-radius:10px;overflow:hidden;font-size:.86rem}
th,td{padding:6px 9px;border-bottom:1px solid #E5E9ED;text-align:right;white-space:nowrap}th{background:#EEF1F4;font-weight:600}
td:first-child,th:first-child,td.l,th.l{text-align:left;white-space:normal}
.pos{color:#1E7650}.neg{color:#B03A2E}.mejor{background:#FFF6DC}.actual{background:#EAF3FF;font-weight:600}
.caja{background:#fff;border:1px solid #D5DBE1;border-radius:12px;padding:14px 16px;margin:10px 0}
.pista{background:#FFF6DC;border-left:4px solid #B08A2E;padding:8px 12px;border-radius:6px;margin:8px 0}
.ley{display:flex;gap:16px;flex-wrap:wrap;font-size:.8rem;color:#5B6672;margin:6px 0}
.ley i{display:inline-block;width:11px;height:11px;border-radius:3px;margin-right:5px;vertical-align:-1px}
.cal td{padding:3px 2px;text-align:center;font-size:.72rem;min-width:22px}.cal td.l{text-align:left;padding-left:9px;white-space:nowrap}
svg text{font:11px system-ui,sans-serif;fill:#5B6672}
.scroll{overflow:auto}
"""


def h(x):
    return html.escape(str(x))


def cls(v):
    return "pos" if (v or 0) > 0 else "neg" if (v or 0) < 0 else ""


def svg_linea_tiempo(D, partes, nombres, dias=14, fn=None):
    ops, sen, fn = D["operaciones"], D["senales"], fn or {}
    if not sen and not ops:
        return "<p class='muted'>Sin señales.</p>"
    t1 = max([s[0] for s in sen] + [o[1] for o in ops])
    t0 = t1 - dias * 86_400_000
    W, izq, fila = 1120, 170, 46
    H = 20 + len(partes) * fila + 24
    X = lambda t: izq + (t - t0) / (t1 - t0) * (W - izq - 10)
    out = ['<svg viewBox="0 0 {} {}" width="100%" role="img" aria-label="Señales y operaciones de los últimos {} días">'
           .format(W, H, dias)]
    for k, sid in enumerate(partes):
        y = 14 + k * fila
        out.append('<rect x="6" y="{}" width="10" height="10" rx="2" fill="{}"/>'.format(y + fila / 2 - 5, COLORES[k % 6]))
        out.append('<text x="22" y="{}" style="fill:#17212B">{}</text>'.format(y + fila / 2 + 4, h(nombres[sid][:24])))
        out.append('<line x1="{}" x2="{}" y1="{}" y2="{}" stroke="#E0E5EA"/>'.format(izq, W - 10, y + fila, y + fila))
    d = t0 - (t0 % 86_400_000) + 86_400_000
    while d < t1:
        x = X(d)
        out.append('<line x1="{0}" x2="{0}" y1="14" y2="{1}" stroke="#EEF1F4"/><text x="{0}" y="{2}" text-anchor="middle">{3}</text>'
                   .format(x, 14 + len(partes) * fila, H - 6, datetime.fromtimestamp(d / 1000).strftime("%d/%m")))
        d += 86_400_000
    for ta, tc, k, side, r in ops:
        if tc < t0:
            continue
        xa = max(X(ta), izq)
        out.append('<rect x="{:.1f}" y="{}" width="{:.1f}" height="6" rx="2" fill="{}"><title>{} {} · {} R · {} → {}</title></rect>'
                   .format(xa, 14 + k * fila + fila - 12, max(X(tc) - xa, 2), "#1E8A5A" if r >= 0 else "#C2453A",
                           h(nombres[partes[k]]), "largo" if side == 1 else "corto", fmt(r, 2, True), hora(ta), hora(tc)))
    color = {"entra": "#17212B", "ocupada": "#8A96A3", "filtro": "#C77700", "limite": "#C2453A"}
    for t, k, side, que, det in sen:
        if t < t0:
            continue
        x, y, s = X(t), 14 + k * fila + 17, 6
        pts = "{0},{1} {2},{3} {4},{3}".format(x, y - s, x + s, y + s, x - s) if side == 1 else \
            "{0},{1} {2},{3} {4},{3}".format(x, y + s, x + s, y - s, x - s)
        c = color.get(que, "#8A96A3")
        propia = que == "ocupada" and det == k
        if que == "ocupada":
            detalle = "ya en su operación" if propia else "bloqueada por {}".format(
                nombres[partes[det]] if isinstance(det, int) and 0 <= det < len(partes) else "?")
        else:
            detalle = {"entra": "entra", "filtro": "filtrada: {}".format(fn.get(det, det)),
                       "limite": "límite del día: {}".format(det)}.get(que, que)
        out.append('<polygon points="{}" fill="{}" stroke="{}" stroke-width="1.5"{}><title>{} · {} · {} · {}</title></polygon>'
                   .format(pts, c if que == "entra" else "none", c, ' opacity=".4"' if propia else "",
                           h(nombres[partes[k]]), "largo" if side == 1 else "corto", hora(t), h(detalle)))
    out.append("</svg>")
    return "".join(out)


def svg_curvas(curvas):
    """curvas: [(nombre, color, [[t, r], ...])]."""
    puntos = [p for _, _, c in curvas for p in c]
    if not puntos:
        return "<p class='muted'>Sin operaciones.</p>"
    t0, t1 = min(p[0] for p in puntos), max(p[0] for p in puntos)
    lo, hi = min(0, min(p[1] for p in puntos)), max(0, max(p[1] for p in puntos))
    W, H, izq = 1120, 260, 50
    if t1 == t0:
        t1 = t0 + 1
    if hi == lo:
        hi = lo + 1
    X = lambda t: izq + (t - t0) / (t1 - t0) * (W - izq - 10)
    Y = lambda v: 10 + (hi - v) / (hi - lo) * (H - 30)
    out = ['<svg viewBox="0 0 {} {}" width="100%" role="img" aria-label="Curva de R acumulado">'.format(W, H)]
    paso = max(1, round((hi - lo) / 5))
    v = math.ceil(lo / paso) * paso
    while v <= hi:
        out.append('<line x1="{0}" x2="{1}" y1="{2:.1f}" y2="{2:.1f}" stroke="{3}"/><text x="{4}" y="{5:.1f}" text-anchor="end">{6} R</text>'
                   .format(izq, W - 10, Y(v), "#B9C2CB" if v == 0 else "#EEF1F4", izq - 6, Y(v) + 4, fmt(v, 0, True)))
        v += paso
    for nombre, col, c in curvas:
        if not c:
            continue
        d = "M{:.1f},{:.1f} ".format(X(c[0][0]), Y(0)) + " ".join("L{:.1f},{:.1f}".format(X(t), Y(r)) for t, r in c)
        out.append('<path d="{}" fill="none" stroke="{}" stroke-width="2"><title>{}</title></path>'.format(d, col, h(nombre)))
    out.append('<text x="{}" y="{}">{}</text><text x="{}" y="{}" text-anchor="end">{}</text>'.format(
        izq, H - 4, datetime.fromtimestamp(t0 / 1000).strftime("%d/%m/%y"), W - 10, H - 4,
        datetime.fromtimestamp(t1 / 1000).strftime("%d/%m/%y")))
    out.append("</svg>")
    return "".join(out)


def tabla_horas(tab, partes, nombres):
    filas = []
    for k, sid in enumerate(partes):
        mx = max(1, max(x[0] for x in tab[sid]))
        celdas = []
        for hh, (n, e) in enumerate(tab[sid]):
            a = 0.08 + 0.8 * n / mx if n else 0
            celdas.append('<td style="background:rgba(61,123,217,{:.2f})" title="{}:00 · {} señales · {} entran">{}</td>'
                          .format(a, hh, n, e, n or ""))
        filas.append("<tr><td class='l'>{}</td>{}</tr>".format(h(nombres[sid]), "".join(celdas)))
    return ("<div class='scroll'><table class='cal'><tr><th class='l'>Estrategia</th>{}</tr>{}</table></div>"
            .format("".join("<th>{}</th>".format(x) for x in range(24)), "".join(filas)))


def informe(base, resultados, solas, partes, nombres, P, dias, origen):
    D = base["desglose"]
    tf_bot = TF_MS.get(P["timeframe"], 60_000)
    est = {e["id"]: e for e in D["estrategias"]}
    ahora = datetime.now().strftime("%d/%m/%Y %H:%M")
    o = ["<!doctype html><html lang='es'><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>",
         "<title>Análisis de estrategias · {} {}</title><style>{}</style><main>".format(h(P["symbol"]), h(P["timeframe"]), CSS)]
    o.append("<h1>Análisis de estrategias · {} {}</h1>".format(h(P["symbol"]), h(P["timeframe"])))
    o.append("<p class='muted'>{} · del {} al {} · {} · generado el {} con analizar_estrategias.py {}</p>".format(
        h(" + ".join(nombres[s] for s in partes)), datetime.fromtimestamp(base["from"] / 1000).strftime("%d/%m/%Y"),
        datetime.fromtimestamp(base["to"] / 1000).strftime("%d/%m/%Y"), h(origen), ahora, VERSION))
    o.append("<div class='pista'>Resultados pasados de pruebas en el histórico, no un consejo de inversión. Si una "
             "variante gana por poco o con pocas operaciones, puede ser casualidad; y quedarse siempre con la mejor de "
             "muchas sobreajusta. Confirma lo que cambies con otro periodo (por ejemplo --dias 30 y --dias 180).</div>")

    # 1) estrategias de la configuración actual
    o.append("<h2>1. La configuración actual, estrategia a estrategia</h2>")
    o.append("<table><tr><th>#</th><th class='l'>Estrategia</th><th>TF</th><th>Dirección</th><th>Ops.</th><th>Acierto</th>"
             "<th>PF</th><th>R</th><th>Largos</th><th>Cortos</th><th>Señales</th><th>Entran</th><th>Bloqueadas</th><th>Filtradas</th></tr>")
    for k, sid in enumerate(partes):
        e = est.get(sid)
        if not e:
            continue
        m, S, L, C = e["metrics"], e["senales"], e["lados"]["LONG"], e["lados"]["SHORT"]
        o.append("<tr><td>{}</td><td class='l'><i style='display:inline-block;width:10px;height:10px;border-radius:3px;background:{}'></i> {}</td>"
                 "<td>{}</td><td>{}</td><td>{}</td><td>{}</td><td>{}</td><td class='{}'>{}</td><td class='{}'>{} ({})</td>"
                 "<td class='{}'>{} ({})</td><td>{}</td><td>{}</td><td>{}</td><td>{}</td></tr>".format(
                     e["prioridad"], COLORES[k % 6], h(nombres[sid]), h(e["tf"]), h(e["direccion"]), m["n"],
                     fmt(m.get("winrate"), 1) + " %" if m["n"] else "–", fmt(m.get("pf")), cls(m.get("total_r")),
                     fmt(m.get("total_r"), 2, True), cls(L.get("total_r")), fmt(L.get("total_r"), 2, True), L["n"],
                     cls(C.get("total_r")), fmt(C.get("total_r"), 2, True), C["n"], S["total"], S["entra"], S["ocupada"],
                     sum(S["filtro"].values())))
    o.append("</table>")
    o.append(svg_curvas([(nombres[s], COLORES[k % 6], est[s]["curva"]) for k, s in enumerate(partes) if s in est]))
    o.append("<div class='ley'>{}</div>".format("".join("<span><i style='background:{}'></i>{}</span>".format(
        COLORES[k % 6], h(nombres[s])) for k, s in enumerate(partes))))

    # 2) variantes
    if resultados:
        b = resumen(base)
        o.append("<h2>2. Variantes</h2><p class='muted small'>Mismo periodo y mismas velas. «Δ R» es la diferencia con la "
                 "configuración actual. Ordenadas por R total dentro de cada grupo; la mejor de cada grupo, en amarillo.</p>")
        grupos = {}
        for g, n, r, err in resultados:
            grupos.setdefault(g, []).append((n, r, err))
        for g in ("Actual", "Sola", "Sin", "Dirección", "Prioridad"):
            if g not in grupos:
                continue
            titulo = {"Actual": "Configuración actual", "Sola": "Cada estrategia sola (en su temporalidad y con su dirección)",
                      "Sin": "El bot sin cada estrategia", "Dirección": "Cambiando la dirección de una estrategia",
                      "Prioridad": "Cada orden de prioridad (si dos dan señal en la misma vela)"}[g]
            o.append("<h3>{}</h3><table><tr><th class='l'>Variante</th><th>Ops.</th><th>Acierto</th><th>PF</th><th>R</th>"
                     "<th>Δ R</th><th>DD máx.</th><th>SQN</th></tr>".format(titulo))
            filas = sorted(grupos[g], key=lambda x: -(x[1]["r"] if x[1] and x[1]["n"] else -1e9))
            unica = len(filas) > 1 and filas[0][1] and filas[0][1]["n"] and filas[1][1] and \
                filas[0][1]["r"] > filas[1][1]["r"] + 1e-9      # la mejor, si de verdad hay una mejor
            for i, (n, r, err) in enumerate(filas):
                if err:
                    o.append("<tr><td class='l'>{}</td><td colspan='7' class='l neg'>{}</td></tr>".format(h(n), h(err)))
                    continue
                d = r["r"] - b["r"]
                o.append("<tr class='{}'><td class='l'>{}</td><td>{}</td><td>{}</td><td>{}</td><td class='{}'>{}</td>"
                         "<td class='{}'>{}</td><td>{}</td><td>{}</td></tr>".format(
                             "actual" if g == "Actual" else "mejor" if i == 0 and unica else "", h(n), r["n"],
                             fmt(r["winrate"], 1) + " %" if r["n"] else "–", fmt(r["pf"]), cls(r["r"]), fmt(r["r"], 2, True),
                             cls(d), fmt(d, 2, True) if g != "Actual" else "", fmt(r["dd"], 1), fmt(r["sqn"])))
            o.append("</table>")
        pistas = []
        for n, r, err in grupos.get("Dirección", []):
            if r and r["r"] - b["r"] > max(1.0, 0.1 * abs(b["r"])) and r["n"] >= 20:
                pistas.append("«{}» mejora {} R con {} operaciones.".format(n, fmt(r["r"] - b["r"], 2, True), r["n"]))
        for n, r, err in grupos.get("Sin", []):
            if r and r["r"] - b["r"] > max(1.0, 0.1 * abs(b["r"])):
                pistas.append("«{}» mejora {} R: esa estrategia resta en esta combinación (quizá rinda mejor en su "
                              "propio bot).".format(n, fmt(r["r"] - b["r"], 2, True)))
        if pistas:
            o.append("<h3>Para probar</h3>" + "".join("<div class='pista'>{}</div>".format(h(p)) for p in pistas))

    # 3) cómo se pisan
    if len(partes) > 1:
        o.append("<h2>3. Cómo se pisan las estrategias</h2>")
        co = coste_oportunidad(base, solas, partes, tf_bot)
        if not co:
            o.append("<p>Ninguna señal quedó bloqueada por la operación de otra estrategia.</p>")
        else:
            o.append("<p class='muted small'>«Habrían dado»: lo que dieron esas mismas señales en la prueba de la "
                     "estrategia sola (solo las que allí llegaron a operar). «La que bloqueó»: lo que dieron las "
                     "operaciones que las bloquearon. Si lo bloqueado habría dado más que lo que lo bloqueó, la prioridad "
                     "o la combinación no ayudan.</p>")
            o.append("<table><tr><th class='l'>Señal de</th><th class='l'>Bloqueada por</th><th>Señales</th>"
                     "<th>Casan en la prueba sola</th><th>Habrían dado</th><th>Acierto</th><th>La que bloqueó dio</th>"
                     "<th>De lado contrario</th><th>…y esa operación perdió</th></tr>")
            for (a, bb), x in sorted(co.items(), key=lambda kv: -kv[1]["n"]):
                o.append("<tr><td class='l'>{}</td><td class='l'>{}</td><td>{}</td><td>{}</td><td class='{}'>{}</td><td>{}</td>"
                         "<td class='{}'>{}</td><td>{}</td><td>{}</td></tr>".format(
                             h(nombres[a]), h(nombres[bb]), x["n"], x["casan"], cls(x["r_a"]),
                             fmt(x["r_a"], 2, True) + " R" if solas.get(a) else "–",
                             fmt(100.0 * x["gana_a"] / x["casan"], 0) + " %" if x["casan"] else "–",
                             cls(x["r_b"]), fmt(x["r_b"], 2, True) + " R", x["contrarias"], x["contrarias_b_pierde"]))
            o.append("</table>")
        c = D.get("coinciden") or {}
        o.append("<p>Velas con señal de dos o más estrategias a la vez: <b>{}</b> (mismo lado {}, lados contrarios {}).</p>"
                 .format(c.get("mismo", 0) + c.get("contrario", 0), c.get("mismo", 0), c.get("contrario", 0)))

    # 4) línea de tiempo y horas
    o.append("<h2>{}. Cuándo da señal cada estrategia</h2>".format(4 if len(partes) > 1 else 3))
    o.append("<div class='caja'>{}</div>".format(svg_linea_tiempo(D, partes, nombres, 14, base.get("filter_names"))))
    o.append("<div class='ley'><span>▲ largo · ▼ corto</span><span>relleno: entra</span><span>gris: bloqueada por otra "
             "operación</span><span>naranja: filtrada</span><span>rojo: límite del día</span><span>barras: operaciones "
             "(verde gana, rojo pierde)</span><span>Pasa el ratón por una marca para ver el detalle.</span></div>")
    o.append("<h3>Señales por hora (hora de este ordenador)</h3>")
    o.append(tabla_horas(por_hora(D, partes), partes, nombres))
    o.append("<p class='muted small'>Los datos completos están en senales.csv y operaciones.csv, junto a este informe.</p>")
    o.append("</main></html>")
    return "".join(o)


def escribir_csv(carpeta, base, partes, nombres, P, resultados):
    D = base["desglose"]
    fn = base.get("filter_names") or {}
    with open(os.path.join(carpeta, "senales.csv"), "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f, delimiter=";")
        w.writerow(["fecha", "estrategia", "lado", "resultado", "detalle"])
        for t, k, side, que, det in D["senales"]:
            if que == "ocupada":
                det = "propia" if det == k else nombres[partes[det]] if 0 <= det < len(partes) else det
            elif que == "filtro":
                det = fn.get(det, det)
            w.writerow([datetime.fromtimestamp(t / 1000).strftime("%Y-%m-%d %H:%M"), nombres[partes[k]],
                        "largo" if side == 1 else "corto", que, det])
    with open(os.path.join(carpeta, "operaciones.csv"), "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f, delimiter=";")
        w.writerow(["entrada", "salida", "estrategia", "lado", "R"])
        for ta, tc, k, side, r in D["operaciones"]:
            w.writerow([datetime.fromtimestamp(ta / 1000).strftime("%Y-%m-%d %H:%M"),
                        datetime.fromtimestamp(tc / 1000).strftime("%Y-%m-%d %H:%M"), nombres[partes[k]],
                        "largo" if side == 1 else "corto", str(round(r, 3)).replace(".", ",")])
    if resultados:
        with open(os.path.join(carpeta, "variantes.csv"), "w", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f, delimiter=";")
            w.writerow(["grupo", "variante", "operaciones", "acierto", "PF", "R", "DD", "SQN", "error"])
            for g, n, r, err in resultados:
                num = lambda v: "" if v is None else str(round(v, 3)).replace(".", ",")   # Excel en español
                w.writerow([g, n] + (["", "", "", "", "", "", err] if err else
                                     [r["n"], num(r["winrate"]), num(r["pf"]), num(r["r"]), num(r["dd"]), num(r["sqn"]), ""]))


# ----------------------------------------------------------------------------- principal
def main():
    ap = argparse.ArgumentParser(description="Analiza las estrategias de un bot de KaBor Quant (desde el portátil).")
    ap.add_argument("--pi", help="dirección del panel, p. ej. https://192.168.1.50:8083")
    ap.add_argument("--usuario", help="tu usuario del panel (la contraseña se pide al ejecutar)")
    ap.add_argument("--token", default=os.environ.get("KQ_TOKEN"), help="token de API (./bot.sh configurar), en vez de usuario")
    ap.add_argument("--bot", help="número del bot (el que sale en la dirección: #bot/3)")
    ap.add_argument("--dias", type=int, default=90, help="días de histórico (por defecto 90; 0 = todo)")
    ap.add_argument("--rapido", action="store_true", help="sin probar todos los órdenes de prioridad")
    ap.add_argument("--solo-actual", action="store_true", help="solo la configuración actual, sin variantes")
    ap.add_argument("--archivo", help="analizar un JSON descargado del panel («Descargar datos»), sin conectar")
    ap.add_argument("--certificado", help="cert.pem de la Raspberry (data/tls/cert.pem) para verificar la conexión")
    ap.add_argument("--sin-verificar", action="store_true", help="no comprobar el certificado (solo en tu red de casa)")
    ap.add_argument("--salida", default=".", help="carpeta donde guardar el informe")
    a = ap.parse_args()

    resultados, solas = [], {}
    if a.archivo:
        with open(a.archivo, encoding="utf-8") as f:
            d = json.load(f)
        if d.get("tipo") != "desglose":
            raise SystemExit("Ese archivo no es un desglose descargado del panel de KaBor Quant.")
        base = {"desglose": d["desglose"], "metrics": d["metrics"], "from": d["desde"], "to": d["hasta"],
                "filter_names": d.get("filter_names")}
        P = d["params"]
        origen = "archivo {}".format(os.path.basename(a.archivo))
    else:
        if not a.pi or not a.bot:
            ap.error("indica --pi y --bot (o --archivo)")
        if not a.token and not a.usuario:
            ap.error("indica --usuario (o --token)")
        pi = Pi(a.pi, a.token, verificar=not a.sin_verificar, certificado=a.certificado)
        try:
            if not a.token:
                pi.entrar(a.usuario, getpass.getpass("Contraseña de {}: ".format(a.usuario)))
            cfg = pi.llamar("/api/b/{}/config".format(a.bot))
        except urllib.error.URLError as e:
            error_tls(e.reason)
        except ssl.SSLError as e:
            error_tls(e)
        if not cfg.get("params"):
            raise SystemExit("No se pudo leer el bot {}: {}".format(a.bot, cfg.get("errors") or cfg.get("error") or cfg))
        P = cfg["params"]
        nombres0 = {k: v.get("nombre", k) for k, v in (cfg.get("estrategias") or {}).items()}
        partes = [P["estrategia"]] + [x for x in (P.get("combinar") or []) if x != P["estrategia"]]
        nombres = {s: nombres0.get(s, s) for s in partes}
        orden_bot = None
        if len(partes) > 1:      # la prioridad actual (por puntuación), la misma que usa el panel al probar
            ie = pi.llamar("/api/b/{}/estrategias".format(a.bot))
            orden_bot = [e["id"] for e in (ie.get("estrategias") or []) if e.get("activa")] or None
        lista = variantes(P, partes, nombres, a.rapido) if not a.solo_actual else \
            [("Actual", "Configuración actual", P, None, None)]
        lista = [(g, n, Q, orden if orden is not None else orden_bot, var) for g, n, Q, orden, var in lista]
        print("Bot {} · {} {} · {} · {} pruebas de {} días en la Raspberry".format(
            a.bot, P["symbol"], P["timeframe"], " + ".join(nombres.values()), len(lista), a.dias or "todos los"))
        base = None
        try:
            for i, (g, n, Q, orden, var) in enumerate(lista, 1):
                t = time.time()
                j, err = prueba(pi, Q, a.dias, orden, var)
                print("  [{}/{}] {:<52} {}".format(i, len(lista), n[:52], err or "{} ops · {} R · {:.1f} s".format(
                    j["metrics"]["n"], fmt(j["metrics"].get("total_r"), 2, True), time.time() - t)))
                if g == "Actual":
                    if not j:
                        raise SystemExit("La prueba de la configuración actual falla: {}".format(err))
                    base = j
                if g == "Sola" and j:
                    solas[var["sola"]] = j
                resultados.append((g, n, resumen(j) if j else None, err))
        except KeyboardInterrupt:
            print("\nInterrumpido: hago el informe con lo que hay.")
        finally:
            pi.salir()
        if base is None:
            raise SystemExit("Sin resultados.")
        if len(partes) == 1:
            solas[partes[0]] = base
        origen = "Raspberry {}".format(a.pi)
    D = base["desglose"]
    partes = [e["id"] for e in sorted(D["estrategias"], key=lambda e: e["prioridad"])]
    nombres = {e["id"]: e["nombre"] for e in D["estrategias"]}
    # el desglose numera las estrategias en su orden de prioridad
    carpeta = os.path.join(a.salida, "analisis_{}_{}_{}".format(P["symbol"], P["timeframe"],
                                                                 datetime.now().strftime("%Y%m%d_%H%M")))
    os.makedirs(carpeta, exist_ok=True)
    with open(os.path.join(carpeta, "informe.html"), "w", encoding="utf-8") as f:
        f.write(informe(base, resultados, solas, partes, nombres, P, a.dias, origen))
    escribir_csv(carpeta, base, partes, nombres, P, resultados)
    print("Informe: {}".format(os.path.abspath(os.path.join(carpeta, "informe.html"))))


if __name__ == "__main__":
    main()
