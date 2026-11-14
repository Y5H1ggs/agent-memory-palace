"""test_docs.py - 文档与代码的一致性。

为什么要测文档：外部评审读的是文档，不是代码。一次「README 说 0.9、代码是 0.10」
就会让人得出「这套东西还是单写者玩具」的结论 —— 本次就是被这么误读的。

所以这里把几件最容易漂的事钉死：
  1. 版本号三处必须一致（pyproject / __init__ / CHANGELOG 顶部）；
  2. README 与 llms.txt 必须写着当前版本；
  3. 文档里不许再把 `store.json` 当成默认存储（0.10 起默认是 SQLite）；
  4. 历史文档必须带时效声明 —— 否则模型读进来会当成现状。
"""
from __future__ import annotations

import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import memory_palace  # noqa: E402


def _read(*parts: str) -> str:
    with open(os.path.join(ROOT, *parts), "r", encoding="utf-8") as f:
        return f.read()


def test_version_is_consistent() -> None:
    pyproject = _read("pyproject.toml")
    m = re.search(r'^version = "([^"]+)"', pyproject, re.M)
    assert m, "pyproject 里找不到 version"
    assert m.group(1) == memory_palace.__version__, (m.group(1), memory_palace.__version__)

    changelog = _read("CHANGELOG.md")
    first = re.search(r"^## \[(\d+\.\d+\.\d+)\]", changelog, re.M)
    assert first, "CHANGELOG 找不到版本小节"
    assert first.group(1) == memory_palace.__version__, (
        f"CHANGELOG 顶部是 {first.group(1)}，代码是 {memory_palace.__version__}"
    )


def test_readme_and_llms_state_current_version() -> None:
    version = memory_palace.__version__
    for name in ("README.md", "llms.txt"):
        text = _read(name)
        assert version in text, f"{name} 没写当前版本 {version}"


def test_llms_txt_covers_current_capabilities() -> None:
    """llms.txt 是给机器读的现状。它必须提到存储、并发、嵌入与领域数。"""
    text = _read("llms.txt")
    for token in ("SQLite", "WAL", "BEGIN IMMEDIATE", "ollama", "sentence-transformers"):
        assert token in text, f"llms.txt 缺少 {token}"
    for domain in ("zh-finance", "zh-tech", "zh-medical", "zh-legal", "zh-support", "zh-personal"):
        assert domain in text, f"llms.txt 缺少领域 {domain}"


def test_no_doc_claims_json_is_the_default_store() -> None:
    """0.10 起默认是 SQLite。文档里再把 store.json 当默认路径，就是主动误导评审。

    允许提到 store.json —— 它是可选的 JSON 后端、也是 0.9 的旧默认；
    不允许的是把它写成「不传 --store 时的路径」。
    """
    offenders = []
    for dirpath, _dirs, files in os.walk(ROOT):
        if any(skip in dirpath for skip in (".git", "__pycache__", "outputs", ".workbuddy")):
            continue
        for name in files:
            if not name.endswith(".md"):
                continue
            # CHANGELOG 是历史账本：它必须能写下「当时默认是什么」，否则不能算账本。
            if name == "CHANGELOG.md":
                continue
            path = os.path.join(dirpath, name)
            with open(path, "r", encoding="utf-8") as f:
                text = f.read()
            for pattern in ("默认 `~/.memory-palace/store.json`", ".memory-palace/store.json"):
                if pattern in text:
                    offenders.append(f"{os.path.relpath(path, ROOT)} :: {pattern}")
    assert not offenders, "这些文档还把 store.json 当默认路径：" + "; ".join(offenders)


def test_historical_docs_are_marked() -> None:
    """历史文档不删（记录有价值），但必须一眼看出「这是当时的判断」。"""
    for name in ("docs/COMPETITIVE_ANALYSIS.md", "docs/CRITIQUE_RESPONSE.md"):
        head = _read(*name.split("/")).split("---")[0][:600]
        assert "时效" in head, f"{name} 开头缺少时效声明，会被当成当前状态"
        assert "llms.txt" in head, f"{name} 的时效声明应指向 llms.txt"


def test_llms_full_is_current() -> None:
    """`llms-full.txt` 给评审模型用，过期比没有更糟：它会被当成现状引用。

    这条检查是「改代码但忘了重新生成包」的唯一防线。
    """
    import subprocess

    proc = subprocess.run(
        [sys.executable, os.path.join(ROOT, "tools", "make_review_bundle.py"), "--check"],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, (proc.stdout + proc.stderr).strip()


def test_llms_full_lists_storage_layer() -> None:
    """包必须让「sqlite_store.py 存在」这件事一目了然。

    这次评审的原始误判就是「没看到 SQLite」——所以直接钉住这一行。
    """
    text = _read("llms-full.txt")
    assert "memory_palace/sqlite_store.py" in text
    assert "tests/test_store.py" in text


def test_readme_numbers_have_a_source() -> None:
    """README 里出现的性能数字必须能对上一条命令，否则就是「传说」。"""
    text = _read("README.md")
    for command in ("bench 1000", "memory-palace eval", "calibrate"):
        assert command in text, f"README 缺少复现命令 {command}"


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"  [PASS] {name}")
    print("\n文档一致性自测通过 ✅")
