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
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from nekro_agent.core.logger import get_sub_logger

logger = get_sub_logger("persona_memory_log")

# ==================== 常量 ====================

MEMLOG_FILE = "memory_log.json"
MEMLOG_VERSION = 1


def _safe_int(value: Any, default: int = 0) -> int:
    """任意值 → int，坏值回落 default（绝不抛）。"""
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    try:
        return int(float(str(value).strip()))
    except (TypeError, ValueError):
        return default

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
        """宽松解析：坏字段回落默认值。

        ⚠️ 旧实现六处裸 `int()`：一个事件条目的 `hits` 被写坏（"2"/null/对象）
        就让 `/api/memory_log` 直接 500，而且归纳循环每 2 小时重试一次、永远失败。
        """
        d = d or {}
        return cls(
            id=str(d.get("id") or ""),
            scope=str(d.get("scope") or ""),
            content=str(d.get("content") or ""),
            title=str(d.get("title") or ""),
            tags=[str(x) for x in (d.get("tags") or [])],
            event_at=max(0, _safe_int(d.get("event_at"), 0)),
            created_at=max(0, _safe_int(d.get("created_at"), 0)),
            updated_at=max(0, _safe_int(d.get("updated_at"), 0)),
            hits=max(0, _safe_int(d.get("hits"), 0)),
            last_hit=max(0, _safe_int(d.get("last_hit"), 0)),
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


# ======================================================================
# 损坏保护（v2.1.1）：解析失败时先备份原件，并在人工修复前**拒绝写入**。
# 早先的行为是「按空文档继续」——渲染没问题，但写路径读到同一份空文档，
# 第一次 write_anchor / save_note / 归纳落库就会把「只含新条目」的文档写回，
# 静默抹掉此前全部内容且无备份。这里改成 fail-safe + fail-closed。
# ======================================================================
_DEGRADED: Dict[str, str] = {}


class MemoryLogCorrupted(RuntimeError):
    """memory_log.json 损坏或版本不符：拒绝写入，等待人工修复。"""


def _backup_corrupt(fp: Path) -> str:
    """把损坏文件原样复制一份到同目录，返回备份路径（失败返回空串）。"""
    try:
        stamp = time.strftime("%Y%m%d_%H%M%S")
        dst = fp.parent / f"{fp.stem}.corrupt_{stamp}{fp.suffix}"
        dst.write_bytes(fp.read_bytes())
        return str(dst)
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[memory_log] 备份损坏文件失败: {exc}")
        return ""


def _mark_degraded(fp: Path, reason: str) -> None:
    """记录降级原因；首次降级时备份原文件（只备份一次，避免反复读反复写）。"""
    key = str(fp)
    if key in _DEGRADED:
        return
    bak = _backup_corrupt(fp)
    _DEGRADED[key] = reason
    logger.error(
        f"[memory_log] {fp.name} 已降级为只读：{reason}。"
        f"原文件已备份到 {bak or '(备份失败)'}；修复或替换该文件之前，"
        "所有写入都会被拒绝，以免覆盖掉既有数据。"
    )


def corruption_reason() -> str:
    """当前文件是否处于降级状态；返回原因（空串 = 正常）。"""
    return _DEGRADED.get(str(_log_file()), "")


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
        # 版本不符一律按损坏处理（降级、拒绝写入）：新版本可能换了字段语义，
        # 用旧代码读+写回就是静默改坏数据。缺失（老文件）则补上当前版本。
        _ver = data.get("version")
        if _ver is None:
            data["version"] = MEMLOG_VERSION
        elif _safe_int(_ver, -1) != MEMLOG_VERSION:
            raise ValueError(f"版本不符（文件 {_ver!r}，当前 {MEMLOG_VERSION}）")
        # ⚠️ entries 存在但类型不对属于**文件损坏**，必须走降级：
        #    早先这里静默 `= []`，既不标记降级、下一次写入又会把原文件覆盖掉。
        if "entries" not in data:
            data["entries"] = []
        elif not isinstance(data["entries"], list):
            raise ValueError("entries 不是数组")
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[memory_log] 解析失败，按空处理：{exc}")
        _mark_degraded(fp, f"JSON 解析失败：{exc}")
        data = {"version": MEMLOG_VERSION, "entries": []}
    else:
        # 解析成功 → 对称解除降级（否则管理员修好文件后写入仍被永久跳过，直到重启）
        _DEGRADED.pop(str(fp), None)

    _read_cache[fp.parent] = {"mtime": st.st_mtime, "size": st.st_size,
                              "doc": data}
    return data


def _write_sync(doc: Dict[str, Any]) -> None:
    """原子写盘；成功后刷新缓存。失败只记日志。

    ⚠️ 降级期间**直接跳过写盘**：此处的 doc 是「按空处理」得到的空文档，
    写回去等于把既有事件记忆整块清空（delete_entry / bump_hits 也会走到这里）。
    """
    _reason = corruption_reason()
    if _reason:
        logger.warning(f"[memory_log] 文件处于损坏保护状态（{_reason}），本次写盘已跳过")
        return
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
    """毫秒时间 + 6 位随机：旧实现用 `time_ns() % 100000`，同一毫秒内多次调用
    会撞（后台批量写入时确实会），而删除/强化都按 id 定位 —— 撞了就误删别人的条目。
    """
    return f"mem_{int(time.time() * 1000):x}_{uuid.uuid4().hex[:8]}"


# ==================== 对外 API ====================


async def list_entries(scope: Optional[str] = None) -> List[MemEntry]:
    """列出记忆（可按频道过滤），按 event_at 降序。"""
    async with _lock:
        doc = _read_sync()
    items = [MemEntry.from_raw(d) for d in (doc.get("entries") or [])
             if isinstance(d, dict) and str(d.get("id") or "").strip()]
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
        # ⚠️ 守卫必须在**读之后**：_read_sync 才是检测/解除降级的唯一入口。
        #    放在读之前会有两个后果：首次损坏时不报 corrupt（写盘被静默跳过，
        #    调用方却以为写成功了）；文件修好后守卫先短路，降级永不解除。
        doc = _read_sync()
        _reason = corruption_reason()
        if _reason:
            # 文件损坏时不落盘：此时的 doc 是空文档，写回等于清空全部事件记忆
            return {"added": 0, "skipped": 0, "evicted": 0, "corrupt": _reason}
        existing = [MemEntry.from_raw(d) for d in (doc.get("entries") or [])
                    if isinstance(d, dict) and str(d.get("id") or "").strip()]
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
            # event_at 钳制：LLM 可能给出未来时间（"2027-01-01"）或离谱的远古值。
            # 未来时间会让这条记忆**永久**排在最前面（排序按 event_at 降序），
            # 一直挤占注入名额；远古值则永远排在末尾。钳到 [now-10年, now]。
            _raw_ev = _safe_int(it.get("event_at"), 0) or now
            ev = min(now, max(now - 10 * 365 * 86400, _raw_ev))
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
