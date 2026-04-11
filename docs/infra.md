# Infraestructura — CMC Bot + GES Assistant

## VPS DigitalOcean `cmc-bot`

| Dato | Valor |
|---|---|
| Host | `root@157.245.13.107` |
| Hostname | `cmc-bot` |
| OS | Ubuntu 24.04 (kernel 6.8) |
| RAM | 1 GB (sin swap) |
| Disco | 24 GB (13% usado) |
| Dominio | `agentecmc.cl` (DNS en Cloudflare, registrar NIC Chile) |

### Acceso SSH

- Auth **solo por llave pública** — password deshabilitado el 2026-04-10.
- Llave local: `~/.ssh/id_ed25519` (Ed25519, sin passphrase).
- Conexión directa: `ssh root@157.245.13.107`.
- Contraseña root rotada como fallback — guardada en el password manager personal del usuario, **no en este repo**.
- Si alguna vez perdés la llave: se puede recuperar usando el "Droplet Console" de DigitalOcean desde el panel web y ahí usar la contraseña del password manager para volver a entrar.

### Cambios recientes de seguridad (2026-04-10)

- SSH Ed25519 key generada en Mac local y copiada al VPS con `ssh-copy-id`.
- `/etc/ssh/sshd_config.d/50-cloud-init.conf` → `PasswordAuthentication no`.
- Backup del config original: `/etc/ssh/sshd_config.bak.2026-04-10`.
- Contraseña root rotada a 24 chars aleatorios (openssl).

## Servicios en el VPS

### 1. Chatbot CMC (WhatsApp + Meta/IG/FB)

| | |
|---|---|
| Ruta | `/opt/chatbot-cmc/` |
| Puerto | `0.0.0.0:8001` |
| Ejecución | `nohup venv/bin/uvicorn app.main:app ...` (proceso suelto, no systemd) |
| Logs | `/var/log/cmc-bot.log` |
| Reverse proxy | nginx → `https://agentecmc.cl` (certbot Let's Encrypt) |

**Redeploy — one-liner desde el Mac (recomendado):**
```bash
git push origin main
ssh root@157.245.13.107 "cd /opt/chatbot-cmc && git pull && pkill -f 'chatbot-cmc.*uvicorn'; sleep 2; cd /opt/chatbot-cmc && setsid nohup venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8001 </dev/null >/var/log/cmc-bot.log 2>&1 & disown"
```

**Redeploy — sesión SSH interactiva:**
```bash
ssh root@157.245.13.107
cd /opt/chatbot-cmc
git pull
pkill -f 'chatbot-cmc.*uvicorn'
sleep 2
setsid nohup venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8001 </dev/null >/var/log/cmc-bot.log 2>&1 &
disown
```

**⚠️ `setsid` no es opcional cuando el comando viaja dentro de `ssh "..."`:** un `nohup ... &` pelado deja el uvicorn asociado al pty remoto, y éste se muere al cerrarse la sesión SSH → bot caído. `setsid` fuerza un nuevo process group, desligando el proceso del SSH. Verificado en prod el 2026-04-10 (8 s de downtime recuperado re-levantando con setsid).

**Verificación post-deploy:**
```bash
curl -s -o /dev/null -w 'HTTP %{http_code}\n' https://agentecmc.cl/health   # → 200
ssh root@157.245.13.107 "ps aux | grep 'chatbot-cmc.*uvicorn' | grep -v grep"
```

### 2. GES Clinical Assistant (backend API de triage)

| | |
|---|---|
| Ruta | `/opt/ges-assistant/` |
| Puerto | `127.0.0.1:8002` (solo localhost, no expuesto a internet) |
| Ejecución | systemd — `ges-assistant.service` |
| Autostart | enabled |
| RAM típica | ~70 MB |
| DB | SQLite en `data/ges.db` con ~102 patologías seed |

**Gestión:**
```bash
systemctl status ges-assistant
systemctl restart ges-assistant
journalctl -u ges-assistant -f
```

**Redeploy desde Mac local (rsync):**
```bash
rsync -az --delete \
  --exclude='__pycache__' --exclude='*.pyc' --exclude='venv' --exclude='.env' \
  /Users/rodrigoolavarria/ges-clinical-app/backend/ \
  root@157.245.13.107:/opt/ges-assistant/
ssh root@157.245.13.107 "systemctl restart ges-assistant"
```

### 3. nginx

- Config activa: `/etc/nginx/sites-enabled/agentecmc`
- Certbot auto-renew configurado.
- Futuro: subdomain `ges.agentecmc.cl` para el panel frontend (pendiente).

## Variables de entorno relevantes

`/opt/chatbot-cmc/.env` incluye:
- `GES_ASSISTANT_URL=http://localhost:8002` (apunta al backend GES)

El resto (Meta tokens, Medilink, Anthropic) no documentado acá por seguridad — revisar el archivo directo con `cat /opt/chatbot-cmc/.env` vía SSH.

## Memoria total (post-deploy GES)

```
Total:     961 MB
Usado:     ~500 MB
Libre:     ~460 MB
Sin swap — considerar agregar swapfile 2 GB si se deploya el frontend
```

## Próximos pasos pendientes

- [ ] Git init + push privado de `ges-clinical-app` a GitHub
- [ ] Frontend Next.js del GES en Vercel → `ges.agentecmc.cl`
- [ ] Exponer endpoints públicos del backend GES vía nginx subdomain `api-ges.agentecmc.cl` (solo los que usa el frontend, NO `/triage` que sigue siendo local)
- [ ] Agregar swapfile 2 GB al VPS antes de montar el frontend
- [ ] Cron backup semanal de `/opt/ges-assistant/data/ges.db`
