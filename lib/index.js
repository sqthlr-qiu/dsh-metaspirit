// index.js —— DSH 工具插件入口（免编译产物）
// （由 src/index.ts 等价转写：无构建环境时的产物，逻辑须与 TS 源同步维护）
// 形态：name='metaspirit'、inject=['tools']、apply(ctx, config) 用 defineTool 注册 8 个工具。
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
