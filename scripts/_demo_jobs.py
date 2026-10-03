# -*- coding: utf-8 -*-
"""联调演示：造 1 个有总量任务 + 1 个无总量任务，持续上报约 60 秒。"""
import sys, os, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from progress import Progress, register, update, finish, panel_alive

# 情形一：有总量（进度环 + 速率 + ETA）
p = Progress("KB1 图片知识 → KB2 镜像（演示）", total=3286, unit="张", job_id="demo-mirror")
# 情形二：无总量（扫描条 + 已运行时长）
register("导出近 30 天日报（演示）", job_id="demo-export", source="demo", message="正在导出…")

done = 1900
end = time.time() + 60
i = 0
while time.time() < end:
    done += 3
    i += 1
    p.set(done, message="已镜像 %d / 3286" % done)
    if i % 4 == 0:
        update("demo-export", log_line="%s  已导出 %d 个文件" % (time.strftime("%H:%M:%S"), i * 2))
    time.sleep(2)
p.ok("演示结束")
finish("demo-export", ok=True, message="演示结束")
print("DEMO_DONE")
