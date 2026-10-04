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

⚠️ 两套文案（用户 2026-10-04 定的口径，v1.0.66 修订）：
- **步骤条 / 会话内容**（`_step_label` → `humanize.plain_tool`）→ 给人看：中文前缀
  （`执行命令:` / `编辑文件:` …）+ **人话内容**；**两处逐字一致**（同一函数，v1.0.66 统一）。
- **日志**（`_label_raw`）→ **完全保持原样**（原始工具名 + 原始命令 + `[自动]` 标记）：
  日志是给**专业人员排障**看的，不做人话化。

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
try:
    import humanize as H          # 「人话」词典：命令行 / 助手正文 → 中文通俗说法（v1.0.59）
except Exception:
    H = None

MAX_LABEL = 40

# 主干工具成步、轻量工具只进日志（口径不变，只是步骤标签的**内容**换成人话）
_STEP_TOOLS = ("Bash", "PowerShell", "Write", "Edit", "Agent",
               "TaskCreate", "TaskUpdate", "TaskOutput")


def _short(s, n=MAX_LABEL):
    """中间省略：保留首尾关键信息（命令开头 + 末尾参数/文件名），中间用 … 截断，
    避免长命令/路径把日志撑出容器。"""
    s = (s or "").strip()
    if len(s) <= n:
        return s
    head = (n - 1) // 2
    tail = n - 1 - head
    return s[:head] + "…" + s[-tail:]


def _brief_input(tool, tinput):
    """日志用：从工具入参里取一段原始摘要（不翻译）。"""
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


def _label_raw(tool, tinput):
    """**原样**标签（日志专用）—— 与 v1.0.58 及以前**逐字一致**，日志口径不许漂移。

    返回 (label, mode)；mode ∈ {'step','log','skip'}。
    """
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


def _step_label(tool, tinput, raw_label):
    """步骤条文案 = 与会话内容行**同一个** `humanize.plain_tool()`（v1.0.66 统一口径）。

    早期这里自己拼前缀（`"执行命令: " + H.plain_command(...)`）、会话内容行只有内容，
    于是同一件事在一张卡里两种措辞（用户 2026-10-04 报「表达重复，可统一」）。
    现在两处共用 `humanize.plain_tool`（前缀仍是原来的中文前缀，只把内容换成人话），
    **改文案只改 humanize 一处**。人话化失败时原样退回 `raw_label`，绝不留空。
    """
    if H is None or not H.enabled():
        return raw_label
    try:
        return H.plain_tool(tool, tinput) or raw_label
    except Exception:
        return raw_label


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
        # ⚠️ 标题别写死「会话执行中」：纯图片 / 纯附件提交时 WB 的 bundle 里
        # `if(!el) return;` 直接跳过整条 UserPromptSubmit，卡只能在这里（PreToolUse 或
        # PostToolUse）补建 —— 用户看到的就是这张卡的标题。这里读 transcript 数附件，
        # 写成人话「用户发送了 3 张图片…」；读不到才退回「会话执行中」。
        P.register(P.user_input_title(session_id), job_id=jid, source="auto-step",
                   session_id=session_id, message="首个工具调用，自动建卡", steps=[])
        j = P.load_jobs().get(jid)
        if j is None:
            print(json.dumps(out, ensure_ascii=False))
            return

    label, mode = _label_raw(tool, tinput)
    if mode == "skip":
        print(json.dumps(out, ensure_ascii=False))
        return
    if j.get("manual_steps"):
        # 主动接管模式：只把动作记进日志，不打乱手动步骤条（日志保持原样文案）
        P.update(jid, log_line="[自动·日志] %s" % (label or tool))
    elif mode == "step" and label:
        # 步骤条走人话文案；**日志仍写原始文案**（`log_text`），保证排障信息不丢
        P.append_auto_step(jid, _step_label(tool, tinput, label), complete=not PRE,
                           log_text=label)
    else:  # log 模式：日志保持原样（原始工具名 + 原始参数）
        P.update(jid, log_line="[自动] %s" % label)
    print(json.dumps(out, ensure_ascii=False))


if __name__ == "__main__":
    try:
        main()
    except Exception:
        print(json.dumps({"continue": True}))
