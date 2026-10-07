"""
_job_watchdog_blast contaba "131042" como texto en el log: sumaba sus propios
resúmenes ("err_131042=0") y teléfonos/RUT → el 7-oct pausó el blast por "28
errores" que eran 0 reales. Ahora cuenta desde message_statuses.
"""
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))
TMP_DB = Path(tempfile.mkdtemp(prefix="cmc_test_wdb_")) / "s.db"
os.environ["SESSIONS_DB"] = str(TMP_DB)
os.environ.setdefault("SQLCIPHER_KEY", "")

import session  # noqa: E402
session.DB_PATH = TMP_DB
import jobs  # noqa: E402


def _st(wamid, status, code, horas_atras=1):
    with session.db() as c:
        c.execute("INSERT INTO message_statuses (wamid, phone, status, ts, error_code) "
                  "VALUES (?,?,?, datetime('now', ?), ?)",
                  (wamid, "569", status, f"-{horas_atras} hours", code))
        c.commit()


def test_cuenta_solo_fallos_reales_de_24h():
    with session.db() as c:
        c.execute("DELETE FROM message_statuses"); c.commit()
    _st("a", "failed", "131042")
    _st("b", "failed", "131042")
    _st("c", "failed", "132000")
    _st("d", "failed", "131026")          # número sin WhatsApp: no cuenta
    _st("e", "delivered", None)
    _st("f", "failed", "131042", horas_atras=30)  # fuera de las 24 h
    assert jobs._errores_meta_24h() == (2, 1)


def test_texto_en_el_log_ya_no_suma():
    src = (ROOT / "app" / "jobs.py").read_text(encoding="utf-8")
    assert 'log_tail.count("131042")' not in src
