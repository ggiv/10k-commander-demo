#!/bin/sh
set -eu
: "${SEC_USER_AGENT:?Set an identified SEC_USER_AGENT in the runner environment}"
: "${GROWTH_COST_DATA_DIR:?Choose persistent project storage}"
task_project_root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$task_project_root"
exec uv run --locked growth-cost refresh --cutoff "${1:-$(date +%F)}"
