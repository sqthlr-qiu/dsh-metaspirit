# learning/trajectory_recorder.py —— 轨迹落盘（帧段 + 目的 + 操作 + 反馈 + 奖励）
# 铁律：trajectory_recorder 记录 (purpose, action_json, frame_segment, observed_result, reward)。
# 输出 trajectory/*.jsonl，每行一条 StepRecord（JSON）。

import os
import json
import time

from ms_types import StepRecord, worldstate_to_json


class TrajectoryRecorder:
    def __init__(self, out_dir: str = "trajectory"):
        self.out_dir = out_dir
        os.makedirs(out_dir, exist_ok=True)
        self.path = os.path.join(out_dir, f"traj_{int(time.time())}.jsonl")
        self.count = 0

    def log_worldstate(self, ws) -> None:
        """记录一拍世界状态（供帧段数据集积累）。"""
        try:
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(json.dumps({"kind": "worldstate",
                                    "ts": time.time(),
                                    "ws": worldstate_to_json(ws)},
                                   ensure_ascii=False) + "\n")
        except Exception:
            pass

    def log_step(self, record: StepRecord) -> None:
        """记录一条 StepRecord（目的/操作/帧段/反馈/奖励）。"""
        self.count += 1
        try:
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(json.dumps({"kind": "step",
                                    "t": record.t,
                                    "purpose": record.purpose,
                                    "action_json": record.action_json,
                                    "frame_segment": record.frame_segment,
                                    "observed_result": record.observed_result,
                                    "reward": record.reward},
                                   ensure_ascii=False) + "\n")
        except Exception:
            pass

    def stats(self) -> dict:
        return {"file": self.path, "steps": self.count}
