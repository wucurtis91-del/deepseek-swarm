# Persistent loop examples

## Offline multi-round demo (no paid API)

```bash
./examples/loop/fake/run-demo.sh
```

It runs external-host mode with fake backends:

1. `start` writes the initial decision packet;
2. `decide` dispatches one task; the fake worker answers; the fake reviewer checks it strictly;
3. the next packet contains the verified artifact/evidence;
4. the second `decide` submits `complete`, and the controller validates the goal criteria.

The demo exercises the loop, state and protocol with scripted responses.
Use `--allow-fake-backend` with the fake configuration.

## Real external-host mode

```bash
export DEEPSEEK_API_KEY=...
# planner uses your Claude Code login; worker/reviewer use DeepSeek
python3 scripts/swarm_loop.py start runs/project \
  --goal examples/loop/goal.json \
  --config examples/loop/roles.example.json
python3 scripts/swarm_loop.py next runs/project
# Read the packet, write a decision (see decision.dispatch.json)
python3 scripts/swarm_loop.py decide runs/project my-decision.json
python3 scripts/swarm_loop.py next runs/project
python3 scripts/swarm_loop.py status runs/project
```

Startup configuration is saved with the run. Later commands reuse it when `--config` is omitted,
even if the original configuration file moves. An explicit `--config` overrides roles, loop options
and prices for that command; the run's saved budget limits remain in force. Older runs without a
configuration snapshot still require `--config` for custom settings.

`decision.complete.json` is a template; use the evidence IDs produced in the current packet.

## Automatic-host mode

```bash
python3 scripts/swarm_loop.py start runs/auto \
  --goal examples/loop/goal.json \
  --config examples/loop/roles.example.json \
  --mode auto
python3 scripts/swarm_loop.py auto runs/auto --max-steps 20
```

The planner call goes through the configured Claude backend. If the backend is unavailable or its
identity does not match the configuration, the run stops and records the reason.

## Goal file

See `goal.json` for the shape: `goal`, `constraints`, `completion_criteria` (stable IDs) and an
optional `workspace` with a git repo for code tasks.

## Decision file

`decision.dispatch.json` shows the required task fields: `question`, `produces`,
`decision_impact`, `acceptance` with stable criterion IDs, and an acceptance policy. For code tasks,
also set `kind: "code"`, `allowed_paths` and a `negative_check` if the task writes tests.

`decision.complete.json` shows the completion shape: every goal criterion is covered and cites
verified evidence from a completed task.
