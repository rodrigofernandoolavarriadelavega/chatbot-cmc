"""Tests para tanda 5: robustez + observabilidad.

Cubre:
  - F046: jobs críticos tienen misfire_grace_time y coalesce en main.py
  - B5:   alerta_oob es no-op sin env, llama Telegram API con env
  - B6:   synthetic check existe en scheduler y tiene misfire_grace
  - B7:   ping_deadman es no-op sin URL, hace GET con URL
"""
from __future__ import annotations

import asyncio
import sys
import os
import importlib
import types
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ── Path setup ────────────────────────────────────────────────────────────────
_APP = Path(__file__).resolve().parent.parent / "app"
if str(_APP) not in sys.path:
    sys.path.insert(0, str(_APP))


# ═══════════════════════════════════════════════════════════════════════════════
# F046 — misfire_grace_time y coalesce en jobs críticos del scheduler
# ═══════════════════════════════════════════════════════════════════════════════

class TestF046MisfireGrace:
    """Verifica que los jobs críticos se registran con misfire_grace_time y coalesce."""

    # IDs que DEBEN tener misfire_grace_time y coalesce
    CRITICOS = {
        "recordatorios_diarios",
        "consent_agendados_horario",
        "recordatorios_2h",
        "recordatorios_48h",
        "sync_citas_recepcion",
        "seguimiento_postconsulta",
        "seguimiento_postconsulta_morning",
        "enrolar_atendidos_dia",
        "bi_sync_v2_diario",
        "cumpleanos_diario",
        "reactivacion_pacientes",
        "waitlist_check",
        "synthetic_check_agendar",
    }

    def _collect_add_job_calls(self) -> dict[str, dict]:
        """Parsea main.py y extrae kwargs de cada scheduler.add_job por id.

        Usa un mini-parser de paréntesis para manejar CronTrigger(...) anidado.
        """
        import re
        main_path = _APP / "main.py"
        src = main_path.read_text()
        results = {}

        # Encuentra cada scheduler.add_job( y extrae el bloque completo
        # considerando paréntesis anidados (CronTrigger(...) tiene su propio cierre).
        pattern = re.compile(r'scheduler\.add_job\(')
        for m in pattern.finditer(src):
            start = m.end()   # posición justo después del '('
            depth = 1
            i = start
            while i < len(src) and depth > 0:
                if src[i] == '(':
                    depth += 1
                elif src[i] == ')':
                    depth -= 1
                i += 1
            block = src[start:i - 1]  # contenido entre el par exterior

            m_id = re.search(r'id\s*=\s*["\']([^"\']+)["\']', block)
            if not m_id:
                continue
            job_id = m_id.group(1)
            m_mft  = re.search(r'misfire_grace_time\s*=\s*(\d+)', block)
            m_coal = re.search(r'coalesce\s*=\s*(True|False)', block)
            results[job_id] = {
                "misfire_grace_time": int(m_mft.group(1)) if m_mft else None,
                "coalesce": (m_coal.group(1) == "True") if m_coal else None,
            }
        return results

    def test_criticos_tienen_misfire_grace(self):
        calls = self._collect_add_job_calls()
        missing = []
        for job_id in self.CRITICOS:
            info = calls.get(job_id, {})
            if info.get("misfire_grace_time") is None:
                missing.append(f"{job_id}: misfire_grace_time ausente")
        assert not missing, "Jobs sin misfire_grace_time:\n" + "\n".join(missing)

    def test_criticos_tienen_coalesce(self):
        calls = self._collect_add_job_calls()
        missing = []
        for job_id in self.CRITICOS:
            info = calls.get(job_id, {})
            if not info.get("coalesce"):
                missing.append(f"{job_id}: coalesce ausente o False")
        assert not missing, "Jobs sin coalesce=True:\n" + "\n".join(missing)

    def test_misfire_grace_recordatorios_es_1h(self):
        """El job de recordatorios diarios debe tener al menos 3600s de gracia."""
        calls = self._collect_add_job_calls()
        info = calls.get("recordatorios_diarios", {})
        grace = info.get("misfire_grace_time")
        assert grace is not None and grace >= 3600, (
            f"recordatorios_diarios.misfire_grace_time={grace} (esperado >= 3600)"
        )


# ═══════════════════════════════════════════════════════════════════════════════
# B5 — alerta_oob: no-op sin env, llama API con env
# ═══════════════════════════════════════════════════════════════════════════════

class TestAlertaOOB:

    def test_noop_sin_env(self):
        """Sin TELEGRAM_ALERT_TOKEN retorna False sin hacer ninguna llamada."""
        # Limpiar env para el test
        env_bak = {k: os.environ.pop(k, None) for k in ("TELEGRAM_ALERT_TOKEN", "TELEGRAM_ALERT_CHAT_ID")}
        try:
            import alertas_oob
            importlib.reload(alertas_oob)
            result = asyncio.get_event_loop().run_until_complete(
                alertas_oob.alerta_oob("test mensaje")
            )
            assert result is False
        finally:
            for k, v in env_bak.items():
                if v is not None:
                    os.environ[k] = v

    def test_noop_con_token_sin_chat(self):
        """Solo token sin chat_id → False."""
        env_bak = {k: os.environ.pop(k, None) for k in ("TELEGRAM_ALERT_TOKEN", "TELEGRAM_ALERT_CHAT_ID")}
        try:
            os.environ["TELEGRAM_ALERT_TOKEN"] = "fake_token"
            # chat_id ausente
            import alertas_oob
            importlib.reload(alertas_oob)
            result = asyncio.get_event_loop().run_until_complete(
                alertas_oob.alerta_oob("test")
            )
            assert result is False
        finally:
            for k, v in env_bak.items():
                if v is not None:
                    os.environ[k] = v
            os.environ.pop("TELEGRAM_ALERT_TOKEN", None)

    def test_llama_api_con_env(self):
        """Con ambas vars, debe hacer POST a la URL correcta y retornar True si 200."""
        import httpx

        env_bak = {k: os.environ.pop(k, None) for k in ("TELEGRAM_ALERT_TOKEN", "TELEGRAM_ALERT_CHAT_ID")}
        try:
            os.environ["TELEGRAM_ALERT_TOKEN"] = "fake_token_123"
            os.environ["TELEGRAM_ALERT_CHAT_ID"] = "fake_chat_456"
            import alertas_oob
            importlib.reload(alertas_oob)

            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client.post = AsyncMock(return_value=mock_resp)

            with patch("httpx.AsyncClient", return_value=mock_client):
                result = asyncio.get_event_loop().run_until_complete(
                    alertas_oob.alerta_oob("alerta de prueba")
                )

            assert result is True
            mock_client.post.assert_called_once()
            call_args = mock_client.post.call_args
            # La URL debe contener el token
            assert "fake_token_123" in call_args.args[0]
            # El body debe tener chat_id y text
            payload = call_args.kwargs.get("json") or {}
            assert payload.get("chat_id") == "fake_chat_456"
            assert "alerta de prueba" in payload.get("text", "")
        finally:
            for k, v in env_bak.items():
                if v is not None:
                    os.environ[k] = v
            os.environ.pop("TELEGRAM_ALERT_TOKEN", None)
            os.environ.pop("TELEGRAM_ALERT_CHAT_ID", None)

    def test_retorna_false_si_api_falla(self):
        """Si la API responde != 200, retorna False (no lanza excepción)."""
        import httpx

        env_bak = {k: os.environ.pop(k, None) for k in ("TELEGRAM_ALERT_TOKEN", "TELEGRAM_ALERT_CHAT_ID")}
        try:
            os.environ["TELEGRAM_ALERT_TOKEN"] = "tok"
            os.environ["TELEGRAM_ALERT_CHAT_ID"] = "chat"
            import alertas_oob
            importlib.reload(alertas_oob)

            mock_resp = MagicMock()
            mock_resp.status_code = 500
            mock_resp.text = "Internal Server Error"
            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client.post = AsyncMock(return_value=mock_resp)

            with patch("httpx.AsyncClient", return_value=mock_client):
                result = asyncio.get_event_loop().run_until_complete(
                    alertas_oob.alerta_oob("falla")
                )
            assert result is False
        finally:
            for k, v in env_bak.items():
                if v is not None:
                    os.environ[k] = v
            os.environ.pop("TELEGRAM_ALERT_TOKEN", None)
            os.environ.pop("TELEGRAM_ALERT_CHAT_ID", None)


# ═══════════════════════════════════════════════════════════════════════════════
# B7 — ping_deadman: no-op sin URL, hace GET con URL
# ═══════════════════════════════════════════════════════════════════════════════

class TestPingDeadman:

    def test_noop_sin_url(self):
        """Sin la var de entorno retorna False sin llamar a nadie."""
        env_key = "HEALTHCHECKS_TESTJOB_URL"
        bak = os.environ.pop(env_key, None)
        try:
            import alertas_oob
            importlib.reload(alertas_oob)
            result = asyncio.get_event_loop().run_until_complete(
                alertas_oob.ping_deadman("TESTJOB")
            )
            assert result is False
        finally:
            if bak is not None:
                os.environ[env_key] = bak

    def test_hace_get_con_url(self):
        """Con URL configurada hace GET y retorna True si 200."""
        env_key = "HEALTHCHECKS_TESTJOB_URL"
        bak = os.environ.pop(env_key, None)
        try:
            os.environ[env_key] = "https://hc-ping.com/fake-uuid-1234"
            import alertas_oob
            importlib.reload(alertas_oob)

            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client.get = AsyncMock(return_value=mock_resp)

            with patch("httpx.AsyncClient", return_value=mock_client):
                result = asyncio.get_event_loop().run_until_complete(
                    alertas_oob.ping_deadman("TESTJOB")
                )

            assert result is True
            mock_client.get.assert_called_once_with("https://hc-ping.com/fake-uuid-1234")
        finally:
            os.environ.pop(env_key, None)
            if bak is not None:
                os.environ[env_key] = bak

    def test_nombre_case_insensitive(self):
        """ping_deadman("recordatorios") lee HEALTHCHECKS_RECORDATORIOS_URL."""
        env_key = "HEALTHCHECKS_RECORDATORIOS_URL"
        bak = os.environ.pop(env_key, None)
        try:
            os.environ[env_key] = "https://hc-ping.com/rec-uuid"
            import alertas_oob
            importlib.reload(alertas_oob)

            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client.get = AsyncMock(return_value=mock_resp)

            with patch("httpx.AsyncClient", return_value=mock_client):
                result = asyncio.get_event_loop().run_until_complete(
                    alertas_oob.ping_deadman("recordatorios")   # minuscula
                )
            assert result is True
        finally:
            os.environ.pop(env_key, None)
            if bak is not None:
                os.environ[env_key] = bak
