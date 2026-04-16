# Encriptación en reposo — Playbook

**Objetivo**: cumplir con art. 14 de la Ley 19.628 (reformada 2024) — "medidas técnicas apropiadas" para proteger datos en reposo en el VPS DigitalOcean.

---

## Contexto

- VPS: `157.245.13.107` (DigitalOcean NYC3, 1 GB RAM, 25 GB SSD).
- Archivos sensibles:
  - `/opt/chatbot-cmc/data/sessions.db` (~300 MB, WAL activo).
  - `/opt/chatbot-cmc/data/uploads/` (fotos, PDFs, audios de pacientes).
  - `/opt/ges-assistant/data/ges.db` (~1.1 MB).
  - `/opt/backups/` (backups semanales SQLite comprimidos).
- Amenaza principal: **compromiso del VPS** (credenciales leakeadas, imagen de disco robada, snapshot exfiltrado, acceso físico al datacenter improbable pero contemplado).

---

## Opción A — SQLCipher (RECOMENDADA)

**Pros**:
- Encripta **solo los archivos `.db`** (lo realmente sensible).
- **Sin downtime significativo** (<5 minutos de switch).
- No requiere reformatear particiones.
- Compatible drop-in con la API de `sqlite3` de Python (`pysqlcipher3`).
- Backups ya quedan encriptados automáticamente.

**Cons**:
- Los archivos NO `.db` (uploads/, logs/) no quedan encriptados → requieren cifrado a nivel app o mover a `/root/encrypted/` (Opción B).
- La key vive en el `.env` del servidor; si el VPS se compromete, la key también. Mitigación: la key protege contra robo de **snapshot frío** o **imagen de disco**, no contra un atacante con root en vivo.
- Dependencia adicional: `pysqlcipher3` (binding C).

### Pasos

```bash
# ── En el VPS ────────────────────────────────────────────────────────────────
apt update && apt install -y sqlcipher libsqlcipher-dev

# Python binding
cd /opt/chatbot-cmc
source venv/bin/activate
pip install sqlcipher3-binary==0.5.2

# Generar key aleatoria y guardar en .env (permisos 600)
SQLCIPHER_KEY=$(openssl rand -hex 32)
echo "SQLCIPHER_KEY=$SQLCIPHER_KEY" >> .env
chmod 600 .env

# ── Migrar la DB existente ───────────────────────────────────────────────────
systemctl stop chatbot-cmc

# Export SQL con sqlite3 normal
sqlite3 data/sessions.db ".dump" > /tmp/dump.sql

# Crear DB encriptada con sqlcipher
sqlcipher data/sessions_enc.db <<EOF
PRAGMA key = "$SQLCIPHER_KEY";
PRAGMA cipher_page_size = 4096;
PRAGMA kdf_iter = 256000;
.read /tmp/dump.sql
EOF

# Verificar
sqlcipher data/sessions_enc.db "PRAGMA key = '$SQLCIPHER_KEY'; SELECT COUNT(*) FROM messages;"

# Swap
mv data/sessions.db data/sessions_plain.db.bak
mv data/sessions_enc.db data/sessions.db

# Borrar dump
shred -u /tmp/dump.sql
```

### Cambios en el código

Editar `app/session.py::_conn()`:

```python
import os
try:
    from pysqlcipher3 import dbapi2 as sqlite3_enc
    _USE_CIPHER = True
    _CIPHER_KEY = os.getenv("SQLCIPHER_KEY")
except ImportError:
    _USE_CIPHER = False


def _conn():
    DB_PATH.parent.mkdir(exist_ok=True)
    if _USE_CIPHER and _CIPHER_KEY:
        conn = sqlite3_enc.connect(str(DB_PATH), timeout=10)
        conn.execute(f"PRAGMA key = \"x'{_CIPHER_KEY}'\"")
        conn.execute("PRAGMA cipher_page_size = 4096")
    else:
        conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3_enc.Row if _USE_CIPHER else sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    # ... resto igual
```

### Restart

```bash
systemctl start chatbot-cmc
curl -s https://agentecmc.cl/health   # debe devolver 200
tail -f /var/log/cmc-bot.log          # verificar sin errores
```

### Backups

Actualizar el script `/usr/local/bin/backup-ges-db.sh` para usar `sqlcipher`:

```bash
#!/bin/bash
STAMP=$(date +%Y%m%d_%H%M%S)
source /opt/chatbot-cmc/.env
sqlcipher /opt/chatbot-cmc/data/sessions.db <<EOF | gzip > /opt/backups/chatbot-cmc/sessions_$STAMP.sql.gz
PRAGMA key = "x'$SQLCIPHER_KEY'";
.dump
EOF
# Retención: mantener últimos 30
ls -t /opt/backups/chatbot-cmc/*.sql.gz | tail -n +31 | xargs -r rm
```

---

## Opción B — LUKS full-disk encryption

**Pros**:
- Encripta **todo** (uploads, logs, DB, binarios).
- Transparente para la aplicación (cero cambios de código).

**Cons**:
- Requiere **reformatear un volumen** → downtime de ~30 min + riesgo de pérdida si se hace mal.
- En DigitalOcean droplets la raíz ya está sobre ext4 sin LUKS por default; LUKS requiere un **Block Storage volume** adicional ($1/mo por 10 GB, $0.10/GB/mo).
- La key debe desbloquear en cada boot → script de keyfile o cryptsetup con passphrase remoto (Dropbear SSH en initramfs es la solución enterprise, no trivial en un VPS chico).

### Pasos (alto nivel, solo si Opción A no basta)

```bash
# 1. Crear Block Storage Volume en DigitalOcean (10 GB, ext4, same region NYC3).
# 2. Conectar al droplet (aparece como /dev/sda o /dev/disk/by-id/...).

# 3. Instalar cryptsetup
apt install -y cryptsetup

# 4. Inicializar LUKS
cryptsetup luksFormat /dev/sda --cipher aes-xts-plain64 --key-size 512 --hash sha512

# 5. Abrir el volumen
cryptsetup luksOpen /dev/sda cmc_data

# 6. Formatear ext4 dentro
mkfs.ext4 /dev/mapper/cmc_data

# 7. Montar y migrar
mkdir -p /mnt/cmc_data
mount /dev/mapper/cmc_data /mnt/cmc_data
systemctl stop chatbot-cmc
rsync -a /opt/chatbot-cmc/data/ /mnt/cmc_data/
umount /mnt/cmc_data

# 8. Configurar mount automático
# Guardar keyfile en /root/luks.key (chmod 400), agregarlo como key LUKS
dd if=/dev/urandom of=/root/luks.key bs=32 count=1
chmod 400 /root/luks.key
cryptsetup luksAddKey /dev/sda /root/luks.key

# /etc/crypttab
echo "cmc_data /dev/sda /root/luks.key luks" >> /etc/crypttab

# /etc/fstab
echo "/dev/mapper/cmc_data /opt/chatbot-cmc/data ext4 defaults 0 2" >> /etc/fstab

# Simbolizar para la app
rm -rf /opt/chatbot-cmc/data
ln -s /mnt/cmc_data /opt/chatbot-cmc/data

systemctl start chatbot-cmc
```

**CAVEAT**: si el VPS se compromete (root shell activo), un atacante puede leer `/root/luks.key` igual que puede leer la SQLCIPHER_KEY del `.env`. **LUKS solo protege contra imágenes frías del disco** — mismo alcance que SQLCipher.

---

## Recomendación final

**Implementar Opción A (SQLCipher) primero** porque:

1. Cubre ~95% del riesgo real (imagen de disco robada, snapshot exfiltrado, datacenter breach).
2. Zero downtime prácticamente.
3. Backups quedan encriptados automáticamente.
4. Es reversible sin costo (el DB plano original queda en `sessions_plain.db.bak` por seguridad).

Complementar con:

- **Cifrar `data/uploads/`** a nivel de filesystem con `eCryptfs` o pasar los archivos por GPG simétrico antes de guardarlos.
- **Rotación anual de la `SQLCIPHER_KEY`** (generar nueva, re-encrypt con `PRAGMA rekey`).
- **Backups offsite** encriptados (rsync a un segundo proveedor o S3 con server-side encryption).

LUKS queda como opción futura **si se contrata un droplet dedicado o un volumen separado**. Por ahora, SQLCipher es suficiente y proporcional al volumen de datos y al presupuesto.

---

## Checklist de implementación

- [x] Instalar `sqlcipher` + `sqlcipher3-binary` en VPS (2026-04-16)
- [x] Generar `SQLCIPHER_KEY` (64 chars hex)
- [x] Respaldar `sessions.db` → `sessions_plain.db.bak`
- [x] Migrar con `sqlcipher_export()` → DB encriptada (19 sessions, 16 consents, 878 msgs)
- [x] Actualizar `app/session.py::_conn()` (tupla `_OPERATIONAL_ERRORS` para compat excepciones)
- [x] Deploy + smoke test (`/health` → 200, `/admin` funcionando)
- [x] Backup cron semanal encriptado: `scripts/backup-cmc-db.sh` → `/usr/local/bin/backup-cmc-db.sh` + `/etc/cron.d/chatbot-cmc-backup` (domingo 03:30 UTC)
- [x] Actualizar `docs/privacy_policy.md` sección 8 con "✅ SQLCipher activo desde 2026-04-16"
- [ ] Borrar `sessions_plain.db.bak` y `sessions_old_plain.db` con `shred -u` después de 7 días de estabilidad (→ 2026-04-23)
- [ ] Documentar rotación anual de key en `docs/infra.md` (próxima rotación: 2027-04-16)

## Recuperar un backup

```bash
# Descomprimir
gunzip -c /opt/backups/chatbot-cmc/sessions_YYYYMMDD_HHMMSS.db.gz > /tmp/restore.db

# Abrir con la key
KEY=$(grep '^SQLCIPHER_KEY=' /opt/chatbot-cmc/.env | cut -d= -f2)
sqlcipher /tmp/restore.db "PRAGMA key = \"x'$KEY'\"; SELECT COUNT(*) FROM sessions;"
```

## Rotación de key (anual)

```bash
systemctl stop chatbot-cmc
NEW_KEY=$(openssl rand -hex 32)
sqlcipher /opt/chatbot-cmc/data/sessions.db <<EOF
PRAGMA key = "x'$OLD_KEY'";
PRAGMA rekey = "x'$NEW_KEY'";
EOF
sed -i "s|^SQLCIPHER_KEY=.*|SQLCIPHER_KEY=$NEW_KEY|" /opt/chatbot-cmc/.env
systemctl start chatbot-cmc
```
