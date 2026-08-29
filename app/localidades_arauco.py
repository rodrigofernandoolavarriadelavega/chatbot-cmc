# -*- coding: utf-8 -*-
"""Diccionario de localidades de la Provincia de Arauco + resolvedor de comuna/sector.

POR QUE EXISTE
--------------
El campo `comuna` de la ficha de Medilink **cae por defecto en "ARAUCO"**. El dueño
lo cazo el 2026-08-29 atendiendo a una paciente cuya direccion era de Curanilahue y
cuya ficha decia comuna Arauco. Eso significa que Curanilahue (y toda comuna que no
sea Arauco) esta **subcontada por diseno**, no por ruido.

REGLA DURA: **la DIRECCION manda sobre el campo comuna.** El campo solo se usa
cuando la direccion no nombra ninguna localidad conocida.

Ademas el negocio necesita SECTOR, no solo comuna: para el CMC no es lo mismo un
paciente de Laraquete que uno de Arauco urbano (decision del dueno). Por eso cada
entrada trae comuna Y sector.

CONFIANZA
---------
"alta"  fuente publica (Wikipedia de la comuna, prensa, wikimapia) o >=95% de
        acuerdo en los datos propios con n>=15 fichas.
"media" una sola fuente, o 85-95% de acuerdo en los datos.
"baja"  nombre generico, ambiguo entre comunas, o la fuente publica CONTRADICE
        a los datos propios. **Nunca reasignar comuna con confianza baja.**

Fuentes: Wikipedia (comunas de Arauco, Los Alamos, Curanilahue), Censo 2024 INE,
wikimapia, prensa local sobre inundaciones de Curanilahue, y votacion sobre las
15.988 fichas de `pacientes_heatmap` (ver `herramientas/localidades_auditar.py`).
"""
from __future__ import annotations
import re
import unicodedata

__all__ = ["resolver", "normalizar", "LOCALIDADES", "COMUNAS", "POBLACION"]

# ── poblacion Censo 2024 (INE) ──────────────────────────────────────────────
POBLACION = {
    "Arauco": 37163, "Canete": 34640, "Curanilahue": 31750,
    "Lebu": 26043, "Los Alamos": 21084, "Contulmo": 5838, "Tirua": 9664,
}
COMUNAS = tuple(POBLACION)

# ── el diccionario: NOMBRE -> (comuna, sector, tipo, confianza) ─────────────
# `sector` es como queremos verlo en los reportes; `tipo` distingue localidad
# rural de poblacion/villa urbana, porque una villa no desambigua la comuna sola.
_D: dict[str, tuple[str, str, str, str]] = {}

def _add(nombres, comuna, sector, tipo, conf):
    for n in (nombres if isinstance(nombres, (list, tuple)) else [nombres]):
        _D[normalizar(n)] = (comuna, sector, tipo, conf)

def normalizar(s) -> str:
    if not s:
        return ""
    s = unicodedata.normalize("NFKD", str(s)).encode("ascii", "ignore").decode()
    return " ".join(s.upper().split())

# ═══════════════════════ COMUNA DE ARAUCO ═══════════════════════════════════
_add(["Arauco"], "Arauco", "Arauco urbano", "ciudad", "alta")
_add(["Carampangue"], "Arauco", "Carampangue", "pueblo", "alta")
_add(["Laraquete"], "Arauco", "Laraquete", "pueblo", "alta")
_add(["Ramadillas", "Ramadilla"], "Arauco", "Ramadillas", "pueblo", "alta")
_add(["Tubul"], "Arauco", "Tubul", "caleta", "alta")
_add(["Llico"], "Arauco", "Llico", "caleta", "alta")
_add(["Punta Lavapie", "Lavapie"], "Arauco", "Punta Lavapié", "caleta", "alta")
_add(["Rumena"], "Arauco", "Rumena", "localidad", "media")
_add(["Trana"], "Arauco", "Trana", "localidad", "media")
_add(["Ponotro"], "Arauco", "Ponotro", "localidad", "media")
_add(["Quiapo"], "Arauco", "Quiapo", "localidad", "media")
_add(["La Isla"], "Arauco", "La Isla", "localidad", "media")
# rurales confirmados por los datos propios (n>=15, 100% Arauco)
_add(["Conumo", "Conumo Alto"], "Arauco", "Conumo", "localidad", "alta")
_add(["Horcones"], "Arauco", "Horcones", "localidad", "alta")
_add(["Pichilo"], "Arauco", "Pichilo", "localidad", "alta")
_add(["El Parron", "Parron"], "Arauco", "El Parrón", "localidad", "alta")
_add(["Los Maitenes", "Maitenes"], "Arauco", "Los Maitenes", "localidad", "alta")
_add(["Maquehua"], "Arauco", "Maquehua", "localidad", "alta")
_add(["Punta Carampangue"], "Arauco", "Carampangue", "localidad", "alta")
_add(["Cruce Norte"], "Arauco", "Carampangue", "localidad", "media")
# poblaciones y villas urbanas (NO desambiguan solas: tipo="villa")
for _n, _sec in [
    ("Villa Don Carlos", "Arauco urbano"), ("Volcan Antuco", "Arauco urbano"),
    ("Volcan Llaima", "Arauco urbano"), ("Volcan Yates", "Arauco urbano"),
    ("El Mirador", "Arauco urbano"), ("La Meseta", "Arauco urbano"),
    ("Villa Altamira", "Arauco urbano"), ("Bosques de Montemar", "Arauco urbano"),
    ("Villa Pehuen", "Arauco urbano"), ("Patria", "Arauco urbano"),
    ("Villa El Bosque", "Laraquete"), ("El Pinar", "Laraquete"),
    ("Villa Vista Hermosa", "Laraquete"), ("Las Araucarias", "Laraquete"),
    ("Villa Radiata", "Laraquete"), ("Los Laureles", "Laraquete"),
    ("El Boldo", "Laraquete"),
    ("Villa Esperanza", "Carampangue"), ("Villa Amanecer", "Carampangue"),
    ("Los Sauces", "Carampangue"), ("Monsalve", "Carampangue"),
    ("Union Carampanguina", "Carampangue"),
]:
    _add([_n], "Arauco", _sec, "villa", "media")

# ═══════════════════════ COMUNA DE CURANILAHUE ══════════════════════════════
_add(["Curanilahue", "Curanilhue", "Curanilague"], "Curanilahue", "Curanilahue urbano", "ciudad", "alta")
_add(["Plegarias", "Plegaria"], "Curanilahue", "Plegarias", "localidad", "alta")
# Colico: los datos propios lo dan 91% Curanilahue (n=53). Correccion sobre mi
# suposicion inicial de que era "San Jose de Colico" de Arauco.
_add(["Colico", "Colico Norte", "Colico Sur", "San Jose de Colico", "San Jose Colico"],
     "Curanilahue", "Colico", "localidad", "media")
_add(["Eleuterio Ramirez"], "Curanilahue", "Curanilahue urbano", "villa", "alta")
_add(["Las Hortalizas", "Hortalizas"], "Curanilahue", "Curanilahue urbano", "villa", "media")
_add(["Los Amarillos"], "Curanilahue", "Curanilahue urbano", "villa", "media")
_add(["Carcoop"], "Curanilahue", "Curanilahue urbano", "villa", "media")
_add(["Miraflores"], "Curanilahue", "Curanilahue urbano", "villa", "baja")
_add(["Rio Trongol"], "Curanilahue", "Curanilahue urbano", "villa", "media")
_add(["Boca de Trongol"], "Curanilahue", "Trongol", "localidad", "media")

# ═══════════════════════ COMUNA DE LOS ALAMOS ═══════════════════════════════
_add(["Los Alamos"], "Los Alamos", "Los Álamos urbano", "ciudad", "alta")
_add(["Antihuala"], "Los Alamos", "Antihuala", "pueblo", "alta")
_add(["Cerro Alto"], "Los Alamos", "Cerro Alto", "pueblo", "alta")
_add(["Tres Pinos"], "Los Alamos", "Tres Pinos", "pueblo", "alta")
_add(["Pilpilco"], "Los Alamos", "Pilpilco", "localidad", "alta")
_add(["La Araucana"], "Los Alamos", "La Araucana", "localidad", "media")
_add(["Sara de Lebu"], "Los Alamos", "Sara de Lebu", "localidad", "media")
_add(["Temuco Chico"], "Los Alamos", "Temuco Chico", "localidad", "media")
_add(["La Virgen"], "Los Alamos", "La Virgen", "localidad", "media")
_add(["Agua de los Gansos"], "Los Alamos", "Agua de los Gansos", "localidad", "media")
_add(["Quillaitun"], "Los Alamos", "Quillaitún", "localidad", "media")
_add(["La Aguada"], "Los Alamos", "La Aguada", "localidad", "media")
_add(["Ranquilco"], "Los Alamos", "Ranquilco", "localidad", "media")
_add(["Pichillanquehue"], "Los Alamos", "Pichillanquehue", "localidad", "media")
_add(["Toco Toco"], "Los Alamos", "Toco Toco", "localidad", "media")

# ═══════════════════════ RESTO DE LA PROVINCIA ══════════════════════════════
_add(["Lebu"], "Lebu", "Lebu urbano", "ciudad", "alta")
_add(["Millaneco"], "Lebu", "Millaneco", "localidad", "media")
_add(["Boca Lebu"], "Lebu", "Boca Lebu", "localidad", "media")
_add(["Canete"], "Canete", "Cañete urbano", "ciudad", "alta")
_add(["Cayucupil"], "Canete", "Cayucupil", "localidad", "alta")
_add(["Peleco"], "Canete", "Peleco", "localidad", "media")
_add(["Antiquina"], "Canete", "Antiquina", "localidad", "media")
_add(["Pocuno"], "Canete", "Pocuno", "localidad", "media")
_add(["Huillinco"], "Canete", "Huillinco", "localidad", "baja")   # tambien en Contulmo
_add(["Contulmo"], "Contulmo", "Contulmo urbano", "ciudad", "alta")
_add(["Elicura"], "Contulmo", "Elicura", "localidad", "media")
_add(["Calebu"], "Contulmo", "Calebu", "localidad", "media")
_add(["Tirua"], "Tirua", "Tirúa urbano", "ciudad", "alta")
_add(["Quidico"], "Tirua", "Quidico", "caleta", "alta")
_add(["Tranaquepe"], "Tirua", "Tranaquepe", "localidad", "media")
_add(["Casa de Piedra"], "Tirua", "Casa de Piedra", "localidad", "media")

# fuera de la provincia, para no contarlos como locales
for _n, _c in [("Lota", "Lota"), ("Coronel", "Coronel"), ("Concepcion", "Concepción"),
               ("San Pedro de la Paz", "San Pedro de la Paz"), ("Talcahuano", "Talcahuano"),
               ("Chiguayante", "Chiguayante"), ("Hualpen", "Hualpén"), ("Santiago", "Santiago"),
               ("Temuco", "Temuco"), ("Nacimiento", "Nacimiento"), ("Los Angeles", "Los Ángeles")]:
    _add([_n], "Fuera de la provincia", _c, "ciudad", "alta")

# ── AMBIGUOS: aparecen en mas de una comuna. NUNCA reasignan por si solos. ──
# El resolvedor los usa solo para confirmar lo que ya dice el campo.
AMBIGUOS: dict[str, tuple[str, ...]] = {
    "TRAUCO": ("Arauco", "Los Alamos"),
    "TRONGOL": ("Curanilahue", "Los Alamos"),
    "TRONGOL ALTO": ("Curanilahue", "Los Alamos"),
    "TRONGOL BAJO": ("Curanilahue", "Los Alamos"),
    "PANGUE": ("Los Alamos", "Arauco"),
    "LOS RIOS": ("Los Alamos", "Arauco"),
    # 🔴 CONFLICTO REAL: las fuentes publicas (wikimapia, prensa de inundaciones)
    # dicen que Chillancito es sector de Curanilahue, pero el 65% de las fichas
    # que lo escriben estan marcadas Arauco y varias dicen "CHILLANCITO S/N
    # CARAMPANGUE". O hay dos, o el campo esta mal en masa. PREGUNTAR AL DUENO.
    "CHILLANCITO": ("Curanilahue", "Arauco"),
    # nombres de arbol/planta: son calle en media provincia, no localidad
    "LOS CASTANOS": ("Arauco", "Curanilahue"),
    "LOS BOLDOS": ("Arauco", "Curanilahue"),
    "EL BOLDO": ("Arauco", "Curanilahue"),
    "LOS ALAMOS": ("Los Alamos", "Arauco"),
    "CERRO VERDE": ("Los Alamos", "Arauco"),
    "VILLARRICA": ("Curanilahue", "Arauco"),
    "SANTA MARIA": ("Curanilahue", "Arauco"),
    "EDUARDO FREI": ("Curanilahue", "Arauco"),
    "PEDRO AGUIRRE CERDA": ("Curanilahue", "Arauco"),
    "RICARDO LAGOS": ("Curanilahue", "Arauco"),
    "EL SAUCE": ("Curanilahue", "Arauco"),
    "NAVIDAD": ("Curanilahue", "Arauco"),
    "EL DOS": ("Curanilahue", "Arauco"),
}

LOCALIDADES = _D

# nombres largos primero: "PUNTA CARAMPANGUE" debe ganarle a "CARAMPANGUE",
# y "TRONGOL BAJO" a "TRONGOL".
_ORDEN = sorted(set(list(_D) + list(AMBIGUOS)), key=lambda k: -len(k))
_PATRONES = [(k, re.compile(r"(?<![A-Z])" + re.escape(k).replace(r"\ ", r"\s+") + r"(?![A-Z])"))
             for k in _ORDEN]


# Palabras que convierten al nombre siguiente en CALLEJERO, no geografico.
# "Calle Los Alamos 53, Laraquete" NO es Los Alamos: es una calle en Laraquete.
# "Sector"/"Camino" NO entran: esos si suelen anteceder a una localidad real.
_VIA = re.compile(r"\b(CALLE|CALLEJON|PASAJE|PSJE|PJE|AVENIDA|AVDA|AV|POBLACION|"
                  r"POBL|VILLA|DIAGONAL|SUBIDA|SUBID)\s+(?:LOS\s+|LAS\s+|EL\s+|LA\s+)?$")


def _candidatos(d: str):
    """Todas las localidades nombradas en la direccion, con su posicion.
    Descarta las que vienen detras de una palabra de via (son calles)."""
    out = []
    for clave, pat in _PATRONES:
        for m in pat.finditer(d):
            if _VIA.search(d[:m.start()]):
                continue                      # "calle Los Alamos" -> no cuenta
            out.append((m.start(), clave))
    return out


def resolver(direccion: str | None, comuna_campo: str | None = None,
             ciudad_campo: str | None = None) -> dict:
    """Devuelve {comuna, sector, fuente, confianza, conflicto}.

    La DIRECCION manda. El campo solo entra si la direccion no dice nada, o para
    desempatar un nombre ambiguo. `conflicto=True` marca los casos en que la
    direccion contradice al campo: son los pacientes mal ubicados.
    """
    campo = normalizar(comuna_campo) or normalizar(ciudad_campo)
    campo_com = None
    if campo and campo not in ("0", "-", "SIN DATO", "S/I", "NULL"):
        hit = _D.get(campo)
        campo_com = hit[0] if hit else ("Arauco" if campo.startswith("ARAU") else None)

    d = normalizar(direccion)
    if d:
        # En una direccion chilena la LOCALIDAD va al final ("... 8 Laraquete").
        # Tomar el primer nombre que calce hacia Los Alamos cuando el domicilio
        # termina en Laraquete fue un falso positivo real del barrido batch.
        cands = _candidatos(d)
        cands.sort(key=lambda t: -t[0])          # el ultimo nombrado manda
        for _pos, clave in cands:
            if clave in AMBIGUOS:
                # solo confirma; si el campo no es candidato, no inventamos
                cands = AMBIGUOS[clave]
                if campo_com in cands:
                    return {"comuna": campo_com, "sector": clave.title(),
                            "fuente": "direccion+campo", "confianza": "baja", "conflicto": False}
                continue
            com, sec, tipo, conf = _D[clave]
            # una villa urbana no basta para mover a otra comuna con poca confianza
            if tipo == "villa" and campo_com and campo_com != com and conf != "alta":
                continue
            return {"comuna": com, "sector": sec, "fuente": "direccion",
                    "confianza": conf,
                    "conflicto": bool(campo_com and campo_com != com)}

    if campo_com:
        # El campo solo trae el NOMBRE DE LA COMUNA: no sabemos el sector. Decir
        # "Arauco urbano" aqui seria inventar, y encima infla el urbano a costa de
        # Carampangue y Laraquete, que es justo la distincion que el negocio quiere.
        hit = _D.get(campo)
        sector = hit[1] if (hit and hit[2] != "ciudad") else campo_com + " · sector sin dato"
        return {"comuna": campo_com, "sector": sector,
                "fuente": "campo", "confianza": "media", "conflicto": False}
    return {"comuna": None, "sector": None, "fuente": None,
            "confianza": None, "conflicto": False}
