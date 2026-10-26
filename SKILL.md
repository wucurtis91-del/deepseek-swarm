---
name: deepseek-swarm
description: Offload token-heavy work to several DeepSeek workers running in parallel, each checked by an independent DeepSeek reviewer against acceptance criteria and sent back for rework if it fails, while the planning agent (Claude Code or Codex) plans, verifies and merges. Use when the user says 用 DeepSeek 并行做 / 让 DeepSeek 干 / 省 token / 多个 DeepSeek 一起想, or asks to split a large reading, summarising, solving, translating, drafting or brainstorming job across cheap parallel workers, or a coding job such as writing tests, reviewing many files or reading an unfamiliar codebase. For multi-round work whose next step depends on results (investigate then decide, repair, replan) it also has a persistent loop with saved state, budgets and verified completion. Needs the Claude Code CLI and a DeepSeek API key (env DEEPSEEK_API_KEY, or a DeepSeek provider saved in CC Switch).
---

# DeepSeek 并行工作（deepseek-swarm）

这份说明写给规划者，可能是 Claude Code，也可能是 Codex。下文的「你」都指规划者。

分工：**你负责规划、核对和汇总；DeepSeek 负责执行和互相复核。** 执行者和复核员都是通过 Claude Code
命令行（`claude -p`）调用的 DeepSeek，所以不管谁当规划者，本机都要装有 Claude Code CLI。

根据任务是否需要逐轮决策选择入口：

| 情况 | 用哪个 |
|---|---|
| 任务清单一开始就定得下来，各块可以并行 | `scripts/swarm.py`：一次性运行，见下面的「流程」 |
| 下一步要看上一步的结果：先调查再决定、失败要修复、假设被推翻要重规划，或者需要预算和中断恢复 | `scripts/swarm_loop.py`：持续工作流，见后面「持续工作流」一节 |

下文的 `SWARM` 和 `LOOP` 分别指这两个脚本的绝对路径。两个都只用 Python 标准库。

一次性运行时，每个任务的流程是：

1. 执行者（worker）做出结果，并附一份自查（selfcheck）。
2. 独立复核员（reviewer）对照验收标准和源文件，把计算全部重算一遍，给出通过或不通过。
3. 不通过的结果带着复核意见打回执行者，最多返工 `--max-rework` 次（默认 2）。

汇总前核对关键结论：数值用程序重算，事实回到源文件，代码运行对应测试。复核意见与程序检查一起作为验收依据。

## 流程

1. **检查环境**：运行 `python3 -I SWARM check`，结果应为 `"ok": true`；失败时先处理报错中的配置或环境问题。
2. **准备输入**：给任务提供准确、可定位的源材料。
   - 给执行者**最精确的源文件**。优先提供 LaTeX、源代码或原始数据。PDF 输入先转成文本，图表和版式信息另外补充。
   - **容易误读的约定，自己先核实**（例如看一眼原图），再把核实后的结论写进 `shared_context`。
3. **拆任务**：拆成 2～20 块子任务。每块的 prompt 要能单独看懂，写清楚
   做什么、读哪些文件、输出什么格式。
   - 同一类活要对很多文件做（每讲一份摘要、每个模块一份测试），用带 `inputs` 的模板任务展开。
   - 有先后关系的（先分章再统稿、先写接口再写实现），用 `depends_on` 连接任务。
   - **先定每块的 `max_chars`**：按汇总时需要读取的内容来定，常用 300～1500。
     在 prompt 中列出需要保留的结论和推导步骤。
   - **指定输出结构和各部分篇幅**。例如「输出 5 条建议，每条一个标题
     加 3 行：问题、做法、代价，每行不超过 60 字」。各部分加起来控制在 `max_chars` 的 80% 以内。
   - 内容过多时，按子问题继续拆分。
   - 执行者和复核员的提示词已经要求「多想少说」：推理放在思考里，正文只写结论和关键步骤；
     复核员每条意见限 30～80 字。任务 prompt 只需补充本任务的输出要求。
4. **写验收标准（`acceptance`）**：每个任务列 4～10 条，要具体、能逐条判断。
   - 逐条列出每个小问或要点，例如「(b) 求出 V_A、V_B」。
   - 写出容易出错的地方，例如「节点 C 的方程要包含 10 Ω 加 6 V 那条支路」。
   - **篇幅用 `max_chars` 控制**。程序按非空白字符计数，超长时先压缩；仍然超长则返回验收失败。
     `acceptance` 用来描述内容与正确性要求。
   - 验收标准描述检验方法与要求，参考答案留在独立检查中。
   - 复核员必须逐条回应每一条标准，判通过时 `errors` 也必须为空。格式不符时重新复核，
     重试耗尽后记录对应状态。
   - **多角度分析、头脑风暴、方案比较这类开放题，默认不复核**：照样写 `acceptance`（执行者会看到，
     并按它自查），同时给任务设 `"review": false`。汇总时由你自己抽查关键事实。需要逐项严格核对时打开复核。
5. **预检再运行**：用了 `inputs` 或 `depends_on` 时，先运行 `python3 -I SWARM plan tasks.json`，
   确认展开后的 id、分层顺序和检查命令都对，这一步不调用 DeepSeek；加 `--show <id>` 可以看某个任务
   完整的执行者 prompt。然后运行 `python3 -I SWARM run tasks.json`。预计超过 2 分钟的话放到后台运行。
6. **核对**，按这个顺序：
   - 先看 `summary.json`，状态有这几种：
     - `passed`：通过了复核或检查命令，而且不是 stale。`verified_by` 写明是哪一种（`review`、
       `check_cmd` 或两者）。程序检查的覆盖范围由测试内容决定。
     - `unverified`：返工后仍没通过。
     - `unreviewed`：有结果但没复核。可能是没写 `acceptance`、任务设了 `"review": false`、用了
       `--no-review`，或者复核调用重试后仍失败、复核结果一直无效；后两种同时列在 `review_failed` 里，
       原因在 `review_error`。这些结果按未验收草稿处理。
     - `blocked`：依赖的上游任务没完成（`failed`、`unverified` 或 `blocked`），所以没有运行。
     - `stale`：结果已经过期。它的任务定义、或者它所依赖的上游结果，在它跑完之后变了，常见于用
       `--only` 只重跑了上游。重跑后更新完成证据。
     - `failed`：执行调用失败，没有结果。
   - 再看每个任务的 `selfcheck.uncertain`，这里是最可能出错的地方。
   - 对关键结论做**独立验证**：数值可以用程序重算，事实对照原文，引用逐条查。
   - 通读结果，找复核员容易漏掉的问题：标签和描述错误、前后矛盾、凭空出现的「如图」。
   - 不合格的，改了 prompt 或验收标准后用 `--only id1,id2` 重跑。
7. **汇总**：合并成最终结果交给用户，附上关键依据、验证结果，以及仍待确认的内容。

## 任务文件

```json
{
  "shared_context": "读者是工科本科生。中文，术语附英文。源文件 paper.tex 给出精确电路。已核实：电压源 + 端在上。",
  "tasks": [
    {"id": "q1", "prompt": "解第 1 题的 (a)(b)(c)。每小问先写所用定律，再写方程和结果，不写文字解释。",
     "cwd": "/abs/path", "tools": "read", "max_chars": 1200,
     "acceptance": ["(a) 给出 R_EQ 及化简过程", "(b) 依次求出 V_B、V_A、V_S，注明所用定律",
                    "所有数值带单位，且与电路连接一致（复核员要独立求解核对）"]},
    {"id": "idea", "prompt": "从成本角度提 3 个方案，每个一行标题加两行理由，每行不超过 50 字。",
     "tools": "none", "max_chars": 400, "review": false,
     "acceptance": ["3 个方案互不重复", "每个方案说明省在哪里"]}
  ]
}
```

任务 id 只能用字母、数字、`_` 和 `-`，因为它会变成结果文件名。

每个任务的可选字段：
- `max_chars`：正文篇幅上限，按非空白字符计，自查块不算。
- `acceptance`：有这一项才会进行复核和返工。
- `review`：设为 `false` 时不复核，但执行者仍会看到 `acceptance` 并自查。开放题默认这样设。
- `cwd`、`tools`、`add_dirs`：工作目录、工具预设、额外允许访问的目录。
- `model`、`review_model`：执行者和复核员用的模型。
- `max_turns`、`timeout`：每次调用的最大轮数和超时秒数。
- `check_argv` 或 `check_cmd`，加上 `check_timeout`：执行者每交回一版，脚本就在任务的 `cwd` 里运行这个
  检查（默认最长 300 秒）。退出码不是 0 就把输出的最后 3000 字打回给执行者返工，这一轮不请复核员。
  - `check_argv` 是参数数组，例如 `["python3", "-m", "unittest", "tests.test_parser"]`，不经过 shell，
    优先用它。`check_cmd` 是一条 shell 命令字符串。两者只能选一个。
  - `{result}` 会被换成这一轮结果文件的路径，例如 `["python3", "-m", "json.tool", "{result}"]` 检查 JSON 格式。
  - 检查只由你写，DeepSeek 写不了它。
- `check_env`：检查命令额外需要的环境变量。检查命令默认只继承 PATH、HOME、VIRTUAL_ENV 等少数变量，
  不继承 PYTHONPATH，也不继承任何密钥；测试需要别的变量就写在这里。
- `inputs`：一个或几个 glob（相对于 `cwd`），每匹配到一个文件就展开成一个任务。所有字符串字段里的
  `{path}`（相对路径）、`{name}`（文件名）、`{stem}`（不带扩展名）、`{i}`、`{n}`（第几个、共几个）
  都会被替换。id 里没有占位符时会自动加上 `-{stem}`；文件名没有英文字母时改用序号。
  展开后超过 50 个任务就拒绝运行（`--max-tasks` 可以调）。文件名里的空格和特殊字符不会被当成命令执行；
  但 `check_cmd` 里放在单引号中的占位符会被拒绝，这种情况改用 `check_argv`。
- `depends_on`：要等哪些任务完成，可用 `*` 通配，例如 `["lec-*"]`。上游结果会放进这个任务的 prompt，
  每份最多 `upstream_chars` 字（默认 6000），超出的部分执行者可以自己去读全文。上游没完成，这个任务
  就标成 `blocked`。用 `--only` 只重跑下游时，会沿用上游的旧结果；只重跑上游时，下游不会自动重跑，
  会被标成 `stale`。

例子：先给每讲出摘要，再统稿。

```json
{"tasks": [
  {"id": "lec-{stem}", "inputs": "notes/*.md", "cwd": "/abs/course", "tools": "read", "max_chars": 500,
   "review": false, "prompt": "读 {path}（第 {i}/{n} 讲），列 3 个核心概念，每个一行定义加一行例子。"},
  {"id": "overview", "depends_on": ["lec-*"], "tools": "none", "max_chars": 1200,
   "prompt": "根据上游的各讲摘要，写一份全课程概念地图：按主题分组，标出各讲之间的先后依赖。"}
]}
```

`tools` 预设：

| 预设 | 能做什么 | 用途 |
|---|---|---|
| `none` | 不用工具 | 纯思考、写作、头脑风暴 |
| `read`（默认） | Read、Grep、Glob | 读文件后分析或解题。复核员固定用这一档 |
| `edit` | 增加 Edit、Write，自动接受修改 | 每个执行者写**各自的**文件 |
| `bash` | 增加 Bash，不再询问权限 | 只在用户明确同意、并且目录隔离时使用 |

常用参数：
- `--concurrency 6`：同时运行的任务数。
- `--max-rework 2`：最多返工次数。
- `--no-review`：跳过复核。只在用户明确要快、不要求质量时使用。
- `--review-model deepseek-v4-pro`：复核员换用更大的模型。
- `--only a,b`：只重跑指定的任务。
- `--max-chars 800`：给没写 `max_chars` 的任务一个默认上限。
- `--retries 1`：执行和复核调用出错时各自重试几次。复核失败不会占用返工次数；鉴权和配置错误不重试，
  会改文件的任务超时后也不重试。
- `--max-output-tokens 64000`：每次调用的输出上限，推理也算在内。Claude Code 默认 32000，
  DeepSeek 想得长时会撞到。
- `--max-tasks 50`：`inputs` 展开后最多允许多少个任务。

## 写代码时怎么用

分工：你定接口、拆任务、集成和最终运行；DeepSeek 做读得多、写得多、互不依赖的部分。

适合交出去的：读懂陌生代码库（`read`，每人一个模块，交回短摘要）；代码审查和找 bug（`read`，开复核，
复核员按源码核对每条 bug 能否触发）；写单元测试、互不依赖的小模块、样板代码、文档和注释（`edit`，
每人写自己的文件，配检查命令）；大段日志分析（`read`）；技术选型（`none`，不复核）。
要多轮修复、每版都要留补丁和测试证据的代码活，用持续工作流的代码任务（`kind: "code"`）。

接口耦合较强的改动、交互式调试和小改动适合由主持者直接处理。架构决策可以先让 worker 收集方案，再统一选择。

规划要点：
- **先定接口再派活**：函数签名、数据格式、文件位置、命名风格、语言版本、能不能加依赖，都写进
  `shared_context`。执行者使用独立上下文，项目约定通过任务输入传入。
- **一个任务只写自己的文件**。`__init__.py`、路由表、配置这类共用文件留给你最后统一改。
- **在 git 分支上跑**，跑完看 `git diff`，不满意就还原。
- **`max_chars` 管交回的文字，不管写进文件的代码**。`edit` 任务把它设成 200～400，只交回
  「改了哪些文件、做了什么、哪里没把握」。
- **用检查命令跑测试，通常不开复核**：复核员只能读代码，不能运行。检查只跑这个任务自己的
  测试（例如 `["python3", "-m", "unittest", "tests.test_parser"]`）；全部任务集成后再运行完整测试集。
- **先写接口，再并行实现**：用 `depends_on` 让一个执行者先写好接口文件，其他执行者再基于它写实现。
- **选择工作目录**：执行者可读取 `cwd` 和 `add_dirs`，读取内容会进入模型请求。
  使用只包含任务所需文件的目录，把 `.env` 和密钥留在目录外。
- **执行权限**：检查命令以当前用户权限运行 worker 生成的代码和测试。
  新项目首次执行时说明工作目录和运行范围。

```json
{
  "shared_context": "Python 3.11，测试用 unittest，不加依赖。只能新建自己那个测试文件，不改其他文件。交回内容只写：新建了哪个文件、覆盖了哪些函数、哪些行为没把握。",
  "tasks": [
    {"id": "test-{stem}", "inputs": "src/*.py", "cwd": "/abs/repo", "tools": "edit", "max_chars": 300,
     "prompt": "读 {path}，在 tests/test_{stem}.py 写单元测试。",
     "check_argv": ["python3", "-m", "unittest", "tests.test_{stem}"],
     "acceptance": ["每个公开函数至少 1 个正常用例和 1 个边界用例", "空输入、非法输入都有用例"],
     "review": false}
  ]
}
```

## 结果

默认写在任务文件旁边的 `<tasks>-results/` 里：
- `<id>.md`：最终版本。
- `<id>.vN.md` 和 `<id>.vN.review.json`：每一轮的版本，以及这一轮没通过的原因（复核员意见、
  字数检查或检查命令的输出，`checked_by` 写明是哪一种）。
- `summary.json`：每个任务的状态、轮数、自查、复核结论、耗时和估算费用。费用按 DeepSeek 官方价格估算：
  高峰是周一到周五 UTC 01:00–04:00 和 06:00–10:00，其余时间（包括周末）半价。价格表里没有的模型记为
  费用未知；`cost_coverage` 写明费用是全部已知、部分已知还是未知。

## 成本怎么控制

实测（见 `benchmarks/`）：一份小试卷，Claude 自己简洁地做大约要 $0.5～0.8；走并行流程，规划者这边的规划、
读完全部结果、核对就要 $0.6～0.9，再加上 DeepSeek 的 $0.24，总成本高于直接完成。更适合分工的情况是：

- **要读的材料多**（很多长文档），而需要的结果短。可以把主要阅读工作交给 worker。
- **你只读摘要和抽查**：用脚本、`summary.json` 和自查字段来核对，不把每份全文都读进上下文。
- **每个任务都设 `max_chars`**：结果越短，主持者阅读与复核的开销越小。
- **在上下文短的会话里规划**：长会话里每调用一次工具，都要把整个上下文重新读一遍。

小任务可以直接完成；多任务分工按整个流程的调用、阅读和验证成本比较。

## 持续工作流（swarm_loop.py）

适用：先调查再决定、中途可能出现反证、代码改动要留补丁和测试证据、运行需要预算或中断恢复。
固定且互不依赖的任务用 `swarm.py run`；几行代码或必须自己交互调试的问题直接做。

**外部主持（默认，推荐）**：由你自己当主持者。Claude Code 和 Codex 都用这种方式。

```bash
python3 -I $LOOP start runs/demo --goal goal.json --config roles.json
python3 -I $LOOP next runs/demo        # 读 packets/NNNN.json，然后写 decision.json
python3 -I $LOOP decide runs/demo decision.json
python3 -I $LOOP next runs/demo
```

重复「`next` → 写决策 → `decide`」，直到 `status` 变成 `complete`、`paused`、`needs_input` 或 `cancelled`。
你自己会话的 token 不计入控制器的账本，记为 `external_unmetered`。

**自动主持**（`start --mode auto`，然后 `auto runs/x --max-steps 20`）：控制器自己调用配置里的 planner，
默认是用本机 Claude 登录的 Claude，**会消耗用户的 Claude 用量**，最多跑 `loop.max_steps` 步（默认 50）。
只在用户同意时用。planner 不可用或模型对不上时会暂停，不会换模型继续。

输入：
- `goal.json`：`goal`、`constraints`、带稳定 id 的 `completion_criteria`，以及可选的 `workspace.repo`
  （代码任务用的 git 仓库）。
- `roles.json`：planner、worker、reviewer、critic 各自的模型、密钥来源和工具，外加 `loop` 和 `budget`。
  模板是 `examples/loop/roles.example.json`。配置在 `start` 时随运行保存，后面的命令不用再给。
- 决策文件：每个任务必须有 `id`、`prompt`、`question`、`produces`、`decision_impact`，验收标准带 id，
  并写 `acceptance_policy`；`satisfies` 写它负责哪几条完成条件。示例在 `examples/loop/`。

决策（`reason` 必填；`evidence` 只能引用 packet 里的 `ev-...`）：

| action | 什么时候用 |
|---|---|
| `dispatch` | 派新任务，依赖满足后执行 |
| `repair` | 方向对、实现或验收失败：给 `target_task` 和 `instructions`，v+1 接着上一版改 |
| `verify` | 要独立检查已有结果：检查必须能区分对错 |
| `replan` | 假设被推翻、方法不可行、任务定义错了：可增改和取消任务，受影响的下游标 stale |
| `review` | 研究模式中解释待审结果、记录下一步方向，不创建任务 |
| `complete` | 每条完成条件都有当前有效的证据 |
| `request_input` | 缺只有用户能给的信息：写具体 `question` |
| `pause` | 预算、外部条件或明确要停 |
| `request_context` | 只想读某份产物的正文，不执行动作 |

验收方式（`acceptance_policy`）：`deterministic` 每条标准指向一个程序检查；`semantic` 由复核员逐条判断；
`combined` 两者都要通过；`none` 是明确未验收的草稿，不能当完成证据。写测试的代码任务加
`negative_check`：在副本里注入一个已知错误，测试必须能抓出来。

**完成校验**：`complete` 只能引用已验收的产物证据。控制器会沿依赖链重新核对任务状态和版本、产物和补丁的
哈希、输入文件和验收结果；任何一项变了，旧证据就不能用来完成，拒绝理由会写进下一份 packet。

**代码任务**（`kind: "code"`）：在 `workspace.repo` 的独立 git worktree 里执行，从当前 HEAD 建立，
不会重置或覆盖用户工作区和未提交的改动。会记录基线 commit、改动文件、补丁和检查结果；上游任务的补丁会先应用。
`allowed_paths` 是验收范围，不是沙箱：越界改动会导致验收失败。

**预算**：`max_calls`、`max_workflow_rounds`、`max_seconds`、`max_tokens`、`max_usd`、`max_unknown_calls`。
到了上限就不再发起新调用，已经在跑的调用按自身超时结束。`max_usd` 是按价格表估算的软上限。

**状态与恢复**：运行目录里有 `state.json`、带哈希链的 `events.jsonl`、`packets/`、`decisions/`、`artifacts/`、
`worktrees/`。`status` 看进度和预算，`resume` 从中断处恢复，`cancel` 安全停止，`artifacts --show TASK`
看产物。中断时状态 `interrupted` 表示没有副作用，`unknown` 表示可能已经改了文件，都不会自动重放。

离线测试和 fake 评测（`python3 benchmarks/harness.py --mode fake`）用于验证控制流程与计量。
模型质量和实际成本用 `--mode real` 测量，按用户确认的预算执行。

## 研究模式（可选）

用 `start --research` 或 `goal.research: true` 开启。默认关闭时行为不变。

- 任务用 `tests_hypotheses` 声明检验哪些假设，用 `depends_on_hypotheses` 声明结果依赖哪些假设；
  `key_experiment: true` 的完成结果会进入 `packet.research.pending_results`，即使尚未被任何角色标成
  `against`。
- 可选的 `research_prediction` 是程序检查：`{"hypothesis":"h1","check":"unit","expect_exit":0}`。
  只有该检查实际不匹配时才生成 `prediction_mismatch`；没有程序检查时，控制器不声称发现科学矛盾。
- 用 `review` 处理待审查结果：`research.review_ids` + 引用 `observation` 的解释 + `direction`。
  `review` 只更新研究判断，不创建任务；需要实验/改图时用 `dispatch`/`verify`/`replan` 携带同一
  `research`。待审查的 key/prediction 结果必须解释其 observation、相关假设的当前版本并给方向；
  已 resolved 的 `review_id` 不能覆盖后来的 pending 事件，精确重放是幂等 no-op。
- `complete` 时仍有任何待审查事件会被拒绝；`complete` 可以与研究更新同条提交，但按更新后的最终
  状态校验，失败则整条回滚。解释可用 `supersedes` 修正同一 observation/版本/条件下的旧解释；
  旧解释保留在历史但 inactive，不再支持假设。
- `direction.choice` 是 `continue|experiment|narrow|switch|pause|stop`。切换/暂停是投入选择，
  不要求负结果，也不会改写假设状态或让已有依赖任务失去完成资格；显式有效性变化必须用
  `hypothesis_updates`（定义/范围变化会升版）。单假设校准、复现和测量任务合法；控制器不会把
  一次实现失败或 `against` 解释自动变成 `contradicted`。
- 任务执行时记录依赖的假设版本；定义/范围升版或显式 status 变化后，依赖任务进入
  `needs_review`，不能直接完成目标。`complete` 会先在同一事务里应用研究更新、传播依赖和刷新
  pending/有效性，再校验；失败整条回滚，成功后重新打开刷新不应再产生本条决策带来的新待审事件。
- `review` 的 research payload 精确重放只是幂等 no-op，不会消费新的 pending；新的
  `dispatch`/`verify`/`repair`/`replan` 仍需按当前 pending 通过审查校验，不能拿旧 payload 当执行许可。
- `needs_review` 任务可用 `replan.cancel` 取消；artifact 和研究历史保留，完成证据标为失效，
  受影响下游失效，随后可用替代任务完成目标。
- 证据失效会先按验收同一套 artifact/patch/input/依赖链校验刷新，再保留观察/解释历史，标记依赖
  解释 `needs_review`，并保留其他有效支持；失效 observation 不能用新的 `supports`/`against`
  解释，`evidence_invalidated` 事件可单独审查关闭；改路线不删除无关 artifact。旧
  `hypotheses/refuted` 在启用研究模式时标为 `legacy/unassessed`。
- 研究材料有大小限制和分页（`pending_total`/`next_offset`/`limits`，正文用
  `research_page` 或 `request_context` 继续取页）；关键 id/状态/假设版本/失效引用不会被裁剪或
  被空对象、null、篡改结构绕过；若最小结构仍超预算会报告 `limits.min_chars`。研究事件不会
  额外调用模型，审查使用现有 planner 预算。

完整字段、状态和示例见 `README.md` 与 `examples/research/`。

## 给 Codex 用时

- Codex 通过 `~/.codex/skills/deepseek-swarm` 读到的就是这个文件夹，和 Claude Code 共用同一份脚本。
- 执行者要联网访问 `api.deepseek.com`。密钥存在 CC Switch 里的话，还要读 `~/.cc-switch/cc-switch.db`。
  Codex 的沙箱不允许联网或不允许读这个文件时，运行脚本前要放行，否则每个调用都会失败。
- 持续工作流用外部主持：Codex 自己读 packet、写决策。自动主持的 planner 是 Claude，不是 Codex。

## 运行约定

- **隐私**：执行者以 `--bare` 模式运行，不读 CLAUDE.md、AGENTS.md、记忆、插件和 MCP。任务仅传入必要资料；
  个人敏感信息需要用户明确同意后传入。
- **文件分工**：用 `edit` 或 `bash` 预设时，每个任务写各自的文件，最后由你合并。
- **模型**：默认是 `deepseek-flash`。只有用户要求或任务特别难时，才用 `deepseek-v4-pro`。
  `deepseek-v4-pro` 更贵，而且不能识图。
- **调用预算**：自动主持使用 Claude 用量，`benchmarks/harness.py --mode real` 使用 API 额度，
  按用户已确认的调用范围和预算运行。
- **API Key**：通过环境变量或已有凭证配置读取，日志和产物中省略密钥值。
