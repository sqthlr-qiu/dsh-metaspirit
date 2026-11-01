// index.js —— DSH 工具插件入口（免编译产物）
// （由 src/index.ts 等价转写：无构建环境时的产物，逻辑须与 TS 源同步维护）
// 形态：name='metaspirit'、inject=['tools']、apply(ctx, config) 用 defineTool 注册 11 个工具。
// v1.4.1：新增 metaspirit_plan（时间轴流水线单拍决策）——核心决策机制在 DSH 形态可达。
// v1.4.1-r3：新增 metaspirit_pipeline（流水线主循环 start/stop/status）。
import { defineTool } from '@deepseek-ai/dsh-tools'
import { EngineClient } from './engine_client.js'

export const name = 'metaspirit'
export const inject = ['tools']

const TOOLS = [
  {
    name: 'metaspirit_start',
    description: '拉起元灵引擎：P4 目标编译 + P5 奖励编译，返回 session_id 与目标树。',
    parameters: {
      game: { type: 'string', description: '游戏类型，如 minecraft / generic_window' },
      window_title: { type: 'string', description: '游戏窗口标题（用于捕获定位）' },
      background_text: { type: 'string', description: '游戏背景介绍文本（目标与奖励编译依据）' },
      action_space_path: { type: 'string', description: '操作白名单 yaml 路径（可选）' },
    },
    execute: (e, a) => e.rpc('start', {
      game: a.game, window_title: a.window_title,
      background_text: a.background_text, action_space_path: a.action_space_path,
    }),
  },
  {
    name: 'metaspirit_perceive',
    description: '获取元灵最新感知的世界状态（五路语义+事件转变+置信度）。',
    parameters: {
      force_deep: { type: 'boolean', description: '用旗舰档重感知一次' },
    },
    execute: (e, a) => e.rpc('perceive', { force_deep: a.force_deep }),
  },
  {
    name: 'metaspirit_act',
    description: '执行一个原子操作（内部过 ActionValidator 白名单，违规返回拒绝）。',
    parameters: {
      action: { type: 'object', additionalProperties: true, description: 'Action JSON：{type, detail, params, expected_effect}' },
      expected_effect: { type: 'string', description: '预期效果（用于操作后对照验证）' },
    },
    execute: (e, a) => e.rpc('act', { action: a.action, expected_effect: a.expected_effect }),
  },
  {
    name: 'metaspirit_plan',
    description: 'v1.4.1 时间轴流水线单拍决策：感知 → P13 规划一条「时刻→预期→操作」短时时间轴（1-12 项，游戏无关意图，自动绑定按键并定时执行）。规划不等上一条执行完；若上一条未完成则取代（supersede）并附改进意见；返回七段结构化决策日志（现状/是否改变/上一步完成情况/目标达成度与反思+反思落实反馈/下一步目标/操作概述/时间轴）。持续自动运行请改用引擎 --overlay 模式或多次调用本工具。',
    parameters: {
      user_message: { type: 'string', description: '玩家实时指令（最高优先级，不越过生存底线）' },
      fresh: { type: 'boolean', description: '强制重新感知后再规划（默认用最近一次世界状态）' },
    },
    execute: (e, a) => e.rpc('plan', {
      user_message: a.user_message || '', fresh: !!a.fresh,
    }),
  },
  {
    name: 'metaspirit_pipeline',
    description: 'v1.4.1-r3 时间轴流水线主循环控制：start 让引擎自主持续「感知→规划→执行」（不等 Agent 逐拍调用）/ stop 停止并返回会话统计 / status 查询运行状态与流水线指标。start 需先 metaspirit_start。',
    parameters: {
      action: { type: 'string', description: 'start | stop | status' },
      n_cycles: { type: 'number', description: 'start 时的拍数（缺省=持续运行直到 stop）' },
      plan_interval: { type: 'number', description: 'start 时每拍规划间隔秒（缺省用配置）' },
    },
    execute: async (e, a) => {
      const action = (a.action || 'status').toLowerCase()
      if (action === 'start') {
        return e.rpc('pipeline_start', {
          n_cycles: a.n_cycles ?? null, plan_interval: a.plan_interval ?? null,
        })
      }
      if (action === 'stop') return e.rpc('pipeline_stop', {})
      return e.rpc('pipeline_status', {})
    },
  },
  {
    name: 'metaspirit_reflect',
    description: 'P8 反思：把一次"预期不符"的经历复盘为可复用教训并写入记忆库。',
    parameters: {
      situation: { type: 'string', description: '当时情境描述' },
      attempted: { type: 'string', description: '尝试了什么操作' },
      observed: { type: 'string', description: '实际观察到的反馈' },
    },
    execute: (e, a) => e.rpc('reflect', {
      situation: a.situation, attempted: a.attempted, observed: a.observed,
    }),
  },
  {
    name: 'metaspirit_memory_search',
    description: 'P9 数据检索：按 query 检索情景/语义记忆（含关键帧引用）。',
    parameters: {
      query: { type: 'string', description: '检索查询' },
      top_k: { type: 'number', description: '返回条数（默认 5）' },
    },
    execute: (e, a) => e.rpc('memory_search', { query: a.query, top_k: a.top_k }),
  },
  {
    name: 'metaspirit_skills',
    description: 'Voyager 技能库：list 列出技能菜单 / invoke 调用某个技能。',
    parameters: {
      mode: { type: 'string', description: 'list | invoke' },
      skill_name: { type: 'string', description: 'invoke 时的技能名' },
      args: { type: 'object', additionalProperties: true, description: 'invoke 时的参数' },
    },
    execute: (e, a) => e.rpc('skills', { mode: a.mode, skill_name: a.skill_name, args: a.args }),
  },
  {
    name: 'metaspirit_goals',
    description: '查看/更新目标树（更新走 P4 编译器防编造）。',
    parameters: {
      mode: { type: 'string', description: 'view | update' },
    },
    execute: (e, a) => e.rpc('goals', { mode: a.mode }),
  },
  {
    name: 'metaspirit_stop',
    description: '停止引擎，返回本会话统计（生存时长/技能数/成本）。',
    parameters: {},
    execute: (e, a) => e.rpc('stop', {}),
  },
  {
    name: 'metaspirit_overlay',
    description: '桌面悬浮窗：show 拉起透明窗（Electron）并启动后台 HTTP 服务 / hide 关闭 / status 查询状态。',
    parameters: {
      action: { type: 'string', description: 'show | hide | status' },
    },
    execute: async (e, a) => {
      const action = (a.action || 'status').toLowerCase()
      if (action === 'status') {
        const st = await e.rpc('overlay', { action: 'status' })
        return { ...st, electron_found: e.overlayElectronBin() !== null }
      }
      if (action === 'hide') {
        await e.rpc('overlay', { action: 'hide' })
        e.hideOverlay()
        return { running: false, electron_found: e.overlayElectronBin() !== null }
      }
      const st = await e.rpc('overlay', { action: 'show' })
      const r = e.showOverlay({ url: st.url, port: st.port })
      return { ...r, ...st }
    },
  },
]

export function apply(ctx, config = {}) {
  const cfg = { requirePreset: false, ...config }
  const engine = new EngineClient(ctx, cfg)
  // requirePreset=true 时仅在元灵预设已挂载（ctx 标记）后才注册工具，避免误用；
  // 默认 false：挂载即注册，便于无预设环境下直接体验。
  if (cfg.requirePreset && !ctx.metaSpiritPresetActive) {
    ctx.logger?.warn?.('[metaspirit] requirePreset=true 但元灵预设未挂载，跳过工具注册')
    return
  }
  for (const t of TOOLS) {
    ctx.tools.register(defineTool({
      name: t.name,
      description: t.description,
      parameters: t.parameters,
      output: {
        schema: { type: 'object', additionalProperties: true },
        render: (_args, v) => [{ type: 'text', text: JSON.stringify(v, null, 2) }],
      },
      async execute(args) {
        return t.execute(engine, args || {})
      },
    }))
  }
}
