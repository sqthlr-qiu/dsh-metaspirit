# decision/timeline.py —— v1.4.1：时间轴流水线决策（滚动时域规划 + 规划/执行流水线）
# 需求：每次操作游戏时在日志输出七段结构化决策——①现状分析 ②是否改变上一条时间轴未完成的动作
#       ③上一步动作完成情况 ④目标达成度与反思改进（含反思落实反馈） ⑤下一步要实现什么目标
#       ⑥为实现目标应执行什么操作 ⑦接下来一小段时间轴的操作顺序（时刻→预期→操作）。
# v1.4.1 精化（四点）：
#   A. 每步按「时刻 → 预期 → 操作（按键）」顺序表达与执行；
#   B. 动作一律用游戏无关的"抽象意图"（dodge/move_left/camera_turn…）表达，不针对某一个游戏，
#      运行时经 KeymapBinder 映射为当前游戏的按键（按键不一定相同）；绑定失败=intent_not_mapped；
#   C. 每一步执行+反馈（observed/bound/drift）都回流给大模型（下一拍 P13 输入 action_feedbacks），
#      并经引擎回调落 StepRecord/轨迹 → 支撑反思与技能沉淀的持续深度学习；
#   D. 时间随实际耗时动态调整：每步执行后测 drift（实际结束-计划结束），后续动作触发时刻自动平移；
#      反思不止于生成——ReflectionLedger 跟踪每条反思/改进意见的落实情况，后续拍强制评估并回填反馈。
# 核心语义不变：规划不等执行——上一条时间轴尚未执行完，下一条时间轴的计算就已开始；
#       新时间轴提交时若旧时间轴仍有未执行动作，则取代（supersede）之，并对被放弃部分附改进意见。

import os
import json
import asyncio
from dataclasses import dataclass, field

from ms_types import Action
from llm.provider import MockProvider
from llm.json_utils import call_with_retry
from decision.keymap import KeymapBinder


@dataclass
class TimelineAction:
    """时间轴上的一个定时动作——「时刻 → 预期 → 操作」三段式，游戏无关。
    at_s 相对本时间轴起点（秒）；intent 为抽象动作；expected 为该时刻要达到的预期效果；
    具体按键由 KeymapBinder 在执行时按当前游戏映射。"""
    at_s: float                       # 时刻：相对偏移（秒，两位小数，如 0.10）
    intent: str                       # 操作：抽象动作意图（dodge/move_left/camera_turn/stop_move…）
    params: dict = field(default_factory=dict)  # 意图参数：duration_s/degrees/direction 等（不含按键）
    expected: str = ""                # 预期：该时刻期望达成的效果（执行后对照 → 反馈回流）


@dataclass
class ActionTimeline:
    """一条短时时间轴（滚动时域的一个规划窗口，时域 ≤5s：战斗 0.5-1.5 / 探索 2-5）。"""
    id: str = ""
    goal: str = ""                    # 本时间轴服务的目标（⑤ 下一步目标）
    horizon_s: float = 2.0
    actions: list = field(default_factory=list)        # list[TimelineAction]，按 at_s 递增，1-12 项
    supersede_of: str = ""            # 被本条取代的上一条 id（""=无）
    improvements: list = field(default_factory=list)   # 对被取代时间轴未执行部分的改进意见
    via: str = "react"                # react（P13 MLLM）/ combat_reflex（反射抢占）
    start_t: float = 0.0              # 提交时刻（scheduler 写回，loop.time()）
    task: object = None               # 整条时间轴的执行 task（v1.4.1：单任务循环制）
    exec: dict = field(default_factory=dict)           # idx -> {"state": ok/abort/invalid, at_s, offset, drift, bound, observed/reason}
    aborted: bool = False             # supersede 标记


@dataclass
class ReflectionItem:
    """一条待落实的反思/改进意见（反思必须有反馈：后续拍强制评估是否生效）。"""
    id: str
    text: str
    source: str                       # supersede_improvement / mismatch_lesson / reflect_lesson
    cycle: int                        # 产生于哪个决策周期
    status: str = "pending"           # pending / applied / failed / ongoing
    feedback: str = ""                # 大模型给出的落实证据


class ReflectionLedger:
    """反思落实台账：登记 → 下发 P13 评估 → 回填反馈。保证"反思有反馈"闭环。"""

    def __init__(self):
        self.items: list = []
        self._seq = 0

    def add(self, text: str, source: str, cycle: int) -> str:
        text = (text or "").strip()
        if not text:
            return ""
        self._seq += 1
        rid = f"rf-{self._seq}"
        self.items.append(ReflectionItem(rid, text, source, cycle))
        if len(self.items) > 50:
            self.items = self.items[-50:]
        return rid

    def pending(self) -> list:
        return [it for it in self.items if it.status == "pending"]

    def apply_feedback(self, fb: dict) -> bool:
        rid = str(fb.get("id", ""))
        for it in self.items:
            if it.id == rid and it.status == "pending":
                st = str(fb.get("status", "ongoing"))
                it.status = st if st in ("applied", "failed", "ongoing") else "ongoing"
                it.feedback = str(fb.get("evidence", ""))[:120]
                return True
        return False

    def recent_feedbacks(self, n: int = 6) -> list:
        done = [it for it in self.items if it.status != "pending"][-n:]
        return [{"id": it.id, "text": it.text[:60], "status": it.status,
                 "feedback": it.feedback} for it in done]


class TimelineScheduler:
    """时间轴调度器：单任务循环制（便于 drift 动态重锚）+ supersede 语义 + 完成情况跟踪。
    执行独立于规划循环 —— 规划循环在时间轴执行期间继续算下一条（流水线）。"""

    def __init__(self, binder: KeymapBinder, executor, validator=None,
                 on_action_feedback=None):
        self.binder = binder
        self.executor = executor
        self._validator = validator       # ActionValidator.validate 可调用（绑定后仍强制白名单）
        self.on_action_feedback = on_action_feedback   # 每步执行+反馈回调（引擎注入：轨迹/学习回流）
        self.current: ActionTimeline | None = None
        self.history: list = []
        self.supersede_events: list = []
        self.last_drift: float = 0.0      # 最近一次实测漂移（供 P13 时间校正参考）
        self._seq = 0

    def _next_id(self) -> str:
        self._seq += 1
        return f"tl-{self._seq}"

    # —— 状态查询（供决策日志 ②③ 段与 P13 反馈输入）——
    def pending_desc(self) -> list:
        tl = self.current
        if tl is None:
            return []
        return [f"{ta.at_s:.2f}s {ta.intent}"
                for i, ta in enumerate(tl.actions) if i not in tl.exec]

    def prev_status_text(self) -> str:
        tl = self.current
        if tl is None:
            return "（尚无执行中的时间轴，这是本会话第一条）"
        n = len(tl.actions)
        ok = sum(1 for e in tl.exec.values() if e.get("state") == "ok")
        inv = sum(1 for e in tl.exec.values() if e.get("state") == "invalid")
        pend = self.pending_desc()
        s = f"{tl.id}：已执行 {ok}/{n}" + (f"，校验/绑定拒绝 {inv}" if inv else "")
        if pend:
            s += f"，未执行待触发：{'、'.join(pend)}"
        else:
            s += "（即将全部完成）"
        return s

    def action_feedbacks(self, n: int = 12) -> list:
        """最近时间轴每步的执行反馈（供 P13 逐步对照学习：预期 vs 实际 + 绑定 + drift）。"""
        tl = self.current or (self.history[-1] if self.history else None)
        if tl is None:
            return []
        out = []
        for i, ta in enumerate(tl.actions):
            e = tl.exec.get(i, {})
            out.append({"at": ta.at_s, "intent": ta.intent, "expected": ta.expected,
                        "state": e.get("state", "pending"),
                        "bound": e.get("bound", ""), "observed": e.get("observed", ""),
                        "drift_s": round(e.get("drift", 0.0), 3)})
        return out[-n:]

    def all_exec_records(self) -> list:
        tls = list(self.history) + ([self.current] if self.current else [])
        return [e for tl in tls for e in tl.exec.values() if e.get("state") == "ok"]

    # —— 提交（supersede 语义在此）——
    async def submit(self, tl: ActionTimeline, change_prev: str = "supersede") -> dict:
        loop = asyncio.get_event_loop()
        if not tl.id:
            tl.id = self._next_id()
        result = {"superseded": None, "aborted": []}
        start_t = loop.time()
        if self.current is not None:
            old = self.current
            pend_idx = [i for i in range(len(old.actions)) if i not in old.exec]
            if pend_idx and change_prev == "supersede":
                old.aborted = True
                if old.task and not old.task.done():
                    old.task.cancel()
                aborted_desc = []
                for i in pend_idx:
                    old.exec[i] = {"state": "abort"}
                    ta = old.actions[i]
                    aborted_desc.append(f"{ta.at_s:.2f}s {ta.intent}")
                self.supersede_events.append(
                    {"from": old.id, "to": tl.id, "aborted": aborted_desc})
                result = {"superseded": old.id, "aborted": aborted_desc}
            elif change_prev == "keep" and pend_idx:
                # r3 修复（keep 双轴串行化）：keep 时旧轴剩余动作继续触发，
                # 新轴从"旧轴剩余动作预计完成时刻"起排——避免两条时间轴并行按键
                # （此前仅靠 validator 冷却兜底）。
                drift_est = 0.0
                for e in old.exec.values():
                    d = e.get("offset", 0.0) - e.get("at_s", 0.0)
                    if d > drift_est:
                        drift_est = d          # 已执行步的实测累计平移
                last_at = max(old.actions[i].at_s for i in pend_idx)
                old_end = old.start_t + last_at + drift_est
                if old_end > start_t:
                    start_t = old_end
            # change_prev == "keep" 且无剩余：旧轴已完成，新轴立即开始
            self.history.append(old)
        tl.start_t = start_t
        tl.task = asyncio.create_task(self._run_timeline(tl))
        self.current = tl
        return result

    async def _run_timeline(self, tl: ActionTimeline):
        """单任务顺序执行：到点 → 绑定意图 → 白名单校验 → 执行 → 反馈回流 → 测 drift 动态重锚后续时刻。"""
        loop = asyncio.get_event_loop()
        drift = 0.0
        for idx, ta in enumerate(tl.actions):
            fire = tl.start_t + ta.at_s + drift       # D：时刻随实际耗时动态调整
            delay = fire - loop.time()
            if delay > 0:
                await asyncio.sleep(delay)
            if tl.aborted:
                return
            actual = loop.time() - tl.start_t
            # B：意图 → 当前游戏按键（按键不一定相同）
            act, reason = self.binder.bind(ta.intent, ta.params)
            if act is None:
                tl.exec[idx] = {"state": "invalid", "at_s": ta.at_s, "offset": actual,
                                "reason": reason, "bound": ""}
                self._feedback(tl, idx, ta, tl.exec[idx])
                continue
            bound_desc = self.binder.describe(ta.intent, ta.params)
            if self._validator is not None:
                act, ok, vreason = self._validator(act)
                if not ok:
                    tl.exec[idx] = {"state": "invalid", "at_s": ta.at_s, "offset": actual,
                                    "reason": vreason, "bound": bound_desc}
                    self._feedback(tl, idx, ta, tl.exec[idx])
                    continue
            res = await self.executor.execute(act)
            observed = res.get("observed_result", "")
            end_actual = loop.time() - tl.start_t
            # D：时间随实际耗时动态调整——实际结束晚于"计划结束(at+duration)"的部分
            # 顺延后续动作；只顺延不提前（dry_run 不真实消耗 duration，提前会抢跑），限幅 0.5s
            dur = float(ta.params.get("duration_s", 0) or 0)
            step_drift = max(0.0, end_actual - (ta.at_s + dur))
            drift = min(0.5, step_drift)
            self.last_drift = drift
            tl.exec[idx] = {"state": "ok", "at_s": ta.at_s, "offset": end_actual,
                            "drift": step_drift, "bound": bound_desc, "observed": observed}
            self._feedback(tl, idx, ta, tl.exec[idx])  # C：每步执行+反馈 → 学习层

    def _feedback(self, tl, idx, ta, rec):
        if self.on_action_feedback:
            try:
                self.on_action_feedback(tl, idx, ta, rec)
            except Exception:
                pass


# ============================ P13 时间轴规划器 ============================

def _load_p13() -> str:
    p = os.path.join(os.path.dirname(__file__), "..", "prompts", "p13_timeline.txt")
    try:
        with open(p, "r", encoding="utf-8") as f:
            return f.read()
    except Exception:
        return ""


async def plan_timeline(provider, ws, goal_view: dict, action_space: dict,
                        prev_status_text: str, prev_pending: list, prev_id: str,
                        action_feedbacks: list, pending_reflections: list,
                        last_drift: float = 0.0,
                        user_message: str = "", identity: str = "",
                        memory_digest: list | None = None) -> dict:
    """P13：一次调用产出七段决策 JSON。输入含上一步每步执行反馈与待评估反思（学习闭环）。
    identity：P1 身份提示词（no_go 红线）；memory_digest：记忆检索摘要（r3 接回 P13 注入）。"""
    from decision.react_loop import _ws_min
    payload = {
        "world_state": _ws_min(ws),
        "goal_tree": goal_view,
        "whitelist_summary": action_space,
        "prev_timeline": {"id": prev_id, "status": prev_status_text, "pending": prev_pending},
        "action_feedbacks": action_feedbacks,          # 每步：预期 vs 实际 + 绑定 + drift
        "time_drift_hint": f"上一步实测时间漂移 {last_drift:+.2f}s，本条时间轴的 at 请据此校正",
        "pending_reflections": pending_reflections,    # 待落实评估的反思/改进意见
        "memory_digest": memory_digest or [],          # r3：记忆检索摘要注入时间轴主决策路径
        "user_directive": user_message or "",
        "now_hint": "你正在计算下一条时间轴；上一条可能仍在执行，现在就规划，不要等它执行完。",
    }
    p13 = _load_p13()
    user = p13 + "\n\n【输入】" + json.dumps(payload, ensure_ascii=False)
    sys_prompt = ("你是元灵的时间轴规划器。" + (f"\n\n{identity}" if identity else ""))
    if isinstance(provider, MockProvider):
        return (await provider.complete(tier="fast", system="", user=user, mode="timeline"))[0]

    async def _make(feedback):
        return await provider.complete(tier="fast", system=sys_prompt,
                                       user=user + (feedback or ""),
                                       expect_json=True, mode="timeline")

    def _degrade():
        return {"change_prev": "supersede",
                "situation_analysis": "API 异常或输出不合法，转入保守滑行",
                "goal_progress": "本轮无法评估", "reflection": "等待恢复后重新感知评估",
                "next_goal": "维持当前状态", "ops_plan": "等待观察一拍",
                "timeline": [{"at": 0.0, "intent": "stop_move", "params": {"duration_s": 0.5},
                              "expected": "等待一拍后重新感知"}],
                "improvements_on_discarded": ["API 异常，放弃原计划剩余动作，改保守等待"],
                "reflection_feedback": []}

    return await call_with_retry(_make, ["timeline"], _degrade)


def normalize_timeline(data: dict) -> tuple:
    """P13 输出 -> (ActionTimeline, change_prev, improvements, reflection_feedback)。"""
    change_prev = data.get("change_prev", "supersede")
    if change_prev not in ("keep", "supersede"):
        change_prev = "supersede"
    improvements = [str(x) for x in (data.get("improvements_on_discarded") or []) if str(x).strip()]
    if change_prev == "keep":
        improvements = []
    rf = [x for x in (data.get("reflection_feedback") or []) if isinstance(x, dict)]
    tl_actions = []
    for a in (data.get("timeline") or [])[:12]:
        try:
            at = max(0.0, float(a.get("at", 0.0) or 0.0))
            intent = str(a.get("intent", "")).strip()
            if not intent:
                continue
            tl_actions.append(TimelineAction(
                at_s=round(min(at, 4.5), 3),
                intent=intent,
                params=a.get("params") or {},
                expected=str(a.get("expected", ""))))
        except Exception:
            continue
    if not tl_actions:
        tl_actions = [TimelineAction(0.0, "stop_move", {"duration_s": 0.5},
                                     "感知不明确，保守等待一拍")]
    horizon = max(ta.at_s + float(ta.params.get("duration_s", 0) or 0) for ta in tl_actions) + 0.5
    tl = ActionTimeline(goal=str(data.get("next_goal", "")),
                        horizon_s=round(min(horizon, 5.0), 2),
                        actions=tl_actions, improvements=improvements, via="react")
    return tl, change_prev, improvements, rf


# ============================ 七段结构化决策日志 ============================

def format_decision_block(b: dict) -> str:
    lines = [
        f"════ 决策周期 #{b['cycle']} @ t={b['t']:.2f}s （路径={b['via']}，时间轴={b['tl_id']}）════",
        f"① 现状分析：{b['situation']}",
        f"② 是否改变上一条时间轴接下来未完成的动作：{b['changed']}",
        f"③ 上一步动作的完成情况：{b['prev_status']}",
        f"④ 目标达成度与反思改进：达成 {b['goal_progress']}；反思：{b['reflection']}",
    ]
    if b.get("reflection_feedback"):
        lines.append("    —— 反思落实反馈：" + "；".join(
            f"[{r['id']} {r['status']}] {r['feedback']}" for r in b["reflection_feedback"]))
    lines += [
        f"⑤ 下一步要实现什么目标：{b['next_goal']}",
        f"⑥ 为了实现目标应该执行什么操作：{b['ops_plan']}",
        f"⑦ 接下来一小段时间轴（{b['horizon']:.2f}s，共 {len(b['timeline'])} 项；每步=时刻→预期→操作）：",
    ]
    for ta in b["timeline"]:
        lines.append(f"    {ta['at_s']:.2f}s 后 → 预期: {ta['expected'] or '（见操作说明）'}"
                     f" → 操作: {ta['desc']}")
    if b.get("improvements"):
        lines.append("    —— 对上一条未执行完部分的改进意见：" + "；".join(b["improvements"]))
    return "\n".join(lines)


def ta_desc(ta: TimelineAction, binder: KeymapBinder | None = None) -> str:
    """⑦ 段每步的"操作"描述：意图+参数，附当前游戏的实际按键绑定（按键不一定相同）。"""
    p = ta.params or {}
    bits = [ta.intent]
    if p.get("degrees"):
        bits.append(f"{p.get('direction') or 'right'} {p['degrees']} 度")
    if p.get("duration_s"):
        bits.append(f"持续 {float(p['duration_s']):.2f}s")
    s = " ".join(str(x) for x in bits)
    if binder is not None:
        s += f"（绑定: {binder.describe(ta.intent, ta.params)}）"
    return s
