---
name: deepseek-swarm
description: Offload token-heavy work to several DeepSeek workers running in parallel, each checked by an independent DeepSeek reviewer against acceptance criteria and sent back for rework if it fails, while Claude plans, verifies and merges. Use when the user says 用 DeepSeek 并行做 / 让 DeepSeek 干 / 省 token / 多个 DeepSeek 一起想, or asks to split a large reading, summarising, solving, translating, drafting or brainstorming job across cheap parallel workers, or a coding job such as writing tests, reviewing many files or reading an unfamiliar codebase. Needs Claude Code CLI and a DeepSeek API key (env DEEPSEEK_API_KEY, or a DeepSeek provider saved in CC Switch).
---

# DeepSeek 并行工作（deepseek-swarm）

分工：**你（Claude）负责规划、核对和汇总；DeepSeek 负责执行和互相复核。** 每个任务的流程是：

1. 执行者（worker）做出结果，并附一份自查（selfcheck）。
2. 独立复核员（reviewer）对照验收标准和源文件，把计算全部重算一遍，给出通过或不通过。
3. 不通过的结果带着复核意见打回执行者，最多返工 `--max-rework` 次（默认 2）。

所有任务并行。脚本是 `scripts/swarm.py`，只用 Python 标准库。下文的 `SWARM` 指这个脚本的绝对路径。

**完成度是第一目标，省钱其次。** 复核通过不等于正确：在 2026-10-22 的实测里，复核员漏掉了一处
表述错误，最后是由 Claude 核对出来的。最后一道把关永远是你自己。

## 流程

1. **检查环境**：运行 `python3 -I SWARM check`，结果应为 `"ok": true`。失败就停下，把报错告诉用户。
2. **准备输入**：完成度主要取决于这一步。
   - 给执行者**最精确的源文件**。例如有 LaTeX、源代码或原始数据时，就不要只给从 PDF 提取的
     文字（提取时图和结构会丢失）。DeepSeek 读不了 PDF，要先转成文本。
   - **容易误读的约定，自己先核实**（例如看一眼原图），再把核实后的结论写进 `shared_context`。
3. **拆任务**：拆成 2～20 块子任务。每块的 prompt 要能单独看懂，写清楚
   做什么、读哪些文件、输出什么格式。
   - 同一类活要对很多文件做（每讲一份摘要、每个模块一份测试），写一个带 `inputs` 的模板任务，不要手抄 20 遍。
   - 有先后关系的（先分章再统稿、先写接口再写实现），用 `depends_on`，不要拆成两个任务文件手动接力。
   - **先定每块的 `max_chars`**：按你打算读多少来定，不是按 DeepSeek 想写多少，常用 300～1500。
     要的是结论就别要过程；要过程就点名哪几步必须写。
   - **把输出结构和每部分的篇幅写死**，不要只给一个总字数。例如「输出 5 条建议，每条一个标题
     加 3 行：问题、做法、代价，每行不超过 60 字」。各部分加起来控制在 `max_chars` 的 80% 以内。
   - 一块内容装不进上限，就再拆成两块，不要放宽上限。
   - 执行者和复核员的提示词已经要求「多想少说」：推理放在思考里，正文只写结论和关键步骤；
     复核员每条意见限 30～80 字。这些不用在 prompt 里重复。
4. **写验收标准（`acceptance`）**：每个任务列 4～10 条，要具体、能逐条判断。
   - 逐条列出每个小问或要点，例如「(b) 求出 V_A、V_B」。
   - 写出容易出错的地方，例如「节点 C 的方程要包含 10 Ω 加 6 V 那条支路」。
   - **篇幅不写进验收标准，用 `max_chars`**。程序会数字数（非空白字符），超了先让执行者压缩，
     压不下来就直接判不通过，不再花钱请复核员数字数。2026-10-23 的实测里，6 个任务都写了
     「不超过 1500 字」，交回来的有 2200～3400 字，复核员好几轮都耗在数字数上。
   - 不要把预期答案写进标准里，否则复核就失去了意义。
   - **多角度分析、头脑风暴、方案比较这类开放题，默认不复核**：照样写 `acceptance`（执行者会看到，
     并按它自查），同时给任务设 `"review": false`。汇总时由你自己抽查关键事实。用户明确要求严格核对时
     才打开复核。依据：2026-10-23 的实测里，6 个角度的头脑风暴有复核时花了 28 分钟、$1.37，
     大部分耗在核对行号和数字数上。
5. **预检再运行**：用了 `inputs` 或 `depends_on` 时，先运行 `python3 -I SWARM plan tasks.json`，
   确认展开后的 id、分层顺序和检查命令都对，这一步不调用 DeepSeek；加 `--show <id>` 可以看某个任务
   完整的执行者 prompt。然后运行 `python3 -I SWARM run tasks.json`。预计超过 2 分钟的话放到后台运行。
6. **核对**，按这个顺序：
   - 先看 `summary.json`，状态有五种：
     - `passed`：通过了复核或检查命令。`verified_by` 写明是哪一种（`review`、`check_cmd` 或两者）。
       只靠 `check_cmd` 通过的，说明命令通过了，不说明测试本身写得够严。
     - `unverified`：返工后仍没通过。
     - `unreviewed`：有结果但没复核。可能是没写 `acceptance`、任务设了 `"review": false`、用了 `--no-review`，或者复核调用
       重试后仍失败；最后这种同时列在 `review_failed` 里，原因在 `review_error`。这些结果只能当草稿。
     - `blocked`：依赖的上游任务没完成（`failed`、`unverified` 或 `blocked`），所以没有运行。
     - `failed`：执行调用失败，没有结果。
   - 再看每个任务的 `selfcheck.uncertain`，这里是最可能出错的地方。
   - 对关键结论做**独立验证**：数值可以用程序重算，事实对照原文，引用逐条查。
   - 通读结果，找复核员容易漏掉的问题：标签和描述错误、前后矛盾、凭空出现的「如图」。
   - 不合格的，改了 prompt 或验收标准后用 `--only id1,id2` 重跑。
7. **汇总**：合并成最终结果交给用户，说明哪些部分由 DeepSeek 起草、你核对了什么、核对方法是什么。
   没核对过的内容不能当作确定结论交给用户。

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
- `check_cmd`、`check_timeout`：执行者每交回一版，脚本就在任务的 `cwd` 里运行这条命令（默认最长
  300 秒）。退出码不是 0 就把输出的最后 3000 字打回给执行者返工，这一轮不请复核员。
  `{result}` 会被换成这一轮结果文件的路径，例如 `python3 -m json.tool {result}` 检查 JSON 格式。
  命令只能由你写，DeepSeek 写不了它。
- `inputs`：一个或几个 glob（相对于 `cwd`），每匹配到一个文件就展开成一个任务。所有字符串字段里的
  `{path}`（相对路径）、`{name}`（文件名）、`{stem}`（不带扩展名）、`{i}`、`{n}`（第几个、共几个）
  都会被替换。id 里没有占位符时会自动加上 `-{stem}`；文件名没有英文字母时改用序号。
  展开后超过 50 个任务就拒绝运行（`--max-tasks` 可以调）。占位符是原样替换的，路径里可能有空格时，
  在 `check_cmd` 里自己加引号。
- `depends_on`：要等哪些任务完成，可用 `*` 通配，例如 `["lec-*"]`。上游结果会放进这个任务的 prompt，
  每份最多 `upstream_chars` 字（默认 6000），超出的部分执行者可以自己去读全文。上游没完成，这个任务
  就标成 `blocked`。用 `--only` 只重跑下游时，会沿用上游的旧结果；只重跑上游时，下游不会自动重跑。

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
- `--retries 1`：执行和复核调用出错时各自重试几次。复核失败不会占用返工次数。
- `--max-output-tokens 64000`：每次调用的输出上限，推理也算在内。Claude Code 默认 32000，
  DeepSeek 想得长时会撞到。
- `--max-tasks 50`：`inputs` 展开后最多允许多少个任务。

## 写代码时怎么用

分工：你定接口、拆任务、集成和最终运行；DeepSeek 做读得多、写得多、互不依赖的部分。

适合交出去的：读懂陌生代码库（`read`，每人一个模块，交回短摘要）；代码审查和找 bug（`read`，开复核，
复核员按源码核对每条 bug 能否触发）；写单元测试、互不依赖的小模块、样板代码、文档和注释（`edit`，
每人写自己的文件，配 `check_cmd`）；大段日志分析（`read`）；技术选型（`none`，不复核）。

不要交出去的：牵一发动全身的改动（拆开后接口对不上）；要边运行边调试的 bug；架构决策（可以让
DeepSeek 先列方案）；改几行的小事。

规划要点：
- **先定接口再派活**：函数签名、数据格式、文件位置、命名风格、语言版本、能不能加依赖，都写进
  `shared_context`。执行者看不到 CLAUDE.md，项目约定不写就不知道。
- **一个任务只写自己的文件**。`__init__.py`、路由表、配置这类共用文件留给你最后统一改。
- **在 git 分支上跑**，跑完看 `git diff`，不满意就还原。
- **`max_chars` 管交回的文字，不管写进文件的代码**。`edit` 任务把它设成 200～400，只交回
  「改了哪些文件、做了什么、哪里没把握」。
- **用 `check_cmd` 跑测试，通常不开复核**：复核员只能读代码，不能运行。检查命令只跑这个任务自己的
  测试（例如 `python3 -m unittest tests.test_parser`），不要跑整个测试集：并行的其他执行者可能
  还没写完自己的文件。
- **先写接口，再并行实现**：用 `depends_on` 让一个执行者先写好接口文件，其他执行者再基于它写实现。
- **不要把有机密的目录交给执行者**：它能读 `cwd` 和 `add_dirs` 里的文件，读到的内容会发给 DeepSeek。
  `.env`、密钥、token 所在的目录不要给；需要时复制一份去掉机密的代码。
- **`check_cmd` 会运行执行者写的代码**：比如它写的测试，会以用户的权限直接在这台电脑上执行，没有沙箱。
  只在 git 管理、没有机密的目录里用；第一次对某个项目这样用之前，先告诉用户。

```json
{
  "shared_context": "Python 3.11，测试用 unittest，不加依赖。只能新建自己那个测试文件，不改其他文件。交回内容只写：新建了哪个文件、覆盖了哪些函数、哪些行为没把握。",
  "tasks": [
    {"id": "test-{stem}", "inputs": "src/*.py", "cwd": "/abs/repo", "tools": "edit", "max_chars": 300,
     "prompt": "读 {path}，在 tests/test_{stem}.py 写单元测试。",
     "check_cmd": "python3 -m unittest tests.test_{stem}",
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
- `summary.json`：每个任务的状态、轮数、自查、复核结论、耗时和估算费用。费用按 DeepSeek
  官方价格估算，区分高峰和非高峰时段。

## 什么时候真的省钱

实测（见 `benchmarks/`）：一份小试卷，Claude 自己简洁地做大约要 $0.5～0.8；走并行流程，Claude 这边的规划、
读完全部结果、核对就要 $0.6～0.9，再加上 DeepSeek 的 $0.24，**反而没省钱**。值得用的情况是：

- **要读的材料多**（很多长文档），而需要的结果短。这正是 DeepSeek 最便宜的地方。
- **你只读摘要和抽查**：用脚本、`summary.json` 和自查字段来核对，不把每份全文都读进上下文。
- **每个任务都设 `max_chars`**：DeepSeek 写得很啰嗦，篇幅越长，你读的成本和复核的成本都越高。
- **在上下文短的会话里规划**：长会话里每调用一次工具，都要把整个上下文重新读一遍。

用户要的是完成度时，不要为了省钱牺牲核对。但对于小任务，如果自己做更快更省，就直接告诉用户。

## 规则

- **隐私**：执行者以 `--bare` 模式运行，不读 CLAUDE.md、记忆、插件和 MCP。不要把用户的住址、
  证件、健康等个人信息写进任务，除非用户明确同意。
- **不要同时写同一个文件**：用 `edit` 或 `bash` 预设时，每个任务写各自的文件，最后由你合并。
- **模型**：默认是 `deepseek-flash`。只有用户要求或任务特别难时，才用 `deepseek-v4-pro`。
  `deepseek-v4-pro` 更贵，而且不能识图。
- **作业和考试**：执行者的结果只是草稿和参考，最终答案由你核对。遵守学术诚信。
- **API Key**：不要打印出来，也不要写进任何文件。
