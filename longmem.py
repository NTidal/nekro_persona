"""长期记忆第 1 层：按天压缩对话，形成永不遗忘的摘要时间轴。

设计要点
- 原料：`DBChatMessage`（当日该频道的全部消息），不依赖 memory_log 的内部结构。
- 产物：`<插件数据目录>/backgrounds/longmem/daily/<YYYY-MM-DD>.json`
  {"date", "chat_key", "summary", "entities", "msg_count", "ts", "version"}
- 永不删除；上层（周/月）以后可再聚合。
- 注入：取最近 N 天摘要，拼成一段有字符预算的块，附在 persona_prompt 末尾。
- 成本：每频道每天一次 LLM 调用；输入硬上限 30 万字符（超限只取最近部分）。
"""

from __future__ import annotations

import asyncio
import json
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

MAX_INPUT_CHARS = 300_000
MAX_MESSAGES = 8_000
SUMMARY_TARGET = 700
INJECT_BUDGET = 1_200


def _root(data_dir: Path) -> Path:
    return Path(data_dir) / "backgrounds" / "longmem" / "daily"


def daily_path(data_dir: Path, day: str) -> Path:
    return _root(data_dir) / f"{day}.json"


def load_daily(data_dir: Path, day: str) -> Optional[Dict[str, Any]]:
    p = daily_path(data_dir, day)
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
        return d if isinstance(d, dict) and d.get("summary") else None
    except Exception:  # noqa: BLE001
        return None


def save_daily(data_dir: Path, day: str, payload: Dict[str, Any]) -> bool:
    try:
        p = daily_path(data_dir, day)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(p)
        return True
    except Exception:  # noqa: BLE001
        return False


def _day_range(day: str) -> tuple[int, int]:
    """日期字符串 → [当天 0 点, 次日 0 点) 的 unix 秒。"""
    d = datetime.strptime(day, "%Y-%m-%d")
    start = int(d.replace(tzinfo=timezone.utc).timestamp())
    return start, start + 86400


def build_input(rows: List[Any]) -> tuple[str, int]:
    """把消息行压成「[HH:MM] 昵称: 内容」的纯文本，带硬上限。"""
    lines: List[str] = []
    total = 0
    used = 0
    for r in rows[-MAX_MESSAGES:]:
        try:
            ts = int(getattr(r, "send_timestamp", 0) or 0)
            hhmm = datetime.fromtimestamp(ts).strftime("%H:%M") if ts else "--:--"
            who = str(getattr(r, "sender_nickname", "") or getattr(r, "sender_name", "") or "?")[:12]
            txt = " ".join(str(getattr(r, "content_text", "") or "").split())[:400]
        except Exception:  # noqa: BLE001
            continue
        if not txt:
            continue
        line = f"[{hhmm}] {who}: {txt}"
        if total + len(line) > MAX_INPUT_CHARS:
            break
        lines.append(line)
        total += len(line)
        used += 1
    return "\n".join(lines), used


SUMMARY_PROMPT = (
    "下面是一天里群聊/私聊的原始记录。请输出两段，用 JSON 返回，不要解释：\n"
    '{"summary": "600~800 字的当日摘要", "entities": ["出现过的具体人名/地名/物件，最多 12 个"]}\n'
    "要求：\n"
    "1. 只写记录里真实出现过的事实，不要推测、不要补充未出现的细节；\n"
    "2. 按时间顺序叙述，突出：谁说了什么要紧的事、情绪变化、承诺或约定、未解决的悬念；\n"
    "3. 摘要会被长期保存并在以后被检索，所以要写具体（人名、地点、事件名），不要写成「聊了很多」；\n"
    "4. 不要提及「记录」「摘要」这类元信息，直接叙述发生过什么。"
)


CHUNK_CHARS = 40_000


def _split_chunks(text: str, limit: int = CHUNK_CHARS):
    """按行切块，保证每块不超过 limit 字符（单块失败不影响其它块）。"""
    chunks, cur, size = [], [], 0
    for line in text.split("\n"):
        if size + len(line) + 1 > limit and cur:
            chunks.append("\n".join(cur))
            cur, size = [], 0
        cur.append(line)
        size += len(line) + 1
    if cur:
        chunks.append("\n".join(cur))
    return chunks


async def _summarize_chunk(ask, day: str, text: str) -> Dict[str, Any]:
    raw = await ask(SUMMARY_PROMPT, f"以下是 {day} 的聊天记录：\n\n{text}", "longmem_daily")
    if not raw:
        return {}
    body = str(raw).strip()
    if body.startswith("```"):
        body = body.strip("`")
    try:
        d = json.loads(body[body.find("{"): body.rfind("}") + 1])
    except Exception:  # noqa: BLE001
        return {"summary": " ".join(body.split())[:1200], "entities": []}
    summary = " ".join(str(d.get("summary") or "").split())
    if not summary:
        return {}
    ents = [str(x)[:16] for x in (d.get("entities") or []) if str(x).strip()][:12]
    return {"summary": summary, "entities": ents}


async def summarize_day(ask, chat_key: str, day: str, text: str, msg_count: int) -> Dict[str, Any]:
    """分块摘要：单块被内容审查或报错就跳过，其余块合并（部分覆盖好过没有）。"""
    chunks = _split_chunks(text)
    parts, ents, failed = [], [], 0
    for ch in chunks:
        try:
            info = await _summarize_chunk(ask, day, ch)
        except Exception:  # noqa: BLE001  内容审查/超时等单块失败
            failed += 1
            continue
        if info.get("summary"):
            parts.append(info["summary"])
            ents.extend(info.get("entities") or [])
    if not parts:
        return {"error": "all_chunks_failed", "chunks": len(chunks)}
    seen, uniq = set(), []
    for e in ents:
        if e not in seen:
            seen.add(e)
            uniq.append(e)
    return {"summary": " ".join(parts)[:2600], "entities": uniq[:12],
            "chunks": len(chunks), "failed_chunks": failed}


def render_block(data_dir: Path, days: int = 3, budget: int = INJECT_BUDGET) -> str:
    """最近 N 天的摘要，按时间正序拼接，受字符预算约束。"""
    today = date.today()
    picked: List[Dict[str, Any]] = []
    for i in range(1, max(1, days) + 1):
        day = (today - timedelta(days=i)).isoformat()
        d = load_daily(data_dir, day)
        if d:
            picked.append(d)
    if not picked:
        return ""
    picked.reverse()
    out: List[str] = []
    used = 0
    for d in picked:
        line = f"{d.get('date')}：{d.get('summary')}"
        if used + len(line) > budget:
            line = line[: max(0, budget - used)] + "…"
        out.append(line)
        used += len(line)
        if used >= budget:
            break
    return "## 你记得的这几天\n" + "\n".join(out)


def _date_span(start: str, end: str):
    """[start, end] 闭区间 → 日期列表（含首末）。"""
    d0 = datetime.strptime(start, "%Y-%m-%d").date()
    d1 = datetime.strptime(end, "%Y-%m-%d").date()
    out = []
    while d0 <= d1:
        out.append(d0.isoformat())
        d0 += timedelta(days=1)
    return out


async def ensure_days(data_dir: Path, chat_key: str, fetch_rows, ask,
                      days_back: int = 3, start: Optional[str] = None,
                      end: Optional[str] = None, overwrite: bool = False,
                      min_msgs: int = 4) -> List[str]:
    """补齐日摘要。

    - 默认行为：补最近 days_back 天（不含今天）。
    - 指定 start/end：只补 [start, end] 这一段（闭区间），用于回填历史。
    - overwrite=True 时重算已存在的日期。
    返回本次成功写入的日期列表。
    """
    if start and end:
        targets = _date_span(start, end)
    else:
        today = date.today()
        targets = [(today - timedelta(days=i)).isoformat()
                   for i in range(1, max(1, days_back) + 1)]
        targets.reverse()                       # 从旧到新

    made: List[str] = []
    for day in targets:
        if not overwrite and load_daily(data_dir, day):
            continue
        lo, hi = _day_range(day)
        try:
            rows = await fetch_rows(chat_key, lo, hi)
        except Exception:  # noqa: BLE001
            continue
        if not rows:
            continue
        text, used = build_input(rows)
        if used < min_msgs:
            continue
        info = await summarize_day(ask, chat_key, day, text, used)
        if not info or not info.get("summary"):
            continue
        ok = save_daily(data_dir, day, {
            "version": 1, "date": day, "chat_key": chat_key,
            "summary": info["summary"], "entities": info.get("entities") or [],
            "msg_count": used, "ts": int(time.time()),
            "chunks": info.get("chunks"), "failed_chunks": info.get("failed_chunks"),
        })
        if ok:
            made.append(day)
    return made


def build_fetcher(DBChatMessage, max_rows: int = MAX_MESSAGES):
    """返回 async fetch_rows(chat_key, start_ts, end_ts) -> list[rows]。"""
    async def _fetch(chat_key: str, start_ts: int, end_ts: int):
        return await (DBChatMessage.filter(
            chat_key=chat_key,
            send_timestamp__gte=start_ts - 1,
            send_timestamp__lt=end_ts,
        ).order_by("id").limit(max_rows).all())
    return _fetch
