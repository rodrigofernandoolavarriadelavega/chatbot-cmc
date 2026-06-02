# Alma — Handoff de la construcción nocturna (2026-06-02)

> Para Rodrigo, al despertar. Resumen de TODO lo construido en la noche, cómo revisarlo,
> y el orden seguro para encenderlo. **NADA está commiteado. NADA está desplegado. Todo OFF.**

## TL;DR

Se construyó la capa **operativa y agéntica de Alma** sobre el cerebro existente:
- **Fase 4 (Alma operativa)**: cancelación de hora → oferta a lista de espera → reserva provisional → confirmación (auto bajo riesgo o recepción).
- **23 orquestadores** (sense→propose→gobernar) cubriendo agenda, especialidades, finanzas, demanda, inventario, fidelización, reputación, clínico, datos, marketing y atención.
- **Panel** `/alma/orquestadores`: briefing del día + catálogo con dry-run en vivo + inbox aprobar/descartar.
- **Snapshot + briefing + CLI** para correr el dry-run por cron y ver el resumen matinal al instante.
- **31 tests offline verdes en 4 suites** (`python3 tests/run_alma_suite.py`), incl. un test de **integración end-to-end** (TestClient) que prueba endpoints + página con auth real.

Con todos los flags apagados (estado actual), **el bot se comporta idéntico a hoy.**

## Cómo revisar (5 min)

```bash
cd ~/chatbot-cmc
python3 tests/run_alma_suite.py        # las 3 suites Alma → debe decir "TODO VERDE"
git status --short                      # ver el diff (OJO: hay WIP de sesiones paralelas mezclado)
```
El catálogo y la bitácora completa están en `docs/ALMA_ORCHESTRATORS.md`.

**Abrir el panel** (cuando el bot corra): `https://agentecmc.cl/alma/orquestadores?token=cmc_admin_2026`
(o en local `http://127.0.0.1:8001/alma/orquestadores?token=cmc_admin_2026`). Verás el Briefing del día,
el catálogo filtrable por dominio con dry-run en vivo, y el inbox de propuestas. Todo en modo lectura
mientras los flags estén apagados.

## Archivos de la noche

**Nuevos** (Fase 4 + orquestadores):
- `app/alma_brain/operativa.py` — Fase 4 (relleno de cupos, claim atómico, política).
- `app/alma_brain/orchestrators/` — `base.py`, `__init__.py`, `snapshot.py`, `briefing.py` + **21 orquestadores** (confirmaciones, hueco_proactivo, no_show_recovery, adherencia_kine, controles_ortodoncia, cobranza_ortodoncia, demanda_abrir_agenda, inventario_compra, reactivacion, reputacion_nps, resultados_examenes, preventivo_edad, cumpleanos, crosssell_postconsulta, campanas_estacionales, agenda_salud, ges_backlog, primera_vez_sin_retorno, control_cronico, ficha_incompleta, ads_anomalia, referral_sin_cerrar, conversacion_parada).
- `templates/alma_orquestadores.html` — panel.
- `scripts/register_oferta_cupo_template.py` — registra el template Meta (ya corrido, PENDING).
- `scripts/alma_orq_snapshot.py` — CLI del snapshot (para cron).
- `tests/test_alma_operativa.py`, `tests/test_alma_orchestrators.py`, `tests/run_alma_suite.py`.
- `docs/ALMA_ORCHESTRATORS.md`, `docs/ALMA_NOCHE_HANDOFF.md`.

**Modificados** (aditivo):
- `app/session.py` — tablas `waitlist_offers`, `resultados_pendientes` + helpers; `get_promotores_recientes`, `get_cronicos_para_control`.
- `app/alma_brain/policy.py` — `should_auto_confirm` + `OfferContext` (dentro del paquete alma_brain, que es WIP no commiteado de otra sesión).
- `app/jobs.py` — hook de cancelación (dispara Fase 4) + expiry de holds.
- `app/flows.py` — handler de aceptación de cupo en IDLE.
- `app/admin_routes.py` — endpoints operativa + orquestadores + ruta de página `/alma/orquestadores`.
- `tests/test_alma_brain.py` — tests de `should_auto_confirm`.
- `CLAUDE.md` — entradas de sesión.

## Orden SEGURO de encendido (cuando decidas, NO antes de Fase 0)

Todo está apagado con kill-switches dedicados. Encender de a uno y observar:

1. **Mirar sin actuar** (cero riesgo): entra a `/alma/orquestadores`, dale "Recalcular sugerencias", revisá los dry-run de cada orquestador. Esto NO contacta a nadie ni escribe nada, aunque todo esté apagado.
2. **Fase 4, paso 1 — invitar sin escribir Medilink**: `ALMA_OPERATIVA_ENABLED=true`. Ahora una cancelación ofrece el cupo a la lista de espera y, cuando alguien acepta, queda en **recepción** (hold blando, NO toca Medilink). Validá manualmente un tiempo.
3. **Fase 4, paso 2 — auto-confirmar bajo riesgo**: recién cuando confíes, `ALMA_OPERATIVA_AUTOCONFIRM=true`. Ahora el sistema crea la cita en Medilink solo para los casos de bajo riesgo (paciente conocido, especialidad coincide, margen de horas). El resto sigue cayendo a recepción.
4. **Orquestadores, uno a uno**: `ALMA_ORQ_<NAME>_ENABLED=true` para el que quieras activar en modo propose (encola worklists para que recepción las accione). Empezá por los que solo proponen worklists (no auto-contactan): p. ej. `ALMA_ORQ_CONFIRMACIONES_ENABLED`, `ALMA_ORQ_CONTROL_CRONICO_ENABLED`.
5. **Template Meta**: poné `USE_TEMPLATES=true` solo cuando `oferta_cupo` esté **APPROVED** en Meta (hoy PENDING) — si no, la invitación fuera de la ventana de 24h no entrega.

> Regla de oro: encendé **una** cosa, observá 24–48h, después la siguiente.

## Qué falta antes de encender escrituras (Fase 0 — bloqueante)

GPT lo marcó bien y el código lo respeta: **antes de que Alma escriba en Medilink sin humano** (auto-confirm) conviene tener Fase 0:
- Sacar el **token de la URL** (hoy `?token=...` queda en el historial del navegador).
- **Login real** por usuario (no token compartido) + cookie httpOnly firmada.
- **Bitácora de auditoría** de acciones (quién aprobó/ejecutó qué y cuándo) — el decision log de alma_brain ya cubre las propuestas; falta el de acciones de panel.

Mientras Fase 0 no esté, dejá `ALMA_OPERATIVA_AUTOCONFIRM=false` (todo a recepción) y los orquestadores en modo propose (nunca execute).

## Cierre de la noche

**Totales (10 iteraciones autónomas):**
- **Fase 4 (Alma operativa)** completa + **23 orquestadores** en 12 dominios (agenda, atención, clínico, datos, demanda, fidelización, finanzas, inventario, kinesiología, marketing, ortodoncia, reputación).
- Chasis (`base`/`__init__`/`snapshot`/`briefing`) + panel `/alma/orquestadores` (briefing + filtro por dominio + dry-run en vivo + inbox) + CLI + métricas.
- **~3.200 líneas** de código nuevo + ~6 archivos modificados de forma aditiva.
- **~32 tests unitarios + 1 suite de integración** (TestClient), 4 suites, todas verdes.
- **Todo gateado OFF. Nada commiteado. Nada desplegado.** Con los flags apagados el bot se comporta idéntico a hoy.

**Verificación**: `python3 tests/run_alma_suite.py` → debe decir "TODO VERDE — las 4 suites Alma pasan".

### Backlog de orquestadores futuros (necesitan infra de datos nueva)

No se construyeron porque dependen de datos que aún no existen limpios o que viven en WIP de otras sesiones. Cuando esa infra se estabilice, son candidatos directos al mismo chasis:

| Candidato | Qué haría | Qué data falta |
|-----------|-----------|----------------|
| `pago_pendiente_consulta` | atenciones sin copago registrado → cobrar/registrar | que `/alma/pagos` (WIP) se formalice y el copago esperado vs registrado sea consultable |
| `interconsulta_sin_respuesta` | interconsultas emitidas sin respuesta del especialista → seguir | módulo Interconsultas (WIP de otra sesión) con estado/fecha |
| `esterilizacion_ciclo` | ciclos de esterilización por vencer / sin registro → alertar (SEREMI) | módulo Esterilización (WIP) con fechas de ciclo y vencimiento |
| `pni_segunda_dosis` | niños con 1ª dosis que deben 2ª → recordar | un **tracker de dosis administradas** (no existe; CMC define si vacuna) |
| `boxes_ociosidad` | box infrautilizado por franja → reasignar/ofertar | el modelo de ocupación real de `/boxes` (vive en el proyecto BI, no en el bot) |

> Estos NO se forzaron a propósito: un orquestador sin data sólida solo genera ruido. Mejor 23 que proponen señal real que 28 con 5 vacíos.

## Sobre el commit

**No commiteé nada** (regla de la sesión). El working tree tiene **WIP de varias sesiones paralelas** mezclado (módulos clínicos, kine/orto, alma_brain). Para commitear lo de la noche hay que hacer **`git add` selectivo** de los archivos listados arriba — un `git add .` mezclaría trabajo de otras sesiones. El paquete `app/alma_brain/` completo es WIP no commiteado (de la sesión Alma Brain); mis aportes viven dentro.
