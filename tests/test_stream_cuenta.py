# SPDX-License-Identifier: Apache-2.0
"""Stream de la cuenta contra un sandbox simulado en local: la entrada, la venta parcial y el cierre no gastan
lecturas REST; si el stream se corta, KaBor pregunta a la API y luego reanuda. Necesita el paquete `websockets`."""
import time
import unittest

from comun import puerto_libre
import ejecucion as E

try:
    import websockets  # noqa: F401
    import mfp_simulado as F
except ImportError:
    F = None

MK = "binance|BTCUSDT"


@unittest.skipIf(F is None, "falta el paquete websockets (pip install websockets) para el sandbox simulado")
class StreamCuenta(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        rest, ws = puerto_libre(), puerto_libre()
        F.arrancar(rest, ws)
        E.STREAM_PRUEBAS = "ws://127.0.0.1:%d/v1/sandbox/stream" % ws
        cls.key = "fp_test_stream%d" % time.time_ns()
        cls.host = "http://127.0.0.1:%d" % rest
        cls.tr = E.MFPTrading(cls.host, cls.key, 10, "pruebas")
        cls.tr.stream = E.stream_para(cls.key, cls.host, "pruebas")
        t0 = time.time()
        while not cls.tr.stream.al_dia() and time.time() - t0 < 10:
            time.sleep(0.1)

    @classmethod
    def tearDownClass(cls):
        E.streams_en_uso(set())
        E.STREAM_PRUEBAS = None

    def test_1_conecta(self):
        self.assertTrue(self.tr.stream.al_dia(), self.tr.stream.public())

    def test_2_entrada_parcial_y_cierre_sin_lecturas_rest(self):
        tr = self.tr
        g0 = time.time()
        self.assertIsNone(tr.position("acc1", MK))
        o = tr.wait_order(tr.place({"client_order_id": "c1", "type": "market", "account_id": "acc1", "market_id": MK,
                                    "side": "buy", "size": 0.01, "stop_loss_price": 59000,
                                    "take_profit_price": 62000}, "c1"))
        self.assertEqual(o.get("status"), "filled")
        rp = tr.position("acc1", MK)
        self.assertTrue(rp and abs(rp["size"] - 0.01) < 1e-9)
        self.assertEqual(len(tr.working_exits("acc1", MK)), 2)
        tr.wait_order(tr.close(rp["id"], {"size": 0.004}, "p1"))
        self.assertAlmostEqual(tr.position("acc1", MK)["size"], 0.006, places=9)
        tr.wait_order(tr.close(rp["id"], {}, "c2"))
        self.assertIsNone(tr.position("acc1", MK))
        cerrada = tr.closed_position("acc1", rp["id"])
        self.assertEqual(cerrada and cerrada.get("close_reason"), "manual")
        self.assertEqual(tr.working_exits("acc1", MK), [])
        self.assertEqual(F.gets(g0, self.key), [], "el stream debería ahorrar todas las lecturas")

    def test_3_corte_lectura_rest_y_reanudacion(self):
        tr = self.tr
        F.cortar_ws()
        time.sleep(0.4)
        self.assertFalse(tr.stream.al_dia())
        g0 = time.time()
        tr.position("acc1", MK)
        self.assertEqual(len(F.gets(g0, self.key)), 1, "sin stream se pregunta a la API")
        t0 = time.time()
        while not tr.stream.al_dia() and time.time() - t0 < 15:
            time.sleep(0.1)
        self.assertTrue(tr.stream.al_dia())
        self.assertGreaterEqual(F.STATS["resumes"], 1)


if __name__ == "__main__":
    unittest.main()
