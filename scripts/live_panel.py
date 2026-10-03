# -*- coding: utf-8 -*-
"""通用任务执行进度·实时面板（127.0.0.1:8791）

只读 ~/.workbuddy/live-progress/jobs.json（由 progress.py / hook_bg.py 写入），
把「当前所有任务」渲染成多卡片实时面板：时间线 / 步骤进度条 / 进度环 / 指标格 / 推进曲线 / 日志尾部。

启动:
    python scripts/live_panel.py [--port 8791]            # 前台
    （推荐由代理以后台任务方式启动，可跨轮次存活）

设计要点:
- 只读注册表，绝不写业务数据；采样与速率由本进程自行维护（samples.json）。
- 支持两类任务：有总量（进度环+百分比+ETA）与无总量（扫描条+已运行时长+日志）。
- 布局沿用「容器内不溢出」规则：曲线柱高用百分比、柱数按容器宽下采样、容器 overflow:hidden；
  指标格只允许 4+0 / 2+2 / 1×4 三种排布。
"""
import os
import sys
import json
import time
import hashlib
import argparse
import subprocess
import threading
import datetime
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import progress as P  # noqa: E402

SKILL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_SKILL_NAME = "雪人老师·[WorkBuddy]任务执行进度·实时面板"


def _skill_meta():
    """从 SKILL.md frontmatter 读「中文名 + 版本号」——标题里的版本永远跟着 SKILL.md 走。

    读不到就退回默认名 + 未知版本，绝不让面板因为读元数据而启不来。
    """
    name, ver = DEFAULT_SKILL_NAME, "?"
    try:
        with open(os.path.join(SKILL_DIR, "SKILL.md"), encoding="utf-8") as f:
            txt = f.read()
        # 只扫 frontmatter（首个 --- 与下一个 --- 之间）：正文里也有 name/version 字样会误伤
        fm = txt.split("\n---", 1)[0] if txt.startswith("---") else txt[:4000]
        for line in fm.splitlines():
            s = line.strip()
            if s.startswith("name:") and len(s) > 5:
                name = s.split(":", 1)[1].strip()
            elif s.startswith("version:") and len(s) > 8:
                ver = s.split(":", 1)[1].strip()
    except Exception:
        pass
    return name, ver


SKILL_NAME_ZH, SKILL_VERSION = _skill_meta()

# 页面标题去品牌前缀：「雪人老师·」只在 SKILL.md 的 name/displayName 出现（按用户要求不动），
# 面板窗口标题与页眉只展示主体名 → 页面实际显示「📊 [WorkBuddy]任务执行进度·实时面板 Ver:…”」。
# SKILL_NAME_ZH 仍保留 SKILL.md 原口径，需要全名时用它。
BRAND_PREFIXES = ("雪人老师·",)
def _panel_name(name):
    for p in BRAND_PREFIXES:
        if name.startswith(p):
            return name[len(p):].strip()
    return name

PANEL_NAME = _panel_name(SKILL_NAME_ZH)                                       # 页面展示名（去品牌）
PANEL_TITLE = "📊 %s Ver:%s" % (PANEL_NAME, SKILL_VERSION)   # <title> / h1 文案
H1_TEXT = "%s Ver:%s" % (PANEL_NAME, SKILL_VERSION)          # 图标由 .hicon span 出
# v1.0.38 页面图标：内联 SVG data URI（不引入任何外部文件，面板保持纯自包含）
# ⚠️ v1.0.38 再改：初版画的是「三根柱 + 绿底」，和 h1 前的 📊 柱状图 emoji 撞脸、分不清，
# 换成**深色底 + 亮绿对勾 ✓**：一眼可辨，且对勾 = 任务跑完/已收尾，语义也贴面板。
PANEL_FAVICON = ("data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'>"
                 "<rect width='32' height='32' rx='7' fill='%230f1720'/>"
                 "<path d='M9 16.8 L14 21.8 L23.5 11' fill='none' stroke='%233fb27f' "
                 "stroke-width='3.6' stroke-linecap='round' stroke-linejoin='round'/></svg>")


SAMPLES_FILE = os.path.join(P.ROOT, "samples.json")
WINDOW = 1800            # 速率统计窗口（秒）
SAMPLES = {}             # job_id -> list[[ts, done]]
JOB_START = {}           # job_id -> started_at（用于识别"同一 id 的新一轮运行"）
LOCK = threading.Lock()


def load_samples():
    try:
        with open(SAMPLES_FILE, encoding="utf-8") as f:
            raw = json.load(f)
        now = time.time()
        for jid, arr in (raw or {}).items():
            keep = [[float(t), int(d)] for t, d in arr if now - float(t) <= WINDOW]
            if keep:
                SAMPLES[jid] = keep
    except Exception:
        pass


def save_samples():
    try:
        with LOCK:
            data = {k: [[round(t, 1), d] for t, d in v][-300:] for k, v in SAMPLES.items() if v}
        os.makedirs(P.ROOT, exist_ok=True)
        tmp = SAMPLES_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f)
        os.replace(tmp, SAMPLES_FILE)
    except Exception:
        pass


def _code_fingerprint():
    """自身代码指纹（live_panel.py + progress.py 的内容哈希）——变了就该热更新。"""
    try:
        h = hashlib.md5()
        for p in (os.path.abspath(__file__),
                  os.path.join(os.path.dirname(os.path.abspath(__file__)), "progress.py")):
            with open(p, "rb") as f:
                h.update(f.read())
        return h.hexdigest()
    except Exception:
        return None


CODE_FP = [_code_fingerprint()]     # 启动时记一份，用于比对
SRV = [None]                        # 服务器对象（热更新前要先关监听套接字）
LAST_FP_CHECK = [0.0]


def hot_reload_if_changed(force_check=False):
    """**热更新**：检测到自身代码变了就原地重启进程（os.execv），无需手动 restart。

    关键点：exec 之前必须先 `server_close()` 关掉监听套接字——否则新进程继承这个 fd，
    会撞上「端口已被占用」而自动跳过，面板反而掉线。
    """
    now = time.time()
    if not force_check and now - LAST_FP_CHECK[0] < 3:
        return False
    LAST_FP_CHECK[0] = now
    try:
        fp = _code_fingerprint()
    except Exception:
        return False
    if not fp or fp == CODE_FP[0]:
        return False
    print("[hot-reload] 检测到代码变更 → 自动重启面板进程加载新代码", flush=True)
    try:
        helper = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                              "_panel_relaunch.py")
        if not os.path.exists(helper):
            raise RuntimeError("缺少接替者脚本 _panel_relaunch.py")
        kw = {"creationflags": 0x00000008 | 0x00000200} if os.name == "nt" else {}
        # ① 先拉起接替者（独立进程，等端口腾出后用**同样参数**重新拉起面板）
        subprocess.Popen([sys.executable, helper] + sys.argv[1:],
                         cwd=os.path.dirname(os.path.abspath(__file__)),
                         close_fds=True, **kw)
        # ② 再关监听、立刻退出——顺序不能反，否则中间会有一段时间没人服务
        s = SRV[0]
        if s is not None:
            try:
                s.server_close()
            except Exception:
                pass
        os._exit(0)
    except Exception as e:
        print("[hot-reload] 自动重启失败：%s —— 请手动执行 panel_ctl.py restart" % e,
              flush=True)
    return True


def sample_loop():
    n = 0
    while True:
        try:
            hot_reload_if_changed()
            # WB 权威状态同步：sessions.status 一变（working→completed 等），
            # 会话卡在 ≤2s 内自动收尾（内部 1s 限流；失败静默，交给 gc 兜底）
            try:
                P.sync_wb_sessions()
            except Exception:
                pass
            now = time.time()
            with LOCK:
                for j in P.list_jobs():
                    jid = j["id"]
                    if j.get("status") != "running":
                        continue
                    d = int(j.get("done") or 0)
                    # 丢弃会造成速率失真的旧采样：①同一 id 换了 started_at（新一轮运行）；
                    # ②进度回退；③进度**跃升**（中途接管/桥接补报，如 0→2900）→ 视为取样基线重建。
                    prev = SAMPLES.get(jid)
                    total = j.get("total") or 0
                    jumped = bool(prev and total and (d - prev[-1][1]) > max(50, 0.2 * total))
                    if JOB_START.get(jid) != j.get("started_at") or (prev and d < prev[-1][1]) or jumped:
                        SAMPLES[jid] = []
                        JOB_START[jid] = j.get("started_at")
                    arr = SAMPLES.setdefault(jid, [])
                    arr.append([now, d])
                    del arr[:-400]
                # 清理消失任务的采样
                alive = {j["id"] for j in P.list_jobs()}
                for k in list(SAMPLES):
                    if k not in alive:
                        del SAMPLES[k]
        except Exception:
            pass
        n += 1
        if n % 60 == 0:          # 采样点密集了（1s 一拍），仍保持约 1 分钟落盘一次
            save_samples()
        time.sleep(1)            # v1.0.36：5s→1s，会话状态反馈不拖沓


def _fmt_dur(sec):
    if sec is None:
        return "-"
    sec = int(sec)
    if sec < 60:
        return "%ds" % sec
    if sec < 3600:
        return "%dm%02ds" % (sec // 60, sec % 60)
    return "%dh%02dm" % (sec // 3600, (sec % 3600) // 60)


def _tail_file(path, n=10):
    if not path or not os.path.exists(path):
        return []
    try:
        if os.path.getsize(path) > 2 * 1024 * 1024:
            with open(path, "rb") as f:
                f.seek(-200000, os.SEEK_END)
                txt = f.read().decode("utf-8", "ignore")
        else:
            txt = open(path, encoding="utf-8", errors="ignore").read()
        return [ln for ln in txt.splitlines() if ln.strip()][-n:]
    except Exception:
        return []


def _count_file_lines(path, max_lines=200000, max_bytes=8 * 1024 * 1024):
    """统计日志文件的**真实**非空行数，供指标格「日志 N 行」使用。

    只读、不修改；双上限（行数 / 字节）防止超大日志拖慢 2 秒一次的面板轮询。
    注意：不要把「日志 N 行」写成 len(log_tail) —— log_tail 恒为最后 10 行，
    那样每张卡都会显示成 10 行（数值失真）。
    """
    if not path or not os.path.exists(path):
        return 0
    try:
        n, read = 0, 0
        with open(path, encoding="utf-8", errors="ignore") as f:
            for ln in f:
                read += len(ln.encode("utf-8", "ignore"))
                if ln.strip():
                    n += 1
                if n >= max_lines or read >= max_bytes:
                    break
        return n
    except Exception:
        return 0


def job_payload(j, now):
    jid = j["id"]
    st = j.get("status", "running")
    started = j.get("started_at") or now
    updated = j.get("updated_at") or now
    elapsed = (j.get("ended_at") or now) - started if st != "running" else now - started
    if st == "running" and j.get("ended_at"):
        elapsed = j["ended_at"] - started
    total = j.get("total")
    done = int(j.get("done") or 0)
    pct = round(done / total * 100, 2) if total else None

    with LOCK:
        arr = [list(x) for x in SAMPLES.get(jid, [])]
    stale = st == "running" and (now - updated) > P.STALE_AFTER
    running_like = (st == "running") and not stale   # 已结束/疑似结束：速率与 ETA 无意义，一律不算
    rate = None
    rate_avg = False
    if running_like and total and len(arr) >= 2:
        t0, d0 = arr[0]
        span = now - t0
        if span >= 20 and done > d0:
            r = round((done - d0) / span, 3)
            rate = r if r > 0 else None   # 取整为 0 时视为未采样，避免与 ETA 口径不一致
    # ---- 整体平均兜底（2026-10-01，用户报「速率/预计完成一直采样中」）----
    # CLI 步进式任务（代理手动 set --done）两次更新之间 done 长期不变，
    # 采样窗口内算不出速率 → 速率与 ETA 永远「采样中」。
    # 兜底：窗口速率缺失时，用 started_at 以来的整体平均速率（done ÷ 已运行），
    # 对「大部分进度在面板启动前已完成」的任务也能给出可用的均值速率与 ETA。
    if running_like and total and not rate and 0 < done < total \
            and started and (now - started) >= 20:
        r_avg = round(done / (now - started), 3)
        if r_avg > 0:
            rate = r_avg
            rate_avg = True
    eta = (total - done) / rate if (running_like and rate and rate > 0 and total and done < total) else None
    eta_clock = (datetime.datetime.now() + datetime.timedelta(seconds=eta)).strftime("%H:%M:%S") if eta else None

    # ---- 时间兜底（planned_sec）---------------------------------------------
    # 有些长任务**中途拿不到任何可数的进度**（典型：整段提交给远端排队，全程只
    # 有一个"完成/没完成"，done 永远是 0）。这类任务采样算不出速率，也就没有
    # ETA。脚本若用 planned_sec 声明了预估总耗时，这里退化为时间基线：
    #   时间进度 = 已运行 / 预估（封顶 99%）  预计完成 = 开始时刻 + 预估
    planned = j.get("planned_sec") or None
    time_pct = None
    eta_overdue = None
    if running_like and planned and planned > 0:
        elapsed_sec = max(0.0, now - started)
        time_pct = round(min(99.0, elapsed_sec / planned * 100), 2)
        eta_overdue = int(max(0.0, elapsed_sec - planned))
        if not eta_clock:                     # 采样算不出 ETA 时才用预估兜底
            eta_clock = datetime.datetime.fromtimestamp(
                started + planned).strftime("%H:%M:%S")

    label = {"running": "运行中", "done": "已完成", "failed": "失败",
             "aborted": "异常中断"}.get(st, st)
    if stale:
        label = "疑似已结束"

    # 日志尾部（只取最后 10 行用于展示）与日志总行数（用于指标格，必须分开算）
    _log_file = j.get("log_file")
    _log_arr = j.get("log") or []
    _tail_from_file = _tail_file(_log_file)
    if _tail_from_file:
        log_tail = _tail_from_file
        log_count = _count_file_lines(_log_file) or len(_tail_from_file)
    else:
        log_tail = _log_arr[-10:]
        log_count = len(_log_arr)
    # 无总量且有时间基线 → 用**真实时间戳**折算进度曲线（否则 done 恒定，曲线是一条死线）
    if (not total) and running_like and planned and len(arr) >= 2:
        arr = [[t, round(min(99.0, max(0.0, (t - started) / planned * 100)))] for t, _ in arr]
    return {
        "id": jid,
        "title": j.get("title") or jid,
        "status": st,
        "label": label,
        "stale": stale,
        "done": done,
        "total": total,
        "pct": pct,
        "unit": j.get("unit") or "项",
        "determinate": bool(total),
        "message": j.get("message") or "",
        "exec": _fmt_dur(elapsed),
        "ago": int(max(0, now - updated)),
        "rate": round(rate, 3) if rate else None,
        "rate_avg": rate_avg,
        "sec_per_item": round(1 / rate, 1) if rate else None,
        "eta_clock": eta_clock,
        "planned_sec": int(planned) if planned else None,
        "time_pct": time_pct,
        "eta_overdue": eta_overdue,
        "source": j.get("source") or "script",
        "cmd": (j.get("cmd") or "")[:160],
        "cwd": (j.get("cwd") or "").replace("\\", "/").split("/")[-1],
        "session": (j.get("session_id") or "")[:6],
        "log_tail": log_tail[-10:],
        "log_count": log_count,      # 真实日志行数（不是 log_tail 的 10 行切片）
        "samples": [{"t": int(t), "d": d} for t, d in arr[-120:]],
        "started_ts": started,
        "received_ts": j.get("received_at") or started,
        "steps": j.get("steps") or [],
        "current_step": int(j.get("current_step") or 0),
        "updated_ts": updated,
    }


def _is_active(j):
    """活跃 = 运行中且未超时失联；其余（已完成/失败/疑似结束）一律归入折叠区"""
    return j["status"] == "running" and not j["stale"]


def payload():
    now = time.time()
    jobs = [job_payload(j, now) for j in P.list_jobs()]
    act = [j for j in jobs if _is_active(j)]
    fin = [j for j in jobs if not _is_active(j)]
    act.sort(key=lambda j: -(j.get("started_ts") or 0))      # 新起的任务排前
    fin.sort(key=lambda j: -(j.get("updated_ts") or 0))      # 最近结束的排前
    jobs = act + fin
    return {
        "now": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "port": PORT[0],
        "summary": {
            "active": len(act), "running": len(act), "finished": len(fin),
            "done": len([j for j in fin if j["status"] == "done"]),
            "failed": len([j for j in fin if j["status"] == "failed"]),
            "aborted": len([j for j in fin if j["status"] == "aborted"]),
            "total": len(jobs),
        },
        "jobs": jobs,
        # 前端据此判断「页面代码是否已过期」：服务端一重启（改了 UI），build 就变，
        # 已打开的旧标签页会在下一次轮询时自动 location.reload()，无需用户手动刷新。
        "build": BUILD,
    }


# ── 本机资源采样（CPU / 内存）：**纯标准库**实现，不依赖 psutil，跨平台 ──
_LAST_CPU = [None]   # (idle, total) 上一次采样；CPU 占用必须用两次采样的增量才算得准


def _cpu_snapshot():
    """返回 (idle, total) 累计计数（单位随平台，只用于求增量）。

    Windows：GetSystemTimes 的 kernel 时间**已包含 idle**，故 total=kernel+user。
    Linux：/proc/stat 首行 user+nice+system+idle。
    """
    if sys.platform.startswith("win"):
        try:
            import ctypes
            from ctypes import wintypes
            i, k, u = wintypes.FILETIME(), wintypes.FILETIME(), wintypes.FILETIME()
            if not ctypes.windll.kernel32.GetSystemTimes(
                    ctypes.byref(i), ctypes.byref(k), ctypes.byref(u)):
                return None
            ft = lambda x: (x.dwHighDateTime << 32) | x.dwLowDateTime
            return float(ft(i)), float(ft(k) + ft(u))
        except Exception:
            return None
    try:
        p = open("/proc/stat", encoding="utf-8").readline().split()[1:]
        user, nice, system, idle = float(p[0]), float(p[1]), float(p[2]), float(p[3])
        return idle, user + nice + system + idle
    except Exception:
        return None


def _mem_info():
    """返回 (used_gb, total_gb, percent)。Windows 用 GlobalMemoryStatusEx；其余读 /proc/meminfo。"""
    if sys.platform.startswith("win"):
        try:
            import ctypes
            from ctypes import wintypes

            class MEMORYSTATUSEX(ctypes.Structure):
                _fields_ = [("dwLength", wintypes.DWORD), ("dwMemoryLoad", wintypes.DWORD),
                            ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                            ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                            ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                            ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]

            s = MEMORYSTATUSEX()
            s.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
            if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(s)):
                return None, None, None
            total_gb = round(s.ullTotalPhys / 1073741824.0, 1)
            used_gb = round((s.ullTotalPhys - s.ullAvailPhys) / 1073741824.0, 1)
            return used_gb, total_gb, float(s.dwMemoryLoad)
        except Exception:
            return None, None, None
    try:
        info = {}
        for ln in open("/proc/meminfo", encoding="utf-8"):
            k, _, v = ln.partition(":")
            try:
                info[k.strip()] = float(v.split()[0]) / 1048576.0     # kB → GB
            except Exception:
                pass
        total = info.get("MemTotal", 0.0)
        avail = info.get("MemAvailable", info.get("MemFree", 0.0))
        used = max(0.0, total - avail)
        return round(used, 1), round(total, 1), (used / total * 100.0 if total else None)
    except Exception:
        return None, None, None


def sys_payload():
    """CPU% / 内存% —— 供底部状态栏轮询（/api/sys）。取不到就返回 null，前端显示 —。"""
    snap = _cpu_snapshot()
    cpu = None
    if snap:
        prev, _LAST_CPU[0] = _LAST_CPU[0], snap
        if prev is None:                      # 首次进程内采样：需要两次才有增量
            time.sleep(0.15)
            snap2 = _cpu_snapshot()
            if snap2:
                prev, snap, _LAST_CPU[0] = snap, snap2, snap2
        if prev:
            didle = max(0.0, snap[0] - prev[0])
            dtot = max(0.0, snap[1] - prev[1])
            if dtot > 0:
                cpu = round(min(100.0, max(0.0, (dtot - didle) / dtot * 100.0)), 1)
    used, total, pct = _mem_info()
    return {"cpu": cpu, "mem": round(pct, 1) if pct is not None else None,
            "mem_used_gb": used, "mem_total_gb": total,
            "ts": datetime.datetime.now().strftime("%H:%M:%S")}


PORT = [8791]
LOG_RESUME_SEC = [8.0]   # 日志区上滑后静默多少秒自动恢复跟随（见 --log-resume-sec）
START_TS = [time.time()] # 本实例启动时刻
LAST_HIT = [time.time()] # 最近一次 HTTP 请求时刻（"还有人在看"的判据）

# ⚠️ v1.0.38 血坑：**必须是 raw 字符串 `r"""`**。
#   PAGE 里全是 CSS/JS，反斜杠出现在 JS 正则中。用普通 `"""` 时，Python 会把 JS 里的
# `\b`（退格 chr(8)）、`\1`（八进制 chr(1)）等等**有效转义吃掉了**——`\s \d \w` 靠 Python
# 不认识才"侥幸"保留（还会冒 DeprecationWarning）。结果：正则静默失效，实测前端残留
# `2cjst9complete`。教训：**页面模板里的 JS 正则，反斜杠一律 raw 化，别指望 Python 放过。
PAGE = r"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>__TITLE__</title>
<link rel="icon" type="image/svg+xml" href="__FAVICON__">
<style>
:root{--bg:#0f1720;--card:#16212c;--line:#27384a;--tx:#e8eef5;--sub:#93a7bb;--acc:#3fb27f;--acc2:#e0a13a;--bad:#d9534f;--ink:#111a23}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--tx);font:14px/1.6 "Microsoft YaHei",system-ui,-apple-system,sans-serif;padding:clamp(10px,2.6vw,20px)}
.wrap{width:100%;max-width:1180px;margin:0 auto}
h1{font-size:clamp(13px,2.5vw,18px);margin:0 0 6px;font-weight:600;word-break:break-word}
h1 .hicon{margin-right:6px}
.sub{color:var(--sub);font-size:clamp(10px,1.7vw,12px);word-break:break-word}
.chips{display:flex;flex-wrap:wrap;gap:8px;margin:clamp(8px,1.6vw,14px) 0}
.chip{border:1px solid var(--line);border-radius:999px;padding:3px 11px;font-size:clamp(10px,1.6vw,12px);color:var(--sub);white-space:nowrap}
.chip b{color:var(--tx);font-weight:600}
.chip.run b{color:var(--acc)}.chip.bad b{color:var(--bad)}
/* ═══════════════ 卡片内部尺寸契约：**全部固定 px，不随视口变化** ═══════════════
   一整页卡片的高度由「各部件固定高度 + 固定间距」逐段相加而来，因此：
     · 任何宽度下卡片都一样高、间距都一样大（不会一会儿挤在一起、一会儿又拉太开）；
     · 唯一的例外是**外层页壳**（body padding / h1 / 顶部栏 / 底部状态栏 / 折叠区外边距），
       它们跟随视口缩放，且顶/底固定栏高度由 JS 实测后回填 body padding（见 syncBars）。
   ⚠️ 维护规则：卡片内**禁止**再出现 clamp(...)、vw、百分比高度或 em 字号；
      要改某段高度/间距，必须同步核对下面这张竖排账（活跃卡，自上而下）：
        卡片内边距 14
        chead  ≈24 + 下间距 10
        tl     58  + 下间距 12          ← 时间线（定高，内部 2 列网格）
        cbody  176                     ← 扫描条 34 / 进度环 132 + 间距 12 + 指标格 2×60+10
        .t     17.6 + 上间距 12 + 下间距 6
        steps  106 + 上下间距 2/4       ← 3 行 × 28px
        .t     17.6 + 12 + 6
        dots   52  + 上间距 4
        meta   32  + 上间距 10
        .t     17.6 + 12 + 6
        pre    92
        卡片内边距 14
      历史教训：这些值早期混用 clamp(…vw…) —— 视口一变，间距就变，窄的时候内容比盒子高、
      居中溢出后互相压叠（指标格盖住元信息），宽的时候又空出一大块。固定 px 后两头都没了。 */
.grid{display:grid;gap:14px;grid-template-columns:repeat(auto-fit,minmax(min(340px,100%),1fr));align-items:start}
.card{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:14px;min-width:0;overflow:hidden}
.chead{display:flex;gap:8px;align-items:flex-start;justify-content:space-between;margin-bottom:10px}
/* 标题：强制单行不换行；过长时走「中段省略」（保留开头与结尾，比尾省略信息量大） */
.ctitle{font-weight:600;font-size:14px;min-width:0;flex:1 1 auto;
  white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.badge{display:inline-flex;align-items:center;gap:6px;font-size:11px;padding:2px 9px;border-radius:999px;border:1px solid var(--line);flex:0 0 auto;white-space:nowrap}
.dot{width:8px;height:8px;flex:0 0 auto;border-radius:50%;background:var(--acc)}
.dot.live{box-shadow:0 0 0 0 rgba(63,178,127,.7);animation:p 1.6s infinite}
@keyframes p{0%{box-shadow:0 0 0 0 rgba(63,178,127,.6)}70%{box-shadow:0 0 0 8px rgba(63,178,127,0)}100%{box-shadow:0 0 0 0 rgba(63,178,127,0)}}
/* 卡片主体（进度环 / 扫描条 + 指标格）：高度**锁死 176px**，两种形态等高。
   ⚠️ 高度必须是「各部件写死高度之和」，且必须 ≥ 实际内容高度 ——
   内容比容器高时，align-content:center 会把内容**上下同时溢出**（overflow 可见），
   下沿就盖到后面兄弟节点的身上。v1.0.22 的真实 bug 即此：176 的内容塞进 150 的盒子 →
   折叠卡里指标格下沿压住元信息（重叠 23.8px）、活跃卡里压住「执行步骤」标题（11.8px），
   同时扫描条上沿向上溢出盖住时间线（12.8px）。看起来像"内容重叠"，根因是盒子矮了 26px。
   账目（宽屏，严丝合缝）：扫描条 34 + 主体间距 12 + 指标格 2×60 + 格间距 10 = 176。
   环式（.ring 由 JS 按卡片实际宽度决定，见 renderCard）环高 132 < 176 → 垂直居中，不溢出。 */
.cbody{display:grid;gap:12px;grid-template-columns:minmax(0,1fr);
  height:176px;align-content:center;overflow:hidden}
.cbody.ringmode{grid-template-columns:minmax(96px,132px) minmax(0,1fr);align-items:center}
.ring{position:relative;width:min(132px,42vw);aspect-ratio:1/1;margin:0 auto}
.ring svg{display:block;width:100%;height:100%;transform:rotate(-90deg)}
.ring .mid{position:absolute;inset:0;display:flex;flex-direction:column;align-items:center;justify-content:center}
.ring .pct{font-size:18px;font-weight:700;line-height:1.1}
.ring .small{color:var(--sub);font-size:10px}
.scan{height:34px;border-radius:10px;border:1px solid var(--line);background:#0c141b;position:relative;overflow:hidden}
.scan i{position:absolute;top:0;bottom:0;width:38%;border-radius:10px;background:linear-gradient(90deg,rgba(63,178,127,0),rgba(63,178,127,.85),rgba(63,178,127,0));animation:scan 1.9s linear infinite}
@keyframes scan{0%{left:-40%}100%{left:102%}}
.scan.ended i{display:none}
.scan.ended{background:linear-gradient(90deg,rgba(63,178,127,.25),rgba(63,178,127,.12))}
.scanstxt{position:absolute;inset:0;display:flex;align-items:center;justify-content:center;padding:0 12px;font-size:11px;color:var(--sub)}
.scanstxt>span{overflow:hidden;text-overflow:ellipsis;white-space:nowrap;min-width:0;max-width:100%}
/* 指标格：2×2 固定四格。间距、格子高度全部写死 —— 由 .cbody 的高度账目反推而来，
   改任何一项都要同步改 .cbody 的 176px，否则又会溢出压到下面的分区。 */
.stats{display:grid;gap:10px;grid-template-columns:repeat(2,minmax(0,1fr))}
.stat{background:var(--ink);border:1px solid var(--line);border-radius:10px;padding:7px 10px;min-width:0;
  height:60px;display:flex;flex-direction:column;justify-content:center;overflow:hidden}
.stat .k{color:var(--sub);font-size:10px;line-height:1.35;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.stat .v{font-size:15px;font-weight:600;margin-top:2px;line-height:1.3;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.t{color:var(--sub);font-size:11px;margin:12px 0 6px}
.dots{display:flex;height:52px;align-items:flex-end;gap:2px;margin-top:4px;overflow:hidden;
  padding:6px;border:1px solid var(--line);border-radius:10px;background:var(--ink)}
.dots i{display:block;flex:1 1 0;min-width:0;background:linear-gradient(180deg,var(--acc),#245c45);border-radius:2px}
.dots.empty{display:flex;align-items:center;justify-content:center;color:var(--sub);font-size:10px;border:1px dashed var(--line);border-radius:10px;height:52px}
/* 日志区：固定高度（不再随行数变化），超出纵向滚动；配深色滚动条 */
pre{background:#0c141b;border:1px solid var(--line);border-radius:10px;padding:9px;height:92px;overflow:auto;font-size:10.5px;color:#a9c2d6;margin:0;white-space:pre-wrap;word-break:break-word}
pre::-webkit-scrollbar,.steps::-webkit-scrollbar{width:8px;height:8px}
pre::-webkit-scrollbar-track,.steps::-webkit-scrollbar-track{background:transparent}
pre::-webkit-scrollbar-thumb,.steps::-webkit-scrollbar-thumb{background:#2a3a48;border-radius:8px}
pre::-webkit-scrollbar-thumb:hover,.steps::-webkit-scrollbar-thumb:hover{background:#3d5265}
/* 元信息区：强制单行（不换行、不出滚动条）；文本超长时自动走马灯滚动。
   无缝做法：轨道内放两份完全相同的内容，滚动 -50% 即首尾相接，永不跳字。 */
.meta{color:var(--sub);font-size:10px;margin-top:10px;
  padding:7px 10px;border:1px solid var(--line);border-radius:10px;background:var(--ink);
  overflow:hidden;white-space:nowrap;word-break:normal}
/* ── 通用「单行 + 走马灯」组件（元信息行 / 底部状态栏共用）──
   结构：容器 > .mtrack > .mtxt(内容) + .mtxt(副本)
   常态只显示第一份；放不下时容器加 .mq → 显示副本并匀速滚动 -50%，首尾相接不跳字。 */
.mtrack{display:inline-flex;white-space:nowrap;will-change:transform}
.mtxt{flex:0 0 auto;white-space:nowrap;padding-right:2.5em}
.mtxt:last-child{display:none}                  /* 第二份仅走马灯时需要，平时必须藏起来 */
.mq .mtxt:last-child{display:block}
/* 位移由 JS 逐帧驱动（见 mqStep），**不再用 CSS animation**：
   animation 绑在元素上，元素被移动（insertBefore/appendChild）或结构变动就会从 0 重启 → 跳闪；
   自维护 offset 则可在每次 render 后原样「接着滚」，视觉完全连续。 */
/* 时间线：📥接到命令 · ▶开始执行 · ⏱已运行（把"实时"的锚点时间亮出来）
   ⚠️ 高度必须**锁死**：早前用 flex-wrap 自由换行，某张卡只是「已运行 32m56s」比「1h53m」多一个字
   → 恰好挤出第二行 → 该卡比别人高 21px，整列参差。现改为定高网格：固定 2 列 × 2 行（第 3 组独占第二行），
   行数恒定、标签过长只裁标签（时间值永不裁），各卡高度绝对一致。 */
.tl{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:2px 12px;
  height:58px;align-content:center;overflow:hidden;margin:0 0 12px;
  padding:8px 11px;border:1px solid var(--line);border-radius:10px;background:var(--ink);
  font-size:11px;color:var(--sub)}
.tg{display:flex;align-items:center;gap:5px;min-width:0;white-space:nowrap}
.tg>span{flex:0 1 auto;overflow:hidden;text-overflow:ellipsis;min-width:0}
.tl b{flex:0 0 auto;color:var(--tx);font-weight:600;font-variant-numeric:tabular-nums}
.tl .t-recv{color:#9fd0ff}.tl .t-start{color:var(--acc)}.tl .t-run{color:var(--acc2)}
/* 图标位：**固定宽度的槽位**（居中），让三组标签的文字起点完全对齐。
   ⚠️ 踩坑：原先把「emoji + 文字」写在同一个 span 里（`📥 接到命令`）——
   三个 emoji 的**字宽并不相同**（实测 11px 字号下：📥 15.11px / ▶ 9.48px / ⏱ 9.95px），
   于是第二行换行出来的「⏱ 已运行」文字起点比第一行「📥 接到命令」左移了 5px，
   视觉上就是"换行后左侧没对齐"。现在图标独占一个定宽槽位，标签文字起点恒定。 */
.tl .ic{flex:0 0 auto;width:1.5em;text-align:center;font-style:normal;line-height:1}
/* 步骤进度条（执行到哪一步）：已完成✓ / 进行中🔄 / 待办⏳
   完全纵向排列（一步一行，不横向自动换行）；固定「3 行」高度，超出纵向滚动 —— 各卡片同高 */
/* 高度 = 3×行高28 + 2×间距4 + 上下内边距12 + 上下边框2 = 106px，恰好露出 3 行、不多不少 */
.steps{display:flex;flex-direction:column;flex-wrap:nowrap;align-items:stretch;gap:4px;margin:2px 0 4px;
  height:106px;overflow-y:auto;overflow-x:hidden;
  padding:6px;border:1px solid var(--line);border-radius:10px;background:#0f1922}
.step{display:flex;align-items:center;gap:6px;font-size:11px;flex:0 0 auto;
  line-height:18px;   /* 固定行高 → 每行恒为 28px（18+内边距8+边框2），三行高度才可精确计算 */
  padding:4px 10px;border-radius:999px;border:1px solid var(--line);color:var(--sub);
  background:#0c141b;white-space:nowrap;max-width:100%;min-width:0;overflow:hidden}
.step .stxt{overflow:hidden;text-overflow:ellipsis;white-space:nowrap;min-width:0;flex:1 1 auto}
.step .num{flex:0 0 auto}   /* 序号不参与收缩，只让文字收缩出省略号 */
/* 空步骤占位（没上报步骤明细的任务）：只占位、不喧宾夺主，保证与有步骤的卡等高 */
.steps .none{display:flex;align-items:center;height:28px;padding:0 2px;
  color:var(--sub);opacity:.75;font-size:10px;white-space:nowrap;
  overflow:hidden;text-overflow:ellipsis}
.cbody pre{white-space:pre-wrap;word-break:break-all;overflow-wrap:anywhere}
.step .num{display:inline-flex;width:18px;height:18px;align-items:center;justify-content:center;
  border-radius:50%;background:#1c2a36;font-size:11px;font-weight:600;color:var(--sub)}
.step.done{color:#bfe9d3;border-color:#265c43;background:rgba(63,178,127,.10)}
.step.done .num{background:var(--acc);color:#06231a}
.step.cur{color:var(--tx);border-color:var(--acc2);background:rgba(224,161,58,.14);
  box-shadow:0 0 0 0 rgba(224,161,58,.5);animation:p 1.6s infinite}
.step.cur .num{background:var(--acc2);color:#2a1c05}
/* 状态标记：固定前缀（等宽），文字起点因此始终对齐 —— ◉已完成 / ◍进行中 / ○待办 */
.step .st{flex:0 0 auto;width:1.1em;text-align:center;font-size:12px;line-height:1}
.step.done .st{color:var(--acc)}                  /* ◉ 实心绿 */
.step.cur .st{color:var(--acc2)}                  /* ◍ 橙（进行中） */
.step.todo .st{color:var(--sub);opacity:.55}      /* ○ 灰（待办） */
.empty{background:var(--card);border:1px dashed var(--line);border-radius:14px;padding:clamp(14px,3vw,26px);color:var(--sub);font-size:clamp(11px,1.7vw,13px)}
.empty code{background:var(--ink);border:1px solid var(--line);border-radius:6px;padding:1px 6px;color:#cfe3f3;font-size:clamp(10px,1.6vw,12px)}
/* 已结束任务折叠区：默认收起，只显示活跃任务 */
.fold{margin-top:clamp(12px,2.2vw,18px);border:1px solid var(--line);border-radius:14px;background:var(--card);overflow:hidden}
.fold>summary{cursor:pointer;list-style:none;padding:clamp(10px,1.7vw,14px) clamp(12px,2vw,16px);display:flex;align-items:center;gap:9px;color:var(--sub);font-size:clamp(11px,1.8vw,13px);user-select:none}
.fold>summary::-webkit-details-marker{display:none}
.fold>summary:hover{color:var(--tx)}
.fold .arrow{flex:0 0 auto;transition:transform .18s ease}
.fold[open] .arrow{transform:rotate(90deg)}
.fold .foldgrid{padding:0 clamp(10px,1.8vw,16px) clamp(12px,2vw,16px);grid-template-columns:repeat(auto-fit,minmax(min(340px,100%),1fr))}
/* 折叠区内的卡片改为紧凑模式：隐去步骤/曲线/日志，只留状态/指标/元信息 */
.card.mini .t,.card.mini .dots,.card.mini pre,.card.mini .steps{display:none!important}
/* 删除记录：图标按钮 + 二次确认（不做弹窗，避免预览面板里 confirm 被拦） */
.xdel{flex:0 0 auto;border:1px solid var(--line);background:var(--ink);color:var(--sub);
  border-radius:8px;padding:2px 9px;font-size:11px;line-height:1.6;
  cursor:pointer;white-space:nowrap;font-family:inherit;transition:.15s}
.xdel:hover{color:#ffb4b4;border-color:#7c3b3b}
.xdel.arm{color:#ffe0e0;border-color:#b04a4a;background:#2c1414}
.clearbtn{margin-left:auto;flex:0 0 auto;border:1px solid var(--line);background:var(--ink);
  color:var(--sub);border-radius:8px;padding:3px 11px;font-size:11px;
  cursor:pointer;white-space:nowrap;font-family:inherit;transition:.15s}
.clearbtn:hover{color:#ffb4b4;border-color:#7c3b3b}
.clearbtn.arm{color:#ffe0e0;border-color:#b04a4a;background:#2c1414}
/* ── 顶部固定头栏（与底部状态栏对称）：标题 + 采集信息 + 统计胶囊 ──
   高度不写死：由 JS 的 syncBars() 量出实际高度后写进 body 的 padding-top，
   窄屏胶囊换行变高也不会遮住卡片（这里的 clamp 只是 JS 执行前的兜底值）。 */
body{padding-top:clamp(84px,15vw,104px);padding-bottom:clamp(46px,7vw,58px)}
.topbar{position:fixed;left:0;right:0;top:0;z-index:30;display:block;
  padding:clamp(7px,1.4vw,10px) clamp(10px,2.2vw,16px);background:#111a23;border-bottom:1px solid var(--line)}
.topbar h1{margin-bottom:3px}
.topbar .chips{margin:clamp(5px,1vw,8px) 0 0}
/* 采集信息行：单行显示，超出走马灯（否则固定栏会被撑成两行、高度抖动） */
.hdrbar{white-space:nowrap;overflow:hidden}
.sysbar{position:fixed;left:0;right:0;bottom:0;z-index:30;display:block;
  padding:8px clamp(10px,2.2vw,16px);background:#111a23;border-top:1px solid var(--line);
  font-size:clamp(9.5px,1.5vw,11.5px);color:var(--sub);
  overflow:hidden;white-space:nowrap;text-align:center}   /* 内容默认居中（.mtrack 是 inline-flex，可被 text-align 居中） */
.sysbar .sbk{color:var(--sub)}
.sysbar .sbv{display:inline-block;min-width:3.1em;color:var(--tx);font-weight:600;font-variant-numeric:tabular-nums}
.sysbar .sbbar{display:inline-block;width:clamp(54px,8vw,104px);height:6px;border-radius:999px;
  background:#1c2a36;overflow:hidden;vertical-align:middle;margin:0 2px 0 5px}
.sysbar .sbbar>b{display:block;height:100%;width:0;border-radius:999px;background:var(--acc);transition:width .45s ease}
.sysbar .sbbar>b.warn{background:var(--acc2)}
.sysbar .sbbar>b.bad{background:var(--bad)}
.sysbar .sbsep{color:var(--line);margin:0 7px}
.sysbar .sbdim{color:var(--sub);opacity:.85}
</style></head><body>
<!-- 顶部固定头栏：标题 + 采集信息 + 统计胶囊（内层 .wrap 与正文同宽，保持左右对齐） -->
<div class="topbar" id="topbar"><div class="wrap">
<h1><span class="hicon">📊</span>__H1TEXT__</h1>
<div class="sub hdrbar" id="hdr">加载中…</div>
<div class="chips" id="chips"></div>
</div></div>
<div class="wrap">
<div class="grid" id="grid"></div>
<div class="empty" id="empty" style="display:none">
<b>当前没有正在执行的任务。</b><br><br>
代理在接到用户命令时会立即建卡并展开本看板；任何脚本也能一行接入：<code>from progress import Progress</code> → <code>with Progress("任务名", total=100) as p: p.set(done)</code><br>
命令行接入：<code>python progress.py begin --title "任务名" --total 100 --steps "准备,处理,收尾"</code>
</div>
<details class="fold" id="fold" style="display:none">
<summary><span class="arrow">▶</span><span id="foldtxt">已结束的任务</span><button class="clearbtn" id="clearfin" type="button" title="清空全部已结束记录（运行中的任务不受影响）">清空已结束</button></summary>
<div class="grid foldgrid" id="foldgrid"></div>
</details>
</div>
<!-- 底部固定状态栏：本机 CPU / 内存；单行显示，内容超出时自动走马灯（副本由 JS 克隆） -->
<div class="sysbar" id="sysbar"><div class="mtrack"><span class="mtxt"><span class="sbk">CPU</span> <span class="sbv cpuv">—</span><span class="sbbar"><b class="cpub"></b></span><span class="sbsep">|</span><span class="sbk">内存</span> <span class="sbv memv">—</span><span class="sbbar"><b class="memb"></b></span><span class="sbsep">|</span><span class="sbdim sysmem"></span><span class="sbsep">|</span><span class="sbdim systs"></span></span></div></div>
<script>
const INIT=__INITIAL__;
const SYSINIT=__SYS__;
const LOG_RESUME_MS=__LOGRESUME__*1000;   // 用户上滑后，静默这么久无操作 → 自动恢复跟随
const SBUILD='__BUILD__';                 // 服务端构建指纹：轮询发现不一致 → 自动重载（免手动刷新）
const RA=52, RC=2*Math.PI*RA;      // 环半径/周长（viewBox 132）
const CARDS={};                    // id -> {el, refs}
const LAST={};                     // id -> samples 用于重绘
const DATA={};                     // id -> 本轮数据，尺寸变化时据此重排形态（环式/条式）

/* 指标格排布：**恒定 2×2**（4 格永远两行两列，不再按宽度切 4+0 / 1×4）。
   早期按实测宽度在 4+0 / 2+2 / 1×4 之间切换，留下两个坑：
   ① 高度随排布变（1 行 60px / 2 行 130px），而 .cbody 高度是写死的 →
      宽卡走 1 行时下方空出 70px 空白，窄卡走 2 行则刚好填满，同屏两副面孔；
   ② 卡片宽窄不同 → 排布不同 → 卡片高度不再一致（用户要的是"各卡等高"）。
   现在恒定 2×2：stats 恒为 60×2+10 = 130，严丝合缝填满 .cbody 的 176px，全站等高、无空白。
   （.stat 高度 60 与 .cbody 的 176 是一笔账，改一处必须同步另一处。） */
function layoutStats(el){
  if(!el) return;
  el.style.gridTemplateColumns='repeat(2, minmax(0,1fr))';
  el.dataset.cols=2;
}
/* 推进曲线：柱高百分比封顶 100%、柱数按容器宽下采样、容器 overflow:hidden 兜底 */
function drawDots(ds, s){
  const W=ds.clientWidth, H=ds.clientHeight;
  if(W<=0||H<=0) return;
  if(!s||s.length<2){ ds.className='dots empty'; ds.textContent='采样中…'; return; }
  ds.className='dots';
  const PITCH=5, maxBars=Math.max(6, Math.floor((W+2)/PITCH));
  let arr=s;
  if(s.length>maxBars){ const step=(s.length-1)/(maxBars-1); arr=[]; for(let i=0;i<maxBars;i++) arr.push(s[Math.round(i*step)]); }
  const mn=arr[0].d, mx=arr[arr.length-1].d, span=Math.max(mx-mn,1);
  ds.innerHTML=arr.map(p=>{ const pct=Math.max(10,Math.min(100,10+90*((p.d-mn)/span))); return '<i style="height:'+pct.toFixed(1)+'%"></i>'; }).join('');
}
/* 文本转义：步骤名/日志常含 < > & " 等字符，防注入、也防止把 title 属性撑破 */
function esc(s){return String(s==null?'':s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');}
/* v1.0.38：标题清洗（前端兜底，针对 jobs.json 里的历史脏数据 / 其它建卡路径）。
   hook_prompt 会拿到 WB 注入的 XML（<task-notification>…</task-notification>、
   <command-name>、<local-command-stdout> 等）并混进用户原文 → 直接当标题就成了一串代码。
   这里把注入块整块剥掉，再清裸尖括号和 id/status 之类残留，剩下的才是人话。 */
const SRC_ZH={'script':'脚本建卡（Progress 上下文）','cli':'命令行 progress.py',
  'hook-bg':'后台任务 Hook','user-request':'用户命令 Hook','hook':'运行时 Hook 登记',
  'auto-step':'步骤自动映射'};
function srcTxt(s){ if(s==null||s==='') return '—'; return SRC_ZH[s]||s; }
function cleanTitle(s){
  s=String(s==null?'':s);
  /* 注入块整块剥掉：`…</task-notification>` **或到字符串末尾**（未闭合时也要连内部文本一起
     带走，否则会残留 `2cjst9complete` 这种碎片——实测踩到）。 */
  // 注入块整块剥掉：`…</task-notification>` 或**到字符串末尾**（未闭合时连内部文本一起带走）
  s=s.replace(/<\s*(task-notification|system-reminder|command-name|local-command-(stdout|stderr)|antml:param|tool_use)\b[\s\S]*?(?:<\s*\/\s*\1\s*>|$)/gi,' ');
  s=s.replace(/<[^>]{0,200}>/g,'');                                        // 任何残留尖括号块
  s=s.replace(/\b(?:task[-_]?id|task[-_]?name|session[-_]?id|hook[-_]?type|status)\s*[:=]\s*[^\s,;>]+/gi,'');
  s=s.replace(/\s+/g,' ').replace(/^[\s\-–—|·、,，。;；:：]+|[\s\-–—|·、,，。;；:：]+$/g,'');
  return s||'用户请求';
}
/* 中段省略：超长文本渲染成「首段…尾段」，保留开头（命令/动作）与结尾（文件名/参数） */
function midShort(s,n){
  s=String(s==null?'':s);
  if(s.length<=n) return s;
  const head=Math.ceil((n-1)/2), tail=n-1-head;
  return s.slice(0,head)+'…'+s.slice(s.length-tail);
}
function fmtDur(sec){
  sec=Math.max(0,Math.round(sec||0));
  if(sec<60) return sec+'s';
  if(sec<3600){ const m=Math.floor(sec/60), s=sec%60; return m+'m'+(s<10?'0':'')+s+'s'; }
  const h=Math.floor(sec/3600), m=Math.floor((sec%3600)/60); return h+'h'+(m<10?'0':'')+m+'m';
}
/* 「最近更新」的相对时间：秒 → 分钟 → 小时 → 天。
   ⚠️ 早期直接拼 d.ago+'s 前'，卡片放久了会变成「8908s 前」这种难读的长串；
      统一切到这个函数（最长 3 位数字 + 2 个汉字，固定宽度也不会撑破指标格）。 */
function fmtAgo(sec){
  sec=Math.max(0,Math.round(sec||0));
  if(sec<60) return sec<=0?'刚刚':sec+' 秒前';
  if(sec<3600) return Math.floor(sec/60)+' 分钟前';
  if(sec<86400) return Math.floor(sec/3600)+' 小时前';
  return Math.floor(sec/86400)+' 天前';
}
function fmtClock(ts){
  if(!ts) return '-';
  const t=new Date(ts*1000), p=n=>(''+n).padStart(2,'0');
  return p(t.getHours())+':'+p(t.getMinutes())+':'+p(t.getSeconds());
}
function rateTxt(d){ if(!d.sec_per_item) return '采样中…'; return (d.rate_avg?'≈':'')+d.sec_per_item+' 秒/'+d.unit+(d.rate_avg?'（均值）':''); }
function metricsFor(d){
  const ended = d.status!=='running' || d.stale;
  if(d.determinate){
    if(ended) return [['已完成', d.done+' / '+d.total],['剩余', Math.max(d.total-d.done,0)+' '+d.unit],['速率','—'],['耗时', d.exec||'—']];
    return [['已完成', d.done+' / '+d.total],['剩余', Math.max(d.total-d.done,0)+' '+d.unit],['速率', rateTxt(d)],['预计完成', d.eta_clock||(d.done>=d.total?'即将完成':'采样中…')]];
  }
  if(ended) return [['状态', d.label],['耗时', d.exec||'—'],['最近更新', fmtAgo(d.ago)],['来源', srcTxt(d.source)]];
  // 无总量 + 有预估总时长 → 用时间基线给出「预计完成」，不再永远显示"采样中…"
  if(d.planned_sec){
    const eta = d.eta_overdue>0 ? ('已超 '+fmtDur(d.eta_overdue)) : (d.eta_clock||'—');
    return [['已运行', d.exec],['预估耗时', fmtDur(d.planned_sec)],
            ['时间进度', (d.time_pct===null?'—':d.time_pct+'%')],['预计完成', eta]];
  }
  // 日志行数取「真实总行数」log_count；缺失时退回尾部条数（不要直接用 log_tail.length，它恒为 10）
  const logN=(d.log_count!=null?d.log_count:(d.log_tail||[]).length);
  return [['已运行', d.exec],['最近更新', fmtAgo(d.ago)],['日志', logN+' 行'],['来源', srcTxt(d.source)]];
}
async function postJSON(url, body){
  const r=await fetch(url,{method:'POST',headers:{'Content-Type':'application/json'},
                           body:JSON.stringify(body||{}),cache:'no-store'});
  return await r.json();
}
/* 二次确认（不用 window.confirm，预览面板 iframe 里可能被拦）：
   第一次点击 → 变「确认？」，4 秒内再点才执行；否则自动还原。 */
function armConfirm(btn, label, fn){
  if(btn.dataset.arm==='1'){
    btn.dataset.arm='0'; btn.classList.remove('arm'); btn.textContent=label;
    clearTimeout(btn._armT); fn(); return;
  }
  btn.dataset.arm='1'; btn.classList.add('arm'); btn.textContent='确认？';
  clearTimeout(btn._armT);
  btn._armT=setTimeout(function(){
    btn.dataset.arm='0'; btn.classList.remove('arm'); btn.textContent=label;
  },4000);
}
function buildCard(d){
  const el=document.createElement('div'); el.className='card'; el.dataset.id=d.id;
  el.innerHTML =
    // 头部顺序：标题（可伸缩占位） → 状态徽标 → 删除按钮（永远在最右侧）
    '<div class="chead"><div class="ctitle"></div><span class="badge"><span class="dot"></span><span class="btxt"></span></span><button class="xdel" type="button" title="删除这条记录">✕</button></div>'+
    '<div class="tl"></div>'+
    '<div class="cbody"><div class="cleft"></div><div class="stats"></div></div>'+
    '<div class="t tsteps">执行步骤</div><div class="steps"></div>'+
    // 分区标题各带专用 class（tsteps / tdots）：控制显隐时必须精确定位，
    // 不能靠 parentNode.querySelector('.t') —— 那会命中卡片里**第一个** .t（即「执行步骤」）
    '<div class="t tdots">推进曲线</div><div class="dots"></div>'+
    '<div class="meta"></div>'+
    '<div class="t">日志（尾部）</div><pre></pre>';
  const refs={title:el.querySelector('.ctitle'),badge:el.querySelector('.badge'),dot:el.querySelector('.dot'),
    btxt:el.querySelector('.btxt'),body:el.querySelector('.cbody'),left:el.querySelector('.cleft'),
    stats:el.querySelector('.stats'),dots:el.querySelector('.dots'),meta:el.querySelector('.meta'),log:el.querySelector('pre'),
    tl:el.querySelector('.tl'),steps:el.querySelector('.steps'),stepsTitle:el.querySelector('.tsteps'),
    dotsTitle:el.querySelector('.tdots'),xdel:el.querySelector('.xdel')};
  // 单条删除：点一次变「确认？」，4 秒内再点一次才真删（运行中的任务不显示此按钮）
  refs.xdel.addEventListener('click',function(e){
    e.preventDefault(); e.stopPropagation();
    armConfirm(refs.xdel, '✕', async function(){
      let out={};
      try{ out=await postJSON('/api/delete',{id:d.id}); }catch(err){}
      if(out.ok){ el.remove(); delete CARDS[d.id]; tick(); }
      else { refs.xdel.textContent='删除失败'; setTimeout(function(){ refs.xdel.textContent='✕'; },1600); }
    });
  });
  // 用户手动上滑 → 暂停自动跟随；回到底部 → 立即恢复；
  // 上滑后静默 LOG_RESUME_MS 无操作 → 也自动滚回底部（避免一直停在半截日志）
  refs.log.addEventListener('scroll', function(){
    const l=refs.log;
    if(l.scrollHeight - l.scrollTop - l.clientHeight < 8){
      l.dataset.stick='1';
      clearTimeout(l._resumeT);
    }else{
      l.dataset.stick='0';
      clearTimeout(l._resumeT);
      l._resumeT=setTimeout(function(){ l.dataset.stick='1'; l.scrollTop=l.scrollHeight; }, LOG_RESUME_MS);
    }
  });
  CARDS[d.id]={el:el,refs:refs};
  return CARDS[d.id];
}
/* 日志自动跟随：内容变化时滚到底，始终露出最后一行 */
function setLog(el, lines){
  const arr=lines||[];
  // ⚠️ 拼接分隔符用 String.fromCharCode(10)，**别写 '\\n'**：PAGE 是 raw 字符串（见文件头血坑），
  // 源码里的 '\\n' 会原样进 JS，被 JS 解析成「反斜杠 + n」两个字面字符 → 日志换行整段消失。
  const NL=String.fromCharCode(10);
  const txt=arr.length?arr.join(NL):'（暂无日志 · 本任务只上报了步骤/进度，未写明细）';
  if(el.dataset.txt===txt) return;
  el.textContent=txt;
  el.dataset.txt=txt;
  if(el.dataset.stick!=='0') el.scrollTop=el.scrollHeight;
}
function renderCard(c,d){
  const r=c.refs;
  if(!d.title) d.title='';
  d.title=cleanTitle(d.title);   // v1.0.38：历史脏标题（注入的 XML 混进原文）在渲染前洗一遍
  DATA[d.id]=d;                 // 缓存本轮数据：窗口/折叠区尺寸变化时 relayout() 据此重排形态
  fitMiddle(r.title, d.title);   // 单行 + 超长中段省略（完整标题挂 title 悬停可见）
  r.btxt.textContent=d.label;
  const color=d.status==='running'&&!d.stale?'#3fb27f':(d.status==='failed'?'#d9534f':'#e0a13a');
  r.dot.style.background=color;
  r.dot.className='dot'+(d.status==='running'&&!d.stale?' live':'');
  // 运行中的任务不给删除入口（避免误删在跑的任务记录）；已结束/失败/疑似结束才显示
  if(r.xdel) r.xdel.style.display=(d.status==='running'&&!d.stale)?'none':'';
  // 时间线：📥接到命令 → ▶开始执行 → ⏱已运行（把"实时"的锚点时间亮出来）
  // 三个「标签+时间」小组各自成块，配合定高网格 → 无论时间串长短都占同样行数、卡片等高。
  // ⚠️ 图标必须单独放进 `<i class="ic">` 定宽槽位：三个 emoji 字宽不同（📥 比 ▶/⏱ 宽约 5px），
  //    合并写在标签 span 里时，换行到第二行的「⏱ 已运行」文字起点会左移 → 看着没对齐。
  const recv=fmtClock(d.received_ts), start=fmtClock(d.started_ts);
  r.tl.innerHTML=
    '<span class="tg"><i class="ic">📥</i><span class="t-recv">接到命令</span><b>'+recv+'</b></span>'+
    '<span class="tg"><i class="ic">▶</i><span class="t-start">开始执行</span><b>'+start+'</b></span>'+
    '<span class="tg"><i class="ic">⏱</i><span class="t-run">已运行</span><b>'+d.exec+'</b></span>';
  // 步骤进度条：已完成✓ / 进行中🔄 / 待办⏳（展示"执行到哪一步"）
  if(d.steps && d.steps.length){
    let cur=d.current_step;
    if(d.status==='done') cur=d.steps.length;     // 已完成 → 全部打勾
    r.steps.style.display='flex';   // 纵向排列：不再用 › 分隔符（否则会独占一行）
    if(r.stepsTitle) r.stepsTitle.style.display='';
    r.steps.innerHTML=d.steps.map(function(nm,i){
      const cls=i<cur?'done':(i===cur?'cur':'todo');
      // 固定状态标记：◉ 已完成 / ◍ 进行中 / ○ 待办（等宽，文字起点对齐）；已完成不再打 ✓
      const st=i<cur?'◉':(i===cur?'◍':'○');
      // 双保险：JS 侧先按 40 字做中段省略（防超长撑破容器），完整原文挂 title 供悬停查看
      const full=esc(nm), disp=esc(midShort(nm,40));
      return '<span class="step '+cls+'" title="'+full+'"><span class="st">'+st+'</span><span class="num">'+(i+1)+'</span><span class="stxt">'+disp+'</span></span>';
    }).join('');
  }else{
    // 「执行步骤」是卡片的固定分区：即使没有步骤明细也**要留位**（否则不同卡片会差一大截、
    // 破坏等高）。占位文案说明原因，用户知道这只是没上报明细、不是面板坏了。
    r.steps.style.display='flex';
    if(r.stepsTitle) r.stepsTitle.style.display='';
    r.steps.innerHTML='<span class="none">○ 暂无步骤明细 · 本任务只上报进度/日志</span>';
  }
  // 步骤区是固定高度的滚动容器：自动滚到「当前步骤」，让进行中的那步始终露出；
  // 全部完成时滚到底露出最后一步。（只在溢出时才动，未溢出不动）
  if(d.steps && d.steps.length && r.steps.scrollHeight>r.steps.clientHeight+2){
    const curEl=r.steps.querySelector('.step.cur');
    if(curEl){
      const cr=r.steps.getBoundingClientRect(), er=curEl.getBoundingClientRect();
      if(er.top<cr.top || er.bottom>cr.bottom){
        r.steps.scrollTop += (er.top-cr.top) - (cr.height-er.height)/2;
      }
    }else{
      r.steps.scrollTop=r.steps.scrollHeight;
    }
  }
  // 左侧：有总量 + 卡片够宽 → 进度环；否则 → 扫描条
  // ⚠️ 必须按**卡片实际宽度**判断，不能只看「有无总量」：折叠区是 auto-fit 多列网格，窄卡（~340px）
  //    里环会独占 132px，右栏只剩 ~160px，指标格被迫折成 4 行、把卡片撑高 40px+。
  //    宽度不够时用扫描条，两种形态就都锁在 176px 的 .cbody 里，整行卡片完全对齐。
  const useRing = !!d.determinate && c.el.clientWidth>=420;
  if(useRing){
    if(!r.left.dataset.kind||r.left.dataset.kind!=='ring'){
      r.left.dataset.kind='ring';
      r.left.innerHTML='<div class="ring"><svg viewBox="0 0 132 132" preserveAspectRatio="xMidYMid meet">'+
        '<circle cx="66" cy="66" r="52" fill="none" stroke="#22323f" stroke-width="12"></circle>'+
        '<circle class="arc" cx="66" cy="66" r="52" fill="none" stroke="#3fb27f" stroke-width="12" stroke-linecap="round" stroke-dasharray="'+RC.toFixed(1)+'" stroke-dashoffset="'+RC.toFixed(1)+'"></circle></svg>'+
        '<div class="mid"><div class="pct"></div><div class="small"></div></div></div>';
    }
    const p=r.left.querySelector('.pct'), s=r.left.querySelector('.small'), a=r.left.querySelector('.arc');
    p.textContent=(d.pct===null?'-':d.pct+'%');
    s.textContent=d.done+' / '+d.total;
    a.setAttribute('stroke-dashoffset', RC*(1-(d.pct||0)/100));
    a.setAttribute('stroke', color);
    r.body.classList.add('ringmode');
  }else{
    if(r.left.dataset.kind!=='scan'){
      r.left.dataset.kind='scan';
      r.left.innerHTML='<div class="scan"><i></i><div class="scanstxt"><span></span></div></div>';
    }
    const bar=r.left.querySelector('.scan'), txt=r.left.querySelector('.scanstxt>span');
    // 无总量任务的大条文案：**运行中且有步骤 → 实时显示当前步骤**（随 current_step 推进而变，
    // 不再永远卡在建卡时写死的 message，如「接到命令」）；其余情况回落 message/label。
    const hasSteps=d.steps&&d.steps.length;
    let slabel;
    if(d.status==='running'&&!d.stale&&hasSteps){
      const cs=Math.max(0,Math.min(d.current_step||0,d.steps.length-1));
      slabel='▶ 第'+(cs+1)+'/'+d.steps.length+'步 · '+d.steps[cs];
    }else if(hasSteps){
      slabel='共 '+d.steps.length+' 步 · '+(d.message||d.label);
    }else{
      slabel=d.message||(d.status==='running'&&!d.stale?('已运行 '+d.exec):d.label);
    }
    if(txt) txt.textContent=slabel;
    if(bar){ // 只有运行中才放扫描动画；已结束用静态填充（add/remove 成对，避免 class 累积）
      if(d.status==='running'&&!d.stale){ bar.classList.add('live'); bar.classList.remove('ended'); }
      else { bar.classList.remove('live'); bar.classList.add('ended'); }
    }
    r.body.classList.remove('ringmode');
  }
  // 指标格（4+0 / 2+2 / 1×4）
  const m=metricsFor(d);
  if(r.stats.dataset.n!=='4'){ r.stats.innerHTML=m.map(()=>'<div class="stat"><div class="k"></div><div class="v"></div></div>').join(''); r.stats.dataset.n='4'; }
  const boxes=r.stats.children;
  for(let i=0;i<m.length;i++){ boxes[i].querySelector('.k').textContent=m[i][0]; boxes[i].querySelector('.v').textContent=m[i][1]; }
  layoutStats(r.stats);
  // 曲线（无样本就整段隐去，避免空壳）
  // ⚠️ 踩坑：早期这里写 r.dots.parentNode.querySelector('.t') —— parentNode 是整张卡片，
  //    命中的是卡片里**第一个** .t（「执行步骤」标题！）而不是「推进曲线」标题。
  //    于是有采样数据的卡会把「执行步骤」标题强行显示出来，哪怕它一条步骤都没有（空标题 bug）。
  //    现在两个分区标题各有专用 class，精确定位。
  LAST[d.id]=d.samples;
  // 与「执行步骤」同理：曲线区是固定分区，无采样也要**留位**（保证各卡片等高），
  // 占位文案由 drawDots() 内部渲染成「采样中…」。
  drawDots(r.dots, d.samples);
  r.dots.style.display='';
  if(r.dotsTitle) r.dotsTitle.style.display='';
  // 元信息 + 日志
  const bits=[];
  if(d.message) bits.push(d.message);
  bits.push('更新于 '+fmtAgo(d.ago));
  if(d.cmd) bits.push('命令: '+d.cmd);
  if(d.cwd) bits.push('目录: '+d.cwd);
  if(d.session) bits.push('会话: '+d.session);
  setMarquee(r.meta, bits.join(' · '));
  setLog(r.log, d.log_tail);
}
/* 元信息：永远单行；文本超出容器宽度才启动走马灯（否则静止显示，避免短文空转） */
/* ⚠️ 防跳闪的关键：轨道结构**只建一次**，之后只改两份 .mtxt 的 textContent。
   早期实现每次刷新都重建 innerHTML，正在跑的 CSS 动画随之从 0 重启 → 视觉上"跳一下"。
   （状态栏之所以不闪，正是因为它一直只改文字、从不重建结构。） */
function setMarquee(el, txt){
  if(!el) return;
  txt=String(txt==null?'':txt);
  let tr=el.firstElementChild;
  if(!tr||!tr.classList||!tr.classList.contains('mtrack')){
    el.dataset.txt='';                 // 结构重建过 → 强制重新同步一次文字
    el.innerHTML='<div class="mtrack"><span class="mtxt"></span><span class="mtxt" aria-hidden="true"></span></div>';
    tr=el.firstElementChild;
  }
  if(el.dataset.txt!==txt){
    el.dataset.txt=txt;
    const copies=tr.querySelectorAll('.mtxt');
    for(let i=0;i<copies.length;i++) copies[i].textContent=txt;   // textContent 自带转义，无需 esc()
  }
  fitMarquee(el);
}
/* 单行文本的「中段省略」：按实际宽度二分出能放下的最长长度，再渲染成 首…尾。
   （CSS 的 text-overflow 只能做尾省略；这里补中段省略，既保证不换行又留住首尾信息） */
function fitMiddle(el, txt){
  if(!el) return;
  if(txt!=null) el.dataset.full=String(txt);
  const full=el.dataset.full||'';
  el.title=full;
  el.textContent=full;
  if(el.clientWidth<=0) return;                       // 卡片处于收起态，等展开重排
  if(el.scrollWidth<=el.clientWidth+1) return;        // 放得下 → 原样显示
  let lo=6, hi=full.length;
  while(lo<hi){
    const mid=(lo+hi+1)>>1;
    el.textContent=midShort(full,mid);
    if(el.scrollWidth<=el.clientWidth+1) lo=mid; else hi=mid-1;
  }
  el.textContent=midShort(full,lo);
}
/* ── 走马灯引擎：JS 逐帧推进位移（不再靠 CSS animation）─────────────
   用户建议：render 每 2 秒跑一次，那就「记住位置、接着滚」。
   实现：offset 挂在元素自身（el._mq），由一条 rAF 统一推进 ——
     · render() 只改文字、不重建轨道 DOM → offset 天然保留，自动续播；
     · 文本变化导致轨道宽度变化时，offset **按比例**映射到新宽度，位置不跳；
     · 卡片被删除时靠 isConnected 自动出列，不会泄漏。 */
const MQ=[]; let MQLAST=0;
function mqRegister(el){
  if(!el) return null;
  if(!el._mq){ el._mq={off:0,w:0,speed:26,debt:0,on:false,cw:-1}; MQ.push(el); }
  return el._mq;
}
/* ⚠️ v1.0.38 二次平滑：上一版用「帧率 EMA × 乘数 comp」补偿掉帧，代价是每帧位移不再恒定——
   正常帧 0.44px、补偿帧 1.42px（实测 3.2× 波动），肉眼就是「匀速中突然窜一下」。
   改法：**欠账分摊**。真实时间应走多少先记账，单帧只走「钳制后的正常量」；被钳掉的部分存进
   debt，之后**每帧只多补一点点**（≈speed*0.012px）慢慢还完。于是——
     · 正常帧：debt=0 → 位移严格恒定（60fps 下每帧都是同一个数，零抖动）；
     · 掉帧后：单帧绝不突进，最多 2× 正常量，且 1 秒内平滑收敛回匀速。 */
/* ⚠️ 平滑演变：v1.0.37 把 dt 钳到 ≤50ms（原来 0.25s，掉一帧整条轨道猛跳），
   但用「帧率 EMA × 乘数 comp」补欠账 → 单帧位移中位 0.44px / 峰值 1.42px（3.2×），
   补偿本身又变成「匀速中突然窜一下」。
   v1.0.38 改为下面的**欠账分摊**：单帧恒定走「钳制后的正常量」，被钳掉的部分存进
   `debt` 由后续每帧小额摊还（MQREPAY）→ 正常帧位移严格恒定、掉帧后不突进。
   注：v1.0.37 那版遗留的「抖动」主因其实不在这里，而在 fitMarquee（见下）。 */
const MQCAP=0.05;                                                // 单帧最多按 50ms 计（掉帧不猛跳）
const MQREPAY=0.012;                                             // 欠账每帧摊还速率（≈0.31px@26px/s）
function mqStep(ts){
  const raw=MQLAST?(ts-MQLAST)/1000:0;
  MQLAST=ts;
  if(raw>0){
  for(let i=MQ.length-1;i>=0;i--){
    const el=MQ[i];
    if(!el.isConnected){ MQ.splice(i,1); continue; }    // 卡片已移除 → 出列
    const m=el._mq;
    if(!m||!m.on||m.w<=0) continue;
    let step=m.speed*Math.min(raw,MQCAP);              // 钳制后的「正常」步进（匀速下限）
    if(m.debt>0){                                      // 掉帧欠账：每帧只多补一小口，逐帧摊平
      const add=Math.min(m.debt, m.speed*MQREPAY);
      m.debt-=add; step+=add;
    }
    m.debt=Math.min(m.debt+m.speed*raw-step, m.speed*0.8);
    m.off+=step;
    if(m.off>=m.w){ m.off=m.off%m.w; m.debt=0; }       // 回绕后清欠账，别带着欠款冲过循环点
    const tr=el.firstElementChild;
    if(tr) tr.style.transform='translateX('+(-m.off).toFixed(2)+'px)';
  }
  }
  requestAnimationFrame(mqStep);
}
requestAnimationFrame(mqStep);
function fitMarquee(el, force){
  if(!el) return;
  const tr=el.firstElementChild, one=tr&&tr.firstElementChild;
  if(!one) return;
  const m=mqRegister(el);
  const cw=el.clientWidth;
  /* ⚠️ v1.0.38 早退：文字没变、容器宽度也没变 → 走马灯判定（on/w/class）必然不变，
     直接沿用上次结果，**省掉 scrollWidth 的强制同步布局**。此前每 tick 会为每张卡读一次
     scrollWidth（10+ 次 reflow），帧间隔因此出现 50~83ms 尖刺 —— 那才是「刷新时一顿」的主因。
     resize 时 clientWidth 变化 → 自动不早退，重新测量（无需显式 force）。 */
  if(!force && el.dataset.lpmq===el.dataset.txt && m.cw===cw) return;
  el.dataset.lpmq=el.dataset.txt; m.cw=cw;
  const w=one.scrollWidth||one.getBoundingClientRect().width;
  const over=w>cw+1;
  el.classList.toggle('mq', over);
  if(!over){ m.on=false; tr.style.transform=''; m.w=w; return; }
  /* ⚠️ v1.0.38：宽度变了只更新「循环周期 / 速度」，**offset 一动不动**。
     旧逻辑按比例映射 `off=off/m.w*w`：文本从「9 秒前」变「10 秒前」会让 w 1065→1074（+9px），
     off 跟着放大 0.84% —— off 已在 800px 时一次瞬移 +6.7px，肉眼就是「滚着滚着跳一格」。
     视觉位置只由 off 决定，off 不变 = 画面纹丝不动，这才是真正的「位置不跳」。 */
  m.w=w; m.on=true;
  m.speed=w/Math.max(9,Math.min(46,w/26));              // 等效旧实现：整条走完 9~46s，长文从容短文不慌
  if(m.off>=m.w) m.off=m.off%m.w;                      // 只有真滚出范围才回绕（双份文本无缝）
  tr.style.transform='translateX('+(-m.off).toFixed(2)+'px)';
}
function isActive(d){ return d.status==='running'&&!d.stale; }
function placeCard(d, parent, mini, seen){
  seen[d.id]=1;
  const c=CARDS[d.id]||buildCard(d);
  if(c.el.parentNode!==parent) parent.appendChild(c.el);   // 先迁移父级，再测量布局
  c.el.classList.toggle('mini', !!mini);
  renderCard(c,d);
}
function foldLabel(n, open){ return '已结束的任务 '+n+'（点击'+(open?'收起':'展开')+'）'; }
function render(p){
  if(!p) return;
  // 固定头栏里这行也必须单行（否则会把头栏撑成两行、高度抖动）→ 超长走马灯
  setMarquee(document.getElementById('hdr'),
             '时间 '+p.now+' · 面板端口 '+p.port+' · 采集自 ~/.workbuddy/live-progress/jobs.json');
  const s=p.summary||{};
  document.getElementById('chips').innerHTML=
    '<span class="chip run">运行中 <b>'+(s.active||0)+'</b></span>'+
    '<span class="chip">已结束 <b>'+(s.finished||0)+'</b></span>'+
    '<span class="chip bad">失败 <b>'+(s.failed||0)+'</b></span>'+
    '<span class="chip">任务总数 <b>'+(s.total||0)+'</b></span>';
  const grid=document.getElementById('grid');
  const fg=document.getElementById('foldgrid');
  const fold=document.getElementById('fold');
  const jobs=p.jobs||[];
  const act=jobs.filter(isActive), fin=jobs.filter(d=>!isActive(d));
  // 主区只显示活跃任务；其余收进折叠区（紧凑卡片）
  document.getElementById('empty').style.display=act.length?'none':'block';
  const seen={};
  act.forEach(d=>placeCard(d, grid, false, seen));
  fin.forEach(d=>placeCard(d, fg, true, seen));
  // 按 payload 顺序重排：placeCard 只在新卡/换区时才 appendChild，新完成的任务会被甩到
  // 折叠区末尾。appendChild = **移到末尾**，所以必须**正序**追加，最终 DOM 顺序才等于
  // payload 顺序（旧实现倒序追加，把整段顺序写反了：最新结束的反而排到最后）。
  // ⚠️ 只把「位置不对」的卡片挪到位，绝不无条件 appendChild。
  // 早期实现每轮都对所有卡片 appendChild —— 那是「先摘再挂」，会让卡片内正在跑的 CSS 动画
  // （元信息走马灯等）每 2 秒重启一次，视觉上就是不停跳闪（状态栏在卡片外故不受影响）。
  [[grid, act], [fg, fin]].forEach(function(pair){
    // ⚠️ CARDS[id] 是 {el, refs} 包装对象，重排要的是 DOM 元素本身（取 .el）
    const parent=pair[0], list=pair[1].map(function(d){ const c=CARDS[d.id]; return c?c.el:null; }).filter(Boolean);
    for(let i=0;i<list.length;i++){
      const cur=parent.children[i];
      if(cur!==list[i]) parent.insertBefore(list[i], cur||null);
    }
  });
  fold.style.display=fin.length?'block':'none';
  document.getElementById('foldtxt').textContent=foldLabel(fin.length, fold.open);
  Object.keys(CARDS).forEach(id=>{ if(!seen[id]){ CARDS[id].el.remove(); delete CARDS[id]; delete LAST[id]; delete DATA[id]; } });
}
/* 顶部/底部固定栏都是 position:fixed，正文必须留出等高 padding，否则首尾卡片会被压住。
   栏高不写死（窄屏胶囊会换行变高），这里量出实际高度回填 body padding。 */
function syncBars(){
  const tb=document.getElementById('topbar'), sb=document.getElementById('sysbar');
  // 正文与上下固定栏之间再留一段呼吸间距（宽屏稍宽、窄屏收紧），避免卡片贴着栏边
  const gap=window.innerWidth>=900?18:12;
  if(tb && tb.offsetHeight) document.body.style.paddingTop=(tb.offsetHeight+gap)+'px';
  if(sb && sb.offsetHeight) document.body.style.paddingBottom=(sb.offsetHeight+gap)+'px';
}
function relayout(){
  syncBars();
  // 尺寸变了要**整卡重排**：环式/条式是按卡片实际宽度决定的（折叠区展开后列宽才会确定），
  // 只重算指标格列数不够 —— 形态本身可能要从条式切成环式（反之亦然）。
  // renderCard 内部只改文字/属性、不重建走马灯轨道，重复调用是安全的。
  Object.keys(CARDS).forEach(id=>{ if(DATA[id]) renderCard(CARDS[id], DATA[id]); });
  fitMarquee(document.getElementById('hdr'));
  fitMarquee(document.getElementById('sysbar'));
}
/* ── 底部状态栏：本机 CPU / 内存（跟主轮询一起 2s 刷新）── */
function sbCls(v){ return v>=85?'bad':(v>=65?'warn':''); }
function sysSetAll(sel, txt){ document.querySelectorAll(sel).forEach(function(e){ e.textContent=txt; }); }
function setSys(s){
  if(!s) return;
  sysSetAll('.cpuv', s.cpu==null?'—':Math.round(s.cpu)+'%');
  sysSetAll('.memv', s.mem==null?'—':Math.round(s.mem)+'%');
  document.querySelectorAll('.cpub').forEach(function(b){
    if(s.cpu==null) return; b.style.width=Math.min(100,s.cpu)+'%'; b.className='cpub '+sbCls(s.cpu); });
  document.querySelectorAll('.memb').forEach(function(b){
    if(s.mem==null) return; b.style.width=Math.min(100,s.mem)+'%'; b.className='memb '+sbCls(s.mem); });
  sysSetAll('.sysmem', s.mem_used_gb==null?'':('已用 '+s.mem_used_gb+' / '+s.mem_total_gb+' GB'));
  sysSetAll('.systs', s.ts?('采样 '+s.ts):'');
  fitMarquee(document.getElementById('sysbar'));   // 数值位数会变，每次都重判是否该走马灯
}
async function tickSys(){
  const urls=['/api/sys','http://127.0.0.1:'+location.port+'/api/sys'];
  for(const u of urls){
    try{ const r=await fetch(u,{cache:'no-store'}); if(!r.ok) continue; setSys(await r.json()); return; }catch(e){}
  }
}
async function tick(){
  const urls=['/api/jobs','http://127.0.0.1:'+location.port+'/api/jobs'];
  for(const u of urls){
    try{
      const r=await fetch(u,{cache:'no-store'});
      if(!r.ok) continue;
      const p=await r.json();
      // 面板代码在本实例启动后改过（build 变）→ 直接重载，避免用户一直看到旧 UI
      if(p&&p.build&&p.build!==SBUILD){ location.reload(); return; }
      render(p);
      tickSys();
      return;
    }catch(e){}
  }
  setMarquee(document.getElementById('hdr'), '（实时刷新暂时不可用，显示的是首次加载的数据）');
}
let RAF=0;
function relayoutSoon(){ if(RAF) return; RAF=requestAnimationFrame(function(){ RAF=0; relayout(); }); }
const FOLD=document.getElementById('fold');
try{ if(localStorage.getItem('lp_fold')==='1') FOLD.open=true; }catch(e){}
FOLD.addEventListener('toggle',function(){
  try{ localStorage.setItem('lp_fold', FOLD.open?'1':'0'); }catch(e){}
  const t=document.getElementById('foldtxt');
  if(t) t.textContent=foldLabel(document.getElementById('foldgrid').children.length, FOLD.open);
  relayoutSoon();          // 展开/收起后卡片宽度变化 → 重排指标格
});
render(INIT); relayout();
// 「清空已结束」：只清非活跃记录，运行中的任务不动
const CLR=document.getElementById('clearfin');
if(CLR){
  CLR.addEventListener('click',function(e){
    e.preventDefault(); e.stopPropagation();      // 别触发 <details> 展开/收起
    armConfirm(CLR, '清空已结束', async function(){
      let out={};
      try{ out=await postJSON('/api/clear-finished',{}); }catch(err){}
      if(out.ok){ tick(); } else { CLR.textContent='清空失败'; setTimeout(function(){ CLR.textContent='清空已结束'; },1600); }
    });
  });
}
window.addEventListener('resize', relayoutSoon);
if(window.ResizeObserver){
  try{
    const ro=new ResizeObserver(relayoutSoon);
    ro.observe(document.getElementById('grid'));
    ro.observe(document.getElementById('foldgrid'));
  }catch(e){}
}
/* 状态栏：克隆一份内容用于无缝走马灯，并用首屏数据立即渲染（不等第一次 2s 轮询） */
(function(){
  const sb=document.getElementById('sysbar');
  if(!sb) return;
  const tr=sb.querySelector('.mtrack'), one=tr&&tr.querySelector('.mtxt');
  if(tr&&one&&!tr.querySelector('.mtxt:nth-child(2)')){
    const c=one.cloneNode(true); c.setAttribute('aria-hidden','true'); tr.appendChild(c);
  }
  setSys(SYSINIT);
})();
setInterval(tick,1000); tick();   /* v1.0.36：2s→1s，尽量跟手（后端同步已 1s 限流）*/
</script></body></html>
"""

# 页面代码指纹：服务端一重启（PAGE 有改动）指纹就变，旧标签页下次轮询即自动重载。
BUILD = hashlib.md5(PAGE.encode("utf-8")).hexdigest()[:10]


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, body, ctype):
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        LAST_HIT[0] = time.time()      # 任何请求都算"有人在使用"，看门狗据此判空闲
        path = self.path.split("?")[0]
        if path in ("/", "/index.html"):
            try:
                initial = json.dumps(payload(), ensure_ascii=False)
            except Exception as e:
                initial = json.dumps({"error": str(e)}, ensure_ascii=False)
            try:
                sysinit = json.dumps(sys_payload(), ensure_ascii=False)
            except Exception:
                sysinit = "null"
            self._send(PAGE.replace("__INITIAL__", initial)
                           .replace("__LOGRESUME__", str(LOG_RESUME_SEC[0]))
                           .replace("__BUILD__", BUILD)
                           .replace("__TITLE__", PANEL_TITLE)
                           .replace("__FAVICON__", PANEL_FAVICON)
                           .replace("__H1TEXT__", H1_TEXT)
                           .replace("__SYS__", sysinit)
                           .encode("utf-8"), "text/html; charset=utf-8")
        elif path == "/api/jobs":
            self._send(json.dumps(payload(), ensure_ascii=False).encode("utf-8"),
                       "application/json; charset=utf-8")
        elif path == "/api/sys":
            # 底部状态栏：本机 CPU / 内存（纯 stdlib 采样，取不到就返回 null）
            self._send(json.dumps(sys_payload(), ensure_ascii=False).encode("utf-8"),
                       "application/json; charset=utf-8")
        else:
            self.send_response(404)
            self.end_headers()

    # 面板对业务数据只读；下面两个接口是**用户主动的清理动作**，一律转交登记库
    # （progress.py 是注册表唯一写方，自带跨进程锁），面板不自己写 jobs.json。
    def do_POST(self):
        LAST_HIT[0] = time.time()
        path = self.path.split("?")[0]
        try:
            n = int(self.headers.get("Content-Length") or 0)
            data = json.loads(self.rfile.read(n) or b"{}") if n else {}
            if not isinstance(data, dict):
                data = {}
        except Exception:
            data = {}
        try:
            if path == "/api/delete":
                jid = str(data.get("id") or "").strip()
                removed = P.delete_jobs([jid], force=bool(data.get("force")))
                out = {"ok": bool(removed), "removed": removed}
            elif path == "/api/clear-finished":
                out = {"ok": True, "removed_count": P.delete_finished()}
            else:
                self.send_response(404)
                self.end_headers()
                return
            self._send(json.dumps(out, ensure_ascii=False).encode("utf-8"),
                       "application/json; charset=utf-8")
        except Exception as e:
            self._send(json.dumps({"ok": False, "error": str(e)},
                                  ensure_ascii=False).encode("utf-8"),
                       "application/json; charset=utf-8")


def _already_running(port, timeout=1.5):
    """端口上是否已有一个可用的面板（用于双击启动的幂等判断）"""
    import socket as _s
    s = _s.socket()
    s.settimeout(timeout)
    if s.connect_ex(("127.0.0.1", port)) != 0:
        s.close()
        return False
    s.close()
    try:
        import urllib.request
        op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        r = op.open("http://127.0.0.1:%d/api/jobs" % port, timeout=timeout)
        return b"summary" in r.read(2000)
    except Exception:
        return False


def _log(msg):
    """pythonw.exe 下 sys.stderr/stdout 都是 None，直接写会抛异常——统一走这里。"""
    for stream in (sys.stderr, sys.stdout):
        try:
            if stream is not None:
                stream.write(msg + "\n")
                stream.flush()
                return
        except Exception:
            pass


def _watchdog(srv, idle_exit=0.0, ttl=0.0):
    """自我收尾看门狗——保证面板**永远不会长期占用会话**。

    两条退出条件（都为 0 则永不退出，即常驻模式）：
      · ttl        绝对存活上限，到点即退（防呆，避免任何形式的僵尸面板）；
      · idle_exit  连续 idle_exit 秒既无 HTTP 请求、又无运行中任务 → 退出。

    为什么需要它：面板是长驻服务。若它由会话内的后台任务启动，会话就会一直等它结束，
    表现为「任务早就跑完了，会话却永远处于未完成状态」。idle_exit 是最后一道保险：
    用户不再看、也没有任务在跑时，面板自己安静退出，把会话释放掉。
    """
    while True:
        time.sleep(10)
        reason = None
        try:
            now = time.time()
            if ttl and now - START_TS[0] > ttl:
                reason = "已达最大存活时间 %.0f 分钟" % (ttl / 60.0)
            elif idle_exit and now - LAST_HIT[0] > idle_exit:
                try:
                    running = len(P.list_jobs(only_running=True))
                except Exception:
                    running = 0
                if running == 0:
                    reason = "已空闲 %.0f 分钟且无运行中任务" % (idle_exit / 60.0)
        except Exception:
            reason = None
        if not reason:
            continue
        # 退出动作必须在 try 之外：日志本身失败也绝不能挡住 shutdown
        _log("[live-panel] 自动退出：%s（端口 %s）" % (reason, PORT[0]))
        try:
            srv.shutdown()
        except Exception:
            pass
        return


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8791)
    ap.add_argument("--log-resume-sec", type=float, default=8.0,
                    help="日志区用户上滑后，静默多少秒无操作自动滚回底部（默认 8；0=禁用自动恢复）")
    ap.add_argument("--idle-exit", type=float, default=0.0,
                    help="空闲多少秒后自动退出（无 HTTP 请求且无运行中任务；默认 0=不退出）")
    ap.add_argument("--ttl", type=float, default=0.0,
                    help="最大存活秒数，到点强制退出（默认 0=不限）")
    args = ap.parse_args()
    PORT[0] = args.port
    LOG_RESUME_SEC[0] = args.log_resume_sec
    START_TS[0] = LAST_HIT[0] = time.time()
    os.makedirs(P.ROOT, exist_ok=True)

    # 幂等：端口上已有面板就直接复用，不再起第二个实例（双击启动脚本会走到这里）
    if _already_running(args.port):
        print("面板已在运行: http://127.0.0.1:%d/  （本次启动自动跳过）" % args.port, flush=True)
        return 0

    try:
        srv = ThreadingHTTPServer(("127.0.0.1", args.port), H)
    except OSError as e:
        print("启动失败：端口 %d 被其他程序占用（%s）。换端口：--port 8792" % (args.port, e), flush=True)
        return 1
    SRV[0] = srv                      # 供热更新使用（exec 前要关监听套接字）
    CODE_FP[0] = _code_fingerprint()  # 以「本次启动时的代码」为基线
    load_samples()
    threading.Thread(target=sample_loop, daemon=True).start()
    if args.idle_exit or args.ttl:
        threading.Thread(target=_watchdog, args=(srv, args.idle_exit, args.ttl),
                         daemon=True).start()
    print("通用任务执行进度·实时面板: http://127.0.0.1:%d/  (jobs=%s%s)"
          % (args.port, P.JOBS,
             "，空闲 %.0f 分钟自动退出" % (args.idle_exit / 60.0) if args.idle_exit else ""),
          flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    # --build：只打印当前代码的页面指纹并退出（panel_ctl 用它判断「跑着的是不是旧版」）
    if "--build" in sys.argv[1:]:
        print(BUILD, flush=True)
        sys.exit(0)
    sys.exit(main() or 0)
