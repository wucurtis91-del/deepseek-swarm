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

## Second run with token accounting

The same tasks file was rerun after per-task token counting was added to `swarm.py`.

- All 6 tasks passed. Q1 needed **3 rounds**. The v1 reviewer caught a real contradiction: the answer said "set
  the source to zero, i.e. short it", then "leave it open". The v2 fix introduced a new wrong check equation, which
  the reviewer also caught, and v3 passed. The final key values in all 6 answers match the reference.
- Wall time was 752 s, mostly the Q1 reworks.

| DeepSeek tokens (6 workers + reviewers + reworks) | |
|---|---|
| Input (uncached) | 160,816 |
| Input (cache read) | 193,664 |
| Output, mostly reasoning tokens; the visible text is about 52k characters | 351,267 |
| **Cost** (off-peak prices; peak would be about 2×) | **$0.24** |

## Cost comparison (API-equivalent, Claude Opus 5.5 at $4 / $20 / $0.20 cache read / $5 cache write per MTok)

The Claude figures are **estimates**: Claude's own token use was not metered per step. They assume about 6 tool
calls per approach and count re-reading the session context on every call. Context size is the dominant factor:
this session had grown to about 300–375k tokens, while a fresh session starts at about 90–120k.

| Approach | Long session (this one) | Fresh session |
|---|---|---|
| A. Claude alone, concise (about 15k output tokens incl. thinking) | about $0.80 | about $0.50 |
| B. Swarm: Claude plans, reads all outputs and verifies, plus DeepSeek | about $0.90 + $0.24 ≈ **$1.10** | about $0.60 + $0.24 ≈ **$0.85** |
| C. Claude doing the swarm's full volume itself (DeepSeek's token counts priced at Opus rates) | about $7.70 | about $7.70 |

**On this task, the swarm did not save money compared with Claude working concisely (A vs B).** The paper is small
(Claude's own answer was only about 15k output tokens), so Claude's planning, its reading of about 52k characters of
verbose worker output, and its final verification cost about as much as solving the paper directly. Case C is not
realistic: Claude would not generate that much text for this job. It shows the volume DeepSeek absorbed.

### When the swarm does save money

- **Input-heavy work**: many long documents to read, with only short results needed. For example, reading 300k tokens
  of notes costs Claude about $1.20 in input alone, plus context growth; DeepSeek reads it for a few cents.
- **Claude reads summaries, not full outputs.** Verify with scripts, structured fields or spot checks rather than
  pasting every worker's full text into the planning session.
- **Cap worker verbosity** with a length criterion, which also cuts review cost.
- **Plan from a short session.** Every tool call in a long session re-reads the whole context.
- On a Claude subscription plan the cost is not billed per token. The saving instead shows up as usage-limit headroom.

## Takeaways

1. On a well-defined, checkable and decomposable task, the swarm with review matched Claude on correctness and was more thorough in showing working.
2. **A passed review is not proof of correctness.** The reviewers passed everything and missed the Q3 wording slip. The planner's final check, here an independent numerical solve, is still needed.
3. The quality comes mostly from the inputs. Workers got exact topology (the LaTeX rather than lossy PDF text), conventions the planner had verified, and specific acceptance criteria that named the likely traps.
4. DeepSeek output is verbose. If length matters, add a length limit as an acceptance criterion.
5. Limits of this test: there was one sample, the problems are textbook-level with clean numbers, and the rework loop was never triggered on this task (it was exercised in a smaller test).
