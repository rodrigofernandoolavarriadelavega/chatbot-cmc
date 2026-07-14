# Ficha de ingreso — Profesional/Prestación nueva (CMC)

Responder TODO esto por cada profesional nuevo. Es el insumo del agente
`cmc-nuevo-profesional`. Cada pregunta alimenta una parte del ecosistema (bot / memoria / web / SEO).
Lo que quede sin responder **bloquea** o **degrada** la integración.

## 1. Identidad del profesional
- [ ] **Nombre completo** + prenombre (Dr. / Dra. / Prof. / TM): __________
- [ ] **RUT** (con dígito verificador): __________
- [ ] **¿Ya está creado en Medilink?** (Sí/No) → **si NO, crearlo primero** __________
- [ ] **ID de Medilink** (el bot agenda por ID, no por nombre — obligatorio): __________
- [ ] **Celular/WhatsApp del profesional**: __________
- [ ] **¿Recibe alertas de sus citas por WhatsApp?** (Sí/No) → si Sí, ese número va a `PROF_ID_TO_PHONE`
- [ ] **Email**: __________

## 2. La prestación / servicio
- [ ] **Especialidad exacta** (cómo debe aparecer, ej. "Tecnología Médica Oftalmológica"): __________
- [ ] **¿Qué incluye?** (frase clara para el bot y la web): __________
- [ ] **Duración por consulta** (intervalo en minutos): __________
- [ ] **Días y horario de atención**: __________
- [ ] **Modalidad**: presencial / **telemedicina** (¿videollamada?): __________
- [ ] **¿Requiere abono/comprobante previo?** (como Psiquiatría) (Sí/No + monto): __________
- [ ] **Edad mínima / máxima** de pacientes (si aplica): __________
- [ ] **¿Tiene subtipos/exámenes distintos?** (ej. por órgano, por examen) — listar: __________

## 3. Precios
- [ ] **Precio particular**: $__________
- [ ] **¿Acepta Fonasa?** (Sí/No) → si Sí, **precio/bono Fonasa**: __________
- [ ] **¿Precio distinto por subtipo o edad** (ej. niño vs adulto)? __________
- [ ] **¿Precio de control / segunda consulta**? __________
- [ ] **Métodos de pago**: médica = efectivo/transferencia · dental = + débito/crédito *(regla fija)*

## 4. Conocimiento para el bot (cómo lo pide el paciente)
- [ ] **¿Cómo lo dice la gente?** — sinónimos/frases coloquiales que deben rutear a esta prestación
      (ej. oftalmología: "vista", "lentes", "examen de la vista", "no veo bien", "presión del ojo"): __________
- [ ] **¿Cross-sell** con qué otra prestación del CMC? __________
- [ ] **¿Reemplaza o complementa** a otro profesional existente? __________
- [ ] **¿Algún síntoma que deba derivar** a este profesional? (para triage): __________

## 5. Web + SEO
- [ ] **¿Va en la web pública?** (tarjeta de especialidad en centromedicocarampangue.cl) (Sí/No)
- [ ] **¿Blog SEO?** (Sí/No) → **slug** sugerido (sin tilde): /blog/__________
- [ ] **Nombre canónico Schema.org** de la especialidad (ej. Ophthalmology): __________
- [ ] **Descripción corta** (1 línea para la tarjeta): __________

## 6. Marketing / respaldo (publicidad NO engañosa)
- [ ] **Credenciales reales y verificables** publicitables (diplomado, universidad, años exp.): __________
      ⚠️ NO usar "certificado/acreditado/habilitado/Superintendencia" salvo que sea verdad y esté validado.
- [ ] **Ángulo de venta / diferenciador** (por qué elegirnos): __________
- [ ] **¿Somos competitivos en precio?** (análisis de competencia zona Arauco): __________
- [ ] **¿Foto del profesional** para la web? (Sí/No)

## 7. Reglas de marca (recordatorio — el agente las aplica solo)
- Una sola sede: Monsalve 102, Carampangue. "Olavarría" = apellido, no sucursal.
- Teléfonos: bot **+56966610737** · fijo **(44) 296 5226** (nunca (41)). Nunca el personal +56987834148.
- Precios y datos clínicos: **no se inventan** — se piden.

---
> Al completar esta ficha, invocar el agente `cmc-nuevo-profesional`, que edita las ~14
> ubicaciones de código + memoria + web + blog, verifica, y deja todo listo para deploy
> (el humano deploya con `scripts/ship.sh`).
