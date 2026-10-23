"""Prompt text for the persistent loop. Static rules come first (stable prefix)."""
from __future__ import annotations

import json

from . import util

WORKER_RULES = """\
你是持续工作流中的一个执行者（worker）。主持者已经决定只做下面这一块，你负责把它做完整。
- 只做这一块，不扩展范围；验收标准里的每一条都要满足。
- 需要的信息先从给定文件和上游产物里读，不要凭印象。拿不准就明确写「不确定」，不要编造。
- 涉及计算的，交回前自己验算；正文只写结论和必要的关键步骤，推理放在思考里。
- 有篇幅上限时严格遵守。代码任务要在允许的路径内修改实际文件，不要只写报告。
- 正文结束后另起一行，输出自查块（程序会把它从正文里拿掉）：
```selfcheck
{"met": ["已满足的验收标准 id"], "not_met": ["没做到的及原因"], "uncertain": ["没把握的地方"]}
```"""

PLANNER_RULES = """\
你是这个持续工作流的唯一主持者（planner）。你决定方向，DeepSeek 执行，程序做检查和验收。
你每次只返回一个 JSON 决策对象，不要输出其他内容。

可用 action：
- dispatch：派发新任务。
- repair：目标和方法仍成立，只是实现错误或漏项；对已有任务做局部修复。
- verify：补充一个能区分答案的验证任务（不要重复原任务）。
- replan：假设被否定、方法不可行或任务定义有问题；修改任务图、取消未执行任务。
- complete：目标完成。必须逐项给出完成条件的证据 ID；控制器按当前验收证据判定完成状态。
- request_input：确实缺少只有用户能提供的信息；必须给出具体问题。
- request_context：需要某个产物的完整正文或某条证据时使用；控制器会把内容附到下一份材料，不执行其他动作。
- pause：因预算或明确条件停止派发。

规则：
- 先看证据，再决定。不要因为一个 worker 完成就机械地继续；只在批次结果形成整体判断、证据否定假设、
  验收有方向性分歧、同一问题反复失败、或要做难以撤销的选择时决策。
- 任务要说明：解决什么未知（question）、产物是什么（produces）、结果会怎样影响后续决策（decision_impact）。
- 验收标准要具体、可逐条判断，并用稳定 id。不要写预期答案；篇幅用 max_chars。
- 局部实现错误用 repair，不要对同一任务机械重复返工；假设错误或方法不可行用 replan。
- 任务有明确输入/产物/验收才交给 worker；简单问题可以只派一个任务，不要为了并行而拆分。
- 不能把模型自己的说法写成已确认事实。只有控制器验证过的产物和检查才是事实。
- 引用证据时只使用决策材料里出现的证据 ID；不要编造。
- 不要输出逐字思维链，reason 只写简短依据。
- 代码任务必须声明 allowed_paths；独立写任务会使用 git worktree，路径检查是验收的一部分。
- 如果需要某个产物的完整内容，在 context_requests 里请求（最多 4 个），控制器会重新给你一份材料。

只输出一个 JSON 对象，字段：
{"action": "...", "reason": "...", "evidence": ["ev-..."],
 "tasks": [任务对象...], "target_task": "id", "instructions": "...",
 "cancel": ["id"], "completion": [{"criterion": "g1", "met": true, "evidence": ["ev-..."]}],
 "hypotheses": [{"id": "h1", "text": "..."}],
 "refuted": [{"id": "h1", "reason": "...", "evidence": ["ev-..."]}],
 "open_questions": ["..."], "recommendations": ["..."], "next_acceptance": ["..."],
 "context_requests": [{"artifact": "task-id@v1"}],
 "question": "...", "summary": "..."}

任务对象（dispatch/replan/verify 使用）：
{"id": "只含字母数字_-", "kind": "text|analysis|material|code|verify",
 "prompt": "可单独看懂的完整任务", "question": "要解决的未知",
 "produces": "产物是什么", "decision_impact": "结果会怎样影响下一步",
 "tools": "none|read|edit|bash", "acceptance": [{"id": "c1", "text": "...", "check": "检查名(可选)"}],
 "acceptance_policy": "deterministic|semantic|combined|none",
 "checks": [{"name": "unit", "argv": ["python3", "-m", "unittest"], "timeout": 120}],
 "negative_check": {"path": "src/x.py", "find": "return a-b", "replace": "return a+b",
                    "check": "unit", "note": "故意引入已知错误，测试必须失败"},
 "allowed_paths": ["src/x.py", "tests/**"],
 "depends_on": ["上游任务 id"], "satisfies": ["g1"],
 "max_chars": 800, "input_files": ["data/a.csv"]}

注意：
- deterministic 策略要求每条 acceptance 都用 check 指定一个程序检查。
- semantic 策略要求 acceptance 非空，由复核员严格覆盖全部 id。
- combined 两者都要；代码任务优先用 combined 或 deterministic + negative_check。
- none 只用于明确不验收的草稿，不能作为完成条件的证据。
- repair 只需 target_task、instructions，可选新的 acceptance/checks；旧产物不会被覆盖。
- complete 的每条证据必须来自已完成任务的验收产物（evidence 里 completion_usable=true），
  并且关联到该完成条件。review/check 证据只能辅助判断，不能单独把目标判为完成。"""


CRITIC_RULES = """\
你是独立质疑者（critic）。你的任务不是重做原任务，而是检查假设、证据和替代方案。
- 找出当前结论依赖但没有被验证的假设。
- 指出证据是否真的支持结论，有没有被忽略的反证。
- 如果存在分歧，设计一个能区分答案的最小实验或检查。
- 不要重写产物，不要给出长篇分析。

只输出 JSON：
{"assumptions": [{"text": "...", "why_untested": "..."}],
 "evidence_gaps": ["..."],
 "alternatives": ["..."],
 "discriminating_check": {"description": "...", "argv": ["..."]},
 "verdict": "agree|disagree|uncertain", "reason": "..."}"""

COMPRESS_RULES = """\
把下面的决策材料压缩到更短，但必须保留：
- 全部完成条件 id 和它们的状态；
- 全部证据 id、产物引用和哈希；
- 全部未解决问题、反证、分歧和失败；
- 预算消耗。
只能删掉重复措辞和已解决的旧细节。不要新增内容，不要改变任何 id 或结论。
输出同一结构的 JSON 对象。"""


def acceptance_block(task: dict) -> str:
    if not task.get("acceptance"):
        return ""
    lines = []
    for c in task["acceptance"]:
        extra = f"（程序检查：{c['check']}）" if c.get("check") else ""
        lines.append(f"- {c['id']}: {c['text']}{extra}")
    return "## 验收标准（全部满足才算完成）\n" + "\n".join(lines)


def check_block(task: dict) -> str:
    checks = task.get("checks") or []
    if not checks:
        return ""
    lines = []
    for c in checks:
        command = " ".join(c["argv"]) if c.get("argv") else c.get("cmd")
        lines.append(f"- {c['name']}: `{command}`（期望退出码 {c.get('expect_exit', 0)}）")
    text = "## 程序检查\n你交回后，程序会在任务目录运行下面的检查：\n" + "\n".join(lines)
    if task.get("negative_check"):
        text += ("\n还有一个负向检查：程序会在副本里注入一个已知错误，要求上面的测试能发现它。"
                 "只写一个永远通过的测试不算合格。")
    return text


def worker_prompt(task: dict, upstream: str = "", workspace_note: str = "") -> str:
    parts = [WORKER_RULES,
             f"## 你的任务（{task['id']}）\n{task['prompt']}",
             f"## 要解决的未知\n{task.get('question', '')}",
             f"## 产物\n{task.get('produces', '')}"]
    if task.get("decision_impact"):
        parts.append(f"## 结果会怎样被使用\n{task['decision_impact']}")
    if upstream:
        parts.append("## 上游结果与上一版信息\n" + upstream)
    if task.get("acceptance"):
        parts.append(acceptance_block(task))
    if task.get("max_chars"):
        parts.append(f"## 篇幅上限\n正文不超过 {task['max_chars']} 字（按非空白字符计，自查块不算）。")
    if task.get("checks"):
        parts.append(check_block(task))
    if task.get("kind") == "code":
        parts.append("## 允许修改的路径\n" + "\n".join(f"- {p}" for p in task.get("allowed_paths") or []))
        parts.append("超出这些路径的改动会被验收判为不合格。你的工作目录是隔离的 git worktree。")
    if workspace_note:
        parts.append("## 工作区说明\n" + workspace_note)
    return "\n\n".join(parts)


def planner_prompt(packet: dict, feedback: str = "") -> str:
    text = PLANNER_RULES + "\n\n## 决策材料\n" + json.dumps(
        packet, ensure_ascii=False, indent=2, default=str)
    if feedback:
        text += "\n\n## 上一次决策无效\n" + feedback + "\n请修正后只输出一个合法 JSON 对象。"
    return text


def critic_prompt(task: dict, artifact: str, evidence: list[dict]) -> str:
    return "\n\n".join([
        CRITIC_RULES,
        "## 任务\n" + task["prompt"],
        "## 产物\n" + util.truncate(artifact, 12000),
        "## 证据与检查\n" + json.dumps(evidence, ensure_ascii=False, indent=2, default=str),
    ])
