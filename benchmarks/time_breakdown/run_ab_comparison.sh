#!/usr/bin/env bash
# Automated A/B Time Breakdown Benchmark: Passive vs Active Prefill
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BASE_DIR="${1:-/tmp/research-time-breakdown-ab}"

RUN_PASSIVE="$BASE_DIR/passive"
RUN_ACTIVE="$BASE_DIR/active"

rm -rf "$RUN_PASSIVE" "$RUN_ACTIVE"

echo "=========================================================="
echo " [1/2] RUNNING PASSIVE PREFILL BENCHMARK"
echo "=========================================================="
python3 "$SCRIPT_DIR/bench_resident.py" "$RUN_PASSIVE" --no-active-prefill
python3 "$SCRIPT_DIR/plot_breakdown.py" "$RUN_PASSIVE" --out "$RUN_PASSIVE/time-breakdown.png" --title "Time Breakdown: Passive Prefill"

sleep 2

echo "=========================================================="
echo " [2/2] RUNNING ACTIVE PREFILL BENCHMARK"
echo "=========================================================="
python3 "$SCRIPT_DIR/bench_resident.py" "$RUN_ACTIVE" --active-prefill
python3 "$SCRIPT_DIR/plot_breakdown.py" "$RUN_ACTIVE" --out "$RUN_ACTIVE/time-breakdown.png" --title "Time Breakdown: Active Prefill"

echo "=========================================================="
echo " BENCHMARK COMPLETE"
echo "=========================================================="
echo "Passive Output: $RUN_PASSIVE/time-breakdown.png"
echo "Active Output : $RUN_ACTIVE/time-breakdown.png"
