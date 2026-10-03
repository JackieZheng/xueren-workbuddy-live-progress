# -*- coding: utf-8 -*-
"""直测 hook_prompt.py：不真的拉起面板/建卡，只校验注入文案能正确渲染（含收尾契约）。"""
import io
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import hook_prompt as H

# 打桩：避免真的启动面板与写注册表
created = []
H._ensure_panel = lambda port: (True, "test")
H._nudge_allowed = lambda sid, min_gap=180, bucket="": True
H.P.register = lambda *a, **k: created.append((a, k)) or "sess-test"


class _FakeStdin(io.StringIO):
    pass


old_stdin = sys.stdin
sys.stdin = _FakeStdin(json.dumps({
    "session_id": "test-1234",
    "prompt": "任务执行完要主动结束实时进度",
}, ensure_ascii=False))

buf = io.StringIO()
old_stdout = sys.stdout
sys.stdout = buf
try:
    H.main()
finally:
    sys.stdout = old_stdout
    sys.stdin = old_stdin

raw = buf.getvalue().strip()
print("--- hook stdout ---")
print(raw)
data = json.loads(raw)
ctx = data.get("hookSpecificOutput", {}).get("additionalContext", "")
print("--- additionalContext ---")
print(ctx)

checks = [
    ("含 finish-session", "finish-session" in ctx),
    ("含收尾契约", "收尾契约" in ctx),
    ("含验收标准", "list --running" in ctx),
    ("无未替换占位 %s", "%s" not in ctx),
    ("含正确脚本路径", "progress.py" in ctx and "xueren-workbuddy-live-progress" in ctx),
    ("建卡一次", len(created) == 1),
]
print("--- checks ---")
ok = True
for name, r in checks:
    print(("PASS  " if r else "FAIL  ") + name)
    ok = ok and r
print("RESULT:", "OK" if ok else "FAIL")
sys.exit(0 if ok else 1)
