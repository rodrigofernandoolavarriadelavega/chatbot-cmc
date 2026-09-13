"""Capa 1 del motor de territorio: denominador y demanda no capturada.

Estos tests son la ESPECIFICACION de `clasificar_paciente()`, que todavia no
esta implementada. Van a fallar hasta que se defina la regla — eso es a
proposito: fijan el contrato antes que el codigo.

El caso que motiva todo: en agosto 2026 Curanilahue fue el mejor mes del ano
($2,40M) y aun asi su participacion BAJO de 12% a 7,7%, porque Arauco crecio
mas. Con denominador esa contradiccion desaparece: son dos preguntas distintas
(cuanto pesa vs cuanto del territorio capturo) y solo la segunda dice si queda
espacio.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))

import territorio as t
from territorio import Parametro, Visita


TASA_FICTICIA = Parametro(
    nombre="tasa_de_prueba", valor=2.0, unidad="consultas/hab/ano",
    fuente="INVENTADA PARA EL TEST — no usar en produccion", confianza="alta",
)


# ── el guardrail: un supuesto sin fuente no produce numeros ─────────────────
def test_tasa_sin_fuente_levanta_en_vez_de_estimar():
    with pytest.raises(t.ParametroSinFuente):
        t.potencial("Curanilahue")


def test_tasa_con_fuente_si_calcula():
    p = t.potencial("Curanilahue", tasa=TASA_FICTICIA)
    assert p.habitantes == 31750                    # Censo 2024
    assert p.consultas_anuales == 63500.0


# ── el denominador no se inventa ────────────────────────────────────────────
def test_sector_de_curanilahue_no_tiene_denominador_a_proposito():
    """La ciudad de Curanilahue esta partida en 4 distritos censales que no
    calzan con como la gente nombra sus sectores. Asignarlos seria inventar el
    dato justo en la comuna cuya subcuenta motivo todo el diccionario."""
    hab, _f, conf = t.poblacion_de("Colico", "sector")
    assert hab is None and conf == "sin_fuente"


def test_penetracion_sin_denominador_se_reporta_no_se_omite():
    pen = t.penetracion("Colico", {}, hasta="2026-09-07", nivel="sector")
    assert pen.pct is None and pen.habitantes is None
    assert "sin denominador" in pen.nota


def test_sector_usa_censo_2024_no_el_reescalado_2017():
    """El reescalado 2017 inflaba Carampangue 29% (4.688 vs 3.644 real)."""
    hab, fuente, conf = t.poblacion_de("Carampangue", "sector")
    assert hab == 3644
    assert conf == "alta"
    assert "2024" in fuente


def test_el_sector_suma_urbano_y_rural():
    """La capa rural del INE sola da Carampangue = 363: es solo su periferia.
    Confundir las capas subestima cada pueblo en un orden de magnitud."""
    assert t.poblacion_de("Carampangue", "sector")[0] > 3000


# ── contrato de clasificar_paciente() ──────────────────────────────────────
UNA_VISITA_RECIENTE = [Visita(1, "2026-08-20", "Curanilahue", "Curanilahue", "MG")]
UNA_VISITA_VIEJA    = [Visita(2, "2024-03-11", "Curanilahue", "Curanilahue", "MG")]
RELACION_CONTINUA   = [Visita(3, f"2026-0{m}-10", "Curanilahue", "Curanilahue", "MG")
                       for m in (3, 5, 8)]
CORTE = "2026-09-07"


def test_relacion_continua_es_capturado():
    assert t.clasificar_paciente(RELACION_CONTINUA, CORTE) == "capturado"


def test_visita_vieja_no_sigue_contando_como_capturado():
    """Un paciente de hace 2,5 anos no es demanda capturada hoy.

    Si contara, la penetracion solo sube con el tiempo y nunca detecta que un
    territorio se esta perdiendo — que es justo lo que hay que poder ver.
    """
    assert t.clasificar_paciente(UNA_VISITA_VIEJA, CORTE) == "no_capturado"


def test_visita_unica_reciente_es_parcial():
    """Ni capturado ni perdido: visible, sin decidir por nosotros."""
    assert t.clasificar_paciente(UNA_VISITA_RECIENTE, CORTE) == "parcial"


def test_serie_de_kine_es_UN_episodio_no_una_relacion():
    """El test que mas mueve los numeros.

    10 sesiones + 2 reevaluaciones en ~8 semanas es un episodio clinico. Si
    contara como capturado, inflaria la penetracion justo en los sectores donde
    kine es fuerte — que son los que se estan evaluando para invertir.
    """
    serie = [Visita(9, f"2026-06-{d:02d}", "Carampangue", "Arauco", "KINE")
             for d in (1, 3, 8, 10, 15, 17, 22, 24, 29)]
    serie += [Visita(9, "2026-07-06", "Carampangue", "Arauco", "KINE"),
              Visita(9, "2026-07-20", "Carampangue", "Arauco", "KINE")]
    assert t.clasificar_paciente(serie, CORTE) == "parcial"


def test_dos_atenciones_separadas_por_un_trimestre_si_son_relacion():
    v = [Visita(10, "2026-02-10", "Carampangue", "Arauco", "MG"),
         Visita(10, "2026-06-15", "Carampangue", "Arauco", "DENTAL")]
    assert t.clasificar_paciente(v, CORTE) == "capturado"


def test_citas_futuras_no_cuentan():
    """La base trae citas agendadas hasta 2027. Contarlas seria leer el futuro."""
    futuras = [Visita(11, "2027-03-01", "Carampangue", "Arauco", "MG"),
               Visita(11, "2027-07-01", "Carampangue", "Arauco", "MG")]
    assert t.clasificar_paciente(futuras, CORTE) == "no_capturado"


def test_sin_visitas_es_no_capturado():
    assert t.clasificar_paciente([], CORTE) == "no_capturado"


def test_penetracion_no_necesita_ningun_supuesto():
    """El cociente personas/habitantes solo depende del Censo."""
    pacientes = {1: RELACION_CONTINUA, 2: UNA_VISITA_RECIENTE, 3: UNA_VISITA_VIEJA}
    pen = t.penetracion("Curanilahue", pacientes, hasta=CORTE)   # sin tasa
    assert (pen.capturados, pen.parciales) == (1, 1)
    assert pen.pct == pytest.approx(1 / 31750 * 100)
    # los parciales no entran al numerador, pero se reportan como cota superior
    assert pen.pct_con_parciales == pytest.approx(2 / 31750 * 100)


# ── regresion: colision de nombres entre niveles (bug 2026-09-07) ───────────
def test_comuna_arauco_no_devuelve_la_poblacion_de_la_ciudad():
    """El bug que se comio la mitad del denominador de la comuna mas grande.

    "Arauco" es comuna (37.163), ciudad (15.980) y provincia. Con precedencia
    implicita, pedir la COMUNA devolvia la CIUDAD y la penetracion salia 11,32%
    en vez de 4,99%. No explotaba: devolvia un numero plausible.
    """
    assert t.poblacion_de("Arauco", "comuna")[0] == 37163
    assert t.poblacion_de("Arauco urbano", "sector")[0] == 17615
    # y el nombre corto NO es un sector valido: no debe caer de vuelta a la comuna
    assert t.poblacion_de("Arauco", "sector")[0] is None


def test_ninguna_clave_de_sector_es_huerfana():
    """Una clave que ningun paciente puede igualar se lee como '0% de
    penetracion' cuando en realidad es 'no lo estamos mirando'."""
    from localidades_arauco import LOCALIDADES
    emitibles = {v[1] for v in LOCALIDADES.values()}
    huerfanas = sorted(set(t.POBLACION_SECTOR) - emitibles)
    assert huerfanas == [], f"claves sin sector correspondiente: {huerfanas}"


def test_la_suma_de_sectores_no_supera_su_comuna():
    """Guardia de coherencia: si una localidad quedo mal asignada, esto lo caza."""
    from collections import defaultdict
    from localidades_arauco import LOCALIDADES
    sector_comuna = {v[1]: v[0] for v in LOCALIDADES.values()}
    suma = defaultdict(int)
    for sector, (hab, _f, _c) in t.POBLACION_SECTOR.items():
        suma[sector_comuna[sector]] += hab
    for comuna, total in suma.items():
        assert total <= t.POBLACION[comuna], (
            f"{comuna}: sectores suman {total:,} > comuna {t.POBLACION[comuna]:,}")


def test_no_hay_homonimos_entre_comunas():
    """El INE tiene un Quidico en Arauco y otro en Tirua. Un dict literal con
    clave repetida se queda callado con la ultima y borra la otra."""
    from localidades_arauco import LOCALIDADES
    sector_comuna = {v[1]: v[0] for v in LOCALIDADES.values()}
    for sector in t.POBLACION_SECTOR:
        assert sector in sector_comuna, f"{sector} no tiene comuna en el diccionario"
