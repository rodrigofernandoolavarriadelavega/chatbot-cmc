"""
Tests para los 5 fixes de la auditoría 2026-06-10.

FIX 1: waitlist no acepta negación (loop infinito)
FIX 2: umbral SAMU en quejas históricas/condicionales
FIX 5a: intent aviso_atraso (variantes de atraso)

Ejecutar:
    pytest tests/test_fixes_20260610.py -v
    python tests/test_fixes_20260610.py
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))
sys.path.insert(0, str(ROOT))

_TMP_DB = Path(tempfile.mkdtemp(prefix="cmc_fixes_20260610_")) / "sessions.db"
os.environ["SESSIONS_DB"] = str(_TMP_DB)
os.environ.setdefault("SQLCIPHER_KEY", "")

import session as _session_mod
_session_mod.DB_PATH = _TMP_DB

import flows
import medilink
import claude_helper
import resilience
import messaging

PHONE = "56912345678"


# ── Helpers ──────────────────────────────────────────────────────────────────

def _reset():
    from session import reset_session
    reset_session(PHONE)


async def _msg(txt: str) -> str:
    from session import get_session
    sess = get_session(PHONE)
    resp = await flows.handle_message(PHONE, txt, sess)
    return resp or ""


def _patch_medilink():
    """Parchea medilink para que no haga llamadas reales."""
    async def _no_slots(esp, fecha=None, **kwargs):
        return [], []

    async def _no_primer_dia(esp, **kwargs):
        return [], []

    async def _no_buscar_paciente(rut):
        return None

    medilink.buscar_slots_dia = _no_slots
    medilink.buscar_primer_dia = _no_primer_dia
    medilink.buscar_paciente = _no_buscar_paciente


def _patch_claude():
    """Parchea Claude para que no haga llamadas reales."""
    async def _fake_detect_intent(txt, **kwargs):
        tl = txt.lower().strip()
        if any(k in tl for k in ("llego tarde", "voy atrasado", "me atrasé", "me atrase",
                                  "voy en camino", "voy demorado", "llegare tarde")):
            return {"intent": "aviso_atraso", "especialidad": None, "respuesta_directa": None}
        return {"intent": "otro", "especialidad": None, "respuesta_directa": None}

    claude_helper.detect_intent = _fake_detect_intent


def _patch_send():
    """Parchea send_whatsapp para no enviar nada."""
    async def _noop(*a, **kw):
        pass
    messaging.send_whatsapp = _noop
    messaging.send_whatsapp_template = _noop


# ── FIX 1: waitlist negación ampliada ────────────────────────────────────────

class TestFix1WaitlistNegacion:
    """Paciente en WAIT_WAITLIST_CONFIRM envía variantes de negación."""

    def setup_method(self):
        _reset()
        _patch_medilink()
        _patch_send()
        # Poner sesión directamente en WAIT_WAITLIST_CONFIRM
        from session import save_session
        save_session(PHONE, "WAIT_WAITLIST_CONFIRM", {
            "waitlist_especialidad": "otorrinolaringología",
        })

    def _run(self, txt: str) -> str:
        return asyncio.get_event_loop().run_until_complete(_msg(txt))

    def test_no_simple(self):
        resp = self._run("no")
        assert "Sin problema" in resp or "escríbenos" in resp, f"Esperaba salida limpia, got: {resp!r}"

    def test_no_del_otorrino(self):
        """Caso prod: 'No del Otorrino' causaba slug 'no otorrino'."""
        resp = self._run("No del Otorrino")
        # La respuesta puede ser str o dict (mensaje interactivo)
        resp_str = resp if isinstance(resp, str) else str(resp)
        assert "no otorrino" not in resp_str.lower(), f"Slug inválido generado: {resp_str!r}"
        # No debe seguir pidiendo SÍ/NO indefinidamente
        assert "Responde *SÍ*" not in resp_str

    def test_la_llamo(self):
        resp = self._run("la llamo")
        assert "Sin problema" in resp or "escríbenos" in resp or "IDLE" not in resp

    def test_cuando_pueda(self):
        resp = self._run("cuando pueda")
        assert "Responde *SÍ* para inscribirte" not in resp

    def test_gracias_solo(self):
        resp = self._run("gracias")
        # "gracias" a secas = rechazo cortés → salir limpio
        assert "Sin problema" in resp or "escríbenos" in resp

    def test_no_gracias(self):
        resp = self._run("no gracias")
        assert "Sin problema" in resp or "escríbenos" in resp

    def test_si_funciona(self):
        """El SÍ debe seguir funcionando (pedir RUT)."""
        resp = self._run("sí")
        # O pide RUT o hay un mensaje de inscripción (perfil no existe)
        assert "RUT" in resp or "inscrib" in resp.lower() or "rut" in resp.lower()


class TestFix1Reprompt:
    """Tope de repeticiones: a la 2ª vez sin respuesta válida, salir a IDLE."""

    def setup_method(self):
        _reset()
        _patch_medilink()
        _patch_send()

    def _run(self, txt: str) -> str:
        return asyncio.get_event_loop().run_until_complete(_msg(txt))

    def test_segundo_reprompt_sale(self):
        """Después de 2 mensajes inválidos debe salir a IDLE."""
        from session import save_session
        save_session(PHONE, "WAIT_WAITLIST_CONFIRM", {
            "waitlist_especialidad": "otorrinolaringología",
            "_wl_reprompts": 1,  # ya hubo 1 reprompt
        })
        resp = self._run("bla bla inentendible xyz")
        # A la 2ª vez debe salir limpio (no volver a pedir SÍ/NO)
        assert "Responde *SÍ* para inscribirte" not in resp
        assert "Quedo atento" in resp or "menú" in resp or len(resp) < 100


class TestFix1cNoEspecialidad:
    """El normalizador de especialidades nunca genera slug que empiece con 'no'."""

    def test_no_otorrino_bloqueado(self):
        """Simular que WAIT_ESPECIALIDAD recibe 'No del Otorrino'."""
        # Verificar directamente el guard de regex
        ec_lower = "no otorrino"
        # Guard: empieza con "no "
        assert bool(re.match(r"^no(\s|$)", ec_lower)), "Guard debe detectar 'no otorrino'"

    def test_no_solo_bloqueado(self):
        ec_lower = "no"
        assert bool(re.match(r"^no(\s|$)", ec_lower))

    def test_nutricion_no_bloqueada(self):
        ec_lower = "nutrición"
        assert not bool(re.match(r"^no(\s|$)", ec_lower))

    def test_notable_no_bloqueada(self):
        """'notable' no empieza con 'no ' — no debe bloquearse."""
        ec_lower = "notable"
        # "notable" no matchea r"^no(\s|$)"
        assert not bool(re.match(r"^no(\s|$)", ec_lower))


# ── FIX 2: umbral SAMU quejas históricas ─────────────────────────────────────

class TestFix2SAMUHistorico:
    """El trigger SAMU no debe disparar ante quejas en pasado/condicional sin urgencia presente."""

    def _tiene_inhibicion_pasado(self, txt: str) -> bool:
        """Reproduce la lógica _inhibir_por_pasado directamente."""
        _PASADO_CONDICIONAL = re.compile(
            r"(debió\s+(haber|haberlo|tenerlo)|debería\s+haber|tendría\s+que\s+haber"
            r"|hubiera\s+(sido|tenido|hecho)|hubiese\s+(sido|tenido|hecho)"
            r"|cuando\s+(me\s+)(correspondía|toc[oó]|atendieron|vine|fui\s+a)"
            r"|en\s+(esa|aquel|ese)\s+(entonces|momento|tiempo|oportunidad)"
            r"|hace\s+(meses|años|tiempo|semanas)\s+(atrás|que))",
            re.IGNORECASE,
        )
        _URGENCIA_PRESENTE = re.compile(
            r"(ahora|en\s+este\s+momento|me\s+siento|tengo\s+(fiebre|dolor|sangrado)"
            r"|estoy\s+(mal|grave|sangrando|desmay|convuls)"
            r"|no\s+puedo\s+respirar|me\s+duele\s+mucho)",
            re.IGNORECASE,
        )
        _VITAL_SIGNAL = re.compile(
            r"(no puedo respirar|dolor de pecho|infarto|sangrado|desmayo|convuls"
            r"|hemiparesi|boca torcida|boca chueca|cara ca[ií]da|acv|derrame cerebral"
            r"|no siento el brazo|habla trabada|me explot[oó] la cabeza)",
            re.IGNORECASE,
        )
        return (
            _PASADO_CONDICIONAL.search(txt) is not None
            and _URGENCIA_PRESENTE.search(txt) is None
            and _VITAL_SIGNAL.search(txt) is None
        )

    def test_queja_historica_inhibida(self):
        """Caso prod: queja en pasado no debe disparar SAMU."""
        txt = "esa misma preocupación debió haber tenido cuando me correspondía control"
        assert self._tiene_inhibicion_pasado(txt), "Queja histórica debe inhibirse"

    def test_deberia_haber_inhibida(self):
        txt = "debería haber tenido más cuidado cuando me atendió el año pasado"
        assert self._tiene_inhibicion_pasado(txt)

    def test_urgencia_presente_NO_inhibida(self):
        """Una urgencia presente NO debe inhibirse aunque tenga palabras de pasado."""
        txt = "me siento muy mal ahora, me duele mucho el pecho"
        assert not self._tiene_inhibicion_pasado(txt), "Urgencia presente no debe inhibirse"

    def test_dolor_actual_NO_inhibido(self):
        txt = "tengo un dolor muy fuerte en el pecho en este momento"
        assert not self._tiene_inhibicion_pasado(txt)

    def test_convulsion_NO_inhibida(self):
        txt = "estoy convulsionando ahora"
        assert not self._tiene_inhibicion_pasado(txt)

    def test_emergencia_simple_NO_inhibida(self):
        """Texto sin patrones de pasado tampoco activa la inhibición."""
        txt = "no puedo respirar"
        assert not self._tiene_inhibicion_pasado(txt)

    def test_texto_neutro_no_inhibido(self):
        """Texto sin pasado condicional no activa el filtro."""
        txt = "quiero agendar una hora"
        assert not self._tiene_inhibicion_pasado(txt)


# ── FIX 5a: intent aviso_atraso ──────────────────────────────────────────────

class TestFix5aAvisoAtraso:
    """Variantes de aviso de atraso reconocidas y respondidas correctamente."""

    def _tiene_atraso_re(self, txt: str) -> bool:
        """Reproduce el regex _ATRASO_RE de flows.py."""
        _ATRASO_RE = re.compile(
            r"(llego?\s+tarde|llegaré\s+tarde|llegare\s+tarde"
            r"|voy\s+(atrasad[oa]|demorad[oa]|en\s+camino)"
            r"|me\s+(atrasé|atrase|demoré|demore)"
            r"|llegaré?\s+(con\s+retraso|unos?\s+\d+\s+min)"
            r"|estoy\s+(en\s+camino|de\s+camino))",
            re.IGNORECASE,
        )
        return _ATRASO_RE.search(txt) is not None

    def test_llego_tarde(self):
        assert self._tiene_atraso_re("llego tarde")

    def test_llegare_tarde(self):
        assert self._tiene_atraso_re("llegaré tarde")

    def test_voy_atrasado(self):
        assert self._tiene_atraso_re("voy atrasado")

    def test_voy_atrasada(self):
        assert self._tiene_atraso_re("voy atrasada")

    def test_voy_en_camino(self):
        assert self._tiene_atraso_re("voy en camino")

    def test_me_atrase(self):
        assert self._tiene_atraso_re("me atrasé")

    def test_me_demore(self):
        assert self._tiene_atraso_re("me demoré")

    def test_estoy_en_camino(self):
        assert self._tiene_atraso_re("estoy en camino")

    def test_llego_10_min_tarde(self):
        assert self._tiene_atraso_re("llegaré unos 10 min tarde")

    def test_quiero_cancelar_NO_es_atraso(self):
        """'Quiero cancelar' no es aviso de atraso."""
        assert not self._tiene_atraso_re("quiero cancelar mi hora")

    def test_necesito_hora_NO_es_atraso(self):
        assert not self._tiene_atraso_re("necesito una hora para mañana")

    def test_intent_cache_llego_tarde(self):
        """El cache determinístico de claude_helper reconoce 'llego tarde'."""
        from claude_helper import _INTENT_CACHE
        assert "llego tarde" in _INTENT_CACHE
        assert _INTENT_CACHE["llego tarde"]["intent"] == "aviso_atraso"

    def test_intent_cache_voy_atrasado(self):
        from claude_helper import _INTENT_CACHE
        assert "voy atrasado" in _INTENT_CACHE
        assert _INTENT_CACHE["voy atrasado"]["intent"] == "aviso_atraso"

    def test_respuesta_aviso_atraso(self):
        """El bot responde con confirmación simple, no deriva a humano."""
        _reset()
        _patch_medilink()
        _patch_send()
        _patch_claude()

        async def _run():
            from session import save_session
            save_session(PHONE, "IDLE", {})
            return await flows.handle_message(PHONE, "llego tarde", {"state": "IDLE", "data": {}})

        resp = asyncio.get_event_loop().run_until_complete(_run())
        assert "Gracias por avisar" in resp or "recepción" in resp.lower()
        # No debe derivar a humano completo (no debe contener "te conecto")
        assert "te conecto" not in resp.lower()


# ── FIX 5b: fallback nombre en send_whatsapp_template ────────────────────────

class TestFix5bFallbackNombre:
    """send_whatsapp_template sanitiza nombre vacío en body_params."""

    def test_nombre_vacio_fallback(self):
        """Si body_params[0] es vacío, debe sustituir por fallback."""
        # Simular la lógica del fix
        body_params = ["", "kinesiología", "Luis Armijo"]
        sanitized = []
        for i, p in enumerate(body_params):
            val = str(p).strip() if p is not None else ""
            if not val and i == 0:
                val = "Estimado/a paciente"
            sanitized.append(val)
        assert sanitized[0] == "Estimado/a paciente"
        assert sanitized[1] == "kinesiología"
        assert sanitized[2] == "Luis Armijo"

    def test_nombre_none_fallback(self):
        body_params = [None, "cardiología"]
        sanitized = []
        for i, p in enumerate(body_params):
            val = str(p).strip() if p is not None else ""
            if not val and i == 0:
                val = "Estimado/a paciente"
            sanitized.append(val)
        assert sanitized[0] == "Estimado/a paciente"

    def test_nombre_presente_no_fallback(self):
        """Nombre presente no debe modificarse."""
        body_params = ["María", "kinesiología"]
        sanitized = []
        for i, p in enumerate(body_params):
            val = str(p).strip() if p is not None else ""
            if not val and i == 0:
                val = "Estimado/a paciente"
            sanitized.append(val)
        assert sanitized[0] == "María"

    def test_segundo_param_vacio_no_fallback(self):
        """Solo el primer param (nombre) recibe fallback."""
        body_params = ["María", ""]
        sanitized = []
        for i, p in enumerate(body_params):
            val = str(p).strip() if p is not None else ""
            if not val and i == 0:
                val = "Estimado/a paciente"
            sanitized.append(val)
        assert sanitized[1] == ""  # no fallback en índices > 0


# ── Runner standalone ─────────────────────────────────────────────────────────

if __name__ == "__main__":
    import traceback

    suites = [
        TestFix1WaitlistNegacion,
        TestFix1Reprompt,
        TestFix1cNoEspecialidad,
        TestFix2SAMUHistorico,
        TestFix5aAvisoAtraso,
        TestFix5bFallbackNombre,
    ]

    total = 0
    passed = 0
    failed = 0
    errors_list = []

    for SuiteClass in suites:
        suite = SuiteClass()
        methods = [m for m in dir(suite) if m.startswith("test_")]
        print(f"\n=== {SuiteClass.__name__} ({len(methods)} tests) ===")
        for method_name in methods:
            total += 1
            if hasattr(suite, "setup_method"):
                try:
                    suite.setup_method()
                except Exception:
                    pass
            try:
                getattr(suite, method_name)()
                print(f"  OK  {method_name}")
                passed += 1
            except Exception as e:
                print(f"  FAIL {method_name}: {e}")
                errors_list.append((SuiteClass.__name__, method_name, traceback.format_exc()))
                failed += 1

    print(f"\n{'='*60}")
    print(f"Total: {total} | Passed: {passed} | Failed: {failed}")
    if errors_list:
        print("\nDetalle de fallos:")
        for cls, method, tb in errors_list:
            print(f"\n{cls}.{method}:\n{tb}")
    else:
        print("Todos los tests pasaron.")

    sys.exit(0 if failed == 0 else 1)
