/**
 * memory-palace 的 DSH 插件（host 半）。
 *
 * 为什么是「调 CLI」而不是「重写一遍逻辑」：
 * 记忆只有一份，语义必须只有一处定义。这个插件是**适配层**——
 * 它只负责把模型的一次调用翻译成 `python -m memory_palace.cli ...`，
 * 再把输出原样交回去。所以换宿主（Claude Code / Codex / Cursor）时，
 * 记忆的写法、衰减、弃答判定完全不变。
 *
 * 工具描述是写给模型看的：它决定模型什么时候调、传什么。
 * 所以每个 description 都写「何时用」，不是「这个函数做什么」。
 */
import { spawn } from 'node:child_process'

export const name = 'memory-palace'

// inject 不是可选项：它让 Cordis 先把 tools 注册表准备好，apply 才能安全用 ctx.tools
export const inject = ['tools']

const SETTINGS = {
  python: process.env.MEMORY_PALACE_PYTHON || 'python',
  store: process.env.MEMORY_PALACE_STORE || '',
  project: process.env.MEMORY_PALACE_PROJECT || '',
  repo: process.env.MEMORY_PALACE_REPO || process.cwd(),
}

function callCli(sub, args, signal) {
  return new Promise((resolve) => {
    const argv = ['-m', 'memory_palace.cli', sub, ...args]
    if (SETTINGS.store) argv.push('--store', SETTINGS.store)
    if (SETTINGS.project) argv.push('--project', SETTINGS.project)

    const child = spawn(SETTINGS.python, argv, {
      cwd: SETTINGS.repo,
      env: { ...process.env, PYTHONPATH: SETTINGS.repo, PYTHONIOENCODING: 'utf-8' },
      windowsHide: true,
      signal,
    })
    let out = ''
    let err = ''
    child.stdout?.on('data', (chunk) => (out += chunk))
    child.stderr?.on('data', (chunk) => (err += chunk))
    child.on('error', (exc) => resolve({ code: -1, out: '', err: String(exc) }))
    child.on('close', (code) => resolve({ code, out: out.trim(), err: err.trim() }))
  })
}

/** 统一的执行壳：子进程失败要**抛**（= isError），让模型自己纠正。 */
async function run(sub, args, signal) {
  const { code, out, err } = await callCli(sub, args, signal)
  if (code !== 0) {
    throw new Error(`memory-palace ${sub} 失败（exit ${code}）：${err || out || '无输出'}`)
  }
  return out || '（无输出）'
}

const text = (value) => [{ type: 'text', text: value }]

export function apply(ctx) {
  ctx.tools.register({
    name: 'memory_context',
    description:
      '取一块受预算约束的长期记忆上下文，用于放进本轮 system prompt。' +
      '在需要延续性的任务开始前调用：用户提到「上次」「之前说的」「按老规矩」、或换了新项目继续同类工作时。' +
      '返回的块里如果写着「没有相关记忆」，就不要凭印象补全。',
    parameters: {
      query: { type: 'string', description: '当前任务的一句话描述（不传则按显著度取）' },
      scope: {
        type: 'string',
        description: 'auto（默认，全局+当前项目）/ global（只要跨项目的）/ project（只要本项目的）/ all',
      },
      budget: { type: 'number', description: '字符预算，默认 900' },
      tree: { type: 'boolean', description: '记忆多于约 30 条时用树状注入，更省 token' },
    },
    output: { schema: { type: 'string' }, render: (_a, v) => text(v) },
    async execute(args, exec) {
      const argv = []
      if (args.query) argv.push('--query', String(args.query))
      if (args.scope) argv.push('--scope', String(args.scope))
      if (args.budget) argv.push('--budget', String(Math.round(args.budget)))
      if (args.tree) argv.push('--tree')
      return run('context', argv, exec.signal)
    },
  })

  ctx.tools.register({
    name: 'memory_remember',
    description:
      '记住一条**结论或教训**（不是过程、不是原文）。用户说「记住」「以后都这样」时调用。' +
      '项目内的决定用默认 scope；用户偏好、硬规则、跨项目通用的教训用 scope=global。' +
      '用户改主意时用 supersedes 指向旧记忆名：旧的会失效而不是被删。',
    parameters: {
      content: { type: 'string', required: true, description: '一句话结论' },
      scope: { type: 'string', description: 'global（跨项目）/ project（默认，本项目）/ session' },
      tags: { type: 'string', description: '逗号分隔标签' },
      tier: { type: 'string', description: 'trace / ephemeral / event / fact / core' },
      pin: { type: 'boolean', description: '钉死：不衰减、不参与自动遗忘，只给真正的硬规则' },
      supersedes: { type: 'string', description: '被本条取代的旧记忆名（多个用逗号分隔）' },
    },
    output: { schema: { type: 'string' }, render: (_a, v) => text(v) },
    async execute(args, exec) {
      const argv = []
      if (args.scope) argv.push('--scope', String(args.scope))
      if (args.tags) argv.push('--tags', String(args.tags))
      if (args.tier) argv.push('--tier', String(args.tier))
      if (args.pin) argv.push('--pin')
      for (const old of String(args.supersedes || '').split(',').map((s) => s.trim()).filter(Boolean)) {
        argv.push('--supersedes', old)
      }
      argv.push(String(args.content))
      return run('remember', argv, exec.signal)
    },
  })

  ctx.tools.register({
    name: 'memory_recall',
    description:
      '按语义或名字检索记忆，返回名字、内容与重要性。需要精确查看某条、或要看「当时怎么定的」时用。' +
      '问历史决定用 as_of（例如 30d 或 2026-10-01）。',
    parameters: {
      query: { type: 'string' },
      scope: { type: 'string', description: 'auto（默认）/ global / project / all' },
      as_of: { type: 'string', description: '回看某个时刻为真的记忆：30d 或 2026-10-01' },
      limit: { type: 'number' },
    },
    output: { schema: { type: 'string' }, render: (_a, v) => text(v) },
    async execute(args, exec) {
      const argv = ['--json']
      if (args.query) argv.push(String(args.query))
      if (args.scope) argv.push('--scope', String(args.scope))
      if (args.as_of) argv.push('--as-of', String(args.as_of))
      if (args.limit) argv.push('-k', String(Math.round(args.limit)))
      return run('recall', argv, exec.signal)
    },
  })

  ctx.tools.register({
    name: 'memory_promote',
    description:
      '把一条项目记忆升为跨项目共享。当某个项目的教训其实是通用的（例如「上线前必须先跑迁移」）时用，' +
      '让下个项目也记得。这是一次显式决定，不要自动把项目细节提升为全局。',
    parameters: {
      name: { type: 'string', required: true, description: '记忆名' },
    },
    output: { schema: { type: 'string' }, render: (_a, v) => text(v) },
    async execute(args, exec) {
      return run('promote', [String(args.name)], exec.signal)
    },
  })

  ctx.tools.register({
    name: 'memory_status',
    description:
      '记忆库体检：规模、作用域分布、价值/结构健康、生效风格。用户问「你记得什么」「记住多少」时先看它。',
    parameters: {},
    output: { schema: { type: 'string' }, render: (_a, v) => text(v) },
    async execute(_args, exec) {
      return run('status', [], exec.signal)
    },
  })
}
