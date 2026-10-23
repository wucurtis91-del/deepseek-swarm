# deepseek-swarm

Let Claude plan and review while several cheap **DeepSeek** workers do the token-heavy work in parallel.

[中文说明见下方](#中文说明)

Each worker is a headless [Claude Code](https://claude.com/claude-code) process (`claude -p`) pointed at
DeepSeek's [Anthropic-compatible endpoint](https://api-docs.deepseek.com/guides/anthropic_api). A
small Python script (standard library only) starts them in parallel, collects the results and writes a summary.
It ships as a Claude Code **skill**, so a Claude session knows when and how to use it.

Each task can carry **acceptance criteria**. When it does, an independent DeepSeek reviewer re-checks the
result against them and the source files, re-doing every calculation. A result that fails review goes back to a
worker with the reviewer's notes, up to two times by default.

```
Claude (plan) ──► tasks.json ──► swarm.py ──┬─► DeepSeek worker 1 ─► ch1.md
                                            ├─► DeepSeek worker 2 ─► ch2.md
                                            └─► DeepSeek worker N ─► chN.md
Claude (check + merge) ◄── summary.json ◄───┘
                     (each worker: produce → review → rework if failed)
```

## Why

- **Cheap.** Workers run in `--bare` mode, which skips CLAUDE.md, memory, hooks, plugins and MCP. A worker
  starts at roughly 1k input tokens of overhead, compared with tens of thousands in a normal session. In a test,
  four parallel tasks took 5.5 s and cost about $0.002.
- **Private.** Because of `--bare`, your personal CLAUDE.md and memories are never sent to the worker.
- **Isolated.** Workers get a clean environment and never inherit the calling session's credentials. The API key
  is read from `DEEPSEEK_API_KEY`, or from a DeepSeek provider saved in [CC Switch](https://github.com/farion1231/cc-switch).
  This works whichever provider CC Switch currently has active.

## Requirements

- Claude Code CLI (`claude`) on your `PATH`
- Python 3.9+
- A DeepSeek API key

## Install

```bash
git clone https://github.com/wucurtis91-del/deepseek-swarm ~/.claude/skills/deepseek-swarm
export DEEPSEEK_API_KEY=sk-...          # skip this if you use CC Switch
python3 ~/.claude/skills/deepseek-swarm/scripts/swarm.py check
```

## Use

From a Claude Code session, just ask: *"use DeepSeek in parallel to summarise each chapter in notes/"*. The skill
tells Claude how to split the work, run it, check the results and merge them.

To run it by hand:

```bash
python3 scripts/swarm.py run examples/tasks.example.json --concurrency 6
python3 scripts/swarm.py run tasks.json --only ch2,ch5     # rerun selected tasks
```

`tasks.json` holds an optional `shared_context` and a list of tasks with these fields: `id` (letters, digits, `_`
and `-`) and `prompt`, plus optional `max_chars` (a length limit), `acceptance` (a list of criteria, which turns on
review and rework), `cwd`, `tools` (`none`, `read` by default, `edit` or `bash`), `model`, `review_model`,
`max_turns`, `timeout` and `add_dirs`.
Useful flags: `--max-rework N`, `--no-review`, `--review-model`, `--max-chars N`, `--retries N` and
`--max-output-tokens N`. See [SKILL.md](SKILL.md) for details.

**Length is enforced by the script, not the reviewer.** With `max_chars` set, the script counts the result
(non-whitespace characters). An over-long result goes back to the worker for compression; one that stays too long
fails the round without a reviewer call. Workers and reviewers are told to think in their reasoning and keep the
visible output to conclusions and key steps.

Results go to `<tasks>-results/`: the final `<id>.md` for each task, every round's version and review
(`<id>.vN.md`, `<id>.vN.review.json`), and a `summary.json` with status, rounds, self-check, review verdict, time and an
estimated cost based on DeepSeek's published peak and off-peak prices. Status is `ok` (passed review), `unverified`
(still failing after rework), `unreviewed` (no criteria, `--no-review`, or the review call kept failing; the last also
appears under `review_failed`) or `failed` (no result).

## How well does it work?

[One benchmark](benchmarks/2026-10-22-nodal-mesh.md) used a six-question circuits practice paper. With review on,
the swarm got every numerical answer right, matching Claude working alone, and showed more working. DeepSeek cost
$0.13–0.24 per run. On a task this small, though, the swarm did **not** save money overall: Claude's own planning,
reading and checking cost about as much as solving it directly. The swarm pays off on input-heavy work where Claude
only reads summaries. One sample, not a general claim.

## Caveats

- Worker output is a draft, and a passed review is not proof. Check key results independently.
- Workers using the `edit` or `bash` tools must not write to the same files.
- DeepSeek cannot read PDF files; convert them to text first.

## 中文说明

**用途：** Claude 负责拆任务、检查和汇总，多个 DeepSeek 同时执行，适合大量阅读、总结、翻译、出题、头脑风暴这类费 token 的工作。

**API Key：** 从环境变量 `DEEPSEEK_API_KEY` 读取，或者从 CC Switch 里已保存的 DeepSeek 供应商读取。

**隐私：** worker 不读取 CLAUDE.md 和记忆，不会把你的个人资料发给 DeepSeek。

**使用：** 在 Claude Code 里直接说「用 DeepSeek 并行做……」，或者按上面的命令手动运行。

## License

MIT
