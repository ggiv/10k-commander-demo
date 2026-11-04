-- Every input behind the latest two annual periods, with what a reviewer must confirm.
WITH latest AS (
    SELECT *, dense_rank() OVER (PARTITION BY cik ORDER BY period_end DESC) AS recency
    FROM metrics
), needed AS (
    SELECT DISTINCT i.selected_fact_id FROM latest m JOIN metric_inputs i USING (metric_id)
    WHERE m.recency <= 2
)
SELECT s.cik, s.period_start, s.period_end, s.canonical, s.status, s.value, s.unit,
       s.quality_flags, c.accession, c.taxonomy, c.concept, f.filing_date,
       f.primary_document_url AS source_url,
       s.status = 'selected' AND EXISTS (
           SELECT 1 FROM reconciliation r
           WHERE r.cik = s.cik AND r.period_start = s.period_start AND r.period_end = s.period_end
               AND r.canonical = s.canonical AND r.value = s.value AND r.unit = s.unit
               AND r.source_url = f.primary_document_url
               AND len(trim(r.statement_reference)) > 0 AND len(trim(r.reviewer)) > 0
               AND r.reviewed_on IS NOT NULL AND len(trim(r.scope_notes)) > 0
       ) AS reconciled
FROM needed n JOIN selected_facts s USING (selected_fact_id)
LEFT JOIN fact_candidates c ON c.observation_id = s.observation_id
LEFT JOIN filings f ON f.cik = c.cik AND f.accession = c.accession
ORDER BY s.cik, s.period_end DESC, s.canonical
