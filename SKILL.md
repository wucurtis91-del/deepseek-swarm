---
name: deepseek-swarm
description: Drive a persistent loop where Claude decides direction, DeepSeek workers execute, and a Python controller enforces acceptance, state, budgets and recovery. Use for multi-round reading, extraction, coding, test-writing or diagnostics whose next step depends on results. Use the one-shot runner when the task list is fixed. Requires Claude Code CLI and a DeepSeek key (DEEPSEEK_API_KEY or a DeepSeek provider in CC Switch).
---

# DeepSeek 持续工作流

**分工**：Claude 主持方向、写决策和最终判断；DeepSeek worker 执行单个任务；Python 控制器负责
调度、预算、检查、复核、持久化、补丁与验收证据。完成状态由当前验收证据和 `complete` 校验决定。

**入口**：

- 固定任务清单，一次跑完：`scripts/swarm.py run tasks.json`（原行为）。
- 结果会影响下一步、需要修复/重规划/补验收：`scripts/swarm_loop.py`（本文件）。

`SWARM`/`LOOP` 分别指两个脚本的绝对路径。完整安装、配置、决策 schema 和运行目录说明见
`README.md`；本文件只给操作要点。

## 适用与最短路径

适用：先调查再决定、任务中途可能出现反证、代码改动需要 diff/测试/路径验收、运行需要预算或恢复。
不适用：几个固定且互不依赖的小任务用 `swarm.py run`；几行代码或必须自己交互调试的问题直接做。

外部主持模式（默认，复用当前 Claude 会话）：

```bash
python3 -I $LOOP start runs/demo --goal goal.json --config roles.json
python3 -I $LOOP next runs/demo
# 读 packets/NNNN.json，写 decision.json
python3 -I $LOOP decide runs/demo decision.json
python3 -I $LOOP next runs/demo
```

重复 `next → 写 decision → decide`，直到 `status` 为 `complete`、`paused`、`needs_input` 或
`cancelled`。`next` 会输出/写入决策材料；`decide` 执行就绪任务并写下一份材料。会话自身的
token 不进控制器账本，状态记为 `external_unmetered`。

自动主持模式（控制器调用配置的 planner）：

```bash
python3 -I $LOOP start runs/auto --goal goal.json --config roles.json --mode auto
python3 -I $LOOP auto runs/auto --max-steps 20
```

自动模式默认最多 `loop.max_steps`（配置，默认 50）步；planner 鉴权/配置/模型身份不可用时
`paused`，不会换模型继续。fake backend 仅用于离线测试，CLI 必须显式加
`--allow-fake-backend`。

## 参数与输入输出

| 参数/命令 | 行为 |
|---|---|
| `start RUN_DIR --goal FILE` | 创建运行目录；`--goal-text` + 可重复 `--criteria` 可不写文件。默认 `mode=external`，`isolate=worktree`。 |
| `start --config FILE` | 角色、预算、loop 配置；启动配置随状态保存，后续命令默认沿用；显式 `--config` 覆盖本次命令的角色与 loop 配置，运行预算保持原值。 |
| `start --workspace REPO` | 代码任务的 git 仓库；也可在 goal 的 `workspace.repo` 指定。 |
| `start --isolate worktree` | 代码任务唯一支持的隔离方式；`isolate=none` 在启动前明确报错，不会创建或复用其他目录。 |
| `next RUN_DIR` | 打印当前决策材料；除恢复外只读。 |
| `decide RUN_DIR FILE` | 校验并应用一个 JSON 决策，然后执行就绪任务、写下一份材料。 |
| `auto RUN_DIR [--max-steps N]` | 自动 planner 循环；默认 `external` 运行需先切换为 auto。 |
| `resume` | 从上一次状态恢复；`executing`/`running` 会先走恢复流程。 |
| `status` / `cancel` | 查看摘要/预算；取消写标记并通知活动写入者。 |
| `artifacts [--show TASK]` | 列出或打印任务产物和补丁。 |
| `validate RUN_DIR FILE` | 只校验决策；`complete` 的过期证据也会在这里被拒绝。 |

决策文件和 packet 都是 JSON。任务必须有 `id`、`prompt`、`question`、`produces`、
`decision_impact`；其余默认值见 `README.md` 的任务 schema。

## 决策

每个决策必须有非空 `reason`；`evidence` 只能引用材料里的 `ev-...`。

| action | 何时用 | 结果/后续 |
|---|---|---|
| `dispatch` | 有新的可执行任务 | 新任务进入 pending；依赖满足后执行。 |
| `repair` | 方向正确，实现/验收失败 | 给 `target_task` 和 `instructions`；旧产物保留，v+1 从旧代码补丁继续，旧证据失效。 |
| `verify` | 需要独立检查已有结果 | 给 `target_task`；验证任务必须是能区分答案的检查。 |
| `replan` | 假设被否定、方法不可行、任务定义错误 | 可增改任务、取消未完成任务；受影响下游标 `stale`。 |
| `complete` | 全部完成条件已有当前有效证据 | 控制器重新核对任务状态/版本、产物/补丁哈希、输入与上游、验收标准；失败则记录 `completion_rejection` 并继续。 |
| `request_input` | 缺少只有用户能提供的信息 | 必须给具体 `question`；状态 `needs_input`。 |
| `pause` | 预算、外部条件或明确停止 | 给 `reason`；状态 `paused`。 |
| `request_context` | 需要正文但不想执行动作 | 支持 `offset`/`limit`/`next_offset`；请求内容不会被普通裁剪丢弃。 |

代码 `repair`：默认把上一版补丁应用到当前任务的输入基线（上游补丁已提交为 `input_commit`），
worker 从上一版结果继续；最终 patch 是相对当前输入基线的完整结果，旧补丁和旧产物保留。
上一版补丁不存在/冲突、基线已变化、越界改动、上一版处于 `unknown`/`interrupted` 时，任务记录
`repair_inheritance`、`previous_base_changed`、`outside_scope` 或 `patch_conflict`，由主持者选择
检查、修复或重规划；不覆盖用户工作区。

## 验收与完成

- `deterministic`：每条 criterion 用 `check` 指向程序检查。
- `semantic`：reviewer 必须覆盖全部 criterion id，`met` 必须是布尔；缺项、字符串布尔、非空
  `errors` 都不能通过。
- `combined`：程序检查和复核都通过。
- `none`：明确未验收的草稿，不能作为完成证据。
- `negative_check`：在副本中注入已知错误，要求测试能检出该错误并失败。

`complete` 引用的是 `completion_usable=true` 的 artifact 证据。完成前会沿依赖链递归核对当前任务
和祖先任务的状态/版本、artifact/patch 是否存在及哈希、`input_files`、验收标准签名和验收结果；
错误会带失效任务、文件和依赖路径。共享上游的文件只读取一次，每条依赖边分别核对版本。删除/修改产物、补丁或输入、
修改祖先版本后，旧证据不能再完成；自动与外部模式走同一检查。

## 代码、权限与预算

- 代码任务在 `workspace.repo` 的独立 git worktree 中执行，隔离方式只支持 `worktree`；记录
  `base_commit`、`input_commit`、changed files、diff/patch、检查、越界文件和上游补丁。用户未提交
  改动不会被重置或覆盖。
- `allowed_paths` 是验收范围，不是运行时沙箱；越界改动导致验收失败。
- 检查命令以当前用户身份运行，文件访问权限与当前用户一致。
- 文件进入哪个模型请求：planner 收到决策材料（摘要、哈希、明确请求的产物片段）；worker 收到
  任务 prompt、上游正文和其工作目录文件；reviewer 收到结果正文、验收标准、检查输出和 git diff。
  不会把整个仓库或全部历史自动放进 planner/reviewer 请求。
- 非 git 工作区没有 patch 证据，不能作为完成证据；`--allow-unversioned-code` 只关闭 git 要求，
  不改变这一点，写入仍按快照串行执行。
- `max_chars` 是可见正文字符上限；`max_output_tokens` 是单次 API 输出上限；`max_tokens` 是
  估算预算（一次 CLI 调用可能包含多轮模型请求）。
- `max_seconds` 是停止新增调用的运行预算；已经启动的进程按自身 `timeout` 或被取消而结束，
  预算到点不会强行杀掉在途进程。`max_calls`/轮次也是停止新增工作的限制。
- `max_usd` 按价格表估算，是软预算；未知价格不会套用便宜默认价。未取得 usage 的超时调用记为
  unknown，不是零费用。`max_calls_per_role` 计入在途调用。

## 状态与恢复

运行目录包含 `state.json`、`events.jsonl`（带 seq/哈希链）、`packets/`、`decisions/`、
`artifacts/`、`worktrees/`、`run.lock`、`cancel.request`。

- 截断的事件尾部会在写入前修复；同一 `RunStore` 连续 commit 不会再次截断新事件；损坏中段或
  未来 schema 会报错，不用静默降级。
- `state.json` 损坏时从最后完整事件快照和后续事件重建。
- 已完成且仍有效的任务不重跑；中断时 `interrupted` 表示无副作用，`unknown` 表示可能已写文件，
  不会自动重放。
- 上游结果、输入文件或角色配置变化会使下游 `stale`，旧证据不可用于完成。
- `resume` 恢复后继续；`cancel` 安全停止；`status` 显示 `triggers` 和 `stop_reason`。

## 一次性入口

```bash
python3 -I $SWARM check
python3 -I $SWARM plan tasks.json
python3 -I $SWARM run tasks.json --concurrency 6
python3 -I $SWARM run tasks.json --only ch2,ch5
```

保留 `inputs`、`depends_on`、`acceptance`、`check_cmd`、`max_chars`、`review`/`--no-review`、
重试和返工；`check_argv` 优先且不经过 shell；`--only` 重跑后下游失效会写 `stale` 并返回非零。

## 测试与评测边界

```bash
python3 -I -m unittest discover -s tests -v
python3 benchmarks/harness.py --mode fake --include-claude-alone
```

离线测试和 fake benchmark 只验证控制逻辑、状态、预算、验收和恢复，不能证明真实模型更准确或
更省钱。真实评测需要用户明确同意预算：

```bash
python3 benchmarks/harness.py --mode real --config roles.json \
    --i-understand-this-spends --max-usd 3 --max-calls 60
```
