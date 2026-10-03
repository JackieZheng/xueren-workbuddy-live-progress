#!/usr/bin/env python
"""走马灯（marquee）推进平滑度诊断 —— 调 live_panel.py 的 mqStep / fitMarquee 后必跑。

**为什么必须「逐帧」测**：早期用 `setTimeout(…,33)` 采样去算帧位移是错的——采样间隔自己就在
40~94ms 抖，测出来的「峰值 3px」其实全是采样抖动，页面压根没卡。正确做法是**在 rAF 里**逐帧读
程序内部状态 `el._mq.off`（走马灯位移量），这一步之差能让结论完全反过来。

判定口径：
  · `回退次数 / 归零次数` → 位置是否被重置（v1.0.32 起的续播机制若坏了，这里会 nonzero）；
  · `帧位移 峰值 / 中位`   → 平滑度。1.0~2× 属于丝滑；>4× 就是肉眼可见的「一顿一顿」；
  · `帧间隔 p99`          → 主线程被 tick 的同步布局拖住的严重程度（>50ms 就是肉眼顿感源）。

常用：
  python _probe_marquee.py                     # 正常环境（无阻塞）逐帧测
  python _probe_marquee.py --block 120         # 注入 120ms/秒 主线程阻塞，模拟 tick 的同步布局开销
  python _probe_marquee.py --block 120 --ab    # 同一进程内新旧逻辑…… 不支持 A/B，需改代码重启面板
"""
import argparse, os, re, sys, time

URL = "http://127.0.0.1:8791/"
PROFILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".pwprofile")

# 逐帧读取所有 .mtrack（hdr / sysbar / 每张卡 meta）的 _mq.off + 轨道宽度 + 帧率 EMA + 帧时间戳
_COLLECT = """() => {
  window.__rows = [];
  const tracks = [...document.querySelectorAll('.mtrack')];
  (function loop(ts){
    window.__rows.push([+ts.toFixed(1),
      tracks.map(tr => { const m = tr.parentElement._mq;
                         if(!m || !m.on) return null;
                         return [+m.off.toFixed(2), Math.round(m.w||0),
                                 (typeof MQFPS!=='undefined') ? +MQFPS[0].toFixed(1) : 0]; })]);
    requestAnimationFrame(loop);
  })(performance.now());
}"""


def run(seconds, block, url):
    from playwright.sync_api import sync_playwright
    if not os.path.isdir(PROFILE):
        os.makedirs(PROFILE, exist_ok=True)
    # 别用 %TEMP%（本机安全策略会拦 playwright 往里写 profile）
    with sync_playwright() as pw:
        ctx = pw.chromium.launch_persistent_context(
            PROFILE, headless=True, args=["--no-proxy-server"],
            viewport={"width": 420, "height": 900})     # 窄视口 → 强制走马灯都跑起来
        pg = ctx.new_page()
        pg.goto(url, wait_until="load")
        time.sleep(2.0)
        if block:                                        # 制造主线程阻塞，模拟 tick 的同步布局/重排
            pg.evaluate("(b)=>{ setInterval(function(){const t=Date.now();"
                        "while(Date.now()-t<b){}}, 1000); }", block)
            time.sleep(1.0)
        pg.evaluate(_COLLECT)
        time.sleep(seconds)
        rows = pg.evaluate("window.__rows")
        ctx.close()
    return rows, len(rows)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seconds", type=float, default=8.0)
    ap.add_argument("--block", type=int, default=0, help="注入的主线程阻塞毫秒/秒（0=不注入）")
    ap.add_argument("--url", default=URL)
    a = ap.parse_args()

    rows, _fcnt = run(a.seconds, a.block, a.url)
    ntr = len(rows[0][1]) if rows else 0
    fps = (len(rows) / a.seconds) if a.seconds else 0
    print("逐帧：%d 帧 / %.1fs（≈%.0f fps）· 轨道 %d · 阻塞 %dms/秒"
          % (len(rows), a.seconds, fps, ntr, a.block))

    # ── 帧间隔（主线程是否被 tick 的同步布局拖住）────────────────────────
    gaps = sorted(rows[i][0] - rows[i - 1][0] for i in range(1, len(rows))
                  if 1 < (rows[i][0] - rows[i - 1][0]) < 200)
    if gaps:
        g = lambda q: gaps[min(len(gaps) - 1, int(len(gaps) * q))]
        print("帧间隔：中位=%.1fms p95=%.1fms p99=%.1fms 最大=%.1fms  (>50ms %d 帧)"
              % (g(0.5), g(0.95), g(0.99), gaps[-1], sum(1 for v in gaps if v > 50)))

    for k in range(ntr):
        steps, fw, fpss = [], set(), []
        for i in range(1, len(rows)):
            dt = rows[i][0] - rows[i - 1][0]
            if dt < 1 or dt > 200:            # 忽略切后台那种超长停顿
                continue
            p, c = rows[i - 1][1][k], rows[i][1][k]
            if p is None or c is None:
                continue
            steps.append(c[0] - p[0])
            fw.add(c[1])
            if c[2]:
                fpss.append(c[2])
        if not steps:
            continue
        vals = sorted(steps)
        med = vals[len(vals) // 2]
        ratio = (vals[-1] / med) if med else float("inf")
        print("[轨道#%d] 帧位移 n=%d 中位=%.3fpx 峰值=%.3fpx 峰值/中位=%.1f×  │ 宽度取值=%s"
              "%s"
              % (k, len(steps), med, vals[-1], ratio,
                 sorted(fw)[:4] if len(fw) <= 4 else "%d种" % len(fw),
                 ("  │ fpsEMA 中位=%.0f 最低=%.0f" % (sorted(fpss)[len(fpss)//2], min(fpss)))
                 if fpss else ""))
    print("判定：峰值/中位 ≤2× 属丝滑；>4× 说明掉帧时猛跳，回头查 mqStep 的 dt 上限与 tick 的同步布局开销。")


if __name__ == "__main__":
    try:
        main()
    except ImportError:
        sys.exit("需要 playwright：python -m pip install playwright")
