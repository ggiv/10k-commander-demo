-- Eligible facts are ranked by mapping priority, then filing date.
-- Period keys use exact dates; fiscal-year labels are retained as metadata.
CREATE TABLE eligible_candidates AS
SELECT c.*, m.canonical, m.priority, m.multiplier,
       c.value * m.multiplier AS normalized_value,
       f.form AS filing_form, f.filing_date, f.accepted_at, f.report_date, f.primary_document_url,
       m.source_url AS mapping_source_url, m.explanation AS mapping_explanation,
       dense_rank() OVER (
           PARTITION BY c.cik, c.period_start, c.period_end, m.canonical, m.unit
           ORDER BY m.priority ASC, f.filing_date DESC, f.accepted_at DESC NULLS LAST
       ) AS precedence
FROM fact_candidates c
JOIN companies co ON c.cik = co.cik AND co.included
JOIN mappings m ON c.taxonomy = m.taxonomy AND c.concept = m.concept AND c.unit = m.unit
    AND (m.cik = '*' OR m.cik = c.cik)
    AND c.period_end BETWEEN m.valid_from AND m.valid_to
JOIN filings f ON c.cik = f.cik AND c.accession = f.accession
CROSS JOIN releases r
-- The accession links a fact to its filing; the inventory is authoritative for form and date.
WHERE f.form IN ('10-K', '10-K/A')
    AND f.filing_date <= r.cutoff AND c.period_end <= r.cutoff
    AND c.value IS NOT NULL AND c.period_start IS NOT NULL
    AND date_diff('day', c.period_start, c.period_end) + 1 BETWEEN 330 AND 400;

-- Conflicting start dates for the same annual end date exclude that period.
CREATE TABLE period_conflicts AS
SELECT * FROM (SELECT DISTINCT cik, period_start, period_end
               FROM eligible_candidates WHERE canonical = 'revenue')
QUALIFY count(*) OVER (PARTITION BY cik, period_end) > 1;

CREATE TABLE ranked_periods AS
SELECT cik, period_start, period_end,
       dense_rank() OVER (PARTITION BY cik ORDER BY period_end DESC) AS recency
FROM (SELECT DISTINCT cik, period_start, period_end
      FROM eligible_candidates e WHERE canonical = 'revenue' AND NOT EXISTS (
          SELECT 1 FROM period_conflicts x WHERE x.cik = e.cik AND x.period_end = e.period_end
      ));

-- Up to six available input years support five output years.
CREATE TABLE periods AS
SELECT p.cik, p.period_start, p.period_end,
       date_diff('day', p.period_start, p.period_end) + 1 AS duration_days,
       coalesce(string_agg(DISTINCT a.flag, ';' ORDER BY a.flag), '') AS flags
FROM ranked_periods p LEFT JOIN annotations a
    ON p.cik = a.cik AND p.period_start = a.period_start AND p.period_end = a.period_end
WHERE p.recency <= 6
GROUP BY p.cik, p.period_start, p.period_end;

CREATE TABLE selection_candidates AS
SELECT e.* FROM eligible_candidates e JOIN periods p USING (cik, period_start, period_end)
WHERE e.precedence = 1;

-- Priority still decides, but a newer differing value under another concept stays visible.
CREATE TABLE priority_conflicts AS
SELECT DISTINCT w.cik, w.period_start, w.period_end, w.canonical, w.unit
FROM selection_candidates w JOIN eligible_candidates o
    ON w.cik = o.cik AND w.period_start = o.period_start AND w.period_end = o.period_end
    AND w.canonical = o.canonical AND w.unit = o.unit
WHERE o.priority > w.priority AND o.filing_date > w.filing_date
    AND o.normalized_value <> w.normalized_value;

CREATE TABLE selected_facts AS
WITH canonical AS (
    SELECT DISTINCT canonical, unit FROM mappings
), evidence AS (
    SELECT cik, period_start, period_end, canonical, unit,
           count(DISTINCT normalized_value) AS competing_values,
           count(DISTINCT observation_id) AS observation_count,
           min(observation_id) AS chosen_observation
    FROM selection_candidates GROUP BY ALL
)
SELECT sha256(concat_ws('|', r.release_id, p.cik, p.period_start, p.period_end,
                        k.canonical, k.unit)) AS selected_fact_id,
       r.release_id, p.cik, p.period_start, p.period_end, k.canonical, k.unit,
       CASE WHEN e.competing_values = 1 THEN c.normalized_value ELSE NULL END AS value,
       CASE WHEN e.competing_values = 1 THEN e.chosen_observation ELSE NULL END AS observation_id,
       CASE WHEN e.competing_values IS NULL THEN 'missing'
            WHEN e.competing_values > 1 THEN 'ambiguous' ELSE 'selected' END AS status,
       CASE WHEN e.competing_values IS NULL THEN 'No eligible mapped annual observation'
            WHEN e.competing_values > 1 THEN 'Conflicting values with equal precedence; review required'
            ELSE 'Reviewed mapping priority; latest eligible filing/acceptance; identical values deduplicated'
       END AS selection_reason,
       coalesce(e.observation_count, 0) AS top_observation_count,
       r.mapping_version,
       concat_ws(';', nullif(p.flags, ''),
           CASE WHEN c.report_date <> p.period_end THEN 'later_comparative' END,
           CASE WHEN p.duration_days NOT BETWEEN 360 AND 373 THEN 'unusual_period_length' END,
           CASE WHEN e.observation_count > 1 THEN 'repeated_observations' END,
           CASE WHEN c.form <> c.filing_form OR c.filed <> c.filing_date
                THEN 'filing_metadata_mismatch' END,
           CASE WHEN pc.cik IS NOT NULL THEN 'later_value_under_lower_priority_concept' END,
           CASE WHEN k.canonical = 'stock_compensation' AND c.normalized_value < 0
                THEN 'negative_stock_compensation' END
       ) AS quality_flags
FROM periods p CROSS JOIN canonical k CROSS JOIN releases r
LEFT JOIN evidence e ON p.cik = e.cik AND p.period_start = e.period_start
    AND p.period_end = e.period_end AND k.canonical = e.canonical AND k.unit = e.unit
LEFT JOIN selection_candidates c ON c.observation_id = e.chosen_observation
    AND c.canonical = k.canonical
LEFT JOIN priority_conflicts pc ON p.cik = pc.cik AND p.period_start = pc.period_start
    AND p.period_end = pc.period_end AND k.canonical = pc.canonical AND k.unit = pc.unit
QUALIFY row_number() OVER (PARTITION BY selected_fact_id ORDER BY c.priority) = 1;

CREATE TABLE exceptions AS
SELECT 'selection' AS category, s.cik, s.period_start, s.period_end, s.canonical,
       s.status AS code, s.selection_reason AS explanation
FROM selected_facts s WHERE s.status <> 'selected'
UNION ALL
SELECT 'selection', s.cik, s.period_start, s.period_end, s.canonical,
       'later_value_under_lower_priority_concept',
       'A later filing reports a different value under a lower-priority concept; review the mapping'
FROM selected_facts s JOIN priority_conflicts pc USING (cik, period_start, period_end, canonical, unit)
UNION ALL
SELECT DISTINCT 'eligibility', e.cik, e.period_start, e.period_end, e.canonical,
       'filing_metadata_mismatch',
       'Company Facts form or filing date differs from the filing inventory; inventory values used'
FROM eligible_candidates e JOIN periods p USING (cik, period_start, period_end)
WHERE e.form <> e.filing_form OR e.filed <> e.filing_date
UNION ALL
SELECT 'eligibility', x.cik, x.period_start, x.period_end, 'revenue',
       'conflicting_period_definition',
       'Annual revenue facts disagree on this fiscal year start; year withheld pending review'
FROM period_conflicts x
UNION ALL
SELECT DISTINCT 'eligibility', c.cik, c.period_start, c.period_end, m.canonical,
       CASE WHEN c.unit <> m.unit THEN 'conflicting_unit'
            WHEN f.accession IS NULL THEN 'filing_not_in_inventory'
            WHEN c.period_start IS NULL THEN 'instant_not_duration'
            ELSE 'ineligible_period_length' END,
       'Observation retained but ineligible; inspect candidate and mapping'
FROM fact_candidates c JOIN mappings m ON c.taxonomy = m.taxonomy AND c.concept = m.concept
    AND (m.cik = '*' OR m.cik = c.cik)
JOIN companies co ON c.cik = co.cik AND co.included
LEFT JOIN filings f ON c.cik = f.cik AND c.accession = f.accession
CROSS JOIN releases r
WHERE c.form IN ('10-K', '10-K/A') AND c.filed <= r.cutoff AND c.period_end <= r.cutoff
    AND (c.unit <> m.unit OR f.accession IS NULL OR c.period_start IS NULL
         OR date_diff('day', c.period_start, c.period_end) + 1 NOT BETWEEN 330 AND 400);
