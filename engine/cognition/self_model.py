# cognition/self_model.py —— 自我状态卡维护（HP/位置/装备/目标链）
# 来源三级降级：桥接真值 > MLLM UI 语义 > 行为推断。本 MVP 以 MLLM 语义为主。

import time

from ms_types import SelfState, WorldState


class SelfModel:
    def __init__(self, name: str = "agent"):
        self.state = SelfState(name=name, updated_at=time.time())

    def update_from_worldstate(self, ws: WorldState):
        """从 WorldState 的自我感知字段更新 SelfState。"""
        sc = ws.self_state
        self.state.name = sc.name
        # HP 优先取数值（若 self_check 给出 condition 文本则保留）
        self.state.hp = sc.hp if sc.hp is not None else 1.0
        self.state.position = sc.position
        self.state.facing = sc.facing
        self.state.animation = sc.animation
        self.state.equipment = sc.equipment
        self.state.inventory = sc.inventory
        self.state.goal_chain = sc.goal_chain
        self.state.updated_at = time.time()
        return self.state

    def snapshot(self) -> SelfState:
        return self.state
