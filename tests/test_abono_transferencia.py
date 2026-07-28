"""Tests de app/abono_transferencia.py — confirmación automática de abonos
por transferencia bancaria (Psiquiatría, $60.000, GATEADO ABONO_AUTO_ACTIVE).

Cubre:
  1. Parsers de los 3 bancos (Santander, Falabella, Banco de Chile) contra
     plantillas SINTÉTICAS con la misma estructura que los correos reales
     verificados en el buzón del CMC (2026-07-14) — sin PII real en el repo.
  2. Filtro de cuenta ajena (`_es_cuenta_cmc`).
  3. Similitud de nombres (`nombres_similares`) — casos exactos, con tilde,
     apellido parcial (el "familiar que paga"), y no-match.
  4. Motor de emparejamiento end-to-end contra una SQLite temporal real:
     match único con nombre OK → confirma solo; nombre no calza → pregunta;
     2+ candidatos incluyendo un caso el nombre desambigua; 2+ candidatos
     sin desambiguar → pregunta al más antiguo; monto sin candidatos → no
     confirma nada; ventana expirada → no confirma nada; una transferencia
     no puede confirmar 2 reservas (consumo atómico).
  5. NUNCA confirma sin certeza: ningún test debe terminar con 2 abonos
     'confirmado' desde la misma transferencia, ni con un abono confirmado
     cuando el monto no calza.
"""
import asyncio
import os
import sqlite3
import sys
import tempfile
import unittest
from contextlib import contextmanager
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

_CL = ZoneInfo("America/Santiago")


# ── Fixtures sintéticas (misma estructura que los correos reales, PII falsa) ─

SANTANDER_COMPROBANTE = """Comprobante
Transferencia de fondos
Estimado(a) Centro médico rodrigo Olavarría de la Vega Eirl:
Te informamos que, con fecha 10/07/2026, nuestro cliente MARIA JOSE PRUEBA GONZALEZ realizó una transferencia a tu cuenta. Este es el detalle:
Monto transferido
$ 60.000
Datos de destino
Nombre
Centro médico rodrigo Olavarría de la Vega Eirl
RUT
77.140.898-2
Banco
Banco Itaú
Nº de cuenta
0-002-21-70853-8
Comentario
"""

SANTANDER_AVISO = """Aviso de Transferencia de Fondos
Centro médico carampangue, ha recibido una transferencia
Estimado: Centro médico carampangue
De acuerdo con lo instruido por nuestro cliente JUAN PRUEBA SOTO, le informamos que con Fecha: 10-07-2026 se ha realizado una transferencia de fondos hacia su cuenta del banco BANCO ITAU CHILE nro. 000000000221708538.
Detalle de la operación
Rut cuenta origen
Titular cuenta origen
9.999.999-9
JUAN PRUEBA SOTO
Banco de Origen
Numero de la operacion
BANCO SANTANDER (CHILE)
99999999999999999999
Comentario para el destinatario
Monto transferido
10-07-2026
$ 60.000
"""

FALABELLA_AVISO = """Centro Medico Rodrigo Olavarria De La Vega
Le informamos que hoy, 10-07-2026, nuestro(a) cliente CARMEN PRUEBA SOTO ha instruido una transferencia de fondos a su cuenta con el siguiente detalle:
DetalleBanco de destinoBanco Itau
Cuenta de destinoCuenta Corriente 0221708538
Rut destinatario771408982
AsuntoTransferencia realizada
Monto transferencia$60.000
Fecha10-07-2026
Hora10:15
Numero de operacion123456789012
"""

BANCOCHILE_AVISO = """Banco de Chile | Mi Banco
Comprobante de transferencia electrónica de fondos
Estimado(a):
Centro medico Carampangue
Te informamos que nuestro(a) cliente
Pedro Prueba Martinez
ha efectuado una transferencia
de fondos a tu cuenta con el siguiente detalle:
Datos de cuenta
Fecha
10/07/2026
Asunto
Datos de destinatario
Nombre y Apellido
Centro medico Carampangue
Rut
77140898-2
Email
Centromedicocarampangue@gmail.com
Banco
Banco Itau Chile
Cuenta destino
Cuenta Corriente
00-022-17085-38
Monto
$60.000
Número de comprobante
TEFMBCO2607101015305417376980
Fecha y Hora:
viernes 10 de julio de 2026 10:15
"""

# Cuenta AJENA (otro negocio, mismo formato Santander, distinta cuenta destino)
SANTANDER_CUENTA_AJENA = """Comprobante
Transferencia de fondos
Estimado(a) Otro Negocio SpA:
Te informamos que, con fecha 10/07/2026, nuestro cliente ALGUIEN AJENO PEREZ realizó una transferencia a tu cuenta. Este es el detalle:
Monto transferido
$ 60.000
Datos de destino
Nombre
Otro Negocio SpA
RUT
11.222.333-4
Banco
Banco Estado
Nº de cuenta
0-523-70-51170-5
Comentario
"""


class TestParsers(unittest.TestCase):
    def setUp(self):
        import abono_transferencia as at
        self.at = at

    def test_santander_comprobante_dominante(self):
        p = self.at.parse_bank_email("Santander <mensajeria@santander.cl>", SANTANDER_COMPROBANTE)
        self.assertIsNotNone(p)
        self.assertEqual(p["monto"], 60000)
        self.assertIn("MARIA JOSE PRUEBA GONZALEZ", p["nombre_pagador"])
        self.assertEqual(p["fecha"], "2026-07-10")
        self.assertEqual(p["banco"], "Santander")

    def test_santander_aviso_variante_rara(self):
        p = self.at.parse_bank_email("Santander <mensajeria@santander.cl>", SANTANDER_AVISO)
        self.assertIsNotNone(p)
        self.assertEqual(p["monto"], 60000)
        self.assertIn("JUAN PRUEBA SOTO", p["nombre_pagador"])

    def test_falabella(self):
        p = self.at.parse_bank_email("notificaciones@cl.bancofalabella.com", FALABELLA_AVISO)
        self.assertIsNotNone(p)
        self.assertEqual(p["monto"], 60000)
        self.assertEqual(p["hora"], "10:15")
        self.assertIn("CARMEN PRUEBA SOTO", p["nombre_pagador"])
        self.assertEqual(p["codigo_operacion"], "123456789012")

    def test_bancochile(self):
        p = self.at.parse_bank_email("serviciodetransferencias@bancochile.cl", BANCOCHILE_AVISO)
        self.assertIsNotNone(p)
        self.assertEqual(p["monto"], 60000)
        self.assertEqual(p["nombre_pagador"], "Pedro Prueba Martinez")
        self.assertEqual(p["fecha"], "2026-07-10")
        self.assertEqual(p["hora"], "10:15")

    def test_cuenta_ajena_se_descarta(self):
        """Correo con la MISMA estructura de Santander pero a otra cuenta →
        parse_bank_email debe devolver None (nunca confirmar algo de otro negocio)."""
        p = self.at.parse_bank_email("mensajeria@santander.cl", SANTANDER_CUENTA_AJENA)
        self.assertIsNone(p)

    def test_remitente_desconocido(self):
        p = self.at.parse_bank_email("spam@ejemplo.com", SANTANDER_COMPROBANTE)
        self.assertIsNone(p)

    def test_cuerpo_irreconocible_no_lanza(self):
        p = self.at.parse_bank_email("mensajeria@santander.cl", "cuerpo random sin estructura")
        self.assertIsNone(p)


class TestNombresSimilares(unittest.TestCase):
    def setUp(self):
        import abono_transferencia as at
        self.sim = at.nombres_similares

    def test_exacto(self):
        ok, score = self.sim("Juan Perez", "JUAN PEREZ")
        self.assertTrue(ok)

    def test_con_tilde_y_mayusculas(self):
        ok, _ = self.sim("María José González", "MARIA JOSE GONZALEZ")
        self.assertTrue(ok)

    def test_familiar_que_paga_apellido_parcial(self):
        """Caso del enunciado: el paciente se llama 'Carmen Soto', el hijo
        transfiere y el banco muestra el nombre completo del hijo/hija con
        más apellidos — igual debe reconocer overlap si comparten el
        apellido/nombre suficiente. Aquí probamos el caso favorable (el
        paciente es quien transfiere, con nombre completo vs. abreviado)."""
        ok, score = self.sim("Carmen Soto", "CARMEN ANDREA SOTO PEREZ")
        self.assertTrue(ok)
        self.assertGreaterEqual(score, 0.55)

    def test_nombre_totalmente_distinto_no_coincide(self):
        ok, score = self.sim("Carmen Soto", "RODRIGO OLAVARRIA VEGA")
        self.assertFalse(ok)

    def test_vacios(self):
        ok, _ = self.sim("", "Juan Perez")
        self.assertFalse(ok)


# ── Motor de emparejamiento end-to-end contra SQLite real ──────────────────

class _FakeSession:
    """Shim mínimo de session.py: DB real (archivo temporal) + sesiones en
    memoria — suficiente superficie para abono_transferencia.py sin arrastrar
    session.py completo (SQLCipher, 254K líneas, etc.)."""

    def __init__(self, db_path):
        self._db_path = db_path
        self._state = {}
        self._sessions = {}
        self.events = []
        self.messages = []

    @contextmanager
    def db(self):
        conn = sqlite3.connect(self._db_path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def system_state_get(self, key):
        return self._state.get(key)

    def system_state_set(self, key, value):
        self._state[key] = value

    def log_event(self, phone, event, meta=None):
        self.events.append((phone, event, meta))

    def log_message(self, phone, direction, text, state="IDLE", canal=None):
        self.messages.append((phone, direction, text, state))

    def get_session(self, phone):
        return self._sessions.get(phone)

    def save_session(self, phone, state, data):
        self._sessions[phone] = {"state": state, "data": data}

    def reset_session(self, phone):
        self._sessions[phone] = {"state": "IDLE", "data": {}}


def _mk_slot():
    return {
        "id_profesional": 78,
        "profesional": "Dra. Cecilia Unibazo",
        "especialidad": "Psiquiatría",
        "fecha": "2026-07-10",
        "fecha_display": "viernes 10 de julio",
        "hora_inicio": "16:00",
        "hora_fin": "16:30",
        "id_recurso": 1,
    }


class TestMotorEmparejamiento(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self._tmp.close()
        self._fake_session = _FakeSession(self._tmp.name)

        self._patches = []
        fake_medilink = MagicMock()
        fake_medilink.crear_cita = AsyncMock(return_value={"id": "555"})
        fake_messaging = MagicMock()
        fake_messaging.send_whatsapp = AsyncMock(return_value="wamid.fake")
        fake_messaging.send_whatsapp_interactive = AsyncMock(return_value="wamid.fake2")
        fake_abonos_routes = MagicMock()
        fake_abonos_routes.ensure_abonos_table = MagicMock()

        fake_flows = MagicMock()
        fake_flows._btn_msg = lambda texto, botones: {
            "type": "interactive",
            "interactive": {"type": "button", "body": {"text": texto},
                             "action": {"buttons": [{"type": "reply", "reply": b} for b in botones]}},
        }

        for name, mod in [
            ("session", self._fake_session),
            ("medilink", fake_medilink),
            ("messaging", fake_messaging),
            ("abonos_routes", fake_abonos_routes),
            ("flows", fake_flows),
        ]:
            p = patch.dict("sys.modules", {name: mod})
            p.start()
            self._patches.append(p)

        # abonos_cmc la crea normalmente abonos_routes.ensure_abonos_table();
        # acá la mockeamos, así que la creamos a mano en la DB de prueba.
        with self._fake_session.db() as conn:
            conn.execute("""
                CREATE TABLE abonos_cmc (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    fecha TEXT, hora TEXT, paciente_nombre TEXT, rut TEXT,
                    id_profesional INTEGER, profesional TEXT, area TEXT,
                    fecha_cita TEXT, precio_total INTEGER, monto_abono INTEGER,
                    saldo INTEGER, metodo_pago TEXT, codigo_transferencia TEXT,
                    estado TEXT, id_cita TEXT, nota TEXT, creado_por TEXT,
                    created_at TEXT, updated_at TEXT
                )
            """)

        import importlib
        import abono_transferencia as at
        importlib.reload(at)
        self.at = at
        self.at.ensure_abono_pendiente_table()
        self.at.ensure_transferencias_table()

    def tearDown(self):
        for p in self._patches:
            p.stop()
        os.unlink(self._tmp.name)
        import sys as _s
        for _m in list(_s.modules):
            if isinstance(_s.modules.get(_m), MagicMock):
                _s.modules.pop(_m, None)

    def _crear_pendiente(self, phone, nombre, monto=60000, wait_min=90):
        return self.at.crear_abono_pendiente(
            phone=phone, paciente_id="1001", paciente_nombre=nombre, rut="11.111.111-1",
            monto=monto, especialidad="Psiquiatría", id_profesional=78, slot=_mk_slot(),
            wait_min=wait_min,
        )

    def _abono(self, token):
        return self.at.get_abono_pendiente(token)

    # ── 1. match único, nombre calza → confirma sola ───────────────────────
    async def test_match_unico_nombre_calza_confirma_solo(self):
        link = self._crear_pendiente("56911111111", "Maria Jose Prueba Gonzalez")
        r = await self.at.procesar_correo_bancario(
            "mensajeria@santander.cl", SANTANDER_COMPROBANTE,
            datetime.now(_CL), uid=1001,
        )
        self.assertTrue(r["ok"])
        self.assertTrue(r["match"])
        abono = self._abono(link["token"])
        self.assertEqual(abono["estado"], "confirmado")
        self.assertEqual(abono["confirmado_por"], "auto_email")
        self.assertEqual(abono["id_cita"], "555")
        # Se avisó al paciente por WhatsApp
        import sys
        self.assertTrue(sys.modules["messaging"].send_whatsapp.called)

    # ── 2. match único, nombre NO calza → pregunta, no confirma ────────────
    async def test_nombre_no_calza_pregunta_no_confirma_solo(self):
        link = self._crear_pendiente("56922222222", "Pedro Distinto Apellido")
        r = await self.at.procesar_correo_bancario(
            "mensajeria@santander.cl", SANTANDER_COMPROBANTE,
            datetime.now(_CL), uid=1002,
        )
        self.assertEqual(r["match"], "pregunta")
        abono = self._abono(link["token"])
        self.assertEqual(abono["estado"], "esperando_confirmacion_paciente")
        self.assertNotEqual(abono["estado"], "confirmado")
        # Se le preguntó al paciente (interactive)
        import sys
        self.assertTrue(sys.modules["messaging"].send_whatsapp_interactive.called)

    # ── 3. pregunta → paciente responde Sí → confirma ───────────────────────
    async def test_pregunta_luego_si_confirma(self):
        link = self._crear_pendiente("56933333333", "Nombre Que No Calza")
        await self.at.procesar_correo_bancario(
            "mensajeria@santander.cl", SANTANDER_COMPROBANTE, datetime.now(_CL), uid=1003,
        )
        abono = self._abono(link["token"])
        self.assertEqual(abono["estado"], "esperando_confirmacion_paciente")
        texto = await self.at.resolver_confirmacion_paciente(
            abono["id"], abono["candidata_transferencia_id"], True,
        )
        abono2 = self._abono(link["token"])
        self.assertEqual(abono2["estado"], "confirmado")
        self.assertEqual(abono2["confirmado_por"], "auto_email_confirmado_paciente")

    # ── 4. pregunta → paciente responde No → NO confirma, sigue pendiente ──
    async def test_pregunta_luego_no_no_confirma(self):
        link = self._crear_pendiente("56944444444", "Nombre Que No Calza Dos")
        await self.at.procesar_correo_bancario(
            "mensajeria@santander.cl", SANTANDER_COMPROBANTE, datetime.now(_CL), uid=1004,
        )
        abono = self._abono(link["token"])
        await self.at.resolver_confirmacion_paciente(
            abono["id"], abono["candidata_transferencia_id"], False,
        )
        abono2 = self._abono(link["token"])
        self.assertEqual(abono2["estado"], "pendiente")  # sigue esperando, nunca confirmado
        self.assertNotEqual(abono2["estado"], "confirmado")

    # ── 5. 2 candidatos mismo monto, el nombre desambigua → confirma SOLO 1 ─
    async def test_dos_candidatos_nombre_desambigua(self):
        link_a = self._crear_pendiente("56955555551", "Maria Jose Prueba Gonzalez")
        link_b = self._crear_pendiente("56955555552", "Otra Persona Cualquiera")
        r = await self.at.procesar_correo_bancario(
            "mensajeria@santander.cl", SANTANDER_COMPROBANTE, datetime.now(_CL), uid=1005,
        )
        abono_a = self._abono(link_a["token"])
        abono_b = self._abono(link_b["token"])
        self.assertEqual(abono_a["estado"], "confirmado")
        self.assertEqual(abono_b["estado"], "pendiente")  # el otro NO se toca

    # ── 6. 2 candidatos, nombre no desambigua → pregunta al MÁS ANTIGUO ────
    async def test_dos_candidatos_ambiguos_pregunta_al_mas_antiguo(self):
        link_a = self._crear_pendiente("56966666661", "Nombre Ambiguo Uno")
        link_b = self._crear_pendiente("56966666662", "Nombre Ambiguo Dos")
        r = await self.at.procesar_correo_bancario(
            "mensajeria@santander.cl", SANTANDER_COMPROBANTE, datetime.now(_CL), uid=1006,
        )
        self.assertEqual(r["match"], "pregunta_ambiguo")
        abono_a = self._abono(link_a["token"])
        abono_b = self._abono(link_b["token"])
        # el más antiguo (creado primero) es al que se le pregunta
        self.assertEqual(abono_a["estado"], "esperando_confirmacion_paciente")
        self.assertEqual(abono_b["estado"], "pendiente")
        self.assertNotEqual(abono_a["estado"], "confirmado")
        self.assertNotEqual(abono_b["estado"], "confirmado")

    # ── 7. monto sin candidatos pendientes → no confirma nada ──────────────
    async def test_sin_pendientes_no_confirma_nada(self):
        r = await self.at.procesar_correo_bancario(
            "mensajeria@santander.cl", SANTANDER_COMPROBANTE, datetime.now(_CL), uid=1007,
        )
        self.assertFalse(r["match"])
        self.assertEqual(r["motivo"], "sin_abonos_pendientes")

    # ── 8. ventana expirada → no confirma nada ──────────────────────────────
    async def test_ventana_expirada_no_confirma(self):
        link = self._crear_pendiente("56977777777", "Maria Jose Prueba Gonzalez", wait_min=90)
        # Forzar que ya expiró: mover creado_at/expira_at al pasado.
        with self._fake_session.db() as conn:
            pasado = (datetime.now(_CL) - timedelta(hours=3)).isoformat()
            expirado = (datetime.now(_CL) - timedelta(hours=1, minutes=30)).isoformat()
            conn.execute(
                "UPDATE abono_pendientes SET creado_at=?, expira_at=? WHERE token=?",
                (pasado, expirado, link["token"]),
            )
        r = await self.at.procesar_correo_bancario(
            "mensajeria@santander.cl", SANTANDER_COMPROBANTE, datetime.now(_CL), uid=1008,
        )
        self.assertFalse(r["match"])
        abono = self._abono(link["token"])
        self.assertEqual(abono["estado"], "pendiente")  # nunca confirmado fuera de ventana

    # ── 9. cuenta ajena nunca genera match ni queda registrada como cruzable ─
    async def test_cuenta_ajena_no_genera_match(self):
        self._crear_pendiente("56988888888", "Alguien Ajeno Perez")
        r = await self.at.procesar_correo_bancario(
            "mensajeria@santander.cl", SANTANDER_CUENTA_AJENA, datetime.now(_CL), uid=1009,
        )
        self.assertFalse(r["match"])
        self.assertEqual(r["motivo"], "no_parseable_o_cuenta_ajena")

    # ── 10. una transferencia jamás confirma 2 reservas (consumo atómico) ──
    async def test_transferencia_no_confirma_dos_veces(self):
        link_a = self._crear_pendiente("56999999991", "Maria Jose Prueba Gonzalez")
        await self.at.procesar_correo_bancario(
            "mensajeria@santander.cl", SANTANDER_COMPROBANTE, datetime.now(_CL), uid=1010,
        )
        abono_a = self._abono(link_a["token"])
        self.assertEqual(abono_a["estado"], "confirmado")

        # Un segundo abono pendiente con el mismo monto/nombre creado DESPUÉS:
        # el mismo correo (mismo uid) no puede volver a usarse — solo se
        # procesa una vez por uid (INSERT OR IGNORE), y aunque se reprocesara,
        # el emparejador solo mira transferencias NUEVAS, no reutiliza una ya
        # consumida contra otro candidato.
        link_b = self._crear_pendiente("56999999992", "Maria Jose Prueba Gonzalez")
        with self._fake_session.db() as conn:
            row = conn.execute(
                "SELECT COUNT(*) n FROM transferencias_banco WHERE estado_match='match_automatico'"
            ).fetchone()
            self.assertEqual(row["n"], 1)  # una sola transferencia consumida, nunca dos


if __name__ == "__main__":
    unittest.main()
