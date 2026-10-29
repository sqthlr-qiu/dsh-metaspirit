# llm/json_utils.py —— JSON 修复 + schema 校验 + 失败重试一次再降级
# 铁律：所有 MLLM 输出强制 JSON + schema 校验，解析失败重试一次，再失败降级为保守动作(wait)。

import json
import re


class BudgetExceededError(Exception):
    """日预算熔断异常：当 BudgetMeter 判定当前 tier 不允许调用时抛出，
    由 call_with_retry 捕获并降级，避免上层误把空结果当正常输出。"""


def repair_json(text: str) -> str:
    """尽力把 LLM 输出修复成可解析的 JSON 字符串。
    策略：提取首个 { 到末个 } 之间的内容；修复尾随逗号；补引号等轻量处理。"""
    if text is None:
        return "{}"
    # 去掉 ```json ``` 代码围栏
    t = text.strip()
    if t.startswith("```"):
        t = re.sub(r"^```[a-zA-Z]*\s*", "", t)
        t = re.sub(r"\s*```$", "", t)
        t = t.strip()
    # 提取最外层 { } 或 [ ]
    start = min([i for i in (t.find("{"), t.find("[")) if i >= 0], default=-1)
    if start == -1:
        return "{}"
    open_ch, close_ch = ("{", "}") if t[start] == "{" else ("[", "]")
    depth = 0
    end = -1
    for i in range(start, len(t)):
        if t[i] == open_ch:
            depth += 1
        elif t[i] == close_ch:
            depth -= 1
            if depth == 0:
                end = i
                break
    if end == -1:
        end = len(t) - 1
    body = t[start:end + 1]
    # 修复尾随逗号： , 后紧跟 } 或 ]
    body = re.sub(r",\s*([}\]])", r"\1", body)
    # 修复缺少引号的键（简单 heuristic：把 字母数字_ 组成的裸键加引号）
    body = re.sub(r"([{,]\s*)([A-Za-z_][A-Za-z0-9_]*)\s*:", r'\1"\2":', body)
    return body


def safe_json_loads(text: str, default=None):
    """先直接解析，失败则用 repair_json 再解析；都失败返回 default。"""
    if default is None:
        default = {}
    try:
        return json.loads(text)
    except (json.JSONDecodeError, TypeError):
        try:
            return json.loads(repair_json(text))
        except Exception:
            return default


def validate_schema(data: dict, required_keys: list, optional_shapes: dict | None = None) -> list:
    """宽松 schema 校验：检查 required_keys 存在且非 None。返回缺失/异常字段列表（空=通过）。"""
    problems = []
    for k in required_keys:
        if k not in data or data[k] is None:
            problems.append(f"missing:{k}")
    return problems


async def call_with_retry(make_call, schema_required, degrade_fn, max_retry: int = 1):
    """通用调用：先尝试 make_call()（协程，返回 (parsed_dict, raw_text)），校验 schema；
    失败重试一次（把上一次错误贴回），仍失败则调用 degrade_fn() 返回保守结果。
    make_call 签名：async (error_feedback: str | None) -> (dict, raw)"""
    last_err = None
    for attempt in range(max_retry + 1):
        feedback = f"上一轮输出不符合 schema，问题：{last_err}。请只输出符合要求的 JSON。" if last_err else None
        try:
            data, raw = await make_call(feedback)
            data = data if isinstance(data, dict) else safe_json_loads(raw if raw else "{}")
            problems = validate_schema(data, schema_required)
            if not problems:
                return data
            last_err = ";".join(problems)
        except BudgetExceededError:
            # 预算熔断：不重试，直接降级（重试无效）
            return degrade_fn()
        except Exception as e:  # 解析/网络异常
            last_err = str(e)
    # 重试耗尽 -> 降级
    return degrade_fn()
