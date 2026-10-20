"""关系记忆（memory log）—— 独立于种子层与 tier4 的「事件史」。

设计要点（与 tier4 note 的分工）：
  · tier4 note  = 角色自己记的功能小抄（约定/地址/代号），模型主动写，80 条/频道
  · memory log  = 系统自动沉淀的**事件史**（谁做了什么、发生了什么），按时间线组织

与好感度解耦：素材来自 exec_code 里 adjust_favorability 的 reason，
**不看 delta、不看成功与否** —— 严格模式下规则拒绝的调用同样贡献素材，
因为「事件发生了」与「分数涨没涨」是正交的。

存储：backgrounds/memory_log.json（与 tier4.json 同目录，跟随账号上下文）
清理：复用 tier2/3 已验证的热度机制（importance × 时间衰减），不是简单 LRU。
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from nekro_agent.core.logger import get_sub_logger

logger = get_sub_logger("persona_memory_log")

# ==================== 常量 ====================

MEMLOG_FILE = "memory_log.json"
MEMLOG_VERSION = 1

# 热度参数（与 memory.py 的 lifecycle 同口径，但独立配置）
HEAT_HALF_LIFE_DAYS = 30.0
HEAT_FLOOR = 0.55
HEAT_REINFORCE_STEP = 0.10
UNSEEN_PENALTY = 0.7
"""从未被注入过的记忆的基准惩罚系数。

为什么需要：新条目 last_hit=0，若直接拿 created_at 当基准会得到「刚更新」
的满分，和「常被想起」的老条目平票，淘汰时退化成纯按时间删最旧。
乘 0.7 后，刚创建的条目热度约 0.79，低于任何命中过的条目 ——
「先被用过，才留住」符合记忆的选择压力。
"""

# 每频道容量上限（超出按热度淘汰）
MAX_ENTRIES_PER_SCOPE = 200


# ==================== 目录提供者（与 tier4 同款）====================

_bg_dir_provider: Optional[Callable[[], Path]] = None
_PKG_BG_DIR = Path(__file__).parent / "backgrounds"
_read_cache: Dict[Path, Dict[str, Any]] = {}
_lock = asyncio.Lock()


def configure_bg_dir_provider(provider: Optional[Callable[[], Path]]) -> None:
    """注册「背景数据根目录」提供者（每次访问时解析，跟随账号上下文）。"""
    global _bg_dir_provider
    _bg_dir_provider = provider


def _resolve_bg_dir() -> Path:
    provider = _bg_dir_provider
    if provider is None:
        return _PKG_BG_DIR
    try:
        target = Path(provider())
        target.mkdir(parents=True, exist_ok=True)
        return target
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[memory_log] 数据目录不可用，回退包内目录：{exc}")
        return _PKG_BG_DIR


def bg_dir() -> Path:
    return _resolve_bg_dir()


def _log_file() -> Path:
    return _resolve_bg_dir() / MEMLOG_FILE


def log_path() -> Path:
    """当前作用域的 memory_log.json 路径（供 WebUI 展示）。"""
    return _log_file()


# ==================== 数据结构 ====================


@dataclass
class MemEntry:
    """一条关系记忆（事件）。"""

    id: str
    scope: str = ""                  # chat_key
    content: str = ""                # 事件描述（LLM 归纳后）
    title: str = ""                  # 简短标题（同时作检索词）
    tags: List[str] = field(default_factory=list)
    event_at: int = 0                # 事件发生时间
    created_at: int = 0
    updated_at: int = 0
    # 热度机制
    hits: int = 0                    # 被注入次数
    last_hit: int = 0                # 上次被注入时间
    # 溯源
    source: str = "auto"             # auto | manual
    ref_msg_ids: List[str] = field(default_factory=list)

    @classmethod
    def from_raw(cls, d: Dict[str, Any]) -> "MemEntry":
        d = d or {}
        return cls(
            id=str(d.get("id") or ""),
            scope=str(d.get("scope") or ""),
            content=str(d.get("content") or ""),
            title=str(d.get("title") or ""),
            tags=[str(x) for x in (d.get("tags") or [])],
            event_at=int(d.get("event_at") or 0),
            created_at=int(d.get("created_at") or 0),
            updated_at=int(d.get("updated_at") or 0),
            hits=int(d.get("hits") or 0),
            last_hit=int(d.get("last_hit") or 0),
            source=str(d.get("source") or "auto"),
            ref_msg_ids=[str(x) for x in (d.get("ref_msg_ids") or [])],
        )

    def to_raw(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "scope": self.scope,
            "content": self.content,
            "title": self.title,
            "tags": list(self.tags),
            "event_at": self.event_at,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "hits": self.hits,
            "last_hit": self.last_hit,
            "source": self.source,
            "ref_msg_ids": list(self.ref_msg_ids),
        }


# ==================== 热度（参照 memory.py 的 lifecycle）====================


def heat_factor(entry: MemEntry, now: Optional[float] = None) -> float:
    """热度系数 ∈ [HEAT_FLOOR, 1]。

    与 memory.heat_factor 同思路：久未命中 → 趋近 floor；常被命中 → 抬升。

    ⚠️ 「从未被命中」必须与「刚命中过」区分开，否则新条目（last_hit=0）
    会被当成最新鲜的，淘汰时和热门条目平票 → 退化成纯按时间删最旧，
    把「老但常被想起」的记忆误删。这里用两条规则保证区分：
      ① 从未命中：基准时间取 created_at 且额外乘 UNSEEN_PENALTY；
      ② 命中过：基准取 last_hit，按半衰期衰减 + hits 强化。
    """
    now = now if now is not None else time.time()
    hits = int(entry.hits or 0)
    last_hit = int(entry.last_hit or 0)

    if last_hit <= 0:
        # 从未被注入过：以创建时间为基准，并施加「未经检验」惩罚
        anchor = int(entry.created_at or entry.event_at or 0)
        if anchor <= 0:
            base = 0.0
        else:
            days = max(0.0, (now - anchor) / 86400.0)
            base = (0.5 ** (days / HEAT_HALF_LIFE_DAYS)) * UNSEEN_PENALTY
    else:
        days = max(0.0, (now - last_hit) / 86400.0)
        base = 0.5 ** (days / HEAT_HALF_LIFE_DAYS)

    reinforce = min(0.3, hits * HEAT_REINFORCE_STEP)
    heat = HEAT_FLOOR + (1.0 - HEAT_FLOOR) * min(1.0, base + reinforce)
    return max(HEAT_FLOOR, min(1.0, heat))


# ==================== 读写 ====================


def _read_sync() -> Dict[str, Any]:
    """读全量文档（带 mtime+size 缓存）。失败返回空结构。"""
    fp = _log_file()
    try:
        st = fp.stat()
    except FileNotFoundError:
        return {"version": MEMLOG_VERSION, "entries": []}
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[memory_log] 读取失败：{exc}")
        return {"version": MEMLOG_VERSION, "entries": []}

    cache = _read_cache.get(fp.parent) or {}
    if (cache.get("mtime") == st.st_mtime and cache.get("size") == st.st_size
            and cache.get("doc") is not None):
        return cache["doc"]

    try:
        data = json.loads(fp.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("根节点不是对象")
        data.setdefault("version", MEMLOG_VERSION)
        data.setdefault("entries", [])
        if not isinstance(data["entries"], list):
            data["entries"] = []
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[memory_log] 解析失败，按空处理：{exc}")
        data = {"version": MEMLOG_VERSION, "entries": []}

    _read_cache[fp.parent] = {"mtime": st.st_mtime, "size": st.st_size,
                              "doc": data}
    return data


def _write_sync(doc: Dict[str, Any]) -> None:
    """原子写盘；成功后刷新缓存。失败只记日志。"""
    fp = _log_file()
    tmp = fp.with_suffix(fp.suffix + ".tmp")
    try:
        tmp.write_text(json.dumps(doc, ensure_ascii=False, indent=1),
                       encoding="utf-8")
        tmp.replace(fp)
        st = fp.stat()
        _read_cache[fp.parent] = {"mtime": st.st_mtime, "size": st.st_size,
                                  "doc": doc}
    except Exception as exc:  # noqa: BLE001
        _read_cache.pop(fp.parent, None)
        logger.warning(f"[memory_log] 写入失败：{exc}")


def _new_id() -> str:
    return f"mem_{int(time.time() * 1000):x}_{int(time.time_ns() % 100000):05d}"


# ==================== 对外 API ====================


async def list_entries(scope: Optional[str] = None) -> List[MemEntry]:
    """列出记忆（可按频道过滤），按 event_at 降序。"""
    async with _lock:
        doc = _read_sync()
    items = [MemEntry.from_raw(d) for d in doc.get("entries") or []]
    if scope:
        items = [e for e in items if e.scope == scope]
    items.sort(key=lambda e: e.event_at or e.created_at, reverse=True)
    return items


async def add_entries(items: List[Dict[str, Any]], scope: str,
                      source: str = "auto") -> Dict[str, Any]:
    """批量追加记忆（归纳结果），同一批内按 title 去重，并按热度淘汰超限。"""
    if not items:
        return {"added": 0, "skipped": 0, "evicted": 0}
    now = int(time.time())
    async with _lock:
        doc = _read_sync()
        existing = [MemEntry.from_raw(d) for d in doc.get("entries") or []]
        by_scope = [e for e in existing if e.scope == scope]
        existing_titles = {(e.title or "").strip()[:24] for e in by_scope}

        added = 0
        skipped = 0
        new_entries: List[MemEntry] = []
        for it in items:
            title = str(it.get("title") or "").strip()[:40]
            content = str(it.get("content") or "").strip()
            if not title or not content:
                skipped += 1
                continue
            if title[:24] in existing_titles:
                skipped += 1
                continue
            existing_titles.add(title[:24])
            ev = int(it.get("event_at") or 0) or now
            new_entries.append(MemEntry(
                id=_new_id(),
                scope=scope,
                content=content,
                title=title,
                tags=[str(x) for x in (it.get("tags") or [])][:8],
                event_at=ev,
                created_at=now,
                updated_at=now,
                source=source,
                ref_msg_ids=[str(x) for x in (it.get("ref_msg_ids") or [])][:20],
            ))
            added += 1

        all_entries = existing + new_entries
        # 淘汰：按热度升序删，直到 scope 内不超限
        evicted = 0
        scope_entries = [e for e in all_entries if e.scope == scope]
        if len(scope_entries) > MAX_ENTRIES_PER_SCOPE:
            scope_entries.sort(key=lambda e: (heat_factor(e, now), e.event_at))
            drop_ids = {e.id for e in
                        scope_entries[:len(scope_entries) - MAX_ENTRIES_PER_SCOPE]}
            all_entries = [e for e in all_entries if e.id not in drop_ids]
            evicted = len(drop_ids)

        doc["entries"] = [e.to_raw() for e in all_entries]
        doc["version"] = MEMLOG_VERSION
        _write_sync(doc)

    return {"added": added, "skipped": skipped, "evicted": evicted}


async def bump_hits(entries: List[MemEntry]) -> int:
    """注入命中时强化热度（hits+1、last_hit=now）。"""
    if not entries:
        return 0
    ids = {e.id for e in entries}
    now = int(time.time())
    async with _lock:
        doc = _read_sync()
        n = 0
        for d in doc.get("entries") or []:
            if d.get("id") in ids:
                d["hits"] = int(d.get("hits") or 0) + 1
                d["last_hit"] = now
                n += 1
        if n:
            _write_sync(doc)
    return n


async def delete_entry(entry_id: str) -> bool:
    async with _lock:
        doc = _read_sync()
        before = len(doc.get("entries") or [])
        doc["entries"] = [d for d in (doc.get("entries") or [])
                          if d.get("id") != entry_id]
        removed = before - len(doc["entries"])
        if removed:
            _write_sync(doc)
    return removed > 0


async def stats(scope: Optional[str] = None) -> Dict[str, Any]:
    """统计信息（供 WebUI 与定时任务判断）。"""
    entries = await list_entries(scope)
    now = time.time()
    scopes: Dict[str, int] = {}
    for e in entries:
        scopes[e.scope] = scopes.get(e.scope, 0) + 1
    heats = [heat_factor(e, now) for e in entries]
    return {
        "total": len(entries),
        "scopes": scopes,
        "max_per_scope": MAX_ENTRIES_PER_SCOPE,
        "heat_avg": round(sum(heats) / len(heats), 3) if heats else None,
        "heat_min": round(min(heats), 3) if heats else None,
    }
