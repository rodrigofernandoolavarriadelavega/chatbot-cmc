"""
Tests de los módulos Alma construidos sobre el kit compartido
(inventario, pacientes, interconsultas, esterilización, finanzas, equipo,
documentos, habilitación, mantención, calidad, exámenes, tareas, liquidaciones,
tablero) + la integración licencia→bot.

Objetivo: red de seguridad. Como una sesión paralela edita main.py/config.py en
vivo, estos tests pinean que cada router importa y que la lógica pura (estados,
fechas, filtro de licencia fail-safe) se mantiene correcta. No requieren servidor
ni red; los que tocan DB usan la sessions.db local y sólo verifican fail-safe.

Correr: pytest tests/test_alma_modules.py   (o: python tests/test_alma_modules.py)
"""
from __future__ import annotations
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "app"))
sys.path.insert(0, str(ROOT))

_CL = ZoneInfo("America/Santiago")
_HOY = datetime.now(_CL).date()

ROUTERS = {
    "inventario_routes": "/alma/api/inventario",
    "pacientes_routes": "/alma/api/pacientes",
    "interconsultas_routes": "/alma/api/interconsultas",
    "esterilizacion_routes": "/alma/api/esterilizacion",
    "finanzas_routes": "/alma/api/finanzas",
    "equipo_routes": "/alma/api/equipo",
    "documentos_routes": "/alma/api/documentos",
    "habilitacion_routes": "/alma/api/habilitacion",
    "mantencion_routes": "/alma/api/mantencion",
    "calidad_routes": "/alma/api/calidad",
    "examenes_routes": "/alma/api/examenes",
    "tareas_routes": "/alma/api/tareas",
    "liquidaciones_routes": "/alma/api/liquidaciones",
    "tablero_routes": "/alma/api/tablero",
}


def test_routers_importan_y_tienen_rutas():
    import importlib
    for mod_name, prefix in ROUTERS.items():
        mod = importlib.import_module(mod_name)
        assert hasattr(mod, "router"), f"{mod_name} sin router"
        assert mod.router.prefix == prefix, f"{mod_name} prefix {mod.router.prefix} != {prefix}"
        assert len(mod.router.routes) >= 1, f"{mod_name} sin rutas"


def test_alma_common_auth_existe():
    import alma_common
    assert callable(alma_common.require_admin)


def test_inventario_estado_stock():
    import inventario_routes as inv
    assert inv._estado_stock(0, 5) == "agotado"
    assert inv._estado_stock(3, 5) == "bajo"
    assert inv._estado_stock(5, 5) == "bajo"     # igual al mínimo = bajo
    assert inv._estado_stock(10, 5) == "ok"


def test_mantencion_add_months_y_estado():
    import mantencion_routes as mant
    # fin de mes: 31-ene + 1 mes = 28/29-feb (no 31)
    d = mant._add_months(date(2026, 1, 31), 1)
    assert d.month == 2 and d.day in (28, 29)
    assert mant._add_months(date(2026, 6, 2), 6) == date(2026, 12, 2)
    # estados
    pasado = (_HOY - timedelta(days=1)).isoformat()
    futuro = (_HOY + timedelta(days=120)).isoformat()
    pronto = (_HOY + timedelta(days=10)).isoformat()
    assert mant._estado_mant(pasado) == "vencida"
    assert mant._estado_mant(futuro) == "ok"
    assert mant._estado_mant(pronto) == "proxima"
    assert mant._estado_mant("") == "sin_plan"


def test_documentos_estado_calc():
    import documentos_routes as doc
    pasado = (_HOY - timedelta(days=1)).isoformat()
    futuro = (_HOY + timedelta(days=120)).isoformat()
    pronto = (_HOY + timedelta(days=10)).isoformat()
    assert doc._estado_calc(pasado, "vigente") == "vencido"
    assert doc._estado_calc(futuro, "vigente") == "vigente"
    assert doc._estado_calc(pronto, "vigente") == "por_vencer"
    assert doc._estado_calc("", "archivado") == "archivado"


def test_tareas_vencida():
    import tareas_routes as tar
    pasado = (_HOY - timedelta(days=2)).isoformat()
    futuro = (_HOY + timedelta(days=2)).isoformat()
    assert tar._vencida(pasado, "pendiente") is True
    assert tar._vencida(pasado, "hecha") is False     # hecha nunca vencida
    assert tar._vencida(futuro, "pendiente") is False
    assert tar._vencida("", "pendiente") is False


def test_equipo_honorarios_default():
    import equipo_routes as eq
    # % derivados de DB Mensual
    assert eq.HONORARIO_PCT_DEFAULT[1] == 71    # Olavarría
    assert eq.HONORARIO_PCT_DEFAULT[13] == 75   # Márquez
    assert eq.HONORARIO_PCT_DEFAULT[66] == 60   # Daniela ortodoncia
    assert 73 not in eq.HONORARIO_PCT_DEFAULT   # Abarca es monto fijo
    # fail-safe: nunca lanza, siempre set
    r = eq.profesionales_en_licencia()
    assert isinstance(r, set)


def test_filtrar_licencia_failsafe(monkeypatch):
    import medilink as ml
    import equipo_routes as eq
    # con alguien de licencia → lo excluye
    monkeypatch.setattr(eq, "profesionales_en_licencia", lambda *a, **k: {60})
    assert ml._filtrar_licencia([60, 1, 73]) == [1, 73]
    # sin nadie de licencia → intacto
    monkeypatch.setattr(eq, "profesionales_en_licencia", lambda *a, **k: set())
    assert ml._filtrar_licencia([60, 1]) == [60, 1]
    # si el helper REVIENTA → fail-safe: no filtra
    def _boom(*a, **k):
        raise RuntimeError("db caída")
    monkeypatch.setattr(eq, "profesionales_en_licencia", _boom)
    assert ml._filtrar_licencia([60, 1]) == [60, 1]
    # lista vacía → vacía
    assert ml._filtrar_licencia([]) == []


def test_tablero_q1_failsafe():
    import tablero_routes as tab
    from session import _conn
    with _conn() as conn:
        # tabla inexistente → default, sin excepción
        assert tab._q1(conn, "SELECT COUNT(*) FROM tabla_que_no_existe_xyz", (), 0) == 0
        # query válida simple
        assert tab._q1(conn, "SELECT 7") == 7


if __name__ == "__main__":
    # runner sin pytest (monkeypatch manual para el test que lo usa)
    import types
    passed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                if "monkeypatch" in fn.__code__.co_varnames[: fn.__code__.co_argcount]:
                    import medilink as ml, equipo_routes as eq
                    _orig = eq.profesionales_en_licencia
                    class _MP:
                        def setattr(self, obj, attr, val): setattr(obj, attr, val)
                    fn(_MP())
                    eq.profesionales_en_licencia = _orig
                else:
                    fn()
                print(f"ok  {name}")
                passed += 1
            except Exception as e:
                print(f"FAIL {name}: {e}")
                raise
    print(f"\n{passed} tests OK")
