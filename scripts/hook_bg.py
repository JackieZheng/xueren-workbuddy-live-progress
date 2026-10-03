# -*- coding: utf-8 -*-
"""PostToolUse hook：自动把「后台任务」登记到任务执行进度·实时面板，并提醒代理打开面板。

由 ~/.workbuddy/settings.json 的 hooks.PostToolUse 调用（matcher: Bash|TaskOutput）。
约定：stdin 收 hook JSON，stdout 回 hook JSON（绝不影响会话——全部异常吞掉）。

职责：
1. Bash + run_in_background → 自动登记一个 indeterminate 任务（标题从命令提炼）。
2. TaskOutput → 依据返回内容把对应任务回填为 已完成 / 失败。
3. **自动拉起面板服务**（不在跑就调 panel_ctl.py start，WMI 会话外常驻），
   再通过 additionalContext 让代理 present_files 打开面板。
"""
import os
import re
import sys
import json
import time
import socket
import hashlib
import subprocess

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    import progress as P
except Exception:
    P = None

HERE = os.path.dirname(os.path.abspath(__file__))
PANEL_CTL = os.path.join(HERE, "panel_ctl.py")
START_GUARD = os.path.join(P.ROOT, "panel_start.json") if P else None
NUDGE_FILE = None

# 调试开关：在注册表目录放一个 debug_hook.on 文件即开始记录 hook 原始入参（排查用，删文件即关）
DEBUG_MARK = os.path.join(P.ROOT, "debug_hook.on") if P else None


def _debug_dump(data):
    try:
        if not DEBUG_MARK or not os.path.exists(DEBUG_MARK):
            return
        with open(os.path.join(P.ROOT, "hook_in.jsonl"), "a", encoding="utf-8") as f:
            f.write(json.dumps(data, ensure_ascii=False)[:6000] + "\n")
    except Exception:
        pass


def _read_stdin():
    # 显式 UTF-8 解码（中文 Windows 默认 cp936 → 直接 sys.stdin.read() 会出乱码）
    return P.read_stdin_json() if P is not None else {}


def _resp_text(resp):
    if resp is None:
        return ""
    if isinstance(resp, str):
        return resp
    try:
        return json.dumps(resp, ensure_ascii=False)
    except Exception:
        return str(resp)


def _title_from_cmd(cmd):
    """从命令提炼一个人类可读标题：优先脚本名，其次首个可执行词。"""
    cmd = (cmd or "").strip()
    if not cmd:
        return "后台任务"
    m = re.findall(r"[\w\-./\\]+\.(?:py|js|mjs|cjs|sh|ps1|exe|bat)", cmd)
    if m:
        name = os.path.basename(m[-1].replace("\\", "/"))
        return name
    first = re.split(r"\s*(?:&&|\|\||;|\|)\s*", cmd)[0]
    return (first[:70] + "…") if len(first) > 70 else first


def _nudge_allowed(session_id, min_gap=600, bucket=""):
    """给「提醒」限频，避免刷屏。

    ⚠️ 踩坑（2026-10-01）：bucket 必须按 hook 来源区分！
    早期 hook_bg（PostToolUse，每次 Bash/TaskOutput 都触发）与 hook_prompt
    （UserPromptSubmit，只在用户提交命令时触发）**共用同一个 key = session_id**，
    高频的那个会把额度吃光 —— 后果是「本轮开始前打开面板」这条**最关键的回合级提醒**
    被静默掐掉，代理整轮都不知道该打开面板（用户视角：这个会话怎么没自动打开面板）。
    现在各自一个桶：hook_prompt 用 bucket="prompt"、hook_bg 用 bucket="bg"，互不挤占。
    """
    global NUDGE_FILE
    if P is None:
        return True
    NUDGE_FILE = os.path.join(P.ROOT, "nudges.json")
    key = ("%s:%s" % (bucket, session_id or "default")) if bucket else (session_id or "default")
    try:
        with open(NUDGE_FILE, encoding="utf-8") as f:
            d = json.load(f)
    except Exception:
        d = {}
    now = time.time()
    last = d.get(key, 0)
    if now - last < min_gap:
        return False
    d[key] = now
    d = {k: v for k, v in d.items() if now - v < 86400}
    try:
        os.makedirs(P.ROOT, exist_ok=True)
        with open(NUDGE_FILE, "w", encoding="utf-8") as f:
            json.dump(d, f)
    except Exception:
        pass
    return True


def _tcp_open(port, timeout=0.4):
    s = socket.socket()
    s.settimeout(timeout)
    try:
        return s.connect_ex(("127.0.0.1", int(port))) == 0
    except Exception:
        return False
    finally:
        s.close()


def _panel_ok(port, timeout=1.0):
    """端口上是不是**真面板**（能取到 /api/jobs 且含 summary）；探活绕过本机代理。"""
    if not _tcp_open(port):
        return False
    try:
        import urllib.request
        op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        r = op.open("http://127.0.0.1:%d/api/jobs" % int(port), timeout=timeout)
        return b"summary" in r.read(2000)
    except Exception:
        return False


def _start_guard_ok(min_gap=60):
    """失败限频：60 秒内启动失败过就不再重试，避免每个 hook 都卡十几秒。"""
    try:
        with open(START_GUARD, encoding="utf-8") as f:
            d = json.load(f)
    except Exception:
        d = {}
    return (time.time() - float(d.get("last_fail") or 0)) >= min_gap


def _mark_start_fail():
    try:
        os.makedirs(P.ROOT, exist_ok=True)
        with open(START_GUARD, "w", encoding="utf-8") as f:
            json.dump({"last_fail": time.time()}, f)
    except Exception:
        pass


def _stale_panel(port):
    """面板在跑、但跑的是**旧版代码**时返回 (运行中 build, 磁盘 build)，否则 (None, None)。"""
    run = None
    try:
        import urllib.request
        op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        r = op.open("http://127.0.0.1:%d/api/jobs" % int(port), timeout=1.5)
        run = (json.loads((r.read() or b"{}").decode("utf-8", errors="replace")) or {}).get("build")
    except Exception:
        return None, None
    code = None
    try:
        p = subprocess.run([sys.executable, os.path.join(HERE, "live_panel.py"), "--build"],
                           capture_output=True, timeout=15)
        b = (p.stdout or b"").decode("utf-8", errors="replace").split()
        code = b[-1] if b else None
    except Exception:
        pass
    return (run, code) if (run and code and run != code) else (None, None)


def _ensure_panel(port):
    """确保面板服务在线；返回 (alive, note)。不在跑就自动拉起（会话外常驻）。"""
    if _panel_ok(port):
        run, code = _stale_panel(port)
        if run:
            return True, ("⚠️ 端口 %d 上跑的是**旧版面板**（运行中 build=%s，磁盘代码 build=%s）——"
                          "代码改动不会生效（面板通常会在 3 秒内热更新，这里说明热更新没成功）。"
                          "完成升级：python %s upgrade（或 restart）；若提示停止失败，"
                          "按其指引结束占用进程或换端口 --port %d。"
                          % (int(port), run, code, PANEL_CTL, int(port) + 1))
        return True, None
    if _tcp_open(port):
        return False, ("端口 %d 被非面板程序占用：请改用 panel_ctl.py start --port 8792" % port)
    if not _start_guard_ok():
        return False, "面板启动刚失败过，60 秒内不再重试"
    try:
        exe = sys.executable
        pw = os.path.join(os.path.dirname(exe), "pythonw.exe")
        if not os.path.exists(pw):
            pw = exe
        # --auto：自动拉起的实例带空闲自退，绝不留下"永远不退出的面板拖住会话"
        r = subprocess.run([exe, PANEL_CTL, "start", "--port", str(int(port)),
                            "--no-browser", "--auto", "--json"],
                           capture_output=True, timeout=40)
        out = (r.stdout or b"").decode("utf-8", errors="replace").strip()
        info = json.loads(out.splitlines()[-1]) if out else {}
    except Exception:
        info = {}
    if _panel_ok(port, timeout=1.5):
        if info.get("detached") is False:
            return True, ("⚠️ 面板未真正脱离会话（%s）：请停掉后用 start_panel.bat 重启，"
                          "否则会话可能停在未完成状态" % (info.get("detach_why") or "原因未知"))
        return True, "面板服务已自动启动（会话外，空闲自动退出）"
    _mark_start_fail()
    return False, "面板服务自动启动失败（可手动 exec panel_ctl.py start）"


def _running_bg_of(jobs, session_id):
    """本会话仍在运行的 bg-* 卡 id 列表（hook_bg / hook_stop 共用口径）。"""
    return [k for k, v in jobs.items()
            if k.startswith("bg-") and v.get("status") == "running"
            and (v.get("session_id") or "") == (session_id or "")]


def main():
    data = _read_stdin()
    _debug_dump(data)
    out = {"continue": True}
    if P is None:
        print(json.dumps(out, ensure_ascii=False))
        return

    tool = data.get("tool_name") or ""
    tinput = data.get("tool_input") or {}
    resp = data.get("tool_response")
    session_id = data.get("session_id")
    cwd = data.get("cwd")
    rtext = _resp_text(resp)
    notes = []
    panel_up = None
    panel_note = None

    # 1) 后台 Bash 任务 → 自动登记
    if tool == "Bash" and isinstance(tinput, dict) and tinput.get("run_in_background"):
        cmd = tinput.get("command") or ""
        panel_up, panel_note = _ensure_panel(P.PANEL_PORT)   # 自动拉起面板服务
        tid = None
        m = re.search(r"task_id\s*[:=]\s*([A-Za-z0-9_-]+)", rtext)
        if m:
            tid = m.group(1)
        jid = "bg-" + (tid or hashlib.md5((cmd + str(time.time())).encode()).hexdigest()[:8])
        title = _title_from_cmd(cmd)
        # 以下三类不登记为"任务卡片"：
        #  - live_panel.py：面板服务自身，属基础设施（页面能打开即说明它在跑）
        #  - progress_bridge.py / progress.py start：脚本内部已自带进度上报
        #  - asr_whole.py（xueren-audio-video-to-text）：同样自带 progress 上报，避免双卡
        if "live_panel.py" in cmd:
            notes.append("面板服务自身不登记为任务")
        elif (("progress_bridge.py" in cmd)
              or re.search(r"progress\.py\s+start\b", cmd)
              or ("asr_whole.py" in cmd)):
            notes.append("该命令自带进度上报，跳过自动登记")
        else:
            P.register(P.clean_title(title), job_id=jid, source="hook-bg",
                       session_id=session_id, cwd=cwd,
                       cmd=cmd.strip()[:400], message="后台启动")
            P.update(jid, log_line="后台启动 · " + cmd.strip()[:140])
            notes.append("已登记后台任务 %s（id=%s）" % (title, jid))

    # 2) TaskOutput → 回填完成态
    #    真实载荷（本机实测）：tool_input={"task_id":...}，tool_response 是**字符串**文本块，
    #    首段形如 "Shell ID: xx\nCommand: ...\nStatus: completed\nDuration: 37s..."。
    #    所以以 "Status: <state>" 为准；只有拿不到 Status 行时才退回关键词判断。
    elif tool == "TaskOutput" and isinstance(tinput, dict):
        tid = tinput.get("task_id")
        if tid:
            jid = "bg-" + tid
            jobs = P.load_jobs()
            if jid in jobs:
                low = rtext.lower()
                m = re.search(r"status:\s*([a-z]+)", low)
                st = m.group(1) if m else ""
                if st in ("completed", "success", "succeeded", "failed", "error") \
                        or (not st and "completed" in low):
                    ok = st not in ("failed", "error")
                    bg_started = (P.load_jobs().get(jid) or {}).get("received_at") or 0
                    P.finish(jid, ok=ok, message="后台任务已结束" if ok else "后台任务失败")
                    notes.append("后台任务 %s 已标记为 %s" % (tid, "完成" if ok else "失败"))
                    # 级联收尾（v1.0.31）：Stop hook 遇到「后台任务仍在跑」会保留 sess 卡，
                    # 等到这里最后一个后台任务结束时一并收掉——否则卡片永远挂着 running。
                    # 防误收：若后台任务启动后用户又提交了新命令（sess.received_at 更新），
                    # 说明新回合已开始，卡片归新回合管，不收。
                    try:
                        _jobs2 = P.load_jobs()
                        _sid = session_id or ""
                        _sjid = "sess-" + (_sid or "default")
                        _s = _jobs2.get(_sjid)
                        if _s and _s.get("status") == "running" \
                                and not _running_bg_of(_jobs2, _sid) \
                                and (_s.get("received_at") or 0) <= bg_started:
                            P.finish(_sjid, ok=True, message="后台任务全部结束·自动收尾")
                    except Exception:
                        pass

    # 3) 自动维护面板服务 + 提醒代理打开面板
    url = P.panel_url()
    if panel_up is None:
        panel_up = _panel_ok(P.PANEL_PORT)
    running = len(P.list_jobs(only_running=True))
    if running or notes:
        parts = []
        if notes:
            parts.append("；".join(notes))
        if panel_note:
            parts.append(panel_note)
        if not panel_up:
            parts.append("任务执行进度·实时面板未运行")
        if _nudge_allowed(session_id, min_gap=180, bucket="bg"):
            ctx = ("【任务执行进度·实时面板 · 通用约定】%s。面板地址 %s（%s）。"
                   "面板服务的启停已由 hook 自动维护（用 panel_ctl 在**会话外**创建，"
                   "自动实例还带空闲自退），你**不需要、也不要**再去启动它——"
                   "**严禁用 Bash run_in_background 启动 live_panel.py**：那会让面板变成会话的后台任务，"
                   "任务永不结束，会话就会永远停在「未完成」状态。"
                   "只要本轮存在运行中的后台任务，就**必须**在本轮结束前调用 present_files 打开该地址，"
                   "让用户在预览区实时看到进度。"
                   "会话里要开关面板时直接用：python %s status|check|start|stop|restart"
                   ) % ("；".join(parts) if parts else "已检测到后台任务状态变化",
                        url, "运行中" if panel_up else "未运行", PANEL_CTL)
            out["hookSpecificOutput"] = {"hookEventName": "PostToolUse", "additionalContext": ctx}
    print(json.dumps(out, ensure_ascii=False))


if __name__ == "__main__":
    try:
        main()
    except Exception:
        print(json.dumps({"continue": True}))
