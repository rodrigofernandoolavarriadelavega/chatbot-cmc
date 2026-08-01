"""Tests de app/eco_orden_ocr.py — decisión sobre órdenes leídas por visión.

Los casos replican las 17 órdenes REALES de la práctica del 2026-08-01
(19 imágenes de producción, 90 días). No llaman a la API: prueban
`decidir_accion()`, que es pura, con las extracciones que la visión produjo.

Uso:
  pytest tests/test_eco_orden_ocr.py -v
  python tests/test_eco_orden_ocr.py
"""
from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))

os.environ.setdefault("SQLCIPHER_KEY", "")

from eco_orden_ocr import decidir_accion, msg_oferta, MSG_OBSTETRICA  # noqa: E402


def _orden(examenes, confianza="alta"):
    return {"tipo_documento": "orden_medica",
            "examenes_solicitados": examenes,
            "confianza": confianza}


class TestOrdenesRealesPardo(unittest.TestCase):
    """Órdenes del corpus que deben terminar en oferta de agenda con Pardo (68)."""

    CASOS = [
        "Ecotomografía mamaria bilateral",              # matrona manuscrita
        "Ecografía rodilla izquierda",                  # Hosp. Arauco
        "Ecografía de partes blandas de pierna derecha",
        "Ecotomografía cervical",
        "Eco hombro izquierdo",                         # Hosp. Curanilahue
        "Ecografía partes blandas región inguinal izquierda",
        "Ecografía renal y de vías urinarias",          # receta Clínica NES
        "Ecotomografía partes blandas región lumbosacra",
        "Ecotomografía vésico prostática",              # orden CMC
        "Ecografía mano derecha y muñeca",              # Hosp. Arauco
    ]

    def test_todas_ofrecen_agenda_con_pardo(self):
        for examen in self.CASOS:
            dec = decidir_accion(_orden([examen]))
            self.assertEqual(dec["accion"], "ofrecer_agenda", f"{examen!r} → {dec}")
            self.assertEqual(dec["routing"]["id_profesional"], 68, examen)

    def test_oferta_tiene_boton_y_precio(self):
        dec = decidir_accion(_orden(["Ecotomografía mamaria bilateral"]))
        msg = msg_oferta(dec["tipo_texto"], dec["routing"])
        body = msg["interactive"]["body"]["text"]
        self.assertIn("mamaria", body.lower())
        self.assertIn("David Pardo", body)
        self.assertIn("$40.000", body)
        ids = [b["reply"]["id"] for b in msg["interactive"]["action"]["buttons"]]
        self.assertIn("agendar_sugerido", ids)


class TestOrdenesRealesRejon(unittest.TestCase):
    def test_pelvica_ginecologica_a_rejon(self):
        # matrona Sarai Gómez, corpus real
        dec = decidir_accion(_orden(["Ecografía pélvica ginecológica"]))
        self.assertEqual(dec["accion"], "ofrecer_agenda")
        self.assertEqual(dec["routing"]["id_profesional"], 61)


class TestObstetrica(unittest.TestCase):
    def test_checklist_obstetrica_no_disponible(self):
        # CESFAM Curanilahue: X en "Ecografía obstétrica" (Emb 7+2)
        dec = decidir_accion(_orden(["Ecografía obstétrica"]))
        self.assertEqual(dec["accion"], "obstetrica")

    def test_transvaginal_obstetrica_no_disponible(self):
        # matrona Skarmeta: "Ecografía Transvaginal Obstétrica", embarazo inicial
        dec = decidir_accion(_orden(["Ecografía transvaginal obstétrica"]))
        self.assertEqual(dec["accion"], "obstetrica")
        self.assertIn("no realizamos", MSG_OBSTETRICA)


class TestCaenARecepcion(unittest.TestCase):
    def test_comprobante_de_pago(self):
        # corpus real: transferencia $7.880 en contexto eco
        dec = decidir_accion({"tipo_documento": "comprobante_pago",
                              "examenes_solicitados": [], "confianza": "alta"})
        self.assertEqual(dec["accion"], "recepcion")
        self.assertEqual(dec["motivo"], "no_es_orden")

    def test_orden_no_eco_holter(self):
        # corpus real: "Holter de presión arterial" — es orden pero no eco
        dec = decidir_accion(_orden(["Holter de presión arterial"]))
        self.assertEqual(dec["accion"], "recepcion")
        self.assertEqual(dec["motivo"], "sin_eco_ruteable")

    def test_radiografia_con_parte_del_cuerpo_no_ofrece(self):
        # GUARD del caso real de la validación e2e: Haiku transcribió
        # "Escoliosis lumbar (radiografía)" y el keyword suelto "lumbar"
        # habría ofrecido agenda de eco. Sin raíz ecográfica → recepción.
        dec = decidir_accion(_orden(["Escoliosis lumbar (radiografía)"]))
        self.assertEqual(dec["accion"], "recepcion")
        self.assertEqual(dec["motivo"], "sin_eco_ruteable")

    def test_radiografia_de_rodilla_no_ofrece(self):
        dec = decidir_accion(_orden(["Radiografía de rodilla izquierda"]))
        self.assertEqual(dec["accion"], "recepcion")


class TestFiltroEcoEnMixtas(unittest.TestCase):
    def test_orden_mixta_rx_mas_eco_ofrece_la_eco(self):
        # corpus real ...4814: [Rx tórax, Ecotomografía abdominal, Rx pelvis]
        # → la única eco es la abdominal: ofrecerla (antes caía a multi_examen)
        dec = decidir_accion(_orden([
            "Radiografía de tórax", "Ecotomografía abdominal",
            "Radiografía de pelvis"]))
        self.assertEqual(dec["accion"], "ofrecer_agenda")
        self.assertEqual(dec["routing"]["id_profesional"], 68)

    def test_transcripcion_imperfecta_ecozoografia(self):
        # corpus real ...4004: la visión transcribió "Ecozoografía cervical"
        # — fuzzy de raíz la reconoce como eco igual
        dec = decidir_accion(_orden(["Ecozoografía cervical"]))
        self.assertEqual(dec["accion"], "ofrecer_agenda")
        self.assertEqual(dec["routing"]["id_profesional"], 68)

    def test_multi_examen(self):
        # corpus real: 4 órdenes (antebrazos + muñecas) → recepción decide
        dec = decidir_accion(_orden([
            "Ecotomografía antebrazo derecho", "Ecotomografía antebrazo izquierdo",
            "Ecotomografía muñeca derecha", "Ecotomografía muñeca izquierda"]))
        self.assertEqual(dec["accion"], "recepcion")
        self.assertEqual(dec["motivo"], "multi_examen")

    def test_confianza_baja(self):
        dec = decidir_accion(_orden(["Eco algo ilegible"], confianza="baja"))
        self.assertEqual(dec["accion"], "recepcion")
        self.assertEqual(dec["motivo"], "confianza_baja")

    def test_ecocardiograma_waitlist_a_recepcion(self):
        dec = decidir_accion(_orden(["Ecocardiograma"]))
        self.assertEqual(dec["accion"], "recepcion")
        self.assertEqual(dec["motivo"], "flujo_waitlist")

    def test_extraccion_none(self):
        self.assertEqual(decidir_accion(None)["accion"], "recepcion")

    def test_orden_sin_examenes(self):
        dec = decidir_accion(_orden([]))
        self.assertEqual(dec["accion"], "recepcion")
        self.assertEqual(dec["motivo"], "sin_examenes")


if __name__ == "__main__":
    unittest.main(verbosity=2)
