# deepseek-swarm

**English** · [简体中文](README.zh-CN.md)

**Claude plans. DeepSeek executes. Verified results drive the next step.**

A technical skill for Claude Code and Codex, with a standalone Python CLI for parallel tasks and
persistent workflows. Reuse your current session as the planner, or let a configured Claude backend
run the loop. The controller schedules work, checks artifacts, tracks budgets and keeps the state
needed to continue, repair, replan or finish.

[Quick start](#install) · [Persistent loop](#the-persistent-loop) · [Research mode](#optional-research-mode) · [Cost control](#context-and-cost-control) · [Examples](examples/loop/README.md) · [完整中文指南](README.zh-CN.md)

## Choose a workflow

- **Batch reading and extraction:** parallel workers turn long inputs into short, checkable results.
- **Code and diagnostics:** investigate, implement in worktrees, run checks and repair from saved patches.
- **Research and experiments:** preserve observations and hypothesis versions, review key results and
  record whether to continue, test an alternative or change direction.

There are two entry points:

| Entry point | What it does |
|---|---|
| `scripts/swarm.py` | The original one-shot runner: `check`, `plan`, `run`. Existing tasks files keep working. |
| `scripts/swarm_loop.py` | The persistent decision loop: `start`, `next`, `decide`, `auto`, `status`, `resume`, `cancel`, `artifacts`, `validate`. |

Existing one-shot task files keep working. Standard library only, Python 3.9+.

```
Claude planner ──decision──► controller ──task batch──► DeepSeek workers
      ▲                          │                         │
      │                          ├── deterministic checks  │
      │                          ├── selected semantic review
      │                          ├── scope / patch checks
      │                          └── budget, state, recovery
      └──── decision packet ◄──── evidence + unresolved + budget
```

The planner decides direction; the controller owns state, scheduling, budgets, deterministic checks,
retries, persistence, cancellation and the record of what actually happened. The loop stops only
when every goal criterion has current, verified evidence accepted by the controller.

## Live progress

Every run writes `live/index.html` (in the results folder, or the loop's run folder): a page that refreshes
every 2 seconds and shows, per task, whether the worker is thinking, reading a file or writing, how much it
has thought so far and the latest of it, elapsed time, and cost so far (the part still streaming is an
estimate and is marked as one). `live/status.json` holds the same data.

```bash
python3 scripts/swarm.py watch RESULTS --serve --open   # pop up the page (http on 127.0.0.1)
python3 scripts/swarm.py watch RESULTS                  # the same, in the terminal
python3 scripts/swarm.py watch RESULTS --once --thinking  # one snapshot, for an agent to relay
```

Workers run the Claude CLI with `--output-format stream-json`, so thinking, tool calls and per-message
usage arrive while the call is still running. `scripts/swarm_loop.py watch RUN_DIR` takes the same options.

## What the controller provides

- **Isolated execution.** Workers run in Claude Code's `--bare`/`--safe-mode` mode with a minimal
  environment. Code tasks normally run in a separate git worktree and report a patch.
- **Continuity without replaying context.** The planner gets a compact decision packet, not every
  worker transcript. Full artifacts stay on disk and can be requested by reference.
- **Controller-side acceptance.** Deterministic checks run first; semantic review is opt-in and
  strict about criterion IDs and types. Code acceptance uses patches, paths and hashes.
- **Explicit accounting.** Unknown prices, missing usage and the external planner's unmeasured
  usage are recorded as unknown/partial.

## Install

```bash
git clone https://github.com/wucurtis91-del/deepseek-swarm ~/.claude/skills/deepseek-swarm
export DEEPSEEK_API_KEY=sk-...          # or keep a DeepSeek provider in CC Switch
python3 ~/.claude/skills/deepseek-swarm/scripts/swarm.py check
```

Claude Code CLI (`claude`), Python 3.9+, a DeepSeek key and (for automatic planning) a Claude
backend are required.

Try the complete offline workflows first. These use fake backends and make no paid API calls:

```bash
cd ~/.claude/skills/deepseek-swarm
bash examples/loop/fake/run-demo.sh
bash examples/research/fake/run-demo.sh
```

For real work, copy `examples/loop/roles.example.json` to `roles.json`, configure the models and
credentials available to your account, and define your goal and completion criteria in `goal.json`.
Commands below run from the repository root. See [the loop walkthrough](examples/loop/README.md)
for JSON examples and an external-host session.

## The persistent loop

### External-host mode (reuse your current Claude session)

Your current Claude Code or Codex session reads a decision packet and writes a structured decision.
This is the default mode: planning stays in the session you are already using.

```bash
python3 scripts/swarm_loop.py start runs/demo --goal examples/loop/goal.json \
    --config examples/loop/roles.example.json
python3 scripts/swarm_loop.py next runs/demo            # print the decision packet
# Claude writes decision.json (schema below)
python3 scripts/swarm_loop.py decide runs/demo decision.json
python3 scripts/swarm_loop.py next runs/demo            # next packet after execution
python3 scripts/swarm_loop.py status runs/demo
python3 scripts/swarm_loop.py cancel runs/demo --reason "user stopped"
```

`decide` applies the decision, executes the ready batch, and writes the next packet. `next` is
read-only apart from recovery of an interrupted run. The external Claude session's token usage is
not visible to this process: it is recorded as `external_unmetered`, and cost coverage is `partial`,
with partial coverage.

### Automatic-host mode

The controller calls the configured `planner` backend with the same decision protocol and loops
without a human relaying messages.

```bash
python3 scripts/swarm_loop.py start runs/auto --goal goal.json --config roles.json --mode auto
python3 scripts/swarm_loop.py auto runs/auto --max-steps 20
```

Automatic mode uses a real backend. The CLI refuses a `fake` backend unless
`--allow-fake-backend` is passed explicitly; fake mode is for tests and offline demos only. If the
planner is unavailable, the run saves a `paused` status and a `stop_reason`.

### Optional research mode

Enable research mode with `--research` at `start`, `goal.research: true`, or
`loop.research_mode: true`. It keeps a research ledger and brings key results back to the planner
for interpretation and a direction decision.

```bash
python3 scripts/swarm_loop.py start runs/research \
    --goal examples/research/goal.json --config roles.json --research
python3 scripts/swarm_loop.py next runs/research
# write a decision based on packet.research.pending_results
python3 scripts/swarm_loop.py decide runs/research decision.json --config roles.json
```

The shortest walkthrough and JSON templates are in [examples/research/README.md](examples/research/README.md).

**Data model.** Research mode keeps four separate layers:

- `state.research.observations`: controller-created records for completed artifact evidence, with
  `evidence`, `version`, `artifact_sha256`, `source`, `source_group`, status and revision history;
- `state.research.interpretations`: planner judgments that reference an observation and a hypothesis
  version, with `supports` / `against` / `inconclusive` / `mixed`, conditions, rationale, source,
  active/needs-review state, history and optional `supersedes` references for a correction;
- `state.hypotheses`: versioned hypotheses (`open`, `supported`, `contested`, `contradicted`,
  `inconclusive`, `superseded`, `legacy`), scope, core flag, alternatives and current valid evidence;
- `state.research.reviews`: direction decisions with a trigger, evidence references and reason.

A controller-computed `source_group` is based on the task, artifact hash, input hashes, upstream
versions and optional `experiment_group`. A planner-provided `independent: true` is retained only as
a claim and never treated as proof of independence. Reusing the same observation does not create a
second independent support.

**Task relationship.** Tasks can declare:

```json
{"tests_hypotheses": ["h1", "h2"], "depends_on_hypotheses": ["h1"],
 "key_experiment": true,
 "research_prediction": {"hypothesis": "h1", "check": "unit", "expect_exit": 0}}
```

`tests_hypotheses` says what the experiment tests; `depends_on_hypotheses` says what must remain
valid for the task result to be usable. The controller records the hypothesis versions a task
depended on when it starts. If a definition or scope change bumps a hypothesis version, or an
explicit `hypothesis_updates` entry changes its status,
dependent tasks are marked `needs_review` and cannot be used to complete the goal until the
dependency is handled; tasks that only tested the old hypothesis remain valid observations. A
`switch`/`narrow` direction is an investment choice and does **not** change hypothesis status or
dependent task eligibility; use `hypothesis_updates` for an explicit validity change.
`key_experiment` puts a completed result into `packet.research.pending_results` even when no one has
labelled it `against`. `research_prediction` creates a `prediction_mismatch` event only when the
declared program check has an unexpected exit code; without a declared check the controller does not
claim that a scientific contradiction was detected.

**Decision protocol.** `review` is a research-only action that updates research state without
creating a task:

```json
{
  "action": "review",
  "reason": "Explain the pending result and choose the next direction.",
  "research": {
    "review_ids": ["rr-0001"],
    "interpretations": [{
      "observation": "obs-0001", "hypothesis": "h1", "hypothesis_version": 1,
      "relation": "inconclusive", "conditions": "one pilot",
      "rationale": "ambiguous", "source": "planner"
    }],
    "direction": {"choice": "experiment", "hypothesis": "h1",
                  "alternatives": ["h2"], "reason": "run a discriminating test"}
  }
}
```

`research` can also accompany `dispatch`, `verify`, `replan` or `complete` so one planner call can
review and act.
A review ID must name a currently pending event; a resolved ID cannot cover a later pending result.
A key/prediction result must be explained against its own observation, a related hypothesis and that
hypothesis's current version, and the decision must record a direction. Batch review is allowed, but
`complete` is rejected while any research event is still pending. `complete` may appear in the same
decision as research updates; the controller applies the updates, propagates hypothesis changes to
dependent tasks and pending events, refreshes evidence validity, and only then validates completion
against that final state. If it fails, the research and planner updates in that decision are rolled
back together. After a successful commit, reopening and refreshing must not create new dependency
failures or pending events caused by that same decision. An exact replay of an already applied
review payload is an idempotent no-op, not coverage for a new event: a new `dispatch`, `verify`,
`repair` or `replan` that carries the old payload is still rejected while current pending results
remain. Interpretations may use `supersedes` to correct an active interpretation only when the
observation, hypothesis, version and conditions match; superseded interpretations stay in history
but are inactive and cannot support a hypothesis again. A review with no task graph change does not
create a fake dispatch task.

`direction.choice` is `continue`, `experiment`, `narrow`, `switch`, `pause` or `stop`. These are
investment decisions, not truth or validity labels. A switch or pause does **not** require a negative
result; opportunity cost and resource constraints are valid reasons. It does not rewrite the
hypothesis status or invalidate a dependent task's completed result. A positive single-hypothesis
calibration, replication or measurement task is also valid; experiments do not need two hypotheses.
The controller records the reason and does not auto-convert `against` evidence into
`contradicted`. Evidence interpretations still derive `open`/`supported`/`contested`/
`needs_review`; a switch or pause does not. `contradicted`/`superseded` and definition/scope
changes come only from an explicit `hypothesis_updates` entry (definition/scope changes bump the
version).

**Invalidation and recovery.** Before a research review, the controller reuses the completion
validator to re-check the recorded artifact, code patch, input hashes and the full dependency chain,
with a shared cache. Evidence that becomes stale/superseded or whose files no longer match
invalidates its observation, marks dependent interpretations `needs_review`, and preserves revision
history. Invalidated observations cannot receive new `supports`/`against` interpretations; the
resulting `evidence_invalidated` event can be reviewed and closed without resubmitting the invalid
support. Remaining valid supports are retained; a partial support loss does not negate the whole
hypothesis. Legacy `hypotheses`/`refuted` records are migrated to `legacy`/`unassessed` when research
mode is enabled; they are not silently promoted to current truth. A `needs_review` task can be
cancelled through `replan.cancel`; its artifact and research history remain, while its completion
evidence becomes superseded and affected downstream results are invalidated so a replacement route
can finish the goal. Research state is in the normal state/event journal, so recovery keeps
observations, review IDs, review fingerprints and pending events.

The research packet is bounded and includes active hypotheses, pending/reviewed results, valid and
invalidated evidence references, source groups, recent reviews, totals for
hypotheses/pending/invalidated/reviewed records, `pending_offset`/`pending_returned`,
`next_offset`, `truncated_sections` and `limits`. Pending results are paginated; fetch
another page with `context_requests: [{"research_page": N}]`. If the required structure, including an empty page,
exceeds its budget, `limits.over_budget` is set and `limits.min_chars` reports the required space
instead of silently pretending it fit. Trimming/compression must retain research IDs, statuses,
hypothesis versions, pending evidence references and invalidation references; an empty, null,
reshaped or tampered `research` section is rejected rather than accepted as compression.

No research event adds a model call. Direction review uses the existing planner accounting/budget.

### Roles and provider isolation

`roles.json`:

```json
{
  "roles": {
    "planner": {
      "backend": "claude_cli", "provider": "anthropic",
      "model": "claude-opus-5-5", "auth": "local_login", "tools": "read"
    },
    "worker": {
      "backend": "claude_cli", "provider": "deepseek",
      "model": "deepseek-flash", "endpoint": "https://api.deepseek.com/anthropic",
      "auth": "api_key", "api_key_env": "DEEPSEEK_API_KEY", "tools": "read"
    },
    "reviewer": {
      "backend": "claude_cli", "provider": "deepseek",
      "model": "deepseek-flash", "endpoint": "https://api.deepseek.com/anthropic",
      "auth": "api_key", "api_key_env": "DEEPSEEK_API_KEY", "tools": "read"
    },
    "critic": {
      "backend": "claude_cli", "provider": "deepseek",
      "model": "deepseek-flash", "endpoint": "https://api.deepseek.com/anthropic",
      "auth": "api_key", "api_key_env": "DEEPSEEK_API_KEY", "enabled": false
    }
  }
}
```

- Each role has its own backend, provider, model, endpoint, credential source, tool preset and
  output cap.
- A Claude model cannot be sent to a DeepSeek endpoint, or a DeepSeek model to an Anthropic
  endpoint: configuration validation refuses the mix before any call.
- `auth: local_login` uses the Claude Code CLI's own login with `--safe-mode` (no CLAUDE.md,
  plugins, hooks or MCP). `auth: api_key` uses `--bare` with only the role's key in the child
  environment.
- The controller records the configured provider, requested model, any models present in the
  response, and an `identity` of `verified` / `unknown` / `mismatch`. A mismatch is an error, not a
  silent fallback.

## Decision protocol

Every planner turn returns one JSON object:

```json
{
  "action": "dispatch",
  "reason": "The deadline is the critical unknown; one task is enough.",
  "evidence": [],
  "tasks": [{
    "id": "deadline",
    "kind": "analysis",
    "prompt": "Read notes/alpha.txt and extract the deadline only.",
    "question": "What is the project deadline?",
    "produces": "One line: deadline=YYYY-MM-DD",
    "decision_impact": "Determines whether the schedule risk is real.",
    "tools": "read",
    "acceptance": [{"id": "c1", "text": "Deadline is present and exact", "check": "facts"}],
    "acceptance_policy": "deterministic",
    "checks": [{"name": "facts", "argv": ["python3", "verify.py", "{result}"]}],
    "allowed_paths": [],
    "depends_on": [],
    "satisfies": ["g1"],
    "max_chars": 120
  }]
}
```

Actions: `dispatch`, `repair`, `verify`, `replan`, `complete`, `review`, `request_input`, `pause`,
and `request_context`.

- `dispatch` adds tasks. Existing task IDs cannot be re-dispatched; use `replan` to change them.
- `repair` targets a failed/unverified task: the method stays valid, the implementation is wrong.
  It bumps the task version, preserves the old artifact, invalidates old acceptance evidence and
  enforces the per-task repair limit. For code tasks the previous patch is reapplied to the current
  input baseline before the worker starts; the new patch is the full result relative to that baseline.
  Conflicts, a missing previous patch, a changed base, out-of-scope inherited changes or an
  `unknown`/`interrupted` prior result are exposed as explicit task state.
- `verify` adds a task that depends on and reads a target artifact. Design a check that can
  distinguish the competing answers; do not repeat the whole original task.
- `replan` changes the task graph: add/modify tasks, cancel unfinished or failed tasks, adjust
  dependencies. Unknown dependencies, cycles and illegal status changes are rejected. Old
  results and decisions are kept; affected downstream results become `stale`.
- `complete` must cover every goal criterion with evidence IDs. The controller validates that each
  cited evidence is a verified artifact from a *completed* task, linked to that criterion. A code
  artifact additionally needs a real patch, a diff hash and no out-of-scope files. Before accepting,
  it refreshes the current task/version, artifact/patch hashes, input files, upstream versions/status
  and acceptance signature; stale evidence is rejected with reasons. A rejected completion decision
  is recorded and the run continues.
- `review` is available only in research mode. It records interpretations and a direction without
  changing the task graph; it cannot carry `tasks`. Use `dispatch`/`verify`/`replan` when work must
  change.
- `request_input` stops with a concrete question (`needs_input`). `pause` stops with a reason.
- `request_context` pulls an artifact/evidence body into the next packet without executing another
  action. It supports `offset` and `limit`; long content is paged (`next_offset` in the packet),
  and explicitly requested content is kept even when the packet exceeds its soft display budget.
  Packets also carry short artifact excerpts, so many decisions need no request.

Every decision can carry `hypotheses`, `refuted`, `open_questions`, `recommendations`,
`next_acceptance` and `context_requests`. Facts, pending assumptions, recommendations and refuted
directions are stored separately; model claims do not become facts until the controller verifies an
artifact or check.

## State, recovery and staleness

Each run gets a `run_id` and a directory containing:

- `state.json`: the current snapshot (goal, constraints, criteria, tasks, evidence, facts,
  hypotheses, refuted directions, open questions, decisions, usage, budget, triggers, stop reason);
- `events.jsonl`: append-only events with sequence numbers and a hash chain;
- `packets/`, `decisions/`, `artifacts/`, `worktrees/`, `tmp/`;
- `run.lock` and `cancel.request`.

Writes use an atomic snapshot plus a checksummed event journal. A truncated last event is ignored
with a note; a corrupt middle event or a future schema version fails loudly. A missing/corrupt
snapshot is rebuilt from the last full event snapshot. Only one process can hold the writer lock
for a run directory. Repeated commits on the same `RunStore` repair the damaged tail once, clear the
repair offset, and keep every valid event with a continuous sequence and hash chain; calling repair
again is a no-op.

On recovery:

- completed tasks that are still valid are not re-dispatched;
- a task caught `running` becomes `interrupted` (no side effects) or `unknown` (it may have changed
  files), with a note that it was not replayed;
- code worktrees that still exist have their changes captured as a recovery patch;
- the next packet explains what is recoverable and asks the planner for repair/replan;
- shell commands are not promised to be exactly-once. Unknown side effects are treated as unknown.

The run saves its startup role, loop, price and budget configuration in `state.json` and the event
journal. Later commands reuse this snapshot when `--config` is omitted; the source configuration
file is no longer needed. Roles store credential environment-variable names, not resolved API keys.
An explicit `--config` overrides roles, loop options and prices for that command; existing run budget
limits stay in force. Older runs without this snapshot use defaults or an explicit `--config`.

A task records the definition hash, input file hashes, upstream result versions and the worker/reviewer
configuration fingerprint. If an upstream artifact/version, an input file or a role's model/endpoint
configuration changes, the result becomes `stale` and its evidence is no longer usable for completion.
Old results are kept and show their version and dependencies. Event patches are computed against the
last committed snapshot, not against the already-mutated state; a corrupt snapshot can therefore be
rebuilt from task, evidence, budget and decision events.

Before a `complete` is accepted, the controller refreshes the evidence rather than trusting its old
snapshot: it walks the dependency chain recursively and checks each task's current status, definition
and result version, artifact and patch hashes, input files, acceptance signature and acceptance
result. Failures name the invalid task, file and dependency path. Results for shared upstream tasks
are cached once per completion validation. A stale or modified result produces a
`completion_rejection` with the specific reasons and returns the tasks/evidence to the planner.
External and automatic modes use the same check.

## Acceptance

Per task, `acceptance_policy` is one of:

- `deterministic`: every criterion names a `check`; only program checks decide.
- `semantic`: a reviewer must return exactly the required criterion IDs, with real booleans.
- `combined`: checks and review both pass.
- `none`: an explicitly unverified draft; it cannot satisfy a goal criterion.

Rules enforced by the controller:

- a pass with missing/extra criteria, string `"true"`, non-list `errors`, or a non-empty `errors`
  list cannot pass;
- a review service failure is `review_unavailable`, not `unreviewed` success;
- an invalid review response is retried and then `review_invalid`, not passed;
- worker output cannot lower criteria; only a recorded planner decision can change them, which
  invalidates the old acceptance evidence;
- `negative_check` is a test requirement: inject a known fault into a copy; the tests must fail to
  catch that injected error;
- `complete` is revalidated against the current run: the full dependency chain, task/version,
  acceptance signature, artifact and patch hashes, input files and upstream versions/status.
  Evidence that no longer matches is rejected with reasons; the planner must repair, replan or
  provide current evidence.

Code tasks:

- run in a separate git worktree from the recorded baseline commit, so user uncommitted changes are
  never reset or included silently;
- record changed files, a binary diff/patch, test/check results, outside-scope files and the
  baseline commit;
- apply upstream dependency patches and commit them into the isolated worktree as the task's
  `input_commit`; the worker's own patch is diffed against that input baseline, so upstream changes
  are not duplicated, scope-checked twice or applied twice;
- for `repair`, apply the previous version's patch on top of the current input baseline before the
  worker starts. The final patch is the full task result relative to that baseline, not just the
  latest fix. Old artifacts and patch versions are retained;
- record `repair_inheritance` (`inherit_patch`, `fresh_no_patch`, `uncertain_patch`), and
  `previous_base_changed` when the prior patch was built on a different base. A missing previous
  patch, an apply conflict or an out-of-scope inherited change becomes an explicit `patch_conflict`
  or `unverified` state for the planner to inspect;
- detect patches that conflict with each other, not just with the baseline, include each task's
  own upstream patch chain in that check, and mark the task `patch_conflict` so the planner must
  serialise or replan;
- a path-scope violation fails acceptance. Scope checks are acceptance, not a runtime sandbox.

Code isolation is deliberately limited to `worktree`: `workspace.isolate=none` is rejected before
the run starts. If `workspace.repo` is not a git repo and `require_git_for_code` is false, code tasks
run directly with a file-snapshot diff; writes are serialized and there is no patch evidence, so such
a task cannot be completion evidence.

## Budget, usage and prices

`goal.budget` or the config `budget` supports:

- `max_calls`, `max_workflow_rounds`, `max_seconds` (stop starting new calls; an already-running
  process ends by its own `timeout` or cancellation, not by killing it at the budget boundary);
- `max_tokens` (an **estimated** budget: one CLI call may make several tool-use turns, so the
  controller reserves and passes the remaining output allowance but cannot hard-cap total provider
  tokens across those turns);
- `max_usd` (soft: estimated from the price table, can be a lower bound);
- `max_unknown_calls` (hard limit for calls whose cost/usage is unknown);
- `max_repairs_per_task`, `max_calls_per_role`.

Calls are metered separately for `planner`, `worker`, `reviewer`, `critic` and `compressor`;
retries are recorded separately from product rework. Reservations happen before concurrent calls.
`max_calls_per_role` counts both completed and in-flight calls, and concurrent unknown-price calls
reserve an unknown-cost slot so they cannot collectively exceed `max_unknown_calls`. The per-call
output cap is the remaining token allowance, not the role maximum, and that same value is reserved.
A timed-out call with no usage is unknown, not free. Unknown model prices are never replaced by a
cheap default. Prices carry their source and update date in `config.PRICES`; the report shows
`cost_usd_known`, `cost_coverage`, `cost_unknown_calls` and, for external planning,
`external_unmetered`.

Provider usage is stored raw; cache read/write and reasoning fields are kept separately so they can
be audited. Character counts are only used for visible-length limits (`max_chars`); they are never
presented as token counts. Packet token counts are labelled estimates.

Three different controls are kept separate: `max_chars` is the visible-body length; `max_output_tokens`
is the API output cap; `reasoning_effort` is passed to the CLI only when a role explicitly sets it and
the backend supports it (a DeepSeek role with `reasoning_effort` is rejected until that interface is verified).
Do not assume that more hidden reasoning reduces billed reasoning tokens,
and do not raise every role to one huge cap. Cache benefits are only claimed when provider usage
reports cache reads.

## Context and cost control

A task-level `model` override is validated against the role provider, then passed to the actual
backend as `--model`; identity checking, budget reservation and cost accounting use that same
effective model.

The planner gets a decision packet: goal and criteria, state summary, new evidence, key artifact
references with hashes and short excerpts, unresolved items, disagreements, budget, recent
decisions, and the current decision triggers.

What enters a model request is bounded by role: the planner sees the packet and explicitly requested
artifact/evidence excerpts; a worker sees its task prompt, upstream text and its worktree files; a
reviewer sees the result body, acceptance criteria, check output and git diff. The whole repository and
all history are not automatically attached to planner/reviewer requests.

- Full artifacts stay in files. `request_context` or `context_requests` pulls selected bodies.
- Packet trimming is deterministic and never drops unresolved items, refutations or evidence IDs.
- Optional model compression (`loop.packet_compress.enabled`) is off by default; if enabled it must
  preserve those IDs and is metered under `compressor`, so its cost can be compared with the reading
  it saves.
- Each role has its own default output cap; there is no single huge cap for every task.
- Cache savings are only claimed when provider usage reports cache reads. The packet records raw
  usage; no cache benefit is assumed.

The critic is off by default. It can be enabled for repeated failures or check/review disagreement;
it challenges assumptions, evidence and alternatives and proposes a discriminating check. It never
rewrites the task and never becomes the source of truth.

## Compatibility with the one-shot runner

`scripts/swarm.py check|plan|run` and the existing `tasks.json` fields still work:

- `inputs` expansion, `depends_on`, `acceptance`, `check_cmd`, `max_chars`, `--only`, `review` /
  `--no-review`, rework and retries are preserved;
- `check_argv` is new and preferred: it runs without a shell;
- legacy `check_cmd` placeholders are substituted safely (unsafe filenames go through quoted
  environment variables; a placeholder inside single quotes is rejected);
- `summary.json` adds `run_id`, `total_calls`, `stale`, `stale_reasons`, `result_sha256`,
  `definition_hash`, `dep_versions`, cost coverage and unknown-call counts;
- a review call failure remains `unreviewed` with `review_outcome=review_unavailable` and is listed
  under `review_failed`; it is never counted as a pass;
- a stale downstream result is not listed in `passed` and makes the command exit 1.

The [one-shot benchmark](benchmarks/2026-10-22-nodal-mesh.md) includes a small task where planning
and review cost more than the worker savings. Input-heavy work with short outputs is a useful next
comparison: measure the whole workflow, including planning and verification.

## Tests

```bash
python3 -I -m unittest discover -s tests -v
```

The suite exercises scheduling, acceptance, research reviews, recovery, code patches, budgets and
pricing. It runs locally with fake model responses and real subprocess checks. CI runs the suite on
Python 3.9, 3.11 and 3.13.

## Benchmark harness

```bash
python3 benchmarks/harness.py --mode fake --include-claude-alone
python3 benchmarks/harness.py --mode fake --out benchmarks/results
```

It compares:

- A. the original one-shot static task flow (`swarm.py` style);
- B. the new dynamic loop (external or automatic; fake in offline mode);
- C. an optional Claude-alone baseline (`--include-claude-alone`).

Cases: long-document extraction, a code fix with fixed tests and a negative test, and a diagnostic
where the first result changes the next hypothesis. The harness scores the output with independent checks.

Reference answers live in the grader outside the worker-visible case directory; case goals and
tasks describe what to establish, not the expected values. The legacy diagnostic also depends on its
extract task. Real mode uses one global call/USD ledger across all approaches; the loop receives the
remaining budget, the Claude-alone path is checked before its call, and the bounded legacy run is
skipped if its upper bound exceeds the remaining call budget. Overshoot, if any, is reported.

Example output from the offline harness (fake backend):

| Case | Approach | Quality | Calls | Cost (known) | Coverage |
|---|---|---|---|---|---|
| reading | legacy | pass | 2 | $0.00048 | full |
| reading | loop | pass | 4 | $0.00289 | full |
| reading | Claude alone (simulated) | pass | 1 | $0.0014 | full |
| code | legacy | pass | 1 | $0.00024 | full |
| code | loop | pass | 3 | $0.002845 | full |
| diagnostic | legacy | pass | 4 | $0.00096 | full |
| diagnostic | loop | pass | 6 | $0.004335 | full |

This table uses scripted responses to check execution and accounting. To measure model quality and
end-to-end cost, run the same cases with real backends and an explicit budget:

```bash
python3 benchmarks/harness.py --mode real --config roles.json \
    --i-understand-this-spends --max-usd 3 --max-calls 60
```

Real mode requires the spending flag and both budget limits. Its report includes model configuration,
git commit, Python version, raw results and independent checks.

## Runtime notes

The runner is a single-machine CLI. Real backends use Claude Code CLI with Anthropic or DeepSeek;
additional providers need a backend adapter. Workers and check commands run with the current user's
filesystem permissions. Worktrees isolate code changes; use a dedicated workspace when selecting
which files to expose to a worker.

Interrupted commands with possible side effects are recorded as `unknown` for the planner to inspect.
Cost reports distinguish measured usage, estimates and external-session usage. In real benchmarks,
the legacy baseline is admitted using an estimated call bound and may overshoot; the harness records
that in its report.

## Documentation

- [简体中文：完整入门与使用指南](README.zh-CN.md)
- [Planner skill guide](SKILL.md)
- [Loop examples](examples/loop/README.md)
- [Research examples](examples/research/README.md)
- [Research evaluation fixtures](benchmarks/research/README.md)

## License

[MIT](LICENSE)

**English** · [简体中文](README.zh-CN.md)
