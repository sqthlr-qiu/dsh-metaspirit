# types.py —— 核心数据结构（按《AI实施规格书》第2节直接采用）
# 说明：本文件仅依赖标准库 dataclasses / enum / time，零第三方依赖。

from dataclasses import dataclass, field
from enum import Enum
import time


class SceneType(str, Enum):
    """场景类型枚举（同时是 str，方便 JSON 序列化与比较）。"""
    COMBAT = "combat"
    PUZZLE = "puzzle"
    STORY = "story"
    EXPLORE = "explore"
    MENU = "menu"
    LOADING = "loading"


@dataclass
class Frame:
    """单帧：落盘路径 + 捕获时间戳（毫秒，MLLM 绝对时间对齐用）。"""
    image_path: str                 # 落盘帧文件
    t_ms: int                       # 捕获时间戳（毫秒）
    is_keyframe: bool
    roi: str | None = None          # 若为 ROI 裁剪图，标注来源区域 "combat_threat"/"dialog"/"ui"


@dataclass
class FrameSegment:
    """MLLM 感知的最小视频单元：关键帧序列 + ROI 裁剪组。"""
    frames: list                    # 4-8 张全屏关键帧（带时间戳），元素为 Frame
    rois: list                      # 0-3 张高清裁剪图，元素为 Frame
    span_t_ms: tuple                # 时间跨度 (start_ms, end_ms)


@dataclass
class SelfState:
    """自我状态卡——"以操作的人物为自我"的数据载体。"""
    name: str = "agent"
    hp: float = 1.0                 # 归一化 0-1
    resources: dict = field(default_factory=dict)
    position: tuple | None = None   # 画面 bbox 中心（MLLM 输出）；桥接时为世界坐标
    facing: str = "unknown"
    animation: str = "unknown"
    equipment: list = field(default_factory=list)
    inventory: list = field(default_factory=list)
    goal_chain: list = field(default_factory=list)
    updated_at: float = 0.0


@dataclass
class Entity:
    """世界中的其他生物/物体。"""
    name: str = "unknown"
    bbox: tuple = (0, 0, 0, 0)      # (x1,y1,x2,y2) 绝对像素
    state: str = "unknown"
    intent_guess: str = "unknown"   # 敌意/中性/友善（MLLM 语义输出）
    threat: float = 0.0
    relation: str = "unknown"       # 结合记忆的关系感知
    track_id: str | None = None     # 战斗跟踪器维护的高频位置流 id


@dataclass
class WorldState:
    """MLLM 感知调用（P3）的输出——决策的唯一世界输入；逐字段置信度。"""
    frame_segment: FrameSegment
    timestamp: float
    scene: SceneType
    urgency: float
    confidence: dict                # {"entities":0.9,"ui":0.95,...}
    self_state: SelfState
    entities: list                  # list[Entity]
    screen_text: dict               # {"dialog":原文照录, "tutorial":..., "numbers":...}
    ui_state: dict                  # {"active_panel":..., "clickable":[{label,bbox}]}
    environment: dict               # {"hazards":..., "resources":..., "walkable":...}
    event_transitions: list         # 【时间语义】"持刀者开始挥砍"/"对话框出现"/"血条下降"
    recent_change: str = ""
    uncertainty: list = field(default_factory=list)


@dataclass
class Goal:
    """目标树节点。"""
    id: str
    text: str
    priority: int                   # 0=生存底线
    derived_from: str               # 防编造溯源
    subtasks: list = field(default_factory=list)
    success_signal: str = ""
    abort_condition: str = ""
    reward_fn: str | None = None


@dataclass
class Action:
    """原子操作（白名单内）。"""
    type: str                       # keypress/click/move/skill/wait/speak
    detail: str
    params: dict = field(default_factory=dict)   # click 需 bbox，且过校准
    expected_effect: str = ""


@dataclass
class StepRecord:
    """轨迹最小单元——MLLM 条件化 BC 的训练样本格式。"""
    t: float
    purpose: str                    # 操作前目的（thought）
    action_json: dict               # 操作指令
    frame_segment: dict             # {前2-4帧, 操作帧, 后2-4帧} 文件路径+时间戳
    observed_result: str            # MLLM 识别的操作后反馈（事件转变）
    reward: float | None = None     # reward_fn 回填


@dataclass
class AttentionPlan:
    """三级算力分配计划。"""
    frame_rate_capture: int         # 采集帧率：战斗30-60 / 解密5-10 / 剧情1-3
    keyframe_interval_ms: int       # 关键帧间隔：战斗300-500 / 解密1000-2000 / 剧情3000-5000
    roi_targets: list               # ROI 裁剪目标区域
    mllm_tier: str                  # reflex / fast(快档API) / deep(旗舰API)
    ignore: list = field(default_factory=list)
    perceive_hz: float = 1.0        # 感知节拍频率（决策/感知解耦用）


# ---- 小工具：把结构化对象转成 JSON 友好的 dict（dataclass -> dict） ----
def _to_jsonable(obj):
    """把 dataclass / Enum / tuple 递归转成可 JSON 序列化的结构。"""
    if hasattr(obj, "__dataclass_fields__"):
        out = {}
        for k, v in obj.__dict__.items():
            out[k] = _to_jsonable(v)
        return out
    if isinstance(obj, Enum):
        return obj.value
    if isinstance(obj, tuple):
        return list(obj)
    if isinstance(obj, dict):
        return {k: _to_jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list,)):
        return [_to_jsonable(v) for v in obj]
    return obj


def worldstate_to_json(ws: "WorldState") -> dict:
    """WorldState -> dict（供 JSON-RPC / 日志 / 轨迹使用）。"""
    return _to_jsonable(ws)


def json_to_worldstate(d: dict) -> "WorldState":
    """dict -> WorldState（从 JSON-RPC / Mock 结果重建，缺失字段安全兜底）。"""
    # FrameSegment 重建
    seg_d = d.get("frame_segment", {}) or {}
    frames = [Frame(image_path=f.get("image_path", ""),
                    t_ms=int(f.get("t_ms", 0)),
                    is_keyframe=bool(f.get("is_keyframe", False)),
                    roi=f.get("roi")) for f in seg_d.get("frames", [])]
    rois = [Frame(image_path=f.get("image_path", ""),
                  t_ms=int(f.get("t_ms", 0)),
                  is_keyframe=bool(f.get("is_keyframe", False)),
                  roi=f.get("roi")) for f in seg_d.get("rois", [])]
    seg = FrameSegment(frames=frames, rois=rois,
                       span_t_ms=tuple(seg_d.get("span_t_ms", (0, 0))))
    # SelfState 重建
    ss_d = d.get("self_state", {}) or {}
    ss = SelfState(
        name=ss_d.get("name", "agent"),
        hp=float(ss_d.get("hp", 1.0)),
        resources=ss_d.get("resources", {}) or {},
        position=tuple(ss_d.get("position", ())) if ss_d.get("position") else None,
        facing=ss_d.get("facing", "unknown"),
        animation=ss_d.get("animation", "unknown"),
        equipment=ss_d.get("equipment", []) or [],
        inventory=ss_d.get("inventory", []) or [],
        goal_chain=ss_d.get("goal_chain", []) or [],
        updated_at=float(ss_d.get("updated_at", time.time())),
    )
    # Entities 重建
    entities = []
    for e in d.get("entities", []) or []:
        entities.append(Entity(
            name=e.get("name", "unknown"),
            bbox=tuple(e.get("bbox", (0, 0, 0, 0))),
            state=e.get("state", "unknown"),
            intent_guess=e.get("intent_guess", "unknown"),
            threat=float(e.get("threat", 0.0)),
            relation=e.get("relation", "unknown"),
            track_id=e.get("track_id"),
        ))
    scene_raw = d.get("scene", "explore")
    if isinstance(scene_raw, dict):
        scene_val = scene_raw.get("type", "explore")
        urgency = float(scene_raw.get("urgency", 0.0))
    else:
        scene_val = scene_raw
        urgency = float(d.get("urgency", 0.0))
    try:
        scene = SceneType(scene_val)
    except ValueError:
        scene = SceneType.EXPLORE
    return WorldState(
        frame_segment=seg,
        timestamp=float(d.get("timestamp", time.time())),
        scene=scene,
        urgency=urgency,
        confidence=d.get("confidence", {}) or {},
        self_state=ss,
        entities=entities,
        screen_text=d.get("screen_text", {}) or {},
        ui_state=d.get("ui_state", {}) or {},
        environment=d.get("environment", {}) or {},
        event_transitions=d.get("event_transitions", []) or [],
        recent_change=d.get("recent_change", ""),
        uncertainty=d.get("uncertainty", []) or [],
    )
