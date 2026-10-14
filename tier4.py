"""Tier 4 生长层（身份锚点 + 未来 note）—— 独立于阶梯记忆的运行时可追加层。

设计定位（见 tier4_growth_layer_spec.md v3）：
- **tier1/2/3 = 种子**：手工策划、静态、模型不可写。
- **tier4 = 生长**：角色自己在运行时追加，记录"他怎么长成现在这样"。
  模型可追加，但受**硬频率闸**约束（审慎累积，不每轮都长）。

四条铁律的落地：
1. 审慎累积 → 本模块的频率闸原语（总量 / 每日 / 最小间隔）。
2. 只增不改种子 → 本模块**只读写 `backgrounds/tier4.json`**，绝不触碰 tier1/2/3/favor.json。
3. 作者可审计 → 提供 CRUD（list/delete/set_salience）供 WebUI；每次写在 plugin.py 侧记日志。
4. 触发可见 → 只有显式工具调用（anchor_memory）才写；渲染只读、不写。

存储：单一全局文件 `backgrounds/tier4.json`（S1）。
- 一把全局写锁（写稀疏，频率闸限死；读不加锁，仅写加锁）。
- 每条带 `kind`（note|anchor）+ `scope`（global|chat_key）。阶段 A 只用 anchor/global。

本模块**不 import plugin.py**（避免循环依赖、保持 golden 边界干净）。
文案（guide/确认串/拒绝理由）留在 plugin.py；这里只做确定性的存储与注入算法。
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from nekro_agent.core.logger import get_sub_logger

logger = get_sub_logger("persona_tier4")

_TIER4_NAME = "tier4.json"
_TIER4_VERSION = 1
_CHANNEL_INDEX_NAME = "_channel_index.json"
_CHANNEL_INDEX_VERSION = 1

_PKG_BG_DIR = Path(__file__).parent / "backgrounds"
_BG_DIR = _PKG_BG_DIR
_TIER4_FILE = _BG_DIR / _TIER4_NAME
_CHANNEL_INDEX_FILE = _BG_DIR / _CHANNEL_INDEX_NAME

ANCHOR_KIND = "anchor"
NOTE_KIND = "note"
GLOBAL_SCOPE = "global"


def configure_bg_dir(data_dir) -> Path:
    """把生长层数据目录切到插件数据目录（与 memory.configure_bg_dir 同一路径）。

    plugin.py 在初始化时会用同一个 backgrounds 目录调用两者，使 tier4.json 与
    阶梯记忆、hit_stats sidecar 落在一起。目录不可用时回退包内目录，保证仍可运行。
    """
    global _BG_DIR, _TIER4_FILE, _CHANNEL_INDEX_FILE
    target = Path(data_dir)
    try:
        target.mkdir(parents=True, exist_ok=True)
    except Exception:  # noqa: BLE001
        logger.exception(f"[tier4] 数据目录不可用，回退包内目录：{target}")
        target = _PKG_BG_DIR
    _BG_DIR = target
    _TIER4_FILE = target / _TIER4_NAME
    _CHANNEL_INDEX_FILE = target / _CHANNEL_INDEX_NAME
    # 目录变了：作废读缓存与频道索引的进程内已见集，下次按新目录重取
    _invalidate_read_cache()
    _known_channels.clear()
    global _known_channels_loaded
    _known_channels_loaded = False
    return target


def bg_dir() -> Path:
    """当前生长层数据目录。"""
    return _BG_DIR


def tier4_path() -> Path:
    """tier4.json 的绝对路径（WebUI 私有导出用）。"""
    return _TIER4_FILE


# ---------------- 小工具（本地，避免依赖 plugin.py）----------------
def _ts_now() -> int:
    return int(time.time())


def _day_start_ts(ts: int) -> int:
    """取 ts 当天 00:00 的时间戳（与 plugin._day_start_ts 同口径）。"""
    lt = time.localtime(ts)
    return int(time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1)))


def _clip(value: object, limit: int) -> str:
    """压平成单行并按字符数截断（渲染侧兜底，写入侧已处理）。"""
    return " ".join(str(value or "").split())[:limit]


def _norm_content(content: str) -> str:
    """去重用归一化：小写、去空白，用于精确匹配判重。"""
    return " ".join(str(content or "").lower().split())


# ---------------- 数据模型 ----------------
@dataclass
class Tier4Entry:
    """生长层单条记录。kind 决定行为，scope 决定归属。

    阶段 A 只用 kind=anchor / scope=global；note 字段先就位，阶段 B 迁移 memo 时启用。
    """

    id: str
    kind: str = ANCHOR_KIND
    scope: str = GLOBAL_SCOPE
    content: str = ""
    title: str = ""                      # note 用（检索关键词/去重键）；anchor 可空
    tags: List[str] = field(default_factory=list)
    min_favor: int = 0                   # note 受好感度门控；anchor 忽略
    expire_at: int = 0                   # note 可 TTL；anchor 恒 0
    salience: int = 1                    # anchor 核心度 1..3；note 忽略
    source_user_id: str = ""
    source_chat_key: str = ""
    created_at: int = 0
    updated_at: int = 0

    @classmethod
    def from_raw(cls, data: Dict[str, Any]) -> "Tier4Entry":
        d = data or {}
        return cls(
            id=str(d.get("id") or ""),
            kind=str(d.get("kind") or ANCHOR_KIND),
            scope=str(d.get("scope") or GLOBAL_SCOPE),
            content=str(d.get("content") or ""),
            title=str(d.get("title") or ""),
            tags=[str(x) for x in (d.get("tags") or []) if str(x).strip()],
            min_favor=int(d.get("min_favor") or 0),
            expire_at=int(d.get("expire_at") or 0),
            salience=int(d.get("salience") or 1),
            source_user_id=str(d.get("source_user_id") or ""),
            source_chat_key=str(d.get("source_chat_key") or ""),
            created_at=int(d.get("created_at") or 0),
            updated_at=int(d.get("updated_at") or 0),
        )

    def to_raw(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "scope": self.scope,
            "content": self.content,
            "title": self.title,
            "tags": list(self.tags),
            "min_favor": int(self.min_favor),
            "expire_at": int(self.expire_at),
            "salience": int(self.salience),
            "source_user_id": self.source_user_id,
            "source_chat_key": self.source_chat_key,
            "created_at": int(self.created_at),
            "updated_at": int(self.updated_at),
        }

    def is_expired(self, now: Optional[int] = None) -> bool:
        """note 的 TTL 判定；anchor 的 expire_at 恒 0，永不过期。"""
        if not self.expire_at:
            return False
        return self.expire_at <= (now if now is not None else _ts_now())


@dataclass
class Tier4Meta:
    """全局频率闸状态（阶段 A 只服务 anchor；note 的频率闸在阶段 B 独立）。"""

    day_key: int = 0
    day_count: int = 0
    last_anchor_at: int = 0

    @classmethod
    def from_raw(cls, data: Dict[str, Any]) -> "Tier4Meta":
        d = data or {}
        return cls(
            day_key=int(d.get("day_key") or 0),
            day_count=int(d.get("day_count") or 0),
            last_anchor_at=int(d.get("last_anchor_at") or 0),
        )

    def to_raw(self) -> Dict[str, Any]:
        return {
            "day_key": int(self.day_key),
            "day_count": int(self.day_count),
            "last_anchor_at": int(self.last_anchor_at),
        }

    def roll_day(self, now: int) -> None:
        """跨天把当日计数归零。读 day_count 前先调用。"""
        dk = _day_start_ts(int(now))
        if self.day_key != dk:
            self.day_key = dk
            self.day_count = 0


@dataclass
class Tier4Doc:
    """整份 tier4.json 的内存表示。"""

    version: int = _TIER4_VERSION
    meta: Tier4Meta = field(default_factory=Tier4Meta)
    entries: List[Tier4Entry] = field(default_factory=list)

    @classmethod
    def from_raw(cls, data: Dict[str, Any]) -> "Tier4Doc":
        d = data or {}
        entries = [Tier4Entry.from_raw(e) for e in (d.get("entries") or []) if isinstance(e, dict)]
        return cls(
            version=int(d.get("version") or _TIER4_VERSION),
            meta=Tier4Meta.from_raw(d.get("meta")),
            entries=entries,
        )

    def to_raw(self) -> Dict[str, Any]:
        return {
            "version": int(self.version),
            "meta": self.meta.to_raw(),
            "entries": [e.to_raw() for e in self.entries],
        }

    def of_kind(self, kind: str, *, scope: Optional[str] = None) -> List[Tier4Entry]:
        return [
            e for e in self.entries
            if e.kind == kind and (scope is None or e.scope == scope)
        ]


# ---------------- 读写（原子 + 全局写锁；读不加锁）----------------
# 读-改-写并发保护：与 _archive_lock 同理，tool / WebUI 都会写同一 blob。
# tier4 写极稀疏（频率闸限死），单全局锁无压力；渲染读路径不加锁。
_tier4_lock = asyncio.Lock()

# 渲染读缓存：按文件 mtime 复用，避免每轮每频道都重新解析。
# 仅在 mtime 变化时重读；写路径落盘后立即刷新，保证「刚写的锚点下一轮即可见」。
_read_cache: Dict[str, Any] = {"mtime": None, "size": None, "doc": None}


def _invalidate_read_cache() -> None:
    _read_cache["mtime"] = None
    _read_cache["size"] = None
    _read_cache["doc"] = None


def _fresh_doc() -> Tier4Doc:
    return Tier4Doc()


def _read_tier4_sync() -> Tier4Doc:
    """从磁盘读 tier4.json；缺失 / 版本不符 / 解析失败 → 空文档（fail-open，绝不影响渲染）。"""
    fp = _TIER4_FILE
    try:
        st = fp.stat()
    except FileNotFoundError:
        return _fresh_doc()
    except Exception:  # noqa: BLE001
        logger.debug(f"[tier4] 状态读取失败，按空处理: {fp}")
        return _fresh_doc()

    mtime, size = st.st_mtime, st.st_size
    if (
        _read_cache["doc"] is not None
        and _read_cache["mtime"] == mtime
        and _read_cache["size"] == size
    ):
        return _read_cache["doc"]
    try:
        data = json.loads(fp.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[tier4] 解析失败，本次按空文档继续: {exc}")
        doc = _fresh_doc()
    else:
        doc = Tier4Doc.from_raw(data) if isinstance(data, dict) else _fresh_doc()
        if doc.version != _TIER4_VERSION:
            logger.warning(f"[tier4] 版本不符（{doc.version}!={_TIER4_VERSION}），本次按空处理")
            doc = _fresh_doc()
    _read_cache["mtime"] = mtime
    _read_cache["size"] = size
    _read_cache["doc"] = doc
    return doc


def _write_tier4_sync(doc: Tier4Doc) -> None:
    """原子写盘；成功后刷新读缓存。失败只记日志（调用方已持有写锁）。"""
    fp = _TIER4_FILE
    tmp = fp.with_suffix(fp.suffix + ".tmp")
    try:
        tmp.write_text(json.dumps(doc.to_raw(), ensure_ascii=False), encoding="utf-8")
        tmp.replace(fp)
        # 刷新读缓存到新内容
        st = fp.stat()
        _read_cache["mtime"] = st.st_mtime
        _read_cache["size"] = st.st_size
        _read_cache["doc"] = doc
    except Exception as exc:  # noqa: BLE001
        _invalidate_read_cache()
        logger.warning(f"[tier4] 写入失败（不影响本次运行）: {exc}")


async def open_tier4() -> Tier4Doc:
    """读取当前生长层文档（不加锁；渲染与写入前的读都用它）。

    返回的 doc 与其 entries 是缓存持有的同一对象；调用方若要修改并写回，
    必须在 `tier4_lock()` 保护下调用 `store_tier4`，或改用下面的高层写函数。
    """
    return _read_tier4_sync()


async def store_tier4(doc: Tier4Doc) -> None:
    """整份写回（调用方负责持锁）。"""
    _write_tier4_sync(doc)


def tier4_lock() -> asyncio.Lock:
    """全局写锁。tool / WebUI 的读-改-写都应 `async with tier4_lock():`。"""
    return _tier4_lock


def entry_count(doc: Tier4Doc, kind: str = ANCHOR_KIND) -> int:
    """某 kind 的现存条数（总量闸用）。"""
    return len(doc.of_kind(kind))


# ---------------- 频率闸评估（纯函数，供 tool 组合拒绝文案）----------------
# 返回命中的闸码；"" 表示通过。
GATE_OK = ""
GATE_DISABLED = "disabled"
GATE_MAX_TOTAL = "max_total"
GATE_MAX_PER_DAY = "max_per_day"
GATE_INTERVAL = "interval"


def check_anchor_gates(doc: Tier4Doc, cfg: Dict[str, Any], now: int) -> str:
    """按当前 doc.meta（已 roll_day）与 cfg 判断能否再写一条 anchor。

    cfg 期望键：enabled / max_total / max_per_day / min_interval_hours。
    注意：调用前需先 `doc.meta.roll_day(now)`，否则 day_count 可能是昨天的值。
    """
    if not cfg.get("enabled", True):
        return GATE_DISABLED
    total = entry_count(doc, ANCHOR_KIND)
    max_total = int(cfg.get("max_total", 30))
    if total >= max_total:
        return GATE_MAX_TOTAL
    day_count = int(doc.meta.day_count)
    max_per_day = int(cfg.get("max_per_day", 1))
    if day_count >= max_per_day:
        return GATE_MAX_PER_DAY
    min_gap = max(0, int(cfg.get("min_interval_hours", 6))) * 3600
    if min_gap and doc.meta.last_anchor_at and (now - doc.meta.last_anchor_at) < min_gap:
        return GATE_INTERVAL
    return GATE_OK


# ---------------- 写入（高层原子；tool 用）----------------
def _new_id() -> str:
    try:
        import uuid

        return uuid.uuid4().hex[:10]
    except Exception:  # noqa: BLE001
        return f"{int(time.time()*1000)%10**10:010x}"


async def write_anchor(
    content: str,
    *,
    salience: int = 1,
    tags: Optional[List[str]] = None,
    source_user_id: str = "",
    source_chat_key: str = "",
    cfg: Dict[str, Any],
    salience_max: int = 3,
    dedup_matcher=None,
    now: Optional[int] = None,
) -> Dict[str, Any]:
    """频率闸 + 去重 + 落库，全程在**同一把**全局锁内完成（read-modify-write 原子）。

    tool 只调这一个函数即可：不要在 `tier4_lock()` 里再嵌套调用本函数——
    asyncio.Lock 不可重入，会死锁。

    返回 dict：
      {"action": "added"|"refreshed"|"rejected", "gate": 闸码或 "",
       "entry": 新/被刷新条目的 to_raw(), "total": 现 anchor 总数, "day_count": 当日已写}
    - rejected  → gate ∈ {disabled,max_total,max_per_day,interval}
    - refreshed → dedup_matcher 命中已有条目，就地刷新而非新增（不 bump 当日计数）
    - added     → 新增并 bump meta.day_count / last_anchor_at
    """
    now = _ts_now() if now is None else int(now)
    hi = max(1, int(salience_max))
    clean_salience = max(1, min(hi, int(salience or 1)))
    clean_content = _clip(content, int(cfg.get("max_content", 200) or 200))
    clean_tags = [str(x).strip()[:12] for x in (tags or []) if str(x).strip()][:6]

    async with _tier4_lock:
        doc = _read_tier4_sync()
        doc.meta.roll_day(now)

        gate = check_anchor_gates(doc, cfg, now)
        if gate != GATE_OK:
            return {
                "action": "rejected", "gate": gate, "entry": None,
                "total": entry_count(doc, ANCHOR_KIND), "day_count": int(doc.meta.day_count),
            }

        # 可选去重：命中已有 anchor 则就地刷新（不占新条、不计当日额度）
        if dedup_matcher is not None:
            existing = doc.of_kind(ANCHOR_KIND, scope=GLOBAL_SCOPE)
            match = next((e for e in existing if dedup_matcher(clean_content, e)), None)
            if match is not None:
                match.content = clean_content
                match.salience = clean_salience
                if clean_tags:
                    match.tags = clean_tags
                match.updated_at = now
                _write_tier4_sync(doc)
                return {
                    "action": "refreshed", "gate": "", "entry": match.to_raw(),
                    "total": entry_count(doc, ANCHOR_KIND), "day_count": int(doc.meta.day_count),
                }

        entry = Tier4Entry(
            id=f"t4_{_new_id()}",
            kind=ANCHOR_KIND,
            scope=GLOBAL_SCOPE,
            content=clean_content,
            tags=clean_tags,
            salience=clean_salience,
            source_user_id=str(source_user_id or ""),
            source_chat_key=str(source_chat_key or ""),
            created_at=now,
            updated_at=now,
        )
        doc.entries.append(entry)
        doc.meta.day_count = int(doc.meta.day_count) + 1
        doc.meta.last_anchor_at = now
        _write_tier4_sync(doc)
        return {
            "action": "added", "gate": "", "entry": entry.to_raw(),
            "total": entry_count(doc, ANCHOR_KIND), "day_count": int(doc.meta.day_count),
        }


# ---------------- 单条改写（self-lock；WebUI 用）----------------
async def list_entries(kind: Optional[str] = None) -> List[Tier4Entry]:
    """只读快照（每条为独立副本），供 WebUI / 渲染前检查用。"""
    doc = _read_tier4_sync()
    items = doc.entries if kind is None else doc.of_kind(kind)
    return [Tier4Entry(**e.to_raw()) for e in items]


async def delete_entry(entry_id: str) -> bool:
    """按 id 删除一条。返回是否删了。"""
    async with _tier4_lock:
        doc = _read_tier4_sync()
        before = len(doc.entries)
        doc.entries = [e for e in doc.entries if e.id != entry_id]
        if len(doc.entries) == before:
            return False
        _write_tier4_sync(doc)
        return True


async def set_salience(entry_id: str, value: int, salience_max: int = 3) -> bool:
    """调整某条 anchor 的核心度（钳到 [1, salience_max]）。返回是否命中。"""
    async with _tier4_lock:
        doc = _read_tier4_sync()
        target = next((e for e in doc.entries if e.id == entry_id and e.kind == ANCHOR_KIND), None)
        if target is None:
            return False
        hi = max(1, int(salience_max))
        target.salience = max(1, min(hi, int(value or 1)))
        target.updated_at = _ts_now()
        _write_tier4_sync(doc)
        return True


# ---------------- 注入选择 + 渲染（§5.1：预检 + salience 单调填充）----------------
def select_anchors(
    anchors: List[Tier4Entry],
    *,
    budget: int,
    max_count: int,
) -> List[Tier4Entry]:
    """salience 单调填充（v3 定稿），返回被选中注入的条目（保持注入顺序）。

    不变量：若某条 salience=S 的 anchor 未注入，当且仅当
      ① 它单独就超 `budget`，或
      ② 排序在它之前、salience ≥ S 的条目已把预算占满。
    **绝不允许**：因前面某条更长，把本该排在前的高 salience 锚点跳过、却注入了更低 salience 的。

    做法：
      · 预检剔除单条就超预算的（留着只会挡道）。
      · 稳定排序 salience desc, created_at desc。
      · 单调闸 min_salience_seen：一旦注入过某更低档，绝不再回填更高档（保序）。
      · 放不下的先 continue（同/更低档更短时可补位）。
    """
    budget = max(0, int(budget))
    cap = max(0, int(max_count))
    if budget <= 0 or cap == 0:
        return []

    # ① 排序前预检：单条超总预算的直接剔除
    pool = [a for a in anchors if 0 < len(a.content) <= budget]
    # ② 稳定排序
    pool.sort(key=lambda a: (-int(a.salience), -int(a.created_at)))

    out: List[Tier4Entry] = []
    used = 0
    min_salience_seen = 10**9  # 单调闸：已注入过的最低 salience
    for a in pool:
        # ③ 已允许过更低档 → 绝不再回填更高档（保证注入顺序单调、核心不被低档挤掉）
        if int(a.salience) > min_salience_seen:
            break
        blk = len(a.content)
        if used + blk > budget:
            continue  # 放不下的先跳过，同/更低档更短者仍可补位
        out.append(a)
        used += blk
        min_salience_seen = int(a.salience)
        if cap and len(out) >= cap:
            break
    return out


def render_anchor_block(
    doc: Tier4Doc,
    cfg: Dict[str, Any],
    guide_text: str = "",
    *,
    now: Optional[int] = None,
) -> str:
    """渲染身份锚点注入块（阶段 A：仅 kind=anchor / scope=global）。

    cfg 期望键：enabled / inject_chars / inject_max / guide_always。
    空 tier4（或未启用且无引导）→ 返回 ""，保证 persona_prompt 逐字节不变。
    """
    if not cfg.get("enabled", True):
        return ""
    guide_always = bool(cfg.get("guide_always", True))
    anchors = [
        a for a in doc.of_kind(ANCHOR_KIND, scope=GLOBAL_SCOPE)
        if not a.is_expired(now)
    ]
    if not anchors:
        return guide_text if guide_always else ""

    picked = select_anchors(
        anchors,
        budget=int(cfg.get("inject_chars", 1200) or 0),
        max_count=int(cfg.get("inject_max", 8) or 0),
    )
    if not picked:
        return guide_text if guide_always else ""

    lines: List[str] = []
    for a in picked:
        lines.append(f"· {a.content}")
    if not lines:
        return guide_text if guide_always else ""
    if guide_text:
        return guide_text + "\n" + "\n".join(lines)
    return "\n".join(lines)


# ==================================================================
# 阶段 B：kind=note —— 频道级功能笔记（接替原 memo 子系统）
# ==================================================================
# render_note_block 是对 plugin._render_memo_block 的**逐字节复刻**：同样的
# 过滤（未过期 + min_favor≤score）、同样的排序（updated_at desc）、同样的
# 选取（最近 inject_recent 条 + 话题命中 inject_matched 条）、同样的行格式
# `· 【title】content`、同样的预算 break、同样的 guide 兜底。迁移 golden 靠
# 「旧 memo 注入 == 新 note 注入」逐字节一致来证明「重生不改行为」。
_note_punct_re = None


def _note_title_key(title: str) -> str:
    """标题归一化：去空白与标点、转小写（与旧 _memo_title_key 完全一致）。"""
    global _note_punct_re
    if _note_punct_re is None:
        import re as _re

        _note_punct_re = _re.compile(
            r"[\s，。、；：！？,.;:!?「」『』（）()\[\]【】\"'<>·—~～]+"
        )
    return _note_punct_re.sub("", str(title or "")).lower()


def _note_similarity(a: str, b: str) -> float:
    ka, kb = _note_title_key(a), _note_title_key(b)
    if not ka or not kb:
        return 0.0
    if ka == kb:
        return 1.0
    from difflib import SequenceMatcher

    return SequenceMatcher(None, ka, kb).ratio()


def _note_match_terms(note: Tier4Entry) -> List[str]:
    """话题匹配词：显式 tags 优先，标题整体作兜底（同旧 _memo_match_terms）。"""
    terms = [str(x).strip().lower() for x in (note.tags or []) if str(x).strip()]
    tk = _note_title_key(note.title)
    if len(tk) >= 2:
        terms.append(tk)
    return terms


def _note_key(note: Tier4Entry) -> str:
    return note.id or note.title


def find_note(
    doc: Tier4Doc, chat_key: str, title: str, *, dedup_min_len: int = 2
) -> Optional[Tier4Entry]:
    """在某频道的 note 里按标题查找（精确 → 包含 → 近同 0.92）。同旧 _find_memo。"""
    key = _note_title_key(title)
    if not key:
        return None
    notes = [n for n in doc.entries if n.kind == NOTE_KIND and n.scope == chat_key]
    for n in notes:  # ① 精确
        if _note_title_key(n.title) == key:
            return n
    min_len = max(2, int(dedup_min_len or 2))
    for n in notes:  # ② 包含
        mk = _note_title_key(n.title)
        shorter = mk if len(mk) <= len(key) else key
        if len(shorter) >= min_len and (mk in key or key in mk):
            return n
    for n in notes:  # ③ 近同（高阈值兜底；中低阈值会误合并「小明/小红生日」）
        if _note_similarity(title, n.title) >= 0.92:
            return n
    return None


def render_note_block(
    doc: Tier4Doc,
    cfg: Dict[str, Any],
    chat_key: str,
    score: int,
    recent_text: str,
    guide_text: str = "",
    *,
    now: Optional[int] = None,
) -> str:
    """渲染频道级 note 注入块（对旧 _render_memo_block 的逐字节复刻）。"""
    if not cfg.get("enabled", True):
        return ""
    guide_always = bool(cfg.get("guide_always", True))
    now = _ts_now() if now is None else now

    live = [
        n for n in doc.entries
        if n.kind == NOTE_KIND and n.scope == chat_key
        and not n.is_expired(now) and int(n.min_favor or 0) <= score
    ]
    if not live:
        return guide_text if guide_always else ""
    live.sort(key=lambda n: -int(n.updated_at or 0))

    picked: List[Tier4Entry] = []
    seen = set()
    inject_recent = int(cfg.get("inject_recent", 2) or 0)
    for n in live[:max(0, inject_recent)]:
        picked.append(n)
        seen.add(_note_key(n))

    inject_matched = int(cfg.get("inject_matched", 3) or 0)
    hit = 0
    if recent_text and inject_matched > 0:
        low = recent_text.lower()
        for n in live:
            if _note_key(n) in seen:
                continue
            if any(term and term in low for term in _note_match_terms(n)):
                picked.append(n)
                seen.add(_note_key(n))
                hit += 1
                if hit >= inject_matched:
                    break
    if not picked:
        return guide_text if guide_always else ""

    inject_chars = int(cfg.get("inject_chars", 900) or 0)
    lines: List[str] = []
    used = 0
    for n in picked:
        blk = f"· 【{n.title}】{n.content}"
        if used + len(blk) > inject_chars:
            break
        lines.append(blk)
        used += len(blk)
    if not lines:
        return guide_text if guide_always else ""
    return guide_text + "\n" + "\n".join(lines)


async def add_or_update_note(
    chat_key: str,
    title: str,
    content: str,
    *,
    ttl_hours: int = 0,
    min_favor: int = 0,
    tags: Optional[List[str]] = None,
    source_user_id: str = "",
    cfg: Dict[str, Any],
) -> Dict[str, Any]:
    """新增或覆盖一条频道 note（同 save_memo 的标题去重语义）。锁内 read-modify-write。

    cfg：enabled / max_items / max_content / dedup_min_len。
    返回 {"action": created|updated|rejected|disabled, "note": entry|None, "reason": ...}
    """
    if not cfg.get("enabled", True):
        return {"action": "disabled", "note": None, "reason": "disabled"}
    clean_title = _clip(title, 40)
    if not clean_title:
        return {"action": "rejected", "note": None, "reason": "empty_title"}
    max_content = int(cfg.get("max_content", 400) or 400)
    clean_content = _clip(content, max_content + 1)
    if not clean_content:
        return {"action": "rejected", "note": None, "reason": "empty_content"}
    if len(clean_content) > max_content:
        return {"action": "rejected", "note": None, "reason": "too_long"}

    now = _ts_now()
    ttl = max(0, int(ttl_hours or 0))
    expire_at = now + ttl * 3600 if ttl else 0
    favor = max(0, min(100, int(min_favor or 0)))
    clean_tags = [str(x).strip()[:12] for x in (tags or []) if str(x).strip()][:6]

    async with _tier4_lock:
        doc = _read_tier4_sync()
        existing = find_note(
            doc, chat_key, clean_title,
            dedup_min_len=int(cfg.get("dedup_min_len", 2) or 2),
        )
        if existing is not None:
            existing.title = clean_title
            existing.content = clean_content
            if clean_tags:
                existing.tags = clean_tags
            existing.min_favor = favor
            existing.expire_at = expire_at
            existing.updated_at = now
            _write_tier4_sync(doc)
            return {"action": "updated", "note": existing.to_raw(), "reason": ""}
        live_in_scope = [
            n for n in doc.entries if n.kind == NOTE_KIND and n.scope == chat_key
            and not n.is_expired(now)
        ]
        if len(live_in_scope) >= int(cfg.get("max_items", 80) or 80):
            return {"action": "rejected", "note": None, "reason": "max_items"}
        entry = Tier4Entry(
            id=f"note_{_new_id()}",
            kind=NOTE_KIND,
            scope=chat_key,
            content=clean_content,
            title=clean_title,
            tags=clean_tags,
            min_favor=favor,
            expire_at=expire_at,
            source_user_id=str(source_user_id or ""),
            source_chat_key=chat_key,
            created_at=now,
            updated_at=now,
        )
        doc.entries.append(entry)
        _write_tier4_sync(doc)
        return {"action": "created", "note": entry.to_raw(), "reason": ""}


async def forget_note(chat_key: str, title: str) -> Optional[str]:
    """删除某频道匹配 title 的一条 note（沿用 find_note 的三级匹配）。返回被删条目标题。"""
    async with _tier4_lock:
        doc = _read_tier4_sync()
        target = find_note(doc, chat_key, title)
        if target is None:
            return None
        doc.entries = [e for e in doc.entries if e.id != target.id]
        _write_tier4_sync(doc)
        return target.title


async def clear_expired_notes(chat_key: str) -> int:
    """删除某频道已过期的 note，返回删除数。复刻旧 WebUI clear_expired 语义。"""
    now = _ts_now()
    async with _tier4_lock:
        doc = _read_tier4_sync()
        before = len(doc.entries)
        doc.entries = [
            e for e in doc.entries
            if not (e.kind == NOTE_KIND and e.scope == chat_key and e.is_expired(now))
        ]
        removed = before - len(doc.entries)
        if removed:
            _write_tier4_sync(doc)
        return removed


# ---------------- 迁移：ChannelArchive.memos → tier4 note（纯函数，无 IO）----------------
# 编排（枚举频道、备份 blob、清 archive、写报告、落库）在 plugin.py B3 做；
# 这里只做「一批 memo dict → 一批 note 条目 + 去重」的纯映射，便于离线单测。
def plan_memo_migration(
    doc: Tier4Doc,
    memos_by_chat: Dict[str, List[Dict[str, Any]]],
) -> Dict[str, Any]:
    """把各频道 memos 规划为待追加的 Tier4Entry(kind=note)。

    幂等：以 `note_{原 memo.id}` 为条目 id，若已存在同 id 则跳过（防止 tier4 写成功
    但 archive 清空失败后重跑重复迁移）。原 memo.id 缺失时按内容派生 id。
    返回 {"new_entries": [...], "migrated": {chat_key: 计数}, "skipped": 计数,
          "errors": [{"chat_key","memo_id","error"}]}
    """
    existing_ids = {e.id for e in doc.entries}
    new_entries: List[Tier4Entry] = []
    migrated: Dict[str, int] = {}
    skipped = 0
    errors: List[Dict[str, Any]] = []
    for chat_key, memos in (memos_by_chat or {}).items():
        ck = str(chat_key or "").strip()
        if not ck or not isinstance(memos, list):
            continue
        for memo in memos:
            if not isinstance(memo, dict):
                errors.append({"chat_key": ck, "memo_id": "", "error": "not_a_dict"})
                continue
            mid = str(memo.get("id") or "").strip()
            content = str(memo.get("content") or "").strip()
            if not mid and not content:
                continue
            new_id = f"note_{mid}" if mid else f"note_{_hash_id(ck, content)}"
            if new_id in existing_ids:
                skipped += 1
                continue
            try:
                entry = Tier4Entry(
                    id=new_id,
                    kind=NOTE_KIND,
                    scope=ck,
                    content=str(memo.get("content") or ""),
                    title=str(memo.get("title") or ""),
                    tags=[str(x) for x in (memo.get("tags") or []) if str(x).strip()],
                    min_favor=int(memo.get("min_favor") or 0),
                    expire_at=int(memo.get("expire_at") or 0),
                    source_user_id=str(memo.get("source_user_id") or ""),
                    source_chat_key=ck,
                    created_at=int(memo.get("created_at") or 0),
                    updated_at=int(memo.get("updated_at") or 0),
                )
            except Exception as exc:  # noqa: BLE001
                errors.append({"chat_key": ck, "memo_id": mid, "error": f"{type(exc).__name__}:{exc}"})
                continue
            new_entries.append(entry)
            existing_ids.add(new_id)
            migrated[ck] = migrated.get(ck, 0) + 1
    return {
        "new_entries": new_entries,
        "migrated": migrated,
        "skipped": skipped,
        "errors": errors,
    }


def _hash_id(chat_key: str, content: str) -> str:
    import hashlib

    return hashlib.sha1(f"{chat_key}\x00{content}".encode("utf-8")).hexdigest()[:10]


async def apply_memo_migration(plan: Dict[str, Any]) -> int:
    """把 plan 的新条目并入 tier4.json（锁内一次写）。返回实际新增条数。"""
    new_entries = plan.get("new_entries") or []
    if not new_entries:
        return 0
    async with _tier4_lock:
        doc = _read_tier4_sync()
        have = {e.id for e in doc.entries}
        added = 0
        for e in new_entries:
            if e.id in have:
                continue
            doc.entries.append(e)
            have.add(e.id)
            added += 1
        if added:
            _write_tier4_sync(doc)
        return added


# ---------------- 迁移报告 _migrations.json（删子系统后唯一审计来源）----------------
_MIGRATION_FILE_NAME = "_migrations.json"


def _migration_path() -> Path:
    return _BG_DIR / _MIGRATION_FILE_NAME


async def save_migration_report(report: Dict[str, Any]) -> None:
    """原子写迁移报告（供审计：某频道 memo 到底迁没迁、有无错误）。"""
    payload = dict(report)
    payload.setdefault("schema", 2)
    tmp = _migration_path().with_suffix(_migration_path().suffix + ".tmp")
    try:
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(_migration_path())
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[tier4] 迁移报告写入失败: {exc}")


def load_migration_report() -> Dict[str, Any]:
    try:
        data = json.loads(_migration_path().read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except Exception:  # noqa: BLE001
        return {}


# ---------------- 频道索引（阶段 A 建，为阶段 C 导出生长包铺路）----------------
# PluginStore 无法枚举 chat_key（事实三）；自维护一份轻量索引，导出时 ∪ DBChatMessage。
_channel_index_lock = asyncio.Lock()
# 进程内已见频道集合：_store_archive 每条消息都会调 note_channel，
# 让「已在索引里的频道」走一次集合查表即返回，避免每轮抢锁 + 读写文件。
_known_channels: set[str] = set()
_known_channels_loaded = False


async def note_channel(chat_key: str) -> bool:
    """把一个 chat_key 去重追加进 _channel_index.json。

    每次 `_store_archive()` 成功后调用（见 plugin.py A2 接线）。返回是否新增了。
    """
    global _known_channels_loaded
    ck = str(chat_key or "").strip()
    if not ck:
        return False
    if not _known_channels_loaded:
        async with _channel_index_lock:
            if not _known_channels_loaded:
                _known_channels.update(_load_channel_index_sync())
                _known_channels_loaded = True
    if ck in _known_channels:  # 热路径：集合命中即返回，不抢锁不读盘
        return False
    async with _channel_index_lock:
        if ck in _known_channels:
            return False
        keys = _load_channel_index_sync()
        if ck not in keys:
            keys.append(ck)
            _save_channel_index_sync(keys)
        _known_channels.add(ck)
        return True


def _load_channel_index_sync() -> List[str]:
    try:
        data = json.loads(_CHANNEL_INDEX_FILE.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[tier4] 频道索引读取失败，按空处理: {exc}")
        return []
    if not isinstance(data, dict):
        return []
    return [str(x) for x in (data.get("chat_keys") or []) if str(x).strip()]


def _save_channel_index_sync(keys: List[str]) -> None:
    payload = {"version": _CHANNEL_INDEX_VERSION, "chat_keys": keys}
    tmp = _CHANNEL_INDEX_FILE.with_suffix(_CHANNEL_INDEX_FILE.suffix + ".tmp")
    try:
        tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        tmp.replace(_CHANNEL_INDEX_FILE)
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[tier4] 频道索引写入失败（不影响本次运行）: {exc}")


async def load_channel_index() -> List[str]:
    """读取已记录的频道键列表（阶段 C 导出用；此处供审计/测试）。"""
    return _load_channel_index_sync()
