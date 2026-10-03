#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""检查 GitHub / SkillHub 上是否有新版本，并可自动更新当前 skill。

设计约束
--------
- **零凭据**：GitHub 走匿名 API（公共仓库），SkillHub 走公开搜索/下载接口；
  脚本里不含任何 token、账号、邮箱、本机绝对路径。
- **仓库坐标自解析**：owner/repo 从 SKILL.md 的 `github:` 字段读取，
  SkillHub 标识从 `slug:` 字段读取——换账号/改仓库名都不用改脚本。
- **可回滚**：更新前先备份到 `cache/backups/`，任一步失败自动还原。

用法
----
    python check_update.py                    # 只检查，打印结论
    python check_update.py --auto             # 有新版本就自动更新
    python check_update.py --auto --json      # 结构化输出（供自动化编排调用）
    python check_update.py --force            # 忽略版本比较，强制跑一遍更新流程（自测用）
    python check_update.py --source skillhub  # 只用 SkillHub 通道
    python check_update.py --prune            # 同步时删除远端已不存在的文件
    python check_update.py --backups          # 列出本地备份

退出码：0 = 正常（无更新 / 已更新成功）；1 = 出错（下载/校验失败且未回滚成功）；
        2 = 有新版本但未更新（未加 --auto）。
"""
from __future__ import annotations

import argparse
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
SKILL_DIR = os.path.dirname(HERE)
SKILL_MD = os.path.join(SKILL_DIR, "SKILL.md")
BACKUP_DIR = os.path.join(SKILL_DIR, "cache", "backups")
LOG_FILE = os.path.join(SKILL_DIR, "cache", "update.log")

# 更新时本地保留、绝不覆盖/删除
KEEP_NAMES = {"cache", ".git", "__pycache__", ".tmp-publish-skillhub", ".tmp_verify"}
KEEP_FILE_SUFFIX = (".pyc", ".pyo", ".log")

UA = {"User-Agent": "xueren-skill-update-checker/1.0 (+python-urllib)"}
TIMEOUT = 30
RETRIES = 3


# ---------------------------------------------------------------------------
# 基础设施
# ---------------------------------------------------------------------------

def _reconfigure_stdio():
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def _log(msg):
    line = "%s  %s" % (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), msg)
    try:
        os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
        with io.open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass
    print(msg, flush=True)


def _opener():
    """绕开本机代理（本机代理会拦 127.0.0.1 / 影响直连）。"""
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))


OPENER = _opener()


def http_get(url, timeout=TIMEOUT, retries=RETRIES, raw=False):
    """带重试的 GET。raw=True 返回 bytes。"""
    last = None
    for i in range(retries):
        try:
            req = urllib.request.Request(url, headers=UA)
            with OPENER.open(req, timeout=timeout) as r:
                data = r.read()
            return data if raw else data.decode("utf-8", "replace")
        except Exception as e:                 # noqa: BLE001
            last = e
            if i < retries - 1:
                time.sleep(1.5 * (i + 1))
    raise RuntimeError("请求失败 %s ：%s" % (url, last))


# ---------------------------------------------------------------------------
# SKILL.md frontmatter
# ---------------------------------------------------------------------------

def read_frontmatter(path=SKILL_MD):
    """只解析最前面的 --- ... --- 之间的 key: value（不引 yaml 依赖）。"""
    fm = {}
    try:
        with io.open(path, encoding="utf-8") as f:
            text = f.read(8000)
    except Exception:
        return fm
    m = re.match(r"^---\s*\n(.*?)\n---", text, re.S)
    if not m:
        return fm
    for line in m.group(1).splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        mm = re.match(r"^([A-Za-z_][A-Za-z0-9_]*):\s*(.*)$", line)
        if mm:
            fm[mm.group(1)] = mm.group(2).strip().strip('"').strip("'")
    return fm


def parse_ver(v):
    nums = re.findall(r"\d+", str(v or ""))
    return tuple(int(x) for x in nums[:4]) if nums else (0,)


def ver_gt(a, b):
    return parse_ver(a) > parse_ver(b)


# ---------------------------------------------------------------------------
# 远端版本查询
# ---------------------------------------------------------------------------

def repo_coords(fm):
    """从 github: 字段解析 (owner, repo)。"""
    url = fm.get("github", "")
    m = re.search(r"github\.com[/:]([^/]+)/([^/\s#?]+)", url)
    if not m:
        return None, None
    return m.group(1), m.group(2).removesuffix(".git")


def remote_github(fm, timeout=TIMEOUT):
    """返回 {version, tag, url} 或 {'error': ...}。"""
    owner, repo = repo_coords(fm)
    if not owner:
        return {"error": "SKILL.md 缺少 github 字段，无法走 GitHub 通道"}
    api = "https://api.github.com/repos/%s/%s/releases/latest" % (owner, repo)
    try:
        data = json.loads(http_get(api, timeout=timeout))
    except Exception as e:                     # noqa: BLE001
        return {"error": "GitHub API 不可达：%s" % e}
    tag = data.get("tag_name") or ""
    if not tag:
        return {"error": "GitHub 仓库尚无 Release"}
    return {"version": tag.lstrip("vV"), "tag": tag,
            "url": data.get("html_url") or "", "owner": owner, "repo": repo}


def remote_skillhub(fm, timeout=TIMEOUT):
    """用公开搜索接口查 SkillHub 上的版本；返回 {version, url} 或 {'error': ...}。"""
    slug = fm.get("slug") or os.path.basename(SKILL_DIR)
    api = ("https://api.skillhub.cn/api/v1/search?q=%s&limit=100"
           % urllib.parse.quote(slug))
    try:
        data = json.loads(http_get(api, timeout=timeout))
    except Exception as e:                     # noqa: BLE001
        return {"error": "SkillHub 搜索不可达：%s" % e}
    for r in data.get("results") or []:
        if r.get("slug") == slug:
            ns = r.get("namespace") or {}
            handle = ns.get("handle") if isinstance(ns, dict) else None
            return {"version": str(r.get("version") or "").strip(),
                    "url": r.get("homepage") or
                           ("https://skillhub.cn/skills/%s/%s" % (handle, slug) if handle else ""),
                    "slug": slug}
    return {"error": "SkillHub 尚未索引到 %s（新发布通常有延迟）" % slug}


# ---------------------------------------------------------------------------
# 下载 & 解压
# ---------------------------------------------------------------------------

def fetch_github_archive(fm, tag, workdir, timeout=90):
    owner, repo = repo_coords(fm)
    url = "https://codeload.github.com/%s/%s/tar.gz/refs/tags/%s" % (owner, repo, tag)
    blob = http_get(url, timeout=timeout, raw=True)
    tgz = os.path.join(workdir, "pkg.tar.gz")
    with open(tgz, "wb") as f:
        f.write(blob)
    dest = os.path.join(workdir, "pkg")
    os.makedirs(dest, exist_ok=True)
    with tarfile.open(tgz, "r:gz") as t:
        members = t.getmembers()
        # 去掉最外层目录前缀（owner-repo-sha/）
        for m in members:
            parts = m.name.split("/", 1)
            if len(parts) < 2 or not parts[1]:
                continue
            m.name = parts[1]
            if m.isdir() or m.isfile():
                t.extract(m, dest, filter="data" if hasattr(tarfile, "data_filter") else None)
    return dest


def fetch_skillhub_zip(fm, workdir, timeout=90):
    slug = fm.get("slug") or os.path.basename(SKILL_DIR)
    url = "https://api.skillhub.cn/api/v1/download?slug=%s" % urllib.parse.quote(slug)
    blob = http_get(url, timeout=timeout, raw=True)
    zp = os.path.join(workdir, "pkg.zip")
    with open(zp, "wb") as f:
        f.write(blob)
    dest = os.path.join(workdir, "pkg")
    os.makedirs(dest, exist_ok=True)
    with zipfile.ZipFile(zp) as z:
        names = z.namelist()
        # 可能带一层 skill 目录前缀
        top = {n.split("/")[0] for n in names if "/" in n}
        strip = ""
        if len(top) == 1 and not any(n.count("/") == 0 and n for n in names):
            strip = list(top)[0] + "/"
        for n in names:
            if n.endswith("/"):
                continue
            inner = n[len(strip):] if strip and n.startswith(strip) else n
            if not inner:
                continue
            out = os.path.join(dest, *inner.split("/"))
            os.makedirs(os.path.dirname(out), exist_ok=True)
            with z.open(n) as src, open(out, "wb") as dst:
                shutil.copyfileobj(src, dst)
    return dest


def package_version(pkg_dir):
    return read_frontmatter(os.path.join(pkg_dir, "SKILL.md")).get("version", "")


def validate_package(pkg_dir, expect_ver=""):
    """校验下载到的包确实是本 skill（防止乱包把目录写坏）。"""
    problems = []
    if not os.path.isfile(os.path.join(pkg_dir, "SKILL.md")):
        problems.append("缺少 SKILL.md")
    if not os.path.isfile(os.path.join(pkg_dir, "scripts", "live_panel.py")):
        problems.append("缺少 scripts/live_panel.py")
    if not os.path.isfile(os.path.join(pkg_dir, "scripts", "progress.py")):
        problems.append("缺少 scripts/progress.py")
    v = package_version(pkg_dir)
    if not v:
        problems.append("包内 SKILL.md 无 version 字段")
    elif expect_ver and parse_ver(v) != parse_ver(expect_ver):
        problems.append("包内版本 %s 与远端声明 %s 不一致" % (v, expect_ver))
    return problems, v


# ---------------------------------------------------------------------------
# 备份 / 应用 / 回滚
# ---------------------------------------------------------------------------

def backup_current(tag=""):
    os.makedirs(BACKUP_DIR, exist_ok=True)
    local_ver = read_frontmatter().get("version", "0")
    name = "v%s_%s%s.zip" % (local_ver, datetime.now().strftime("%Y%m%d-%H%M%S"),
                             ("_" + tag) if tag else "")
    path = os.path.join(BACKUP_DIR, name)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        for dp, dn, fn in os.walk(SKILL_DIR):
            dn[:] = [d for d in dn if d not in KEEP_NAMES]
            for f in fn:
                fp = os.path.join(dp, f)
                if f.endswith(KEEP_FILE_SUFFIX):
                    continue
                z.write(fp, os.path.relpath(fp, SKILL_DIR).replace(os.sep, "/"))
    return path


def list_package_files(pkg_dir):
    out = []
    for dp, dn, fn in os.walk(pkg_dir):
        dn[:] = [d for d in dn if d not in KEEP_NAMES]
        for f in fn:
            rel = os.path.relpath(os.path.join(dp, f), pkg_dir).replace(os.sep, "/")
            if rel.startswith("cache/"):
                continue
            out.append(rel)
    return out


def apply_package(pkg_dir, prune=False):
    """把包内容同步到 SKILL_DIR。返回 (写入数, 删除数)。"""
    files = list_package_files(pkg_dir)
    written = 0
    for rel in files:
        src = os.path.join(pkg_dir, rel)
        dst = os.path.join(SKILL_DIR, *rel.split("/"))
        top = rel.split("/")[0]
        if top in KEEP_NAMES:
            continue
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copy2(src, dst)
        written += 1

    removed = 0
    if prune:
        keep = set(files)
        for dp, dn, fn in os.walk(SKILL_DIR):
            dn[:] = [d for d in dn if d not in KEEP_NAMES]
            for f in fn:
                fp = os.path.join(dp, f)
                rel = os.path.relpath(fp, SKILL_DIR).replace(os.sep, "/")
                if rel.startswith("cache/") or rel in keep or f.endswith(KEEP_FILE_SUFFIX):
                    continue
                try:
                    os.remove(fp)
                    removed += 1
                except OSError:
                    pass
    # 清掉可能失效的字节码缓存
    for dp, dn, fn in os.walk(SKILL_DIR):
        for d in list(dn):
            if d == "__pycache__":
                shutil.rmtree(os.path.join(dp, d), ignore_errors=True)
                dn.remove(d)
    return written, removed


def restore_backup(zip_path):
    with zipfile.ZipFile(zip_path) as z:
        z.extractall(SKILL_DIR)
    for dp, dn, fn in os.walk(SKILL_DIR):
        for d in list(dn):
            if d == "__pycache__":
                shutil.rmtree(os.path.join(dp, d), ignore_errors=True)


# ---------------------------------------------------------------------------
# 冒烟校验 & 面板重启
# ---------------------------------------------------------------------------

def smoke_test(expected_ver=""):
    """返回 (ok, 明细 list)。"""
    details = []
    ok = True

    # 1) 语法
    py = sys.executable
    bad = []
    for dp, dn, fn in os.walk(os.path.join(SKILL_DIR, "scripts")):
        dn[:] = [d for d in dn if d != "__pycache__"]
        for f in fn:
            if not f.endswith(".py"):
                continue
            fp = os.path.join(dp, f)
            r = subprocess.run([py, "-m", "py_compile", fp],
                               capture_output=True, text=True)
            if r.returncode != 0:
                bad.append(f)
    if bad:
        ok = False
        details.append("py_compile 失败：%s" % ", ".join(bad))
    else:
        details.append("脚本语法 OK")

    # 2) 版本核对
    v = read_frontmatter().get("version", "")
    details.append("本地版本 = %s" % v)
    if expected_ver and parse_ver(v) != parse_ver(expected_ver):
        ok = False
        details.append("版本与预期(%s)不一致" % expected_ver)

    # 3) 面板存活（只在本来就跑着的时候检查）
    port = _detect_panel_port()
    if port:
        time.sleep(6)                                      # 等热更新自动重启完成
        url = "http://127.0.0.1:%d/" % port
        code = 0
        for _ in range(4):
            try:
                with OPENER.open(urllib.request.Request(url, headers=UA), timeout=6) as r:
                    code = r.status
                if code == 200:
                    break
            except Exception:                              # noqa: BLE001
                time.sleep(2)
        if code == 200:
            details.append("面板 http %d 正常（%s）" % (code, url))
        else:
            ok = False
            details.append("面板无响应（%s）" % url)
    else:
        details.append("面板未运行，跳过存活检查")

    return ok, details


def _detect_panel_port():
    """探测本机面板端口（在跑才返回，否则 None）。"""
    cands = [8791]
    try:
        sys.path.insert(0, os.path.join(SKILL_DIR, "scripts"))
        import panel_ctl                                    # type: ignore
        cands = [panel_ctl.DEFAULT_PORT]
        for k in (panel_ctl._load_state() or {}):
            if str(k).isdigit():
                cands.append(int(k))
    except Exception:
        pass
    for p in dict.fromkeys(cands):
        try:
            with OPENER.open(urllib.request.Request("http://127.0.0.1:%d/" % p, headers=UA),
                             timeout=4) as r:
                if r.status == 200:
                    return p
        except Exception:                                   # noqa: BLE001
            continue
    return None


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def do_update(fm, remote, source, prune=False, quiet=False):
    """下载 → 校验 → 备份 → 同步 → 冒烟；失败自动回滚。返回 dict。"""
    ver = remote.get("version", "")
    tag = remote.get("tag") or ("v" + ver)
    result = {"updated": False, "rolled_back": False, "steps": []}

    with tempfile.TemporaryDirectory(prefix="skillupd-") as wd:
        pkg = ""
        errs = []
        order = ["github", "skillhub"] if source == "both" else [source]
        for src in order:
            try:
                if src == "github":
                    if not tag:
                        raise RuntimeError("缺少 tag")
                    pkg = fetch_github_archive(fm, tag, wd)
                else:
                    pkg = fetch_skillhub_zip(fm, wd)
                problems, pv = validate_package(pkg, ver)
                if problems:
                    raise RuntimeError("包校验失败：%s" % "；".join(problems))
                result["steps"].append("%s 通道下载并校验通过（包内版本 %s）" % (src, pv))
                result["source_used"] = src
                break
            except Exception as e:                          # noqa: BLE001
                errs.append("%s：%s" % (src, e))
                pkg = ""
        if not pkg:
            result["error"] = "所有下载通道失败 → " + " | ".join(errs)
            return result

        bak = backup_current(tag=tag)
        result["backup"] = bak
        result["steps"].append("已备份 → %s" % os.path.basename(bak))

        try:
            written, removed = apply_package(pkg, prune=prune)
            result["steps"].append("已同步 %d 个文件%s" % (written, ("，清理 %d 个" % removed) if removed else ""))
            ok, details = smoke_test(expected_ver=ver)
            result["steps"].extend(details)
            if not ok:
                raise RuntimeError("冒烟校验未通过")
            result["updated"] = True
        except Exception as e:                              # noqa: BLE001
            result["error"] = "更新失败，正在回滚：%s" % e
            try:
                restore_backup(bak)
                result["rolled_back"] = True
                smoke_test()
                result["steps"].append("已回滚到更新前状态")
            except Exception as e2:                          # noqa: BLE001
                result["steps"].append("⚠️ 回滚也失败：%s（备份仍在 %s）" % (e2, bak))
    return result


def main():
    _reconfigure_stdio()
    ap = argparse.ArgumentParser(description="检查并自动更新本 skill（GitHub / SkillHub）")
    ap.add_argument("--auto", action="store_true", help="发现新版本则自动更新")
    ap.add_argument("--json", action="store_true", help="以 JSON 输出（供自动化调用）")
    ap.add_argument("--force", action="store_true", help="忽略版本比较，强制走一遍更新流程（自测用）")
    ap.add_argument("--source", choices=["github", "skillhub", "both"], default="both",
                    help="检查/下载通道（默认 both，GitHub 优先）")
    ap.add_argument("--prune", action="store_true", help="同步时删除远端已不存在的文件")
    ap.add_argument("--backups", action="store_true", help="列出本地备份")
    ap.add_argument("--no-color", action="store_true", help="（占位，保持兼容）")
    args = ap.parse_args()

    if args.backups:
        os.makedirs(BACKUP_DIR, exist_ok=True)
        items = sorted(os.listdir(BACKUP_DIR), reverse=True)
        if args.json:
            print(json.dumps({"backups": items}, ensure_ascii=False))
        else:
            print("备份目录：%s" % BACKUP_DIR)
            for i in items:
                print("  %-40s %.1f KB" % (i, os.path.getsize(os.path.join(BACKUP_DIR, i)) / 1024))
            if not items:
                print("  （暂无备份）")
        return 0

    fm = read_frontmatter()
    local_ver = fm.get("version", "0")
    payload = {"local_version": local_ver, "skill_dir": SKILL_DIR,
               "checked_at": datetime.now().isoformat(timespec="seconds")}

    remotes = {}
    if args.source in ("github", "both"):
        remotes["github"] = remote_github(fm)
    if args.source in ("skillhub", "both"):
        remotes["skillhub"] = remote_skillhub(fm)
    payload["remote"] = remotes

    # 取版本最高的那个可用远端
    best_src, best = None, None
    for src, r in remotes.items():
        if r.get("error") or not r.get("version"):
            continue
        if best is None or ver_gt(r["version"], best["version"]):
            best_src, best = src, r

    if best is None:
        payload["status"] = "unknown"
        payload["message"] = "；".join("%s: %s" % (k, v.get("error", "无版本信息"))
                                       for k, v in remotes.items())
        payload["ok"] = False
    else:
        payload["latest_source"] = best_src
        payload["latest_version"] = best["version"]
        newer = ver_gt(best["version"], local_ver)
        payload["newer"] = newer
        if not newer and not args.force:
            payload["status"] = "up-to-date"
            payload["ok"] = True
        elif not args.auto and not args.force:
            payload["status"] = "update-available"
            payload["ok"] = True
            payload["hint"] = "加 --auto 执行自动更新"
        else:
            payload["status"] = "updating"
            upd = do_update(fm, best, "both" if args.source == "both" else args.source,
                            prune=args.prune)
            payload["update"] = upd
            payload["ok"] = bool(upd.get("updated"))
            payload["status"] = "updated" if upd.get("updated") else (
                "rolled-back" if upd.get("rolled_back") else "update-failed")

    # 日志 & 输出
    _log("[%s] 本地 %s → 远端 %s（%s）" % (
        payload.get("status"), local_ver,
        payload.get("latest_version", "?"), payload.get("latest_source", "-")))
    if args.json:
        print(json.dumps(payload, ensure_ascii=False))
    else:
        print("本地版本：%s" % local_ver)
        for src, r in remotes.items():
            print("  %-9s : %s" % (src, r.get("version") or ("✗ " + str(r.get("error")))))
        print("结论    ：%s" % payload.get("status"))
        if payload.get("message"):
            print("说明    ：%s" % payload["message"])
        for s in (payload.get("update") or {}).get("steps", []):
            print("  · %s" % s)
        if payload.get("update", {}).get("error"):
            print("  ⚠️ %s" % payload["update"]["error"])

    if payload.get("status") == "update-available":
        return 2
    return 0 if payload.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main() or 0)
