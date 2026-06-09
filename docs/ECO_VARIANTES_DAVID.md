# Catálogo de ecografías de David Pardo (CMC)

> **David Pardo** — Tecnólogo Médico · Ecografía · id Medilink **68** · **$40.000**
> particular, sin Fonasa · WhatsApp +56 9 9220 1931 · recibo "David Pardo M.
> Servicio Ecotomografía".
>
> Fuente de verdad del routing: `app/ecografias.py` (`ECOGRAFIA_ROUTING`).
> Lo que **NO** es de David:
> - transvaginal / pélvica / ginecológica / obstétrica → **Dr. Tirso Rejón** (id 61, $35.000)
> - ecocardiograma / corazón → **Dr. Miguel Millán** (id 60, $110.000, lista de espera)
>
> Reconstruido el 2026-06-09 (los docs originales del portavión se perdieron del
> working tree antes de commitearse).

## Variantes oficiales (desde el código — `ecografia_general_pardo`)

Todas estas resuelven a David Pardo (id 68). Se normalizan sin tildes/minúscula
antes de comparar, así que no hace falta duplicar con/sin tilde.

- **Mamaria / mamas** (partes blandas, NO ginecológica): mamaria, eco mamaria,
  eco de mamas, eco mamas, ecografía mamaria, ecografía de mamas, ecotomografía
  mamaria, eco de mama, ecografía de mama
- **Abdominal:** abdominal, eco abdominal, ecografía abdominal, abdomen, abdomen completo
- **Renal:** renal, eco renal, ecografía renal
- **Vesical / vejiga:** vesical, vejiga, eco vesical, ecografía vesical
- **Hepática / hígado / vesícula:** hepática, ecografía hepática, hígado, eco
  hígado, ecografía hígado, vesícula, eco vesícula, ecografía vesícula
- **Tiroides:** tiroides, tiroidea, eco tiroides, ecografía tiroides, ecografía tiroidea
- **Partes blandas / superficial:** partes blandas, eco partes blandas, ecografía
  partes blandas, superficial, eco superficial
- **Testicular:** testicular, testicul, texticul, eco testicular, ecografía
  testicular, inguinal escrotal, inguino escrotal
- **Cuello:** eco cuello, ecografía de cuello
- **Próstata:** próstata, eco próstata, ecografía próstata
- **Musculoesquelética:** musculoesquelética, músculo esquelética, eco músculo,
  musculoesquelético, músculo esquelético
- **Articulaciones miembro superior:** hombro, brazo, codo, muñeca, mano, dedo
- **Articulaciones miembro inferior:** cadera, rodilla, tobillo, pie
- **Articulación genérico:** articulación
- **Doppler (no cardíaco):** doppler, eco doppler, ecografía doppler
- **Inguinal:** inguinal, eco inguinal, ecografía inguinal

## Términos reales de pacientes (del portavión de auditoría)

> Consolidado por el workflow `portavion-eco-friccion` sobre **917 conversaciones
> reales de producción** (corte de alta fricción: 360 auditadas por 18 agentes).
> Estos son los términos que **escriben los pacientes** para pedir una eco que hace
> David — útiles para ampliar el matcher de `ecografias.py` y matar el menu-loop.

**Raíz / alias** (todos deben normalizar a "ecografía" ANTES del ruteo):
`ecografia`, `ecografias`, `eco`, `eco grafia` / `eco grafias` (con espacio),
`ecotomografia`, `eco tomografia`, `ecotografia` (typo), `ecotomagrafia` (typo)

**Abdomen / vísceras:** abdominal, abdominales, eco abdominal, ecotomografia abdominal,
eco grafia abdominal, abdominal renal, renal, reno vesical / renovesical / renovecical (typos),
vesical, vejiga, hepatica, higado, vesicula

**Mamaria** (de David, NO Rejón): mamaria, mamas, eco mamaria, ecografia mamaria,
ecografia mamaria bilateral, ecografia de mama, eco grafia mamaria, **ecomamaria (pegado)**,
ecotomografia mamaria

**Tiroides / cuello:** tiroidea, ecografia tiroidea, cuello, cervical (eco de cuello / partes blandas)

**Partes blandas / pared:** partes blandas, superficial, ecografia/eco partes blandas,
ecotomografia de partes blandas, pared abdominal, gluteos, gluteos bilateral,
omoplato / omoplatos (typo), escapula

**Urogenital masculino:** testicular, ecotomografia testicular, inguino-escrotal,
inguinal escrotal, inguinal, prostata, ecografia pelvica masculina (próstata/vejiga)

**Musculoesquelética / articulaciones:** musculoesqueletica, hombro, ecotomografia de hombro y brazo,
brazo, codo, muñeca (ecografia muñeca mano derecha), mano, dedos, cadera (eco de cadera derecha),
rodilla, tobillo (ecotomografia de tobillo), pie, **muslo** (eco tomografia de muslo),
**pierna**, **pantorrilla**, **lumbar** (ecografia lumbar), articulacion

**Doppler:** doppler (no cardíaco)

### ⚠️  Términos que NO son de David (los confunden los pacientes)
- `transvaginal` / `intravaginal` / `intravajinal` / `endovaginal` / pélvica ginecológica → **Rejón** ($35.000)
- `ecocardiograma` / corazón / doppler cardíaco → **Millán** ($110.000, lista de espera)
- eco de embarazo / obstétrica / morfológica / 11-14 semanas / sexo del bebé / doppler fetal → **CMC NO la hace** (solo confirmación temprana ~7-8 sem con Rejón)
- `ecg` = electrocardiograma, NO ecografía
- "la tomo" NO debe matchear "tomografía"

## Gaps de cobertura léxica detectados (pendiente de agregar a `ecografias.py`)

Estos términos los escriben pacientes pero **no resuelven** hoy en `ECOGRAFIA_ROUTING`
(caen a "preguntar tipo"): **muslo, pierna, pantorrilla, lumbar, omóplato/escápula,
glúteos, pared abdominal**, y el alias **ecotomografía/ecotomagrafía** como raíz.
Agregar en `ecografia_general_pardo["keywords"]` (van todos a David, id 68).
