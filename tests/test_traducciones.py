# SPDX-License-Identifier: Apache-2.0
"""Diccionarios del panel: JSON válido, sin textos vacíos y con los mismos huecos {0}, {1}… que el original."""
import json
import os
import re
import unittest

from comun import RAIZ

HUECO = re.compile(r"\{\d+\}")


def huecos_ok(k, v):
    """La traducción usa solo huecos del original y no pierde ninguno, salvo los pegados a una palabra (finales
    de plural como «cambio{1}», que en otro idioma pueden sobrar). Puede repetirlos."""
    hk, hv = set(HUECO.findall(k)), set(HUECO.findall(v))
    if not hv <= hk:
        return False
    return all(re.search(r"\w" + re.escape(h), k) for h in hk - hv)


class Diccionarios(unittest.TestCase):
    def test_en_y_fr(self):
        for lang in ("en", "fr"):
            with self.subTest(idioma=lang):
                with open(os.path.join(RAIZ, "web", "i18n", lang + ".json"), encoding="utf-8") as f:
                    d = json.load(f)
                self.assertGreater(len(d), 1000)
                malos = [k for k, v in d.items() if not isinstance(v, str) or not v.strip() or not huecos_ok(k, v)]
                self.assertEqual(malos, [], "textos vacíos o con huecos distintos")

    def test_mismas_claves_en_los_dos_idiomas(self):
        dd = {}
        for lang in ("en", "fr"):
            with open(os.path.join(RAIZ, "web", "i18n", lang + ".json"), encoding="utf-8") as f:
                dd[lang] = set(json.load(f))
        self.assertEqual(sorted(dd["en"] - dd["fr"]), [], "faltan en francés")
        self.assertEqual(sorted(dd["fr"] - dd["en"]), [], "faltan en inglés")


if __name__ == "__main__":
    unittest.main()
