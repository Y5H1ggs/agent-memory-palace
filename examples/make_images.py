"""make_images.py - 生成 README 用的图（截图脚本，可复现）。

    python examples/make_images.py

产出（docs/images/）：
    dashboard.png     记忆面板整页（由 render 生成的真实 HTML 截图）
    terminal-view.png 终端视图 `memory-palace view` 的真实输出
    terminal-tree.png 树状注入 `context --tree` 的真实输出
    architecture.png  架构图（内联 SVG，不是手绘图片）

为什么用脚本生成：README 里的图如果手绘，代码一改图就撒谎。
这里每一张都能用一条命令重跑出来，图上的数字就是 `examples/showcase-store.json` 的数字。
依赖：本机装了 Chrome 或 Edge（headless 截图）。没装则只生成 HTML 并提示手动截图。
"""
from __future__ import annotations

import html
import os
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "docs", "images")
STORE = os.path.join(ROOT, "examples", "showcase-store.json")

CHROME_CANDIDATES = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/usr/bin/google-chrome",
    "/usr/bin/chromium",
]


def find_browser() -> str | None:
    for path in CHROME_CANDIDATES:
        if os.path.exists(path):
            return path
    for name in ("chrome", "google-chrome", "chromium", "msedge"):
        found = shutil.which(name)
        if found:
            return found
    return None


def shot(browser: str, page: str, out: str, width: int, height: int) -> bool:
    cmd = [
        browser, "--headless=new", "--disable-gpu", "--hide-scrollbars",
        "--force-device-scale-factor=1.6",  # 2x 太糊，1.6 在 GitHub 上清晰又不算大
        f"--window-size={width},{height}",
        f"--screenshot={out}",
        "file:///" + page.replace("\\", "/"),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    return os.path.exists(out) and os.path.getsize(out) > 5000


TERMINAL_CSS = """
*{box-sizing:border-box}
body{margin:0;background:transparent;font:13px/1.62 ui-monospace,SFMono-Regular,Menlo,
 "Cascadia Mono",Consolas,"Liberation Mono",monospace}
.term{background:#101215;border-radius:12px;padding:0 0 16px;width:900px;
 box-shadow:0 18px 48px rgba(0,0,0,.28);overflow:hidden}
.bar{display:flex;align-items:center;gap:7px;padding:11px 14px;background:#191c21;
 border-bottom:1px solid #23272e}
.dot{width:10px;height:10px;border-radius:50%}
.t{color:#8b94a3;font-size:11.5px;margin-left:8px;letter-spacing:.4px}
pre{margin:0;padding:15px 18px;color:#d7dbe0;white-space:pre-wrap;word-break:break-word}
.p{color:#7ee0b8}
.b{color:#f0b866}
.d{color:#7d8794}
"""


def terminal_html(title: str, text: str) -> str:
    lines = []
    for raw in text.splitlines():
        line = html.escape(raw)
        if line.startswith("记忆宫殿"):
            line = f'<span class="p">{line}</span>'
        elif line.startswith("重要性") or line.startswith("生效风格") or line.startswith("自述"):
            line = f'<span class="b">{line}</span>'
        elif line.startswith("价值") or line.startswith("已遗忘") or line.startswith("（空库"):
            line = f'<span class="d">{line}</span>'
        lines.append(line)
    body = "\n".join(lines)
    return f"""<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8"><style>{TERMINAL_CSS}</style>
</head><body><div class="term">
<div class="bar">
  <span class="dot" style="background:#ff5f57"></span>
  <span class="dot" style="background:#febc2e"></span>
  <span class="dot" style="background:#28c840"></span>
  <span class="t">{html.escape(title)}</span>
</div>
<pre>{body}</pre>
</div></body></html>"""


ARCH_SVG = """<svg viewBox="0 0 1180 560" xmlns="http://www.w3.org/2000/svg" font-family="system-ui,'PingFang SC','Microsoft YaHei',sans-serif">
  <defs>
    <marker id="a" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto">
      <path d="M0,0 L10,5 L0,10 z" fill="#6b7280"/>
    </marker>
    <style>
      .box{fill:#ffffff;stroke:#d9d7cf;stroke-width:1.4;rx:10}
      .core{fill:#f2f7f5;stroke:#2f6f5e;stroke-width:1.6}
      .side{fill:#fbf8f2;stroke:#cbb27a;stroke-width:1.3}
      .io{fill:#f6f7f9;stroke:#c9ccd3;stroke-width:1.3}
      .t{font-size:15px;fill:#1c1c1a;font-weight:600}
      .s{font-size:12px;fill:#71716a}
      .lbl{font-size:11.5px;fill:#8a8a80}
      .edge{stroke:#6b7280;stroke-width:1.4;fill:none;marker-end:url(#a)}
      .dash{stroke:#b9b6ab;stroke-width:1.2;fill:none;stroke-dasharray:5 4;marker-end:url(#a)}
    </style>
  </defs>

  <text x="40" y="40" class="t" style="font-size:17px">memory-palace · 一轮代谢（每轮固定次序，次序本身是设计）</text>
  <text x="40" y="63" class="s">写入 → 沉淀 → 抓取 → 遗忘 → 自述 → 人格 → 反思 → 自修正</text>

  <rect class="box io" x="40" y="100" width="150" height="72" rx="10"/>
  <text x="58" y="128" class="t" style="font-size:14px">写入 remember</text>
  <text x="58" y="150" class="s">情绪由文本/结果推断</text>

  <rect class="box core" x="240" y="86" width="250" height="100" rx="10"/>
  <text x="260" y="114" class="t">记忆图 + 语义索引</text>
  <text x="260" y="136" class="s">幂等衰减 · 共现边 · 逻辑边</text>
  <text x="260" y="158" class="s">概念投影 / 多义词裁决</text>

  <rect class="box side" x="540" y="86" width="190" height="100" rx="10"/>
  <text x="558" y="114" class="t" style="font-size:14px">沉淀</text>
  <text x="558" y="136" class="s">碎片 → 结论</text>
  <text x="558" y="158" class="s">单向门 · 碎片降级</text>

  <rect class="box side" x="770" y="86" width="180" height="100" rx="10"/>
  <text x="788" y="114" class="t" style="font-size:14px">价值取舍</text>
  <text x="788" y="136" class="s">四分量 + 预算</text>
  <text x="788" y="158" class="s">判决带理由</text>

  <rect class="box side" x="540" y="230" width="190" height="100" rx="10"/>
  <text x="558" y="258" class="t" style="font-size:14px">自述 projection</text>
  <text x="558" y="280" class="s">章节 · 弧线 · 可靠度</text>
  <text x="558" y="302" class="s">不落成节点</text>

  <rect class="box side" x="770" y="230" width="180" height="100" rx="10"/>
  <text x="788" y="258" class="t" style="font-size:14px">反思 / 自修正</text>
  <text x="788" y="280" class="s">只提建议</text>
  <text x="788" y="302" class="s">白名单 + 抗滞后验证</text>

  <rect class="box io" x="240" y="360" width="250" height="86" rx="10"/>
  <text x="260" y="388" class="t" style="font-size:14px">注入块 context</text>
  <text x="260" y="410" class="s">根常驻 · 枝按需展开 · 预算约束</text>

  <rect class="box io" x="540" y="360" width="190" height="86" rx="10"/>
  <text x="558" y="388" class="t" style="font-size:14px">三条接入面</text>
  <text x="558" y="410" class="s">MCP · HTTP · CLI</text>

  <rect class="box io" x="770" y="360" width="180" height="86" rx="10"/>
  <text x="788" y="388" class="t" style="font-size:14px">任意 agent</text>
  <text x="788" y="410" class="s">Claude Code / Codex / DSH …</text>

  <path class="edge" d="M190,136 L236,136"/>
  <path class="edge" d="M490,130 L536,130"/>
  <path class="edge" d="M730,130 L766,130"/>
  <path class="edge" d="M860,186 L860,226"/>
  <path class="edge" d="M636,186 L636,226"/>
  <path class="edge" d="M636,330 L400,356"/>
  <path class="edge" d="M770,280 L730,280" />
  <path class="dash" d="M860,330 L860,356"/>
  <path class="edge" d="M490,392 L536,392"/>
  <path class="edge" d="M730,392 L766,392"/>

  <text x="200" y="126" class="lbl">写入</text>
  <text x="500" y="120" class="lbl">结晶</text>
  <text x="736" y="120" class="lbl">存留</text>
  <text x="644" y="216" class="lbl">讲故事</text>
  <text x="782" y="216" class="lbl">调参</text>
  <text x="500" y="348" class="lbl">取一块</text>
  <text x="736" y="348" class="lbl">注入</text>

  <text x="40" y="500" class="lbl">三条不变量：权重是「锚点+时间」的纯函数（GC 跑 N 次账本不变）· 情绪乘在稳定性上而非权重上 · 叙事是读法不是节点</text>
  <text x="40" y="524" class="lbl">所有改动都是 Patch：带 before/after/观察窗，验证不过就回滚</text>
</svg>"""


def main() -> int:
    os.makedirs(OUT, exist_ok=True)
    if not os.path.exists(STORE):
        print("先运行 python examples/showcase.py 生成演示库", file=sys.stderr)
        return 1
    browser = find_browser()
    if not browser:
        print("没找到 Chrome/Edge，无法自动截图；HTML 已生成，可手动截。", file=sys.stderr)

    tmp = tempfile.mkdtemp(prefix="mp-img-")

    # 1) 面板整页
    page = os.path.join(ROOT, "docs", "sample-palace.html")
    if browser and shot(browser, page, os.path.join(OUT, "dashboard.png"), 1100, 2050):
        print("✓ dashboard.png")

    # 2) 终端视图
    env = dict(os.environ, MEMORY_PALACE_STORE=STORE, PYTHONPATH=ROOT)
    view = subprocess.run(
        [sys.executable, "-m", "memory_palace.cli", "view", "--limit", "9"],
        capture_output=True, text=True, encoding="utf-8", env=env, cwd=ROOT,
    ).stdout.strip()
    tree = subprocess.run(
        [sys.executable, "-m", "memory_palace.cli", "context", "--tree",
         "--query", "怎么避免线上事故", "--budget", "700"],
        capture_output=True, text=True, encoding="utf-8", env=env, cwd=ROOT,
    ).stdout.strip()

    for name, title, text, height in (
        ("terminal-view.png", "memory-palace view", view, 560),
        ("terminal-tree.png", "memory-palace context --tree", tree, 470),
    ):
        html_path = os.path.join(tmp, name.replace(".png", ".html"))
        with open(html_path, "w", encoding="utf-8") as f:
            f.write(terminal_html(title, text))
        if browser and shot(browser, html_path, os.path.join(OUT, name), 940, height):
            print(f"✓ {name}")

    # 3) 架构图
    arch_path = os.path.join(tmp, "arch.html")
    with open(arch_path, "w", encoding="utf-8") as f:
        f.write(
            "<!DOCTYPE html><html><head><meta charset='utf-8'>"
            "<style>body{margin:0;background:#f7f6f3;padding:24px}</style></head><body>"
            + ARCH_SVG + "</body></html>"
        )
    if browser and shot(browser, arch_path, os.path.join(OUT, "architecture.png"), 1240, 620):
        print("✓ architecture.png")

    print(f"\n输出目录：{OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
