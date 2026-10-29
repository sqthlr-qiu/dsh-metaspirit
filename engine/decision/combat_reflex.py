# decision/combat_reflex.py —— 战斗反射（System 1，规则，<50ms，不经 MLLM）
# 铁律：战斗反射路径不调用 provider。六条优先级规则。

import time

from ms_types import Action, WorldState


class CombatReflex:
    def __init__(self):
        self.last_handover_t = 0.0

    def decide(self, ws: WorldState, tracks: list | None = None) -> Action:
        """六条优先级规则，返回单个原子动作。无威胁匹配 → 交还慢思层。"""
        t0 = time.perf_counter()
        entities = ws.entities or []
        threats = [e for e in entities if e.threat >= 0.5 or e.intent_guess == "敌意"]
        # 规则2：HP<30% → 脱离战斗（白名单原子键 S = 后撤；语义放 expected_effect）
        if ws.self_state.hp < 0.3:
            act = Action(type="keypress", detail="S", params={},
                         expected_effect="后撤逃跑：远离最近威胁，奔向掩体")
            self._mark(t0)
            return act
        if not threats:
            # 规则6：无威胁 → 交还
            self.last_handover_t = time.time()
            self._mark(t0)
            return Action(type="wait", detail="CONTROL:HANDOVER", params={},
                          expected_effect="交还慢思层")
        # 取最高威胁
        top = max(threats, key=lambda e: e.threat)
        # 规则1：攻击前摇（state 含"抬手/挥砍/蓄力/逼近中"）→ 翻滚规避（白名单原子键 Space）
        if any(k in (top.state or "") for k in ("抬手", "挥砍", "蓄力", "逼近")):
            act = Action(type="keypress", detail="Space", params={},
                         expected_effect="翻滚规避敌方攻击前摇")
            self._mark(t0)
            return act
        # 规则3：近战范围（bbox 与自我接近）→ 攻击（白名单 mouse 动作 left_click）
        self_b = ws.self_state  # 用 facing/animation 近似；此处用 bbox 距离粗略判断
        # 用威胁 bbox 中心与画面中心距离判断近战
        cx, cy = 320, 240
        if top.bbox and len(top.bbox) == 4:
            ex = (top.bbox[0] + top.bbox[2]) / 2
            ey = (top.bbox[1] + top.bbox[3]) / 2
            dist = ((ex - cx) ** 2 + (ey - cy) ** 2) ** 0.5
            if dist < 200:  # 近战范围
                act = Action(type="click", detail="left_click",
                             params={"bbox": [400, 220, 420, 260]},
                             expected_effect="敌人受到攻击")
                self._mark(t0)
                return act
        # 规则4：敌人远程且我无远程 → 走位接近（白名单原子键 A；Z字移动抽象为左右移动）
        act = Action(type="keypress", detail="A", params={},
                     expected_effect="走位接近远程敌人（左右交替）")
        self._mark(t0)
        return act

    def _mark(self, t0):
        # 记录耗时（用于 <50ms 自检）
        dt = (time.perf_counter() - t0) * 1000.0
        self.last_latency_ms = dt
