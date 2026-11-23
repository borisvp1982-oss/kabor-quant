# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Borislav Vladimirov Petrakiev
"""
noticias.py -- Filtro de noticias de KaBor Quant (desde la 6.4).

* Calendario económico: feed público semanal de Forex Factory en JSON (sin clave, gratis, no oficial).
  Se descarga cada hora en un hilo propio y se guarda en cache/calendario.json; un fallo nunca frena a los bots.
* Antes de cada entrada, Engine._handle pregunta permitir(bot, hora_de_entrada): si la entrada cae dentro de la
  ventana de una noticia (X min antes y después), la señal se ignora con el motivo, igual que con el riesgo global.
  Las operaciones abiertas no se tocan: siguen con su stop y su objetivo.
* Aviso por Telegram («Noticias») unos minutos antes de cada ventana, con los bots que tienen una operación abierta.
* No consulta ningún modelo de IA: es lógica fija.
"""
import json
import os
import re
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

FUENTES = ("https://nfs.faireconomy.media/ff_calendar_thisweek.json",
           "https://cdn-nfs.faireconomy.media/ff_calendar_thisweek.json")
CONFIG = "noticias.json"
CACHE = os.path.join("cache", "calendario.json")
ESTADO = os.path.join("cache", "noticias_estado.json")
REFRESCO_S = 3600          # una descarga por hora (el feed bloquea si se le pide demasiado)
REINTENTO_S = 900          # tras un fallo, se reintenta a los 15 min
MIN_MANUAL_S = 300         # «Actualizar ahora» como mucho cada 5 min
CADUCA_S = 36 * 3600       # sin descargar en 36 h, el calendario se considera caducado
AVISO_FALLO_S = 6 * 3600   # aviso de problema si lleva 6 h sin poder descargarlo
IMPACTOS = ("High", "Medium", "Low")
IMPACTO_ES = {"High": "alto", "Medium": "medio", "Low": "bajo"}
DEFAULTS = {"activo": False, "divisas": "USD", "impactos": ["High"], "min_antes": 15, "min_despues": 15,
            "grandes": "FOMC, Federal Funds Rate, Non-Farm, CPI, Fed Chair", "grandes_antes": 30,
            "grandes_despues": 60, "ignorar": "", "sin_calendario": "operar", "bots_fuera": [], "aviso_min": 30}


DIAS = ("lun", "mar", "mié", "jue", "vie", "sáb", "dom")


def dia_hora(ms, tz):
    d = datetime.fromtimestamp(ms / 1000, tz)
    return "{} {} {}".format(DIAS[d.weekday()], d.strftime("%d"), d.strftime("%H:%M"))


def _ms():
    return int(time.time() * 1000)


def _palabras(txt):
    return [p.strip().lower() for p in re.split(r"[,;\n]", txt or "") if p.strip()]


def _escribir(path, obj, privado=False):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=1, ensure_ascii=False)
    if privado:
        os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def _leer(path, defecto):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return defecto


def parsear(datos):
    """Lista del feed -> eventos [{t, titulo, divisa, impacto, prevision, anterior}] ordenados por hora."""
    if not isinstance(datos, list):
        raise ValueError("el calendario no tiene el formato esperado")
    out = []
    for e in datos:
        try:
            imp = str(e.get("impact", "")).strip().capitalize()
            if imp not in IMPACTOS:               # «Holiday» y otros no bloquean
                continue
            d = datetime.fromisoformat(str(e["date"]).replace("Z", "+00:00"))
            if d.tzinfo is None:
                d = d.replace(tzinfo=timezone.utc)
            out.append({"t": int(d.timestamp() * 1000), "titulo": str(e.get("title", "")).strip()[:120],
                        "divisa": str(e.get("country", "")).strip().upper()[:4], "impacto": imp,
                        "prevision": str(e.get("forecast") or "")[:20], "anterior": str(e.get("previous") or "")[:20]})
        except (KeyError, ValueError, TypeError, AttributeError):
            continue
    if datos and not out:
        raise ValueError("el calendario no trae eventos reconocibles")
    out.sort(key=lambda x: x["t"])
    return out


class Noticias:
    def __init__(self, hub):
        self.hub = hub
        self.lock = threading.RLock()
        self.cfg = dict(DEFAULTS)
        self.cfg.update({k: v for k, v in _leer(CONFIG, {}).items() if k in DEFAULTS})
        cache = _leer(CACHE, {})
        self.eventos = cache.get("eventos") or []
        self.actualizado = cache.get("actualizado")      # ms de la última descarga buena
        self.fuente = cache.get("fuente")
        self.error = None
        self.ultimo_intento = 0
        self.fallo_desde = None
        self.fallo_avisado = False
        self.avisados = set(_leer(ESTADO, {}).get("avisados", []))
        self.ventanas = []
        self._recalcular()

    # ---------- calendario ----------
    def descargar(self):
        ult = None
        for url in FUENTES:
            try:
                req = urllib.request.Request(url, headers={"User-Agent": "KaBorQuant/6.4 (+filtro de noticias)",
                                                           "Accept": "application/json"})
                with urllib.request.urlopen(req, timeout=20) as r:
                    eventos = parsear(json.loads(r.read().decode("utf-8")))
                with self.lock:
                    self.eventos, self.actualizado, self.fuente, self.error = eventos, _ms(), url, None
                    self.fallo_desde, self.fallo_avisado = None, False
                    self._recalcular()
                _escribir(CACHE, {"actualizado": self.actualizado, "fuente": url, "eventos": eventos})
                return True, len(eventos)
            except (urllib.error.URLError, OSError, ValueError) as e:
                ult = "{}: {}".format(url.split("/")[2], getattr(e, "reason", None) or e)
        with self.lock:
            self.error = ult
            if self.fallo_desde is None:
                self.fallo_desde = _ms()
        return False, ult

    def caducado(self):
        return not self.actualizado or _ms() - self.actualizado > CADUCA_S * 1000

    # ---------- ventanas de bloqueo ----------
    def _relevante(self, e):
        c = self.cfg
        div = {d.upper() for d in _palabras(c["divisas"])}
        if div and e["divisa"] not in div:
            return False
        if e["impacto"] not in c["impactos"]:
            return False
        tit = e["titulo"].lower()
        return not any(p in tit for p in _palabras(c["ignorar"]))

    def _grande(self, e):
        tit = e["titulo"].lower()
        return any(p in tit for p in _palabras(self.cfg["grandes"]))

    def _recalcular(self):
        """Une las ventanas que se solapan (p. ej. CPI y Core CPI a la misma hora) en una sola."""
        c, out = self.cfg, []
        for e in self.eventos:
            if not self._relevante(e):
                continue
            g = self._grande(e)
            a, d = (c["grandes_antes"], c["grandes_despues"]) if g else (c["min_antes"], c["min_despues"])
            ini, fin = e["t"] - a * 60_000, e["t"] + d * 60_000
            ev = dict(e, grande=g)
            if out and ini <= out[-1]["fin"]:
                out[-1]["fin"] = max(out[-1]["fin"], fin)
                out[-1]["eventos"].append(ev)
            else:
                out.append({"ini": ini, "fin": fin, "eventos": [ev]})
        self.ventanas = out

    @staticmethod
    def _clave(v):
        return "{}-{}".format(v["ini"], v["eventos"][0]["titulo"])

    def ventana_en(self, t):
        for v in self.ventanas:
            if v["ini"] <= t < v["fin"]:
                return v
            if v["ini"] > t:
                break
        return None

    def texto_ventana(self, v, tz):
        e = v["eventos"][0]
        h = datetime.fromtimestamp(e["t"] / 1000, tz).strftime("%H:%M")
        extra = " y {} más".format(len(v["eventos"]) - 1) if len(v["eventos"]) > 1 else ""
        return "{} ({}, {}) a las {}{}".format(e["titulo"], e["divisa"], IMPACTO_ES[e["impacto"]], h, extra)

    # ---------- lo que preguntan los bots ----------
    def aplica_a(self, bot):
        return str(bot.id) not in self.cfg["bots_fuera"]

    def permitir(self, bot, t_entrada):
        """(True, None) si el bot puede entrar a la hora t_entrada (ms); si no, (False, motivo)."""
        with self.lock:
            if not self.cfg["activo"] or not self.aplica_a(bot):
                return True, None
            if self.caducado() and self.cfg["sin_calendario"] == "bloquear":
                return False, "filtro de noticias sin calendario actualizado"
            v = self.ventana_en(t_entrada)
            if v:
                tz = self.hub.tz
                return False, "noticia: {} (sin entradas de {} a {})".format(
                    self.texto_ventana(v, tz), datetime.fromtimestamp(v["ini"] / 1000, tz).strftime("%H:%M"),
                    datetime.fromtimestamp(v["fin"] / 1000, tz).strftime("%H:%M"))
            return True, None

    # ---------- hilo: descarga y avisos ----------
    def bucle(self):
        while True:
            try:
                ahora = time.time()
                falta = (not self.actualizado or ahora * 1000 - self.actualizado >= REFRESCO_S * 1000)
                if falta and ahora - self.ultimo_intento >= (REINTENTO_S if self.error else 60):
                    self.ultimo_intento = ahora
                    ok, info = self.descargar()
                    if not ok:
                        self.hub.log("Noticias: no se pudo descargar el calendario ({})".format(info), "warn")
                self._avisar()
            except Exception as e:                # nunca debe tumbar el hilo
                self.hub.log("Noticias: {}".format(e), "error")
            time.sleep(30)

    def _avisar(self):
        av = self.hub.avisos
        if not av:
            return
        with self.lock:
            c = dict(self.cfg)
            vent = list(self.ventanas)
            fallo = self.fallo_desde
        if not c["activo"]:
            return
        if fallo and not self.fallo_avisado and _ms() - fallo > AVISO_FALLO_S * 1000:
            self.fallo_avisado = True
            if av.wants("problemas"):
                av.push("problemas", "⚠️ <b>Filtro de noticias</b>: llevo más de 6 h sin poder descargar el calendario "
                        "({}). {}".format(self.error or "sin respuesta",
                                          "Los bots bloquean las entradas mientras tanto." if
                                          c["sin_calendario"] == "bloquear" else
                                          "Los bots siguen operando sin filtro."))
        if not c["aviso_min"]:
            return
        ahora = _ms()
        for v in vent:
            k = self._clave(v)
            if k in self.avisados or v["fin"] < ahora:
                continue
            if v["ini"] - ahora > c["aviso_min"] * 60_000:
                break
            self.avisados.add(k)
            self.avisados = {x for x in self.avisados if int(x.split("-")[0]) > ahora - 7 * 86_400_000}
            _escribir(ESTADO, {"avisados": sorted(self.avisados)})
            if av.wants("noticias"):
                av.push("noticias", self._texto_aviso(v, ahora))

    def _texto_aviso(self, v, ahora):
        from avisos import esc, fr
        tz = self.hub.tz
        hm = lambda ms: datetime.fromtimestamp(ms / 1000, tz).strftime("%H:%M")
        mins = max(0, round((v["eventos"][0]["t"] - ahora) / 60000))
        L = ["📰 <b>Noticia en {} min</b>".format(mins)]
        for e in v["eventos"]:
            det = " · ".join(x for x in ("previsión " + e["prevision"] if e["prevision"] else "",
                                         "anterior " + e["anterior"] if e["anterior"] else "") if x)
            L.append("{} · {}{} ({}, {}){}".format(hm(e["t"]), "⭐ " if e["grande"] else "", esc(e["titulo"]),
                                                  e["divisa"], IMPACTO_ES[e["impacto"]], " · " + esc(det) if det else ""))
        bots = [b for b in self.hub.active_bots() if self.aplica_a(b)]
        L.append("Sin entradas de {} a {} en {} bot{}.".format(hm(v["ini"]), hm(v["fin"]), len(bots),
                                                              "" if len(bots) == 1 else "s"))
        abiertas = []
        for b in self.hub.active_bots():
            try:
                p = b.summary().get("position")
            except Exception:
                p = None
            if p:
                abiertas.append("{} ({} {} R)".format(esc(b.name), "▲" if p["side"] == "LONG" else "▼",
                                                     fr(p["unreal_r"]) if p.get("unreal_r") is not None else "?"))
        if abiertas:
            L.append("⚠️ Con operación abierta: " + ", ".join(abiertas))
        return "\n".join(L)

    # ---------- panel y Telegram ----------
    def configurar(self, nuevo):
        if not isinstance(nuevo, dict):
            return False, ["Faltan los ajustes"]
        c, errs = dict(self.cfg), []

        def entero(k, lo, hi, nombre):
            try:
                v = int(float(nuevo.get(k, c[k])))
                if not lo <= v <= hi:
                    raise ValueError
                c[k] = v
            except (TypeError, ValueError):
                errs.append("{}: un número entre {} y {}".format(nombre, lo, hi))

        c["activo"] = bool(nuevo.get("activo", c["activo"]))
        divs = [d.upper() for d in _palabras(str(nuevo.get("divisas", c["divisas"])))]
        if any(not re.fullmatch(r"[A-Z]{3}", d) for d in divs):
            errs.append("Divisas: códigos de 3 letras separados por comas (USD, EUR…)")
        c["divisas"] = ", ".join(divs)
        imp = [i for i in (nuevo.get("impactos") or []) if i in IMPACTOS]
        if not imp:
            errs.append("Marca al menos un nivel de impacto")
        c["impactos"] = imp or c["impactos"]
        entero("min_antes", 0, 240, "Minutos antes")
        entero("min_despues", 0, 240, "Minutos después")
        entero("grandes_antes", 0, 480, "Minutos antes (grandes)")
        entero("grandes_despues", 0, 480, "Minutos después (grandes)")
        entero("aviso_min", 0, 240, "Aviso previo")
        for k in ("grandes", "ignorar"):
            c[k] = ", ".join(x.strip() for x in re.split(r"[,;\n]", str(nuevo.get(k, c[k]))) if x.strip())[:400]
        if nuevo.get("sin_calendario", c["sin_calendario"]) not in ("operar", "bloquear"):
            errs.append("Sin calendario: «operar» o «bloquear»")
        else:
            c["sin_calendario"] = nuevo.get("sin_calendario", c["sin_calendario"])
        if isinstance(nuevo.get("bots_fuera"), list):
            c["bots_fuera"] = [str(x) for x in nuevo["bots_fuera"]][:50]
        if errs:
            return False, errs
        with self.lock:
            antes = self.cfg["activo"]
            self.cfg = c
            self._recalcular()
        _escribir(CONFIG, c)
        if antes != c["activo"]:
            self.hub.log("Filtro de noticias {}".format("activado" if c["activo"] else "desactivado"), "warn")
        return True, []

    def actualizar_ya(self):
        if time.time() - self.ultimo_intento < MIN_MANUAL_S:
            return False, "Espera unos minutos: el calendario se acaba de pedir (el feed bloquea si se insiste)."
        self.ultimo_intento = time.time()
        ok, info = self.descargar()
        return ok, ("{} eventos esta semana".format(info) if ok else "No se pudo: {}".format(info))

    def public(self):
        with self.lock:
            ahora = _ms()
            v = self.ventana_en(ahora) if self.cfg["activo"] else None
            prox = [w for w in self.ventanas if w["fin"] >= ahora][:25]
            return {"config": dict(self.cfg), "actualizado": self.actualizado, "error": self.error,
                    "caducado": self.caducado(), "n_semana": len(self.eventos),
                    "ahora": v, "proximas": prox}

    def texto(self, horas=48):
        p = self.public()
        tz = self.hub.tz
        c = p["config"]
        L = ["📰 <b>Noticias</b> · filtro {}".format("activo" if c["activo"] else "<b>desactivado</b>")]
        if p["caducado"]:
            L.append("⚠️ Calendario sin actualizar{}".format(" ({})".format(p["error"]) if p["error"] else ""))
        if p["ahora"]:
            L.append("⛔ Ahora sin entradas hasta las {}".format(
                datetime.fromtimestamp(p["ahora"]["fin"] / 1000, tz).strftime("%H:%M")))
        lim = _ms() + horas * 3_600_000
        prox = [w for w in p["proximas"] if w["ini"] <= lim]
        if not prox:
            L.append("Sin noticias del filtro ({} · impacto {}) en las próximas {} h.".format(
                c["divisas"] or "todas", "/".join(IMPACTO_ES[i] for i in c["impactos"]), horas))
        for w in prox:
            f = lambda ms, fmt="%H:%M": datetime.fromtimestamp(ms / 1000, tz).strftime(fmt)
            L.append("\n<b>{}</b> · sin entradas {}–{}".format(dia_hora(w["eventos"][0]["t"], tz), f(w["ini"]),
                                                             f(w["fin"])))
            for e in w["eventos"]:
                L.append("{}{} ({}, {})".format("⭐ " if e["grande"] else "· ", e["titulo"].replace("<", "&lt;"),
                                               e["divisa"], IMPACTO_ES[e["impacto"]]))
        return "\n".join(L)

    def linea_estado(self):
        """Una línea para /estado, o None si el filtro está desactivado."""
        p = self.public()
        if not p["config"]["activo"]:
            return None
        tz = self.hub.tz
        if p["ahora"]:
            return "📰 Noticia: sin entradas hasta las {}".format(
                datetime.fromtimestamp(p["ahora"]["fin"] / 1000, tz).strftime("%H:%M"))
        if p["proximas"]:
            w = p["proximas"][0]
            return "📰 Próxima noticia: {} · {}".format(
                dia_hora(w["eventos"][0]["t"], tz),
                w["eventos"][0]["titulo"].replace("<", "&lt;"))
        return "📰 Sin noticias del filtro esta semana" + (" (calendario sin actualizar)" if p["caducado"] else "")
