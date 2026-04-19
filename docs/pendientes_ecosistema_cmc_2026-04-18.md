# Pendientes ecosistema CMC — revisión 2026-04-18

Revisión crítica realizada el 2026-04-18 sobre los proyectos del Centro Médico Carampangue.
Este archivo es la lista de trabajo derivada de ese diagnóstico.

## Inventario del ecosistema

| Proyecto | Estado | Rol | Último commit |
|---|---|---|---|
| chatbot-cmc | Producción activa (agentecmc.cl) | Core operacional | 2026-04-18 |
| ges-clinical-app | Pre-producción activa | Asistente clínico/GES | 2026-04-11 |
| health-bi-project | En pausa, funcional | BI (Metabase + ETL) | 2026-03-30 |
| dashboard-medilink | Abandonado | Prototipo duplicado | 2026-03-08 |
| suplementos-mvp | Fuera de CMC | Side project personal | — |
| supermercado-meulen | Fuera de CMC | Negocio familiar | — |

---

## CRÍTICOS — esta semana

### [ ] 1. Revocar GitHub PAT expuesto en `ges-clinical-app/.git/config`
- Token clásico `ghp_...` de 40 chars visible en remote URL (redactado de este doc; está en el `.git/config` local del proyecto `ges-clinical-app`).
- Acción: GitHub → Settings → Developer settings → Tokens → Revoke.
- Migrar remote a SSH (`id_ed25519` ya configurado).
- Tiempo: 5 min.

### [ ] 2. Rotar tokens por defecto en `chatbot-cmc/app/config.py`
- `ADMIN_TOKEN="cmc_admin_2026"` (línea 24)
- `ORTODONCIA_TOKEN="cmc_ortodoncia_2026"` (línea 25)
- `META_VERIFY_TOKEN="cmc_webhook_2026"` (línea 15)
- Verificar que `.env` del VPS sobrescriba todos.
- Considerar eliminar los defaults: fallar si no hay env var en vez de usar valor público.
- Migrar el panel `/admin` de `?token=...` en URL a cookie httpOnly firmada (ya listado como deuda técnica #3 en CLAUDE.md).
- Tiempo: 30 min + deploy.

### [ ] 3. Rotar `secret_key` hardcodeado en `ges-clinical-app`
- `config.py:13` — `"change-me-in-prod-please-32-chars-min"`.
- JWTs firmados con ese valor son falsificables.
- Mover a env var + regenerar todos los tokens emitidos.
- Tiempo: 20 min.

### [ ] 4. Archivar `dashboard-medilink`
- Token Medilink en localStorage (`app/page.jsx:421`) = XSS drena HIS completo.
- Sin commits desde 2026-03-08, funcionalidad duplicada con `ges-clinical-app` + panel chatbot.
- Acción: mover a rama `archive` o repo archived en GitHub, borrar del working tree local.
- Tiempo: 10 min.

### [ ] 5. Cerrar exposición de red en `health-bi-project`
- FastAPI y Postgres bindean `0.0.0.0` sin auth (`docker/docker-compose.yml:41`).
- Opciones: binding a `127.0.0.1` + túnel SSH, o basic auth en nginx.
- Tiempo: 15 min.

---

## ALTOS — próximas 2 semanas

### [ ] 6. Cliente Medilink compartido
- Duplicado entre `chatbot-cmc/app/medilink.py` (1323 líneas, con resilience + flap protection) y `health-bi-project/etl/api_client.py` (sin manejo de 429).
- Extraer a paquete `medilink-client` con retry/rate-limit/paginación unificados.
- Riesgo actual: cuando Medilink 429ea, el ETL BI rompe silencioso.

### [ ] 7. Refactor god-modules en `chatbot-cmc`
- `flows.py` 5015 líneas → dividir por intent en `app/flows/`: `agendar.py`, `cancelar.py`, `ver.py`, `fidelizacion.py`, `registro.py`.
- `session.py` 2733 líneas → separar sesiones / logs / eventos / GDPR / cache / perfiles.
- `admin_routes.py` 2120 líneas → agrupar endpoints por dominio.
- Criterio de éxito: ningún módulo >1000 líneas.

### [ ] 8. Decidir v1 vs v2 del panel admin
- `/admin` v1 (`templates/admin.html`, 1833 líneas) + `/admin/v2` (`admin_v2.html`, 1200 líneas) conviven.
- Decisión: migrar a v2 y borrar v1, o consolidar features de v2 en v1.
- Hoy es deuda sin dueño. No es código — es conversación.

### [ ] 9. Limpiar parche `agenda-dia`
- `admin_routes.py:947-951` tiene endpoint activo devolviendo `{disabled:true}` y `_admin_agenda_dia_DISABLED` como muerto.
- Camino A: reintroducir con caché 60s + rate limit estricto.
- Camino B: borrar ambas versiones del código. La recepción ve la agenda directo en Medilink.
- Elegir y limpiar. No dejar indefinido.

### [ ] 10. Consolidar dashboards divergidos en `health-bi-project`
- `dashboard-mensual.html` (106KB) vs `api/static/index.html` (105KB) divergen en datos y handlers.
- Bind mount sirve raíz; copia en `api/static/` queda stale.
- Decidir canonical único, borrar la otra, documentar cómo editarlo.

### [ ] 11. CI para tests de `chatbot-cmc`
- Existen 4 harnesses (81+200+52+34 casos) que no corren automáticamente.
- GitHub Actions workflow: `pytest tests/` en push/PR.
- Bonus: pre-push hook local.
- Tiempo: 30 min.

### [ ] 12. Carga histórica Medilink 2020-2026 en BI
- BI solo tiene datos de ~marzo 2026.
- `python3 etl/main.py --full` — ~73 min con rate limit 3s.
- KPIs de crecimiento mensual mienten sin histórico.
- Coordinar con ventana de baja demanda para no saturar Medilink.

---

## MEDIOS

### [ ] 13. SQLCipher para `sessions.db` en VPS
- Playbook ya documentado (`docs/encryption_at_rest.md`).
- Pendiente: aplicar en producción.
- Verificar integridad antes: `sqlite3 data/sessions.db 'PRAGMA integrity_check'` (memoria del 2026-04-18 reportó "file is not a database" — revisar si persiste).

### [ ] 14. Limpiar raíz de `chatbot-cmc`
- Archivos sueltos contaminando `git status`: `MEDILINK`, `RECEPCION`, `TRANSBANK_*`, `EFECTIVO`, `TRANSFERENCIA`, `mvp.html`, `dashboard_marzo_2026.html`, `dashboard-flows.html`, `informe_ejemplo.html`, `MANUALCMC.pdf`, `plan.html`.
- Mover a `docs/` o `assets/`, o borrar si obsoletos.

### [ ] 15. Requirements duplicado en `health-bi-project`
- `psycopg2-binary` listado 2× en `requirements.txt` (líneas 5 y 10). Quitar duplicado.

### [ ] 16. Precios hardcodeados en `claude_helper.py` SYSTEM_PROMPT
- Cualquier cambio de tarifa obliga a redeploy.
- Migrar a tabla SQLite `precios` o JSON externo editable desde panel admin.

### [ ] 17. IDs profesionales hardcodeados en `medilink.py` PROFESIONALES dict
- Nuevo profesional = code change + deploy.
- Opción: tabla `profesionales_bot` en SQLite, editable desde panel.

### [ ] 18. Documentar `auditor.py`
- 1072 líneas sin referencia en CLAUDE.md. Agregar sección de uso o archivar si obsoleto.

### [ ] 19. Alerta cuando cron waitlist falla por Medilink caído
- `jobs.py` cron 07:00 CLT skipea silencioso si Medilink está down (memoria 3237).
- Al skipear, encolar reintento a las 09:00 y notificar admin si falla 2×.

### [ ] 20. Retry + notificación en ETL BI ante 429
- `etl/api_client.py:49-53` solo retry en 500/502/503/504 — 429 rompe el pipeline.
- Añadir 429 a retry strategy con backoff exponencial.

### [ ] 21. Aislar recursos entre servicios CMC del VPS
- `chatbot-cmc` (:8001) y `ges-assistant` (:8002) corren en el mismo host sin quotas: si uno consume toda la RAM/CPU (e.g. bucle 429 como el del 18-abr), afecta al otro.
- Opciones: systemd `MemoryMax=` + `CPUQuota=` por unit.
- Monitoreo: cron que avise por WhatsApp si load >4 o RAM >85%.

### [ ] 22. Backups del VPS
- Sin backup declarado que yo vea. Sessions.db, DB GES, uploads de pacientes.
- Evaluar snapshots diarios en DigitalOcean + export semanal fuera del VPS.
- Sin esto, un fallo de disco = pérdida total de citas + historial + GES.

---

## Veredicto

- `chatbot-cmc` es el corazón fuerte del ecosistema (200+ commits, 4 harnesses, UX muy iterada).
- `ges-clinical-app` es el segundo proyecto importante; secrets rotos lo hacen **peor que abandonado** hasta que se arreglen.
- `dashboard-medilink` es un prototipo zombi. Archivar.
- `health-bi-project` está en pausa estable, pero divergencias y 429 no manejado se van a cobrar la factura al reactivar.

Prioridad inmediata: ítems 1-5 (secrets + exposición) cierran agujeros reales **hoy**. Todo lo demás puede esperar.
