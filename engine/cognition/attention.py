# cognition/attention.py —— 注意力调度器（帧率/ROI/MLLM 档位 三级分配）
# 依据场景类型给出 AttentionPlan。战斗路径 mllm_tier=reflex（规则，不经 MLLM）。

from ms_types import AttentionPlan, SceneType

# 场景 → 三级参数表
_TABLE = {
    SceneType.COMBAT:  dict(fps=45, kf=400, rois=["combat_threat"], tier="reflex", hz=10.0),
    SceneType.PUZZLE:  dict(fps=8,  kf=1500, rois=["puzzle_area"], tier="deep",   hz=1.0),
    SceneType.STORY:   dict(fps=2,  kf=4000, rois=["dialog"],      tier="deep",   hz=0.5),
    SceneType.EXPLORE: dict(fps=10, kf=1000, rois=[],              tier="fast",   hz=1.0),
    SceneType.MENU:    dict(fps=2,  kf=2000, rois=[],              tier="fast",   hz=0.5),
    SceneType.LOADING: dict(fps=2,  kf=2000, rois=[],              tier="fast",   hz=0.5),
}


class AttentionScheduler:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        # 用配置覆盖默认表
        pf = cfg.get("perception", {}).get("capture_fps", {})
        ki = cfg.get("perception", {}).get("keyframe_interval_ms", {})
        for sc, name in [(SceneType.COMBAT, "combat"), (SceneType.PUZZLE, "puzzle"),
                         (SceneType.STORY, "story"), (SceneType.EXPLORE, "explore")]:
            if name in pf:
                _TABLE[sc]["fps"] = pf[name]
            if name in ki:
                _TABLE[sc]["kf"] = ki[name]

    def plan_for(self, scene: SceneType) -> AttentionPlan:
        t = _TABLE.get(scene, _TABLE[SceneType.EXPLORE])
        return AttentionPlan(
            frame_rate_capture=t["fps"],
            keyframe_interval_ms=t["kf"],
            roi_targets=list(t["rois"]),
            mllm_tier=t["tier"],
            perceive_hz=t["hz"],
        )

    def update_from_worldstate(self, ws) -> AttentionPlan:
        """从最新 WorldState 推导注意力计划（合并 P2 场景分类）。"""
        return self.plan_for(ws.scene)
