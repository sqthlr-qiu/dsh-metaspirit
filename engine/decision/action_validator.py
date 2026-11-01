# decision/action_validator.py —— 操作合法性验证（白名单 + 坐标范围 + 冷却 + 重复上限）
# 铁律：ActionValidator 白名单强制；违规改写 wait 并记日志。战斗反射输出也需过校验。
# 协议卫生：拒绝日志走 stderr（stdout 是 JSON-RPC 通道）。

import sys
import time
import json

from ms_types import Action

# 允许的动作类型
_ALLOWED_TYPES = {"keypress", "click", "move", "skill", "wait", "speak"}


def _err(msg: str):
    print(msg, file=sys.stderr)


class ActionValidator:
    def __init__(self, action_space: dict, max_same_repeat: int = 2,
                 window_size: tuple = (640, 480), time_fn=None):
        self.action_space = action_space or {}
        self.max_same_repeat = max_same_repeat
        self.window_size = window_size
        # 可注入时钟（测试用），默认系统时钟
        self._now = time_fn or time.time
        self.keys = set(self.action_space.get("keys", []))
        self.mouse = set(self.action_space.get("mouse", []))
        self.cooldowns = self.action_space.get("cooldowns", {}) or {}
        # 禁用动作清单（配置消费：safety.forbidden_actions 或顶层 forbidden_actions）
        _f = self.action_space.get("forbidden_actions") \
            or (self.action_space.get("safety", {}) or {}).get("forbidden_actions") or []
        self.forbidden = set(str(x) for x in _f)
        self.recent: list = []          # 最近动作指纹（用于重复上限）
        self.last_exec_t: dict = {}     # 动作指纹 -> 上次执行时间

    def fingerprint(self, act: Action) -> str:
        """动作指纹：类型 + 细节 + 关键参数（bbox 用中心坐标，其余参数排序 JSON）。
        仅当参数改变语义才影响指纹，避免同动作不同坐标被误判为重复上限。"""
        p = act.params or {}
        parts = []
        bbox = p.get("bbox")
        if bbox and len(bbox) == 4:
            cx = (bbox[0] + bbox[2]) / 2
            cy = (bbox[1] + bbox[3]) / 2
            parts.append(f"bbox=({cx:.0f},{cy:.0f})")
        # 其余参数排序后纳入指纹（排除 bbox，已单独处理）
        other = {k: v for k, v in sorted(p.items()) if k != "bbox"}
        if other:
            parts.append("p=" + json.dumps(other, sort_keys=True, ensure_ascii=False))
        sig = ":".join(parts)
        return f"{act.type}:{act.detail}:{sig}" if sig else f"{act.type}:{act.detail}"

    def validate(self, act: Action) -> tuple:
        """返回 (action, ok, reason)。违规把 act 改写为 wait。"""
        # 类型白名单
        if act.type not in _ALLOWED_TYPES:
            return self._reject(act, f"type_not_whitelisted:{act.type}")
        # 禁用动作清单（如 "keypress/Q" 或裸 "speak"）
        if act.detail in self.forbidden or f"{act.type}/{act.detail}" in self.forbidden:
            return self._reject(act, f"forbidden_action:{act.type}/{act.detail}")
        # keypress 必须在 keys 白名单
        if act.type == "keypress":
            if act.detail not in self.keys:
                return self._reject(act, f"key_not_whitelisted:{act.detail}")
        # click / move 的 detail 必须在 mouse 白名单（含坐标范围）
        if act.type in ("click", "move"):
            if act.detail not in self.mouse:
                # 不在白名单：直接拒绝（白名单绕过修复）
                return self._reject(act, f"mouse_not_whitelisted:{act.detail}")
            bbox = act.params.get("bbox")
            if bbox and len(bbox) == 4:
                x1, y1, x2, y2 = bbox
                w, h = self.window_size
                if not (0 <= x1 <= w and 0 <= y1 <= h and 0 <= x2 <= w and 0 <= y2 <= h
                        and x1 <= x2 and y1 <= y2):
                    return self._reject(act, f"bbox_out_of_range:{bbox}")
        # skill 必须带非空技能名（技能是否存在于库由执行方 invoke 时判定）
        if act.type == "skill" and not str(act.detail or "").strip():
            return self._reject(act, "skill_no_detail")
        # 冷却
        fp = self.fingerprint(act)
        cd = self._cooldown_for(act)
        last = self.last_exec_t.get(fp)
        if last is not None and (self._now() - last) < cd:
            return self._reject(act, f"cooldown:{cd}s")
        # 重复上限
        same_count = sum(1 for r in self.recent[-self.max_same_repeat:] if r == fp)
        if same_count >= self.max_same_repeat:
            return self._reject(act, f"repeat_cap:{self.max_same_repeat}")
        # 通过
        self.recent.append(fp)
        self.last_exec_t[fp] = self._now()
        if len(self.recent) > 20:
            self.recent = self.recent[-20:]
        return (act, True, "ok")

    def _cooldown_for(self, act: Action) -> float:
        if act.type == "keypress" and "攻击" in act.detail:
            return float(self.cooldowns.get("attack_s", 0.5))
        return float(self.cooldowns.get("default_s", 0.2))

    def _reject(self, act: Action, reason: str) -> tuple:
        wait = Action(type="wait", detail="wait", params={}, expected_effect="（违规改写）保持安全")
        _err(f"[ActionValidator] 拒绝动作 {act.type}/{act.detail}: {reason} -> 改写为 wait")
        return (wait, False, reason)
