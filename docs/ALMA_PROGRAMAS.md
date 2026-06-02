# Alma — Motor de Programas Clínicos

Módulo `/alma/programas` (route `app/programas.py`, template `templates/alma_programas.html`).
Construido 2026-06-02. Refuerza las especialidades del CMC convirtiendo el historial de
atenciones en **acción de reenganche**: a quién contactar para que vuelva cuando debe.

La plata recurrente de un centro médico no está en captar, está en que el paciente
**complete su tratamiento** o **vuelva a su control**. Este motor detecta a quién se está
enfriando y arma la lista accionable, con el mensaje de WhatsApp listo.

## Arquitectura — un motor, muchos programas

En vez de un módulo por especialidad, hay **un motor genérico** parametrizado por config.
Agregar una especialidad = agregar una entrada de diccionario, sin código nuevo.

### Tipos de programa

| Tipo | Qué mira | Estados | Ejemplos |
|------|----------|---------|----------|
| `adherencia` | Tratamiento multi-sesión. Detecta **episodios** (un hueco > `gap_nuevo` días abre uno nuevo) y clasifica por días desde la última sesión vs cadencia esperada. | en_curso · **riesgo** · abandono · completado · alta · cerrado | Nutrición, Psicología, Fonoaudiología, Odontología |
| `control` | Recall periódico. El paciente debería volver cada N días. | al_dia · **pronto** · **vencido** | Cardiología, Gastro, Ginecología, Matrona, Podología, Traumatología, Medicina General |
| `tamizaje` | Cohorte poblacional por edad/sexo (no por historial de tratamiento). A quién le corresponde un control preventivo que no tiene. | **lapsed** (tuvo control, se atrasó — alta confianza) · nunca (sin registro — validar) | PAP, EMPAM, Chequeo cardiovascular |

Los buckets **accionables** (worklist) por tipo están en `ACCIONABLES`.

### Configs
- `PROGRAMAS` — 11 programas de tratamiento (adherencia/control), filtran `bi.fact_atenciones` por `especialidad_ids`.
- `TAMIZAJE` — 3 cohortes preventivas, consultan toda `bi.dim_paciente` por edad/género + último control relevante.

Medicina General usa `especialidad_ids = [1, 10]` (Medilink desdobla "General" e "Medicina General"; el ETL ya unifica el nombre — ver `health-bi-project/etl/transform.py::_canon_especialidad`).

## Fuente de datos
- **BI Postgres** (`bi_helper.bi_query`): `fact_atenciones` + `dim_paciente` (incl. teléfono para wa.me) + `dim_profesional` + `fact_ingresos` (monto). Degradación elegante: si la BI cae → vacío + `source_status="bi_unavailable"`, nunca 500.
- **sessions.db** tabla `programa_plan` (programa, paciente_id): plan de sesiones, estado manual (alta/pausa/no_contactar), notas. Llaveada por `bi.paciente_id`.

## Valor agéntico
- **Next-best-action** (`GET /{prog}/accion/{paciente_id}`): redacta el mensaje WhatsApp personalizado.
- **Lista de hoy** (`build_digest` → `GET /_/digest`): cola unificada y priorizada cruzando los 11 programas + Kine + Ortodoncia. Expuesta también como tool `get_worklist_hoy` del **Copilot Alma** (`alma_brain/tools.py`).
- **Resumen / portfolio** (`build_overview` → `GET /_/overview`): todos los programas de un vistazo, ranqueados por **ingreso recuperable** (accionables × ticket/valor promedio). El tablero ejecutivo.
- **KPI ingreso recuperable**: plata estimada que se rescata si se trabaja la worklist.

## Endpoints
```
GET  /alma/programas                       página (shell)
GET  /alma/api/programas                   lista de programas (selector, con categoría)
GET  /alma/api/programas/{prog}/resumen    KPIs del programa
GET  /alma/api/programas/{prog}/pacientes  worklist (con wa link); tamizaje capado a 500
GET  /alma/api/programas/{prog}/accion/{paciente_id}   mensaje WhatsApp sugerido
PUT  /alma/api/programas/{prog}/plan/{paciente_id}     plan/estado manual
GET  /alma/api/programas/{prog}/export     CSV
GET  /alma/api/programas/_/digest          lista de hoy unificada
GET  /alma/api/programas/_/overview        portfolio ranqueado
```

## Umbrales (clínicamente razonables, editables en la config)
- Adherencia: `en_curso_max` / `riesgo_max` / `abandono_max` / `gap_nuevo`.
- Control: `control_ok` / `control_due` (días desde la última visita).
- Tamizaje: `edad_min` / `edad_max` / `genero` / `esp_recall` (especialidades que cuentan como "control hecho") / `dias` (ventana).

## Tests
`tests/test_alma_programas.py` — adherencia, control, tamizaje, wa link, validez de configs. Sin BI (mocks).

## Reporte standalone
`python app/programas_report.py [--json] [--top N] [--programa kine]` — la lista de hoy por consola/cron, sin tocar el bot.

## Caveat de datos (tamizaje PAP)
La cohorte PAP salió 100% "nunca" en la validación — puede ser real (pacientes que solo vinieron por otras especialidades) o un gap de backfill de atenciones históricas de matrona/gineco. **Validar una muestra contra Medilink antes de hacer outreach masivo de PAP.** Por eso el motor prioriza "lapsed" (alta confianza) sobre "nunca".

## Estado
LOCAL OK + tests verdes + SQL validado contra BI viva (kine 1.663 filas, orto 583, cohortes PAP 3.931 / EMPAM 1.502 / CV 5.286). **SIN commit, SIN deploy** — pendiente revisión de Rodrigo. Deploy con `git add` selectivo (hubo sesiones paralelas en el árbol).
