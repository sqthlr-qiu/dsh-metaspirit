# decision/keymap.py —— v1.4.1 新增：抽象动作意图 → 具体按键绑定（游戏无关层）
# 原则：时间轴上的动作一律用"意图"表达（dodge/move_left/camera_turn/stop_move…），
# 不针对某一个游戏、不硬编码按键；绑定时才查当前游戏的 keymap（按键不一定相同）。
# 绑定失败（意图未映射）→ invalid "intent_not_mapped"，进入决策日志与下一轮反思。

import os

try:
    import yaml
except ImportError:                      # 纯标准库兜底（engine 其余处同口径）
    yaml = None


# 通用兜底键位（keymap 文件缺失/解析失败时使用；任意游戏可被自己的 keymap 覆盖）
_GENERIC_KEYMAP = {
    "intents": {
        "dodge":        {"type": "keypress", "key": "Space"},
        "jump":         {"type": "keypress", "key": "Space"},
        "move_forward": {"type": "keypress", "key": "W"},
        "move_back":    {"type": "keypress", "key": "S"},
        "move_left":    {"type": "keypress", "key": "A"},
        "move_right":   {"type": "keypress", "key": "D"},
        "stop_move":    {"type": "wait", "detail": "wait"},
        "camera_turn":  {"type": "move", "detail": "move"},   # dx 由角度×dx_per_degree×方向计算
        "interact":     {"type": "keypress", "key": "E"},
        "attack":       {"type": "click", "detail": "left_click"},
        "open_ui":      {"type": "keypress", "key": "E"},
    },
    "camera": {"dx_per_degree": 10.0},   # 每度对应的鼠标位移（按游戏灵敏度可调）
}


def load_keymap(engine_dir: str, game: str = "") -> dict:
    """加载 keymap：config/keymap/{game}.yaml 优先，缺省回落 generic（内联兜底，保证总能绑定）。
    解析链：pyyaml → 极简解析器兜底（config_loader._minimal_yaml_parse，支持 flow style）；
    pyyaml 缺失或解析失败都不再"整份放弃"（v1.4.1 修复：极简解析器兜底）。"""
    if game:
        p = os.path.join(engine_dir, "config", "keymap", f"{game}.yaml")
        if os.path.exists(p):
            try:
                with open(p, "r", encoding="utf-8") as f:
                    text = f.read()
                data = None
                if yaml is not None:
                    try:
                        data = yaml.safe_load(text)
                    except Exception:
                        data = None
                if not isinstance(data, dict) or not data.get("intents"):
                    try:
                        from config_loader import _minimal_yaml_parse
                        data = _minimal_yaml_parse(text)
                    except Exception:
                        data = None
                if isinstance(data, dict) and data.get("intents"):
                    return data
            except Exception:
                pass
    return _GENERIC_KEYMAP


class KeymapBinder:
    """意图 → Action 绑定器。bind() 返回 (Action|None, reason)。
    绑定产物仍必须过 ActionValidator 白名单（键位映射错不改白名单铁律）。"""

    def __init__(self, keymap: dict):
        self.keymap = keymap or dict(_GENERIC_KEYMAP)
        self.intents = self.keymap.get("intents", {}) or {}
        self.camera = self.keymap.get("camera", {}) or {}

    def bind(self, intent: str, params: dict | None = None):
        params = params or {}
        if intent == "__direct__":
            # 反射直通：combat_reflex 产出的是具体 Action，不经意图层（仍过白名单校验）
            return self._mk(params.get("type", "wait"), params.get("detail", "wait"),
                            {}, "reflex_direct"), ""
        spec = self.intents.get(intent)
        if not spec:
            return None, f"intent_not_mapped:{intent}"
        t = spec.get("type", "wait")
        if t == "keypress":
            return self._mk(t, spec.get("key", ""), {}, intent), ""
        if t == "wait":
            return self._mk("wait", "wait", {}, intent), ""
        if t == "click":
            p = {}
            if spec.get("target") == "center":
                p = {}
            return self._mk("click", spec.get("detail", "left_click"), p, intent), ""
        if t == "move":
            # 视角/镜头：角度 × 每度位移 × 方向 → dx/dy（参数里的 degrees/direction 游戏无关）
            # right→dx+、left→dx−；down→dy+、up→dy−（俯仰）；斜向（如 up_right）取分量
            deg = float(params.get("degrees", 0) or 0)
            direction = str(params.get("direction", "right") or "right")
            dxdy = int(round(abs(deg) * float(self.camera.get("dx_per_degree", 10.0))))
            direction = direction.lower()
            dx = dxdy if "right" in direction else (-dxdy if "left" in direction else 0)
            dy = dxdy if "down" in direction else (-dxdy if "up" in direction else 0)
            note = f"{intent} {direction} {abs(deg)}度"
            return self._mk("move", spec.get("detail", "move"),
                            {"dx": dx, "dy": dy, "note": note}, intent), ""
        # 自定义类型由各游戏 keymap 扩展（如 skill），透传 detail
        return self._mk(t, spec.get("detail", intent), params, intent), ""

    def _mk(self, t, detail, params, intent):
        from ms_types import Action
        return Action(type=t, detail=detail, params=params, expected_effect=f"intent:{intent}")

    def describe(self, intent: str, params: dict | None = None) -> str:
        """给决策日志用的绑定描述（不真正构造 Action）。"""
        act, reason = self.bind(intent, params)
        if act is None:
            return f"⚠ {reason}"
        extra = params or {}
        bits = [act.type, act.detail]
        if act.params.get("dx"):
            bits.append(f"dx={act.params['dx']}")
        if extra.get("duration_s"):
            bits.append(f"持续{float(extra['duration_s']):.2f}s")
        return " ".join(str(b) for b in bits)
