# Motor de territorio — Capa 1: denominador y demanda no capturada

**Estado:** motor completo, 17/17 tests verdes, **corrido contra produccion.**
`app/territorio.py` · `app/imputacion_territorial.py`
`scripts/territorio_reporte.py` · `scripts/censo2024_entidades.py` · `scripts/cargar_censo_entidades.py`
En health-bi-project: `scripts/sync_localidad_v2.py`
Tests: `test_territorio.py` (18) · `test_imputacion_territorial.py` (9) · `test_registro_localidad.py` (14) — **41/41**

    PYTHONPATH=app python3 scripts/territorio_reporte.py 2026-09-07

## Que problema resuelve

El heatmap mide de donde vienen los que vinieron: **demanda capturada**. No dice
cuanta hay. Por eso agosto 2026 dio una lectura contradictoria en Curanilahue —
mejor mes del ano ($2,40M) y a la vez participacion a la baja (12% → 7,7%).
Son dos preguntas distintas y solo una importa para decidir:

| Pregunta | Metrica | Dice |
|---|---|---|
| Cuanto pesa este sector | % de la venta | nada sobre si queda espacio |
| **Cuanto del sector capturo** | **penetracion** | **si conviene profundizar o salir** |

## Que hace y que NO hace

Hace: convierte habitantes (Censo 2024) en consultas potenciales, cuenta
capturados desde datos propios, y saca penetracion por sector/comuna.

**No calcula fuga.** El residual se llama `no_capturado` y mezcla cuatro cosas
que todavia no sabemos separar: red publica local, privados locales, gente que
se atiende fuera de la provincia, y gente que no consulta. Separarlas exige DEIS
por comuna de residencia — eso es Capa 2. Llamarlo "fuga" hoy seria inventar.

## Resultado real — corte 2026-09-07

40.483 atenciones · 12.530 pacientes · **3.028 sin comuna (24,2%, era 35,8%)**
1.460 con comuna imputada por telefono compartido.

| Comuna | habitantes | capturados | penetracion | + parciales |
|---|---:|---:|---:|---:|
| Arauco | 37.163 | 2.112 | **5,68%** | 17,01% |
| Curanilahue | 31.750 | 204 | **0,64%** | 2,89% |
| Los Alamos | 21.084 | 36 | 0,17% | 0,84% |
| Lebu | 26.043 | 7 | 0,03% | 0,22% |
| Tirua | 9.664 | 3 | 0,03% | 0,04% |
| Canete | 34.640 | 3 | 0,01% | 0,16% |
| Contulmo | 5.838 | 0 | 0,00% | 0,07% |

**Arauco captura 9x mas que Curanilahue** (5,68% vs 0,64%) — la brecha real es
mucho mayor que la que sugiere la venta ($5,79M vs el resto), porque Curanilahue
tiene casi la misma poblacion que Arauco.

**Por sector (denominador Censo 2024 por localidad, urbano+rural):**

| sector | habitantes | capturados | penetracion |
|---|---:|---:|---:|
| Carampangue | 3.644 | 358 | **9,82%** |
| Horcones | 830 | 40 | 4,82% |
| Laraquete | 5.123 | 154 | 3,01% |
| Pichilo | 737 | 22 | 2,99% |
| Ramadillas | 2.178 | 60 | 2,75% |
| Arauco urbano | 17.615 | 221 | 1,25% |
| **Cerro Alto** | **6.504** | 8 | **0,12%** |
| **Curanilahue urbano** | **28.504** | 18 | **0,06%** |
| Los Alamos urbano | 8.331 | 0 | 0,00% |

### El sesgo que hace invalida la comparacion entre sectores

**Arauco urbano NO tiene 1,35% de penetracion. Ese numero es un PISO.** Un vecino
de Carampangue escribe "Carampangue" en su direccion; uno de la ciudad de Arauco
escribe solo la calle y cae en "sector sin dato". La cobertura de sector lo mide:

| Comuna | capturados con sector | cobertura |
|---|---|---:|
| Arauco | 802 / 1.854 | 43,3% |
| Los Alamos | 1 / 35 | 2,9% |
| Curanilahue | 0 / 190 | 0,0% (excluida a proposito) |

Por eso el reporte imprime esta tabla siempre: sin ella, alguien compara 7,64%
contra 1,35% y concluye al reves.

## El denominador real: Censo 2024 por localidad

El reescalado del Censo 2017 se reemplazo por el dato real, bajado del Feature
Service publico de microdatos del INE (`scripts/censo2024_entidades.py`). Hay que
sumar DOS capas: `Manzanas` (urbano) y `Manzanas-entidades` (rural). La rural sola
da Carampangue = 363 habitantes, que es solo su periferia.

Contra el dato real, el reescalado fallaba justo donde mas importa:

| sector | estimado 2017→2024 | Censo 2024 real | error |
|---|---:|---:|---:|
| Carampangue | 4.688 | **3.644** | +29% inflado |
| Ramadillas | 3.021 | 2.178 | +39% |
| Arauco urbano | 16.379 | 17.615 | −7% |

Carampangue pasa de 7,64% a **9,82%** de penetracion: el denominador inflado la
subestimaba un 22%, en el sector donde se evaluan Monsalve 168 y el edificio
propio. Por eso el reescalado estaba marcado confianza "media" y nunca "alta".

**Nombres repetidos entre comunas.** El INE tiene un Quidico en Arauco y otro en
Tirua, un Huillinco en Canete y otro en Contulmo, un Ponotro en Canete y otro en
Tirua. `POBLACION_SECTOR` se indexa por nombre: aceptar ambos produce un dict
literal con clave repetida, y **Python se queda callado con la ultima**. El
cargador solo acepta la fila cuya comuna coincide con la del diccionario.

### 23 localidades agregadas al diccionario (2026-09-08)

El cargador lista las localidades del INE que `localidades_arauco` no sabia
emitir. Se agregaron las que pasaron una **prueba empirica**: buscar el nombre en
las 16.079 direcciones reales y descartar toda la que ya aparezca en direcciones
de OTRA comuna — porque ahi ese nombre es una CALLE, no un lugar.

Lo que la prueba cazo, y que ningun criterio a ojo habria cazado:

| nombre | INE dice | en direcciones reales |
|---|---|---|
| **Pehuen** | Lebu, 1.220 hab | **35 veces en ARAUCO, cero en Lebu** — es la Villa Pehuen de Arauco |
| Lleu Lleu | Canete, 558 | 6 en Arauco, cero propias |
| Tucapel | Canete, 341 | calle conocida de Arauco (estaba en los keywords del BI viejo) |

Agregar Pehuen habria mudado 35 pacientes de comuna. Quedaron fuera tambien los
homonimos que ya viven en `AMBIGUOS` (Los Rios, Pangue): el INE los ubica en Los
Alamos, pero el diccionario los tiene ambiguos a proposito.

Resultado: **+30 sectores resueltos, y solo 2 pacientes cambiaron de comuna —
ambos desde "sin dato", ninguno movido entre comunas.**

| comuna | cobertura antes | despues |
|---|---:|---:|
| Los Alamos | 78,5% | **95,0%** |
| Arauco | 91,5% | 93,6% |
| Curanilahue | 89,8% | 91,4% |
| Lebu | 77,1% | 85,7% |
| Canete | 62,0% | 78,6% |
| Tirua | 43,7% | 53,0% |
| Contulmo | 73,6% | 73,6% |

## El campo `localidad` del BI estaba 94,5% inventado

`bi.dim_paciente.localidad` NO era una fuente independiente: se derivaba del mismo
`heatmap_cache.db` con la logica vieja de keywords, y terminaba en
`return "Arauco urbano"` como **fallback**.

| lo que el BI llama "Arauco urbano" | 6.196 |
|---|---:|
| que el resolvedor confirma | **342 (5,5%)** |
| que en realidad son comuna Arauco sin sector | 5.754 |
| que **no tienen direccion alguna** | **5.324** |

`README_LOCALIDAD.md` declara ese campo fuente de verdad para todo dashboard
territorial, asi que el sesgo se propago a todos.
`health-bi-project/scripts/sync_localidad_v2.py` lo reemplaza: usa el resolvedor
nuevo, imputa comuna por telefono, **escribe NULL cuando no sabe**, y agrega
`localidad_fuente` para que la procedencia llegue al dashboard.
Resultado: 72,1% con comuna · 12,9% con sector · 27,9% NULL honesto.
**La escritura a Postgres quedo sin cablear a proposito**: los dashboards van a
mostrar de golpe un bucket "sin dato" grande, y esa es una decision del dueno.

## Atacar el punto ciego (35,8% → 24,2%)

Diagnostico primero, porque cambio la estrategia: de las 6.391 fichas sin comuna,
**6.055 (94,7%) no tienen NI direccion NI comuna NI ciudad — solo celular.** Un
diccionario de calles no las rescata. El telefono es la unica senal, y la
imputacion por telefono compartido no es *una* palanca: es la unica.

**Backtest sobre las 16.079 fichas reales:**

| nivel | acierto | n |
|---|---:|---:|
| comuna | **95,5%** | 3.482 |
| sector | 76,8% | 482 |

**Se imputa comuna, NO sector.** Compartir telefono implica compartir hogar, y el
hogar implica comuna casi siempre — pero no sector: una madre en Arauco urbano
inscribe a un hijo que vive en Carampangue. 1 de cada 4 mal asignado es
inaceptable justo en la variable que decide inversiones.

Guardrales: unanimidad (donantes que discrepan no imputan — es numero reciclado o
cuidador de varias familias), nunca pisa un dato propio, y `fuente="telefono"`
viaja hasta el reporte.

**Techo de esta palanca:** recupera ~1 de cada 3 (1.460 de 4.488). El resto tiene
un telefono que nadie mas comparte, o cuyos donantes tampoco tienen domicilio.

## Un bug de resolucion que estaba oculto en `localidades_arauco`

`_VIA` metia `VILLA` y `POBLACION` en la misma bolsa que `CALLE` y `PASAJE`, pero
una villa **es** un nombre de lugar. El efecto era absurdo: que una villa
resolviera dependia de si su clave del diccionario incluia por casualidad la
palabra "Villa". `Villa El Bosque` resolvia (clave VILLA EL BOSQUE) y
`Villa Los Sauces` no (clave LOS SAUCES).

El fix condiciona el bloqueo al tipo de lo que matcheo, y agrega una regla nueva:
**una localidad le gana a una villa aunque la villa venga despues en el texto.**
Los nombres de villa se repiten entre pueblos — "Villa El Bosque" existe en
Laraquete Y en Llico, y tres pacientes de Llico se estaban mudando a Laraquete.
Resultado: +48 sectores resueltos, 0 regresiones.

## Los dos bugs que cazaron los guardrails

**1. Colision de nombres entre niveles.** "Arauco" es comuna (37.163), ciudad
(15.980) y provincia. Con un solo diccionario y precedencia implicita, la consulta
por la COMUNA devolvia la poblacion de la CIUDAD: penetracion 11,32% en vez de
4,99%, **mas del doble**. No explotaba — devolvia un numero plausible que habria
sobrevivido a cualquier revision que no sumara las filas. Fix: `poblacion_de()`
exige el `nivel` como argumento, nunca lo deduce del nombre.

**2. Claves huerfanas.** Cuatro distritos (Los Nancos, Lavapie, Caripilun, Pangue)
no tienen sector correspondiente en `localidades_arauco`, asi que su fila habria
dicho "0%" para siempre — que se lee como "ahi no hay nadie" cuando en realidad
era "no lo estamos mirando". Hay un test que falla si alguien agrega otra.

## Los dos guardrails, y por que

1. **Parametro sin fuente = excepcion, no warning.** `TASA_CONSULTAS_ANUALES`
   (consultas per capita/ano) mueve mas el resultado que cualquier otro numero:
   al doble de tasa la penetracion cae a la mitad y la conclusion se da vuelta.
   Hoy no tiene fuente citable, asi que el motor **se niega a correr**. Es lo
   contrario del "92% de asertividad" de XBREIN: un numero sin definicion que se
   propaga hasta parecer un hecho.
2. **Denominador ausente no se estima.** `POBLACION_SECTOR` esta vacio a
   proposito. Repartir la poblacion comunal a prorrata le daria a Carampangue un
   denominador falso justo donde se deciden inversiones (Monsalve 168, edificio
   propio). Un sector sin dato sale del calculo y se reporta aparte.

## Los 2 datos que faltan (ambos publicos y gratis)

| Dato | Donde | Bloquea |
|---|---|---|
| Consultas ambulatorias per capita/ano, Biobio | DEIS/MINSAL REM A04 x proyeccion INE | todo el calculo de potencial |
| Poblacion por entidad poblada (Carampangue, Laraquete, Colico...) | INE Censo 2024, "entidades pobladas" | penetracion a nivel SECTOR (a nivel comuna ya funciona) |

Ojo: el INE suprime entidades muy chicas por confidencialidad. Varios sectores
rurales van a quedar sin dato **para siempre**, y deben quedar en `None`.

## La regla de captura (decidida 2026-09-07)

    capturado    >=2 atenciones en 24 meses, separadas >=90 dias
    parcial      alguna atencion en la ventana, sin relacion sostenida
    no_capturado ninguna atencion en la ventana

**24 meses** porque el consumo de salud es episodico: a 12 meses la ventana echa a
un adulto sano con un hueco legitimo de 14 meses; a 36 nunca detecta que un
territorio se esta perdiendo.

**90 dias de separacion** es la condicion que mas mueve los numeros. Sin ella, una
serie de kinesiologia (10 sesiones + 2 reevaluaciones en ~8 semanas) contaria como
paciente capturado. Eso es UN episodio clinico, no una relacion — y el error no
seria parejo: inflaria la penetracion justo en los sectores donde kine es fuerte,
que son los que se estan evaluando para invertir.

Las dos columnas (`capturados` y `+parciales`) acotan por arriba y por abajo, asi
que la eleccion de vara no decide sola la conclusion del plan 2030: se ve el rango.

## Que sigue

1. **Bajar el 24,2% que queda sin comuna.** El telefono ya dio lo que tenia. Las
   dos fuentes que faltan: el campo `localidad` del Postgres BI, y lo que el bot
   captura en conversacion (`sessions.db`) desde el fix de localidad opcional.
2. **Subir la cobertura de sector**, que es el cuello que queda: solo el 41,3% de
   los capturados de Arauco y el 8,8% de los de Curanilahue tienen sector. No es
   falta de diccionario — es que la ficha no trae direccion.
3. **Capa 2: DEIS por comuna de residencia** — recien ahi el residual se puede
   separar en red publica / privados / fuga real / no consulta.
