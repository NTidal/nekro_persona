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
import re
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
    """最近 N 天的摘要，**按天均分字符预算**，保证每天都能露头。

    以前是把 budget 顺序消耗：一天 2600 字的摘要会把 1200 预算一次性吃满，
    后面几天永远进不了注入（实测 NA1 只注入了 1 天）。
    """
    today = date.today()
    picked: List[Dict[str, Any]] = []
    for i in range(1, max(1, days) + 1):
        day = (today - timedelta(days=i)).isoformat()
        d = load_daily(data_dir, day)
        if d:
            picked.append(d)
    if not picked:
        return ""
    picked.reverse()                                  # 从旧到新
    per_day = max(120, budget // max(1, len(picked)))  # 每天至少给 120 字
    out: List[str] = []
    used = 0
    for d in picked:
        summ = str(d.get("summary") or "")
        line = f"{d.get('date')}：{summ}"
        if len(line) > per_day:
            line = line[: max(0, per_day - 1)] + "…"
        out.append(line)
        used += len(line)
        if used >= budget:
            break
    return "## 你记得的这几天\n" + "\n".join(out)


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
    if not is_memory_chat(chat_key):
        return []                       # 网页测试/沙盒会话不生成长期记忆

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
            # 日对账：用当天原文更新已有约定（失败只记日志，不影响摘要）
            try:
                from . import commitments as _cm

                _cd = _cm.load(data_dir)
                _acts = _cm.reconcile(_cd, chat_key, text, ask)
                if _acts:
                    _cm.save(data_dir, _cd)
            except Exception:  # noqa: BLE001
                pass
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


# ────────────────────────── 会话级：已答清单 / 原话检索 ──────────────────────────

SESSION_MAX_TOPICS = 6
SESSION_TOPIC_TTL = 3600          # 1 小时内有效
RAW_LIMIT_DEFAULT = 6
RAW_DAYS_DEFAULT = 30
RAW_DAYS_MAX = 90
RAW_TEXT_MAX = 200

_QUESTION_HINTS = ("什么", "为什么", "怎么", "哪", "吗", "?", "？", "是不是", "能不能", "多少", "谁")


def _session_file(data_dir: Path, chat_key: str) -> Path:
    import hashlib

    h = hashlib.sha1(chat_key.encode("utf-8")).hexdigest()[:16]
    return Path(data_dir) / "backgrounds" / "longmem" / "session" / f"{h}.json"


def _load_session(data_dir: Path, chat_key: str) -> Dict[str, Any]:
    p = _session_file(data_dir, chat_key)
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def _save_session(data_dir: Path, chat_key: str, data: Dict[str, Any]) -> None:
    try:
        p = _session_file(data_dir, chat_key)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(p)
    except Exception:  # noqa: BLE001
        pass


_SKIP_PREFIX = ("[Image", "[image", "[图片", "[表情", "[语音", "[文件", "/var/lib/docker")

# 不参与长期记忆的会话前缀：网页测试会话、临时沙盒会话
_SKIP_CHAT_PREFIX = ("web-session_", "sandbox_", "test_", "sim_")


def is_memory_chat(chat_key: str) -> bool:
    """网页测试/沙盒会话不进长期记忆。"""
    k = str(chat_key or "").strip()
    if not k:
        return False
    return not any(k.startswith(p) for p in _SKIP_CHAT_PREFIX)


def extract_topic(text: str) -> str:
    """只把**提问**抽成话题；陈述、附和、图片占位符一律忽略。

    判据：句子里必须出现问号或疑问词，且长度 >= 4、不含媒体占位符。
    """
    t = " ".join(str(text or "").split())
    if not t or len(t) < 4:
        return ""
    if any(t.startswith(p) for p in _SKIP_PREFIX):
        return ""
    if not any(k in t for k in _QUESTION_HINTS):
        return ""
    for sep in ("。", "！", "？", "!", "?", "\n"):
        if sep in t:
            t = t.split(sep)[0]
            break
    t = t.strip().strip("，,、 ")
    if len(t) < 4:
        return ""
    return t[:24]


def remember_topic(data_dir: Path, chat_key: str, text: str, now: Optional[float] = None) -> None:
    """把本轮用户提过的话题记下来（下一轮起视为“已回应过”）。"""
    topic = extract_topic(text)
    if len(topic) < 2:
        return
    now = now or time.time()
    d = _load_session(data_dir, chat_key)
    topics = [x for x in (d.get("topics") or []) if isinstance(x, dict)]
    topics = [x for x in topics if now - float(x.get("ts") or 0) < SESSION_TOPIC_TTL]
    if not any(x.get("t") == topic for x in topics):
        topics.append({"t": topic, "ts": now})
    d["topics"] = topics[-SESSION_MAX_TOPICS:]
    _save_session(data_dir, chat_key, d)


def render_answered(data_dir: Path, chat_key: str, exclude: str = "",
                    now: Optional[float] = None) -> str:
    """已答话题清单：告诉模型别再重复回答。"""
    now = now or time.time()
    d = _load_session(data_dir, chat_key)
    cur = extract_topic(exclude)
    items = [x.get("t") for x in (d.get("topics") or [])
             if isinstance(x, dict) and now - float(x.get("ts") or 0) < SESSION_TOPIC_TTL]
    items = [t for t in items if t and t != cur]
    if not items:
        return ""
    return ("## 已回应过的话题\n以下话题你已经回应过了，用户再发新内容时**不要重复回答**，"
            "只回应新内容：\n" + "；".join(items[-SESSION_MAX_TOPICS:]))


async def search_messages(DBChatMessage, chat_key: str, keywords: List[str],
                          days: int = RAW_DAYS_DEFAULT, limit: int = RAW_LIMIT_DEFAULT,
                          context: int = 0):
    """在原始消息里按关键词检索；context>0 时带回命中处前后的上下文消息。

    返回 [{"time","who","text","match":bool,"offset":int}]，按时间正序。
    """
    days = max(1, min(int(days or RAW_DAYS_DEFAULT), RAW_DAYS_MAX))
    limit = max(1, min(int(limit or RAW_LIMIT_DEFAULT), 8))
    context = max(0, min(int(context or 0), 10))
    lo = int(time.time()) - days * 86400

    def _clean(t: str) -> str:
        t = re.sub(r"\[Image: [^\]]*\]", "[图片]", t)
        t = re.sub(r"\[File: [^\]]*\]", "[文件]", t)
        t = re.sub(r"\[Voice: [^\]]*\]", "[语音]", t)
        t = re.sub(r"/var/lib/docker/\S+", "[文件]", t)
        return " ".join(t.split())

    def _row(r):
        ts = int(getattr(r, "send_timestamp", 0) or 0)
        return {
            "id": getattr(r, "id", 0),
            "time": datetime.fromtimestamp(ts).strftime("%m-%d %H:%M") if ts else "--",
            "who": str(getattr(r, "sender_nickname", "") or getattr(r, "sender_name", "") or "?")[:12],
            "text": _clean(str(getattr(r, "content_text", "") or ""))[:RAW_TEXT_MAX],
            "ts": ts,
        }

    hits: Dict[int, Dict[str, Any]] = {}
    for kw in [str(k).strip() for k in (keywords or []) if str(k).strip()][:4]:
        try:
            rows = await DBChatMessage.filter(
                chat_key=chat_key, send_timestamp__gte=lo,
                content_text__icontains=kw,
            ).order_by("-send_timestamp").limit(limit).all()
        except Exception:  # noqa: BLE001
            continue
        for r in rows:
            mid = getattr(r, "id", None)
            if mid is None or mid in hits:
                continue
            hits[mid] = _row(r)

    if not hits:
        return []

    picked = sorted(hits.values(), key=lambda x: -x["ts"])[:limit]

    if context <= 0:
        picked.sort(key=lambda x: x["ts"])
        for x in picked:
            x["match"] = True
            x["offset"] = 0
        return picked

    # 带上下文：只对最近 2 个命中展开，避免灌太多
    out: List[Dict[str, Any]] = []
    seen_ids = set()
    for h in sorted(picked, key=lambda x: -x["ts"])[:2]:
        try:
            rows = await DBChatMessage.filter(
                chat_key=chat_key, id__gte=max(1, h["id"] - context),
                id__lte=h["id"] + context,
            ).order_by("id").limit(context * 2 + 1).all()
        except Exception:  # noqa: BLE001
            rows = []
        if not rows:
            h["match"], h["offset"] = True, 0
            if h["id"] not in seen_ids:
                out.append(h)
                seen_ids.add(h["id"])
            continue
        for r in rows:
            row = _row(r)
            if row["id"] in seen_ids:
                continue
            seen_ids.add(row["id"])
            row["match"] = row["id"] == h["id"]
            row["offset"] = int(row["id"]) - int(h["id"])
            out.append(row)
    out.sort(key=lambda x: (x.get("ts") or 0, x.get("id") or 0))
    # 总字符预算 2600：先丢离命中最远的行
    budget = 2600
    while out and sum(len(x["text"]) + 32 for x in out) > budget:
        far = max(out, key=lambda x: abs(int(x.get("offset") or 0)))
        if abs(int(far.get("offset") or 0)) == 0:
            break
        out.remove(far)
    return out[:40]


def render_raw(items: List[Dict[str, Any]]) -> str:
    if not items:
        return "没找到相关原话。"
    lines = ["【以下为未经改写的原始发言；▶ 是命中的那条，其余为它前后的上下文。可直接引用，不要整段念成档案】"]
    for it in items:
        mark = "▶ " if it.get("match") else "  "
        lines.append(f"{mark}[{it.get('time')}] {it.get('who')}: {it.get('text')}")
    return "\n".join(lines)
