# -*- coding: utf-8 -*-
"""人话化：把工具调用、命令行、助手正文翻译成「不懂技术也能看懂」的中文短语。

为什么单独立一个模块：**步骤条**（`hook_auto_step`）、**日志行**（各处 update/log_line）、
**面板元信息/载荷**（`live_panel`）三处都要用同一套说法。逻辑集中在这里，
避免各写一份导致口径漂移（同一件事在步骤里叫「执行命令」、在日志里叫别的）。

口径（用户 2026-10-04 要求，2026-10-04 二次修订）：
  · **步骤条**：**沿用原来的中文前缀格式**（`执行命令:` / `编辑文件:` …），只把「内容」换成人话；
  · **日志**：**一律保持原样**（原始工具名 + 原始命令 / `[自动]` 标记）—— 日志是给专业人员看的，
    不做人话化（对比 `hook_auto_step._label_raw`）；
  · **会话内容**：正文转单行人话 + 前缀一个「类型图标」（`icon_for`），方便区分是结论 / 提醒 / 进行中。

原始信息不丢：日志里保留原始命令，卡片元信息的「命令」字段也保留原文（`jobs.json`）。

总开关：`LIVE_PROGRESS_HUMANIZE=0` → 所有函数一律返回原文（排障 / 对照用）。
"""
import os
import re


def enabled():
    """是否启用翻译。设 `LIVE_PROGRESS_HUMANIZE=0` 可整体关闭（返回原文）。"""
    return os.environ.get("LIVE_PROGRESS_HUMANIZE", "1") != "0"


_WS = re.compile(r"\s+")


def _squeeze(s):
    return _WS.sub(" ", str(s or "")).strip()


def _base(path):
    """取文件名（路径只留最后一段，小白不需要看整条绝对路径）。"""
    p = str(path or "").replace("\\", "/").rstrip("/")
    return p.rsplit("/", 1)[-1] or p


def _short(s, n=32):
    """中间省略（与面板 / hook_auto_step 的口径一致）。"""
    s = _squeeze(s)
    if len(s) <= n:
        return s
    head = (n - 1) // 2
    tail = n - 1 - head
    return s[:head] + "…" + s[-tail:]


def _head(s, n=60):
    """只截头（尾部补 `…`）。

    ⚠️ 用在**用户原话**上：句子被中间挖掉一块（`_short`）读起来像"坏掉的文本"，
    而且 `_short` 的尾巴常常正是没信息量的部分（用户报过
    `运行系统命令（cd "用户资料盘（E 盘）…e(fp)))}'）`）。截头则永远保留最该看的前半句。
    """
    s = _squeeze(s)
    return s if len(s) <= n else s[:n].rstrip() + "…"


# ────────────────────────────────────────────────────────── 命令行 → 人话
# 顺序即优先级：越具体的规则越靠前；`cd xxx &&` 先剥掉（只是切换目录，不是动作）。
_LEAD_CD = re.compile(r"^cd\s+[^&;|]+?\s*(?:&&|;|\|\|)\s*", re.I)

# (正则, 人话) —— 只写「在干什么」，不解释参数
_CMD_RULES = [
    # 包管理 / 构建
    (re.compile(r"pip[\d.]*\s+install\b|python[\d.]*\s+-m\s+pip\s+install\b", re.I), "安装 Python 依赖包"),
    (re.compile(r"\b(?:npm|pnpm|yarn)\s+(?:install|i|ci)\b", re.I), "安装项目依赖"),
    (re.compile(r"\b(?:npm|pnpm|yarn)\s+run\s+build\b", re.I), "构建项目"),
    (re.compile(r"\b(?:npm|pnpm|yarn)\s+(?:test|run\s+test)\b", re.I), "跑项目自测"),
    # 版本管理
    (re.compile(r"\bgit\s+status\b", re.I), "查看代码改动状态"),
    (re.compile(r"\bgit\s+diff\b", re.I), "查看具体改了哪几行"),
    (re.compile(r"\bgit\s+log\b", re.I), "查看提交历史"),
    (re.compile(r"\bgit\s+add\b", re.I), "把改动放进待提交清单"),
    (re.compile(r"\bgit\s+commit\b", re.I), "提交代码改动"),
    (re.compile(r"\bgit\s+push\b", re.I), "把代码推送到远端仓库"),
    (re.compile(r"\bgit\s+(?:pull|fetch)\b", re.I), "拉取远端最新代码"),
    (re.compile(r"\bgit\s+clone\b", re.I), "下载代码仓库"),
    (re.compile(r"\bgit\s+(?:tag|release)\b", re.I), "给版本打标签 / 发布版本"),
    (re.compile(r"\bgit\s+remote\b", re.I), "查看远端仓库地址"),
    (re.compile(r"\bgit\s+", re.I), "做一次代码版本管理操作"),
    # 运行脚本 / 代码
    (re.compile(r"python[\d.]*\s+-m\s+pytest\b", re.I), "跑一遍项目的自测"),
    (re.compile(r"python[\d.]*\s+-c\b", re.I), "运行一小段 Python 代码"),
    (re.compile(r"python[\d.]*\s+-m\s+\S+", re.I), "运行一个 Python 模块"),
    (re.compile(r"python[\d.]*\s+\S*?\b([\w.\-]+\.py)", re.I), "运行脚本"),
    (re.compile(r"\bnode\s+\S*?([\w.\-]+\.js)", re.I), "运行脚本"),
    # 看目录 / 找文件 / 看内容
    (re.compile(r"\b(?:ls|dir)\b", re.I), "查看目录里有哪些文件"),
    (re.compile(r"\bfind\b", re.I), "按名字查找文件"),
    (re.compile(r"\b(?:grep|rg)\b", re.I), "在文件内容里搜索关键字"),
    (re.compile(r"\b(?:cat|head|tail|sed|awk|more|less)\b", re.I), "查看文件内容"),
    (re.compile(r"\bwc\b", re.I), "数一下文件的行数/字数"),
    (re.compile(r"\bwhich|where\b", re.I), "查找程序的安装位置"),
    # 文件操作
    (re.compile(r"\bmkdir\b", re.I), "新建文件夹"),
    (re.compile(r"\b(?:cp|copy|xcopy|robocopy)\b", re.I), "复制文件"),
    (re.compile(r"\b(?:mv|move|ren|rename)\b", re.I), "移动 / 重命名文件"),
    (re.compile(r"\b(?:rm|del|remove|rmdir)\b", re.I), "删除文件"),
    (re.compile(r"\b(?:zip|tar|unzip|7z|gzip)\b", re.I), "压缩 / 解压文件"),
    # 网络
    (re.compile(r"\b(?:curl|wget|Invoke-WebRequest|iwr)\b", re.I), "访问一个网络地址取数据"),
    (re.compile(r"\bssh\b", re.I), "连到远程服务器"),
    # 进程 / 系统
    (re.compile(r"\b(?:tasklist|Get-Process|ps)\b", re.I), "查看正在运行的程序"),
    (re.compile(r"\b(?:taskkill|Stop-Process|kill)\b", re.I), "结束某个程序"),
    (re.compile(r"\bnetstat\b", re.I), "查看网络端口占用"),
    (re.compile(r"\b(?:set|export|echo\s+\$)\b", re.I), "查看 / 设置环境变量"),
    (re.compile(r"\becho\b", re.I), "输出一段文字"),
    (re.compile(r"\bpowershell\b", re.I), "运行一条 PowerShell 命令"),
    (re.compile(r"\btimeout\b", re.I), "等待一小会儿"),
]

_PY_SCRIPT_ARG = re.compile(
    r"(?:python[\d.]*|node|npx)\s+\S*?\b([\w.\-]+\.(?:py|js|mjs|cjs))", re.I)


# ── 「像不像一条命令」：兜底分支的分流阀（v1.0.66）
# `hook_prompt` 会把**用户原话**存进卡片 `cmd` 字段，面板元信息用 `plain_command` 渲染它。
# 用户原话是自然语言（「先不更新，我看下还有没有要优化的」），早期一律落到兜底分支 →
# 显示成「运行系统命令（先不更新，我看下…）」；WB 注入的 `<task-notification>` 系统通知
# 同样中招（实测 8 张卡里 4 张）。
# 判据：**以「可执行名」开头**才算命令——可选 `&`/引号前缀，名字后必须紧跟空白/结尾/分隔符。
# 于是中文开头的自然语言、`<tag>`、`@image#1:xxx.png` 一律判成"不是命令"（走用户原话分支）。
# ⚠️ 带引号的那一支要求**引号里含路径分隔符**：本机工作区在 `用户资料盘（E 盘）/…`，
#    `"用户资料盘（E 盘）/工作室/x.exe" --arg` 这种必须认成命令；而用户把整句话加引号
#    （`"帮我改一下"`）不该被当成命令。
_CMD_HEAD = re.compile(
    r'^\s*(?:&\s*)?(?:'
    r'"[^"]*[\\/][^"]*"|\'[^\']*[\\/][^\']*\'|'
    r'[A-Za-z0-9_.\-~/\\:$]+'
    r')(?=\s|$|[;|&)])')

# 兜底短语：只报「执行的是哪个程序」，不再把整条命令首尾各截一半
# （早期 `_short(raw, 24)` 会截出 `cd "用户资料盘（E 盘）…e(fp)))}'` 这种尾巴带括号的垃圾串）。
_PROG_PHRASE = [
    (re.compile(r"^python[\d.]*(?:\.exe)?$|^py(?:\.exe)?$", re.I), "运行一段 Python 代码"),
    (re.compile(r"^node(?:js)?(?:\.exe)?$", re.I), "运行一段 Node 代码"),
    (re.compile(r"^(?:pwsh|powershell|cmd|conhost)(?:\.exe)?$", re.I), "运行一条系统命令"),
    (re.compile(r"^(?:ba|z|da|k)?sh(?:\.exe)?$|^env$", re.I), "运行一段命令行脚本"),
    (re.compile(r"^php(?:\.exe)?$", re.I), "运行一段 PHP 代码"),
    (re.compile(r"^java(?:w)?(?:\.exe)?$", re.I), "运行一个 Java 程序"),
    (re.compile(r"^sqlite3?(?:\.exe)?$", re.I), "查询数据库"),
    (re.compile(r"^7z(?:a|\.exe)?$|^tar(?:\.exe)?$|^zip(?:\.exe)?$|^unzip(?:\.exe)?$", re.I),
     "压缩 / 解压文件"),
]


def looks_like_command(s):
    """这段文本是「命令行」还是「一句自然语言」（用户原话 / 系统通知）？

    只用于兜底分支：真正命中 `_CMD_RULES` 的照旧走规则，不受影响。
    """
    t = _squeeze(s)
    m = _CMD_HEAD.match(t)
    if not m:
        return False
    head = t[:m.end()].strip(" \"'&").lower()
    # 裸网址 / 纯链接不算命令（用户常把一条链接单独丢过来）
    return "://" not in head and not head.startswith(("http", "www."))


def _prog_name(cmd):
    """取命令的「执行程序名」（首个 token 的 basename），用于判不出动作时的兜底短语。

    ⚠️ 必须支持**带引号且路径里有空格**的程序（`& "C:/Program Files/A/a.exe" arg`）——
    早期直接按空白切首个 token，会切出 `C:/Program` → 兜底成「运行命令 Program」（实测）。
    """
    t = str(cmd or "").lstrip()
    if t.startswith("&"):                      # PowerShell 调用运算符
        t = t[1:].lstrip()
    m = re.match(r'^"([^"]+)"', t) or re.match(r"^'([^']+)'", t)
    if m:
        return _base(m.group(1))
    m = re.match(r"^([^\s\"']+)", t)
    return _base(m.group(1)) if m else ""


# 形如 `<task-notification>…</task-notification>` 的注入块 / 系统通知：既不是命令，
# 也不该原样怼给用户（真剥不干净时至少给人话）。正常路径由 `hook_prompt` 与
# `live_panel._cmd_fields` 先剥掉，这里只是最后一道兜底。
_XML_ISH = re.compile(r"^\s*<[\w:.\-]+\s*>[\s\S]*</")


def plain_command(cmd):
    """把一条 shell / PowerShell 命令翻译成「在干什么」（人话）。

    规则命中 → 纯中文短语；`python xxx.py` / `node xxx.js` 会把脚本名带上，方便对着文件找；
    兜底分两种（v1.0.66）：
      · **不是命令**（用户原话 / 系统通知）→ 原样返回那句人话，交给面板「在做什么」展示，
        绝不再包一层「运行系统命令（…）」；
      · 是命令但规则不认 → 只报**执行程序名**（`运行一段 Python 代码` / `运行命令 foo.exe`），
        不再把整条命令首尾各截一半（那会截出 `…e(fp)))}'` 这种垃圾尾巴）。
    原始命令一字不丢：日志行 / `jobs.json` 的 `cmd` 字段仍是原文。
    """
    raw = _squeeze(cmd)
    if not raw:
        return "运行一条命令"
    if not enabled():
        return raw
    body = _LEAD_CD.sub("", raw) or raw
    for rx, phrase in _CMD_RULES:
        if rx.search(body):
            if phrase in ("运行脚本",):        # 带上脚本名：告诉用户动的是哪个文件
                m = _PY_SCRIPT_ARG.search(raw)
                return "运行脚本 %s" % (m.group(1) if m else _short(raw, 24))
            return phrase
    if not looks_like_command(raw):
        if _XML_ISH.match(raw):
            return "收到一条系统通知"         # 注入块没剥干净时的兜底，别把标签怼给用户
        # 用户原话：**只截头**（保留最该看的前半句），不做中段省略——面板元信息「在做什么」
        # 是单行简洁字段，长原话交给走马灯滚也读不完；完整原文仍在 `cmd` 字段里。
        return _head(raw, 60)
    name = _prog_name(body)
    for rx, phrase in _PROG_PHRASE:
        if rx.match(name):
            return phrase
    if name:
        return "运行命令 %s" % _short(name, 24)
    return "运行一条系统命令"


# ────────────────────────────────────────────────────────── 工具调用 → 人话
# ⚠️ 这张表就是**步骤条前缀**（`执行命令` / `编辑文件` / `子代理` …）——v1.0.66 起会话内容
#    的工具行也用它，两处**逐字一致**（见 `plain_tool` 的说明）。
_TOOL_ZH = {
    "Read": "读取文件",
    "Write": "写入文件",
    "Edit": "编辑文件",
    "NotebookEdit": "编辑笔记本文件",
    "Bash": "执行命令",
    "PowerShell": "执行命令",
    "Grep": "搜索内容",
    "Glob": "查找文件",
    "WebFetch": "读取网页内容",
    "WebSearch": "上网搜索",
    "Agent": "子代理",
    "TaskCreate": "建任务",
    "TaskUpdate": "更新任务",
    "TaskOutput": "读取后台输出",
    "TaskList": "查看任务清单",
    "TaskStop": "停掉后台任务",
    "TodoWrite": "更新待办清单",
    "present_files": "把结果展示给你看",
    "AskUserQuestion": "向你确认一个问题",
    "Skill": "调用一项技能",
    "SkillManage": "整理技能",
    "ListMcpResources": "查看可用的外部数据源",
    "ReadMcpResource": "读取外部数据源内容",
}


def _brief_input(tool, tinput):
    """从工具入参里挑出「最该让人看到的那一个」，用于拼人话短语。"""
    if not isinstance(tinput, dict):
        return ""
    if tool in ("Read", "Write", "Edit", "NotebookEdit"):
        return _base(tinput.get("file_path"))
    if tool == "Grep":
        return _short(tinput.get("pattern"), 22)
    if tool == "Glob":
        return _short(tinput.get("pattern"), 22)
    if tool == "WebSearch":
        return _short(tinput.get("query"), 22)
    if tool == "WebFetch":
        u = str(tinput.get("url") or "")
        m = re.match(r"https?://([^/]+)", u)
        return m.group(1) if m else _short(u, 22)
    if tool == "Agent":
        d = tinput.get("description") or tinput.get("prompt") or ""
        if isinstance(d, list):
            d = " ".join(str(x) for x in d)
        return _short(d, 22)
    if tool in ("TaskCreate", "TaskUpdate"):
        return _short(tinput.get("subject") or tinput.get("taskId"), 22)
    if tool == "TaskOutput":
        # 后台任务 id（形如 bg-xOWhtD）对用户毫无意义 → 内容留空，只显示前缀「读取后台输出」。
        # 原始 id 仍完整留在日志行里（`hook_auto_step._label_raw`），排障不受影响。
        return ""
    if tool == "Skill":
        return _short(tinput.get("skill") or tinput.get("command"), 22)
    return ""


def plain_tool(tool, tinput=None):
    """工具调用 → **唯一权威**的动作文案：`<前缀>: <人话内容>`（v1.0.66）。

    前缀沿用步骤条既有的中文前缀（`执行命令` / `编辑文件` / `子代理` …，用户 2026-10-04
    定的口径「保持原来的输出格式，只把内容换成人话」）；内容走人话化。

    ⚠️ **步骤条与会话内容必须逐字一致**：早期步骤条自己拼前缀、会话内容行只有内容，
    同一件事在一张卡里两种措辞（用户 2026-10-04 报「步骤前缀『执行命令：』与会话内容行
    表达重复，可统一」）。现在两处都调这个函数，改文案只改这里。
    """
    tool = str(tool or "")
    tinput = tinput if isinstance(tinput, dict) else {}
    if not enabled():
        return "%s: %s" % (tool, _short(_brief_input(tool, tinput) or ""))
    if tool in ("Bash", "PowerShell"):
        body = plain_command(tinput.get("command"))
    else:
        body = _brief_input(tool, tinput)
    head = _TOOL_ZH.get(tool)
    if not head:
        # 未知工具：保留工具名（含英文）便于排障，但前缀中文，别让小白以为面板坏了
        return ("其他操作（%s）%s" % (tool, ("：" + body) if body else "")).strip()
    return ("%s: %s" % (head, body)) if body else head


# ─────────── 执行项的类型图标 key（**逐字照搬**会话窗的 toolIconEntries，2026-10-04 用户定）
# 来源：app.asar `packages/conversation-render/src/scenarios/common/tool-call/tool-icon-registry.ts`
# 的 `toolIconEntries`（工具名清单原样保留）；匹配策略也一样 = 精确（大小写不敏感）
# → 前缀（最长前缀优先）→ 兜底。key → 具体图标在 `live_panel.ICON_KEY2COMP`。
_TOOL_KEY_EXACT = {
    "read": ["Read", "read_file", "NotebookRead", "read_lints", "preview_url", "PreviewUrl",
             "OpenResultView", "present_files", "PresentFiles", "ReadMcpResource",
             "FetchMcpResource", "ListMcpResources", "ConversationSearch"],
    "edit": ["Write", "Edit", "MultiEdit", "write_to_file", "replace_in_file", "append_to_file",
             "edit_file", "NotebookEdit", "NotebookWrite", "IDEWriteFile", "IDEReplaceFile",
             "ReplaceFile", "SaveMemory", "UpdateMemory"],
    "run": ["Bash", "PowerShell", "execute_command", "run_command", "run_terminal_cmd",
            "BashOutput", "KillShell", "TerminalExecutor", "CloudStudioExecuteCommand",
            "InstallBinary"],
    "search": ["Glob", "Grep", "search_files", "search_file", "search_content",
               "codebase_search", "CodebaseSearch", "RAG_search", "ToolSearch", "tool_search",
               "list_code_definition_names", "SearchIntegration", "RgSearchContent", "LSP"],
    "folder": ["list_files", "list_dir", "FileUpload", "EnterWorktree", "LeaveWorktree"],
    "web": ["WebFetch", "WebSearch", "web_fetch", "web_search"],
    "delete": ["DeleteFiles", "delete_files", "delete_file", "CronDelete", "TeamDelete"],
    "skill": ["Skill", "SkillManage", "UseSkill", "use_skill", "SlashCommand"],
    "done": ["completion", "finish_task", "plan_attempt_completion", "DeliverAttachments",
             "weixinpay"],
    "plan": ["plan_task", "PlanCreate", "plan_create", "PlanUpdate", "plan_update", "TaskCreate",
             "create_tasks", "TaskGet", "TaskUpdate", "update_task", "TaskList", "list_tasks",
             "TaskStop", "TaskOutput", "append_task", "EnterPlanMode", "ExitPlanMode",
             "TodoWrite", "todo_write", "task", "Task", "CronCreate", "CronList", "PdcCreate",
             "pdc_create", "PdcUpdate", "pdc_update", "AutomationUpdate"],
    "agent": ["Agent", "TeamCreate", "team_create", "SendMessage", "send_message",
              "AskUserQuestion", "ask_followup_question", "dispatch_specialist",
              "send_to_specialist", "dispatch_to_specialist", "list_experts",
              "list_specialists", "list_environments", "session",
              "list_specialist_sessions", "create_specialist_session"],
    "image": ["ImageGen", "image_gen", "ImageEdit", "image_edit", "VideoGen", "video_gen"],
    "widget": ["ShowWidget", "ReadMe"],
    "database": ["SupabaseExecuteSql", "SupabaseGetLogs", "SupabaseApplyMigration",
                 "SupabaseListMigration", "SupabaseListTables"],
    "cloud": ["CloudStudioDeploy", "CloudStudioFetchLog", "connect_cloud_service",
              "ConnectCloudService"],
    "debug": ["DebugIssueReproduction", "DebugIssueConfirm"],
    "location": ["get_location", "GetLocation", "poi_query", "PoiQuery", "pick_location",
                 "PickLocation", "mcp__connector-proxy__pick_location"],
    "tool": ["mcp_call_tool", "mcp_get_tool_description", "call_integration",
             "call_tcb_integration", "call_eop_integration", "call_anydev_integration",
             "call_lighthouse_integration", "search_integration_tool", "defer_execute_tool"],
}
_TOOL_KEY_PREFIX = [                      # 与会话窗同序，但按「最长前缀优先」排序后再匹配
    ("mcp__browser__", "browser"), ("mcp__computer-use__", "computer"),
    ("mcp__connector-proxy__", "location"), ("supabase", "database"),
    ("cloudstudio", "cloud"), ("debug", "debug"), ("mcp__", "tool"),
]
_TOOL_KEY_PREFIX.sort(key=lambda x: -len(x[0]))


def _norm_tool(name):
    """与会话窗同款归一化：小写 + 去下划线/连字符（"ToolSearch"→"toolsearch"）。"""
    return re.sub(r"[_\-]", "", str(name or "")).lower()


def tool_icon_key(tool, text=""):
    """执行项的图标 key（照搬会话窗 `resolveIconComponent` 的策略）。判不出 → "tool" 兜底。"""
    if not str(tool or ""):
        return "tool"
    n = _norm_tool(tool)
    for key, names in _TOOL_KEY_EXACT.items():
        if _norm_tool(key) == n or any(_norm_tool(x) == n for x in names):
            return key
    for prefix, key in _TOOL_KEY_PREFIX:
        if n.startswith(_norm_tool(prefix)):   # 前缀也要过同一套归一化（下划线已被剥掉）
            return key
    # 兜底再试一层：工具名判不出 → 按人话短语的动词判（read/edit/run/search 之外不给猜）
    s = str(text or "").lstrip(" ·-—")
    for rx, key in ((re.compile(r"^(修改|编辑|写入)"), "edit"), (re.compile(r"^(读取|查看|浏览|打开)"), "read"),
                    (re.compile(r"^(运行|复跑|执行|测试|安装|重启|清理)"), "run"),
                    (re.compile(r"^(搜索|查找|检索|排查)"), "search")):
        if rx.match(s):
            return key
    return "tool"


# ──────────────────────────────────────────────────────── 助手正文 → 单行人话
# markdown 噪音清洗（面板里是单行展示，不渲染 markdown）
_MD_RULES = [
    (re.compile(r"^\s{0,3}#{1,6}\s*", re.M), ""),                       # 标题
    (re.compile(r"^\s{0,3}[-*+]\s+", re.M), "· "),                      # 列表 → ·
    (re.compile(r"^\s{0,3}>\s?", re.M), ""),                            # 引用
    (re.compile(r"^\s*[-*_]{3,}\s*$", re.M), " "),                      # 分割线
    (re.compile(r"\*\*(.+?)\*\*", re.S), r"\1"),                        # 粗体
    (re.compile(r"~~(.+?)~~", re.S), r"\1"),                            # 删除线
    (re.compile(r"(?<![*\w])\*(?!\s)(.+?)(?<!\s)\*(?![*\w])", re.S), r"\1"),   # 斜体
    (re.compile(r"\[([^\]]+)\]\([^)]*\)"), r"\1"),                      # 链接 → 文字
    (re.compile(r"^\s*```.*$", re.M), " "),                             # 代码块围栏
]

# 受保护片段：这些位置**不做**名词翻译（避免把文件名/代码/链接里的英文翻坏）
_PROTECT = re.compile(
    r"`[^`]*`"                                          # 行内代码
    r"|https?://\S+"                                    # 链接
    r"|[A-Za-z0-9_.\-]*[/\\][A-Za-z0-9_.\-/\\]+"        # 路径
    r"|[A-Za-z0-9][A-Za-z0-9_.\-]*[._\-][A-Za-z0-9_.\-]+"   # 含 . _ - 的标识符
)

# 常见技术名词 → 中文（只收「当独立单词出现」时几乎不歧义的词）
_GLOSSARY = [
    (r"\bpanels?\b", "面板"), (r"\bskills?\b", "技能"), (r"\bhooks?\b", "自动触发器"),
    (r"\bscripts?\b", "脚本"), (r"\bversions?\b", "版本"), (r"\breleases?\b", "版本发布"),
    (r"\brepos?(?:itory|itories)?\b", "代码仓库"), (r"\bcommits?\b", "提交"),
    (r"\bpushes?\b|\bpushed\b", "推送"), (r"\bbuilds?\b", "构建"), (r"\bdeploys?\b", "部署"),
    (r"\bcache\b", "缓存"), (r"\blogs?\b", "日志"), (r"\bconfigs?\b", "配置"),
    (r"\btimeouts?\b", "超时"), (r"\bbugs?\b", "缺陷"), (r"\btests?\b", "测试"),
    (r"\blayouts?\b", "布局"), (r"\brender(?:s|ed|ing)?\b", "渲染"),
    (r"\bpreviews?\b", "预览"), (r"\btemplates?\b", "模板"), (r"\bbackups?\b", "备份"),
    (r"\bsyncs?\b|\bsynced\b", "同步"), (r"\bqueries\b|\bquery\b", "查询"),
    (r"\bpatterns?\b", "匹配规则"), (r"\bfiles?\b", "文件"), (r"\bpaths?\b", "路径"),
    (r"\benvs?\b|\benvironment\b", "运行环境"), (r"\bAPIs?\b", "接口"),
    (r"\bCHANGELOG\b", "更新说明"), (r"\bREADME\b", "使用说明"),
]
_GLOSSARY = [(re.compile(rx, re.I), zh) for rx, zh in _GLOSSARY]

_INLINE_CMD = re.compile(
    r"^(?:python[\d.]*|pip[\d.]*|git|npm|pnpm|yarn|node|npx|curl|wget|ls|dir|cd|cat|head"
    r"|tail|grep|rg|find|sed|awk|mkdir|cp|mv|rm|zip|tar|unzip|taskkill|tasklist|netstat"
    r"|powershell|pwsh|echo|set|export|timeout|which|where|wc|ssh)\b"
    r"|^[\w./\\\-]+\s+\S", re.I)


def _plain_inline_code(code):
    """行内代码：像「命令」的走命令翻译，否则原样保留（多为文件名 / 变量名）。"""
    s = str(code or "").strip()
    if not s:
        return ""
    if _INLINE_CMD.search(s):
        return plain_command(s)
    return s


# 英文引导句：「助手先写一句英文引出、破折号/冒号后面才是那句中文说明」——
# 实测很常见（`Now let me implement the core change in progress.py — 把「会话流」逐条还原`）。
# 给小白看时**只留那句中文**（丢的是"我这就去做 XX"这类过程话，信息不丢）。
# ⚠️ 只有"后面真有中文"才剥：纯英文句（`Now rewrite sync_stream to filter kinds:`）
#    原样保留 —— 宁可留英文，也不能把整句删空。
_EN_LEAD = re.compile(
    r"^\s*(?:(?:now|so|ok|okay|alright|next|first|then)[\s,，]+)?"
    r"(?:let me|let's|i'll|i will|i am going to|i'm going to|i need to|i want to|i should"
    r"|we'll|we will|we need to)"
    r"[^—–:：。；;，,]{0,200}?[—–:：]\s*", re.I)
_CJK = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")


def _strip_en_lead(s):
    m = _EN_LEAD.match(s)
    if not m:
        return s
    rest = s[m.end():]
    return rest if _CJK.search(rest) and len(rest) >= 4 else s


def plain_text(text, limit=0):
    """助手正文 → 「单行 + 人话」。面板「会话内容」区一行一条，所以这里把换行压成空格。

    limit>0 时按字符数截断（存储侧用，避免 payload 过大）。
    """
    s = str(text or "")
    if not enabled():
        s = _squeeze(s)
        return s[:limit] if limit and len(s) > limit else s
    for rx, rep in _MD_RULES:
        s = rx.sub(rep, s)
    # 行内代码单独处理成「命令 → 人话」
    s = re.sub(r"`([^`]+)`", lambda m: _plain_inline_code(m.group(1)), s)
    s = _squeeze(s)
    s = _strip_en_lead(s)
    # 分段做名词翻译，跳过受保护片段
    out, pos = [], 0
    for m in _PROTECT.finditer(s):
        out.append(_gloss(s[pos:m.start()]))
        out.append(m.group(0))
        pos = m.end()
    out.append(_gloss(s[pos:]))
    s = _squeeze("".join(out))
    return s[:limit] if limit and len(s) > limit else s


def _gloss(seg):
    for rx, zh in _GLOSSARY:
        seg = rx.sub(zh, seg)
    return seg


# ────────────────────────────────────────────────────── 会话内容：类型图标
# 用户 2026-10-04：「保留原来会话前边的图标以区分输出类型；没有图标的加一个，
# 比如『正在思考』可以放个思考的小图标」→ 2026-10-04 二次确认：「**保留输出前的
# 默认图标即可**，没有的才找个风格相同、类型相配的图标」。
# 于是判据顺序是（顺序本身就是口径）：
#   ① 正文**已经带图标**（助手自己写的前导 emoji / 符号）→ 一个字都不动；
#   ② 没带 → 按开头几个字猜类型，给一个**同一套 emoji 家族**里最贴近的；
#   ③ 猜不出 → 💬（最中性的"说了一句话"）。
# 全套图标（统一彩色 emoji 风格，别再引入别的风格）：
#   ✅ 完成   ❌ 失败   ⚠️ 提醒   📊 数据/结果   🔍 排查/查看   📌 总结/结论
#   💡 建议   🧭 方向/方案   💭 进行中（正在做/打算做）   💬 普通一句话
# 面板另外两类行由 `progress.stream_row` 直接给：⚙️ 工具调用 / 💭 正在思考。
_EMOJI_HEAD = re.compile(
    r"[\U0001F000-\U0001FAFF"      # 大部分 emoji
    r"\u203C-\u20FF"               # ‼ ⁉ ™ ℹ …
    r"\u2100-\u218F"               # ℹ ⓘ 等字母式符号
    r"\u2190-\u27BF"               # ← → ⚙ ✅ ➕ …
    r"\u2900-\u297F"               # ⤴ ⤵ 等箭头
    r"\u2B00-\u2BFF"               # ⬅ ⭐ 等
    r"\uFE0F\u200D]"               # 变体选择符 / 零宽连接符
)
_ICON_RULES = [
    # ⚠️ 别在零宽断言后面加 `?`（`\b?` 在 Python 3.11+ 直接报 "nothing to repeat"，
    #    会让整个模块 import 失败 → 人话化静默失效）
    # v1.0.63：值从 emoji 改成**图标 key**——面板拿 key 去 `app_icons.APP_ICONS`
    # 取**应用自带的单色 SVG**（用户口径：用原来的图标，自定义的也必须同样单色）。
    (re.compile(r"^\s*(?:✅|✔|☑|完成|已完成|已改好|已修好|搞定了|搞定|done)", re.I), "ok"),
    (re.compile(r"^\s*(?:❌|失败|报错|出错|错误|挂了)"), "fail"),
    (re.compile(r"^\s*(?:⚠️|⚠|注意|风险|警告|但要注意|小心)"), "warn"),
    (re.compile(r"^\s*(?:📊|数据|统计|指标|结果|对比)"), "data"),
    (re.compile(r"^\s*(?:🔍|查|搜|排查|定位|看看|检查)"), "search"),
    (re.compile(r"^\s*(?:📌|总结|结论|要点|一句话)"), "pin"),
    (re.compile(r"^\s*(?:💡|建议|推荐|提示|小技巧)"), "idea"),
    (re.compile(r"^\s*(?:🧭|方向|方案|路线|计划)"), "route"),
    (re.compile(r"^\s*(?:正在|正在跑|正在做|现在|接下来|先|马上|准备|等待|稍等|我来|我先把"
                 r"|我改|我在|先跑|正在验证)"), "loading"),
    # 英文开头的同义说法（助手常混着写）：先给"进行中"这一档
    (re.compile(r"^\s*(?:now\s+)?(?:let me|let's|i'll|i will|i am going to|i'm going to"
                 r"|i need to|i want to|i should|we'll|we will|we need to|next|first|then"
                 r"|let us)\b", re.I), "loading"),
    (re.compile(r"^\s*(?:found|identified|located|confirmed|verified|checked)\b", re.I), "search"),
    (re.compile(r"^\s*(?:fixed|done|finished|completed|solved|works|working now)\b", re.I), "ok"),
    (re.compile(r"^\s*(?:warning|warn|note|careful|caution|be careful|heads up)\b", re.I), "warn"),
]


# 图标前面可能还套着一层「壳」（引号 / 括号 / 列表点 / 破折号）：判定"有没有图标"时要
# 先把壳剥掉，否则 `「✅ 完成」` 会被当成"没有图标"，前面再叠一个 → `💬 「✅ 完成」`。
_LEAD_WRAP = re.compile(r"^[\s\"'“”‘’「」『』（）()\[\]【】<>《》\-—–·•*]+")


def icon_key(text):
    """给一条 会话内容挑「类型图标 key」；正文已自带前导图标时返回空串（不另配，原样保留）。

    v1.0.63 起返回的是 **key**（ok/fail/warn/data/search/pin/idea/route/loading/chat），
    面板据此渲染 `app_icons.APP_ICONS` 里的**应用自带单色 SVG**——不再往正文里拼 emoji
    （用户口径：不是要"同款"，是要**原来的图标**；自定义的也必须同样单色）。
    key → 具体图标的对应表在 `live_panel.ICON_KEY2COMP`。
    """
    s = str(text or "").lstrip()
    if not s:
        return ""
    core = _LEAD_WRAP.sub("", s) or s
    if _EMOJI_HEAD.match(core[0]):
        return ""                      # 助手自己带了图标 → 正文原样，别再配一个
    for rx, key in _ICON_RULES:
        if rx.search(s):
            return key
    return "chat"                      # 兜底：普通一句话
