# decision/reward_compiler.py —— P5：奖励函数代码生成 + 静态审计 + 受限执行
# 铁律：禁止 import os/sys/socket；用 exec + 受限 globals（不依赖 RestrictedPython）。

import re

from llm.json_utils import call_with_retry

FORBIDDEN = ["import os", "import sys", "import socket", "import subprocess",
             "import shutil", "__import__", "eval(", "exec(", "open(", "compile(",
             "__builtins__", "globals(", "locals(", "breakpoint(",
             "__class__", "__subclasses__", "__bases__", "__mro__",
             "__globals__", "__code__", "__closure__", "__dict__",
             "__getattribute__", "__builtins__"]

# 双下划线 dunder 逃逸：禁止一切 .__xxx__ 访问（如 ().__class__.__subclasses__()）
_DUNDER_RE = re.compile(r"\.__\w+__\b")


def static_audit(code: str) -> list:
    """静态审计：返回违规项列表（空=通过）。"""
    problems = []
    for token in FORBIDDEN:
        if token in code:
            problems.append(f"forbidden:{token}")
    # 双下划线 dunder 逃逸拦截（沙盒越权）
    if _DUNDER_RE.search(code):
        problems.append("forbidden:dunder")
    # 禁止任何 import 语句（沙盒内不允许导入）
    if re.search(r"^\s*import\s+", code, re.MULTILINE) or re.search(r"^\s*from\s+\w+\s+import", code, re.MULTILINE):
        problems.append("forbidden:any_import")
    # 必须有 reward 函数定义
    if not re.search(r"def\s+reward\s*\(", code):
        problems.append("missing:def reward")
    return problems


def _safe_globals():
    """受限 globals：只暴露安全内置子集，无文件/网络/导入能力。"""
    safe = {
        "abs": abs, "min": min, "max": max, "len": len, "sum": sum,
        "float": float, "int": int, "str": str, "bool": bool,
        "dict": dict, "list": list, "tuple": tuple, "range": range,
        "True": True, "False": False, "None": None,
    }
    return {"__builtins__": safe}


def compile_and_test(code: str, sample=None):
    """静态审计 + 受限执行单测。返回 (ok: bool, error: str, fn_or_None)。"""
    problems = static_audit(code)
    if problems:
        return (False, ";".join(problems), None)
    gl = _safe_globals()
    try:
        exec(compile(code, "<reward>", "exec"), gl)  # noqa: S102 受限 globals
        fn = gl.get("reward")
        if fn is None:
            return (False, "missing:reward_after_exec", None)
        # 单测：默认样本（前后状态差）
        sb = (sample or {}).get("before", {"hp": 1.0, "inventory": []})
        sa = (sample or {}).get("after", {"hp": 1.0, "inventory": ["wood"]})
        act = (sample or {}).get("action", "wait")
        val = fn(sb, act, sa)
        float(val)  # 确保返回标量
        return (True, "", fn)
    except Exception as e:
        return (False, f"exec_error:{e}", None)


class RewardCompiler:
    def __init__(self, provider, cfg: dict):
        self.provider = provider
        self.cfg = cfg
        self.last_code: str | None = None
        self.last_fn = None

    async def compile_reward(self, goal_text: str, state_schema: str = "") -> tuple:
        """生成奖励代码；返回 (code, ok, error)。"""
        if getattr(self.provider, "is_mock", False):
            code = (await self.provider.complete(tier="deep", system="", user="", mode="reward"))[0]
        else:
            prompt = (
                "你是奖励设计器（Eureka 范式）。编写 reward(state_before, action, state_after)->float。\n"
                f"【目标】{goal_text}\n【可用状态字段】{state_schema or 'hp, hunger, position, inventory, threat_count'}\n"
                "只使用 state 字典中存在的字段，用 .get() 安全取值。输出 ```python 代码块```。"
            )

            async def _make(feedback):
                data, _ = await self.provider.complete(
                    tier="deep", system="", user=prompt + (feedback or ""),
                    expect_json=False, mode="reward")
                return data, data

            def _degrade():
                return ("def reward(state_before, action, state_after):\n    return 0.0\n",)
            data = await call_with_retry(_make, [], _degrade)
            code = data if isinstance(data, str) else str(data)
        ok, err, fn = compile_and_test(code)
        if ok:
            self.last_code = code
            self.last_fn = fn
        else:
            # 审计/执行失败：退回安全零奖励，保证不崩溃
            self.last_code = "def reward(state_before, action, state_after):\n    return 0.0\n"
            self.last_fn = None
        return (self.last_code, ok, err)

    def reward_of(self, state_before, action, state_after):
        if self.last_fn is None:
            return 0.0
        try:
            return float(self.last_fn(state_before, action, state_after))
        except Exception:
            return 0.0
