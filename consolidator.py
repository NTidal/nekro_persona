"""关系记忆的归纳引擎 —— 从 exec_code 素材里提取「事件」，攒批后交给 LLM 归纳。

设计要点：
  · **与好感度解耦**：素材来自 adjust_favorability 的 reason，但**不看 delta、
    不看调用是否被规则拒绝** —— 严格模式下被拒绝的调用同样贡献素材，
    因为「事件发生了」与「分数涨没涨」是正交的两件事。
  · **多源**：除 reason 外，也吃 send_voice_clone 的语音文本（更慎重的表达）。
  · **攒批**：达到 N 条或超过 T 小时才归纳一次，避免每轮都调 LLM。
  · **不主动写库**：归纳结果返回给调用方（plugin.py），由它决定写入 memory_log。
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from nekro_agent.core.logger import get_sub_logger

logger = get_sub_logger("persona_consolidator")

# ==================== 参数 ====================

class ConsolidationFailed(RuntimeError):
    """归纳最终失败（网络 / 超时 / 坏 JSON / 模型组不可用）。

    ⚠️ 必须用异常而不是「返回空列表」来表达失败：调用方把空列表理解为
    「模型判断这批没什么可记的」，于是不回填素材 —— 一批（最多几十条）
    互动素材就此永久丢失，日志还撒谎说「素材保留待下轮」。
    """


BATCH_MIN_ITEMS = 20
"""攒够多少条素材才归纳（避免一轮一调）。"""

BATCH_MAX_AGE_HOURS = 2.0
"""素材最久攒多久必须归纳（防止冷频道永远不触发）。"""

MAX_ITEMS_PER_CALL = 60
"""单次归纳最多喂多少条素材（控制 token）。"""

MAX_CHARS_PER_ITEM = 300
"""单条素材最大字符数（reason 一般 30~50 字，超长的截断）。"""

LLM_MAX_TOKENS = 800
"""输出上限。关掉思考后 400 就够（实测 334 字符），留 800 给长归纳余量。"""

LLM_TEMPERATURE = 0.3
LLM_MAX_RETRIES = 1
"""重试次数。关掉思考后失败率 0%（实测 6/6），留 1 次兜住偶发。"""

LLM_TIMEOUT_SECONDS = 30
"""单次调用硬超时。

NA 的 gen_openai_chat_response 在未传 max_wait_time 时默认 3600 秒
（openai.py:524），一个卡住的请求能挂一小时 —— 定时任务绝不能这样。
"""

DISABLE_THINKING = {"thinking": {"type": "disabled"}}
"""关闭思考模式。

实测（聊天组 deepseek-v4.1-flash，同一 prompt）：
  · 不关：3.22s / 4.72s，且思考 token 挤占 max_tokens，偶发
          finish_reason=length + 正文为空 → 直接抛异常
  · 关掉：0.86s / 0.91s / 0.95s，JSON 合法率 6/6
等价写法 `reasoning_effort: "none"`（1.02s）也可用，但语义不如显式 disabled 清晰。
"""

# ==================== 素材提取 ====================

# adjust_favorability 的 reason 参数（单双引号都要支持）
_RE_REASON = re.compile(
    r"""reason\s*=\s*(?:f?)(["'])(?P<v>.{4,}?)\1""", re.S)
# send_voice_clone 的第一个字符串参数
_RE_VOICE = re.compile(
    r"""send_voice_clone\s*\(\s*_?ck\s*,\s*(?:f?)(["'])(?P<v>.{4,}?)\1""", re.S)

# 无信息量的 reason（纯氛围/无具体行为）—— 与 favor_scale 的 vague 判定同思路
_NOISE_PATTERNS = [
    re.compile(r"^由于神秘原因"),
    re.compile(r"^(测试|test)$", re.I),
]


def _clip(s: str, n: int) -> str:
    s = (s or "").strip()
    return s if len(s) <= n else s[: n - 1] + "…"


def _norm(s: str) -> str:
    """归一化用于去重：去空白、去标点。"""
    return re.sub(r"[\s，。、！？…,.!?~～\-—\"'（）()【】\[\]]+", "", s or "")


def extract_materials(code_text: str, created_at: int = 0,
                      msg_id: str = "") -> List[Dict[str, Any]]:
    """从一段 exec_code.code_text 里提取可用于归纳的素材。

    返回 [{"kind": "favor"|"voice", "text": str, "at": int, "msg_id": str}]
    """
    out: List[Dict[str, Any]] = []
    if not code_text:
        return out

    for m in _RE_REASON.finditer(code_text):
        text = _clip(m.group("v"), MAX_CHARS_PER_ITEM)
        if not text or len(text) < 6:
            continue
        if any(p.search(text) for p in _NOISE_PATTERNS):
            continue
        out.append({"kind": "favor", "text": text,
                    "at": int(created_at or 0), "msg_id": str(msg_id or "")})

    for m in _RE_VOICE.finditer(code_text):
        text = _clip(m.group("v"), MAX_CHARS_PER_ITEM)
        if not text or len(text) < 8:
            continue
        out.append({"kind": "voice", "text": text,
                    "at": int(created_at or 0), "msg_id": str(msg_id or "")})

    return out


def dedup_materials(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """按归一化文本去重，保留最早出现的。"""
    seen = set()
    out = []
    for it in items:
        key = _norm(it.get("text") or "")[:60]
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(it)
    return out


# ==================== 攒批状态 ====================

@dataclass
class PendingBucket:
    """一个频道的待归纳素材桶。"""

    chat_key: str
    items: List[Dict[str, Any]]
    first_at: float = 0.0     # 最早素材的入桶时间（用于超时判断）
    last_at: float = 0.0

    def should_flush(self, now: Optional[float] = None) -> bool:
        if not self.items:
            return False
        now = now if now is not None else time.time()
        if len(self.items) >= BATCH_MIN_ITEMS:
            return True
        return (self.first_at > 0
                and now - self.first_at >= BATCH_MAX_AGE_HOURS * 3600)


_buckets: Dict[str, PendingBucket] = {}


def push_materials(chat_key: str, items: List[Dict[str, Any]],
                   now: Optional[float] = None) -> int:
    """把素材推进攒批桶，返回该桶当前累计条数。"""
    if not chat_key or not items:
        return len(_buckets.get(chat_key, PendingBucket(chat_key, [])).items)
    now = now if now is not None else time.time()
    b = _buckets.get(chat_key)
    if b is None:
        b = PendingBucket(chat_key=chat_key, items=[], first_at=now, last_at=now)
        _buckets[chat_key] = b
    b.items.extend(items)
    b.last_at = now
    if b.first_at <= 0:
        b.first_at = now
    return len(b.items)


def take_batch(chat_key: str, limit: int = MAX_ITEMS_PER_CALL) -> List[Dict[str, Any]]:
    """取出一批待归纳素材（最多 limit 条），剩余留在桶里。"""
    b = _buckets.get(chat_key)
    if not b or not b.items:
        return []
    batch = b.items[:limit]
    b.items = b.items[limit:]
    if not b.items:
        b.first_at = b.last_at = 0.0
    return batch


def pushes_back(chat_key: str, items: List[Dict[str, Any]],
                now: Optional[float] = None) -> int:
    """把一批素材放回桶**头部**（归纳失败时用），返回桶内累计条数。

    与 `push_materials` 的区别：
      · 插到头部而不是尾部 —— 保证重试时素材顺序不乱
      · 重置 `first_at` 为当前时间 —— 失败后不要立即重试（否则会在
        「失败 → 立即重试 → 再失败」之间空转烧 token），按正常的超时
        节奏再等一轮
    """
    if not chat_key or not items:
        return 0
    now = now if now is not None else time.time()
    b = _buckets.get(chat_key)
    if b is None:
        b = PendingBucket(chat_key=chat_key, items=[], first_at=now, last_at=now)
        _buckets[chat_key] = b
    b.items = list(items) + b.items
    b.last_at = now
    # ⚠️ 必须**无条件**重置：docstring 承诺「失败后不要立即重试」，
    #    但旧实现只在 first_at<=0 时才设，已有桶的 first_at 保持很久以前，
    #    于是 should_flush 立刻又判定超时 —— ≥门槛的失败桶每 60 秒重试一次。
    b.first_at = now
    return len(b.items)


def buckets_ready(now: Optional[float] = None) -> List[str]:
    """返回所有该归纳的频道（达到条数或超时）。"""
    now = now if now is not None else time.time()
    return [ck for ck, b in _buckets.items() if b.should_flush(now)]


def bucket_stats() -> Dict[str, Any]:
    """攒批状态快照（供 WebUI 与日志）。"""
    return {
        "buckets": len(_buckets),
        "pending": {ck: len(b.items) for ck, b in _buckets.items() if b.items},
        "min_items": BATCH_MIN_ITEMS,
        "max_age_hours": BATCH_MAX_AGE_HOURS,
    }


def clear_buckets() -> None:
    _buckets.clear()


# ==================== Prompt ====================

SYSTEM_PROMPT = (
    "你是记忆整理助手。给你一段角色与用户的互动记录（每条是一次关系的"
    "「变化依据」或角色说过的语音内容），请把**真正发生过的事件**归纳成"
    "若干条可长期留存的记忆。"
)

USER_PROMPT_TEMPLATE = """以下是最近一段互动的要点记录（{n} 条）：

{materials}

请归纳成 1~5 条**事件记忆**，要求：

1. 只记**发生过什么**，不要复述情绪形容词，不要写"关系变好了"这类判断
2. 每条 30~80 字，第三人称或角色第一人称均可，但要具体（谁做了什么、结果如何）
3. 合并重复：同一件事被多次提到只保留一条
4. 忽略纯日常寒暄（打招呼、发表情、无信息量的调侃）
5. 如果没有值得记的事，返回空数组

输出严格 JSON（不要 markdown 代码块）：
{{"memories": [{{"title": "8字以内标题", "content": "事件描述",
  "tags": ["检索关键词1", "关键词2"], "event_at": "YYYY-MM-DD HH:MM"}}]}}
"""


def build_prompt(items: List[Dict[str, Any]]) -> str:
    lines = []
    for i, it in enumerate(items, 1):
        ts = ""
        if it.get("at"):
            ts = time.strftime("%m-%d %H:%M", time.localtime(int(it["at"])))
        kind = "语音" if it.get("kind") == "voice" else "互动"
        lines.append(f"{i}. [{ts}][{kind}] {it.get('text', '')}")
    return USER_PROMPT_TEMPLATE.format(n=len(items), materials="\n".join(lines))


# ==================== LLM 调用 ====================

_RE_FENCE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.S)


def parse_response(raw: str, *, strict: bool = False) -> List[Dict[str, Any]]:
    """解析 LLM 返回的 JSON；容忍 markdown 围栏与前后废话。

    strict=True（归纳流程用）时，**响应不可解析一律抛 ConsolidationFailed**，
    而不是返回空列表 —— 空列表在调用方语义里是「模型说没有可记的内容」。
    strict=False 保留旧的宽松行为，仅有测试/离线分析会用到。
    """
    if not raw:
        if strict:
            raise ConsolidationFailed("模型返回空内容")
        return []
    text = _RE_FENCE.sub("", raw.strip())
    # 容错：从第一个 { 到最后一个 }
    i, j = text.find("{"), text.rfind("}")
    if i >= 0 and j > i:
        text = text[i:j + 1]
    try:
        data = json.loads(text)
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[memory_log] 归纳结果不是合法 JSON：{exc}；原文前 200：{raw[:200]}")
        if strict:
            raise ConsolidationFailed(f"归纳结果不是合法 JSON：{exc}") from exc
        return []
    if not isinstance(data, dict):
        if strict:
            raise ConsolidationFailed("归纳结果根节点不是对象")
        return []
    mems = data.get("memories")
    if not isinstance(mems, list):
        if strict:
            raise ConsolidationFailed("归纳结果缺少 memories 数组")
        return []
    out: List[Dict[str, Any]] = []
    for m in mems:
        if not isinstance(m, dict):
            continue
        title = str(m.get("title") or "").strip()[:40]
        content = str(m.get("content") or "").strip()
        if not title or len(content) < 8:
            continue
        ev = 0
        raw_at = str(m.get("event_at") or "").strip()
        if raw_at:
            for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
                try:
                    ev = int(time.mktime(time.strptime(raw_at, fmt)))
                    break
                except Exception:  # noqa: BLE001
                    continue
        out.append({
            "title": title,
            "content": content[:400],
            "tags": [str(x) for x in (m.get("tags") or [])][:8],
            "event_at": ev,
        })
    return out


async def consolidate_with_llm(items: List[Dict[str, Any]],
                               model_group_name: str = "",
                               max_retries: int = LLM_MAX_RETRIES,
                               ) -> List[Dict[str, Any]]:
    """把一批素材交给 LLM 归纳，返回结构化记忆列表。

    失败（网络/超时/坏 JSON/模型组不可用）会自动重试；**最终仍失败则抛
    `ConsolidationFailed`**，由调用方把素材回填到桶里等下一轮。
    只有「响应合法且模型确实没产出可用记忆」时才返回空列表。
    """
    if not items:
        return []

    from nekro_agent.core.config import config
    from nekro_agent.services.agent.openai import (gen_openai_chat_response,
                                                   parse_extra_body)

    name = (model_group_name or "").strip() or config.USE_MODEL_GROUP
    try:
        group = config.get_model_group_info(name)
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[memory_log] 模型组 {name!r} 不可用：{exc}")
        raise ConsolidationFailed(f"模型组 {name!r} 不可用：{exc}") from exc

    extra_body = parse_extra_body(
        group.EXTRA_BODY, source_hint=f"persona memory_log: {name}") or {}
    extra_body.setdefault("response_format", {"type": "json_object"})
    # 关闭思考模式（省时 + 避免思考 token 挤占输出预算导致正文为空）
    extra_body.update(DISABLE_THINKING)

    prompt = build_prompt(items)
    last_err: Optional[BaseException] = None
    for attempt in range(max_retries + 1):
        try:
            resp = await asyncio.wait_for(
                gen_openai_chat_response(
                    model=group.CHAT_MODEL,
                    messages=[
                        {"role": "system", "content": SYSTEM_PROMPT},
                        {"role": "user", "content": prompt},
                    ],
                    api_key=group.API_KEY,
                    base_url=group.BASE_URL,
                    temperature=LLM_TEMPERATURE,
                    max_tokens=LLM_MAX_TOKENS,
                    extra_body=extra_body or None,
                    max_wait_time=LLM_TIMEOUT_SECONDS,
                ),
                timeout=LLM_TIMEOUT_SECONDS + 5,
            )
            parsed = parse_response(resp.response_content, strict=True)
            logger.info(
                f"[memory_log] 归纳完成：素材 {len(items)} 条 → 记忆 {len(parsed)} 条"
                f"（model={group.CHAT_MODEL}, attempt={attempt + 1}）")
            return parsed
        except asyncio.TimeoutError:
            last_err = TimeoutError(f"超过 {LLM_TIMEOUT_SECONDS}s")
            logger.warning(
                f"[memory_log] 归纳超时（{LLM_TIMEOUT_SECONDS}s），"
                f"第 {attempt + 1}/{max_retries + 1} 次")
        except Exception as exc:  # noqa: BLE001
            last_err = exc
            wait = min(8.0, 1.5 ** attempt)
            logger.warning(
                f"[memory_log] 归纳失败（第 {attempt + 1}/{max_retries + 1} 次）："
                f"{type(exc).__name__}: {exc}；{wait:.1f}s 后重试")
            if attempt < max_retries:
                await asyncio.sleep(wait)
            continue
        if attempt < max_retries:
            await asyncio.sleep(min(8.0, 1.5 ** attempt))

    # ⚠️ 这里必须抛，不能 return []：调用方见到空列表会当成「模型判断无内容」
    #    而不回填素材，那批互动就永久丢了（旧实现的日志还写着「素材保留待下轮」）。
    raise ConsolidationFailed(f"归纳最终失败：{last_err}")
