# main.py —— 入口：装配各模块 + --smoke 冒烟测试 + Engine 本体（供 rpc_server 调用）
# 铁律落实：纯 API（provider 两档）、Mock 端到端可跑、异步队列、预算熔断、白名单强制。

import os
import sys
import json
import time
import asyncio
import argparse
import threading
import urllib.request
import urllib.error
import concurrent.futures

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
from decision.timeline import (TimelineScheduler, ActionTimeline, TimelineAction,
                               ReflectionLedger, plan_timeline, normalize_timeline,
                               format_decision_block, ta_desc)
from decision.keymap import KeymapBinder, load_keymap
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
        self.combat = CombatReflex(
            center=tuple(cfg.get("decision", {}).get("window_center", (320, 240))))
        self.validator = ActionValidator(asp, cfg.get("decision", {}).get("max_same_action_repeat", 2))
        # 真实链路：dry_run 由配置决定（默认 True），observer 注入真实帧差观察
        self.executor = ActionExecutor(
            dry_run=bool(cfg.get("decision", {}).get("dry_run", True)),
            observe_delay=cfg.get("decision", {}).get("observe_delay", 0.8),
            observer=lambda: self.capture.snapshot())
        # L4.5 时间轴流水线（v1.4）：规划不等执行 + supersede + 七段决策日志
        # L4.5 时间轴流水线（v1.4.1：意图键位映射 + drift 动态重锚 + 反思落实台账 + 每步反馈回流）
        self.binder = KeymapBinder(load_keymap(os.path.dirname(__file__),
                                               cfg.get("game", "minecraft")))
        self.timeline = TimelineScheduler(self.binder, self.executor,
                                          on_action_feedback=self._on_timeline_action)
        self.reflection_ledger = ReflectionLedger()   # 反思必须有反馈：登记→P13 评估→回填
        self.decision_cycle = 0
        self.overlap_cycles = 0             # "上一条时间轴未完成时下一条计算已开始"的次数（流水线证据）
        self.last_blocks: list = []         # 最近一轮 pipeline 的决策日志块
        _logs_dir = os.path.join(os.path.dirname(__file__), "logs")
        os.makedirs(_logs_dir, exist_ok=True)
        self.decision_log_path = os.path.join(_logs_dir, "decision_log.txt")
        self.decision_log_jsonl = os.path.join(_logs_dir, "decision_log.jsonl")
        # L6 学习
        self.reflector = Reflector(self.provider, cfg, self.memory)
        self.trajectory = TrajectoryRecorder()
        # 运行时
        self.latest_ws: WorldState | None = None
        self.session_id: str | None = None
        self.started_at: float = 0.0
        self.learning_queue: asyncio.Queue = asyncio.Queue()
        # Overlay（悬浮窗）相关运行时
        self._overlay = None                  # OverlayServer 实例（后台线程托管）
        self.overlay_host: str = "127.0.0.1"
        self.overlay_port: int = 8765
        self._loop = None                     # 事件循环（user_say_sync 跨线程调度用）
        self.latest_thought: str = ""
        self.latest_action: str = ""
        self.events_log: list = []            # 最近事件（对话/决策/感知摘要）
        self._stop_requested: bool = False
        self.pending_user_directive: str = ""  # 玩家指令（悬浮窗/chat 写入，plan_once 消费注入 P13）
        self._skill_seq_count: dict = {}       # P10 技能沉淀：时间轴成功意图序列出现次数
        self._p1_identity: str = self._load_identity()

    def _load_identity(self) -> str:
        """P1 身份提示词（no_go 生存红线）注入所有 MLLM system 段。"""
        try:
            p = os.path.join(os.path.dirname(__file__), "prompts", "p1_identity.txt")
            with open(p, "r", encoding="utf-8") as f:
                return f.read().strip()
        except Exception:
            return ""

    def _consume_user_directive(self) -> str:
        d, self.pending_user_directive = self.pending_user_directive, ""
        return d

    # —— P10 技能沉淀（Voyager 范式）：时间轴成功序列出现 ≥2 次 → 沉淀为技能 ——
    def _maybe_save_skill(self, tl) -> str:
        try:
            if tl.aborted or not tl.actions:
                return ""
            if any(e.get("state") != "ok" for e in tl.exec.values()):
                return ""
            sig = tuple(ta.intent for ta in tl.actions)
            self._skill_seq_count[sig] = self._skill_seq_count.get(sig, 0) + 1
            if self._skill_seq_count[sig] < 2:
                return ""
            name = "skill_" + "_".join(sig)[:40].replace("/", "") + f"_{len(sig)}"
            if any(s["name"] == name for s in self.skill_lib.list_skills()):
                return ""
            steps = "; ".join(
                f"{i+1}. {ta.at_s:.2f}s {ta.intent}({ta.expected})"
                for i, ta in enumerate(tl.actions))
            code = (f"# 由时间轴成功序列自动沉淀（P10，出现 {self._skill_seq_count[sig]} 次）\n"
                    f"def {name}(env=None, **params):\n"
                    f"    \"\"\"触发场景: {tl.goal}。复刻该意图序列的节奏与参数。\"\"\"\n"
                    f"    return {{\"intents\": {list(sig)!r}, \"goal\": {tl.goal!r},\n"
                    f"            \"steps\": {steps!r}, \"horizon_s\": {tl.horizon_s}}}\n")
            self.skill_lib.save_skill(name, code,
                                      trigger=f"{tl.goal}（{len(sig)}步意图序列）",
                                      meta={"from": "timeline_v1.4.1",
                                            "intents": list(sig)})
            self.events_log.append({"t": time.time(), "type": "skill_saved",
                                    "text": f"沉淀技能 {name}（成功序列 ×{self._skill_seq_count[sig]}）"})
            return name
        except Exception:
            return ""

    # —— 装配：开局 ——
    async def start(self, game: str = "minecraft", window_title: str = "",
                    background_text: str = "", action_space_path: str = "") -> dict:
        self.session_id = (f"ms-{int(asyncio.get_event_loop().time()*1000) % 10**10}"
                           f"-{os.urandom(2).hex()}")  # time+随机，避免 loop.time 重复
        self.started_at = asyncio.get_event_loop().time()
        # 加载操作白名单
        if not action_space_path:
            action_space_path = os.path.join(os.path.dirname(__file__), "config", "action_space", "minecraft.yaml")
        asp = load_config(action_space_path)
        self.cfg["_action_space"] = asp
        self.validator = ActionValidator(asp, self.cfg.get("decision", {}).get("max_same_action_repeat", 2))
        # v1.4.1：重挂时间轴调度器（新白名单校验器 + 当前游戏键位绑定 + 每步反馈回流）
        self.binder = KeymapBinder(load_keymap(os.path.dirname(__file__), game))
        self.timeline = TimelineScheduler(self.binder, self.executor,
                                          validator=self.validator.validate,
                                          on_action_feedback=self._on_timeline_action)
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
        # r3：抓帧（含逐帧 sleep）移入线程池，避免阻塞常驻事件循环、挤压时间轴定时器
        frames = await asyncio.to_thread(self.capture.grab_batch, plan.frame_rate_capture, 6)
        seg = self.sampler.build_segment(frames, plan, events=[])
        ws = await self.perceiver.perceive(seg, scene_override=scene_override, tier=tier)
        ws = self.calibrator.verify(ws)
        self.scheduler.update_from_worldstate(ws)
        self.self_model.update_from_worldstate(ws)
        self.working_memory.push(ws)
        self.trajectory.log_worldstate(ws)
        self.latest_ws = ws
        self.events_log.append({
            "t": ws.timestamp, "type": "perceive",
            "text": f"场景={ws.scene.value} 紧迫度={ws.urgency:.2f} 实体数={len(ws.entities)}",
        })
        if len(self.events_log) > 200:
            self.events_log = self.events_log[-200:]
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
            # F9 接回：决策前检索记忆摘要注入 P6（查询=当前目标）
            digest = self.memory.search(str(self.goal_compiler.view())[:80], 5)
            act = await self.react.step(
                ws, self.goal_compiler.view(), self.cfg.get("_action_space", {}),
                self.working_memory.recent_trajectory(), self.skill_lib.list_skills(),
                digest)
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
        self.events_log.append({
            "t": asyncio.get_event_loop().time(), "type": "decide",
            "text": f"{via}: {act.type}/{act.detail} 预期={act.expected_effect}",
        })
        if len(self.events_log) > 200:
            self.events_log = self.events_log[-200:]
        return {"action": rec.action_json, "observed_result": observed,
                "matched": matched, "valid": ok, "reason": reason, "via": via,
                "reward": reward, "reflex_latency_ms": reflex_latency_ms}

    # —— v1.4.1：时间轴流水线决策（时刻→预期→操作 意图时间轴 + 每步反馈回流 + drift 动态重锚
    #              + supersede 改进意见 + 反思落实反馈 + 七段日志）——
    def _on_timeline_action(self, tl, idx, ta, rec: dict):
        """每步执行+反馈回调（TimelineScheduler 注入）：落轨迹 StepRecord + 工作记忆，
        保证"每一步执行加反馈都连接到大模型能继续深度学习"（trajectory→反思/技能/蒸馏数据）。"""
        try:
            ws = self.latest_ws
            state = {"hp": ws.self_state.hp if ws else 1.0,
                     "inventory": (ws.self_state.inventory if ws else []) or []}
            reward = self.reward_compiler.reward_of(state, ta.intent, state)
            rec_step = StepRecord(
                t=time.time(), purpose=ta.expected or ta.intent,
                action_json={"intent": ta.intent, "params": ta.params,
                             "bound": rec.get("bound", ""), "at_s": ta.at_s,
                             "drift": round(rec.get("drift", 0.0), 3),
                             "state": rec.get("state")},
                frame_segment={}, observed_result=rec.get("observed", rec.get("reason", "")),
                reward=reward)
            self.trajectory.log_step(rec_step)
            self.working_memory.push_trajectory(
                ta.expected or ta.intent, rec_step.action_json, rec_step.observed_result)
            # P10 技能沉淀：整条时间轴全部动作 ok（最后一步回调时检查）→ 成功序列计数
            if idx == len(tl.actions) - 1:
                self._maybe_save_skill(tl)
        except Exception:
            pass

    async def plan_once(self, ws: WorldState, user_message: str = "") -> dict:
        """时间轴流水线的一拍：产出七段结构化决策日志并提交新时间轴。
        关键语义：本调用在上一条时间轴执行期间发起（规划与执行流水线并行）；
        新时间轴提交时若旧时间轴仍有未执行动作，则取代之并附改进意见（改进意见进反思台账）；
        每步执行反馈与待评估反思一起送入 P13——学习闭环。"""
        loop = asyncio.get_event_loop()
        self.decision_cycle += 1
        plan = self.scheduler.plan_for(ws.scene)
        prev_text = self.timeline.prev_status_text()
        prev_pending = self.timeline.pending_desc()
        prev_id = self.timeline.current.id if self.timeline.current else ""
        if prev_pending:
            self.overlap_cycles += 1  # 流水线重叠证据：上一条未完成，本拍计算已开始
        # C：每步执行反馈（预期 vs 实际 + 绑定 + drift）+ D：待落实反思 → 都进大模型输入
        feedbacks = self.timeline.action_feedbacks()
        pend_refl = [{"id": it.id, "text": it.text, "source": it.source}
                     for it in self.reflection_ledger.pending()]
        last_drift = self.timeline.last_drift
        # F9 接回：记忆检索结果注入决策（查询=当前目标+场景关键词）
        _gv = self.goal_compiler.view()
        _scene_txt = getattr(ws.scene, "value", str(ws.scene))
        _mem_q = (" ".join(str(g.get("goal", "")) for g in (_gv.get("current_goals") or []))
                  + f" {_scene_txt}")
        memory_digest = self.memory.search(_mem_q[:80], 5)
        if not user_message:
            user_message = self._consume_user_directive()  # F15 接回：玩家指令透传进 P13
        # r3 接回（F3 目标树事件驱动更新）：重大事件触发 P4 增量重编译
        # （紧迫威胁 / HP 骤降 / 玩家指令 / 每 5 拍例行回顾）
        try:
            _hp = ws.self_state.hp if ws.self_state else 1.0
            _evt = (f"第{self.decision_cycle}拍 场景={getattr(ws.scene, 'value', ws.scene)} "
                    f"紧迫度={ws.urgency:.2f} HP={_hp:.2f} 玩家指令={user_message or '无'}")
            if (ws.urgency > 0.6 or _hp < 0.35 or user_message
                    or self.decision_cycle % 5 == 0):
                await self.goal_compiler.update(_evt)
        except Exception:
            pass
        if plan.mllm_tier == "reflex" and ws.urgency > 0.6:
            # 战斗反射抢占：不经 MLLM，直接生成单动作应急时间轴并取代旧时间轴
            # 动作用 __direct__ 直通意图（反射产出具体 Action，仍过白名单校验）
            import time as _t
            t0 = _t.perf_counter()
            act = self.combat.decide(ws, tracks=[])
            reflex_latency_ms = (_t.perf_counter() - t0) * 1000.0
            via = "combat_reflex"
            tl = ActionTimeline(goal="应急闪避/威胁响应", horizon_s=0.6,
                                actions=[TimelineAction(at_s=0.0,
                                    intent="__direct__",
                                    params={"type": act.type, "detail": act.detail},
                                    expected=act.expected_effect)],
                                via="combat_reflex",
                                improvements=[f"战斗反射抢占：放弃剩余时间轴，先执行 {act.detail}"])
            change_prev = "supersede"
            situation = f"紧迫度 {ws.urgency:.2f} 超过反射阈值，走 combat_reflex 快路径（不经 MLLM）"
            goal_progress = "战斗突发情况，无 MLLM 达成度评估"
            reflection = "反射路径只处理即时威胁；下一拍 MLLM 将重新感知并重算时间轴"
            next_goal = "脱离/消除眼前威胁"
            ops_plan = f"立即执行 {act.detail}"
            horizon = tl.horizon_s
            rf_raw = []
        else:
            via = "react"
            reflex_latency_ms = None
            # 白名单摘要 = 当前游戏键位绑定 + 动作空间（P13 只允许从 intents 里选）
            asp = dict(self.cfg.get("_action_space", {}) or {})
            asp["intents"] = sorted(self.binder.intents.keys())
            data = await plan_timeline(
                self.provider, ws, self.goal_compiler.view(),
                asp, prev_text, prev_pending,
                prev_id, feedbacks, pend_refl, last_drift,
                user_message=user_message, identity=self._p1_identity,
                memory_digest=memory_digest)
            tl, change_prev, improvements, rf_raw = normalize_timeline(data)
            situation = str(data.get("situation_analysis", ""))
            goal_progress = str(data.get("goal_progress", ""))
            reflection = str(data.get("reflection", ""))
            next_goal = str(data.get("next_goal", ""))
            ops_plan = str(data.get("ops_plan", ""))
            horizon = tl.horizon_s
        tl.supersede_of = prev_id if change_prev == "supersede" else ""
        sub = await self.timeline.submit(tl, change_prev=change_prev)
        # D：supersede 改进意见登记进反思台账（后续拍强制评估落实情况）
        if sub["superseded"]:
            for imp in tl.improvements:
                self.reflection_ledger.add(imp, "supersede_improvement", self.decision_cycle)
        # D：回填 P13 给出的反思落实反馈（反思有反馈）
        rf_view = []
        if via == "react":
            for fb in rf_raw:
                if self.reflection_ledger.apply_feedback(fb):
                    it = next((x for x in self.reflection_ledger.items
                               if x.id == str(fb.get("id", ""))), None)
                    if it:
                        rf_view.append({"id": it.id, "status": it.status,
                                        "feedback": it.feedback})
        # F9/F3 接回：每拍落一条情景记忆（事件驱动：决策周期作为情景事件）
        try:
            self.memory.add_episodic({
                "kind": "decision_cycle", "cycle": self.decision_cycle,
                "scene": getattr(ws.scene, "value", str(ws.scene)),
                "urgency": round(ws.urgency, 2), "timeline": tl.id,
                "goal": tl.goal, "via": via, "superseded": bool(sub["superseded"]),
                "aborted": sub["aborted"], "improvements": list(tl.improvements)})
        except Exception:
            pass
        if sub["superseded"]:
            changed = (f"是 → 取代 {sub['superseded']}，放弃未执行的 {len(sub['aborted'])} 项"
                       f"（{'、'.join(sub['aborted'][:4])}{'…' if len(sub['aborted']) > 4 else ''}）")
        else:
            changed = "否 → 上一条时间轴剩余未完成动作保留，继续按（含 drift 修正的）原时刻执行"
        t_now = loop.time() - self.started_at if self.started_at else 0.0
        block = {
            "cycle": self.decision_cycle, "t": round(t_now, 3), "via": via,
            "tl_id": tl.id, "situation": situation, "changed": changed,
            "prev_status": prev_text, "goal_progress": goal_progress,
            "reflection": reflection, "next_goal": next_goal, "ops_plan": ops_plan,
            "horizon": horizon,
            "timeline": [{"at_s": ta.at_s, "expected": ta.expected,
                          "intent": ta.intent, "desc": ta_desc(ta, self.binder)}
                         for ta in tl.actions],
            "improvements": list(tl.improvements),
            "reflection_feedback": rf_view,
            "time_drift_hint": round(last_drift, 3),
            "reflex_latency_ms": reflex_latency_ms,
            "superseded": sub["superseded"], "aborted": sub["aborted"],
        }
        text = format_decision_block(block)
        print(text, file=sys.stderr)  # stdout 是 JSON-RPC 通道，人读日志走 stderr
        with open(self.decision_log_path, "a", encoding="utf-8") as f:
            f.write(text + "\n\n")
        with open(self.decision_log_jsonl, "a", encoding="utf-8") as f:
            f.write(json.dumps(block, ensure_ascii=False) + "\n")
        self.events_log.append({"t": time.time(), "type": "timeline",
                                "text": f"#{self.decision_cycle} {tl.id}({via}) h={horizon:.2f}s "
                                        f"superseded={bool(sub['superseded'])}"})
        if len(self.events_log) > 200:
            self.events_log = self.events_log[-200:]
        self.latest_thought = situation[:80]
        self.latest_action = f"{tl.id}:{next_goal[:40]}"
        return block

    async def run_pipeline(self, n_cycles: int | None = None, scenes: list | None = None,
                           plan_interval: float | None = None, user_message: str = "") -> dict:
        """时间轴流水线主循环：感知→规划（不等上一条时间轴执行完）→提交（未完成则取代+改进意见）→重复。
        规划间隔 plan_interval 模拟真实 MLLM 计算耗时；动作执行由调度器按相对时刻独立触发。"""
        iv = plan_interval or float(self.cfg.get("decision", {}).get("pipeline_interval", 1.5))
        i = 0
        blocks = []
        while n_cycles is None or i < n_cycles:
            if self._stop_requested:
                break
            sc = scenes[i] if scenes and i < len(scenes) else None
            ws = await self.sense(sc)
            blocks.append(await self.plan_once(ws, user_message=user_message))
            i += 1
            if n_cycles is None or i < n_cycles:
                await asyncio.sleep(iv)
        self.last_blocks = blocks
        return {"cycles": i, "overlap_cycles": self.overlap_cycles,
                "timelines": len(self.timeline.history) + (1 if self.timeline.current else 0),
                "supersedes": len(self.timeline.supersede_events),
                "blocks": blocks}

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
        self._stop_requested = True
        return {
            "session_id": self.session_id,
            "survival_seconds": round(elapsed, 2),
            "skills_count": len(self.skill_lib.list_skills()),
            "cost": self.budget.status(),
            "trajectory": self.trajectory.stats(),
            "memory": self.memory.stats(),
        }

    # —— 悬浮窗对话：用户消息作为指令进入决策 ——
    async def user_say(self, text: str) -> dict:
        """处理用户一句话：写入工作记忆(标记 user_chat) + 目标编译器(user_directive)，
        并用带"玩家指令"上下文的决策生成一次回复（thought+action 说明）。"""
        text = (text or "").strip()
        self.working_memory.push_user_chat(text)
        self.goal_compiler.add_user_directive(text)
        self.pending_user_directive = text  # 注入下一拍 P13 时间轴（F15 接回）
        self.events_log.append({"t": time.time(), "type": "chat_in", "text": text})
        if isinstance(self.provider, MockProvider):
            data = (await self.provider.complete(
                tier="fast", system="", user=text, mode="chat"))[0]
            reply = data.get("reply", "收到。")
            thought = data.get("thought", "")
            act_d = data.get("action", {}) or {}
        else:
            ws = self.latest_ws or await self.sense()
            digest = self.memory.search(f"{text} {getattr(ws.scene, 'value', '')}"[:80], 5)
            act = await self.react.step(
                ws, self.goal_compiler.view(), self.cfg.get("_action_space", {}),
                self.working_memory.recent_trajectory(), self.skill_lib.list_skills(),
                digest, user_message=text)
            act, ok, reason = self.validator.validate(act)
            thought = act.expected_effect or "（理解你的指令）"
            reply = (f"收到：{text}。我打算执行 {act.type}/{act.detail}，"
                     f"预期 {act.expected_effect}。")
            act_d = {"type": act.type, "detail": act.detail}
        self.latest_thought = thought
        self.latest_action = f"{act_d.get('type', '')}/{act_d.get('detail', '')}"
        self.events_log.append({"t": time.time(), "type": "chat_out", "text": reply})
        if len(self.events_log) > 200:
            self.events_log = self.events_log[-200:]
        return {"reply": reply, "thought": thought, "action": act_d}

    def user_say_sync(self, text: str) -> dict:
        """同步包装：供 HTTP 子线程调用。优先把协程调度到引擎事件循环；
        否则回退到独立事件循环执行（Mock 模式足够）。"""
        if self._loop is not None:
            try:
                fut = asyncio.run_coroutine_threadsafe(self.user_say(text), self._loop)
                return fut.result(timeout=30)
            except Exception:
                pass
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(self.user_say(text))
        finally:
            loop.close()

    # —— Overlay 悬浮窗后端 ——
    def overlay_state(self) -> dict:
        ws = self.latest_ws
        if ws is not None:
            scene = ws.scene.value
            urgency = ws.urgency
            hp = ws.self_state.hp
        else:
            scene, urgency, hp = "unknown", 0.0, None
        cg = self.goal_compiler.view()
        current = cg.get("current_goals", []) or []
        goals_txt = "; ".join(g.get("goal", "") for g in current[:3])
        return {
            "session_id": self.session_id,
            "running": self.started_at > 0,
            "scene": scene,
            "urgency": round(urgency, 3),
            "hp": hp,
            "current_goals": goals_txt,
            "budget": self.budget.status(),
            "skills_count": len(self.skill_lib.list_skills()),
            "latest_thought": self.latest_thought,
            "latest_action": self.latest_action,
            "ts": time.time(),
        }

    def overlay_status(self) -> dict:
        return {
            "running": self._overlay is not None and self._overlay.is_running(),
            "host": self.overlay_host,
            "port": self.overlay_port,
            "url": f"http://{self.overlay_host}:{self.overlay_port}/",
        }

    def overlay_screen_png(self) -> bytes:
        from overlay.overlay_page import make_screen_png
        scene = self.latest_ws.scene.value if self.latest_ws is not None else None
        return make_screen_png(scene)

    def recent_events(self) -> list:
        return list(self.events_log)[-20:]

    def start_overlay(self, host: str | None = None, port: int | None = None) -> dict:
        if self._overlay is not None:
            return self.overlay_status()
        cfg = self.cfg.get("overlay", {}) or {}
        self.overlay_host = host or cfg.get("host", "127.0.0.1")
        self.overlay_port = int(port or cfg.get("port", 8765))
        from overlay.overlay_server import OverlayServer
        self._overlay = OverlayServer(self, self.overlay_host, self.overlay_port)
        self._overlay.start()
        return self.overlay_status()

    def stop_overlay(self) -> dict:
        if self._overlay is not None:
            self._overlay.stop()
            self._overlay = None
        return self.overlay_status()

    def request_stop(self) -> None:
        """由 Overlay /api/control 触发，请求主循环退出。"""
        self._stop_requested = True


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

    # —— v1.4.1 时间轴流水线子测试 ——
    # 规划间隔 0.25s < 时间轴时域 ~1.6s：强制制造"上一条未执行完、下一条计算已开始"的重叠。
    # 验证：①流水线并行 ②supersede+改进意见 ③意图键位映射（无硬编码按键）④每步反馈回流
    # ⑤反思落实反馈（反思有反馈闭环）⑥drift 动态重锚 ⑦七段决策日志。
    print("\n[smoke] 时间轴流水线子测试：3 拍（explore/explore/explore），规划间隔 0.25s…")
    tl_res = await engine.run_pipeline(n_cycles=3, scenes=["explore", "explore", "explore"],
                                       plan_interval=0.25)
    await asyncio.sleep(0.05)  # flush：让已到点的定时动作 task 跑完再统计
    assert engine.overlap_cycles >= 1, "流水线重叠未发生：上一条时间轴执行中时下一条计算未开始"
    assert tl_res["supersedes"] >= 1, "未发生 supersede：没有在上一条时间轴未完成时提交新时间轴"
    assert any(e.get("aborted") for e in engine.timeline.supersede_events), \
        "supersede 未记录被放弃的未执行动作"
    assert any(b.get("improvements") for b in engine.last_blocks), \
        "决策日志缺少对未执行部分的改进意见"
    # B：意图化——时间轴动作一律为抽象意图，不出现硬编码按键；执行时经 KeymapBinder 绑定
    for b in engine.last_blocks:
        for ta in b["timeline"]:
            assert ta.get("intent") and ta["intent"] != "__direct__" or b["via"] == "combat_reflex", \
                f"时间轴动作未意图化: {ta}"
            assert "detail" not in ta and "key" not in str(ta.get("params", {})), \
                f"时间轴动作泄漏具体按键: {ta}"
    bound_recs = engine.timeline.all_exec_records()
    assert any(r.get("bound") for r in bound_recs), "意图未被绑定到具体按键（KeymapBinder 未生效）"
    assert engine.binder.bind("teleport", {})[0] is None, "未知意图未被拒绝"
    # D：反思有反馈——supersede 改进意见登记台账，且至少一条获得落实反馈回填
    assert engine.reflection_ledger.items, "反思台账为空：改进意见未登记"
    done = [it for it in engine.reflection_ledger.items if it.status != "pending"]
    assert done and any(it.feedback for it in done), \
        "反思没有反馈：台账中的改进意见未被后续拍评估回填"
    assert any(b.get("reflection_feedback") for b in engine.last_blocks), \
        "决策日志缺少反思落实反馈"
    # C：每步反馈回流——每步执行都落了轨迹（trajectory 含 intent 字段）
    assert engine.trajectory.count >= 3, "每步执行+反馈未落轨迹"
    # 定时触发精度：已执行(ok)动作的实际偏移与计划 at_s 误差 ≤ 0.05s（dry_run 无 drift 时）
    recs = bound_recs
    for e in recs:
        assert abs(e["offset"] - e["at_s"]) <= 0.05, \
            f"定时触发精度超差: at={e['at_s']:.2f} actual={e['offset']:.3f}"
    # 七段决策日志完整性（文本日志含 ①-⑦ 全部标记 + 时刻→预期→操作 渲染）
    with open(engine.decision_log_path, "r", encoding="utf-8") as f:
        log_text = f.read()
    for marker in ("① 现状分析", "② 是否改变上一条时间轴", "③ 上一步动作的完成情况",
                   "④ 目标达成度与反思改进", "⑤ 下一步要实现什么目标",
                   "⑥ 为了实现目标应该执行什么操作", "⑦ 接下来一小段时间轴",
                   "反思落实反馈", "预期: ", "操作: "):
        assert marker in log_text, f"决策日志缺少段: {marker}"

    # D：drift 动态重锚单元测试——慢速执行器模拟实际耗时，验证后续动作时刻自动平移
    print("[smoke] drift 动态重锚单元测试（慢速执行器 40ms/步）…")
    from decision.timeline import TimelineScheduler as _TS
    class _SlowExec:
        async def execute(self, act):
            await asyncio.sleep(0.04)
            return {"observed_result": "慢速执行完成", "executed": True, "dry_run": False}
    sched = _TS(engine.binder, _SlowExec())
    slow_tl = ActionTimeline(actions=[
        TimelineAction(0.0, "move_forward", {"duration_s": 0.0}, "e1"),
        TimelineAction(0.10, "move_forward", {"duration_s": 0.0}, "e2"),
        TimelineAction(0.20, "stop_move", {}, "e3")])
    await sched.submit(slow_tl)
    await slow_tl.task
    off3 = slow_tl.exec[2]["offset"]
    assert off3 >= 0.24, f"drift 重锚未生效：第三动作实际 {off3:.3f}s，应 ≥0.24s（0.20+两步慢执行平移）"
    print(f"[smoke] drift 重锚：第三动作计划 0.20s → 实际 {off3:.3f}s（随实际耗时自动后移）")
    print(f"[smoke] 流水线：cycles={tl_res['cycles']} 重叠拍={tl_res['overlap_cycles']} "
          f"时间轴={tl_res['timelines']} supersede={tl_res['supersedes']} "
          f"已执行动作={len(recs)}（定时误差全部 ≤0.05s）")
    print(f"[smoke] 决策日志: {engine.decision_log_path}")

    # —— Overlay 悬浮窗子测试（HTTP 服务：state/chat/screen 全 200）——
    # 注：不把 smoke 的事件循环设为 engine._loop，避免 HTTP 客户端阻塞主循环导致 chat 死锁；
    # user_say_sync 会回退到独立事件循环执行（Mock 模式足够）。真实 --overlay 模式才用主循环调度。
    print("\n[smoke] 启动 Overlay HTTP 服务子测试…")
    ov = engine.start_overlay()
    assert ov.get("running"), "Overlay 未启动"
    base = f"http://{engine.overlay_host}:{engine.overlay_port}"
    # 轮询等待服务就绪（后台线程托管）
    for _ in range(50):
        if engine._overlay and engine._overlay.is_running():
            try:
                urllib.request.urlopen(base + "/api/state", timeout=1)
                break
            except Exception:
                time.sleep(0.05)

    def _get(url):
        with urllib.request.urlopen(url, timeout=5) as r:
            return r.status, r.read()

    def _post(url, body, token=""):
        headers = {"Content-Type": "application/json"}
        if token:
            headers["X-MetaSpirit-Token"] = token
        req = urllib.request.Request(url, data=body, headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, r.read()

    _tok = getattr(engine._overlay, "token", "") if engine._overlay else ""
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as ex:
        f_state = ex.submit(_get, base + "/api/state")
        st_code, st_raw = f_state.result()
        st = json.loads(st_raw.decode("utf-8"))
        assert st_code == 200 and "session_id" in st, f"state 异常: {st_code}"

        f_chat = ex.submit(_post, base + "/api/chat",
                           json.dumps({"text": "去西边看看"}).encode("utf-8"), _tok)
        ch_code, ch_raw = f_chat.result()
        ch = json.loads(ch_raw.decode("utf-8"))
        assert ch_code == 200 and ch.get("reply"), f"chat 异常: {ch_code} {ch}"

        f_screen = ex.submit(_get, base + "/api/screen")
        sc_code, sc_raw = f_screen.result()
        assert sc_code == 200 and sc_raw[:8] == b"\x89PNG\r\n\x1a\n", f"screen 异常: {sc_code}"

        # 鉴权：无 token 的 POST 必须 401（跨 Origin 网页无法注入指令/停机）
        def _post_no_token():
            try:
                _post(base + "/api/control", json.dumps({"action": "stop"}).encode("utf-8"))
                return 200
            except urllib.error.HTTPError as e:
                return e.code
            except Exception:
                return -1
        f_noauth = ex.submit(_post_no_token)
        na_code = f_noauth.result()
        assert na_code == 401, f"无 token POST 未被拒绝（got {na_code}）"
    engine.stop_overlay()
    assert (engine.overlay_status().get("running") is False), "Overlay 未关闭"
    print(f"[smoke] Overlay 子测试通过：/api/state={st_code} /api/chat={ch_code}(reply='{ch.get('reply','')[:12]}…') /api/screen={sc_code} PNG={len(sc_raw)}B 无tokenPOST={na_code}(401=已拦截)")

    print("\n[smoke] 战斗反射延迟(ms):", [round(x, 3) for x in reflex_latencies])
    print("[smoke] 轨迹文件:", engine.trajectory.path)
    print("[smoke] 目标编译→感知3拍→决策(1反射+1反思)→dry_run执行→落盘 全部通过")
    print("SMOKE PASS")
    return 0


def main():
    parser = argparse.ArgumentParser(description="元灵 MetaSpirit 引擎")
    parser.add_argument("--smoke", action="store_true", help="运行端到端冒烟测试")
    parser.add_argument("--overlay", action="store_true", help="启动引擎 + Overlay 悬浮窗常驻")
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

    if args.overlay:
        asyncio.run(overlay_main(engine))
        sys.exit(0)

    # 非 smoke：启动实时主循环（DSH 插件通常通过 rpc_server 驱动；此处提供最小实时循环）
    print("引擎已就绪（实时主循环）。通常通过 rpc_server.py 经 JSON-RPC 驱动；"
          "加 --overlay 可启动悬浮窗 HTTP 服务。")


async def overlay_main(engine: "Engine"):
    """--overlay：启动引擎 + Overlay 悬浮窗，并进行感知/决策常驻循环。"""
    engine._loop = asyncio.get_running_loop()
    await engine.start("minecraft", "", "夜晚将至，需在天黑前收集木材搭建庇护所。", "")
    status = engine.start_overlay()
    print(f"[overlay] Overlay 服务已启动：{status.get('url')}（Ctrl+C 退出）")
    interval = engine.cfg.get("decision", {}).get("pipeline_interval", 1.5)
    try:
        # v1.4：主循环切换为时间轴流水线（感知→规划不等执行→提交/取代），动作由调度器定时触发
        await engine.run_pipeline(plan_interval=interval)
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        engine.stop_overlay()
        print("\n[overlay] 已停止悬浮窗服务。")


if __name__ == "__main__":
    main()
