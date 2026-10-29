# dsh-metaspirit —— 元灵 MetaSpirit DSH 插件

游戏自生存智能体「元灵 MetaSpirit」的 DeepSeek Harness（DSH）工具插件壳 + 随包分发的 Python 引擎本体。

- **验证版本**：DSH `0.2.1-rc.2`（开发者预览期，API 可能变动；本插件按此版本规格编写）。
- **Node 要求**：`node >= 24`。
- **Python 引擎**：纯标准库即可运行（Python 3.11+），真实 API 调用为 OpenAI 兼容端点（默认 DeepSeek-V4 系列）。

## 架构

| 部分 | 技术 | 职责 |
|---|---|---|
| `dsh-metaspirit`（本目录） | TypeScript | 注册 8 个 `metaspirit_*` 工具；管理 Python 引擎子进程（spawn / JSON-RPC / 健康检查 / 崩溃重启） |
| `engine/`（Python 本体） | Python 3.11+ | 抓屏、帧采样、MLLM 感知、记忆、键鼠执行、轨迹落盘；以 JSON-RPC over stdio 与插件通信 |

## 8 个工具（对应引擎 JSON-RPC 方法）

`metaspirit_start` / `metaspirit_perceive` / `metaspirit_act` / `metaspirit_reflect` /
`metaspirit_memory_search` / `metaspirit_skills` / `metaspirit_goals` / `metaspirit_stop`

## 安装

```bash
# 方式 A：从 GitHub 仓库安装（推荐，lib/ 已随库分发，无需本地构建）
dsh plugin --profile web add https://github.com/<OWNER>/dsh-metaspirit.git

# 方式 B：本地安装插件（开发态）
dsh plugin --profile web add file:/abs/path/dsh-metaspirit

# 2) 复制元灵预设（未挂预设时工具不可见，防止误用）
cp -R preset ~/.dsh/.agent-presets/metaspirit

# 3) 验证已挂载
dsh --profile web --dump-config | grep metaspirit
```

> `package.json` 仅保留 `build` 脚本（`pnpm build` 可用 tsdown 从 src 重新生成 lib）；不再注册 `prepare`，避免从 git 安装时触发远程构建。`lib/` 为与 `src/` 同步维护的等价 JS 产物，直接可用。

> **requirePreset（默认 false）**：插件 `apply(ctx, config)` 接收 `config`。当 `config.requirePreset=true` 时，仅在元灵预设（preset）已挂载后才注册 8 个工具，避免误用；默认 `false`（挂载即可用，便于无预设环境直接体验）。该配置写在 `cordis.patch.yml` / `cordis.yml` 的 `config` 段。

## Python 解释器（engine_client）

插件通过 `engine_client.ts` 启动 Python 引擎子进程。解释器探测顺序：

1. 环境变量 `METASPIRIT_PYTHON`（显式指向解释器绝对路径，跨平台最可靠）；
2. 依次探测 `python` → `python3` → `py`（Windows 上 `py` 启动器覆盖各版本）；
3. 若都不可用，控制台给出明确提示，并回退到 `METASPIRIT_PYTHON`（可能为空，由 spawn 报错暴露）。

如需固定版本，请设置 `METASPIRIT_PYTHON`，例如 `METASPIRIT_PYTHON=/usr/bin/python3.11`。

## 随包引擎与同步（engine/）

`engine/`（Python 本体）**随插件包分发**，位于 `dsh-plugin/engine/`，由 `package.json` 的 `files` 字段包含，运行时 `enginePath` 默认 `"./engine"`。

> **同步策略**：修改 `engine/` 源码后，需把更新同步进 `dsh-plugin/engine/`（仅拷贝 `.py/.yaml/.txt/.md` 等源码，排除 `__pycache__`、`frames_cache`、`trajectory` 等运行时产物）。保持两者一致，否则运行时加载的是旧代码。

## Python 引擎配置

引擎配置位于 `engine/config/agent.yaml`。关键项：

- `mllm.fast/deep`：`provider` 固定 `openai`（OpenAI 兼容），`model` 默认 `deepseek-v4-flash` / `deepseek-v4`；`base_url_env` / `api_key_env` 指向环境变量（`DEEPSEEK_BASE_URL` / `DEEPSEEK_API_KEY`）。
- `budget.daily_usd_cap`：日预算熔断（超限自动降级为仅 fast + 降频）。

## Mock 模式（无需 API Key 即可体验）

设置环境变量 `METASPIRIT_MOCK=1`（或在 `agent.yaml` 设 `mock: true`），引擎走 `MockProvider`：
- 返回合规的五路语义 / 决策 / 目标 / 奖励 / 反思 JSON；
- 构造"敌人接近 → 反射/攻击 → 反馈"的小剧本；
- 所有执行走 `dry_run`（打印 + 模拟反馈，不真正按键）。

```bash
# 引擎端到端冒烟（无需 Key）
cd engine
python main.py --smoke        # 末行打印 SMOKE PASS，退出码 0

# 引擎 JSON-RPC 自检
python rpc_server.py --selftest

# 手动喂 ping（stdin 一行一个 JSON-RPC 请求）
echo '{"jsonrpc":"2.0","method":"ping","params":{},"id":1}' | python rpc_server.py
```

## 已知限制

- 真实 MLLM 视觉感知需自备 DeepSeek（或 OpenAI 兼容）多模态 API Key；无 Key 时只能跑 Mock。
- `pydirectinput` / `dxcam` / `chromadb` / `paddleocr` 等均为可选依赖，缺失时自动降级（dry_run / 合成帧 / JSON 记忆 / 无校准）。
- DSH 处于预览期，工具 schema 与 `cordis.patch.yml` 格式以验证版本 `0.2.1-rc.2` 为准。
- TypeScript 插件未在本仓库本地编译（需 `pnpm i && pnpm build`）；语法按 DSH 规范编写。
