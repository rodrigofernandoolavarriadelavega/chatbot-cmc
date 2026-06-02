export const meta = {
  name: 'portavion-eco-friccion',
  description: 'Portavión de agentes auditores que revisan las conversaciones de ecografía del chatbot CMC donde más hay fricción, y consolidan las variantes de ecografía de David Pardo',
  phases: [
    { title: 'Escuadrón', detail: '18 auditores, uno por lote de 20 conversaciones de alta fricción' },
    { title: 'Síntesis', detail: 'consolidar patrones, fixes y catálogo de variantes eco de David' },
  ],
}

const DIR = '/Users/rodrigoolavarria/chatbot-cmc/data/eco_batches'
const N = 18

const DOMAIN = `Contexto de dominio (CMC, centro médico rural Carampangue/Arauco, Chile):
- ECOGRAFÍA es el servicio con MÁS fricción. Tres profesionales distintos hacen "eco":
  • David Pardo (Tecnólogo Médico, id 68) — $40.000 particular. Hace TODA la ecografía general:
    abdominal, renal, vesical/vejiga, hepática/hígado/vesícula, MAMARIA (partes blandas, NO ginecología),
    tiroidea, partes blandas/superficial, testicular/inguino-escrotal, cuello, próstata,
    musculoesquelética (hombro/codo/muñeca/mano/dedos/cadera/rodilla/tobillo/pie/articulación),
    doppler (no cardíaco), inguinal.
  • Dr. Tirso Rejón (id 61, ginecología) — $35.000 — eco transvaginal/pélvica/ginecológica/ovarios/útero. NUNCA mamaria.
  • Dr. Miguel Millán (id 60, cardiología) — $110.000 lista de espera — ECOCARDIOGRAMA (corazón).
- Bug histórico "eco-bleed": mencionar una parte del cuerpo sin la raíz "eco" ruteaba a ecografía (falsos positivos). Ya fixeado, pero busca residuos.
- Error histórico: eco MAMARIA se ruteaba a Rejón (ginecología) cuando es de David Pardo. Detecta este cruce.
- CMC NO hace eco obstétrica/embarazo/prenatal: solo consulta ginecológica con Rejón.
- "cancelar" en chileno a veces = PAGAR. Pacientes rurales, typos, Fonasa MLE N3.
- Eco NO toma Fonasa (particular). Teléfono personal +56987834148 nunca debe aparecer.`

phase('Escuadrón')
const FINDINGS = {
  type: 'object',
  additionalProperties: false,
  required: ['conversaciones', 'terminos_eco', 'resumen_lote'],
  properties: {
    conversaciones: {
      type: 'array',
      items: {
        type: 'object',
        additionalProperties: false,
        required: ['phone_tail', 'severidad', 'tipos_friccion', 'resumen', 'terminos_eco_usados', 'es_de_david', 'recomendacion_fix'],
        properties: {
          phone_tail: { type: 'string', description: 'últimos 4 dígitos del campo phone' },
          severidad: { type: 'integer', minimum: 0, maximum: 5, description: '0 sin fricción real, 5 fricción severa / paciente perdido' },
          tipos_friccion: { type: 'array', items: { type: 'string', enum: ['intent_mal_clasificado','profesional_equivocado','mamaria_ruteada_a_ginecologia','eco_bleed_residual','obstetrica_no_disponible','precio_confuso','menu_loop','derivacion_innecesaria','no_respondida','abandono_post_precio','fonasa_confuso','otro'] } },
          resumen: { type: 'string', description: 'qué pasó, 1-2 frases' },
          terminos_eco_usados: { type: 'array', items: { type: 'string' }, description: 'términos LITERALES de ecografía que escribió el paciente (ej "eco de mama", "ecografia de guata")' },
          es_de_david: { type: 'string', enum: ['si','no','ambiguo'], description: 'si la eco pedida la hace David Pardo' },
          recomendacion_fix: { type: 'string' },
        },
      },
    },
    terminos_eco: { type: 'array', items: { type: 'string' }, description: 'todos los términos eco literales del lote, deduplicados' },
    resumen_lote: { type: 'string' },
  },
}

const batchResults = await parallel(
  Array.from({ length: N }, (_, i) => () =>
    agent(
      `${DOMAIN}\n\nLee el archivo JSON ${DIR}/batch_${String(i).padStart(2,'0')}.json (con la tool Read o con \`cat\`). Es una lista de conversaciones reales de WhatsApp ya pre-filtradas por alta fricción de ecografía. Cada conversación tiene phone (últimos 4 dígitos), n (nº mensajes), score/sig (heurística previa, solo referencial) y msgs[] con d (in=paciente / out=bot), t (texto), s (estado), ts.\n\nAudita CADA conversación: ¿el bot entendió bien la ecografía pedida? ¿ruteó al profesional correcto (David vs Rejón vs Millán)? ¿hubo loop de menú, derivación innecesaria, abandono, confusión de precio/Fonasa, residuo de eco-bleed, mamaria mandada a ginecología? Extrae los TÉRMINOS LITERALES que el paciente usó para pedir la eco (tal cual los escribió, con typos). Marca si esa eco la hace David Pardo.\n\nNo inventes: si una conversación apenas menciona "eco" de pasada y no hay fricción real, dale severidad 0. Devuelve SOLO el objeto estructurado.`,
      { label: `escuadrón:lote-${String(i).padStart(2,'0')}`, phase: 'Escuadrón', schema: FINDINGS, agentType: 'cmc-conversation-auditor' }
    )
  )
)

const ok = batchResults.filter(Boolean)
const allConvs = ok.flatMap(r => r.conversaciones || [])
const friccion = allConvs.filter(c => (c.severidad || 0) >= 2)

// agregación determinista
const tipoCount = {}
for (const c of friccion) for (const t of (c.tipos_friccion || [])) tipoCount[t] = (tipoCount[t] || 0) + 1
const sevCount = {}
for (const c of allConvs) sevCount[c.severidad] = (sevCount[c.severidad] || 0) + 1

// catálogo de términos eco deduplicado (normalizado)
const norm = s => (s || '').toLowerCase().trim().replace(/\s+/g, ' ')
const termMap = new Map()
for (const c of allConvs) for (const t of (c.terminos_eco_usados || [])) {
  const k = norm(t); if (!k) continue
  if (!termMap.has(k)) termMap.set(k, { termino: k, n: 0, david: 0 })
  const e = termMap.get(k); e.n++; if (c.es_de_david === 'si') e.david++
}
for (const r of ok) for (const t of (r.terminos_eco || [])) {
  const k = norm(t); if (k && !termMap.has(k)) termMap.set(k, { termino: k, n: 0, david: 0 })
}
const terminosDavid = [...termMap.values()].filter(e => e.david > 0 || e.n === 0).sort((a,b)=>b.n-a.n)
const todosTerminos = [...termMap.values()].sort((a,b)=>b.n-a.n)

log(`Escuadrón: ${ok.length}/${N} lotes · ${allConvs.length} convs auditadas · ${friccion.length} con fricción real (sev≥2) · ${todosTerminos.length} términos eco únicos`)

phase('Síntesis')
const SYN = {
  type: 'object',
  additionalProperties: false,
  required: ['patrones_top', 'fixes_priorizados', 'variantes_eco_david', 'veredicto'],
  properties: {
    patrones_top: { type: 'array', items: { type: 'object', additionalProperties: false, required: ['patron','frecuencia','impacto','ejemplo'], properties: { patron: {type:'string'}, frecuencia: {type:'string'}, impacto: {type:'string'}, ejemplo: {type:'string'} } } },
    fixes_priorizados: { type: 'array', items: { type: 'object', additionalProperties: false, required: ['fix','archivo_probable','prioridad','riesgo'], properties: { fix: {type:'string'}, archivo_probable: {type:'string'}, prioridad: {type:'string', enum:['alta','media','baja']}, riesgo: {type:'string', enum:['data_safe','logic_review']} } } },
    variantes_eco_david: { type: 'array', items: { type: 'string' }, description: 'lista consolidada y limpia de TODAS las variantes/términos de ecografía que hace David Pardo (código + lo que dicen los pacientes)' },
    veredicto: { type: 'string', description: 'diagnóstico ejecutivo de la fricción de ecografía, 3-5 frases' },
  },
}

const synthesis = await agent(
  `${DOMAIN}\n\nEres el oficial de síntesis del portavión. El escuadrón auditó ${allConvs.length} conversaciones reales de ecografía de alta fricción del chatbot CMC. Te paso los datos agregados.\n\nConteo de tipos de fricción (en convs con fricción real):\n${JSON.stringify(tipoCount, null, 2)}\n\nDistribución de severidad (0-5):\n${JSON.stringify(sevCount, null, 2)}\n\nLas 40 conversaciones con fricción más severa:\n${JSON.stringify(friccion.sort((a,b)=>b.severidad-a.severidad).slice(0,40).map(c=>({tail:c.phone_tail,sev:c.severidad,tipos:c.tipos_friccion,resumen:c.resumen,david:c.es_de_david,fix:c.recomendacion_fix})), null, 2)}\n\nTérminos de ecografía literales que usaron los pacientes (con frecuencia y cuántas veces eran para David):\n${JSON.stringify(todosTerminos.slice(0,120), null, 2)}\n\nVariantes oficiales de David Pardo en el código (ECOGRAFIA_ROUTING): abdominal, renal, vesical/vejiga, hepática/hígado/vesícula, mamaria/mamas, tiroidea, partes blandas/superficial, testicular/inguino-escrotal, cuello, próstata, musculoesquelética (hombro/brazo/codo/muñeca/mano/dedos/cadera/rodilla/tobillo/pie/articulación), doppler no cardíaco, inguinal.\n\nProduce: (1) patrones_top de fricción rankeados por frecuencia×impacto; (2) fixes_priorizados concretos y accionables (apunta archivo probable: app/ecografias.py, app/flows.py, app/claude_helper.py; marca data_safe vs logic_review); (3) variantes_eco_david = lista consolidada, limpia y exhaustiva de TODAS las variantes de ecografía que hace David, fusionando el catálogo del código con los términos reales de los pacientes (incluye los typos/chilenismos frecuentes como sinónimos); (4) veredicto ejecutivo. Devuelve SOLO el objeto.`,
  { label: 'síntesis:oficial', phase: 'Síntesis', schema: SYN }
)

return {
  cobertura: { lotes_ok: ok.length, lotes_total: N, convs_auditadas: allConvs.length, convs_con_friccion: friccion.length, convs_descartadas_baja_friccion: 557, universo_total_eco: 917 },
  tipos_friccion: tipoCount,
  severidad_dist: sevCount,
  terminos_eco_top: todosTerminos.slice(0, 80),
  sintesis: synthesis,
}