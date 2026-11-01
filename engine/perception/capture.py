# perception/capture.py —— 屏幕捕获（v1.4.1 修复：真实抓帧接线）
# 后端优先级：dxcam → mss → PIL.ImageGrab → 合成占位帧（无任何真实后端时的兜底）。
# 真实帧：长边降采样 ≤1024px（有 PIL 用 LANCZOS，无 PIL 用步进抽点）；
#         尽力按 window_title 定位游戏窗口裁剪（pygetwindow 可选）。
# 帧缓存清理：只保留最近 N=40 张 PNG，防止 frames_cache 无限增长。
# 另提供 snapshot()：16x16 灰度签名，供执行器做"真实执行后的帧差观察"。

import os
import time
import zlib
import struct

from ms_types import Frame

_CACHE_KEEP = 40
_MAX_EDGE = 1024


def _png_encode(rgb: bytes, w: int, h: int) -> bytes:
    """纯标准库 PNG 编码（8bit RGB，无滤波）。"""
    def chunk(tag: bytes, data: bytes) -> bytes:
        c = struct.pack(">I", len(data)) + tag + data
        return c + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
    raw = b"".join(b"\x00" + rgb[y * w * 3:(y + 1) * w * 3] for y in range(h))
    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 6))
            + chunk(b"IEND", b""))


def _downsample(rgb: bytes, w: int, h: int, max_edge: int = _MAX_EDGE):
    """降采样：有 PIL 用 LANCZOS；否则按整数步进抽点。返回 (rgb, w, h)。"""
    long_edge = max(w, h)
    if long_edge <= max_edge:
        return rgb, w, h
    try:
        from PIL import Image  # type: ignore
        import io as _io
        im = Image.new("RGB", (w, h))
        im.putdata(list(zip(rgb[0::3], rgb[1::3], rgb[2::3])))
        scale = max_edge / long_edge
        im2 = im.resize((max(1, int(w * scale)), max(1, int(h * scale))), Image.LANCZOS)
        return im2.tobytes(), im2.width, im2.height
    except ImportError:
        step = (long_edge + max_edge - 1) // max_edge  # 整数步进
        nw, nh = max(1, w // step), max(1, h // step)
        out = bytearray(nw * nh * 3)
        for y in range(nh):
            sy = min(h - 1, y * step) * w * 3
            dy = y * nw * 3
            for x in range(nw):
                sx = min(w - 1, x * step) * 3
                out[dy + x * 3:dy + x * 3 + 3] = rgb[sy + sx:sy + sx + 3]
        return bytes(out), nw, nh


class Capture:
    """帧捕获器。synthetic=True 生成占位帧；False 时尝试真实后端（失败自动回落占位）。"""

    def __init__(self, cache_dir: str = "frames_cache", synthetic: bool = True,
                 window_title: str = ""):
        self.synthetic = synthetic
        self.window_title = window_title
        self.cache_dir = cache_dir
        os.makedirs(cache_dir, exist_ok=True)
        self.backend_name = "synthetic"
        self._backend = None
        self._dxcam = None
        self._mss = None
        self._window_box = None
        self._placeholder_path = ""
        if not synthetic:
            self._backend = self._try_real_backend()
            if self._backend:
                self.backend_name = self._backend[0]
                self._window_box = self._find_window_box()
        else:
            # r3：synthetic 模式只在初始化时写一次占位 PNG，grab_batch 复用同一文件——
            # 每拍写 6 个新文件会触发 Windows Defender 实时扫描（实测拖慢感知拍 1.5-4.5s，
            # 破坏时间轴流水线的重叠语义）
            self._placeholder_path = os.path.join(self.cache_dir, "frame_placeholder.png")
            try:
                import base64
                with open(self._placeholder_path, "wb") as f:
                    f.write(base64.b64decode(
                        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAAC0lEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="))
            except Exception:
                self._placeholder_path = ""

    def _try_real_backend(self):
        try:
            import dxcam  # type: ignore
            self._dxcam = dxcam.create(output_color="RGB")
            return ("dxcam", dxcam)
        except Exception:
            self._dxcam = None
        try:
            import mss  # type: ignore
            return ("mss", mss)
        except ImportError:
            pass
        try:
            from PIL import ImageGrab  # type: ignore
            return ("imagegrab", ImageGrab)
        except ImportError:
            return None
    def _find_window_box(self):
        """尽力定位窗口（pygetwindow 可选）：返回 (l, t, r, b) 或 None（全屏）。"""
        if not self.window_title:
            return None
        try:
            import pygetwindow as gw  # type: ignore
            wins = gw.getWindowsWithTitle(self.window_title)
            if wins:
                w = wins[0]
                if w.width > 100 and w.height > 100:
                    return (max(0, w.left), max(0, w.top),
                            w.left + w.width, w.top + w.height)
        except Exception:
            pass
        return None

    # —— 真实抓帧：返回 (rgb_bytes, w, h) 或 None ——
    def _grab_rgb(self):
        box = self._window_box
        try:
            if self.backend_name == "dxcam" and self._dxcam is not None:
                region = None
                if box:
                    region = (box[0], box[1], box[2], box[3])
                frame = self._dxcam.grab(region=region)
                if frame is None:
                    return None
                h, w = frame.shape[0], frame.shape[1]
                return frame.tobytes(), w, h  # numpy RGB (H,W,3)
            if self.backend_name == "mss":
                import mss as _mss
                with _mss.mss() as sct:
                    if box:
                        mon = {"left": box[0], "top": box[1],
                               "width": box[2] - box[0], "height": box[3] - box[1]}
                    else:
                        mon = sct.monitors[1]
                    shot = sct.grab(mon)
                    w, h, raw = shot.width, shot.height, shot.rgb  # BGRA→已转 RGB
                    return bytes(raw), w, h
            if self.backend_name == "imagegrab":
                IG = self._backend[1]
                im = IG.grab(bbox=box).convert("RGB")
                return im.tobytes(), im.width, im.height
        except Exception:
            return None
        return None

    def _write_png(self, rgb: bytes, w: int, h: int) -> str:
        rgb2, w2, h2 = _downsample(rgb, w, h)
        path = os.path.join(self.cache_dir, f"frame_{int(time.time()*1000)}.png")
        try:
            with open(path, "wb") as f:
                f.write(_png_encode(rgb2, w2, h2))
            return path
        except Exception:
            return ""

    def _placeholder_png(self) -> str:
        import base64
        path = os.path.join(self.cache_dir, f"frame_{int(time.time()*1000)}_s.png")
        try:
            with open(path, "wb") as f:
                f.write(base64.b64decode(
                    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAAC0lEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="))
            return path
        except Exception:
            return ""

    def grab_batch(self, fps: int = 10, n: int = 6) -> list:
        """采集 n 帧。真实后端逐帧真实抓取+落 PNG；合成模式复用初始化时写好的占位文件（零写盘）。"""
        frames = []
        real = (self.backend_name != "synthetic")
        for i in range(n):
            t_ms = int(time.time() * 1000)
            if real:
                got = self._grab_rgb()
                if got:
                    path = self._write_png(*got)
                else:  # 后端单次失败回落占位（不中断感知）
                    path = self._placeholder_png()
            else:
                path = self._placeholder_path
            frames.append(Frame(image_path=path, t_ms=t_ms, is_keyframe=False))
            if real and i < n - 1:
                time.sleep(1.0 / max(1, fps))
        self._prune_cache()
        return frames

    def _prune_cache(self):
        """帧缓存清理：主目录与 roi/ 子目录都只保留最近 _CACHE_KEEP 张（r3 补 roi）。
        排序用文件名内嵌时间戳（frame_<t_ms>.png / roi_<target>_<t_ms>.png），
        不调 os.path.getmtime——刚写完的新文件首次 stat 会触发 Defender 实时扫描
        （实测每个新文件数百 ms，曾拖慢感知拍 1.5-4.5s）。"""
        import re
        _ts_re = re.compile(r"(\d{10,})")
        dirs = [self.cache_dir, os.path.join(self.cache_dir, "roi")]
        for d in dirs:
            try:
                items = []
                for f in os.listdir(d):
                    if not f.endswith(".png"):
                        continue
                    m = _ts_re.findall(f)
                    if not m:
                        continue  # 无时间戳文件（如 frame_placeholder.png）不参与清理
                    items.append((int(m[-1]), os.path.join(d, f)))
                items.sort(key=lambda x: x[0])
                for _, p in items[:-_CACHE_KEEP]:
                    try:
                        os.remove(p)
                    except OSError:
                        pass
            except Exception:
                pass

    def snapshot(self) -> tuple | None:
        """低分辨率灰度签名（16x16），供执行器做帧差观察。返回 (sig_bytes, w, h)。"""
        if self.backend_name == "synthetic":
            return None
        got = self._grab_rgb()
        if not got:
            return None
        rgb, w, h = _downsample(*got, max_edge=16)
        # RGB → 灰度
        gray = bytes((rgb[i] * 299 + rgb[i + 1] * 587 + rgb[i + 2] * 114) // 1000
                     for i in range(0, len(rgb), 3))
        return (gray, len(gray), max(1, len(gray) // max(w, 1) or 1))

    @staticmethod
    def diff_ratio(before, after) -> float:
        """两个 snapshot 的差异率（0=无变化）。任一为 None 返回 -1（不可观测）。"""
        if before is None or after is None:
            return -1.0
        a, b = before[0], after[0]
        n = min(len(a), len(b))
        if n == 0:
            return -1.0
        diff = sum(1 for i in range(n) if abs(a[i] - b[i]) > 12)
        return diff / n

    def close(self):
        try:
            if self._dxcam is not None:
                self._dxcam.release()
        except Exception:
            pass
        self._dxcam = None
