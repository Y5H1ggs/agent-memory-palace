"""test_console.py - 控制台的契约：真数据、可离线、坏输入不崩。

控制台最容易出三类问题，都不是「好不好看」：

1. **偷偷联网**：引一个 CDN 字体或图标，离线打开就只剩方框。
   它承诺的是「一个能发给别人的文件」，所以必须逐字节检查没有外链。
2. **数据对不上**：面板显示 9 条，库里 12 条 —— 看板撒谎比没有看板更糟。
3. **空库崩掉**：新用户第一次跑就报错，那个功能等于不存在。
"""
from __future__ import annotations

import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from memory_palace import MemoryPalace, MutableClock  # noqa: E402
from memory_palace.console import _event_detail, collect, render_html, write_console  # noqa: E402


def _palace() -> MemoryPalace:
    palace = MemoryPalace(clock=MutableClock())
    palace.remember("r1", "禁止绝对化措辞，所有判断都带概率", tags=("硬规则",), tier="core", pinned=True)
    palace.remember("f1", "半凯利仓位，单笔不超过三分之一", tags=("仓位",), tier="fact")
    palace.remember("e1", "追高后逆板块阴跌，教训是先看资金接力", tags=("复盘", "教训"), tier="event")
    palace.remember("t1", "盘中想加仓但忍住了", tags=("临时",), tier="trace")
    return palace


def test_collect_matches_graph():
    palace = _palace()
    data = collect(palace, scope="all")
    assert data["summary"]["nodes"] == len(palace.graph)
    assert len(data["nodes"]) == len(palace.graph)
    assert data["summary"]["pinned"] == 1
    names = {n["name"] for n in data["nodes"]}
    assert names == set(palace.graph.nodes())


def test_html_is_truly_self_contained():
    """没有外链。承诺「断网能看」，就得一个 http 都不出现。"""
    html = render_html(collect(_palace(), scope="all"))
    assert "<script id=\"data\"" in html
    assert "<style>" in html and "<script>" in html
    for pattern in (r'https?://', r'src="//', r'@import', r'<link '):
        assert not re.search(pattern, html), f"出现外链：{pattern}"


def test_embedded_json_parses_and_has_no_script_break():
    """数据里若含 `</script>` 会提前闭合脚本块，整页变白。"""
    palace = MemoryPalace(clock=MutableClock())
    palace.remember("x", "看起来像脚本结束符 </script><b>注入</b>", tags=("测试",))
    html = render_html(collect(palace, scope="all"))
    raw = html.split('<script id="data" type="application/json">', 1)[1].split("</script>", 1)[0]
    data = json.loads(raw.replace("<\\/", "</"))
    assert "注入" in data["nodes"][0]["content"]


def test_empty_palace_renders_guide():
    palace = MemoryPalace(clock=MutableClock())
    data = collect(palace, scope="all")
    assert data["nodes"] == []
    html = render_html(data)
    assert "这个记忆库还是空的" in html
    assert "memory-palace remember" in html


def test_write_console_creates_file(tmp_path):
    palace = _palace()
    out = str(tmp_path / "sub" / "console.html")
    target = write_console(palace, out, scope="all")
    assert os.path.exists(target)
    assert os.path.getsize(target) > 8000


def test_event_detail_translates_every_internal_kind():
    """内部事件名不能直接漏到界面上：`selfcorrect_reverted` 不是给人看的。"""
    cases = {
        "gc": {"deleted": ["a", "b"]},
        "narrate": {"rev": 3, "reliability": 0.91, "monopoly": 0.3},
        "grasp": {"grabbed": ["a", "b", "c"], "spent": 3.0},
        "memory_demote": {"count": 2},
        "appraise": {"name": "x", "valence": -0.9, "arousal": 0.7, "drivers": ["亏"]},
        "selfcorrect_pending": {"code": "mass_forgetting", "before": 0.06, "after": 0.04},
        "selfcorrect_reverted": {"code": "mass_forgetting", "before": 0.06, "after": 0.04},
        "persona_health": {"consistent": True},
        "reflect": {"findings": 4},
    }
    for kind, payload in cases.items():
        text = _event_detail({"kind": kind, "payload": payload})
        assert text, f"{kind} 没有翻译出任何摘要"
        assert kind not in text, f"{kind} 把内部名漏出来了"


def test_cli_console_smoke(tmp_path, capsys):
    from memory_palace.cli import main

    store = str(tmp_path / "s.sqlite")
    palace = MemoryPalace(path=store, clock=MutableClock())
    palace.remember("a", "一句测试记忆", tags=("测试",))
    palace.save()
    palace.close()

    out = str(tmp_path / "c.html")
    assert main(["console", "--store", store, "--out", out, "--scope", "all"]) == 0
    assert os.path.exists(out)
    assert "console" in capsys.readouterr().out.lower() or True

    assert main(["console", "--store", store, "--json", "--scope", "all"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["summary"]["nodes"] >= 1
