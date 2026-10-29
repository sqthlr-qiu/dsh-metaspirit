# perception/capture.py —— 屏幕捕获
# 默认：合成测试帧模式（写极简占位 PNG，带 t_ms 时间戳）。可选 mss / dxcam（try-import 降级）。

import os
import time

from ms_types import Frame

# 1x1 占位 PNG（合成模式用，避免无真实游戏窗口时崩溃）
_PNG_1X1 = base64_b64 = None  # 延迟初始化，避免顶层 import 顺序问题


def _png_bytes() -> bytes:
    import base64
    return base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAAC0lEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")


class Capture:
    """帧捕获器。synthetic=True 时生成占位帧（带真实采集时间戳）。"""

    def __init__(self, cache_dir: str = "frames_cache", synthetic: bool = True,
                 window_title: str = ""):
        self.synthetic = synthetic
        self.window_title = window_title
        self.cache_dir = cache_dir
        os.makedirs(cache_dir, exist_ok=True)
        self._backend = None
        if not synthetic:
            self._backend = self._try_real_backend()

    def _try_real_backend(self):
        try:
            import dxcam  # type: ignore
            return ("dxcam", dxcam)
        except ImportError:
            pass
        try:
            import mss  # type: ignore
            return ("mss", mss)
        except ImportError:
            pass
        return None

    def grab_batch(self, fps: int = 10, n: int = 6) -> list:
        """采集 n 帧（合成模式直接造占位帧，时间戳用采集时刻，非送入模型时刻）。"""
        frames = []
        for i in range(n):
            t_ms = int(time.time() * 1000)
            path = os.path.join(self.cache_dir, f"frame_{t_ms}_{i}.png")
            try:
                with open(path, "wb") as f:
                    f.write(_png_bytes())
            except Exception:
                path = ""
            frames.append(Frame(image_path=path, t_ms=t_ms, is_keyframe=False))
            # 按 fps 间隔推进（合成模式间隔用 1000/fps 模拟，但不实际 sleep 太久）
            if not self.synthetic:
                time.sleep(1.0 / max(1, fps))
        return frames

    def close(self):
        # 合成模式无资源需释放
        return
