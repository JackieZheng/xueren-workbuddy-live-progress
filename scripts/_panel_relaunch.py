# -*- coding: utf-8 -*-
"""面板热更新的「接替者」：等旧进程释放端口 → 用**完全相同的参数**启动新面板 → 自己退出。

由 `live_panel.py` 内部调用，无需手写：

    python _panel_relaunch.py --port 8791 [--idle-exit 1800] [--ttl 3600] ...

为什么要这么绕（踩坑记录）：
Windows 下 `os.execv` **不是**替换进程映像，而是「启动新进程 + 让老进程退出」——
两者会短暂并存：新进程一上来做幂等探测 `_already_running()`，看到老进程还占着端口就
自动跳过退出，老进程随即退出 → **两头都没了，面板掉线**。
所以改成：老进程先拉起本接替者（独立进程），自己立刻关监听并退出；接替者轮询到端口
腾出来以后，再用原参数把新面板拉起来，然后自己退出（新面板成为孤儿进程继续常驻）。
"""
import os
import sys
import time
import socket
import subprocess

HERE = os.path.dirname(os.path.abspath(__file__))
PANEL = os.path.join(HERE, "live_panel.py")
DETACH = 0x00000008 | 0x00000200      # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP（Windows）


def _port_free(port):
    s = socket.socket()
    s.settimeout(0.6)
    try:
        return s.connect_ex(("127.0.0.1", int(port))) != 0
    except Exception:
        return True
    finally:
        try:
            s.close()
        except Exception:
            pass


def main():
    args = [a for a in sys.argv[1:]]
    port = 8791
    if "--port" in args:
        try:
            port = int(args[args.index("--port") + 1])
        except Exception:
            pass
    # 等旧进程彻底让出端口（最多 ~20 秒）
    ok = False
    for _ in range(100):
        if _port_free(port):
            ok = True
            break
        time.sleep(0.2)
    if not ok:
        print("[relaunch] 端口 %d 一直被占用，放弃自动重启（请手动 panel_ctl.py start）"
              % port, flush=True)
        return 1
    exe = sys.executable
    kw = {"creationflags": DETACH} if os.name == "nt" else {}
    try:
        subprocess.Popen([exe, PANEL] + args, cwd=HERE, close_fds=True, **kw)
    except Exception as e:
        print("[relaunch] 启动新面板失败：%s" % e, flush=True)
        return 1
    print("[relaunch] 已用原参数启动新面板：%s %s" % (os.path.basename(exe), " ".join(args)),
          flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main() or 0)
