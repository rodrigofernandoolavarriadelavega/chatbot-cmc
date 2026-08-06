"""
test_examenes_lab.py — el clasificador de resultados de examen.

El caso 1 es el PDF real que rompió el flujo el 2026-08-06 (Manuel Yaupe,
Inmunomédica). Los casos "NO" son los que importan de verdad: clasificar de
más manda a un paciente que quería agendar hacia recepción y le cuesta la hora.
"""
import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "app"))

from examenes_lab import parece_examen, nombre_en_examen

# El texto tal como PyMuPDF lo extrae del PDF real: el orden de las columnas
# sale mezclado y las etiquetas quedan separadas de su valor por un salto de
# línea. Si el regex del nombre asume "Nombre: X" en una línea, falla acá.
YAUPE = """Para verificar este certificado, escanear el QR
o ingresar a www.inmunomedica.cl
Nº Orden
:
Nombre
: MANUEL ARTURO YAUPE RIVAS
00489717
Rut
Toma Muestra
23-07-2026 09:00:56
:
13.861.327-5
:
Fecha Nac.
: 15/12/1979
Solicitante
: TONIA VARELA JIMENEZ
HEMOGRAMA
Hematocrito 44,5 % 40,0 - 54,0
Hemoglobina 15,1 g/dL 13,0 - 18,0
Leucocitos 7.200 /uL 4.000 - 11.000
PERFIL BIOQUIMICO  Muestra: Suero
Glucosa 98 mg/dL Valores de referencia: 70 - 100
Colesterol total 212 mg/dL
Trigliceridos 155 mg/dL
"""

SI = [
    ("examen real Yaupe", YAUPE),
    ("laboratorio + referencia", """LABORATORIO CLINICO HOSPITAL DE ARAUCO
    Paciente: ROSA ELENA MUNOZ SOTO   Rut: 9.876.543-2
    TSH 3,45 uUI/mL   Valores de referencia: 0,4 - 4,0
    Metodo: Quimioluminiscencia   Muestra: Suero
    Validado por: TM Carla Rojas"""),
    ("informe de imagen", """INFORME RADIOLOGICO
    Examen: Radiografia de torax AP y lateral
    Tecnica: Se realizan proyecciones estandar.
    Hallazgos: Campos pulmonares bien ventilados, sin focos de condensacion.
    Conclusion: Radiografia de torax dentro de limites normales.
    Medico radiologo: Dr. Juan Perez"""),
    ("orina completa sin nombre de lab", """RESULTADO DE EXAMEN
    ORINA COMPLETA  Muestra: Orina
    Sedimento urinario: 2-3 leucocitos por campo
    Valores de referencia dentro de rango
    Solicitado por: Dr. Andres Abarca"""),
]

NO = [
    # El veto que evita robarle la hora a quien quiere agendar.
    ("orden medica sin resultados", """ORDEN DE EXAMEN
    Se solicita: Hemograma, Perfil bioquimico, TSH
    Paciente: Luis Alberto Soto   Rut: 15.222.333-4
    Indicacion medica: control anual
    Dr. Rodrigo Olavarria"""),
    ("comprobante de transferencia", """Comprobante de transferencia
    Monto transferido: $60.000
    Destinatario: Centro Medico Carampangue
    Cuenta corriente 0012345678  Banco Itau
    Nro. de operacion 88213"""),
    ("mensaje corto", "hola quiero una hora"),
    ("consentimiento informado", """CONSENTIMIENTO INFORMADO PARA PROCEDIMIENTO
    Yo, abajo firmante, declaro haber sido informado del procedimiento
    y autorizo su realizacion. Firma del paciente."""),
    ("menciona un analito de pasada", """Buenas tardes, el doctor me dijo que
    tengo el colesterol alto y queria consultar por una hora de nutricion."""),
    ("ficha de ingreso", """FICHA DE INGRESO PACIENTE
    Nombre: Ana Maria Torres   Edad: 34   Comuna: Arauco
    Motivo de consulta: control"""),
]

fallos = 0
for nombre, txt in SI:
    ok, sen = parece_examen(txt)
    print(f"{'ok ' if ok else 'FALLA'}  SI  {nombre:34s} {sen[:3]}")
    fallos += not ok
for nombre, txt in NO:
    ok, sen = parece_examen(txt)
    print(f"{'ok ' if not ok else 'FALLA'}  NO  {nombre:34s} {sen[:3]}")
    fallos += ok

# El nombre es informativo (va en el aviso a recepción), pero si sale basura
# el aviso confunde más de lo que ayuda.
casos_nombre = [
    (YAUPE, "MANUEL ARTURO YAUPE RIVAS"),
    ("Paciente: ROSA ELENA MUNOZ SOTO   Rut: 9.876.543-2", "ROSA ELENA MUNOZ SOTO"),
    ("Nombre del paciente: Ana Maria Torres\nEdad: 34", "Ana Maria Torres"),
    ("HEMOGRAMA\nHematocrito 44,5 %", ""),          # sin etiqueta → vacío, no basura
]
for txt, esperado in casos_nombre:
    got = nombre_en_examen(txt)
    ok = got == esperado
    print(f"{'ok ' if ok else 'FALLA'}  nombre  {got!r:35s} esperado {esperado!r}")
    fallos += not ok

print(f"\n{'TODO OK' if not fallos else str(fallos) + ' FALLAS'} "
      f"({len(SI) + len(NO) + len(casos_nombre)} casos)")
sys.exit(1 if fallos else 0)
