# -*- coding: utf-8 -*-
"""通用实时进度 · 任务登记库（客户端库 + CLI）

任何脚本/命令都能把自己的进度写进一个共享注册表，面板（live_panel.py）只读该注册表。
注册表位置: ~/.workbuddy/live-progress/jobs.json   （可用环境变量 LIVE_PROGRESS_DIR 覆盖）

作为库使用（推荐给 Python 脚本）:
    from progress import Progress
    with Progress("镜像 KB1→KB2", total=3286, unit="张") as p:
        for i, item in enumerate(items, 1):
            do_work(item)
            if i % 10 == 0:
                p.set(i, message=f"已镜像 {i}")
        # 退出 with 自动标记完成；异常则自动标记失败

作为 CLI 使用（给 bat / shell / 其他语言）:
    python progress.py start  --title "导出报表" --total 120 [--id myjob] [--unit 条]
    python progress.py set    --id myjob --done 30 --msg "正在导出 30/120"
    python progress.py inc    --id myjob [--n 1] [--msg "..."]
    python progress.py log    --id myjob --msg "..."
    python progress.py finish --id myjob [--failed] [--msg "..."]
    python progress.py finish-session [--session <id>]        # 收尾：结束本会话的 sess-<id> 卡（任务执行完必调）
    python progress.py list   [--running] [--json]
    python progress.py plan   --id myjob --sec 90            # 不可细分任务：声明预估总耗时
    python progress.py delete --id myjob [--force]           # 删一条记录（活跃任务默认受保护）
    python progress.py clear  [--finished|--all]             # 清空记录
"""
import os
import sys
import json
import time
import argparse
import contextlib
import hashlib
import re

try:  # 让 Windows 控制台也能print中文
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

ROOT = os.environ.get("LIVE_PROGRESS_DIR") or os.path.join(
    os.path.expanduser("~"), ".workbuddy", "live-progress")
JOBS = os.path.join(ROOT, "jobs.json")
LOCK = os.path.join(ROOT, "jobs.lock")
PANEL_PORT = int(os.environ.get("LIVE_PROGRESS_PORT") or 8791)
MAX_JOBS = 40          # 注册表最多保留条数（按更新时间）
MAX_LOG = 40           # 每个任务保留的日志行数
STALE_AFTER = 900      # 秒：running 任务多久无更新视为"疑似已结束"
GC_ABANDON_AFTER = 1800  # 秒：会话卡 30 分钟无更新 → 判定「异常中断」，兜底收尾（只动 sess-*）


# ---------------------------------------------------------------- 基础读写
def _ensure_dir():
    os.makedirs(ROOT, exist_ok=True)


# ---------------------------------------------------------------- 文件锁原语
# 锁文件**常驻**（只加锁/解锁，绝不删除），避免高频写产生海量删除操作。
try:
    import msvcrt
    _LOCK_IMPL = "msvcrt"

    def _lock_acquire(fd):
        try:
            os.lseek(fd, 0, os.SEEK_SET)
        except Exception:
            pass
        try:
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)   # 非阻塞加锁
            return True
        except OSError:
            return False

    def _lock_release(fd):
        try:
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        except Exception:
            pass
except ImportError:                                   # POSIX
    import fcntl
    _LOCK_IMPL = "fcntl"

    def _lock_acquire(fd):
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except OSError:
            return False

    def _lock_release(fd):
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except Exception:
            pass


@contextlib.contextmanager
def _lock(timeout=5.0):
    """跨进程写互斥。

    用操作系统级文件锁（Windows msvcrt / POSIX fcntl）锁同一个**常驻**锁文件：
    不再"创建-删除"锁文件，因为高频写（如进度桥接）会删出成千上万个锁文件，
    撞上本机安全策略（单轮删除 >50 次即拦截）导致进程被杀。
    """
    _ensure_dir()
    fd = os.open(LOCK, os.O_CREAT | os.O_RDWR)
    try:
        if os.fstat(fd).st_size == 0:
            os.write(fd, b"0")            # msvcrt 需要至少 1 字节才能加锁
        t0 = time.time()
        got = False
        while True:
            if _lock_acquire(fd):
                got = True
                break
            if time.time() - t0 > timeout:
                break                     # 超时放弃加锁，宁可丢一次更新也不阻塞调用方
            time.sleep(0.01)              # 细粒度重试：高并发下吞吐更好
        try:
            yield
        finally:
            if got:
                _lock_release(fd)
    finally:
        os.close(fd)


def load_jobs():
    for _ in range(3):
        try:
            with open(JOBS, encoding="utf-8") as f:
                d = json.load(f)
            return d if isinstance(d, dict) else {}
        except FileNotFoundError:
            return {}
        except Exception:
            time.sleep(0.05)          # 偶发半截文件 → 稍等重读，避免误判为空
    return {}


def _safe_save(jobs):
    """进度是旁路功能：保存失败只告警，绝不抛异常拖垮调用它的长任务。"""
    try:
        return save_jobs(jobs)
    except Exception as e:
        try:
            sys.stderr.write("[progress] 保存失败（忽略，不影响主任务）: %s\n" % e)
        except Exception:
            pass
        return None


def _sweep_tmp(max_remove=5, min_age=300):
    """顺手清掉别的进程留下的半截 tmp 文件（只在成功替换后调用；有数量上限，
    避免撞上本机"单轮删除过多文件"的安全拦截）。"""
    try:
        d = os.path.dirname(JOBS)
        base = os.path.basename(JOBS) + "."
        now = time.time()
        n = 0
        for fn in os.listdir(d):
            if n >= max_remove:
                break
            if fn.startswith(base) and fn.endswith(".tmp"):
                p = os.path.join(d, fn)
                try:
                    if now - os.path.getmtime(p) > min_age:
                        os.remove(p)
                        n += 1
                except Exception:
                    pass
    except Exception:
        pass


def save_jobs(jobs):
    _ensure_dir()
    keep = sorted(jobs.items(), key=lambda kv: kv[1].get("updated_at", 0), reverse=True)[:MAX_JOBS]
    data = dict(keep)
    # 临时文件名**带 pid**：固定名会在多进程并发写时互相踩踏（Windows: os.replace WinError 5）
    tmp = "%s.%d.tmp" % (JOBS, os.getpid())
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    last = None
    for i in range(6):                       # 目标文件正被读取/替换时 Windows 会短暂拒绝，退避重试
        try:
            os.replace(tmp, JOBS)            # 原子替换，避免面板读到半截文件
            _sweep_tmp()
            return data
        except OSError as e:
            last = e
            time.sleep(0.03 * (i + 1))
    try:
        os.remove(tmp)
    except Exception:
        pass
    raise last if last else RuntimeError("save_jobs: replace failed")


def new_id(seed=""):
    if seed:
        return hashlib.md5(seed.encode("utf-8")).hexdigest()[:10]
    return "j%s" % hashlib.md5(("%s%.3f" % (os.getpid(), time.time())).encode()).hexdigest()[:8]


# ---------------------------------------------------------------- 标题清洗（v1.0.38）
# hook_prompt 拿到的是「用户原文 + WB 注入的 XML 片段」拼在一起（后台任务通知
# <task-notification>…</task-notification>、<command-name>、<local-command-stdout> 等），
# 直接截 60 字当卡片标题 → 面板上出现一整串代码。这里把注入块整块剥掉再截断。
_INJECT_TAGS = ("task-notification", "system-reminder", "command-name",
                "local-command-stdout", "local-command-stderr", "antml:param", "tool_use")
_NOISE_KV = re.compile(r"\b(?:task[-_]?id|task[-_]?name|session[-_]?id|hook[-_]?type|status)\s*[:=]\s*[^\s,;>]+",
                       re.I)
_PUNCT = " \t\r\n·—-–|、,，。;；:："


def clean_title(text, limit=44):
    """把「用户原文 + 注入 XML」清洗成一句人话，供卡片标题 / 详情页使用。"""
    t = str(text or "").strip()
    #   `…</task-notification>` **或到字符串末尾**（未闭合的注入块要连内部文本一起带走，
    #   否则残留 `2cjst9complete` 这种碎片）
    for tag in _INJECT_TAGS:
        t = re.sub(r"<\s*%s\b[\s\S]*?(?:</\s*%s\s*>|$)" % (tag, tag), " ", t,
                   flags=re.S | re.I)
    t = re.sub(r"<[^>]{0,200}>", " ", t)          # 兜底：剩余尖括号块（含未闭合的注入标签）
    t = _NOISE_KV.sub("", t)                       # task-id=… / status=complete 之类残留
    t = re.sub(r"\s*[-–—|]{2,}\s*", " ", t)
    t = re.sub(r"\s+", " ", t).strip(_PUNCT)
    if not t:
        t = "用户请求"
    return t[:limit] + ("…" if len(t) > limit else "")


def register(title, total=None, unit="项", job_id=None, source="script",
             session_id=None, cwd=None, cmd=None, log_file=None, message=None,
             planned_sec=None, received_at=None, steps=None, round_reset=False):
    """登记（或覆盖）一个任务，返回 job_id

    planned_sec: 预估总耗时（秒）。**不可细分的长任务**（如"整段提交给远端、
    中途拿不到任何进度"）专用：面板无法从 done 增量算出速率/ETA，就退化为
    按已运行时长 / 预估总时长 折算的时间基线。留空则面板不做时间兜底。

    received_at: **接到用户命令的时刻**（浮点时间戳）。用于面板「时间线」第一行
    「📥 接到命令 HH:MM:SS」。缺省 = now；若同一 job_id 已登记过则**保留旧的**
    received_at（代理"接管"一张在 UserPromptSubmit 阶段建好的会话卡时，
    真正的"接到命令"时间不会被覆盖）。

    round_reset: **新回合重置计时**（v1.0.30）。True 时把 received_at / started_at
    重置为 now、current_step 归零——用于 hook_prompt 在**用户提交新命令**时复用
    同一张 sess-<session_id> 卡的场景：每个回合都是新一轮执行，「接到命令 / 已运行」
    必须从本回合起算，否则会把上一轮的耗时累计进来（曾出现「接到命令 08:45、
    已运行 10h15m」，实际新命令刚到）。代理中途接管卡片时**不要**传这个参数。
    steps: 步骤名列表（如 ["准备","处理","收尾"]），用于面板「步骤进度条」。
    """
    jid = job_id or new_id(title + str(time.time()))
    with _lock():
        jobs = load_jobs()
        now = time.time()
        old = jobs.get(jid, {})
        # steps 统一规整为列表：CLI 传来的是逗号字符串，旧记录可能是 list/字符串/缺失
        raw_steps = steps if steps is not None else old.get("steps")
        if isinstance(raw_steps, str):
            norm_steps = [s.strip() for s in raw_steps.split(",") if s.strip()]
        elif isinstance(raw_steps, (list, tuple)):
            norm_steps = [str(s).strip() for s in raw_steps if str(s).strip()]
        else:
            norm_steps = None
        # 日志基线：保留旧日志；本次若显式接入步骤，补一行「接入 N 步」——让「只上报步骤、
        # 不单独写日志」的任务（如定时任务 begin --steps）也能在日志区看到东西（不再是 0 行）。
        base_log = list(old.get("log") or [])
        if norm_steps and steps is not None:
            base_log.append("%s  接入 %d 步：%s" % (time.strftime("%H:%M:%S"),
                                                  len(norm_steps), " / ".join(norm_steps[:8])))
            del base_log[:-MAX_LOG]
        jobs[jid] = {
            "id": jid,
            "title": title or old.get("title") or jid,
            "status": "running",
            "done": int(old.get("done") or 0),
            "total": total if total is not None else old.get("total"),
            "unit": unit or old.get("unit") or "项",
            "message": message or "已启动",
            "source": source or old.get("source") or "script",
            "session_id": session_id or old.get("session_id"),
            "cwd": cwd or old.get("cwd") or os.getcwd(),
            "cmd": cmd or old.get("cmd"),
            "log_file": log_file or old.get("log_file"),
            "log": base_log,
            "planned_sec": (float(planned_sec) if planned_sec
                            else old.get("planned_sec")),
            "received_at": (float(received_at) if received_at is not None
                            else (now if round_reset else old.get("received_at"))) or now,
            "started_at": (now if round_reset else old.get("started_at")) or now,
            "steps": norm_steps,
            "current_step": 0 if round_reset else old.get("current_step", 0),
            # manual_steps=True 表示用户/代理**主动规划**了步骤（begin --steps / steps）。
            # 该模式下自动映射 hook 只把工具动作记进日志、不再追加步骤，避免打乱手动步骤条。
            # 判定：显式给了非空 steps 列表 → True；否则沿用旧值；显式给空列表 → False（重置为自动模式）。
            "manual_steps": (bool(norm_steps) if steps is not None
                             else (old.get("manual_steps") or False)),
            # 本回合是否已用 present_files 打开过面板（hook_auto_step 检测 present_files
            # 载荷里含面板地址时打点；hook_stop 依据它决定「自动收卡」还是「拦截并要求打开」。
            # round_reset 清空 = 新回合重新判定；register 整体重建 dict，必须显式保留旧值）
            "panel_presented_at": (None if round_reset else old.get("panel_presented_at")),
            "updated_at": now,
            "ended_at": None,
        }
        _safe_save(jobs)
    return jid


def set_steps(job_id, steps):
    """设置/替换步骤列表（如 "准备,处理,收尾" 或 ["准备","处理","收尾"]）。"""
    if isinstance(steps, (list, tuple)):
        names = [str(s).strip() for s in steps if str(s).strip()]
    else:
        names = [s.strip() for s in str(steps).split(",") if s.strip()]
    with _lock():
        jobs = load_jobs()
        j = jobs.get(job_id)
        if j is None:
            return None
        j["steps"] = names
        if j.get("current_step") is None or j.get("current_step") < 0:
            j["current_step"] = 0
        j["manual_steps"] = True        # 显式设置步骤列表 = 主动规划，自动映射退居日志
        log = j.setdefault("log", [])
        log.append("%s  计划 %d 步：%s" % (time.strftime("%H:%M:%S"), len(names),
                                          " / ".join(names[:8])))
        del log[:-MAX_LOG]
        j["updated_at"] = time.time()
        _safe_save(jobs)
    return j


def set_step(job_id, index, name=None):
    """推进到指定步骤（0 基）；可选顺便改该步骤名。

    步骤**真的推进**时自动写一行日志（"▶ 第 i/N 步 xxx"）——修「只上报步骤、不写明细
    日志的任务，日志区永远 0 行」的问题（典型：定时任务用 `begin --steps` + `step --index` 接入）。
    """
    idx = int(index)
    with _lock():
        jobs = load_jobs()
        j = jobs.get(job_id)
        if j is None:
            return None
        steps = j.get("steps") or []
        if name and 0 <= idx < len(steps):
            steps[idx] = str(name)
        old_idx = j.get("current_step")
        j["current_step"] = idx
        if idx != old_idx:                    # 只有真的推进了才记，避免重复推进刷屏
            nm = steps[idx] if 0 <= idx < len(steps) else ""
            log = j.setdefault("log", [])
            log.append("%s  ▶ 第%d/%d步 %s" % (time.strftime("%H:%M:%S"),
                                               idx + 1, max(len(steps), 1), nm))
            del log[:-MAX_LOG]
        j["updated_at"] = time.time()
        _safe_save(jobs)
    return j


def mark_start(job_id, started_at=None):
    """标记「开始执行」时刻（浮点时间戳，缺省 now）。接到命令≠开始执行时用于补记。"""
    ts = float(started_at) if started_at is not None else time.time()
    with _lock():
        jobs = load_jobs()
        j = jobs.get(job_id)
        if j is None:
            return None
        j["started_at"] = ts
        j["updated_at"] = time.time()
        _safe_save(jobs)
    return j


def update(job_id, done=None, total=None, message=None, status=None, unit=None,
           log_line=None, planned_sec=None):
    """更新任务；任务不存在时自动补建，避免调用方先 register 的负担"""
    with _lock():
        jobs = load_jobs()
        now = time.time()
        j = jobs.get(job_id)
        if j is None:
            j = {"id": job_id, "title": job_id, "status": "running", "done": 0, "total": None,
                 "unit": "项", "message": "", "source": "script", "session_id": None,
                 "cwd": os.getcwd(), "cmd": None, "log_file": None, "log": [],
                 "planned_sec": None,
                 "started_at": now, "updated_at": now, "ended_at": None}
            jobs[job_id] = j
        if done is not None:
            j["done"] = int(done)
        if total is not None:
            j["total"] = int(total)
        if unit:
            j["unit"] = unit
        if planned_sec:
            j["planned_sec"] = float(planned_sec)
        if message:
            j["message"] = str(message)
            log = j.setdefault("log", [])
            log.append("%s  %s" % (time.strftime("%H:%M:%S"), message))
            del log[:-MAX_LOG]
        if log_line:
            log = j.setdefault("log", [])
            log.append(str(log_line).rstrip())
            del log[:-MAX_LOG]
        if status:
            j["status"] = status
            if status in ("done", "failed", "aborted"):
                j["ended_at"] = now
                if j.get("total") and status == "done":
                    j["done"] = j["total"]
        elif j.get("status") == "aborted":
            # 会话复活：曾被兜底判「异常中断」的卡又来活动了（并行会话/窗口还在），
            # 自动回 running，免得真在跑的卡被永久钉成「异常中断」。
            j["status"] = "running"
            j["ended_at"] = None
        j["updated_at"] = now
        _safe_save(jobs)
        return j


def finish(job_id, ok=True, message=None):
    return update(job_id, status="done" if ok else "failed", message=message)


def mark_panel_presented(session_id):
    """打点：本回合代理已用 present_files 打开过面板（hook_auto_step 检测到调用时触发）。

    hook_stop 据此判定：回合结束 → 已打开 → 自动收卡；未打开 → 拦截并要求先打开。
    """
    jid = "sess-" + (session_id or "default")
    with _lock():
        jobs = load_jobs()
        j = jobs.get(jid)
        if j is None or j.get("status") != "running":
            return False
        j["panel_presented_at"] = time.time()
        j["updated_at"] = time.time()
        _safe_save(jobs)
        return True


def finish_session(session_id=None, ok=True, message=None):
    """**主动收尾会话卡**（任务执行完必须调用，别让进度卡挂着 running）。

    收尾范围只限会话卡（id 前缀一律 `sess-`），**绝不碰** `bg-*` 后台卡与脚本自建的业务卡
    （那些由各自的 TaskOutput / with 退出自动收尾）。
    - `session_id` 给了 → 只结束 `sess-<session_id>`；
    - 不给 → 结束**所有运行中的 `sess-*` 卡**（agent 拿不到 session id 时的兜底）。
    返回被结束的 id 列表（已结束的卡不动、不重复计入）。
    """
    want = ("sess-" + str(session_id)) if session_id else None
    ended = []
    for j in list_jobs(only_running=True):
        jid = j.get("id") or ""
        if not jid.startswith("sess-"):
            continue
        if want and jid != want:
            continue
        update(jid, status="done" if ok else "failed",
               message=message or "任务执行完毕 · 主动收尾")
        ended.append(jid)
    return ended


# ---------------------------------------------------------------- WB 权威状态同步
WB_DB = os.path.join(os.path.expanduser("~"), ".workbuddy", "workbuddy.db")
_WB_SYNC_LAST = 0.0

# workbuddy.db sessions.status → 会话卡收尾动作；working = 正在执行，保持 running
_WB_STATUS_MAP = {
    "completed": ("done", "WB 会话已完成·自动收尾"),
    "error": ("failed", "WB 会话状态 error·自动收尾"),
    "terminated": ("aborted", "WB 会话已终止·自动收尾"),
    "archived": ("done", "WB 会话已归档·自动收尾"),
}


def _wb_session_states():
    """读 WB 会话权威状态（~/.workbuddy/workbuddy.db · sessions 表，**只读**连接）。

    WB 界面上每个会话的「执行中/已完成」就来自这里：status ∈
    working / completed / error / terminated / archived。WAL 模式并发读安全；
    任何异常返回 None（db 不存在/被锁/表结构变化），调用方静默跳过、交给 gc 兜底。
    """
    try:
        import sqlite3
        con = sqlite3.connect("file:%s?mode=ro" % WB_DB.replace("\\", "/"),
                              uri=True, timeout=1.0)
        try:
            rows = con.execute("select id, status, deleted_at from sessions").fetchall()
        finally:
            con.close()
        return {r[0]: (r[1], r[2]) for r in rows}
    except Exception:
        return None


def sync_wb_sessions(min_gap=1.0, dry=False):
    """**WB 权威状态同步（v1.0.33）**：把 `sess-*` 会话卡与 WB 自己的会话状态对表。

    动机：hook 只知道「回合边界」，判断不了会话是否真的在执行——
    Stop 收尾会把「回合间隙」误报成完成（v1.0.31 被用户打回），不收尾又会让
    已完成的会话卡永远挂 running（v1.0.32 被用户打回）。WB 在 workbuddy.db 的
    sessions.status 里维护了每个会话的实时运行状态（界面上「执行中/已完成」的同源
    数据），直接以它为准：
    - working → 保持 running（正在执行，不收）；
    - completed / archived（或 deleted_at 非空）→ done；error → failed；terminated → aborted；
    - db 里查不到的会话 id → 不动（老数据/异常路径，交给 gc_abandoned 兜底）。
    面板采样循环每拍调用（内部 1s 限流，v1.0.36 从 5s 下调：用户反馈会话状态反馈太慢）→
    WB 状态一变，面板 ≤2s 内跟上。
    `dry=True` 只报告不落盘。返回收尾的 job id 列表。
    """
    global _WB_SYNC_LAST
    now = time.time()
    if not dry and now - _WB_SYNC_LAST < min_gap:
        return []
    _WB_SYNC_LAST = now
    states = _wb_session_states()
    if not states:
        return []
    swept = []
    with _lock():
        jobs = load_jobs()
        for jid, j in jobs.items():
            if not jid.startswith("sess-") or j.get("status") != "running":
                continue
            st = states.get(j.get("session_id") or "")
            if st is None:
                continue
            wb_status, deleted = st
            if wb_status == "working" and not deleted:
                continue
            if deleted:
                act = ("done", "WB 会话已删除·自动收尾")
            else:
                act = _WB_STATUS_MAP.get(wb_status)
            if act is None:
                continue                      # working 或未知状态：不动
            if not dry:
                update(jid, status=act[0], message=act[1])
            swept.append(jid)
    return swept


def read_stdin_json():
    """读 hook 的 stdin JSON——**必须显式按 UTF-8 解码**。
    坑：中文 Windows 下 Python 的 stdin 默认用本地编码（cp936），而宿主写进来的是 UTF-8，
    直接 `sys.stdin.read()` 会把中文解成「宸茬粡…」这类乱码，卡片标题/日志就全废了。
    全部异常吞掉返回 {}，保证 hook 绝不影响会话。
    """
    try:
        raw = sys.stdin.buffer.read() if hasattr(sys.stdin, "buffer") else sys.stdin.read().encode()
    except Exception:
        return {}
    try:
        txt = raw.decode("utf-8", errors="replace")
    except Exception:
        return {}
    try:
        return json.loads(txt) if txt.strip() else {}
    except Exception:
        return {}


def fix_mojibake(s):
    """修「UTF-8 字节被按 GBK 解码」造成的乱码（Windows 中文环境 hook 的经典坑）。

    判定：字符串能 `encode('gbk')` 再 `decode('utf-8')` 且结果不同 → 认定是乱码并还原；
    正常中文几乎不可能同时满足这两条（会抛 UnicodeDecodeError），所以误伤极低。
    """
    if not s or not isinstance(s, str):
        return s
    try:                                   # 整串可还原 → 直接回
        fixed = s.encode("gbk").decode("utf-8")
        if fixed != s:
            return fixed
    except Exception:
        pass
    # 串里混了 GBK 表达不了的字符（…、→、emoji）→ 按「能否编码为 GBK」切段逐段还原
    out, buf = [], []

    def flush():
        if not buf:
            return
        seg = "".join(buf)
        del buf[:]
        try:
            seg = seg.encode("gbk").decode("utf-8")
        except Exception:
            pass
        out.append(seg)

    for ch in s:
        try:
            ch.encode("gbk")
            buf.append(ch)
        except Exception:
            flush()
            out.append(ch)
    flush()
    return "".join(out)


def repair_mojibake(dry=False):
    """批量修注册表里的乱码字段（title / message / cmd / log / steps）。

    返回 [{"id":…, "field":…, "before":…, "after":…}, …]；`dry=True` 只报告不落盘。
    """
    touched = []
    with _lock():
        jobs = load_jobs()
        for jid, j in jobs.items():
            for field in ("title", "message", "cmd"):
                v = j.get(field)
                if isinstance(v, str):
                    nv = fix_mojibake(v)
                    if nv != v:
                        touched.append({"id": jid, "field": field, "before": v, "after": nv})
                        if not dry:
                            j[field] = nv
            log = j.get("log")
            if isinstance(log, list):
                nl = []
                for line in log:
                    if isinstance(line, str):
                        fx = fix_mojibake(line)
                        if fx != line:
                            touched.append({"id": jid, "field": "log",
                                            "before": line[:60], "after": fx[:60]})
                        nl.append(fx)
                    else:
                        nl.append(line)
                if not dry and nl != log:
                    j["log"] = nl
            steps = j.get("steps")
            if isinstance(steps, list):
                ns = []
                for st in steps:
                    if isinstance(st, dict) and isinstance(st.get("name"), str):
                        fx = fix_mojibake(st["name"])
                        if fx != st["name"]:
                            touched.append({"id": jid, "field": "step",
                                            "before": st["name"][:60], "after": fx[:60]})
                        st = dict(st, name=fx)
                    ns.append(st)
                if not dry and ns != steps:
                    j["steps"] = ns
        if touched and not dry:
            _safe_save(jobs)
    return touched


def gc_abandoned(min_idle=GC_ABANDON_AFTER, as_failed=False, dry=False):
    """**异常中断兜底**：把长时间无更新的 `sess-*` 会话卡收尾（默认状态 `aborted`）。

    ⚠️ 定位：只兜「会话异常中断（崩溃/窗口被关/进程被杀）」这一种情况——
    正常路径永远是会话自己 `finish-session` 主动收尾，本函数不是偷懒的替代品。
    只动会话卡（id 前缀 `sess-`），**绝不碰** `bg-*` 后台卡与脚本自建业务卡。
    返回命中列表 [{"id":…, "idle_min":…}]；`dry=True` 只报告不落盘。
    """
    now = time.time()
    hit = []
    for j in list_jobs(only_running=True):
        jid = j.get("id") or ""
        if not jid.startswith("sess-"):
            continue
        base = j.get("updated_at") or j.get("started_at") or now
        idle = now - base
        if idle < min_idle:
            continue
        rec = {"id": jid, "idle_min": round(idle / 60, 1)}
        if not dry:
            update(jid, status="failed" if as_failed else "aborted",
                   message="会话异常中断·兜底收尾（无更新 %.0f 分钟）" % (idle / 60))
        hit.append(rec)
    return hit


def list_jobs(only_running=False):
    jobs = list(load_jobs().values())
    if only_running:
        jobs = [j for j in jobs if j.get("status") == "running"]
    jobs.sort(key=lambda j: j.get("updated_at", 0), reverse=True)
    return jobs


def _is_active(j):
    """活跃 = 运行中且未超时失联（与面板口径一致），删除时保护它们"""
    if j.get("status") != "running":
        return False
    return (time.time() - (j.get("updated_at") or 0)) <= STALE_AFTER


def delete_jobs(job_ids, force=False):
    """删除若干条任务记录，返回实际删除的 id 列表。

    默认**拒绝删除活跃任务**（运行中且未失联）——避免手滑把正在跑的任务记录抹掉；
    `force=True` 可强制删除（用于卡死的僵尸卡）。删除只动注册表，不碰任何业务数据。
    """
    want = [str(x) for x in (job_ids or []) if x]
    removed = []
    if not want:
        return removed
    with _lock():
        jobs = load_jobs()
        for jid in want:
            j = jobs.get(jid)
            if j is None:
                continue
            if not force and _is_active(j):
                continue
            jobs.pop(jid, None)
            removed.append(jid)
        if removed:
            _safe_save(jobs)
    return removed


def delete_finished():
    """清空所有**非活跃**记录（已完成 / 失败 / 疑似结束），返回删除条数。"""
    with _lock():
        jobs = load_jobs()
        keep = {}
        n = 0
        for jid, j in jobs.items():
            if _is_active(j):
                keep[jid] = j
            else:
                n += 1
        if n:
            _safe_save(keep)
    return n


def panel_url():
    return "http://127.0.0.1:%d/" % PANEL_PORT


def panel_alive(timeout=0.3):
    import socket
    s = socket.socket()
    s.settimeout(timeout)
    try:
        return s.connect_ex(("127.0.0.1", PANEL_PORT)) == 0
    finally:
        s.close()


# ---------------------------------------------------------------- 面向对象封装
class Progress(object):
    """一行接入：with Progress("任务名", total=N) as p: p.set(i)"""

    def __init__(self, title, total=None, unit="项", job_id=None, source="script",
                 session_id=None, log_file=None, message=None, planned_sec=None,
                 steps=None):
        self.id = register(title, total=total, unit=unit, job_id=job_id, source=source,
                           session_id=session_id, log_file=log_file, message=message,
                           planned_sec=planned_sec, steps=steps)

    def plan(self, seconds, message=None):
        """声明/修正**预估总耗时**（秒）。不可细分的长任务用它对表：
        面板据此显示「预计完成」钟点与时间进度，超出预估会显「已超预估」。"""
        update(self.id, planned_sec=seconds, message=message)
        return self

    def steps(self, steps):
        """设置步骤列表：set_steps(["准备","处理","收尾"]) 或 .steps("准备,处理,收尾")"""
        set_steps(self.id, steps)
        return self

    def step(self, index, name=None):
        """推进到指定步骤（0 基）；可选顺便改该步骤名。"""
        set_step(self.id, index, name=name)
        return self

    def mark_start(self, started_at=None):
        """标记「开始执行」时刻；接到命令与真正开始执行有间隔时用。"""
        mark_start(self.id, started_at=started_at)
        return self

    def set(self, done=None, total=None, message=None, unit=None):
        update(self.id, done=done, total=total, message=message, unit=unit)
        return self

    def inc(self, n=1, message=None):
        cur = (load_jobs().get(self.id) or {}).get("done", 0)
        update(self.id, done=cur + n, message=message)
        return self

    def log(self, message):
        update(self.id, log_line=message)
        return self

    def ok(self, message=None):
        finish(self.id, True, message or "已完成")
        return self

    def fail(self, message=None):
        finish(self.id, False, message or "失败")
        return self

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        if exc_type is None:
            self.ok()
        else:
            self.fail("%s: %s" % (exc_type.__name__, exc))
        return False


def append_auto_step(job_id, label, complete=True):
    """自动映射：把一次工具调用追加为会话卡的一个步骤。

    complete=True（PostToolUse，工具已完成）→ 该步即 ✓；若 PreToolUse 已先插入了
    同名的「进行中」步（current_step 正指向它），则只把它推进为完成、不重复追加（去重）。
    complete=False（PreToolUse，工具进行中）→ 插入一步并标记 🔄 进行中。
    限长 MAX_AUTO_STEPS 步，被挤掉的旧步骤仍在日志里。"""
    MAX_AUTO_STEPS = 12
    with _lock():
        jobs = load_jobs()
        j = jobs.get(job_id)
        if j is None:
            return None
        steps = list(j.get("steps") or [])
        if (complete and steps and steps[-1] == str(label)
                and j.get("current_step") == len(steps) - 1):
            # PreToolUse 已插入过这一步（进行中），这里只推进为完成，避免重复步骤
            j["current_step"] = len(steps)
        else:
            steps.append(str(label))
            if len(steps) > MAX_AUTO_STEPS:
                steps = steps[-MAX_AUTO_STEPS:]
            j["steps"] = steps
            # 完成后该步即 ✓（current=len，所有步 i<cur）；进行中该步🔄（current=len-1）
            j["current_step"] = len(steps) if complete else len(steps) - 1
        j["manual_steps"] = j.get("manual_steps", False)
        log = j.setdefault("log", [])
        log.append("%s  %s" % (time.strftime("%H:%M:%S"), label))
        del log[:-MAX_LOG]
        j["updated_at"] = time.time()
        _safe_save(jobs)
    return j


# ---------------------------------------------------------------- CLI
def _cli():
    ap = argparse.ArgumentParser(description="通用实时进度 · 任务登记 CLI")
    sub = ap.add_subparsers(dest="cmd")

    p = sub.add_parser("start", help="登记/开始一个任务")
    p.add_argument("--title", required=True)
    p.add_argument("--total", type=int)
    p.add_argument("--unit", default="项")
    p.add_argument("--id")
    p.add_argument("--source", default="cli")
    p.add_argument("--msg", default=None)
    p.add_argument("--plan-sec", type=float, default=None,
                   help="预估总耗时（秒）：不可细分的长任务用，面板据此算预计完成时间")
    p.add_argument("--steps", default=None,
                   help="步骤列表，逗号分隔：准备,处理,收尾（面板显示步骤进度条）")

    p = sub.add_parser("begin", help="登记任务并立刻标记「接到命令」(=now)，可带步骤")
    p.add_argument("--title", required=True)
    p.add_argument("--total", type=int)
    p.add_argument("--unit", default="项")
    p.add_argument("--id")
    p.add_argument("--source", default="cli")
    p.add_argument("--msg", default=None)
    p.add_argument("--plan-sec", type=float, default=None)
    p.add_argument("--steps", default=None,
                   help="步骤列表，逗号分隔：准备,处理,收尾")

    p = sub.add_parser("step", help="推进到指定步骤（0 基）")
    p.add_argument("--id", required=True)
    p.add_argument("--index", type=int, required=True)
    p.add_argument("--name")

    p = sub.add_parser("steps", help="设置/替换步骤列表")
    p.add_argument("--id", required=True)
    p.add_argument("--set", required=True, help="逗号分隔的步骤名：准备,处理,收尾")

    p = sub.add_parser("auto-step", help="（自动映射 hook 专用）追加一个工具调用为步骤")
    p.add_argument("--id", required=True)
    p.add_argument("--label", required=True, help="步骤名，如 写入文件: report.md")
    p.add_argument("--log-only", action="store_true",
                   help="只记一行日志、不追加步骤（轻量工具/主动接管模式用）")

    p = sub.add_parser("plan", help="声明/修正预估总耗时（秒）")
    p.add_argument("--id", required=True)
    p.add_argument("--sec", type=float, required=True)
    p.add_argument("--msg")

    p = sub.add_parser("set", help="更新进度")
    p.add_argument("--id", required=True)
    p.add_argument("--done", type=int)
    p.add_argument("--total", type=int)
    p.add_argument("--unit")
    p.add_argument("--msg")

    p = sub.add_parser("inc", help="进度 +n")
    p.add_argument("--id", required=True)
    p.add_argument("--n", type=int, default=1)
    p.add_argument("--msg")

    p = sub.add_parser("log", help="只追加一行日志")
    p.add_argument("--id", required=True)
    p.add_argument("--msg", required=True)

    p = sub.add_parser("finish", help="标记完成/失败")
    p.add_argument("--id", required=True)
    p.add_argument("--failed", action="store_true")
    p.add_argument("--msg")
    p = sub.add_parser("finish-session",
                       help="主动收尾：结束本会话的 sess-<id> 卡（不带 --session 则结束全部运行中的 sess-* 卡）")
    p.add_argument("--session", help="会话 id；给了就只结束 sess-<id>")
    p.add_argument("--msg", default=None)
    p.add_argument("--failed", action="store_true")

    p = sub.add_parser("gc",
                       help="异常中断兜底：收尾长时间无更新的 sess-* 会话卡（默认 30 分钟）")
    p.add_argument("--min", type=float, default=GC_ABANDON_AFTER / 60.0,
                   help="无更新分钟数阈值，默认 30")
    p.add_argument("--failed", action="store_true", help="标为 failed 而非 aborted")
    p.add_argument("--dry", action="store_true", help="只报告、不落盘")

    p = sub.add_parser("sync-wb",
                       help="WB 权威状态同步：按 workbuddy.db sessions.status 收尾会话卡")
    p.add_argument("--dry", action="store_true", help="只报告、不落盘")

    p = sub.add_parser("fix-mojibake",
                       help="修乱码：把 UTF-8 被按 GBK 解码的标题/日志还原（--dry 只报告）")
    p.add_argument("--dry", action="store_true", help="只报告、不落盘")

    p = sub.add_parser("list", help="列出任务")
    p.add_argument("--running", action="store_true")
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("delete", help="删除任务记录（默认拒绝删活跃任务）")
    p.add_argument("--id", required=True)
    p.add_argument("--force", action="store_true", help="连活跃任务一起删（清僵尸卡用）")

    p = sub.add_parser("clear", help="清空记录")
    p.add_argument("--finished", action="store_true",
                   help="只清已结束/失败/疑似结束（活跃任务保留）；缺省等同于 --finished")
    p.add_argument("--all", action="store_true", help="清空全部（含活跃，慎用）")

    p = sub.add_parser("url", help="打印面板地址（若未运行会给出启动提示）")

    a = ap.parse_args()
    if a.cmd == "start":
        jid = register(a.title, total=a.total, unit=a.unit, job_id=a.id, source=a.source,
                       message=a.msg, planned_sec=a.plan_sec, steps=a.steps)
        print(jid)
    elif a.cmd == "begin":
        # 接到命令即登记：received_at=now（除非该 id 已存在→保留原 received_at）
        jid = register(a.title, total=a.total, unit=a.unit, job_id=a.id, source=a.source,
                       message=a.msg or "接到命令", planned_sec=a.plan_sec, steps=a.steps)
        print(jid)
    elif a.cmd == "step":
        j = set_step(a.id, a.index, name=a.name)
        print(json.dumps({"id": a.id, "current_step": (j or {}).get("current_step")}, ensure_ascii=False))
    elif a.cmd == "steps":
        j = set_steps(a.id, a.set)
        print(json.dumps({"id": a.id, "steps": (j or {}).get("steps")}, ensure_ascii=False))
    elif a.cmd == "auto-step":
        if a.log_only:
            update(a.id, log_line="[自动] %s" % a.label)
            print(json.dumps({"id": a.id, "mode": "log"}, ensure_ascii=False))
        else:
            j = append_auto_step(a.id, a.label)
            print(json.dumps({"id": a.id, "current_step": (j or {}).get("current_step")},
                             ensure_ascii=False))
    elif a.cmd == "plan":
        update(a.id, planned_sec=a.sec, message=a.msg)
    elif a.cmd == "set":
        update(a.id, done=a.done, total=a.total, unit=a.unit, message=a.msg)
    elif a.cmd == "inc":
        cur = (load_jobs().get(a.id) or {}).get("done", 0)
        update(a.id, done=cur + a.n, message=a.msg)
    elif a.cmd == "log":
        update(a.id, log_line="%s  %s" % (time.strftime("%H:%M:%S"), a.msg))
    elif a.cmd == "finish":
        j = finish(a.id, ok=not a.failed, message=a.msg)
        print(json.dumps({"id": j["id"], "status": j["status"]}, ensure_ascii=False))
    elif a.cmd == "finish-session":
        ended = finish_session(a.session, ok=not a.failed, message=a.msg)
        print(json.dumps({"ended": ended, "count": len(ended)}, ensure_ascii=False))
    elif a.cmd == "gc":
        hit = gc_abandoned(min_idle=a.min * 60, as_failed=a.failed, dry=a.dry)
        print(json.dumps({"gc": hit, "count": len(hit), "dry": bool(a.dry)},
                         ensure_ascii=False))
    elif a.cmd == "sync-wb":
        hit = sync_wb_sessions(min_gap=0, dry=a.dry)
        print(json.dumps({"swept": hit, "count": len(hit), "dry": bool(a.dry)},
                         ensure_ascii=False))
    elif a.cmd == "fix-mojibake":
        hit = repair_mojibake(dry=a.dry)
        print(json.dumps({"fixed": hit, "count": len(hit), "dry": bool(a.dry)},
                         ensure_ascii=False, indent=1))
    elif a.cmd == "list":
        jobs = list_jobs(only_running=a.running)
        if a.json:
            print(json.dumps(jobs, ensure_ascii=False, indent=1))
        else:
            for j in jobs:
                tp = "%s/%s" % (j.get("done"), j.get("total") if j.get("total") else "?")
                print("%-10s %-8s %-12s %s" % (j.get("id"), j.get("status"), tp, j.get("title")))
            if not jobs:
                print("(无任务)")
    elif a.cmd == "delete":
        rm = delete_jobs([a.id], force=a.force)
        print(json.dumps({"removed": rm, "ok": bool(rm)}, ensure_ascii=False))
    elif a.cmd == "clear":
        if a.all:
            n = 0
            with _lock():
                jobs = load_jobs()
                n = len(jobs)
                if n:
                    _safe_save({})
            print(json.dumps({"removed": n}, ensure_ascii=False))
        else:
            print(json.dumps({"removed": delete_finished()}, ensure_ascii=False))
    elif a.cmd == "url":
        print(panel_url(), "运行中" if panel_alive() else "未运行（请启动 live_panel.py）")
    else:
        ap.print_help()


if __name__ == "__main__":
    _cli()
