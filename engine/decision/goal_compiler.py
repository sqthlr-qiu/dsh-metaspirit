# decision/goal_compiler.py —— P4：背景文本 → 目标树（MLLM 或 Mock）
# 铁律：每个目标必须标注 derived_from（防编造）；生存底线 priority=0；更新走编译器防编造。

import os

from ms_types import Goal, SceneType
from llm.provider import MockProvider
from llm.json_utils import call_with_retry, safe_json_loads


class GoalCompiler:
    def __init__(self, provider, cfg: dict, game_background: str = ""):
        self.provider = provider
        self.cfg = cfg
        self.game_background = game_background
        self.goal_tree: dict = {"survival_goals": [], "current_goals": [], "growth_goals": [], "no_go": []}
        self.goals: list = []  # list[Goal]
        self.user_directives: list = []  # 悬浮窗玩家指令上下文（最高优先级，不越过 no_go）

    def add_user_directive(self, text: str):
        """把悬浮窗玩家一句话作为 user_directive 上下文追加（生存底线 no_go 仍优先）。"""
        if text:
            self.user_directives.append(text)

    def _to_goals(self, d: dict) -> list:
        out = []
        for grp, prio_base in [("survival_goals", 0), ("current_goals", 1), ("growth_goals", 3)]:
            for i, g in enumerate(d.get(grp, []) or []):
                gid = f"{grp}_{i}"
                out.append(Goal(
                    id=gid,
                    text=g.get("goal", ""),
                    priority=int(g.get("priority", prio_base)),
                    derived_from=g.get("derived_from", "背景/事件推导"),
                    subtasks=g.get("subtasks", []) or [],
                    success_signal=g.get("success_signal", ""),
                    abort_condition=g.get("abort_condition", ""),
                    reward_fn=g.get("reward_fn"),
                ))
        return out

    async def compile_initial(self, background_text: str = "") -> list:
        bg = background_text or self.game_background
        if isinstance(self.provider, MockProvider):
            data = (await self.provider.complete(tier="deep", system="", user="", mode="goal"))[0]
        else:
            prompt = (
                "你是元灵的目标编译器。根据游戏背景设定，推导当前阶段目标树。\n"
                f"【游戏背景设定】\n{bg}\n【最近事件流】\n（开局，无事件）\n"
                "输出 JSON：survival_goals/current_goals/growth_goals/no_go。"
            )

            async def _make(feedback):
                data, raw = await self.provider.complete(
                    tier="deep", system="", user=prompt + (feedback or ""),
                    expect_json=True, mode="goal")
                return data, raw

            def _degrade():
                return {"survival_goals": [{"goal": "维持HP>50%", "why": "默认生存", "priority": 0}],
                        "current_goals": [], "growth_goals": [], "no_go": []}

            data = await call_with_retry(_make, ["survival_goals"], _degrade)
        self.goal_tree = data
        self.goals = self._to_goals(data)
        return self.goals

    def view(self) -> dict:
        return self.goal_tree

    async def update(self, event_stream: str) -> list:
        """重大事件增量更新（仍走编译器防编造）。"""
        if isinstance(self.provider, MockProvider):
            data = (await self.provider.complete(tier="deep", system="", user="", mode="goal"))[0]
            self.goal_tree = data
            self.goals = self._to_goals(data)
            return self.goals
        # 真实模式：把事件流喂给 deep 档重编译（此处简化为复用 compile）
        return await self.compile_initial(self.game_background)
