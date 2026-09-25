"""_paciente_ortodoncia_activo consultaba columnas que no existen en
bi.fact_atenciones (rut, id_profesional, fecha_atencion) → fallaba SIEMPRE y
devolvía 0: nadie contaba como "ya en tratamiento de ortodoncia" (2026-09-25).
"""
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(ROOT / "app"))

import harness_50  # noqa: E402,F401
import flows  # noqa: E402
import winback  # noqa: E402


class _Cur:
    def __init__(self, log): self.log = log
    def __enter__(self): return self
    def __exit__(self, *a): pass
    def execute(self, sql, params): self.log.append((sql, params))
    def fetchone(self): return [4]


class _Conn:
    def __init__(self, log): self.log = log
    def __enter__(self): return self
    def __exit__(self, *a): pass
    def cursor(self): return _Cur(self.log)


def test_query_usa_columnas_reales_y_rut_normalizado(monkeypatch):
    log = []
    monkeypatch.setattr(winback, "bi_conn", lambda: _Conn(log))
    monkeypatch.setattr(flows, "get_profile", lambda ph: {"rut": "22.123.456-7"})
    assert asyncio.run(flows._paciente_ortodoncia_activo("56900000001")) == 4
    sql, params = log[0]
    assert "dim_paciente" in sql and "profesional_id = 66" in sql and "a.fecha >=" in sql
    for col in ("fecha_atencion", "id_profesional", "WHERE rut"):
        assert col not in sql, col
    assert params == ("221234567",)
