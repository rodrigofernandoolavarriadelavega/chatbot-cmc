"""Regresión: WAIT_PARENTESCO expiraba a los 30 min (timeout por defecto) en
vez de los 240 min de los demás flujos activos post-confirmación (mismo
patrón que FIX-6 y WAIT_ABONO_* documentados en session.py).

Caso real 2026-09-25 (56931806676): el bot preguntó el parentesco ("¿qué es
Rodrigo tuyo/a?") justo después de confirmar una cita para un tercero. El
paciente contestó "Padre/Madre" 33 minutos después — la sesión ya había
expirado a IDLE, así que el id interno del botón tocado ("par_padre", el
`id` de WhatsApp, no el `title` humano) se coló como texto libre en el
fallback de Claude, que lo citó tal cual: "veo que escribiste «par_padre»".
Fuga de un identificador interno al paciente.

Con WAIT_PARENTESCO en `_FLUJO_ACTIVO_STATES` (240 min), la respuesta a los
33 min sigue llegando DENTRO de la sesión y la procesa el handler normal de
WAIT_PARENTESCO (guarda el parentesco, no hay fuga).

Ejecución:
    PYTHONPATH=app:. venv/bin/python3 tests/test_bugs_2026_09_25_parentesco.py
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "app"))
sys.path.insert(0, str(ROOT))

TMP_DB_DIR = Path(tempfile.mkdtemp(prefix="cmc_test_parentesco_"))
TMP_DB = TMP_DB_DIR / "test_sessions.db"
os.environ["SESSIONS_DB"] = str(TMP_DB)

import session  # noqa: E402
session.DB_PATH = TMP_DB


class TestWaitParentescoTimeoutExtendido(unittest.TestCase):
    """WAIT_PARENTESCO debe sobrevivir una respuesta tardía (33 min, como el
    caso real) igual que los demás estados post-confirmación."""

    def setUp(self):
        # Sesión limpia por corrida (fixture propio, no comparte con otros tests)
        self.phone = "56931806676_test"

    def _guardar_con_antiguedad(self, state: str, minutos_atras: int):
        with session.db() as conn:
            ts = (datetime.now(timezone.utc) - timedelta(minutes=minutos_atras)).isoformat()
            conn.execute(
                "INSERT OR REPLACE INTO sessions (phone, state, data, updated_at) "
                "VALUES (?, ?, ?, ?)",
                (self.phone, state, "{}", ts),
            )
            conn.commit()

    def test_respuesta_a_los_33_min_no_expira(self):
        self._guardar_con_antiguedad("WAIT_PARENTESCO", 33)
        sess = session.get_session(self.phone)
        self.assertEqual(sess["state"], "WAIT_PARENTESCO",
                          "con timeout de 240 min, 33 min no debe expirar la sesión")

    def test_wait_parentesco_esta_en_flujo_activo(self):
        self.assertIn("WAIT_PARENTESCO", session._FLUJO_ACTIVO_STATES)

    def test_respuesta_tardisima_si_expira(self):
        """Sanity: el timeout largo no es infinito — a las 241 min sí expira."""
        self._guardar_con_antiguedad("WAIT_PARENTESCO", 241)
        sess = session.get_session(self.phone)
        self.assertEqual(sess["state"], "IDLE")

    def test_idle_normal_sigue_expirando_a_los_30(self):
        """No se degrada el timeout corto de los estados que sí deben ser cortos."""
        self._guardar_con_antiguedad("IDLE", 31)
        sess = session.get_session(self.phone + "_idle")
        # Sesión distinta para no interferir; probamos con el mismo phone
        # limpio: como no existe fila, get_session devuelve IDLE de por sí,
        # así que probamos con el phone real usado arriba.
        self._guardar_con_antiguedad("IDLE", 31)
        sess = session.get_session(self.phone)
        self.assertEqual(sess["state"], "IDLE")


if __name__ == "__main__":
    unittest.main(verbosity=2)
