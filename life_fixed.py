"""固定式日程：人工填入的周期性区块。

语义（按需求）：
- 某天只要有固定区块，**这一天不再自动生成**日程，也不补齐空白；
- 注入只覆盖"此刻落在某个区块内"的情况，没落在任何区块 → 不注入；
- 多个区块之间**不互相覆盖**：时间重叠就都保留、都进注入。

存储：<插件数据目录>/backgrounds/life_fixed.json
{"version":1,"items":[
  {"id":"fx_xxx","label":"共鸣理论课","weekdays":[0,2,4],
   "start":"09:00","end":"11:30","doing":"在星炬学院上共鸣理论课",
   "place":"教室","mood":5,"enabled":true}
]}
weekdays: 0=周一 … 6=周日
"""

from __future__ import annotations

import hashlib
import json
import time
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


def path(data_dir: Path) -> Path:
    return Path(data_dir) / "backgrounds" / "life_fixed.json"


def load(data_dir: Path) -> Dict[str, Any]:
    try:
        d = json.loads(path(data_dir).read_text(encoding="utf-8"))
        if not isinstance(d, dict):
            raise ValueError("bad root")
    except Exception:  # noqa: BLE001
        d = {}
    d.setdefault("version", 1)
    items = d.get("items")
    d["items"] = items if isinstance(items, list) else []
    return d


def save(data_dir: Path, d: Dict[str, Any]) -> bool:
    try:
        p = path(data_dir)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(p)
        return True
    except Exception:  # noqa: BLE001
        return False


def _hm(text: str) -> Optional[int]:
    """'09:30' → 570 分钟；非法返回 None。"""
    try:
        h, m = str(text).strip().split(":")[:2]
        h, m = int(h), int(m)
        if 0 <= h <= 24 and 0 <= m <= 59:
            return h * 60 + m
    except Exception:  # noqa: BLE001
        pass
    return None


def _fmt(minute: int) -> str:
    minute = max(0, min(24 * 60, int(minute)))
    return f"{minute // 60:02d}:{minute % 60:02d}"


def validate_item(raw: Dict[str, Any]) -> Tuple[Optional[Dict[str, Any]], str]:
    """校验并归一化一条固定区块。返回 (item, 错误)。"""
    if not isinstance(raw, dict):
        return None, "条目必须是对象"
    label = str(raw.get("label") or "").strip()[:40]
    doing = str(raw.get("doing") or "").strip()[:120]
    if not doing:
        return None, "「在做什么」不能为空"
    start, end = _hm(raw.get("start")), _hm(raw.get("end"))
    if start is None or end is None:
        return None, "时间格式必须是 HH:MM"
    if end <= start:
        return None, "结束时间必须晚于开始时间"
    wds: List[int] = []
    for w in (raw.get("weekdays") or []):
        try:
            wi = int(w)
        except (TypeError, ValueError):
            continue
        if 0 <= wi <= 6 and wi not in wds:
            wds.append(wi)
    if not wds:
        return None, "至少选一个星期几"
    mood = raw.get("mood")
    try:
        mood = max(-40, min(40, int(mood))) if mood is not None and str(mood) != "" else None
    except (TypeError, ValueError):
        mood = None
    item = {
        "id": str(raw.get("id") or "").strip() or
              "fx_" + hashlib.sha1(f"{doing}|{start}|{end}|{time.time()}".encode()).hexdigest()[:8],
        "label": label or doing[:16],
        "weekdays": sorted(wds),
        "start": _fmt(start), "end": _fmt(end),
        "start_min": start, "end_min": end,
        "doing": doing,
        "place": str(raw.get("place") or "").strip()[:40],
        "mood": mood,
        "enabled": bool(raw.get("enabled", True)),
    }
    return item, ""


def blocks_for(d: Dict[str, Any], day: Optional[date] = None) -> List[Dict[str, Any]]:
    """某天生效的固定区块（按开始时间排序）。day 默认今天。"""
    day = day or date.today()
    wd = day.weekday()
    out = [x for x in (d.get("items") or [])
           if x.get("enabled", True) and wd in (x.get("weekdays") or [])]
    out.sort(key=lambda x: int(x.get("start_min") or 0))
    return out


def current_blocks(items: List[Dict[str, Any]], now: Optional[datetime] = None) -> List[Dict[str, Any]]:
    """此刻落在哪些区块里（重叠则全部返回）。"""
    now = now or datetime.now()
    minute = now.hour * 60 + now.minute
    return [x for x in items if int(x.get("start_min") or 0) <= minute < int(x.get("end_min") or 0)]


def render_block(items: List[Dict[str, Any]]) -> str:
    """注入文本：只描述此刻所在的固定区块；空列表 → 空串（调用方据此不注入）。"""
    if not items:
        return ""
    lines = ["## 现在这个时段"]
    for x in items:
        tail = f"（{x['start']}-{x['end']}）"
        place = f"，在{x['place']}" if x.get("place") else ""
        lines.append(f"- 此刻：{x.get('doing')}{place}{tail}")
    return "\n".join(lines)


def has_any(d: Dict[str, Any], day: Optional[date] = None) -> bool:
    return bool(blocks_for(d, day))
