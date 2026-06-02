# Auditoría de fricción — Ecografía (portavión de agentes)

> Generado por el workflow `portavion-eco-friccion` (18 agentes `cmc-conversation-auditor`
> + síntesis) sobre conversaciones reales de producción del chatbot CMC.
> Universo: **917 conversaciones** que tocan ecografía (de 48.652 mensajes / 3.035 pacientes,
> rango 30-mar a 02-jun 2026). Revisadas a fondo las **360 de mayor fricción**;
> **160** con fricción real (severidad ≥2), 24 severas (sev 4-5).

## Veredicto

La ecografía es la **mayor fuente de fricción** del bot y el patrón dominante **NO es de
catálogo, sino de máquina de estados**: el bot reconoce el tipo de eco en el primer mensaje,
pero al pulsar `agendar_sugerido` vuelve a preguntar el tipo y luego no parsea la respuesta
libre ("Abdominal") como selección válida → **loop de menú** que termina en takeover o abandono.

## Patrones rankeados (frecuencia × impacto)

| # | Patrón | Frecuencia | Impacto |
|---|--------|-----------|---------|
| 1 | **Menu-loop en `agendar_sugerido`**: el slot `tipo_eco` no persiste; re-pregunta y no parsea respuesta libre | 49 menu_loop (tails 4580/6558/4004/3098/1147) | **Crítico** — convierte intención de alta calidad en cita perdida / trabajo manual |
| 2 | **Cobertura léxica incompleta**: zonas ME (muslo, pierna, mano, dedos, lumbar, omóplato, pie, tobillo) y alias `ecotomografia` no resuelven | 53 intent_mal_clasificado | **Alto** — el paciente sabe qué quiere, el bot lo obliga a repetir |
| 3 | **Ruteo profesional cruzado**: token corto 'eco'/'cervical'/'piel' → ginecología/kine; mamaria→Rejón en casos límite; transvaginal→Pardo | 11 mamaria→gineco + 25 prof. equivocado + 15 eco-bleed residual | **Crítico** — precio y profesional errados, anulaciones |
| 4 | **FAQ precio/Fonasa no contextual**: responde tarjeta de dirección o Fonasa genérico en vez de "eco = solo particular" | 32 precio_confuso + 13 fonasa + 25 no_respondida | **Alto** — abandono post-precio (14) |
| 5 | **Obstétrica sin respuesta proactiva**: eco de embarazo/semanas/sexo del bebé se rutea sin avisar que CMC no la hace | 22 obstétrica + 27 derivación innecesaria | Medio-alto — falsas expectativas; guarda "menor de edad" mal disparada por "bebé" |
| 6 | **Intent resultado/informe confundido con agendamiento**, incluso bajo HUMAN_TAKEOVER | tails 1708, 6774, 0063, 0090 | Medio — interrumpe a recepción |
| 7 | **Fuga de datos**: teléfono personal +56987834148 y código (41) en templates antiguos de takeover | tail 7250 (×7), 3828, 8998 | **Crítico de privacidad/marca** — el guard (44) no cubre estos templates |

## Fixes priorizados

### Alta
- **[logic_review · flows.py]** Persistir `tipo_eco` entre consulta inicial y `agendar_sugerido`: si el tipo ya se resolvió, ir directo a slots de David sin re-preguntar. Si re-pregunta, aceptar respuesta libre ("Abdominal"/"Abdominales") como selección, no devolver el menú. Revisar handler `WAIT_ECO_TYPE` / transición `agendar_sugerido`.
- **[data_safe · ecografias.py]** Ampliar matcher de David con zonas faltantes: muslo, pierna, pantorrilla, lumbar, omóplato, escápula, mano, dedos, glúteos, pared abdominal, reno-vesical, pélvica masculina, cervical. Cada una resuelve a David sin re-preguntar.
- **[data_safe · ecografias.py]** Normalizar alias `ecotomografia/ecotografia/ecotomagrafia/eco tomografia/eco grafia` → `ecografia` antes del ruteo.
- **[logic_review · ecografias.py]** Eliminar residuo eco-bleed del token 'eco': 'eco' a secas → eco general (David) o pedir tipo, NUNCA Rejón por defecto. Verificar orden de evaluación de keys (rejón antes que pardo) y contaminación de contexto de sesión.
- **[logic_review · ecografias.py]** transvaginal/intravaginal/intravajinal/endovaginal → Rejón ($35.000); mamaria/ecomamaria (pegado) → David ($40.000). Verificar que el fix 676239f+4906130 cubre 'ecomamaria' y 'ecografia mamaria bilateral'.
- **[data_safe · flows.py]** Purgar +56987834148 y (41) de TODOS los templates (takeover/urgencia); extender el guard para que cubra cualquier saliente, no solo los parcheados.

### Media
- **[logic_review · flows.py]** Respuesta Fonasa/precio **contextual** para eco (incl. `WAIT_META_SLOT_CHOICE`/`WAIT_SLOT`): "eco es solo particular — David $40.000 / Rejón transvaginal $35.000 / ecocardiograma Millán $110.000".
- **[data_safe · ecografias.py]** Respuesta proactiva obstétrica no disponible ANTES de mostrar slots; sacar 'obstetrica' de la tarjeta de tipos de Rejón.
- **[logic_review · flows.py]** Guarda "menor de edad" no debe dispararse por "bebé/sexo del bebé" en contexto de embarazo materno.
- **[logic_review · flows.py]** Detectar intent resultado/informe ("resultado","informe","me hice la eco","aún nada de la eco") → consulta/takeover, NO agendamiento; bajo HUMAN_TAKEOVER no interrumpir ni disparar alerta SAMU/131.
- **[logic_review · claude_helper.py]** Priorizar especialidades no disponibles antes de partes blandas: 'dermatólogo/piel' → CESFAM, no eco.
- **[data_safe · ecografias.py]** Corregir el string informativo de David que le atribuye transvaginal/pélvica/obstétrica (son de Rejón). Separar ecocardiograma ($110.000, Millán). Aclarar disponibilidad real de doppler.

### Baja
- **[data_safe · ecografias.py]** "la tomo" a lista negra (no matchear "tomografía"); 'ecg' ≠ ecografía; inscripción en waitlist con nombre canónico, no el texto literal de la query.

## Cómo re-correr el portavión

```bash
cd ~/chatbot-cmc
# 1) refrescar conversaciones desde prod (VPS, SQLCipher):
sshpass -p '***' scp scripts/_extract_eco_convs.py root@157.245.13.107:/opt/chatbot-cmc/scripts/
sshpass -p '***' ssh root@157.245.13.107 'cd /opt/chatbot-cmc; set -a; . .env; set +a; python3 scripts/_extract_eco_convs.py'
sshpass -p '***' scp root@157.245.13.107:/tmp/eco_conversations.json data/eco_conversations.json
# 2) re-puntuar y repartir en lotes:
python3 scripts/_eco_friction_prep.py
# 3) lanzar el portavión (Claude Code): workflow portavion-eco-friccion
```
