# -*- coding: utf-8 -*-
"""PostToolUse hook：把用户的工具调用**默认自动映射**成会话卡（sess-<session>）的步骤，
让面板无需手动接入就显示"执行到哪一步"。

规则（详见 SKILL.md「默认自动映射」）：
- 主干工具（Bash / PowerShell / Write / Edit / Agent / TaskCreate / TaskUpdate / TaskOutput）
  调用完成 → 追加为会话卡的一个步骤（✓ 已完成）。
- 轻量工具（Read / Grep / Glob / WebFetch / WebSearch 等）只记一行日志，不撑爆步骤条。
- 用户/代理**主动规划**了步骤（manual_steps=True，即调用过 begin --steps / steps）后，
  自动映射退居日志，只把动作记进日志，不打乱手动步骤条。
- Bash run_in_background 跳过（已由 hook_bg 登记成独立 bg- 卡）。

由 settings.json 的 hooks.PostToolUse（matcher: *）调用。stdin 收 hook JSON，stdout 回 JSON。
全部异常吞掉，绝不影响会话。
"""
import os
import sys
import json
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    import progress as P
except Exception:
    P = None

MAX_LABEL = 40


def _short(s, n=MAX_LABEL):
    """中间省略：保留首尾关键信息（命令开头 + 末尾参数/文件名），中间用 … 截断，
    避免长命令/路径把步骤条撑出容器。"""
    s = (s or "").strip()
    if len(s) <= n:
        return s
    head = (n - 1) // 2
    tail = n - 1 - head
    return s[:head] + "…" + s[-tail:]


def _label_for(tool, tinput):
    """返回 (label, mode)；mode ∈ {'step','log','skip'}。"""
    if not isinstance(tinput, dict):
        tinput = {}
    if tool == "Bash":
        if tinput.get("run_in_background"):
            return None, "skip"                      # 交给 hook_bg 登记成独立卡
        return "执行命令: " + _short(tinput.get("command", "")), "step"
    if tool == "PowerShell":
        return "PowerShell: " + _short(tinput.get("command", "")), "step"
    if tool == "Write":
        return "写入文件: " + os.path.basename(tinput.get("file_path", "") or ""), "step"
    if tool == "Edit":
        return "编辑文件: " + os.path.basename(tinput.get("file_path", "") or ""), "step"
    if tool == "Agent":
        d = tinput.get("description") or tinput.get("prompt") or ""
        if isinstance(d, list):
            d = " ".join(str(x) for x in d)
        return "子代理: " + _short(d), "step"
    if tool == "TaskCreate":
        return "建任务: " + _short(tinput.get("subject") or ""), "step"
    if tool == "TaskUpdate":
        return "更新任务: " + _short(tinput.get("subject")
                                   or tinput.get("taskId") or ""), "step"
    if tool == "TaskOutput":
        return "读取后台输出: " + _short(tinput.get("task_id") or ""), "step"
    # 其余工具（Read / Grep / Glob / WebFetch / WebSearch / AskUserQuestion / present_files …）
    return "%s: %s" % (tool, _short(_brief_input(tool, tinput))), "log"


def _brief_input(tool, tinput):
    if tool == "Read":
        return tinput.get("file_path", "")
    if tool == "Grep":
        return tinput.get("pattern", "") or ""
    if tool == "Glob":
        return tinput.get("pattern", "")
    if tool in ("WebFetch", "WebSearch"):
        return tinput.get("url") or tinput.get("query") or ""
    if tinput:
        return json.dumps(tinput, ensure_ascii=False)[:40]
    return ""


def main():
    out = {"continue": True}
    PRE = "--pre" in sys.argv          # PreToolUse 调用时带 --pre：插入「进行中」步
    if P is None:
        print(json.dumps(out, ensure_ascii=False))
        return
    try:
        data = P.read_stdin_json()     # 显式 UTF-8 解码，避免中文乱码
    except Exception:
        print(json.dumps(out, ensure_ascii=False))
        return

    tool = data.get("tool_name") or ""
    tinput = data.get("tool_input") or {}
    session_id = data.get("session_id") or ""
    if not session_id or not tool:
        print(json.dumps(out, ensure_ascii=False))
        return

    jid = "sess-" + session_id

    # present_files 打开面板检测：载荷里含面板地址（端口）即打点 panel_presented_at，
    # hook_stop 在回合结束时据此决定「自动收卡」还是「拦截并要求先打开面板」。
    if tool == "present_files":
        try:
            blob = json.dumps(tinput, ensure_ascii=False)
        except Exception:
            blob = str(tinput)
        if str(P.PANEL_PORT) in blob or (P.panel_url() in blob):
            try:
                P.mark_panel_presented(session_id)
            except Exception:
                pass

    j = P.load_jobs().get(jid)
    if j is None:
        # 兜底：自动映射不依赖 UserPromptSubmit 是否触发——首个工具调用即自建会话卡，
        # 保证「默认自动映射」在任意会话都能工作（含平台未派发 UserPromptSubmit 的情况）。
        # 之后若 UserPromptSubmit 才触发，因共用同一 jid 会复用此卡并保留更早的 received_at。
        P.register("会话执行中", job_id=jid, source="auto-step",
                   session_id=session_id, message="首个工具调用，自动建卡", steps=[])
        j = P.load_jobs().get(jid)
        if j is None:
            print(json.dumps(out, ensure_ascii=False))
            return

    label, mode = _label_for(tool, tinput)
    if mode == "skip":
        print(json.dumps(out, ensure_ascii=False))
        return
    if j.get("manual_steps"):
        # 主动接管模式：只把动作记进日志，不打乱手动步骤条
        P.update(jid, log_line="[自动·日志] %s" % (label or tool))
    elif mode == "step" and label:
        P.append_auto_step(jid, label, complete=not PRE)
    else:  # log 模式
        P.update(jid, log_line="[自动] %s" % label)
    print(json.dumps(out, ensure_ascii=False))


if __name__ == "__main__":
    try:
        main()
    except Exception:
        print(json.dumps({"continue": True}))
