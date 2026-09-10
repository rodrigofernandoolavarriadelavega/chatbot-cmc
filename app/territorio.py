# -*- coding: utf-8 -*-
"""Motor de territorio del CMC — Capa 1: DENOMINADOR y demanda no capturada.

POR QUE EXISTE
--------------
El heatmap actual mide **demanda capturada**: de donde vienen los que ya vinieron.
Eso responde "donde estan mis pacientes", no "cuantos hay". Sin denominador, mas
resolucion geografica no agrega informacion: Arauco puede parecer el mejor sector
solo por ser el mas poblado.

Este modulo agrega la mitad que falta: cuanta demanda GENERA cada sector, para
poder decir "en Curanilahue capturamos el 1,7% de lo que ese territorio produce"
en vez de "Curanilahue son $5,79M".

QUE SALE DEL MODELO
-------------------
**Pacientes nuevos por mes**, no pesos. Decision del dueno (2026-09-07): la venta
se contamina con el mix — agosto 2026 crecio por prestaciones de %-alto y el
margen CAYO 35,4%→33,4%. Un modelo calibrado contra pesos aprende el mix, no el
territorio. La conversion a plata es una capa APARTE, encima (arancel x mix).

POR QUE LA PENETRACION NO USA NINGUN SUPUESTO
---------------------------------------------
La primera version de este motor convertia habitantes en "consultas potenciales"
usando una tasa per capita — un parametro sin fuente citable que movia el
resultado mas que ningun otro. Era innecesario: si la salida se mide en
**personas** (decision del dueno), la penetracion es `personas atendidas /
habitantes`, un cociente que solo necesita el Censo. El supuesto mas peligroso
del modelo existia por una eleccion de unidad que ya habiamos descartado.

`TASA_CONSULTAS_ANUALES` sigue aca pero solo alimenta una metrica SECUNDARIA
(cobertura de demanda), que permanece bloqueada hasta que alguien la sourcee.

LO QUE ESTE MODULO **NO** HACE (y por que importa)
--------------------------------------------------
No calcula "fuga". La fuga real (gente de Arauco que se atiende en Concepcion)
exige el dato de la red publica por comuna de residencia (DEIS) — eso es Capa 2.
Aca el residual se llama `no_capturado` y agrupa cuatro cosas distintas que
todavia NO sabemos separar: red publica local, privados locales, fuga fuera de la
provincia, y demanda que simplemente no consulta. Llamarlo "fuga" seria inventar.

REGLA DURA
----------
Todo supuesto es un `Parametro` con fuente. Un parametro sin fuente NO se usa: el
motor levanta excepcion. Es deliberado — es exactamente el error que comete el
"92% de asertividad" de la competencia, un numero sin definicion que se propaga
por cada slide hasta parecer un hecho.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Iterable, Literal, Sequence

from localidades_arauco import POBLACION, COMUNA_DISPLAY

__all__ = [
    "Parametro", "SIN_FUENTE", "TASA_CONSULTAS_ANUALES",
    "POBLACION_SECTOR", "poblacion_de",
    "Visita", "Clasificacion", "clasificar_paciente",
    "Potencial", "potencial", "Penetracion", "penetracion", "Nivel",
    "VENTANA_MESES", "SEPARACION_MINIMA_DIAS",
]

Confianza = Literal["alta", "media", "baja", "sin_fuente"]


def _fecha(iso: str) -> date:
    return date.fromisoformat(iso[:10])


# ── parametros con procedencia ──────────────────────────────────────────────
class ParametroSinFuente(RuntimeError):
    """Se intento usar un supuesto que nadie sourceo. Es un bug, no un warning."""


@dataclass(frozen=True)
class Parametro:
    """Un supuesto del modelo, con de donde salio.

    `valor` puede existir como marcador de posicion mientras `confianza` sea
    "sin_fuente" — pero `usar()` lo rechaza. Asi el placeholder sirve para probar
    el motor y es imposible que se cuele a un numero que alguien va a mirar.
    """
    nombre: str
    valor: float
    unidad: str
    fuente: str
    confianza: Confianza

    def usar(self) -> float:
        if self.confianza == "sin_fuente":
            raise ParametroSinFuente(
                f"'{self.nombre}' no tiene fuente. Es el parametro que mas mueve "
                f"el resultado de todo el modelo. Sourcealo (DEIS/FONASA) o pasa "
                f"un Parametro propio a potencial(..., tasa=...). "
                f"Placeholder actual: {self.valor} {self.unidad}."
            )
        return self.valor


SIN_FUENTE: Confianza = "sin_fuente"

# El coeficiente que convierte habitantes en consultas. Es el parametro mas
# sensible del modelo entero: al doble de tasa, la penetracion cae a la mitad y
# la conclusion estrategica se da vuelta.
# BUSCADO el 2026-09-07 sin resultado citable en una pasada de busqueda web.
# Donde esta de verdad: DEIS/MINSAL, serie de atenciones ambulatorias (REM A04)
# cruzada con proyeccion de poblacion INE — idealmente filtrada a Region del
# Biobio, porque la tasa nacional incluye a Santiago y sobreestima provincia.
TASA_CONSULTAS_ANUALES = Parametro(
    nombre="consultas_ambulatorias_per_capita_anuales",
    valor=2.5,                      # PLACEHOLDER — no se usa hasta tener fuente
    unidad="consultas/habitante/ano",
    fuente="PENDIENTE — DEIS REM A04 x proyeccion INE, Region del Biobio",
    confianza=SIN_FUENTE,
)


# ── denominador poblacional ─────────────────────────────────────────────────
# Nivel COMUNA: Censo 2024, ya vive en localidades_arauco.POBLACION.
#
# Nivel SECTOR: **Censo 2024 INE, por localidad**, bajado del Feature Service
# publico de microdatos (ver scripts/censo2024_entidades.py). Suma el estrato
# URBANO (capa Manzanas) y el RURAL (capa Manzanas-entidades): la capa rural sola
# da Carampangue = 363 habitantes, que es solo su periferia.
#
# Reemplaza al Censo 2017 por distrito reescalado que se uso hasta el 2026-09-07.
# El reescalado suponia que la distribucion interna de la comuna no cambio en 7
# anos, y contra el dato real fallaba justo donde mas importa:
#     Carampangue    estimado 4.688   real 3.644   (+29% inflado)
#     Ramadillas     estimado 3.021   real 2.178   (+39%)
#     Arauco urbano  estimado 16.379  real 17.615  (-7%)
# Un denominador 29% inflado en Carampangue subestimaba su penetracion en ~22%,
# justo en el sector donde se evaluan inversiones (Monsalve 168, edificio propio).
#
# NOMBRES REPETIDOS: el INE tiene un Quidico en Arauco y otro en Tirua, un
# Huillinco en Canete y otro en Contulmo. Como este dict se indexa por nombre,
# aceptar los dos produciria una clave repetida y Python se quedaria callado con
# la ultima. El cargador solo acepta la fila cuya comuna coincide con la que
# localidades_arauco le asigna a ese sector.
#
# Lo que NO esta aca: las localidades del INE que el diccionario todavia no sabe
# emitir (Antiguala 3.484, Guape 1.424, Santa Rosa 1.805...). Salen listadas al
# correr el cargador. Cobertura tras agregar 23 localidades al diccionario (2026-09-08):
# Los Alamos 95,0% · Arauco 93,6% · Curanilahue 91,4% · Lebu 85,7% ·
# Canete 78,6% · Contulmo 73,6% · Tirua 53,0%.
POBLACION_SECTOR: dict[str, tuple[int, str, Confianza]] = {
    'Arauco urbano'         : ( 17615, "Censo 2024 INE localidad (urbano+rural)", "alta"),   # Arauco
    'Laraquete'             : (  5123, "Censo 2024 INE localidad (urbano+rural)", "alta"),   # Arauco
    'Carampangue'           : (  3644, "Censo 2024 INE localidad (urbano+rural)", "alta"),   # Arauco
    'Ramadillas'            : (  2178, "Censo 2024 INE localidad (urbano+rural)", "alta"),   # Arauco
    'Tubul'                 : (  1763, "Censo 2024 INE localidad (urbano+rural)", "alta"),   # Arauco
    'Punta Lavapié'         : (   832, "Censo 2024 INE localidad (urbano+rural)", "alta"),   # Arauco
    'Horcones'              : (   830, "Censo 2024 INE localidad (urbano+rural)", "alta"),   # Arauco
    'Pichilo'               : (   737, "Censo 2024 INE localidad (urbano+rural)", "alta"),   # Arauco
    'Llico'                 : (   559, "Censo 2024 INE localidad (urbano+rural)", "alta"),   # Arauco
    'Nine'                  : (   350, "Censo 2024 INE localidad (urbano+rural)", "alta"),   # Arauco
    'Curaquilla'            : (   291, "Censo 2024 INE localidad (urbano+rural)", "alta"),   # Arauco
    'Las Puentes'           : (   272, "Censo 2024 INE localidad (urbano+rural)", "alta"),   # Arauco
    'Rumena'                : (   265, "Censo 2024 INE localidad (urbano+rural)", "alta"),   # Arauco
    'Raqui'                 : (   225, "Censo 2024 INE localidad (urbano+rural)", "alta"),   # Arauco
    'Quiapo'                : (    95, "Censo 2024 INE localidad (urbano+rural)", "alta"),   # Arauco
    'Cañete urbano'         : ( 17357, "Censo 2024 INE localidad (urbano+rural)", "alta"),   # Canete
    'Guape'                 : (  1424, "Censo 2024 INE localidad (urbano+rural)", "alta"),   # Canete
    'Huillinco'             : (   998, "Censo 2024 INE localidad (urbano+rural)", "alta"),   # Canete
    'Peleco'                : (   921, "Censo 2024 INE localidad (urbano+rural)", "alta"),   # Canete
    'Tres Sauces'           : (   803, "Censo 2024 INE localidad (urbano+rural)", "alta"),   # Canete
    'Lautaro Antiquina'     : (   787, "Censo 2024 INE localidad (urbano+rural)", "alta"),   # Canete
    'Huechicura'            : (   781, "Censo 2024 INE localidad (urbano+rural)", "alta"),   # Canete
    'Llenquehue'            : (   777, "Censo 2024 INE localidad (urbano+rural)", "alta"),   # Canete
    'Cayucupil'             : (   751, "Censo 2024 INE localidad (urbano+rural)", "alta"),   # Canete
    'Lloncao'               : (   739, "Censo 2024 INE localidad (urbano+rural)", "alta"),   # Canete
    'El Reposo'             : (   736, "Censo 2024 INE localidad (urbano+rural)", "alta"),   # Canete
    'Pangueco'              : (   576, "Censo 2024 INE localidad (urbano+rural)", "alta"),   # Canete
    'Pocuno'                : (   309, "Censo 2024 INE localidad (urbano+rural)", "alta"),   # Canete
    'Antiquina'             : (   269, "Censo 2024 INE localidad (urbano+rural)", "alta"),   # Canete
    'Contulmo urbano'       : (  2808, "Censo 2024 INE localidad (urbano+rural)", "alta"),   # Contulmo
    'Calebu'                : (   803, "Censo 2024 INE localidad (urbano+rural)", "alta"),   # Contulmo
    'Elicura'               : (   684, "Censo 2024 INE localidad (urbano+rural)", "alta"),   # Contulmo
    'Curanilahue urbano'    : ( 28504, "Censo 2024 INE localidad (urbano+rural)", "alta"),   # Curanilahue
    'Pichiarauco'           : (   500, "Censo 2024 INE localidad (urbano+rural)", "alta"),   # Curanilahue
    'Lebu urbano'           : ( 19857, "Censo 2024 INE localidad (urbano+rural)", "alta"),   # Lebu
    'Santa Rosa'            : (  1805, "Censo 2024 INE localidad (urbano+rural)", "alta"),   # Lebu
    'Isla Mocha'            : (   433, "Censo 2024 INE localidad (urbano+rural)", "alta"),   # Lebu
    'Millaneco'             : (   223, "Censo 2024 INE localidad (urbano+rural)", "alta"),   # Lebu
    'Los Álamos urbano'     : (  8331, "Censo 2024 INE localidad (urbano+rural)", "alta"),   # Los Alamos
    'Cerro Alto'            : (  6504, "Censo 2024 INE localidad (urbano+rural)", "alta"),   # Los Alamos
    'Antiguala'             : (  3484, "Censo 2024 INE localidad (urbano+rural)", "alta"),   # Los Alamos
    'Quillaitún'            : (   592, "Censo 2024 INE localidad (urbano+rural)", "alta"),   # Los Alamos
    'Sara de Lebu'          : (   584, "Censo 2024 INE localidad (urbano+rural)", "alta"),   # Los Alamos
    'Ranquilco'             : (   535, "Censo 2024 INE localidad (urbano+rural)", "alta"),   # Los Alamos
    'Tirúa urbano'          : (  2404, "Censo 2024 INE localidad (urbano+rural)", "alta"),   # Tirua
    'Quidico'               : (   968, "Censo 2024 INE localidad (urbano+rural)", "alta"),   # Tirua
    'Tranaquepe'            : (   853, "Censo 2024 INE localidad (urbano+rural)", "alta"),   # Tirua
    'Quilquilco'            : (   450, "Censo 2024 INE localidad (urbano+rural)", "alta"),   # Tirua
    'Ranquilhue Grande'     : (   447, "Censo 2024 INE localidad (urbano+rural)", "alta"),   # Tirua
}


Nivel = Literal["comuna", "sector"]


def poblacion_de(ambito: str, nivel: Nivel) -> tuple[int | None, str, Confianza]:
    """Habitantes de un ambito. `nivel` es OBLIGATORIO y no se deduce del nombre.

    POR QUE OBLIGATORIO — bug real, cazado el 2026-09-07 contra datos de
    produccion. "Arauco" es a la vez comuna (37.163), ciudad (15.980) y
    provincia. Con un solo diccionario y precedencia implicita, la consulta por
    la COMUNA Arauco devolvia la poblacion de la CIUDAD y la penetracion salia
    11,32% en vez de 4,99% — mas del doble. No explotaba: devolvia un numero
    perfectamente plausible. Cualquier jerarquia geografica tiene este problema
    (comuna/ciudad/distrito comparten nombre por regla, no por excepcion), asi
    que el nivel se pide, no se adivina.

    Nunca estima: un ambito sin dato sale como None y se reporta aparte.
    """
    if nivel == "comuna":
        if ambito in POBLACION:
            return (POBLACION[ambito], "Censo 2024 INE", "alta")
    elif ambito in POBLACION_SECTOR:
        return POBLACION_SECTOR[ambito]
    return (None, "sin dato", SIN_FUENTE)


# ── clasificacion de pacientes ──────────────────────────────────────────────
@dataclass(frozen=True)
class Visita:
    """Una atencion observada, ya resuelta a sector por localidades_arauco."""
    id_paciente: int
    fecha: str                      # ISO "YYYY-MM-DD"
    sector: str | None
    comuna: str | None
    especialidad: str | None = None


Clasificacion = Literal["capturado", "parcial", "no_capturado"]


VENTANA_MESES = 24
SEPARACION_MINIMA_DIAS = 90


def clasificar_paciente(visitas: Sequence[Visita], hasta: str) -> Clasificacion:
    """Decide si un paciente cuenta como CAPTURADO por el CMC.

    Regla (decidida 2026-09-07):
      capturado    >=2 atenciones en los ultimos 24 meses, separadas >=90 dias
      parcial      alguna atencion en la ventana, pero sin relacion sostenida
      no_capturado ninguna atencion en la ventana

    POR QUE 24 MESES. El consumo de salud es episodico: un adulto sano puede
    tener un hueco legitimo de 14 meses y seguir siendo paciente del CMC. A 12
    meses la ventana los expulsa; a 36 nunca detecta que un territorio se esta
    perdiendo. 24 es ademas la definicion habitual de "paciente activo" en
    panel de atencion primaria.

    POR QUE 90 DIAS DE SEPARACION — es la parte que mas cambia los numeros.
    Sin esa condicion, **una serie de kinesiologia (10 sesiones + 2
    reevaluaciones en ~8 semanas) contaria como paciente capturado**, y lo mismo
    un control a las dos semanas de una consulta. Eso es UN episodio clinico, no
    una relacion. Y no seria un error parejo: inflaria la penetracion justo en
    los sectores donde kine es fuerte, que son los que se estan evaluando para
    invertir. Dos atenciones separadas por un trimestre si son evidencia de que
    el paciente vuelve al CMC por decision propia.

    Las visitas posteriores a `hasta` se ignoran: la base trae citas agendadas
    hasta 2027 y contarlas seria leer el futuro.
    """
    corte = _fecha(hasta)
    inicio = corte - timedelta(days=round(VENTANA_MESES * 30.44))
    fechas = sorted({_fecha(v.fecha) for v in visitas
                     if inicio <= _fecha(v.fecha) <= corte})
    if not fechas:
        return "no_capturado"
    if (fechas[-1] - fechas[0]).days >= SEPARACION_MINIMA_DIAS:
        return "capturado"
    return "parcial"


# ── potencial y penetracion ─────────────────────────────────────────────────
@dataclass(frozen=True)
class Potencial:
    ambito: str
    habitantes: int | None
    consultas_anuales: float | None
    fuente_poblacion: str
    confianza: Confianza


def potencial(ambito: str, tasa: Parametro = TASA_CONSULTAS_ANUALES) -> Potencial:
    """Consultas/ano que GENERA un territorio. Levanta si la tasa no tiene fuente."""
    hab, fuente, conf = poblacion_de(ambito, "comuna")
    if hab is None:
        return Potencial(ambito, None, None, fuente, SIN_FUENTE)
    return Potencial(ambito, hab, hab * tasa.usar(), fuente, conf)


@dataclass(frozen=True)
class Penetracion:
    """Penetracion = personas atendidas / habitantes. Sin supuestos."""
    ambito: str
    display: str
    habitantes: int | None
    capturados: int
    parciales: int
    pct: float | None
    fuente_poblacion: str
    confianza: Confianza
    nota: str = ""

    @property
    def pct_con_parciales(self) -> float | None:
        """Cota superior: cuenta tambien a los de un solo episodio."""
        if not self.habitantes:
            return None
        return (self.capturados + self.parciales) / self.habitantes * 100


def penetracion(ambito: str,
                pacientes: dict[int, Sequence[Visita]],
                hasta: str,
                nivel: Nivel = "comuna") -> Penetracion:
    """Penetracion del CMC en un sector o comuna.

    `pacientes` mapea id_paciente -> sus atenciones, YA filtrado al ambito.
    No recibe ningun parametro estimado: el unico insumo externo es el Censo.
    """
    hab, fuente, conf = poblacion_de(ambito, nivel)
    caps = pars = 0
    for visitas in pacientes.values():
        c = clasificar_paciente(visitas, hasta)
        if c == "capturado":
            caps += 1
        elif c == "parcial":
            pars += 1
    display = COMUNA_DISPLAY.get(ambito, ambito)
    if not hab:
        return Penetracion(ambito, display, None, caps, pars, None, fuente,
                           SIN_FUENTE, nota="sin denominador")
    return Penetracion(ambito, display, hab, caps, pars,
                       caps / hab * 100, fuente, conf)
