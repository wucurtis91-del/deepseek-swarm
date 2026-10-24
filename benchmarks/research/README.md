# Research fixture protocol

This directory provides evidence-replay and experiment-choice fixtures for comparing research
workflows. The fixtures run offline; a model runner can use them to compare the three arms below.

## Fixtures

- `Fixed evidence replay` (`fixtures.EvidenceReplay`): four cases with a fixed
  evidence order. The evaluator-only truth, validity labels and key evidence are
  in `FIXED_CASES`. `public_case` exposes only a neutral id/title/description,
  initial information and legal operations; evidence order, expected decisions
  and error patterns stay evaluator-side. `EVALUATOR_CASE_MAP` keeps the
  original case classification. `next()` returns only `id`, `kind`,
  `result` and `source`; `strength` and other judgments stay evaluator-only in
  the source case or `next_evaluator()`.
- `Choice environment` (`fixtures.ChoiceEnvironment`): a small environment with
  neutral option ids `observe_1`, `calibrate_2`, `transfer_3` and `batch_4`.
  Each option has a cost and an observation; the hidden truth determines the
  outcome. `run` returns only `option`, `observation` and `cost`;
  `EVALUATOR_OPTIONS` and `evaluator_truth()` keep the validity and
  discrimination labels, plus the original case classification in
  `EVALUATOR_CASE_MAP`.

## Arms

`fixtures.ARMS` defines three interfaces:

- `base`: current controller.
- `reflection`: current controller plus one planner prompt suffix.
- `research`: research mode and the persistent ledger.

A real runner should use the same goal, evidence order, budget and maximum
turns for all arms. The fixture `arm_goal` only attaches the arm label and
optional prompt suffix; it does not call a model.

## Metrics

Record, per case and arm:

- final outcome judged by an evaluator-only rubric;
- calls/tokens/cost/time including planner, reviewer, retries and validations;
- spend on a route after it is known to be wrong;
- time/calls from the first decisive evidence to a valid adjustment;
- switch/stop before decisive evidence when the route is actually viable;
- switches without valid evidence, and A→B→A flapping;
- whether a follow-up experiment distinguishes competing explanations.

Keep “necessary exploration” separate from “avoidable spend after the valid
adjustment point”. Do not rank an arm by the number of direction changes.
