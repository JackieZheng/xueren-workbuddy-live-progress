---
id: xueren-workbuddy-live-progress
name: 雪人老师·[WorkBuddy]任务执行进度·实时面板
title: 雪人老师·[WorkBuddy]任务执行进度·实时面板
description: WorkBuddy 会话任务通用「任务执行进度·实时面板」。当启动任何耗时/后台任务（Bash run_in_background、长脚本、镜像/导出/爬取等）时使用；hook 自动登记任务并提醒打开面板，Python 脚本一行接入精确进度。
slug: xueren-workbuddy-live-progress
displayName: 雪人老师·[WorkBuddy]任务执行进度·实时面板
summary: 完整任务执行过程（前台+后台）实时看板：从接到命令→执行中→结束，时间线/步骤进度条/进度环/速率/ETA/日志；前台任务每个工具调用默认自动映射成步骤，无需手动接入。
description_en: A universal live progress panel for background tasks in a session.
version: 1.0.69
author: 雪人
license: MIT
allowed-tools: ""
display_name: xueren-workbuddy-live-progress
display_name_zh: 雪人老师·[WorkBuddy]任务执行进度·实时面板
trigger: ["实时进度", "进度面板", "后台任务跑得怎么样", "看看进度", "预计多久跑完"]
examples: "用户说「看看后台跑得怎么样」→ present_files 打开 http://127.0.0.1:8791/；长脚本接入 progress.Progress 即可精确上报。"
platforms: [WorkBuddy]
github: https://github.com/JackieZheng/xueren-workbuddy-live-progress
skillhub: https://skillhub.cn/skills/indiv-xueren/xueren-workbuddy-live-progress
metadata:
  author: 雪人
  category: 效率工具
description_zh: WorkBuddy 会话任务通用「任务执行进度·实时面板」。hook 自动登记 Bash 后台任务并提醒代理打开面板；Python 脚本一行接入精确进度（进度环/速率/ETA/推进曲线/日志尾部）。
---

# 雪人老师·[WorkBuddy]任务执行进度·实时面板

一次用户请求从「接到命令」到「结束」的完整执行过程，实时看板在 `http://127.0.0.1:8791/`——**前台与后台任务都覆盖**，不限于后台 Bash。

每张卡五件事：📥 接到命令 → ▶ 开始执行 → ⏱ 已运行（实时计时）；步骤进度条（✓ 已完成 / 🔄 进行中 / ⏳ 待办）；量化进度（进度环 + 已完成/剩余 + 速率/预计完成）；**会话内容**（助手说过的话 + 执行项，一行一条）；日志（任何无法量化的过程一律进日志，实时滚动）。

## 会话内容（v1.0.55 建区，曾名「AI 输出」；**v1.0.64 改名**——v1.0.62 起已收全会话流，旧名名不副实）

面板此前只有「机器日志」，看不到助手自己说了什么——助手正文不经工具、没有现成 hook 事件，所以走**双通道**：

- **自动（默认开）**：读本会话 transcript `~/.workbuddy/projects/<工作区>/<session_id>.jsonl`，
  把**本轮**（最后一条用户消息之后）助手在会话里出现过的话**逐条**同步进当前会话卡的 **会话内容**区：
  - **回合进行中就同步**（v1.0.59）：transcript 是边说边写的，会话卡每 3 秒做一次尾部读（**文件没长就不读**），
    不用等回合结束；回合结束由 Stop hook 补一次收尾。
  - **增量 + 去重**：游标 `reply_mid`（transcript 行 id）保证同一条只上一次；首次补最近 12 条、
    游标丢失补最近 8 条，绝不把一整轮刷屏。「不看整个会话考古」——只取当前这一轮。
  - **收全会话流（v1.0.62，默认）**：不只正文——**修改/读取/编辑/思考/测试这些执行项也逐条进**，
    每条带会话窗同款默认图标（✏️ 改 / 👁 看 / ⌨️ 跑 / 🔍 搜 / 💭 深度思考）。
    嫌吵可收窄：`LIVE_PROGRESS_STREAM_KINDS=text`（思考改回活体指示、工具行只进步骤条；
    改完面板 10 秒内热更新，`/api/jobs` 顶层 `stream_kinds` 能看到**进程内实际生效**的值）。
  - **思考块两种形态别混**：历史行 =「💭 深度思考」（落卡，对齐会话窗的「深度思考」标签）；
    活体行 =「💭 正在思考…」（`stream_kind=think` 且末行不是思考历史行时，面板末尾临时挂、不落盘）。
  - 只在文件尾部读（默认 6MB / 最多解析 6000 行），读不到就静默跳过，绝不影响会话。想彻底关掉：`LIVE_PROGRESS_REPLY=0`。
- **主动**：多回合长任务里想先在面板给个说法，直接 `python scripts/progress.py reply --text "已扫完 1200 个文件，正在预筛…"`（不带 `--id` 时按 `--session` 写 `sess-<session_id>`）。脚本内嵌用 `progress.reply("…", job_id="sess-xxx")`。
- **排障**：`python scripts/progress.py stream --session <sid>` 看「窗口内几条 / 会入卡几条 / 具体哪几行」（默认只看不写）；
  加 `--sync` 真投递；`--backfill N` 把**整会话**最近 N 条正文补进卡（卡被重建或刚跨回合、看着空时用）；
  `python scripts/_debug_reset_reply.py <sid>` 一键「清空 + 按当前口径重同步」。

展示约定：卡片里 **会话内容区（定高 80px = 4 行，在推进曲线与日志之间）**一行一条、超宽走中段省略
（全文挂 `title` 悬停）、超出可滚动，只取尾部若干条渲染；空态显示「本轮会话还没内容 · 正文与执行项会自动同步到这里」
——**占位也要占**，所有卡片因此仍然等高。会话内容与日志互不干扰：它不计入日志行数，也不写进 `log` 列表。

**人话化与图标**（`scripts/humanize.py`，总开关 `LIVE_PROGRESS_HUMANIZE=0`）：正文压成单行、
去 markdown、技术名词中文化、行内命令讲通俗说法，并把英文引导句剥掉只留中文（`Now let me … — 真正的那句中文`）。
每条前面一个**类型图标**：正文自带图标就**一个字不动**（`ic` 留空），没有才按类型补一个；
图标**一律用应用自带的单色 SVG**（v1.0.63，用户口径：不是"同款"，是**原来的图标**，自定义的也必须同样单色）
——`scripts/app_icons.py`（从 `app.asar` 的 `wb-components/Icon/icons/*.tsx` **原样照搬**，勿手改，
重抽用 `_probe_gen_app_icons.py`），面板按行的 `ic` key 渲染 `<svg viewBox fill>`（`currentColor` 随文字色）：
- **执行项**（key 归属逐字照搬会话窗 `toolIconEntries`，`humanize.tool_icon_key`）：
  `read` 眼睛 / `edit` 铅笔 / `run` 终端 / `search` 放大镜 / `web` 地球 / `skill` / `plan` / `agent` / `image` … 兜底 `tool` 扳手；
- **正文**（`humanize.icon_key`）：`ok` 绿圈勾 / `fail` ✕ / `warn` 橙三角（这三个是应用状态图标、原色）
  / `data` / `search` / `pin` / `idea` / `route` / `loading` / 兜底 `chat` 气泡；
- **思考**：`deep`（应用自己的 WbDeepIcon）；历史行「深度思考」、活体行「正在思考…」都走它。
⚠️ 日志**不做**人话化（保留原始工具名与原始命令，给专业人员排障用）。

**步骤条 = 会话内容行，逐字一致**（v1.0.66）：两处都调 `humanize.plain_tool()`（`<前缀>: <人话内容>`，
前缀沿用步骤条既有的中文前缀），**同一件事在卡里不许有两种措辞**；改文案只改 humanize 一处
（契约由 `_test_humanize.py` 的「步骤条 == 会话内容行」断言锁住）。
**元信息「在做什么: …」的分流**（v1.0.66，用户报「接到命令的人话误判」）：会话卡的 `cmd` 存的是
**用户原话**（hook_prompt 已剥掉 WB 注入块），`plain_command` 兜底前先判「像不像命令」
（`humanize.looks_like_command`，以可执行名开头才算）——是命令但规则不认 → 只报**执行程序名**
（`运行一段 Python 代码` / `运行命令 foo.exe`，不再把整条命令首尾各截一半）；是自然语言原话 →
**只截头 60 字**原样展示（不做中段省略）；`<task-notification>` 这类注入 → 「收到一条系统通知」。
历史卡片里的原始注入由 `live_panel._cmd_fields` 再剥一层。

## 默认自动映射

**计时从「用户按下发送」那刻起**（v1.0.56）：`UserPromptSubmit` hook 一收到提交就立刻建卡钉锚点（实测建卡 6~7ms、整条 hook 60~130ms，远低于 15s 时限），面板「📥 接到命令 / ▶ 开始执行 / ⏱ 已运行」全从这一刻算起，**不是 AI 开工才开始走**；纯图片、空文本、只有附件的提交也照样建卡，防止退化成「AI 开始处理才计时」。面板拉起改走后台线程，冷启动再慢也不会拖住这条 hook。

**没有文本时卡片写人话**（v1.0.57）：整条提交没有一句用户真话（纯图片 / 纯音频 / 纯文件 / 只有引用标记）时，标题自动落成附件统计，例如`用户发送了 9 张图片`、`用户发送了 3 张图片（1 个音频、1 个视频、2 个文件）`；类别多到装不下会往回砍类并补「…」，绝不硬截断成半句话。反过来，**图文混发时永远是那句真话优先**，附件统计只顶在「一条真话都没有」的场合。

对每个用户请求自动建一张 `sess-<session_id>` 会话卡；你执行的**每个工具调用默认自动变成它的一个步骤**——Bash / Write / Edit / Agent / TaskOutput 等**主干工具成步**，Read / Grep / Glob / WebFetch / WebSearch 等**轻量工具只进日志**。想自己规划步骤：`begin --id sess-<session_id> --steps "准备,处理,收尾"` 接管；一旦接管（`manual_steps=True`）自动映射退居日志，绝不打乱你的步骤条。`UserPromptSubmit` 没派发时首个工具调用也会兜底建卡。

## 打开 / 关闭面板

- **全自动（默认）**：三个时机 hook 都会自动确保面板在线——**会话启动**（SessionStart，含 WB 重启后恢复会话）、**提交命令**（UserPromptSubmit）、**起后台任务**（`Bash + run_in_background`），统一走 `panel_ctl.py start --auto`（WMI 会话外创建 + 空闲 30 分钟自退）；代理再 `present_files` 打开——用户零操作。
- **会话里说一句**：「打开进度面板」/「关掉进度面板」→ `python scripts/panel_ctl.py start|stop`（`restart` / `status` / `check` 同理，幂等安全）。
- **双击**：`start_panel.bat` / `stop_panel.bat`（不经代理，可长期常驻）。
- 服务已在跑时浏览器直接开地址即可（**关掉预览标签不等于停服务**）；`panel_ctl.py check` 一句话回答「会不会拖住会话」；换端口 `panel_ctl.py start --port 8792`。

> 🔴 **铁律：绝不用 `Bash run_in_background` 启动 `live_panel.py`。** 面板是长驻服务，一旦成为会话后台任务就**永不结束**→「任务跑完了会话却停在不完成」。合法路径只有 `panel_ctl.py start`（WMI 会话外）与双击 `start_panel.bat`；真有漏网实例 `panel_ctl.py check` 会判定，且 `--auto` 有空闲自退兜底。

> 🔴 **收尾契约（v1.0.33 建立，v1.0.36 提速）**：会话卡收尾以 **WB 权威会话状态**为准——面板采样循环每 1 秒读 `~/.workbuddy/workbuddy.db` 的 `sessions.status`（WB 界面同源）自动对表（`sync_wb_sessions` 内部 1s 限流）：`working`（执行中）→ 保持运行中；`completed/archived/已删除` → done；`error` → failed；`terminated` → aborted，**≤2 秒跟上**；前端轮询同为 1 秒。你**无需**手动 `finish-session`，也不会出现「没结束却显示结束」或「结束了还挂 running」。兜底：后台卡（`bg-*`）由 `hook_bg` 在最后一个后台任务完成时级联收（漏读输出时用 `finish --id bg-xxx`）；脚本自建卡由 `with Progress` 退出自动收；db 查不到的会话 / 异常中断（崩溃、关窗口）由 `SessionEnd` 与「30 分钟无更新」gc 收为 aborted。
> ⚠️ **唯一不能省的**：本轮若有**后台任务仍在跑**，结束前必须 `present_files` 打开面板（Stop hook 会 exit 2 拦截提醒）。

> ⚠️ 服务是**内存态**：WB 重启或 `stop` / 空闲 30 分钟后服务会消失（页面变空白）。但**下次会话启动 / 提交命令 / 起后台任务时 hook 会自动把它拉起来**，无需手动；也可手动 `panel_ctl.py start` 或双击 `start_panel.bat`。会话 job 只回收代理自己 spawn 的后代进程，故启动一律走 WMI；待办/历史数据（`~/.workbuddy/live-progress/jobs.json`）与进程无关，重启后照样可见。

## 开机 / 登录自启（v1.0.41）

想让面板「开机即常驻、首条命令零冷启动」，装下面两条通道（都幂等，重复跑无害）：

| 通道 | 装法 | 谁触发 |
|---|---|---|
| 启动文件夹 | `%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\LiveProgressPanel.bat`（默认已装） | explorer 登录后 |
| 计划任务 | `powershell -NoProfile -ExecutionPolicy Bypass -File scripts/install_panel_boot_task.ps1`（注册 AtLogOn；非管理员可能被 `0x80070005 拒绝访问` 拒掉，换「以管理员身份运行」的 PowerShell 重跑即可） | 用户登录 |

- 🔴 **启动器一律用 `pythonw.exe`，不是 `python.exe`**：`python.exe` 自带控制台窗口，登录时会弹一个 cmd 窗口（即使加了 `/MIN` 也只是一个最小化窗口，桌面照样脏）；`pythonw.exe` 根本没有控制台。计划任务的 Action 也**直接指向 `pythonw.exe`**，中间不要套 `powershell -File xxx.ps1`。
- **面板不等 WorkBuddy**：签到脚本必须等 WB 进程起来（AtRest 加密登录态要用客户端内存密钥，最多等 180s），面板只是本地 http + 读 `workbuddy.db` 磁盘文件 → 登录瞬间就绪，实测 **1 秒内**起来、`父进程 WmiPrvSE.exe`（会话外常驻）、窗口列表**零残留**。
- **收益就是干掉 hook 超时**：开机常驻后 `hook_session` / `hook_prompt` 走快路径（已在跑 → 0.x 秒返回），不再撞 `Hook timed out after 15000ms`；万一真没起来它们仍会兜底拉起（`--auto`，空闲 30 分钟自退）。两种策略是自洽的：`hook_bg._persist_desired()` 检测到 Startup bat 存在 → 兜底实例也常驻，不会「开机常驻、hook 反而 30 分钟自退」打架。
- 关掉自启：删掉 Startup 里的 bat，再跑 `scripts/uninstall_panel_boot_task.ps1`。
- 排查用日志 `~/.workbuddy/scripts/panel_boot.log`（每次登录后一行时间戳）；`panel_ctl.py status` 看运行实例与父进程。
- ⚠️ 查计划任务别用 `Get-ScheduledTask -TaskName`（只搜根路径），用户级任务在 `\<用户名>\` 下，用 `Get-ScheduledTask | ?{ $_.TaskName -like "*Panel*" }` 全量列。

## 你的工作方式

1. **本轮开始就打开面板**：`SessionStart`（会话启动）与 `UserPromptSubmit`（提交命令）hook 都会自动确保服务在线并建立会话卡，别等后台任务——本轮执行前就用 `present_files` 打开 `http://127.0.0.1:8791/`（hook 会提醒，但别依赖提醒）。
2. **接管会话卡**：执行任何任务时用 `progress.py begin --title "…" --steps "准备,处理,收尾"` 接管 `sess-<session_id>`，每完成一步 `progress.py step --id <id> --index N`。
3. **收尾已自动化**（见上）：无需手动 `finish-session`；只兜底处理「后台任务仍在跑时要开面板」与「个别没被 hook 触达的卡用 `list --running` 找出后 `finish --id <id>`」。
4. **写长任务脚本顺手接入**：`from progress import Progress` 一行上报。**能细分计数就给 `total`**（进度环/速率/ETA 全都有）；**中途拿不到任何进度的整体任务**（整段提交远端、单次不可分割转换）**别给 total，改给 `planned_sec` 预估总耗时**——面板走时间基线（已运行/预估、预计完成、超时显「已超预估」），比一直「采样中…」有用。
5. **不伪造进度**：数据一律来自真实登记，回复里的百分比必须与面板一致；`planned_sec` 是**显式声明的预估**，回复里要写明"预估"。
6. **删记录**：面板卡片右上角 `✕` 或折叠区「清空已结束」（二次确认）；脚本场景 `progress.py delete --id <id> [--force]` / `clear [--finished|--all]`。**运行中的任务默认删不掉**，要先确认再 `--force`。

## 执行流程

```bash
# Phase 1 · 确保面板在线（hook 通常已自动拉起，仅手动排查时用）
python scripts/panel_ctl.py status            # 在不在、哪个端口
python scripts/panel_ctl.py check             # 会不会拖住会话（父进程判定）
python scripts/panel_ctl.py start [--port 8791] [--auto] [--idle-exit 秒] [--ttl 秒]
python scripts/panel_ctl.py stop | restart
# ❌ 禁止 Bash run_in_background 跑 live_panel.py

# Phase 2 · 任务接入（CLI）
python scripts/progress.py begin --title "用户请求" --steps "准备,处理,收尾"
python scripts/progress.py steps --id <id> --set "准备,处理,收尾"   # 中途补/改
python scripts/progress.py step  --id <id> --index 1 [--name "处理"]  # 推进（0 基）
python scripts/progress.py start --title "导出报表" --total 120 --unit 条
python scripts/progress.py set --id <id> --done 30 --msg "30/120"   # 或 inc --n 5
python scripts/progress.py log  --id <id> --msg "..."
python scripts/progress.py plan --id <id> --sec 90                  # 不可细分：预估总耗时
python scripts/progress.py finish --id <id> [--failed] [--msg "…"]

# Phase 2 · 任务接入（Python，推荐）
# from progress import Progress
# with Progress("镜像 KB1→KB2", total=3286, unit="张") as p:
#     for i, item in enumerate(items, 1):
#         do_work(item); p.set(i, message=f"已镜像 {i}")
# with 正常退出自动 done，抛异常自动 failed；不可细分任务用 Progress(..., planned_sec=90)

# Phase 3 · 交付
python scripts/progress.py list --running   # 验收：本轮是否还有卡在跑（有则 finish --id <id>）
# 然后 present_files 打开面板，回复里给出高层摘要（运行中/已完成/失败 + ETA）
```

其余 CLI：`gc [--min 30] [--failed] [--dry]`（异常中断兜底）、`fix-mojibake [--dry]`（还原 UTF-8 被按 GBK 解码的标题/日志）、`list [--running] [--json]`、`delete --id <id> [--force]`、`clear [--finished|--all]`、`auto-step --id <id> --label "…"`（自动映射内部用）。

**桥接接入**（任务已在跑、脚本没接 progress）：`progress_bridge.py --id <id> --title "…" --state-file <状态文件> --total 3286 --unit 张 --poll 15 --stall 300 --settle 40`——轮询状态文件条目数作 done；停更超 `--stall` 秒判定结束；到量后还需再静默 `--settle` 秒才完成（防总量估低提前收尾）。

## 配置与参数

- 注册表 `~/.workbuddy/live-progress/jobs.json`（`LIVE_PROGRESS_DIR` 覆盖）；端口 8791（`LIVE_PROGRESS_PORT` 覆盖）；上限 40 条（按 `updated_at` 保留最新）。
- **每个任务保留的日志行数默认 40**（`progress.py` 的 `MAX_LOG`）：写满后**直接丢弃旧行**，所以长会话的早期日志不会留在注册表里 —— 卡片上「日志 N 行」到 40 就封顶。
  要留住更多历史：`LIVE_PROGRESS_MAX_LOG=300`（环境变量，取不到值即回退 40）。⚠️ 面板日志区**只渲染最后 10 行**（定高滚动框），放大上限并不会让可见行数变多。
- **会话卡按回合清零**（v1.0.65）：同一会话提交新命令 = 新回合——「会话内容」「日志（尾部）」「步骤」「计时锚点」全部**清零重开**（会话内容自 v1.0.60 起就是按回合的，v1.0.65 补齐日志）；「本轮 N 行 · 累计 M 行」里的**累计**计数保留，只作 tooltip 参考。脚本卡（`begin`，不带 round_reset）不受影响，日志照旧保留。
- hooks 安装 `python scripts/install_hooks.py`（幂等、自动备份 settings.json；`--status` / `--uninstall`）。
- **改代码即生效**：面板热更新——`scripts/` 下**任何运行期 `.py` 一改**（`live_panel.py` / `progress.py` / `humanize.py` …，即**非下划线开头**的那些；`_test_` / `_probe_` / `_debug_` 等开发级脚本改动**不触发**，改测试不该重启面板），运行中的面板 10 秒内用同参数重启（前端 build 变了自动 reload），通常无需手动 restart。
  ⚠️ **唯一例外**：这一批改的**正是指纹函数本身**（或面板已在跑的是旧逻辑）时，需手动 `panel_ctl.py restart` 一次完成"换代"，之后全自动；自动重启失败（端口被占）时 `panel_ctl.py upgrade` → `restart` → 换端口三选一。
- `panel_ctl.py status` 给出「运行中 build / 磁盘 build」，**该 build 只哈希页面源码（`PAGE`）**，不一致即改动未生效；但它对 `humanize.py` / `progress.py` 这类运行期模块的变化**不敏感**——"面板没变但 status 报一致 ✅"时，先怀疑热更新指纹（见 DEVLOG v1.0.61）。
- 看板标题 `📊 {SKILL.md 中文名} Ver:{version}`——启动时从 frontmatter 读，改版本号热更一下标题就跟着变。
- 陈旧判定：running 超 15 分钟无更新 → 显示「疑似已结束」；SessionStart 清 3 天前的已结束任务。
- 速率口径：需 ≥2 采样点 + 跨度 ≥20 秒 + `done` 有增长；`total=1` 或全程 `done` 不动**天然无速率**（是口径不是故障），此时改用细分计数或 `planned_sec`。
- 结束态一律**不算速率与 ETA**。

## 发布与自更新

| 通道 | 命令 |
|---|---|
| GitHub | `publish_skill.py --skill xueren-workbuddy-live-progress --user <your-github-user> --token <PAT>`（建仓库 + 推 + 自动 Release `v<version>`） |
| SkillHub | `publish_skillhub.py --skill xueren-workbuddy-live-progress --exclude "assets/*.png" --changelog "…"`（**拒收 png**，必须 `--exclude`；png 只服务 GitHub 仓库与本地，SkillHub 包内不插图） |
| 自更新 | `python scripts/check_update.py [--auto]`（查 GitHub / SkillHub 最新版，`--auto` 自动更新） |

`check_update.py`：`--auto --json`（结构化，供定时任务）/ `--force`（强制跑一遍）/ `--source skillhub|github|both` / `--backups`。零凭据（GitHub 匿名 API + SkillHub 公开接口），仓库坐标自 `github:`、SkillHub 标识自 `slug:` 解析；链路＝下载 → 包校验（须含 `SKILL.md` + `live_panel.py` + `progress.py`）→ 备份 `cache/backups/` → 同步（`cache/`、`.git` 不覆盖）→ 冒烟（`py_compile` + 版本核对 + 面板 200）→ 失败自动回滚。改完不用手动重启面板（热更新生效，hooks 也不用重装）。SkillHub 索引有延迟，刚发的版本搜不到属正常。

定时检测（每天 09:30）：`python ~/.workbuddy/skills/xueren-workbuddy-live-progress/scripts/check_update.py --auto --json`。

**文档插图一律用外链 URL**（v1.0.49 起 `README.md` 已改）：SkillHub 拒收 png，包内插图在 SkillHub 详情页必然裂，所以配图走 **GitHub raw 外链**
（`https://raw.githubusercontent.com/JackieZheng/xueren-workbuddy-live-progress/main/assets/panel_preview.png`）。
已验证 **SkillHub 与 GitHub 都支持外链图片**（SkillHub 详情页渲染的是 `SKILL.md` 正文、外链图直连加载成功），且**插图必须放正文段落级**（写在列表项里的 `![]()` 不会被渲染，会原样残留 markdown 原文）。
发布时 `--exclude "assets/*.png"` 照旧：png 只留在 GitHub 仓库与本地，不进 SkillHub 用户包。

**改过 `check_update.py` 后**：跑 `scripts/_test_check_update.py` 做离线自检（不联网、不碰真实目录，三层场景：正常更新 / 坏包回滚 / 零污染；跑法见 `docs/update-test.md`——该手册属开发内容，**不随 SkillHub / GitHub 发布物外发**，只在本地仓库与备份库里）。⚠️ 别用「跑一次 `--auto` 看结果」验收 —— 本地版本通常领先远端，只会走 `up-to-date` 快路径，更新链路一次都执行不到。

## 文件清单

- 根目录：`start_panel.bat`（双击启动）/ `stop_panel.bat`（双击停止）
- 开机自启（落到 `~/.workbuddy/scripts/`，**纯 ASCII/英文注释**）：`install_panel_boot_task.ps1`（注册 AtLogOn 计划任务）、`uninstall_panel_boot_task.ps1`（注销）、启动文件夹 `LiveProgressPanel.vbs`（登录时由 explorer 拉起；**wscript 窗口样式 0 全隐藏**——v1.0.69 前的 `.bat` 会在登录瞬间闪一个 cmd 黑窗，已替换；源码在 `scripts/panel_boot.vbs`）
- `scripts/progress.py`：登记库 + CLI（唯一写方，原子写 + 跨进程锁）
- `scripts/live_panel.py`：面板服务 8791，只读注册表 + `--idle-exit` / `--ttl` 看门狗
- `scripts/panel_ctl.py`：开关控制器（严格探活 / WMI 会话外启动 / 脱离会话校验）
- `scripts/progress_bridge.py`：状态文件桥接（轮询上报 + 停更收尾 + settle）
- hooks：`hook_bg.py`（后台登记 + 自动拉面板 + 完成回填）、`hook_prompt.py`（建会话卡 + 回合提醒，20 秒限频桶）、`hook_auto_step.py`（工具调用自动成步骤）、`hook_stop.py`（Stop 强制开面板 / SessionEnd 收）、`hook_session.py`（**会话启动即拉起面板** + 收异常中断卡 + 注入约定）、`install_hooks.py`（各事件 matcher 全用 `*`，覆盖 WB 重启后的会话恢复）
- `scripts/app_icons.py`：**应用自带单色 SVG 图标表**（v1.0.63，从 `app.asar` 的 `wb-components/Icon/icons/*.tsx` 原样照搬；`APP_ICONS` 内层节点 + `APP_ICON_BOX` viewBox + `APP_ICON_ATTRS` svg 层属性）——**生成物勿手改**，重抽跑 `scripts/_probe_gen_app_icons.py <asar> app_icons.py <组件名…>`
- `scripts/humanize.py`：「人话化」词典（命令行→在干什么、助手正文→单行中文、类型图标 key）——**步骤条 / 面板载荷 / 会话内容都用这一套**，别在调用方各写一份；总开关 `LIVE_PROGRESS_HUMANIZE=0`；日志**不走**它（保留原始命令给专业人员排障）
- `scripts/check_update.py`：版本自更新；`_test_check_update.py`（自更新离线自测，`docs/update-test.md`）、`_test_logcount.py`（日志累计行数单调性，**自隔离**）、`_test_humanize.py`（人话/图标契约）、`_test_reply_contract.py`（会话内容存储契约）、`_test_stream_contract.py`（会话流逐条抓取契约）、`_probe_transcript.py`（transcript 取证：`shape` / `stream` / `turns` / `line` / `fc` / `attach` / `user`）、`_probe_reply_rows.py` / `_probe_real_panel.py`（会话内容区渲染体检）、`_probe_panel_head.py`（抓真面板 HTML 头：标题版本号 / `\x01`·`\x08` 残留 / `/api/jobs` 顶层 `stream_kinds`，**验"磁盘改了线上有没有变"的权威口径**）、`_debug_reset_reply.py`（卡口径重置重同步）；`_demo_jobs.py` / `_probe_marquee.py` / `_test_hook.py`（演示与自测，可删）
- `docs/update-test.md`：自更新链路自检手册（开发用，**AI 工作流程不看这里**；**开发内容，两个发布渠道都不外发**）
- `docs/DEVLOG.md`：版本演进与踩坑记录（开发级，**AI 工作流程不看这里**；两个发布渠道都不外发）
- **文档分级铁律（2026-10-03 用户定策）**：`SKILL.md`（给 AI 的流程）+ `README.md`（给用户的功能介绍）= **产品级**，SkillHub 与 GitHub Release 都带；
  `docs/**`（DEVLOG、排障手册等）+ `_test_` / `_probe_` / `_demo_` 开头脚本 = **开发级**，**两个渠道一律不外发、也不搬去根目录**。
  新增开发文档一律直接落 `docs/`，别再往根目录丢。
- `assets/panel_preview.png`：横屏效果图（README 用）

## 注意事项

- 🔴 **面板绝不能成为会话的后台任务**（见铁律）；只有 WMI `Win32_Process.Create`（父进程 `WmiPrvSE.exe`）或双击 bat 启动的实例能跨会话常驻，代理直接 Popen 的实例命令结束即被回收；面板服务自身不登记为任务卡。
- **会话里也能开关面板**：说「打开/关闭进度面板」即可；`status` 严格探活（`/api/jobs` 含 `summary` 才算真面板）。
- **跨会话的旧任务不会自动出现**：hook 只认本会话启动的后台任务；早前启动的长任务用 `progress_bridge.py` 桥接其状态文件上板。
- **后台完成态依赖 TaskOutput**：若后台任务结束后代理从不调 TaskOutput，卡片会停 running → 15 分钟后转「疑似已结束」，此时手动 `finish --id bg-xxx`。
- **hook 中文解码**：Windows 下 Python stdin 默认 cp936，宿主写的是 UTF-8 → 一律走 `progress.read_stdin_json()`（显式 UTF-8），**不要**回退 `sys.stdin.read()`；已写进注册表的乱码用 `fix-mojibake --dry` 先看清单。
- **不要并发写同一 job id**（多进程会互相覆盖状态）。
- **hook 在 Windows 上走 Git Bash 执行**，命令路径用正斜杠 + 双引号；`install_hooks.py` 幂等，重复执行不产生重复条目。
