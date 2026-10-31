"""生活时间轴（B1 地基）—— 给角色一条「日子在往前走」的线。

被 `plugin.py` 装配，负责三件事：

- **一天的骨架**：今天在哪里过、两段小插曲、十几段「此刻在做什么」
- **身体的手感**：能量 / 心情词 / 身上那点小状况（纯本地算，不花 token）
- **昨夜的一点残影**：梦，只用来解释「醒来时的状态」，不进正文叙事

设计要点（与 persona 既有模块保持一致）：

- **数据驱动**：场景 / 插曲 / 状况词全部来自 `backgrounds/life.json`，内置包只作兜底；
  和 `favor.json` 的 `mood_bands` 同一个思路 —— 文案归运营，逻辑归代码
- **确定性可复现**：同一天 + 同一份冷却历史 → 抽出同一张卡（`blake2b(date|salt)` 播种），
  便于复现「昨天为什么这么说」，也避免同进程内重复抽签
- **不信任模型输出**：模型给的时间段一律过 `validate_plan()`（段数 / 空档 / 覆盖率），
  不合格先试着 `repair_plan()` 接缝，接不上就老实说今天没什么安排
- **来源可追溯**：每份日程带 `source`（`model` / `repaired` / `template`），
  兜底日渲染成含糊措辞，绝不假装「今天过得很具体」
- **作用域是全局**：她在不同人面前心情可以不同（那是 mood），
  但「今天在做什么」只有一份 —— 否则会出现"同时在不同人面前过不同日子"
- 本模块**不 import plugin.py**，外部依赖全部注入 `Engine`，单测脱离容器也能跑

`LIFE_ENABLED` 默认关闭：B1 只铺地基，注入合并与三向联动由后续批次接上。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import random
import re
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional, Tuple

from nekro_agent.core.logger import get_sub_logger

logger = get_sub_logger("persona_life")

# ---------------- 常量 ----------------

STATE_KEY = "life_state"           # global 作用域：一份人生
HISTORY_KEY = "life_history"       # 场景 / 插曲的冷却历史
GLOBAL_CHAT_KEY = "global"

SCENE_COOLDOWN = 6                 # 最近 N 次用过的场景不再抽
INTERLUDE_COOLDOWN = 10            # 最近 N 段用过的插曲不再抽
INTERLUDE_PER_DAY = 2

PLAN_MIN_SEGMENTS = 12             # 「像样的一天」至少这么多段
PLAN_MAX_SEGMENTS = 20
PLAN_MAX_GAP_MIN = 240             # 相邻两段之间的空档上限（分钟）
PLAN_MIN_COVER_MIN = 1200          # 各段时长之和至少 20 小时
MERGE_SLACK_MIN = 15               # 修补时把 ≤15 分钟的小缝直接接上

ENERGY_TICK_SECONDS = 600
BLOCK_LINE_CAP = 60
BLOCK_BODY_CAP = 380

SALT = "persona-life-v1"           # 换掉它就换一整套抽签结果（内容包换代时用）


# ---------------- 小工具 ----------------

def _now() -> float:
    return time.time()


def _today(now: Optional[datetime] = None) -> str:
    return (now or datetime.now()).strftime("%Y-%m-%d")


def _squeeze(text: Any, cap: int = 100) -> str:
    """压成单行 —— 注入块里出现换行会把条目结构撕开。"""
    return " ".join(str(text or "").split())[:cap]


def _hhmm(minute_of_day: int) -> str:
    total = int(minute_of_day) % (24 * 60)
    return f"{total // 60:02d}:{total % 60:02d}"


def _minute(text: str) -> Optional[int]:
    m = re.match(r"^\s*(\d{1,2}):(\d{2})\s*$", str(text or ""))
    if not m:
        return None
    hour, minute = int(m.group(1)), int(m.group(2))
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    return hour * 60 + minute


def _seed(*parts: Any) -> int:
    raw = "|".join(str(p) for p in parts)
    return int.from_bytes(hashlib.blake2b(raw.encode("utf-8"), digest_size=8).digest(), "big")


def _rng(*parts: Any) -> random.Random:
    return random.Random(_seed(SALT, *parts))


def _loose_json(raw: Optional[str]) -> Any:
    """从模型输出里抠 JSON：容忍 ``` 围栏与前后废话。"""
    if not raw:
        return None
    text = str(raw).strip()
    fence = re.search(r"```(?:json)?\s*(.+?)```", text, re.S)
    if fence:
        text = fence.group(1).strip()
    for left, right in (("{", "}"), ("[", "]")):
        i, j = text.find(left), text.rfind(right)
        if i != -1 and j > i:
            try:
                return json.loads(text[i:j + 1])
            except Exception:  # noqa: BLE001
                continue
    return None


try:  # 节假日是可选增强，装不上就工作日 / 周末二分
    import chinese_calendar as _cal  # type: ignore
except Exception:  # noqa: BLE001
    _cal = None

_WEEKDAY = "一二三四五六日"


def day_label(now: Optional[datetime] = None) -> str:
    """如「2026-06-15 周一 工作日 14:30」。判定失败不抛错。"""
    cur = now or datetime.now()
    kind = "周末" if cur.weekday() >= 5 else "工作日"
    if _cal is not None:
        try:
            holiday = None
            try:
                detail = _cal.get_holiday_detail(cur.date())
                holiday = detail[1] if detail and detail[1] else None
            except Exception:  # noqa: BLE001
                holiday = None
            if _cal.is_holiday(cur.date()) and holiday:
                kind = f"法定节假日（{holiday}）"
            elif _cal.is_holiday(cur.date()):
                kind = "假日"
            elif _cal.is_workday(cur.date()) and cur.weekday() >= 5:
                kind = "调休工作日"
        except Exception:  # noqa: BLE001
            pass
    return f"{cur.strftime('%Y-%m-%d')} 周{_WEEKDAY[cur.weekday()]} {kind} {cur.strftime('%H:%M')}"


# ---------------- 内容包（数据驱动，内置只作兜底） ----------------

@dataclass(frozen=True)
class Scene:
    id: str
    name: str          # 「安静的屋子」
    constraint: str    # 一句约束，写进生成提示词


@dataclass(frozen=True)
class Interlude:
    id: str
    text: str


BUILTIN_SCENES: Tuple[Scene, ...] = (
    Scene("quiet_room", "安静的屋子", "大部分时间待在熟悉的屋子里，节奏很慢。"),
    Scene("errands", "出门办事", "有几件必须出门处理的小事，来回占掉不少时间。"),
    Scene("windowsill", "临窗坐着", "靠窗待着，看外面的光和人来人往。"),
    Scene("rain_inside", "雨天不出门", "雨下了一整天，原本想做的事都往回收。"),
    Scene("one_deadline", "赶一件事", "有件必须今天弄完的事，节奏比平时紧。"),
    Scene("low_battery", "身上不舒服", "整个人提不起劲，做什么都慢半拍。"),
    Scene("visitors", "有人来过", "有人来了一趟，一天被切成前后两半。"),
    Scene("nothing_planned", "什么都没安排", "醒了就随它去，没有计划。"),
)

BUILTIN_INTERLUDES: Tuple[Interlude, ...] = (
    Interlude("old_object", "翻出一样很久没碰过的东西，手上停了一会儿"),
    Interlude("wrong_call", "接到一通打错的电话，对方的声音莫名有点耳熟"),
    Interlude("time_skip", "回过神发现天已经暗了，中间那段记不清"),
    Interlude("stuck", "一件本以为很容易的事卡住了，来回试了几遍"),
    Interlude("praised", "被一个不太熟的人夸了一句"),
    Interlude("spilled", "手一滑碰洒了东西，收拾了好一阵"),
    Interlude("old_song", "听到一首很久没听过的歌，停下来听完了"),
    Interlude("oversleep", "本来只想躺一会儿，结果睡过去了一个钟头"),
    Interlude("sudden_rain", "雨来得突然，原来的安排被迫改掉"),
    Interlude("good_bite", "吃到一样意外好吃的东西，记住了那个味道"),
    Interlude("long_wait", "等了很久，等到后面反而不着急了"),
    Interlude("old_message", "翻到一条很久以前的消息，看了一会儿"),
)

BUILTIN_ACHES: Tuple[Dict[str, Any], ...] = (
    {"label": "肩膀有点酸", "hours": (3, 6)},
    {"label": "脑子有点钝", "hours": (2, 4)},
    {"label": "眼睛发干", "hours": (2, 5)},
    {"label": "手有点凉", "hours": (3, 5)},
    {"label": "胃里空落落的", "hours": (1, 3)},
    {"label": "整个人轻飘飘的", "hours": (2, 4)},
)

MOOD_WORDS_HIGH = ("轻快", "有点期待", "平静", "松松散散", "心情不错")
MOOD_WORDS_LOW = ("有点蔫", "提不起劲", "发闷", "平平的", "懒得动")

# 兜底骨架：刻意含糊，不编造细节
TEMPLATE_DAY: Tuple[Tuple[str, str, str], ...] = (
    ("00:00-07:30", "睡着，夜里偶尔翻个身", "沉"),
    ("07:30-09:30", "醒过来，慢慢把早上过完", "有点钝"),
    ("09:30-12:30", "做点手上的事，中间起身走了走", "平平的"),
    ("12:30-15:00", "停下来歇了一阵，什么也没干", "松"),
    ("15:00-18:30", "接着做，被一件小事分神", "有点烦"),
    ("18:30-21:30", "收尾，顺手整理了一下周围", "松下来"),
    ("21:30-00:00", "安静下来，等这一天过去", "安宁"),
)


def load_content_pack() -> Tuple[Tuple[Scene, ...], Tuple[Interlude, ...], Tuple[Dict[str, Any], ...]]:
    """读 `backgrounds/life.json`；缺文件 / 坏 JSON / 字段不对 → 全部退回内置包。"""
    path = None
    try:
        from . import memory as _mem

        getter = getattr(_mem, "_bg_dir", None)
        path = getter() / "life.json" if callable(getter) else None
    except Exception:  # noqa: BLE001
        path = None
    if path is None:
        return BUILTIN_SCENES, BUILTIN_INTERLUDES, BUILTIN_ACHES
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return BUILTIN_SCENES, BUILTIN_INTERLUDES, BUILTIN_ACHES
    if not isinstance(payload, dict):
        return BUILTIN_SCENES, BUILTIN_INTERLUDES, BUILTIN_ACHES

    scenes: List[Scene] = []
    for item in payload.get("scenes") or []:
        if isinstance(item, dict) and str(item.get("id") or "").strip():
            scenes.append(Scene(str(item["id"]).strip(), _squeeze(item.get("name"), 24),
                                _squeeze(item.get("constraint"), 120)))

    interludes: List[Interlude] = []
    for item in payload.get("interludes") or []:
        if isinstance(item, dict) and str(item.get("id") or "").strip():
            interludes.append(Interlude(str(item["id"]).strip(), _squeeze(item.get("text"), 60)))

    aches: List[Dict[str, Any]] = []
    for item in payload.get("aches") or []:
        if not isinstance(item, dict) or not _squeeze(item.get("label"), 16):
            continue
        hours = item.get("hours") or [2, 5]
        if isinstance(hours, (list, tuple)) and len(hours) == 2:
            aches.append({"label": _squeeze(item.get("label"), 16),
                          "hours": (int(hours[0]), int(hours[1]))})

    return (tuple(scenes) or BUILTIN_SCENES,
            tuple(interludes) or BUILTIN_INTERLUDES,
            tuple(aches) or BUILTIN_ACHES)


# ---------------- 数据结构 ----------------

@dataclass
class Segment:
    start: int                 # 当天第几分钟
    end: int                   # 可 > 1440（跨天）
    doing: str
    feel: str = ""

    @property
    def window(self) -> str:
        return f"{_hhmm(self.start)}-{_hhmm(self.end)}"

    def covers(self, minute_of_day: int) -> bool:
        return (self.start <= minute_of_day < self.end
                or self.start <= minute_of_day + 24 * 60 < self.end)


@dataclass
class DayPlan:
    summary: str
    segments: List[Segment]
    source: str = "model"            # model / repaired / template
    built_at: float = field(default_factory=_now)

    def to_json(self) -> Dict[str, Any]:
        return {
            "summary": self.summary,
            "source": self.source,
            "built_at": self.built_at,
            "segments": [{"start": s.start, "end": s.end, "doing": s.doing, "feel": s.feel}
                         for s in self.segments],
        }

    @staticmethod
    def from_json(raw: Any) -> Optional["DayPlan"]:
        if not isinstance(raw, dict) or not isinstance(raw.get("segments"), list):
            return None
        segments: List[Segment] = []
        for item in raw["segments"]:
            if not isinstance(item, dict):
                return None
            try:
                segments.append(Segment(int(item["start"]), int(item["end"]),
                                        _squeeze(item.get("doing"), 120), _squeeze(item.get("feel"), 24)))
            except Exception:  # noqa: BLE001
                return None
        if not segments:
            return None
        return DayPlan(_squeeze(raw.get("summary"), 80) or "普通的一天", segments,
                       str(raw.get("source") or "model"), float(raw.get("built_at") or _now()))


@dataclass
class Vitals:
    energy: int = 72
    mood: str = "平静"
    aches: List[Dict[str, Any]] = field(default_factory=list)
    stamped: float = field(default_factory=_now)

    def to_json(self) -> Dict[str, Any]:
        return asdict(self)

    @staticmethod
    def from_json(raw: Any) -> Optional["Vitals"]:
        if not isinstance(raw, dict):
            return None
        try:
            return Vitals(int(raw.get("energy", 72)), _squeeze(raw.get("mood"), 16) or "平静",
                          [a for a in (raw.get("aches") or []) if isinstance(a, dict)],
                          float(raw.get("stamped") or _now()))
        except Exception:  # noqa: BLE001
            return None


@dataclass
class Dream:
    text: str = ""
    mood: str = "平静"
    afterglow: str = ""
    date: str = ""

    def to_json(self) -> Dict[str, Any]:
        return asdict(self)

    @staticmethod
    def from_json(raw: Any) -> Optional["Dream"]:
        if not isinstance(raw, dict):
            return None
        return Dream(_squeeze(raw.get("text"), 500), _squeeze(raw.get("mood"), 16) or "平静",
                     _squeeze(raw.get("afterglow"), 80), str(raw.get("date") or ""))


@dataclass
class Card:
    date: str
    scene_id: str
    scene_name: str
    constraint: str
    interludes: List[Interlude] = field(default_factory=list)

    def to_json(self) -> Dict[str, Any]:
        return {"date": self.date, "scene_id": self.scene_id, "scene_name": self.scene_name,
                "constraint": self.constraint,
                "interludes": [{"id": i.id, "text": i.text} for i in self.interludes]}

    @staticmethod
    def from_json(raw: Any) -> Optional["Card"]:
        if not isinstance(raw, dict) or not raw.get("scene_id"):
            return None
        items = [Interlude(str(i.get("id") or ""), _squeeze(i.get("text"), 60))
                 for i in (raw.get("interludes") or []) if isinstance(i, dict)]
        return Card(str(raw.get("date") or ""), str(raw.get("scene_id")),
                    _squeeze(raw.get("scene_name"), 24), _squeeze(raw.get("constraint"), 120), items)


@dataclass
class DayState:
    date: str = ""
    plan: Optional[DayPlan] = None
    vitals: Optional[Vitals] = None
    dream: Optional[Dream] = None
    card: Optional[Card] = None
    recent: List[Dict[str, Any]] = field(default_factory=list)   # 最近几天骨架，给"别重复"用

    def to_json(self) -> Dict[str, Any]:
        return {
            "date": self.date,
            "plan": self.plan.to_json() if self.plan else None,
            "vitals": self.vitals.to_json() if self.vitals else None,
            "dream": self.dream.to_json() if self.dream else None,
            "card": self.card.to_json() if self.card else None,
            "recent": self.recent[-7:],
        }

    @staticmethod
    def from_json(raw: Any) -> "DayState":
        if not isinstance(raw, dict):
            return DayState()
        return DayState(
            date=str(raw.get("date") or ""),
            plan=DayPlan.from_json(raw.get("plan")),
            vitals=Vitals.from_json(raw.get("vitals")),
            dream=Dream.from_json(raw.get("dream")),
            card=Card.from_json(raw.get("card")),
            recent=[r for r in (raw.get("recent") or []) if isinstance(r, dict)],
        )


# ---------------- 校验与修补 ----------------

def validate_plan(segments: List[Segment]) -> Tuple[bool, str]:
    """能不能用：段数够、没有大空档、覆盖一整天。返回 (合格, 原因)。"""
    if len(segments) < 3:
        return False, f"段数太少({len(segments)})"
    ordered = sorted(segments, key=lambda s: s.start)
    if ordered[0].start > PLAN_MAX_GAP_MIN:
        return False, f"开头缺 {ordered[0].start} 分钟"
    if any(not s.doing for s in ordered):
        return False, "有空白的段落"
    for prev, cur in zip(ordered, ordered[1:]):
        if cur.start < prev.start:
            return False, "时间段乱序"
        if cur.start - prev.end > PLAN_MAX_GAP_MIN:
            return False, f"{_hhmm(prev.end)}-{_hhmm(cur.start)} 空了太久"
    # 覆盖率按「各段时长之和」算：段与段之间的空档由上面那条单独判，
    # 不能用「下一段起点 - 上一段起点」来累加 —— 那会把空档算成已覆盖时间。
    covered = sum(max(0, min(24 * 60, s.end) - s.start) for s in ordered)
    if covered < PLAN_MIN_COVER_MIN:
        return False, f"只覆盖 {covered} 分钟"
    return True, "ok"


def repair_plan(segments: List[Segment]) -> List[Segment]:
    """能救就救：丢坏段、裁越界、接上相邻的空缝。救不动返回空列表。"""
    usable: List[Segment] = []
    for seg in segments:
        if not seg.doing or seg.end <= seg.start or seg.start >= 1440:
            continue
        usable.append(Segment(max(0, seg.start), min(1440, seg.end), seg.doing, seg.feel))
    if len(usable) < 3:
        return []
    usable.sort(key=lambda s: s.start)
    merged: List[Segment] = []
    for seg in usable:
        # 只接「真正的小缝」（0 < 空档 ≤ 15 分钟）。
        # ⚠️ 不能把 gap == 0 的相邻段也并掉：一份正常日程里每段本来就是首尾相接的，
        # 那样会一路合并成一段，反而过不了段数下限。
        gap = seg.start - merged[-1].end if merged else 0
        if merged and 0 < gap <= MERGE_SLACK_MIN:
            merged[-1].end = max(merged[-1].end, seg.end)
            if seg.doing and seg.doing not in merged[-1].doing:
                merged[-1].doing = _squeeze(f"{merged[-1].doing}；{seg.doing}", 120)
        else:
            merged.append(seg)
    return merged


def template_plan(summary: str = "没什么安排的一天") -> DayPlan:
    segments: List[Segment] = []
    for window, doing, feel in TEMPLATE_DAY:
        left, right = window.split("-", 1)
        start = _minute(left) or 0
        end = _minute(right) or 24 * 60
        segments.append(Segment(start, end if end > start else end + 24 * 60, doing, feel))
    return DayPlan(summary, segments, source="template")


def recent_doings(recent: List[Dict[str, Any]], days: int = 3) -> List[str]:
    rows = [r for r in (recent or []) if isinstance(r, dict)]
    if not rows or days <= 0:
        return []
    picked = sorted(rows, key=lambda r: str(r.get("date") or ""))[-days:]
    out: List[str] = []
    for row in picked:
        for item in row.get("doings") or []:
            text = _squeeze(item, 40)
            if text:
                out.append(text)
    return out


def avoid_hint(doings: List[str], limit: int = 6) -> str:
    items: List[str] = []
    for text in doings or []:
        text = _squeeze(text, 40)
        if text:
            items.append(text)
        if len(items) >= limit:
            break
    return f"最近几天已经出现过这些安排，别重复：{'、'.join(items)}。" if items else ""


# ---------------- 能量曲线（贴昼夜节律，不是随机游走） ----------------

_HOUR_ENERGY = {
    0: 18, 1: 15, 2: 13, 3: 12, 4: 13, 5: 16,
    6: 26, 7: 42, 8: 58, 9: 70, 10: 78, 11: 80,
    12: 68, 13: 58, 14: 62, 15: 70, 16: 74, 17: 72,
    18: 66, 19: 60, 20: 54, 21: 46, 22: 34, 23: 24,
}


def energy_target(hour: int) -> int:
    """一天里的能量基准：清晨低、上午起、午后小落、傍晚次高、深夜最低。"""
    return int(_HOUR_ENERGY.get(int(hour) % 24, 60))


# ---------------- 引擎 ----------------

class Engine:
    """把「读状态 → 生成 → 落盘 → 渲染」串起来；外部依赖全部注入。"""

    def __init__(self, *, store_read: Callable, store_write: Callable, config: Any,
                 ask_model: Optional[Callable] = None, persona_text: Optional[Callable] = None):
        self._read = store_read          # async (key) -> Any
        self._write = store_write        # async (key, value) -> None
        self._config = config
        self._ask = ask_model            # async (prompt, system, task) -> Optional[str]
        self._persona = persona_text     # async () -> str
        self._lock = asyncio.Lock()

    # ---- 配置 ----
    def _cfg(self, name: str, default: Any) -> Any:
        return getattr(self._config, name, default)

    @property
    def on(self) -> bool:
        return bool(self._cfg("LIFE_ENABLED", False))

    # ---- 读写 ----
    async def load(self) -> DayState:
        try:
            return DayState.from_json(await self._read(STATE_KEY))
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[life] 读生活状态失败，按空处理: {exc!r}")
            return DayState()

    async def save(self, state: DayState) -> None:
        try:
            await self._write(STATE_KEY, state.to_json())
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[life] 写生活状态失败: {exc!r}")

    async def load_history(self) -> Dict[str, List[str]]:
        try:
            raw = await self._read(HISTORY_KEY)
        except Exception:  # noqa: BLE001
            raw = None
        if not isinstance(raw, dict):
            return {"scenes": [], "interludes": []}
        return {"scenes": [str(x) for x in (raw.get("scenes") or [])],
                "interludes": [str(x) for x in (raw.get("interludes") or [])]}

    async def save_history(self, hist: Dict[str, List[str]]) -> None:
        try:
            await self._write(HISTORY_KEY, hist)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[life] 写抽签历史失败: {exc!r}")

    # ---- 抽签 ----
    def roll_card(self, date_key: str, hist: Dict[str, List[str]]) -> Tuple[Card, Dict[str, List[str]]]:
        scenes, interludes, _ = load_content_pack()
        rng = _rng("card", date_key)

        cooled_scenes = set((hist.get("scenes") or [])[-SCENE_COOLDOWN:])
        scene_pool = [s for s in scenes if s.id not in cooled_scenes] or list(scenes)
        scene = rng.choice(scene_pool)

        cooled_interludes = set((hist.get("interludes") or [])[-INTERLUDE_COOLDOWN:])
        interlude_pool = [i for i in interludes if i.id not in cooled_interludes] or list(interludes)
        rng.shuffle(interlude_pool)
        picked = interlude_pool[:INTERLUDE_PER_DAY]

        card = Card(date_key, scene.id, scene.name, scene.constraint, picked)
        new_hist = {
            "scenes": (list(hist.get("scenes") or []) + [scene.id])[-SCENE_COOLDOWN * 2:],
            "interludes": (list(hist.get("interludes") or []) + [i.id for i in picked])[-INTERLUDE_COOLDOWN * 3:],
        }
        return card, new_hist

    # ---- 初始手感 ----
    def seed_vitals(self, state: DayState, date_key: str, hour: Optional[int] = None) -> Vitals:
        rng = _rng("vitals", date_key)
        base = energy_target(datetime.now().hour if hour is None else hour)
        energy = max(15, min(100, base + rng.randint(-8, 8)))
        dream_mood = (state.dream.mood if state.dream and state.dream.text else "") or ""
        if dream_mood:
            if any(w in dream_mood for w in ("轻快", "柔", "暖", "稳", "松")):
                energy = min(100, energy + rng.randint(2, 6))
            elif any(w in dream_mood for w in ("沉", "闷", "累", "慌", "低")):
                energy = max(15, energy - rng.randint(3, 9))
        mood = dream_mood or rng.choice(MOOD_WORDS_HIGH if energy >= 70 else MOOD_WORDS_LOW)
        _, _, aches = load_content_pack()
        chosen = rng.sample(list(aches), k=min(len(aches), rng.randint(0, 2))) if aches else []
        now = _now()
        return Vitals(energy, mood,
                      [{"label": a["label"], "until": now + rng.uniform(*a["hours"]) * 3600} for a in chosen],
                      now)

    # ---- 手感衰减 ----
    def tick(self, state: DayState, now: Optional[float] = None) -> bool:
        """清掉过期的身体小状况，让能量朝当前时段的基准靠。返回是否有改动。"""
        vitals = state.vitals
        if vitals is None:
            return False
        ts = _now() if now is None else float(now)
        changed = False
        alive = [a for a in vitals.aches if float(a.get("until", 0) or 0) > ts]
        if len(alive) != len(vitals.aches):
            vitals.aches = alive
            changed = True
        if ts - vitals.stamped >= ENERGY_TICK_SECONDS:
            target = energy_target(datetime.fromtimestamp(ts).hour)
            delta = target - vitals.energy
            step = 0 if abs(delta) <= 1 else (2 if delta > 0 else -2)
            vitals.stamped = ts
            if step:
                vitals.energy = max(10, min(100, vitals.energy + step))
                changed = True
        return changed

    # ---- 当前那段 ----
    def segment_at(self, state: DayState, now: Optional[datetime] = None) -> Optional[Segment]:
        if state.plan is None or not state.plan.segments:
            return None
        cur = now or datetime.now()
        minute = cur.hour * 60 + cur.minute
        for seg in state.plan.segments:
            if seg.covers(minute):
                return seg
        return None

    # ---- 模型调用 ----
    async def _ask_model(self, prompt: str, *, system: str, task: str) -> Optional[str]:
        if self._ask is None:
            return None
        try:
            return await self._ask(prompt, system=system, task=task)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[life] 模型调用失败（{task}）: {exc!r}")
            return None

    async def _persona_text(self) -> str:
        if self._persona is None:
            return ""
        try:
            return await self._persona() or ""
        except Exception:  # noqa: BLE001
            return ""

    # ---- 生成日程 ----
    async def rebuild_plan(self, state: DayState, *, reason: str = "") -> DayState:
        date_key = _today()
        if not (state.card and state.card.date == date_key):
            card, hist = self.roll_card(date_key, await self.load_history())
            await self.save_history(hist)
            state.card = card

        lo = max(3, min(16, int(self._cfg("LIFE_PLAN_MIN_SEGMENTS", PLAN_MIN_SEGMENTS))))
        hi = max(lo, min(PLAN_MAX_SEGMENTS, int(self._cfg("LIFE_PLAN_MAX_SEGMENTS", PLAN_MAX_SEGMENTS))))
        persona = await self._persona_text()
        system = (
            "你在替一个角色安排今天。她的设定如下，日子要贴着这个设定过。\n"
            f"【角色】\n{persona or '（设定缺失，按作息规律、生活平淡的普通人来写）'}"
        )
        raw = await self._ask_model(self._plan_prompt(state, lo, hi), system=system,
                                    task=f"life_plan{':' + reason if reason else ''}")
        plan = self._parse_plan(raw or "", hi)
        if plan is not None:
            ok, why = validate_plan(plan.segments)
            if not ok:
                logger.info(f"[life] 日程不合格（{why}），尝试修补")
                fixed = repair_plan(plan.segments)
                if fixed and validate_plan(fixed)[0]:
                    plan = DayPlan(plan.summary, fixed, source="repaired")
                else:
                    plan = None
        if plan is None:
            logger.warning("[life] 今日日程走兜底模板（模型不可用或输出不可用）")
            plan = template_plan()

        state.plan = plan
        state.date = date_key
        state.recent = (state.recent + [{"date": date_key,
                                         "doings": [s.doing for s in plan.segments]}])[-7:]
        if state.vitals is None:
            state.vitals = self.seed_vitals(state, date_key)
        await self.save(state)
        return state

    def _plan_prompt(self, state: DayState, lo: int, hi: int) -> str:
        card = state.card
        lines = ["把今天排出来。要求如下。", "", f"今天：{day_label()}"]
        if card is not None:
            lines.append(f"在哪过：{card.scene_name}。{card.constraint}")
            if card.interludes:
                lines.append("今天会发生两件小事（嵌进不同时段，不用写成大事）："
                             + "；".join(i.text for i in card.interludes))
        avoid = avoid_hint(recent_doings(state.recent))
        if avoid:
            lines.append(avoid)
        style = str(self._cfg("LIFE_PLAN_STYLE_HINT", "") or "").strip()
        if style:
            lines.append(f"风格：{style}")
        lines += [
            "",
            f"排 {lo}-{hi} 段，覆盖一整天（含睡觉），每段 window 写成 HH:MM-HH:MM，前后接上，别留大空档。",
            "**段长不要平均分**：睡觉通常是一整块，刚醒和睡前各可以有一小段。",
            "doing 写具体的小动作（「靠着桌沿把眼睛闭了一会儿」而不是「休息」）；"
            "一段是一小段连续的生活，不是一个瞬间，也不是任务名。",
            "feel 用 1-3 个短词写当时的感受或身体状态，别每段都同一个词。",
            "允许有一天什么也没发生，那种日子反而可信。",
            "整天里至少有两三段是松快的，不要全写成没力气、发闷。",
            "summary 用一句话说今天的底色，口语。",
            "",
            '只回 JSON，不要解释：'
            '{"summary": "...", "segments": [{"window": "HH:MM-HH:MM", "doing": "...", "feel": "..."}]}',
        ]
        return "\n".join(lines)

    def _parse_plan(self, raw: str, hi: int) -> Optional[DayPlan]:
        payload = _loose_json(raw)
        if not isinstance(payload, dict):
            return None
        segments: List[Segment] = []
        for item in payload.get("segments") or payload.get("events") or []:
            if not isinstance(item, dict):
                continue
            window = _squeeze(item.get("window") or item.get("time"), 24)
            for ch in ("—", "–", "－", "~", "～", "至", "到"):
                window = window.replace(ch, "-")
            window = window.replace("：", ":").replace(" ", "")
            if "-" not in window:
                continue
            left, right = window.split("-", 1)
            start, end = _minute(left), _minute(right)
            doing = _squeeze(item.get("doing") or item.get("activity"), 120)
            if start is None or end is None or not doing:
                continue
            segments.append(Segment(start, end if end > start else end + 24 * 60, doing,
                                    _squeeze(item.get("feel") or item.get("mood"), 24)))
        if len(segments) < 3:
            return None
        segments.sort(key=lambda s: s.start)
        return DayPlan(_squeeze(payload.get("summary"), 80) or "普通的一天",
                       segments[:hi], source="model")

    # ---- 生成梦境 ----
    async def rebuild_dream(self, state: DayState) -> DayState:
        date_key = _today()
        persona = await self._persona_text()
        materials = [s.doing for s in (state.plan.segments if state.plan else []) if "睡" not in s.doing][:8]
        material_line = ("昨天做过：" + "；".join(materials)) if materials else "（没有明确素材，随心取材）"
        system = ("你在写一个角色昨夜的梦。梦要像她会做的，不要写成设定介绍。\n"
                  f"【角色】\n{persona or '（设定缺失，按生活平淡的普通人来写）'}")
        prompt = (
            "写她昨夜的一个梦，醒来还残留在脑子里的那种。\n\n"
            f"【素材】{material_line}\n\n"
            "素材在梦里会变形重组，不要照抄白天的事；第一人称，120-300 字；"
            "可以跳接、不合逻辑，但要摸得到一条情绪线；从具体的物件、光、声音、身体感受里长出来；"
            "醒前那一瞬可以突然断掉。\n"
            "mood 写醒来那一刻的情绪（1-2 个短词）；afterglow 一句话写还残留着什么。\n"
            '只回 JSON：{"text": "梦", "mood": "...", "afterglow": "..."}'
        )
        payload = _loose_json(await self._ask_model(prompt, system=system, task="life_dream"))
        if isinstance(payload, dict) and _squeeze(payload.get("text"), 20):
            state.dream = Dream(_squeeze(payload.get("text"), 500),
                                _squeeze(payload.get("mood"), 16) or "平静",
                                _squeeze(payload.get("afterglow"), 80), date_key)
        else:
            # 没记住梦是常态，不是故障：留空，渲染时自动省略这一行
            state.dream = Dream("", "平静", "", date_key)
        await self.save(state)
        return state

    # ---- 跨天滚动 ----
    async def ensure_today(self) -> DayState:
        """保证状态是今天的：跨天时依次滚「梦 → 日程 → 手感」。永不抛异常。"""
        date_key = _today()
        state = await self.load()
        if state.date == date_key and state.plan is not None:
            return state
        async with self._lock:
            state = await self.load()
            if state.date == date_key and state.plan is not None:
                return state
            await self.rebuild_dream(state)      # 素材来自昨天的骨架，必须先于覆盖
            state = await self.load()
            state = await self.rebuild_plan(state, reason="daily")
            if state.vitals is None:
                state.vitals = self.seed_vitals(state, date_key)
            state.date = date_key
            await self.save(state)
            logger.info(f"[life] 已滚动到 {date_key}（来源: {state.plan.source if state.plan else '无'}）")
            return state

    # ---- 渲染 ----
    def render(self, state: DayState, *, chat_key: str = "", cap: Optional[int] = None) -> str:
        """生活状态注入块。信息不全就少说几句，绝不编。"""
        if state.plan is None:
            return ""
        lines = ["【你今天的生活】", f"现在：{day_label()}"]
        if chat_key:
            if "private" in chat_key:
                lines.append("对话场景：私聊")
            elif "group" in chat_key:
                lines.append("对话场景：群聊")

        if state.plan.source == "template":
            lines.append("今天没什么安排，过得比较淡")
        else:
            if state.plan.summary:
                lines.append(f"今天：{_squeeze(state.plan.summary, BLOCK_LINE_CAP)}")
            seg = self.segment_at(state)
            if seg is not None:
                feel = f"（{_squeeze(seg.feel, 16)}）" if seg.feel else ""
                lines.append(f"此刻（{seg.window}）：{_squeeze(seg.doing, BLOCK_LINE_CAP)}{feel}")

        if state.vitals is not None:
            lines.append(f"状态：能量 {max(0, min(100, state.vitals.energy))}/100，"
                         f"心情{_squeeze(state.vitals.mood, 16) or '平静'}")
            aches = "、".join(_squeeze(a.get("label"), 12) for a in state.vitals.aches if a.get("label"))
            if aches:
                lines.append(f"身上：{aches}")
        if state.dream and state.dream.text and state.dream.afterglow:
            lines.append(f"昨夜那点余韵：{_squeeze(state.dream.afterglow, 50)}")

        body = "\n".join(lines)[: int(cap or self._cfg("LIFE_BLOCK_CAP", BLOCK_BODY_CAP))]
        tail = ("（这些只是你此刻的底子，不用主动交代；"
                "**别写进笔记或长期记忆**——它是会变的东西。)")
        return body + "\n" + tail


# ---------------- 模块级装配 ----------------

_ENGINE: Optional[Engine] = None


def attach(engine: Optional[Engine]) -> None:
    global _ENGINE
    _ENGINE = engine


def engine() -> Optional[Engine]:
    return _ENGINE


def is_on() -> bool:
    return bool(_ENGINE is not None and _ENGINE.on)
