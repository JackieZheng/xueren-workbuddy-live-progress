# 雪人老师·[WorkBuddy]任务执行进度·实时面板

> WorkBuddy 会话任务通用「任务执行进度 · 实时面板」——用 hook 自动把工具调用映射成步骤，
> 打开一个浏览器页面即可实时看到每个任务跑到哪一步、还剩多少、卡在哪儿。

![面板效果](https://raw.githubusercontent.com/JackieZheng/xueren-workbuddy-live-progress/main/assets/panel_preview.png)

（图为 v1.0.27 在 1440px 横屏视口下的实拍：左侧三张并行任务卡，各卡片严格等高；底部常驻 CPU / 内存状态栏。
图片走 GitHub raw 外链，SkillHub / GitHub 详情页都能直出，不受包内二进制文件限制。）

---

## 这个 skill 解决什么问题

用 AI 干活时，最难受的是**过程不可见**：长任务跑着跑着不知道进行到哪、卡在哪一步、还要多久；
一个会话里并行几条任务时更是只能盯着输出猜。本 skill 给 WorkBuddy 装上一块**实时看板**：

- 每轮对话自动登记一张任务卡，**工具调用自动映射成执行步骤**（读文件、跑脚本、下载…逐一推进）
- 打开 `http://127.0.0.1:8791/` 就能看到：任务卡、进度环/扫描条、步骤条、推进曲线、日志尾、耗时
- 任务结束自动收尾；会话异常中断有兜底；面板自身带**热更新**，改代码不用手动重启

## 特性

| 能力 | 说明 |
|---|---|
| **零接入成本** | 装好 hooks 后自动生效；会话卡由 `UserPromptSubmit` 自动建，工具调用自动成为步骤 |
| **主动接管** | 脚本任务可用 `progress.py` 精确上报总量/进度/日志；两套机制共存不打架 |
| **实时** | 面板轮询 + 前端增量渲染（2 秒一帧），步骤/日志/速率/ETA 实时刷新 |
| **卡片严格等高** | 卡片内部尺寸全部固定 px，宽窄视口下同组卡片高度完全一致，不重叠、不抖 |
| **单行走马灯** | 元信息、状态栏、标题等单行文本超长自动无缝滚动（跨刷新记住位置，不跳闪） |
| **底部系统栏** | 常驻显示 CPU / 内存占用（纯标准库采样，无第三方依赖） |
| **异常中断兜底** | 会话崩溃/关窗导致的僵尸卡，下次会话启动自动标记为「异常中断」，会话复活还能自动回 running |
| **热更新** | 改动 `live_panel.py` / `progress.py` 后 3 秒内自动用同参数重启，前端自动重载，不掉线 |
| **零依赖** | 全部纯 Python 标准库；不需要 Flask / FastAPI / Node |

## 安装

### 方式一：SkillHub（推荐）

```bash
skillhub install xueren-workbuddy-live-progress --namespace indiv-xueren
```

### 方式二：手动安装

把本目录放到 WorkBuddy 的 skill 目录下，然后安装 hooks：

```bash
# 1) 放置 skill（目录名保持 xueren-workbuddy-live-progress）
#    ~/.workbuddy/skills/xueren-workbuddy-live-progress/

# 2) 安装 hooks（幂等；自动备份 settings.json）
python ~/.workbuddy/skills/xueren-workbuddy-live-progress/scripts/install_hooks.py

# 3) 查看安装状态
python ~/.workbuddy/skills/xueren-workbuddy-live-progress/scripts/install_hooks.py --status
```

> hooks 写入 `~/.workbuddy/settings.json`，在**新会话**中生效。
> 卸载：`install_hooks.py --uninstall`。

## 快速开始

```bash
# 打开面板（会话外常驻，带空闲自退；也可用同目录的 start_panel.bat）
python ~/.workbuddy/skills/xueren-workbuddy-live-progress/scripts/panel_ctl.py start

# 看一眼状态 / 是否会影响当前会话
python ~/.workbuddy/skills/xueren-workbuddy-live-progress/scripts/panel_ctl.py status
python ~/.workbuddy/skills/xueren-workbuddy-live-progress/scripts/panel_ctl.py check
```

浏览器打开 <http://127.0.0.1:8791/> 即可。

## 命令速查

### 面板控制 `panel_ctl.py`

```bash
panel_ctl.py status                        # 在不在？哪个端口？运行中 build 与磁盘是否一致
panel_ctl.py check                         # 会不会拖住当前会话（父进程判定）
panel_ctl.py start [--port 8791] [--no-browser] [--idle-exit 秒]
panel_ctl.py stop | restart | upgrade      # upgrade = 只在跑着旧版时才停旧起新
```

### 任务上报 `progress.py`

```bash
# 建卡（有总量 / 有步骤）
progress.py start  --title "导出报表" --total 120 --unit 条
progress.py begin  --title "用户请求" --steps "准备,处理,收尾"

# 推进
progress.py set    --id <id> --done 30 --msg "30/120"
progress.py inc    --id <id> --n 5
progress.py step   --id <id> --index 1 [--name "处理"]
progress.py log    --id <id> --msg "…"
progress.py plan   --id <id> --sec 90          # 不可细分任务：声明预估总耗时

# 收尾
progress.py finish         --id <id> [--failed] [--msg "…"]
progress.py finish-session                     # 🔴 会话卡必须主动收尾
progress.py gc [--min 30] [--dry]              # 异常中断兜底
progress.py list [--running] [--json]
progress.py fix-mojibake [--dry]               # 修复被按 GBK 误解码的标题/日志
```

Python 里也可以直接调用：

```python
import sys; sys.path.insert(0, "~/.workbuddy/skills/xueren-workbuddy-live-progress/scripts")
import progress as P

P.register("批量下载素材包", total=120, unit="个", steps=["扫描", "下载", "校验"])
P.update("批量下载素材包", done=40, log_line="已下载 40 / 120")
P.set_step("批量下载素材包", 1)
P.finish("批量下载素材包", ok=True, message="全部完成")
```

## 工作原理

```
        ┌──────────────── WorkBuddy 会话 ────────────────┐
        │  UserPromptSubmit ──► hook_prompt.py  ─┐       │
        │  PreToolUse       ──► hook_auto_step   │       │
        │  PostToolUse      ──► hook_auto_step   ├──► jobs.json（原子写 + 文件锁）
        │  PostToolUse(Bash bg) ► hook_bg.py     │        │
        │  SessionStart     ──► hook_session.py ─┘        │
        └────────────────────────────────────────────────┘
                                 │
                    live_panel.py（本地 HTTP 服务 :8791）
                                 │  轮询 /api/jobs、/api/sys
                                 ▼
                        浏览器面板（实时刷新）
```

- **注册表**：`~/.workbuddy/live-progress/jobs.json`，进程间用文件锁 + 原子替换写入，多会话并发安全。
- **任务卡**：会话卡 `sess-<session_id>`、后台任务卡 `bg-<task_id>`、脚本自建业务卡，三类互不干扰。
- **面板**：纯 `http.server` + 内联 HTML/CSS/JS 单页，无构建步骤；`/api/sys` 由标准库采样 CPU / 内存。

## 项目结构

```
xueren-workbuddy-live-progress/
├── SKILL.md                  # skill 定义（16 字段 frontmatter + 7 章节）
├── README.md
├── assets/
│   └── panel_preview.png     # 面板效果图
├── scripts/
│   ├── install_hooks.py      # 安装/卸载/查看 hooks（幂等）
│   ├── hook_session.py       # SessionStart：建卡、清陈旧、异常中断兜底
│   ├── hook_prompt.py        # UserPromptSubmit：会话卡 + 约定提醒
│   ├── hook_auto_step.py     # Pre/PostToolUse：工具调用 → 步骤/日志
│   ├── hook_bg.py            # 后台任务登记 + 面板保活/升级提醒
│   ├── progress.py           # 任务注册表核心（CLI + API）
│   ├── progress_bridge.py    # 进度桥（长任务细粒度上报）
│   ├── live_panel.py         # 面板服务（HTTP + 单页 UI + 热更新）
│   ├── panel_ctl.py          # 面板启停/状态/升级
│   ├── check_update.py       # 版本自更新（查 GitHub/SkillHub → 下载 → 备份 → 同步 → 冒烟 → 回滚）
│   └── _panel_relaunch.py    # 热更新接力进程（Windows 下平滑换进程）
├── start_panel.bat / stop_panel.bat
├── cache/                    # 运行期截图等临时产物（不入库）
└── docs/                     # 开发级文档（不外发，见下）
    ├── DEVLOG.md             # 版本演进与踩坑记录
    └── update-test.md        # 自更新链路自检手册
```

> **文档分级（产品口径）**：`SKILL.md` / `README.md` 面向用户与 AI，两个发布渠道都带；
> `docs/` 与 `DEVLOG.md` 属开发级（测试 / 排障 / 版本日志），**两个渠道都不外发**；
> `scripts/` 下 `_test_` / `_probe_` / `_demo_` 开头的脚本同理。

## 自动更新

内置 `scripts/check_update.py`，**无需任何凭据**即可比对 GitHub Release 与 SkillHub 上的版本：

```bash
python ~/.workbuddy/skills/xueren-workbuddy-live-progress/scripts/check_update.py          # 只检查
python ~/.workbuddy/skills/xueren-workbuddy-live-progress/scripts/check_update.py --auto   # 有新版本就更新
```

更新链路自带保险：下载（GitHub tarball 优先，失败退 SkillHub）→ 包校验（必须有 `SKILL.md` + `scripts/live_panel.py`）→ 备份到 `cache/backups/` → 同步文件（本地 `cache/`、`.git` 永不覆盖）→ 冒烟（脚本语法 + 面板 HTTP 200）→ **任一步失败自动回滚**。

配一条每日定时任务即可长期保持最新：

```bash
python ~/.workbuddy/skills/xueren-workbuddy-live-progress/scripts/check_update.py --auto --json
```

面板自带热更新，更新完无需手动重启，hooks 也不用重装。

## 常见问题

**Q：面板会不会拖住会话？**
不会。面板用 `panel_ctl.py` 在**会话外**创建（Windows 下 detached），并带空闲自退；`panel_ctl.py check` 可随时判定。
**严禁**用 `Bash run_in_background` 启动 `live_panel.py` —— 那会变成会话的后台任务，任务永不结束。

**Q：改了代码面板没变化？**
面板每 3 秒比对 `live_panel.py` / `progress.py` 的内容指纹，变了自动重启加载。
若长时间不一致，用 `panel_ctl.py status` 看「运行中 build / 磁盘 build」，再 `panel_ctl.py upgrade`。

**Q：卡片一直停在「运行中」怎么办？**
正常流程是任务结束主动 `finish` / `finish-session`。若会话异常中断（崩溃、关窗），
下次会话启动会自动把 30 分钟无更新的 `sess-*` 卡标成「异常中断」；也可手动
`progress.py gc --min 30`。会话若又恢复活动，卡片会自动回到 running。

**Q：标题/日志出现「宸茬粡…」这类乱码？**
中文 Windows 下 Python `sys.stdin` 默认 cp936 而宿主写 UTF-8 所致。本项目所有 hook 已强制按 UTF-8 读取；
存量数据可用 `progress.py fix-mojibake` 还原。

## 环境要求

- Python 3.8+（仅标准库，无需 pip 安装任何依赖）
- Windows / macOS / Linux 均可运行（面板启动方式对 Windows 做了专门适配）

## License

MIT © 雪人

---

> 本项目为 WorkBuddy 专用（其 hooks 机制、`jobs.json` 约定依赖 WorkBuddy 运行时），
> 暂未在其它 AI 工具中验证通用性。
