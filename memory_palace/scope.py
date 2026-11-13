"""scope.py - 记忆属于谁：全局 / 某个项目 / 本次会话。

记忆最容易被搞坏的地方不是存不下，是**串味**：
  · 全放一个池子 → 别的项目的临时决定会挤进这个项目的注入块，`--budget` 被无关内容吃掉；
  · 全按项目隔离 → 用户偏好、硬规则、教训每换一个项目就要重说一遍。

所以这里不是"要不要跨项目"，是**分两层**：
  · `global`  —— 跨项目共享：用户偏好、硬规则、可复用的教训。换项目也在。
  · `proj:X`  —— 只属于某个项目：它的架构决策、踩过的坑、当前进度。
  · `session` —— 只属于本次会话（工作记忆），不跨进程保留价值。

默认视图是 `auto` = global + 当前项目。这是「跨项目但不串味」的那条线：
跨项目的东西显式写 global，项目内的东西默认留在项目里，谁也不污染谁。

项目标识怎么来（按可靠性排序）：
  1. `--project X` / 环境变量 `MEMORY_PALACE_PROJECT` —— 显式指定永远优先
  2. git 的 origin remote → `proj:owner/repo` —— 换目录、换机器、重新 clone 都还是同一个项目
  3. 目录名 + 路径哈希 → `proj:目录名-ab12cd` —— 没有 git 时的兜底，可读且稳定
"""
from __future__ import annotations

import hashlib
import os
import re
from pathlib import Path

GLOBAL = "global"
SESSION = "session"
PROJECT_PREFIX = "proj:"
SCOPE_MODES = ("auto", "global", "project", "session", "all")

_REMOTE = re.compile(r"^\s*url\s*=\s*(.+?)\s*$", re.M)
_SLUG = re.compile(r"[^0-9A-Za-z\u4e00-\u9fff._/-]+")


def _clean(text: str) -> str:
    return _SLUG.sub("-", text).strip("-./") or "unnamed"


def parse_remote(url: str) -> str | None:
    """把 git remote 归一成 `owner/repo`。

    四种写法都要认，因为同一个仓库在不同人机器上的 remote 长得不一样：
      git@github.com:owner/repo.git
      https://github.com/owner/repo.git
      ssh://git@host:22/owner/repo
      /local/path/repo.git
    """
    raw = url.strip()
    if not raw:
        return None
    if raw.startswith("git@") or "://" not in raw:
        # scp 风格：git@host:owner/repo.git
        tail = raw.split(":", 1)[1] if ":" in raw else raw
    else:
        tail = re.sub(r"^[a-z+]+://[^/]+/", "", raw, flags=re.I)
    tail = tail.strip("/")
    if tail.endswith(".git"):
        tail = tail[:-4]
    parts = [p for p in tail.split("/") if p]
    if not parts:
        return None
    if len(parts) == 1:
        return _clean(parts[0])
    return f"{_clean(parts[-2])}/{_clean(parts[-1])}"


def git_project(cwd: str | Path) -> str | None:
    """从 .git/config 读 origin。不调用 git 命令 —— 每次注入都 fork 一个进程不值得。"""
    path = Path(cwd)
    for base in (path, *path.parents):
        config = base / ".git" / "config"
        if config.is_file():
            try:
                text = config.read_text(encoding="utf-8", errors="replace")
            except OSError:
                return None
            # 优先取 [remote "origin"] 段里的 url
            match = re.search(
                r'\[remote\s+"origin"\]\s*(.*?)(?=\n\[|\Z)', text, re.S
            )
            body = match.group(1) if match else text
            found = _REMOTE.search(body)
            slug = parse_remote(found.group(1)) if found else None
            if slug:
                return PROJECT_PREFIX + slug
            return PROJECT_PREFIX + _clean(base.name)
    return None


def detect_project(cwd: str | None = None, explicit: str | None = None) -> str:
    """当前项目标识。显式 > 环境变量 > git remote > 目录名+哈希。"""
    if explicit:
        return normalize_project(explicit)
    env = os.environ.get("MEMORY_PALACE_PROJECT", "").strip()
    if env:
        return normalize_project(env)
    here = os.path.abspath(cwd or os.getcwd())
    from_git = git_project(here)
    if from_git:
        return from_git
    digest = hashlib.sha1(here.encode("utf-8")).hexdigest()[:6]
    return f"{PROJECT_PREFIX}{_clean(os.path.basename(here))}-{digest}"


def normalize_project(value: str) -> str:
    raw = value.strip()
    if raw in ("", GLOBAL):
        return GLOBAL
    if raw.startswith(PROJECT_PREFIX):
        return PROJECT_PREFIX + _clean(raw[len(PROJECT_PREFIX):])
    return PROJECT_PREFIX + _clean(raw)


def normalize_scope(value: str | None, project: str) -> str:
    """把用户写的 scope 归一。空 = 跟随默认（项目内）。"""
    raw = (value or "").strip().lower()
    if raw in ("", "project", "proj", "local", "本 项目"):
        return project
    if raw in ("global", "shared", "跨项目"):
        return GLOBAL
    if raw in ("session", "temp", "临时"):
        return SESSION
    return normalize_project(raw)


def scopes_for(mode: str, project: str) -> set[str] | None:
    """某一种视图该看哪些 scope。返回 None = 不过滤（全部）。

    `auto` 是默认：全局 + 当前项目。这条线决定了「换项目后还记得什么」——
    记得住偏好与规则，记不住上个项目的实现细节。
    """
    key = (mode or "auto").strip().lower()
    # 归一化在这里做，而不是指望每个调用方都先归一 —— 裸名 "repo-a" 与
    # 节点上存的 "proj:repo-a" 不相等，这种不一致会让过滤静默失效（踩过）。
    here = normalize_project(project)
    if key in ("all", "*"):
        return None
    if key in ("global", "shared"):
        return {GLOBAL}
    if key in ("project", "proj", "local"):
        return {here}
    if key in ("session", "temp"):
        return {SESSION}
    return {GLOBAL, here}


def label(scope: str) -> str:
    if scope == GLOBAL:
        return "全局"
    if scope == SESSION:
        return "会话"
    if scope.startswith(PROJECT_PREFIX):
        return "项目·" + scope[len(PROJECT_PREFIX):]
    return scope
