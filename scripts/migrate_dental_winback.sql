-- migrate_dental_winback.sql
-- Sprint win-back dental (2026-05-10)
-- Ejecutar en la base de datos BI (PostgreSQL, schema bi).
-- Idempotente: usa IF NOT EXISTS / ON CONFLICT / CREATE OR REPLACE.
--
-- IMPORTANTE: estas tablas son INDEPENDIENTES de las tablas generales
-- (marketing_consent, opt_outs_marketing, winback_envios).
-- Un paciente puede tener opt-out de marketing general y aceptar el dental,
-- o viceversa. No hay FK cruzados deliberadamente.

-- 1. Consentimiento dental --------------------------------------------------

CREATE TABLE IF NOT EXISTS bi.dental_consent (
    phone           TEXT PRIMARY KEY,
    status          TEXT NOT NULL DEFAULT 'pending'
                        CHECK (status IN ('pending', 'accepted', 'declined', 'no_response')),
    consent_sent_at TIMESTAMPTZ,
    response_at     TIMESTAMPTZ,
    response_method TEXT
);

COMMENT ON TABLE bi.dental_consent IS
    'Consentimiento explícito DENTAL por phone. Independiente de bi.marketing_consent. '
    'DENTAL_CONSENT_BLAST_ACTIVE y DENTAL_WINBACK_ACTIVE deben ser false '
    'en .env hasta aprobación explícita de Rodrigo.';

-- 2. Opt-outs dental --------------------------------------------------------

CREATE TABLE IF NOT EXISTS bi.dental_opt_outs (
    phone        TEXT PRIMARY KEY,
    source       TEXT,
    reason       TEXT,
    opted_out_at TIMESTAMPTZ DEFAULT NOW()
);

COMMENT ON TABLE bi.dental_opt_outs IS
    'Phones que respondieron NO al consent dental o solicitaron baja del programa dental. '
    'Exclusión permanente de campañas outbound dentales.';

-- 3. Registro de envíos dental ----------------------------------------------

CREATE TABLE IF NOT EXISTS bi.dental_winback_envios (
    id            SERIAL PRIMARY KEY,
    paciente_id   INT,
    subcohorte    TEXT NOT NULL,
    telefono      TEXT NOT NULL,
    template_meta TEXT NOT NULL,
    canal         TEXT NOT NULL DEFAULT 'whatsapp',
    especialidad  TEXT,
    enviado_at    TIMESTAMPTZ DEFAULT NOW(),
    response_type TEXT,
    respondio_at  TIMESTAMPTZ,
    opt_out       BOOLEAN DEFAULT false
);

COMMENT ON TABLE bi.dental_winback_envios IS
    'Log de cada mensaje winback dental enviado. Independiente de bi.winback_envios.';

-- 4. Vista: pacientes dentales contactables ---------------------------------
-- Pool: pacientes con >= 1 atención con profesional dental
-- (IDs 55=Burgos, 72=Jiménez, 66=Castillo, 75=Fredes, 69=Valdés, 76=Fuentealba).

CREATE OR REPLACE VIEW bi.v_dental_cohortes_contactables AS
WITH ultima_dental AS (
    SELECT
        p.paciente_id                                                        AS paciente_id,
        p.nombre,
        p.apellido,
        p.telefono                             AS telefono,
        MAX(a.fecha)                                                AS ultima_atencion,
        NOW()::DATE - MAX(a.fecha)::DATE                            AS dias_inactivo,
        (ARRAY_AGG(pr.nombre ORDER BY a.fecha DESC))[1]             AS ultimo_profesional,
        (ARRAY_AGG(e.nombre  ORDER BY a.fecha DESC))[1]             AS ultima_especialidad,
        (ARRAY_AGG(a.profesional_id ORDER BY a.fecha DESC))[1]      AS profesional_id_ultimo
    FROM bi.dim_paciente p
    INNER JOIN bi.fact_atenciones a   ON a.paciente_id   = p.paciente_id
    INNER JOIN bi.dim_profesional pr ON pr.profesional_id           = a.profesional_id
    LEFT  JOIN bi.dim_especialidad e ON e.especialidad_id            = a.prestacion_id
    WHERE a.profesional_id IN (55, 72, 66, 75, 69, 76)
      AND a.fecha >= CURRENT_DATE - INTERVAL '2 years'
    GROUP BY p.paciente_id, p.nombre, p.apellido, p.celular, p.telefono
),
clasificada AS (
    SELECT
        ud.*,
        CASE
            WHEN ud.profesional_id_ultimo = 66 AND ud.dias_inactivo BETWEEN 30  AND 180 THEN 'dental_ortodoncia_180d'
            WHEN ud.profesional_id_ultimo IN (75, 69) AND ud.dias_inactivo BETWEEN 90 AND 365 THEN 'dental_endo_implanto_180d'
            WHEN ud.profesional_id_ultimo IN (55, 72) AND ud.dias_inactivo BETWEEN 90  AND 180 THEN 'dental_odonto_general_180d'
            WHEN ud.profesional_id_ultimo IN (55, 72) AND ud.dias_inactivo BETWEEN 181 AND 365 THEN 'dental_odonto_general_365d'
            WHEN ud.profesional_id_ultimo = 76 AND ud.dias_inactivo BETWEEN 90 AND 365 THEN 'dental_estetica_180d'
            ELSE NULL
        END AS subcohorte
    FROM ultima_dental ud
)
SELECT c.*
FROM clasificada c
WHERE c.telefono IS NOT NULL
  AND LENGTH(regexp_replace(c.telefono, '[^0-9]', '', 'g')) >= 8
  AND NOT EXISTS (
      SELECT 1 FROM bi.dental_opt_outs o WHERE o.phone = c.telefono
  )
  AND c.subcohorte IS NOT NULL;

COMMENT ON VIEW bi.v_dental_cohortes_contactables IS
    'Pool de pacientes dentales contactables por sub-cohorte.';

-- 5. Índices ----------------------------------------------------------------

CREATE INDEX IF NOT EXISTS idx_dental_consent_status
    ON bi.dental_consent (status);

CREATE INDEX IF NOT EXISTS idx_dental_consent_sent_at
    ON bi.dental_consent (consent_sent_at);

CREATE INDEX IF NOT EXISTS idx_dental_winback_envios_telefono
    ON bi.dental_winback_envios (telefono, enviado_at DESC);

CREATE INDEX IF NOT EXISTS idx_dental_winback_envios_fecha
    ON bi.dental_winback_envios (enviado_at);

CREATE INDEX IF NOT EXISTS idx_dental_opt_outs_phone
    ON bi.dental_opt_outs (phone);

-- 6. Query diagnóstica (ejecutar manualmente para estimar pool inicial) ------
-- SELECT subcohorte, COUNT(*) AS pacientes
-- FROM bi.v_dental_cohortes_contactables
-- GROUP BY subcohorte ORDER BY subcohorte;
