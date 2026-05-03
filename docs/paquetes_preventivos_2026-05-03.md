# Paquetes preventivos CMC — Propuesta v1

**Fecha:** 2026-05-03
**Objetivo:** Subir ticket promedio sin más tráfico. Vender un "chequeo completo" como producto único en vez de servicios sueltos.
**Meta:** +3-5M/mes en 60-90 días.

---

## Por qué funciona

Hoy un paciente que viene por "control mujer" gasta una consulta ($16-30k). Si le ofreces un paquete que incluye PAP + ECO TV de una sola vez, se va con un chequeo completo y tú facturas 2-3x más en la misma sesión. La paciente percibe ahorro (vs precio individual sumado), tú subes ticket y resuelves todo en una visita.

La fricción que rompe esta venta hoy: nadie le dice al paciente que existe el combo. El bot va a hacerlo.

---

## Los 4 paquetes propuestos

### 1. Chequeo Mujer 30+

**Componentes (particular):**
| Servicio | Precio individual |
|----------|-------------------|
| Consulta Matrona (Saraí Gómez) o Ginecología (Dr. Rejón) | $30.000 |
| PAP | $20.000 |
| Ecografía transvaginal (Dr. Pardo) | $35.000 |
| **Suma sin descuento** | **$85.000** |

**Precio paquete:** $69.990 (descuento 18%, $15k de ahorro)
**Versión Fonasa** (Matrona tarifa preferencial $16.000):
- Suma: $71.000
- Paquete Fonasa: **$59.990**

**Pitch en bot:**
> "¿Sabías que tenemos un *Chequeo Mujer 30+* que incluye control con matrona, PAP y ecografía transvaginal — todo el mismo día, $69.990 (ahorro $15k vs por separado)? Es ideal para revisarte una vez al año. ¿Te lo cuento?"

**Frecuencia recomendada:** anual.
**Cross-sell:** Mamografía (cuando exista convenio externo) + densitometría desde 40+.

---

### 2. Chequeo Hombre 40+

**Componentes (particular):**
| Servicio | Precio individual |
|----------|-------------------|
| Consulta Medicina General (foco cardiovascular) | $25.000 |
| ECG (electrocardiograma) | $20.000 |
| Examen físico de próstata (incluido en MG) | — |
| Lab básico perfil bioquímico (cuando exista) | $25.000* |
| **Suma sin descuento** | **$70.000** (con lab) / **$45.000** (sin lab v1) |

**Precio paquete v1 (sin lab):** $39.990 (descuento 11%, $5k ahorro)
**Versión Fonasa** (MG bono $7.880 + ECG $20.000): suma $27.880 → paquete **$24.990**.
**v2 con lab marca blanca propio:** $59.990 (cuando se cierre lab TME).

**Pitch:**
> "Hombre 40+: *Chequeo de 40 minutos*. Consulta médica enfocada en presión, corazón y próstata + ECG. $39.990 (Fonasa $24.990). Una vez al año. ¿Te agendo?"

**Cross-sell:** Cardiología si ECG sale alterado ($40k); urólogo (si llega a CMC).

---

### 3. Chequeo Escolar (preescolar y escolar)

**Componentes (particular):**
| Servicio | Precio individual |
|----------|-------------------|
| Consulta Pediatría o Medicina General | $25.000* |
| Audiometría (Fonoaudiología) | $25.000 |
| Evaluación visual básica (si hay convenio óptica) | externo |
| Informe médico escolar firmado | incluido |
| **Suma** | **$50.000** |

**Precio paquete:** $39.990 (descuento 20%, $10k ahorro)
**Pitch (campaña marzo + agosto inicio semestre):**
> "Vuelta a clases: *Chequeo Escolar* incluye consulta pediátrica + audiometría + informe médico para el colegio. $39.990. Listo en 1 hora. ¿Te agendo a tu hijo?"

**Cross-sell:** Nutrición ($20k) si IMC alterado; Psicología infantil si hay derivación.

---

### 4. Chequeo Deportivo / Pre-actividad física

**Componentes (particular):**
| Servicio | Precio individual |
|----------|-------------------|
| Consulta Medicina General (foco cardio + músculo-esquelético) | $25.000 |
| ECG en reposo | $20.000 |
| Informe de aptitud deportiva firmado | incluido |
| **Suma** | **$45.000** |

**Precio paquete:** $34.990 (descuento 22%, $10k ahorro)
**Pitch (campaña enero + agosto = inscripciones gimnasio/club):**
> "¿Vas a inscribirte en gimnasio, maratón o club deportivo? *Chequeo Deportivo* con MG + ECG + informe firmado de aptitud. $34.990 listo en 45 min."

**Cross-sell:** Cardiología si soplo o ECG alterado; Trauma + kine si dolor músculo-esquelético previo.

---

## Reglas de copy importantes (no violar)

1. **NO usar "certificado/acreditado/habilitado por Superintendencia"** — es publicidad engañosa. Usar "informe médico", "evaluación", "control".
2. **Métodos de pago**: en estos paquetes (todos médicos) → efectivo o transferencia. NO mencionar tarjeta. Si fueran dentales sí, pero no aplica.
3. **Fonasa**: SIEMPRE mencionar precio Fonasa cuando aplica (mujeres 30+, hombres 40+ con MG). Es el público mayoritario del CMC.
4. **No dar diagnóstico en el copy** — los paquetes son preventivos, no diagnósticos. "Detecta a tiempo" sí; "diagnostica" no.
5. **Tono**: español chileno neutro, "tú" estándar. Sin argentinismos. Sin emojis excesivos.

---

## Implementación bot (flow)

Disparadores donde el bot ofrece paquete proactivamente:

| Contexto | Paquete a ofrecer |
|----------|-------------------|
| Mujer 30-50, agenda matrona/gineco | Chequeo Mujer 30+ |
| Hombre 40-60, agenda MG | Chequeo Hombre 40+ |
| Mes febrero-marzo o julio-agosto, agenda pediatría | Chequeo Escolar |
| Mes enero o agosto, MG por motivo "deporte/gimnasio" | Chequeo Deportivo |
| Cualquier paciente nuevo > 30 años pregunta "¿qué chequeos hacen?" | El que aplica por edad/sexo |

Estado de sesión: agregar `data["paquete_ofrecido"] = "MUJER_30"` para evitar repetición.
Cooldown: no ofrecer mismo paquete al mismo teléfono en menos de 90 días.

Flow detalle:
1. Bot detecta contexto → mensaje con pitch + 2 botones: "Sí, cuéntame más" / "Otro día"
2. Si "Sí" → bot manda detalle (componentes + precio + ahorro) + 2 botones: "Agendar paquete" / "Solo la consulta"
3. Si "Agendar paquete" → bot agenda primer servicio del paquete (ej: matrona) y agenda en mismo día/semana los siguientes
4. Métrica: log evento `paquete_ofrecido`, `paquete_aceptado`, `paquete_completado` (cuando todos los servicios se hicieron)

---

## Landing dedicada

URL: `centromedicocarampangue.cl/chequeos`

Estructura:
- Hero: "Chequea tu salud antes de que sea urgente — 4 planes desde $34.990"
- 4 cards (uno por paquete) con: icono, qué incluye, precio Fonasa/particular, botón "Agendar por WhatsApp"
- Sección "¿Por qué hacerlo?" — frecuencia recomendada por edad
- Sección "Cómo funciona" — todo el mismo día, 1 visita, listos en 1-2 horas
- FAQ: ¿qué traer? ¿en ayunas? ¿lo cubre Fonasa? ¿pueden venir niños?
- CTA fijo abajo: "Reserva por WhatsApp +56966610737"

---

## Templates Meta (preparar para aprobación)

### Plantilla 1 — campaña paid Meta "Chequeo Mujer 30+"
- Imagen: mujer 30-40, tono cálido, en consulta con matrona
- Copy primario: "Tu chequeo anual completo en una sola visita. Matrona + PAP + Ecografía. Desde $59.990 (Fonasa). Centro Médico Carampangue."
- CTA: "Agendar por WhatsApp"
- Targeting: mujeres 28-50, comunas Arauco/Curanilahue/Lebu/Cañete + 30 km

### Plantilla 2 — "Chequeo Hombre 40+"
- Imagen: hombre 40+, profesional, con doctor
- Copy: "Hombre 40+: tu corazón y tu próstata necesitan revisión anual. ECG + consulta médica. $24.990 con Fonasa, $39.990 particular."
- Targeting: hombres 38-65, mismas comunas

### Plantilla 3 — "Vuelta a clases" (estacional feb-mar y jul-ago)
- Imagen: niño/a feliz, mochila colegio
- Copy: "Vuelta a clases sin estrés. Chequeo Escolar con pediatra + audiometría + informe médico. $39.990. Lo dejamos listo en 1 hora."
- Targeting: padres 30-50

### Plantilla 4 — "Chequeo Deportivo" (estacional ene + ago)
- Imagen: persona running / gym
- Copy: "¿Te inscribiste al gimnasio o vas a correr una maratón? Chequeo deportivo con MG + ECG + informe de aptitud. $34.990. Antes de empezar, asegura que estás listo."
- Targeting: 25-50, intereses fitness/running/gym

---

## Métricas de éxito

| Métrica | Baseline (estimar) | Meta 90d |
|---------|-------------------|----------|
| Paquetes vendidos/mes | 0 | 60-100 |
| Ticket promedio | (consulta sola ~$25k) | $40-50k |
| % pacientes que aceptan paquete cuando se ofrece | n/a | >25% |
| Recurrencia anual (paquete año siguiente) | n/a | >40% (medir 12m) |
| Δ ingreso mensual | 0 | +3-5M |

---

## Lo que necesito de Rodrigo

1. **Aprobar precios** o ajustarlos. Especialmente:
   - Pediatría: ¿$25.000 particular es correcto? Verificar.
   - Audiometría como componente del Chequeo Escolar — ¿conviene incluirla o es opcional?
   - Margen mínimo aceptable por paquete (descuento del 18-22% propuesto).
2. **Confirmar contraindicaciones**: ¿hay paciente que NO debería contratar el paquete? (ej: embarazada en Chequeo Mujer cambia el flujo).
3. **Decidir si lanzar todos a la vez o uno cada 2 semanas** (mi recomendación: arrancar con Mujer 30+ que tiene más demanda histórica, escalar resto en olas).
4. **Definir si el "informe médico escolar/deportivo"** lo firma el médico tratante o necesita formato especial.

---

## Pendiente para v2 (cuando llegue lab marca blanca)

- Sumar perfil bioquímico básico al Chequeo Hombre 40+ y al Chequeo Mujer 30+.
- Crear "Chequeo Senior 60+" con perfil completo + ECG + control múltiples patologías crónicas. Ticket potencial $80-120k.
- Crear "Chequeo Pre-laboral" para vender al canal empresas (ver carpeta_comercial_empresas).

---

**Status:** Listo para aprobación de precios. Una vez aprobado, implementación bot + landing en 5-7 días hábiles.
