# Check-in QR + Auto-pago por transferencia — Diseño

**Estado:** diseño aprobado (2026-06-09) · build en curso
**Objetivo:** que el paciente se auto-registre al llegar (sin que recepción marque "llegó")
y, si paga por transferencia, que pueda pagar solo: manda el comprobante, el bot lo lee,
lo valida, lo deja **"en sala de espera"** y crea el pago en el **cuadro de Pagos**.

---

## 1. Dos features separables (se construyen en este orden)

| # | Feature | Riesgo | Sirve para |
|---|---------|--------|-----------|
| A | **Check-in QR** → marca "en sala" | ninguno | TODOS los pacientes (efectivo, Fonasa, transferencia) |
| B | **Auto-pago transferencia** (comprobante → bot lee → registra) | medio (comprobante falsificable) | particular/copago por transferencia |

A es la base y entrega valor solo. B se monta encima.

---

## 2. Piezas que ya existen y se reusan

- **Flujo web** estilo `/agendar` (agendador_routes) → se clona a `/checkin`.
- **Lookup de cita de hoy + arancel por RUT** → ya lo hacen Pagos (`pagos_routes`) y Agenda.
- **Lectura de imágenes con visión Claude** → patrón del módulo *examen-a-ficha* (foto→texto).
- **Registro automático en Pagos** → `pagos_cmc` (método=transferencia, folio=N° operación).
- **Bot maneja imágenes entrantes** → `messaging.py` ya descarga media de WhatsApp.

---

## 3. Flujo A — Check-in QR

```
QR impreso en recepción (estático, apunta a /checkin)
   │
   ▼
/checkin  → paciente escribe su RUT
   │
   ▼
Busca cita(s) de HOY en Medilink (por RUT → id_paciente → /citas hoy)
   │
   ├── sin cita hoy   → "No encontramos tu hora de hoy. Acércate a recepción."
   └── con cita       → "Hola [nombre], tu hora es 10:30 con Dr. X."
                         botón [Avisar que llegué]
   │
   ▼
Marca check-in → estado "en sala de espera" (tabla local checkin_cmc)
   │
   ▼
Aparece en el panel de recepción/profesional: lista "En sala" (orden de llegada)
```

### Modelo de datos (nuevo)
Tabla `checkin_cmc`:
| campo | tipo | nota |
|-------|------|------|
| id | PK | |
| fecha | TEXT | YYYY-MM-DD |
| rut | TEXT | |
| paciente_nombre | TEXT | |
| id_cita | TEXT | cita Medilink del día |
| id_profesional | INT | |
| hora_cita | TEXT | |
| estado | TEXT | `en_sala` · `atendido` · `pagado` |
| pagado | INT | 0/1 (lo setea Feature B) |
| llegada_at | TEXT | timestamp del check-in |

> Medilink **no** tiene un estado "en sala/esperando" estándar (sus estados:
> Confirmado, Atendiéndose, Atendido, No asiste…). Por eso el check-in se trackea
> **local** y se muestra en el panel. Opcional: empujar a Medilink "Confirmado" o
> dejar un comentario en la cita.

### Dónde se ve
- Nueva pill/sección **"En sala (N)"** en el panel v2/v3 con la lista por orden de llegada
  (más antiguo primero — el que lleva más esperando arriba). Botón "marcar atendido".

---

## 4. Flujo B — Auto-pago por transferencia

```
En /checkin, si la cita es PARTICULAR/copago y elige "pagar por transferencia":
   │
   ▼
Muestra: cuenta CMC + monto esperado ($ arancel/copago)
   "Transfiere $X a [cuenta]. Luego mándame el comprobante por WhatsApp 👉 [link wa.me]"
   (o subir la imagen en la misma página)
   │
   ▼
Paciente transfiere y envía el comprobante (imagen)
   │
   ▼
Bot lee el comprobante con visión Claude:
   { monto, fecha, cuenta_destino, n_operacion, banco_origen }
   │
   ▼
VALIDACIÓN (plausibilidad, no prueba):
   ✓ monto ≈ esperado (tolerancia)
   ✓ cuenta_destino == cuenta del CMC
   ✓ fecha == hoy (o ±1 día)
   ✓ n_operacion NO usado antes  ← anti-reuso (tabla comprobantes_cmc)
   │
   ├── válido   → marca pagado=1 + estado en_sala + crea pago en pagos_cmc
   │              (metodo=transferencia, folio=n_operacion, copago=monto)
   │              → "✅ Pago recibido, pasa a sala 🪑"
   └── dudoso   → "Recepción va a revisar tu comprobante" + alerta a recepción
```

### El riesgo (mirarlo de frente)
Un comprobante es una **imagen**: se puede falsear o reusar uno viejo. La confirmación
**real** solo llega con la **cartola del banco (T+1)**. Entonces el bot hace un
**chequeo de plausibilidad**, NO una prueba de pago. Mitigaciones:
1. **Anti-reuso**: guardar `n_operacion` en `comprobantes_cmc`; si se repite → rechazo.
2. **Cuenta + monto + fecha** deben calzar.
3. **Reconciliación posterior**: el módulo Conciliación / `auditor.py` cruza los pagos
   marcados como transferencia contra la cartola real del banco → marca el que no aparezca.
4. **Provisional**: el paciente pasa a sala con comprobante "plausible"; si después no
   calza con el banco, queda flag para cobrar/seguir. (Así operan casi todas las clínicas.)

---

## 5. Quién paga por adelantado
- **Particular** (psiquiatría, estética, etc.): sí, tiene sentido pre-pago.
- **Fonasa / bono web**: el copago es bajo y el bono lo gestiona Fonasa → probablemente
  NO pre-pago por transferencia (pasa por recepción). El check-in (A) igual aplica.
- **Efectivo**: paga en recepción; el check-in (A) marca llegada igual.

→ El QR de check-in es universal. El auto-pago (B) se ofrece solo cuando aplica.

---

## 6. Decisiones que necesito del dueño (para Feature B)
1. **Cuenta(s) bancaria(s) del CMC** a las que el paciente transfiere (para validar destino).
2. **Qué prestaciones exigen pre-pago** por transferencia (¿todas las particular? ¿algunas?).
3. **Tolerancia de monto** (¿exacto? ¿±?).
4. **Qué hacer si el comprobante es dudoso**: ¿igual pasa a sala (provisional) o espera a recepción?

---

## 7. Orden de construcción
1. **A1** — tabla `checkin_cmc` + endpoints (buscar cita por RUT, marcar llegada).
2. **A2** — página `/checkin` (web premium, estilo agendador).
3. **A3** — sección "En sala" en el panel de recepción.
4. **B1** — opción "pagar por transferencia" en /checkin (muestra cuenta + monto).
5. **B2** — lectura de comprobante con visión + validación + anti-reuso.
6. **B3** — al validar: pago en `pagos_cmc` + pagado=1 + alerta recepción.
7. **B4** — reconciliación con cartola (extiende Conciliación/auditor).

Cada paso se despliega gateado (`CHECKIN_ENABLED`, `AUTOPAGO_ENABLED`) en OFF hasta validar.
