#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""进度桥接器 —— 把「外部状态文件」的进度接进任务执行进度·实时面板。

用途：某个长任务**已经跑起来**、且脚本本身没有接入 progress.py（典型：早前会话
启动的进程、第三方脚本、批处理）。桥接器轮询它的状态文件，用文件里的条目数作为
已完成量上报面板；一旦文件长时间不再更新（源进程结束/被杀），自动收尾并标记完成。

用法:
    python progress_bridge.py --id mirror-kb1-kb2 --title "KB1→KB2 图片知识镜像" \\
        --state-file ../state/mirror_img_kb1_to_kb2.json --total 3198 --unit 张

    # 用日志行数当进度
    python progress_bridge.py --id job2 --title "导出" --state-file out.log --count-mode lines --total 500

参数:
    --state-file   被监视的文件（每条成功即写盘的那种状态文件最合适）
    --count-mode   auto(默认) | len | lines | size
                   auto: .json 用 len（dict/list 条目数），其它用 lines
    --offset       在条目数基础上加一个基线（例：本轮之前已完成 N 条 → --offset N）
    --total        总量（不传则面板走"无总量"模式，只显示已处理数）
    --poll         轮询间隔秒（默认 10）
    --stall        状态文件多久不更新即判定源任务结束（默认 300 秒）
    --grace        启动后至少观察多久才允许判定结束（默认 30 秒）
    --log-file     可选：把该文件最后一行当作当前消息显示
"""
import os
import sys
import json
import time
import argparse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import progress as P  # noqa: E402


def read_count(path, mode):
    """返回 (count, ok)。path 不存在返回 (None, False)。"""
    if not os.path.exists(path):
        return None, False
    try:
        if mode == "size":
            return os.path.getsize(path), True
        if mode == "lines":
            with open(path, "rb") as f:
                return sum(1 for _ in f), True
        if mode == "len":
            with open(path, encoding="utf-8") as f:
                d = json.load(f)
            return (len(d) if isinstance(d, (dict, list)) else 1), True
        # auto
        if path.lower().endswith(".json"):
            with open(path, encoding="utf-8") as f:
                d = json.load(f)
            return (len(d) if isinstance(d, (dict, list)) else 1), True
        with open(path, "rb") as f:
            return sum(1 for _ in f), True
    except Exception:
        return None, False


def tail_line(path):
    if not path or not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8", errors="ignore") as f:
            lines = [ln.strip() for ln in f if ln.strip()]
        return lines[-1][:160] if lines else None
    except Exception:
        return None


def main():
    ap = argparse.ArgumentParser(description="把外部状态文件的进度桥接到任务执行进度·实时面板")
    ap.add_argument("--id", required=True, help="面板中的任务 id（稳定，便于重复运行复用同一卡片）")
    ap.add_argument("--title", default=None)
    ap.add_argument("--state-file", required=True)
    ap.add_argument("--count-mode", default="auto", choices=["auto", "len", "lines", "size"])
    ap.add_argument("--offset", type=int, default=0)
    ap.add_argument("--total", type=int, default=None)
    ap.add_argument("--unit", default="项")
    ap.add_argument("--poll", type=float, default=10.0)
    ap.add_argument("--stall", type=float, default=300.0)
    ap.add_argument("--grace", type=float, default=30.0)
    ap.add_argument("--settle", type=float, default=30.0,
                    help="达到预估总量后，状态文件需静默这么多秒才判定真完成（默认 30；"
                         "防止总量估低时刚到 N 就被误判结束）")
    ap.add_argument("--log-file", default=None)
    ap.add_argument("--message", default=None, help="自定义进度文案模板，可用 {done} {total} {pct}")
    ap.add_argument("--source", default="bridge")
    args = ap.parse_args()

    path = os.path.abspath(args.state_file)
    title = args.title or ("桥接：" + os.path.basename(path))
    if not os.path.exists(path):
        print("状态文件不存在: %s" % path, flush=True)
        return 2

    cnt, ok = read_count(path, args.count_mode)
    if not ok:
        print("状态文件无法解析: %s" % path, flush=True)
        return 2

    P.register(title, total=args.total, unit=args.unit, job_id=args.id,
               source=args.source, message="桥接中（监听 %s）" % os.path.basename(path))
    # 立刻把真实基数写进去，别让面板先看到 0（否则中途接管会被算成"瞬移"进度）
    P.update(args.id, done=cnt + args.offset, total=args.total,
             message="已处理 %d%s（桥接接管）" % (cnt + args.offset,
                                              (" / " + str(args.total)) if args.total else ""))
    start = time.time()
    last_cnt = cnt
    last_change = time.time()
    last_report = 0.0
    over_since = None       # 首次达到预估总量的时刻（用于 settle 判定）
    print("桥接启动: id=%s 当前 %s%s" % (args.id, cnt, ("/" + str(args.total)) if args.total else ""), flush=True)

    try:
        while True:
            time.sleep(args.poll)
            now = time.time()
            cnt, ok = read_count(path, args.count_mode)
            if ok and cnt is not None and cnt != last_cnt:
                last_cnt = cnt
                last_change = now
            done = (last_cnt or 0) + args.offset
            total = args.total
            if total and done > total:
                total = done              # 实际已超出预估总量 → 动态抬升，避免百分比 >100%
            if args.message:
                try:
                    msg = args.message.format(done=done, total=total or "?", pct=(
                        "%.1f%%" % (done / total * 100) if total else "?"))
                except Exception:
                    msg = args.message
            else:
                msg = "已处理 %d%s" % (done, (" / " + str(total)) if total else "")
            tl = tail_line(args.log_file)
            if tl:
                msg += " · " + tl
            if now - last_report >= max(args.poll, 3) or done != (P.load_jobs().get(args.id) or {}).get("done"):
                P.update(args.id, done=done, total=total, message=msg)
                last_report = now
            # 源任务结束判定：状态文件长时间不更新
            if now - start >= args.grace and now - last_change > args.stall:
                P.finish(args.id, True, "源任务已停止（最后进度 %d%s）" % (
                    done, (" / " + str(total)) if total else ""))
                print("桥接结束: %s 已停止更新 %.0fs，最终 %d" % (path, args.stall, done), flush=True)
                return 0
            # 达到预估总量：不立刻收尾——总量只是估计值，文件可能还在增长。
            # 只有「到量后又静默 settle 秒」才判定真完成，避免估低时提前退出。
            if total and done >= total:
                if over_since is None:
                    over_since = now
                elif now - over_since >= args.settle:
                    P.finish(args.id, True, "已达成目标 %d%s" % (done, "/" + str(total)))
                    print("桥接结束: 已达成总量 %d" % done, flush=True)
                    return 0
            else:
                over_since = None
    except KeyboardInterrupt:
        P.finish(args.id, True, "桥接被手动停止（最后进度 %d）" % ((last_cnt or 0) + args.offset))
        print("桥接被中断", flush=True)
        return 0


if __name__ == "__main__":
    sys.exit(main())
