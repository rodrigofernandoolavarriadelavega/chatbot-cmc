# RUNBOOK OPERATIVO — Chatbot CMC
# Centro Médico Carampangue — agentecmc.cl
# Destinatario: agente Claude Code o Rodrigo con poco contexto disponible
# Escrito con datos reales del servidor (verificados 2026-05-29)

---

## DATOS DE ACCESO RAPIDO

| Campo | Valor |
|-------|-------|
| Servidor | 157.245.13.107 (DigitalOcean) |
| Ruta app | /opt/chatbot-cmc/ |
| Servicio systemd | chatbot-cmc.service |
| Puerto uvicorn | 8001 |
| Health check | https://agentecmc.cl/health |
| Log principal | /var/log/cmc-bot.log |
| .env | /opt/chatbot-cmc/.env |
| Acceso SSH | sshpass -p '<PASSWORD>' ssh root@157.245.13.107 |

La contrasena del servidor NO se documenta aqui. Esta en las credenciales conocidas del agente / en el archivo de sistema de Claude.

**Restart rapido del servicio:**
```
sshpass -p '<PASSWORD>' ssh root@157.245.13.107 "systemctl restart chatbot-cmc"
```

**Logs en vivo:**
```
sshpass -p '<PASSWORD>' ssh root@157.245.13.107 "journalctl -u chatbot-cmc -f"
```

**Deploy estandar (desde Mac, repo local /Users/rodrigoolavarria/chatbot-cmc):**
```
cd /Users/rodrigoolavarria/chatbot-cmc && git push origin main && sshpass -p '<PASSWORD>' ssh root@157.245.13.107 "cd /opt/chatbot-cmc && git pull && systemctl restart chatbot-cmc"
```

**Verificar que el bot esta arriba:**
```
curl -s -o /dev/null -w "%{http_code}" https://agentecmc.cl/health
```
Debe devolver 200.

---

## FALLO 1 — Bot no responde mensajes (servicio caido / uvicorn muerto)

**Sintoma:**
- Pacientes no reciben respuesta en WhatsApp.
- `curl https://agentecmc.cl/health` devuelve 502 o no responde.

**Diagnostico:**
```
sshpass -p '<PASSWORD>' ssh root@157.245.13.107 "systemctl status chatbot-cmc"
```
Buscar: `Active: failed` o `Active: activating (auto-restart)`. Si el servicio esta en bucle de restart, revisar por que crashea:
```
sshpass -p '<PASSWORD>' ssh root@157.245.13.107 "journalctl -u chatbot-cmc -n 50 --no-pager"
```

**Fix:**
```
sshpass -p '<PASSWORD>' ssh root@157.245.13.107 "systemctl restart chatbot-cmc"
```

**Verificacion:**
```
sshpass -p '<PASSWORD>' ssh root@157.245.13.107 "systemctl status chatbot-cmc --no-pager"
curl -s -o /dev/null -w "%{http_code}" https://agentecmc.cl/health
```
Debe mostrar `Active: active (running)` y HTTP 200.

**Nota:** El servicio tiene `Restart=always` con `RestartSec=3s`. Si crashea en bucle (se ve en `journalctl` como reinicios repetidos), el problema no es el restart sino el codigo — ir al Fallo 3 (deploy roto).

---

## FALLO 2 — Token de WhatsApp Cloud API expirado o invalido

**Sintoma:**
- El bot recibe mensajes (el webhook responde 200 al POST de Meta) pero NO envia respuestas al paciente.
- En los logs aparece algo como:
  ```
  {"error":{"message":"Invalid OAuth access token","type":"OAuthException","code":190}}
  ```
  o `HTTP 401` al llamar a `graph.facebook.com`.

**Diagnostico:**
```
sshpass -p '<PASSWORD>' ssh root@157.245.13.107 "grep -i 'OAuthException\|Invalid.*token\|401.*graph\|facebook.*401' /var/log/cmc-bot.log | tail -20"
```
Confirmar que el error viene de `graph.facebook.com`, no de Medilink.

**Fix:**

1. Obtener un token nuevo desde Meta for Developers:
   - Ir a https://developers.facebook.com/ → App CMC → WhatsApp → Configuracion de API
   - Generar un nuevo token de acceso permanente (System User, no temporal de 24h)
   - Copiar el nuevo valor

2. Editar el .env en el servidor:
```
sshpass -p '<PASSWORD>' ssh root@157.245.13.107 "nano /opt/chatbot-cmc/.env"
```
Cambiar el valor de `META_ACCESS_TOKEN` por el nuevo token.

3. Reiniciar el servicio para que tome el nuevo valor:
```
sshpass -p '<PASSWORD>' ssh root@157.245.13.107 "systemctl restart chatbot-cmc"
```

**Verificacion:**
Enviar un mensaje de WhatsApp de prueba al numero del bot (+56966610737) y confirmar que responde. Tambien revisar:
```
sshpass -p '<PASSWORD>' ssh root@157.245.13.107 "tail -20 /var/log/cmc-bot.log"
```
No debe aparecer `OAuthException` ni `401`.

**Nota:** El token esta en `META_ACCESS_TOKEN` dentro de `/opt/chatbot-cmc/.env`. La variable `META_PAGE_ACCESS_TOKEN` es para Messenger (Facebook), `META_MESSENGER_TOKEN` para Instagram — son tokens separados. Si solo falla WhatsApp, solo necesitas renovar `META_ACCESS_TOKEN`.

---

## FALLO 3 — Deploy roto (bot crasheo tras un git pull)

**Sintoma:**
- El bot dejo de responder justo despues de un deploy.
- `systemctl status chatbot-cmc` muestra `failed` o restart loop.
- El health check devuelve 502.

**Diagnostico:**
Buscar el error de import o sintaxis en el journal:
```
sshpass -p '<PASSWORD>' ssh root@157.245.13.107 "journalctl -u chatbot-cmc -n 80 --no-pager | grep -A5 -i 'error\|traceback\|ImportError\|SyntaxError\|ModuleNotFound'"
```
O revisar el log directo del proceso:
```
sshpass -p '<PASSWORD>' ssh root@157.245.13.107 "tail -100 /var/log/cmc-bot.log | grep -i 'error\|traceback\|exception'"
```

**Fix — rollback al commit anterior:**

1. Ver el commit actual y el anterior:
```
sshpass -p '<PASSWORD>' ssh root@157.245.13.107 "cd /opt/chatbot-cmc && git log --oneline -5"
```

2. Hacer rollback al commit anterior (NO usa --hard para no perder datos de sessions.db, solo resetea el codigo):
```
sshpass -p '<PASSWORD>' ssh root@157.245.13.107 "cd /opt/chatbot-cmc && git checkout HEAD~1 -- app/ && systemctl restart chatbot-cmc"
```

3. Si el problema es un archivo especifico que se identifica en el traceback, se puede rollback solo ese archivo:
```
sshpass -p '<PASSWORD>' ssh root@157.245.13.107 "cd /opt/chatbot-cmc && git checkout HEAD~1 -- app/flows.py && systemctl restart chatbot-cmc"
```

**Verificacion:**
```
curl -s -o /dev/null -w "%{http_code}" https://agentecmc.cl/health
```
Debe devolver 200.

Luego, desde el repo local, corregir el bug, hacer commit y re-deployar con el deploy estandar.

**Nota critica:** Despues del rollback, el servidor queda con codigo desincronizado del repo remoto. El proximo `git pull` va a intentar avanzar al commit roto. Antes de volver a deployar, asegurate de que el commit en `main` ya tiene el fix aplicado.

---

## FALLO 4 — Medilink caido o token invalido (API no responde / 401)

**Sintoma:**
- Pacientes que intentan agendar reciben mensajes de error o el bot no muestra horarios disponibles.
- En logs aparece `401`, `403`, o timeouts al dominio `api.medilink2.healthatom.com`.
- El endpoint `/health` del bot sigue en 200 — el bot esta vivo, solo la integracion Medilink falla.

**Diagnostico — verificar conectividad y token:**
```
sshpass -p '<PASSWORD>' ssh root@157.245.13.107 "grep -i 'medilink\|healthatom\|401\|403\|timeout\|ConnectionError' /var/log/cmc-bot.log | tail -30"
```

Para confirmar el token activo:
```
sshpass -p '<PASSWORD>' ssh root@157.245.13.107 "source /opt/chatbot-cmc/.env && curl -s -o /dev/null -w '%{http_code}' -H \"Authorization: Token \$MEDILINK_TOKEN\" \"\$MEDILINK_BASE_URL/sucursales\""
```
- 200 = token valido, el problema es otro (caida de Medilink, timeout de red).
- 401 = token expirado o invalido.
- Sin respuesta / timeout = Medilink caido del lado de ellos.

**Fix — token invalido (401):**

1. Obtener un nuevo token desde el panel de Medilink (https://medilink2.healthatom.com → Configuracion → API).
2. Editar el .env:
```
sshpass -p '<PASSWORD>' ssh root@157.245.13.107 "nano /opt/chatbot-cmc/.env"
```
Actualizar `MEDILINK_TOKEN`.
3. Reiniciar:
```
sshpass -p '<PASSWORD>' ssh root@157.245.13.107 "systemctl restart chatbot-cmc"
```

**Fix — Medilink caido del lado de ellos:**
No hay fix local. El circuit breaker en `app/resilience.py` ya maneja reintentos automaticos. El bot degrada gracefully: los flujos de agendamiento fallan con mensaje amigable al paciente, pero el resto del bot (FAQ, derivacion a humano) sigue funcionando.

Comportamiento degradado esperado mientras Medilink esta caido:
- Flujos afectados: agendar, ver citas, cancelar citas, ver disponibilidad.
- Flujos que siguen: FAQ, respuesta a preguntas generales, escalado a humano.
- Los recordatorios y crons que dependen de Medilink se saltaran ese ciclo sin crashear el servicio.

Monitorear la recuperacion:
```
sshpass -p '<PASSWORD>' ssh root@157.245.13.107 "tail -f /var/log/cmc-bot.log | grep -i 'medilink\|healthatom'"
```

**Verificacion:**
```
sshpass -p '<PASSWORD>' ssh root@157.245.13.107 "source /opt/chatbot-cmc/.env && curl -s -o /dev/null -w '%{http_code}' -H \"Authorization: Token \$MEDILINK_TOKEN\" \"\$MEDILINK_BASE_URL/sucursales\""
```
Debe devolver 200.

---

## FALLO 5 — PostgreSQL (BI) sin conexiones disponibles / Docker caido

**Contexto:** El BI PostgreSQL corre en un contenedor Docker (`health_bi_postgres`) en el mismo VPS, escuchando en `127.0.0.1:5432`. Los endpoints del panel admin y los jobs de winback/BI lo usan. El chatbot de agendamiento en si NO depende de Postgres — usa SQLite (`sessions.db`). Si Postgres cae, solo se degradan los dashboards BI y los jobs de fidelizacion/winback.

**Sintoma:**
- Errores en logs: `connection pool exhausted`, `too many connections`, `psycopg2.OperationalError`, `could not connect to server`.
- Los endpoints `/admin/api/*` devuelven 500 pero el bot de WhatsApp sigue funcionando.

**Diagnostico — verificar que el contenedor Docker esta corriendo:**
```
sshpass -p '<PASSWORD>' ssh root@157.245.13.107 "docker ps | grep health_bi_postgres"
```
Debe mostrar `Up ... (healthy)`.

**Diagnostico — contar conexiones activas:**
```
sshpass -p '<PASSWORD>' ssh root@157.245.13.107 "docker exec health_bi_postgres psql -U health_user -d health_bi -c \"SELECT count(*), state FROM pg_stat_activity WHERE datname='health_bi' GROUP BY state;\""
```
Si hay muchas conexiones en estado `idle` (decenas), hay un leak — el pool no esta devolviendo conexiones.

**Fix — conexiones colgadas (leak):**
Matar las conexiones idle sin actividad:
```
sshpass -p '<PASSWORD>' ssh root@157.245.13.107 "docker exec health_bi_postgres psql -U health_user -d health_bi -c \"SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname='health_bi' AND state='idle' AND query_start < now() - interval '5 minutes';\""
```
Luego reiniciar el bot para que el pool se reinicialice limpio:
```
sshpass -p '<PASSWORD>' ssh root@157.245.13.107 "systemctl restart chatbot-cmc"
```

**Fix — contenedor Docker caido:**
```
sshpass -p '<PASSWORD>' ssh root@157.245.13.107 "docker start health_bi_postgres"
```
Esperar ~10 segundos a que Postgres levante, luego:
```
sshpass -p '<PASSWORD>' ssh root@157.245.13.107 "systemctl restart chatbot-cmc"
```

**Fix — Docker daemon caido (mas grave):**
```
sshpass -p '<PASSWORD>' ssh root@157.245.13.107 "systemctl start docker && docker start health_bi_postgres"
```

**Verificacion:**
```
sshpass -p '<PASSWORD>' ssh root@157.245.13.107 "docker ps | grep health_bi_postgres"
```
Debe mostrar `(healthy)`. Luego verificar que el bot responde:
```
curl -s -o /dev/null -w "%{http_code}" https://agentecmc.cl/health
```

---

## FALLO 6 — /health no responde o nginx devuelve 502

**Sintoma:**
- `curl https://agentecmc.cl/health` devuelve 502 Bad Gateway o no responde.
- El bot no atiende mensajes.

**Diagnostico — diagnosticar la cadena nginx → uvicorn:**

Paso 1: Verificar que nginx esta corriendo:
```
sshpass -p '<PASSWORD>' ssh root@157.245.13.107 "systemctl status nginx --no-pager"
```

Paso 2: Verificar que uvicorn esta escuchando en 8001:
```
sshpass -p '<PASSWORD>' ssh root@157.245.13.107 "ss -tlnp | grep 8001"
```
Debe mostrar `127.0.0.1:8001` con el pid de uvicorn.

Paso 3: Probar uvicorn directamente (sin pasar por nginx):
```
sshpass -p '<PASSWORD>' ssh root@157.245.13.107 "curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8001/health"
```
- Si devuelve 200: nginx esta mal configurado o caido.
- Si no responde: uvicorn esta caido → ir al Fallo 1.

Paso 4: Ver logs de nginx si el problema es nginx:
```
sshpass -p '<PASSWORD>' ssh root@157.245.13.107 "tail -30 /var/log/nginx/error.log"
```

**Fix — nginx caido:**
```
sshpass -p '<PASSWORD>' ssh root@157.245.13.107 "systemctl restart nginx"
```

**Fix — nginx tiene config rota (por cambio reciente):**
```
sshpass -p '<PASSWORD>' ssh root@157.245.13.107 "nginx -t"
```
Si muestra errores de sintaxis, hay que revisar el archivo en `/etc/nginx/sites-enabled/`. Para rollback:
```
sshpass -p '<PASSWORD>' ssh root@157.245.13.107 "nginx -t && systemctl reload nginx"
```

**Fix — uvicorn caido (502 porque nginx no tiene a quien proxear):**
```
sshpass -p '<PASSWORD>' ssh root@157.245.13.107 "systemctl restart chatbot-cmc"
```

**Verificacion completa de la cadena:**
```
sshpass -p '<PASSWORD>' ssh root@157.245.13.107 "systemctl status nginx chatbot-cmc --no-pager && ss -tlnp | grep 8001"
curl -s -o /dev/null -w "%{http_code}" https://agentecmc.cl/health
```

---

## FALLO 7 — sessions.db locked o SQLCipher

**Contexto:** `sessions.db` es la base SQLite principal del bot (sesiones de conversacion, logs de mensajes, deduplicacion). Esta CIFRADA con SQLCipher — la clave esta en la variable `SQLCIPHER_KEY` dentro de `/opt/chatbot-cmc/.env`. NO se puede abrir con `sqlite3` estandar ni con DB Browser for SQLite sin la clave y el modulo sqlcipher3.

**Sintoma:**
- Logs muestran: `database is locked`, `sqlite3.OperationalError: database is locked`.
- El bot no puede leer ni escribir sesiones → todos los mensajes se procesan como sesion nueva, conversaciones rotas.

**Diagnostico — confirmar lock:**
```
sshpass -p '<PASSWORD>' ssh root@157.245.13.107 "grep -i 'database is locked\|OperationalError' /var/log/cmc-bot.log | tail -20"
```

**Diagnostico — ver que proceso tiene el archivo abierto:**
```
sshpass -p '<PASSWORD>' ssh root@157.245.13.107 "lsof /opt/chatbot-cmc/sessions.db"
```

**Fix — lock por proceso zombie (lo mas comun):**
Si hay un proceso de uvicorn viejo todavia con el archivo abierto (despues de un restart fallido):
```
sshpass -p '<PASSWORD>' ssh root@157.245.13.107 "pkill -f 'uvicorn app.main' && sleep 2 && systemctl start chatbot-cmc"
```

**Fix — lock genuino de SQLite (WAL journal atascado):**
SQLite usa WAL (Write-Ahead Log). Si el proceso murio mal, puede quedar un archivo `sessions.db-wal` o `sessions.db-shm` que bloquea la DB. Eliminarlos es seguro si el bot esta detenido:
```
sshpass -p '<PASSWORD>' ssh root@157.245.13.107 "systemctl stop chatbot-cmc && rm -f /opt/chatbot-cmc/sessions.db-wal /opt/chatbot-cmc/sessions.db-shm && systemctl start chatbot-cmc"
```
IMPORTANTE: Solo eliminar estos archivos con el bot DETENIDO. Si el bot esta corriendo y escribiendo, eliminarlos puede corromper la DB.

**Acceso manual a sessions.db (requiere sqlcipher3):**
Para inspeccionar la DB manualmente en el servidor (usar solo en diagnostico, NO en produccion con el bot corriendo):
```
sshpass -p '<PASSWORD>' ssh root@157.245.13.107 "source /opt/chatbot-cmc/.env && /opt/chatbot-cmc/venv/bin/python3 -c \"
import sys; sys.path.insert(0, '/opt/chatbot-cmc')
from sqlcipher3 import dbapi2 as sqlite
conn = sqlite.connect('/opt/chatbot-cmc/sessions.db')
conn.execute(\\\"PRAGMA key='x:\\\"\" + \"\$SQLCIPHER_KEY\" + \"\\\"'\\\")
cur = conn.execute('SELECT count(*) FROM sessions')
print('Sesiones activas:', cur.fetchone()[0])
conn.close()
\""
```

**REGLA CRITICA:** NUNCA copiar `sessions.db` a otro lugar para abrirlo sin la clave `SQLCIPHER_KEY`. El archivo es ilegible sin ella. La clave esta UNICAMENTE en `/opt/chatbot-cmc/.env` en el servidor.

**Verificacion:**
```
sshpass -p '<PASSWORD>' ssh root@157.245.13.107 "systemctl status chatbot-cmc --no-pager && tail -10 /var/log/cmc-bot.log"
```
No deben aparecer errores de `locked` o `OperationalError`.

---

## REFERENCIA RAPIDA — Variables de entorno criticas en /opt/chatbot-cmc/.env

| Variable | Que es | Donde renovar si expira |
|----------|--------|------------------------|
| META_ACCESS_TOKEN | Token WhatsApp Cloud API | developers.facebook.com → App CMC → WhatsApp |
| META_PAGE_ACCESS_TOKEN | Token Messenger (Facebook) | developers.facebook.com → App CMC → Messenger |
| META_MESSENGER_TOKEN | Token Instagram | developers.facebook.com → App CMC → Instagram |
| MEDILINK_TOKEN | Token API Medilink | medilink2.healthatom.com → Configuracion → API |
| SQLCIPHER_KEY | Clave cifrado sessions.db | NO renovar sin migrar la DB — es permanente |
| ADMIN_TOKEN | Token panel admin /admin/* | Cambiar en .env + reiniciar bot |
| BI_DB_HOST/PORT/NAME/USER/PASSWORD | PostgreSQL Docker | Contenedor health_bi_postgres en 127.0.0.1:5432 |

Despues de cambiar CUALQUIER variable en `.env`, reiniciar el servicio:
```
sshpass -p '<PASSWORD>' ssh root@157.245.13.107 "systemctl restart chatbot-cmc"
```

---

## REFERENCIA RAPIDA — Comandos de diagnostico frecuentes

**Ver logs en vivo:**
```
sshpass -p '<PASSWORD>' ssh root@157.245.13.107 "journalctl -u chatbot-cmc -f"
```

**Ver ultimas 100 lineas del log:**
```
sshpass -p '<PASSWORD>' ssh root@157.245.13.107 "tail -100 /var/log/cmc-bot.log"
```

**Buscar errores en el log:**
```
sshpass -p '<PASSWORD>' ssh root@157.245.13.107 "grep -i 'error\|exception\|traceback\|critical' /var/log/cmc-bot.log | tail -30"
```

**Ver todos los servicios relevantes:**
```
sshpass -p '<PASSWORD>' ssh root@157.245.13.107 "systemctl status chatbot-cmc nginx docker --no-pager"
```

**Ver uso de disco (si el log crece sin control):**
```
sshpass -p '<PASSWORD>' ssh root@157.245.13.107 "df -h && ls -lh /var/log/cmc-bot.log"
```

**Rotar log manualmente si esta muy grande:**
```
sshpass -p '<PASSWORD>' ssh root@157.245.13.107 "cp /var/log/cmc-bot.log /var/log/cmc-bot.log.$(date +%Y%m%d) && truncate -s 0 /var/log/cmc-bot.log && systemctl restart chatbot-cmc"
```
