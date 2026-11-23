#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Borislav Vladimirov Petrakiev
"""
diagnostico.py -- Diagnóstico de KaBor Quant (bots v5.1). SOLO LECTURA: no toca ningún archivo ni envía órdenes.

Uso (en la carpeta del programa, con el servicio en marcha o parado, da igual):
    ./venv/bin/python diagnostico.py            # todos los bots
    ./venv/bin/python diagnostico.py 2          # solo el bot nº 2
    ./venv/bin/python diagnostico.py --dias 10  # ventana de comparación (por defecto 7 días)

Para cada bot comprueba, en este orden, todo lo que puede dejar a un bot sin señales:
  1. Disco lleno (si no se puede escribir, las señales no se anotan ni se avisan).
  2. Riesgo global, guardián, pausa manual, límite diario, posición o entrada colgada.
  3. Si llegan velas, y si son velas "vivas" (precio que se mueve) o planas (mercado muerto / retirado).
  4. El mercado al que está atado el bot y si sigue activo en el catálogo de MyFundedPerps.
  5. Qué ha pasado con las señales: emitidas, filtradas (y por qué), ignoradas, descartadas por coste.
  6. LA PRUEBA CLAVE: el mismo motor y la misma configuración del bot sobre velas recién descargadas de los
     últimos N días. Si ahí salen señales y en directo no -> fallo del bot en directo. Si tampoco salen -> es el
     mercado o la configuración, no el programa.
"""
import argparse
import csv
import json
import os
import shutil
import sys
import time
from collections import Counter
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import kcbot as KB  # noqa: E402  (el programa solo arranca con main(); importarlo no hace nada)
import nucleo as K  # noqa: E402

OK, MAL, AVISO, INFO = "[ok]", "[!!]", "[??]", "    "
hallazgos = []


def p(tag, msg, bot=None):
    print("{} {}".format(tag, msg))
    if tag in (MAL, AVISO):
        hallazgos.append((tag, (bot + ": " if bot else "") + msg))


def utc(ms):
    return datetime.fromtimestamp(ms / 1000, timezone.utc).strftime("%Y-%m-%d %H:%M UTC") if ms else "-"


def hace(ms):
    if not ms:
        return "nunca"
    m = (time.time() * 1000 - ms) / 60000
    return "hace {:.0f} min".format(m) if m < 120 else "hace {:.1f} h".format(m / 60) if m < 2880 \
        else "hace {:.1f} días".format(m / 1440)


def leer_json(path, defecto=None):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return defecto


def cola_csv(path, n):
    """Últimas n filas de un CSV (sin leerlo entero en memoria)."""
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8", errors="replace") as f:
        cab = f.readline().strip().split(",")
        f.seek(0, 2)
        fin = f.tell()
        trozo = min(fin, 400 * n + 4096)
        f.seek(fin - trozo)
        lineas = f.read().splitlines()[1:] if trozo < fin else f.read().splitlines()[1:]
    filas = list(csv.DictReader([",".join(cab)] + lineas[-n:]))
    return filas


# ---------------------------------------------------------------------------------------------------------
def general(cfg):
    print("\n==================== PROGRAMA ====================")
    du = shutil.disk_usage(cfg.data_dir)
    libre = du.free / 1e9
    pct = 100 * du.used / du.total
    if libre < 0.3 or pct > 97:
        p(MAL, "Disco casi lleno: {:.2f} GB libres ({:.0f} % usado). Si no se puede escribir, el bot pierde "
               "las señales sin avisar.".format(libre, pct))
    else:
        p(OK, "Disco: {:.1f} GB libres ({:.0f} % usado)".format(libre, pct))
    tam = 0
    for raiz, _, files in os.walk(cfg.data_dir):
        for f in files:
            try:
                tam += os.path.getsize(os.path.join(raiz, f))
            except OSError:
                pass
    p(INFO, "Carpeta data: {:.0f} MB".format(tam / 1e6))
    p(INFO, "Ejecución en sandbox (FP_EXEC): {}".format("ACTIVADA" if cfg.exec else "desactivada (todo en papel)"))

    rg = leer_json("riesgo_global.json", {}) or {}
    c, g = rg.get("config") or {}, rg.get("guard")
    if not c.get("activo"):
        p(OK, "Riesgo global: desactivado")
    elif g:
        p(MAL, "Riesgo global ACTIVO Y DISPARADO: {} {} de {} {} (acción «{}»). Ningún bot abre operaciones "
               "{}.".format(g.get("kind"), "diaria" if g.get("scope") == "día" else "semanal", g.get("value"),
                            g.get("unit"), g.get("action"),
                            "hasta que pulses «Reactivar todos los bots»" if g.get("action") == "parar"
                            else "hasta el periodo siguiente"))
    else:
        p(OK, "Riesgo global activo, sin límite alcanzado (máx. abiertas: {})".format(c.get("max_abiertas") or "sin límite"))

    av = leer_json("avisos.json", {}) or {}
    if av.get("activo") and av.get("token") and av.get("chat_id"):
        p(OK, "Avisos de Telegram activados")
    else:
        p(AVISO, "Avisos de Telegram DESACTIVADOS: aunque hubiera señales, no te llegarían")

    errs = []
    d = "logs"
    if os.path.isdir(d):
        for f in sorted(os.listdir(d))[-3:]:
            try:
                with open(os.path.join(d, f), encoding="utf-8", errors="replace") as fh:
                    errs += [x.rstrip() for x in fh if "[ERROR]" in x or "[WARN ]" in x]
            except OSError:
                pass
    if errs:
        p(AVISO, "Últimos errores/avisos del programa ({} en los 3 últimos días):".format(len(errs)))
        for x in errs[-8:]:
            print("       " + x[:200])


# ---------------------------------------------------------------------------------------------------------
def bot(meta, cfg, req, catalogo, dias, hour_fn):
    bid, nombre = meta["id"], meta.get("name", meta["id"])
    carpeta = os.path.join("bots", bid)
    print("\n==================== BOT {} · {} ====================".format(bid, nombre))
    if meta.get("archived"):
        p(INFO, "Archivado: no busca entradas (se omite).")
        return
    conf = leer_json(os.path.join(carpeta, "config.json"))
    if not conf:
        p(MAL, "No puedo leer config.json", nombre)
        return
    P, errs = K.validate(conf.get("params"))
    if errs:
        p(MAL, "La configuración no valida: {}".format("; ".join(errs)), nombre)
    sym, tf, sid = P["symbol"], P["timeframe"], P.get("estrategia")
    tf_ms = K.TF_MS[tf]
    p(INFO, "Estrategia {} · {} {} · configuración nº {} · modo {}".format(sid, sym, tf, conf.get("id"),
                                                                       P.get("ex_modo", "papel")))
    if not K.REGISTRY.get(sid):
        p(MAL, "La estrategia «{}» no está instalada o tiene un error: el bot no puede funcionar".format(sid), nombre)
        return

    # --- filtros comunes que pueden matar todas las señales ---
    filtros = []
    if P["direction"] != "ambos":
        filtros.append(P["direction"])
    if P["hr_on"]:
        filtros.append("horario {}-{} h{}".format(P["hr_from"], P["hr_to"], ", solo L-V" if P["hr_weekdays"] else ""))
    if P["tr_mode"] != "no":
        filtros.append("tendencia {} en {}".format(P["tr_mode"], P["tr_tf"]))
    if P["sl_min_atr"]:
        filtros.append("stop mín. {} ATR".format(P["sl_min_atr"]))
    if P["sl_max_atr"]:
        filtros.append("stop máx. {} ATR".format(P["sl_max_atr"]))
    if P["cooldown"]:
        filtros.append("espera {} velas".format(P["cooldown"]))
    p(INFO, "Filtros activos: {} · coste máx. {} R".format(", ".join(filtros) or "ninguno", P["max_cost"]))

    # --- estado guardado ---
    est = leer_json(os.path.join(carpeta, "estado.json"), {}) or {}
    st = est.get("strat") or {}
    last_t = est.get("last_t") or 0
    edad = time.time() * 1000 - (last_t + tf_ms) if last_t else None
    if not last_t:
        p(MAL, "El bot no ha procesado ninguna vela (estado.json vacío)", nombre)
    elif edad > max(3 * tf_ms + 60_000, 30 * 60_000):
        p(MAL, "Última vela procesada: {} ({}). El bot NO está recibiendo velas.".format(utc(last_t), hace(last_t)),
          nombre)
    else:
        p(OK, "Última vela procesada: {} ({})".format(utc(last_t), hace(last_t)))
    if est.get("saved_at") and time.time() * 1000 - est["saved_at"] > max(3 * tf_ms, 30 * 60_000) and last_t \
            and edad is not None and edad < 3 * tf_ms:
        p(MAL, "estado.json no se guarda desde {}: ¿disco lleno o error al escribir?".format(hace(est["saved_at"])),
          nombre)
    if est.get("paused"):
        p(MAL, "Entradas PAUSADAS A MANO desde el panel (todas las señales se ignoran)", nombre)
    g = est.get("guard")
    if g:
        p(MAL, "GUARDIÁN DISPARADO: {} {} de {:+} {} (límite {}), acción «{}». {}".format(
            g.get("kind"), "diaria" if g.get("scope") == "día" else "semanal", g.get("value"), g.get("unit"),
            g.get("limit"), g.get("action"),
            "No se reactiva solo: pulsa «Reactivar» en el panel del bot." if g.get("action") == "parar"
            else "Se reanuda solo en el periodo siguiente."), nombre)
    if st.get("blocked"):
        p(AVISO, "Límite diario de la estrategia alcanzado hoy: {}".format(st["blocked"]), nombre)
    pos = st.get("pos")
    if pos:
        abierta = hace(pos.get("t_open"))
        lado = "LONG" if pos.get("side") == 1 else "SHORT"
        tag = MAL if pos.get("t_open") and time.time() * 1000 - pos["t_open"] > 2 * 86_400_000 else AVISO
        p(tag, "Hay una operación {} abierta desde {} ({} velas): entrada {}, SL {}, TP {}. Mientras siga abierta "
               "el bot NO da señales nuevas.".format(lado, abierta, pos.get("bars"), pos.get("entry"), pos.get("sl"),
                                                     pos.get("tp")), nombre)
    if st.get("pending"):
        p(AVISO, "Entrada pendiente guardada: {}".format(st["pending"]), nombre)
    armed = (st.get("estrategia") or {}).get("armed")
    if armed:
        p(INFO, "Lados armados de la estrategia: largo={} corto={}".format(armed.get("1"), armed.get("-1")))
    cnt = est.get("cnt") or {}
    p(INFO, "Contadores desde el arranque: señales {}, ignoradas {}, filtradas {}, coste {}, errores {}, "
            "huecos {}".format(cnt.get("signals"), cnt.get("ignored"), sum((cnt.get("filtered") or {}).values()),
                               cnt.get("skipped_cost"), cnt.get("errors"), cnt.get("gaps")))

    # --- eventos de los últimos días ---
    desde = time.time() * 1000 - dias * 86_400_000
    evs = [e for e in cola_csv(os.path.join(carpeta, "eventos.csv"), 4000) if int(e.get("ms") or 0) >= desde]
    tipos = Counter(e["evento"] for e in evs)
    p(INFO, "Eventos en los últimos {} días: {}".format(dias, dict(tipos) or "NINGUNO"))
    motivos = Counter(e["detalle"] for e in evs if e["evento"] in ("señal_filtrada", "señal_ignorada"))
    if motivos:
        p(AVISO, "Señales que se quedaron por el camino: {}".format(dict(motivos.most_common(6))), nombre)
    for e in evs[-6:]:
        print("       {} {:<18} {} {}".format(e["ts_vela_utc"], e["evento"], e["lado"], e["detalle"][:90]))

    # --- velas guardadas: ¿vivas o planas? ---
    vel = cola_csv(os.path.join(carpeta, "velas.csv"), 600)
    if vel:
        try:
            hl = [(float(v["high"]), float(v["low"]), float(v["close"])) for v in vel]
            planas = sum(1 for h, l_, _ in hl if h == l_)
            cierres = len(set(c for _, _, c in hl[-288:]))
            p(INFO, "velas.csv: {} últimas velas, {:.0f} % planas (máximo = mínimo), {} cierres distintos en las "
                    "últimas 288".format(len(hl), 100 * planas / len(hl), cierres))
            if planas / len(hl) > 0.3 or cierres < 20:
                p(MAL, "Las velas recientes están PLANAS o casi: el mercado no cotiza (retirado, en «solo reducir» o "
                       "sin liquidez). Con velas planas el ATR se hunde y la estrategia no puede dar señales.", nombre)
            ts = [int(v["ms"]) for v in vel]
            huecos = sum(1 for a, b in zip(ts, ts[1:]) if b - a > tf_ms * 3)
            if huecos > 5:
                p(AVISO, "{} huecos de más de 3 velas en las últimas {} velas".format(huecos, len(ts)), nombre)
        except (KeyError, ValueError) as e:
            p(AVISO, "No pude leer velas.csv: {}".format(e), nombre)

    # --- el mercado ---
    ins = next((i for i in leer_json("instrumentos.json", []) or [] if i.get("symbol") == sym), None)
    mid = (ins or {}).get("market_id")
    p(INFO, "Instrumento {}: mercado {} (proveedor {}, coin {})".format(sym, mid, (ins or {}).get("provider"),
                                                                       (ins or {}).get("coin")))
    if catalogo is not None and mid:
        m = next((x for x in catalogo if x.get("market_id") == mid), None)
        if m is None:
            p(MAL, "El mercado {} YA NO ESTÁ en el catálogo de MyFundedPerps".format(mid), nombre)
        else:
            extra = {k: m.get(k) for k in ("trading_enabled", "status", "reduce_only", "is_delisted", "delisted")
                     if k in m}
            if m.get("trading_enabled") is False or m.get("reduce_only") or m.get("is_delisted") or m.get("delisted"):
                p(MAL, "El mercado {} está desactivado / solo reducir en el catálogo: {}".format(mid, extra), nombre)
            else:
                p(OK, "Mercado {} activo en el catálogo {}".format(mid, extra or ""))
        alt = [x.get("market_id") for x in catalogo
               if (set(KB.ALIASES.get(sym, [sym])) & {str(x.get("symbol", "")).upper(), str(x.get("coin", "")).upper(),
                                                     str(x.get("coin", "")).upper().split(":")[-1]})]
        if len(alt) > 1:
            p(INFO, "Otros mercados del catálogo para {}: {}".format(sym, ", ".join(str(a) for a in alt)))

    # --- prueba clave: mismo motor, misma configuración, velas frescas ---
    if req is None:
        p(AVISO, "Sin conexión con MyFundedPerps: no puedo hacer la comparación con velas frescas", nombre)
        return
    try:
        market = {k: (ins or {}).get(k) for k in KB.MKT_KEYS} if mid else None
        if not market or not market.get("provider"):
            raise RuntimeError("el instrumento no tiene mercado asignado todavía")
        warm = max(600, K.warmup_bars(P))
        n = warm + int(dias * 86_400_000 / tf_ms)
        cs = req.candles(market, tf, n)
        if not cs:
            p(MAL, "MyFundedPerps no devuelve velas de {} {} ahora mismo".format(mid, tf), nombre)
            return
        p(INFO, "Velas frescas de MFP: {} (última {} -> {})".format(len(cs), utc(cs[-1][0]), hace(cs[-1][0] + tf_ms)))
        if time.time() * 1000 - (cs[-1][0] + tf_ms) > 3 * tf_ms + 60_000:
            p(MAL, "La API tampoco tiene velas recientes de {}: el mercado no cotiza o está retirado".format(mid), nombre)
        reciente = cs[-288:]
        planas = sum(1 for c in reciente if c[2] == c[3])
        if reciente and planas / len(reciente) > 0.3:
            p(MAL, "{:.0f} % de las velas recientes de la API son planas: mercado muerto".format(
                100 * planas / len(reciente)), nombre)
        htf = None
        if P["tr_mode"] != "no":
            hn = int(n * tf_ms / K.TF_MS[P["tr_tf"]]) + 4 * max(P["tr_rsi_len"], P["tr_ema_len"]) + 10
            htf = req.candles(market, P["tr_tf"], hn)
        w = max(0, len(cs) - int(dias * 86_400_000 / tf_ms))
        trades, info = K.backtest(cs, P, hour_fn, htf, warm=w, instrument_cost=(ins or {}).get("cost_pct"))
        senales_live = tipos.get("señal", 0) + tipos.get("señal_ignorada", 0)
        print("       Backtest de los últimos {} días con la configuración EXACTA del bot: {} señales, {} "
              "operaciones, filtradas {}, por coste {}, días bloqueados {}".format(
                  dias, info["signals"], len(trades), info["filtered"] or 0, info["skipped_cost"],
                  info["block_days"] or 0))
        print("       En directo, mismo periodo: {} señales".format(senales_live))
        if info["signals"] > 0 and senales_live == 0:
            p(MAL, "EL BACKTEST DA {} SEÑALES Y EL BOT EN DIRECTO NINGUNA en los mismos días: el fallo está en el bot "
                   "en directo (velas que no llegan, estado colgado o error al escribir).".format(info["signals"]),
              nombre)
        elif info["signals"] == 0 and sum(info["filtered"].values()) > 0:
            p(MAL, "La estrategia SÍ ve oportunidades, pero los filtros las eliminan todas: {}".format(info["filtered"]),
              nombre)
        elif info["signals"] == 0:
            p(AVISO, "Con velas reales de estos días la estrategia tampoco da señales: no es un fallo del programa, "
                     "es el mercado (o la configuración).", nombre)
        else:
            p(OK, "Backtest y directo coinciden en lo esencial")
    except Exception as e:
        p(AVISO, "No pude hacer la comparación con velas frescas: {}".format(e), nombre)


# ---------------------------------------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("bot", nargs="?", help="número de bot (opcional)")
    ap.add_argument("--dias", type=float, default=7)
    ap.add_argument("--sin-red", action="store_true", help="no consultar MyFundedPerps")
    a = ap.parse_args()
    for k, v in KB.load_env_file(os.path.join(HERE, "config.env")).items():
        os.environ.setdefault(k, v)
    cfg = KB.Cfg()
    K.REGISTRY = K.Registry(KB.STRAT_DIR)
    K.REGISTRY.scan()
    if not os.path.isdir(cfg.data_dir):
        sys.exit("No encuentro la carpeta de datos {}".format(cfg.data_dir))
    os.chdir(cfg.data_dir)
    print("Diagnóstico KaBor Quant · {} · programa v{}".format(datetime.now().strftime("%Y-%m-%d %H:%M"), KB.VERSION))
    for pl in K.REGISTRY.list():
        if pl.error:
            p(MAL, "Estrategia {} con error de carga: {}".format(pl.file, pl.error))
    general(cfg)

    req, catalogo = None, None
    if not a.sin_red:
        silencio = lambda *x, **k: None
        try:
            catalogo = KB.as_list(KB.MFPRest(cfg.host, cfg.key).get("/v1/markets", auth=False))
        except Exception as e:
            p(AVISO, "No pude leer el catálogo de MyFundedPerps: {}".format(e))
            catalogo = leer_json("mercados.json")
        try:
            req = KB.MFPStream(silencio)
            req.server_now()
        except Exception as e:
            p(AVISO, "No pude conectar con el WebSocket de datos de MFP: {}".format(e))
            req = None
    hour_fn = KB.make_hour_fn(KB.get_tz(cfg.local_tz))
    metas = leer_json("bots.json", []) or []
    if a.bot:
        metas = [m for m in metas if str(m["id"]) == str(a.bot)]
    for m in metas:
        try:
            bot(m, cfg, req, catalogo, a.dias, hour_fn)
        except Exception as e:
            p(AVISO, "Fallo al revisar el bot {}: {}: {}".format(m.get("id"), type(e).__name__, e))

    print("\n==================== RESUMEN ====================")
    if not hallazgos:
        print("No he encontrado nada raro.")
    for tag, msg in hallazgos:
        print("{} {}".format(tag, msg))


if __name__ == "__main__":
    main()
