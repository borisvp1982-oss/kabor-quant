# SPDX-License-Identifier: Apache-2.0
"""Motor y backtest: estrategias instaladas, reproducibilidad, una operación a la vez, varias estrategias por bot,
temporalidad y dirección de cada estrategia."""
import math
import unittest

from comun import K, ajustes, agrupar, velas

COMBINADAS = ["rsi_niveles", "keltner_ruptura"]


def solapes(trades):
    ts = sorted((x["t_open"], x["t_close"]) for x in trades)
    return sum(1 for a, b in zip(ts, ts[1:]) if b[0] < a[1])


class Estrategias(unittest.TestCase):
    def test_todas_cargan_y_sus_valores_por_defecto_son_validos(self):
        ids = list(K.REGISTRY.items)
        self.assertGreater(len(ids), 5)
        for sid in ids:
            with self.subTest(estrategia=sid):
                pl = K.plugin_for(sid)
                self.assertFalse(getattr(pl, "error", None), getattr(pl, "error", None))
                P, errs = K.validate(dict(K.defaults(sid), estrategia=sid))
                self.assertEqual(errs, [])


class Backtest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cs = velas(dias=45, tf="15m")

    def test_reproducible_y_sin_operaciones_solapadas(self):
        P = ajustes("cruce_emas", symbol="BTC", timeframe="15m")
        t1, _ = K.backtest(self.cs, P, warm=300)
        t2, _ = K.backtest(self.cs, P, warm=300)
        self.assertGreater(len(t1), 5, "la prueba necesita operaciones")
        self.assertEqual([(x["t_open"], round(x["r"], 6)) for x in t1], [(x["t_open"], round(x["r"], 6)) for x in t2])
        self.assertEqual(solapes(t1), 0)
        for x in t1:
            self.assertTrue(math.isfinite(x["r"]))
            self.assertIn(x["side"], (1, -1))
            self.assertLessEqual(x["t_open"], x["t_close"])

    def test_metricas(self):
        P = ajustes("cruce_emas", symbol="BTC", timeframe="15m")
        t, _ = K.backtest(self.cs, P, warm=300)
        m = K.metrics(t)
        self.assertEqual(m["n"], len(t))
        self.assertAlmostEqual(m["total_r"], sum(x["r"] for x in t), places=6)


class VariasEstrategias(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cs = velas(dias=45, tf="15m", semilla=11)
        cls.P = ajustes("cruce_emas", symbol="BTC", timeframe="15m", combinar=COMBINADAS)

    def test_una_operacion_a_la_vez_y_cada_una_con_su_duena(self):
        t, info = K.backtest(self.cs, self.P, warm=400, registro=True)
        self.assertGreater(len(t), 5)
        self.assertEqual(solapes(t), 0, "dos operaciones abiertas a la vez")
        partes = K.partes_de(self.P)
        self.assertEqual(partes[0], "cruce_emas")
        for x in t:
            self.assertIn(x.get("estrategia"), partes)
        # las señales bloqueadas lo fueron por una operación abierta
        for s in info["registro"]:
            self.assertIn(s[3], ("entra", "ocupada", "filtro", "limite"))

    def test_direccion_de_cada_estrategia(self):
        Q = dict(self.P)
        Q[K.clave_sub("rsi_niveles", "direction")] = "solo cortos"
        Q, errs = K.validate(Q)
        self.assertEqual(errs, [])
        t, info = K.backtest(self.cs, Q, warm=400, registro=True)
        self.assertFalse([x for x in t if x.get("estrategia") == "rsi_niveles" and x["side"] == 1],
                         "rsi_niveles abrió largos estando en «solo cortos»")
        filtradas = [s for s in info["registro"] if s[1] == "rsi_niveles" and s[2] == 1 and s[3] == "filtro"]
        self.assertTrue(all(s[4] == "direction" for s in filtradas))

    def test_ajustes_de_una_estrategia_sola_y_sin_ella(self):
        sola = K.ajustes_de(self.P, "rsi_niveles")
        self.assertEqual(sola["estrategia"], "rsi_niveles")
        self.assertEqual(sola["combinar"], [])
        sin = K.sin_estrategia(self.P, "keltner_ruptura")
        self.assertEqual(K.partes_de(sin), ["cruce_emas", "rsi_niveles"])
        sin_principal = K.sin_estrategia(self.P, "cruce_emas")
        self.assertEqual(K.partes_de(sin_principal)[0], "rsi_niveles")
        for Q in (sola, sin, sin_principal):
            self.assertEqual(K.validate(Q)[1], [])


class Temporalidades(unittest.TestCase):
    def test_estrategia_de_30m_en_un_bot_de_5m_igual_que_en_un_bot_de_30m(self):
        cs5 = velas(dias=30, tf="5m", semilla=3)
        cs30 = agrupar(cs5, "5m", "30m")
        P30 = ajustes("cruce_emas", symbol="BTC", timeframe="30m")
        t30, _ = K.backtest(cs30, P30, warm=200)
        P5 = ajustes("cruce_emas", symbol="BTC", timeframe="5m", tf_principal="30m")
        t5, _ = K.backtest(cs5, P5, warm=1200)
        self.assertGreaterEqual(len(t30), 5)
        self.assertEqual([(x["t_open"], x["side"], x["why"]) for x in t5], [(x["t_open"], x["side"], x["why"]) for x in t30],
                         "las operaciones no coinciden")


if __name__ == "__main__":
    unittest.main()
