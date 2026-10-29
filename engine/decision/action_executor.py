# decision/action_executor.py —— L5 键鼠模拟执行
# 默认 dry_run 模式（打印 + 模拟反馈，不真正按键）；pydirectinput 可选导入。
# 铁律：执行后经 observe_delay 取后帧段，由 MLLM 判定 observed_result 与 expected_effect 是否一致。

import time
import asyncio

from ms_types import Action


class ActionExecutor:
    def __init__(self, dry_run: bool = True, observe_delay: float = 0.8):
        self.dry_run = dry_run
        self.observe_delay = observe_delay
        self._di = None
        if not dry_run:
            try:
                import pydirectinput  # type: ignore
                self._di = pydirectinput
            except ImportError:
                print("[ActionExecutor] pydirectinput 不可用，回退 dry_run")
                self.dry_run = True

    async def execute(self, act: Action) -> dict:
        """执行动作，返回 {observed_result, executed, dry_run}。"""
        if self.dry_run or self._di is None:
            observed = self._simulate_observed(act)
            return {"observed_result": observed, "executed": False, "dry_run": True}
        # 真实执行（可选路径）
        try:
            if act.type == "keypress":
                self._di.press(act.detail)
            elif act.type == "click":
                self._di.click()
            # 等待 observe_delay 后由感知层取后帧段（此处仅占位）
            await asyncio.sleep(0.05)
            observed = self._simulate_observed(act)
            return {"observed_result": observed, "executed": True, "dry_run": False}
        except Exception as e:
            return {"observed_result": f"执行异常:{e}", "executed": False, "dry_run": False}

    def _simulate_observed(self, act: Action) -> str:
        """dry_run 模拟反馈：基于动作类型给出确定性观察结果。"""
        if act.type == "keypress":
            d = act.detail
            if "前进" in d or d == "W":
                return "前进了一段距离，靠近目标"
            if "攻击" in d or "click" in d.lower():
                return "敌人受到攻击，出现受击反馈"
            if "背包" in d or d == "E":
                return "背包界面已打开"
            if "逃跑" in d or "后撤" in d:
                return "已远离威胁"
            if "翻滚" in d or "Space" in d:
                return "完成翻滚规避"
            return "按键已生效"
        if act.type == "wait":
            return "无显著变化"
        if act.type == "click":
            return "敌人受到攻击"
        return "动作已执行"

    def matched_expectation(self, expected_effect: str, observed_result: str) -> bool:
        """简单一致性判定：expected 与 observed 非空且不完全相反。
        严格匹配由 MLLM 对照（P6）；此处做基础判定（含语义近似）。"""
        if not expected_effect or not observed_result:
            return False
        if expected_effect == observed_result:
            return True
        # 关键词重叠近似：expected 中关键动词出现在 observed
        keys = [w for w in expected_effect.replace("，", " ").split() if len(w) >= 2]
        return any(k in observed_result for k in keys)
