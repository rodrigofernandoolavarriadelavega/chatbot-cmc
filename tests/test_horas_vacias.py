"""
Tests unitarios para el job de horas vacías D+1.

Caso principal: 3 slots libres disponibles, 5 candidatos elegibles
-> se envían exactamente 5 mensajes (todos pasan los filtros).

Tests offline: mockean get_slots_libres, send_whatsapp y funciones de session.
No tocan SQLite ni Medilink real.
"""
import asyncio
import sys
import os
import unittest
from datetime import datetime
from zoneinfo import ZoneInfo
from unittest.mock import AsyncMock, patch

# Path hacia app/
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

# Importar los módulos una sola vez (se reusan en todos los tests)
import jobs
from medilink import get_slots_libres
from session import (
    get_candidatos_horas_vacias,
    log_horas_vacias_envio,
    mark_horas_vacias_respondio,
    mark_horas_vacias_agendo,
    get_horas_vacias_envios_hoy,
)


def _make_slot(hora_inicio: str, especialidad: str, prof_id: int) -> dict:
    return {
        "hora_inicio": hora_inicio,
        "hora_fin": "00:00",
        "profesional": "Dr. Test",
        "especialidad": especialidad,
        "fecha": "2026-05-04",
        "fecha_display": "4/5/2026",
        "id_profesional": prof_id,
        "id_recurso": 1,
    }


async def _noop_sleep(_):
    """Reemplaza asyncio.sleep para no esperar en tests."""
    pass


_CLT = ZoneInfo("America/Santiago")


class TestHorasVaciasDiaSiguiente(unittest.IsolatedAsyncioTestCase):

    async def test_3_slots_5_candidatos_5_envios(self):
        """Con 3 slots libres y 5 candidatos elegibles deben enviarse 5 mensajes."""
        candidatos = [
            "56911111111",
            "56922222222",
            "56933333333",
            "56944444444",
            "56955555555",
        ]
        slots_libres = [
            _make_slot("09:00", "medicina general", 73),
            _make_slot("09:15", "medicina general", 73),
            _make_slot("09:30", "medicina general", 1),
        ]
        mensajes_enviados = []
        envios_registrados = []

        async def _slots_side(prof_id, fecha):
            if prof_id in (73, 1, 13):
                return slots_libres
            return []

        # Lunes 14:00 CLT — fuera de finde, dentro de ventana de envío
        lunes_14 = datetime(2026, 5, 4, 14, 0, tzinfo=_CLT)

        with patch.object(jobs, "get_slots_libres", side_effect=_slots_side), \
             patch.object(jobs, "get_candidatos_horas_vacias", return_value=candidatos), \
             patch.object(jobs, "get_horas_vacias_envios_hoy", return_value=0), \
             patch.object(jobs, "log_horas_vacias_envio",
                          side_effect=lambda *a, **kw: envios_registrados.append(a) or 1), \
             patch.object(jobs, "log_event"), \
             patch.object(jobs, "send_whatsapp",
                          new_callable=AsyncMock,
                          side_effect=lambda phone, txt: mensajes_enviados.append(phone)), \
             patch.object(jobs, "_canal_de_phone", return_value="wa"), \
             patch.object(jobs, "_ESPECIALIDADES_HORAS_VACIAS",
                          [("Medicina General", [73, 1, 13])]), \
             patch("asyncio.sleep", new=_noop_sleep):

            # Parchamos datetime.now dentro del namespace del job para que devuelva lunes 14:00
            import datetime as _dt_mod
            _real_dt = _dt_mod.datetime

            class _FakeDT(_real_dt):
                @classmethod
                def now(cls, tz=None):
                    if tz is not None:
                        return lunes_14
                    return lunes_14.replace(tzinfo=None)

            with patch("datetime.datetime", _FakeDT):
                await jobs._job_horas_vacias_dia_siguiente()

        self.assertEqual(
            len(mensajes_enviados), 5,
            f"Esperaba 5 envíos, se hicieron {len(mensajes_enviados)}: {mensajes_enviados}"
        )
        self.assertEqual(len(envios_registrados), 5)
        self.assertEqual(set(mensajes_enviados), set(candidatos))

        # El log de eventos "horas_vacias_enviado" debe estar presente (llamado desde jobs)
        # log_event está mockeado en jobs, así que no podemos inspeccionar directamente,
        # pero verificamos que se llamó la cantidad correcta de veces.
        # (5 envíos + posibles log_event de otras especialidades = al menos 5)
        # Verificamos via envios_registrados que el path completo se ejecutó.
        self.assertTrue(len(envios_registrados) >= 1)

    async def test_menos_de_3_slots_no_envia(self):
        """Con 2 slots libres no se envían mensajes."""
        slots_2 = [
            _make_slot("09:00", "medicina general", 73),
            _make_slot("09:15", "medicina general", 73),
        ]
        mensajes_enviados = []
        lunes_14 = datetime(2026, 5, 4, 14, 0, tzinfo=_CLT)

        import datetime as _dt_mod
        _real_dt = _dt_mod.datetime

        class _FakeDT(_real_dt):
            @classmethod
            def now(cls, tz=None):
                if tz is not None:
                    return lunes_14
                return lunes_14.replace(tzinfo=None)

        with patch.object(jobs, "get_slots_libres",
                          new_callable=AsyncMock, return_value=slots_2), \
             patch.object(jobs, "get_candidatos_horas_vacias",
                          return_value=["56911111111", "56922222222"]), \
             patch.object(jobs, "get_horas_vacias_envios_hoy", return_value=0), \
             patch.object(jobs, "log_horas_vacias_envio"), \
             patch.object(jobs, "log_event"), \
             patch.object(jobs, "send_whatsapp",
                          new_callable=AsyncMock,
                          side_effect=lambda phone, txt: mensajes_enviados.append(phone)), \
             patch.object(jobs, "_canal_de_phone", return_value="wa"), \
             patch.object(jobs, "_ESPECIALIDADES_HORAS_VACIAS",
                          [("Medicina General", [73])]), \
             patch("asyncio.sleep", new=_noop_sleep), \
             patch("datetime.datetime", _FakeDT):
            await jobs._job_horas_vacias_dia_siguiente()

        self.assertEqual(
            len(mensajes_enviados), 0,
            f"No debía enviar con 2 slots, envió a: {mensajes_enviados}"
        )

    async def test_tope_diario_respetado(self):
        """Si ya se enviaron 30 mensajes hoy para esa especialidad, no se envía nada."""
        slots = [_make_slot(f"09:0{i}", "medicina general", 73) for i in range(5)]
        mensajes_enviados = []
        lunes_14 = datetime(2026, 5, 4, 14, 0, tzinfo=_CLT)

        import datetime as _dt_mod

        class _FakeDT(_dt_mod.datetime):
            @classmethod
            def now(cls, tz=None):
                if tz is not None:
                    return lunes_14
                return lunes_14.replace(tzinfo=None)

        with patch.object(jobs, "get_slots_libres",
                          new_callable=AsyncMock, return_value=slots), \
             patch.object(jobs, "get_candidatos_horas_vacias",
                          return_value=["56911111111"]), \
             patch.object(jobs, "get_horas_vacias_envios_hoy", return_value=30), \
             patch.object(jobs, "log_horas_vacias_envio"), \
             patch.object(jobs, "log_event"), \
             patch.object(jobs, "send_whatsapp",
                          new_callable=AsyncMock,
                          side_effect=lambda phone, txt: mensajes_enviados.append(phone)), \
             patch.object(jobs, "_canal_de_phone", return_value="wa"), \
             patch.object(jobs, "_ESPECIALIDADES_HORAS_VACIAS",
                          [("Medicina General", [73])]), \
             patch("asyncio.sleep", new=_noop_sleep), \
             patch("datetime.datetime", _FakeDT):
            await jobs._job_horas_vacias_dia_siguiente()

        self.assertEqual(len(mensajes_enviados), 0, "Tope diario no respetado")

    async def test_candidatos_vacios_no_envia(self):
        """Con 5 slots libres pero 0 candidatos, no se envía nada."""
        slots = [_make_slot(f"09:0{i}", "medicina general", 73) for i in range(5)]
        mensajes_enviados = []
        lunes_14 = datetime(2026, 5, 4, 14, 0, tzinfo=_CLT)

        import datetime as _dt_mod

        class _FakeDT(_dt_mod.datetime):
            @classmethod
            def now(cls, tz=None):
                if tz is not None:
                    return lunes_14
                return lunes_14.replace(tzinfo=None)

        with patch.object(jobs, "get_slots_libres",
                          new_callable=AsyncMock, return_value=slots), \
             patch.object(jobs, "get_candidatos_horas_vacias", return_value=[]), \
             patch.object(jobs, "get_horas_vacias_envios_hoy", return_value=0), \
             patch.object(jobs, "log_horas_vacias_envio"), \
             patch.object(jobs, "log_event"), \
             patch.object(jobs, "send_whatsapp",
                          new_callable=AsyncMock,
                          side_effect=lambda phone, txt: mensajes_enviados.append(phone)), \
             patch.object(jobs, "_canal_de_phone", return_value="wa"), \
             patch.object(jobs, "_ESPECIALIDADES_HORAS_VACIAS",
                          [("Medicina General", [73])]), \
             patch("asyncio.sleep", new=_noop_sleep), \
             patch("datetime.datetime", _FakeDT):
            await jobs._job_horas_vacias_dia_siguiente()

        self.assertEqual(len(mensajes_enviados), 0, "No debe enviar sin candidatos")

    async def test_finde_tarde_no_envia(self):
        """Sábado >= 13:00 CLT: el job salta sin enviar."""
        slots = [_make_slot(f"09:0{i}", "medicina general", 73) for i in range(5)]
        mensajes_enviados = []
        sabado_14 = datetime(2026, 5, 2, 14, 0, tzinfo=_CLT)  # sábado

        import datetime as _dt_mod

        class _FakeDT(_dt_mod.datetime):
            @classmethod
            def now(cls, tz=None):
                if tz is not None:
                    return sabado_14
                return sabado_14.replace(tzinfo=None)

        with patch.object(jobs, "get_slots_libres",
                          new_callable=AsyncMock, return_value=slots), \
             patch.object(jobs, "get_candidatos_horas_vacias",
                          return_value=["56911111111"]), \
             patch.object(jobs, "get_horas_vacias_envios_hoy", return_value=0), \
             patch.object(jobs, "log_horas_vacias_envio"), \
             patch.object(jobs, "log_event"), \
             patch.object(jobs, "send_whatsapp",
                          new_callable=AsyncMock,
                          side_effect=lambda phone, txt: mensajes_enviados.append(phone)), \
             patch.object(jobs, "_canal_de_phone", return_value="wa"), \
             patch.object(jobs, "_ESPECIALIDADES_HORAS_VACIAS",
                          [("Medicina General", [73])]), \
             patch("asyncio.sleep", new=_noop_sleep), \
             patch("datetime.datetime", _FakeDT):
            await jobs._job_horas_vacias_dia_siguiente()

        self.assertEqual(len(mensajes_enviados), 0, "No debe enviar finde >= 13:00")

    def test_helpers_session_importables(self):
        """Verifica que las funciones helper existen en session."""
        self.assertTrue(callable(get_candidatos_horas_vacias))
        self.assertTrue(callable(log_horas_vacias_envio))
        self.assertTrue(callable(mark_horas_vacias_respondio))
        self.assertTrue(callable(mark_horas_vacias_agendo))
        self.assertTrue(callable(get_horas_vacias_envios_hoy))

    def test_get_slots_libres_importable(self):
        """Verifica que get_slots_libres existe en medilink."""
        self.assertTrue(callable(get_slots_libres))


if __name__ == "__main__":
    unittest.main()
