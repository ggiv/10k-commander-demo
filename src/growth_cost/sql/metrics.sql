-- Financial metric definitions, calculations, and input lineage.
CREATE TABLE metric_definitions AS
SELECT * FROM (VALUES
 ('revenue_growth', 'Annual revenue growth', '%',
  '100 × (revenue / prior annual revenue − 1)',
  'Reported growth includes acquisitions, disposals and currency effects.'),
 ('operating_margin', 'GAAP operating margin', '%',
  '100 × operating income / revenue', 'GAAP costs are retained.'),
 ('free_cash_flow_margin', 'Project-defined free cash flow margin', '%',
  '100 × (operating cash flow − cash purchases of PP&E) / revenue',
  'Non-GAAP; other investment and capitalized software may be excluded. Inspect scope notes.'),
 ('stock_compensation_intensity', 'Stock-based compensation intensity', '%',
  '100 × stock-based compensation / revenue', 'Not a cash spending ratio; disclosure scope matters.'),
 ('basic_share_growth', 'Growth in weighted-average basic shares', '%',
  '100 × (basic weighted-average shares / prior annual basic weighted-average shares − 1)',
  'Proxy for share-base change, not direct holder dilution. Buybacks consume cash.'),
 ('operating_margin_change', 'Annual change in GAAP operating margin', 'pp',
  'Operating margin − prior comparable annual operating margin',
  'Percentage points; requires consecutive comparable annual periods.')
) AS d(metric, label, unit, formula, interpretation);

CREATE TABLE metric_input_requirements AS
SELECT * FROM (VALUES
 ('revenue_growth', 'revenue', 'current_revenue', false),
 ('revenue_growth', 'revenue', 'prior_revenue', true),
 ('operating_margin', 'operating_income', 'operating_income', false),
 ('operating_margin', 'revenue', 'current_revenue', false),
 ('free_cash_flow_margin', 'operating_cash_flow', 'operating_cash_flow', false),
 ('free_cash_flow_margin', 'capex', 'cash_purchases_of_ppe', false),
 ('free_cash_flow_margin', 'revenue', 'current_revenue', false),
 ('stock_compensation_intensity', 'stock_compensation', 'stock_compensation', false),
 ('stock_compensation_intensity', 'revenue', 'current_revenue', false),
 ('basic_share_growth', 'basic_shares', 'current_basic_shares', false),
 ('basic_share_growth', 'basic_shares', 'prior_basic_shares', true),
 ('operating_margin_change', 'operating_income', 'operating_income', false),
 ('operating_margin_change', 'revenue', 'current_revenue', false),
 ('operating_margin_change', 'operating_income', 'prior_operating_income', true),
 ('operating_margin_change', 'revenue', 'prior_revenue', true)
) AS i(metric, canonical, role, prior_period);

CREATE TABLE period_comparability AS
SELECT *,
       lag(period_start) OVER w AS prior_start, lag(period_end) OVER w AS prior_end,
       lag(duration_days) OVER w AS prior_duration_days, lag(flags) OVER w AS prior_flags,
       coalesce(
           period_start = lag(period_end) OVER w + INTERVAL 1 DAY
           AND duration_days BETWEEN 360 AND 373
           AND lag(duration_days) OVER w BETWEEN 360 AND 373
           AND abs(duration_days - lag(duration_days) OVER w) <= 7, false
       ) AS comparable
FROM periods WINDOW w AS (PARTITION BY cik ORDER BY period_end);

CREATE TABLE metric_inputs AS
SELECT sha256(concat_ws('|', r.release_id, p.cik, p.period_start, p.period_end,
                        i.metric)) AS metric_id,
       r.release_id, p.cik, p.period_start, p.period_end, i.metric, i.role,
       s.selected_fact_id, coalesce(s.status, 'missing') AS input_status,
       i.prior_period
FROM period_comparability p CROSS JOIN metric_input_requirements i CROSS JOIN releases r
LEFT JOIN selected_facts s ON s.cik = p.cik AND s.canonical = i.canonical
    AND s.period_start = CASE WHEN i.prior_period THEN p.prior_start ELSE p.period_start END
    AND s.period_end = CASE WHEN i.prior_period THEN p.prior_end ELSE p.period_end END
QUALIFY dense_rank() OVER (PARTITION BY p.cik ORDER BY p.period_end DESC) <= 5;

CREATE TABLE metrics AS
WITH inputs AS (
    SELECT i.metric_id, i.release_id, i.cik, i.period_start, i.period_end, i.metric,
           bool_or(i.input_status = 'missing') AS has_missing,
           bool_or(i.input_status = 'ambiguous') AS has_ambiguity,
           bool_or(i.prior_period) AS needs_prior,
           max(s.value) FILTER (WHERE i.role = 'current_revenue') AS revenue,
           max(s.value) FILTER (WHERE i.role = 'prior_revenue') AS prior_revenue,
           max(s.value) FILTER (WHERE i.role = 'operating_income') AS operating_income,
           max(s.value) FILTER (WHERE i.role = 'prior_operating_income') AS prior_operating_income,
           max(s.value) FILTER (WHERE i.role = 'operating_cash_flow') AS operating_cash_flow,
           max(s.value) FILTER (WHERE i.role = 'cash_purchases_of_ppe') AS capex,
           max(s.value) FILTER (WHERE i.role = 'stock_compensation') AS stock_compensation,
           max(s.value) FILTER (WHERE i.role = 'current_basic_shares') AS basic_shares,
           max(s.value) FILTER (WHERE i.role = 'prior_basic_shares') AS prior_basic_shares,
           string_agg(DISTINCT nullif(s.quality_flags, ''), ';') AS input_flags
    FROM metric_inputs i LEFT JOIN selected_facts s USING (selected_fact_id)
    GROUP BY i.metric_id, i.release_id, i.cik, i.period_start, i.period_end, i.metric
), classified AS (
    SELECT i.*, p.comparable,
           CASE WHEN i.has_ambiguity THEN 'ambiguous'
                WHEN i.has_missing THEN 'missing'
                WHEN i.needs_prior AND NOT p.comparable THEN 'noncomparable'
                WHEN i.metric = 'basic_share_growth'
                     AND (contains(p.flags, 'share_basis_change')
                          OR contains(p.prior_flags, 'share_basis_change')) THEN 'noncomparable'
                WHEN i.metric = 'basic_share_growth' AND i.prior_basic_shares <= 0
                     THEN 'invalid_denominator'
                -- Moves this large are IPO conversions, splits, or scale errors, not share growth.
                WHEN i.metric = 'basic_share_growth'
                     AND abs(100 * (i.basic_shares / i.prior_basic_shares - 1)) >= 50
                     THEN 'noncomparable'
                WHEN i.metric NOT IN ('basic_share_growth', 'revenue_growth') AND i.revenue <= 0
                     THEN 'invalid_denominator'
                WHEN i.metric IN ('revenue_growth', 'operating_margin_change')
                     AND i.prior_revenue <= 0 THEN 'invalid_denominator'
                ELSE 'available' END AS eligibility_status
    FROM inputs i JOIN period_comparability p USING (cik, period_start, period_end)
), calculated AS (
    SELECT *, CASE WHEN eligibility_status = 'available' THEN CASE metric
        WHEN 'revenue_growth' THEN 100 * (revenue / prior_revenue - 1)
        WHEN 'operating_margin' THEN 100 * operating_income / revenue
        WHEN 'free_cash_flow_margin' THEN 100 * (operating_cash_flow - capex) / revenue
        WHEN 'stock_compensation_intensity' THEN 100 * stock_compensation / revenue
        WHEN 'basic_share_growth' THEN 100 * (basic_shares / prior_basic_shares - 1)
        WHEN 'operating_margin_change'
            THEN 100 * operating_income / revenue - 100 * prior_operating_income / prior_revenue
        END ELSE NULL END AS value
    FROM classified
), compared AS (
    -- Review thresholds: growth over 100%, or a margin moving over 50 points in a year.
    SELECT *, CASE
        WHEN metric IN ('revenue_growth', 'basic_share_growth') THEN abs(value) > 100
        WHEN metric = 'operating_margin_change' THEN abs(value) > 50
        WHEN comparable THEN abs(value - lag(value) OVER w) > 50
        END AS large_change
    FROM calculated WINDOW w AS (PARTITION BY cik, metric ORDER BY period_end)
)
SELECT metric_id, release_id, cik, period_start, period_end, metric, value,
       eligibility_status, r.definition_version,
       concat_ws(';', nullif(input_flags, ''),
           CASE WHEN large_change THEN 'large_change_review' END,
           CASE WHEN r.synthetic THEN 'synthetic_fixture' END,
           CASE WHEN NOT r.mapping_reviewed THEN 'mapping_review_pending' END
       ) AS quality_flags,
       CASE eligibility_status
           WHEN 'missing' THEN 'Required input unavailable; inspect metric_inputs'
           WHEN 'ambiguous' THEN 'Competing source observations require review'
           WHEN 'noncomparable' THEN 'Annual periods or share-count bases are not comparable, or shares moved 50% or more'
           WHEN 'invalid_denominator' THEN 'Denominator must be positive'
           ELSE '' END AS unavailable_reason
FROM compared CROSS JOIN (
    SELECT definition_version, synthetic, mapping_reviewed FROM releases
) r;

CREATE VIEW coverage AS
SELECT c.cik, c.ticker, d.metric,
       count(m.metric_id) AS periods, count(m.value) AS available,
       count(m.metric_id) FILTER (WHERE m.eligibility_status = 'missing') AS missing,
       count(m.metric_id) FILTER (WHERE m.eligibility_status = 'ambiguous') AS ambiguous,
       count(m.metric_id) FILTER (WHERE m.eligibility_status IN
           ('noncomparable', 'invalid_denominator')) AS ineligible
FROM companies c CROSS JOIN metric_definitions d LEFT JOIN metrics m
    ON c.cik = m.cik AND d.metric = m.metric
WHERE c.included GROUP BY c.cik, c.ticker, d.metric;

CREATE VIEW metric_lineage AS
SELECT m.metric_id, m.release_id, m.cik, m.period_start, m.period_end,
       m.metric, m.value AS metric_value, m.eligibility_status, m.quality_flags,
       i.role, i.input_status, s.selected_fact_id, s.canonical, s.value AS input_value,
       s.unit AS input_unit, s.period_start AS input_period_start,
       s.period_end AS input_period_end, s.selection_reason, s.mapping_version,
       s.observation_id, c.value AS original_value, c.taxonomy, c.concept, c.unit,
       c.accession, f.filing_date, f.accepted_at, f.primary_document_url,
       c.snapshot_id, sn.url AS snapshot_url, sn.retrieved_at, sn.content_hash, sn.storage_ref,
       f.snapshot_id AS inventory_snapshot_id
FROM metrics m JOIN metric_inputs i USING (metric_id)
LEFT JOIN selected_facts s USING (selected_fact_id)
LEFT JOIN fact_candidates c ON s.observation_id = c.observation_id
LEFT JOIN filings f ON c.cik = f.cik AND c.accession = f.accession
LEFT JOIN source_snapshots sn ON c.snapshot_id = sn.snapshot_id;

-- Endpoint margin changes and average reported growth describe different quantities.
-- Incomplete windows return no value. Acquisition exclusion is the one exception: it averages
-- the remaining years, reports excluded_periods, and is never labelled complete.
CREATE VIEW trend_summaries AS
WITH scenarios AS (
    SELECT * FROM (VALUES (3, false), (5, false), (3, true), (5, true))
      AS v(window_years, exclude_acquisition_years)
), ranked AS (
    SELECT m.*, dense_rank() OVER (PARTITION BY cik ORDER BY period_end DESC) AS recency
    FROM metrics m
), windowed AS (
    SELECT m.*, s.window_years, s.exclude_acquisition_years,
           NOT s.exclude_acquisition_years OR NOT contains(m.quality_flags, 'acquisition') AS kept
    FROM ranked m CROSS JOIN scenarios s WHERE m.recency <= s.window_years
), summarized AS (
    SELECT release_id, cik, metric, window_years, exclude_acquisition_years,
           min(period_end) FILTER (kept) AS first_end, max(period_end) FILTER (kept) AS last_end,
           count(*) AS window_periods,
           count(*) FILTER (kept) AS periods, count(value) FILTER (kept) AS available,
           count(*) FILTER (NOT kept) AS excluded_periods,
           avg(value) FILTER (kept) AS mean_of_available,
           arg_max(value, period_end) FILTER (kept AND value IS NOT NULL)
               - arg_min(value, period_end) FILTER (kept AND value IS NOT NULL) AS change_of_available
    FROM windowed GROUP BY release_id, cik, metric, window_years, exclude_acquisition_years
)
SELECT release_id, cik, metric, window_years, exclude_acquisition_years,
       first_end, last_end, periods, available, excluded_periods,
       CASE WHEN window_periods = window_years AND periods > 0 AND available = periods
            THEN mean_of_available END AS mean_value,
       CASE WHEN window_periods = window_years AND periods > 0 AND available = periods
            THEN change_of_available END AS endpoint_change,
       available = window_years AS complete
FROM summarized;
