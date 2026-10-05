"""CAPI Purchase con valor real (2026-10-05): solo atendidos, value = margen del centro,
difiere si la caja no cerró, idempotente, Imagendent = venta - costo."""
import asyncio
import sys
import tempfile
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))

import session  # noqa: E402

session.DB_PATH = Path(tempfile.mkdtemp()) / "t.db"
with session.db() as _c:
    session._run_ddl_inline(_c)
    _c.commit()

import ausentismo  # noqa: E402
import capi_purchase as cp  # noqa: E402
import meta_capi  # noqa: E402

ausentismo.ensure_ausentismo_table()
cp.ensure_table()
cp.CAPI_PURCHASE_DESDE = "2026-10-01"

FALLAS = []


def check(nombre, cond):
    print(("OK  " if cond else "FAIL") + " " + nombre)
    if not cond:
        FALLAS.append(nombre)


HOY = date(2026, 10, 5)
AYER = "2026-10-04"
CLT = ZoneInfo("America/Santiago")
# sync completo de la noche del 4 (23:59 CLT = 02:59 UTC del 5)
SYNC_OK = "2026-10-05 02:59:05"

with session.db() as c:
    c.execute("CREATE TABLE IF NOT EXISTS equipo_cmc (id_medilink INTEGER, pct_honorario INTEGER)")
    c.execute("INSERT INTO equipo_cmc VALUES (1, 70), (55, 45)")
    c.execute("""CREATE TABLE IF NOT EXISTS convenio_consumo (
        detalle_id INTEGER PRIMARY KEY, convenio TEXT, atencion_id INTEGER, fecha TEXT,
        id_paciente INTEGER, id_profesional INTEGER, venta INTEGER, cobrado INTEGER, costo INTEGER)""")


def cita(id_cita, phone, pac, prof, estado, anul=0, fecha=AYER, hora="10:00", esp="Medicina General"):
    with session.db() as c:
        c.execute("INSERT INTO citas_bot(phone,id_cita,especialidad,profesional,fecha,hora) VALUES(?,?,?,?,?,?)",
                  (phone, str(id_cita), esp, "X", fecha, hora))
        c.execute("INSERT OR REPLACE INTO ausentismo_citas(id_cita,id_profesional,id_paciente,fecha,hora,"
                  "id_estado,anulacion) VALUES(?,?,?,?,?,?,?)", (id_cita, prof, pac, fecha, hora, estado, anul))


_pid = [100]


def pago(pac, prof, monto, fecha=AYER, synced=SYNC_OK):
    _pid[0] += 1
    with session.db() as c:
        c.execute("INSERT INTO bi_pagos_caja(pago_id,fecha,id_profesional,id_paciente,monto,synced_at) "
                  "VALUES(?,?,?,?,?,?)", (_pid[0], fecha, prof, pac, monto, synced))


ENVIADOS = []


async def fake_send(event_name, phone, **kw):
    ENVIADOS.append({"event": event_name, "phone": phone, **kw})
    return {"events_received": 1}


meta_capi.send_event = fake_send


def correr():
    ENVIADOS.clear()
    return asyncio.run(cp.enviar_purchases(HOY))


# 1) atendido con pago: Olavarría (1, pct 70) cobró 20.000 -> centro 6.000
cita(1001, "56911111111", 501, 1, 2)
pago(501, 1, 20000)
# 2) no-show -> no envía
cita(1002, "56922222222", 502, 1, 8)
pago(502, 1, 20000)          # aunque exista pago, no-show no envía
# 3) Imagendent: Burgos (55, pct 45) cobró 55.000, de los cuales 15.000 son radiografías
#    centro = 40.000*0,55 + (15.000-10.000) = 22.000+5.000 = 27.000
cita(1003, "56933333333", 503, 55, 2, esp="Odontología General")
pago(503, 55, 40000)
pago(503, 55, 15000)
with session.db() as c:
    c.execute("INSERT INTO convenio_consumo VALUES(1,'imagendent',1,?,503,55,15000,15000,10000)", (AYER,))
# 4) atendido sin pago y caja cerrada -> estimado (mediana de pagos prev. del prof 1 = 20.000 -> 6.000)
cita(1004, "56944444444", 504, 1, 2)
# historial del prof 1 para la mediana
for m in (20000, 20000, 20000):
    pago(900 + m % 7, 1, m, fecha="2026-09-20")
# 5) duplicada: misma persona, mismo prof, mismo día
cita(1005, "56955555555", 505, 1, 2, hora="09:00")
cita(1006, "56955555555", 505, 1, 2, hora="09:30")
pago(505, 1, 10000)
# 6) cita sin fila en ausentismo -> difiere
with session.db() as c:
    c.execute("INSERT INTO citas_bot(phone,id_cita,especialidad,profesional,fecha,hora) VALUES(?,?,?,?,?,?)",
              ("56966666666", "1007", "Medicina General", "X", AYER, "11:00"))
# 7) reagenda (14) y anulada no envían
cita(1008, "56977777777", 508, 1, 14, anul=1)

r = correr()
por_cita = {e["event_id"]: e for e in ENVIADOS}
e1 = por_cita.get("purchase_cita_1001")
check("atendido con pago: value = margen centro (30%)", e1 and e1["value"] == 6000.0)
check("custom_data venta_total + currency CLP",
      e1 and e1["custom_data"]["venta_total"] == 20000 and e1["custom_data"]["currency"] == "CLP"
      and e1["currency"] == "CLP")
check("no marca value_estimado cuando hay pago", e1 and "value_estimado" not in e1["custom_data"])
check("event_time = hora real de la cita (10:00 CLT)",
      e1 and e1["event_time"] == int(datetime(2026, 10, 4, 10, 0, tzinfo=CLT).timestamp()))
check("no-show no envía", "purchase_cita_1002" not in por_cita)
e3 = por_cita.get("purchase_cita_1003")
check("Imagendent: venta - costo en radiografías", e3 and e3["value"] == 27000.0 and e3["custom_data"]["venta_total"] == 55000)
e4 = por_cita.get("purchase_cita_1004")
check("sin pago con caja cerrada: estimado marcado", e4 and e4["value"] == 6000.0
      and e4["custom_data"].get("value_estimado") is True and e4["custom_data"]["venta_total"] == 0)
check("duplicada paciente+prof+día: UNA visita",
      ("purchase_cita_1005" in por_cita) != ("purchase_cita_1006" in por_cita))
check("sin fila de ausentismo: difiere", "purchase_cita_1007" not in por_cita and r["diferidos"] == 1)
check("reagenda no envía", "purchase_cita_1008" not in por_cita)
check("contadores", r["enviados"] == 4 and r["duplicados"] == 1 and r["no_atendidos"] == 2
      and r["estimados"] == 1)

# idempotencia: segunda corrida no reenvía nada de lo ya enviado
r2 = correr()
check("idempotente: segunda corrida no reenvía", r2["enviados"] == 0 and not ENVIADOS)

# caja aún sin sincronizar para el día: difiere (no inventa monto)
cita(2001, "56988888888", 601, 1, 2, fecha="2026-10-03")
with session.db() as c:
    c.execute("UPDATE bi_pagos_caja SET synced_at='2026-10-03 15:00:00' WHERE fecha>='2026-10-03' AND fecha<='2026-10-04'")
r3 = correr()
check("caja no cerrada y sin pago: difiere", r3["enviados"] == 0 and r3["diferidos"] >= 2)
with session.db() as c:
    c.execute("UPDATE bi_pagos_caja SET synced_at=? WHERE fecha>='2026-10-03'", (SYNC_OK,))
r4 = correr()
check("al cerrar la caja se envía el diferido (estimado)",
      any(e["event_id"] == "purchase_cita_2001" for e in ENVIADOS))

# fallo de Meta: no se marca y se reintenta


async def fake_fail(event_name, phone, **kw):
    return {"error": "http_500"}


cita(3001, "56999999999", 701, 1, 2, fecha="2026-10-04", hora="12:00")
pago(701, 1, 30000)
meta_capi.send_event = fake_fail
r5 = asyncio.run(cp.enviar_purchases(HOY))
check("error de Meta: no se marca", r5["errores"] >= 1 and r5["enviados"] == 0)
meta_capi.send_event = fake_send
ENVIADOS.clear()
r6 = asyncio.run(cp.enviar_purchases(HOY))
check("reintento exitoso al día siguiente", any(e["event_id"] == "purchase_cita_3001" for e in ENVIADOS))

# fuera de ventana (>6 días) no se toca
cita(4001, "56900000001", 801, 1, 2, fecha="2026-09-25")
pago(801, 1, 10000, fecha="2026-09-25")
cp.CAPI_PURCHASE_DESDE = "2026-09-01"
ENVIADOS.clear()
asyncio.run(cp.enviar_purchases(HOY))
check("citas de hace más de 6 días no se envían", not any(e["event_id"] == "purchase_cita_4001" for e in ENVIADOS))

print("\n%s" % ("TODO OK" if not FALLAS else "FALLAS: %s" % FALLAS))
sys.exit(1 if FALLAS else 0)
