# -*- coding: utf-8 -*-
"""SessionStart hook：会话开始时**确保面板服务在线**、清理陈旧任务，并注入使用约定。

设计原则：不改会话行为，只做三件事——① 拉起面板服务；② 兜底收尾异常中断的会话卡；③ 注入约定。

⚠️ 为什么 SessionStart 也要拉起面板（v1.0.35）：
面板是**内存态**服务（WMI 会话外常驻 + 空闲 30 分钟自退），WB 重启或长时间空闲后它就没了。
此前只有 hook_prompt（提交命令）与 hook_bg（起后台任务）会 ensure，
于是「**WB 刚启动、还没提交第一条命令**」的空档里，用户打开预览面板看到的是**空白/无法访问**
（用户反馈：「WB 启动了，实时进度看板没启动？」）。
现在会话一启动（SessionStart）就 ensure 一次，做到「打开会话即有面板」。

面板服务的启停也可用 `python scripts/panel_ctl.py status|start|stop|restart` 手动开关。
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

# 复用 hook_bg 的面板拉起逻辑（WMI 会话外创建 + 幂等 + 失败限频），避免重复实现
try:
    from hook_bg import _ensure_panel
except Exception:
    _ensure_panel = None


def prune(days=3):
    """清理超过 N 天且已结束的任务，避免注册表无限增长。"""
    try:
        jobs = P.load_jobs()
        now = time.time()
        keep = {}
        for k, j in jobs.items():
            if j.get("status") == "running":
                keep[k] = j
            elif now - j.get("updated_at", 0) < days * 86400:
                keep[k] = j
        if len(keep) != len(jobs):
            P.save_jobs(keep)
    except Exception:
        pass


def main():
    try:
        data = P.read_stdin_json()     # 显式 UTF-8 解码，避免中文乱码
    except Exception:
        data = {}
    out = {"continue": True}
    if P is None:
        print(json.dumps(out, ensure_ascii=False))
        return
    try:
        os.makedirs(P.ROOT, exist_ok=True)
        prune()
        # 异常中断兜底：上一轮会话若崩了/窗口被关，会话卡会永远挂在 running——
        # 新会话启动时按「30 分钟无更新」判定并收尾为 aborted（只动 sess-*，不碰业务卡）。
        gc = []
        try:
            gc = P.gc_abandoned()
        except Exception:
            gc = []
        # 会话一开始就确保面板在线（v1.0.35）：面板是内存态服务，WB 重启/空闲自退后就没了；
        # 此前只有「提交命令」与「起后台任务」才 ensure → 「WB 刚启动还没提交命令」的空档里
        # 用户打开预览是空白。这里幂等 ensure 一次，做到「开会话即有面板」。
        # ⚠️ quick=True（v1.0.40）：本 hook 时限只有 10s，而完整拉起路径最坏 40s+，
        #    慢路径会被「Hook timed out」拦掉——兜底反而起不来面板。quick 版秒回。
        panel_up, panel_note = (False, None)
        if _ensure_panel is not None:
            try:
                # wait_up=0：只发起不等端口（本 hook 时限只有 10s，面板冷启动要 ~10s，
                # 等它必然超时）。面板起来由紧跟其后的 UserPromptSubmit / 后台任务 hook 兜。
                panel_up, panel_note = _ensure_panel(P.PANEL_PORT, quick=True, wait_up=0)
            except Exception:
                panel_up, panel_note = (False, None)
        alive = P.panel_alive() or panel_up
        ctx = ("【任务执行进度·实时面板 · 本会话约定】通用面板地址 %s（当前%s）。"
               "本会话中若启动任何耗时/后台任务：①优先让脚本用 progress.Progress 上报进度；"
               "②面板服务在**会话启动 / 提交命令 / 起后台任务三个时机都会自动兜底拉起**"
               "（会话外创建；配了开机自启则常驻，否则 30 分钟空闲自退），进程冷启动约需 10 秒，"
               "拉起期间面板可能短暂打不开，下一条命令会自动再兜一次，不要手动 stop，"
               "也不要用 Bash run_in_background 去起它，"
               "你只需在本轮结束前调用 present_files 打开面板地址，让用户在预览区看到实时进度；"
               "③用户要开关面板时用 python %s status|check|start|stop|restart。"
               "⚠️ **严禁用 Bash run_in_background 启动 live_panel.py**——面板若成为会话的后台任务，"
               "它会一直运行、永不结束，会话就会永远停在「未完成」状态。"
               "🟢 **收尾已自动化**：面板每 5 秒读 WB 权威会话状态（workbuddy.db sessions.status）自动对表，"
               "回合结束（completed）≤7 秒自动收卡，你无需手动 finish-session；"
               "SessionEnd 与「30 分钟无更新」gc 仅作兜底。"
               "%s%s"
               ) % (P.panel_url(), "运行中" if alive else "未运行",
                    os.path.join(os.path.dirname(os.path.abspath(__file__)), "panel_ctl.py"),
                    ("（本次启动已兜底收尾 %d 张异常中断的会话卡）" % len(gc)) if gc else "",
                    ("（面板拉起提示：%s）" % panel_note) if panel_note else "")
        out["hookSpecificOutput"] = {"hookEventName": "SessionStart", "additionalContext": ctx}
    except Exception:
        pass
    print(json.dumps(out, ensure_ascii=False))


if __name__ == "__main__":
    try:
        main()
    except Exception:
        print(json.dumps({"continue": True}))
