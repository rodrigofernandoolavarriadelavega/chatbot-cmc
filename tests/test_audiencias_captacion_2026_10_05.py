"""Audiencias de captación Meta (exclusión + semilla): filtros de consentimiento,
nombres no sensibles, flag apagado = cero llamadas, solo hashes viajan.

DB temporales y API de Meta mockeada: este test NO toca prod ni Meta."""
from __future__ import annotations

import asyncio
import hashlib
import json
import re
import sqlite3
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))

import pytest  # noqa: E402

import audiencias_captacion as AC  # noqa: E402
import config  # noqa: E402
import custom_audiences_sync as CAS  # noqa: E402

HOY = date(2026, 10, 5)


def _tel(n: int) -> str:
    return f"9{n:08d}"           # 9 dígitos, celular chileno


def _bi(opt_out=(), ok=(), no=()):
    norm = lambda xs: {AC._clave(x) for x in xs}   # noqa: E731 — igual que cargar_bi_sets
    return {"opt_out": norm(opt_out), "consent_ok": norm(ok), "consent_no": norm(no)}


@pytest.fixture
def mundo(tmp_path):
    c = sqlite3.connect(tmp_path / "sessions.db")
    c.row_factory = sqlite3.Row
    c.executescript("""
        CREATE TABLE bi_pagos_caja (pago_id INTEGER PRIMARY KEY, fecha TEXT, id_profesional INTEGER,
                                    id_paciente INTEGER, monto INTEGER);
        CREATE TABLE equipo_cmc (id_medilink INTEGER, pct_honorario INTEGER);
        CREATE TABLE contact_tags (phone TEXT, tag TEXT, ts TEXT);
        CREATE TABLE privacy_consents (phone TEXT, status TEXT, revoked_at TEXT);
        CREATE TABLE gdpr_deletions (rut TEXT, phone TEXT);
        CREATE TABLE citas_bot (phone TEXT, id_paciente_medilink INTEGER, es_tercero INTEGER);
        CREATE TABLE contact_profiles (phone TEXT, rut TEXT, nombre TEXT);
    """)
    h = sqlite3.connect(tmp_path / "heatmap.db")
    h.execute("CREATE TABLE pacientes_heatmap (id INTEGER, rut TEXT, nombre TEXT, apellidos TEXT, "
              "fecha_nacimiento TEXT, celular TEXT)")

    class M:
        pass
    m = M()
    m.c, m.h = c, h
    m._pago = 0

    def paciente(pid, cel, nombre="Ana", ape="Perez", nac="1985-04-02", rut=None):
        h.execute("INSERT INTO pacientes_heatmap VALUES (?,?,?,?,?,?)",
                  (pid, rut or f"{10000000 + pid}-K", nombre, ape, nac, cel))

    def pago(pid, fecha="2026-08-01", monto=20000, prof=1):
        m._pago += 1
        c.execute("INSERT INTO bi_pagos_caja VALUES (?,?,?,?,?)", (m._pago, fecha, prof, pid, monto))

    m.paciente, m.pago = paciente, pago
    return m


def _listas(m, bi, **kw):
    return AC.construir_listas(m.c, m.h, bi, HOY, **kw)


def _k(n):
    return _tel(n)


# ── Filtros ───────────────────────────────────────────────────────────────────
def test_exclusion_y_filtros_de_consentimiento(mundo):
    m = mundo
    # A: sano, con consent
    m.paciente(1, _tel(1)); m.pago(1)
    # B: baja en BI (opt_outs_marketing, formato con 56)
    m.paciente(2, _tel(2)); m.pago(2)
    # C: tag local marketing_opt_out (numero equivocado / baja)
    m.paciente(3, _tel(3)); m.pago(3)
    m.c.execute("INSERT INTO contact_tags VALUES (?,?,?)", ("56" + _tel(3), "marketing_opt_out", "x"))
    # D: privacy_consents revocado
    m.paciente(4, _tel(4)); m.pago(4)
    m.c.execute("INSERT INTO privacy_consents VALUES (?,?,?)", ("56" + _tel(4), "accepted", "2026-01-01"))
    # E: menor de edad
    m.paciente(5, _tel(5), nac="2015-01-01"); m.pago(5)
    # F y G: comparten teléfono
    m.paciente(6, _tel(6)); m.pago(6)
    m.paciente(7, _tel(6), nombre="Luis", rut="20000000-1"); m.pago(7)
    # H: derecho al olvido por RUT (celular distinto en la fila borrada)
    m.paciente(8, _tel(8), rut="12345678-5"); m.pago(8)
    m.c.execute("INSERT INTO gdpr_deletions VALUES (?,?)", ("12.345.678-5", "56911111111"))
    # I: pago hace 30 meses (fuera de ventana de 24)
    m.paciente(9, _tel(9)); m.pago(9, fecha="2024-04-01")
    # J: teléfono basura / fijo
    m.paciente(10, "dfgfdfd"); m.pago(10)
    m.paciente(11, "412965226"); m.pago(11)
    # K: consent rechazado en BI
    m.paciente(12, _tel(12)); m.pago(12)
    # L: sin consent registrado
    m.paciente(13, _tel(13)); m.pago(13)
    # M: el bot conoce OTRO nombre para ese WhatsApp (número reciclado/tercero)
    m.paciente(14, _tel(14), nombre="Carmen", ape="Soto"); m.pago(14)
    m.c.execute("INSERT INTO contact_profiles VALUES (?,?,?)", ("56" + _tel(14), "", "Baldremina Quezada"))
    # N: teléfono solo vía cita agendada a un tercero -> NO es del paciente
    m.paciente(15, ""); m.pago(15)
    m.c.execute("INSERT INTO citas_bot VALUES (?,?,?)", ("56" + _tel(15), 15, 1))
    # O: teléfono del paciente solo conocido por el bot (cita propia)
    m.paciente(16, ""); m.pago(16)
    m.c.execute("INSERT INTO citas_bot VALUES (?,?,?)", ("56" + _tel(16), 16, 0))
    # P: teléfono solo vía contact_profiles + RUT
    m.paciente(17, "", rut="15555555-5"); m.pago(17)
    m.c.execute("INSERT INTO contact_profiles VALUES (?,?,?)", ("56" + _tel(17), "15.555.555-5", "Ana Perez"))

    con_consent = [_tel(i) for i in (1, 2, 3, 4, 5, 6, 8, 9, 14, 16, 17)]
    bi = _bi(opt_out=["56" + _tel(2)], ok=con_consent, no=[_tel(12)])

    estricto = _listas(m, bi, exclusion_exige_consent=True)
    assert set(estricto["exclusion"]) == {_tel(1), _tel(16), _tel(17)}

    laxo = _listas(m, bi, exclusion_exige_consent=False)
    # sin exigir consent entra además quien no tiene registro (13); NUNCA bajas/menores/compartidos/olvidados/rechazados
    assert set(laxo["exclusion"]) == {_tel(1), _tel(13), _tel(16), _tel(17)}
    for prohibido in (2, 3, 4, 5, 6, 8, 9, 10, 11, 12, 14, 15):
        assert _tel(prohibido) not in laxo["exclusion"], prohibido
    assert laxo["stats"]["exclusion"]["final"] == 4


def test_semilla_ordena_por_margen_con_pct_del_profesional(mundo):
    m = mundo
    m.c.executemany("INSERT INTO equipo_cmc VALUES (?,?)", [(1, 50), (2, 90), (3, 0)])
    # margen: 1 -> 10.000 * .5 ; 2 -> 10.000 * .1 ; prof 3 (pct 0 = sin dato -> 70) ; prof 99 ausente -> 70
    for pid, prof, monto in [(1, 1, 100000), (2, 2, 100000), (3, 3, 100000), (4, 99, 100000)]:
        m.paciente(pid, _tel(pid)); m.pago(pid, prof=prof, monto=monto)
    # paciente 5: pago hace 20 meses -> está en exclusión (24m) pero NO en semilla (18m)
    m.paciente(5, _tel(5)); m.pago(5, fecha="2025-02-01", monto=900000)
    bi = _bi(ok=[_tel(i) for i in range(1, 6)])
    r = _listas(m, bi, semilla_top=1.0)
    assert r["semilla"][0] == _tel(1)                        # 50k
    assert set(r["semilla"][1:3]) == {_tel(3), _tel(4)}      # 30k y 30k
    assert r["semilla"][3] == _tel(2)                        # 10k
    assert _tel(5) not in r["semilla"] and _tel(5) in r["exclusion"]
    top = _listas(m, bi, semilla_top=0.25)
    assert top["semilla"] == [_tel(1)]


def test_semilla_siempre_exige_consent(mundo):
    m = mundo
    m.paciente(1, _tel(1)); m.pago(1)
    assert _listas(m, _bi(), semilla_top=1.0)["semilla"] == []
    assert _listas(m, _bi(), exclusion_exige_consent=False)["semilla"] == []


# ── Nombres no sensibles ──────────────────────────────────────────────────────
def test_nombres_y_descripciones_no_revelan_nada_sensible():
    textos = [AC.NOMBRE_EXCLUSION, AC.NOMBRE_SEMILLA, AC.NOMBRE_SIMILAR,
              AC.DESCRIPCION_EXCLUSION, AC.DESCRIPCION_SEMILLA, AC.DESCRIPCION_SIMILAR]
    for t in textos:
        bajo = re.sub(r"\s+", " ", t.lower())
        for term in AC._TERMINOS_PROHIBIDOS:
            assert term not in bajo, (t, term)
    from medilink import PROFESIONALES
    for p in PROFESIONALES.values():
        esp = re.sub(r"[^a-z]", "", p["especialidad"].lower())
        if len(esp) > 4:
            assert not any(esp in re.sub(r"[^a-z]", "", t.lower()) for t in textos)


# ── Flag y llamadas a Meta ────────────────────────────────────────────────────
class FakeMeta:
    def __init__(self, monkeypatch):
        self.gets, self.posts = [], []
        self._n = 0

        async def _get(url, params=None):
            self.gets.append((url, params))
            return {"data": []}

        async def _post(url, data):
            self.posts.append((url, data))
            self._n += 1
            return {"id": f"AUD_{self._n}"}
        monkeypatch.setattr(CAS, "_meta_get", _get)
        monkeypatch.setattr(CAS, "_meta_post", _post)
        monkeypatch.setattr(CAS, "_META_ACCESS_TOKEN", "tok-test")


def test_flag_apagado_no_llama_a_nada(monkeypatch):
    meta = FakeMeta(monkeypatch)
    assert config.META_AUDIENCIAS_CAPTACION_ACTIVE is False        # default
    assert config.META_AUDIENCIA_EXCLUSION_EXIGE_CONSENT is True   # default
    llamado = []
    monkeypatch.setattr(AC, "construir_listas_prod", lambda *a, **k: llamado.append(1))
    monkeypatch.setattr(AC, "cargar_bi_sets", lambda: llamado.append(1))
    res = asyncio.run(AC.sync_audiencias_captacion(HOY))
    assert res == {"status": "flag_off"}
    asyncio.run(AC.job_audiencias_captacion())
    assert meta.gets == [] and meta.posts == [] and llamado == []


def _mundo_grande(m, n_consent=130):
    for i in range(1, n_consent + 1):
        m.paciente(i, _tel(i)); m.pago(i, monto=10000 + i)
    return _bi(ok=[_tel(i) for i in range(1, n_consent + 1)])


def _sync_con(monkeypatch, m, bi, **kw):
    monkeypatch.setattr(config, "META_AUDIENCIAS_CAPTACION_ACTIVE", True)
    monkeypatch.setattr(AC, "construir_listas_prod",
                        lambda hoy=None: AC.construir_listas(m.c, m.h, bi, HOY, **kw))
    return asyncio.run(AC.sync_audiencias_captacion(HOY))


def test_flag_encendido_solo_viajan_hashes_y_nombres_neutros(monkeypatch, mundo):
    meta = FakeMeta(monkeypatch)
    bi = _mundo_grande(mundo, 130)
    res = _sync_con(monkeypatch, mundo, bi, semilla_top=1.0)
    assert res["status"] == "ok"
    por_nombre = {a["name"]: a for a in res["audiencias"]}
    assert por_nombre[AC.NOMBRE_EXCLUSION]["phones"] == 130
    assert por_nombre[AC.NOMBRE_SEMILLA]["phones"] == 130
    assert por_nombre[AC.NOMBRE_SEMILLA].get("lookalike_id")

    creadas = [d["name"] for u, d in meta.posts if u.endswith("/customaudiences")]
    assert set(creadas) == {AC.NOMBRE_EXCLUSION, AC.NOMBRE_SEMILLA, AC.NOMBRE_SIMILAR}
    for u, d in meta.posts:
        assert "usersreplace" in u or u.endswith("/customaudiences")
        blob = json.dumps(d)
        assert not re.search(r"\b9\d{8}\b", blob) and "56" + _tel(1) not in blob   # ningún teléfono crudo
        assert "rut" not in blob.lower() and "monto" not in blob.lower()
        for term in AC._TERMINOS_PROHIBIDOS:
            assert term not in blob.lower(), term
    # payload: 64 hex, session completa
    envios = [d for u, d in meta.posts if "usersreplace" in u]
    assert len(envios) == 2
    for d in envios:
        payload, ses = json.loads(d["payload"]), json.loads(d["session"])
        assert payload["schema"] == ["PHONE_SHA256"]
        assert all(re.fullmatch(r"[0-9a-f]{64}", row[0]) for row in payload["data"])
        assert ses["last_batch_flag"] is True and ses["batch_seq"] == 1 and ses["estimated_num_total"] == 130
    esperado = hashlib.sha256(("56" + _tel(1)).encode()).hexdigest()
    assert esperado in [r[0] for r in json.loads(envios[0]["payload"])["data"]]


def test_semilla_bajo_el_minimo_no_se_sube(monkeypatch, mundo):
    meta = FakeMeta(monkeypatch)
    bi = _mundo_grande(mundo, 60)
    res = _sync_con(monkeypatch, mundo, bi, semilla_top=1.0)
    sem = {a["name"]: a for a in res["audiencias"]}[AC.NOMBRE_SEMILLA]
    assert sem["status"] == "bajo_minimo" and sem["phones"] == 60
    nombres = [d.get("name") for u, d in meta.posts if u.endswith("/customaudiences")]
    assert AC.NOMBRE_SEMILLA not in nombres and AC.NOMBRE_SIMILAR not in nombres


def test_lote_grande_se_parte_con_sesion_unica(monkeypatch):
    meta = FakeMeta(monkeypatch)
    hs = [hashlib.sha256(str(i).encode()).hexdigest() for i in range(25_000)]
    assert asyncio.run(AC._reemplazar_usuarios("AUD_X", hs))
    ses = [json.loads(d["session"]) for _, d in meta.posts]
    assert [s["batch_seq"] for s in ses] == [1, 2, 3]
    assert [s["last_batch_flag"] for s in ses] == [False, False, True]
    assert len({s["session_id"] for s in ses}) == 1


def test_bi_caido_no_envia_nada(monkeypatch, mundo):
    meta = FakeMeta(monkeypatch)
    monkeypatch.setattr(config, "META_AUDIENCIAS_CAPTACION_ACTIVE", True)
    monkeypatch.setattr(AC, "construir_listas_prod", lambda hoy=None: None)
    res = asyncio.run(AC.sync_audiencias_captacion(HOY))
    assert res == {"status": "bi_no_disponible"}
    assert meta.posts == [] and meta.gets == []


def test_lista_vacia_no_vacia_la_audiencia_existente(monkeypatch, mundo):
    meta = FakeMeta(monkeypatch)
    res = _sync_con(monkeypatch, mundo, _bi(ok=[]))
    assert all(a["status"] in ("empty", "bajo_minimo") for a in res["audiencias"])
    assert meta.posts == []


def test_crear_lookalike_historico_no_cambia(monkeypatch):
    """custom_audiences_sync sigue creando su similar con el nombre de siempre."""
    meta = FakeMeta(monkeypatch)
    asyncio.run(CAS._crear_lookalike("SEED"))
    assert meta.posts[0][1]["name"] == "CMC Lookalike 1% Chile"


def test_job_registrado_con_misfire_grace_time():
    src = (ROOT / "app" / "main.py").read_text()
    i = src.index('id="audiencias_captacion_diario"')
    bloque = src[i:i + 200]
    assert "misfire_grace_time" in bloque and "coalesce=True" in bloque
    jobs = (ROOT / "app" / "jobs.py").read_text()
    assert "async def _job_audiencias_captacion" in jobs
