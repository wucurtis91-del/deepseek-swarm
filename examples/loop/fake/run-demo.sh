#!/usr/bin/env bash
# Offline demo: fake backends only. This does not call any paid API.
set -euo pipefail
cd "$(dirname "$0")/../../.."
RUN=$(mktemp -d "${TMPDIR:-/tmp}/deepseek-swarm-fake-demo.XXXXXX")
python3 scripts/swarm_loop.py start "$RUN" \
  --goal examples/loop/fake/goal.json \
  --config examples/loop/fake/config.json \
  --allow-fake-backend --fake-script examples/loop/fake/fake-script.json
python3 scripts/swarm_loop.py next "$RUN" --allow-fake-backend >"$RUN/step1.txt"
echo "step 1 packet: $RUN/step1.txt"
python3 scripts/swarm_loop.py decide "$RUN" examples/loop/fake/decision.dispatch.json \
  --allow-fake-backend \
  --fake-script examples/loop/fake/fake-script.json
python3 scripts/swarm_loop.py next "$RUN" --allow-fake-backend >"$RUN/step2.txt"
echo "step 2 packet: $RUN/step2.txt"
python3 scripts/swarm_loop.py decide "$RUN" examples/loop/fake/decision.complete.json \
  --allow-fake-backend \
  --fake-script examples/loop/fake/fake-script.json
python3 scripts/swarm_loop.py status "$RUN" --allow-fake-backend

printf "Run artifacts: %s\n" "$RUN"
