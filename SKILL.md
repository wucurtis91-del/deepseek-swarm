---
name: deepseek-swarm
description: Offload token-heavy work to several DeepSeek workers running in parallel, while Claude plans, checks and merges. Use when the user says 用 DeepSeek 并行做 / 让 DeepSeek 干 / 省 token / 多个 DeepSeek 一起想, or asks to split a large reading, summarising, translating, drafting or brainstorming job across cheap parallel workers. Needs Claude Code CLI and a DeepSeek API key (env DEEPSEEK_API_KEY, or a DeepSeek provider saved in CC Switch).
---

# DeepSeek 并行工作（deepseek-swarm）

分工：**你（Claude）规划、检查、汇总；DeepSeek 执行。** 每个 worker 都是一个独立的
`claude -p` 进程，接 DeepSeek 的 Anthropic 兼容接口，并行运行。

脚本：`scripts/swarm.py`（只用 Python 标准库）。下面的 `SWARM` 指这个文件的绝对路径。

## 流程

1. **先检查**：`python3 -I SWARM check`，应返回 `"ok": true`。失败就停下，把报错告诉用户。
2. **拆任务**：把工作拆成 2～20 块**互不依赖**的子任务。每块的 prompt 要能单独看懂：
   做什么、读哪些文件、输出什么格式、多长、做到什么算完成。
   所有 worker 共用的要求放进 `shared_context`。
3. **写任务文件**：放在 scratchpad 或项目里，格式见下文。
4. **运行**：`python3 -I SWARM run tasks.json [--concurrency 6]`。
   如果预计超过 2 分钟，就放到后台运行。
5. **检查**：读 `summary.json` 和每个 `<id>.md`。核对事实、计算和引用，以及有没有提到
   不存在的「如图」或文件。不合格的改 prompt 后用 `--only id1,id2` 重跑。
6. **汇总**：由你合并成最终结果交给用户，并说明哪些部分由 DeepSeek 起草、你核对过什么。
   没核对过的内容不能当作确定结论。

## 任务文件

```json
{
  "shared_context": "读者是工科本科生。用中文，术语附英文。每份不超过 300 字。",
  "tasks": [
    {"id": "ch1", "prompt": "读 notes/ch1.txt，总结要点并出 2 道练习题。", "cwd": "/abs/path"},
    {"id": "ch2", "prompt": "……", "cwd": "/abs/path", "tools": "read"},
    {"id": "idea", "prompt": "不用读文件，从成本角度提 3 个方案。", "tools": "none"}
  ]
}
```

每个任务可选字段：`cwd`（工作目录，默认是运行脚本时的目录）、`tools`、`model`、
`max_turns`、`timeout`（秒）、`add_dirs`（额外允许读写的目录列表）。

`tools` 预设：

| 预设 | 能做什么 | 用途 |
|---|---|---|
| `none` | 不用工具 | 纯思考、写作、头脑风暴 |
| `read`（默认） | Read、Grep、Glob | 读文件后总结或分析 |
| `edit` | 加上 Edit、Write，自动接受修改 | 每个 worker 写**各自的**文件 |
| `bash` | 加上 Bash，不再询问权限 | 只在用户明确同意、并且目录隔离时使用 |

## 结果

默认输出到任务文件旁边的 `<tasks>-results/`：每个任务一个 `<id>.md`（结果正文）和
`<id>.json`（原始输出），另有一个 `summary.json`，记录状态、耗时、token 和估算费用。
费用按 DeepSeek 官方价格并区分高峰时段估算，不是账单。

## 规则

- **隐私**：worker 跑在 `--bare` 模式，不读取 CLAUDE.md、记忆、插件或 MCP。不要把用户的
  住址、证件、健康等个人信息写进任务 prompt，除非用户明确同意。
- **不能同时写同一个文件**：用 `edit`/`bash` 时，每个任务写不同的文件或子目录，最后由你合并。
- **PDF**：DeepSeek 读不了 PDF 文件。先转成文本，比如用 `pdftotext`，或者先用 Read 读出
  再存成 .txt。
- **模型**：默认是 `deepseek-flash`（便宜，支持识图）。只有用户要求时才用 `--model deepseek-v4-pro`。
- **作业和考试**：worker 的结果只当草稿或参考，最终答案由你核对，而且要遵守学术诚信。
- 不要把 API Key 打印出来，也不要写进任何文件。
