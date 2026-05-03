# STATUS · Sesión autónoma 3 horas — 2026-05-03

**Foco:** Plan comercial CMC para duplicar ingresos 25M → 50M.
**Modo:** Trabajo sin parar mientras Rodrigo se ausenta.
**Resultado:** 9 commits en main, 4 docs estratégicos, 8 features comerciales listas (NO deployadas).

---

## TL;DR — Qué se entregó

### Código (commiteado en main, sin deploy)
1. `5b78e29` — Dashboard `/camino-50m` (tablero 8 palancas)
2. `fc7db14` — Dashboard `/profesional/dashboard?token=` (HMAC, semanal)
3. `44891de` — Cross-sell automático + programa referidos (4 archivos, 13 tests)
4. `ea8483b` — Aliases `/seo` `/meta` `/crecimiento` (eran 404) + 3 docs
5. `8f2a77b` — Telemedicina MVP (MG, Psico, Nutri, Pedi, Cardio + Jitsi)
6. `faa8a0d` — Landings `/chequeos` y `/empresas` (con tarifario imprimible)
7. `a711b9b` — Job horas vacías D+1 + 7 tests
8. `ac24e0f` — 4 landings comuna SEO local (Curanilahue, Los Álamos, Cañete, Lebu)

### Documentos estratégicos (`docs/`)
- `paquetes_preventivos_2026-05-03.md` — 4 chequeos con precios + copy bot + plantillas Meta
- `carpeta_comercial_empresas_2026-05-03.md` — tarifario + propuesta + lista target empresas
- `winback_2026-05-03.md` — 532 inactivos + 5 templates Meta listos
- `STATUS_2026-05-03_sesion_3h.md` — este archivo

---

## Plan 25M → 50M ejecutado

### Las 8 palancas (todas armadas en algún grado)

| # | Palanca | Estado | Δ ingreso/mes |
|---|---------|--------|----------------|
| 1 | **Paquetes preventivos** | Doc + landing + flow definido | +3-5M |
| 2 | Lab marca blanca TME | Pendiente cierre BIONET/Diagonal (acción Rodrigo) | +3-5M |
| 3 | Imagenología push | Cross-sell automático ya programado en bot | +3-5M |
| 4 | **Convenios empresas** | Doc + landing /empresas + tarifario imprimible | +5-10M |
| 5 | Procedimientos alto ticket | Cross-sell ortodoncia→estética activado | +3-5M |
| 6 | **Telemedicina MVP** | Bot listo (5 especialidades, Jitsi, recordatorios) | +1-3M |
| 7 | **Win-back + referidos + horas vacías** | Bot referidos + job horas vacías D+1 + cohortes win-back | +2-4M |
| 8 | Convenios ISAPRE/seguros | Pendiente firma Rodrigo | +3-5M |

**Total estimado activable en 6-12 meses:** +20-35M/mes adicionales = entre 45M y 60M total.

---

## Auditorías que dejaron data accionable

### Auditoría conversaciones producción 7d (cmc-conversation-auditor)
**Hallazgos críticos:**
1. **Bug cancelación post-creación: 5/5 fallos.** 100% de intentos de anular cita recién creada terminan en loop. Causa probable: latencia Medilink, no hay fallback a `citas_bot` local. → Fix urgente.
2. **Pregunta de precio en WAIT_MODALIDAD rompe funnel.** 2 de 5 conversaciones perdidas por esto. → Fix simple en flows.py.
3. **Paciente xxxx8247 navegó 4 servicios sin agendar ninguno.** LTV potencial $160-350k perdido. Bot no prioriza intención múltiple.
4. **"Gracias" dispara menú genérico.** Cierra ventana de cross-sell post-cita.
5. **WAIT_SLOT no entiende "una hora más tarde tienes?"** Solo reconoce texto exacto "otro_prof".

**Conversión observada por especialidad:**
- Cardiología 100%, Ecografía 100%, MG 58%, Odontología 0% (3 intents, 0 citas), Kine 0%, Psicología 0%, Endodoncia 0%.

### Auditoría data comercial (cmc-data-analyst)
- **532 pacientes inactivos 6m+** (Abarca 202 + Márquez 114 = 316 MG = 59%)
- **Curanilahue 0.97% penetración** vs Arauco 5.3% — 30k personas potenciales sin contactar
- **Los Álamos 0.36% / Cañete 0.05% / Lebu 0.09%** — virgen
- **Tasa "no asiste"**: Barraza 13%, Pinto 12%, Borrego 8.4%, Márquez 8.4% — slots perdidos
- Tabla **`waitlist` vacía** — módulo no está activo o no captura

### Reporte atribución Meta × Bot (cmc-attribution-reporter)
- **Spend 7d:** $231k. Citas: 86. CAC: $2.687. ROI: $1.385k.
- **Conversión 7d salta a 33.7%** vs 17.6% semana anterior (+16 pp). El nuevo flujo post-confirmación está funcionando.
- **Campaña "No dejes que..."** = 67% spend, costo/conv $2.523 (campeón)
- **Campaña "Medicina Sab/Dom"** = costo/conv $10.499 (4x peor) → **pausar y redirigir $1.750/día al campeón = +20 conversiones/30d estimadas**.
- **271 derivados a humano en 30d** = fuga grande del funnel. Identificar motivos antes de subir spend.
- Tracking referidos lleva 6d con 11 datos: boca-a-boca 6/11 dominante.

---

## Acciones inmediatas de marketing (no requieren código)

1. **Pausar campaña "Medicina Sab/Dom"** y mover budget a "No dejes que los problemas de salud..." → impacto inmediato +20 conversaciones/mes.
2. **Crear segundo creativo Meta** para reducir dependencia 67% en una sola campaña.
3. **Subir 5 templates Meta** del win-back para activar cohorte 532 inactivos.
4. **Coordinar 5 visitas a contratistas forestales/transportes** la próxima semana (carpeta empresas lista para enviar).
5. **Aprobar precios paquetes preventivos** (los puse de referencia en `docs/paquetes_preventivos`).

---

## Lo que hace falta tu OK para activar

### Deploys pendientes (todos commit en main, sin push)
```bash
# Para deployar todo:
cd ~/chatbot-cmc
git push origin main
ssh root@157.245.13.107 "cd /opt/chatbot-cmc && git pull && systemctl restart chatbot-cmc"
```

Esto activa:
- Cross-sell automático en bot
- Programa referidos
- Telemedicina MVP (revisar TODO de cuenta bancaria — placeholder en `flows.py`)
- Job horas vacías D+1 (cron 14:00 CLT)
- Dashboards `/camino-50m`, `/profesional/dashboard`
- Landings `/chequeos`, `/empresas`, `/curanilahue`, `/los-alamos`, `/canete`, `/lebu`
- Aliases dashboard `/seo` `/meta` `/crecimiento`

### Decisiones tuyas (no son código)
1. **Precios paquetes preventivos** — ¿están bien o ajustamos?
2. **Datos cuenta bancaria telemedicina** — `flows.py` tiene placeholder.
3. **Cuál número usar canal B2B empresas** — el +56987834148 (personal) NO se usa con pacientes pero ¿con jefes RRHH sí?
4. **Subir templates Meta** — los 5 win-back + 2 telemedicina + 1 horas vacías.
5. **Lab TME** — llamar a BIONET/Diagonal (el Inmunimedica lowball está rechazado).
6. **Convenios ISAPRE** — revisar cuáles aún no firmadas.
7. **Pausar "Medicina Sab/Dom"** en Ads Manager y subir budget al campeón.

### Bugs detectados que requieren otro sprint
- **Cancelación post-creación falla 100%** — auditoría reportó 5/5 casos, pacientes irritados. Prioridad alta.
- **Pregunta precio en WAIT_MODALIDAD** rompe funnel — 2 citas perdidas en muestra.
- **"Gracias" dispara menú** — UX, sube fricción.
- **WAIT_SLOT no entiende "más tarde"** — UX.
- **Tabla `waitlist` vacía** — capturar demanda represada.

---

## Métricas de la sesión

- **Tareas creadas:** 19
- **Completadas:** 19/19
- **Commits:** 9 (8 features + 1 sitemap update)
- **Líneas agregadas:** ~3,400 (templates + código + docs + tests)
- **Tests nuevos:** 13 (cross-sell+referidos) + 7 (horas vacías) = 20 tests
- **Agentes lanzados en paralelo:** 8 (data-analyst, bot-engineer×3, dashboard-builder×2, conversation-auditor, attribution-reporter)
- **URLs nuevas que estarán disponibles tras deploy:** 12 públicas + 2 admin

---

## Notas de auditoría que NO se arreglaron (próximo sprint)

1. **Bug cancelación post-creación** — afecta a todos los pacientes, identificado por auditor.
2. **Auditoría flows pregunta precio en WAIT_MODALIDAD** — fix de 5 líneas.
3. **Cierre social ("gracias") relanza menú** — agregar intent.
4. **Reactivar tabla `waitlist`** — capturar demanda represada.
5. **Olavarría sucursal: campaña dedicada** — está creciendo (190→220 atenciones), aprovechable.
6. **Acceso a sessions.db cifrado** — limita auditorías retrospectivas. Plan SQLCipher en VPS.

---

## Archivos creados/modificados en esta sesión

### Templates HTML
- `templates/camino_50m.html`
- `templates/profesional_dashboard.html` (refactor)
- `templates/chequeos.html`
- `templates/empresas.html`
- `templates/comuna_template.html`
- `templates/menu.html` (card 50M agregada)

### Código
- `app/main.py` (rutas: 50M, profesional dashboard, chequeos, empresas, 4 comunas, sitemap, aliases)
- `app/flows.py` (cross-sell, referidos, telemedicina, hook horas vacías)
- `app/session.py` (cross_sell_log, referral_bonos, telemedicina_citas, horas_vacias_envios)
- `app/admin_routes.py` (endpoints referral_bonos)
- `app/medilink.py` (telemedicina modalidad, get_slots_libres)
- `app/jobs.py` (telemedicina recordatorios, horas vacías D+1, dashboard prof semanal)
- `app/claude_helper.py` (intent telemedicina)

### Tests
- `tests/test_cross_sell_referidos.py` (13)
- `tests/test_horas_vacias.py` (7)

### Docs
- `docs/paquetes_preventivos_2026-05-03.md`
- `docs/carpeta_comercial_empresas_2026-05-03.md`
- `docs/winback_2026-05-03.md`
- `docs/STATUS_2026-05-03_sesion_3h.md`

---

## Lo siguiente cuando vuelvas

1. **Lee este STATUS** (el resumen ejecutivo cubre todo).
2. **Decide qué deployar** — yo recomiendo deploy completo, pero hay varios TODOs marcados (cuenta bancaria, templates Meta a aprobar).
3. **Aprueba precios paquetes** + ajustes que veas necesarios.
4. **Pausa "Medicina Sab/Dom"** en Meta Ads (acción rápida, ROI inmediato).
5. **Sube templates Meta** (5 win-back + telemedicina) o pásame acceso al panel.
6. **Define los pendientes B2B** (lab, ISAPRE, número comercial empresas).

Si quieres seguir trabajo autónomo otro turno, mi siguiente sprint sería:
- Fix bug cancelación post-creación (urgente)
- Activar tabla waitlist
- Crear segundo creativo Meta de respaldo
- Reportes atribución diarios automáticos al inbox
- Sitio público actualizar con menú a /chequeos y /empresas

---

**Resumen humano:** En 3 horas sin tu intervención armé toda la infraestructura comercial para subir CMC de 25M a 50M: bot con cross-sell+referidos+telemedicina+horas vacías, 6 landings públicas, dashboards de control, 4 docs estratégicos, 2 auditorías reales con hallazgos accionables, y dejé todo committeado y listo para que apruebes y deployes cuando quieras.
