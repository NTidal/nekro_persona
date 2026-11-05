"""承诺（约定）生命周期管理。

为什么单独一套：约定是**有状态**的实体（待办/已完成/已过期/已改约），
塞进静态记忆会永远停在"答应了"的那一刻，完成或改期都不会更新。

存储：<插件数据目录>/backgrounds/commitments.json
{
  "version": 1,
  "items": [{
    "id": "cmt_xxx", "scope": "<chat_key>", "text": "周四一起吃加辣的炸鸡",
    "made_at": 1793600000, "due": "2026-10-30" | null,
    "status": "pending|done|cancelled|expired|superseded",
    "done_at": null, "result": "", "reason": "",
    "supersedes": null, "superseded_by": null, "rev": 1,
    "last_mentioned_at": 1793600000, "revive_count": 0,
    "source_entry": "mem_xxx",
    "history": [{"at": ts, "action": "created|updated|done|cancelled|expired|revived|superseded", "note": ""}]
  }],
  "seen_entries": ["mem_id", ...]      # 已扫描过的 memory_log 条目，避免重复提取
}
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

GRACE_DAYS = 3            # 到期后宽限几天才标 expired
STALE_DAYS = 14           # 无 due 的约定，超过这么久在注入里提示
MAX_PENDING_INJECT = 3
INJECT_BUDGET = 420
EXPIRE_RESERVE = 12       # 注释"另有 N 条过期"的行，出现在 budget 之外

_PROMISE_WORDS = ("约定", "答应", "约好", "说好", "改天", "下次", "回头", "带你去", "请你吃")
_CANCEL_WORDS = ("算了", "不去了", "取消", "作罢", "改别")
_DUE_PATTERNS = (
    (re.compile(r"(下下|下|这|本)?周([一二三四五六日天])"), "week"),
    (re.compile(r"(今天|今晚)"), "today"),
    (re.compile(r"明天|明日"), "tomorrow"),
    (re.compile(r"后天"), "day_after"),
    (re.compile(r"(\d{1,2})月(\d{1,2})[日号]"), "md"),
    (re.compile(r"(\d{4})-(\d{2})-(\d{2})"), "ymd"),
)
_WEEKDAY = {"一": 0, "二": 1, "三": 2, "四": 3, "五": 4, "六": 5, "日": 6, "天": 6}


def _path(data_dir: Path) -> Path:
    return Path(data_dir) / "backgrounds" / "commitments.json"


def load(data_dir: Path) -> Dict[str, Any]:
    try:
        d = json.loads(_path(data_dir).read_text(encoding="utf-8"))
        if not isinstance(d, dict):
            raise ValueError("bad root")
    except Exception:  # noqa: BLE001
        d = {}
    d.setdefault("version", 1)
    d.setdefault("items", [])
    d.setdefault("seen_entries", [])
    return d


def save(data_dir: Path, d: Dict[str, Any]) -> bool:
    try:
        p = _path(data_dir)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(p)
        return True
    except Exception:  # noqa: BLE001
        return False


def _new_id(scope: str, text: str) -> str:
    h = hashlib.sha1(f"{scope}|{text}|{time.time()}".encode("utf-8")).hexdigest()[:10]
    return f"cmt_{h}"


def _today() -> date:
    return date.today()


def parse_due(text: str, base: Optional[date] = None) -> Optional[str]:
    """从文本里猜一个到期日（猜不到返回 None）。"""
    base = base or _today()
    t = str(text or "")
    for pat, kind in _DUE_PATTERNS:
        m = pat.search(t)
        if not m:
            continue
        try:
            if kind == "today":
                return base.isoformat()
            if kind == "tomorrow":
                return (base + timedelta(days=1)).isoformat()
            if kind == "day_after":
                return (base + timedelta(days=2)).isoformat()
            if kind == "md":
                mo, dd = int(m.group(1)), int(m.group(2))
                y = base.year + (1 if (mo, dd) < (base.month, base.day) else 0)
                return date(y, mo, dd).isoformat()
            if kind == "ymd":
                return date(int(m.group(1)), int(m.group(2)), int(m.group(3))).isoformat()
            if kind == "week":
                prefix, wd = m.group(1) or "", _WEEKDAY.get(m.group(2), 0)
                delta = (wd - base.weekday()) % 7
                if prefix in ("下", "下下"):
                    delta += 7 if prefix == "下" else 14
                elif prefix in ("这", "本") and delta == 0:
                    delta = 7
                return (base + timedelta(days=delta)).isoformat()
        except Exception:  # noqa: BLE001
            continue
    return None


def looks_like_promise(text: str) -> bool:
    t = str(text or "")
    return any(w in t for w in _PROMISE_WORDS)


def _keywords(text: str) -> set:
    """粗糙关键词：去掉虚词后取 2 字以上的连续块。"""
    t = re.sub(r"[，。！？、；：「」“”()（）\s]", " ", str(text or ""))
    out = set()
    for chunk in t.split(" "):
        chunk = chunk.strip()
        if len(chunk) < 2:
            continue
        for i in range(0, max(1, len(chunk) - 1)):
            out.add(chunk[i:i + 2])
    return out


def _similar(a: str, b: str) -> bool:
    ka, kb = _keywords(a), _keywords(b)
    if not ka or not kb:
        return False
    return len(ka & kb) >= 2


def _log(item: Dict[str, Any], action: str, note: str = "", now: Optional[float] = None) -> None:
    item.setdefault("history", []).append(
        {"at": int(now or time.time()), "action": action, "note": note[:80]})


def upsert_promise(d: Dict[str, Any], scope: str, text: str,
                   due: Optional[str] = None, source_entry: str = "",
                   now: Optional[float] = None) -> Tuple[Dict[str, Any], str]:
    """新增或更新承诺。返回 (item, action) —— action ∈ created|updated|revived。"""
    now = now or time.time()
    items = d["items"]
    pend = [x for x in items if x.get("scope") == scope
            and x.get("status") in ("pending", "expired")]

    # ① 同类 pending/expired → 视为同一件事：更新文本/时间（改约或被再次提起）
    for x in pend:
        if _similar(x.get("text") or "", text):
            was_expired = x.get("status") == "expired"
            x["text"] = text[:120]
            x["due"] = due or x.get("due")
            x["last_mentioned_at"] = int(now)
            x["rev"] = int(x.get("rev") or 1) + 1
            if was_expired:
                x["status"] = "pending"
                x["revive_count"] = int(x.get("revive_count") or 0) + 1
                _log(x, "revived", "过期后又被提起")
                return x, "revived"
            _log(x, "updated", "再次提到/改期")
            return x, "updated"

    # ② 新承诺；如果是被取代的旧条，记链
    item = {
        "id": _new_id(scope, text), "scope": scope, "text": text[:120],
        "made_at": int(now), "due": due, "status": "pending",
        "done_at": None, "result": "", "reason": "",
        "supersedes": None, "superseded_by": None, "rev": 1,
        "last_mentioned_at": int(now), "revive_count": 0,
        "source_entry": source_entry, "history": [],
    }
    _log(item, "created", f"due={due}")
    items.append(item)
    return item, "created"


def set_status(d: Dict[str, Any], item_id: str, status: str,
               note: str = "", now: Optional[float] = None) -> Optional[Dict[str, Any]]:
    for x in d["items"]:
        if x.get("id") == item_id:
            x["status"] = status
            if status == "done":
                x["done_at"] = int(now or time.time())
                x["result"] = note[:120]
            elif status == "cancelled":
                x["reason"] = note[:120]
            elif status == "expired":
                x["reason"] = note[:120] or "到期未了结"
            _log(x, status, note, now)
            return x
    return None


def reschedule(d: Dict[str, Any], item_id: str, new_text: str = "",
               new_due: Optional[str] = None, now: Optional[float] = None) -> Optional[Dict[str, Any]]:
    """改约：旧条目 → superseded，新建一条 pending 指向它。"""
    now = now or time.time()
    old = next((x for x in d["items"] if x.get("id") == item_id), None)
    if old is None:
        return None
    text = (new_text or old.get("text") or "").strip()
    item, _ = upsert_promise(d, old.get("scope") or "", text,
                             due=new_due or old.get("due"), source_entry=old.get("source_entry") or "",
                             now=now)
    if item.get("id") == old.get("id"):
        # 相似度命中：同一件事被更新，直接改 due 即可
        old["due"] = new_due or old.get("due")
        return old
    old["status"] = "superseded"
    old["superseded_by"] = item["id"]
    item["supersedes"] = old["id"]
    _log(old, "superseded", f"→ {item['id']}", now)
    return item


def expire_scan(d: Dict[str, Any], now: Optional[float] = None) -> int:
    """把到期超过宽限期、或很久没动静的 pending 标为 expired。返回改动条数。"""
    now_ts = now or time.time()
    today = datetime.fromtimestamp(now_ts).date()
    n = 0
    for x in d["items"]:
        if x.get("status") != "pending":
            continue
        due = x.get("due")
        if due:
            try:
                d_due = datetime.strptime(due, "%Y-%m-%d").date()
            except Exception:  # noqa: BLE001
                continue
            if today > d_due + timedelta(days=GRACE_DAYS):
                x["status"] = "expired"
                x["reason"] = f"到期 {due} 未了结"
                _log(x, "expired", x["reason"], now_ts)
                n += 1
    return n


def pending_items(d: Dict[str, Any], scope: str) -> List[Dict[str, Any]]:
    return [x for x in d["items"] if x.get("scope") == scope and x.get("status") == "pending"]


def expired_items(d: Dict[str, Any], scope: str) -> List[Dict[str, Any]]:
    return [x for x in d["items"] if x.get("scope") == scope and x.get("status") == "expired"]


def _age_days(item: Dict[str, Any], now_ts: float) -> int:
    return max(0, int((now_ts - float(item.get("made_at") or now_ts)) // 86400))


def render_pending(d: Dict[str, Any], scope: str,
                   budget: int = INJECT_BUDGET, now: Optional[float] = None) -> str:
    """注入块：只放 pending（含迟到未扫的过期项），最多 3 条。"""
    now_ts = now or time.time()
    today = datetime.fromtimestamp(now_ts).date()
    items = pending_items(d, scope)
    if not items:
        return ""
    items = sorted(items, key=lambda x: -float(x.get("last_mentioned_at") or 0))[:MAX_PENDING_INJECT]
    lines: List[str] = []
    used = 0
    for it in items:
        age = _age_days(it, now_ts)
        tail = ""
        due = it.get("due")
        if due:
            try:
                d_due = datetime.strptime(due, "%Y-%m-%d").date()
                if today > d_due + timedelta(days=GRACE_DAYS):
                    tail = f"，约在 {due}（已过期，需要时可复活或了结）"
                else:
                    tail = f"，约在 {due}"
            except Exception:  # noqa: BLE001
                tail = ""
        elif age >= STALE_DAYS:
            tail = "，已经很久了"
        line = f"- [{age} 天前] {it.get('text')}{tail}"
        if used + len(line) > budget:
            break
        lines.append(line)
        used += len(line)
    if not lines:
        return ""
    head = ("## 还没了结的约定\n"
            "这些是**待办**，不是已经发生的事。已经做了/取消了/改期了，"
            "用「约定管理」工具更新状态，别一直当成待约提起。")
    return head + "\n" + "\n".join(lines)


def render_expired_hint(d: Dict[str, Any], scope: str) -> str:
    n = len(expired_items(d, scope))
    if not n:
        return ""
    return f"（另有 {n} 条约定已过期未了结，需要时用「约定管理」查看或复活）"


def harvest_from_memory_log(data_dir: Path, log_path: Path, scope: str,
                            max_scan: int = 200) -> List[str]:
    """从 memory_log.json 里提取新承诺。返回本次新建/更新的 id 列表。"""
    try:
        raw = json.loads(Path(log_path).read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return []
    entries = raw.get("entries") if isinstance(raw, dict) else raw
    if not isinstance(entries, list):
        return []
    d = load(data_dir)
    seen = set(d.get("seen_entries") or [])
    touched: List[str] = []
    for e in entries[-max_scan:]:
        if not isinstance(e, dict):
            continue
        eid = str(e.get("id") or "")
        if not eid or eid in seen:
            continue
        sc = str(e.get("scope") or "")
        if scope and sc and sc != scope:
            continue
        blob = f"{e.get('title') or ''} {e.get('content') or ''}"
        tags = e.get("tags") or []
        if not (looks_like_promise(blob) or "约定" in [str(t) for t in tags]):
            seen.add(eid)
            continue
        text = (e.get("title") or e.get("content") or "")[:120]
        if not text:
            seen.add(eid)
            continue
        if any(w in blob for w in _CANCEL_WORDS) and not any(w in blob for w in ("答应", "约定")):
            seen.add(eid)
            continue
        item, _act = upsert_promise(d, sc or scope, text,
                                    due=parse_due(blob), source_entry=eid)
        seen.add(eid)
        touched.append(item["id"])
    d["seen_entries"] = sorted(seen)[-2000:]
    if touched:
        save(data_dir, d)
    return touched


def match_pending(d: Dict[str, Any], scope: str, text: str,
                  limit: int = 2, min_overlap: int = 1) -> List[Dict[str, Any]]:
    """按本轮发言关键词找出相关约定（检索式注入用；比去重宽松，1 个关键词即可）。"""
    ka = _keywords(text)
    if not ka:
        return []
    scored = []
    for x in d["items"]:
        if x.get("scope") != scope or x.get("status") != "pending":
            continue
        kb = _keywords(x.get("text") or "")
        ov = len(ka & kb)
        if ov >= min_overlap:
            scored.append((ov, float(x.get("last_mentioned_at") or 0), x))
    scored.sort(key=lambda t: (-t[0], -t[1]))
    out = [t[2] for t in scored[:limit]]
    for x in out:
        x["last_mentioned_at"] = int(time.time())
    return out


def render_matched(items: List[Dict[str, Any]], now: Optional[float] = None) -> str:
    """只注入“这轮提到了”的约定，并给出更新建议。"""
    if not items:
        return ""
    now_ts = now or time.time()
    lines = []
    for it in items:
        age = _age_days(it, now_ts)
        due = f"，约在 {it['due']}" if it.get("due") else ""
        lines.append(f"- [{age} 天前] {it.get('text')}{due}")
    return ("## 相关的未了结约定\n"
            "下面这条和你刚才提到的事有关，它**还没了结**：\n" + "\n".join(lines) +
            "\n（如果已经发生、取消或改期，调用「约定管理」更新状态，别当成还没做）")


RECONCILE_PROMPT = (
    "下面是一天的聊天记录，以及当前还没了结的约定列表。请判断：这些约定里，"
    "哪些在今天已经被履行、被取消、或被改期？只输出 JSON，不要解释：\n"
    '{"actions":[{"id":"cmt_xxx","action":"done|cancel|reschedule","due":"YYYY-MM-DD",'
    '"evidence":"记录里的原句片段"}]}\n'
    "要求：\n"
    "1. 没有证据就不要输出该条；evidence 必须是从下面记录里**逐字复制**的片段；\n"
    "2. 只有明确发生（做了/说取消/说改期）才输出，不确定就不输出；\n"
    "3. 最多 5 条；reschedule 必须给新 due；done/cancel 不用给 due。"
)


def _norm(t: str) -> str:
    import re

    return re.sub(r"[\s，。！？、；：「」“”()（）,.!?;:\"'`]", "", str(t or ""))


def reconcile(d: Dict[str, Any], scope: str, day_text: str, ask,
              max_actions: int = 5, now: Optional[float] = None) -> List[str]:
    """日对账：根据当天原文更新约定状态。ask(prompt, system, task) -> str。"""
    pend = pending_items(d, scope)
    if not pend or not day_text:
        return []
    listing = "\n".join(
        f"- id={x['id']} | {x.get('text')}" + (f" | 约在 {x['due']}" if x.get("due") else "")
        for x in pend[:20])
    try:
        raw = ask(RECONCILE_PROMPT,
                  f"【当前未了结约定】\n{listing}\n\n【当天记录】\n{day_text[:20000]}",
                  "commitment_reconcile")
    except Exception:  # noqa: BLE001
        return []
    if not raw:
        return []
    body = str(raw)
    import json as _json

    try:
        data = _json.loads(body[body.find("{"): body.rfind("}") + 1])
    except Exception:  # noqa: BLE001
        return []
    flat_day = _norm(day_text)
    applied: List[str] = []
    for act in (data.get("actions") or [])[:max_actions]:
        if not isinstance(act, dict):
            continue
        item_id = str(act.get("id") or "").strip()
        action = str(act.get("action") or "").strip().lower()
        evidence = _norm(act.get("evidence") or "")
        if not item_id or action not in ("done", "cancel", "reschedule"):
            continue
        if not evidence or evidence not in flat_day:
            continue                     # 护栏：证据必须能在原文里找到
        target = next((x for x in d["items"] if x.get("id") == item_id), None)
        if target is None or target.get("status") not in ("pending", "expired"):
            continue
        if action == "reschedule":
            new_due = str(act.get("due") or "").strip() or None
            item = reschedule(d, item_id, new_due=new_due, now=now)
            if item:
                applied.append(f"reschedule:{item_id}->{new_due}")
        else:
            st = "done" if action == "done" else "cancelled"
            item = set_status(d, item_id, st, f"日对账：{act.get('evidence') or ''}"[:100], now)
            if item:
                applied.append(f"{st}:{item_id}")
    return applied
