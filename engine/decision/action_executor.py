# decision/action_executor.py —— L5 键鼠模拟/真实执行（v1.4.1 修复真实链路）
# dry_run 由配置决定（cfg.decision.dry_run，默认 True；False 时真实注入 pydirectinput）。
# 真实分支：keypress 支持 duration_s 保持（keyDown→sleep→keyUp）；move 消费 dx/dy；
#           click 消费 bbox（先 moveTo 框中心再点击）；执行后经注入的 observer 做
#           真实帧差观察（Capture.snapshot diff）替代模拟文本。
# 协议卫生：所有输出走 stderr（stdout 是 JSON-RPC 通道）。

import sys
import time
import asyncio

from ms_types import Action


def _err(msg: str):
    print(msg, file=sys.stderr)


class ActionExecutor:
    def __init__(self, dry_run: bool = True, observe_delay: float = 0.8,
                 observer=None):
        self.dry_run = dry_run
        self.observe_delay = observe_delay
        self.observer = observer          # callable() -> snapshot|None（真实帧差观察）
        self._di = None
        if not dry_run:
            try:
                import pydirectinput  # type: ignore
                pydirectinput.PAUSE = 0.02   # 降低库内建延迟，时间轴精度不受拖累
                self._di = pydirectinput
            except ImportError:
                _err("[ActionExecutor] pydirectinput 不可用，回退 dry_run")
                self.dry_run = True

    async def execute(self, act: Action) -> dict:
        """执行动作，返回 {observed_result, executed, dry_run}。"""
        if self.dry_run or self._di is None:
            return {"observed_result": self._simulate_observed(act),
                    "executed": False, "dry_run": True}
        # —— 真实执行 ——
        before = self._snapshot()
        try:
            p = act.params or {}
            if act.type == "keypress":
                dur = float(p.get("duration_s", 0) or 0)
                if dur > 0.02:  # 保持语义：keyDown → sleep → keyUp（上限 3s 防卡键）
                    self._di.keyDown(act.detail)
                    await asyncio.sleep(min(dur, 3.0))
                    self._di.keyUp(act.detail)
                else:
                    self._di.press(act.detail)
            elif act.type == "move":
                dx = int(p.get("dx", 0) or 0)
                dy = int(p.get("dy", 0) or 0)
                if dx or dy:
                    self._di.moveRel(dx, dy, relative=True)
            elif act.type == "click":
                bbox = p.get("bbox")
                if bbox and len(bbox) == 4:
                    cx = int((bbox[0] + bbox[2]) / 2)
                    cy = int((bbox[1] + bbox[3]) / 2)
                    self._di.moveTo(cx, cy)
                btn = "right" if "right" in str(act.detail).lower() else "left"
                if str(act.detail).lower().startswith("hold_"):
                    # r3：长按（hold_left/hold_right——mining 等长按交互）
                    dur = min(float(p.get("duration_s", 0.3) or 0.3), 3.0)
                    self._di.mouseDown(button=btn)
                    await asyncio.sleep(dur)
                    self._di.mouseUp(button=btn)
                else:
                    self._di.click(button=btn)
            elif act.type == "keypress" and "scroll" in str(act.detail).lower():
                # r3：滚轮（scroll_up/scroll_down 备用通道）
                self._di.scroll(3 if "up" in str(act.detail).lower() else -3)
            elif act.type == "wait":
                await asyncio.sleep(min(float(p.get("duration_s", 0.1) or 0.1), 3.0))
            else:
                return {"observed_result": f"类型 {act.type} 真实分支未实现，已忽略",
                        "executed": False, "dry_run": False}
        except Exception as e:
            return {"observed_result": f"执行异常:{e}", "executed": False, "dry_run": False}
        # 真实观察：等感知延迟后取帧差（代替模拟文本）
        await asyncio.sleep(min(self.observe_delay, 1.0) * 0.5)
        observed = self._observe_real(before, act)
        return {"observed_result": observed, "executed": True, "dry_run": False}

    def _snapshot(self):
        try:
            return self.observer() if self.observer else None
        except Exception:
            return None

    def _observe_real(self, before, act: Action) -> str:
        after = self._snapshot()
        from perception.capture import Capture
        ratio = Capture.diff_ratio(before, after)
        if ratio < 0:
            return f"已真实执行 {act.type}/{act.detail}（观察不可用，等待感知层下一拍对照）"
        if ratio < 0.02:
            return f"已真实执行 {act.type}/{act.detail}；屏幕几乎无变化（变化率 {ratio:.0%}），预期效果可能未发生"
        return (f"已真实执行 {act.type}/{act.detail}；屏幕变化率 {ratio:.0%}，"
                f"待感知层下一拍判定预期是否达成")

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
        if act.type == "move":
            return "视角已转动"
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
