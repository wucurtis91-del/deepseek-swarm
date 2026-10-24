# Research-mode walkthrough

This example uses the optional research ledger. It does **not** establish a
scientific conclusion by itself; the controller records observations,
planner interpretations and direction decisions, then requires a review when a
predeclared key experiment completes.

## Shortest path

```bash
cp examples/loop/roles.example.json /tmp/research-roles.json
python3 scripts/swarm_loop.py start runs/research \
    --goal examples/research/goal.json \
    --config /tmp/research-roles.json --research
python3 scripts/swarm_loop.py next runs/research
python3 scripts/swarm_loop.py decide runs/research examples/research/decision.dispatch.json \
    --config /tmp/research-roles.json
python3 scripts/swarm_loop.py next runs/research
```

After the key task completes, `packet.research.pending_results` contains an
`id`, `observation`, `evidence` and `hypothesis_ids`. Copy those real IDs into
`decision.review.json`, then submit it:

```bash
python3 scripts/swarm_loop.py decide runs/research /tmp/decision.review.json \
    --config /tmp/research-roles.json
```

The review action only changes research state. It creates no task. To run a
follow-up experiment or change the task graph, use `dispatch`, `verify` or
`replan` and include the same `research` payload in that decision.

## Contract

- Observations are derived from completed task artifacts and have stable IDs,
  artifact hashes, source groups and histories.
- Interpretations are planner judgments with `supports`, `against`,
  `inconclusive` or `mixed`; they reference an observation and a hypothesis
  version.
- A completed `key_experiment` enters `pending_results` even if nobody labelled
  it against anything. The next planner decision must explain it and record a
  direction; `complete` is rejected while any pending review remains.
- A controller-visible prediction mismatch is reported only when a
  `research_prediction` check is declared. Without such a check, the controller
  does not claim to have detected a scientific contradiction.
- A review must use a currently pending review ID. Resolved IDs do not cover a
  later event. Interpretations reference the pending observation, a related
  hypothesis and its current version.
- `switch` and `pause` are investment decisions, not automatic truth labels.
  A switch does not change the hypothesis status, invalidate unrelated
  artifacts, or make a dependent task ineligible. Use an explicit
  `hypothesis_updates` status change for a validity change; definition/scope
  changes bump the version and mark dependent tasks `needs_review`.
- `complete` applies research updates, propagates dependent-task changes and
  refreshes pending/validity in one transaction before completion is judged.
  A rejected combined decision rolls back its research and planner updates.
- A replayed research payload is an idempotent no-op, not permission for new
  work. A new `dispatch`/`verify`/`repair`/`replan` still has to satisfy the
  current pending-review checks.
- A `needs_review` task can be cancelled with `replan.cancel`; its artifact and
  research history remain, its completion evidence is superseded, and a
  replacement route can complete the goal.
- `review` is the only action that can update research state without changing
  the task graph. It cannot carry `tasks`.
- Invalidated observations and legacy refutations remain in history and create
  a recheck event; they are not silently reclassified as current support and
  cannot receive new `supports`/`against` interpretations. The invalidation
  event itself can be reviewed and closed without resubmitting that support.
- An interpretation can use `supersedes` to correct an active interpretation
  with the same observation, hypothesis version and conditions. The old record
  stays in history but is inactive.

## Offline fixtures

`benchmarks/research/fixtures.py` contains the fixed-evidence replays and a
small choice environment. `ARMS` exposes `base`, `reflection` and `research`
configuration labels for a future controlled run. The fixtures contain
evaluator-only ground truth; do not send that section to a model.

A complete offline fake run, including a pending review and a research-only
decision, is in `examples/research/fake/run-demo.sh`:

```bash
bash examples/research/fake/run-demo.sh
```
