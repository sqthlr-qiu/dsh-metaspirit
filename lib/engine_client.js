// engine_client.js —— Python 引擎子进程管理 + JSON-RPC over stdio + 健康检查 + 崩溃重启
// （由 src/engine_client.ts 等价转写：无构建环境时的免编译产物，逻辑须与 TS 源同步维护）
import { spawn, spawnSync } from 'child_process'
import * as path from 'path'
import * as fs from 'fs'
import { fileURLToPath } from 'url'

const __dirname = path.dirname(fileURLToPath(import.meta.url))

export class EngineClient {
  constructor(ctx, config = {}) {
    this.proc = null
    this.pending = new Map()
    this.nextId = 1
    this.buffer = ''
    this.restartCount = 0
    this.maxRestart = 5
    this.stopping = false  // 主动停止标记：stop() 杀进程后 onExit 不应再重启
    this.startedAt = 0     // 本次引擎启动时刻（用于健康运行后重置重启预算）
    // 注意：cordis 的 ctx 是注入代理，访问未 inject 的属性（如 ctx.config）会抛错；
    // 插件配置只能通过 apply(ctx, config) 的第二参数获取。
    this.cfg = { ...(config || {}) }
    this.start()
    // 生命周期由 ctx.effect 管理：会话结束杀掉子进程
    if (ctx && typeof ctx.effect === 'function') {
      ctx.effect(() => this.stop())
    }
  }

  pythonBin() {
    // 1) 环境变量优先（用户显式指定解释器）
    if (process.env.METASPIRIT_PYTHON) return process.env.METASPIRIT_PYTHON
    // 2) 依次探测常见解释器名（Windows 上 py 启动器也覆盖）
    for (const bin of ['python', 'python3', 'py']) {
      if (this.canRun(bin)) return bin
    }
    // 3) 探测不到：给出明确提示，并回退（由 spawn 报错暴露）
    console.error(
      '[metaspirit] 未检测到 Python（python / python3 / py 均不可用）。\n' +
      '          请安装 Python 3.11+，或设置环境变量 METASPIRIT_PYTHON 指向解释器绝对路径。')
    return process.env.METASPIRIT_PYTHON || 'python'
  }

  canRun(bin) {
    try {
      const r = spawnSync(bin, ['--version'])
      // Windows 上 py 可能返回非 0 但仍有输出；以"有版本输出且非文件缺失"为准
      return r.status === 0 || (r.error === undefined && ((r.stdout?.length || 0) + (r.stderr?.length || 0)) > 0)
    } catch {
      return false
    }
  }

  resolveEnginePath() {
    const root = (this.cfg && this.cfg.enginePath) || './engine'
    const candidates = [
      path.resolve(root, 'rpc_server.py'),
      path.resolve(root, 'engine', 'rpc_server.py'),
      path.resolve(process.cwd(), root, 'rpc_server.py'),
      path.resolve(__dirname, '..', 'engine', 'rpc_server.py'),
    ]
    for (const c of candidates) if (fs.existsSync(c)) return c
    return candidates[0]
  }

  start() {
    const enginePy = this.resolveEnginePath()
    const py = this.pythonBin()
    this.startedAt = Date.now()
    this.proc = spawn(py, [enginePy], {
      cwd: path.dirname(enginePy),
      stdio: ['pipe', 'pipe', 'inherit'],
      env: {
        ...process.env,
        METASPIRIT_MOCK: process.env.METASPIRIT_MOCK ||
          (this.cfg && this.cfg.mock ? '1' : ''),
      },
    })
    this.proc.stdout?.on('data', (d) => this.onData(d.toString()))
    this.proc.on('exit', (code) => this.onExit(code))
    this.proc.on('error', (err) => {
      console.error('[metaspirit] 引擎进程错误:', err.message)
    })
  }

  onData(chunk) {
    this.buffer += chunk
    let idx
    while ((idx = this.buffer.indexOf('\n')) >= 0) {
      const line = this.buffer.slice(0, idx).trim()
      this.buffer = this.buffer.slice(idx + 1)
      if (!line) continue
      try {
        const msg = JSON.parse(line)
        if (msg.id !== undefined && this.pending.has(msg.id)) {
          const item = this.pending.get(msg.id)
          this.pending.delete(msg.id)
          if (msg.error) item.reject(new Error(msg.error.message || 'rpc error'))
          else item.resolve(msg.result)
        }
      } catch {
        // 忽略非 JSON 行
      }
    }
  }

  onExit(code) {
    this.proc = null
    // 主动 stop() 触发的退出：属于正常生命周期，不重启（消耗一次标记）
    if (this.stopping) {
      this.stopping = false
      return
    }
    // 稳定运行超过 60s 后视为健康，重置重启预算，避免长会话累计误杀
    if (Date.now() - this.startedAt > 60000) this.restartCount = 0
    if (this.restartCount < this.maxRestart) {
      this.restartCount++
      console.warn(`[metaspirit] 引擎子进程退出(${code})，第 ${this.restartCount} 次重启`)
      this.start()
    } else {
      console.error('[metaspirit] 引擎重启次数超限，已停止')
    }
  }

  rpc(method, params) {
    if (!this.proc || !this.proc.stdin) this.start()
    const id = this.nextId++
    const req = { jsonrpc: '2.0', method, params: params || {}, id }
    return new Promise((resolve, reject) => {
      this.pending.set(id, { resolve, reject })
      this.proc.stdin.write(JSON.stringify(req) + '\n')
      setTimeout(() => {
        if (this.pending.has(id)) {
          this.pending.delete(id)
          reject(new Error(`rpc 超时: ${method}`))
        }
      }, 60000)
    })
  }

  async healthCheck() {
    try {
      const r = await this.rpc('ping', {})
      return !!(r && r.pong)
    } catch {
      return false
    }
  }

  stop() {
    if (this.proc) {
      this.stopping = true
      try { this.proc.kill() } catch { /* ignore */ }
      this.proc = null
    }
  }
}
