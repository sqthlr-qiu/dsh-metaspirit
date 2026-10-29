# cognition/working_memory.py —— 工作记忆（关键帧摘要 + 滚动轨迹窗口）
# 常驻：最近 WorldState 列表（含关键帧摘要）+ 最近 N 个 (thought, action, result) 三元组。

from collections import deque

from ms_types import WorldState


class WorkingMemory:
    def __init__(self, max_worldstates: int = 8, max_trajectory: int = 5):
        self.worldstates: deque = deque(maxlen=max_worldstates)
        self.trajectory: deque = deque(maxlen=max_trajectory)  # (thought, action_json, result)

    def push(self, ws: WorldState):
        self.worldstates.append(ws)

    def latest(self) -> WorldState | None:
        return self.worldstates[-1] if self.worldstates else None

    def push_trajectory(self, thought: str, action_json: dict, result: str):
        self.trajectory.append((thought, action_json, result))

    def recent_trajectory(self) -> list:
        return list(self.trajectory)
