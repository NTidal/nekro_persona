"""角色背景记忆模块（阶梯式 + 好感度门控）

被 `plugin.py` 调用，负责：
- 1 档 `backgrounds/tier1/` : 每轮直接注入（身份级，硬上限，静态 → 缓存友好）
- 2 档 `backgrounds/tier2/` : 提到才检索注入（细节级，关键词优先 + 向量兜底，硬预算）
- 3 档 `backgrounds/tier3/` : 世界知识（其他角色/地图/势力等），提到才检索注入。
  **不受好感度门控** —— 常识不该因为不熟就不说；与 1/2 档「她亲历的事」严格分开措辞。
- 好感度门控：未达 `min_favor` 的记忆不注入正文，改为列入「暂时不想细说的事」

设计要点（本项目实测教训）：
- 2 档 query 用【最近消息原文】，绝不用 LLM 生成（LLM query 是文档腔，实测 0.32 vs 0.49）
- 2 档偏精确：1 档已兜住身份，2 档漏召回不致命，误召回反而会带偏话题
- 嵌入失败要能降级（嵌入 API 会突发 429），且降级函数必须与查询侧参数完全一致
  （该模型 dimensions 会改变投影，漏传会导致向量空间不一致、阈值失效）
- 1 档静态、2 档多数轮次为空 → 系统提示词前缀缓存友好
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import math
import shutil
from array import array as _float_array
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from nekro_agent.core.logger import get_sub_logger

logger = get_sub_logger("persona_memory")

# ---------------- 预算与阈值 ----------------
# 这些是**默认值**：运行时由 plugin.py 的配置项覆盖（见 tier1_settings / tier2_settings）。
# 保留模块级常量是为了：① 老代码/第三方 import 不炸；② 单测可直接引用默认值。
MAX_TIER1_CHARS = 2000       # 1 档硬上限，超限按 weight 降序截断
MAX_TIER2_ENTRIES = 2        # 2 档单轮最多注入条数
MAX_TIER2_CHARS = 500        # 2 档单轮字符预算
TIER2_SIM_THRESHOLD = 0.35   # 2 档向量阈值（30 条实测标定：噪声上限 0.311 / 相关下限 0.351）
TIER2_SCAN_MSGS = 6          # 判断"是否提到"时扫描的最近消息条数

# ---- 3 档：世界知识（扩展）----
# 与 2 档的关键差异：
#   · 不受好感度门控（常识不该因为不熟就不说）
#   · 独立预算与阈值（不与她自己的记忆抢 top-N、不污染 2 档标定）
#   · 独立注入措辞（客观见闻，不是第一人称回忆）
MAX_TIER3_ENTRIES = 3        # 3 档单轮最多注入条数
MAX_TIER3_CHARS = 700        # 3 档单轮字符预算
TIER3_SIM_THRESHOLD = 0.35   # 3 档向量阈值（待用真实世界知识库标定）
TIER3_SCAN_MSGS = 6          # 3 档扫描的最近消息条数
TIER3_ENABLED = True         # 3 档总开关（配置可覆盖）

_PKG_BG_DIR = Path(__file__).parent / "backgrounds"
"""包内自带的背景记忆（首次运行时作为种子复制到插件数据目录）。"""

_BG_DIR = _PKG_BG_DIR
_TIER1_DIR = _BG_DIR / "tier1"
_TIER2_DIR = _BG_DIR / "tier2"
_TIER3_DIR = _BG_DIR / "tier3"
_FAVOR_FILE = _BG_DIR / "favor.json"

# 数据文件清单：种子复制与判存都以这几项为准（不带 .bak/缓存/临时快照）
_BG_FILES = ("favor.json", "tier1/core.json", "tier2/lore.json", "tier3/world.json")

# 向量磁盘缓存（扩展）：
#   向量原先只存内存 → 每次启动都要重嵌全部条目 → 必然撞 429，随机有条目拿不到向量。
#   缓存放数据目录（不是包内），按「嵌入配置签名 + 条目文本」做 key。
_VEC_CACHE_NAME = ".vec_cache.json"
_VEC_CACHE_VERSION = 1


def bg_dir() -> Path:
    """当前背景记忆数据目录。"""
    return _BG_DIR


def configure_bg_dir(data_dir) -> Path:
    """把背景记忆数据目录切到插件数据目录；首次运行从包内种子复制。

    为什么不再放包内：包目录在升级/重装时会被整体替换，用户改过的记忆会被覆盖，
    WebUI 写入产生的 .bak、手工快照也会被打进发布包。数据改放插件数据目录后，
    包内那份只作首次运行的种子。

    只复制 _BG_FILES 里的数据文件；目录不可用时回退包内目录，保证仍可运行。
    """
    global _BG_DIR, _TIER1_DIR, _TIER2_DIR, _TIER3_DIR, _FAVOR_FILE
    target = Path(data_dir)
    try:
        if not target.exists() and _PKG_BG_DIR.is_dir():
            for rel in _BG_FILES:
                src = _PKG_BG_DIR / rel
                if src.is_file():
                    dst = target / rel
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(src, dst)
            logger.info(f"背景记忆数据目录首次初始化（自包内种子复制）：{target}")
        target.mkdir(parents=True, exist_ok=True)
    except Exception:  # noqa: BLE001
        logger.exception(f"背景记忆数据目录不可用，回退包内目录：{target}")
        target = _PKG_BG_DIR
    _BG_DIR = target
    _TIER1_DIR = target / "tier1"
    _TIER2_DIR = target / "tier2"
    _TIER3_DIR = target / "tier3"
    _FAVOR_FILE = target / "favor.json"
    # 目录变了：作废已加载缓存，下次按新目录 mtime 重新读取
    _state["loaded"] = False
    _state["mtime_key"] = None
    return target

_state: Dict[str, Any] = {
    "loaded": False,
    "mtime_key": None,
    "tier1": [],
    "tier2": [],
    "tier2_vecs": [],
    "tier3": [],
    "tier3_vecs": [],
    "last_hit": "",
    "last_hit_tier3": "",
}


def tier1_settings(override: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """1 档运行参数（配置可覆盖模块默认值）。

    1 档是「每轮直接注入、静态不变」的身份级记忆，**不涉及检索**，
    所以只有字符预算一项，没有阈值 / 扫描条数。
    """
    o = override or {}
    return {
        "max_chars": max(50, int(o.get("max_chars", MAX_TIER1_CHARS) or MAX_TIER1_CHARS)),
    }


def tier2_settings(override: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """2 档运行参数（配置可覆盖模块默认值）。"""
    o = override or {}
    return {
        "max_entries": max(1, int(o.get("max_entries", MAX_TIER2_ENTRIES) or MAX_TIER2_ENTRIES)),
        "max_chars": max(50, int(o.get("max_chars", MAX_TIER2_CHARS) or MAX_TIER2_CHARS)),
        "threshold": float(o.get("threshold", TIER2_SIM_THRESHOLD) or TIER2_SIM_THRESHOLD),
        "scan_msgs": max(1, int(o.get("scan_msgs", TIER2_SCAN_MSGS) or TIER2_SCAN_MSGS)),
    }


def tier3_settings(override: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """3 档运行参数（配置可覆盖模块默认值）。"""
    o = override or {}
    return {
        "enabled": bool(o.get("enabled", TIER3_ENABLED)),
        "max_entries": max(1, int(o.get("max_entries", MAX_TIER3_ENTRIES) or MAX_TIER3_ENTRIES)),
        "max_chars": max(50, int(o.get("max_chars", MAX_TIER3_CHARS) or MAX_TIER3_CHARS)),
        "threshold": float(o.get("threshold", TIER3_SIM_THRESHOLD) or TIER3_SIM_THRESHOLD),
        "scan_msgs": max(1, int(o.get("scan_msgs", TIER3_SCAN_MSGS) or TIER3_SCAN_MSGS)),
    }


# ---------------- 加载 ----------------
def _mtime_key() -> Tuple:
    keys: List[Tuple[str, float]] = []
    for d in (_TIER1_DIR, _TIER2_DIR, _TIER3_DIR):
        if d.is_dir():
            for fp in sorted(d.glob("*.json")):
                try:
                    keys.append((fp.name, fp.stat().st_mtime))
                except OSError:
                    continue
    if _FAVOR_FILE.is_file():
        try:
            keys.append((_FAVOR_FILE.name, _FAVOR_FILE.stat().st_mtime))
        except OSError:
            pass
    return tuple(keys)


def _read_dir(d: Path) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    if not d.is_dir():
        return out
    for fp in sorted(d.glob("*.json")):
        try:
            data = json.loads(fp.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001
            logger.error(f"[persona] 背景文件解析失败，已阻止加载: {fp.name}: {exc}")
            raise ValueError(f"背景文件损坏：{fp.name}") from exc
        items = data.get("entries", []) if isinstance(data, dict) else data
        if not isinstance(items, list):
            raise ValueError(f"背景文件 entries 必须是数组：{fp.name}")
        for item in items:
            if not isinstance(item, dict):
                raise ValueError(f"背景文件存在非对象条目：{fp.name}")
            if item.get("enabled", True):
                item["_file"] = fp.stem
                out.append(item)
    return out


def load_favor_cfg() -> Dict[str, Any]:
    if not _FAVOR_FILE.is_file():
        return {}
    try:
        data = json.loads(_FAVOR_FILE.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        logger.error(f"[persona] favor.json 解析失败，已阻止加载: {exc}")
        raise ValueError("favor.json 损坏，已阻止加载") from exc
    if not isinstance(data, dict):
        raise ValueError("favor.json 根节点必须是对象")
    return data


def _embed_source(e: Dict[str, Any]) -> str:
    """2 档条目的嵌入文本：title + content + triggers。

    实测：只嵌入叙述文本时噪声/相关间隔为负（不可用）；加入 triggers 后间隔最大。
    必须把"用户会说的词"嵌进向量。
    """
    title = str(e.get("title", "")).strip()
    content = str(e.get("content", "")).strip()
    trig = " ".join(str(t).strip() for t in (e.get("triggers") or []) if str(t).strip())
    return f"{title} {content} {trig}".strip()


# 嵌入端点降级链（实测两端点同模型、同向量空间，余弦 1.000000，可混用）
_EMBED_GROUPS = ("text-embedding", "text-embedding2")


def _vec_cache_path() -> Path:
    """向量缓存文件（数据目录下）。"""
    return _BG_DIR / _VEC_CACHE_NAME


def _embed_signature() -> str:
    """嵌入配置签名：维度或端点组变了，缓存必须整体失效。

    为什么必须带上：维度不一致会让条目向量与查询向量不在同一空间，
    相似度整体偏移、阈值失效——这种坏法很难从现象上看出来。
    """
    dim = ""
    try:
        from nekro_agent.services.memory.embedding_service import embedding_service

        dim = str(getattr(embedding_service, "resolved_dimension", "") or "")
    except Exception:  # noqa: BLE001
        pass
    return f"v{_VEC_CACHE_VERSION}|dim={dim}|groups={','.join(_EMBED_GROUPS)}"


def _vec_key(text: str, sig: str, scope: str = "") -> str:
    """条目文本 → 缓存 key（带档位前缀）。

    为什么要前缀：缓存文件是 2/3 档共用的，而 _vectorize 每次只处理一个档位。
    清理旧 key 时必须能区分"这是别的档位的 key"和"这是已失效的 key"，
    否则先跑的档位会把后一档的缓存全清掉（实测踩过：2 档把 3 档的 93 条清成 0）。
    """
    h = hashlib.sha1()
    h.update(sig.encode("utf-8"))
    h.update(b"\x00")
    h.update(text.encode("utf-8"))
    return f"{scope}:{h.hexdigest()}" if scope else h.hexdigest()


def _pack_vec(vec: List[float]) -> str:
    """向量 → base64(float32)，比 JSON 数组小约 3 倍。"""
    return base64.b64encode(_float_array("f", vec).tobytes()).decode("ascii")


def _unpack_vec(raw: Any) -> Optional[List[float]]:
    try:
        buf = _float_array("f")
        buf.frombytes(base64.b64decode(str(raw)))
        return list(buf)
    except Exception:  # noqa: BLE001
        return None


def _load_vec_cache() -> Dict[str, str]:
    """读缓存；任何异常都当作空缓存（缓存坏了不该挡住加载）。"""
    fp = _vec_cache_path()
    if not fp.is_file():
        return {}
    try:
        data = json.loads(fp.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or data.get("version") != _VEC_CACHE_VERSION:
            return {}
        if data.get("signature") != _embed_signature():
            logger.info("[persona] 嵌入配置已变，向量缓存作废重建")
            return {}
        vecs = data.get("vectors")
        return vecs if isinstance(vecs, dict) else {}
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[persona] 向量缓存读取失败，按空缓存处理: {exc}")
        return {}


def _save_vec_cache(vecs: Dict[str, str]) -> None:
    """写缓存；失败只记日志。"""
    fp = _vec_cache_path()
    try:
        payload = {
            "version": _VEC_CACHE_VERSION,
            "signature": _embed_signature(),
            "count": len(vecs),
            "vectors": vecs,
        }
        tmp = fp.with_suffix(fp.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        tmp.replace(fp)
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[persona] 向量缓存写入失败（不影响本次运行）: {exc}")


async def _embed_one(text: str) -> Optional[List[float]]:
    """单条嵌入，按降级链依次尝试各端点。

    ⚠️ 必须与查询侧（embedding_service.embed_text）参数完全一致（尤其 dimensions），
    否则条目向量与查询向量不在同一空间 → 相似度整体偏移、阈值失效。
    """
    from nekro_agent.core.config import config
    from nekro_agent.services.agent.openai import gen_openai_embeddings
    from nekro_agent.services.memory.embedding_service import embedding_service

    dim = embedding_service.resolved_dimension
    for group_name in _EMBED_GROUPS:
        g = (config.MODEL_GROUPS or {}).get(group_name)
        if g is None:
            continue
        try:
            vec = await gen_openai_embeddings(
                model=g.CHAT_MODEL,
                input=text.strip(),
                dimensions=dim,
                api_key=g.API_KEY,
                base_url=g.BASE_URL,
                timeout=30,
            )
            if vec:
                return vec
        except Exception as e:  # noqa: BLE001
            logger.debug(f"[persona] 端点 {group_name} 嵌入失败: {e}")
    return None


async def _vectorize(items: List[Dict[str, Any]], label: str,
                     scope: str = "") -> List[Optional[List[float]]]:
    """批量向量化，失败转逐条重试。返回与 items 等长的向量数组。

    实测教训：嵌入 API 会突发 429，必须能降级；且降级函数参数（尤其 dimensions）
    必须与查询侧完全一致，否则向量空间不一致 → 阈值失效。
    """
    vecs: List[Optional[List[float]]] = [None] * len(items)
    if not items:
        return vecs
    texts = [_embed_source(e) for e in items]

    # ── 扩展：先查磁盘缓存，只嵌没命中的 ──
    sig = _embed_signature()
    cache = _load_vec_cache()
    keys = [_vec_key(txt, sig, scope) for txt in texts]
    hit = 0
    for i, k in enumerate(keys):
        if cache.get(k):
            v = _unpack_vec(cache[k])
            if v:
                vecs[i] = v
                hit += 1
    if hit:
        logger.info(f"[persona] {label} 缓存命中 {hit}/{len(items)}")

    pending = [i for i, v in enumerate(vecs) if not v]
    if pending:
        pending_texts = [texts[i] for i in pending]
        try:
            from nekro_agent.services.memory.embedding_service import embedding_service

            got = await embedding_service.embed_batch(pending_texts, batch_size=3)
            for slot, v in zip(pending, list(got)):
                if v:
                    vecs[slot] = v
            logger.info(f"[persona] {label} 批量向量化 "
                        f"{sum(1 for v in vecs if v)}/{len(items)}（待嵌 {len(pending)}）")
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[persona] {label} 批量向量化失败，转逐条: {e}")

    # 新拿到的写回缓存；只清理「本档位」已失效的 key，不碰别的档位
    if pending:
        live = {k: _pack_vec(vecs[i]) for i, k in enumerate(keys) if vecs[i]}
        prefix = f"{scope}:" if scope else ""
        keep = {k: v for k, v in cache.items() if not k.startswith(prefix)}
        keep.update(live)
        if not scope:  # 无 scope 时退回全量合并（不会误删）
            keep = {**cache, **live}
        if len(keep) != len(cache):
            logger.info(f"[persona] {label} 向量缓存：{len(cache)} → {len(keep)} 条（本档 {len(live)}）")
        _save_vec_cache(keep)

    failed = [i for i, v in enumerate(vecs) if not v]
    if failed:
        logger.warning(
            f"[persona] {label} {len(failed)} 条待重试: {[items[i].get('id') for i in failed]}",
        )
        for i in failed:
            vecs[i] = await _embed_one(texts[i])
            if not vecs[i]:
                logger.error(f"[persona] {label} 条目 {items[i].get('id')} 向量化失败，只能靠关键词命中")
            await asyncio.sleep(0.3)
        logger.info(f"[persona] {label} 重试后向量数 {sum(1 for v in vecs if v)}/{len(items)}")
    return vecs


async def ensure_loaded(force: bool = False) -> None:
    """加载背景数据；文件 mtime 变化时自动重载，条目在此一次性向量化（2/3 档）。"""
    mk = _mtime_key()
    if not force and _state["loaded"] and _state["mtime_key"] == mk:
        return

    t1 = _read_dir(_TIER1_DIR)
    t1.sort(key=lambda x: -int(x.get("weight", 1)))
    t2 = _read_dir(_TIER2_DIR)
    t2.sort(key=lambda x: -int(x.get("weight", 1)))
    t3 = _read_dir(_TIER3_DIR)
    t3.sort(key=lambda x: -int(x.get("weight", 1)))
    # 先验证门控配置，避免坏 favor.json 被当成空配置继续运行。
    load_favor_cfg()

    vecs = await _vectorize(t2, "2 档", scope="t2")
    vecs3 = await _vectorize(t3, "3 档", scope="t3")

    _state.update(
        loaded=True, mtime_key=mk,
        tier1=t1, tier2=t2, tier2_vecs=vecs,
        tier3=t3, tier3_vecs=vecs3,
    )
    logger.info(f"[persona] 背景已加载: 1 档 {len(t1)} 条, 2 档 {len(t2)} 条, 3 档 {len(t3)} 条")


# ---------------- 门控 ----------------
def stage_guide(score: int, cfg: Optional[Dict[str, Any]] = None) -> str:
    """当前档位的互动指引（档位专属 + 所有档位共用的边界提醒）。

    配置在 favor.json 的 stage_guides / stage_guide_common。
    实测：339 个关系档案里 316 个在「中立」档，原先每档只有一句内置兜底文案，
    等于绝大多数用户拿不到任何分档指引。
    """
    c = cfg if cfg is not None else load_favor_cfg()
    name = stage_name(score, c)
    guides = c.get("stage_guides") or {}
    body = str(guides.get(name) or "").strip()
    common = str(c.get("stage_guide_common") or "").strip()
    parts = [p for p in (body, common) if p]
    return " ".join(parts)


def stage_name(score: int, cfg: Dict[str, Any]) -> str:
    for st in cfg.get("stages") or []:
        try:
            if score <= int(st.get("max", 999)):
                return str(st.get("name", ""))
        except (TypeError, ValueError):
            continue
    return ""


def split_by_favor(
    entries: List[Dict[str, Any]],
    score: int,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """按 min_favor 把条目分成「可讲」与「暂不细说」。"""
    ok: List[Dict[str, Any]] = []
    blocked: List[Dict[str, Any]] = []
    for e in entries:
        try:
            need = int(e.get("min_favor", 0))
        except (TypeError, ValueError):
            need = 0
        (ok if need <= score else blocked).append(e)
    return ok, blocked


def _topic_of(e: Dict[str, Any]) -> str:
    return str(e.get("title") or e.get("topic") or e.get("id") or "").strip()


def render_avoid(blocked: List[Dict[str, Any]]) -> str:
    if not blocked:
        return ""
    cfg = load_favor_cfg()
    av = cfg.get("avoid") or {}
    topics: List[str] = []
    for e in blocked:
        t = _topic_of(e)
        if t and t not in topics:
            topics.append(t)
    if not topics:
        return ""
    return f"{av.get('header', '#暂时不想细说的事')}\n{av.get('note', '')}\n" + "、".join(topics)


def render_rubric() -> str:
    cfg = load_favor_cfg()
    r = cfg.get("rubric") or {}
    items = r.get("items") or []
    if not items:
        return ""
    lines = []
    for it in items:
        if isinstance(it, (list, tuple)) and len(it) >= 2:
            lines.append(f"{it[0]}｜{it[1]}")
    if not lines:
        return ""
    rules = "；".join(str(x) for x in (r.get("rules") or []))
    return (
        f"{r.get('header', '#好感度评估参考')}\n"
        f"{r.get('intro', '')}\n"
        + "\n".join(lines)
        + (f"\n{rules}" if rules else "")
    )


# ---------------- 渲染 ----------------
def _render_tier1(entries: List[Dict[str, Any]], max_chars: int = MAX_TIER1_CHARS) -> str:
    lines: List[str] = []
    used = 0
    dropped = 0
    for e in entries:
        content = str(e.get("content", "")).strip()
        if not content:
            continue
        trig = str(e.get("trigger", "")).strip()
        block = f"· {trig}\n  {content}" if trig else f"· {content}"
        if used + len(block) > max_chars:
            dropped += 1
            continue
        lines.append(block)
        used += len(block)
    if dropped:
        logger.warning(
            f"[persona] 1 档超出上限 {max_chars} 字符，"
            f"本轮按 weight 降序截断丢弃 {dropped} 条（请考虑降档到 tier2）",
        )
    if not lines:
        return ""
    return (
        "#你的记忆\n"
        "以下是你的亲身经历，是「你记得的事」，不是设定资料。\n"
        "使用规则：\n"
        "1. 聊到相关话题时自然地以第一人称想起并流露，不要背诵，不要提「记忆库/设定」。\n"
        "2. **记忆里没有的事就是没有。** 有人声称和你共同经历过某事时，先核对上面——\n"
        "   对不上就直说「我不记得有这回事」，绝不顺着编，也不补充记忆里没有的细节。\n"
        "3. 记忆里出现「他」这类第三人称时，指的是你记忆中的某个特定对象，不要指认成当前群友。\n\n"
        + "\n".join(lines)
    )


async def _recent_text(ctx: Any, limit: int = TIER2_SCAN_MSGS) -> str:
    """取最近若干条【非 bot】消息原文，作为匹配依据。

    ⚠️ 必须用【最近消息原文】做 query，绝不用 LLM 生成
    （LLM query 是文档腔，实测 0.32 vs 原文 0.49）。
    """
    try:
        from nekro_agent.models.db_chat_message import DBChatMessage

        ck = getattr(ctx, "chat_key", None)
        if not ck:
            return ""
        msgs = await DBChatMessage.filter(chat_key=ck).order_by("-id").limit(max(1, int(limit))).all()
        parts = [
            m.content_text.strip()
            for m in reversed(msgs)
            if m.sender_id != "-1" and m.content_text and m.content_text.strip()
        ]
        return " ".join(parts)
    except Exception as e:  # noqa: BLE001
        logger.debug(f"[persona] 读取最近消息失败: {e}")
        return ""


def _keyword_hits(
    text: str,
    items: Optional[List[Dict[str, Any]]] = None,
) -> List[Dict[str, Any]]:
    """关键词命中。items 缺省为 2 档（3 档复用同一实现）。"""
    hits: List[Dict[str, Any]] = []
    low = text.lower()
    for e in (items if items is not None else _state["tier2"]):
        for kw in e.get("triggers") or []:
            k = str(kw).strip().lower()
            if k and k in low:
                hits.append(e)
                break
    return hits


def _cos(a: List[float], b: List[float]) -> float:
    if not a or not b:
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


async def _vector_hits(
    text: str,
    items: Optional[List[Dict[str, Any]]] = None,
    vecs: Optional[List[Optional[List[float]]]] = None,
    threshold: Optional[float] = None,
) -> List[Tuple[float, Dict[str, Any]]]:
    """向量命中。缺省走 2 档参数（3 档显式传入自己的库/阈值）。"""
    items = _state["tier2"] if items is None else items
    vecs = _state["tier2_vecs"] if vecs is None else vecs
    threshold = TIER2_SIM_THRESHOLD if threshold is None else float(threshold)
    if not text or not any(vecs):
        return []
    try:
        from nekro_agent.services.memory.embedding_service import embed_text

        qv = await embed_text(text)
    except Exception as e:  # noqa: BLE001
        logger.debug(f"[persona] query 嵌入失败，跳过向量匹配: {e}")
        return []
    scored: List[Tuple[float, Dict[str, Any]]] = []
    for e, v in zip(items, vecs):
        if not v:
            continue
        scored.append((_cos(qv, v), e))
    scored.sort(key=lambda x: -x[0])
    return [s for s in scored if s[0] >= threshold]


async def _match_tier2(
    ctx: Any,
    cfg: Optional[Dict[str, Any]] = None,
) -> Tuple[List[Dict[str, Any]], str]:
    """返回 (命中条目, 命中方式)。关键词优先，未命中再走向量。

    cfg: 2 档运行参数（scan_msgs / threshold），缺省用模块默认值。
    """
    s = tier2_settings(cfg)
    if not _state["tier2"]:
        return [], ""
    text = await _recent_text(ctx, limit=int(s["scan_msgs"]))
    if not text:
        return [], ""
    hits = _keyword_hits(text)
    if hits:
        return hits, "keyword"
    vh = await _vector_hits(text, threshold=s["threshold"])
    return [e for _s, e in vh], "vector"


async def _match_tier3(ctx: Any, cfg: Dict[str, Any]) -> Tuple[List[Dict[str, Any]], str]:
    """3 档匹配：关键词优先，未命中再走向量。独立阈值，不受好感度门控。"""
    items = _state["tier3"]
    if not items:
        return [], ""
    text = await _recent_text(ctx, limit=int(cfg.get("scan_msgs", TIER3_SCAN_MSGS)))
    if not text:
        return [], ""
    hits = _keyword_hits(text, items)
    if hits:
        return hits, "keyword"
    vh = await _vector_hits(
        text, items, _state["tier3_vecs"], cfg.get("threshold", TIER3_SIM_THRESHOLD),
    )
    return [e for _s, e in vh], "vector"


def _render_tier2(
    entries: List[Dict[str, Any]],
    max_entries: int = MAX_TIER2_ENTRIES,
    max_chars: int = MAX_TIER2_CHARS,
) -> str:
    lines: List[str] = []
    used = 0
    dropped = 0
    for e in entries[:max_entries]:
        content = str(e.get("content", "")).strip()
        if not content:
            continue
        # 超预算的条目跳过而非 break：否则一条过长记忆会永久阻断其后所有条目，
        # 且作者收不到任何提示（与 _render_tier1 的截断口径保持一致）。
        if used + len(content) > max_chars:
            dropped += 1
            continue
        lines.append(f"· {content}")
        used += len(content)
    if dropped:
        logger.warning(
            f"[persona] 2 档超出单轮预算 {max_chars} 字符，"
            f"本轮跳过 {dropped} 条过长记忆（请缩短 content 或提高 TIER2_MAX_CHARS）",
        )
    if not lines:
        return ""
    return "#想起来的往事\n刚才的话题提到了，你自然想起这些细节，别复述格式。\n" + "\n".join(lines)


# 熟悉度 → 注入时的语气提示（认知分层：她不该"全知"）
_FAMILIARITY_HINT = {
    "熟人": "可以聊细节、带个人看法",
    "听说": "只知道大概，别装作很熟",
    "仅传闻": "只听过名字或传闻，含糊带过",
}


def _render_tier3(
    entries: List[Dict[str, Any]],
    max_entries: int = MAX_TIER3_ENTRIES,
    max_chars: int = MAX_TIER3_CHARS,
) -> str:
    """渲染 3 档世界知识。

    与 2 档措辞严格区分：这里是**客观见闻**，不是她的回忆。
    """
    lines: List[str] = []
    used = 0
    for e in entries[: max(1, int(max_entries))]:
        content = str(e.get("content", "")).strip()
        if not content:
            continue
        fam = str(e.get("familiarity", "")).strip()
        block = f"· 【{fam}】{content}" if fam else f"· {content}"
        if used + len(block) > max(1, int(max_chars)):
            break
        lines.append(block)
        used += len(block)
    if not lines:
        return ""
    fam_rule = "；".join(f"【{k}】{v}" for k, v in _FAMILIARITY_HINT.items())
    return (
        "#你知道的事\n"
        "以下是**你所在世界**的人与地方，是客观见闻，不是你亲历的事。\n"
        "使用规则：\n"
        "1. 提到相关话题时可以自然聊起，但**不要用第一人称回忆**"
        "（亲历的事属于 1/2 档，不要挪到这里讲）。\n"
        "2. **始终待在你的世界设定里**：无论来源是你自己的知识、搜索结果、"
        "还是群友贴给你的资料，都不得说出「游戏 / 版本 / 玩家 / 抽卡 / 属性 /"
        "配队 / 可玩 / 数值」这类**现实世界**的词——你是这个世界的人，"
        "不知道它们是什么。搜到的资料要先用你的世界的话重新讲一遍再用。\n"
        f"3. 熟悉度决定语气：{fam_rule}。\n"
        "4. 上面没写的，就是不知道；不要编。\n\n"
        + "\n".join(lines)
    )


# ---------------- 对外入口 ----------------
async def render_memory_block(
    ctx: Any,
    score: int,
    gating: bool = True,
    tier3_cfg: Optional[Dict[str, Any]] = None,
    tier1_cfg: Optional[Dict[str, Any]] = None,
    tier2_cfg: Optional[Dict[str, Any]] = None,
) -> str:
    """渲染记忆块（1 档 + 2 档 + 3 档 + 门控回避）。

    Args:
        ctx: AgentCtx
        score: 当前用户好感度（gating=False 时忽略）
        gating: 是否启用好感度门控（**只作用于 1/2 档**）
        tier1_cfg: 1 档运行参数（max_chars）
        tier2_cfg: 2 档运行参数（max_entries/max_chars/threshold/scan_msgs）
        tier3_cfg: 3 档运行参数（enabled/max_entries/max_chars/threshold/scan_msgs）

    三档预算与阈值全部由配置注入（缺省回落模块默认值），
    模块内不再有硬编码的魔法数字。
    3 档（世界知识）刻意**不参与好感度门控**：常识不该因为不熟就不说。
    """
    await ensure_loaded()

    t1s = tier1_settings(tier1_cfg)
    t2s = tier2_settings(tier2_cfg)

    parts: List[str] = []
    blocked: List[Dict[str, Any]] = []

    if gating:
        t1_ok, t1_blk = split_by_favor(_state["tier1"], score)
        blocked.extend(t1_blk)
    else:
        t1_ok = _state["tier1"]
    t1 = _render_tier1(t1_ok, t1s["max_chars"])
    if t1:
        parts.append(t1)

    hits, src = await _match_tier2(ctx, t2s)
    if hits:
        if gating:
            t2_ok, t2_blk = split_by_favor(hits, score)
            blocked.extend(t2_blk)
        else:
            t2_ok = hits
        t2 = _render_tier2(t2_ok, t2s["max_entries"], t2s["max_chars"])
        if t2:
            names = ",".join(str(e.get("id", "?")) for e in t2_ok[: t2s["max_entries"]])
            _state["last_hit"] = f"{src}:{names}"
            logger.info(f"[persona] 2 档命中({src}): {names}")
            parts.append(t2)

    # ---- 3 档：世界知识（扩展）----
    # 不参与好感度门控：无论和对方多熟，世界常识都可以聊。
    t3s = tier3_settings(tier3_cfg)
    if t3s["enabled"]:
        hits3, src3 = await _match_tier3(ctx, t3s)
        if hits3:
            t3 = _render_tier3(hits3, t3s["max_entries"], t3s["max_chars"])
            if t3:
                names3 = ",".join(
                    str(e.get("id", "?")) for e in hits3[: t3s["max_entries"]]
                )
                _state["last_hit_tier3"] = f"{src3}:{names3}"
                logger.info(f"[persona] 3 档命中({src3}): {names3}")
                parts.append(t3)

    av = render_avoid(blocked)
    if av:
        parts.append(av)
        cfg = load_favor_cfg()
        logger.info(
            f"[persona] 好感度门控: score={score} stage={stage_name(score, cfg)} "
            f"暂不细说 {len(blocked)} 项",
        )

    if gating:
        rb = render_rubric()
        if rb:
            parts.append(rb)

    return "\n\n".join(parts)


def status(
    tier3_cfg: Optional[Dict[str, Any]] = None,
    tier1_cfg: Optional[Dict[str, Any]] = None,
    tier2_cfg: Optional[Dict[str, Any]] = None,
) -> str:
    """状态摘要（供管理工具返回）。"""
    t1, t2, t3 = _state["tier1"], _state["tier2"], _state["tier3"]
    t1_chars = sum(len(str(e.get("content", ""))) + len(str(e.get("trigger", ""))) for e in t1)
    vec_ok = sum(1 for v in _state["tier2_vecs"] if v)
    vec3_ok = sum(1 for v in _state["tier3_vecs"] if v)
    g1 = sum(1 for e in t1 if int(e.get("min_favor", 0) or 0) > 0)
    g2 = sum(1 for e in t2 if int(e.get("min_favor", 0) or 0) > 0)
    cfg = load_favor_cfg()
    t1s = tier1_settings(tier1_cfg)
    t2s = tier2_settings(tier2_cfg)
    t3s = tier3_settings(tier3_cfg)
    return (
        f"1 档 {len(t1)} 条（{t1_chars}/{t1s['max_chars']} 字符，{g1} 条受门控），"
        f"2 档 {len(t2)} 条（向量 {vec_ok} 条，阈值 {t2s['threshold']}，{g2} 条受门控），"
        f"3 档 {len(t3)} 条（向量 {vec3_ok} 条，阈值 {t3s['threshold']}，"
        f"{'启用' if t3s['enabled'] else '停用'}，不受门控）。"
        f"门控配置：{'启用' if cfg.get('enabled', True) else '关闭'}"
        f"（{len(cfg.get('stages') or [])} 级档位）。"
    )
