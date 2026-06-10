# RUNBOOK — Centro Médico Carampangue (CMC)

## Brecha de datos personales — Procedimiento 72h (Ley 21.719 Art. 49)

### 1. Clasificación de brecha

Una brecha de datos de salud es cualquier acceso, divulgación, alteración o destrucción no autorizada de datos personales tratados por el CMC. Clasificar por impacto:

**Alto riesgo** (notificar a afectados + Agencia dentro de 72h):
- Exposicion de fichas clinicas, diagnosticos, RUT, datos de pago o historial de citas de pacientes identificables
- Acceso no autorizado a la base de sesiones (`sessions.db`) o backups
- Filtracion del token de administracion (`ADMIN_TOKEN`, `MEDILINK_TOKEN`, `META_ACCESS_TOKEN`)
- Volcado de la tabla `privacy_consents` o `gdpr_deletions`
- Compromiso del servidor VPS (acceso root no autorizado)

**Bajo riesgo** (registro interno + evaluacion, sin notificacion externa obligatoria):
- Acceso a logs de nginx ya redactados (PII eliminada por el mapa de redaccion)
- Exposicion de estadisticas agregadas sin datos identificables
- Error de configuracion corregido antes de que haya evidencia de acceso externo

---

### 2. Checklist primeras 24h

**Contener (hacer primero, antes de analizar):**
- [ ] Si el servidor esta comprometido: `systemctl stop chatbot-cmc` en el VPS para detener escritura de nuevos datos
- [ ] Revocar credenciales expuestas: rotar `ADMIN_TOKEN`, `MEDILINK_TOKEN`, `META_ACCESS_TOKEN`, `ANTHROPIC_API_KEY` en `.env` del VPS y en Meta / Medilink / Anthropic
- [ ] Si se expuso un backup: eliminar el archivo del lugar publico y rotar la clave de cifrado
- [ ] Preservar evidencia: copiar logs antes de rotar (`cp /var/log/cmc-bot.log /root/brecha-$(date +%Y%m%d).log`) y logs de nginx (`cp /var/log/nginx/agentecmc-access.log /root/`)

**Evaluar alcance:**
- [ ] Identificar el vector (nginx access log, git history, exposicion de URL con token, acceso SSH, etc.)
- [ ] Determinar el periodo de exposicion (desde cuando hasta cuando)
- [ ] Listar tablas / campos afectados en `sessions.db`: `conversations`, `messages`, `contact_profiles`, `privacy_consents`, `citas_bot`
- [ ] Contar pacientes afectados (consulta SQL en `sessions.db` o backup)
- [ ] Documentar hallazgos en un archivo privado fechado (NO commitear al repo)

---

### 3. Notificacion a la Agencia (dentro de 72h desde deteccion)

**Agencia de Proteccion de Datos Personales de Chile**
- Sitio: https://www.agenciadp.cl
- Canal de notificacion de brechas: formulario en linea o correo indicado en el sitio
- Plazo: 72 horas desde que se tiene conocimiento de la brecha (Art. 49 Ley 21.719)
- Si el plazo no permite reunir todos los datos, se notifica lo conocido y se complementa despues

**Datos minimos que debe incluir la notificacion:**
1. Identidad y datos de contacto del responsable del tratamiento (Dr. Rodrigo Olavarria, medico director, Centro Medico Carampangue)
2. Naturaleza de la brecha (acceso / divulgacion / alteracion / destruccion)
3. Categorias y numero aproximado de titulares afectados
4. Categorias y numero aproximado de registros afectados
5. Consecuencias probables de la brecha
6. Medidas adoptadas o propuestas para remediar

**Notificacion a afectados** (si alto riesgo, Art. 49 inc. 3):
- Comunicar a cada paciente afectado de forma directa (WhatsApp o, si no hay canal, por el medio disponible)
- Informar: que datos se vieron comprometidos, que riesgo implica, que medidas tomo el CMC, como contactar al responsable
- No hay plazo fijo en la ley para afectados, pero debe ser "sin demora indebida"

---

### 4. Plantilla de notificacion a la Agencia

```
NOTIFICACION DE BRECHA DE DATOS PERSONALES
Fecha: [DD/MM/YYYY HH:MM]
Responsable del tratamiento: Centro Medico Carampangue
Representante: Dr. Rodrigo Olavarria (medico director)
Contacto: (41) 296 5226 / +56966610737

1. NATURALEZA DE LA BRECHA
[Describir: acceso no autorizado / divulgacion / perdida / alteracion]
Vector identificado: [descripcion tecnica breve]

2. DATOS AFECTADOS
Categorias: [ej. nombre, RUT, diagnostico, historial de citas, datos de contacto]
Numero aproximado de titulares: [N]
Periodo de exposicion: [desde] hasta [hasta]

3. CONSECUENCIAS PROBABLES
[Ej. riesgo de suplantacion de identidad, exposicion de informacion de salud sensible]

4. MEDIDAS ADOPTADAS
- Contencion: [ej. servicio detenido, credenciales rotadas]
- Remediacion: [ej. parche aplicado, backup restaurado]
- Prevencion futura: [ej. cifrado adicional, rotacion periodica de tokens]

5. INFORMACION COMPLEMENTARIA
[Se adjunta / se enviara en plazo de X dias si la investigacion sigue en curso]
```

---

### 5. Responsable

El responsable del tratamiento de datos personales del CMC es el **medico director (Dr. Rodrigo Olavarria)**. Toda decision de notificacion externa requiere su autorizacion. El equipo tecnico (Adkun) ejecuta la contencion tecnica y prepara el borrador de notificacion.

---

### 6. Contactos de emergencia

| Rol | Contacto |
|-----|----------|
| Medico director | +56987834148 (numero personal, no customer-facing) |
| Soporte tecnico (Adkun) | rodrigo.fernando.mdv@gmail.com |
| Servidor VPS | `ssh root@157.245.13.107` (llave Ed25519) |
| Meta (incidente token) | https://business.facebook.com → System Users → revocar token |
| Medilink (incidente token) | Soporte Healthatom |

---

## Recuperacion del servidor tras incidente

Ver procedimiento completo en `CLAUDE.md` seccion "Deploy en produccion".

### Rollback de emergencia
```bash
ssh root@157.245.13.107
systemctl stop chatbot-cmc
cd /opt/chatbot-cmc
git log --oneline -5          # identificar commit bueno
git checkout <commit-bueno>   # solo si hay razon clara
systemctl start chatbot-cmc
curl -s https://agentecmc.cl/health
```

### Restaurar backup de sesiones
```bash
# Backups diarios en /opt/backups/chatbot-cmc/
ls /opt/backups/chatbot-cmc/
cp /opt/chatbot-cmc/data/sessions.db /opt/chatbot-cmc/data/sessions.db.pre-restore
gunzip -c /opt/backups/chatbot-cmc/sessions_YYYYMMDD_HHMMSS.db.gz > /opt/chatbot-cmc/data/sessions.db
systemctl restart chatbot-cmc
```
