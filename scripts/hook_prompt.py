# -*- coding: utf-8 -*-
"""UserPromptSubmit hook：用户一提交命令，就**立刻**拉起任务执行进度·实时面板并建一张
「用户请求」会话任务卡（received_at = 此刻 = 接到命令的时间），再提醒代理
present_files 打开面板——让用户从「命令落地」那秒起就能在预览区看到执行过程。

由 ~/.workbuddy/settings.json 的 hooks.UserPromptSubmit 调用。
约定：stdin 收 hook JSON，stdout 回 hook JSON（全部异常吞掉，绝不影响会话）。

之所以单独建卡而不是等后台任务：原面板只在「后台 Bash 任务」出现时才开，
纯前台工作（写文件 / 调 API / 推理 / 多步 agent 流程）全程看不到面板。
本 hook 把"接到命令"这一刻钉进时间线，并用一张会话卡承接后续步骤与进度。
"""
import os
import sys
import json
import time
import threading

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
try:
    import progress as P
except Exception:
    P = None

# 复用 hook_bg 的面板拉起与提醒限频逻辑（避免重复实现 WMI 会话外启动那套）
try:
    from hook_bg import _ensure_panel, _nudge_allowed
except Exception:
    _ensure_panel = None
    _nudge_allowed = None


def _read_stdin():
    # 显式 UTF-8 解码（中文 Windows 默认 cp936 → 直接 sys.stdin.read() 会出乱码）
    return P.read_stdin_json() if P is not None else {}


def _extract_prompt(data):
    """从各种可能的 hook 载荷形态里抠出用户提交的文本。"""
    p = (data.get("prompt") or data.get("user_prompt") or data.get("message")
         or data.get("text") or "")
    ti = data.get("tool_input")
    if not p and isinstance(ti, dict):
        p = ti.get("prompt") or ti.get("message") or ti.get("text") or ""
    if isinstance(p, list):                       # 偶尔是 content blocks 数组
        p = " ".join(str(x) for x in p)
    return (p or "").strip()


def _spawn_panel_async(port):
    """后台线程兜底拉面板，主线程不等（v1.0.56）。

    原来这里是同步 `_ensure_panel(port, quick=True)`：quick 版虽已秒回/秒起，
    但面板冷启动或机器忙时仍可能磨到 15s 时限，把整条 UserPromptSubmit hook
    掐掉——卡虽已建（锚点在），stderr 打点与后续逻辑全丢，事后只看得到
    「卡是后来补建的、⏱已运行从 AI 开工那一刻才开始算」。
    改成 daemon 线程后，主线程建完卡立刻回 hook JSON 退出，
    整条 hook 耗时 = 建卡耗时（实测几十毫秒），物理上不可能被超时掐断。
    """
    def _run():
        try:
            _up, note = _ensure_panel(port, quick=True)
            if note:
                sys.stderr.write("[hook_prompt][panel] %s\n" % note)
        except Exception:
            pass
    try:
        threading.Thread(target=_run, daemon=True).start()
    except Exception:
        pass


def main():
    t_begin = time.time()
    data = _read_stdin()
    out = {"continue": True}
    if P is None or _ensure_panel is None:
        print(json.dumps(out, ensure_ascii=False))
        return

    prompt = _extract_prompt(data)
    session_id = (data.get("session_id")
                  or (isinstance(data.get("tool_input"), dict)
                      and data["tool_input"].get("session_id")) or "")

    notes = []

    # ⚠️ 顺序是关键（v1.0.54）：**先建卡，再确保面板在线**。
    #    原先把 _ensure_panel 放在前面，而 UserPromptSubmit 的时限只有 15s——面板一旦
    #    冷启动把这条 hook 顶超时，hook 会被直接掐掉、卡一直没建；要等后续命令（hook_bg /
    #    代理第一次 progress.begin）才补建，received_at 落到**那一刻**，于是面板时间线的
    #    「接到命令」比真实提交晚十几秒、「已运行」同量偏小（实测 1m17s vs 1m00s）。
    #    本 hook 就是「接到命令」唯一的权威时间源，必须赶在拉面板之前把锚点钉死。
    # ⚠️ v1.0.56：**无条件建卡**（原来写成 `if prompt:`，纯图片 / 空文本 / 只有附件的
    #    提交拿不到 prompt 文本就整条跳过 → 卡要等 AI 开工时才被补建，⏱「已运行」从
    #    AI 开始处理那刻才起算，正是用户抱怨的「AI 开始处理才计时」的漏点。
    #    时间锚点是这条 hook 的唯一权威，必须覆盖**所有**提交形态。
    jid = "sess-" + (session_id or "default")
    # 纯图片 / 纯附件提交时 WB **不派发这条 hook**（实测 bundle：
    # `let el=es.join("\n").trim(); if(!el) return;` —— prompt 为空直接跳过整条），
    # 所以走到 `prompt` 为空这一步已经说明是「空文本 + 没数到附件」的极端形态。
    # 标题绝不能再写「（未识别到文本输入）」这种机器话：先按 transcript 数附件
    # （→「用户发送了 3 张图片、1 个文件」），真什么都读不到才用兜底文案（v1.0.57）。
    # ⚠️ 不能简单写 `shown = prompt or …`：纯图片提交时 WB 送来的 prompt **不是空串**
    # 而是整串占位符 `<image_local_path>C:\…\clipboard-x.png</image_local_path>`
    # （v1.0.57 实测，扫过 609 个 transcript）。先抠掉注入块与占位符，真文本为空
    # 才按 transcript 里的附件统计写成人话「用户发送了 N 张图片」。
    shown = prompt
    if not P._strip_injection(shown).strip():
        # 兜底文案也别写「（未识别到文本输入）」这类机器话（用户明确否掉过）：
        # 真连 transcript 都读不到时，退回产品既有口径「会话执行中」。
        shown = P.user_input_title(session_id, fallback="会话执行中")
        notes.append("（本次提交只有附件/占位符，标题取自附件统计）")
    # v1.0.38：prompt 里混着 WB 注入的 XML（<task-notification>…</task-notification> 等），
    # 直接截前 60 字会把整串代码当标题 → 先清洗成人话再截断（见 progress.clean_title）
    title = P.clean_title(shown, 44)
    # 卡片 `cmd` 字段 = **清洗后的用户原话**（剥掉 WB 注入的 `<task-notification>` /
    # `<system-reminder>` 等 XML 与附件占位符）。面板元信息「在做什么: …」直接展示它——
    # 早期这里存的是**原始 prompt**，于是元信息里出现过
    # 「运行系统命令（<task-notification>…）」这种机器话（用户 2026-10-04 报）。
    # 原始 prompt 一字不少地留在 transcript 里，排障不受影响。
    cmd_text = (P._strip_injection(prompt).strip() or shown)[:200]
    t_card = time.time()
    P.register(title, job_id=jid, source="user-request",
               session_id=session_id, message="接到用户命令，待执行",
               cmd=cmd_text, steps=[], round_reset=True)
    notes.append("已建会话任务卡（接到命令 %s，建卡耗时 %.0fms）"
                 % (time.strftime("%H:%M:%S"), (t_card - t_begin) * 1000))

    # 2) 再确保面板服务在线（兜底：万一开机自启 / 上次会话的兜底实例没着落）
    #    ⚠️ 必须 quick=True（v1.0.40）：完整拉起路径
    #    （等 12s 就绪 + PowerShell 查父进程 + 跑 --build）最坏 40s+，
    #    慢路径实测就是「UserPromptSubmit timeout 15000ms」把整条 hook 拦掉。
    #    quick 版秒回且不因启动慢而失败——面板起不来也得让这条命令正常执行。
    #    v1.0.56 起走后台线程，主线程不等（见 _spawn_panel_async）：面板拉起慢
    #    也绝不会拖住这条 hook，卡与锚点早已落盘。
    _spawn_panel_async(P.PANEL_PORT)

    # 3) 提醒代理 present_files 打开面板，让用户实时看到执行过程
    #    ⚠️ 回合级提醒必须用**独立限频桶**（bucket="prompt"）：它只在用户提交命令时触发，
    #    是「打开面板」唯一的信号源。早期与 hook_bg（PostToolUse，每次 Bash 都触发）
    #    共用 key=session_id 的同一个桶，额度被高频的那个吃光 → 这段 additionalContext
    #    被静默吞掉，代理整轮都不会去打开面板（见 hook_bg._nudge_allowed 的踩坑注释）。
    #    另外：即使被限频，也兜一句**精简版**，保证回合级提醒永不静默丢失。
    url = P.panel_url()
    if (_nudge_allowed is None) or _nudge_allowed(session_id, min_gap=20, bucket="prompt"):
        ctx = ("【任务执行进度·实时面板 · 通用约定】用户已提交命令，面板已拉起并建卡。"
               "🔴 **本轮第一个动作**：用 present_files 打开 %s，让用户在预览区实时看到"
               "执行过程：📥接到命令时间 / ▶开始执行时间 / 步骤进度条 / 已完成·剩余量 / 日志。"
               "默认每个工具调用会**自动映射**成这张会话卡的一个步骤（无需手动接入，轻量工具进日志）。"
               "若想自己规划步骤，用 `python %s begin --id %s --title \"...\" --steps \"准备,处理,收尾\"` 接管这张卡"
               "（之后自动映射退居日志，不打乱你的步骤条）；每完成一步 `progress.py step --id %s --index N`。"
               "🟢 **收尾已自动化（v1.0.33）**：面板每 5 秒读 WB 权威会话状态（workbuddy.db sessions.status）自动对表——"
               "会话执行中（working）卡片保持运行中，回合结束（completed）≤7 秒自动收卡，你无需手动 finish-session。"
               "回合结束的 Stop hook 只负责「没开面板就拦截要求先打开」。"
               "**没打开过且本轮有实际操作 → 结束会被拦截**，你会收到一条反馈要求先 present_files 打开面板。"
               "所以：先打开面板，后面就什么都不用管。例外：本轮起的**后台任务卡（bg-*）与自建业务卡**"
               "仍需你用 `list --running` 检查并逐个 `finish --id <id>`（后台任务结束时会自动级联收卡）。"
               ) % (url, os.path.join(HERE, "progress.py"), jid, jid)
    else:
        ctx = ("【任务执行进度·实时面板】面板地址 %s。本轮开始执行前用 present_files 打开它"
               "（让用户实时看到步骤/进度/日志）；本轮结束时面板卡由 Stop hook 自动收尾。"
               % (url,))
    out["hookSpecificOutput"] = {"hookEventName": "UserPromptSubmit", "additionalContext": ctx}
    if notes:
        sys.stderr.write("[hook_prompt] " + "；".join(notes) + "\n")
    # 耗时打点：正常情况下这条 hook 必须在几十毫秒内交卷（时限 15s），
    # 一旦这里看到 1000ms+，就说明卡锚点随时可能被超时连带丢掉，要立刻查。
    sys.stderr.write("[hook_prompt] 本条 hook 总耗时 %.0fms\n" % ((time.time() - t_begin) * 1000))
    print(json.dumps(out, ensure_ascii=False))


if __name__ == "__main__":
    try:
        main()
    except Exception:
        print(json.dumps({"continue": True}))
