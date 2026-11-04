-- Selections worth reading in a filing. A finding is a question for review, not a proven error.
WITH selected AS (
    SELECT s.cik, s.period_start, s.period_end, s.canonical, s.unit,
           s.value::DOUBLE AS value, c.concept
    FROM selected_facts s JOIN fact_candidates c USING (observation_id)
    WHERE s.status = 'selected'
), alternates(canonical, concept, tolerance) AS (
    VALUES ('revenue', 'Revenues', 0.02),
           ('revenue', 'RevenueFromContractWithCustomerExcludingAssessedTax', 0.02),
           ('revenue', 'RevenueFromContractWithCustomerIncludingAssessedTax', 0.02),
           ('revenue', 'SalesRevenueNet', 0.02),
           ('stock_compensation', 'ShareBasedCompensation', 0.05),
           ('stock_compensation', 'AllocatedShareBasedCompensationExpense', 0.05)
), alternate_values AS (
    SELECT f.cik, f.period_start, f.period_end, a.canonical, a.tolerance, f.concept,
           f.value::DOUBLE AS value
    FROM fact_candidates f JOIN alternates a USING (concept)
    WHERE f.form LIKE '10-K%'
    QUALIFY row_number() OVER (
        PARTITION BY f.cik, f.period_start, f.period_end, f.concept ORDER BY f.filed DESC
    ) = 1
), alternate_tag_differs AS (
    SELECT 'alternate_tag_differs' AS check_name, s.cik, s.period_end, s.canonical, s.concept,
           s.value, a.concept AS other_concept, a.value AS other_value,
           'Another tag for this period differs by more than '
               || round(a.tolerance * 100)::INTEGER || '%' AS detail
    FROM selected s JOIN alternate_values a
        ON a.cik = s.cik AND a.canonical = s.canonical AND a.period_start = s.period_start
        AND a.period_end = s.period_end AND a.concept <> s.concept
    WHERE abs(a.value - s.value) > a.tolerance * abs(s.value)
), capex_matches_acquisition_line AS (
    SELECT DISTINCT 'capex_matches_acquisition_line' AS check_name, s.cik, s.period_end,
           s.canonical, s.concept, s.value, f.concept AS other_concept,
           f.value::DOUBLE AS other_value,
           'Selected capex equals a business acquisition line' AS detail
    FROM selected s JOIN fact_candidates f
        ON f.cik = s.cik AND f.period_start = s.period_start AND f.period_end = s.period_end
        AND f.unit = s.unit AND f.concept <> s.concept
    WHERE s.canonical = 'capex' AND s.value > 0 AND abs(f.value::DOUBLE) = s.value
        AND regexp_matches(f.concept, '(?i)acqui.*business|business.*(acqui|combination)')
), history AS (
    SELECT *,
           lag(concept) OVER yearly AS prior_concept,
           lag(value) OVER yearly AS prior_value,
           value / nullif(lag(value) OVER yearly, 0) AS ratio
    FROM selected
    WINDOW yearly AS (PARTITION BY cik, canonical, unit ORDER BY period_end)
), selected_concept_changed AS (
    SELECT 'selected_concept_changed' AS check_name, cik, period_end, canonical, concept, value,
           prior_concept AS other_concept, prior_value AS other_value,
           'The selected tag differs from the prior year' AS detail
    FROM history WHERE concept <> prior_concept
), unexplained_jump AS (
    SELECT 'unexplained_jump' AS check_name, h.cik, h.period_end, h.canonical, h.concept, h.value,
           h.concept AS other_concept, h.prior_value AS other_value,
           'Moved more than 3x while revenue moved less than 1.5x' AS detail
    FROM history h JOIN history r
        ON r.cik = h.cik AND r.period_end = h.period_end AND r.canonical = 'revenue'
    WHERE h.canonical IN ('capex', 'stock_compensation', 'operating_cash_flow')
        AND h.prior_value > 0 AND h.value > 0 AND r.value > 0
        AND (h.ratio > 3 OR h.ratio < 1.0 / 3)
        AND r.ratio BETWEEN 1.0 / 1.5 AND 1.5
        -- Small lines can triple without mattering to any margin.
        AND greatest(h.value, h.prior_value) >= 0.01 * r.value
)
SELECT f.check_name, f.cik, c.ticker, f.period_end, f.canonical, f.concept, f.value,
       f.other_concept, f.other_value, f.detail
FROM (
    SELECT * FROM alternate_tag_differs
    UNION ALL SELECT * FROM capex_matches_acquisition_line
    UNION ALL SELECT * FROM selected_concept_changed
    UNION ALL SELECT * FROM unexplained_jump
) f JOIN companies c USING (cik)
ORDER BY f.check_name, c.ticker, f.period_end, f.canonical, f.other_concept
