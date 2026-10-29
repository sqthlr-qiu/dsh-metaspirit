# perception/keyframe_sampler.py —— 关键帧采样器（时间/变化/事件三规则 → 帧序列段）
# 帧带 t_ms 时间戳：帧间隔即时间语义，配合 P3 提示词让模型在帧序列上做绝对时间对齐。

import os
import time

from ms_types import Frame, FrameSegment


class KeyframeSampler:
    """把连续帧按三规则筛成 FrameSegment（4-8 全屏关键帧 + 0-3 ROI 裁剪组）。"""

    def __init__(self):
        self.last_sampled_t = 0

    def build_segment(self, frames: list, plan, roi_targets: list | None = None,
                      events: list | None = None) -> FrameSegment:
        """三规则：①时间规则（按 keyframe_interval_ms 取）；②变化规则（由调用方先验帧差判定后传入 events）；
        ③事件规则（events 非空，最近帧强制入段）。
        这里合成模式下 frames 已是占位帧；用时间间隔与事件触发决定关键帧。
        注：帧差阈值(frame_diff_threshold)由配置驱动、在抓取层完成，不在本采样器内重复存储。"""
        roi_targets = roi_targets or (plan.roi_targets if plan else [])
        interval = plan.keyframe_interval_ms if plan else 1000
        now = int(time.time() * 1000)

        keyframes: list = []
        last_t = None
        for fr in frames:
            if last_t is None:
                keyframes.append(fr)
                last_t = fr.t_ms
            else:
                dt = fr.t_ms - last_t
                if dt >= interval or len(keyframes) == 0:
                    keyframes.append(fr)
                    last_t = fr.t_ms
        # 事件规则：若有事件且最近帧不在段内，补入
        if events:
            if frames:
                keyframes.append(frames[-1])
        # 强制标记 is_keyframe
        for fr in keyframes:
            fr.is_keyframe = True
        # 限制 4-8 帧：不足补，超出丢最旧
        if len(keyframes) < 4 and frames:
            keyframes = (frames[-4:]) if len(frames) >= 4 else frames
            for fr in keyframes:
                fr.is_keyframe = True
        if len(keyframes) > 8:
            keyframes = keyframes[-8:]

        # ROI 裁剪组（合成模式造占位 ROI 帧）
        rois: list = []
        roi_dir = os.path.join("frames_cache", "roi")
        os.makedirs(roi_dir, exist_ok=True)
        for i, target in enumerate((roi_targets or [])[:3]):
            t_ms = int(time.time() * 1000)
            path = os.path.join(roi_dir, f"roi_{target}_{t_ms}.png")
            try:
                import base64
                with open(path, "wb") as f:
                    f.write(base64.b64decode(
                        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAAC0lEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="))
            except Exception:
                path = ""
            rois.append(Frame(image_path=path, t_ms=t_ms, is_keyframe=False, roi=target))

        span = (keyframes[0].t_ms, keyframes[-1].t_ms) if keyframes else (now, now)
        return FrameSegment(frames=keyframes, rois=rois, span_t_ms=span)
