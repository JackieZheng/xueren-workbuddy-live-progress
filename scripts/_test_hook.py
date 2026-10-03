# -*- coding: utf-8 -*-
"""hook_bg.py 直测：喂造假的 hook JSON，检查登记与回填。"""
import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PY = sys.executable
HB = os.path.join(HERE, "hook_bg.py")


def feed(payload, label):
    r = subprocess.run([PY, HB], input=json.dumps(payload), capture_output=True,
                       text=True, encoding="utf-8")
    print("---", label, "rc=", r.returncode)
    print((r.stdout or "").strip()[:1000])
    if (r.stderr or "").strip():
        print("ERR:", r.stderr.strip()[:400])


feed({
    "session_id": "SESS-TEST-1",
    "cwd": HERE,
    "hook_event_name": "PostToolUse",
    "tool_name": "Bash",
    "tool_input": {"command": 'python -u scripts/backfill_report.py --days 30', "run_in_background": True},
    "tool_response": "Status: Running in background with task_id: TEST123",
}, "bash-bg")

feed({
    "session_id": "SESS-TEST-1",
    "hook_event_name": "PostToolUse",
    "tool_name": "TaskOutput",
    "tool_input": {"task_id": "TEST123"},
    "tool_response": "Status: completed\nCommand completed with exit code 0",
}, "taskoutput-done")

import progress as P
print("jobs now:", [(j["id"], j["title"], j["status"]) for j in P.list_jobs()])
