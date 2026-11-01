// engine_client.ts —— Python 引擎子进程管理 + JSON-RPC over stdio + 健康检查 + 崩溃重启
import { spawn, spawnSync, ChildProcess } from 'child_process'
import * as path from 'path'
import * as fs from 'fs'
import * as os from 'os'
import { fileURLToPath } from 'url'

// ESM 环境无 __dirname：由 import.meta.url 派生（Node type-stripping 与 tsdown 构建均兼容）
const __dirname = path.dirname(fileURLToPath(import.meta.url))

interface PendingItem {
  resolve: (v: any) => void
  reject: (e: any) => void
}

export class EngineClient {
  private proc: ChildProcess | null = null
  private pending: Map<number, PendingItem> = new Map()
  private nextId = 1
  private buffer = ''
  private cfg: any
  private restartCount = 0
  private maxRestart = 5
  private stopping = false  // 主动停止标记：stop() 杀进程后 onExit 不应再重启
  private startedAt = 0     // 本次引擎启动时刻（用于健康运行后重置重启预算）
  private starting: Promise<void> | null = null  // 防重入：并发 rpc 只 spawn 一次
  private overlayProc: ChildProcess | null = null  // Electron 悬浮窗进程
  private rpcTimeoutMs: number   // rpc 超时（可配；真实 API start/P4+P5 可能 >60s）
  private pythonBin(): string {
    // 1) 环境变量优先（用户显式指定解释器）
    if (process.env.METASPIRIT_PYTHON) return process.env.METASPIRIT_PYTHON
    // 2) 依次探测常见解释器名（Windows 上 py 启动器也覆盖）
    const candidates = ['python', 'python3', 'py']
    for (const bin of candidates) {
      if (this.canRun(bin)) return bin
    }
    // 3) 探测不到：给出明确提示，并回退到 METASPIRIT_PYTHON（可能为空，由 spawn 报错暴露）
    console.error(
      '[metaspirit] 未检测到 Python（python / python3 / py 均不可用）。\n' +
      '          请安装 Python 3.11+，或设置环境变量 METASPIRIT_PYTHON 指向解释器绝对路径。')
    return process.env.METASPIRIT_PYTHON || 'python'
  }

  private canRun(bin: string): boolean {
    try {
      const r = spawnSync(bin, ['--version'])
      // Windows 上 py 可能返回非 0 但仍有输出；以"有版本输出且非文件缺失"为准
      return r.status === 0 || (r.error === undefined && (r.stdout?.length || r.stderr?.length) > 0)
    } catch {
      return false
    }
  }

  constructor(ctx: any, config: any = {}) {
    // 注意：cordis 的 ctx 是注入代理，访问未 inject 的属性（如 ctx.config）会抛错；
    // 插件配置只能通过 apply(ctx, config) 的第二参数获取。
    this.cfg = { ...(config || {}) }
    this.rpcTimeoutMs = Number(this.cfg.rpcTimeoutMs || process.env.METASPIRIT_RPC_TIMEOUT || 120000)
    void this.ensureStart()
    // 生命周期由 ctx.effect 管理：会话结束杀掉子进程
    if (ctx && typeof ctx.effect === 'function') {
      ctx.effect(() => this.stop())
    }
  }

  /** 防重入启动：并发 rpc 只 spawn 一次（修复 D2）。 */
  private ensureStart(): Promise<void> {
    if (this.proc && this.proc.stdin) return Promise.resolve()
    if (!this.starting) {
      this.starting = new Promise<void>((resolve) => {
        this.start()
        resolve()
      })
      this.starting.finally(() => { this.starting = null })
    }
    return this.starting
  }

  private resolveEnginePath(): string {
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

  start(): void {
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
    this.proc.stdout?.on('data', (d: Buffer) => this.onData(d.toString()))
    this.proc.on('exit', (code: number | null) => this.onExit(code))
    this.proc.on('error', (err: Error) => {
      console.error('[metaspirit] 引擎进程错误:', err.message)
    })
  }

  private onData(chunk: string): void {
    this.buffer += chunk
    let idx: number
    while ((idx = this.buffer.indexOf('\n')) >= 0) {
      const line = this.buffer.slice(0, idx).trim()
      this.buffer = this.buffer.slice(idx + 1)
      if (!line) continue
      try {
        const msg = JSON.parse(line)
        if (msg.id !== undefined && this.pending.has(msg.id)) {
          const item = this.pending.get(msg.id)!
          this.pending.delete(msg.id)
          if (msg.error) item.reject(new Error(msg.error.message || 'rpc error'))
          else item.resolve(msg.result)
        }
      } catch {
        // 忽略非 JSON 行（如引擎 stderr 之外的调试输出）
      }
    }
  }

  private onExit(code: number | null): void {
    this.proc = null
    // D3：崩溃/退出时立即 reject 全部 pending 调用（不让调用方等满超时）
    for (const [id, item] of this.pending) {
      this.pending.delete(id)
      item.reject(new Error(`engine exited(${code})，请求被中断`))
    }
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

  rpc(method: string, params: any): Promise<any> {
    const id = this.nextId++
    const req = { jsonrpc: '2.0', method, params: params || {}, id }
    return new Promise((resolve, reject) => {
      this.ensureStart().then(() => {
        if (!this.proc || !this.proc.stdin) {
          reject(new Error('engine stdin 不可用'))
          return
        }
        this.pending.set(id, { resolve, reject })
        this.proc.stdin.write(JSON.stringify(req) + '\n')
        setTimeout(() => {
          if (this.pending.has(id)) {
            this.pending.delete(id)
            reject(new Error(`rpc 超时(${this.rpcTimeoutMs}ms): ${method}`))
          }
        }, this.rpcTimeoutMs)
      }).catch(reject)
    })
  }

  async healthCheck(): Promise<boolean> {
    try {
      const r = await this.rpc('ping', {})
      return !!(r && r.pong)
    } catch {
      return false
    }
  }

  stop(): void {
    if (this.proc) {
      this.stopping = true
      // D5：优雅停机——先发 RPC stop 拿会话统计，宽限 500ms 后强杀
      try {
        this.proc.stdin?.write(JSON.stringify(
          { jsonrpc: '2.0', method: 'stop', params: {}, id: -1 }) + '\n')
      } catch { /* ignore */ }
      const p = this.proc
      setTimeout(() => {
        try { p.kill() } catch { /* ignore */ }
      }, 500)
      this.proc = null
    }
  }

  // —— 桌面悬浮窗（Electron 透明壳）——
  private overlayHelperPath(): string {
    // 多候选路径：兼容 src/ 与 lib/ 两种产物位置
    const candidates = [
      path.resolve(__dirname, '..', '..', 'overlay-helper', 'main.js'),
      path.resolve(__dirname, '..', 'overlay-helper', 'main.js'),
      path.resolve(__dirname, 'overlay-helper', 'main.js'),
    ]
    for (const c of candidates) if (fs.existsSync(c)) return c
    return candidates[0]
  }

  overlayElectronBin(): string | null {
    // 1) 环境变量显式指定（绝对路径，跨平台最可靠）
    if (process.env.METASPIRIT_ELECTRON && fs.existsSync(process.env.METASPIRIT_ELECTRON)) {
      return process.env.METASPIRIT_ELECTRON
    }
    // 2) ~/.dsh/electron 目录（dsh-pet 下载的那份）下搜 electron(.exe)
    const dshElectron = path.join(os.homedir(), '.dsh', 'electron')
    if (fs.existsSync(dshElectron)) {
      const exeWin = path.join(dshElectron, 'electron.exe')
      const exeSh = path.join(dshElectron, 'electron')
      if (fs.existsSync(exeWin)) return exeWin
      if (fs.existsSync(exeSh)) return exeSh
    }
    // 3) PATH 中的 electron
    if (this.canRun('electron')) return 'electron'
    // 都没有：返回 null（由调用方降级为浏览器打开）
    return null
  }

  showOverlay(opts: { url: string; port: number }): {
    ok: boolean; electron_found: boolean; running?: boolean;
    url?: string; port?: number; fallback_url?: string; hint?: string
  } {
    const bin = this.overlayElectronBin()
    if (!bin) {
      // 未找到 Electron：HTTP 服务已起（调用方负责），降级为浏览器打开
      return {
        ok: false, electron_found: false,
        fallback_url: opts.url, port: opts.port,
        hint: '未找到 Electron，请用浏览器打开',
      }
    }
    if (this.overlayProc) this.hideOverlay()
    const helper = this.overlayHelperPath()
    this.overlayProc = spawn(bin, [helper], {
      env: { ...process.env, OVERLAY_URL: opts.url },
      stdio: 'inherit',
    })
    this.overlayProc.on('exit', () => { this.overlayProc = null })
    this.overlayProc.on('error', (err: Error) => {
      console.error('[metaspirit] 悬浮窗进程错误:', err.message)
    })
    return { ok: true, electron_found: true, running: true, url: opts.url, port: opts.port }
  }

  hideOverlay(): { running: boolean } {
    if (this.overlayProc) {
      try { this.overlayProc.kill() } catch { /* ignore */ }
      this.overlayProc = null
    }
    return { running: false }
  }
}
