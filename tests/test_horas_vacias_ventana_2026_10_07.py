"""
horas_vacias mandaba SIEMPRE texto libre: 47 de 59 avisos (30 d) fallaron con
131047 (ventana 24 h cerrada). Ahora: ventana abierta → texto; cerrada → solo
template horas_liberadas_v1 con opt-in de marketing; si no, skip.
"""
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))


def _src(p):
    return (ROOT / "app" / p).read_text(encoding="utf-8")


def test_job_revisa_ventana_y_consent_antes_de_enviar():
    s = _src("jobs.py")
    i = s.index("async def _job_horas_vacias") if "async def _job_horas_vacias" in s else 0
    cuerpo = s[i:i + 20000]
    assert "_hv_is_window_open(phone)" in cuerpo
    assert "horas_vacias_skip_ventana" in cuerpo
    assert '"horas_liberadas_v1"' in cuerpo and "_hv_has_mkt(phone)" in cuerpo


def test_botones_del_template_tienen_handler():
    import json, unicodedata, re
    tpl = json.loads((ROOT / "templates" / "whatsapp_templates" /
                      "horas_liberadas_v1.DRAFT.json").read_text(encoding="utf-8"))
    body = next(c for c in tpl["components"] if c["type"] == "BODY")["text"]
    assert sorted(set(re.findall(r"\{\{(\d+)\}\}", body))) == ["1", "2", "3", "4"]
    from triage_ges import normalizar_texto_paciente
    s = _src("flows.py")
    for b in next(c for c in tpl["components"] if c["type"] == "BUTTONS")["buttons"]:
        n = normalizar_texto_paciente(b["text"])
        assert f'"{n}"' in s, f"botón {b['text']!r} → tl_norm {n!r} sin handler"
