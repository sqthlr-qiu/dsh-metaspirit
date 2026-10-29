# rpc_server.py —— JSON-RPC over stdio（方法=8个工具，供 DSH 插件壳调用）
# 协议：每行一个 JSON-RPC 2.0 请求；响应同样是单行 JSON。方法对应 DSH 8 工具。

import os
import sys
import json
import asyncio

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config_loader import load_config
from llm.provider import build_provider, BudgetMeter
from main import Engine
from decision.reward_compiler import static_audit


def make_engine(mock: bool = True) -> Engine:
    here = os.path.dirname(os.path.abspath(__file__))
    cfg = load_config(os.path.join(here, "config", "agent.yaml"))
    if mock:
        os.environ["METASPIRIT_MOCK"] = "1"
        cfg["mock"] = True
    budget = BudgetMeter(cfg.get("budget", {}).get("daily_usd_cap", 10.0))
    provider = build_provider(cfg, "fast", budget)
    return Engine(cfg, provider, budget)


class RpcServer:
    def __init__(self, engine: Engine):
        self.engine = engine
        self.methods = {
            "ping": self.m_ping,
            "start": self.m_start,
            "perceive": self.m_perceive,
            "act": self.m_act,
            "reflect": self.m_reflect,
            "memory_search": self.m_memory_search,
            "skills": self.m_skills,
            "goals": self.m_goals,
            "stop": self.m_stop,
        }

    async def dispatch(self, method: str, params: dict):
        fn = self.methods.get(method)
        if fn is None:
            raise ValueError(f"未知方法: {method}")
        return await fn(params or {})

    # —— 8 工具方法 ——
    async def m_ping(self, p):
        return {"pong": True, "mock": getattr(self.engine.provider, "is_mock", False)}

    async def m_start(self, p):
        return await self.engine.start(
            game=p.get("game", "minecraft"),
            window_title=p.get("window_title", ""),
            background_text=p.get("background_text", ""),
            action_space_path=p.get("action_space_path", ""))

    async def m_perceive(self, p):
        return await self.engine.perceive(force_deep=bool(p.get("force_deep", False)))

    async def m_act(self, p):
        return await self.engine.act(p.get("action", {}), p.get("expected_effect", ""))

    async def m_reflect(self, p):
        return await self.engine.reflect(
            situation=p.get("situation", ""), attempted=p.get("attempted", ""),
            observed=p.get("observed", ""))

    async def m_memory_search(self, p):
        return {"results": self.engine.memory_search(p.get("query", ""), int(p.get("top_k", 5)))}

    async def m_skills(self, p):
        return self.engine.skills(mode=p.get("mode", "list"),
                                  skill_name=p.get("skill_name", ""),
                                  args=p.get("args", {}))

    async def m_goals(self, p):
        return self.engine.goals(mode=p.get("mode", "view"))

    async def m_stop(self, p):
        return self.engine.stop()

    # —— IO 循环 ——
    def serve(self):
        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            try:
                req = json.loads(line)
            except json.JSONDecodeError as e:
                self._write_error(None, -32700, f"解析错误: {e}")
                continue
            method = req.get("method")
            params = req.get("params", {})
            rid = req.get("id")
            try:
                result = asyncio.run(self.dispatch(method, params))
                self._write_result(rid, result)
            except Exception as e:
                self._write_error(rid, -32000, f"{type(e).__name__}: {e}")

    def _write_result(self, rid, result):
        sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": rid, "result": result},
                                    ensure_ascii=False) + "\n")
        sys.stdout.flush()

    def _write_error(self, rid, code, message):
        sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": rid,
                                     "error": {"code": code, "message": message}},
                                    ensure_ascii=False) + "\n")
        sys.stdout.flush()


async def _selftest():
    eng = make_engine(mock=True)
    srv = RpcServer(eng)
    # ping
    r = await srv.dispatch("ping", {})
    assert r.get("pong") is True, "ping 失败"
    # start
    r = await srv.dispatch("start", {"background_text": "测试世界"})
    assert r.get("session_id"), "start 失败"
    # perceive
    r = await srv.dispatch("perceive", {})
    assert "scene" in r, "perceive 失败"
    # act（合法）
    r = await srv.dispatch("act", {"action": {"type": "keypress", "detail": "W"}, "expected_effect": "前进"})
    assert r.get("rejected") is False, "合法动作被拒"
    # act（非法）
    r = await srv.dispatch("act", {"action": {"type": "hack"}, "expected_effect": "x"})
    assert r.get("rejected") is True, "非法动作未拦截"
    # reflect
    r = await srv.dispatch("reflect", {"situation": "s", "attempted": "a", "observed": "o"})
    assert "lesson" in r, "reflect 失败"
    # memory_search / skills / goals / stop
    await srv.dispatch("memory_search", {"query": "木材"})
    await srv.dispatch("skills", {"mode": "list"})
    await srv.dispatch("goals", {"mode": "view"})
    await srv.dispatch("stop", {})
    # 沙盒越权拦截：拒绝 dunder 逃逸（如 ().__class__.__subclasses__()）
    bad = "def reward(a, b, c):\n    return ().__class__.__subclasses__()\n"
    probs = static_audit(bad)
    assert any("forbidden:dunder" in p for p in probs), "奖励沙盒未拦截 dunder 逃逸"
    print("RPC SELFTEST PASS")


def main():
    if "--selftest" in sys.argv:
        asyncio.run(_selftest())
        return
    # 解析 METASPIRIT_MOCK：支持 "1" / "true" / "True" / "yes" / "on"（默认非 mock）
    mock_val = os.environ.get("METASPIRIT_MOCK", "").strip().lower()
    mock = mock_val in ("1", "true", "yes", "on")
    eng = make_engine(mock=mock)
    RpcServer(eng).serve()


if __name__ == "__main__":
    main()
