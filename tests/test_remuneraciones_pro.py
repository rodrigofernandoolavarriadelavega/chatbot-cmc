"""Remuneraciones Pro (`app/remuneraciones_pro_routes.py`) — contrato
docs/remuneraciones_pro_contrato.md.

Cubre: cada tipo de diferencia con un caso mínimo, auth (solo dueño), liquidación
idéntica a `ebitda_routes._ebitda_mes` cuando todo concilia, ajustes, fijo de
Abarca (73), barrido Medilink mockeado (fallo cerrado, sin concurrencia) y texto
de WhatsApp. Sin red: Medilink nunca se llama.

Ejecución:
    PYTHONPATH=app:. ~/chatbot-cmc/venv/bin/python -m pytest tests/test_remuneraciones_pro.py -q
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "app"))
sys.path.insert(0, str(ROOT))
os.environ.pop("SQLCIPHER_KEY", None)

import session  # noqa: E402

_tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_tmp.close()
session.DB_PATH = Path(_tmp.name)

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import config  # noqa: E402
import ebitda_routes  # noqa: E402
import remuneraciones_pro_routes as rp  # noqa: E402

DUENO, RECEP = "tok_dueno_test", "tok_recepcion_test"
config.OLACORE_TOKEN = DUENO
config.ADMIN_TOKEN = RECEP
MES = "2026-09"
DIAS = [f"2026-09-{d:02d}" for d in range(1, 31)]

app = FastAPI()
rp.register_remuneraciones_pro_routes(app)
cli = TestClient(app)
Q = {"token": DUENO}


def _reset_db():
    from session import db
    with db() as c:
        for t in ("bi_pagos_caja", "bi_atenciones", "bi_pago_overrides", "pagos_cmc", "equipo_cmc",
                  "egresos_cmc", "remun_medilink_pagos", "remun_medilink_dias", "remun_verificacion",
                  "remun_ajustes", "remuneracion_estado"):
            c.execute(f"DROP TABLE IF EXISTS {t}")
        c.execute("""CREATE TABLE bi_pagos_caja (pago_id INTEGER PRIMARY KEY, atencion_id INTEGER, fecha TEXT,
            id_profesional INTEGER, id_paciente INTEGER, monto INTEGER, metodo_pago TEXT, n_folio TEXT,
            synced_at TEXT, nombre_paciente TEXT)""")
        c.execute("""CREATE TABLE bi_atenciones (atencion_id INTEGER PRIMARY KEY, fecha TEXT, id_paciente INTEGER,
            id_profesional INTEGER, paciente_nombre TEXT, total INTEGER)""")
        c.execute("CREATE TABLE bi_pago_overrides (pago_id INTEGER PRIMARY KEY, id_profesional INTEGER, atencion_id INTEGER)")
        c.execute("""CREATE TABLE pagos_cmc (id INTEGER PRIMARY KEY AUTOINCREMENT, fecha TEXT, hora TEXT,
            paciente_nombre TEXT DEFAULT '', rut TEXT DEFAULT '', id_profesional INTEGER, profesional TEXT DEFAULT '',
            area TEXT DEFAULT '', prevision TEXT DEFAULT 'particular', copago INTEGER DEFAULT 0,
            bonificacion INTEGER DEFAULT 0, metodo_pago TEXT DEFAULT 'efectivo', id_paciente INTEGER)""")
        c.execute("""CREATE TABLE equipo_cmc (id INTEGER PRIMARY KEY AUTOINCREMENT, id_medilink INTEGER, nombre TEXT,
            especialidad TEXT DEFAULT '', pct_honorario INTEGER DEFAULT 0, telefono TEXT DEFAULT '',
            updated_at TEXT)""")
        c.execute("""CREATE TABLE egresos_cmc (id INTEGER PRIMARY KEY, fecha TEXT, categoria TEXT, descripcion TEXT,
            monto INTEGER, recurrente INTEGER DEFAULT 0, proveedor TEXT)""")
        for pid, nom, pct, tel in ((1, "Dr. Rodrigo Olavarría", 71, "56911111111"),
                                   (73, "Dr. Andrés Abarca", 0, "56922222222"),
                                   (21, "Leonardo Etcheverry", 52, ""),
                                   (55, "Dra. Javiera Burgos", 51, "56933333333")):
            c.execute("INSERT INTO equipo_cmc (id_medilink, nombre, pct_honorario, telefono) VALUES (?,?,?,?)",
                      (pid, nom, pct, tel))
    rp._TAREA.update(task=None, mes=None)


def caja(pago_id, fecha, prof, idpac, monto, nombre):
    from session import db
    with db() as c:
        c.execute("INSERT INTO bi_pagos_caja (pago_id, fecha, id_profesional, id_paciente, monto, nombre_paciente) "
                  "VALUES (?,?,?,?,?,?)", (pago_id, fecha, prof, idpac, monto, nombre))


def recep(fecha, prof, nombre, copago, prevision="particular", area="Medicina General", metodo="efectivo",
          idpac=None):
    from session import db
    with db() as c:
        c.execute("INSERT INTO pagos_cmc (fecha, hora, paciente_nombre, id_profesional, area, prevision, copago, "
                  "metodo_pago, id_paciente) VALUES (?,?,?,?,?,?,?,?,?)",
                  (fecha, "10:00", nombre, prof, area, prevision, copago, metodo, idpac))


def medilink(pagos):
    """pagos: [(pago_id, fecha, prof, idpac, monto, nombre)] → cache, mes completo verificado."""
    dias = {d: [] for d in DIAS}
    for pid, f, prof, idpac, monto, nom in pagos:
        dias[f].append({"id": pid, "fecha_recepcion": f, "id_profesional": prof, "id_paciente": idpac,
                        "monto_pago": monto, "nombre_paciente": nom})
    rp.importar_pagos_medilink(MES, dias)


def lineas(prof):
    r = cli.get("/api/remuneraciones/detalle", params={**Q, "prof": prof, "mes": MES})
    assert r.status_code == 200, r.text
    return r.json()["lineas"]


def mes():
    r = cli.get("/api/remuneraciones/mes", params={**Q, "mes": MES})
    assert r.status_code == 200, r.text
    return r.json()


def prof(m, pid):
    return next(p for p in m["profesionales"] if p["id"] == pid)


class Base(unittest.TestCase):
    def setUp(self):
        _reset_db()


class TestAuth(Base):
    def test_sin_token_403(self):
        self.assertEqual(cli.get("/api/remuneraciones/mes", params={"mes": MES}).status_code, 403)

    def test_token_recepcion_403(self):
        for url in ("/api/remuneraciones/mes", "/api/remuneraciones/verificar-estado"):
            self.assertEqual(cli.get(url, params={"token": RECEP, "mes": MES}).status_code, 403)
        self.assertEqual(cli.post("/api/remuneraciones/ajuste", params={"token": RECEP},
                                  json={"prof": 1, "mes": MES, "monto": 1, "motivo": "x"}).status_code, 403)
        self.assertEqual(cli.get("/alma/remuneraciones", params={"token": RECEP}).status_code, 403)

    def test_cookie_admin_no_abre(self):
        r = cli.get("/api/remuneraciones/mes", params={"mes": MES}, cookies={"cmc_session": "admin:9999999999:x"})
        self.assertEqual(r.status_code, 403)

    def test_bearer_dueno_ok(self):
        r = cli.get("/api/remuneraciones/mes", params={"mes": MES}, headers={"Authorization": f"Bearer {DUENO}"})
        self.assertEqual(r.status_code, 200)

    def test_mes_invalido_400(self):
        self.assertEqual(cli.get("/api/remuneraciones/mes", params={**Q, "mes": "2026-13"}).status_code, 400)

    def test_pagina_dominio_clinico_404(self):
        r = cli.get("/alma/remuneraciones", params=Q, headers={"host": "centromedicocarampangue.cl"})
        self.assertEqual(r.status_code, 404)


class TestTipos(Base):
    def test_ok_tres_fuentes(self):
        caja(1, "2026-09-02", 1, 10, 15130, "Juan Andrés Pérez Soto")
        recep("2026-09-02", 1, "Juan Pérez", 7250, prevision="fonasa")   # 7250 + bonif 7880 = 15130
        medilink([(1, "2026-09-02", 1, 10, 15130, "Juan Andrés Pérez Soto")])
        ls = lineas(1)
        self.assertEqual([x["tipo"] for x in ls], ["ok"])
        self.assertTrue(ls[0]["cuenta_para_pago"])
        self.assertEqual(ls[0]["modulo"]["bonif_est"], 7880)

    def test_fantasma_sistema(self):
        caja(1, "2026-09-02", 1, 10, 20000, "Ana Soto Ruiz")
        recep("2026-09-02", 1, "Ana Soto Ruiz", 20000)
        medilink([])
        x = lineas(1)[0]
        self.assertEqual(x["tipo"], "fantasma_sistema")
        self.assertFalse(x["cuenta_para_pago"])
        self.assertEqual(x["diferencia"], -20000)
        p = prof(mes(), 1)
        self.assertEqual(p["liquidacion"]["base"], 0)
        self.assertEqual(p["liquidacion"]["base_fuente"], "medilink")
        self.assertEqual(p["conciliacion"]["n_dif"], 1)

    def test_fantasma_no_baja_si_mes_sin_verificar(self):
        caja(1, "2026-09-02", 1, 10, 20000, "Ana Soto Ruiz")
        recep("2026-09-02", 1, "Ana Soto Ruiz", 20000)
        p = prof(mes(), 1)
        self.assertEqual(p["liquidacion"]["base"], 20000)
        self.assertEqual(p["liquidacion"]["base_fuente"], "sistema")
        self.assertEqual(p["conciliacion"]["estado"], "sin_verificar")
        self.assertIsNone(p["fuentes"]["medilink"])

    def test_falta_en_sistema(self):
        recep("2026-09-03", 1, "Pedro Lagos Molina", 30000)
        medilink([(7, "2026-09-03", 1, 11, 30000, "Pedro Lagos Molina")])
        x = lineas(1)[0]
        self.assertEqual(x["tipo"], "falta_en_sistema")
        self.assertTrue(x["cuenta_para_pago"])
        self.assertEqual(prof(mes(), 1)["liquidacion"]["base"], 30000)

    def test_otro_profesional(self):
        caja(5, "2026-09-04", 1, 12, 25000, "Rosa Díaz Pino")
        medilink([(5, "2026-09-04", 73, 12, 25000, "Rosa Díaz Pino")])
        sale, entra = lineas(1)[0], lineas(73)[0]
        self.assertEqual(sale["tipo"], "otro_profesional")
        self.assertFalse(sale["cuenta_para_pago"])
        self.assertEqual(sale["otro_profesional_id"], 73)
        self.assertIn("Medilink no informa el profesional", sale["explicacion"])
        self.assertEqual(entra["tipo"], "otro_profesional")
        self.assertTrue(entra["cuenta_para_pago"])
        m = mes()
        self.assertEqual(prof(m, 1)["liquidacion"]["base"], 0)

    def test_monto_distinto(self):
        caja(8, "2026-09-05", 1, 13, 20000, "Luis Mora Vega")
        recep("2026-09-05", 1, "Luis Mora Vega", 20000)
        medilink([(8, "2026-09-05", 1, 13, 25000, "Luis Mora Vega")])
        x = lineas(1)[0]
        self.assertEqual(x["tipo"], "monto_distinto")
        self.assertEqual(x["diferencia"], 5000)
        self.assertEqual(prof(mes(), 1)["liquidacion"]["base"], 25000)

    def test_sin_registro_modulo(self):
        caja(9, "2026-09-06", 1, 14, 15130, "Marta Ríos León")
        medilink([(9, "2026-09-06", 1, 14, 15130, "Marta Ríos León")])
        x = lineas(1)[0]
        self.assertEqual(x["tipo"], "sin_registro_modulo")
        self.assertTrue(x["cuenta_para_pago"])

    def test_solo_en_modulo_no_cuenta_por_defecto(self):
        recep("2026-09-07", 1, "Tomás Rey Paz", 30000, metodo="transferencia")
        medilink([])
        x = lineas(1)[0]
        self.assertEqual(x["tipo"], "solo_en_modulo")
        self.assertFalse(x["cuenta_para_pago"])
        self.assertEqual(x["diferencia"], 30000)

    def test_placeholder_copago_cero_es_silencioso(self):
        recep("2026-09-07", 1, "Carla Vera Soto", 0, prevision="fonasa", metodo="efectivo")
        recep("2026-09-07", 1, "Otro Prellenado Sin", 0, metodo="")
        medilink([])
        self.assertEqual(lineas(1), [])

    def test_dif_modulo_puntual_es_alerta(self):
        caja(10, "2026-09-08", 1, 15, 20000, "Elena Paz Ruiz")
        recep("2026-09-08", 1, "Elena Paz Ruiz", 18000)
        medilink([(10, "2026-09-08", 1, 15, 20000, "Elena Paz Ruiz")])
        x = lineas(1)[0]
        self.assertEqual(x["tipo"], "dif_modulo")
        self.assertEqual(x["diferencia"], -2000)
        p = prof(mes(), 1)
        self.assertEqual(p["conciliacion"]["n_dif"], 0)       # no enciende el badge
        self.assertEqual(p["conciliacion"]["n_alertas"], 1)
        self.assertEqual(p["liquidacion"]["base"], 20000)

    def test_ok_patron_630_fonasa_mg(self):
        pagos = []
        for i in range(5):
            f = f"2026-09-{10 + i:02d}"
            caja(100 + i, f, 1, 200 + i, 15130, f"Paciente Numero{i} Apellido{i}")
            recep(f, 1, f"Paciente Numero{i} Apellido{i}", 7880, prevision="fonasa")  # bonif tecleada como copago
            pagos.append((100 + i, f, 1, 200 + i, 15130, f"Paciente Numero{i} Apellido{i}"))
        medilink(pagos)
        ls = lineas(1)
        self.assertEqual({x["tipo"] for x in ls}, {"ok_patron"})
        m = mes()
        p = prof(m, 1)
        self.assertEqual(p["conciliacion"]["n_dif"], 0)
        self.assertEqual(p["conciliacion"]["n_ok_patron"], 5)
        self.assertEqual(len(m["patrones"]), 1)
        pt = m["patrones"][0]
        self.assertEqual((pt["n"], pt["diferencia_unitaria"], pt["monto"]), (5, 630, 3150))
        self.assertIn("bonificación como copago", pt["causa"])

    def test_prof_distinto_modulo(self):
        caja(11, "2026-09-09", 55, 16, 50000, "Ámbar Sánchez Pardo")
        recep("2026-09-09", 1, "Ámbar Sánchez Pardo", 50000)
        medilink([(11, "2026-09-09", 55, 16, 50000, "Ámbar Sánchez Pardo")])
        x = lineas(55)[0]
        self.assertEqual(x["tipo"], "prof_distinto_modulo")
        self.assertTrue(x["cuenta_para_pago"])
        self.assertEqual(lineas(1), [])

    def test_empareja_por_id_paciente_aunque_nombre_tenga_typo(self):
        caja(12, "2026-09-11", 1, 17, 20000, "Daniela Aravena Acevedo")
        recep("2026-09-11", 1, "Daniela Aravena Cevedo", 20000, idpac=17)
        medilink([(12, "2026-09-11", 1, 17, 20000, "Daniela Aravena Acevedo")])
        self.assertEqual([x["tipo"] for x in lineas(1)], ["ok"])

    def test_apellido_comun_no_empareja(self):
        caja(13, "2026-09-12", 1, 18, 20000, "Ana Soto Ruiz")
        recep("2026-09-12", 1, "Pedro Soto Lagos", 20000)
        medilink([(13, "2026-09-12", 1, 18, 20000, "Ana Soto Ruiz")])
        self.assertEqual(sorted(x["tipo"] for x in lineas(1)), ["sin_registro_modulo", "solo_en_modulo"])


class TestDecision(unittest.TestCase):
    def test_funcion_aislada_y_corta(self):
        import inspect
        src = inspect.getsource(rp._linea_cuenta_para_pago)
        cuerpo = [l for l in src.split('"""')[-1].splitlines() if l.strip()]
        self.assertLessEqual(len(cuerpo), 10)
        self.assertTrue(rp._linea_cuenta_para_pago({"verificado": True, "medilink": {"monto": 1}}))
        self.assertFalse(rp._linea_cuenta_para_pago({"verificado": True, "medilink": None, "sistema": {"monto": 1}}))
        self.assertTrue(rp._linea_cuenta_para_pago({"verificado": False, "sistema": {"monto": 1}}))
        self.assertFalse(rp._linea_cuenta_para_pago({"verificado": False, "sistema": None, "modulo": {"esperado": 1}}))


class TestLiquidacion(Base):
    def _poblar(self):
        pagos = []
        n = 0
        for pid, monto in ((1, 15130), (1, 20000), (73, 15130), (21, 11390), (55, 50000)):
            n += 1
            f = f"2026-09-{n + 1:02d}"
            caja(n, f, pid, 300 + n, monto, f"Persona Uno{n} Dos{n}")
            recep(f, pid, f"Persona Uno{n} Dos{n}", monto)
            pagos.append((n, f, pid, 300 + n, monto, f"Persona Uno{n} Dos{n}"))
        return pagos

    def _ebitda(self):
        from session import db
        with db() as c, ebitda_routes.solo_datos_locales():
            return {p["id"]: p for p in ebitda_routes._ebitda_mes(c, MES)["profesionales"]}

    def _comparar(self, m):
        e = self._ebitda()
        self.assertEqual({p["id"] for p in m["profesionales"]}, set(e))
        for p in m["profesionales"]:
            q = e[p["id"]]
            self.assertEqual(p["liquidacion"]["base"], q["ingreso"])
            self.assertEqual(p["liquidacion"]["bruto"], q["bruto"])
            self.assertEqual(p["liquidacion"]["liquido"], q["liquido"])
            self.assertEqual(p["liquidacion"]["retencion"], q["retencion"])
        self.assertEqual(m["totales"]["bruto"], sum(q["bruto"] for q in e.values()))
        self.assertEqual(m["totales"]["liquido"], sum(q["liquido"] for q in e.values()))

    def test_igual_a_ebitda_sin_verificar(self):
        self._poblar()
        self._comparar(mes())

    def test_igual_a_ebitda_verificado_y_conciliado(self):
        medilink(self._poblar())
        m = mes()
        self.assertEqual(m["verificado_medilink"]["estado"], "ok")
        self.assertEqual(m["totales"]["n_dif"], 0)
        self.assertEqual(m["totales"]["monto_dif_pago"], 0)
        self.assertEqual(m["totales"]["sistema"], m["totales"]["medilink"])
        self._comparar(m)

    def test_fijo_abarca_intacto(self):
        self._poblar()
        p = prof(mes(), 73)
        self.assertTrue(p["fijo"])
        self.assertIsNone(p["pct"])
        self.assertEqual(p["liquidacion"]["bruto"], ebitda_routes.honorario_fijo(73, MES))
        self.assertTrue(p["sin_retencion"])
        self.assertEqual(p["liquidacion"]["liquido"], p["liquidacion"]["bruto"])

    def test_ajustes_suman_y_se_borran(self):
        self._poblar()
        liq0 = prof(mes(), 1)["liquidacion"]["liquido"]
        r1 = cli.post("/api/remuneraciones/ajuste", params=Q,
                      json={"prof": 1, "mes": MES, "monto": 20000, "motivo": "Reemplazo sábado"})
        r2 = cli.post("/api/remuneraciones/ajuste", params=Q,
                      json={"prof": 1, "mes": MES, "monto": -5000, "motivo": "Radiografía"})
        self.assertEqual((r1.status_code, r2.status_code), (200, 200))
        p = prof(mes(), 1)
        self.assertEqual(p["liquidacion"]["total_ajustes"], 15000)
        self.assertEqual(p["liquidacion"]["a_transferir"], liq0 + 15000)
        self.assertEqual(len(p["liquidacion"]["ajustes"]), 2)
        self.assertEqual(cli.delete(f"/api/remuneraciones/ajuste/{r1.json()['id']}", params=Q).status_code, 200)
        self.assertEqual(prof(mes(), 1)["liquidacion"]["a_transferir"], liq0 - 5000)
        self.assertEqual(cli.post("/api/remuneraciones/ajuste", params=Q,
                                  json={"prof": 1, "mes": MES, "monto": 100, "motivo": ""}).status_code, 400)

    def test_pagado_con_monto(self):
        self._poblar()
        r = cli.post("/api/remuneraciones/pagado", params=Q,
                     json={"prof": 1, "mes": MES, "pagado": True, "monto_pagado": 1000})
        self.assertEqual(r.status_code, 200)
        m = mes()
        p = prof(m, 1)
        self.assertTrue(p["pagado"])
        self.assertEqual(p["monto_pagado"], 1000)
        self.assertEqual(m["totales"]["pagado"], 1000)
        self.assertEqual(m["totales"]["por_pagar"], m["totales"]["a_transferir"] - p["liquidacion"]["a_transferir"])

    def test_config_pct(self):
        self._poblar()
        self.assertEqual(cli.post("/api/remuneraciones/config", params=Q, json={"id": 1, "pct": 101}).status_code, 400)
        self.assertEqual(cli.post("/api/remuneraciones/config", params=Q, json={"id": 1, "pct": 50}).status_code, 200)
        self.assertEqual(prof(mes(), 1)["pct"], 50)

    def test_whatsapp(self):
        self._poblar()
        cli.post("/api/remuneraciones/ajuste", params=Q, json={"prof": 1, "mes": MES, "monto": 20000,
                                                              "motivo": "Reemplazo sábado"})
        r = cli.get("/api/remuneraciones/whatsapp", params={**Q, "prof": 1, "mes": MES})
        self.assertEqual(r.status_code, 200)
        d = r.json()
        t = d["texto"]
        self.assertEqual(d["telefono"], "56911111111")
        self.assertTrue(t.startswith("Estimado Dr. Rodrigo Olavarría:"))
        self.assertIn("septiembre 2026", t)
        self.assertIn("71%", t)
        self.assertIn("Retención 15,25%", t)
        self.assertIn("Reemplazo sábado", t)
        self.assertIn("Persona Uno1 Dos1", t)
        self.assertIn("Total a transferir", t)
        self.assertTrue(t.rstrip().endswith("Centro Médico Carampangue"))
        self.assertNotIn("Medicina General", t)   # nada de especialidad/diagnóstico
        for voseo in ("podés", "tenés", "querés", "avisá"):
            self.assertNotIn(voseo, t)
        self.assertNotIn("Persona Uno1", d["texto_resumen"])
        t73 = cli.get("/api/remuneraciones/whatsapp", params={**Q, "prof": 73, "mes": MES}).json()["texto"]
        self.assertIn("Honorario fijo", t73)
        self.assertIn("Factura", t73)


class TestPedidosFront(Base):
    """Cambios pedidos por el front (13-oct): pct_actualizado_at, patron_id,
    WhatsApp en dos partes, registro de envío, decisión por línea, id de ajuste."""

    def test_pct_actualizado_at(self):
        from session import db
        with db() as c:
            c.execute("UPDATE equipo_cmc SET updated_at='2026-06-02 18:40:00' WHERE id_medilink=1")
        caja(1, "2026-09-02", 1, 10, 20000, "Ana Soto Ruiz")
        self.assertEqual(prof(mes(), 1)["pct_actualizado_at"], "2026-06-02 18:40:00")
        cli.post("/api/remuneraciones/config", params=Q, json={"id": 1, "pct": 60})
        self.assertNotEqual(prof(mes(), 1)["pct_actualizado_at"], "2026-06-02 18:40:00")

    def test_patron_id_estable_y_monto_con_signo(self):
        pagos = []
        for i in range(5):
            f = f"2026-09-{10 + i:02d}"
            nom = f"Paciente Numero{i} Apellido{i}"
            caja(100 + i, f, 1, 200 + i, 15130, nom)
            recep(f, 1, nom, 5000, prevision="fonasa")      # 5000 + 7880 = 12880 → −2250 c/u
            pagos.append((100 + i, f, 1, 200 + i, 15130, nom))
        medilink(pagos)
        ls = lineas(1)
        ids = {x["patron_id"] for x in ls}
        self.assertEqual(ids, {"p12880-15130"})
        self.assertEqual({x["patron"] for x in ls}, ids)
        pt = mes()["patrones"][0]
        self.assertEqual((pt["patron_id"], pt["id"], pt["monto"]), ("p12880-15130", "p12880-15130", -11250))
        self.assertEqual(lineas(1)[0]["patron_id"], "p12880-15130")   # estable entre llamadas

    def test_whatsapp_en_dos_partes_y_envio(self):
        for i in range(60):
            f = f"2026-09-{1 + i % 28:02d}"
            caja(500 + i, f, 1, 600 + i, 15130, f"Persona Larga{i} Apellido{i} Segundo{i}")
        d = cli.get("/api/remuneraciones/whatsapp", params={**Q, "prof": 1, "mes": MES}).json()
        self.assertTrue(d["dos_mensajes"])
        self.assertGreater(d["largo"], 1800)
        self.assertNotIn("Persona Larga0", d["texto_resumen"])
        self.assertIn("mensaje siguiente", d["texto_resumen"])
        self.assertIn("Persona Larga0 Apellido0 Segundo0", d["texto_detalle"])
        self.assertTrue(d["texto_detalle"].startswith("*Detalle de atenciones de septiembre 2026 (60)*"))
        self.assertIsNone(prof(mes(), 1)["whatsapp_enviado_at"])
        r = cli.post("/api/remuneraciones/whatsapp-enviado", params=Q, json={"prof": 1, "mes": MES})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["whatsapp_enviado_at"])
        self.assertEqual(prof(mes(), 1)["whatsapp_enviado_at"], r.json()["whatsapp_enviado_at"])
        self.assertEqual(cli.post("/api/remuneraciones/whatsapp-enviado", params={"token": RECEP},
                                  json={"prof": 1, "mes": MES}).status_code, 403)

    def test_decision_por_linea_solo_en_modulo(self):
        caja(1, "2026-09-02", 1, 10, 20000, "Ana Soto Ruiz")
        recep("2026-09-07", 1, "Tomás Rey Paz", 30000, metodo="transferencia", idpac=77)
        medilink([(1, "2026-09-02", 1, 10, 20000, "Ana Soto Ruiz")])
        x = next(l for l in lineas(1) if l["tipo"] == "solo_en_modulo")
        self.assertIsNone(x["decision"])
        self.assertFalse(x["cuenta_para_pago"])
        self.assertEqual(prof(mes(), 1)["liquidacion"]["base"], 20000)
        body = {"prof": 1, "mes": MES, "clave_linea": x["clave_linea"], "incluir": True}
        r = cli.post("/api/remuneraciones/linea-decision", params=Q, json=body)
        self.assertEqual(r.status_code, 200, r.text)
        x2 = next(l for l in lineas(1) if l["tipo"] == "solo_en_modulo")
        self.assertTrue(x2["decision"])
        self.assertTrue(x2["cuenta_para_pago"])
        self.assertEqual(x2["monto_pago"], 30000)
        p = prof(mes(), 1)
        self.assertEqual(p["liquidacion"]["base"], 50000)
        self.assertEqual(p["conciliacion"]["monto_dif_pago"], 30000)
        cli.post("/api/remuneraciones/linea-decision", params=Q, json={**body, "incluir": None})
        self.assertEqual(prof(mes(), 1)["liquidacion"]["base"], 20000)
        ok = next(l for l in lineas(1) if l["tipo"] != "solo_en_modulo")
        r = cli.post("/api/remuneraciones/linea-decision", params=Q,
                     json={**body, "clave_linea": ok["clave_linea"]})
        self.assertEqual(r.status_code, 400)                   # solo sobre solo_en_modulo
        r = cli.post("/api/remuneraciones/linea-decision", params=Q, json={**body, "clave_linea": "x|1|y"})
        self.assertEqual(r.status_code, 404)
        self.assertEqual(cli.post("/api/remuneraciones/linea-decision", params={"token": RECEP},
                                  json=body).status_code, 403)

    def test_decision_se_borra_con_derecho_al_olvido(self):
        recep("2026-09-07", 1, "Tomás Rey Paz", 30000, idpac=77)
        x = lineas(1)[0]
        cli.post("/api/remuneraciones/linea-decision", params=Q,
                 json={"prof": 1, "mes": MES, "clave_linea": x["clave_linea"], "incluir": True})
        from session import db
        with db() as c:
            n0 = c.execute("SELECT COUNT(*) FROM remun_decisiones").fetchone()[0]
        self.assertEqual(n0, 1)
        res = session.delete_patient_data(None, None, id_paciente_medilink=77, deleted_by="test")
        self.assertEqual(res.get("remun_decisiones"), 1)
        with db() as c:
            self.assertEqual(c.execute("SELECT COUNT(*) FROM remun_decisiones").fetchone()[0], 0)

    def test_ajuste_devuelve_id(self):
        r = cli.post("/api/remuneraciones/ajuste", params=Q,
                     json={"prof": 1, "mes": MES, "monto": 1000, "motivo": "Prueba"})
        self.assertIsInstance(r.json()["id"], int)

    def test_regla_respeta_override_antes_del_default(self):
        self.assertTrue(rp._linea_cuenta_para_pago({"decision": True, "verificado": True, "medilink": None}))
        self.assertFalse(rp._linea_cuenta_para_pago({"decision": False, "verificado": True,
                                                     "medilink": {"monto": 1}}))


class TestBarrido(Base):
    def _correr(self, fake):
        orig = rp._fetch_dia
        rp._fetch_dia = fake
        rp.INTERVALO_S, iv = 0, rp.INTERVALO_S

        async def go():
            ok = await rp.iniciar_barrido(MES)
            segundo = await rp.iniciar_barrido(MES)    # nunca concurrente consigo mismo
            await rp._TAREA["task"]
            return ok, segundo
        try:
            return asyncio.run(go())
        finally:
            rp._fetch_dia = orig
            rp.INTERVALO_S = iv
            rp._TAREA.update(task=None, mes=None)

    def test_barrido_completo_verifica_mes(self):
        caja(1, "2026-09-02", 1, 10, 20000, "Ana Soto Ruiz")
        llamados = []

        async def fake(cli_, fecha):
            llamados.append(fecha)
            if fecha == "2026-09-02":
                return [{"id": 1, "fecha_recepcion": fecha, "id_profesional": 1, "id_paciente": 10,
                         "monto_pago": 20000, "nombre_paciente": "Ana Soto Ruiz"}], True, ""
            return [], True, ""
        ok, segundo = self._correr(fake)
        self.assertTrue(ok)
        self.assertFalse(segundo)
        self.assertEqual(llamados, DIAS)                       # día por día, en orden
        r = cli.get("/api/remuneraciones/verificar-estado", params={**Q, "mes": MES}).json()
        self.assertEqual((r["estado"], r["dias_ok"], r["dias_total"]), ("ok", 30, 30))
        self.assertEqual(prof(mes(), 1)["liquidacion"]["base_fuente"], "medilink")

    def test_dia_fallido_no_se_verifica_fallo_cerrado(self):
        caja(1, "2026-09-02", 1, 10, 20000, "Ana Soto Ruiz")

        async def fake(cli_, fecha):
            if fecha == "2026-09-02":
                return [], False, "Medilink respondió 429"
            return [], True, ""
        self._correr(fake)
        r = cli.get("/api/remuneraciones/verificar-estado", params={**Q, "mes": MES}).json()
        self.assertEqual(r["estado"], "parcial")
        self.assertEqual(r["dias_error"], 1)
        x = lineas(1)[0]
        self.assertNotEqual(x["tipo"], "fantasma_sistema")    # el día caído no inventa anulados
        p = prof(mes(), 1)
        self.assertEqual(p["liquidacion"]["base"], 20000)
        self.assertEqual(p["liquidacion"]["base_fuente"], "sistema")

    def test_aborta_tras_errores_seguidos(self):
        llamados = []

        async def fake(cli_, fecha):
            llamados.append(fecha)
            return [], False, "Medilink respondió 403"
        self._correr(fake)
        self.assertEqual(len(llamados), rp.MAX_ERRORES_SEGUIDOS)
        from session import db
        with db() as c:
            v = c.execute("SELECT estado, error FROM remun_verificacion WHERE mes=?", (MES,)).fetchone()
        self.assertEqual(v["estado"], "error")

    def test_fetch_dia_respeta_status_y_no_escribe(self):
        """El cliente espaciado sólo hace GET; un 403 deja el día incompleto."""
        class R:
            def __init__(self, sc, data):
                self.status_code, self._d = sc, data

            def json(self):
                return {"data": self._d}

        class Fake:
            def __init__(self, resp):
                self.resp, self.metodos = resp, []

            async def get(self, *a, **k):
                self.metodos.append("GET")
                return self.resp
        f = Fake(R(403, []))
        pagos, ok, err = asyncio.run(rp._fetch_dia(rp._ClienteEspaciado(f, 0), "2026-09-02"))
        self.assertFalse(ok)
        self.assertIn("403", err)
        f = Fake(R(200, [{"id": 1, "monto_pago": 10}]))
        pagos, ok, _ = asyncio.run(rp._fetch_dia(rp._ClienteEspaciado(f, 0), "2026-09-02"))
        self.assertTrue(ok)
        self.assertEqual(len(pagos), 1)
        self.assertEqual(set(f.metodos), {"GET"})

    def test_verificar_mes_futuro_400(self):
        r = cli.post("/api/remuneraciones/verificar-medilink", params=Q, json={"mes": "2099-01"})
        self.assertEqual(r.status_code, 400)


if __name__ == "__main__":
    unittest.main(verbosity=2)
