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


def main():
    data = _read_stdin()
    out = {"continue": True}
    if P is None or _ensure_panel is None:
        print(json.dumps(out, ensure_ascii=False))
        return

    prompt = _extract_prompt(data)
    session_id = (data.get("session_id")
                  or (isinstance(data.get("tool_input"), dict)
                      and data["tool_input"].get("session_id")) or "")

    # 1) 立刻确保面板服务在线（兜底：万一开机自启 / 上次会话的兜底实例没着落）
    #    ⚠️ 必须 quick=True（v1.0.40）：本 hook 的时限只有 15s，而完整拉起路径
    #    （等 12s 就绪 + PowerShell 查父进程 + 跑 --build）最坏 40s+，
    #    慢路径实测就是「UserPromptSubmit timeout 15000ms」把整条 hook 拦掉。
    #    quick 版秒回且不因启动慢而失败——面板起不来也得让这条命令正常执行。
    panel_up, panel_note = _ensure_panel(P.PANEL_PORT, quick=True)

    # 2) 建/续一张「用户请求」会话卡：received_at = 接到命令的时刻
    #    job_id 用 sess-<session_id>，同一会话始终复用同一张卡。
    #    round_reset=True（v1.0.30）：**用户新提交一条命令 = 新一轮执行**，
    #    「接到命令 / 已运行」必须从本回合重新起算——否则会累计上一轮耗时
    #    （实测出现「接到命令 08:45、已运行 10h15m」）。步骤列表同步清空，
    #    本回合的工具动作由 hook_bg 重新映射进来。
    notes = []
    # 兜底拉起的结果（"已后台发起"/"端口还没通"…）只进 stderr，便于排查，不影响会话
    if panel_note:
        notes.append(panel_note)
    if prompt:
        jid = "sess-" + (session_id or "default")
        # v1.0.38：prompt 里混着 WB 注入的 XML（<task-notification>…</task-notification> 等），
        # 直接截前 60 字会把整串代码当标题 → 先清洗成人话再截断（见 progress.clean_title）
        title = P.clean_title(prompt, 44)
        P.register(title, job_id=jid, source="user-request",
                   session_id=session_id, message="接到用户命令，待执行",
                   cmd=prompt[:200], steps=[], round_reset=True)
        notes.append("已建会话任务卡（接到命令 %s）" % time.strftime("%H:%M:%S"))

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
               ) % (url, os.path.join(HERE, "progress.py"), jid if prompt else "<id>",
                    jid if prompt else "<id>")
    else:
        ctx = ("【任务执行进度·实时面板】面板地址 %s。本轮开始执行前用 present_files 打开它"
               "（让用户实时看到步骤/进度/日志）；本轮结束时面板卡由 Stop hook 自动收尾。"
               % (url,))
    out["hookSpecificOutput"] = {"hookEventName": "UserPromptSubmit", "additionalContext": ctx}
    if notes:
        sys.stderr.write("[hook_prompt] " + "；".join(notes) + "\n")
    print(json.dumps(out, ensure_ascii=False))


if __name__ == "__main__":
    try:
        main()
    except Exception:
        print(json.dumps({"continue": True}))
