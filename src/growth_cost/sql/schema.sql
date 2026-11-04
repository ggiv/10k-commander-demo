CREATE TABLE source_snapshots (
    snapshot_id VARCHAR PRIMARY KEY, url VARCHAR NOT NULL, retrieved_at TIMESTAMPTZ NOT NULL,
    content_hash VARCHAR NOT NULL, storage_ref VARCHAR NOT NULL, run_id VARCHAR NOT NULL,
    kind VARCHAR NOT NULL, cik VARCHAR NOT NULL
);
CREATE TABLE companies (
    cik VARCHAR PRIMARY KEY, ticker VARCHAR, name VARCHAR, business_model VARCHAR,
    included BOOLEAN, rationale VARCHAR
);
CREATE TABLE mappings (
    canonical VARCHAR, taxonomy VARCHAR, concept VARCHAR, unit VARCHAR, priority INTEGER,
    cik VARCHAR, valid_from DATE, valid_to DATE, multiplier INTEGER,
    source_url VARCHAR, explanation VARCHAR
);
CREATE TABLE annotations (
    cik VARCHAR, period_start DATE, period_end DATE, flag VARCHAR,
    explanation VARCHAR, source_url VARCHAR
);
CREATE TABLE filings (
    cik VARCHAR, accession VARCHAR, form VARCHAR, filing_date DATE,
    accepted_at TIMESTAMPTZ, report_date DATE, primary_document_url VARCHAR,
    snapshot_id VARCHAR, PRIMARY KEY (cik, accession)
);
CREATE TABLE fact_candidates (
    observation_id VARCHAR PRIMARY KEY, snapshot_id VARCHAR, cik VARCHAR, accession VARCHAR,
    taxonomy VARCHAR, concept VARCHAR, unit VARCHAR, period_start DATE, period_end DATE,
    value DECIMAL(38,6), raw_value VARCHAR NOT NULL, form VARCHAR, filed DATE, fiscal_year INTEGER,
    fiscal_period VARCHAR, frame VARCHAR
);
CREATE TABLE releases (
    release_id VARCHAR PRIMARY KEY, cutoff DATE, mapping_version VARCHAR,
    definition_version VARCHAR, synthetic BOOLEAN, mapping_reviewed BOOLEAN,
    code_revision VARCHAR, code_hash VARCHAR, config_hash VARCHAR,
    source_manifest_hash VARCHAR, created_at TIMESTAMPTZ
);
CREATE TABLE reconciliation (
    cik VARCHAR, period_start DATE, period_end DATE, canonical VARCHAR,
    value DECIMAL(38,6), unit VARCHAR, source_url VARCHAR,
    statement_reference VARCHAR, reviewer VARCHAR, reviewed_on DATE, scope_notes VARCHAR,
    PRIMARY KEY (cik, period_start, period_end, canonical, unit)
);
