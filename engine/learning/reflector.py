# learning/reflector.py —— P8 反思（Reflexion，MLLM API）
# 铁律：反思/记忆/蒸馏走异步队列。本模块提供 reflect() 并把教训写入记忆库。

import json
import time

from llm.provider import MockProvider
from llm.json_utils import call_with_retry


class Reflector:
    def __init__(self, provider, cfg: dict, memory_store=None):
        self.provider = provider
        self.cfg = cfg
        self.memory = memory_store

    async def reflect(self, situation: str, attempted: str, observed: str) -> dict:
        """执行反思，返回 lesson dict，并写入语义记忆。"""
        if getattr(self.provider, "is_mock", False):
            data = (await self.provider.complete(tier="deep", system="", user="", mode="reflect"))[0]
        else:
            prompt = (
                "你是我（角色）的内心复盘。基于经历反思，输出 JSON：\n"
                "outcome/what_worked/what_failed/lesson/to_memory/next_time。\n"
                f"【情境】{situation}\n【尝试】{attempted}\n【观察】{observed}\n"
            )

            async def _make(feedback):
                d, raw = await self.provider.complete(
                    tier="deep", system="", user=prompt + (feedback or ""),
                    expect_json=True, mode="reflect")
                return d, raw

            def _degrade():
                return {"outcome": "partial", "what_worked": [],
                        "what_failed": [{"mistake": attempted, "consequence": observed,
                                        "root_cause": "未知"}],
                        "lesson": f"在<{situation}>下，应重新评估动作", "to_memory": {"semantic": []},
                        "next_time": "下次先确认环境再操作"}
            data = await call_with_retry(_make, ["lesson"], _degrade)

        lesson = data.get("lesson", "")
        if self.memory is not None:
            self.memory.add_semantic({
                "fact": lesson, "confidence": 0.8, "source": "reflection",
                "at": time.time(),
            })
            to_mem = data.get("to_memory", {}) or {}
            for s in (to_mem.get("semantic") or []):
                self.memory.add_semantic({"fact": s, "confidence": 0.7, "source": "reflection"})
        return data
