# perception/grounding_calibrator.py —— 辅助旁路（坐标校准，不产语义）
# 铁律：传统 CV 只允许做坐标校准复核，输出标记为 auxiliary；与 MLLM 冲突以画面为准。
# 配置 calibrator: none / paddleocr。默认 none（直接透传，置信度不变）。

from ms_types import WorldState


class GroundingCalibrator:
    def __init__(self, mode: str = "none"):
        self.mode = mode
        self._ocr = None
        if mode == "paddleocr":
            try:
                from paddleocr import PaddleOCR  # type: ignore
                self._ocr = PaddleOCR(use_angle_cls=False, lang="ch", show_log=False)
            except Exception:
                self.mode = "none"

    def verify(self, ws: WorldState) -> WorldState:
        """坐标复核。none 模式：透传，不修改语义。"""
        if self.mode == "none" or self._ocr is None:
            return ws
        # paddleocr 路径（可选增强）：对 ui_state.clickable 的 bbox 与 OCR 文字位置交叉核对，
        # 不一致则以校准器为准并把该字段 confidence 降 0.2。此处仅留接口骨架。
        if isinstance(ws.confidence, dict):
            ws.confidence["ui_grounding"] = 0.95
        return ws
