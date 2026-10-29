// tsdown.config.ts —— DSH 插件构建配置
// 入口 src/index.ts → 产物 lib/index.js（ESM）；永不打包 @deepseek-ai/*（由 DSH 运行时提供）。
import { defineConfig } from 'tsdown'

export default defineConfig({
  entry: ['src/index.ts'],
  outDir: 'lib',
  format: ['esm'],
  target: 'node24',
  // 外部依赖：cordis / dsh-tools 由宿主（DSH）提供，禁止打进产物
  external: ['@deepseek-ai/cordis', '@deepseek-ai/dsh-tools'],
  clean: true,
})
