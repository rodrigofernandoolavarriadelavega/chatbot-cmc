# Variantes de ecografía de David Pardo (id 68)

> Catálogo autoritativo de TODAS las ecografías que realiza **David Pardo**
> (Tecnólogo Médico · Ecografía · id Medilink **68** · **$40.000** particular,
> sin Fonasa · WhatsApp +56 9 9220 1931 · recibo "David Pardo M. Servicio Ecotomografía").
>
> Fuente de verdad del routing: `app/ecografias.py` (`ECOGRAFIA_ROUTING`).
> Lo que **NO** es de David: transvaginal/pélvica/ginecológica/obstétrica → **Dr. Tirso Rejón** (id 61, $35.000);
> ecocardiograma/corazón → **Dr. Miguel Millán** (id 60, $110.000, lista de espera).
>
> Sección "términos reales de pacientes" la completa el portavión de auditoría
> (`scripts/conversation_audit_swarm.py` + workflow `portavion-eco-friccion`).

## Categorías que hace David

| Categoría | Variantes en código |
|-----------|---------------------|
| **Mamaria** (partes blandas, NO ginecología) | mamaria, eco mamaria, eco de mamas, eco mamas, ecografia mamaria, ecografia de mamas, ecotomografia mamaria, eco de mama, ecografia de mama |
| **Abdominal** | abdominal, eco abdominal, ecografia abdominal, abdomen, abdomen completo |
| **Renal** | renal, eco renal, ecografia renal |
| **Vesical / vejiga** | vesical, vejiga, eco vesical, ecografia vesical |
| **Hepática / hígado / vesícula** | hepatica, ecografia hepatica, higado, eco higado, ecografia higado, vesicula, eco vesicula, ecografia vesicula |
| **Tiroidea** | tiroides, tiroidea, eco tiroides, ecografia tiroides, ecografia tiroidea |
| **Partes blandas / superficial** | partes blandas, eco partes blandas, ecografia partes blandas, superficial, eco superficial |
| **Testicular / inguino-escrotal** | testicular, testicul, texticul (typo), eco testicular, ecografia testicular, inguinal escrotal, inguino escrotal |
| **Cuello** | eco cuello, ecografia de cuello |
| **Próstata** | prostata, eco prostata, ecografia prostata |
| **Musculoesquelética** | musculoesqueletica, musculo esqueletica, eco musculo, musculoesqueletico, musculo esqueletico |
| → miembro superior | hombro, brazo, codo, muñeca/muneca, mano, dedo (+ "de"/"eco"/"ecografia" delante) |
| → miembro inferior | cadera, rodilla, tobillo, pie (+ "de"/"eco"/"ecografia" delante) |
| → articulación genérica | articulacion, articulación, eco articulacion, de articulacion |
| **Doppler** (NO cardíaco) | doppler, eco doppler, ecografia doppler |
| **Inguinal** | inguinal, eco inguinal, ecografia inguinal |

## Reglas de oro (anti-regresión)

- **Mamaria es de David Pardo**, no de Rejón. La mama es partes blandas, no órgano reproductivo. (fix 2026-05-25, commits 676239f + 4906130)
- **Transvaginal / pélvica / ovarios / útero → Rejón** (ginecología). Nunca David.
- **Ecocardiograma / corazón / doppler cardíaco → Millán** (cardiología, lista de espera). Nunca David.
- **Obstétrica / embarazo / prenatal**: CMC NO la hace. Solo consulta ginecológica con Rejón.
- Una parte del cuerpo SIN raíz ecográfica real ("eco/ecografia/ecotomografia/doppler/ultrasonido") NO debe rutear a eco (gate anti "eco-bleed", `_tiene_contexto_eco`).

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

### ⚠️ Términos que NO son de David (los confunden los pacientes)
- `transvaginal` / `intravaginal` / `intravajinal` / `endovaginal` / pélvica ginecológica → **Rejón** ($35.000)
- `ecocardiograma` / corazón / doppler cardíaco → **Millán** ($110.000, lista de espera)
- eco de embarazo / obstétrica / morfológica / 11-14 semanas / sexo del bebé / doppler fetal → **CMC NO la hace** (solo confirmación temprana ~7-8 sem con Rejón)
- `ecg` = electrocardiograma, NO ecografía
- "la tomo" NO debe matchear "tomografía"
