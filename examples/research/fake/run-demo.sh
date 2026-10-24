#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../../.." && pwd)"
RUN="$(mktemp -d /tmp/deepseek-swarm-research-demo.XXXXXX)"
CFG="$ROOT/examples/research/fake/config.json"
GOAL="$ROOT/examples/research/fake/goal.json"
FAKE="$ROOT/examples/research/fake/fake-script.json"
DISPATCH="$ROOT/examples/research/fake/decision.dispatch.json"

python3 "$ROOT/scripts/swarm_loop.py" start "$RUN" \
  --goal "$GOAL" --config "$CFG" --research \
  --allow-fake-backend --fake-script "$FAKE"
python3 "$ROOT/scripts/swarm_loop.py" decide "$RUN" "$DISPATCH" \
  --config "$CFG" --allow-fake-backend --fake-script "$FAKE"

python3 - "$RUN" <<'PY'
import json
import sys
from pathlib import Path
run = Path(sys.argv[1])
state = json.loads((run / "state.json").read_text())
packet = json.loads((run / state["pending_packet"]).read_text())
p = packet["research"]["pending_results"][0]
review = {
    "action": "review",
    "reason": "The key result is ambiguous; record an inconclusive interpretation.",
    "research": {
        "review_ids": [p["id"]],
        "interpretations": [{
            "observation": p["observation"],
            "hypothesis": "h1",
            "hypothesis_version": 1,
            "relation": "inconclusive",
            "conditions": "supplied observation; one run",
            "rationale": "The result is compatible with both hypotheses.",
            "source": "planner",
        }],
        "direction": {
            "choice": "experiment",
            "hypothesis": "h1",
            "alternatives": ["h2"],
            "evidence": p["evidence"],
            "reason": "Use a discriminating experiment before switching.",
        },
    },
}
(run / "review.json").write_text(json.dumps(review, ensure_ascii=False, indent=2))
PY

python3 "$ROOT/scripts/swarm_loop.py" decide "$RUN" "$RUN/review.json" \
  --config "$CFG" --allow-fake-backend --fake-script "$FAKE"

python3 - "$RUN" <<'PY'
import json
import sys
from pathlib import Path
run = Path(sys.argv[1])
state = json.loads((run / "state.json").read_text())
ev = next(eid for eid, rec in state["evidence"].items()
         if rec.get("completion_usable") and rec.get("task") == "probe")
decision = {
    "action": "complete",
    "reason": "The goal was to record a direction; the direction review is recorded.",
    "evidence": [ev],
    "completion": [{"criterion": "g1", "met": True, "evidence": [ev]}],
}
(run / "complete.json").write_text(json.dumps(decision, ensure_ascii=False, indent=2))
PY

python3 "$ROOT/scripts/swarm_loop.py" decide "$RUN" "$RUN/complete.json" \
  --config "$CFG" --allow-fake-backend --fake-script "$FAKE"
python3 "$ROOT/scripts/swarm_loop.py" status "$RUN" \
  --config "$CFG" --allow-fake-backend
echo "Research demo artifacts: $RUN"
