-- analysis schema — the 5-tier opportunity funnel output (P0 of Analysis-Architecture.md).
-- Mirrors the warehouse style: explicit FKs, natural-key UNIQUE with ON CONFLICT, created_at/updated_at,
-- JSONB evidence, pgvector halfvec columns. Every statement is IDEMPOTENT (IF NOT EXISTS) so this is
-- safe to re-apply on every deploy / after a power loss.
--
-- Requires pgvector with halfvec (the pgvector/pgvector:pg16 image ships it). Run against market_research:
--   psql "$PG_DSN" -f connectors/analysis_schema.sql
--
-- Only P0-P3 columns are populated by the local tiers; problem_statements/opportunities/
-- opportunity_competitors are created here (full DDL per spec) but filled by the later paid tiers.

CREATE EXTENSION IF NOT EXISTS vector;          -- provides halfvec + hnsw

CREATE SCHEMA IF NOT EXISTS analysis;

-- pain_signals: deduped, embedded, classified unit of pain (Tier 0+1 survivors)
CREATE TABLE IF NOT EXISTS analysis.pain_signals (
    id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    source          TEXT NOT NULL,            -- reddit_comment|reddit_post|app_store|google_play|shopify|youtube_comment|youtube_transcript|...
    source_id       TEXT NOT NULL,            -- natural id of the raw row
    icp_guess       TEXT,                     -- agency|ecom|saas_operator|null
    intent          TEXT NOT NULL,            -- pain|feature_request|praise|question|offtopic
    text            TEXT NOT NULL,
    lexicon_hits    TEXT[] NOT NULL DEFAULT ARRAY[]::TEXT[],
    score           INT,                      -- upvotes / stars / engagement proxy
    dup_count       INT NOT NULL DEFAULT 1,   -- near-duplicates collapsed in (frequency!)
    created_at_src  TIMESTAMPTZ,              -- authored time (drives the time series)
    embedding       halfvec(1024),            -- qwen3-embedding:0.6b, Matryoshka-truncatable
    cluster_id      BIGINT,                   -- set in Tier 2
    deduped         BOOLEAN DEFAULT FALSE,    -- DRIFT: added by tier1_embed.py
    ingested_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (source, source_id)                -- idempotent re-runs
);
CREATE INDEX IF NOT EXISTS pain_signals_hnsw
    ON analysis.pain_signals USING hnsw (embedding halfvec_cosine_ops)
    WITH (m = 16, ef_construction = 64);
CREATE INDEX IF NOT EXISTS pain_signals_cluster ON analysis.pain_signals (cluster_id);
CREATE INDEX IF NOT EXISTS pain_signals_time    ON analysis.pain_signals (created_at_src);

-- problem_clusters: Tier 2 output; carries wave-acceleration + cross-source spread
CREATE TABLE IF NOT EXISTS analysis.problem_clusters (
    id                  BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    label               TEXT,
    medoid_signal_id    BIGINT REFERENCES analysis.pain_signals(id),
    member_count        INT NOT NULL,
    total_dup_weight    INT NOT NULL,         -- sum(dup_count) = true frequency
    source_spread       INT NOT NULL,         -- # distinct sources (corroboration gate input)
    sources             TEXT[] NOT NULL DEFAULT ARRAY[]::TEXT[],
    trend_acceleration  NUMERIC,              -- slope of quarterly member counts 2020->2026
    yoy_ratio           NUMERIC,              -- last-12mo / prior-12mo members
    centroid            halfvec(1024),
    cluster_key         TEXT UNIQUE,           -- DRIFT: added by tier2_cluster.py
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- problem_statements: Tier 3 output (DeepSeek-extracted, merged)  [created now, filled by paid tier]
CREATE TABLE IF NOT EXISTS analysis.problem_statements (
    id                  BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    cluster_id          BIGINT REFERENCES analysis.problem_clusters(id),
    statement           TEXT NOT NULL,
    icp                 TEXT NOT NULL,        -- agency|ecom|saas_operator
    job_to_be_done      TEXT,
    current_workaround  TEXT,
    wtp_quotes          JSONB NOT NULL DEFAULT '[]'::jsonb,   -- [{quote, signal_id}]
    severity            INT,                  -- 1-5
    frequency_note      TEXT,
    pre_score           NUMERIC,              -- cheap rank to select Tier 4 candidates
    extracted_by        TEXT NOT NULL DEFAULT 'deepseek-v3.2',
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (cluster_id)
);

-- opportunities: Tier 4 output; 3-axis scored, web-validated  [created now, filled by paid tier]
CREATE TABLE IF NOT EXISTS analysis.opportunities (
    id                      BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    problem_statement_id    BIGINT NOT NULL REFERENCES analysis.problem_statements(id),
    title                   TEXT NOT NULL,
    statement               TEXT NOT NULL,
    icp                     TEXT NOT NULL,
    evidence_refs           JSONB NOT NULL,   -- {pain_signal_ids, web_sources:[{url,claim}], studies}  (no claim without citations)
    -- AXIS 1: WEDGE-ABILITY (sub-scores 0-5)
    pain_severity           NUMERIC,
    pain_frequency          NUMERIC,          -- from total_dup_weight
    wtp_signal              NUMERIC,
    icp_reachability        NUMERIC,
    build_complexity        NUMERIC,          -- inverse: low complexity = high score
    saturation_ok           BOOLEAN NOT NULL, -- HARD GATE: false if 3+ funded incumbents on exact ICP
    wedge_score             NUMERIC,
    -- AXIS 2: WAVE-ALIGNMENT
    wave                    TEXT,             -- ai_native_ops|vertical_ai_agents|data_sovereignty|null
    trend_acceleration      NUMERIC,          -- carried from cluster
    capital_inflow          JSONB,            -- {funded_company_ids, total_raised, n_rounds_12mo}
    structural_tailwind     TEXT,
    wave_score              NUMERIC,
    -- AXIS 3: EDGE-FIT (ecom/D2C operator, AI-native builder, India cost base)
    edge_fit_notes          TEXT,
    edge_score              NUMERIC,
    -- competition / saturation detail
    competitors             JSONB,            -- [{name, funded, stage, exact_icp_match, url}]
    n_funded_incumbents     INT,
    -- sizing + base rates
    tam_estimate            TEXT,
    sam_estimate            TEXT,
    success_rate_evidence   JSONB,
    -- adversarial verification
    rebuttal                TEXT,             -- strongest bear case that survived
    -- THE BET
    expansion_vector        TEXT NOT NULL,    -- named path wedge -> structural wave
    final_score             NUMERIC,
    validated_by            TEXT,
    status                  TEXT NOT NULL DEFAULT 'scored', -- scored|shortlisted|rejected|building
    created_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (problem_statement_id)
);
CREATE INDEX IF NOT EXISTS opportunities_rank ON analysis.opportunities (final_score DESC);

-- competitor links to your own funding data, first-class
CREATE TABLE IF NOT EXISTS analysis.opportunity_competitors (
    opportunity_id      BIGINT NOT NULL REFERENCES analysis.opportunities(id) ON DELETE CASCADE,
    funded_company_id   BIGINT,               -- FK to funded_companies once it lands
    name                TEXT NOT NULL,
    exact_icp_match     BOOLEAN NOT NULL DEFAULT false,
    notes               TEXT,
    PRIMARY KEY (opportunity_id, name)
);
