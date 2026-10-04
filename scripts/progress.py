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

def home_dir():
    """可靠地取用户主目录。

    ⚠️ 不能只靠 `os.path.expanduser("~")`：**WMI（Win32_Process.Create）创建进程时
    不加载用户配置文件**，`USERPROFILE` / `HOMEDRIVE`+`HOMEPATH` 都可能缺失，
    expanduser 会原样返回 `"~"` —— 面板于是去读一个名叫 `~` 的相对目录，
    卡片全空（实测空环境下 jobs 路径会变成 `~\\.workbuddy\\live-progress\\jobs.json`）。
    先看环境变量，再退回 expanduser；最后一个都没有时用 __file__ 相对定位也没意义，
    直接返回 expanduser 的结果（至少不崩）。
    """
    for k in ("USERPROFILE", "HOME"):
        v = os.environ.get(k)
        if v and os.path.isdir(v):
            return v
    drive, path = os.environ.get("HOMEDRIVE"), os.environ.get("HOMEPATH")
    if drive and path and os.path.isdir(drive + path):
        return drive + path
    got = os.path.expanduser("~")
    return got if got and got != "~" else os.getcwd()


ROOT = os.environ.get("LIVE_PROGRESS_DIR") or os.path.join(
    home_dir(), ".workbuddy", "live-progress")
JOBS = os.path.join(ROOT, "jobs.json")
LOCK = os.path.join(ROOT, "jobs.lock")
PANEL_PORT = int(os.environ.get("LIVE_PROGRESS_PORT") or 8791)
MAX_JOBS = 40          # 注册表最多保留条数（按更新时间）
# 每个任务保留的日志行数（旧日志写满即丢弃旧行 —— 不是环形缓冲，是对外可见的全部历史）。
# 默认 40：jobs.json 体积与面板轮询开销的平衡。要留住长会话的早期日志，用环境变量放大：
#   LIVE_PROGRESS_MAX_LOG=300 python progress.py ...   （或在面板同环境里 export 后启动）
MAX_LOG = int(os.environ.get("LIVE_PROGRESS_MAX_LOG") or 40)
# ── 「会话内容」区（v1.0.55）：面板卡片新增一块展示**助手自己说的话**（transcript 抓 /
#    脚本主动 reply）。与日志分开：日志是机器明细，会话内容是给用户在预览区直接读的结论。
#    v1.0.59：面板改为**一行一条**（逐条对应聊天里的每条助手消息），所以这里多留几条
#    （v1.0.62 起 40 —— 默认收全会话流后 tool/think 行也占窗口，与 STREAM_LIMIT 对齐；
#    面板只取尾部若干条渲染，见 live_panel.REPLY_SHOW）；文本经 `humanize`
#    转成人话（去 markdown / 技术名词中文化 / 行内命令讲通俗说法）。
MAX_REPLY = int(os.environ.get("LIVE_PROGRESS_MAX_REPLY") or 40)   # 卡片保留最近 N 条
REPLY_MAXLEN = int(os.environ.get("LIVE_PROGRESS_REPLY_LEN") or 300)  # 单条截断字数
try:
    import humanize as _H
except Exception:                       # 词典缺失也不能让上报挂掉
    _H = None


def _human(text, limit=0):
    """助手正文 → 单行人话（压换行 / 去 markdown / 技术名词中文化）。

    ⚠️ v1.0.63 起**不再往正文里拼图标**——图标改成独立的 `ic` key（`humanize.icon_key`），
    面板据此渲染应用自带的单色 SVG（用户口径：用原来的图标，别拼 emoji）。正文保持原样，
    助手自己写的 emoji 一个字不动；`humanize` 缺失/关闭时退化为压缩空白后的原文。
    """
    if _H is not None:
        try:
            s = _H.plain_text(text)
            return s[:limit] if limit and len(s) > limit else s
        except Exception:
            pass
    s = " ".join(str(text or "").split())
    return s[:limit] if limit and len(s) > limit else s


def _log_trim(seq):
    """把日志序列裁到最近 MAX_LOG 行（就地）。"""
    del seq[:-MAX_LOG]
    return seq


def _log_total(j, n_lines, base=None):
    """追加了 n_lines 行日志后，计算任务的**累计**日志行数 log_total。

    背景（2026-10-03 用户实测反馈）：jobs.json 里的 log 列表被 MAX_LOG 截断，
    面板指标格「日志 N 行」原本直接取 len(log)，一旦写满 40 行就永远显示 40-
    明明显下面的日志在涨、数字却不动。这里另存一个**不受截断影响**的累计计数器
    log_total（写在 jobs.json 里），面板优先读它。

    基数是「旧计数」与「当前保留行数」的 max：既能接住老任务（迁移前只有
    len(log)），也保证计数只增不倒退。老任务的绝对基数可能偏低（截断时已丢的行
    数没被算进去），但从这一刻起严格正确、且逐行递增。
    """
    prev = j.get("log_total")
    if prev is None and base is None:
        base = len(j.get("log") or [])
    j["log_total"] = (int(prev) if isinstance(prev, (int, float)) else int(base or 0)) \
        + int(n_lines)
    return j["log_total"]


def _log_round_add(j, n_lines, base=None):
    """维护 **当前任务/本回合** 日志行数 log_round（面板指标格显示的那个「日志 N 行」）。

    口径来自用户 2026-10-03 的反馈：「日志计数器为当前任务的日志行数，
    不要累积其它任务的日志行」。会话卡是同一张卡跨多个回合复用，
    若显示 log_total（全会话累计），用户看到的就是 233 行这种"把本会话每条
    命令/每个任务的日志都算进来"的数——不符合"当前任务"的直觉。
    所以拆两个计数器：
      log_total = 本卡**累计**（跨回合），退出面指标格显示，只作累计参考；
      log_round = **当前任务/本回合** 新增行数，round_reset（新用户命令）时归零。
    """
    prev = j.get("log_round")
    if prev is None and base is None:
        base = len(j.get("log") or [])
    if not isinstance(prev, (int, float)):
        prev = None
    else:
        # 自愈：上一版双基准不一致时写入过「本轮 > 累计」的脏值（实测 106/105），
        # 这里收敛回累计值，**本轮 ≤ 累计** 必须恒成立，否则指标格 tooltip 会自相矛盾。
        tot = j.get("log_total")
        if isinstance(tot, (int, float)) and int(prev) > int(tot):
            prev = int(tot)
            j["log_round"] = prev            # 就地归一化，避免每次 push 都靠 clamp
            return prev + int(n_lines)
    j["log_round"] = (int(prev) if prev is not None else int(base or 0)) + int(n_lines)
    return j["log_round"]


def _log_push(j, lines):
    """追加日志行 + 维护两个计数（唯一入口，别再手写 log.append）。

    ⚠️ 两个计数器的迁移基准必须是**同一份**「追加前的保留行数」：早期实现先算 log_total
    再算 log_round，各自在方法内部现取 len(log)（此时已 append 过），于是旧卡迁移后
    出现「本轮 54 行 / 累计 53 行」——看着像账算错了。这里统一用追加前的长度。
    """
    seq = _log_trim(list(j.get("log") or []) + list(lines))
    j["log"] = seq
    n = len(lines)
    base = max(0, len(seq) - n)
    _log_total(j, n, base)
    _log_round_add(j, n, base)
    # 不变量「本轮 ≤ 累计」：老卡里可能残留「本轮 > 累计」的脏值（v1.0.51 中途版本
    # 两个计数器基准不一致写进去的），每次 push 都收敛一次，最多一拍就自愈。
    if int(j.get("log_round") or 0) > int(j.get("log_total") or 0):
        j["log_round"] = int(j["log_total"])
    return j


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
        # 日志基线：v1.0.65 起 **新回合（round_reset=True，用户在同会话提交新命令）日志清零重开**——
        # 旧回合的日志行留在上一回合，新回合从空开始（用户 2026-10-04 反馈「新起会话
        # 日志还挂着之前的内容」；会话内容自始就是按回合清零的，只有日志漏了）。
        # 跨回合累计账本 log_total **保留**（「本轮 N 行 · 累计 M 行」的账不变量仍成立）。
        # 脚本路径（begin / register 不带 round_reset）不受影响，日志照旧保留。
        base_log = [] if round_reset else list(old.get("log") or [])
        # 累计日志行数基线：优先沿用旧计数（register 会重建 dict，必须显式保留），
        # 老任务没有这个字段时退化成当前保留行数，保证不计入后面重复累加。
        base_total = int(old.get("log_total") or len(base_log) or 0)
        # 当前任务/本回合的日志行数：round_reset（用户新提交一条命令 = 新一轮）
        # 时归零——面板指标格显示的「日志 N 行」就是从这个基准往上数。
        # ⚠️ 迁移基准必须 ≤ base_total：老卡没有 log_round 字段时，log_total 与
        # len(base_log) 可能已经不齐（截断/重写过），直接拿 len 做本轮基准会让
        # 「本轮行数」反超「累计行数」（实测 88 vs 87，看着像账算错）。
        base_round = 0 if round_reset else (
            int(old["log_round"]) if isinstance(old.get("log_round"), (int, float))
            else min(base_total, len(base_log)))
        base_round = min(base_round, base_total)   # 不变量：本轮 ≤ 累计
        if norm_steps and steps is not None:
            base_log.append("%s  接入 %d 步：%s" % (time.strftime("%H:%M:%S"),
                                                  len(norm_steps), " / ".join(norm_steps[:8])))
            base_log = _log_trim(base_log)
            base_total += 1
            base_round += 1       # 「接入 N 步」这一行两个计数器都要算（否则本轮会比累计多 1）
        else:
            base_log = _log_trim(base_log)
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
            # round_reset（同会话新回合）视为换了一件任务：本轮没给 cmd 就**不保留**上一轮的
            # 用户原话（与 v1.0.65 的日志清零同源：新回合还挂着上一轮内容会让用户以为没重置）。
            # 脚本卡（不带 round_reset）照旧沿用旧值。
            "cmd": (cmd if cmd is not None
                    else (None if round_reset else old.get("cmd"))),
            "log_file": log_file or old.get("log_file"),
            "log": base_log,
            "log_total": base_total,
            "log_round": base_round,
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
        _log_push(j, ["%s  计划 %d 步：%s" % (time.strftime("%H:%M:%S"), len(names),
                                             " / ".join(names[:8]))])
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
            _log_push(j, ["%s  ▶ 第%d/%d步 %s" % (time.strftime("%H:%M:%S"),
                                                  idx + 1, max(len(steps), 1), nm)])
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
                 "reply": [], "reply_at": None,
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
            _log_push(j, ["%s  %s" % (time.strftime("%H:%M:%S"), message)])
        if log_line:
            _log_push(j, [str(log_line).rstrip()])
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


# ----------------------------------------------------------------「会话内容」区（v1.0.55）
# ⚠️ 隔离测试（_test_attach_contract.py 等）要指一个假 transcript 目录，而
# TRANSCRIPT_DIR 是模块级常量、hook 子进程又继承环境变量 —— 所以留一道环境变量开关，
# 比"改 HOME"安全得多（不会误伤真实会话数据）。
TRANSCRIPT_DIR = (os.environ.get("LIVE_PROGRESS_TRANSCRIPTS")
                  or os.path.join(home_dir(), ".workbuddy", "projects"))


def transcript_path(session_id):
    """会话 transcript 路径：`~/.workbuddy/projects/<工作区目录>/<session_id>.jsonl`。

    目录名由「盘符 + 去冒号的工作区路径」拼成（实测 `D-Users-JackieZheng-WorkBuddy-2026-…`），
    一个工作区一个目录、里面按 session uuid 放一条 jsonl。这里**只按文件名匹配**，
    不去解析目录内容（projects 下可能有上百个目录，扫内容太慢）。
    """
    if not session_id:
        return None
    try:
        if not os.path.isdir(TRANSCRIPT_DIR):
            return None
        for d in os.listdir(TRANSCRIPT_DIR):
            f = os.path.join(TRANSCRIPT_DIR, d, "%s.jsonl" % session_id)
            if os.path.isfile(f):
                return f
    except Exception:
        return None
    return None


# ------------------------------------------------- 用户输入（文本 + 附件）v1.0.57
# transcript 里每条 user 消息的第一个 input_text 常常是 WB 注入的
# `<system-reminder data-role="user-context">…</system-reminder>`（系统/身份注入，
# 不是用户话），认它当用户输入会把纯图片提交误判成「有文本」。
# ⚠️ 三个坑：① 必须 raw 字符串（"…\1…" 会被 Python 吃掉反斜杠变成 chr(1)）；
#            ② 标签名要用**命名捕获组**再 (?P=tag) 回引——(?:…) 是非捕获组，
#              `\1` 会直接报 "invalid group reference"（实测踩到）；
#            ③ 标签清单要跟着 WB 实际注入的标签走——实测扫 609 个 transcript、
#              2386 个 user text 块的标签词频，纯图片提交的 input_text 整块就是
#              `<image_local_path>C:\…\clipboard-xxx.png</image_local_path>`
#              （一个占位符，不是用户话）；还有 `<user_query>` 会把真话裹起来。
#              这两类漏掉的话，纯图片提交的卡片标题会变成一长条 Windows 路径。
_INJECT_TAGS = ("system-reminder", "command-name", "command-message",
                "image_local_path", "user_query",
                "previous_user_message", "previous_assistant_message",
                "previous_tool_call", "conversation_history_summary",
                "summary", "automation_system_reminder",
                "memory_and_skills_reminder", "additional_data",
                "current_time", "identity_context", "product_identity",
                "project_context", "project_layout", "connector-status",
                "user_info", "task-notification")
_INJECT_INNER = re.compile(r"<\s*(?P<tag>%s)"
                           r"[\s\S]*?(?:</\s*(?P=tag)\s*>|$)" % "|".join(_INJECT_TAGS),
                           re.I)


# `<user_query>…</user_query>` 是**壳**：标签是 WB 加的，里面那句才是用户真话。
# 和 system-reminder 相反（那是要连内容一起删掉的注入块），这里只剥壳、留真话。
_QUERY_WRAP = re.compile(r"<\s*user_query\s*>([\s\S]*?)</\s*user_query\s*>", re.I)


def _strip_injection(s):
    """抠掉注入块（含未闭合的），返回真正属于用户的那点文字。

    `<user_query>真话</user_query>` → 只剥壳保留「真话」；
    `<image_local_path>…</image_local_path>` 纯占位符 → 整块删掉（→ 空串）。
    """
    s = str(s or "")
    m = _QUERY_WRAP.search(s)
    if m:
        s = s[:m.start()] + m.group(1) + s[m.end():]
    return _INJECT_INNER.sub(" ", s).strip()


def _user_blocks(path, from_end=True, read_mb=8):
    """取 transcript 里**最后一条** user 消息的 content blocks。

    早期版本把所有 user 消息的行都攒起来，结果「最近一条只有音频」也会被前面
    几条纯图片的附件算进来（实测踩到）。现在遇新 user 消息就丢弃旧攒的。
    """
    blocks = []
    try:
        size = os.path.getsize(path)
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            fh.seek(max(0, size - read_mb * 1048576) if from_end else 0)
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    o = json.loads(line)
                except Exception:
                    continue
                if o.get("type") != "message":
                    continue
                m = o.get("message")
                role = m.get("role") if isinstance(m, dict) else None
                if role is None:
                    role = o.get("role")
                if role != "user":
                    continue
                blocks = []                       # 新的 user 消息 → 旧的整条作废
                c = m.get("content") if isinstance(m, dict) else o.get("content")
                if isinstance(c, str):
                    blocks.append({"type": "input_text",
                                   "text": _strip_injection(c)})
                elif isinstance(c, list):
                    blocks.extend([b for b in c if isinstance(b, dict)])
    except Exception:
        return []
    return blocks


# 实测（586 个历史 transcript 全量扫过）：纯图片提交时用户在 transcript 里是
#   {"type":"message","role":"user","content":[
#       {"type":"input_text","text":"…"},
#       {"type":"image_blob_ref","mime":"image/png","original_filename":"Clipboard_Screenshot.png",…}]}
# 音频 / 视频 / 文件是同一族 blob_ref（只实证到 image_blob_ref，故归类**以 mime 为主、
# type 为辅**，别写死死板的 type 名）。
_ATTACH_GROUPS = (
    ("图片", ("image", "img", "photo", "picture", "png", "jpg", "jpeg", "gif", "webp")),
    ("音频", ("audio", "voice", "speech", "mp3", "wav", "m4a", "flac", "record")),
    ("视频", ("video", "movie", "mp4", "mov", "webm")),
)
_ATTACH_FALLBACK = "文件"
_ATTACH_UNIT = {"图片": "张"}          # 其余一律「个」


def classify_attachment(blk):
    """把一个 transcript content block 归类成「图片 / 音频 / 视频 / 文件」，认不出返回 None。"""
    if not isinstance(blk, dict):
        return None
    blob = "%s %s" % (blk.get("type") or "", blk.get("mime") or "")
    blob = str(blob).lower()
    if not blob.strip():
        return None
    for name, keys in _ATTACH_GROUPS:
        for k in keys:
            if k in blob:
                return name
    return _ATTACH_FALLBACK if "blob" in blob or "file" in blob else None


# 用户文本块里混着 WB 的图片 / 文件 / 技能引用，例如（v1.0.57 真实会话实测全文）：
#   "@image#1:e3dff…png @image#2:Clipboard_Screenshot.png … @image#9:… 帮我把这些图片内容整理成md文档 @skill:ardot-design-core"
# 不抠掉的话，`user_input_title` 会把 `@image#1:…png` 当真话截 44 字当卡片标题，
# 面板上就是一行机器话（用户明确否掉过这类文案）。
# 通用形态：`@image#1:xxx.png` / `@file#2:…` / `@skill:ardot-design-core`（后一种没序号）
_REF_TOKENS = re.compile(r"@[a-z_]+(?:#[0-9]+)?:[^\s@]+", re.I)


def _strip_refs(s):
    """抠掉 `@image#1:xxx.png` / `@skill:xxx` 这类引用标记，剩下才是用户真话。"""
    return _REF_TOKENS.sub(" ", str(s or "")).strip()


def last_user_input(session_id):
    """读本会话 transcript 最近一条用户消息 → `(文本, {中文类别: 数量})`。

    纯图片 / 纯附件提交时 WB **不会派发 UserPromptSubmit hook**（实测 codebuddy
    bundle 里 `if(!el) return;`，el 为空文本 → 整条 hook 跳过），所以 hook 侧拿不到
    prompt；只能回 transcript 数附件，用于把卡片标题写成人话。
    长会话 jsonl 能到几十 MB，先读尾部、找不到再从头部捞一小段。
    """
    path = transcript_path(session_id)
    if not path:
        return "", {}
    blocks = _user_blocks(path, from_end=True)
    if not blocks:
        blocks = _user_blocks(path, from_end=False, read_mb=4)
    text, counts = "", {}
    for b in blocks:
        if b.get("type") in ("input_text", "text"):
            raw = b.get("text")
            if isinstance(raw, str) and _strip_injection(raw).strip():
                # 先剥注入块，再抠掉 @image#N / @skill: 引用标记，剩下的才是真话
                text = _strip_refs(_strip_injection(raw))
            continue
        k = classify_attachment(b)
        if k:
            counts[k] = counts.get(k, 0) + 1
    return text, counts


def describe_attachments(counts, limit=44):
    """`{图片:3, 视频:1, 文件:2}` → `用户发送了 3 张图片、1 个视频、2 个文件`。

    类别多（超过 limit 装不下）时**不硬截断**成半句话，而压缩成
    `用户发送了 3 张图片（音频 1、视频 2、文件 4…）`：主句留数量最多的那类，
    其余进括号，装不下就往回砍类并补「…」。这条文案是纯附件提交的卡片标题
    （v1.0.57 用户明确否掉过「（未识别到文本输入）」这种机器话）。
    """
    if not counts:
        return ""
    names = [n for n in ("图片", "音频", "视频", "文件")
             if int(counts.get(n) or 0) > 0]
    names += [n for n in counts                      # 未来新增的类别（如「文本」）也进括号
              if n not in names and int(counts.get(n) or 0) > 0]
    parts = ["%d %s%s" % (int(counts.get(n) or 0), _ATTACH_UNIT.get(n, "个"), n)
             for n in names]
    if not parts:
        return ""
    head, rest = parts[0], parts[1:]
    if not rest:
        s = "用户发送了 " + head
        return s[:limit - 1] + "…" if len(s) > limit else s
    # 括号里的类别从「全列」开始往回砍，砍到整句装得下为止（v1.0.57 实测修的
    # 两个坑：① 原来只留前 3 类就必补「…」，4 类时其实根本不需要；② 原来不按
    # limit 判断，超长会硬截成半句话）。砍到只剩一类还超长，退回「主句 + 省略号」。
    for keep in range(len(rest), 0, -1):
        inner = "、".join(rest[:keep]) + ("…" if keep < len(rest) else "")
        s = "用户发送了 %s（%s）" % (head, inner)
        if len(s) <= limit:
            return s
    s = "用户发送了 %s（…）" % head
    return s[:limit - 1] + "…" if len(s) > limit else s


def user_input_title(session_id, text=None, limit=44, fallback="会话执行中"):
    """卡片标题：`真话优先` → `用户发送了 N 张图片、M 个文件` → `fallback`。

    优先级（v1.0.57 实测校准）：**用户真话 > 附件统计**。图文混发时（比如「帮我看看这张图」
    + 1 张图）标题就该是那句真话，把附件统计顶上去等于吞掉了用户的指令；只有**整条提交
    没有任何真话**（纯图片 / 纯音频 / 纯文件）才退化成人话附件统计。
    hook_prompt 传 fallback="会话执行中"，hook_auto_step 传默认同值
    （保底：普通工具调用补建卡 / 连 transcript 都读不到时不该顶着用户标题）。
    """
    t = (text or "").strip()
    counts = {}
    if not t:
        try:
            t, counts = last_user_input(session_id)
        except Exception:
            t, counts = "", {}
    t = (t or "").strip()
    if t:
        return clean_title(t, limit)
    return describe_attachments(counts, limit) or fallback


def last_assistant_text(session_id, tail_bytes=6 * 1024 * 1024, max_lines=4000):
    """读 transcript **末尾最近一条助手正文** → `(行 id, 文本)`；抓不到返回 `(None, "")`。

    transcript 形态（实测）：每行一个 JSON，`type:"message"` / `role:"assistant"` /
    `content:[{type:"output_text", text:"…"}]`；助手正文一条消息可能分成多个 output_text
    块。长会话的 jsonl 能到几十 MB，所以只读**尾部** tail_bytes、且最多解析 max_lines 行，
    从后往前找到第一条含 output_text 的 assistant 行即最近一条（此时它多半还没写完整，
    但面板要的就是"最新的那句"）。
    """
    f = transcript_path(session_id)
    if not f:
        return (None, "")
    try:
        size = os.path.getsize(f)
        with open(f, "rb") as fh:
            fh.seek(max(0, size - int(tail_bytes)))
            raw = fh.read().decode("utf-8", errors="replace")
    except Exception:
        return (None, "")
    try:
        lines = raw.splitlines()[-int(max_lines):]
    except Exception:
        lines = []
    for ln in reversed(lines):
        if '"assistant"' not in ln or '"output_text"' not in ln:
            continue
        try:
            o = json.loads(ln)
        except Exception:
            continue
        if (o.get("role") or "") != "assistant":
            continue
        parts = []
        for b in (o.get("content") or []):
            if isinstance(b, dict) and b.get("type") == "output_text":
                t = (b.get("text") or "").strip()
                if t:
                    parts.append(t)
        if parts:
            return (o.get("id"), "\n".join(parts).strip())
    return (None, "")


# ───────────────────────────────────────────────「会话流」逐条还原（v1.0.60）
# 用户 2026-10-04 追加要求：「会话内容」不能只有最后那段回复，**执行过程中会话里出现的
# 中间内容**也要逐条收进去。transcript 里这活儿有三个落点（实测，见
# `_probe_transcript_shape.py` / `_probe_line_json.py`）：
#   · `type:"message"` + `role:"assistant"` + `content:[{type:"output_text", text}]`
#     → 助手正文（一句一条，实测一行只有一个 output_text 块）
#   · `type:"reasoning"` + `rawContent:[{type:"reasoning_text", text}]` → 思考块
#   · `type:"function_call"` + `name` + `arguments`（**JSON 字符串**，要 loads）→ 工具调用
# 默认收**全会话流**（v1.0.62，用户 2026-10-04 圈着会话窗的执行项图标定的口径）：
# 「会话内容」要的是"会话里显示了什么"——不只正文，**修改/读取/编辑/思考/测试这些执行项也逐条进**，
# 每条带会话窗同款默认图标（✏️ 改 / 👁 看 / ⌨️ 跑 / 🔍 搜，深度思考没图标 → 配 💭）。
# 早期默认只收 text（工具人话版步骤条已列、思考交给活体指示），用户明确要"收全"后改掉；
# 嫌吵可收窄：`LIVE_PROGRESS_STREAM_KINDS=text`（思考块改回活体指示、工具行只进步骤条）。
STREAM_KINDS = [k.strip() for k in
                (os.environ.get("LIVE_PROGRESS_STREAM_KINDS") or "text,think,tool").split(",")
                if k.strip()]
STREAM_LIMIT = int(os.environ.get("LIVE_PROGRESS_STREAM_LIMIT") or 40)   # 一轮最多看多少条
STREAM_FIRST = int(os.environ.get("LIVE_PROGRESS_STREAM_FIRST") or 12)   # 首次同步补发条数
STREAM_RESYNC = 8                       # 游标丢失（掉出窗口）时的保守补发条数
TOOL_ICON = "\u2699\ufe0f"              # ⚙️ 仅 CLI 文本输出兜底用（面板走 app_icons 单色 SVG）
THINK_ROW = "正在思考…"                 # **活体**状态行（面板末尾临时挂，不落盘；图标由 ic=deep 给）
THINK_HIST_ROW = "深度思考"             # 思考块的**历史**行（落卡；对齐会话窗的「深度思考」标签）


def _stream_item(o):
    """一行 transcript JSON → 0~1 个流项 `(mid, kind, payload)`；不是流项返回 None。"""
    t = str(o.get("type") or "")
    if t == "message":
        if (o.get("role") or "") != "assistant":
            return None
        parts = [str(b.get("text") or "").strip() for b in (o.get("content") or [])
                 if isinstance(b, dict) and b.get("type") == "output_text"]
        txt = "\n".join(p for p in parts if p).strip()
        return (o.get("id"), "text", txt) if txt else None
    if t == "reasoning":
        return (o.get("id"), "think", "")
    if t == "function_call":
        args = o.get("arguments")
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except Exception:
                args = {"_raw": args}
        if not isinstance(args, dict):
            args = {}
        return (o.get("id"), "tool", {"name": str(o.get("name") or ""), "input": args})
    return None


def recent_stream(session_id, tail_bytes=6 * 1024 * 1024, max_lines=6000,
                  turn_only=True, limit=None, kinds=None):
    """读 transcript 尾部 → **本轮**的会话流 `[(mid, kind, payload), …]`（按行序，旧→新）。

    `turn_only=True` 时只从**最后一条 user 消息**往后取 —— 「会话内容」要的是"当前这轮
    在会话里显示了什么"，不是整个会话的考古（长会话 transcript 能到 55MB）。
    `kinds=None` → 不过滤（含 think / tool，面板要拿它判断"此刻在不在思考"）；
    连续多个思考块并成一条（模型常连续思考好几段，逐块报会把卡片刷满）。
    ⚠️ `limit` 是**对本函数返回值**的截断，缺省不截断 —— 调用方要想"只要最近 N 条正文"，
    必须**先按 kind 过滤再取尾部**（`sync_stream` 就是这么做的）。早期版本把"先截 40 条
    再过滤"写在了这里，结果一轮里工具调用一多（实测 40 条全是 think/tool），
    中间的助手正文**全被截掉** → 卡片「会话内容」空着，正是用户报的那个症状。
    """
    f = transcript_path(session_id)
    if not f:
        return []
    try:
        size = os.path.getsize(f)
        with open(f, "rb") as fh:
            fh.seek(max(0, size - int(tail_bytes)))
            raw = fh.read().decode("utf-8", errors="replace")
    except Exception:
        return []
    try:
        lines = raw.splitlines()[-int(max_lines):]
    except Exception:
        return []
    start = 0
    if turn_only:
        for i in range(len(lines) - 1, -1, -1):
            ln = lines[i]
            if '"user"' not in ln:          # 便宜的预筛，别对每行都 json.loads
                continue
            try:
                o = json.loads(ln)
            except Exception:
                continue
            if o.get("type") == "message" and (o.get("role") or "") == "user":
                start = i + 1
                break
    items, prev_kind = [], None
    for ln in lines[start:]:
        if not ln.strip():
            continue
        try:
            o = json.loads(ln)
        except Exception:
            continue
        it = _stream_item(o)
        if not it or (kinds is not None and it[1] not in kinds):
            continue
        if it[1] == "think" and prev_kind == "think":
            prev_kind = "think"
            continue                        # 连续思考 → 一条
        prev_kind = it[1]
        items.append(it)
    return items[-int(limit):] if limit else items


def stream_cell(kind, payload):
    """流项 → `(人话文本, 图标key)`。图标 key 交给面板渲染 app_icons 的单色 SVG。"""
    if kind == "tool":
        name = (payload or {}).get("name") or ""
        tinput = (payload or {}).get("input") or {}
        txt, ic = "", "tool"
        if _H is not None:
            try:
                txt = _H.plain_tool(name, tinput)
                ic = _H.tool_icon_key(name, txt)
            except Exception:
                txt, ic = "", "tool"
        if not txt:
            txt = name or "工具调用"
        return txt, ic
    if kind == "think":
        return THINK_HIST_ROW, "deep"
    txt = _human(payload, limit=REPLY_MAXLEN)
    ic = ""
    if _H is not None:
        try:
            ic = _H.icon_key(txt)
        except Exception:
            ic = ""
    return txt, ic


def stream_row(kind, payload):
    """流项 → 面板「会话内容」区的一行**文本**（图标走 `stream_cell` 的 key，不拼进正文）。"""
    return stream_cell(kind, payload)[0]


def _blank_job(jid, now):
    """补建卡片的默认字段（与 `update()` 的补建口径一致，避免面板解析炸）。"""
    return {"id": jid, "title": jid, "status": "running", "done": 0, "total": None,
            "unit": "项", "message": "", "source": "script", "session_id": None,
            "cwd": os.getcwd(), "cmd": None, "log_file": None, "log": [], "reply": [],
            "planned_sec": None,
            "started_at": now, "updated_at": now, "ended_at": None}


def sync_stream(session_id, job_id=None, limit=None, ts=None):
    """把 transcript 里**本轮**的会话流**增量**同步到面板「会话内容」区（v1.0.60）。

    - 入卡的只有 `STREAM_KINDS` 里的类型（v1.0.62 起默认 text,think,tool = 收全会话流）；
    - 除此之外顺手记一个 `stream_kind`（**此刻**最新一条流项的类型）——面板拿它决定
      卡片末尾要不要临时挂一行 `💭 正在思考…`（活体行；思考块落卡的是「💭 深度思考」历史行）；
    - 增量游标 = `job["reply_mid"]`（上次同步到的**正文**项 id）：只投递它之后的新项；
    - 游标丢失（首次 / 掉出窗口）时保守地只补最近几条，绝不把一整轮刷进卡片；
    - 返回本次新增条数。失败一律吞掉（hook / 面板轮询都不该被它拖累）。
    """
    sid = str(session_id or "")
    jid = job_id or (("sess-" + sid) if sid else None)
    if not jid or not sid:
        return 0
    try:
        # 不过滤、不截断地读一遍窗口：既要按 kinds 挑出正文（**先过滤后截尾**，
        # 否则一轮里 tool/think 一多就会把正文挤出窗口），也要知道**最新那条**是什么类型
        items_all = recent_stream(sid)
    except Exception:
        return 0
    if not items_all:
        return 0
    head = items_all[-1][1]
    items = [it for it in items_all if it[1] in STREAM_KINDS][-int(limit or STREAM_LIMIT):]
    try:
        cells = {m: stream_cell(k, p) for m, k, p in items}
    except Exception:
        cells = {}
    now = float(ts) if ts else time.time()
    added = 0
    try:
        with _lock():
            jobs = load_jobs()
            j = jobs.get(jid)
            if j is None:
                j = _blank_job(jid, now)
                jobs[jid] = j
            seq = [x for x in (j.get("reply") or []) if isinstance(x, dict)]
            have = set(str(x.get("mid")) for x in seq)
            cur = j.get("reply_mid")
            pos = -1
            if cur:
                for i, it in enumerate(items):
                    if it[0] == cur:
                        pos = i
            if not items:
                start = 0                       # 本轮还没有正文 → 不投递，只更新 stream_kind
            elif pos >= 0:
                start = pos + 1
            elif cur:
                start = max(0, len(items) - STREAM_RESYNC)
            else:
                start = max(0, len(items) - STREAM_FIRST)
            for i in range(start, len(items)):
                m = items[i][0]
                cell = cells.get(m) or ("", "")
                txt, ic = cell[0], cell[1]
                if not txt or (m and str(m) in have):
                    continue                    # 空文本 / 同一 transcript 行只上一次
                seq.append({"t": now, "text": txt, "mid": m, "ic": ic})
                if m:
                    have.add(str(m))
                added += 1
            if items:
                j["reply_mid"] = items[-1][0] or cur
                del seq[:-MAX_REPLY]
                j["reply"] = seq
                j["reply_at"] = now
            if j.get("stream_kind") != head:
                j["stream_kind"] = head             # 面板据此挂「💭 正在思考…」
            j["updated_at"] = now
            _safe_save(jobs)
    except Exception:
        return 0
    return added


def reply(text, job_id=None, session_id=None, mid=None, ts=None, ic=None):
    """把一段「会话内容」挂到任务卡的 会话内容区（面板独立分区，与日志分开）。

    - `job_id` 缺省时按 `sess-<session_id>` 写当前会话卡；
    - 文本经 `humanize.plain_text()` 转成**单行人话**（压换行 / 去 markdown / 技术名词中文化），
      再截断到 REPLY_MAXLEN —— 面板「会话内容」区是**一行一条**（超宽走中段省略）；
    - `ic` = 图标 key（面板据此渲染 app_icons 的单色 SVG）；缺省按文本类型猜
      （`humanize.icon_key`）——**工具/思考行必须显式传**（stream_cell 给的 key 更准）；
    - 最多保留 MAX_REPLY 条（**不做累计计数**，会话内容不是日志）；
    - `mid` 用于去重：Stop hook 每回合都跑，同一条助手消息（transcript 行 id）只写一次；
      比对的是**整个列表**里有没有该 mid，而不只是最后一条 —— 否则中间轮次被覆盖后
      同一条会重复进来（早期只比 `reply_mid` 的写法在多条保留后就会漏判）；
    - 卡片不存在时自动补建（沿用 update() 的补建字段，避免面板解析炸）。
    返回 job dict；无文本/无目标 id 返回 None。
    """
    text = _human(text, limit=REPLY_MAXLEN)
    if not text:
        return None
    if ic is None:
        ic = ""
        if _H is not None:
            try:
                ic = _H.icon_key(text)
            except Exception:
                ic = ""
    jid = job_id or (("sess-" + str(session_id)) if session_id else None)
    if not jid:
        return None
    now = float(ts) if ts else time.time()
    with _lock():
        jobs = load_jobs()
        j = jobs.get(jid)
        if j is None:
            j = _blank_job(jid, now)
            jobs[jid] = j
        seq = [x for x in (j.get("reply") or []) if isinstance(x, dict)]
        if mid and any(x.get("mid") == mid for x in seq):
            return j                      # 同一条助手消息，已经上面板了
        seq.append({"t": now, "text": text, "mid": mid, "ic": ic})
        del seq[:-MAX_REPLY]
        j["reply"] = seq
        j["reply_at"] = now
        if mid:
            j["reply_mid"] = mid          # 兼容旧字段（面板 / 老数据仍在读）
        j["updated_at"] = now
        _safe_save(jobs)
        return j


# ---------------------------------------------------------------- WB 权威状态同步
WB_DB = os.path.join(home_dir(), ".workbuddy", "workbuddy.db")
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


def append_auto_step(job_id, label, complete=True, log_text=None):
    """自动映射：把一次工具调用追加为会话卡的一个步骤。

    complete=True（PostToolUse，工具已完成）→ 该步即 ✓；若 PreToolUse 已先插入了
    同名的「进行中」步（current_step 正指向它），则只把它推进为完成、不重复追加（去重）。
    complete=False（PreToolUse，工具进行中）→ 插入一步并标记 🔄 进行中。
    限长 MAX_AUTO_STEPS 步，被挤掉的旧步骤仍在日志里。

    `log_text`：**日志行**用的文案（v1.0.59）。步骤条走人话、日志要留原始命令，
    两者文案不同，所以分开传；不传就沿用 `label`（老调用方行为不变）。
    """
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
        _log_push(j, ["%s  %s" % (time.strftime("%H:%M:%S"), log_text or label)])
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

    p = sub.add_parser("reply",
                       help="写一段「会话内容」到面板 会话内容区（给预览区直接读的结论/说明）")
    p.add_argument("--text", required=True, help="AI 说的话（超长自动截断到 %d 字）" % REPLY_MAXLEN)
    p.add_argument("--id", default=None, help="目标卡 id；缺省按 --session 写 sess-<session> 会话卡")
    p.add_argument("--session", default=None, help="会话 id（与 --id 二选一）")

    p = sub.add_parser("stream",
                       help="（排障）看本会话「会话内容」区将收到哪几行（--sync 则真投递）")
    p.add_argument("--session", required=True, help="会话 id")
    p.add_argument("--id", default=None, help="投递目标卡 id；缺省 sess-<session>")
    p.add_argument("--limit", type=int, default=STREAM_LIMIT,
                   help="最多看几条（默认 %d；0 = 全部）" % STREAM_LIMIT)
    p.add_argument("--sync", action="store_true", help="真投递到卡片（默认只看不写）")
    p.add_argument("--backfill", type=int, default=0, metavar="N",
                   help="补历史：把整会话最近 N 条助手正文补进「会话内容」区（卡重建/跨回合用）")
    p.add_argument("--json", action="store_true")

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
    elif a.cmd == "reply":
        j = reply(a.text, job_id=a.id, session_id=a.session)
        print(json.dumps({"id": (j or {}).get("id"), "reply": len((j or {}).get("reply") or [])},
                         ensure_ascii=False))
    elif a.cmd == "stream":
        # 排障用：直接看「会话内容」区**将会**收到哪些行（默认只看不写）
        all_items = recent_stream(a.session)                  # 窗口内全部类型
        head = all_items[-1][1] if all_items else None
        in_card = [it for it in all_items if it[1] in STREAM_KINDS]
        lim = int(a.limit or 0)
        shown = in_card[-lim:] if lim else in_card
        rows = [{"mid": m, "kind": k, "text": c[0], "ic": c[1]}
                for m, k, p, c in
                ((m, k, p, stream_cell(k, p)) for m, k, p in shown)]
        if a.json:
            print(json.dumps({"kinds": STREAM_KINDS, "head": head,
                              "window_items": len(all_items), "in_card": len(in_card),
                              "items": rows}, ensure_ascii=False, indent=1))
        else:
            from collections import Counter
            cnt = Counter(k for _m, k, _p in all_items)
            print("入卡类型 = %s；此刻最新一条 = %s；窗口内 %d 条（%s）/ 会入卡 %d 条"
                  % (",".join(STREAM_KINDS), head, len(all_items),
                     " ".join("%s=%d" % (k, cnt[k]) for k in sorted(cnt)), len(in_card)))
            for r in rows:
                print("  ✓ [%s][%s] %s" % (r["kind"], r["ic"], r["text"]))
            if not rows:
                print("(本轮还没有可入卡的助手正文)")
        if a.backfill:
            # 补历史：卡刚被重建 / 换了回合，把**整会话**最近 N 条助手正文补进「会话内容」区
            prev = recent_stream(a.session, turn_only=False, kinds=list(STREAM_KINDS))[
                -int(a.backfill):]
            bjid = a.id or ("sess-" + a.session)
            before = len((load_jobs().get(bjid) or {}).get("reply") or [])
            for m, k, p in prev:
                t, kic = stream_cell(k, p)
                if t:
                    reply(t, job_id=bjid, mid=m, ic=kic)
            after = len((load_jobs().get(bjid) or {}).get("reply") or [])
            print(json.dumps({"backfilled": after - before}, ensure_ascii=False))
        if a.sync:
            print(json.dumps({"synced": sync_stream(a.session, a.id)}, ensure_ascii=False))
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
