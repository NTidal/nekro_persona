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
from datetime import datetime, timedelta
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

# 注入路径的开销：每轮都读一次 store 不值得，渲染结果按频道缓存一会儿。
# 60 秒足够覆盖同一轮的多条消息，又不至于让「此刻」明显落后于时间。
BLOCK_CACHE_TTL = 60

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
    total = int(minute_of_day)
    if total == 24 * 60:                 # 一天的收尾写成 24:00，不是 00:00
        return "24:00"
    total %= 24 * 60
    return f"{total // 60:02d}:{total % 60:02d}"


def _minute(text: str) -> Optional[int]:
    m = re.match(r"^\s*(\d{1,2}):(\d{2})\s*$", str(text or ""))
    if not m:
        return None
    hour, minute = int(m.group(1)), int(m.group(2))
    # 24:00 是合法收尾（模型很爱这么写）；24:30 之类不认
    if hour == 24 and minute == 0:
        return 24 * 60
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


# ---------------- 词元包（种子式内容：抽词，不抽整句） ----------------
#
# 为什么不做"整句池"：整句池里每一句都是作者的世界观，抽到即定型；
# 而词元池只提供**在世界观之内**的名词与动作，句子交给模型按当天语境去串 ——
# 既能每天不重样（组合数上万），又天然不会冒出"上班打卡"这类不存在的场景。

@dataclass(frozen=True)
class WorldPack:
    """一个角色的生活词元。字段全可空，空则回落内置通用包。"""

    setting: str = ""                       # 一句世界设定（写进提示词）
    places: Tuple[str, ...] = ()            # 在哪：星炬学院、食堂、绿洲边…
    props: Tuple[str, ...] = ()             # 手里有什么：终端、数据坞、旧磁带…
    verbs: Tuple[str, ...] = ()             # 会做的事：听课、做同步率练习、修终端…
    feels: Tuple[str, ...] = ()             # 体感词：沉、钝、松快、心里发空…
    events: Tuple[str, ...] = ()            # 可能发生的小事：终端收到旧消息…
    forbidden: Tuple[str, ...] = ()         # 这个世界里不存在的东西（上班/打卡/通勤…）

    def usable(self) -> bool:
        return bool(self.places and self.verbs)


#: 内置默认禁词（"现代都市日常"口径）。可在 life.json 里整体替换。
DEFAULT_FORBIDDEN: Tuple[str, ...] = (
    "上班", "下班", "打卡", "通勤", "开会", "同事", "领导", "工位",
    "加班", "公司", "办公室", "地铁", "公交", "外卖", "便利店", "工资", "房贷",
)

BUILTIN_WORLD = WorldPack(
    setting="普通人的日常：待在屋里、出门办点事、吃饭、发呆。",
    places=("屋子里", "窗边", "楼下", "街角", "常去的店", "半路的站台"),
    props=("杯子", "书", "手机", "钥匙", "袋子", "外套"),
    verbs=("待着", "整理东西", "出门一趟", "煮点吃的", "发呆", "翻旧东西", "走了一段路"),
    feels=("沉", "钝", "松快", "发闷", "暖", "心里发空", "有点烦", "安宁"),
    events=("碰到一件小事打断了安排", "翻出很久没碰的东西", "外面天气变了",
            "听到别人聊起一件事", "临时改了主意", "多花了不少时间"),
    forbidden=("上班", "下班", "打卡", "通勤", "开会", "同事", "领导", "工位",
               "加班", "公司", "办公室", "地铁", "公交", "外卖", "便利店", "工资", "房贷"),
)


def load_world_pack() -> WorldPack:
    """读 ``backgrounds/life.json`` 的 ``world`` 段；缺什么就用内置包补什么。"""
    path = None
    try:
        from . import memory as _mem

        getter = getattr(_mem, "_bg_dir", None)
        path = getter() / "life.json" if callable(getter) else None
    except Exception:  # noqa: BLE001
        path = None
    payload: Dict[str, Any] = {}
    if path is not None:
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                payload = loaded.get("world") if isinstance(loaded.get("world"), dict) else {}
        except Exception:  # noqa: BLE001
            payload = {}

    # ⚠️ 禁词表以**文件为准**：只要 life.json 里出现了 forbidden 键（哪怕是空数组），
    #    就用文件里的，不再并内置那份。理由：内置那份是"现代都市日常"的默认值，
    #    真要去演一个城市上班族时，这份禁词必须能整体换掉 —— 锁死就没法换。
    #    键不存在（老文件/只写了别的字段）时才回落内置默认值。
    has_forbidden = isinstance(payload.get("forbidden"), list)

    def words(key: str, fallback: Tuple[str, ...], cap: int = 40) -> Tuple[str, ...]:
        rows = []
        for item in (payload.get(key) or []):
            text = _squeeze(item, 24)
            if text and text not in rows:
                rows.append(text)
            if len(rows) >= cap:
                break
        return tuple(rows) or fallback

    return WorldPack(
        setting=_squeeze(payload.get("setting"), 200) or BUILTIN_WORLD.setting,
        places=words("places", BUILTIN_WORLD.places),
        props=words("props", BUILTIN_WORLD.props),
        verbs=words("verbs", BUILTIN_WORLD.verbs),
        feels=words("feels", BUILTIN_WORLD.feels),
        events=words("events", BUILTIN_WORLD.events),
        forbidden=(tuple(dict.fromkeys(words("forbidden", ())))
                   if has_forbidden else BUILTIN_WORLD.forbidden),
    )


# ---------------- 旧整句池（保留兼容：没有 world 段的旧 life.json 仍能用） ----------------

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
class DaySeed:
    """今天抽到的词元组合。这是「素材」，不是成品句子。"""

    date: str = ""
    place: str = ""
    prop: str = ""
    verbs: List[str] = field(default_factory=list)
    events: List[str] = field(default_factory=list)
    feels: List[str] = field(default_factory=list)

    def to_json(self) -> Dict[str, Any]:
        return asdict(self)

    @staticmethod
    def from_json(raw: Any) -> Optional["DaySeed"]:
        if not isinstance(raw, dict):
            return None
        return DaySeed(
            date=str(raw.get("date") or ""),
            place=_squeeze(raw.get("place"), 24),
            prop=_squeeze(raw.get("prop"), 24),
            verbs=[_squeeze(x, 24) for x in (raw.get("verbs") or []) if _squeeze(x, 24)],
            events=[_squeeze(x, 40) for x in (raw.get("events") or []) if _squeeze(x, 40)],
            feels=[_squeeze(x, 16) for x in (raw.get("feels") or []) if _squeeze(x, 16)],
        )


def seed_block(seed: Optional[DaySeed], pack: Optional[WorldPack] = None) -> str:
    """把抽到的词元写成「今日素材」块，交给模型去串成具体的生活。"""
    if seed is None:
        return ""
    pack = pack or load_world_pack()
    lines = ["【今日素材（必须用上，不要换成别的场景）】"]
    if pack.setting:
        lines.append(f"世界设定：{pack.setting}")
    if seed.place:
        lines.append(f"在哪：{seed.place}")
    if seed.prop:
        lines.append(f"手边：{seed.prop}")
    if seed.verbs:
        lines.append("会做：" + "、".join(seed.verbs))
    if seed.events:
        lines.append("会发生：" + " / ".join(seed.events))
    if seed.feels:
        lines.append("体感词（挑着用，别全塞进去）：" + "、".join(seed.feels))
    lines.append("要求：把上面的词串成具体的动作与场景，可以加细节，"
                 "但**不要引入这个世界没有的东西**（她不存在于现代职场/校园之外的地方）。")
    return "\n".join(lines)


def world_hits(text: str, pack: Optional[WorldPack] = None) -> List[str]:
    """文本里命中了哪些「这个世界不存在」的词。"""
    pack = pack or load_world_pack()
    body = str(text or "")
    return [w for w in pack.forbidden if w and w in body]


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
    """一天以上的生活状态。

    ``plans`` 以日期为键，可以同时存今天与后几天的骨架（``LIFE_PLAN_HORIZON``）。
    ``plan`` 是「今天那份」的读写别名 —— 上层（渲染 / 当前时段 / 梦境）不必关心
    一次排了几天，只关心今天。
    """

    date: str = ""                       # 今天（滚动指针）
    plans: Dict[str, DayPlan] = field(default_factory=dict)
    vitals: Optional[Vitals] = None
    dream: Optional[Dream] = None
    seed: Optional[DaySeed] = None
    recent: List[Dict[str, Any]] = field(default_factory=list)   # 最近几天骨架，给"别重复"用

    def __init__(self, date: str = "", plans: Optional[Dict[str, DayPlan]] = None,
                 vitals: Optional[Vitals] = None, dream: Optional[Dream] = None,
                 seed: Optional[DaySeed] = None, recent: Optional[List[Dict[str, Any]]] = None,
                 plan: Optional[DayPlan] = None):
        # 手写 __init__ 而不是让 dataclass 生成：老调用点（含测试）还在写 ``plan=``，
        # 那是「今天这份」的简写，等价于 plans[date]=plan。
        self.date = str(date or "")
        self.plans = dict(plans or {})
        self.vitals = vitals
        self.dream = dream
        self.seed = seed
        self.recent = list(recent or [])
        if plan is not None and self.date:
            self.plans[self.date] = plan

    @property
    def plan(self) -> Optional[DayPlan]:
        return self.plans.get(self.date)

    @plan.setter
    def plan(self, value: Optional[DayPlan]) -> None:
        if value is None:
            self.plans.pop(self.date, None)
        else:
            self.plans[self.date] = value

    def to_json(self) -> Dict[str, Any]:
        return {
            "date": self.date,
            "plans": {d: p.to_json() for d, p in self.plans.items() if p is not None},
            # 兼容：老版本只认 "plan"，写一份今天的进去，回退旧代码时也读得懂
            "plan": self.plan.to_json() if self.plan else None,
            "vitals": self.vitals.to_json() if self.vitals else None,
            "dream": self.dream.to_json() if self.dream else None,
            "seed": self.seed.to_json() if self.seed else None,
            "recent": self.recent[-7:],
        }

    @staticmethod
    def from_json(raw: Any) -> "DayState":
        if not isinstance(raw, dict):
            return DayState()
        date_key = str(raw.get("date") or "")
        plans: Dict[str, DayPlan] = {}
        for day, item in (raw.get("plans") or {}).items():
            plan = DayPlan.from_json(item)
            if plan is not None and str(day):
                plans[str(day)] = plan
        if not plans:
            # 旧数据：只有单日 "plan"，归到 state.date 名下
            legacy = DayPlan.from_json(raw.get("plan"))
            if legacy is not None and date_key:
                plans[date_key] = legacy
        return DayState(
            date=date_key,
            plans=plans,
            vitals=Vitals.from_json(raw.get("vitals")),
            dream=Dream.from_json(raw.get("dream")),
            seed=DaySeed.from_json(raw.get("seed")) or DaySeed.from_json(raw.get("card")),
            recent=[r for r in (raw.get("recent") or []) if isinstance(r, dict)],
        )



def extract_day_objects(raw: str) -> List[Dict[str, Any]]:
    """从（可能被截断的）多日输出里，切出**每个完整的** day 对象。

    为什么需要：三天一批的输出有 3-6k 字，模型撞上 max_tokens 会被拦腰截断。
    没有这一步，一次截断就丢掉全部三天（都退兜底模板）；有了它，
    完整的前两天照样能用，只有被截断的那天走兜底。
    """
    text = str(raw or "")
    out: List[Dict[str, Any]] = []
    i = 0
    while True:
        start = text.find('{"date"', i)
        if start == -1:
            start = text.find('{ "date"', i)
        if start == -1:
            break
        depth, j, in_str, esc = 0, start, False, False
        while j < len(text):
            ch = text[j]
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
            else:
                if ch == '"':
                    in_str = True
                elif ch == "{":
                    depth += 1
                elif ch == "}":
                    depth -= 1
                    if depth == 0:
                        break
            j += 1
        if depth != 0:
            break                      # 这个对象没闭合 → 后面都不用看了
        try:
            item = json.loads(text[start:j + 1])
        except Exception:              # noqa: BLE001
            item = None
        if isinstance(item, dict):
            out.append(item)
        i = j + 1
    return out

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
    # 夜里那一段模型经常漏掉（它习惯从早上开排）。补上而不是整份作废：
    # 「00:00-08:10 在睡」是事实，不是编造。
    if usable and usable[0].start > 0:
        head_end = usable[0].start
        usable.insert(0, Segment(0, head_end, "睡着，夜里偶尔翻个身", "沉"))
    if usable and usable[-1].end < 24 * 60:
        usable.append(Segment(usable[-1].end, 24 * 60, "睡下了，等这一天过去", "安宁"))

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


def scrub_plan(plan: DayPlan, pack: Optional[WorldPack] = None) -> DayPlan:
    """把命中越界词的段落改写成世界观内的中性说法（只动命中的段）。

    不整天作废的理由：一份 10 段的日程里可能只有 1 段写了"下班路上"，
    其余 9 段都是好的 —— 整天丢掉反而是更大的信息损失。
    """
    pack = pack or load_world_pack()
    place = (pack.places[0] if pack.places else "自己的地方")
    verb = (pack.verbs[0] if pack.verbs else "待着")
    feel = (pack.feels[0] if pack.feels else "平静")
    segments: List[Segment] = []
    for seg in plan.segments:
        if world_hits(f"{seg.doing} {seg.feel}", pack):
            segments.append(Segment(seg.start, seg.end, f"在{place}{verb}", feel))
        else:
            segments.append(seg)
    summary = plan.summary
    if world_hits(summary, pack):
        summary = "普通的一天"
    return DayPlan(summary, segments, source="repaired", built_at=plan.built_at)


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



# ---------------- 三向联动（B3）：状态 → 情绪 / 语气 ----------------

#: 判「她这会儿腾不出手」的动词/状态词。宁可少判：误判会把正常对话压短。
BUSY_MARKERS = ("赶", "一直在做", "忙", "路上", "排队", "睡着", "没空")

#: 判「她这天本来就不顺」的体感词（只看当前段的 feel 与当天 summary）
NEG_FEEL_MARKERS = ("烦", "沉", "闷", "累", "钝", "发空", "发酸", "发紧", "空", "疲", "晃")


@dataclass
class MoodBias:
    """一次情绪波动的「当下权重」。只调幅度与耐久，不改符号。"""

    factor: float = 1.0
    half_life_mult: float = 1.0
    notes: List[str] = field(default_factory=list)

    def describe(self) -> str:
        return "、".join(self.notes) if self.notes else "无调整"


def mood_bias(state: DayState, *, score: int = 0, mood: int = 0,
              now: Optional[datetime] = None) -> MoodBias:
    """此刻该怎么读这句话。返回乘数与半衰期倍数（纯函数，便于单测）。

    四条规则，各自只在"说得通"的时候生效：

    ① **累了就钝**：深夜或能量见底 → 正负都收着（对负向 ×0.8、对正向 ×0.6）。
       累的时候不容易炸，也没力气高兴 —— 比"越累越敏感"更接近真人。
    ② **本来就不顺**：当前时段体感或当天底色偏负 → 负向 ×1.15（同一句话落在坏日子里更重）。
    ③ **熟的人更耐读**：score≥60 → 负向 ×0.85 且衰减半衰期 ×1.5（信任是缓冲，气也散得慢）；
       score<20 → 负向 ×1.15 且半衰期 ×0.8（还不熟，一句越界就够呛，但忘得也快）。
    ④ 乘数钳在 [0.5, 1.35]：单次调整不该由这些旁路因素主导。
    """
    bias = MoodBias()
    cur = now or datetime.now()
    hour = cur.hour
    energy = state.vitals.energy if state.vitals else 70

    # ① 钝化
    if hour >= 23 or hour < 6:
        bias.factor *= 0.8 if mood <= 0 else 0.6
        bias.notes.append("深夜")
    elif energy < 25:
        bias.factor *= 0.8 if mood <= 0 else 0.6
        bias.notes.append(f"体力见底({energy})")

    # ② 坏日子
    glimpse = []
    seg = None
    if state.plan is not None:
        for cand in state.plan.segments:
            if cand.covers(cur.hour * 60 + cur.minute):
                seg = cand
                break
        glimpse.append(state.plan.summary or "")
    if seg is not None:
        glimpse.append(seg.feel or "")
        glimpse.append(seg.doing or "")
    if any(m in " ".join(glimpse) for m in NEG_FEEL_MARKERS):
        if mood <= 0:
            bias.factor *= 1.15
            bias.notes.append("今天本来就不顺")

    # ③ 关系深浅
    if score >= 60:
        if mood <= 0:
            bias.factor *= 0.85
            bias.half_life_mult *= 1.5
            bias.notes.append("关系深(缓冲)")
    elif score < 20:
        if mood <= 0:
            bias.factor *= 1.15
            bias.half_life_mult *= 0.8
            bias.notes.append("还不熟")

    bias.factor = max(0.5, min(1.35, bias.factor))
    bias.half_life_mult = max(0.5, min(2.0, bias.half_life_mult))
    return bias


def hold_hint(state: DayState, *, mood: int = 0) -> str:
    """此刻要不要把话收着说。返回一句约束（空串=不用）。

    只在**三条同时成立才不打扰**：她腾不出手 / 体力见底 / 情绪明显不好。
    这是语气约束，不是拒答 —— 对方真有事（见 URGENT）时照常回应。
    """
    if state.plan is not None:
        for seg in state.plan.segments:
            if seg.covers(datetime.now().hour * 60 + datetime.now().minute):
                if any(m in f"{seg.doing} {seg.feel}" for m in BUSY_MARKERS):
                    return "（你这会儿正忙着手上的事，回得短一点，别展开）"
                break
    if state.vitals is not None and state.vitals.energy < 20:
        return "（你这会儿很累，话少一点，别硬撑）"
    if mood <= -30:
        return "（你心里还堵着，语气淡一点，别刻意热络）"
    return ""


#: 对方真有事时不收着 —— 语气约束不能压过求救信号
URGENT_MARKERS = ("救命", "出事", "紧急", "医院", "受伤", "害怕", "崩溃", "想不开")


def is_urgent(text: str) -> bool:
    return any(m in str(text or "") for m in URGENT_MARKERS)



# ---------------- 约定（B4）：她答应过 ta 的事 ----------------

#: 认得出这是"一件答应了的事"
PROMISE_MARKERS = ("答应", "约好", "说好", "约定", "别忘", "记得", "要还", "要带",
                   "要交给", "见面", "回消息", "提醒")

#: 相对日期词 → 偏移天数。**不写"下周""月底"这类模糊词**：
#: 判不准就不提醒，宁可漏一次，也别天天在她耳边念同一件事。
REL_DAY_WORDS = (("昨天", -1), ("昨日", -1), ("今天", 0), ("今晚", 0), ("今日", 0),
                  ("明天", 1), ("明日", 1), ("后天", 2))

#: 抽关键词时丢掉的虚词（只用于切词，别拿去过滤抓手）
_TERM_STOP = set("你我他她它们的了吗呢吧啊呀哦嗯和跟与在是有要会把被给对就都也很还再又才")

#: 抓手（笔记标题）只用轻过滤：标点与语气词。
#: ⚠️ 这里不能复用 _TERM_STOP ——「还书」的「还」在虚词表里，
#:    按虚词过滤会把抓手削成一个字，于是"换种说法"永远认不出来。
_HANDLE_STOP = set("的了呢吧啊呀哦嗯，。！？、~～「」『』（）()【】 \t\n")


@dataclass
class Promise:
    due: str                 # YYYY-MM-DD
    text: str                # 原话（截断）
    terms: List[str] = field(default_factory=list)   # 用来判"用户是不是在提这件事"
    handle: str = ""         # 短抓手（笔记标题那种两三个字），换种说法也认得出


def _shift_date(today: str, days: int) -> str:
    try:
        base = datetime.strptime(today, "%Y-%m-%d")
    except Exception:  # noqa: BLE001
        return today
    return (base + timedelta(days=int(days))).strftime("%Y-%m-%d")


def promise_terms(text: str, limit: int = 5) -> List[str]:
    """抽出能代表这件事的关键词：引号里的原话优先，其次去虚词后的 2-4 字片段。"""
    raw = str(text or "")
    terms: List[str] = []
    for quoted in re.findall(r"[「『\"]([^」』\"]{2,20})[」』\"]", raw):
        terms.append(_squeeze(quoted, 20))
    body = re.sub(r"[「『\"][^」』\"]*[」』\"]", " ", raw)
    chunk = ""
    for ch in body:
        if ch in _TERM_STOP or not ch.strip():
            if len(chunk) >= 2:
                terms.append(chunk)
            chunk = ""
            continue
        chunk += ch
        if len(chunk) >= 4:
            terms.append(chunk)
            chunk = ""
    if len(chunk) >= 2:
        terms.append(chunk)
    out: List[str] = []
    for t in terms:
        t = t.strip()
        if len(t) >= 2 and t not in out:
            out.append(t)
        if len(out) >= limit:
            break
    return out


def extract_promises(items: List[Dict[str, Any]], today: str) -> List[Promise]:
    """从「笔记 / 事件史」里挑出**带时间**的约定。没有时间的约定一律不收。

    ``items`` 每项可为 ``{"title","content","expire_at"}``。
    """
    out: List[Promise] = []
    for item in items or []:
        if not isinstance(item, dict):
            continue
        title = _squeeze(item.get("title"), 40)
        content = _squeeze(item.get("content"), 160)
        text = f"{title}：{content}" if title and content and title not in content else (content or title)
        if not text or not any(m in text for m in PROMISE_MARKERS):
            continue
        # 到期日判定：**先看文本里的时间词**（昨天/今天/明天…），
        # 再看笔记的 expire_at —— 而且只有它已经到期（≤ 今天）才算数。
        # ⚠️ 不能把未来的 expire_at 当成到期日：那是"这条笔记什么时候过期"，
        #    不是"她答应的事什么时候该做"。按前者判，一条挂 48 小时的笔记
        #    会显示成"后天到期"，从此永远不进提醒。
        due = ""
        for word, offset in REL_DAY_WORDS:
            if word in text:
                due = _shift_date(today, offset)
                break
        if not due:
            try:
                expire_at = int(item.get("expire_at") or 0)
            except (TypeError, ValueError):
                expire_at = 0
            if expire_at > 0:
                stamp = datetime.fromtimestamp(expire_at).strftime("%Y-%m-%d")
                if stamp <= today:
                    due = stamp
        if not due:
            continue
        out.append(Promise(due=due, text=_squeeze(text, 80), terms=promise_terms(text),
                           handle=_squeeze(title, 12) or (promise_terms(text) or [""])[0]))
    return out


def due_promises(promises: List[Promise], today: str) -> List[Promise]:
    """今天到期的（含已经过期的昨天/更早，逾期更要提）。"""
    return [p for p in promises or [] if p.due and p.due <= today]


def promise_line(promise: Promise) -> str:
    return f"（你还记着：{_squeeze(promise.text, 60)}——该提就提，别提就算了）"


def promise_hit(user_text: str, promise: Promise) -> bool:
    """用户是不是在提这件事。

    两档判据，宁可漏也别瞎认（认错了会莫名加分，比漏掉更糟）：
    1. 直接命中某个关键词；
    2. 换了一种说法 —— 抓手（笔记标题）里的字**大部分**都出现在对方话里。
       实测对方很少原样重复「把书还给她」，多半说「要还的那本书」，
       所以第二档是必需的，但门槛要高（至少 2 个字、且占比 ≥60%）。
    """
    text = str(user_text or "")
    if not text or promise is None:
        return False
    for term in (promise.terms or []):
        if len(term) >= 2 and term in text:
            return True
    handle = "".join(ch for ch in str(promise.handle or "") if ch.strip() and ch not in _HANDLE_STOP)
    if len(handle) >= 2:
        hits = sum(1 for ch in set(handle) if ch in text)
        if hits >= 2 and hits >= int(0.6 * len(set(handle)) + 0.999):
            return True
    return False

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
            return {"places": [], "events": [], "props": []}
        # ⚠️ 键名必须与 roll_card 写的一致（places/events）。
        #    曾经这里还留着旧的 scenes/interludes：读回来的永远是空表，
        #    冷却形同虚设 —— 实测连续 8 天抽到同一个地点。
        return {"places": [str(x) for x in (raw.get("places") or raw.get("scenes") or [])],
                "events": [str(x) for x in (raw.get("events") or raw.get("interludes") or [])],
                "props": [str(x) for x in (raw.get("props") or [])]}

    async def save_history(self, hist: Dict[str, List[str]]) -> None:
        try:
            await self._write(HISTORY_KEY, hist)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[life] 写抽签历史失败: {exc!r}")

    # ---- 抽签 ----
    def roll_card(self, date_key: str, hist: Dict[str, List[str]]) -> Tuple[DaySeed, Dict[str, List[str]]]:
        """抽今天的词元（保留旧名字，避免上层大改）。

        抽的是**词**不是句子：地点/手边物/会做的事/会发生的事/体感词，
        组合交给模型。冷却只压"地点"和"事件"，动作词允许重复（人本来就重复做同样的事）。
        """
        pack = load_world_pack()
        rng = _rng("seed", date_key)

        def _cool(window: int, size: int) -> int:
            """冷却窗口按池子大小自适应。

            池子只有 8 条却硬冷却 10 次，会一路「全在冷却」→ 退回全池 → 天天抽到同一条。
            实测就是这样把事件词抽成了 4/16 种。
            """
            return max(0, min(window, size - 2))

        cooled_places = set((hist.get("places") or [])[-_cool(SCENE_COOLDOWN, len(pack.places)):])
        place_pool = [p for p in pack.places if p not in cooled_places] or list(pack.places)
        place = rng.choice(place_pool) if place_pool else ""

        cooled_events = set((hist.get("events") or [])[-_cool(INTERLUDE_COOLDOWN, len(pack.events)):])
        event_pool = [e for e in pack.events if e not in cooled_events] or list(pack.events)
        rng.shuffle(event_pool)
        events = event_pool[:INTERLUDE_PER_DAY]

        cooled_props = set((hist.get("props") or [])[-_cool(4, len(pack.props)):])
        prop_pool = [p for p in pack.props if p not in cooled_props] or list(pack.props)
        prop = rng.choice(prop_pool) if prop_pool else ""

        verbs = rng.sample(list(pack.verbs), k=min(2, len(pack.verbs))) if pack.verbs else []
        feels = rng.sample(list(pack.feels), k=min(2, len(pack.feels))) if pack.feels else []

        seed = DaySeed(date_key, place, prop, verbs, events, feels)
        new_hist = {
            "places": (list(hist.get("places") or []) + [place])[-SCENE_COOLDOWN * 2:],
            "events": (list(hist.get("events") or []) + events)[-INTERLUDE_COOLDOWN * 3:],
            "props": (list(hist.get("props") or []) + [prop])[-8:],
        }
        return seed, new_hist

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
    async def _ask_model(self, prompt: str, *, system: str, task: str,
                         max_tokens: Optional[int] = None) -> Optional[str]:
        if self._ask is None:
            return None
        try:
            if max_tokens:
                try:
                    return await self._ask(prompt, system=system, task=task, max_tokens=max_tokens)
                except TypeError:
                    # 注入的 ask 实现不认识 max_tokens（测试替身/旧装配）→ 退回默认预算
                    pass
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

    # ---- 生成日程（可一次排多天）----
    def _horizon(self) -> int:
        """一次排几天。1 = 逐日（默认）；3 = 三天一批。"""
        try:
            n = int(self._cfg("LIFE_PLAN_HORIZON", 1) or 1)
        except (TypeError, ValueError):
            n = 1
        return max(1, min(7, n))

    def _plan_tokens(self, days: int) -> int:
        """按天数给输出预算。

        一天 12-18 段大约 2.6k 输出 token；多天不是简单线性 —— 每天的段数会收敛
        （见 ``_plan_prompt``），但摘要与 JSON 开销还在，所以按 1500/天 给余量。
        """
        # ⚠️ 上限压到 4096：实测把 max_tokens 抬到 6000 时供应商直接返回
        #    "非流式响应内容为空"，比截断更糟（连抢救都没得救）。
        return max(2000, min(4096, 1200 + 1100 * max(1, days)))

    async def rebuild_plans(self, state: DayState, dates: List[str], *, reason: str = "") -> DayState:
        if not dates:
            return state
        today = _today()
        # 抽签锚在**这批的第一天**：补将来日期时用那天的种子，而不是永远用今天的。
        anchor = dates[0]
        if not (state.seed and state.seed.date == anchor):
            seed, hist = self.roll_card(anchor, await self.load_history())
            await self.save_history(hist)
            state.seed = seed

        lo = max(3, min(16, int(self._cfg("LIFE_PLAN_MIN_SEGMENTS", PLAN_MIN_SEGMENTS))))
        hi = max(lo, min(PLAN_MAX_SEGMENTS, int(self._cfg("LIFE_PLAN_MAX_SEGMENTS", PLAN_MAX_SEGMENTS))))
        persona = await self._persona_text()
        system = (
            "你在替一个角色安排日子。她的设定如下，日子要贴着这个设定过。\n"
            f"【角色】\n{persona or '（设定缺失，按作息规律、生活平淡的普通人来写）'}"
        )
        if len(dates) > 1:
            # 多日时每天段数收敛：3 天 × 18 段塞不进 4096 的输出预算，
            # 被截断（甚至返回空）就全退兜底。6-10 段足够表达"此刻在做什么"。
            lo = max(5, lo - 6)
            hi = max(lo, min(10, hi - 8))
        raw = await self._ask_model(
            self._plan_prompt(state, lo, hi, dates), system=system,
            task=f"life_plan{':' + reason if reason else ''}:{len(dates)}d",
            max_tokens=self._plan_tokens(len(dates)),
        )
        parsed = self._parse_plans(raw or "", dates, hi)

        # 世界观校验：命中了"这个世界不存在"的词 → 点名重抽一次
        pack = load_world_pack()
        bad = {d: world_hits(self._plan_text(p), pack) for d, p in parsed.items()}
        bad = {d: w for d, w in bad.items() if w}
        if bad:
            names = "、".join(sorted({w for ws in bad.values() for w in ws}))
            logger.warning(f"[life] 日程越界（{names}），点名重抽一次：{', '.join(sorted(bad))}")
            retry = await self._ask_model(
                self._plan_prompt(state, lo, hi, dates)
                + f"\n\n上一次你写了「{names}」——这些东西在她的世界里不存在，重写，别再用。",
                system=system, task=f"life_plan:retry:{len(dates)}d",
                max_tokens=self._plan_tokens(len(dates)),
            )
            retried = self._parse_plans(retry or "", dates, hi)
            for day, plan in retried.items():
                if not world_hits(self._plan_text(plan), pack):
                    parsed[day] = plan

        for day in dates:
            plan = parsed.get(day)
            if plan is not None:
                ok, why = validate_plan(plan.segments)
                if not ok:
                    logger.info(f"[life] {day} 日程不合格（{why}），尝试修补")
                    fixed = repair_plan(plan.segments)
                    plan = DayPlan(plan.summary, fixed, source="repaired") \
                        if fixed and validate_plan(fixed)[0] else None
            if plan is None:
                logger.warning(f"[life] {day} 日程走兜底模板（模型不可用或输出不可用）")
                plan = template_plan()
            else:
                hits = world_hits(self._plan_text(plan), pack)
                if hits:
                    logger.warning(f"[life] {day} 仍有越界词（{'、'.join(hits)}），改写命中段")
                    plan = scrub_plan(plan, pack)
            state.plans[day] = plan
            state.recent = (state.recent + [{"date": day,
                                             "doings": [s.doing for s in plan.segments]}])[-7:]

        state.date = today
        if state.vitals is None:
            state.vitals = self.seed_vitals(state, today)
        await self.save(state)
        return state

    async def rebuild_plan(self, state: DayState, *, reason: str = "") -> DayState:
        """单日包装（保持旧调用点与测试不变）。"""
        return await self.rebuild_plans(state, [_today()], reason=reason)

    @staticmethod
    def _plan_text(plan: Optional[DayPlan]) -> str:
        if plan is None:
            return ""
        body = [plan.summary] + [f"{s.doing} {s.feel}" for s in plan.segments]
        return " ".join(body)

    def _plan_prompt(self, state: DayState, lo: int, hi: int,
                     dates: Optional[List[str]] = None) -> str:
        dates = dates or [_today()]
        multi = len(dates) > 1
        lines = [f"把接下来 {len(dates)} 天排出来。要求如下。" if multi else "把今天排出来。要求如下。", ""]
        if multi:
            lines.append("这几天是同一个人连续的日子，要像一条线：可以埋下铺垫"
                         "（今天累了、明天缓过来），但**不要每天各写各的**。")
            for day in dates:
                try:
                    short = day_label(datetime.strptime(day, "%Y-%m-%d")).rsplit(" ", 1)[0]
                except Exception:  # noqa: BLE001
                    short = day
                lines.append(f"· {day}（{short}）")
        else:
            # ⚠️ 必须用**目标日期**的标签，不能用 now()：
            #    补将来日期时会写出"今天是周日 假日"，模型于是把工作日当假日过。
            try:
                target = datetime.strptime(dates[0], "%Y-%m-%d")
            except Exception:  # noqa: BLE001
                target = None
            lines.append(f"今天：{day_label(target)}" if target else f"今天：{day_label()}")
        material = seed_block(state.seed)
        if material:
            lines.append(material)
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
            "**只在「今日素材」给出的世界与词元范围内写**；不要出现这个世界没有的地点、"
            "身份与作息（她不打卡、不通勤、没有同事和工位）。",
            "整天里至少有两三段是松快的，不要全写成没力气、发闷。",
            "summary 用一句话说今天的底色，口语。",
            "",
        ]
        if multi:
            lines += [
                "",
                f"每天都要排满一整天：**第一段必须从 00:00 开始，最后一段到 24:00 结束**"
                f"（夜里那段也要写，别从早上开排）；各自独立但前后呼应；"
                f"每天 {lo}-{hi} 段，段长不要平均分；doing 一句话说完，别铺陈。",
                '只回 JSON，不要解释：{"days": [{"date": "YYYY-MM-DD", "summary": "...", '
                '"segments": [{"window": "HH:MM-HH:MM", "doing": "...", "feel": "..."}]}]}',
            ]
        else:
            lines.append('只回 JSON，不要解释：'
                         '{"summary": "...", "segments": [{"window": "HH:MM-HH:MM", '
                         '"doing": "...", "feel": "..."}]}')
        return "\n".join(lines)

    def _parse_plans(self, raw: str, dates: List[str], hi: int) -> Dict[str, DayPlan]:
        """把模型输出拆成 {date: DayPlan}。缺的日期不补 —— 由调用方兜底。"""
        payload = _loose_json(raw)
        if not isinstance(payload, dict):
            # 整体 JSON 坏了（多半是被 max_tokens 截断）→ 抢救完整的那几天
            salvaged = extract_day_objects(raw)
            out: Dict[str, DayPlan] = {}
            for item in salvaged:
                day = _squeeze(item.get("date") or "", 10)
                if day not in dates:
                    continue
                plan = self._parse_one(item, hi)
                if plan is not None:
                    out[day] = plan
            logger.warning(
                f"[life] 日程 JSON 不完整（原始 {len(raw)} 字，结尾 {raw[-40:]!r}）→ "
                f"抢救出 {len(out)}/{len(dates)} 天：{', '.join(sorted(out)) or '无'}")
            return out
        items = payload.get("days")
        out: Dict[str, DayPlan] = {}
        if isinstance(items, list) and items:
            for item in items:
                if not isinstance(item, dict):
                    continue
                day = _squeeze(item.get("date") or "", 10)
                if day not in dates:
                    continue
                plan = self._parse_one(item, hi)
                if plan is not None:
                    out[day] = plan
            return out
        # 单日格式（模型没按多日输出）：只认第一天的
        single = self._parse_one(payload, hi)
        if single is not None and dates:
            out[dates[0]] = single
        return out

    def _parse_one(self, payload: Dict[str, Any], hi: int) -> Optional[DayPlan]:
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
            logger.warning(f"[life] 日程段数不足（{len(segments)} 段），单日 JSON {len(payload)} 键")
            return None
        segments.sort(key=lambda s: s.start)
        return DayPlan(_squeeze(payload.get("summary"), 80) or "普通的一天",
                       segments[:hi], source="model")

    def _parse_plan(self, raw: str, hi: int) -> Optional[DayPlan]:
        """单日包装（旧调用点与测试用）。"""
        payload = _loose_json(raw)
        if not isinstance(payload, dict):
            return None
        return self._parse_one(payload, hi)

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
        """保证「今天」以及后几天（LIFE_PLAN_HORIZON）都有骨架。永不抛异常。

        跨天时依次滚「梦 → 日程 → 手感」；同一天内只补缺失的日期（top-up），
        不重排已有的那天 —— 否则每来一条消息都可能把今天重写一遍。
        """
        today = _today()
        horizon = self._horizon()
        need = [_shift_date(today, i) for i in range(horizon)]

        keep_from = _shift_date(today, -1)

        def _ready(st: DayState) -> bool:
            # 除了「今天有骨架、未来几天齐了」，还要求没有过期的旧骨架 ——
            # 否则快路径会一直命中，裁剪永远轮不到执行。
            return (st.date == today and st.plan is not None
                    and all(d in st.plans for d in need)
                    and all(d >= keep_from for d in st.plans))

        state = await self.load()
        if _ready(state):
            return state
        async with self._lock:
            state = await self.load()
            if _ready(state):
                return state
            day_changed = state.date != today
            if day_changed:
                await self.rebuild_dream(state)   # 素材来自昨天的骨架，必须先于覆盖
                state = await self.load()
            missing = [d for d in need if d not in state.plans]
            if missing:
                state = await self.rebuild_plans(state, missing,
                                                 reason="daily" if day_changed else "topup")
            # 只保留「昨天 + 今天 + 未来几天」：更早的骨架没有用处，还占注入预算
            state.plans = {d: p for d, p in state.plans.items() if d >= keep_from}
            if state.vitals is None:
                state.vitals = self.seed_vitals(state, today)
            state.date = today
            await self.save(state)
            logger.info(
                f"[life] 已滚动到 {today}（排了 {len(state.plans)} 天: "
                f"{', '.join(sorted(state.plans))}；今天来源 "
                f"{state.plan.source if state.plan else '无'}）")
            return state

    # ---- 渲染 ----
    def render(self, state: DayState, *, chat_key: str = "", cap: Optional[int] = None,
               score: int = 0, mood: int = 0) -> str:
        """生活状态注入块。信息不全就少说几句，绝不编。

        ``score`` 决定**能知道多细**：还不熟（<20）只给"自己的事，不方便细说"，
        够熟才说具体在做什么 —— 关系变近的回报之一是"知道她更多日常细节"。
        ``mood`` 用来判要不要收着说话（见 ``hold_hint``）。
        """
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
            if seg is not None and int(score) >= 20:
                feel = f"（{_squeeze(seg.feel, 16)}）" if seg.feel else ""
                lines.append(f"此刻（{seg.window}）：{_squeeze(seg.doing, BLOCK_LINE_CAP)}{feel}")
            elif seg is not None:
                lines.append(f"此刻（{seg.window}）：自己的事，不太方便细说")

        if state.vitals is not None:
            lines.append(f"状态：能量 {max(0, min(100, state.vitals.energy))}/100，"
                         f"心情{_squeeze(state.vitals.mood, 16) or '平静'}")
            aches = "、".join(_squeeze(a.get("label"), 12) for a in state.vitals.aches if a.get("label"))
            if aches:
                lines.append(f"身上：{aches}")
        if state.dream and state.dream.text and state.dream.afterglow:
            lines.append(f"昨夜那点余韵：{_squeeze(state.dream.afterglow, 50)}")
        hint = hold_hint(state, mood=mood)
        if hint:
            lines.append(hint)

        body = "\n".join(lines)[: int(cap or self._cfg("LIFE_BLOCK_CAP", BLOCK_BODY_CAP))]
        tail = ("（这些只是你此刻的底子，不用主动交代；"
                "**别写进笔记或长期记忆**——它是会变的东西。)")
        return body + "\n" + tail

    # ---- 注入入口（B2：接进 persona_prompt） ----
    async def block_for(self, chat_key: str, *, now: Optional[float] = None,
                        score: int = 0, mood: int = 0) -> str:
        """给 prompt 用的生活块。**永不抛异常、永不阻塞回复**。

        两个设计约束：
        - 跨天那一轮**不在这里等生成**（要调模型，几秒钟）：本轮先返回空，
          后台滚一次，下一轮就有了。回复延迟比"少一句生活背景"重要得多。
        - 渲染结果按频道缓存 ``BLOCK_CACHE_TTL`` 秒，免得每轮都读一遍 store。
        """
        if not self.on or not bool(self._cfg("LIFE_INJECT_ENABLED", True)):
            return ""
        ts = _now() if now is None else float(now)
        date_key = _today()
        # 缓存键要带上「说得多细」和「要不要收着」—— 同一频道不同用户档位不同，
        # 只按 chat_key 缓存会把 A 的细化版本发给还不熟的 B。
        cache_key = (chat_key, date_key, int(score) >= 20, int(mood) <= -30, self._horizon())
        cached = _BLOCK_CACHE.get(cache_key)
        if cached is not None and ts - cached[0] < BLOCK_CACHE_TTL:
            return cached[1]

        state = await self.load()
        if state.date != date_key or state.plan is None:
            # 跨天：本轮不阻塞，后台滚一次（同一进程内同一频道只滚一次）
            if cache_key not in _ROLLING:
                _ROLLING.add(cache_key)

                async def _roll() -> None:
                    try:
                        await self.ensure_today()
                    except Exception as exc:  # noqa: BLE001
                        logger.warning(f"[life] 后台滚动失败: {exc!r}")
                    finally:
                        _ROLLING.discard(cache_key)
                        _BLOCK_CACHE.pop(cache_key, None)

                try:
                    asyncio.get_event_loop().create_task(_roll())
                except Exception:  # noqa: BLE001
                    _ROLLING.discard(cache_key)
            _BLOCK_CACHE[cache_key] = (ts, "")
            return ""

        # 手感衰减：节流到 ENERGY_TICK_SECONDS，避免每轮都写库
        global _LAST_TICK
        if state.vitals is not None and ts - _LAST_TICK >= ENERGY_TICK_SECONDS:
            _LAST_TICK = ts
            if self.tick(state, now=ts):
                await self.save(state)

        text = self.render(state, chat_key=chat_key, score=score, mood=mood)
        _BLOCK_CACHE[cache_key] = (ts, text)
        return text


# ---------------- 模块级装配 ----------------

_ENGINE: Optional[Engine] = None
_BLOCK_CACHE: Dict[Any, Tuple[float, str]] = {}
_ROLLING: set = set()
_LAST_TICK = 0.0


def attach(engine: Optional[Engine]) -> None:
    global _ENGINE
    _ENGINE = engine


def engine() -> Optional[Engine]:
    return _ENGINE


def is_on() -> bool:
    return bool(_ENGINE is not None and _ENGINE.on)
