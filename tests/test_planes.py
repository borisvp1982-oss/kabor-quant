# SPDX-License-Identifier: Apache-2.0
"""Planes: un plan sigue siendo de su bot aunque el símbolo cambie de nombre (NAS100 → XYZ100)."""
import threading
import types
import unittest

import comun  # noqa: F401  (rutas)
import planes as PL

ALIASES = {"NAS100": ["NAS100", "US100", "XYZ100", "NDX", "NASDAQ"], "GOLD": ["GOLD", "XAU", "XAUUSD"]}


def bot(simbolo, tf, trades=(), pos=None):
    return types.SimpleNamespace(P={"estrategia": PL.ESTRATEGIA_ID, "symbol": simbolo, "timeframe": tf},
                                 lock=threading.Lock(), trades=list(trades), meta={},
                                 st=types.SimpleNamespace(pos=pos))


class Simbolos(unittest.TestCase):
    def setUp(self):
        self.antes = PL.EQUIVALENTES
        PL.EQUIVALENTES = {n: set(g) for g in ALIASES.values() for n in g}

    def tearDown(self):
        PL.EQUIVALENTES = self.antes

    def test_mismo_simbolo(self):
        self.assertTrue(PL.mismo_simbolo("NAS100", "XYZ100"))
        self.assertTrue(PL.mismo_simbolo("xyz100", "NAS100"))
        self.assertTrue(PL.mismo_simbolo("CRWV", "CRWV"))
        self.assertFalse(PL.mismo_simbolo("NAS100", "XAU"))
        self.assertFalse(PL.mismo_simbolo("CRWV", "NVDA"))

    def conector(self, *bots):
        hub = types.SimpleNamespace(bots={str(i): b for i, b in enumerate(bots)}, tz=None)
        return PL.ConectorMCP(hub, "x" * 32, "pruebas")

    def test_resultado_tras_cambiar_el_simbolo_del_bot(self):
        te = 1795518000000
        c = self.conector(bot("CRWV", "15m"),
                          bot("XYZ100", "15m", [{"t_open": te, "t_close": te + 1, "r": -1.012, "exit": 26251.7,
                                                 "why": "SL"}]))
        p = {"simbolo": "NAS100", "temporalidad": "15m", "t_entrada": te}
        r = c._resultado_plan(p)["resultado"]
        self.assertEqual((r["r"], r["motivo"]), (-1.01, "SL"))

    def test_resultado_abierta(self):
        te = 1795677300000
        c = self.conector(bot("XYZ100", "15m", pos={"t_open": te}))
        self.assertEqual(c._resultado_plan({"simbolo": "NAS100", "temporalidad": "15m", "t_entrada": te}),
                         {"resultado": {"abierta": True}})

    def test_respaldo_unico_por_vela(self):
        te = 1795000000000      # símbolo sin alias: vale la única operación de esa vela en esa temporalidad
        c = self.conector(bot("CRWV2", "15m", [{"t_open": te, "t_close": te, "r": 0.5, "why": "TP"}]))
        self.assertEqual(c._resultado_plan({"simbolo": "CRWV", "temporalidad": "15m", "t_entrada": te})
                         ["resultado"]["r"], 0.5)
        dos = self.conector(bot("AAA", "15m", [{"t_open": te, "r": 0.5}]), bot("BBB", "15m", [{"t_open": te, "r": 1}]))
        self.assertEqual(dos._resultado_plan({"simbolo": "CRWV", "temporalidad": "15m", "t_entrada": te}), {})

    def test_otra_temporalidad_no_cuenta(self):
        te = 1795518000000
        c = self.conector(bot("XYZ100", "5m", [{"t_open": te, "r": 1}]))
        self.assertEqual(c._resultado_plan({"simbolo": "NAS100", "temporalidad": "15m", "t_entrada": te}), {})


if __name__ == "__main__":
    unittest.main()
