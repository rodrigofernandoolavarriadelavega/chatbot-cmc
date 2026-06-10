"""Tests de la cola de aprobación del Autopilot (approvals.py).

Pinea el fix 2026-06-10 (en prod había 6 pendientes con DUPLICADOS de la misma
campaña en tandas separadas, porque el dedupe solo miraba pendientes recientes):
  • dedupe contra pendientes VIVAS de cualquier edad,
  • dedupe contra decididas recientes (no insistir tras un reject),
  • las expiradas SÍ pueden volver a proponerse,
  • expire_stale caduca >48h,
  • remind_owner_stale con throttle (máx 1 aviso/20h),
  • _is_auto JAMÁS auto-aplica un pause.

Usa una SQLite temporal (no toca sessions.db). Correr:
  python3 -m pytest tests/test_autopilot_approvals.py -q
"""
import sqlite3
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "app"))

from autopilot import approvals  # noqa: E402


# ── Infra: DB temporal + flags controlados ───────────────────────────────────

@pytest.fixture()
def db(tmp_path, monkeypatch):
    path = str(tmp_path / "test_pending.db")

    def _factory():
        c = sqlite3.connect(path)
        c.row_factory = sqlite3.Row
        return c

    monkeypatch.setattr(approvals, "_conn", _factory)
    monkeypatch.setattr(approvals, "flag_on",
                        lambda k: k == "AUTOPILOT_ENABLED")  # ENABLED on, AUTOAPPLY off
    return path


class FakeAction:
    """Mínimo que enqueue/_is_auto necesitan de una ProposedAction."""

    def __init__(self, action="decrease_budget", campaign_id="c1",
                 confidence=0.7, needs_approval=True):
        self.action = action
        self.campaign_id = campaign_id
        self.campaign_name = "Campaña test"
        self.current_budget_clp = 10000
        self.proposed_budget_clp = 8000
        self.reason = "test"
        self.confidence = confidence
        self.needs_approval = needs_approval


def _age_pending(db_path, hours):
    """Envejece todas las pendientes `hours` horas hacia el pasado."""
    c = sqlite3.connect(db_path)
    c.execute("UPDATE autopilot_pending SET created_ts = created_ts - ?",
              (int(hours * 3600),))
    c.commit()
    c.close()


# ── enqueue + dedupe ─────────────────────────────────────────────────────────

def test_enqueue_apagado_no_encola(db, monkeypatch):
    monkeypatch.setattr(approvals, "flag_on", lambda k: False)
    assert approvals.enqueue([FakeAction()]) == []


def test_enqueue_basico_y_dedupe_inmediato(db):
    assert len(approvals.enqueue([FakeAction()])) == 1
    assert approvals.enqueue([FakeAction()]) == []          # misma campaña+acción


def test_dedupe_pendiente_vieja_no_se_duplica(db):
    """EL fix: una pendiente de 30h (fuera de la ventana de 20h) bloqueaba antes
    NADA y el clon entraba igual — así nacieron los duplicados de prod."""
    approvals.enqueue([FakeAction()])
    _age_pending(db, 30)
    assert approvals.enqueue([FakeAction()]) == []


def test_acciones_distintas_si_conviven(db):
    approvals.enqueue([FakeAction(action="decrease_budget")])
    assert len(approvals.enqueue([FakeAction(action="increase_budget")])) == 1


def test_rechazada_reciente_no_se_repropone(db):
    [pid] = approvals.enqueue([FakeAction()])
    approvals.reject(pid, by="test")
    assert approvals.enqueue([FakeAction()]) == []          # no insistir tras reject


def test_expirada_si_puede_volver(db):
    [pid] = approvals.enqueue([FakeAction()])
    _age_pending(db, 50)
    assert approvals.expire_stale(hours=48) == 1
    assert approvals.get(pid)["status"] == approvals.ST_EXPIRED
    assert len(approvals.enqueue([FakeAction()])) == 1      # puede re-proponerse


def test_sin_needs_approval_no_se_encola(db):
    assert approvals.enqueue([FakeAction(needs_approval=False)]) == []


def test_keep_y_alert_no_mueven_plata(db):
    assert approvals.enqueue([FakeAction(action="keep"),
                              FakeAction(action="alert")]) == []


# ── expire + recordatorio ────────────────────────────────────────────────────

def test_expire_stale_solo_viejas(db):
    approvals.enqueue([FakeAction()])
    assert approvals.expire_stale(hours=48) == 0            # recién creada


def test_remind_owner_stale_con_throttle(db, monkeypatch):
    sent = []
    monkeypatch.setattr(approvals, "_send_stale_reminder",
                        lambda items, ahorro: sent.append((len(items), ahorro)))
    approvals.enqueue([FakeAction()])
    _age_pending(db, 30)                                    # vieja (>24h) pero no caducada
    assert approvals.remind_owner_stale() == 1
    assert sent == [(1, 2000)]                              # ahorro = 10000−8000
    assert approvals.remind_owner_stale() == 0              # throttle 20h: calla
    assert len(sent) == 1


def test_remind_sin_viejas_no_avisa(db, monkeypatch):
    sent = []
    monkeypatch.setattr(approvals, "_send_stale_reminder",
                        lambda items, ahorro: sent.append(1))
    approvals.enqueue([FakeAction()])                       # recién creada
    assert approvals.remind_owner_stale() == 0
    assert sent == []


# ── Fase 3: _is_auto jamás pausa ─────────────────────────────────────────────

def test_is_auto_nunca_pausa(monkeypatch):
    monkeypatch.setattr(approvals, "flag_on", lambda k: True)  # ENABLED + AUTOAPPLY on
    assert approvals._is_auto(FakeAction(action="pause", confidence=0.99)) is False
    assert approvals._is_auto(FakeAction(action="decrease_budget", confidence=0.9)) is True
    assert approvals._is_auto(FakeAction(action="decrease_budget", confidence=0.5)) is False


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
