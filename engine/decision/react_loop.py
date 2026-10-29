# decision/react_loop.py —— P6：思考-行动主循环（MLLM ReAct）
# 输出：thought → action → expected_effect。强制 JSON + 校验。白名单由 ActionValidator 把关。

import os
import json

from ms_types import Action, WorldState
from llm.provider import MockProvider
from llm.json_utils import call_with_retry


def _load_prompt(name: str) -> str:
    p = os.path.join(os.path.dirname(__file__), "..", "prompts", name)
    try:
        with open(p, "r", encoding="utf-8") as f:
            return f.read()
    except Exception:
        return ""


class ReActLoop:
    def __init__(self, provider, cfg: dict, p6_prompt: str = ""):
        self.provider = provider
        self.cfg = cfg
        self.p6_prompt = p6_prompt or _load_prompt("p6_decision.txt")

    async def step(self, ws: WorldState, goal_tree: dict, action_space: dict,
                   recent_trajectory: list, skill_menu: list, memory_digest: list) -> Action:
        """决策主入口：返回 Action（含 expected_effect）。"""
        if isinstance(self.provider, MockProvider):
            data = (await self.provider.complete(tier="fast", system="", user="", mode="decision"))[0]
        else:
            user = (
                f"{self.p6_prompt}\n\n"
                f"【世界状态】{json.dumps(_ws_min(ws), ensure_ascii=False)}\n"
                f"【目标树】{json.dumps(goal_tree, ensure_ascii=False)}\n"
                f"【白名单】{json.dumps(action_space, ensure_ascii=False)}\n"
                f"【最近轨迹】{json.dumps(recent_trajectory, ensure_ascii=False)}\n"
                f"【技能菜单】{json.dumps(skill_menu, ensure_ascii=False)}\n"
                f"【记忆摘要】{json.dumps(memory_digest, ensure_ascii=False)}\n"
            )

            async def _make(feedback):
                data, raw = await self.provider.complete(
                    tier="fast", system="你是元灵的大脑。", user=user + (feedback or ""),
                    expect_json=True, mode="decision")
                return data, raw

            def _degrade():
                # 降级：保守 wait
                return {"thought": "感知不确定，保守等待一拍", "selected_goal": "",
                        "action": {"type": "wait", "detail": "wait", "params": {}},
                        "expected_effect": "保持当前状态", "fallback": "重新感知"}
            data = await call_with_retry(_make, ["action"], _degrade)

        act_d = data.get("action", {}) or {}
        return Action(
            type=act_d.get("type", "wait"),
            detail=act_d.get("detail", "wait"),
            params=act_d.get("params", {}) or {},
            expected_effect=data.get("expected_effect", ""),
        )


def _ws_min(ws: WorldState) -> dict:
    """压缩世界状态（控制 token < 6k）。"""
    return {
        "scene": ws.scene.value,
        "urgency": ws.urgency,
        "self": {"hp": ws.self_state.hp, "animation": ws.self_state.animation,
                 "facing": ws.self_state.facing},
        "entities": [{"name": e.name, "threat": e.threat, "intent": e.intent_guess}
                     for e in ws.entities],
        "recent_change": ws.recent_change,
        "events": ws.event_transitions,
    }
