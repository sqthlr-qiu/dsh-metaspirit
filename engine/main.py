# main.py —— 入口：装配各模块 + --smoke 冒烟测试 + Engine 本体（供 rpc_server 调用）
# 铁律落实：纯 API（provider 两档）、Mock 端到端可跑、异步队列、预算熔断、白名单强制。

import os
import sys
import json
import asyncio
import argparse

# 允许从任意目录以模块方式运行
sys.path.insert(0, os.path.dirname(__file__))

from config_loader import load_config
from ms_types import (WorldState, Action, StepRecord, worldstate_to_json)
from llm.provider import build_provider, BudgetMeter, MockProvider
from perception.capture import Capture
from perception.keyframe_sampler import KeyframeSampler
from perception.mllm_perceiver import MLLMPerceiver
from perception.grounding_calibrator import GroundingCalibrator
from cognition.attention import AttentionScheduler
from cognition.self_model import SelfModel
from cognition.working_memory import WorkingMemory
from memory.stores import MemoryStore
from memory.skill_library import SkillLibrary
from decision.goal_compiler import GoalCompiler
from decision.reward_compiler import RewardCompiler, static_audit
from decision.react_loop import ReActLoop
from decision.combat_reflex import CombatReflex
from decision.action_validator import ActionValidator
from decision.action_executor import ActionExecutor
from learning.reflector import Reflector
from learning.trajectory_recorder import TrajectoryRecorder


class Engine:
    """元灵引擎本体：装配 L1-L6，并暴露与 DSH 8 工具一一对应的方法。"""

    def __init__(self, cfg: dict, provider=None, budget: BudgetMeter | None = None):
        self.cfg = cfg
        self.budget = budget or BudgetMeter(cfg.get("budget", {}).get("daily_usd_cap", 10.0))
        self.provider = provider or build_provider(cfg, "fast", self.budget)
        # L1 感知
        # 与 provider/rpc_server 同口径解析：仅 "1/true/yes/on" 视为 mock，"0" 明确关闭
        _mock_raw = os.environ.get("METASPIRIT_MOCK", "").strip().lower()
        syn = _mock_raw in ("1", "true", "yes", "on") or bool(cfg.get("mock"))
        self.capture = Capture(synthetic=syn, window_title=cfg.get("window_title", ""))
        self.sampler = KeyframeSampler()
        self.perceiver = MLLMPerceiver(self.provider, cfg)
        self.calibrator = GroundingCalibrator(cfg.get("perception", {}).get("calibrator", "none"))
        # L2 认知
        self.scheduler = AttentionScheduler(cfg)
        self.self_model = SelfModel()
        self.working_memory = WorkingMemory()
        # L3 记忆
        self.memory = MemoryStore(use_vector=(cfg.get("memory", {}).get("vector_db") == "chromadb"))
        self.skill_lib = SkillLibrary()
        # L4 决策
        asp = cfg.get("_action_space", {})
        self.goal_compiler = GoalCompiler(self.provider, cfg)
        self.reward_compiler = RewardCompiler(self.provider, cfg)
        self.react = ReActLoop(self.provider, cfg)
        self.combat = CombatReflex()
        self.validator = ActionValidator(asp, cfg.get("decision", {}).get("max_same_action_repeat", 2))
        self.executor = ActionExecutor(dry_run=True, observe_delay=cfg.get("decision", {}).get("observe_delay", 0.8))
        # L6 学习
        self.reflector = Reflector(self.provider, cfg, self.memory)
        self.trajectory = TrajectoryRecorder()
        # 运行时
        self.latest_ws: WorldState | None = None
        self.session_id: str | None = None
        self.started_at: float = 0.0
        self.learning_queue: asyncio.Queue = asyncio.Queue()

    # —— 装配：开局 ——
    async def start(self, game: str = "minecraft", window_title: str = "",
                    background_text: str = "", action_space_path: str = "") -> dict:
        self.session_id = f"ms-{int(asyncio.get_event_loop().time())}"
        self.started_at = asyncio.get_event_loop().time()
        # 加载操作白名单
        if not action_space_path:
            action_space_path = os.path.join(os.path.dirname(__file__), "config", "action_space", "minecraft.yaml")
        asp = load_config(action_space_path)
        self.cfg["_action_space"] = asp
        self.validator = ActionValidator(asp, self.cfg.get("decision", {}).get("max_same_action_repeat", 2))
        # P4 目标编译
        await self.goal_compiler.compile_initial(background_text or "未知世界，先求生存。")
        # P5 奖励编译（首个当前目标）
        cg = self.goal_compiler.goal_tree.get("current_goals", []) or [{"goal": "维持生存"}]
        await self.reward_compiler.compile_reward(cg[0].get("goal", "维持生存"))
        return {"session_id": self.session_id,
                "goal_tree": self.goal_compiler.view()}

    # —— L1 感知（供 smoke 与实时调用）——
    async def sense(self, scene_override: str | None = None, tier: str = "fast") -> WorldState:
        """抓取→采样→感知→校准→注意力→自我模型→工作记忆→落盘。tier 指定感知档位。"""
        plan = self.scheduler.plan_for(_scene_of(scene_override))
        frames = self.capture.grab_batch(plan.frame_rate_capture, n=6)
        seg = self.sampler.build_segment(frames, plan, events=[])
        ws = await self.perceiver.perceive(seg, scene_override=scene_override, tier=tier)
        ws = self.calibrator.verify(ws)
        self.scheduler.update_from_worldstate(ws)
        self.self_model.update_from_worldstate(ws)
        self.working_memory.push(ws)
        self.trajectory.log_worldstate(ws)
        self.latest_ws = ws
        return ws

    async def perceive(self, force_deep: bool = False) -> dict:
        """DSH 工具 metaspirit_perceive：返回最新（或强制深档重感知）世界状态 + 所用 tier。
        返回结构在 worldstate 字段基础上附加 tier。"""
        if force_deep and not isinstance(self.provider, MockProvider):
            # 真实路径：用 deep 档重感知一次
            ws = await self.sense(tier="deep")
            tier = "deep"
        elif self.latest_ws is None:
            ws = await self.sense()
            tier = "fast"
        else:
            ws = self.latest_ws
            tier = "fast"
        out = worldstate_to_json(ws)
        out["tier"] = tier
        return out

    # —— L4 决策 + 执行 ——
    async def decide_and_act(self, ws: WorldState, force_reflex: bool = False,
                             force_mismatch_effect: str | None = None) -> dict:
        plan = self.scheduler.plan_for(ws.scene)
        reflex_latency_ms = None
        # 战斗反射路径（规则，不经 MLLM）
        if (force_reflex or plan.mllm_tier == "reflex") and ws.urgency > 0.6:
            import time as _t
            t0 = _t.perf_counter()
            act = self.combat.decide(ws, tracks=[])
            reflex_latency_ms = (_t.perf_counter() - t0) * 1000.0
            via = "combat_reflex"
        else:
            act = await self.react.step(
                ws, self.goal_compiler.view(), self.cfg.get("_action_space", {}),
                self.working_memory.recent_trajectory(), self.skill_lib.list_skills(), [])
            via = "react"
        # 可选：注入"预期不符"效果（smoke 用，以驱动反思路径）
        if force_mismatch_effect is not None:
            act.expected_effect = force_mismatch_effect
        # 白名单校验（违规改写 wait）
        act, ok, reason = self.validator.validate(act)
        # 执行（dry_run）
        res = await self.executor.execute(act)
        observed = res["observed_result"]
        matched = self.executor.matched_expectation(act.expected_effect, observed)
        # 轨迹落盘（目的/操作/帧段/反馈/奖励）
        purpose = act.expected_effect or via
        fs = worldstate_to_json(ws).get("frame_segment", {})
        sb = {"hp": ws.self_state.hp, "inventory": ws.self_state.inventory}
        sa = {"hp": ws.self_state.hp, "inventory": ws.self_state.inventory}
        reward = self.reward_compiler.reward_of(sb, act.type, sa)
        rec = StepRecord(t=asyncio.get_event_loop().time(), purpose=purpose,
                         action_json={"type": act.type, "detail": act.detail,
                                      "params": act.params, "expected_effect": act.expected_effect},
                         frame_segment=fs, observed_result=observed, reward=reward)
        self.trajectory.log_step(rec)
        self.working_memory.push_trajectory(purpose, rec.action_json, observed)
        return {"action": rec.action_json, "observed_result": observed,
                "matched": matched, "valid": ok, "reason": reason, "via": via,
                "reward": reward, "reflex_latency_ms": reflex_latency_ms}

    async def act(self, action_json: dict, expected_effect: str = "") -> dict:
        """DSH 工具 metaspirit_act：内部过 ActionValidator，违规返回拒绝。"""
        act = Action(type=action_json.get("type", "wait"),
                     detail=action_json.get("detail", "wait"),
                     params=action_json.get("params", {}) or {},
                     expected_effect=expected_effect or action_json.get("expected_effect", ""))
        act, ok, reason = self.validator.validate(act)
        if not ok:
            return {"rejected": True, "reason": reason, "action": {"type": act.type, "detail": act.detail}}
        res = await self.executor.execute(act)
        return {"rejected": False, "observed_result": res["observed_result"], "dry_run": res["dry_run"]}

    # —— L6 反思 ——
    async def reflect(self, situation: str, attempted: str, observed: str) -> dict:
        lesson = await self.reflector.reflect(situation, attempted, observed)
        return lesson

    # —— L3 检索 / 技能 / 目标 ——
    def memory_search(self, query: str, top_k: int = 5) -> list:
        return self.memory.search(query, top_k)

    def skills(self, mode: str = "list", skill_name: str = "", args: dict | None = None) -> dict:
        if mode == "list":
            return {"skills": self.skill_lib.list_skills()}
        if mode == "invoke":
            ok, res = self.skill_lib.invoke_skill(skill_name, **(args or {}))
            return {"name": skill_name, "ok": ok, "result": res}
        return {"error": "unknown_mode"}

    def goals(self, mode: str = "view") -> dict:
        if mode == "view":
            return self.goal_compiler.view()
        return {"error": "update 需走 P4 编译器，请调用 metaspirit_start/事件触发"}

    def stop(self) -> dict:
        elapsed = asyncio.get_event_loop().time() - self.started_at if self.started_at else 0.0
        return {
            "session_id": self.session_id,
            "survival_seconds": round(elapsed, 2),
            "skills_count": len(self.skill_lib.list_skills()),
            "cost": self.budget.status(),
            "trajectory": self.trajectory.stats(),
            "memory": self.memory.stats(),
        }


def _scene_of(name: str | None):
    from ms_types import SceneType
    if name is None:
        return SceneType.EXPLORE
    try:
        return SceneType(name)
    except ValueError:
        return SceneType.EXPLORE


# ============================ --smoke 冒烟测试 ============================
async def run_smoke(engine: Engine) -> int:
    """端到端冒烟：目标编译→感知3拍→决策（含1次战斗反射+1次预期不符反思）→dry_run执行→落盘→SMOKE PASS
    复用 decide_and_act（不再内联重复决策逻辑）；新增奖励沙盒 dunder 逃逸拦截断言。"""
    print("[smoke] 启动引擎（start：加载白名单 + P4 目标编译 + P5 奖励编译）...")
    start_res = await engine.start("minecraft", "", "夜晚将至，需在天黑前收集木材搭建庇护所。", "")
    goals = engine.goal_compiler.goals
    print(f"[smoke] 目标树：{len(goals)} 个目标；P5 奖励编译：", end="")
    cg = engine.goal_compiler.goal_tree.get("current_goals", []) or [{"goal": "维持生存"}]
    code, ok, err = await engine.reward_compiler.compile_reward(cg[0].get("goal", "维持生存"))
    print(f"{'OK' if ok else '降级零奖励('+err+')'}")

    scenes = ["explore", "combat", "explore"]   # 第2拍为战斗（触发反射）
    reflex_latencies = []
    reflect_triggered = False
    for i, sc in enumerate(scenes):
        print(f"\n[smoke] 感知拍 {i+1}/{len(scenes)} —— 场景={sc}")
        ws = await engine.sense(sc)
        engine.working_memory.push(ws)
        force_reflex = (i == 1)  # 第2拍强制走战斗反射
        mismatch = ("我飞上了天空（不可能发生）" if i == 2 else None)  # 第3拍制造预期不符
        res = await engine.decide_and_act(ws, force_reflex=force_reflex,
                                         force_mismatch_effect=mismatch)
        act = res["action"]
        print(f"  决策路径={res['via']} 动作={act['type']}/{act['detail']} "
              f"有效={res['valid']} 观察='{res['observed_result']}' "
              f"匹配={res['matched']} 奖励={res['reward']}")
        if res["reflex_latency_ms"] is not None:
            reflex_latencies.append(res["reflex_latency_ms"])
        # 预期不符 → 触发反思（异步队列语义：放入队列后处理）
        if not res["matched"]:
            reflect_triggered = True
            print("  ⚠ 预期不符 → 触发 P8 反思")
            lesson = await engine.reflector.reflect(
                situation=f"场景={sc}，目标推进", attempted=act["detail"],
                observed=res["observed_result"])
            print(f"  反思教训：{lesson.get('lesson','')}")

    # 断言冒烟关键项
    assert len(reflex_latencies) >= 1, "未触发战斗反射"
    assert reflex_latencies[0] < 50.0, f"战斗反射延迟 {reflex_latencies[0]}ms 超 50ms"
    assert reflect_triggered, "未触发预期不符反思"
    assert engine.trajectory.count >= 3, "轨迹落盘不足"
    # 新增：奖励沙盒必须拦截 dunder 逃逸（如 ().__class__.__subclasses__()）
    bad = "def reward(a, b, c):\n    return ().__class__.__subclasses__()\n"
    probs = static_audit(bad)
    assert any("forbidden:dunder" in p for p in probs), "奖励沙盒未拦截 dunder 逃逸"
    # 新增：validator time_fn 可注入（假时钟 +1s/次，确保冷却判定通过）
    fake_clock = {"t": 0.0}
    v2 = ActionValidator(engine.cfg.get("_action_space", {}), time_fn=lambda: fake_clock["t"])
    a_test = Action(type="keypress", detail="W", params={}, expected_effect="前进")
    _, ok_t1, _ = v2.validate(a_test)
    fake_clock["t"] += 1.0  # +1s：冷却(默认0.2s)应放行
    _, ok_t2, _ = v2.validate(a_test)
    assert ok_t1 and ok_t2, "validator time_fn 注入后冷却/重复校验异常"
    print("\n[smoke] 战斗反射延迟(ms):", [round(x, 3) for x in reflex_latencies])
    print("[smoke] 轨迹文件:", engine.trajectory.path)
    print("[smoke] 目标编译→感知3拍→决策(1反射+1反思)→dry_run执行→落盘 全部通过")
    print("SMOKE PASS")
    return 0


def main():
    parser = argparse.ArgumentParser(description="元灵 MetaSpirit 引擎")
    parser.add_argument("--smoke", action="store_true", help="运行端到端冒烟测试")
    parser.add_argument("--config", default="", help="agent.yaml 路径")
    args = parser.parse_args()

    # 默认配置路径
    here = os.path.dirname(os.path.abspath(__file__))
    cfg_path = args.config or os.path.join(here, "config", "agent.yaml")
    cfg = load_config(cfg_path)

    # 冒烟或 Mock 强制 mock provider
    if args.smoke:
        os.environ["METASPIRIT_MOCK"] = "1"
        cfg["mock"] = True

    budget = BudgetMeter(cfg.get("budget", {}).get("daily_usd_cap", 10.0))
    provider = build_provider(cfg, "fast", budget)
    engine = Engine(cfg, provider, budget)

    if args.smoke:
        rc = asyncio.run(run_smoke(engine))
        sys.exit(rc)

    # 非 smoke：启动实时主循环（DSH 插件通常通过 rpc_server 驱动；此处提供最小实时循环）
    print("引擎已就绪（实时主循环）。通常通过 rpc_server.py 经 JSON-RPC 驱动。")


if __name__ == "__main__":
    main()
