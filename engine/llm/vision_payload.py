# llm/vision_payload.py —— 帧序列+ROI 组包、视觉 token 预算控制
# 铁律：关键帧≤8、ROI≤3；超预算优先丢弃最旧关键帧。帧带 t_ms 时间戳。

import os
import base64

from ms_types import FrameSegment


def _read_b64(path: str) -> str | None:
    """读图转 base64；不存在/不可读返回 None（合成模式常无真图）。"""
    try:
        if path and os.path.exists(path):
            with open(path, "rb") as f:
                return base64.b64encode(f.read()).decode("ascii")
    except Exception:
        return None
    return None


def build_vision_payload(seg: FrameSegment, max_frames: int = 8, max_rois: int = 3):
    """把 FrameSegment 组包成 (images_b64_list, timestamp_text)。
    - 严格限制 frames<=max_frames, rois<=max_rois（丢最旧）。
    - 返回供 MLLM 消费的附带时间戳文本与图像列表。"""
    frames = list(seg.frames)
    rois = list(seg.rois)
    # 预算裁剪：超出丢最旧（保留最新）
    if len(frames) > max_frames:
        frames = frames[-max_frames:]
    if len(rois) > max_rois:
        rois = rois[-max_rois:]

    images = []
    ts_lines = []
    for i, fr in enumerate(frames):
        b64 = _read_b64(fr.image_path)
        if b64:
            images.append(b64)
        tag = "ROI" if fr.roi else "FULL"
        ts_lines.append(f"[帧{i} t={fr.t_ms}ms {tag}]")
    for j, ro in enumerate(rois):
        b64 = _read_b64(ro.image_path)
        if b64:
            images.append(b64)
        ts_lines.append(f"[ROI{j} t={ro.t_ms}ms 区域={ro.roi}]")

    timestamp_text = "帧序列时间戳（绝对时间，单位毫秒）：\n" + "\n".join(ts_lines) if ts_lines \
        else "（合成测试帧模式：无真实图像，仅时间戳文本）"
    return images, timestamp_text


def estimate_visual_tokens(image_count: int, max_px: int = 1024) -> int:
    """粗略估算视觉 token：单帧按分辨率系数估。"""
    # 经验：1024px 长边约 800 token；按比例缩放
    factor = min(1.0, max_px / 1024.0)
    return int(image_count * 800 * factor)
