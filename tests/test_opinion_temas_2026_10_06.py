"""Temas de opinión (2026-10-06): clasificación incremental + tarjeta del Radar.
DB temporal, datos sintéticos y cliente de Anthropic FALSO (sin red)."""
import json
import re
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ / "app"))
import session  # noqa: E402

TMP = Path(tempfile.mkdtemp())
session.DB_PATH = TMP / "t.db"
with session.db() as _c:
    session._run_ddl_inline(_c)
    _c.commit()

import opinion_temas as ot  # noqa: E402
import radar_routes as rr  # noqa: E402

FALLAS = []


def check(n, c):
    print(("OK  " if c else "FAIL") + " " + n)
    if not c:
        FALLAS.append(n)


TEL = "56911112222"
OPS = {"La doctora me trató muy bien, excelente atención": ("trato", "positivo"),
       "Esperé una hora y media para que me atendieran": ("puntualidad_espera", "negativo"),
       "Muy caro el control, no me alcanza para volver": ("precio", "negativo")}
with session.db() as c:
    c.execute("INSERT INTO citas_bot(id_cita,phone,profesional) VALUES('C1',?,'Dra. Prueba')", (TEL,))
    c.execute("INSERT INTO fidelizacion_msgs(phone,tipo,cita_id,enviado_en) VALUES(?,'postconsulta','C1',datetime('now','-2 days'))", (TEL,))
    # fuera de ventana de 72 h, otro tipo, corto
    c.execute("INSERT INTO messages(phone,direction,text,ts) VALUES(?,'in','Mensaje fuera de la ventana de tiempo',datetime('now','-10 hours'))", (TEL,))
    c.execute("UPDATE messages SET ts=datetime('now','-30 days') WHERE id=1")
    for t in list(OPS) + ["Quiero agendar para el lunes por favor, mi correo es juan@mail.cl y mi número 912345678",
                          "corto", "muy corto msg", "Necesito el RUT 12.345.678-9 en la boleta, gracias"]:
        c.execute("INSERT INTO messages(phone,direction,text,ts) VALUES(?,'in',?,datetime('now','-1 days'))", (TEL, t))
    c.execute("INSERT INTO messages(phone,direction,text,ts) VALUES(?,'out','Respuesta del bot a la paciente de prueba',datetime('now','-1 days'))", (TEL,))
    c.commit()


class Falso:
    def __init__(self):
        self.llamadas = []
        self.messages = self

    def create(self, **kw):
        self.llamadas.append(kw)
        filas = re.findall(r"^(\d+): (.*)$", kw["messages"][0]["content"], re.M)
        out = []
        for i, txt in filas:
            tx = json.loads(txt)
            m = next(((t, to) for k, (t, to) in OPS.items() if k == tx), None)
            out.append({"i": int(i), "es_opinion": bool(m), "tema": m[0] if m else "agenda_disponibilidad",
                        "tono": m[1] if m else "neutro"})
        return SimpleNamespace(content=[SimpleNamespace(text="Resultado:\n" + json.dumps(out))],
                               usage=SimpleNamespace(input_tokens=1000, output_tokens=200))


f = Falso()
r = ot.correr(client=f)
enviado = json.dumps(f.llamadas, ensure_ascii=False)
check("clasifica solo mensajes entrantes de la ventana (5, no 'corto', no fuera de ventana, no 'out')", r["nuevos"] == 5)
check("solo 3 son opinión", r["opiniones"] == 3)
check("al modelo no viaja teléfono", TEL not in enviado and "912345678" not in enviado)
check("al modelo no viaja correo ni RUT", "juan@mail.cl" not in enviado and "12.345.678-9" not in enviado
      and "[correo]" in enviado and "[nº]" in enviado)
check("usa Haiku", f.llamadas[0]["model"].startswith("claude-haiku"))
check("costo calculado", abs(r["costo_usd"] - 0.002) < 1e-6)
with session.db() as c:
    rows = c.execute("SELECT * FROM opinion_temas").fetchall()
    check("profesional de la cita", all(x["profesional"] == "Dra. Prueba" for x in rows))
    check("no opiniones sin tema ni texto guardado", all(x["tema"] is None and x["texto"] is None for x in rows if not x["es_opinion"]))
# incremental
f2 = Falso()
r2 = ot.correr(client=f2)
check("incremental: segunda corrida no llama al modelo", r2["nuevos"] == 0 and f2.llamadas == [])
with session.db() as c:
    c.execute("INSERT INTO messages(phone,direction,text,ts) VALUES(?,'in','El doctor explicó todo con mucha claridad',datetime('now'))", (TEL,))
    c.commit()
f3 = Falso()
check("incremental: solo el mensaje nuevo", ot.correr(client=f3)["nuevos"] == 1)
# tope
with session.db() as c:
    for i in range(40):
        c.execute("INSERT INTO messages(phone,direction,text,ts) VALUES(?,'in',?,datetime('now'))", (TEL, f"Comentario número {i} sobre la consulta de hoy"))
    c.commit()
f4 = Falso()
r4 = ot.correr(client=f4, tope=30)
check("tope por corrida", r4["nuevos"] == 30 and len(f4.llamadas) == 2)
check("lotes de hasta 25", all(len(re.findall(r"^\d+: ", k["messages"][0]["content"], re.M)) <= 25 for k in f4.llamadas))
# salida rota del modelo: no guarda y reintenta luego
class Roto(Falso):
    def create(self, **kw):
        return SimpleNamespace(content=[SimpleNamespace(text="no es json")], usage=None)
r5 = ot.correr(client=Roto(), tope=5)
check("JSON inválido no se guarda", r5["nuevos"] == 0)
check("enmascarado", ot.enmascarar("llame al +56 9 1234 5678 o a x@y.cl, rut 12345678-9") == "llame al [nº] o a [correo], rut [nº]")
check("enmascarado conserva cifras cortas", ot.enmascarar("a las 10:30 con 2 recetas") == "a las 10:30 con 2 recetas")

# Radar
d = rr.reputacion_data()["temas"]
blob = json.dumps(d, ensure_ascii=False)
check("radar: hay datos", d["hay"] and d["total"] == 3 and d["ultima"])
t = {x["tema"]: x for x in d["temas"]}
check("radar: conteos por tono", t["trato"]["positivo"] >= 1 and t["puntualidad_espera"]["negativo"] == 1)
check("radar: máx 2 ejemplos ≤140", all(len(x["ejemplos"]) <= 2 and all(len(e["texto"]) <= 140 for e in x["ejemplos"]) for x in d["temas"]))
check("radar: solo es_opinion (nada de agenda)", "agenda_disponibilidad" not in t and "agendar" not in blob)
check("radar: sin PII ni profesional", TEL not in blob and "juan@" not in blob and "Dra. Prueba" not in blob and "profesional" not in blob)
# vacío honesto
with session.db() as c:
    c.execute("DELETE FROM opinion_temas")
    c.commit()
check("radar: vacío si no corrió el job", rr.reputacion_data()["temas"]["hay"] is False)
with session.db() as c:
    c.execute("DROP TABLE opinion_temas")
    c.commit()
check("radar: sin tabla no falla", rr.reputacion_data()["temas"]["hay"] is False)
# registro
main = (RAIZ / "app" / "main.py").read_text(encoding="utf-8")
i = main.index('id="opinion_temas"')
check("job 03:40 con misfire_grace_time", "hour=3, minute=40" in main[i - 200:i] and "misfire_grace_time" in main[i:i + 120])
check("flag TEMAS_OPINION_ACTIVE por defecto activo", ot.activo())
print(f"\n{len(FALLAS)} fallas")
sys.exit(1 if FALLAS else 0)
