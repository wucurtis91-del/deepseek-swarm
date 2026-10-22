# Benchmark: DeepSeek swarm vs Claude alone

**Date:** 2026-10-22
**Task:** Write full worked solutions to a six-question first-year circuits practice paper on the node voltage and mesh current methods (60 marks).
**Paper rules:** Every intermediate step must be shown. Each answer must state the reference node, the assumed current directions and voltage polarities, and the law used at each step. Answers need units, and power balances must be verified.

## Setup

| | DeepSeek swarm | Claude alone |
|---|---|---|
| Model | `deepseek-flash` workers and reviewers | Claude Opus 5.5, one pass |
| Split | 6 tasks, one per question, run in parallel | whole paper |
| Quality loop | 8–9 acceptance criteria per task, an independent reviewer per task, up to 2 reworks | self-check by power balance |
| Input | LaTeX source (exact circuit topology), text of the paper, conventions verified from the PDF | same |

Claude wrote its solutions before seeing any DeepSeek output or the reference answers. The reference answers came from a separate exact modified-nodal-analysis solver run on netlists taken from the LaTeX.

## Results

| | DeepSeek swarm | Claude alone |
|---|---|---|
| Numerical answers matching the reference | **all, 6/6 questions** | **all, 6/6 questions** |
| Sub-parts answered | 27/27 | 27/27 |
| Errors found by final check | 1 wording slip: in Q3 the A–B 3 Ω resistor is called "the upper 3 Ω" (the equations are still right) | none |
| Intermediate steps | very complete; extra cross-checks (KCL at every node, node method used to confirm the mesh answers) | complete except Q6(c), where the elimination is shortened to "solving simultaneously gives…" |
| Length | about 51,000 characters | about 9,700 characters |
| Reviews passed on first round | 6/6 (no reworks were needed) | n/a |
| Wall time | 231 s | similar (not timed precisely) |
| Cost | about $0.13 in DeepSeek API cost (estimated from published prices) | about 7k Claude output tokens |

Each worker's self-check flagged the same thing as uncertain: how to read the source polarity and arrow directions from the circuitikz code. That is the one assumption the planner had verified by looking at the PDF beforehand.

## Takeaways

1. On a well-defined, checkable and decomposable task, the swarm with review matched Claude on correctness and was more thorough in showing working.
2. **A passed review is not proof of correctness.** The reviewers passed everything and missed the Q3 wording slip. The planner's final check, here an independent numerical solve, is still needed.
3. The quality comes mostly from the inputs. Workers got exact topology (the LaTeX rather than lossy PDF text), conventions the planner had verified, and specific acceptance criteria that named the likely traps.
4. DeepSeek output is verbose. If length matters, add a length limit as an acceptance criterion.
5. Limits of this test: there was one sample, the problems are textbook-level with clean numbers, and the rework loop was never triggered on this task (it was exercised in a smaller test).
