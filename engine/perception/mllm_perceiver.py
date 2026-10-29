# perception/mllm_perceiver.py —— 【核心】单次多任务感知：P3 → 五路语义 + event_transitions
# 铁律：感知输入是帧序列段（4-8帧带 t_ms + ROI≤3）；MLLM 单次输出五路语义 + 事件转变。
# 强制 JSON + schema 校验，失败重试一次再降级 wait。

import os

from ms_types import WorldState, json_to_worldstate, SceneType
from llm.provider import MockProvider
from llm.vision_payload import build_vision_payload
from llm.json_utils import call_with_retry, safe_json_loads

# P3 必填字段（schema 校验用）
_PERCEIVE_REQUIRED = ["scene", "self_check", "screen_text", "ui_state",
                      "entities", "environment", "event_transitions"]


def _load_prompt(name: str) -> str:
    p = os.path.join(os.path.dirname(__file__), "..", "prompts", name)
    try:
        with open(p, "r", encoding="utf-8") as f:
            return f.read()
    except Exception:
        return ""


class MLLMPerceiver:
    def __init__(self, provider, cfg: dict):
        self.provider = provider
        self.cfg = cfg
        self.p3_prompt = _load_prompt("p3_perception.txt")

    # —— Mock 场景构造（确定性，供 smoke 与离线演示）——
    def _mock_worldstate(self, scene: str) -> WorldState:
        if scene == "combat":
            d = {
                "scene": {"type": "combat", "urgency": 0.85},
                "self_check": {"bbox": [400, 300, 440, 360], "facing": "南",
                               "animation": "持剑警戒", "condition": "健康", "confidence": 0.92},
                "screen_text": {"dialog": "", "tutorial": "", "numbers": "HP 18/20"},
                "ui_state": {"active_panel": "游戏内", "clickable": []},
                "entities": [{"name": "僵尸", "bbox": [380, 200, 440, 280],
                              "state": "巡逻", "intent_guess": "敌意", "guess": False,
                              "threat": 0.9, "relation": "敌对"}],
                "environment": {"location_guess": "夜晚平原", "walkable_directions": ["后撤"],
                                "hazards": ["黑暗"], "resources": []},
                "event_transitions": [{"t": "近帧", "event": "敌人开始接近并抬手"}],
                "recent_change": "敌人正在接近，进入近战范围",
                "uncertainty": [],
            }
        else:  # explore
            d = {
                "scene": {"type": "explore", "urgency": 0.1},
                "self_check": {"bbox": [400, 300, 440, 360], "facing": "北",
                               "animation": "站立", "condition": "健康", "confidence": 0.9},
                "screen_text": {"dialog": "", "tutorial": "按 W 前进", "numbers": "HP 20/20"},
                "ui_state": {"active_panel": "游戏内", "clickable": []},
                "entities": [],
                "environment": {"location_guess": "出生点平原", "walkable_directions": ["前", "左", "右"],
                                "hazards": [], "resources": ["树木", "石头"]},
                "event_transitions": [{"t": "近帧", "event": "天色渐暗"}],
                "recent_change": "天色渐暗，夜晚将至",
                "uncertainty": [],
            }
        return json_to_worldstate(d)

    async def perceive(self, seg, scene_override: str | None = None,
                       tier: str = "fast") -> WorldState:
        """感知主入口。scene_override 仅在 mock 下用于脚本化场景。
        tier 指定调用档位（fast=快档 / deep=旗舰档），供 force_deep 重感知使用。"""
        if isinstance(self.provider, MockProvider):
            scene = scene_override or "explore"
            return self._mock_worldstate(scene)

        # —— 真实路径：组包 + 调用 + 重试一次 + 降级 ——
        max_frames = self.cfg.get("mllm", {}).get(tier, {}).get("max_frames", 8)
        max_rois = self.cfg.get("mllm", {}).get(tier, {}).get("max_rois", 3)
        images, ts_text = build_vision_payload(seg, max_frames, max_rois)
        user = self.p3_prompt + "\n\n" + ts_text

        async def _make(feedback):
            data, raw = await self.provider.complete(
                tier=tier, system="你是元灵的视觉感知模块。", user=user + (feedback or ""),
                images=images if images else None, expect_json=True, mode="perceive")
            return data, raw

        def _degrade():
            # 降级：保守世界状态（无威胁、原地）
            return json_to_worldstate({
                "scene": {"type": "explore", "urgency": 0.0},
                "self_check": {"bbox": [400, 300, 440, 360], "facing": "unknown",
                               "animation": "unknown", "condition": "unknown", "confidence": 0.2},
                "screen_text": {}, "ui_state": {"active_panel": "unknown", "clickable": []},
                "entities": [], "environment": {}, "event_transitions": [],
                "recent_change": "（感知降级）无法确认画面", "uncertainty": ["感知降级"],
            })

        data = await call_with_retry(_make, _PERCEIVE_REQUIRED, _degrade)
        return json_to_worldstate(data)
