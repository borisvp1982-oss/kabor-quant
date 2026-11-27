# SPDX-License-Identifier: Apache-2.0
"""Conector MCP (SMT): protocolo, herramientas con sus anotaciones y token."""
import unittest

import comun  # noqa: F401  (rutas)
import planes as PL

SOLO_LECTURA = {"kabor_contexto", "kabor_velas", "kabor_dibujos", "kabor_planes", "kabor_bots", "kabor_operaciones",
                "kabor_estadisticas"}


class Conector(unittest.TestCase):
    def setUp(self):
        self.c = PL.ConectorMCP(None, "x" * 32, "pruebas")

    def llamar(self, met, par=None, mid=1):
        return self.c.atender({"jsonrpc": "2.0", "id": mid, "method": met, "params": par or {}})

    def test_initialize(self):
        r = self.llamar("initialize", {"protocolVersion": "2025-06-18"})["result"]
        self.assertEqual(r["protocolVersion"], "2025-06-18")
        self.assertEqual(r["serverInfo"]["name"], "kabor-quant")
        self.assertTrue(r["instructions"])

    def test_herramientas_con_anotaciones(self):
        tools = self.llamar("tools/list")["result"]["tools"]
        nombres = {t["name"] for t in tools}
        self.assertEqual(nombres, SOLO_LECTURA | {"kabor_crear_plan", "kabor_cancelar_pendiente"})
        for t in tools:
            with self.subTest(herramienta=t["name"]):
                a = t["annotations"]
                for k in ("title", "readOnlyHint", "destructiveHint", "idempotentHint", "openWorldHint"):
                    self.assertIn(k, a)
                self.assertEqual(a["readOnlyHint"], t["name"] in SOLO_LECTURA)
                self.assertFalse(a["openWorldHint"])
                self.assertEqual(t["inputSchema"]["type"], "object")
                self.assertFalse(t["inputSchema"]["additionalProperties"])
        crear = next(t for t in tools if t["name"] == "kabor_crear_plan")["annotations"]
        self.assertFalse(crear["destructiveHint"])
        cancelar = next(t for t in tools if t["name"] == "kabor_cancelar_pendiente")["annotations"]
        self.assertTrue(cancelar["destructiveHint"])

    def test_notificaciones_y_errores(self):
        self.assertIsNone(self.c.atender({"jsonrpc": "2.0", "method": "notifications/initialized"}))
        self.assertIn("error", self.c.atender({"id": 1, "method": "ping"}))
        self.assertIn("error", self.llamar("metodo/inexistente"))

    def test_token(self):
        self.assertTrue(self.c.autorizado("x" * 32))
        self.assertFalse(self.c.autorizado("y" * 32))
        self.assertFalse(PL.ConectorMCP(None, "corto", "v").autorizado("corto"))


if __name__ == "__main__":
    unittest.main()
