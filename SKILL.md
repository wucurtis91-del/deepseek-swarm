---
name: deepseek-swarm
description: Offload token-heavy work to several DeepSeek workers running in parallel, each checked by an independent DeepSeek reviewer against acceptance criteria and sent back for rework if it fails, while Claude plans, verifies and merges. Use when the user says 用 DeepSeek 并行做 / 让 DeepSeek 干 / 省 token / 多个 DeepSeek 一起想, or asks to split a large reading, summarising, solving, translating, drafting or brainstorming job across cheap parallel workers. Needs Claude Code CLI and a DeepSeek API key (env DEEPSEEK_API_KEY, or a DeepSeek provider saved in CC Switch).
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
3. **拆任务**：拆成 2～20 块互不依赖的子任务。每块的 prompt 要能单独看懂，写清楚
   做什么、读哪些文件、输出什么格式。
4. **写验收标准（`acceptance`）**：每个任务列 4～10 条，要具体、能逐条判断。
   - 逐条列出每个小问或要点，例如「(b) 求出 V_A、V_B」。
   - 写出容易出错的地方，例如「节点 C 的方程要包含 10 Ω 加 6 V 那条支路」。
   - 写明格式和篇幅，例如「不超过 800 字」。DeepSeek 默认写得很长。
   - 不要把预期答案写进标准里，否则复核就失去了意义。
5. **运行**：`python3 -I SWARM run tasks.json`。预计超过 2 分钟的话放到后台运行。
6. **核对**，按这个顺序：
   - 先看 `summary.json`。`unverified` 表示返工后仍没通过，`failed` 表示调用失败。
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
    {"id": "q1", "prompt": "解第 1 题的 (a)(b)(c)。", "cwd": "/abs/path", "tools": "read",
     "acceptance": ["(a) 给出 R_EQ 及化简过程", "(b) 依次求出 V_B、V_A、V_S，注明所用定律",
                    "所有数值带单位，且与电路连接一致（复核员要独立求解核对）", "不超过 1500 字"]},
    {"id": "idea", "prompt": "从成本角度提 3 个方案。", "tools": "none"}
  ]
}
```

每个任务的可选字段：
- `acceptance`：有这一项才会进行复核和返工。
- `cwd`、`tools`、`add_dirs`：工作目录、工具预设、额外允许访问的目录。
- `model`、`review_model`：执行者和复核员用的模型。
- `max_turns`、`timeout`：每次调用的最大轮数和超时秒数。

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

## 结果

默认写在任务文件旁边的 `<tasks>-results/` 里：
- `<id>.md`：最终版本。
- `<id>.vN.md` 和 `<id>.vN.review.json`：每一轮的版本和复核意见。
- `summary.json`：每个任务的状态、轮数、自查、复核结论、耗时和估算费用。费用按 DeepSeek
  官方价格估算，区分高峰和非高峰时段。

## 什么时候真的省钱

实测（见 `benchmarks/`）：一份小试卷，Claude 自己简洁地做大约要 $0.5～0.8；走并行流程，Claude 这边的规划、
读完全部结果、核对就要 $0.6～0.9，再加上 DeepSeek 的 $0.24，**反而没省钱**。值得用的情况是：

- **要读的材料多**（很多长文档），而需要的结果短。这正是 DeepSeek 最便宜的地方。
- **你只读摘要和抽查**：用脚本、`summary.json` 和自查字段来核对，不把每份全文都读进上下文。
- **给结果设字数上限**：DeepSeek 写得很啰嗦，篇幅越长，你读的成本和复核的成本都越高。
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
