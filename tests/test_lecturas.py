# SPDX-License-Identifier: Apache-2.0
"""Límite de lecturas por clave y respuesta a los avisos de MyFundedPerps (429, Retry-After, X-RateLimit-Warning),
contra un servidor HTTP local."""
import json
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from comun import puerto_libre
import ejecucion as E

ESTADO = {"429": 0, "aviso": False, "gets": 0}


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        ESTADO["gets"] += 1
        if ESTADO["429"] > 0:
            ESTADO["429"] -= 1
            b = json.dumps({"error": {"code": "rate_limited", "message": "slow down"}}).encode()
            self.send_response(429)
            self.send_header("Retry-After", "1")
            self.send_header("X-RateLimit-Scope", "api_key")
        else:
            b = json.dumps({"data": {"id": "acc1"}}).encode()
            self.send_response(200)
            if ESTADO["aviso"]:
                self.send_header("X-RateLimit-Warning", "see /v1/stream")
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b)


class Limitador(unittest.TestCase):
    def test_respeta_el_tope_y_espera_a_que_haya_sitio(self):
        lim = E.Limitador("test")
        ahora = time.time()
        lim.t.extend([ahora - 59.0] * E.GET_TOPE["alta"])      # tope lleno; el primero sale en ~1 s
        t0 = time.time()
        self.assertTrue(lim.pedir())
        self.assertGreater(time.time() - t0, 0.7)
        self.assertEqual(lim.public()["esperas_dia"], 1)

    def test_prioridad_baja_deja_sitio_a_lo_de_operar(self):
        lim = E.Limitador("test")
        ahora = time.time()
        lim.t.extend([ahora] * E.GET_TOPE["baja"])
        with E.prioridad_baja():
            self.assertFalse(lim.pedir(espera_max=0.2))       # la baja no entra
        self.assertTrue(lim.pedir(espera_max=0.2))            # la de operar sí

    def test_aviso_de_mfp_una_vez_por_hora(self):
        lim = E.Limitador("test")
        self.assertTrue(lim.aviso("/v1/positions"))
        self.assertFalse(lim.aviso("/v1/positions"))
        self.assertEqual(lim.public()["avisos_dia"], 2)


class ClienteREST(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.puerto = puerto_libre()
        cls.srv = ThreadingHTTPServer(("127.0.0.1", cls.puerto), H)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def setUp(self):
        self.tr = E.MFPTrading("http://127.0.0.1:%d" % self.puerto, "fp_test_lecturas%d" % time.time_ns(), 5)
        self.avisos = []
        E.AL_AVISO = lambda fin, txt: self.avisos.append(txt)

    def tearDown(self):
        E.AL_AVISO = None
        ESTADO.update({"429": 0, "aviso": False})

    def test_429_espera_el_retry_after_y_reintenta(self):
        ESTADO["429"] = 1
        t0 = time.time()
        a = self.tr.account("acc1")
        self.assertEqual(a.get("id"), "acc1")
        self.assertGreaterEqual(time.time() - t0, 0.9)
        st = self.tr.lim.public()
        self.assertEqual(st["r429_dia"], 1)
        self.assertTrue(self.avisos)

    def test_x_ratelimit_warning_se_anota_y_avisa_una_vez(self):
        ESTADO["aviso"] = True
        for _ in range(3):
            self.tr.account("acc1")
        self.assertEqual(self.tr.lim.public()["avisos_dia"], 3)
        self.assertEqual(len(self.avisos), 1)

    def test_solo_sandbox(self):
        with self.assertRaises(ValueError):
            E.MFPTrading("https://api.myfundedperpetuals.com", "fp_test_x" * 3, 5)
        with self.assertRaises(ValueError):
            E.MFPTrading(E.SANDBOX_HOST, "fp_" + "live_" + "x" * 16, 5)   # clave falsa, partida para el escáner


if __name__ == "__main__":
    unittest.main()
