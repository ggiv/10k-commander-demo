-- Every row is a failed check. Blockers prevent publication; reviews remain visible.
CREATE TABLE validation_results AS
SELECT 'blocker' AS severity, 'period_grain' AS check_name, count(*) AS failures
FROM (SELECT cik, period_end FROM periods GROUP BY ALL HAVING count(*) > 1)
HAVING count(*) > 0
UNION ALL
SELECT 'blocker', 'selected_fact_grain', count(*)
FROM (SELECT cik, period_start, period_end, canonical, unit FROM selected_facts
      GROUP BY ALL HAVING count(*) <> 1) HAVING count(*) > 0
UNION ALL
SELECT 'blocker', 'metric_grain', count(*)
FROM (SELECT cik, period_start, period_end, metric FROM metrics
      GROUP BY ALL HAVING count(*) <> 1) HAVING count(*) > 0
UNION ALL
SELECT 'blocker', 'missing_metric', count(*) FROM (
    SELECT * FROM periods
    QUALIFY dense_rank() OVER (PARTITION BY cik ORDER BY period_end DESC) <= 5
) p CROSS JOIN metric_definitions d
LEFT JOIN metrics m ON p.cik = m.cik AND p.period_start = m.period_start
    AND p.period_end = m.period_end AND d.metric = m.metric
WHERE m.metric_id IS NULL HAVING count(*) > 0
UNION ALL
SELECT 'blocker', 'broken_selected_lineage', count(*)
FROM selected_facts s LEFT JOIN fact_candidates c USING (observation_id)
LEFT JOIN source_snapshots sn ON c.snapshot_id = sn.snapshot_id
LEFT JOIN filings f ON c.cik = f.cik AND c.accession = f.accession
LEFT JOIN source_snapshots fs ON f.snapshot_id = fs.snapshot_id
WHERE s.status = 'selected' AND (c.observation_id IS NULL OR sn.snapshot_id IS NULL
    OR f.accession IS NULL OR fs.snapshot_id IS NULL OR c.unit <> s.unit
    OR c.period_start <> s.period_start OR c.period_end <> s.period_end OR c.cik <> s.cik)
HAVING count(*) > 0
UNION ALL
SELECT 'blocker', 'available_metric_missing_input', count(*) FROM metrics m
JOIN metric_inputs i USING (metric_id) LEFT JOIN selected_facts s USING (selected_fact_id)
WHERE m.eligibility_status = 'available' AND (s.selected_fact_id IS NULL OR s.status <> 'selected')
HAVING count(*) > 0
UNION ALL
SELECT 'blocker', 'metric_input_arity', count(*) FROM (
    SELECT m.metric_id FROM metrics m
    LEFT JOIN metric_inputs i USING (metric_id)
    GROUP BY m.metric_id, m.metric HAVING count(i.role) <>
       (SELECT count(*) FROM metric_input_requirements r WHERE r.metric = m.metric)
) HAVING count(*) > 0
UNION ALL
SELECT 'blocker', 'nonpositive_shares_or_negative_outflow', count(*) FROM selected_facts
WHERE status = 'selected' AND ((canonical = 'capex' AND value < 0)
    OR (canonical = 'basic_shares' AND value <= 0)) HAVING count(*) > 0
UNION ALL
SELECT 'blocker', 'nonfinite_or_status_value', count(*) FROM metrics
WHERE (value IS NOT NULL AND NOT isfinite(value))
    OR (eligibility_status = 'available') <> (value IS NOT NULL) HAVING count(*) > 0
UNION ALL
SELECT 'blocker', 'mapped_value_not_representable', count(DISTINCT c.observation_id)
FROM fact_candidates c JOIN mappings m ON c.taxonomy = m.taxonomy AND c.concept = m.concept
    AND c.unit = m.unit AND (m.cik = '*' OR m.cik = c.cik)
    AND c.period_end BETWEEN m.valid_from AND m.valid_to
CROSS JOIN releases r
WHERE c.value IS NULL AND c.form IN ('10-K', '10-K/A') AND c.filed <= r.cutoff
    AND c.period_end <= r.cutoff AND c.period_start IS NOT NULL
    AND date_diff('day', c.period_start, c.period_end) + 1 BETWEEN 330 AND 400
    AND c.period_end >= coalesce(
        (SELECT min(p.period_end) FROM periods p WHERE p.cik = c.cik), DATE '1900-01-01')
HAVING count(DISTINCT c.observation_id) > 0
UNION ALL
-- An orphan fact blocks only if it could change the result: nothing was selected for its slot,
-- its value differs, or it would outrank the selection. Others stay in the exception report.
SELECT 'blocker', 'mapped_annual_filing_missing_inventory', count(DISTINCT c.observation_id)
FROM fact_candidates c JOIN mappings m ON c.taxonomy = m.taxonomy AND c.concept = m.concept
    AND c.unit = m.unit AND (m.cik = '*' OR m.cik = c.cik)
    AND c.period_end BETWEEN m.valid_from AND m.valid_to
LEFT JOIN filings f ON c.cik = f.cik AND c.accession = f.accession CROSS JOIN releases r
LEFT JOIN selected_facts s ON s.cik = c.cik AND s.period_start = c.period_start
    AND s.period_end = c.period_end AND s.canonical = m.canonical AND s.unit = m.unit
LEFT JOIN selection_candidates w ON w.observation_id = s.observation_id
    AND w.canonical = s.canonical
WHERE c.form IN ('10-K', '10-K/A') AND c.filed <= r.cutoff AND c.period_end <= r.cutoff
    AND (s.value IS NULL OR m.priority < w.priority
         OR (m.priority = w.priority
             AND (c.value * m.multiplier <> s.value OR c.filed > w.filing_date)))
    AND c.period_start IS NOT NULL
    AND date_diff('day', c.period_start, c.period_end) + 1 BETWEEN 330 AND 400
    AND c.period_end >= coalesce(
        (SELECT min(p.period_end) FROM periods p WHERE p.cik = c.cik), DATE '1900-01-01')
    AND f.accession IS NULL HAVING count(DISTINCT c.observation_id) > 0
UNION ALL
SELECT 'review', 'selection_and_period_exceptions', count(*) FROM exceptions
WHERE code IN ('conflicting_period_definition', 'filing_metadata_mismatch',
               'later_value_under_lower_priority_concept')
HAVING count(*) > 0
UNION ALL
SELECT 'review', 'large_changes', count(*) FROM metrics
WHERE contains(quality_flags, 'large_change_review') HAVING count(*) > 0
UNION ALL
SELECT 'review', 'negative_stock_compensation', count(*) FROM selected_facts
WHERE status = 'selected' AND canonical = 'stock_compensation' AND value < 0 HAVING count(*) > 0
UNION ALL
SELECT 'review', 'ambiguous_inputs', count(*) FROM selected_facts
WHERE status = 'ambiguous' HAVING count(*) > 0
UNION ALL
SELECT 'review', 'no_annual_periods', count(*) FROM companies c
WHERE c.included AND NOT EXISTS (SELECT 1 FROM periods p WHERE p.cik = c.cik)
HAVING count(*) > 0;
