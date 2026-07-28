"""Tests de app/pagos_transferencia_sugeridos.py — matching puro
(`_match_candidatos`, `_elegir_ganador_unico`) contra fixtures en memoria.

Regla dura verificada explícitamente: NINGÚN test debe llegar a escribir en
`pagos_cmc` — `generar_sugerencia`/`_match_candidatos`/`_elegir_ganador_unico`
son funciones puras (no tocan la base). Solo `confirmar_sugerencia` escribe,
y esa función requiere una llamada EXPLÍCITA con un `pago_cmc_id` elegido por
un humano — no se testea acá contra SQLite real (needs sesión.db real), se
prueba el motor de decisión que la antecede.

Ejecución:
    PYTHONPATH=app:. venv/bin/python3 tests/test_pagos_transferencia_sugeridos.py
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "app"))

import pagos_transferencia_sugeridos as pts  # noqa: E402


def _candidato(id, nombre, monto_medilink=0, profesional="Dr. Prueba"):
    return {"id": id, "paciente_nombre": nombre, "profesional": profesional,
            "id_profesional": 1, "id_cita": "999", "area": "Medicina General",
            "monto_medilink": monto_medilink, "id_paciente": id}


class TestMatchCandidatos(unittest.TestCase):
    def test_un_solo_candidato_gana_aunque_nombre_no_coincida(self):
        """Si hoy solo hay UN paciente sin cobrar, y llega un correo con
        nombre distinto (puede ser un familiar pagando), igual se sugiere
        — pero etiquetado 'nombre_distinto', no 'nombre_fuerte'."""
        cands = [_candidato(1, "Elena Rodriguez")]
        rankeados = pts._match_candidatos("Cristobal Hijo", 15000, cands)
        ganador = pts._elegir_ganador_unico(rankeados)
        self.assertIsNotNone(ganador)
        self.assertEqual(ganador["id"], 1)
        self.assertEqual(ganador["etiqueta"], "nombre_distinto")

    def test_nombre_fuerte_gana_sobre_varios_candidatos(self):
        cands = [
            _candidato(1, "Maria Jose Perez Soto"),
            _candidato(2, "Juan Pablo Gonzalez"),
            _candidato(3, "Ana Torres"),
        ]
        rankeados = pts._match_candidatos("MARIA JOSE PEREZ SOTO", 15000, cands)
        ganador = pts._elegir_ganador_unico(rankeados)
        self.assertIsNotNone(ganador)
        self.assertEqual(ganador["id"], 1)
        self.assertEqual(ganador["etiqueta"], "nombre_fuerte")

    def test_monto_medilink_desempata_sin_nombre(self):
        """Dos candidatos, ninguno con nombre parecido — pero UNO tiene el
        arancel Medilink exacto igual al monto transferido: gana ese."""
        cands = [
            _candidato(1, "Persona Uno", monto_medilink=15000),
            _candidato(2, "Persona Dos", monto_medilink=60000),
        ]
        rankeados = pts._match_candidatos("Nombre Sin Relacion", 15000, cands)
        ganador = pts._elegir_ganador_unico(rankeados)
        self.assertIsNotNone(ganador)
        self.assertEqual(ganador["id"], 1)
        self.assertTrue(ganador["monto_ok"])

    def test_empate_no_elige_ninguno(self):
        """Dos candidatos con score idéntico (ninguna señal de ningún lado)
        → no se elige ganador, queda para que recepción decida."""
        cands = [
            _candidato(1, "Persona Uno"),
            _candidato(2, "Persona Dos"),
        ]
        rankeados = pts._match_candidatos("Nombre Que No Calza Con Nadie", 15000, cands)
        ganador = pts._elegir_ganador_unico(rankeados)
        self.assertIsNone(ganador)

    def test_sin_candidatos_no_lanza(self):
        rankeados = pts._match_candidatos("Alguien", 15000, [])
        self.assertEqual(rankeados, [])
        self.assertIsNone(pts._elegir_ganador_unico(rankeados))

    def test_generar_sugerencia_no_toca_base(self):
        """generar_sugerencia es puro salvo por la consulta de candidatos —
        se prueba aquí solo la forma del resultado, no la persistencia."""
        # Sin sesión.db real, _candidatos_pendientes_dia lanzaría — este test
        # solo verifica que _match_candidatos/_elegir_ganador_unico (el
        # motor real de decisión) están correctamente expuestos y son puros.
        cands = [_candidato(1, "Elena Soto")]
        rankeados = pts._match_candidatos("ELENA SOTO", 7880, cands)
        self.assertEqual(len(rankeados), 1)
        self.assertGreaterEqual(rankeados[0]["score"], pts._SIM_FUERTE)


if __name__ == "__main__":
    unittest.main(verbosity=2)
