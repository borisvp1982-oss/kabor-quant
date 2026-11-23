# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Borislav Vladimirov Petrakiev
"""
temas.py -- Temas del panel cargados desde un archivo (Ajustes → Temas).

Un tema es un archivo .json pequeño que parte de uno de los estilos que trae el panel («clasico», «cristal» o «lima»)
y cambia sus colores, el fondo, las esquinas o la letra. Se guarda en data/temas/ y se puede usar desde cualquier
navegador o móvil; qué tema usa cada uno se elige en su navegador.

Formato (todas las claves de color son opcionales):

{
  "nombre": "Mi tema",
  "base": "cristal",
  "claro":  {"fondo": "linear-gradient(160deg,#1e6a7e,#9fd3da)", "acento": "#0b7f96", "texto": "#0c1c24"},
  "oscuro": {"fondo": "#05202a", "acento": "#52d3ea"}
}

«claro» se aplica con el sistema en modo claro y «oscuro» en modo oscuro; «todos» se aplica en los dos.
Por seguridad solo se aceptan colores, degradados, medidas y nombres de letra: nada de url(), @import ni código.
"""
import json
import os
import re
import secrets
import threading
import time

BASES = ("clasico", "cristal", "lima")
CLAVES = {          # clave del archivo -> variable CSS del panel
    "fondo": "--fondo", "paneles": "--panel", "campos": "--bg", "texto": "--ink", "texto_suave": "--muted",
    "lineas": "--rule", "acento": "--gold", "acento_suave": "--gold-soft", "ganancia": "--gain", "perdida": "--loss",
    "aviso": "--warn", "azul": "--blue", "morado": "--purple", "esquinas": "--r-big", "esquinas_pequenas": "--r-small",
    "letra": "--font", "contenedor": "--contenedor", "boton": "--boton", "boton_principal": "--btn1",
    "boton_principal_texto": "--btn1-ink", "pastilla_activa": "--pill-on", "pastilla_activa_texto": "--pill-on-ink",
    "destacado": "--lima", "destacado_texto": "--lima-ink", "sombra": "--sombra", "borde": "--borde",
    "menu_fondo": "--sb-bg", "menu_fondo_2": "--sb-bg2", "menu_texto": "--sb-ink", "menu_texto_suave": "--sb-muted",
    "menu_activo": "--sb-active", "menu_acento": "--sb-gold", "menu_hover": "--sb-hover", "menu_lineas": "--sb-rule",
}
MODOS = ("todos", "claro", "oscuro")
VALOR_RE = re.compile(r"^[\w\s#%.,()+\-/\"']{1,600}$")
PROHIBIDO = re.compile(r"url\s*\(|@|expression|javascript|import|\\|var\s*\(", re.I)
MAX_TEMAS = 30
MAX_BYTES = 20_000


def validar(d):
    """Devuelve (tema normalizado, errores)."""
    errs = []
    if not isinstance(d, dict):
        return None, ["El archivo no es un tema (tiene que ser un objeto JSON)"]
    nombre = re.sub(r"\s+", " ", str(d.get("nombre", "")).strip())[:40]
    if not nombre:
        errs.append("Falta el nombre del tema")
    base = str(d.get("base", "clasico")).strip().lower().replace("á", "a")
    if base not in BASES:
        errs.append("«base» tiene que ser una de: " + ", ".join(BASES))
    out = {"nombre": nombre, "base": base}
    hay = False
    for modo in MODOS:
        bloque = d.get(modo)
        if bloque in (None, {}):
            continue
        if not isinstance(bloque, dict):
            errs.append("«{}» tiene que ser un objeto con colores".format(modo))
            continue
        limpio = {}
        for k, v in bloque.items():
            if k not in CLAVES:
                errs.append("Clave desconocida en «{}»: {}".format(modo, k))
                continue
            v = str(v).strip()
            if not VALOR_RE.match(v) or PROHIBIDO.search(v):
                errs.append("Valor no permitido en «{}.{}»: solo colores, degradados, medidas o nombres de letra".format(
                    modo, k))
                continue
            limpio[k] = v
        if limpio:
            out[modo] = limpio
            hay = True
    if not hay and not errs:
        errs.append("El tema no cambia nada: añade colores en «claro», «oscuro» o «todos»")
    return (out if not errs else None), errs


class Temas:
    def __init__(self, folder):
        self.folder = folder
        os.makedirs(folder, exist_ok=True)
        self.lock = threading.Lock()

    def lista(self):
        out = []
        with self.lock:
            for n in sorted(os.listdir(self.folder)):
                if not n.endswith(".json"):
                    continue
                try:
                    with open(os.path.join(self.folder, n), encoding="utf-8") as f:
                        t = json.load(f)
                    t["id"] = n[:-5]
                    out.append(t)
                except (OSError, ValueError):
                    pass
        return sorted(out, key=lambda t: t.get("creado", 0))

    def guardar(self, d):
        if isinstance(d, str):
            if len(d.encode("utf-8")) > MAX_BYTES:
                return None, ["El archivo pasa de 20 KB: un tema son solo unos colores"]
            try:
                d = json.loads(d)
            except ValueError as e:
                return None, ["El archivo no es un JSON válido ({})".format(str(e)[:80])]
        t, errs = validar(d)
        if errs:
            return None, errs
        with self.lock:
            if len([n for n in os.listdir(self.folder) if n.endswith(".json")]) >= MAX_TEMAS:
                return None, ["Ya tienes {} temas: borra alguno antes".format(MAX_TEMAS)]
            tid = "t" + secrets.token_hex(4)
            t["creado"] = int(time.time() * 1000)
            tmp = os.path.join(self.folder, tid + ".json.tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(t, f, indent=1, ensure_ascii=False)
            os.replace(tmp, os.path.join(self.folder, tid + ".json"))
        t["id"] = tid
        return t, []

    def borrar(self, tid):
        if not re.fullmatch(r"t[0-9a-f]{8}", str(tid)):
            raise ValueError("Tema no válido")
        with self.lock:
            try:
                os.remove(os.path.join(self.folder, tid + ".json"))
            except OSError:
                raise ValueError("Ese tema ya no existe")
