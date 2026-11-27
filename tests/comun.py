# SPDX-License-Identifier: Apache-2.0
"""Utilidades de las pruebas: rutas, estrategias instaladas y velas sintéticas reproducibles."""
import math
import os
import random
import socket
import sys

RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if RAIZ not in sys.path:
    sys.path.insert(0, RAIZ)

import nucleo as K  # noqa: E402

if K.REGISTRY is None:
    K.REGISTRY = K.Registry(os.path.join(RAIZ, "estrategias"))
    K.REGISTRY.scan()

MIN = 60_000


def velas(dias=40, tf="5m", semilla=7, precio=60_000.0, inicio=1_767_225_600_000):
    """Paseo aleatorio con tendencias y volatilidad cambiantes (siempre las mismas con la misma semilla)."""
    rnd = random.Random(semilla)
    paso = K.TF_MS[tf]
    n = int(dias * 86_400_000 // paso)
    out, p, deriva = [], precio, 0.0
    for i in range(n):
        if i % 300 == 0:
            deriva = rnd.uniform(-0.0004, 0.0004)
        vol = 0.0015 * (1 + 0.6 * math.sin(i / 500.0))
        o = p
        c = o * (1 + deriva + rnd.gauss(0, vol))
        h = max(o, c) * (1 + abs(rnd.gauss(0, vol / 2)))
        lo = min(o, c) * (1 - abs(rnd.gauss(0, vol / 2)))
        out.append((inicio + i * paso, round(o, 2), round(h, 2), round(lo, 2), round(c, 2)))
        p = c
    return out


def agrupar(cs, tf_origen, tf_destino):
    """Velas de tf_origen agrupadas en tf_destino (como hace el bot)."""
    a, b = K.TF_MS[tf_origen], K.TF_MS[tf_destino]
    out, cur = [], None
    for t, o, h, lo, c in cs:
        t0 = t - t % b
        if cur and cur[0] == t0:
            cur = (t0, cur[1], max(cur[2], h), min(cur[3], lo), c)
        else:
            if cur:
                out.append(cur)
            cur = (t0, o, h, lo, c)
    if cur and (cur[0] + b) <= cs[-1][0] + a:
        out.append(cur)
    return out


def ajustes(estrategia, **extra):
    P, errs = K.validate(dict(K.defaults(estrategia), estrategia=estrategia, **extra))
    if errs:
        raise AssertionError("ajustes no válidos: {}".format(errs))
    return P


def puerto_libre():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p
