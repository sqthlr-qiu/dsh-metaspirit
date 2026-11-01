# rpc_server.py —— JSON-RPC over stdio（方法=9个工具，供 DSH 插件壳调用）
# 协议：每行一个 JSON-RPC 2.0 请求；响应同样是单行 JSON。方法对应 DSH 9 工具。
# v1.4.1 修复：①常驻事件循环（asyncio 任务如时间轴 create_task 不再被每请求的新 loop 取消）
#             ②新增 plan / pipeline_start / pipeline_stop —— 时间轴流水线在 DSH 插件形态可达。

import os
import sys
import json
import threading
import asyncio

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config_loader import load_config
from llm.provider import build_provider, BudgetMeter
from main import Engine
from decision.reward_compiler import static_audit

RPC_TIMEOUT_S = float(os.environ.get("METASPIRIT_RPC_TIMEOUT", "120"))


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
        self.loop: asyncio.AbstractEventLoop | None = None
        self._pipeline_task: asyncio.Task | None = None
        self.methods = {
            "ping": self.m_ping,
            "start": self.m_start,
            "perceive": self.m_perceive,
            "act": self.m_act,
            "plan": self.m_plan,                     # v1.4.1：单拍时间轴规划
            "pipeline_start": self.m_pipeline_start, # v1.4.1：流水线主循环启动
            "pipeline_stop": self.m_pipeline_stop,   # v1.4.1：流水线停止+会话统计
            "pipeline_status": self.m_pipeline_status,  # r3：流水线状态查询
            "reflect": self.m_reflect,
            "memory_search": self.m_memory_search,
            "skills": self.m_skills,
            "goals": self.m_goals,
            "stop": self.m_stop,
            "chat": self.m_chat,
            "overlay": self.m_overlay,
        }

    async def dispatch(self, method: str, params: dict):
        fn = self.methods.get(method)
        if fn is None:
            raise ValueError(f"未知方法: {method}")
        return await fn(params or {})

    # —— 工具方法 ——
    async def m_ping(self, p):
        return {"pong": True, "mock": getattr(self.engine.provider, "is_mock", False),
                "methods": sorted(k for k in self.methods if k != "ping")}

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

    async def m_plan(self, p):
        """单拍时间轴流水线决策：感知 → plan_once（七段决策日志 + 提交时间轴）。
        v1.4.1 把时间轴暴露进 DSH：Agent 用 metaspirit_plan 驱动核心流水线。"""
        ws = self.engine.latest_ws
        if ws is None or p.get("fresh"):
            ws = await self.engine.sense()
        block = await self.engine.plan_once(ws, user_message=p.get("user_message", ""))
        return {"block": block, "timeline_status": self.engine.timeline.prev_status_text()}

    async def m_pipeline_start(self, p):
        """启动时间轴流水线主循环（常驻 loop 上的 task；n_cycles 缺省=持续跑到 pipeline_stop）。"""
        if self.loop is None:
            raise RuntimeError("pipeline 只能在 serve() 常驻 loop 内启动（--selftest 直接 dispatch 除外）")
        if self._pipeline_task and not self._pipeline_task.done():
            return {"started": False, "error": "pipeline_already_running"}
        n_cycles = p.get("n_cycles")
        scenes = p.get("scenes")
        interval = p.get("plan_interval")
        self.engine._stop_requested = False
        self._pipeline_task = self.loop.create_task(
            self.engine.run_pipeline(n_cycles=n_cycles, scenes=scenes, plan_interval=interval))
        return {"started": True, "n_cycles": n_cycles if n_cycles is not None else "unlimited"}

    async def m_pipeline_stop(self, p):
        """停止流水线主循环并返回会话统计。"""
        self.engine.request_stop()
        task = self._pipeline_task
        if task and not task.done():
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=10)
            except Exception:
                task.cancel()
        self._pipeline_task = None
        return {"stopped": True, "summary": self.engine.stop()}

    async def m_pipeline_status(self, p):
        """流水线状态查询（r3：供 metaspirit_pipeline 工具 status 用）。"""
        task = self._pipeline_task
        running = bool(task and not task.done())
        return {"running": running,
                "decision_cycle": self.engine.decision_cycle,
                "overlap_cycles": self.engine.overlap_cycles,
                "timelines": len(self.engine.timeline.history) + (1 if self.engine.timeline.current else 0),
                "supersedes": len(self.engine.timeline.supersede_events),
                "latest_thought": self.engine.latest_thought[:60]}

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

    async def m_chat(self, p):
        """悬浮窗对话：把用户消息作为指令注入决策，返回元灵回复。"""
        text = (p.get("text") or "").strip()
        if not text:
            return {"reply": "", "thought": "", "action": {}, "error": "empty text"}
        return await self.engine.user_say(text)

    async def m_overlay(self, p):
        """悬浮窗控制：action=show 启动 / hide 停止 / status 查询。"""
        action = (p.get("action") or "status").lower()
        if action == "show":
            return self.engine.start_overlay()
        if action == "hide":
            return self.engine.stop_overlay()
        return self.engine.overlay_status()

    # —— IO 循环（v1.4.1：常驻事件循环）——
    def serve(self):
        """常驻 loop + run_coroutine_threadsafe：请求处理在同一个事件循环上执行，
        时间轴 create_task 等 asyncio 任务跨请求存活（修复 D1 每请求新建 loop 导致任务被取消）。"""
        self.loop = asyncio.new_event_loop()
        t = threading.Thread(target=self.loop.run_forever, daemon=True)
        t.start()
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
                fut = asyncio.run_coroutine_threadsafe(self.dispatch(method, params), self.loop)
                result = fut.result(timeout=RPC_TIMEOUT_S)
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
    # chat（悬浮窗对话）→ 返回元灵回复
    r = await srv.dispatch("chat", {"text": "去西边看看"})
    assert r.get("reply"), "chat 未返回回复"
    # overlay（默认未启动 → status.running=false）
    r = await srv.dispatch("overlay", {"action": "status"})
    assert r.get("running") is False and "url" in r, "overlay status 异常"
    r = await srv.dispatch("overlay", {"action": "show"})
    assert r.get("running") is True, "overlay 启动失败"
    r = await srv.dispatch("overlay", {"action": "hide"})
    assert r.get("running") is False, "overlay 停止失败"
    # —— v1.4.1：时间轴流水线经 RPC 可达 ——
    srv.loop = asyncio.get_running_loop()
    r = await srv.dispatch("plan", {"fresh": True})
    assert r.get("block", {}).get("tl_id"), "plan 失败：未产出时间轴"
    r = await srv.dispatch("pipeline_start", {"n_cycles": 2, "plan_interval": 0.25})
    assert r.get("started") is True, "pipeline_start 失败"
    await asyncio.sleep(2.5)  # 等 2 拍流水线跑完
    r = await srv.dispatch("pipeline_stop", {})
    assert r.get("stopped") is True and "summary" in r, "pipeline_stop 失败"
    # r3：pipeline_status
    r = await srv.dispatch("pipeline_status", {})
    assert r.get("running") is False and "decision_cycle" in r, "pipeline_status 失败"
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
