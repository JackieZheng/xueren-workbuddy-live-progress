# -*- coding: utf-8 -*-
"""任务执行进度·实时面板的开关控制器（供会话/命令行统一调用）

用途：让「打开面板 / 关闭面板」不依赖双击 bat，代理在会话里一条命令即可完成。

    python scripts/panel_ctl.py status   [--port 8791] [--json]
    python scripts/panel_ctl.py check    [--port 8791] [--json]   # 面板会不会拖住当前会话？
    python scripts/panel_ctl.py start    [--port 8791] [--no-browser] [--auto] [--idle-exit 秒] [--json]
    python scripts/panel_ctl.py spawn    [--port 8791] [--json]   # 只发起不等待（hook 兜底，秒回）
    python scripts/panel_ctl.py stop     [--port 8791] [--json]
    python scripts/panel_ctl.py restart  [--port 8791] [--json]

关键点：
- start 幂等：端口上已有真面板（能取到 /api/jobs 且含 summary）就只复用，绝不重复起实例。
- start 常驻：默认用 PowerShell WMI（Win32_Process.Create）在**会话 job 对象之外**创建进程，
  因此会话结束后面板不会被回收（代理直接 Popen 的子进程会被 job 对象杀掉，故仅作兜底）。
  启动后会核验新进程的父进程，并在 panel_state.json 里留下「启动方式 + 是否真正脱离会话」。
- **绝不要用 `Bash run_in_background` 启动面板**：那会让面板变成会话的后台任务，
  任务永远不结束 → 会话永远处于未完成状态。要常驻就用 start_panel.bat 双击，
  要自动化就用本脚本（hook 已经在用，且会带 --auto 的空闲自退）。
- stop 按端口找 LISTENING 的 PID 结束，不区分是谁启动的（bat / 代理 / 上次会话都能关）。
- 探活一律绕过本机代理（ProxyHandler({})），否则 127.0.0.1 会被代理拦截误判"没起来"。
"""
import os
import sys
import json
import time
import socket
import argparse
import subprocess
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
PANEL = os.path.join(HERE, "live_panel.py")
DEFAULT_PORT = int(os.environ.get("LIVE_PROGRESS_PORT") or 8791)
PORT_SCAN = range(8791, 8800)      # status 找不到时顺手扫一圈，便于提示"面板在别的端口"
ROOT = (os.environ.get("LIVE_PROGRESS_DIR")
        or os.path.join(os.path.expanduser("~"), ".workbuddy", "live-progress"))
STATE_FILE = os.path.join(ROOT, "panel_state.json")   # 记录"谁、用什么方式、带哪些参数"起的面板
# WMI 拉起时的 stdout/stderr 落点（**每次启动截断**）。用途有二：
#   ① 排障：WMI 报「创建成功」但进程秒退时，这里是唯一能看到 traceback 的地方；
#   ② 修坑：给 pythonw 一个**有效的标准输出句柄** —— WMI（Win32_Process.Create）
#      创建进程不给标准句柄，pythonw 下 `sys.stdout` 会是 None，任何裸 print 都会
#      抛 AttributeError 把进程当场掀掉（2026-10-04 实测踩到）。
SPAWN_LOG = os.path.join(ROOT, "panel_spawn.log")

# hook 自动拉起的实例带空闲自退：既能在会话外常驻，又不会留下永久僵尸面板
AUTO_IDLE_EXIT = 1800.0            # 秒（30 分钟）


# ---------------------------------------------------------------- 基础工具
def _probe(port, timeout=1.2):
    """端口上是否有一个**真正的面板**（只看连通不算，要能取到 /api/jobs 里的 summary）"""
    s = socket.socket()
    s.settimeout(timeout)
    try:
        if s.connect_ex(("127.0.0.1", int(port))) != 0:
            return False
    finally:
        s.close()
    try:
        op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        r = op.open("http://127.0.0.1:%d/api/jobs" % int(port), timeout=timeout)
        return b"summary" in r.read(2000)
    except Exception:
        return False


def _running_build(port, timeout=1.5):
    """运行实例的页面指纹（/api/jobs 的 build 字段）；取不到返回 None。"""
    try:
        op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        r = op.open("http://127.0.0.1:%d/api/jobs" % int(port), timeout=timeout)
        d = json.loads((r.read() or b"{}").decode("utf-8", errors="replace"))
        return d.get("build")
    except Exception:
        return None


def _code_build(timeout=20):
    """磁盘上 live_panel.py 的页面指纹（子进程问它自己，不把服务端 import 进来）。"""
    try:
        r = subprocess.run([sys.executable or "python", PANEL, "--build"],
                           capture_output=True, timeout=timeout)
        b = (r.stdout or b"").decode("utf-8", errors="replace").split()
        return b[-1] if b else None
    except Exception:
        return None


def _stale_warning(port, run_build, code_build):
    """跑着的是旧版时的提醒 + 升级指引（status / check / hook 共用）。"""
    return ("⚠️ 端口 %d 上跑的是**旧版面板**（运行中 build=%s，磁盘代码 build=%s）——"
            "改了 live_panel.py 不会自动生效。\n   完成升级：python %s upgrade"
            "（或 restart）；若提示停止失败，见其给出的换端口/结束占用进程指引。"
            % (int(port), run_build or "?", code_build or "?", os.path.abspath(__file__)))


def _pids_on(port):
    """占用该端口且处于 LISTENING 的 PID 列表（Windows netstat；其它平台返回 []）"""
    pids = []
    try:
        # 不能用 text=True：中文 Windows 的 netstat 输出是 GBK，UTF-8 解码会抛异常
        raw = subprocess.run(["netstat", "-ano"], capture_output=True,
                             timeout=15).stdout or b""
        out = raw.decode("mbcs" if os.name == "nt" else "utf-8", errors="replace")
    except Exception:
        return pids
    for line in out.splitlines():
        if "LISTENING" not in line.upper():
            continue
        parts = line.split()
        if len(parts) >= 5 and parts[1].endswith(":%d" % int(port)):
            try:
                pid = int(parts[-1])
            except ValueError:
                continue
            if pid and pid not in pids:
                pids.append(pid)
    return pids


def _pids_on_fallback(port):
    """netstat 不可用时的兜底（psutil 若存在则用）"""
    try:
        import psutil  # noqa
    except Exception:
        return []
    pids = []
    try:
        for c in psutil.net_connections(kind="inet"):
            if (c.laddr and c.laddr.port == int(port)
                    and c.status == psutil.CONN_LISTEN and c.pid):
                if c.pid not in pids:
                    pids.append(c.pid)
    except Exception:
        pass
    return pids


def _ps_text(script, timeout=25):
    """跑一段 PowerShell 并返回 stdout 文本（中文 Windows 下按 mbcs 解码，避免 UnicodeDecodeError）"""
    if os.name != "nt":
        return ""
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                           capture_output=True, timeout=timeout)
        return (r.stdout or b"").decode("mbcs", errors="replace").strip()
    except Exception:
        return ""


def _parent_of(pid):
    """返回 (父进程 pid, 父进程名)；取不到返回 (None, None)"""
    out = _ps_text(
        "$p=Get-CimInstance Win32_Process -Filter 'ProcessId = %d' -ErrorAction SilentlyContinue;"
        "if($p){ '%d|' + [string]$p.ParentProcessId } else { '%d|' }" % (int(pid), int(pid), int(pid)))
    if not out:
        return None, None
    line = out.splitlines()[-1].strip()
    parts = line.split("|")
    if len(parts) < 2 or not parts[1].strip():
        return None, None
    ppid = int(parts[1]) if parts[1].strip().lstrip("-").isdigit() else None
    if not ppid or ppid <= 0:
        return None, None
    name = _ps_text("(Get-CimInstance Win32_Process -Filter 'ProcessId = %d' "
                    "-ErrorAction SilentlyContinue).Name" % ppid).splitlines()
    return ppid, (name[-1].strip() if name else "") or None


# 这些父进程名意味着"面板还挂在本会话/本机应用下面"，会话不结束它就不算完
_BOUND_HINTS = ("sandbox-cli.exe", "sandbox-center.exe", "codebuddy", "codebuddy.exe",
                "bash.exe", "sh.exe", "cmd.exe", "node.exe", "WorkBuddy.exe")
# 这些父进程名代表"会话之外"，可跨会话常驻
_FREE_HINTS = ("explorer.exe", "WmiPrvSE.exe", "services.exe", "svchost.exe", "wininit.exe")


def _verdict(ppid, pname, self_pid):
    if ppid is None or not pname:
        return "free", "父进程已退出（典型的 WMI/脱离式创建）"
    low = pname.lower()
    if ppid == self_pid:
        return "bound", "父进程就是本次启动命令本身 → 未脱离，会随命令结束被回收"
    if low in [n.lower() for n in _FREE_HINTS]:
        return "free", "父进程 %s：会话之外，可跨会话常驻" % pname
    if low in [n.lower() for n in _BOUND_HINTS]:
        return "bound", "父进程 %s：仍挂在应用/会话进程树下" % pname
    return "unknown", "父进程 %s：无法判定，建议改用 start_panel.bat 由资源管理器启动" % pname


def _save_state(port, d):
    """按端口记账：谁、用什么方式、带哪些参数起的面板（多端口互不覆盖）"""
    try:
        os.makedirs(ROOT, exist_ok=True)
        all_ = _load_state()
        all_[str(int(port))] = d
        tmp = STATE_FILE + ".%d.tmp" % os.getpid()
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(all_, f, ensure_ascii=False, indent=1)
        os.replace(tmp, STATE_FILE)
    except Exception:
        pass


def _load_state(port=None):
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            d = json.load(f) or {}
    except Exception:
        return {}
    if port is None:
        return d
    v = d.get(str(int(port))) or {}
    return v if isinstance(v, dict) else {}


def _pythonw():
    """找一个不弹黑窗的解释器：优先与当前解释器同目录的 pythonw.exe"""
    exe = sys.executable or ""
    cand = os.path.join(os.path.dirname(exe), "pythonw.exe")
    if os.path.exists(cand):
        return cand
    up = os.environ.get("USERPROFILE", "")
    cand = os.path.join(up, ".workbuddy", "binaries", "python", "versions",
                        "3.13.12", "pythonw.exe")
    if os.path.exists(cand):
        return cand
    return "pythonw.exe"


def _wmi_create(cmdline):
    """用 WMI Win32_Process.Create 在会话之外创建进程（父进程=WMI 服务，不会被 session job 回收）。

    实现要点：把 PowerShell 脚本落到临时 .ps1 再 -File 执行。
    直接 `-Command` 传字符串会因为 Windows 命令行里嵌双引号导致 PowerShell 解析失败
    （实测：返回码非 0，静默退化成 Popen，进程随会话被回收）。
    """
    if os.name != "nt":
        return False
    import tempfile
    ps = ("$ErrorActionPreference='Stop'\r\n"
          "$r=([wmiclass]'Win32_Process').Create('%s')\r\n"
          "if($r.ReturnValue -ne 0){ exit 1 }\r\n"
          % cmdline.replace("'", "''"))
    fd, path = tempfile.mkstemp(suffix=".ps1")
    try:
        with os.fdopen(fd, "w", encoding="utf-8-sig") as f:
            f.write(ps)
        r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive",
                            "-ExecutionPolicy", "Bypass", "-File", path],
                           capture_output=True, timeout=40)
        return r.returncode == 0
    except Exception:
        return False
    finally:
        try:
            os.remove(path)
        except Exception:
            pass


def _spawn_detached(port, extra=()):
    """在会话之外创建面板进程；返回 (方式, 是否成功发起)

    ① WMI Win32_Process.Create —— 父进程是 WMI 服务，脱离会话 job 对象，跨会话常驻（首选）；
    ② 兜底：直接 Popen（分离标志）—— 非 Windows / WMI 不可用时可用，
       但实测这种进程会在当前命令结束后被回收，只能应付"先看着"。
    """
    pyw, script = _pythonw(), PANEL
    tail = "".join(" %s" % a for a in extra)
    # 用 cmd /c 包一层做输出重定向：既给子进程一个有效的 stdout 句柄（否则 pythonw 下
    # sys.stdout 为 None，裸 print 会直接崩），又把 traceback 落到 SPAWN_LOG 供事后查。
    # 注意 cmd 的引号规矩：整条命令还要再套一层双引号。
    try:
        os.makedirs(ROOT, exist_ok=True)
        open(SPAWN_LOG, "w", encoding="utf-8").close()      # 每次启动截断，只看这一次
    except Exception:
        pass
    wrapped = ('cmd /c ""%s" "%s" --port %d%s >> "%s" 2>&1"'
               % (pyw, script, int(port), tail, SPAWN_LOG))
    if _wmi_create(wrapped):
        return "wmi", True
    flags = 0
    if os.name == "nt":
        flags = 0x00000008 | 0x00000200          # DETACHED_PROCESS | NEW_PROCESS_GROUP
    try:
        subprocess.Popen([pyw, script, "--port", str(int(port))] + list(extra),
                         creationflags=flags, close_fds=True,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         stdin=subprocess.DEVNULL)
        return "popen", True
    except Exception:
        return "popen", False


def _wait_up(port, timeout=12.0):
    t0 = time.time()
    while time.time() - t0 < timeout:
        if _probe(port, timeout=1.0):
            return True
        time.sleep(0.4)
    return False


def _wait_down(port, timeout=10.0):
    t0 = time.time()
    while time.time() - t0 < timeout:
        if not _pids_on(port) and not _probe(port, timeout=0.6):
            return True
        time.sleep(0.3)
    return False


def _open_browser(port):
    try:
        if os.name == "nt":
            os.startfile("http://127.0.0.1:%d/" % int(port))   # noqa: S606
        else:
            subprocess.Popen(["xdg-open", "http://127.0.0.1:%d/" % int(port)])
        return True
    except Exception:
        return False


# ---------------------------------------------------------------- 四个动作
def _detach_info(port):
    """面板进程是否真的在会话之外（决定性判据：父进程 ≠ 启动它的本进程）"""
    pids = _pids_on(port) or _pids_on_fallback(port)
    if not pids:
        return {"detached": None, "verdict": "unknown", "pid": None, "ppid": None,
                "parent": None, "why": "未取到监听 PID"}
    pid = pids[0]
    ppid, pname = _parent_of(pid)
    verdict, why = _verdict(ppid, pname, os.getpid())
    return {"detached": verdict != "bound", "verdict": verdict, "pid": pid,
            "ppid": ppid, "parent": pname, "why": why}


def act_status(port, as_json=False):
    running = _probe(port)
    pids = _pids_on(port) or _pids_on_fallback(port)
    info = {"running": bool(running), "port": int(port), "pids": pids,
            "url": "http://127.0.0.1:%d/" % int(port)}
    others = []
    if not running:
        for p in PORT_SCAN:
            if p != port and _probe(p, timeout=0.5):
                others.append("http://127.0.0.1:%d/" % p)
    info["other_panels"] = others
    if running:
        info.update(_detach_info(port))
        st = _load_state(port)
        if st.get("how"):
            info["started_how"] = st.get("how")
        info["running_build"] = _running_build(port)
        info["code_build"] = _code_build()
        info["stale"] = bool(info["code_build"] and info["running_build"]
                             and info["running_build"] != info["code_build"])
    if as_json:
        print(json.dumps(info, ensure_ascii=False))
    else:
        if running:
            print("面板在运行：%s  PID=%s" % (info["url"], ",".join(map(str, pids)) or "?"))
            print("  会话关系：%s（%s）"
                  % ("会话外 ✅ 不会拖住会话" if info.get("detached") else "可能挂在本会话 ⚠️",
                     info.get("why") or "未知"))
            print("  版本：运行中 build=%s / 磁盘代码 build=%s（%s）"
                  % (info.get("running_build") or "?", info.get("code_build") or "?",
                     "一致 ✅" if not info.get("stale") else "不一致 ⚠️ 旧版在跑"))
            if info.get("stale"):
                print(_stale_warning(port, info.get("running_build"), info.get("code_build")))
        else:
            print("面板未运行（端口 %d 无监听）" % port)
            if others:
                print("检测到其它端口上的面板：%s" % "、".join(others))
    return 0 if running else 1


def act_check(port, as_json=False):
    """只回答一个问题：这个面板会不会拖住当前会话？（含进程链与启动方式）"""
    running = _probe(port)
    if not running:
        out = {"running": False, "port": int(port), "blocking_session": False,
               "why": "面板未运行，不存在占用会话的问题"}
    else:
        di = _detach_info(port)
        st = _load_state(port)
        out = {"running": True, "port": int(port), "url": "http://127.0.0.1:%d/" % int(port),
               "pid": di.get("pid"), "parent_pid": di.get("ppid"),
               "parent_name": di.get("parent"),
               "detached": di.get("detached"),
               "blocking_session": di.get("detached") is False,
               "why": di.get("why"),
               "started_how": st.get("how"), "started_at": st.get("at"),
               "panel_args": st.get("extra") or []}
    if as_json:
        print(json.dumps(out, ensure_ascii=False))
    else:
        if not out["running"]:
            print("✅ 面板未运行 —— 不存在「占用会话」的问题")
        elif out["blocking_session"]:
            print("⚠️ 面板可能拖住会话：%s" % out["why"])
            print("   处理：panel_ctl.py stop 后改用 start_panel.bat / panel_ctl.py start 重启")
        else:
            print("✅ 面板在会话之外（%s），不会让会话停在未完成状态" % out["why"])
            if out.get("panel_args"):
                print("   启动参数：%s" % " ".join(out["panel_args"]))
    return 0


def act_start(port, no_browser=False, as_json=False, quiet=False,
              idle_exit=0.0, ttl=0.0, auto=False):
    if _probe(port):
        line = "面板已在运行（复用，未重复启动）：http://127.0.0.1:%d/" % int(port)
        print(json.dumps({"status": "already-running", "port": int(port),
                          "url": "http://127.0.0.1:%d/" % int(port)},
                         ensure_ascii=False) if as_json else line)
        return 0
    if not os.path.exists(PANEL):
        print("找不到面板脚本：%s" % PANEL)
        return 2
    extra = []
    # 自动化（hook）拉起的实例默认带空闲自退：会话结束后不留僵尸面板
    eff_idle = float(idle_exit) if idle_exit else (AUTO_IDLE_EXIT if auto else 0.0)
    if eff_idle:
        extra += ["--idle-exit", str(eff_idle)]
    if ttl:
        extra += ["--ttl", str(float(ttl))]
    how, ok = _spawn_detached(port, extra)
    if not ok:
        print("启动失败：无法创建面板进程（WMI 与 Popen 均失败）")
        return 2
    up = _wait_up(port)
    di = _detach_info(port) if up else {}
    _save_state(port, {"how": how, "pid": di.get("pid"), "parent_pid": di.get("ppid"),
                       "parent_name": di.get("parent"), "detached": di.get("detached"),
                       "port": int(port), "extra": extra, "auto": bool(auto),
                       "at": time.strftime("%Y-%m-%d %H:%M:%S")})
    if not as_json:
        print("面板启动方式：%s（%s）" % (how, "会话外常驻" if how == "wmi" else "会话内，会话结束可能被回收"))
        if up and di:
            print("  会话关系：%s" % (di.get("why") or "未知"))
    if up:
        if not no_browser and not quiet:
            _open_browser(port)
        pids = _pids_on(port)
        if as_json:
            print(json.dumps({"status": "started", "how": how, "port": int(port),
                              "pids": pids, "url": "http://127.0.0.1:%d/" % int(port),
                              "detached": di.get("detached"), "detach_why": di.get("why"),
                              "panel_args": extra},
                             ensure_ascii=False))
        else:
            print("面板已就绪：http://127.0.0.1:%d/  PID=%s" % (int(port),
                                                            ",".join(map(str, pids)) or "?"))
        return 0
    print("已发起启动，但 %d 秒内端口未就绪，请稍后再试 status" % 12)
    return 1


def act_spawn(port, as_json=False, idle_exit=0.0, ttl=0.0, auto=False, wait=False):
    """**只发起面板进程、不等待就绪**——供 hook 在严格时限内兜底拉起专用。

    ⚠️ 为什么不复用 `start`：start 里是 `_wait_up(12s)` + `_detach_info`（PowerShell）
     + `_code_build(20s)` + 状态查询，最坏 40s 以上；而 SessionStart（10s）与
     UserPromptSubmit（15s）两个 hook 的时限都小于它 —— 兜底反而必然被「Hook timed out」
     拦掉，面板一次也起不来（v1.0.40 实测复现：UserPromptSubmit 报 timeout 15000ms）。

    逐项耗时实测（本机）：`_wmi_create` 11.9s、`_detach_info` 22.3s（两次 PowerShell
    各 10s+）、`_code_build` 5.2s —— 光 PowerShell 就吃掉 40s。所以 spawn 里
    **默认不查父进程**（`wait=False`），只做「幂等探测 → 会话外建进程 → 记 state」，
    返回是 0.1s 级；`--wait` 才额外查一次（手动排查时用）。

    注意：spawn 后**不保证**端口已就绪（WMI 建进程那一下就要 12s），
    调用方要么异步不等（hook 快路径），要么自己再等几秒（`_wait_up`）。
    """
    if _probe(port, timeout=0.8):
        info = {"status": "already-running", "port": int(port), "url": "http://127.0.0.1:%d/" % int(port)}
        print(json.dumps(info, ensure_ascii=False) if as_json
              else "面板已在运行（复用，未重复启动）：%s" % info["url"])
        return 0
    if not os.path.exists(PANEL):
        print("找不到面板脚本：%s" % PANEL)
        return 2
    extra = []
    eff_idle = float(idle_exit) if idle_exit else (AUTO_IDLE_EXIT if auto else 0.0)
    if eff_idle:
        extra += ["--idle-exit", str(eff_idle)]
    if ttl:
        extra += ["--ttl", str(float(ttl))]
    how, ok = _spawn_detached(port, extra)
    info = {"status": "spawned" if ok else "spawn-failed", "how": how,
            "port": int(port), "panel_args": extra,
            "url": "http://127.0.0.1:%d/" % int(port)}
    if ok:
        # ⚠️ 默认不查父进程：_detach_info 在实测里要 22.3s（两次 PowerShell 各 10s+），
        #    挂在 hook 同步路径上就是必然超时。detached 用 `status` 命令事后查。
        di = _detach_info(port) if wait else {}
        info.update({"pid": di.get("pid") if wait else None,
                     "detached": di.get("detached") if wait else None,
                     "detach_why": di.get("why") if wait else None,
                     "how_detail": how})
        _save_state(port, {"how": how, "pid": di.get("pid"), "parent_pid": di.get("ppid"),
                           "parent_name": di.get("parent"), "detached": di.get("detached"),
                           "port": int(port), "extra": extra, "auto": bool(auto),
                           "at": time.strftime("%Y-%m-%d %H:%M:%S")})
    if as_json:
        print(json.dumps(info, ensure_ascii=False))
    else:
        if ok:
            print("已发起面板启动：%s（%s）→ %s"
                  % (how, "会话外常驻" if info.get("detached") else "会话内，可能被回收", info["url"]))
        else:
            print("发起面板启动失败：WMI 与 Popen 均不可用，请手动 python %s start" % os.path.abspath(__file__))
    return 0 if ok else 2


def act_stop(port, as_json=False):
    pids = _pids_on(port) or _pids_on_fallback(port)
    if not pids:
        if _probe(port):
            print("端口 %d 有响应但未取到 PID，未做处置" % port)
            return 1
        print(json.dumps({"status": "not-running", "port": int(port)},
                         ensure_ascii=False) if as_json
              else "面板本来就没在运行（端口 %d 无监听）" % port)
        return 0
    killed = []
    for pid in pids:
        try:
            r = subprocess.run(["taskkill", "/PID", str(pid), "/F"],
                               capture_output=True, timeout=15)
            if r.returncode == 0:
                killed.append(pid)
        except Exception:
            pass
    down = _wait_down(port)
    if as_json:
        print(json.dumps({"status": "stopped" if down else "stop-failed",
                          "port": int(port), "killed": killed}, ensure_ascii=False))
    else:
        print("面板已停止：端口 %d，结束进程 %s" % (port, ",".join(map(str, killed)) or "无")
              if down else "停止失败：端口 %d 仍被占用（PID %s）" % (port, pids))
    return 0 if down else 1


def act_restart(port, no_browser=False, as_json=False, idle_exit=0.0, ttl=0.0, auto=False):
    act_stop(port, as_json=False)
    time.sleep(0.5)
    return act_start(port, no_browser=no_browser, as_json=as_json,
                     idle_exit=idle_exit, ttl=ttl, auto=auto)


def _upgrade_hint(port, pids):
    """无法自动升级时的处置指引（端口被别的程序占着 / 没权限杀进程）。"""
    return ("  ⚠️ 无法自动升级（旧进程占着端口 %d，PID=%s）。二选一：\n"
            "   ① 结束占用进程后重启：netstat -ano | findstr :%d  →  "
            "taskkill /F /PID <pid>  →  python %s start\n"
            "   ② 换个端口启动：python %s start --port %d（或设环境变量 LIVE_PROGRESS_PORT=%d）\n"
            "   ③ 面板无状态（任务都在 jobs.json 里），停掉再启不会丢任何进度记录。"
            % (int(port), ",".join(map(str, pids)) or "?", int(port),
               os.path.abspath(__file__), os.path.abspath(__file__),
               int(port) + 1, int(port) + 1))


def act_upgrade(port, no_browser=False, as_json=False, idle_exit=0.0, ttl=0.0, auto=False):
    """把面板升到磁盘上的最新代码：只在「运行中的是旧版」时才动手。

    - 没在跑 → 直接启动；
    - 跑的是新版（build 一致）→ 什么都不做；
    - 跑的是旧版 → stop → start，并复核 build；stop 不掉就给出手动处置指引。
    """
    code = _code_build()
    alive = _probe(port)
    if not alive:
        if not as_json:
            print("面板未运行 —— 直接启动最新版（build=%s）" % (code or "?"))
        return act_start(port, no_browser=no_browser, as_json=as_json,
                         idle_exit=idle_exit, ttl=ttl, auto=auto)
    run = _running_build(port)
    if code and run and run == code:
        if as_json:
            print(json.dumps({"status": "up-to-date", "build": code, "port": int(port)},
                             ensure_ascii=False))
        else:
            print("已是最新版（build=%s），无需升级" % code)
        return 0
    if not as_json:
        print("旧版运行中（运行中 build=%s → 磁盘代码 build=%s）：停止旧进程并启动新版…"
              % (run or "?", code or "?"))
    rc = act_stop(port, as_json=False)
    if rc != 0:
        if as_json:
            print(json.dumps({"status": "upgrade-blocked", "port": int(port),
                              "running_build": run, "code_build": code,
                              "pids": _pids_on(port) or _pids_on_fallback(port),
                              "hint": "端口被占用，需手动结束进程或换端口"},
                             ensure_ascii=False))
        else:
            print(_upgrade_hint(port, _pids_on(port) or _pids_on_fallback(port)))
        return 1
    time.sleep(0.5)
    rc = act_start(port, no_browser=no_browser, as_json=as_json, quiet=True,
                   idle_exit=idle_exit, ttl=ttl, auto=auto)
    new = _running_build(port, timeout=3.0)
    ok = bool(new and code and new == code)
    if as_json:
        print(json.dumps({"status": "upgraded" if ok else "upgrade-verify-failed",
                          "port": int(port), "from": run, "to": new, "code_build": code},
                         ensure_ascii=False))
    else:
        print("升级完成 ✅ 运行中 build=%s（代码 build=%s）" % (new, code) if ok
              else "启动后 build 复核不一致（运行中=%s / 代码=%s），请再跑一次 status 确认"
                   % (new, code))
    return 0 if ok else 1


def main():
    ap = argparse.ArgumentParser(description="任务执行进度·实时面板开关控制器")
    ap.add_argument("action",
                    choices=["status", "start", "spawn", "stop", "restart", "check", "upgrade"],
                    help="spawn = 只发起不等待就绪（秒回，hook 兜底专用）；"
                         "check = 只回答「面板会不会拖住当前会话」；"
                         "upgrade = 只在跑着旧版时才重启升级")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--no-browser", action="store_true", help="启动时不自动打开浏览器")
    ap.add_argument("--json", action="store_true", help="机器可读输出")
    ap.add_argument("--idle-exit", type=float, default=0.0,
                    help="面板空闲多少秒自动退出（无请求且无运行中任务；0=常驻）")
    ap.add_argument("--ttl", type=float, default=0.0, help="面板最大存活秒数（0=不限）")
    ap.add_argument("--auto", action="store_true",
                    help="自动化启动（hook）：默认带 --idle-exit %d，避免留下僵尸面板"
                         % int(AUTO_IDLE_EXIT))
    ap.add_argument("--wait", action="store_true",
                    help="spawn 后额外查一次父进程（多花 ~22s，仅手动排查用；hook 路径别加）")
    args = ap.parse_args()

    if args.action == "status":
        return act_status(args.port, args.json)
    if args.action == "check":
        return act_check(args.port, args.json)
    if args.action == "start":
        return act_start(args.port, args.no_browser, args.json,
                         idle_exit=args.idle_exit, ttl=args.ttl, auto=args.auto)
    if args.action == "spawn":
        return act_spawn(args.port, args.json, idle_exit=args.idle_exit,
                         ttl=args.ttl, auto=args.auto)
    if args.action == "stop":
        return act_stop(args.port, args.json)
    if args.action == "upgrade":
        return act_upgrade(args.port, args.no_browser, args.json,
                           idle_exit=args.idle_exit, ttl=args.ttl, auto=args.auto)
    return act_restart(args.port, args.no_browser, args.json,
                       idle_exit=args.idle_exit, ttl=args.ttl, auto=args.auto)


if __name__ == "__main__":
    sys.exit(main() or 0)
