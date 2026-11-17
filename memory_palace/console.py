"""console.py - 配套控制台：一个自包含的 HTML 文件，双击即开。

为什么不是「起个服务 + 前端工程」：

1. 这个库的第一条约束是零依赖。控制台如果要做构建、装 node_modules、引 CDN，
   等于给「下载下来就能跑」加了一道门槛。
2. 它要能**离线**看、能给不装 Python 的人看、能当附件发出去。
   一个 `.html` 文件满足全部三条，一个 dev server 一条都不满足。
3. 数据量本来就不大：1e4 条记忆的曲线压进去也就几百 KB。

所以这里的做法是：CLI 采样 → 内嵌 JSON → 原生 JS 渲染。没有构建步骤，
没有 `npm install`，改完之后刷新浏览器就是最新版本。

它回答四个问题，和 `view` / `render` 的分工：

    view      终端里快速看一眼「谁最重要、谁快被忘掉」
    render    一张静态报告：卡片 + 衰减 + 墓碑
    console   能筛、能排、能展开的**仪表盘**：多一个「代谢时间线」和「寿命时间轴」

视觉上刻意避开两件事：紫蓝渐变（一眼 AI 味）、大面积纯黑（对比过强、读久了累）。
底色是极浅的暖灰，强调色只用在「当前所在」和关键数字上 —— 一个工具不该抢内容的戏。
"""
from __future__ import annotations

import json
import os
from typing import Any

from .render import TIER_COLOR, TIER_LABEL, _days_to_floor, decay_series, life_label

CONSOLE_CSS = """
*,*::before,*::after{box-sizing:border-box}
:root{
  --bg:#f5f5f3; --surface:#ffffff; --surface-2:#fafaf9;
  --line:rgba(24,24,27,.09); --line-strong:rgba(24,24,27,.16);
  --ink:#18181b; --ink-2:#3f3f46; --muted:#71717a; --faint:#a1a1aa;
  --accent:#0f766e; --accent-soft:rgba(15,118,110,.09);
  --warn:#b45309; --warn-soft:rgba(180,83,9,.10);
  --danger:#b91c1c;
  --core:#b3462f; --fact:#2f6f5e; --event:#4a6fa5; --ephemeral:#78786e; --trace:#a08a5a;
  --shadow:0 1px 2px rgba(24,24,27,.04),0 8px 24px -18px rgba(24,24,27,.28);
  --radius:14px;
}
@media (prefers-color-scheme:dark){:root{
  --bg:#0b0c0d; --surface:#131517; --surface-2:#181a1c;
  --line:rgba(255,255,255,.08); --line-strong:rgba(255,255,255,.16);
  --ink:#e9e9e7; --ink-2:#c9c9c6; --muted:#8d9196; --faint:#6b6f74;
  --accent:#2dd4bf; --accent-soft:rgba(45,212,191,.10);
  --warn:#f0a04b; --warn-soft:rgba(240,160,75,.12);
  --danger:#f87171;
  --core:#e0765c; --fact:#5ec8a8; --event:#7fa8d8; --ephemeral:#a3a39a; --trace:#c8ab74;
  --shadow:0 1px 2px rgba(0,0,0,.4),0 8px 24px -18px rgba(0,0,0,.9);
}}
html[data-theme="light"]{
  --bg:#f5f5f3; --surface:#ffffff; --surface-2:#fafaf9;
  --line:rgba(24,24,27,.09); --line-strong:rgba(24,24,27,.16);
  --ink:#18181b; --ink-2:#3f3f46; --muted:#71717a; --faint:#a1a1aa;
  --accent:#0f766e; --accent-soft:rgba(15,118,110,.09);
  --warn:#b45309; --warn-soft:rgba(180,83,9,.10); --danger:#b91c1c;
  --core:#b3462f; --fact:#2f6f5e; --event:#4a6fa5; --ephemeral:#78786e; --trace:#a08a5a;
}
html[data-theme="dark"]{
  --bg:#0b0c0d; --surface:#131517; --surface-2:#181a1c;
  --line:rgba(255,255,255,.08); --line-strong:rgba(255,255,255,.16);
  --ink:#e9e9e7; --ink-2:#c9c9c6; --muted:#8d9196; --faint:#6b6f74;
  --accent:#2dd4bf; --accent-soft:rgba(45,212,191,.10);
  --warn:#f0a04b; --warn-soft:rgba(240,160,75,.12); --danger:#f87171;
  --core:#e0765c; --fact:#5ec8a8; --event:#7fa8d8; --ephemeral:#a3a39a; --trace:#c8ab74;
}
html{color-scheme:light dark}
body{
  margin:0;background:var(--bg);color:var(--ink);
  font:14px/1.6 -apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Hiragino Sans GB","Microsoft YaHei",system-ui,sans-serif;
  -webkit-font-smoothing:antialiased;
}
.num{font-variant-numeric:tabular-nums;font-feature-settings:"tnum"}
.app{display:grid;grid-template-columns:224px 1fr;min-height:100vh}

/* ── 侧栏 ── */
.side{border-right:1px solid var(--line);padding:22px 14px;position:sticky;top:0;height:100vh;
  display:flex;flex-direction:column;gap:22px;background:var(--surface-2)}
.brand{display:flex;align-items:center;gap:9px;padding:0 8px}
.brand .mark{width:22px;height:22px;flex:none;color:var(--accent)}
.brand b{font-size:14px;letter-spacing:.01em;font-weight:600}
.brand span{display:block;font-size:10.5px;color:var(--faint);letter-spacing:.10em;text-transform:uppercase;margin-top:1px}
nav{display:flex;flex-direction:column;gap:2px}
nav button{
  all:unset;display:flex;align-items:center;gap:9px;padding:8px 10px;border-radius:9px;
  font-size:13px;color:var(--muted);cursor:pointer;transition:background .15s,color .15s;
}
nav button:hover{background:var(--line);color:var(--ink-2)}
nav button[aria-current="true"]{background:var(--accent-soft);color:var(--accent);font-weight:560}
nav button .n{margin-left:auto;font-size:11px;color:var(--faint)}
nav svg{width:15px;height:15px;flex:none;opacity:.9}
.side-foot{margin-top:auto;padding:0 8px;font-size:10.5px;line-height:1.7;color:var(--faint);word-break:break-all}
.side-foot b{color:var(--muted);font-weight:500;display:block;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;direction:rtl;text-align:left}
.side-foot b:hover{direction:ltr;white-space:normal}

/* ── 主区 ── */
main{padding:30px 34px 80px;max-width:1160px;width:100%}
.topbar{display:flex;align-items:flex-start;gap:16px;margin-bottom:26px}
.topbar h1{margin:0;font-size:19px;font-weight:600;letter-spacing:-.01em}
.topbar p{margin:3px 0 0;color:var(--muted);font-size:12.5px}
.topbar .spacer{flex:1}
.icon-btn{all:unset;cursor:pointer;width:32px;height:32px;border-radius:9px;border:1px solid var(--line);
  display:grid;place-items:center;color:var(--muted);transition:border-color .15s,color .15s}
.icon-btn:hover{border-color:var(--line-strong);color:var(--ink)}
.icon-btn svg{width:15px;height:15px}

h2.sec{font-size:10.5px;letter-spacing:.11em;text-transform:uppercase;color:var(--faint);
  font-weight:600;margin:30px 0 12px}
h2.sec:first-child{margin-top:0}

/* 指标 */
.metrics{display:grid;grid-template-columns:repeat(auto-fit,minmax(148px,1fr));gap:10px}
.metric{background:var(--surface);border:1px solid var(--line);border-radius:var(--radius);padding:15px 16px}
.metric .k{font-size:10.5px;letter-spacing:.08em;text-transform:uppercase;color:var(--faint);font-weight:600}
.metric .v{font-size:26px;font-weight:600;letter-spacing:-.02em;margin-top:5px;line-height:1.15}
.metric .s{font-size:11.5px;color:var(--muted);margin-top:3px}
.metric.hot .v{color:var(--accent)}
.metric.warn .v{color:var(--warn)}

/* 面板 */
.panel{background:var(--surface);border:1px solid var(--line);border-radius:var(--radius);padding:18px}
.panel + .panel{margin-top:10px}
.panel h3{margin:0 0 12px;font-size:12.5px;font-weight:600;color:var(--ink-2)}
.bar-row{display:grid;grid-template-columns:96px 1fr 44px;align-items:center;gap:10px;
  font-size:12.5px;padding:5px 0}
.bar-row .lab{color:var(--muted);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.track{height:6px;background:var(--line);border-radius:99px;overflow:hidden}
.track > i{display:block;height:100%;border-radius:99px;background:var(--accent);opacity:.85}
.bar-row .val{text-align:right;color:var(--faint);font-size:11.5px}

/* 工具条 */
.tools{display:flex;flex-wrap:wrap;gap:8px;align-items:center;margin-bottom:14px}
.search{flex:1;min-width:180px;position:relative}
.search input{
  width:100%;padding:8px 12px 8px 32px;border-radius:9px;border:1px solid var(--line);
  background:var(--surface);color:var(--ink);font:inherit;font-size:13px;outline:none;
  transition:border-color .15s,box-shadow .15s;
}
.search input:focus{border-color:var(--accent);box-shadow:0 0 0 3px var(--accent-soft)}
.search svg{position:absolute;left:10px;top:50%;transform:translateY(-50%);width:14px;height:14px;color:var(--faint)}
.chips{display:flex;gap:5px;flex-wrap:wrap}
.chip{all:unset;cursor:pointer;padding:5px 10px;border-radius:99px;font-size:12px;color:var(--muted);
  border:1px solid var(--line);background:var(--surface);transition:all .15s}
.chip:hover{border-color:var(--line-strong);color:var(--ink-2)}
.chip[aria-pressed="true"]{background:var(--ink);color:var(--bg);border-color:var(--ink)}
select{appearance:none;padding:7px 28px 7px 11px;border-radius:9px;border:1px solid var(--line);
  background:var(--surface);color:var(--ink-2);font:inherit;font-size:12.5px;cursor:pointer;outline:none;
  background-image:linear-gradient(45deg,transparent 50%,var(--muted) 50%),linear-gradient(135deg,var(--muted) 50%,transparent 50%);
  background-position:calc(100% - 15px) 50%,calc(100% - 10px) 50%;background-size:5px 5px,5px 5px;background-repeat:no-repeat}

/* 记忆卡片 */
.list{display:flex;flex-direction:column;gap:8px}
.mem{background:var(--surface);border:1px solid var(--line);border-radius:var(--radius);overflow:hidden;
  transition:border-color .15s,box-shadow .2s,transform .15s}
.mem:hover{border-color:var(--line-strong);box-shadow:var(--shadow);transform:translateY(-1px)}
.mem-head{display:flex;align-items:center;gap:10px;padding:13px 16px;cursor:pointer}
.mem-head:focus-visible{outline:2px solid var(--accent);outline-offset:-2px}
.dot{width:7px;height:7px;border-radius:99px;flex:none}
.mem-title{flex:1;min-width:0;font-size:13.5px;color:var(--ink);overflow:hidden;
  text-overflow:ellipsis;white-space:nowrap}
.mem-title .tag{color:var(--faint);font-size:11.5px;margin-left:7px}
.mem-meta{display:flex;align-items:center;gap:12px;font-size:11.5px;color:var(--faint);flex:none}
.mem-meta .life{color:var(--muted)}
.mem-meta .life.warn{color:var(--warn)}
.mem-meta .life.pin{color:var(--accent)}
.spark{width:96px;height:24px;flex:none;opacity:.85}
.mem-body{display:grid;grid-template-rows:0fr;transition:grid-template-rows .22s ease}
.mem.open .mem-body{grid-template-rows:1fr}
.mem-body > div{overflow:hidden}
.mem-inner{padding:0 16px 15px;border-top:1px solid var(--line);margin-top:0}
.mem-inner p{margin:12px 0;font-size:13px;color:var(--ink-2);line-height:1.75}
.kv{display:grid;grid-template-columns:repeat(auto-fit,minmax(120px,1fr));gap:8px 18px;font-size:12px}
.kv div{display:flex;justify-content:space-between;gap:8px;padding:5px 0;border-bottom:1px dashed var(--line)}
.kv span:first-child{color:var(--faint)}
.kv span:last-child{color:var(--ink-2)}
.badges{display:flex;gap:6px;flex-wrap:wrap;margin-top:11px}
.badge{font-size:11px;padding:3px 8px;border-radius:6px;background:var(--line);color:var(--muted)}
.badge.warn{background:var(--warn-soft);color:var(--warn)}
.badge.pin{background:var(--accent-soft);color:var(--accent)}

/* 时间轴 */
.axis{position:relative;height:26px;margin:0 0 6px 220px;border-bottom:1px solid var(--line)}
.axis span{position:absolute;top:0;font-size:10.5px;color:var(--faint);white-space:nowrap}
.axis span::after{content:"";position:absolute;left:50%;top:15px;width:1px;height:7px;background:var(--line)}
.age-row{display:grid;grid-template-columns:220px 1fr;align-items:center;gap:0;padding:4px 0}
.age-row .lab{font-size:12px;color:var(--muted);padding-right:12px;overflow:hidden;
  text-overflow:ellipsis;white-space:nowrap}
.age-track{position:relative;height:20px}
.age-track .fill{position:absolute;left:0;top:6px;height:8px;border-radius:99px;opacity:.75}
.age-track .pinmark{position:absolute;right:0;top:2px;font-size:10.5px;color:var(--accent)}
.age-track .fill.pin{background:repeating-linear-gradient(90deg,currentColor 0 5px,transparent 5px 10px);
  opacity:.35;height:2px;top:9px}

/* 事件 */
.events{display:flex;flex-direction:column}
.ev{display:grid;grid-template-columns:15px 1fr auto;gap:11px;padding:9px 0;border-bottom:1px solid var(--line);
  align-items:baseline}
.ev:last-child{border-bottom:0}
.ev .k{font-size:11.5px;color:var(--ink-2)}
.ev .k b{font-weight:560}
.ev .p{font-size:11.5px;color:var(--faint);word-break:break-word}
.ev time{font-size:11px;color:var(--faint);white-space:nowrap}

/* 空态 */
.empty{text-align:center;padding:56px 24px;border:1px dashed var(--line-strong);border-radius:var(--radius);
  background:var(--surface)}
.empty h3{margin:0 0 8px;font-size:15px;font-weight:600}
.empty p{margin:0 auto 18px;color:var(--muted);font-size:13px;max-width:420px}
.empty pre{display:inline-block;text-align:left;margin:0;padding:13px 16px;border-radius:10px;
  background:var(--surface-2);border:1px solid var(--line);font-size:12px;color:var(--ink-2);
  font-family:ui-monospace,SFMono-Regular,"SF Mono",Menlo,Consolas,monospace;line-height:1.9}
.hide{display:none!important}
@media (max-width:820px){
  .app{grid-template-columns:1fr}
  .side{position:static;height:auto;flex-direction:row;align-items:center;gap:12px;overflow-x:auto;
    border-right:0;border-bottom:1px solid var(--line);padding:12px}
  .side-foot{display:none}
  nav{flex-direction:row}
  nav button .n{display:none}
  main{padding:20px 16px 60px}
  .spark{display:none}
  .axis{margin-left:0}
  .age-row{grid-template-columns:110px 1fr}
}
"""

CONSOLE_JS = r"""
const DATA = JSON.parse(document.getElementById('data').textContent);
const TIER = DATA.labels.tier, SCOPE = DATA.labels.scope;
const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];

const fmt = (n, d = 0) => n == null ? '—' : Number(n).toLocaleString('zh-CN',
  { minimumFractionDigits: d, maximumFractionDigits: d });
const esc = (s) => String(s == null ? '' : s).replace(/[&<>"]/g,
  c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
const when = (ts) => {
  if (!ts) return '—';
  const d = new Date(ts * 1000), now = new Date();
  const pad = (n) => String(n).padStart(2, '0');
  const sameDay = d.toDateString() === now.toDateString();
  if (sameDay) return pad(d.getHours()) + ':' + pad(d.getMinutes());
  const diff = Date.now() / 1000 - ts;
  if (diff < 0) return pad(d.getMonth() + 1) + '-' + pad(d.getDate()) + ' ' + pad(d.getHours()) + ':' + pad(d.getMinutes());
  if (diff < 86400 * 7) return Math.round(diff / 86400) + ' 天前';
  if (d.getFullYear() === now.getFullYear()) return pad(d.getMonth() + 1) + '-' + pad(d.getDate());
  return d.getFullYear() + '-' + pad(d.getMonth() + 1) + '-' + pad(d.getDate());
};
const tierColor = (t) => `var(--${t || 'event'})`;

function spark(values, color, h = 24, w = 96) {
  if (!values || !values.length) return '';
  // 统一量程（所有卡片同一个上限）：各自归一化会让「掉得慢的」和「掉得快的」长得一样，
  // 那样这张图就只是装饰了。
  const max = Math.max(1e-6, DATA.ceiling || 1);
  const pts = values.map((v, i) =>
    `${(i / (values.length - 1) * w).toFixed(1)},${(h - 2 - Math.max(0, v) / max * (h - 4)).toFixed(1)}`);
  return `<svg class="spark" viewBox="0 0 ${w} ${h}" fill="none" aria-hidden="true">
    <polyline points="${pts.join(' ')}" stroke="${color}" stroke-width="1.4"
      stroke-linecap="round" stroke-linejoin="round" opacity=".85"/></svg>`;
}

/* ── 总览 ── */
function overview() {
  const s = DATA.summary;
  const metrics = [
    ['在册记忆', fmt(s.nodes), `${s.pinned} 条钉死 · ${s.retired} 条已失效`, ''],
    ['30 天内触底', fmt(s.dying), s.dying ? '再过一阵就真的想不起来了' : '暂时没有记忆会掉出去',
      s.dying ? 'warn' : ''],
    ['已经放下', fmt(s.tombstone), '进墓碑，仍可溯源', ''],
    ['注意力集中度', (s.attention.top1_share * 100).toFixed(0) + '%', 'top1 占比 · 越低越分散', 'hot'],
  ];
  $('#metrics').innerHTML = metrics.map(([k, v, sub, cls]) =>
    `<div class="metric ${cls}"><div class="k">${k}</div><div class="v num">${v}</div>
     <div class="s">${sub}</div></div>`).join('');

  const tiers = Object.entries(s.tiers || {}).sort((a, b) => b[1] - a[1]);
  const maxT = Math.max(1, ...tiers.map(t => t[1]));
  $('#tiers').innerHTML = tiers.map(([t, n]) =>
    `<div class="bar-row"><span class="lab">${TIER[t] || t}</span>
     <span class="track"><i style="width:${n / maxT * 100}%;background:${tierColor(t)}"></i></span>
     <span class="val num">${n}</span></div>`).join('')
    || '<p style="color:var(--muted);font-size:12.5px;margin:0">还没有任何记忆</p>';

  const shares = DATA.shares || [];
  const top = shares[0] ? shares[0].share : 0;
  $('#share-note').textContent = !shares.length ? ''
    : top >= 0.45 ? `注意力很集中：「${shares[0].label}」一家占 ${(top * 100).toFixed(0)}%，要小心回音室`
    : top >= 0.2 ? `注意力有主次：「${shares[0].label}」占 ${(top * 100).toFixed(0)}%`
    : `注意力很分散：最大的「${shares[0].label}」也只占 ${(top * 100).toFixed(0)}% —— 没有谁特别重要`;
  const maxS = Math.max(1e-9, ...shares.map(x => x.share));
  $('#shares').innerHTML = shares.map(x =>
    `<div class="bar-row"><span class="lab">${esc(x.label)}</span>
     <span class="track"><i style="width:${x.share / maxS * 100}%"></i></span>
     <span class="val num">${(x.share * 100).toFixed(0)}%</span></div>`).join('')
    || '<p style="color:var(--muted);font-size:12.5px;margin:0">还没有可以分配注意力的东西</p>';

  const n = s.narrative;
  $('#narrative').innerHTML = n
    ? `<p style="margin:0;color:var(--ink-2);font-size:13px">第 ${n.rev} 版自述 ·
       可靠度 <span class="num">${Number(n.reliability).toFixed(2)}</span> ·
       单主题占比 <span class="num">${Number(n.monopoly).toFixed(2)}</span></p>
       <p style="margin:8px 0 0;font-size:12px;color:var(--muted)">${esc(n.preview || '（这一版还没有章节）')}</p>`
    : '<p style="margin:0;color:var(--muted);font-size:12.5px">还没有形成自述 —— 跑几次 <code>cycle</code> 会长出来。</p>';

  const ch = DATA.channels;
  $('#channels').innerHTML = [
    ['词法通道', `${ch.lexical} 条已索引`],
    ['稠密通道', ch.dense ? `${ch.dense} 条 · ${esc(ch.embedder || '')}` : '未启用（走纯词法）'],
    ['混合权重', Number(ch.hybrid).toFixed(2)],
    ['评测', ch.recall == null ? '未跑' : `recall@5 ${Number(ch.recall).toFixed(3)}`],
  ].map(([k, v]) =>
    `<div style="display:flex;justify-content:space-between;padding:6px 0;border-bottom:1px dashed var(--line)">
      <span style="color:var(--faint);font-size:12px">${k}</span>
      <span style="color:var(--ink-2);font-size:12px">${v}</span></div>`).join('');
}

/* ── 记忆列表 ── */
const state = { q: '', tier: 'all', sort: 'score', pinFirst: false };

function match(n) {
  if (state.tier !== 'all' && n.tier !== state.tier) return false;
  if (!state.q) return true;
  const q = state.q.toLowerCase();
  return (n.content + ' ' + n.name + ' ' + (n.tags || []).join(' ')).toLowerCase().includes(q);
}

function sortNodes(list) {
  const by = state.sort;
  const key = by === 'life'
    ? (a, b) => (a.days == null ? 1e12 : a.days) - (b.days == null ? 1e12 : b.days)
    : by === 'recent'
      ? (a, b) => (b.accessed || 0) - (a.accessed || 0)
      : (a, b) => b.score - a.score;
  const out = [...list].sort(key);
  return state.pinFirst ? out.sort((a, b) => (b.pinned ? 1 : 0) - (a.pinned ? 1 : 0)) : out;
}

function renders() {
  const list = sortNodes(DATA.nodes.filter(match));
  $('#count').textContent = `${list.length} / ${DATA.nodes.length}`;
  if (!list.length) {
    $('#list').innerHTML = `<div class="empty"><h3>没有符合条件的记忆</h3>
      <p>换个关键词，或把筛选器调回「全部」。</p></div>`;
    return;
  }
  $('#list').innerHTML = list.map(n => {
    const lifeCls = n.pinned ? 'pin' : (n.days != null && n.days <= 30 ? 'warn' : '');
    const life = n.pinned ? '钉死' : (n.life || '—');
    return `<article class="mem" data-name="${esc(n.name)}">
      <div class="mem-head" tabindex="0" role="button" aria-expanded="false">
        <span class="dot" style="background:${tierColor(n.tier)}"></span>
        <span class="mem-title">${esc(n.content)}
          <span class="tag">${esc((n.tags || []).join(' · ') || TIER[n.tier] || '')}</span></span>
        <span class="mem-meta">
          ${spark(n.curve, tierColor(n.tier))}
          <span class="life ${lifeCls}">${esc(life)}</span>
          <span class="num" style="color:var(--faint)">${Number(n.score).toFixed(2)}</span>
        </span>
      </div>
      <div class="mem-body"><div><div class="mem-inner">
        <div class="kv">
          <div><span>名字</span><span>${esc(n.name)}</span></div>
          <div><span>作用域</span><span>${esc(SCOPE[n.scope] || n.scope)}</span></div>
          <div><span>层级</span><span>${esc(TIER[n.tier] || n.tier)}</span></div>
          <div><span>当前强度</span><span class="num">${Number(n.weight).toFixed(3)}</span></div>
          <div><span>可靠度</span><span class="num">${Number(n.fidelity).toFixed(2)}</span></div>
          <div><span>被想起</span><span class="num">${n.accesses} 次</span></div>
          <div><span>上次访问</span><span>${when(n.accessed)}</span></div>
          <div><span>写入时间</span><span>${when(n.created)}</span></div>
        </div>
        ${n.affect ? `<div class="badges"><span class="badge">情绪 ${n.affect.tone} ${Number(n.affect.v).toFixed(2)}</span>
          <span class="badge">唤醒 ${Number(n.affect.a).toFixed(2)}</span>
          <span class="badge">来源 ${esc(n.affect.src)}</span></div>` : ''}
        <div class="badges">
          ${n.pinned ? '<span class="badge pin">钉死 · 永不衰减</span>' : ''}
          ${n.consolidated ? `<span class="badge">由 ${n.consolidated} 条沉淀而来</span>` : ''}
          ${n.fidelity < 0.7 ? '<span class="badge warn">情绪强 · 引用细节要存疑</span>' : ''}
          ${n.superseded ? '<span class="badge warn">已被取代 · 默认不再召回</span>' : ''}
        </div>
      </div></div></div>
    </article>`;
  }).join('');

  $$('.mem-head').forEach(h => {
    const open = () => {
      const card = h.parentElement;
      const on = card.classList.toggle('open');
      h.setAttribute('aria-expanded', on ? 'true' : 'false');
    };
    h.addEventListener('click', open);
    h.addEventListener('keydown', e => {
      if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); open(); }
    });
  });
}

/* ── 老化时间轴 ── */
function aging() {
  const H = DATA.horizon;
  const rows = DATA.nodes
    .filter(n => !n.pinned && n.days != null)
    .sort((a, b) => a.days - b.days)
    .slice(0, 40);
  const pinned = DATA.nodes.filter(n => n.pinned);
  $('#axis').innerHTML = [0, 0.25, 0.5, 0.75, 1].map(f => {
    const at = f * 100, shift = f === 1 ? 'translateX(-100%)' : f === 0 ? 'none' : 'translateX(-50%)';
    return `<span style="left:${at}%;transform:${shift}">${Math.round(H * f)} 天</span>`;
  }).join('');
  const body = rows.map(n => {
    const pct = Math.max(1.5, Math.min(100, n.days / H * 100));
    const cls = n.days <= 7 ? 'var(--danger)' : n.days <= 30 ? 'var(--warn)' : tierColor(n.tier);
    const tag = n.tags && n.tags.length ? `[${n.tags[0]}] ` : '';
    return `<div class="age-row"><span class="lab" title="${esc(n.content)}">${esc(tag + n.content)}</span>
      <span class="age-track"><i class="fill" style="width:${pct}%;background:${cls}"></i></span></div>`;
  }).join('') || '<p style="color:var(--muted);font-size:12.5px">没有会衰减的记忆。</p>';
  const pinRows = pinned.map(n =>
    `<div class="age-row"><span class="lab" title="${esc(n.content)}">${esc(n.content)}</span>
     <span class="age-track" style="color:var(--accent)"><i class="fill pin" style="width:100%"></i>
     <span class="pinmark">钉死 · 不参与衰减</span></span></div>`).join('');
  $('#aging').innerHTML = body + pinRows;
}

/* ── 代谢事件 ── */
const KIND = {
  remember: ['写入', 'var(--fact)'], recall: ['想起', 'var(--event)'],
  miss: ['没命中', 'var(--warn)'], forget: ['遗忘', 'var(--ephemeral)'],
  drop: ['丢弃', 'var(--danger)'], consolidate: ['沉淀', 'var(--core)'],
  cycle: ['代谢', 'var(--accent)'], outcome: ['反馈', 'var(--trace)'],
  reflect: ['反思', 'var(--event)'], style: ['风格', 'var(--trace)'],
  appraise: ['情绪评估', 'var(--trace)'], grasp: ['抓取', 'var(--event)'],
  memory_demote: ['记忆降档', 'var(--ephemeral)'], gc: ['清理', 'var(--muted)'],
  narrate: ['自述', 'var(--core)'], persona_health: ['人格体检', 'var(--fact)'],
  selfcorrect_pending: ['参数待验证', 'var(--warn)'],
  selfcorrect_reverted: ['参数已回滚', 'var(--danger)'],
};
function events() {
  const rows = (DATA.events || []).slice(0, 100);
  $('#events').innerHTML = rows.map(e => {
    const [name, color] = KIND[e.kind] || [e.kind, 'var(--muted)'];
    return `<div class="ev"><span class="dot" style="background:${color};margin-top:6px"></span>
      <span class="k"><b>${esc(name)}</b><span class="p" style="margin-left:9px">${esc(e.detail || '')}</span></span>
      <time>${when(e.ts)}</time></div>`;
  }).join('') || '<p style="color:var(--muted);font-size:12.5px">还没有事件。</p>';
}

/* ── 墓碑 ── */
function tombs() {
  const rows = DATA.tombs || [];
  $('#tombs').innerHTML = rows.map(t =>
    `<div class="mem"><div class="mem-head" style="cursor:default">
      <span class="dot" style="background:var(--faint)"></span>
      <span class="mem-title" style="color:var(--muted)">${esc(t.content)}</span>
      <span class="mem-meta"><span>${when(t.at)}</span></span>
    </div></div>`).join('')
    || `<div class="empty"><h3>墓碑是空的</h3><p>还没有记忆被主动放下。这是好事 —— 说明该留的都还留着。</p></div>`;
}

/* ── 视图切换 ── */
function go(view) {
  $$('main > section').forEach(s => s.classList.toggle('hide', s.dataset.view !== view));
  $$('nav button').forEach(b => b.setAttribute('aria-current', b.dataset.go === view ? 'true' : 'false'));
  if (view === 'aging') aging();
  if (view === 'events') events();
  if (view === 'tombs') tombs();
}

function theme(t) {
  const html = document.documentElement;
  if (t) { html.dataset.theme = t; try { localStorage.setItem('mp-theme', t); } catch (e) {} }
  const cur = html.dataset.theme || (matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light');
  $('#theme').setAttribute('aria-label', cur === 'dark' ? '切到浅色' : '切到深色');
}

/* ── 启动 ── */
(function boot() {
  const saved = (() => { try { return localStorage.getItem('mp-theme'); } catch (e) { return null; } })();
  if (saved) document.documentElement.dataset.theme = saved;
  theme();

  $('#theme').addEventListener('click', () => {
    const cur = document.documentElement.dataset.theme
      || (matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light');
    theme(cur === 'dark' ? 'light' : 'dark');
  });
  $$('nav button').forEach(b => b.addEventListener('click', () => go(b.dataset.go)));

  const hash = (location.hash || '').slice(1);
  if (['overview', 'memories', 'aging', 'events', 'tombs'].includes(hash)) go(hash);

  if (!DATA.nodes.length) {
    $('#empty').classList.remove('hide');
    $$('main > section').forEach(s => { if (s.dataset.view !== 'overview' && s.id !== 'empty') s.classList.add('hide'); });
    return;
  }
  overview(); renders(); aging(); events(); tombs();

  $('#q').addEventListener('input', e => { state.q = e.target.value.trim(); renders(); });
  $$('[data-tier]').forEach(b => b.addEventListener('click', () => {
    state.tier = b.dataset.tier;
    $$('[data-tier]').forEach(x => x.setAttribute('aria-pressed', x === b ? 'true' : 'false'));
    renders();
  }));
  $('#sort').addEventListener('change', e => { state.sort = e.target.value; renders(); });
  $('#pinFirst').addEventListener('click', e => {
    state.pinFirst = !state.pinFirst;
    e.currentTarget.setAttribute('aria-pressed', state.pinFirst ? 'true' : 'false');
    renders();
  });
})();
"""


def _esc(value: Any) -> str:
    return (
        str(value)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _event_detail(event: dict[str, Any]) -> str:
    """把事件 payload 翻成一句人话。

    不能只做「键名白名单扫描」：`gc` 的 `deleted`、`narrate` 的 `reliability`、
    `selfcorrect_*` 的 `before/after` 语义完全不同，通用扫描出来的是一串没有主语的名词。
    事件流是给人看「这一轮到底发生了什么」的，每个类型都得单独翻译。
    """
    kind = str(event.get("kind") or "")
    payload = event.get("payload") or {}
    if not isinstance(payload, dict):
        return ""

    def names(key: str, limit: int = 3) -> str:
        values = payload.get(key) or []
        if not isinstance(values, list):
            return ""
        return "、".join(str(v) for v in values[:limit]) + ("…" if len(values) > limit else "")

    if kind == "appraise":
        bits = [str(payload.get("name") or "?")]
        if isinstance(payload.get("valence"), (int, float)):
            bits.append(f"效价 {float(payload['valence']):+.2f}")
        if isinstance(payload.get("arousal"), (int, float)):
            bits.append(f"唤醒 {float(payload['arousal']):.2f}")
        drivers = payload.get("drivers") or []
        if drivers:
            bits.append("触发词 " + "、".join(str(d) for d in drivers[:3]))
        return " · ".join(bits)
    if kind == "grasp":
        return f"抓住 {len(payload.get('grabbed') or [])} 条 · 预算 {payload.get('spent', 0):g}"
    if kind == "memory_demote":
        count = payload.get("count") or len(payload.get("names") or [])
        return f"降档 {count} 条" + (f"：{names('names')}" if names("names") else "")
    if kind == "gc":
        deleted = payload.get("deleted") or []
        return f"清掉 {len(deleted)} 条" + (f"：{names('deleted')}" if deleted else "")
    if kind == "narrate":
        return (
            f"第 {payload.get('rev', 0)} 版 · 可靠度 {float(payload.get('reliability', 0)):.2f}"
            f" · 单主题 {float(payload.get('monopoly', 0)):.2f}"
        )
    if kind == "persona_health":
        return "一致" if payload.get("consistent") else f"漂移 {float(payload.get('distance', 0)):.2f}"
    if kind == "reflect":
        return f"{payload.get('findings', 0)} 条发现"
    if kind.startswith("selfcorrect"):
        verdict = "待验证" if kind.endswith("pending") else "已回滚"
        return f"{payload.get('code', '?')}：{payload.get('before', '?')} → {payload.get('after', '?')}（{verdict}）"
    if kind == "consolidate":
        return f"结晶出 {names('created') or '1 条'}"
    if kind in ("remember", "drop", "forget", "recall", "miss", "outcome", "style"):
        return names("names") or str(payload.get("name") or payload.get("query") or "")[:60]
    # 兜底：只报最有信息量的前两个短字段，不要整包 JSON
    bits = []
    for key in ("name", "query", "reason", "count", "rev"):
        if key in payload:
            bits.append(f"{key} {payload[key]}")
    return " · ".join(bits[:2])


def collect(
    palace,
    *,
    horizon_days: int = 60,
    limit: int = 300,
    scope: str = "auto",
) -> dict[str, Any]:
    """把库里的东西压成前端要的形状。

    这里是**唯一**读取内部结构的入口：前端不碰 `MemoryNode`，只吃 JSON。
    将来换存储、加字段，改这一个函数就够了，不用动 HTML。
    """
    from .scope import GLOBAL, label as scope_label, scopes_for

    now = palace.now()
    cfg = palace._config
    # 控制台是只读的，但索引不一定已经建好（新进程、或刚写完还没查过）。
    # 不 sync 的话「词法通道 0 条已索引」会让人以为语义层坏了，其实只是没热身。
    palace.semantics.sync(palace.graph)
    snap = palace.snapshot()
    scopes = None if scope == "all" else scopes_for(scope, palace.project)
    assessment = palace.assessment(now)

    live = [
        n for n in palace.graph.all_nodes()
        if not n.is_retired() and (scopes is None or n.scope in scopes)
    ]
    live.sort(key=lambda n: -assessment.get(n.name, {}).get("total", 0.0))
    shown = live[:limit]

    total_mass = sum(max(0.0, v.get("total", 0.0)) for v in assessment.values()) or 1.0
    nodes = []
    for node in shown:
        score = float(assessment.get(node.name, {}).get("total", 0.0))
        days = _days_to_floor(node, now, cfg.forget_w, cfg.affect_stability_gain)
        fidelity = node.affect.fidelity(cfg.affect_distortion)
        affect = None
        if node.affect.source != "neutral" and (
            abs(node.affect.valence) >= 0.4 or node.affect.arousal >= 0.5
        ):
            affect = {
                "tone": "负" if node.affect.valence < 0 else "正",
                "v": round(node.affect.valence, 2),
                "a": round(node.affect.arousal, 2),
                "src": node.affect.source,
            }
        nodes.append(
            {
                "name": node.name,
                "content": node.content[:240],
                "tier": node.tier,
                "tags": list(node.tags)[:5],
                "scope": node.scope,
                "pinned": bool(node.pinned),
                "score": round(score, 4),
                "weight": round(node.effective_weight(now), 4),
                "fidelity": round(fidelity, 3),
                "days": None if days is None else round(days, 1),
                "life": "钉死" if node.pinned else life_label(days),
                "accesses": int(node.access_count),
                "accessed": float(node.last_accessed),
                "created": float(node.created_at),
                "curve": [round(v, 4) for v in decay_series(node, now, horizon_days, 15)],
                "consolidated": len(node.consolidated_from),
                "superseded": bool(node.superseded_by),
                "affect": affect,
            }
        )

    pinned_n = sum(1 for n in live if n.pinned)
    dying = sum(
        1 for n in live
        if not n.pinned and (_days_to_floor(n, now, cfg.forget_w, cfg.affect_stability_gain) or 1e9) <= 30
    )
    retired = sum(1 for n in palace.graph.all_nodes() if n.is_retired())
    attention = snap.get("attention") or {}

    # 按**标签**聚合注意力，而不是一条一行：同一枚「硬规则」出现在两条记忆上时，
    # 拆成两个 11% 会让人以为「有两件事各占一成」，其实是一类事占了两成。
    bucket: dict[str, float] = {}
    for node in live:
        score = max(0.0, assessment.get(node.name, {}).get("total", 0.0))
        tag = node.tags[0] if node.tags else (node.content[:12] or node.name)
        bucket[tag] = bucket.get(tag, 0.0) + score
    shares = [
        {"label": tag, "share": round(total / total_mass, 4)}
        for tag, total in sorted(bucket.items(), key=lambda kv: -kv[1])[:8]
    ]

    head = palace.narrative_head()
    narrative = None
    if head is not None:
        raw = head.to_dict() if hasattr(head, "to_dict") else {}
        chapters = raw.get("chapters") or []
        preview = " → ".join(
            f"{c.get('arc', '')}「{c.get('title', '')}」" for c in chapters[:4] if c.get("title")
        ) or "（这一版还没有章节）"
        narrative = None if int(raw.get("rev", 0)) <= 0 else {
            "rev": int(raw.get("rev", 0)),
            "reliability": float(raw.get("reliability", 0.0)),
            "monopoly": float(raw.get("monopoly", 0.0)),
            "preview": preview,
        }

    # 稠密通道：`_dense` 是 DenseIndex，`len()` 是已入索引的向量数。
    # 不能从 snapshot 里拿 —— 那里报的是词法索引条数，不是稠密向量数。
    dense_index = getattr(palace.semantics, "_dense", None)
    dense = len(dense_index) if dense_index is not None else 0
    embedder_obj = getattr(palace.semantics, "embedder", None)
    embedder = getattr(embedder_obj, "name", type(embedder_obj).__name__) if embedder_obj else ""

    # 墓碑时间：不能用 `last_accessed` —— 那是「最后一次被想起」，不是「什么时候被放下」。
    # 从事件流里反查 drop / gc 的时间，查不到才退回节点自己的时间戳。
    dropped_at: dict[str, float] = {}
    for event in palace.events:
        if event.kind not in ("drop", "gc"):
            continue
        payload = event.payload or {}
        for name in (payload.get("deleted") or []):
            dropped_at[str(name)] = float(event.ts)
        if payload.get("name"):
            dropped_at[str(payload["name"])] = float(event.ts)
    tombs = [
        {
            "name": name,
            "content": node.content[:180],
            "at": dropped_at.get(name, float(getattr(node, "last_accessed", 0.0) or 0.0)),
        }
        for name, node in list(palace.tombstone.items())[-40:]
    ]

    events = [
        {
            "kind": str(e.kind),
            "ts": float(e.ts),
            "detail": _event_detail({"kind": e.kind, "payload": e.payload}),
        }
        for e in palace.events[-100:]
    ][::-1]

    return {
        "generated_at": now,
        "store": {
            "path": palace._path or "（内存）",
            "size_kb": round(
                os.path.getsize(palace._path) / 1024, 1
            ) if palace._path and os.path.exists(palace._path) else 0.0,
        },
        "project": palace.project,
        "view_scope": "全部作用域" if scopes is None else "、".join(scope_label(s) for s in sorted(scopes)),
        "horizon": int(horizon_days),
        "summary": {
            "nodes": snap["nodes"],
            "edges": snap["edges"],
            "tombstone": snap["tombstone"],
            "events": snap["events"],
            "pinned": pinned_n,
            "dying": dying,
            "retired": retired,
            "tiers": snap["tiers"],
            "entropy_health": snap["entropy_health"],
            "structure_health": snap["structure_health"],
            "attention": {
                "top1_share": float(attention.get("top_share", 0.0)),
                "top3_share": float(attention.get("top3_share", 0.0)),
                "effective_n": float(attention.get("effective_n", 0.0)),
            },
            "narrative": narrative,
            "active_style": snap.get("active_style") or [],
        },
        "channels": {
            "lexical": int(snap.get("semantic_indexed", 0)),
            "dense": dense,
            "embedder": embedder,
            "hybrid": float(getattr(cfg, "hybrid_weight", 0.0)),
            "recall": None,
        },
        "labels": {
            "tier": TIER_LABEL,
            "scope": {GLOBAL: "全局", f"proj:{palace.project}": "当前项目"},
        },
        "ceiling": max(1.0, max((n["weight"] for n in nodes), default=1.0)),
        "nodes": nodes,
        "shares": shares,
        "events": events,
        "tombs": tombs,
        "truncated": len(live) > len(shown),
    }


def render_html(data: dict[str, Any], title: str = "记忆宫殿 · 控制台") -> str:
    raw = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    # `</script>` 会提前闭合脚本块；`<\/` 在 JSON 里等价于 `</`，但不再被 HTML 解析器识别
    payload = raw.replace("</", "<\\/")
    s = data["summary"]
    subtitle = (
        f"{s['nodes']} 条在册 · {s['pinned']} 条钉死 · {s['dying']} 条 30 天内触底"
        f" · 视图：{data['view_scope']}"
    )
    empty = (
        '<section id="empty" class="hide" style="margin-top:8px">'
        '<div class="empty"><h3>这个记忆库还是空的</h3>'
        "<p>控制台读的是真实数据，不会替你造几条好看的样例。先写一条结论，再回来看：</p>"
        "<pre>memory-palace remember \"叫我 Howard，不要加先生/老板这类后缀\" --tier fact --pin\n"
        "memory-palace remember \"以后输出表格化，风险提示放末尾\" --tags 偏好 --tier fact</pre>"
        "</div></section>"
    )
    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{_esc(title)}</title>
<style>{CONSOLE_CSS}</style>
</head>
<body>
<div class="app">
  <aside class="side">
    <div class="brand">
      <svg class="mark" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6"
           stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
        <path d="M4 20V9.5L12 4l8 5.5V20"/><path d="M9 20v-6h6v6"/>
      </svg>
      <div><b>记忆宫殿</b><span>memory palace</span></div>
    </div>
    <nav>
      <button data-go="overview" aria-current="true">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7"><rect x="3" y="3" width="7" height="7" rx="1.5"/><rect x="14" y="3" width="7" height="7" rx="1.5"/><rect x="3" y="14" width="7" height="7" rx="1.5"/><rect x="14" y="14" width="7" height="7" rx="1.5"/></svg>
        总览</button>
      <button data-go="memories">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7"><path d="M4 7h16M4 12h16M4 17h10"/></svg>
        记忆<span class="n num">{s['nodes']}</span></button>
      <button data-go="aging">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7"><circle cx="12" cy="12" r="8.5"/><path d="M12 7.5V12l3 2"/></svg>
        老化<span class="n num">{s['dying']}</span></button>
      <button data-go="events">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7"><path d="M3 12h4l2.5-6 4 12L16 12h5"/></svg>
        代谢<span class="n num">{s['events']}</span></button>
      <button data-go="tombs">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7"><path d="M5 20V9a7 7 0 0 1 14 0v11"/><path d="M9 20v-6h6v6"/></svg>
        墓碑<span class="n num">{s['tombstone']}</span></button>
    </nav>
    <div class="side-foot">
      <div><b>{_esc(data['store']['path'])}</b></div>
      <div>{data['store']['size_kb']} KB · 生成于 {_esc(str(data['generated_at'])[:0]) or ''}{_esc(_human_time(data['generated_at']))}</div>
      <div style="margin-top:6px">项目 <b title="{_esc(data['project'])}">{_esc(data['project'])}</b></div>
    </div>
  </aside>
  <main>
    <div class="topbar">
      <div><h1>控制台</h1><p>{_esc(subtitle)}</p></div>
      <div class="spacer"></div>
      <button class="icon-btn" id="theme" aria-label="切换主题">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7">
          <path d="M12 3v2M12 19v2M3 12h2M19 12h2M5.6 5.6l1.4 1.4M17 17l1.4 1.4M18.4 5.6L17 7M7 17l-1.4 1.4"/>
          <circle cx="12" cy="12" r="3.6"/></svg>
      </button>
    </div>

    {empty}

    <section data-view="overview">
      <div class="metrics" id="metrics"></div>
      <h2 class="sec">层级分布</h2>
      <div class="panel" id="tiers"></div>
      <h2 class="sec">注意力份额</h2>
      <div class="panel"><div id="shares"></div>
        <p id="share-note" style="margin:10px 0 0;font-size:12px;color:var(--muted)"></p></div>
      <h2 class="sec">自述与通道</h2>
      <div class="panel"><h3>自述</h3><div id="narrative"></div></div>
      <div class="panel" id="channels"></div>
    </section>

    <section data-view="memories" class="hide">
      <div class="tools">
        <label class="search">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><circle cx="11" cy="11" r="6.5"/><path d="m16 16 4 4"/></svg>
          <input id="q" type="search" placeholder="搜内容、标签、名字…" autocomplete="off">
        </label>
        <div class="chips">
          <button class="chip" data-tier="all" aria-pressed="true">全部</button>
          <button class="chip" data-tier="core" aria-pressed="false">核心</button>
          <button class="chip" data-tier="fact" aria-pressed="false">事实</button>
          <button class="chip" data-tier="event" aria-pressed="false">经历</button>
          <button class="chip" data-tier="ephemeral" aria-pressed="false">临时</button>
        </div>
        <button class="chip" id="pinFirst" aria-pressed="false">钉死优先</button>
        <select id="sort" aria-label="排序">
          <option value="score">按重要度</option>
          <option value="life">按剩余寿命</option>
          <option value="recent">按最近访问</option>
        </select>
        <span id="count" class="num" style="font-size:12px;color:var(--faint)"></span>
      </div>
      <div class="list" id="list"></div>
    </section>

    <section data-view="aging" class="hide">
      <h2 class="sec">未来 {data['horizon']} 天：谁先跌破遗忘线</h2>
      <div class="panel">
        <div class="axis" id="axis"></div>
        <div id="aging"></div>
      </div>
      <p style="font-size:12px;color:var(--muted);margin-top:12px">
        条形长度 = 距跌破遗忘线的天数。钉死的条目单列在底部，它们不参与衰减。
        这里只画最危险的 40 条 —— 全画出来就成了一堵墙。
      </p>
    </section>

    <section data-view="events" class="hide">
      <h2 class="sec">最近发生的事</h2>
      <div class="panel"><div class="events" id="events"></div></div>
    </section>

    <section data-view="tombs" class="hide">
      <h2 class="sec">被主动放下的东西</h2>
      <div class="list" id="tombs"></div>
    </section>
  </main>
</div>
<script id="data" type="application/json">{payload}</script>
<script>{CONSOLE_JS}</script>
</body>
</html>
"""


def _human_time(ts: float) -> str:
    import datetime

    return datetime.datetime.fromtimestamp(float(ts)).strftime("%Y-%m-%d %H:%M")


def write_console(
    palace,
    out: str,
    *,
    title: str = "记忆宫殿 · 控制台",
    horizon_days: int = 60,
    limit: int = 300,
    scope: str = "auto",
) -> str:
    data = collect(palace, horizon_days=horizon_days, limit=limit, scope=scope)
    html = render_html(data, title=title)
    target = os.path.abspath(os.path.expanduser(out))
    parent = os.path.dirname(target)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(target, "w", encoding="utf-8") as handle:
        handle.write(html)
    return target
