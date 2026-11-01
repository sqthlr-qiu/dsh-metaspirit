# cognition/working_memory.py —— 工作记忆（关键帧摘要 + 滚动轨迹窗口）
# 常驻：最近 WorldState 列表（含关键帧摘要）+ 最近 N 个 (thought, action, result) 三元组。

import time
from collections import deque

from ms_types import WorldState


class WorkingMemory:
    def __init__(self, max_worldstates: int = 8, max_trajectory: int = 5):
        self.worldstates: deque = deque(maxlen=max_worldstates)
        self.trajectory: deque = deque(maxlen=max_trajectory)  # (thought, action_json, result)
        self.user_chats: deque = deque(maxlen=20)  # 悬浮窗玩家对话（来源标记 user_chat）

    def push(self, ws: WorldState):
        self.worldstates.append(ws)

    def latest(self) -> WorldState | None:
        return self.worldstates[-1] if self.worldstates else None

    def push_trajectory(self, thought: str, action_json: dict, result: str):
        self.trajectory.append((thought, action_json, result))

    def push_user_chat(self, text: str):
        """悬浮窗玩家消息写入工作记忆（标记来源 user_chat）。"""
        if text:
            self.user_chats.append({"t": time.time(), "source": "user_chat", "text": text})

    def recent_trajectory(self) -> list:
        return list(self.trajectory)
