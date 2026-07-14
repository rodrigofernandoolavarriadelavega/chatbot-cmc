# Lector de correos de Medilink (`app/email_ticker.py`) — notas de diseño

Ver también: `docs/medilink_gotchas.md` (reglas de la API), `app/agenda_ticker.py`
(ticker en vivo que este módulo alimenta con hora exacta de creación).

## Qué hace

Lee por IMAP (solo lectura) el Gmail del centro (`centromedicocarampangue@gmail.com`),
al que Medilink notifica cada agendamiento/anulación/reagendamiento. La cabecera
`Date` del correo es la hora exacta del evento — dato que la API de Medilink NO
entrega (solo `fecha_actualizacion`, que es un proxy de última modificación).

El correo no trae `id_cita`. El cruce contra una cita real de Medilink se hace en
dos niveles (ver docstring del módulo para el detalle):
1. Tabla local `agenda_ticker` (gratis, sin tocar Medilink).
2. Si no hay match, **una** consulta paginada a Medilink por fecha exacta —
   validado empíricamente 2026-07-14 contra 26 correos reales: **26/26 cruzados**
   con paginación completa (sin paginar, se pierden citas en fechas con más de 50
   citas y el cruce falla en falso — Medilink pagina de a 50).

## Oportunidad NO implementada: verificación automática de abonos de psiquiatría

**No construir sin que el dueño lo pida explícitamente — esto es solo el
hallazgo, documentado para no perderlo.**

Hoy (`abono_gate` / flujo de psiquiatría) el bot le pide al paciente una foto
del comprobante de transferencia para verificar el abono de la Dra. Unibazo.
Al mismo buzón Gmail del centro llegan avisos de transferencia de **al menos 3
bancos**, con suficiente información para cruzar automáticamente contra un
paciente y un monto esperado, sin necesitar la foto:

### Banco Falabella (`notificaciones@cl.bancofalabella.com`)
Asunto: `Aviso de transferencia de fondos recibida`. Cuerpo en texto plano, trae:
- **Nombre del cliente que transfiere** (ej. `CYNTHIA MACARENA TORREZ`)
- **Monto** (`Monto transferencia: $7.880`)
- **Fecha y hora** (`Fecha 13-07-2026`, `Hora 16:08`)
- **Número de operación** (`Numero de operacion 658660270611`)
- Banco/cuenta de destino (siempre el mismo, es la cuenta del centro)
- **No trae** RUT del que transfiere ni glosa/mensaje.

### Scotiabank (`avisos.info@scotiabank.cl`)
Asunto: `Aviso de Transferencia`. **Solo HTML, sin parte text/plain** (a
diferencia de los correos de Medilink) — habría que limpiar el HTML con el
mismo `_strip_html` que ya tiene `email_ticker.py`. Trae:
- **Nombre del cliente** (ej. `VIVIANA ANDREA SAMORA`)
- **Monto** (`Monto : 2.364`)
- **Fecha** (`13/07/2026`, sin hora exacta en el cuerpo — la cabecera `Date`
  del correo sí la tiene)
- **Mensaje/glosa que escribe quien transfiere** (ej. `Mensaje: consulta
  viviana Samora`) — **esto es oro para el cruce**: en la muestra revisada el
  cliente escribió su propio nombre en la glosa.
- Número de cuenta de destino (fijo, del centro).

### Banco de Chile (`serviciodetransferencias@bancochile.cl`)
Asunto: `Aviso de transferencia de fondos`. **No estaba en la lista original
del pedido** (solo se mencionaban Falabella y Scotiabank) — hay un tercer
banco activo en el mismo buzón. HTML pesado (XHTML con estilos inline), no se
inspeccionó el detalle de campos en esta pasada — pendiente si se retoma la
idea.

### Por qué es una buena idea (y qué falta para construirla)
- El monto esperado del abono de psiquiatría es fijo (`ABONO_PSIQUIATRIA_CLP`,
  hoy $60.000 en `config.py`) — cruzar por monto exacto + fecha reciente
  + (si hay glosa) nombre del paciente sería un match de alta confianza.
- **Riesgo real**: nombre del que transfiere puede no ser el paciente (paga un
  familiar) — no se puede cruzar solo por nombre, hay que combinarlo con monto
  + ventana de tiempo desde que el bot pidió el abono, y dejar el caso ambiguo
  para que recepción lo confirme a mano (mismo principio de "no adivinar" de
  este módulo).
- Requiere: (1) extender el poller de `email_ticker.py` para reconocer estos
  3 remitentes además de los 2 de Medilink, (2) un parser de HTML para
  Scotiabank/Banco de Chile (Falabella ya viene en texto plano), (3) una regla
  de negocio explícita de cuánta ambigüedad es aceptable antes de escalar a
  recepción en vez de auto-confirmar, (4) decisión del dueño sobre si
  auto-confirmar sin intervención humana o solo sugerir en el panel.
