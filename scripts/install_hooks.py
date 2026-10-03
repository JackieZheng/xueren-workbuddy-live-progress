# -*- coding: utf-8 -*-
"""把「任务执行进度·实时面板」的 hooks 幂等写入 WorkBuddy 用户配置（~/.workbuddy/settings.json）。

用法:
    python scripts/install_hooks.py            # 安装/更新
    python scripts/install_hooks.py --status   # 查看当前状态
    python scripts/install_hooks.py --uninstall

写入内容（只动 hooks 字段，其余原样保留；写入前自动备份 settings.json.bak.<时间戳>）:
- SessionStart(*) → hook_session.py             会话一开始就**自动拉起面板服务** + 收异常中断卡 + 注入约定
                                                ⚠️ matcher 必须是 `*` 而非 `startup`：WB 重启后
                                                「恢复/继续已有会话」也要 ensure 面板（否则打开预览是空白）
- UserPromptSubmit(*) → hook_prompt.py          用户一提交命令就拉起面板 + 建「接到命令」会话卡
- PostToolUse(Bash|TaskOutput) → hook_bg.py     后台任务自动登记 + 提醒打开面板
- PostToolUse(*) → hook_auto_step.py            工具调用默认自动映射为会话卡步骤
- PreToolUse(*) → hook_auto_step.py --pre       工具开始前插入「进行中」步骤（实时🔄）
- Stop(*) → hook_stop.py                       回合结束：没打开过面板就 exit 2 拦截要求先 present_files
                                                （**不收尾**——回合≠会话结束）
- SessionEnd(*) → hook_stop.py                 会话真正终止：收尾会话卡 + 本会话 bg 卡标停
"""
import os
import sys
import json
import time
import shutil
import argparse

SKILL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HOOK_BG = os.path.join(SKILL_DIR, "scripts", "hook_bg.py").replace("\\", "/")
HOOK_SESSION = os.path.join(SKILL_DIR, "scripts", "hook_session.py").replace("\\", "/")
HOOK_PROMPT = os.path.join(SKILL_DIR, "scripts", "hook_prompt.py").replace("\\", "/")
HOOK_AUTO = os.path.join(SKILL_DIR, "scripts", "hook_auto_step.py").replace("\\", "/")
HOOK_STOP = os.path.join(SKILL_DIR, "scripts", "hook_stop.py").replace("\\", "/")

# ⚠️ MARKERS 必须覆盖**全部** hook 脚本：strip_ours() 靠它识别「本 skill 的条目」——
# 漏一个（曾漏 hook_stop.py）的后果是重装时那个事件既删不掉、又会重复追加。
MARKERS = ("hook_bg.py", "hook_session.py", "hook_prompt.py", "hook_auto_step.py",
           "hook_stop.py")


def config_dir():
    d = os.environ.get("WORKBUDDY_CONFIG_DIR")
    return d if d and d.strip() else os.path.join(os.path.expanduser("~"), ".workbuddy")


def settings_path():
    return os.path.join(config_dir(), "settings.json")


def py_exe():
    """优先用真正的 python.exe（hook 要往 stdout 输出 JSON，pythonw 无控制台）。"""
    exe = sys.executable or "python"
    if exe.lower().endswith("pythonw.exe"):
        cand = os.path.join(os.path.dirname(exe), "python.exe")
        if os.path.exists(cand):
            return cand
    return exe


def cmd_for(script):
    return '"%s" "%s"' % (py_exe().replace("\\", "/"), script)


def load_settings():
    p = settings_path()
    if not os.path.exists(p):
        return {}
    with open(p, encoding="utf-8") as f:
        return json.load(f)


def save_settings(d, backup=True):
    p = settings_path()
    os.makedirs(os.path.dirname(p), exist_ok=True)
    if backup and os.path.exists(p):
        bak = "%s.bak.%s" % (p, time.strftime("%Y%m%d-%H%M%S"))
        shutil.copy2(p, bak)
        print("已备份 ->", bak)
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=2)
    os.replace(tmp, p)
    print("已写入 ->", p)


def _is_ours(entry):
    for h in entry.get("hooks", []) or []:
        c = h.get("command", "") or ""
        if any(m in c for m in MARKERS):
            return True
    return False


def strip_ours(settings):
    hooks = settings.setdefault("hooks", {})
    removed = []
    for ev in list(hooks.keys()):
        arr = hooks.get(ev) or []
        kept = []
        for e in arr:
            if isinstance(e, dict) and _is_ours(e):
                removed.append(ev)
            else:
                kept.append(e)
        hooks[ev] = kept
    return removed


def install():
    s = load_settings()
    removed = strip_ours(s)
    hooks = s.setdefault("hooks", {})
    hooks.setdefault("SessionStart", []).append({
        # matcher 用 "*"：startup（新会话）、resume/continue（WB 重启后恢复会话）都要 ensure 面板
        "matcher": "*",
        "hooks": [{"type": "command", "command": cmd_for(HOOK_SESSION), "timeout": 10,
                   "description": "任务执行进度·实时面板：会话开始自动拉起面板服务 + 注入约定 + 清理陈旧任务"}],
    })
    hooks.setdefault("UserPromptSubmit", []).append({
        "matcher": "*",
        "hooks": [{"type": "command", "command": cmd_for(HOOK_PROMPT), "timeout": 15,
                   "description": "任务执行进度·实时面板：用户提交命令即拉起面板 + 建「接到命令」会话卡"}],
    })
    hooks.setdefault("PostToolUse", []).append({
        "matcher": "Bash|TaskOutput",
        "hooks": [{"type": "command", "command": cmd_for(HOOK_BG), "timeout": 15,
                   "description": "任务执行进度·实时面板：后台任务自动登记 + 提醒打开面板"}],
    })
    hooks.setdefault("PostToolUse", []).append({
        "matcher": "*",
        "hooks": [{"type": "command", "command": cmd_for(HOOK_AUTO), "timeout": 10,
                   "description": "任务执行进度·实时面板：工具调用自动映射为会话卡步骤（默认开）"}],
    })
    hooks.setdefault("PreToolUse", []).append({
        "matcher": "*",
        "hooks": [{"type": "command", "command": cmd_for(HOOK_AUTO) + " --pre", "timeout": 10,
                   "description": "任务执行进度·实时面板：工具开始前插入进行中步骤（实时🔄）"}],
    })
    # Stop = **每个回合**结束（≠会话结束）：只负责「本轮没打开过面板就 exit 2 拦截」，绝不收尾
    hooks.setdefault("Stop", []).append({
        "matcher": "*",
        "hooks": [{"type": "command", "command": cmd_for(HOOK_STOP), "timeout": 10,
                   "description": "任务执行进度·实时面板：回合结束前未打开面板则拦截要求先 present_files"}],
    })
    # SessionEnd = 会话真正终止（切换/清空/关闭）：收尾会话卡 + 本会话 bg 卡标记停止跟踪
    hooks.setdefault("SessionEnd", []).append({
        "matcher": "*",
        "hooks": [{"type": "command", "command": cmd_for(HOOK_STOP), "timeout": 10,
                   "description": "任务执行进度·实时面板：会话终止自动收尾会话卡"}],
    })
    save_settings(s)
    print("已替换旧条目:", removed or "无")
    print("hooks 事件 ->", {k: len(v) for k, v in s["hooks"].items()})
    print("说明：hooks 变更后可能需要重启会话（或在 /hooks 面板确认）才会生效。")


def uninstall():
    if not os.path.exists(settings_path()):
        print("settings.json 不存在，无需卸载")
        return
    s = load_settings()
    removed = strip_ours(s)
    save_settings(s)
    print("已移除本 skill 的 hooks 条目:", removed or "无")


def status():
    p = settings_path()
    print("配置文件:", p, "(存在)" if os.path.exists(p) else "(不存在)")
    if not os.path.exists(p):
        return
    s = load_settings()
    for ev, arr in (s.get("hooks") or {}).items():
        for e in arr or []:
            mark = "★" if isinstance(e, dict) and _is_ours(e) else " "
            for h in (e.get("hooks") or []):
                print("%s %-16s matcher=%-18s %s" % (mark, ev, e.get("matcher", "-"), h.get("command", "")))
    try:
        sys.path.insert(0, os.path.join(SKILL_DIR, "scripts"))
        import progress as P
        print("面板地址:", P.panel_url(), "| 状态:", "运行中" if P.panel_alive() else "未运行",
              "| 注册表:", P.JOBS)
    except Exception as e:
        print("面板状态读取失败:", e)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--uninstall", action="store_true")
    ap.add_argument("--status", action="store_true")
    a = ap.parse_args()
    if a.status:
        status()
    elif a.uninstall:
        uninstall()
    else:
        install()
