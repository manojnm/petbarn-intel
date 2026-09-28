-- Petbarn Intel knowledge store. One SQLite file, WAL mode.
-- See docs/data-model.md for the ER diagram and rationale.

PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

-- ---------------------------------------------------------------------------
-- Catalog
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS products (
    product_id          TEXT PRIMARY KEY,
    name                 TEXT NOT NULL,
    brand                TEXT,
    url                  TEXT NOT NULL,
    url_key              TEXT,
    categories_json       TEXT NOT NULL DEFAULT '[]',
    pet_types_json        TEXT NOT NULL DEFAULT '[]',
    description          TEXT,
    specification_json    TEXT NOT NULL DEFAULT '{}',
    ingredients          TEXT,
    feeding_guide        TEXT,
    features_json         TEXT NOT NULL DEFAULT '[]',
    price_min            REAL,
    price_max            REAL,
    currency             TEXT NOT NULL DEFAULT 'AUD',
    stock_status         TEXT,
    rating_value         REAL,
    review_count         INTEGER,
    written_review_count  INTEGER,
    tier                 TEXT NOT NULL DEFAULT 'census',
    sources_json          TEXT NOT NULL DEFAULT '{}',
    confidence_json        TEXT NOT NULL DEFAULT '{}',
    completeness          REAL NOT NULL DEFAULT 0.0,
    disagreements_json     TEXT NOT NULL DEFAULT '{}',
    scraped_at            TEXT,
    updated_at            TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_products_brand ON products (brand);
CREATE INDEX IF NOT EXISTS idx_products_tier ON products (tier);
CREATE INDEX IF NOT EXISTS idx_products_rating ON products (rating_value);
CREATE INDEX IF NOT EXISTS idx_products_price ON products (price_min);

CREATE TABLE IF NOT EXISTS variants (
    sku          TEXT PRIMARY KEY,
    product_id   TEXT NOT NULL REFERENCES products (product_id) ON DELETE CASCADE,
    name         TEXT NOT NULL,
    size         TEXT,
    gtin         TEXT,
    price        REAL,
    currency     TEXT NOT NULL DEFAULT 'AUD',
    member_price REAL,
    availability TEXT,
    image_url    TEXT,
    url          TEXT
);

CREATE INDEX IF NOT EXISTS idx_variants_product ON variants (product_id);

CREATE TABLE IF NOT EXISTS price_history (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    sku          TEXT NOT NULL,
    product_id   TEXT NOT NULL,
    price        REAL,
    member_price REAL,
    availability TEXT,
    recorded_at  TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_price_history_sku ON price_history (sku, recorded_at);

CREATE TABLE IF NOT EXISTS vendor_summaries (
    product_id       TEXT PRIMARY KEY REFERENCES products (product_id) ON DELETE CASCADE,
    paragraph        TEXT,
    bullets          TEXT,
    disclaimer       TEXT,
    vendor_created_at TEXT,
    fetched_at        TEXT NOT NULL
);

-- ---------------------------------------------------------------------------
-- Reviews & Q&A
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS reviews (
    review_id          TEXT PRIMARY KEY,
    product_id         TEXT NOT NULL REFERENCES products (product_id) ON DELETE CASCADE,
    rating             INTEGER NOT NULL,
    title              TEXT,
    text               TEXT NOT NULL,
    author             TEXT,
    submitted_at       TEXT,
    is_recommended     INTEGER,
    helpful_votes      INTEGER NOT NULL DEFAULT 0,
    not_helpful_votes  INTEGER NOT NULL DEFAULT 0,
    verified_purchaser INTEGER,
    incentivized       INTEGER,
    syndicated         INTEGER,
    vader_compound     REAL,
    vader_label        TEXT,
    sample_reasons_json TEXT NOT NULL DEFAULT '[]',
    fetched_at         TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_reviews_product ON reviews (product_id);
CREATE INDEX IF NOT EXISTS idx_reviews_rating ON reviews (product_id, rating);
CREATE INDEX IF NOT EXISTS idx_reviews_submitted ON reviews (product_id, submitted_at);

CREATE TABLE IF NOT EXISTS review_aspects (
    review_id      TEXT NOT NULL REFERENCES reviews (review_id) ON DELETE CASCADE,
    aspect         TEXT NOT NULL,
    sentiment      TEXT NOT NULL,
    evidence       TEXT,
    prompt_version TEXT NOT NULL,
    model          TEXT NOT NULL,
    extracted_at   TEXT NOT NULL,
    PRIMARY KEY (review_id, aspect, prompt_version)
);

CREATE INDEX IF NOT EXISTS idx_review_aspects_review ON review_aspects (review_id);

CREATE TABLE IF NOT EXISTS review_embeddings (
    review_id  TEXT PRIMARY KEY REFERENCES reviews (review_id) ON DELETE CASCADE,
    model      TEXT NOT NULL,
    dim        INTEGER NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS questions (
    question_id    TEXT PRIMARY KEY,
    product_id     TEXT NOT NULL REFERENCES products (product_id) ON DELETE CASCADE,
    question_text  TEXT NOT NULL,
    answer_texts_json TEXT NOT NULL DEFAULT '[]',
    submitted_at   TEXT,
    fetched_at     TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_questions_product ON questions (product_id);

-- ---------------------------------------------------------------------------
-- Full text + vector search (hybrid retrieval, reciprocal rank fusion at
-- query time -- see scraping/reviews/search.py)
-- ---------------------------------------------------------------------------

-- Deliberately NOT a contentless (content='') table: contentless FTS5 tables
-- discard UNINDEXED column values entirely, which would break filtering by
-- product_id. This duplicates review_id/product_id/text (small overhead at
-- our scale) in exchange for being able to filter and retrieve them directly.
CREATE VIRTUAL TABLE IF NOT EXISTS fts_reviews USING fts5 (
    review_id UNINDEXED,
    product_id UNINDEXED,
    text
);

-- fts_reviews and vec_reviews (sqlite-vec, created lazily in Python because it
-- needs the extension loaded and a fixed dimension) are kept in sync by the
-- enrichment service, not by triggers, so re-embedding on a schema/model
-- change is an explicit, auditable step.

-- ---------------------------------------------------------------------------
-- Operations: scrape runs, field-level events, incidents, agent traces
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS scrape_runs (
    run_id           TEXT PRIMARY KEY,
    run_type         TEXT NOT NULL,
    status           TEXT NOT NULL DEFAULT 'running',
    started_at       TEXT NOT NULL,
    finished_at      TEXT,
    pages_fetched    INTEGER NOT NULL DEFAULT 0,
    products_touched INTEGER NOT NULL DEFAULT 0,
    reviews_fetched  INTEGER NOT NULL DEFAULT 0,
    errors           INTEGER NOT NULL DEFAULT 0,
    notes_json        TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS field_events (
    event_id      TEXT PRIMARY KEY,
    run_id        TEXT,
    product_id    TEXT NOT NULL,
    field_name    TEXT NOT NULL,
    source        TEXT NOT NULL,
    success       INTEGER NOT NULL,
    fallback_used INTEGER NOT NULL DEFAULT 0,
    error         TEXT,
    recorded_at   TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_field_events_run ON field_events (run_id);
CREATE INDEX IF NOT EXISTS idx_field_events_field ON field_events (field_name, recorded_at);
CREATE INDEX IF NOT EXISTS idx_field_events_product ON field_events (product_id);

CREATE TABLE IF NOT EXISTS incidents (
    incident_id  TEXT PRIMARY KEY,
    kind         TEXT NOT NULL,
    severity     TEXT NOT NULL,
    product_id   TEXT,
    field_name   TEXT,
    detail       TEXT NOT NULL,
    opened_at    TEXT NOT NULL,
    resolved_at  TEXT
);

CREATE INDEX IF NOT EXISTS idx_incidents_open ON incidents (resolved_at);

CREATE TABLE IF NOT EXISTS raw_cache (
    cache_key    TEXT PRIMARY KEY,
    url          TEXT NOT NULL,
    method       TEXT NOT NULL DEFAULT 'GET',
    status_code  INTEGER,
    tier         TEXT,
    content_path TEXT NOT NULL,
    content_type TEXT,
    etag         TEXT,
    last_modified TEXT,
    fetched_at   TEXT NOT NULL,
    expires_at   TEXT,
    latency_ms   REAL
);

CREATE INDEX IF NOT EXISTS idx_raw_cache_url ON raw_cache (url);

CREATE TABLE IF NOT EXISTS traces (
    trace_id        TEXT PRIMARY KEY,
    session_id      TEXT NOT NULL,
    turn_id         TEXT NOT NULL,
    node            TEXT NOT NULL,
    kind            TEXT NOT NULL,
    name            TEXT NOT NULL,
    args_summary     TEXT,
    result_summary   TEXT,
    latency_ms      REAL,
    tokens_in       INTEGER,
    tokens_out      INTEGER,
    cost_usd        REAL,
    error           TEXT,
    created_at      TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_traces_session ON traces (session_id, turn_id);
CREATE INDEX IF NOT EXISTS idx_traces_created ON traces (created_at);

CREATE TABLE IF NOT EXISTS chat_sessions (
    session_id  TEXT PRIMARY KEY,
    created_at  TEXT NOT NULL,
    title       TEXT
);

CREATE TABLE IF NOT EXISTS chat_messages (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id    TEXT NOT NULL REFERENCES chat_sessions (session_id) ON DELETE CASCADE,
    role          TEXT NOT NULL,
    content       TEXT NOT NULL,
    turn_id       TEXT,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at    TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_chat_messages_session ON chat_messages (session_id, id);
