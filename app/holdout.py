"""
Grupo de control (holdout) para los mensajes automáticos.

Sin grupo de control no se puede saber si un automático CAUSA citas o si esas
personas habrían agendado igual (auditoría 9-oct-2026: las cifras de retorno
eran estimaciones, no pruebas). Un % fijo de los candidatos NO recibe el
mensaje; se registra `<rail>_holdout` en el mismo punto donde se habría enviado,
y el panel compara agendamiento contra `<rail>_enviado`.

Asignación determinista por (rail, últimos 9 dígitos): la misma persona queda
siempre en el mismo grupo para ese automático (no se "contamina" entre
semanas) y es independiente entre automáticos.

HOLDOUT_PCT (env, default 10). 0 apaga el control.
"""
import hashlib
import os


def _pct() -> int:
    try:
        return max(0, min(50, int(os.getenv("HOLDOUT_PCT", "10"))))
    except ValueError:
        return 10


def en_grupo_control(phone: str, rail: str) -> bool:
    pct = _pct()
    if pct <= 0:
        return False
    clave = "".join(ch for ch in (phone or "") if ch.isdigit())[-9:] or (phone or "")
    h = hashlib.sha256(f"{rail}:{clave}".encode()).digest()
    return int.from_bytes(h[:4], "big") % 100 < pct
