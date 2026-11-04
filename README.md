# 10k Commander Demo

An auditable SEC financial-metrics backend, published as **reference code only**.

Python retrieves and preserves SEC disclosures; DuckDB SQL selects annual facts, calculates metrics, and validates their lineage. MotherDuck commands upload immutable releases and compare local and hybrid queries.

**This repository provides no real company data, findings, or analysis.** It ships only code, synthetic fixtures, and generic configuration templates. Nothing here is investment, accounting, or financial advice, and the example numbers are engineering fixtures, not observations about any company. Anything you retrieve by running the live commands is your own responsibility; see [data and source treatment](DATA_LICENSE.md).

## Run the offline example

Requires Python 3.12+ and [uv](https://docs.astral.sh/uv/guides/projects/). The Python distribution is named `10k-commander-demo`; its command is `growth-cost` and its import package is `growth_cost`. Run from the repository root:

```sh
uv sync --locked
uv run growth-cost demo
uv run growth-cost report coverage
uv run growth-cost inspect --cik 9000000001 --period-end 2025-12-31 --metric revenue_growth
```

`demo` makes no network requests, and it refuses to replace a non-synthetic publication, so keep live data in its own `GROWTH_COST_DATA_DIR`. It builds and publishes a **synthetic** release under `data/`: three fictional companies, six input years, five output years, five primary metrics plus annual operating-margin change. Every JSON response includes the release ID and synthetic-data label. Fictional fixture accessions and filing URLs are not real SEC filings.

The latest Example Applications inputs independently reconcile to revenue growth of 25%, operating and project-defined free cash flow margins of 30%, stock-compensation intensity of 10%, and growth in weighted-average basic shares of approximately 1.85185%. These numbers are engineering examples, not company observations. [Fixture inputs and hand-worked expectations](tests/fixtures/README.md) explain them.

```sh
uv run growth-cost report overview --window-start 2025-02-01 --window-end 2026-01-31
uv run growth-cost report trend_summaries
uv run growth-cost report exceptions
uv run growth-cost report metric_definitions
uv run growth-cost bundle data/reproduction.tar.gz
uv run growth-cost export data/export
```

Overview requires an explicit reporting-end window and retains companies with no observation in it. It returns exact periods, definitions, eligibility, flags, and metric IDs. `--business-model subscription` narrows the fixture overview. No business formula is recalculated in Python exports.

## Check the backend

```sh
uv run ruff check .
uv run ruff format --check .
uv run mypy src
uv run pytest -m 'not cloud'
```

These checks and the complete offline example run locally without credentials. Tests cover conflicting values and units, duplicate disclosures, later comparative figures, missing inputs, nonpositive denominators, fiscal gaps, share-count basis changes, source corruption, interrupted builds, rerun safety, and metric-to-source tracing.

## Retrieve sources and build a candidate (bring your own configuration)

The repository contains templates only: `config/companies.example.csv` (header), `config/concepts.example.yml` (generic us-gaap mappings, unreviewed), and `config/reconciliation.example.csv` (header). Copy them outside the repository, choose your own companies, and supply your actual identifying contact in the request header:

```sh
export GROWTH_COST_DATA_DIR=/persistent/path/growth-cost
export SEC_USER_AGENT='Your organization your-real-contact@example.com'
uv run growth-cost refresh --companies my/companies.csv --mappings my/concepts.yml --cutoff 2026-11-04
```

Replace the example contact before requesting SEC data. SEC retrieval needs no API key. Requests are sequential, paced at two per second, conditionally cached, and retried at most four times for transient failures. Submission history files are followed. Transport headers and contact details are never written to release manifests or run logs. See [SEC access guidance](https://www.sec.gov/search-filings/edgar-search-assistance/accessing-edgar-data).

`refresh` retrieves sources and builds a candidate; it **does not publish**. The JSON response contains its release ID. Raw responses are compressed and content-addressed under the persistent data directory. Per-run manifests live alongside the archive objects, so old captures remain rebuildable. An unchanged payload reuses its object, and the same sources/configuration/code reuse the same release. A release's `cutoff` is the latest source date within the requested cutoff (`requested_cutoff` records what was asked for), so moving the date forward with no new source dates does not create a release.

To build from an existing capture without contacting the SEC:

```sh
uv run growth-cost build --bundle /persistent/path/growth-cost/archive/manifest.json \
  --companies my/companies.csv --mappings my/concepts.yml \
  --reconciliation my/reconciliation.csv --cutoff 2026-11-04
```

List the inputs to reconcile and trace any candidate metric before it is published:

```sh
uv run growth-cost report reconciliation_inputs --release-id RELEASE_ID
uv run growth-cost inspect --release-id RELEASE_ID --cik CIK --period-end YYYY-MM-DD --metric revenue_growth
```

Fill in your reconciliation file against the filings, review the coverage/exception reports and `report mapping_audit`, and then explicitly publish:

```sh
uv run growth-cost validate RELEASE_ID
uv run growth-cost publish RELEASE_ID
uv run growth-cost status
```

Publishing mappings you have not reviewed requires `--unreviewed`; every metric then carries `mapping_review_pending`. Setting `reviewed: true` alone does not satisfy the publication gate: the latest two annual periods for each included company need all six measures and independently recorded filing reconciliations, including prior-year denominator inputs.

## Find or change a calculation

| Responsibility | Location |
|---|---|
| HTTP, pacing, immutable source archive | `src/growth_cost/sec.py` |
| Source observations and filing inventory | `src/growth_cost/normalize.py` |
| Data types and configuration boundaries | `src/growth_cost/contracts.py` |
| Ordered batch execution | `src/growth_cost/pipeline.py` |
| Selection policy | `src/growth_cost/sql/selected_facts.sql` |
| All financial formulas and input roles | `src/growth_cost/sql/metrics.sql` |
| Publication-blocking checks | `src/growth_cost/sql/checks.sql` |
| Local publication and feasibility gate | `src/growth_cost/warehouse.py` |
| Published-data reports and evidence inspection | `src/growth_cost/analysis.py` |
| MotherDuck upload, hybrid benchmark, reader verification | `src/growth_cost/cloud.py` |

Use `uv run growth-cost --help` for the full command list and `uv run growth-cost COMMAND --help` for command options. MotherDuck commands read the writer credential from `motherduck_token`; reader verification also requires `MOTHERDUCK_READER_TOKEN` from a separate account and `MOTHERDUCK_SHARE_URL`. Supply credentials through the environment and keep share URIs private. The application does not load `.env` implicitly.

## Interpretation and limits

This is latest-eligible-disclosure history as of a cutoff, not a reconstruction of what was knowable on each historical date. An API retrieval timestamp is not a historical market-data snapshot. Period keys are exact dates; source fiscal-year labels are not comparison keys. Missing and ambiguous values remain null with reasons.

Project-defined free cash flow subtracts **cash purchases of PP&E**, stored as a positive amount, from operating cash flow. It can exclude capitalized software and other investment. It is a non-GAAP project measure, not management's adjusted cash flow. Stock compensation is shown separately; weighted-average basic share growth is a share-base proxy, not direct holder dilution. Acquisitions, working capital, repurchases, and scope differences require source review.

The example does not establish performance, cost, causal, valuation, or investment conclusions.

Code: [MIT](LICENSE). Authored synthetic fixtures: [CC0](DATA_LICENSE.md). No license is asserted over third-party disclosures.
