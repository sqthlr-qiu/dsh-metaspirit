# llm/provider.py —— 统一 MLLM API 接口
# 铁律：纯 API 路线，禁止 import torch/transformers/vllm/peft。
# 两档：fast=快档API(默认 deepseek-v4-flash) / deep=旗舰API(默认 deepseek-v4)，OpenAI 兼容接口。
# 预算记账 + 日预算熔断（超限降级仅 fast + 低频）。

import os
import json
import base64
import asyncio
import urllib.request
import urllib.error

from llm.json_utils import safe_json_loads, BudgetExceededError


# 视觉 token 估算：每帧按长边分辨率粗估；价格表（USD / 1K token）为经验默认值，可改。
_PRICE_PER_1K = {
    "deepseek-v4-flash": 0.0001,   # 占位价（快档便宜）
    "deepseek-v4": 0.0005,         # 占位价（旗舰档）
}
_VISUAL_TOKENS_PER_FRAME = 800     # 单帧视觉 token 经验估值（<=1024px）


class BudgetMeter:
    """API 成本记账与熔断。"""

    def __init__(self, daily_usd_cap: float = 10.0):
        self.daily_usd_cap = daily_usd_cap
        self.total_usd = 0.0
        self.total_tokens = 0
        self.calls = 0
        self.degraded = False  # 熔断后仅 fast + 低频

    def account(self, model: str, text_tokens: int, image_count: int):
        vtok = image_count * _VISUAL_TOKENS_PER_FRAME
        toks = text_tokens + vtok
        price = _PRICE_PER_1K.get(model, 0.0003)
        usd = (toks / 1000.0) * price
        self.total_usd += usd
        self.total_tokens += toks
        self.calls += 1
        if self.total_usd >= self.daily_usd_cap:
            self.degraded = True
        return usd

    def allow_tier(self, tier: str) -> bool:
        if not self.degraded:
            return True
        # 熔断：只允许 fast 档（deep 降级跳过），由调用方决定降频
        return tier == "fast"

    def status(self) -> dict:
        return {
            "total_usd": round(self.total_usd, 4),
            "total_tokens": self.total_tokens,
            "calls": self.calls,
            "daily_usd_cap": self.daily_usd_cap,
            "degraded": self.degraded,
        }


class MockProvider:
    """Mock 模式：不经网络，返回合规 JSON（构造"敌人接近→反射/攻击→反馈"小剧本）。"""

    def __init__(self):
        self.is_mock = True
        self.name = "mock"

    async def complete(self, tier: str, system: str, user: str,
                       images=None, expect_json: bool = True, mode: str = "chat"):
        """返回 (parsed_dict_or_str, raw_text)。"""
        if mode == "perceive":
            return self._mock_perceive(), ""
        if mode == "decision":
            return self._mock_decision(), ""
        if mode == "goal":
            return self._mock_goal(), ""
        if mode == "reflect":
            return self._mock_reflect(), ""
        if mode == "reward":
            return self._mock_reward_code(), ""  # 返回代码字符串
        if mode == "chat":
            return self._mock_chat(user), ""
        if mode == "timeline":
            return self._mock_timeline(user), ""
        return {"text": "mock ok"}, "mock ok"

    # —— 脚本化合规输出 ——
    def _mock_perceive(self) -> dict:
        # 默认探索场景的五路语义；战斗场景由 mllm_perceiver 脚本覆盖
        return {
            "scene": {"type": "explore", "urgency": 0.1},
            "self_check": {"bbox": [400, 300, 440, 360], "facing": "北",
                           "animation": "站立", "condition": "健康", "confidence": 0.9},
            "screen_text": {"dialog": "", "tutorial": "按 W 前进", "numbers": "HP 20/20"},
            "ui_state": {"active_panel": "游戏内", "clickable": []},
            "entities": [],
            "environment": {"location_guess": "出生点平原", "walkable_directions": ["前", "左", "右"],
                            "hazards": [], "resources": ["树木", "石头"]},
            "event_transitions": [{"t": "近帧", "event": "天色渐暗"}],
            "recent_change": "天色渐暗，夜晚将至",
            "uncertainty": [],
        }

    def _mock_decision(self) -> dict:
        return {
            "thought": "我前方有树木，按计划先采集木材。",
            "selected_goal": "收集木材x8",
            "action": {"type": "keypress", "detail": "W", "params": {}},
            "expected_effect": "前进了一段距离，靠近树木",
            "fallback": "若被阻挡，改为向左移动绕行",
        }

    def _mock_goal(self) -> dict:
        return {
            "survival_goals": [
                {"goal": "维持HP>50%", "why": "怪物与饥饿会造成伤害", "priority": 0}
            ],
            "current_goals": [
                {"goal": "在天黑前建造庇护所", "priority": 1,
                 "derived_from": "事件流：日落倒计时 + 背景：夜晚怪物刷新",
                 "subtasks": ["收集木材x8", "合成工作台", "找平地", "搭建并放置火把"],
                 "success_signal": "庇护所建成且我在其内部",
                 "abort_condition": "HP<30%时放弃并优先逃命"}
            ],
            "growth_goals": [
                {"goal": "探明地图西侧未探索区域", "priority": 3, "trigger": "无更高优先级任务时"}
            ],
            "no_go": ["不可攻击村民", "不可进入岩浆区"],
        }

    def _mock_reflect(self) -> dict:
        return {
            "outcome": "partial",
            "what_worked": ["靠近目标后按下前进键有效"],
            "what_failed": [{"mistake": "预期效果与实际不符", "consequence": "动作未达成目标",
                             "root_cause": "误判"}],
            "lesson": "在<靠近资源但被阻挡>时，应该<先向左移动绕行>，因为<正前方可能有障碍>",
            "to_memory": {"semantic": ["出生点附近树木可用于早期木材采集"], "skill_candidate": ""},
            "next_time": "下次遇到同样情况先微调走位再前进",
        }

    def _mock_chat(self, user: str) -> dict:
        """悬浮窗对话：人物卡口吻的中文回复（如'收到，我这就去看西边……'）。"""
        u = (user or "").strip() or "（沉默）"
        return {
            "reply": f"收到，{u}……我这就去办，先把周围情况看清楚。",
            "thought": "理解玩家指令，准备行动",
            "action": {"type": "wait", "detail": "wait", "params": {}},
            "expected_effect": "响应玩家指令",
        }

    def _mock_timeline(self, user: str) -> dict:
        """P13 时间轴规划 Mock（v1.4.1）：
        - 动作一律为抽象意图（dodge/camera_turn/move_left/stop_move…），不含具体按键；
        - payload 里 prev_timeline.pending 非空 → supersede 变体（附对上一条未执行部分的改进意见）；
        - payload 里 pending_reflections 非空 → 逐条回填反思落实反馈（反思有反馈）。"""
        import re as _re
        supersede = '"pending": []' not in (user or "")
        out = {
            "situation_analysis": "敌人比预估更近，上一条时间轴的节奏偏慢，需要收紧时间轴并加大转向幅度"
            if supersede else
            "前方有树木资源，天色渐暗；自身状态健康，紧迫度低，可按既定采集路线推进",
            "change_prev": "supersede" if supersede else "keep",
            "goal_progress": ("生存目标达成约 70%；本回合规避目标达成 30%" if supersede
                              else "当前目标（收集木材/维持生存）达成约 60%"),
            "reflection": ("上条时间轴 0.15s 的视角转动幅度不足（约 15 度），未覆盖侧翼；0.20s 变向偏晚"
                           if supersede else
                           "上一步前进有效靠近了目标；视角转动幅度略小，本次加大到 30 度"),
            "next_goal": ("在 0.5s 内脱离攻击/爆炸范围并让侧翼进入画面" if supersede
                          else "继续接近目标资源并保持对周边的观察"),
            "ops_plan": ("先闪避，再加大视角右转幅度并提前变向，最后停住确认安全" if supersede
                         else "保持前进，中途转动视角扫视侧翼，到位后停住观察"),
            "timeline": ([
                {"at": 0.10, "intent": "dodge", "params": {"duration_s": 0.10},
                 "expected": "翻滚闪避完成，脱离爆炸范围"},
                {"at": 0.15, "intent": "camera_turn",
                 "params": {"degrees": 30, "direction": "right", "duration_s": 0.10},
                 "expected": "视野右移 30 度，侧翼进入画面"},
                {"at": 0.20, "intent": "move_left", "params": {"duration_s": 0.14},
                 "expected": "移动方向改为向左，横向拉开距离"},
                {"at": 0.34, "intent": "stop_move", "params": {},
                 "expected": "停止移动，稳定视角观察"},
            ] if supersede else [
                {"at": 0.10, "intent": "move_forward", "params": {"duration_s": 0.30},
                 "expected": "前进一段距离，靠近目标"},
                {"at": 0.50, "intent": "camera_turn",
                 "params": {"degrees": 30, "direction": "left", "duration_s": 0.10},
                 "expected": "视野左移 30 度，侧翼进入画面"},
                {"at": 0.70, "intent": "move_forward", "params": {"duration_s": 0.30},
                 "expected": "继续前进接近资源"},
                {"at": 1.10, "intent": "stop_move", "params": {},
                 "expected": "停止移动，进入下一感知拍"},
            ]),
            "improvements_on_discarded": ([
                "上一条 0.15s 的视角转动幅度不足（约 15 度），本次改为 30 度并保持 0.1s",
                "上一条 0.20s 的变向偏晚，敌情比预估近，本次维持 0.20s 但改用横向位移",
                "上一条 0.34s 的停止保留，但停止前必须确认已脱离危险半径",
            ] if supersede else []),
            "reflection_feedback": [],
        }
        # 反思落实反馈：对 payload 里待评估的 rf-* 逐条给出 Mock 评估（applied + 证据）
        # 注意：只扫 payload 段（【输入】之后），避免把 P13 提示词里的示例 id（rf-3）误当待评估项
        payload_part = (user or "").split("【输入】", 1)[-1]
        ids = _re.findall(r'"id": "(rf-\d+)"', payload_part)
        if ids:
            out["reflection_feedback"] = [
                {"id": rid, "status": "applied",
                 "evidence": "本轮 0.15s 已按 30 度执行且侧翼进入画面，观察反馈与预期一致（Mock 评估）"}
                for rid in ids]
        return out

    def _mock_reward_code(self) -> str:
        return (
            "def reward(state_before: dict, action: str, state_after: dict) -> float:\n"
            "    r = 0.0\n"
            "    hp_b = state_before.get('hp', 1.0)\n"
            "    hp_a = state_after.get('hp', 1.0)\n"
            "    if hp_a < hp_b:\n"
            "        r -= 2.0 * (hp_b - hp_a)\n"
            "    inv_b = len(state_before.get('inventory', []))\n"
            "    inv_a = len(state_after.get('inventory', []))\n"
            "    if inv_a > inv_b:\n"
            "        r += 1.0 * (inv_a - inv_b)\n"
            "    if action in ('wait',):\n"
            "        r -= 0.1\n"
            "    return r\n"
        )


class OpenAICompatProvider:
    """OpenAI 兼容 REST 接口（urllib 直连；若可用则 try import openai SDK 优先）。"""

    def __init__(self, model: str, base_url: str, api_key: str,
                 budget: BudgetMeter | None = None, timeout: float = 60.0):
        self.is_mock = False
        self.name = "openai-compat"
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.budget = budget
        self.timeout = timeout
        self._sdk = None
        try:
            import openai  # type: ignore
            self._sdk = openai
        except ImportError:
            self._sdk = None

    async def complete(self, tier: str, system: str, user: str,
                       images=None, expect_json: bool = True, mode: str = "chat"):
        # 熔断检查：deep 档被降级时跳过（抛预算异常，由 call_with_retry 降级）
        if self.budget and not self.budget.allow_tier(tier):
            raise BudgetExceededError(
                f"日预算已熔断，tier={tier} 不允许调用")

        text_tokens = max(1, (len(system) + len(user)) // 4)
        image_count = len(images) if images else 0

        if expect_json:
            user = user + "\n\n请只输出可被 JSON 解析的结果，不要使用代码围栏。"

        if self._sdk is not None:
            data = await self._complete_sdk(system, user, images, expect_json)
        else:
            data = await self._complete_urllib(system, user, images, expect_json)

        # 记账
        if self.budget:
            self.budget.account(self.model, text_tokens, image_count)

        if mode == "reward":
            # 奖励代码以字符串返回（从 ```python 块或全文提取）
            return _extract_code(data if isinstance(data, str) else json.dumps(data)), data if isinstance(data, str) else ""
        if isinstance(data, str):
            return safe_json_loads(data), data
        return data, json.dumps(data)

    async def _complete_sdk(self, system, user, images, expect_json):
        import openai
        client = openai.OpenAI(api_key=self.api_key, base_url=self.base_url)
        content = []
        if images:
            for im in images:
                content.append({"type": "text", "text": user})
                content.append({"type": "image_url", "image_url": {
                    "url": f"data:image/png;base64,{im}"}})
            messages = [{"role": "system", "content": system},
                        {"role": "user", "content": content}]
        else:
            messages = [{"role": "system", "content": system},
                        {"role": "user", "content": user}]
        resp = await asyncio.to_thread(
            client.chat.completions.create, model=self.model, messages=messages,
            temperature=0.2, response_format={"type": "json_object"} if expect_json else None)
        return resp.choices[0].message.content or "{}"

    async def _complete_urllib(self, system, user, images, expect_json):
        content = []
        if images:
            for im in images:
                content.append({"type": "text", "text": user})
                content.append({"type": "image_url", "image_url": {
                    "url": f"data:image/png;base64,{im}"}})
            user_msg = content
        else:
            user_msg = user
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user_msg},
            ],
            "temperature": 0.2,
        }
        if expect_json:
            payload["response_format"] = {"type": "json_object"}
        req = urllib.request.Request(
            self.base_url + "/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json",
                     "Authorization": f"Bearer {self.api_key}"})

        def _urlopen_blocking() -> str:
            # r3：同步 urlopen 移入线程池——无 openai SDK 时的降级分支不再阻塞事件循环
            # （此前 ≤60s 的同步阻塞会挤压时间轴定时器，损害定时精度）
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return resp.read().decode("utf-8")

        try:
            body = await asyncio.to_thread(_urlopen_blocking)
            return json.loads(body)["choices"][0]["message"]["content"] or "{}"
        except urllib.error.URLError as e:
            raise RuntimeError(f"OpenAI 兼容接口调用失败: {e}")


def _extract_code(text: str) -> str:
    """从 LLM 输出中提取 python 代码块，否则原样返回。"""
    import re
    m = re.search(r"```python\s*(.*?)```", text, re.DOTALL)
    if m:
        return m.group(1).strip()
    return text.strip()


def build_provider(cfg: dict, tier: str = "fast", budget: BudgetMeter | None = None):
    """按配置构造 provider。环境变量 METASPIRIT_MOCK=1 或 cfg.mock=true 走 Mock。"""
    import os
    if os.environ.get("METASPIRIT_MOCK", "").strip().lower() in ("1", "true", "yes", "on") or cfg.get("mock"):
        return MockProvider()
    mllm = cfg.get("mllm", {})
    spec = mllm.get(tier, mllm.get("fast", {}))
    base_url = os.environ.get(spec.get("base_url_env", ""), "https://api.deepseek.com/v1")
    api_key = os.environ.get(spec.get("api_key_env", ""), "")
    model = spec.get("model", "deepseek-v4-flash")
    return OpenAICompatProvider(model=model, base_url=base_url, api_key=api_key, budget=budget)
