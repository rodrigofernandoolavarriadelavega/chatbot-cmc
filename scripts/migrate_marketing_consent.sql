-- migrate_marketing_consent.sql
-- Tarea B/C/D/E del roadmap win-back (2026-05-10)
-- Ejecutar en la base de datos BI (PostgreSQL, schema bi).
-- Idempotente: usa IF NOT EXISTS / ON CONFLICT.

-- ── 1. Consentimiento de marketing ──────────────────────────────────────────

CREATE TABLE IF NOT EXISTS bi.marketing_consent (
    phone           TEXT PRIMARY KEY,
    status          TEXT NOT NULL DEFAULT 'pending'
                        CHECK (status IN ('pending', 'accepted', 'declined', 'no_response')),
    consent_sent_at TIMESTAMPTZ,
    response_at     TIMESTAMPTZ,
    response_method TEXT          -- 'reply' | 'manual' | 'import'
);

COMMENT ON TABLE bi.marketing_consent IS
    'Registro de consentimiento de marketing por número de WhatsApp. '
    'WINBACK_ACTIVE y MARKETING_CONSENT_BLAST_ACTIVE deben estar false '
    'en .env hasta aprobación explícita de Rodrigo.';

-- ── 2. Opt-outs de marketing ─────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS bi.opt_outs_marketing (
    phone      TEXT PRIMARY KEY,
    source     TEXT,   -- 'consent_marketing_v1' | 'manual' | 'baja_chatbot'
    reason     TEXT,   -- 'declined_marketing' | 'user_request' | etc.
    created_at TIMESTAMPTZ DEFAULT NOW()
);

COMMENT ON TABLE bi.opt_outs_marketing IS
    'Phones que respondieron NO al consent o solicitaron baja de marketing. '
    'Exclusión permanente de todas las campañas outbound.';

-- ── 3. Lista de espera de slots ──────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS bi.lista_espera_slots (
    id           SERIAL PRIMARY KEY,
    phone        TEXT NOT NULL,
    especialidad TEXT NOT NULL,
    created_at   TIMESTAMPTZ DEFAULT NOW(),
    notified_at  TIMESTAMPTZ,
    expired_at   TIMESTAMPTZ,
    UNIQUE (phone, especialidad)
);

COMMENT ON TABLE bi.lista_espera_slots IS
    'Pacientes que quieren ser notificados si se libera un slot '
    'en una especialidad específica (anti no-show Tarea E).';

-- ── 4. Vista: cohortes contactables con consentimiento ───────────────────────

CREATE OR REPLACE VIEW bi.v_winback_cohortes_con_consent AS
    SELECT wc.*
    FROM bi.v_winback_cohortes_contactables wc
    INNER JOIN bi.marketing_consent mc
        ON mc.phone = wc.telefono
        AND mc.status = 'accepted';

COMMENT ON VIEW bi.v_winback_cohortes_con_consent IS
    'Subconjunto de v_winback_cohortes_contactables que ya aceptaron marketing. '
    'Usar esta vista (no la base) para envíos winback una vez activado.';

-- ── 5. Índices ───────────────────────────────────────────────────────────────

CREATE INDEX IF NOT EXISTS idx_marketing_consent_status
    ON bi.marketing_consent (status);

CREATE INDEX IF NOT EXISTS idx_marketing_consent_sent_at
    ON bi.marketing_consent (consent_sent_at);

CREATE INDEX IF NOT EXISTS idx_lista_espera_esp
    ON bi.lista_espera_slots (especialidad, notified_at);
