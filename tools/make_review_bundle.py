"""make_review_bundle.py - 把「仓库现状」压成一份可以直接粘给模型的文件。

为什么需要它：模型看不了你的硬盘，也常常拿不到 GitHub 的实时内容。
它读到的是**你给它的那段文本**。只要那段文本是三天前的、或者只截了前半个目录，
它就会非常自信地得出「这里没有 SQLite」——而且内部逻辑自洽，看不出破绽。

这次评审就是这么翻车的：Qwen 拿到的是一份 0.5.x 时代的粘贴文本，
于是它说「只有单写者、store.json、palace.py 1300 行」。仓库里其实早有
`sqlite_store.py`（897 行）与 `tests/test_store.py`（492 行）。

所以这里生成 `llms-full.txt`：
  · llms.txt（权威摘要，含版本与边界）
  · README.md（人读的现状）
  · 文件清单（路径 / 行数 / sha256 前 8 位 / 模块首行说明）
  · 测试清单（每个测试文件里的用例名）
  · CHANGELOG 最新一节
不塞完整源码（几十万行，模型反而看不进去），但把**存在性与规模**摆明白。

用法：
    python tools/make_review_bundle.py           # 生成 / 更新 llms-full.txt
    python tools/make_review_bundle.py --check    # 只校验是否最新（CI 用）
"""
from __future__ import annotations

import hashlib
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "llms-full.txt")

SKIP_DIRS = {
    ".git", "__pycache__", ".workbuddy", "outputs", ".venv", "node_modules", ".github",
    # 这些目录只在本地存在：CI 上没有 `.pytest_cache`，本地有。
    # 一旦把它写进清单，`--check` 会在 CI 上永远报「已过期」—— 而且看不出为什么。
    ".pytest_cache", ".mypy_cache", ".ruff_cache", "build", "dist", ".idea",
}
SKIP_SUFFIX = (".pyc", ".png", ".jpg", ".sqlite", ".db", ".pyo")


def _read(path: str) -> str:
    """按「通用换行」读，并把换行统一成 \n。

    这是跨平台一致性的根：git 在 Windows 上检出 CRLF、在 Linux 上检出 LF，
    同一份代码会算出两个 sha256（第一次跑 CI 就是这么红的）。
    包里存哈希是为了自证完整，那就必须对换行不敏感。
    """
    with open(path, "r", encoding="utf-8", newline=None) as f:
        return f.read().replace("\r\n", "\n").replace("\r", "\n")


def _first_docline(path: str) -> str:
    """取模块 docstring 的第一句，作为「这个文件管什么」。"""
    try:
        text = _read(path)
    except (OSError, UnicodeDecodeError):
        return ""
    m = re.match(r'\s*(?:"""|\'\'\')(.*?)(?:"""|\'\'\')', text, re.S)
    if not m:
        return ""
    body = m.group(1).strip().splitlines()
    for line in body:
        line = line.strip()
        if not line:
            continue
        # 跳过「test_xxx.py - 」这类前缀
        line = re.sub(r"^[\w./-]+\.py\s*-\s*", "", line)
        return line[:80]
    return ""


def _inventory() -> list[str]:
    rows: list[str] = ["## 文件清单（代码与文档）", "", "| 路径 | 行数 | sha256 | 说明 |", "|---|---|---|---|"]
    for dirpath, dirs, files in os.walk(ROOT):
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS)
        for name in sorted(files):
            if name.endswith(SKIP_SUFFIX):
                continue
            if not name.endswith((".py", ".md", ".toml", ".txt", ".yml", ".yaml")):
                continue
            full = os.path.join(dirpath, name)
            rel = os.path.relpath(full, ROOT).replace("\\", "/")
            if rel == "llms-full.txt":
                continue
            try:
                text = _read(full)
            except (OSError, UnicodeDecodeError):
                continue
            lines = text.count("\n") + 1
            digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:8]
            note = _first_docline(full) if name.endswith(".py") else ""
            if rel == "README.md":
                note = "人读的现状（能力 / 数字 / 边界）"
            elif name == "llms.txt":
                note = "机器读的现状（权威摘要）"
            elif name == "CHANGELOG.md":
                note = "版本账本（最新一节在最上面）"
            rows.append(f"| `{rel}` | {lines} | `{digest}` | {note} |")
    rows.append("")
    return rows


def _tests() -> list[str]:
    rows: list[str] = ["## 测试清单（用例名）", ""]
    test_dir = os.path.join(ROOT, "tests")
    if not os.path.isdir(test_dir):
        return rows
    for name in sorted(os.listdir(test_dir)):
        if not name.endswith(".py") or name == "__init__.py":
            continue
        path = os.path.join(test_dir, name)
        cases = re.findall(r"^def (test_\w+)\(", _read(path), re.M)
        rows.append(f"- `tests/{name}`（{len(cases)} 个用例）：" + "、".join(cases[:24]))
        if len(cases) > 24:
            rows.append(f"  （另有 {len(cases) - 24} 个）")
    rows.append("")
    return rows


def _changelog_head() -> list[str]:
    text = _read(os.path.join(ROOT, "CHANGELOG.md"))
    m = re.search(r"^## \[\d+\.\d+\.\d+\].*?(?=^## \[|\Z)", text, re.M | re.S)
    rows = ["## CHANGELOG（最新一节）", ""]
    rows.append(m.group(0).strip() if m else "（未找到版本小节）")
    rows.append("")
    return rows


def build() -> str:
    version = re.search(r'__version__ = "([^"]+)"', _read(os.path.join(ROOT, "memory_palace", "__init__.py"))).group(1)
    parts: list[str] = []
    parts.append("# memory-palace — 仓库现状全文（给评审/模型用）\n")
    parts.append(
        f"> 版本 **{version}**。本文件由 `tools/make_review_bundle.py` 生成，内容与仓库逐字对应。\n"
        "> 如果你只收到这一份文件，它已经包含：权威摘要、README、文件清单（含行数与哈希）、\n"
        "> 测试清单、最新变更。**不要**据此猜测「某个文件不存在」——先在这份清单里查。\n"
    )
    parts.append("\n" + "=" * 72 + "\n\n")
    parts.append(_read(os.path.join(ROOT, "llms.txt")).strip() + "\n")
    parts.append("\n" + "=" * 72 + "\n\n")
    parts.append(_read(os.path.join(ROOT, "README.md")).strip() + "\n")
    parts.append("\n" + "=" * 72 + "\n\n")
    parts.extend(line + "\n" for line in _changelog_head())
    parts.append("=" * 72 + "\n\n")
    parts.extend(line + "\n" for line in _inventory())
    parts.append("=" * 72 + "\n\n")
    parts.extend(line + "\n" for line in _tests())
    return "".join(parts)


def main(argv: list[str]) -> int:
    content = build()
    if "--check" in argv:
        if not os.path.exists(OUT):
            print("llms-full.txt 不存在，请先运行 python tools/make_review_bundle.py")
            return 1
        current = _read(OUT)
        if current != content:
            print("llms-full.txt 已过期：代码/文档变了但没重新生成。")
            # 只说「过期」等于让人猜。指出第一处不同的行，必要时连期望值一起给。
            old_lines = current.splitlines()
            new_lines = content.splitlines()
            for index in range(max(len(old_lines), len(new_lines))):
                old = old_lines[index] if index < len(old_lines) else "<缺少该行>"
                new = new_lines[index] if index < len(new_lines) else "<多出该行>"
                if old != new:
                    print(f"  第一处不同在第 {index + 1} 行：")
                    print(f"    文件里：{old[:120]}")
                    print(f"    应为  ：{new[:120]}")
                    break
            print("修法：python tools/make_review_bundle.py")
            return 1
        print(f"llms-full.txt 是最新的（{len(content.encode('utf-8')) / 1024:.0f} KB）")
        return 0
    with open(OUT, "w", encoding="utf-8", newline="\n") as f:
        f.write(content)
    # 报字节，不报字符：中文一个字符三字节，按字符说 KB 会少报一半
    print(f"✓ llms-full.txt  {len(content.encode('utf-8')) / 1024:.0f} KB / {len(content) / 1024:.1f}K 字符")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
