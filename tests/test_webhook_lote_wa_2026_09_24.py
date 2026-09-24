"""Bug 2026-09-24: el webhook de WhatsApp leía solo entry[0] / changes[0] /
messages[0]. Meta agrupa actualizaciones en un solo POST, así que el resto se
descartaba: 1.326 de 1.570 mensajes en 24 h quedaron en "sent" aunque el
paciente respondió (watchdog de entrega en APAGÓN al 47%), y un mensaje
entrante en la 2ª posición del lote se perdía. `_partir_lote_wa` parte el lote
en unidades que el handler de siempre sabe procesar.
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))
sys.path.insert(0, str(ROOT))

from main import _partir_lote_wa, _request_interno  # noqa: E402

META = {"display_phone_number": "56966610737", "phone_number_id": "1"}


def _st(wamid, status):
    return {"id": wamid, "recipient_id": "56911111111", "status": status}


def _msg(mid, frm, texto):
    return {"id": mid, "from": frm, "type": "text", "text": {"body": texto}}


def _payload(*entries):
    return {"object": "whatsapp_business_account", "entry": list(entries)}


def _entry(*values):
    return {"id": "waba", "changes": [{"field": "messages", "value": {"messaging_product": "whatsapp",
                                                                   "metadata": META, **v}} for v in values]}


def _todo(unidades, clave):
    return [x for u in unidades for e in u["entry"] for c in e["changes"]
            for x in c["value"].get(clave, [])]


def test_payload_unitario_no_se_parte():
    p = _payload(_entry({"messages": [_msg("m1", "569A", "hola")],
                         "contacts": [{"wa_id": "569A", "profile": {"name": "A"}}]}))
    assert _partir_lote_wa(p) == [p]


def test_lote_de_estados_en_varios_entry_no_pierde_ninguno():
    p = _payload(_entry({"statuses": [_st("w1", "delivered"), _st("w2", "read")]}),
                 _entry({"statuses": [_st("w3", "delivered")]}, {"statuses": [_st("w4", "read")]}))
    u = _partir_lote_wa(p)
    assert len(u) == 3
    assert [s["id"] for s in _todo(u, "statuses")] == ["w1", "w2", "w3", "w4"]
    for x in u:  # cada unidad es lo que el handler lee en [0][0]
        assert len(x["entry"]) == 1 and len(x["entry"][0]["changes"]) == 1


def test_dos_mensajes_de_pacientes_distintos_en_un_change():
    p = _payload(_entry({"messages": [_msg("m1", "569A", "hola"), _msg("m2", "569B", "hora eco")],
                         "contacts": [{"wa_id": "569A"}, {"wa_id": "569B"}],
                         "statuses": [_st("w9", "read")]}))
    u = _partir_lote_wa(p)
    assert len(u) == 2
    v1, v2 = (x["entry"][0]["changes"][0]["value"] for x in u)
    assert v1["messages"][0]["id"] == "m1" and v1["contacts"] == [{"wa_id": "569A"}]
    assert v2["messages"][0]["id"] == "m2" and v2["contacts"] == [{"wa_id": "569B"}]
    assert v1["statuses"][0]["id"] == "w9" and "statuses" not in v2  # sin doble conteo
    assert v1["metadata"] == META and v2["metadata"] == META


def test_request_interno_entrega_el_body_y_marca_el_scope():
    p = _payload(_entry({"statuses": [_st("w1", "sent")]}))
    req = _request_interno(p)
    assert req.scope["cmc_webhook_split"] is True
    assert json.loads(asyncio.run(req.body())) == p
