# ROADMAP CDISAH — órdenes de trabajo ejecutables

> **Para quién es este archivo:** cualquier modelo de IA (o humano) que abra una sesión
> futura sin el contexto en que se escribió. Cada orden (M1…M12) es autocontenida:
> objetivo, precondiciones, pasos exactos, criterio de éxito y límites. Si solo vas a
> leer una sección, lee **Guardrails** primero.
>
> Origen: análisis profundo del 2026-07-12/14 contra datos reales de producción,
> mapeado sobre la WHO CDISAH 2.ª ed. (2023). Dashboard visual: `static/mejora-cdisah.html`.
> Marco general: `static/arquitectura.html` y memoria `marco_frentes_alma_who_cdhi`.

---

## GUARDRAILS — violarlos rompe producción o la ley

1. **El VPS jamás se edita a mano.** Es destino de deploy, no de trabajo. Todo cambio
   nace local → revisión del dueño → `bash scripts/ship.sh "<msg>" <archivos>`.
   (Incidente 2026-06-07: editar en el server dejó prod 21 commits divergida.)
2. **Los agentes NO hacen `git commit`.** Dejan el working tree listo y le indican al
   dueño el comando. Es regla explícita del dueño.
3. **La API de Medilink tiene rate limit real.** El 2026-07-13 un análisis masivo gatilló
   HTTP 429 — y es la MISMA API con la que el bot atiende pacientes. Para análisis usar
   SIEMPRE las cachés locales de prod (`bi_atenciones`, `bi_pagos_caja` en `sessions.db`),
   nunca fetch masivo a `api.medilink2.healthatom.com`.
4. **Cómo abrir `sessions.db` de prod** (está cifrada con SQLCipher):
   ```python
   from sqlcipher3 import dbapi2 as drv
   con = drv.connect('data/sessions.db')          # en /opt/chatbot-cmc, con .env cargado
   con.execute(f"PRAGMA key = \"x'{SQLCIPHER_KEY}'\"")   # clave HEX cruda, no passphrase
   con.execute("PRAGMA cipher_page_size = 4096")
   ```
5. **Fuentes de verdad de la plata** (confundirlas ya costó un día de análisis):
   - **Venta total** = `bi_pagos_caja` (espejo de la caja Medilink; es lo que muestra DB Mensual `/cmc/mensual`).
   - **Copago y medio de pago** = `pagos_cmc` (lo que teclea recepción en Alma). Medilink marca 99,7% "Efectivo" → inútil para mix de tarjeta.
   - **Deuda** = `bi_atenciones.deuda` **cruzada** contra `bi_pagos_caja` vía `atencion_id`. Nunca leerla sola: ~45% de la deuda cruda es obsoleta.
6. **Nada customer-facing sin consent.** Contactos de cobranza/marketing los hace
   recepción (humano), no el bot, mientras M3 (Ley 21.719) no esté cerrado.
7. Reglas fijas del CMC: tarjetas SOLO dental · fijo (44) 296 5226, nunca (41) ·
   número personal +56987834148 jamás a pacientes · dashboards sin CDN (CSP) ·
   `with db() as`, nunca `with _conn() as`.

---

## Estado de cuenta (medido, no estimado)

| Qué | Valor | Fuente | Fecha |
|---|---|---|---|
| Venta junio 2026 | **$26.039.530** | `bi_pagos_caja` | 2026-07-12 |
| Fuga por atenciones sin cerrar | **~$25,5M/año** (569 at. en 180d, tasa 2%→15%) | `/atenciones` finalizado=0 ∧ total=0 | 2026-07-12 |
| Deuda cobrable confirmada | **$1.695.780** (48 at., doble fuente) | atenciones×caja | 2026-07-14 |
| Deuda dudosa (caja registra pagos) | $1.360.330 (13 at.) — revisar a mano | ídem | 2026-07-14 |
| Mix tarjeta real | **92% débito / 8% crédito** (n=92; crédito n=7; 99% sin N° comprobante) | `pagos_cmc` jun–jul | 2026-07-12 |
| Demanda insatisfecha #1 | **Pediatría, 13 solicitudes** con teléfono | `demanda_no_disponible` | 2026-07-13 |
| Deadline legal | **1-dic-2026** — Ley 21.719 plena vigencia | texto legal verificado | — |

---

## ÓRDENES DE TRABAJO

### M1 · Cobranza validada — $1,7M ya ganados · P0 · esfuerzo: horas
**Objetivo:** entregar a recepción la lista de las 48 atenciones con deuda confirmada, con teléfono, para que llame.
**Precondición:** ninguna. Solo lectura.
**Pasos:**
1. En prod (`ssh root@157.245.13.107`, `/opt/chatbot-cmc`, `.env` cargado), abrir `sessions.db` (guardrail 4) y correr:
   ```sql
   SELECT a.atencion_id, a.fecha, a.paciente_nombre, a.id_paciente, a.total, a.abonado, a.deuda,
          COALESCE((SELECT SUM(p.monto) FROM bi_pagos_caja p WHERE p.atencion_id=a.atencion_id),0) AS pagado_caja
   FROM bi_atenciones a WHERE a.deuda > 0 ORDER BY a.fecha DESC;
   ```
2. Filtrar `total - pagado_caja >= deuda - 1000` (deuda confirmada). Lo demás va a una pestaña "revisar a mano".
3. Teléfono: cruzar `id_paciente`/RUT contra `contact_profiles` (misma DB) o vía `session.get_phone_by_rut`.
4. Ordenar por `deuda × frescura` (2026 primero; lo de 2024 va al final, cobrabilidad baja).
5. Exportar CSV → entregarlo al dueño/recepción. **El contacto lo hace un humano** (guardrail 6).
**Éxito:** CSV entregado; a 30 días, $ cobrado > $0 registrado en caja contra esas atenciones.
**No hacer:** mensajes automáticos de cobranza por WhatsApp.

### M2 · Frenar la fuga de no-cierre — ~$25,5M/año · P0 · esfuerzo: bajo
**Objetivo:** que las atenciones sin cerrar (`finalizado=0 ∧ total=0`) se vean todos los días, por profesional.
**Contexto:** tasa histórica 2% → 15% desde marzo 2026. Rodrigo pasó de 0/mes (dic–feb) a 78 en junio (43% del total). **Pregunta abierta al dueño: ¿qué cambió en marzo?** — puede ser proceso, no software.
**Precondición:** el detector ya existe y está probado: `~/health-bi-project/scripts/atenciones_no_cerradas.py` (usa período de gracia 7d para no inflar con atenciones recientes).
**Pasos:**
1. Portarlo a leer `bi_atenciones` local (hoy golpea la API — guardrail 3): misma lógica, fuente `sessions.db` prod.
2. Cron diario (patrón de `app/jobs.py`) que genere el resumen por profesional.
3. Enviar resumen al dueño por el canal admin existente (ver `doctor_alerts.py` como patrón). NO a pacientes.
4. Dejar listo sin deploy; el dueño embarca con `ship.sh`.
**Éxito:** tasa de no-cierre mensual de vuelta bajo 2% (línea base dic–feb) en 60 días.

### M3 · Ley 21.719 — paquete mínimo · P0 legal · deadline 1-dic-2026
**Objetivo:** cerrar las 4 brechas con multa: encargados de tratamiento, consentimiento, registro, accesos.
**Hecho clave:** el bot envía datos de salud a Anthropic (Claude) y OpenAI (Whisper) en EE.UU. → son "encargados de tratamiento" (art. 15 bis, verificado) y es transferencia internacional (arts. 27-29).
**Pasos:**
1. **DPA**: firmar Data Processing Addendum con Anthropic y OpenAI (ofrecen estándar; trámite del dueño, no de código).
2. **Consent separado** para transferencia internacional en el primer contacto del bot — NO empaquetado en el opt-in general. Tabla `consent_records` versionada con snapshot del texto exacto mostrado.
3. **`access_log_sensibles`**: middleware en `claude_helper.py` y `messaging.py` (Whisper) que registre cada payload de salud que sale a terceros.
4. **Política de privacidad** (`/privacidad`): informar la transferencia y sus garantías (obligación literal art. 14 ter letra h).
5. ARCO 30+30 días, flujo de brechas "sin dilación indebida" (usar 72h como estándar interno), retención conciliada con los 15 años de ficha (Ley 20.584), documentar categoría PYME (Ley 20.416 — amonestación en vez de multa el 1er año).
**Detalle completo** (DDL, endpoints, checklist de 10 ítems): investigación del 2026-07-12 en la conversación de origen; spec resumida en memoria `evidencia_internacional_frentes_cmc`.
**Éxito:** checklist 10/10 antes del 1-dic-2026.

### M4 · ENO — delegado de epidemiología · P0 · costo ≈ cero
El Decreto 7/2019 (art. 3) obliga al director a nombrar un delegado. Es un papel firmado + un recordatorio en el flujo clínico cuando se registre un diagnóstico de la lista ENO (notificación vía EPIVIGILA, manual). Preguntar además a la SEREMI Biobío si el CMC tiene código DEIS (define si aplica REM).

### M5 · Auditar `AUTOPILOT_EXECUTE=true` · P0 · esfuerzo: minutos
**Hallazgo 2026-07-13:** el `.env` de prod tiene `AUTOPILOT_ENABLED=true` y `AUTOPILOT_EXECUTE=true`, pero la memoria del proyecto dice que Autopilot estaba gated OFF. **Contradicción sin resolver.**
**Pasos:** leer `app/autopilot/engine.py` (el flag se consume ahí, default false) y el log de acciones del Autopilot en prod; determinar si está ejecutando cambios de campañas solo. Si sí y el dueño no lo sabía → apagar `AUTOPILOT_EXECUTE` y revisar qué hizo. Reportar al dueño en cualquier caso.

### M6 · Embarcar el fix de método de pago · P1 · ya escrito
**Estado:** código listo en el working tree local (`templates/alma_pagos.html`, `app/pagos_routes.py`, `scripts/qa_pagos.py`), validado (deep-import + `node --check`). Elimina los 3 defaults silenciosos de "efectivo" y exige N° de comprobante en tarjeta.
**Pasos:** el dueño revisa → `bash scripts/ship.sh "fix(pagos): elimina defaults silenciosos + exige comprobante" templates/alma_pagos.html app/pagos_routes.py scripts/qa_pagos.py`. Después: corregir "86/14" → **92/8** en `static/tarjetas-analisis.html` (el 86/14 salió de 309 registros de 6 días; el real es 92/8 con n=1.956 — y anotar que crédito n=7 no soporta conclusiones).
**Éxito:** en 90 días, % de tarjetas con comprobante > 90% (hoy: 1%), medible con `scripts/qa_pagos.py`.

### M7 · Doble recordatorio 3d + 1d · P1 · evidencia RCT n=54.066
Hoy hay recordatorio 09:00 del día + 2h antes. El RCT (AJMC) muestra que **3 días + 1 día = 4,4% de no-show vs 5,8% (solo 3d)**. Tocar `app/reminders.py` — archivo core: correr `tests/harness_50.py` antes de proponer deploy. Sin perfilamiento (no requiere M3).

### M8 · Copago en el momento · P1 · evidencia +27% (HFMA)
Cargar el arancel MLE 2026 (Excel oficial: `fonasa.gob.cl/wp-content/uploads/sites/3/2026/03/4-Arancel-MLE-2026.xlsx`, requiere user-agent de navegador) a una tabla local; estimador de copago por prestación×nivel; hint en `alma_pagos.html` cuando lo cobrado difiera del arancel. Prerrequisito humano: confirmar el nivel MLE (1/2/3) del CMC.

### M9 · Medir adopción del Copiloto de Ficha · P1 · repo aparte (sin colisión)
En `~/copiloto-ficha`: log por nota de `% aceptada sin edición mayor` (Levenshtein >15% = mayor), tiempo hasta firma, uso semanal por médico, e ítem único de burnout (Dolan 1-5) trimestral. El 15% de médicos de un RCT nunca abrió la herramienta asignada — sin este número todo lo demás es fe.

### M10 · «Mis exámenes» · P2 · NO es un portal
Link con token por WhatsApp (256 bits, hash almacenado, expira 7d, verificación últimos 4 del RUT), **gate clínico en el servidor**: Tier 1 auto-publica rutina normal · Tier 2 revisión médica · Tier 3 llamada obligatoria (valores críticos). **Reglas duras legales:** VIH y anatomía patológica JAMÁS por link (Ley 19.779/DS 182: entrega personal por personal capacitado) · el contenido clínico NUNCA va en el cuerpo del mensaje WhatsApp (política Meta) — el mensaje solo dice "tu resultado está listo". Umbral de muerte: <40% de apertura a 90 días → rediseñar canal, no agregar features.

### M11 · Llamada telefónica a no-shows de alto riesgo · P2 · DESPUÉS de M3
Evidencia chilena (CMM U. de Chile, 18.000 citas): llamada −7,8pp vs WhatsApp −5,4pp; 60% prefiere la llamada. **Bloqueado por M3:** un score de riesgo es "elaboración de perfiles" bajo la Ley 21.719 → exige Evaluación de Impacto previa y modelo explicable (regresión logística, NO boosting). Llamar solo al decil superior (VPP ~0,18). Medir con A/B dentro del grupo de alto riesgo.

### M12 · Cierre del loop de derivación · P2
Registro de cada derivación: emitida → agendada → atendida → contrarreferencia recibida, con alerta de loop abierto a N días. Evidencia OMS: en ~la mitad de las derivaciones el informe nunca vuelve. No depende de estándares externos — funciona con PDF.

### Órdenes solo-humano (ningún modelo puede ejecutarlas)
- **H1 · Bonos Fonasa:** entrar con Clave Única al portal del operador (cobranza3.bonoelectronico.cl si es i-Med) y bajar el reporte de bonos sin documentar/rechazados. Contexto: el bono caduca a los **180 días** si no se cobra (Res. 49/2009); la revisión de rechazados prescribe a los **5 años** (persona jurídica) — activo dormido sobre 53k citas.
- **H2 · Pediatría:** 13 solicitudes con teléfono en `demanda_no_disponible`. Decisión de contratación (aunque sea media jornada): lista de espera desde el día uno.
- **H3 · ¿Qué cambió en marzo?** — la respuesta a la curva de no-cierre probablemente es de proceso, no de código.

---

## Orden de ejecución (90 días)

- **Ahora (semanas 1-2):** M1 · M2 · M4 · M5 · M6 — plata inmediata y riesgo, sin dependencias.
- **Luego (mes 1-2):** M3 (empezar YA por el DPA, es trámite) · M7 · M8 · M9.
- **Después (mes 2-3):** M10 · M11 (tras M3) · M12. En paralelo humano: H1, H2, H3.
- **No hacer todavía:** integración HL7 con laboratorio (sin evidencia de que los labs chicos la soporten) · portal clásico con login (la evidencia lo mata) · CDS interruptivo (override 90%) · encender orquestadores de contacto masivo antes de M3.
