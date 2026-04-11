# Infraestructura — CMC Bot + GES Assistant

## VPS DigitalOcean `cmc-bot`

| Dato | Valor |
|---|---|
| Host | `root@157.245.13.107` |
| Hostname | `cmc-bot` |
| OS | Ubuntu 24.04 (kernel 6.8) |
| RAM | 1 GB + 2 GB swap (agregado 2026-04-10) |
| Disco | 24 GB (14% usado, incluye swapfile) |
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

## Memoria total (post-swapfile)

```
RAM:       961 MB  (~495 MB usado, ~466 MB disponible)
Swap:      2.0 GB  (0 B usado — vm.swappiness=10, solo swapea en emergencia)
Total ef.: ~3 GB disponibles para el frontend
```

El swapfile está en `/swapfile` (2 GB, chmod 600), persistido en `/etc/fstab` y
con `vm.swappiness=10` en `/etc/sysctl.d/99-swappiness.conf` para mantener la
RAM caliente. Creado con `fallocate -l 2G`.

## Backup automático del GES Assistant

| | |
|---|---|
| Script | `/usr/local/bin/backup-ges-db.sh` |
| Cron | `/etc/cron.d/ges-assistant-backup` — **domingo 03:30 UTC** (00:30 Chile) |
| Destino | `/opt/backups/ges-assistant/ges_YYYYMMDD_HHMMSS.db.gz` |
| Método | `sqlite3 .backup` (online, seguro con el service corriendo) + gzip |
| Retención | Últimos **8 backups** (~2 meses con cron semanal) |
| Logs | `/var/log/ges-backup.log` |
| Tamaño típico | ~412 KB comprimido (DB cruda 1.1 MB, ratio 2.7x) |

**Ejecución manual:**
```bash
ssh root@157.245.13.107 "/usr/local/bin/backup-ges-db.sh && ls -lh /opt/backups/ges-assistant/"
```

**Restore:**
```bash
ssh root@157.245.13.107
systemctl stop ges-assistant
gunzip -c /opt/backups/ges-assistant/ges_YYYYMMDD_HHMMSS.db.gz > /opt/ges-assistant/data/ges.db
systemctl start ges-assistant
```

## Repo GitHub privado

- `ges-clinical-app` → https://github.com/rodrigofernandoolavarriadelavega/ges-clinical-app (privado)
- Init + primer commit `56983b0` el 2026-04-10 (monorepo: `backend/` + `frontend/`)
- La DB `backend/data/ges.db` está gitignoreada — se regenera con `scripts/seed*.py`
- `chatbot-cmc` también vive en GitHub privado (mismo owner)

**⚠️ Deuda de seguridad** — el Personal Access Token está embedded en plaintext en el `remote.origin.url` de ambos repos (tanto en el Mac como en el VPS en `/opt/chatbot-cmc/.git/config`). Plan cuando haya ventana:

1. Rotar el PAT en GitHub → Settings → Tokens
2. Migrar `chatbot-cmc` + `ges-clinical-app` a SSH key: subir `~/.ssh/id_ed25519.pub` a GitHub y correr `git remote set-url origin git@github.com:...`
3. **Actualizar el VPS también** — el `/opt/chatbot-cmc/.git/config` tiene el mismo PAT, si se rota sin actualizar ese remote el próximo `git pull` en deploy va a fallar

## Próximos pasos pendientes

- [ ] Frontend Next.js del GES en Vercel → `ges.agentecmc.cl`
- [ ] Exponer endpoints públicos del backend GES vía nginx subdomain `api-ges.agentecmc.cl` (solo los que usa el frontend, NO `/triage` que sigue siendo local)
- [ ] CORS restrictivo en el backend GES para el origen de Vercel
- [ ] Rotación de PAT + migración a SSH keys (chatbot-cmc + ges-clinical-app + VPS)
