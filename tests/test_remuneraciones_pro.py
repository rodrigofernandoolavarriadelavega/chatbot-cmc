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
                  "remun_ajustes", "remuneracion_estado", "remun_envios", "remun_decisiones",
                  "remun_reporte_cab", "remun_reporte_medilink", "remun_reparto", "remun_atencion_check",
                  "ausentismo_citas"):
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


# ═════════════════ Fase 2: atribución (caso real Armijo, sep-2026) ═════════════
import json as _json  # noqa: E402
import bi_sync  # noqa: E402

ARM = _json.loads((ROOT / "tests" / "fixtures" / "remun_armijo_2026_09.json").read_text())
_MESES_EN = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
_TABLA = {15130: "med", 11390: "kine", 3680: "kine", 15070: "kine", 20980: "psico"}


def _fecha_ui(iso):
    return f"{iso[8:10]} {_MESES_EN[int(iso[5:7]) - 1]} {iso[:4]}"


def texto_reporte(filas=None, total=None, sep="    ", nombre="Luis Ariel Armijo Toloza"):
    """Mismo formato que copia la UI de Medilink (nombres de pacientes FALSOS)."""
    filas = ARM["reporte_filas"] if filas is None else filas
    total = sum(f["monto"] for f in filas) if total is None else total
    L = ["Pagos período de ", nombre, "Generar 30 Sep 2026al01 Sep 2026del",
         "Mostrar/Esconder resumen de tipos de pago", "Total$" + f"{total:,}".replace(",", "."),
         sep.join(["#", "# Trat.", "Medio de pago", "Nombre paciente", "Recepción", "Monto"])]
    for i, f in enumerate(filas):
        L.append(sep.join([str(f["pago_id"]), str(f["id_atencion"]), "Efectivo", f"Paciente Ficticio {i}",
                           _fecha_ui(f["fecha"]), "$" + f"{f['monto']:,}".replace(",", ".")]))
    return "\n".join(L)


class TestParserReporte(unittest.TestCase):
    def test_parser_formato_real_espacios_y_tabs(self):
        for sep in ("    ", "\t", "  \t "):
            r = rp.parsear_reporte_medilink(texto_reporte(sep=sep))
            self.assertEqual((r["n"], r["total"]), (70, 935250))
            f = next(x for x in r["filas"] if x["pago_id"] == 40457)
            self.assertEqual((f["id_atencion"], f["fecha"], f["monto"], f["medio"]),
                             (58314, "2026-09-28", 11390, "Efectivo"))
            self.assertEqual(r["profesional_texto"], "Luis Ariel Armijo Toloza")
            self.assertEqual(r["periodo"], ("2026-09-01", "2026-09-30"))

    def test_parser_inicio_literal_del_dueno(self):
        t = ("Pagos período de \nLuis Ariel Armijo Toloza\nGenerar 30 Sep 2026al01 Sep 2026del\n"
             "Mostrar/Esconder resumen de tipos de pago\nTotal$37.850\n"
             "#    # Trat.    Medio de pago    Nombre paciente    Recepción    Monto\n"
             "40587    58556    Efectivo    Ana Uno Prueba    30 Sep 2026    $11.390\n"
             "40457    58314    Efectivo    Ana Uno Prueba    28 Sep 2026    $11.390\n"
             "40188    57984    Efectivo    Berta Dos Prueba    22 Sep 2026    $3.680\n"
             "39213    56371    Efectivo    Berta Dos Prueba    01 Sep 2026    $11.390\n")
        r = rp.parsear_reporte_medilink(t)
        self.assertEqual([x["pago_id"] for x in r["filas"]], [40587, 40457, 40188, 39213])
        self.assertEqual(r["filas"][3]["fecha"], "2026-09-01")

    def test_meses_en_espanol_y_total_con_espacio(self):
        t = ("Total $26.520\n1    2    Tarjeta de débito    X Y    3 ago 2026    $15.130\n"
             "3    4    Transferencia    Z W    4 dic. 2026    $ 11.390")
        r = rp.parsear_reporte_medilink(t)
        self.assertEqual([x["fecha"] for x in r["filas"]], ["2026-08-03", "2026-12-04"])
        self.assertEqual(r["filas"][0]["medio"], "Tarjeta de débito")

    def test_suma_distinta_del_total_es_error(self):
        with self.assertRaisesRegex(ValueError, "no coincide con el Total"):
            rp.parsear_reporte_medilink(texto_reporte(total=900000))
        with self.assertRaisesRegex(ValueError, "Total"):
            rp.parsear_reporte_medilink("40587    58556    Efectivo    X    30 Sep 2026    $11.390")


class _ArmijoBase(Base):
    def setUp(self):
        super().setUp()
        self._tabla = bi_sync._tabla_monto_area
        bi_sync._tabla_monto_area = lambda c: dict(_TABLA)
        from session import db
        with db() as c:
            for r in ARM["espejo"]:
                c.execute("INSERT INTO bi_pagos_caja (pago_id, atencion_id, fecha, id_profesional, id_paciente, "
                          "monto, nombre_paciente) VALUES (?,?,?,?,?,?,?)",
                          (r["pago_id"], r["atencion_id"], r["fecha"], r["id_profesional"], r["id_paciente"],
                           r["monto"], f"Paciente Ficticio {r['id_paciente']}"))
            for a in ARM["atenciones"]:
                c.execute("INSERT INTO bi_atenciones (atencion_id, fecha, id_paciente, id_profesional, total) "
                          "VALUES (?,?,?,?,?)", (a["atencion_id"], a["fecha"], a["id_paciente"],
                                                 a["id_profesional"], a["total"]))
            c.execute("CREATE TABLE ausentismo_citas (fecha TEXT, id_paciente INTEGER, id_profesional INTEGER, "
                      "estado_cita TEXT)")
            for x in ARM["citas"]:
                c.execute("INSERT INTO ausentismo_citas VALUES (?,?,?,?)",
                          (x["fecha"], x["id_paciente"], x["id_profesional"], x["estado_cita"]))
            c.execute("INSERT INTO equipo_cmc (id_medilink, nombre, pct_honorario) VALUES (77,'Luis Armijo',41)")
            c.execute("INSERT INTO equipo_cmc (id_medilink, nombre, pct_honorario) VALUES (13,'Dr. Alonso Márquez',75)")

    def tearDown(self):
        bi_sync._tabla_monto_area = self._tabla

    def _base(self, pid):
        return prof(mes(), pid)["liquidacion"]["base"]

    def _pegar(self, **kw):
        return cli.post("/api/remuneraciones/reporte-medilink", params=Q,
                        json={"prof": 77, "mes": MES, "texto": texto_reporte(), **kw})



class TestAtribucionArmijo(_ArmijoBase):
    def test_espejo_da_953990_y_detector_propone_partido(self):
        self.assertEqual(self._base(77), 953990)
        m = mes()
        prop = {p["pago_id"]: p for p in m["repartos_propuestos"]}
        self.assertIn(40457, prop)
        self.assertEqual(sorted((x["id_profesional"], x["monto"]) for x in prop[40457]["partes"]),
                         [(13, 15130), (77, 11390)])
        self.assertEqual(prop[40457]["origen"], "detector")
        ml = next(x for x in lineas(13) if 40457 in x["sistema"]["pago_ids"])
        self.assertEqual(ml["tipo"], "partido")
        self.assertEqual(ml["efecto_pago"], -11390)
        al = next(x for x in lineas(77) if x["tipo"] == "partido")
        self.assertEqual(al["efecto_pago"], 11390)
        self.assertFalse(al["cuenta_para_pago"])              # hasta que el dueño confirme
        self.assertGreater(prof(m, 77)["conciliacion"]["n_dif"], 0)   # badge rojo
        self.assertNotIn(39968, prop)                         # sin chequeo en vivo no se inventa

    def test_sin_aplicar_con_atencion_total_cero_en_vivo(self):
        from session import db
        with db() as c:
            rp._ensure_tablas(c)
            c.execute("INSERT INTO remun_atencion_check (atencion_id, id_profesional, total, abonado, deuda, "
                      "fetched_at) VALUES (58037, 77, 0, 0, 0, datetime('now'))")
        x = next(l for l in lineas(77) if 39968 in (l["sistema"] or {}).get("pago_ids", []))
        self.assertEqual(x["tipo"], "sin_aplicar")
        self.assertIn("re-aplicarlo o es saldo a favor", x["explicacion"])
        self.assertEqual(x["efecto_pago"], -30130)
        # probable, no seguro: no se propone reparto sin el reporte
        self.assertNotIn(39968, {p["pago_id"] for p in mes()["repartos_propuestos"]})

    def test_dos_pagos_misma_atencion_cero_el_reporte_desempata(self):
        """39799 y 39968 apuntan a la atención #58037 ($0 en vivo): el detector
        marca ambos como probables; con el reporte solo queda 39968."""
        from session import db
        with db() as c:
            rp._ensure_tablas(c)
            c.execute("INSERT INTO remun_atencion_check (atencion_id, id_profesional, total, abonado, deuda, "
                      "fetched_at) VALUES (58037, 77, 0, 0, 0, datetime('now'))")
        pids = lambda: sorted(l["pagos_linea"][0]["pago_id"] for l in lineas(77) if l["tipo"] == "sin_aplicar")  # noqa: E731
        self.assertEqual(pids(), [39799, 39968])
        self._pegar()
        self.assertEqual(pids(), [39968])
        x = next(l for l in lineas(77) if l["tipo"] == "sin_aplicar")
        self.assertIn("re-aplicarlo o es saldo a favor", x["explicacion"])

    def test_previsualizar_no_guarda(self):
        r = self._pegar(guardar=False)
        self.assertEqual(r.status_code, 200, r.text)
        d = r.json()
        self.assertEqual((d["guardado"], d["n"], d["total"], d["base_actual"]), (False, 70, 935250, 953990))
        self.assertEqual(d["diferencia_vs_base"], -18740)
        self.assertEqual(self._base(77), 953990)

    def test_reporte_pegado_manda_la_base_y_marca_tipos(self):
        r = self._pegar()
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual((r.json()["base_antes"], r.json()["base_ahora"]), (953990, 935250))
        m = mes()
        a = prof(m, 77)
        self.assertEqual(a["liquidacion"]["base"], 935250)
        self.assertEqual(a["liquidacion"]["base_fuente"], "reporte_medilink")
        self.assertEqual(a["verificacion"]["atribucion"], "verificada_reporte")
        self.assertTrue(a["atribucion"]["cuadra"])
        ls = lineas(77)
        tipos = Counter(x["tipo"] for x in ls)
        self.assertEqual(tipos["sin_aplicar"], 1)
        self.assertEqual(tipos["partido"], 1)
        # sin módulo Pagos en este fixture: el tipo primario es la alerta de recepción, pero
        # toda línea que el reporte trae queda marcada reporte_ok o partido
        self.assertEqual(sum(1 for x in ls if "reporte_ok" in x["tipos"]) + tipos["partido"],
                         sum(1 for x in ls if x["reporte"]))
        sa = next(x for x in ls if x["tipo"] == "sin_aplicar")
        self.assertEqual(sa["pagos_linea"][0]["pago_id"], 39968)
        self.assertFalse(sa["cuenta_para_pago"])
        pa = next(x for x in ls if x["tipo"] == "partido")
        self.assertEqual((pa["reporte"]["monto"], pa["monto_pago"]), (11390, 11390))
        mq = next(x for x in lineas(13) if 40457 in x["sistema"]["pago_ids"])
        self.assertEqual(mq["tipo"], "posible_exceso")
        self.assertEqual(mq["efecto_pago"], -11390)
        self.assertIn("Pegue su reporte", mq["explicacion"])
        prop = {p["pago_id"]: p for p in m["repartos_propuestos"]}
        self.assertEqual(prop[40457]["origen"], "reporte")
        self.assertEqual(sorted((x["id_profesional"], x["monto"]) for x in prop[39968]["partes"]), [(0, 30130)])
        # el reporte no guarda nombres de pacientes
        from session import db
        with db() as c:
            cols = {r[1] for r in c.execute("PRAGMA table_info(remun_reporte_medilink)").fetchall()}
        self.assertFalse(any("nombre" in x or "paciente" in x for x in cols))

    def test_reporte_de_otro_mes_o_suma_mala_400(self):
        r = cli.post("/api/remuneraciones/reporte-medilink", params=Q,
                     json={"prof": 77, "mes": MES, "texto": texto_reporte(total=1)})
        self.assertEqual(r.status_code, 400)
        self.assertIn("no coincide", r.json()["detail"])
        r = cli.post("/api/remuneraciones/reporte-medilink", params=Q,
                     json={"prof": 77, "mes": "2026-08", "texto": texto_reporte()})
        self.assertEqual(r.status_code, 400)

    def test_reparto_confirmado_mueve_a_ambos(self):
        b77, b13 = self._base(77), self._base(13)
        r = cli.post("/api/remuneraciones/reparto", params=Q,
                     json={"pago_id": 40457, "partes": [{"id_profesional": 77, "monto": 11390},
                                                        {"id_profesional": 13, "monto": 15130}]})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self._base(77), b77 + 11390)
        self.assertEqual(self._base(13), b13 - 11390)
        m = mes()
        self.assertNotIn(40457, {p["pago_id"] for p in m["repartos_propuestos"]})
        self.assertEqual(m["totales"]["sistema"], sum(r["monto"] for r in ARM["espejo"]))   # caja intacta
        from session import db
        with db() as c:   # el espejo NO se toca
            self.assertEqual(c.execute("SELECT id_profesional, monto FROM bi_pagos_caja WHERE pago_id=40457"
                                       ).fetchone()[:], (13, 26520))
        # con 39968 sin asignar, el espejo de Armijo = reporte de Medilink
        cli.post("/api/remuneraciones/reparto", params=Q,
                 json={"pago_id": 39968, "partes": [{"id_profesional": 0, "monto": 30130}]})
        self.assertEqual(self._base(77), 935250)
        self._pegar()
        ls = lineas(77)
        self.assertTrue(all("reporte_ok" in x["tipos"] for x in ls if x["reporte"]))
        self.assertFalse({"partido", "sin_aplicar"} & {x["tipo"] for x in ls})
        self.assertEqual(self._base(77), 935250)
        self.assertNotIn("posible_exceso", {x["tipo"] for x in lineas(13)})
        self.assertEqual(cli.delete("/api/remuneraciones/reparto/40457", params=Q).status_code, 200)
        self.assertEqual(self._base(13), b13)

    def test_reparto_invalido(self):
        r = cli.post("/api/remuneraciones/reparto", params=Q,
                     json={"pago_id": 40457, "partes": [{"id_profesional": 77, "monto": 30000}]})
        self.assertEqual(r.status_code, 400)
        r = cli.post("/api/remuneraciones/reparto", params=Q,
                     json={"pago_id": 1, "partes": [{"id_profesional": 77, "monto": 1}]})
        self.assertEqual(r.status_code, 404)

    def test_auth_solo_dueno_en_endpoints_nuevos(self):
        for metodo, url, kw in (("post", "/api/remuneraciones/reporte-medilink",
                                 {"json": {"prof": 77, "mes": MES, "texto": "x"}}),
                                ("delete", "/api/remuneraciones/reporte-medilink", {"params": {"prof": 77}}),
                                ("get", "/api/remuneraciones/repartos", {}),
                                ("post", "/api/remuneraciones/reparto", {"json": {"pago_id": 40457, "partes": []}}),
                                ("delete", "/api/remuneraciones/reparto/40457", {})):
            kw = dict(kw)
            kw["params"] = {**kw.get("params", {}), "token": RECEP, "mes": MES}
            self.assertEqual(getattr(cli, metodo)(url, **kw).status_code, 403, url)

    def test_barrido_verifica_atenciones_candidatas(self):
        llamadas = []

        async def fake_dia(cli_, fecha):
            return [], False, "no"            # no importa: se pide solo_atenciones

        async def fake_aten(cli_, aid):
            llamadas.append(aid)
            return {"atencion_id": aid, "id_profesional": 77, "total": 0, "abonado": 0, "deuda": 0}
        o1, o2 = rp._fetch_dia, rp._fetch_atencion
        rp._fetch_dia, rp._fetch_atencion = fake_dia, fake_aten

        async def go():
            await rp.iniciar_barrido(MES, solo_atenciones=True)
            await rp._TAREA["task"]
        try:
            asyncio.run(go())
        finally:
            rp._fetch_dia, rp._fetch_atencion = o1, o2
            rp._TAREA.update(task=None, mes=None)
        self.assertEqual(sorted(llamadas), [58037, 58238])     # solo las de total $0 locales
        x = next(l for l in lineas(77) if 39968 in (l["sistema"] or {}).get("pago_ids", []))
        self.assertEqual(x["tipo"], "sin_aplicar")


class TestJefaturaKine(_ArmijoBase):
    """Leo Etcheverry (21) = 50% de su venta + 10% de la venta de Luis Armijo (77),
    que paga el centro. Base de Luis = la misma con que se le paga a él."""

    def setUp(self):
        super().setUp()
        from session import db
        with db() as c:
            c.execute("UPDATE equipo_cmc SET pct_honorario=50 WHERE id_medilink IN (21, 77)")
            c.execute("INSERT INTO bi_pagos_caja (pago_id, fecha, id_profesional, id_paciente, monto, "
                      "nombre_paciente) VALUES (999001, '2026-09-10', 21, 1, 2818560, 'Paciente Ficticio Leo')")

    def test_jefatura_con_reporte_de_luis(self):
        self.assertEqual(self._pegar().status_code, 200)
        m = mes()
        luis, leo = prof(m, 77), prof(m, 21)
        self.assertEqual(luis["liquidacion"]["base"], 935250)
        self.assertEqual(luis["liquidacion"]["bruto"], 467625)                 # a Luis no se le descuenta
        j = leo["liquidacion"]["jefatura"]
        self.assertEqual([(x["id_profesional"], x["base"], x["monto"]) for x in j], [(77, 935250, 93525)])
        self.assertEqual(leo["liquidacion"]["bruto"], 1502805)                 # 0,5 × 2.818.560 + 93.525
        self.assertEqual(leo["liquidacion"]["liquido"], round(1502805 * (1 - ebitda_routes.RETENCION_BOLETA)))
        t = cli.get("/api/remuneraciones/whatsapp", params={**Q, "prof": 21, "mes": MES}).json()["texto"]
        self.assertIn("Jefatura kinesiología: 10% de la venta de Luis Armijo ($935.250) = $93.525", t)
        self.assertIn("Boleta de honorarios a emitir: $1.502.805", t)

    def test_jefatura_igual_en_ebitda_sin_reporte(self):
        from session import db
        with db() as c, ebitda_routes.solo_datos_locales():
            e = {p["id"]: p for p in ebitda_routes._ebitda_mes(c, MES)["profesionales"]}
        leo = prof(mes(), 21)
        self.assertEqual(e[21]["bruto"], 1409280 + 95399)                     # 10% de 953.990 (espejo)
        self.assertEqual(leo["liquidacion"]["bruto"], e[21]["bruto"])
        self.assertEqual(leo["liquidacion"]["liquido"], e[21]["liquido"])
        self.assertEqual(e[21]["cmc"], 2818560 - 1409280 - 95399)             # lo paga el centro
        self.assertEqual(e[77]["bruto"], 476995)


from collections import Counter  # noqa: E402

if __name__ == "__main__":
    unittest.main(verbosity=2)
