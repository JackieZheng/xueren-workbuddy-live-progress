# -*- coding: utf-8 -*-
"""Stop / SessionEnd hook：回合结束与会话结束的**确定性收尾**（v1.0.31 新增）。

背景（2026-10-01 用户反馈的两个问题）：
1. 「会话没自动打开预览面板」——收尾契约全靠代理自觉，经常被忽略；
2. 「WB 提示会话已结束，面板还显示正在执行」——sess 卡只在
   SessionStart + 30 分钟无更新时被 gc 标异常中断，正常结束但没跑
   finish-session 的会话，卡片会一直挂着 running。

本 hook 挂在两个事件上（settings.json 注册）：

- **Stop**（Agent 完成响应，每回合一次，⚠️ 不等于会话结束）：
  - 本会话还有 running 的 bg-* 卡 → 不动（后台任务仍在跑，等 hook_bg 级联收尾）；
  - 本回合有实际工具动作（steps 非空）但**从未**用 present_files 打开过面板
    → **exit 2 拦截结束**，stderr 反馈强制代理先打开面板（文档：Stop 退出码 2 =
    提供反馈，stderr 注入给 Agent）；
  - stop_hook_active=True（已拦截过一次）→ 不再死循环，直接放行（不收卡）；
  - **其余情况（已打开面板 / 纯问答）一律「不收卡」**——Stop 只是回合结束，
    会话可能还有下一回合（用户会继续追问/派活），卡片必须保持 running，
    交给 SessionEnd（会话真正终止）或显式 finish-session 收尾。
    ❌ 旧逻辑（v1.0.31）：Stop 在「面板打开过」时直接 finish 会话卡，
    导致**每个回合结束都被误报成「任务已完成」**——用户视角：任务明明没结束却显示结束。

- **SessionEnd**（会话终止：切换/删除/清空）：
  - 无条件收掉本会话 sess 卡；running 的 bg-* 卡标记「面板停止跟踪」
    （任务在系统里继续跑，但本会话已终止、不会再有 TaskOutput 回填）。

全部异常吞掉：任何出错都 exit 0，绝不影响会话本身。
"""
import os
import sys
import json

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    import progress as P
except Exception:
    P = None


def _out(obj):
    print(json.dumps(obj, ensure_ascii=False))


def _ok(system_message=None):
    o = {"continue": True}
    if system_message:
        o["systemMessage"] = system_message
    _out(o)
    return 0


def _running_bg_of(jobs, session_id):
    return [k for k, v in jobs.items()
            if k.startswith("bg-") and v.get("status") == "running"
            and (v.get("session_id") or "") == (session_id or "")]


def handle_stop(data):
    session_id = data.get("session_id") or ""
    jid = "sess-" + (session_id or "default")
    jobs = P.load_jobs()
    j = jobs.get(jid)
    if not j or j.get("status") != "running":
        return _ok()

    # 后台任务仍在跑：卡片保持 running，等 hook_bg 级联收尾（最后一个 bg 结束时收）
    if _running_bg_of(jobs, session_id):
        return _ok()

    # 已经拦截过一次（stop_hook_active）→ 不再死循环，直接放行；
    # 不收卡：会话结束由 SessionEnd 或显式 finish-session 收尾。
    if data.get("stop_hook_active"):
        return _ok()

    received = j.get("received_at") or 0
    presented = j.get("panel_presented_at") or 0
    active = bool(j.get("steps"))            # 本回合有工具动作（round_reset 后重新累计）

    # 本回合干了活但从未打开面板 → 拦截结束，强制补 present_files
    # （注意：Stop = 回合结束，**不等于**会话/任务结束，这里绝不收卡，
    #  否则面板会在每个回合结束时误报「已完成」——用户视角：任务明明没结束却显示结束）
    if active and not (presented and presented >= received):
        msg = ("【进度面板·收尾契约（自动强制）】本轮结束前没有用 present_files 打开实时面板，"
               "用户无法在预览区看到执行过程。请立即调用 present_files 打开 %s"
               "（把该地址放进 files 数组即可，可附上本轮产物），打开后正常结束本轮；"
               "面板卡会在**会话结束（SessionEnd）时**自动收尾，无需再手动 finish-session。"
               ) % P.panel_url()
        sys.stderr.write(msg + "\n")
        _out({"continue": False, "stopReason": msg})
        return 2

    # 已打开面板 / 纯问答：本轮正常结束，但会话仍在继续 → 卡片保持 running，
    # 交给 SessionEnd（会话终止）或显式 finish-session 收尾。
    return _ok()


def handle_session_end(data):
    session_id = data.get("session_id") or ""
    jid = "sess-" + (session_id or "default")
    jobs = P.load_jobs()
    j = jobs.get(jid)
    if j and j.get("status") == "running":
        P.finish(jid, ok=True, message="会话结束·自动收尾")
    # 会话终止后不会再有 TaskOutput 回填：running 的 bg 卡标记「停止跟踪」
    for k in _running_bg_of(jobs, session_id):
        P.finish(k, ok=True, message="会话已结束，面板停止跟踪（后台任务继续在系统中运行）")
    return _ok()


def main():
    if P is None:
        _out({"continue": True})
        return 0
    try:
        data = P.read_stdin_json()
    except Exception:
        _out({"continue": True})
        return 0
    event = (data.get("hook_event_name") or "").lower()
    try:
        if event == "sessionend":
            return handle_session_end(data)
        return handle_stop(data)          # 默认按 Stop 处理
    except Exception:
        try:
            _out({"continue": True})
        except Exception:
            pass
        return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        print(json.dumps({"continue": True}))
        sys.exit(0)
