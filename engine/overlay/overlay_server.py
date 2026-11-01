# overlay/overlay_server.py —— 悬浮窗后端 Overlay HTTP 服务（纯标准库）
# threading + http.server.ThreadingHTTPServer；默认 127.0.0.1:8765，可配置。
# 路由：
#   GET  /               聊天+监控页面
#   GET  /api/state      JSON 引擎状态
#   GET  /api/screen     最新屏幕帧 PNG（纯标准库编码）
#   POST /api/chat       body {text} → 调 engine.user_say → {reply, thought, action}
#   GET  /api/events     最近事件（决策/反思/感知/对话摘要）
#   POST /api/control    body {action:stop} 停止引擎会话

import json
import secrets
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from overlay.overlay_page import render_page, make_screen_png


class OverlayServer:
    """后台线程托管的 Overlay HTTP 服务；持有一个 Engine 引用。
    安全（v1.4.1）：启动时生成随机 token——POST /api/chat、/api/control 必须携带
    header X-MetaSpirit-Token（同源页面由服务端注入 token，跨 Origin 网页无法获得）；
    GET 只读接口（state/screen/events）开放。CORS 不再对 POST 放行 *。"""

    def __init__(self, engine, host: str = "127.0.0.1", port: int = 8765):
        self.engine = engine
        self.host = host
        self.port = port
        self.token: str = secrets.token_urlsafe(16)
        self._httpd = None
        self._thread = None

    def is_running(self) -> bool:
        return self._httpd is not None

    def start(self) -> None:
        if self._httpd is not None:
            return
        handler = self._make_handler()
        self._httpd = ThreadingHTTPServer((self.host, self.port), handler)
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        if self._httpd is not None:
            try:
                self._httpd.shutdown()
            except Exception:
                pass
            try:
                self._httpd.server_close()
            except Exception:
                pass
            self._httpd = None
            self._thread = None

    # ------------------------------------------------------------------
    def _make_handler(self):
        engine = self.engine
        server = self

        class _Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass  # 静默访问日志

            def _authed(self) -> bool:
                return (self.headers.get("X-MetaSpirit-Token", "") == server.token
                        or f"token={server.token}" in (self.path or ""))

            def _send(self, code, body, ctype="application/json; charset=utf-8",
                      cors: bool = False):
                if isinstance(body, str):
                    body = body.encode("utf-8")
                try:
                    self.send_response(code)
                    self.send_header("Content-Type", ctype)
                    self.send_header("Content-Length", str(len(body)))
                    if cors:
                        # 只读 GET 允许跨域读取；POST 不放行（鉴权 + 无 CORS 双保险）
                        self.send_header("Access-Control-Allow-Origin", "*")
                    self.end_headers()
                    self.wfile.write(body)
                except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
                    # 客户端提前断开（如页面关闭/测试结束），静默忽略
                    pass

            def do_GET(self):
                path = self.path.split("?")[0]
                try:
                    if path in ("/", "/index.html"):
                        self._send(200, render_page("embed=1" in self.path,
                                                    token=server.token),
                                   "text/html; charset=utf-8")
                    elif path == "/api/state":
                        self._send(200, json.dumps(engine.overlay_state(), ensure_ascii=False),
                                   cors=True)
                    elif path == "/api/screen":
                        png = engine.overlay_screen_png()
                        self._send(200, png, "image/png", cors=True)
                    elif path == "/api/events":
                        self._send(200, json.dumps({"events": engine.recent_events()},
                                                  ensure_ascii=False), cors=True)
                    else:
                        self._send(404, json.dumps({"error": "not found"}))
                except Exception as e:
                    self._send(500, json.dumps({"error": str(e)}))

            def do_POST(self):
                path = self.path.split("?")[0]
                if not self._authed():
                    self._send(401, json.dumps({"error": "unauthorized: 缺少/错误的 X-MetaSpirit-Token"}))
                    return
                try:
                    length = int(self.headers.get("Content-Length", 0) or 0)
                    raw = self.rfile.read(length) if length else b"{}"
                    try:
                        data = json.loads(raw.decode("utf-8") or "{}")
                    except Exception:
                        data = {}
                    if path == "/api/chat":
                        text = (data.get("text") or "").strip()
                        if not text:
                            self._send(400, json.dumps({"error": "empty text"}))
                            return
                        try:
                            res = engine.user_say_sync(text)
                        except Exception as e:
                            self._send(500, json.dumps({"error": str(e)}))
                            return
                        self._send(200, json.dumps(res, ensure_ascii=False))
                    elif path == "/api/control":
                        action = (data.get("action") or "")
                        if action == "stop":
                            try:
                                engine.request_stop()
                            except Exception:
                                pass
                        self._send(200, json.dumps({"ok": True, "action": action}))
                    else:
                        self._send(404, json.dumps({"error": "not found"}))
                except Exception as e:
                    self._send(500, json.dumps({"error": str(e)}))

        return _Handler
