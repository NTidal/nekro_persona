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
import random
import shutil
import time
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
# 命中统计 sidecar（A4 访问强化）：与 .vec_cache.json 同款模式——独立文件、
# 版本化、原子写、读失败按空处理。**绝不回写作者的 backgrounds/*.json**，
# 否则会弄脏 curated 内容、触发热重载 mtime、并和 WebUI 编辑抢写。
_HIT_STATS_NAME = ".hit_stats.json"
_HIT_STATS_VERSION = 1


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
    # 查询侧嵌入缓存也一并清空（内存卫生；正确性本已由 key 含签名保证）
    _query_cache_clear()
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
    # A4 访问强化：命中计数（内存态，去抖落盘到 .hit_stats.json）
    "hit_stats": {},
    "hit_dirty": False,
    "hit_last_save": 0.0,
    # 查询侧嵌入缓存（v1.6.2）：key=签名+文本哈希 → (存入时刻, 向量)。
    # 只缓存 query 向量，短 TTL + LRU，纯内存不落盘（磁盘缓存只服务条目侧）。
    "query_cache": {},
    # 嵌入后端健康（v1.6.3）：custom 端点连续失败时置降级标志，供 /api/overview 暴露、
    # 概览页告警「检索已降级为纯关键词」。绝不混空间回落 NA（见 _embed_mark_*）。
    "embed_degraded": False,
    "embed_degraded_reason": "",
    "embed_fail_streak": 0,
    "embed_last_warn": 0.0,
}

# ---- 查询侧嵌入缓存参数（v1.6.2）----
# 为什么需要：条目侧向量有磁盘缓存，但每轮对话的 query 向量旧实现现算现扔，
# 且同一轮里 2 档 / 3 档各嵌一次几乎相同的最近文本 → 每轮至少 2 次嵌入 API 调用。
# 限流较严的端点上纯属浪费。缓存按 _embed_signature() 隔离，
# 换模型自动失效；短 TTL 保证话题漂移后不会长期用陈旧 query 向量。
_QUERY_CACHE_TTL = 300.0      # 秒：5 分钟，覆盖一段连续对话，又不至于长期陈旧
_QUERY_CACHE_MAX = 128        # 条：LRU 上限，超出淘汰最旧



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
        "lifecycle": o.get("lifecycle"),   # A2/A3：透传生命周期配置给检索核心
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
        "lifecycle": o.get("lifecycle"),   # A2/A3：透传生命周期配置给检索核心
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

# ---- 独立嵌入后端（v1.6.0）----
# plugin.py 在 init 时注入一个「返回嵌入配置 dict」的 callable，使 memory.py
# 不必 import 插件配置、且能跟随运行时配置变更。缺省 None → 走 NA 内置（provider="na"）。
# 配置 dict 形如：{"provider":"na"|"custom","base_url","api_key","model","dimensions","batch_size","timeout"}
_EMBED_CFG_PROVIDER = None


def set_embed_config_provider(fn) -> None:
    """由 plugin.py 注入嵌入配置提供者（callable → dict）。"""
    global _EMBED_CFG_PROVIDER
    _EMBED_CFG_PROVIDER = fn


def _embed_cfg() -> Dict[str, Any]:
    """当前嵌入配置；拿不到时安全回落到 NA 内置。"""
    if _EMBED_CFG_PROVIDER is not None:
        try:
            cfg = _EMBED_CFG_PROVIDER()
            if isinstance(cfg, dict):
                return cfg
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"[persona] 读取嵌入配置失败，回落 NA 内置: {exc}")
    return {"provider": "na"}


def _embed_is_custom(cfg: Optional[Dict[str, Any]] = None) -> bool:
    c = cfg if cfg is not None else _embed_cfg()
    if str(c.get("provider", "na") or "na").lower() != "custom":
        return False
    # custom 必须有 base_url + model 才可用，否则回落 NA（避免半配置把检索打挂）
    return bool(str(c.get("base_url", "")).strip() and str(c.get("model", "")).strip())


def _vec_cache_path() -> Path:
    """向量缓存文件（数据目录下）。"""
    return _BG_DIR / _VEC_CACHE_NAME


def _embed_signature() -> str:
    """嵌入配置签名：后端身份 / 维度 / 端点组变了，缓存必须整体失效。

    为什么必须带上：维度或模型不一致会让条目向量与查询向量不在同一空间，
    相似度整体偏移、阈值失效——这种坏法很难从现象上看出来。
    ⚠️ v1.6.0：custom 后端的签名含 base_url+model+dimensions，切换嵌入模型时
    签名变化 → 旧向量缓存自动整体作废重建，杜绝新旧模型向量混算。
    na 路径的签名格式保持与历史版本逐字一致，避免无谓地让既有缓存失效。
    """
    cfg = _embed_cfg()
    if _embed_is_custom(cfg):
        return (
            f"v{_VEC_CACHE_VERSION}|custom|{str(cfg.get('base_url','')).rstrip('/')}|"
            f"{cfg.get('model','')}|dim={int(cfg.get('dimensions', 0) or 0)}"
        )
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


# ---------------- A4 访问强化：命中统计 sidecar ----------------
def _hit_stats_path() -> Path:
    return _BG_DIR / _HIT_STATS_NAME


def _hit_key(tier: int, entry_id: str) -> str:
    return f"t{int(tier)}:{entry_id}"


def _load_hit_stats() -> Dict[str, Dict[str, Any]]:
    """读命中统计；任何异常按空处理（坏了不该挡住加载，与 vec_cache 同款容错）。"""
    fp = _hit_stats_path()
    if not fp.is_file():
        return {}
    try:
        data = json.loads(fp.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or data.get("version") != _HIT_STATS_VERSION:
            return {}
        stats = data.get("stats")
        return stats if isinstance(stats, dict) else {}
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[persona] 命中统计读取失败，按空处理: {exc}")
        return {}


def _save_hit_stats() -> None:
    """原子写命中统计；失败只记日志，绝不影响渲染。"""
    fp = _hit_stats_path()
    try:
        payload = {
            "version": _HIT_STATS_VERSION,
            "count": len(_state["hit_stats"]),
            "stats": _state["hit_stats"],
        }
        tmp = fp.with_suffix(fp.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        tmp.replace(fp)
        _state["hit_dirty"] = False
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[persona] 命中统计写入失败（不影响本次运行）: {exc}")


def _bump_hits(entries: List[Dict[str, Any]], tier: int, now: float,
               min_interval: float = 30.0) -> None:
    """命中即强化：内存计数 +1、刷新 last；去抖落盘。fire-and-forget，绝不抛。"""
    try:
        stats = _state["hit_stats"]
        for e in entries:
            eid = str(e.get("id", "")).strip()
            if not eid:
                continue
            k = _hit_key(tier, eid)
            s = stats.get(k)
            if not isinstance(s, dict):
                s = {"hits": 0, "last": 0}
                stats[k] = s
            s["hits"] = int(s.get("hits", 0)) + 1
            s["last"] = int(now)
        _state["hit_dirty"] = True
        if now - float(_state.get("hit_last_save", 0.0)) >= min_interval:
            _save_hit_stats()
            _state["hit_last_save"] = float(now)
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"[persona] 命中强化失败（忽略）: {exc}")


def _prune_hit_stats() -> None:
    """裁掉已不存在条目的孤儿统计（内容删除后避免 sidecar 无限膨胀）。"""
    valid = set()
    for tier, items in ((2, _state["tier2"]), (3, _state["tier3"])):
        for e in items:
            eid = str(e.get("id", "")).strip()
            if eid:
                valid.add(_hit_key(tier, eid))
    stats = _state["hit_stats"]
    for k in list(stats.keys()):
        if k not in valid:
            stats.pop(k, None)


def hit_stats_snapshot() -> Dict[str, Dict[str, Any]]:
    """只读快照，供 WebUI 概览/召回测试台展示冷热。"""
    return {k: dict(v) for k, v in _state["hit_stats"].items()}


def flush_hit_stats() -> None:
    """把去抖残留的命中统计立即落盘（插件 cleanup 时调用，避免丢最后几次强化）。"""
    if _state.get("hit_dirty"):
        _save_hit_stats()


# ---------------- A2/A3 生命周期：热度调制排序 ----------------
def _lc_field(o: Dict[str, Any], key: str, default: Any, cast, lo=None, hi=None):
    """取生命周期参数：**区分「未提供/None」与「显式 0」**。

    旧写法 `o.get(k, default) or default` 会把合法的 0 吞成默认值——
    floor=0（冷门记忆可彻底冷却）、protect_weight=0（不豁免任何条目）、
    reinforce_bonus=0（命中不回补、纯衰减）都是作者可能想要的真实取值。
    只有 key 缺失或值为 None 才回落默认；其余按 cast 转型并夹在 [lo, hi]。
    """
    v = o.get(key, None)
    if v is None:
        v = default
    try:
        v = cast(v)
    except (TypeError, ValueError):
        v = cast(default)
    if lo is not None:
        v = max(cast(lo), v)
    if hi is not None:
        v = min(cast(hi), v)
    return v


def lifecycle_settings(override: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """生命周期运行参数（默认**关闭**，作者显式开启才生效）。"""
    o = override or {}
    return {
        "enabled": bool(o.get("enabled", False)),
        "half_life_days": _lc_field(o, "half_life_days", 30, float, lo=1.0),
        "floor": _lc_field(o, "floor", 0.6, float, lo=0.0, hi=1.0),
        "protect_weight": _lc_field(o, "protect_weight", 4, int, lo=0),
        "reinforce_bonus": _lc_field(o, "reinforce_bonus", 0.1, float, lo=0.0),
    }


def heat_factor(stat: Optional[Dict[str, Any]], now: float, lc: Dict[str, Any]) -> float:
    """热度系数 ∈ [floor, 1]。久未命中→趋近 floor；常被命中→抬升。

    吸收 LivingMemory 的 access_factor 思想（memory_engine_crud.py:1280）：
    被访问的记忆衰减慢。never-hit 视为冷（base=0）→ 落在 floor。
    """
    if not lc.get("enabled"):
        return 1.0
    hits = int((stat or {}).get("hits", 0))
    last = int((stat or {}).get("last", 0))
    if last <= 0:
        base = 0.0
    else:
        days = max(0.0, (float(now) - last) / 86400.0)
        base = 0.5 ** (days / float(lc["half_life_days"]))
    reinforce = min(0.3, hits * float(lc["reinforce_bonus"]))
    heat = lc["floor"] + (1.0 - lc["floor"]) * min(1.0, base + reinforce)
    return max(lc["floor"], min(1.0, heat))


def effective_rank(entry: Dict[str, Any], tier: int, now: float, lc: Dict[str, Any]) -> float:
    """排序用有效权重 = 作者 weight × 热度。高 weight 条目豁免降权（保护核心设定）。"""
    w = float(entry.get("weight", 1) or 1)
    if not lc.get("enabled") or w >= float(lc["protect_weight"]):
        return w
    stat = _state["hit_stats"].get(_hit_key(tier, str(entry.get("id", ""))))
    return w * heat_factor(stat, now, lc)


def _order_by_heat(scored: List["ScoredEntry"], tier: int, now: float,
                   lc: Dict[str, Any]) -> List["ScoredEntry"]:
    """生命周期开启时按 (分数带, 热度) 重排；关闭则**原样返回**（保证行为不变）。

    分数带 round(score, 2)：吸收 ±0.005 的嵌入抖动，只在**近似平票**时用热度决胜，
    绝不覆盖明显更高的相关分——真实标定显示 top-1 cos 命中率 100%，
    相关序是对的，热度只该在噪声带内稳定选择。
    """
    if not lc.get("enabled") or not scored:
        return scored
    return sorted(
        scored,
        key=lambda it: (round(float(it[0]), 2), effective_rank(it[2], tier, now, lc)),
        reverse=True,
    )


def _embed_retry_after(resp, attempt: int) -> float:
    """计算退避时长：优先尊重服务端 Retry-After，否则指数退避（带抖动），封顶 20s。"""
    ra = 0.0
    try:
        v = resp.headers.get("Retry-After") if resp is not None else None
        if v:
            ra = float(str(v).strip())
    except (TypeError, ValueError, AttributeError):
        ra = 0.0
    exp = min(20.0, 0.8 * (2 ** attempt))
    jitter = random.uniform(0.0, 0.25 * exp) if exp else 0.0
    return max(ra, exp) + jitter


async def _embed_post(client, url: str, headers: Dict[str, str],
                      payload: Dict[str, Any], attempts: int = 5):
    """底层 POST：仅在 429/5xx/网络错误上退避重试。

    返回 (status, json_or_None)：
      - 成功：(200, dict)
      - 明确失败（如 400 超限 / 401 鉴权）：(status_code, None) —— 由调用方决定是否拆批
      - 重试耗尽：(last_status_or_None, None)
    把「该不该拆批」的判断留给上层，本函数只负责把可恢复的限流扛过去。
    """
    import httpx

    status: Optional[int] = None
    last: Optional[Exception] = None
    for attempt in range(attempts):
        try:
            res = await client.post(url, headers=headers, json=payload)
            status = res.status_code
            if res.status_code < 400:
                try:
                    return res.status_code, res.json()
                except Exception:  # noqa: BLE001  非 JSON 响应
                    return res.status_code, None
            if res.status_code == 429 or res.status_code >= 500:
                await asyncio.sleep(_embed_retry_after(res, attempt))
                continue
            return res.status_code, None  # 4xx（400/401/403…）不重试
        except Exception as e:  # noqa: BLE001  网络/超时
            last = e
            await asyncio.sleep(_embed_retry_after(None, attempt))
    if last is not None:
        logger.debug(f"[persona] custom 嵌入网络重试耗尽: {last}")
    return status, None


# ---- custom 嵌入健康追踪（v1.6.3）----
# custom 端点宕机 / 限流耗尽时，条目与 query 都拿不到向量 → 2/3 档退化成纯关键词检索。
# 这本身是**安全降级**：绝不混空间回落 NA——NA 查询向量去比 custom 空间的条目向量，
# 余弦是无意义的，比「没向量、退回关键词」更隐蔽地坏（阈值静默失效、召回全错）。
# 但降级必须**响亮告警**，否则作者以为向量检索还在工作。告警去抖，避免限流时刷屏。
_EMBED_FAIL_THRESHOLD = 3       # 连续失败多少次判定为降级
_EMBED_WARN_INTERVAL = 300.0    # 降级告警去抖：最多每 5 分钟刷一次日志


def _embed_mark_ok() -> None:
    """custom 嵌入成功：清零失败计数、解除降级标志（从降级恢复时记一条 info）。"""
    if _state.get("embed_degraded"):
        logger.info("[persona] custom 嵌入端点已恢复，2/3 档向量检索重新可用")
    _state["embed_fail_streak"] = 0
    _state["embed_degraded"] = False
    _state["embed_degraded_reason"] = ""


def _embed_mark_fail(reason: str) -> None:
    """custom 嵌入失败：累计连续失败，达阈值置降级标志并去抖告警。"""
    _state["embed_fail_streak"] = int(_state.get("embed_fail_streak", 0)) + 1
    if _state["embed_fail_streak"] < _EMBED_FAIL_THRESHOLD:
        return
    _state["embed_degraded"] = True
    _state["embed_degraded_reason"] = reason
    now = time.time()
    if now - float(_state.get("embed_last_warn", 0.0)) >= _EMBED_WARN_INTERVAL:
        _state["embed_last_warn"] = now
        logger.warning(
            f"[persona] custom 嵌入端点连续 {_state['embed_fail_streak']} 次失败（{reason}）："
            "2/3 档向量检索已降级为**纯关键词命中**，语义召回暂停。"
            "**不回落 NA 内置嵌入**——条目向量是 custom 空间嵌的，混用会得到无意义相似度。"
            "请检查 EMBED_BASE_URL / EMBED_API_KEY / EMBED_MODEL 与端点可用性。"
        )


def embed_health() -> Dict[str, Any]:
    """custom 嵌入后端健康快照（供 /api/overview 暴露、概览页告警）。

    na 后端不经这里追踪（NA 内置嵌入有自己的降级链），故 provider!=custom 时
    degraded 恒为 False。degraded=True 表示 2/3 档已退化为纯关键词命中。
    """
    return {
        "degraded": bool(_state.get("embed_degraded")),
        "reason": str(_state.get("embed_degraded_reason", "") or ""),
        "fail_streak": int(_state.get("embed_fail_streak", 0) or 0),
    }


async def _embed_one_custom(text: str, cfg: Dict[str, Any]) -> Optional[List[float]]:
    """custom 后端单条嵌入：httpx 直连，带退避重试。

    ⚠️ 查询侧与逐条兜底都走这里。一次 429 若直接返回 None，检索就空手而归，
    所以必须重试（尊重 Retry-After 的指数退避），不能像旧实现那样一次失败即放弃。
    成功 / 失败都会更新嵌入健康标志（v1.6.3），供降级告警与概览页展示。
    """
    import httpx

    base_url = str(cfg.get("base_url", "")).rstrip("/")
    api_key = str(cfg.get("api_key", "")).strip()
    model = str(cfg.get("model", ""))
    dim = int(cfg.get("dimensions", 0) or 0)
    timeout = int(cfg.get("timeout", 30) or 30)
    url = f"{base_url}/embeddings"
    headers = {"Content-Type": "application/json; charset=utf-8",
               "Authorization": f"Bearer {api_key}"}
    payload: Dict[str, Any] = {"model": model, "input": text.strip()}
    if dim:
        payload["dimensions"] = dim
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            status, data = await _embed_post(client, url, headers, payload)
            if data:
                items = data.get("data", []) or []
                if items and items[0].get("embedding"):
                    _embed_mark_ok()
                    return list(items[0]["embedding"])
            _embed_mark_fail(f"单条嵌入 status={status}")
            logger.debug(f"[persona] custom 单条嵌入失败 status={status}")
            return None
    except Exception as e:  # noqa: BLE001
        _embed_mark_fail(f"单条嵌入异常 {e}")
        logger.debug(f"[persona] custom 单条嵌入异常: {e}")
        return None


async def _embed_batch_custom(texts: List[str], cfg: Dict[str, Any]) -> List[Optional[List[float]]]:
    """custom 后端批量嵌入：httpx 发 list-input 请求，按 index 解析全部向量。

    gen_openai_embeddings 只取 data[0]、无法批量，故这里直接用 httpx。
    **自适应拆半**：部分端点对单次请求的条数 / 总 token 有上限，整批会 400。失败时把该 chunk 对半拆、递归降到单条为止，并把「更小的可用
    批量」记住用于后续 chunk——既兼容低上限端点，又比「整批失败→全部逐条」高效得多。
    **主动节流**：chunk 之间 sleep，避免瞬时打爆端点的 QPS 限流（429）。
    """
    import httpx

    out: List[Optional[List[float]]] = [None] * len(texts)
    if not texts:
        return out
    base_url = str(cfg.get("base_url", "")).rstrip("/")
    api_key = str(cfg.get("api_key", "")).strip()
    model = str(cfg.get("model", ""))
    dim = int(cfg.get("dimensions", 0) or 0)
    timeout = int(cfg.get("timeout", 30) or 30)
    url = f"{base_url}/embeddings"
    headers = {"Content-Type": "application/json; charset=utf-8",
               "Authorization": f"Bearer {api_key}"}
    state = {"bs": max(1, int(cfg.get("batch_size", 10) or 10))}
    throttle = 0.3  # chunk 间隔，压制 429

    async def _post_chunk(client, chunk_texts: List[str], offset: int, depth: int) -> None:
        payload: Dict[str, Any] = {"model": model, "input": chunk_texts}
        if dim:
            payload["dimensions"] = dim
        status, data = await _embed_post(client, url, headers, payload)
        if data:
            for item in (data.get("data", []) or []):
                try:
                    idx = offset + int(item.get("index", 0))
                except (TypeError, ValueError):
                    continue
                emb = item.get("embedding")
                if 0 <= idx < len(out) and emb:
                    out[idx] = list(emb)
            return
        # 400 且还能拆 → 批量条数/token 超限（部分端点上限较小），对半拆递归
        if status == 400 and len(chunk_texts) > 1 and depth < 8:
            state["bs"] = max(1, min(state["bs"], len(chunk_texts) // 2))
            mid = len(chunk_texts) // 2
            await _post_chunk(client, chunk_texts[:mid], offset, depth + 1)
            await asyncio.sleep(throttle)
            await _post_chunk(client, chunk_texts[mid:], offset + mid, depth + 1)
            return
        logger.warning(f"[persona] custom 嵌入失败 @{offset} (n={len(chunk_texts)}): status={status}")

    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            i = 0
            while i < len(texts):
                step = max(1, state["bs"])
                await _post_chunk(client, [str(t).strip() for t in texts[i:i + step]], i, 0)
                i += step
                await asyncio.sleep(throttle)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[persona] custom 批量嵌入客户端异常: {e}")
    # 健康判定（v1.6.3）：非空请求拿回至少一个向量=端点可用；一个都没有=降级。
    # 部分成功不算降级（限流拆批时常见），只要端点还在响应就清零失败计数。
    if texts:
        if any(out):
            _embed_mark_ok()
        else:
            _embed_mark_fail(f"批量嵌入 {len(texts)} 条全部失败")
    return out


async def _embed_entries(texts: List[str]) -> List[Optional[List[float]]]:
    """条目侧批量嵌入统一入口：按 provider 路由 na / custom。"""
    cfg = _embed_cfg()
    if _embed_is_custom(cfg):
        return await _embed_batch_custom(texts, cfg)
    from nekro_agent.services.memory.embedding_service import embedding_service

    return list(await embedding_service.embed_batch(texts, batch_size=3))


async def _embed_query(text: str) -> Optional[List[float]]:
    """查询侧单条嵌入统一入口：与条目侧**同源**，保证向量空间一致。

    v1.6.2：加短 TTL + LRU 内存缓存（按 _embed_signature() 隔离）。
    同一轮里 2/3 档、以及连续多轮里高度重叠的最近文本，都能命中缓存，
    免去重复的嵌入 API 调用（限流较严的端点尤其受益）。
    缓存失效条件：超过 TTL、被 LRU 淘汰、或嵌入签名变化（换模型/维度/端点）。
    """
    t = (text or "").strip()
    if not t:
        return None
    sig = _embed_signature()
    key = _query_cache_key(t, sig)
    cached = _query_cache_get(key)
    if cached is not None:
        return cached
    vec = await _embed_query_uncached(text, sig)
    if vec:
        _query_cache_put(key, vec)
    return vec


def _query_cache_key(text: str, sig: str) -> str:
    """query 缓存 key：签名 + 文本 sha1。签名入 key → 换模型自动落到不同命名空间。"""
    h = hashlib.sha1()
    h.update(sig.encode("utf-8"))
    h.update(b"\x00")
    h.update(text.encode("utf-8"))
    return h.hexdigest()


def _query_cache_get(key: str) -> Optional[List[float]]:
    """取缓存；过期或未命中返回 None。命中则刷新 LRU 顺序（dict 保序，移到末尾）。"""
    qc: Dict[str, Any] = _state.get("query_cache") or {}
    ent = qc.get(key)
    if not ent:
        return None
    ts, vec = ent
    if time.time() - float(ts) > _QUERY_CACHE_TTL:
        qc.pop(key, None)   # 惰性过期：读到才算，避免额外定时任务
        return None
    qc.pop(key, None)
    qc[key] = (ts, vec)     # 重新插入到末尾 = 最近使用
    return list(vec)


def _query_cache_put(key: str, vec: List[float]) -> None:
    """写缓存并执行 LRU 淘汰（超出上限丢最旧）。"""
    qc: Dict[str, Any] = _state.setdefault("query_cache", {})
    qc.pop(key, None)
    qc[key] = (time.time(), list(vec))
    while len(qc) > _QUERY_CACHE_MAX:
        oldest = next(iter(qc))   # dict 保序，第一个即最旧
        qc.pop(oldest, None)


def _query_cache_clear() -> None:
    """清空查询缓存（嵌入配置变更 / 测试隔离时调用）。"""
    _state["query_cache"] = {}


async def _embed_query_uncached(text: str, sig: str) -> Optional[List[float]]:
    """真正发嵌入请求（无缓存）。sig 已由调用方算好，避免重复计算签名。"""
    cfg = _embed_cfg()
    if _embed_is_custom(cfg):
        return await _embed_one_custom(text, cfg)
    from nekro_agent.services.memory.embedding_service import embed_text

    return await embed_text(text)



async def _embed_one(text: str) -> Optional[List[float]]:
    """单条嵌入，按降级链依次尝试各端点。

    ⚠️ 必须与查询侧（embedding_service.embed_text）参数完全一致（尤其 dimensions），
    否则条目向量与查询向量不在同一空间 → 相似度整体偏移、阈值失效。
    v1.6.0：custom 后端优先，走用户自配端点；否则用 NA 内置 MODEL_GROUPS 降级链。
    """
    cfg = _embed_cfg()
    if _embed_is_custom(cfg):
        return await _embed_one_custom(text, cfg)
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
            got = await _embed_entries(pending_texts)
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
    # A4：命中统计独立于内容 mtime——首次加载读入，随后裁剪孤儿（内容删除后残留的 key）。
    if not _state["hit_stats"] and not _state.get("hit_loaded"):
        _state["hit_stats"] = _load_hit_stats()
        _state["hit_loaded"] = True
    _prune_hit_stats()
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


# 带分候选：(相似度分, 命中来源, 条目)。source ∈ {"keyword","vector"}。
# A7：分数贯穿检索链路，供访问强化 / 召回测试台 / 阈值标定消费——
# 旧实现在 _match_tier2 里用 `[e for _s,e in vh]` 把分数丢了。
ScoredEntry = Tuple[float, str, Dict[str, Any]]


def apply_gating(
    scored: List[ScoredEntry],
    score: int,
    gating: bool = True,
    tier: int = 2,
) -> Tuple[List[ScoredEntry], List[Dict[str, Any]]]:
    """门控过滤（纯函数，**保序**）。返回 (可讲的带分候选, 被拦条目)。

    与 split_by_favor 判定完全一致（need <= score 放行），但作用于 ScoredEntry
    并保留分数与顺序。tier3（世界知识）永不门控；gating=False 时全放行。
    """
    if not gating or int(tier) == 3:
        return list(scored), []
    ok: List[ScoredEntry] = []
    blocked: List[Dict[str, Any]] = []
    for item in scored:
        e = item[2]
        try:
            need = int(e.get("min_favor", 0))
        except (TypeError, ValueError):
            need = 0
        if need <= score:
            ok.append(item)
        else:
            blocked.append(e)
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
        qv = await _embed_query(text)
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


async def _match_tier2_text(
    text: str,
    cfg: Optional[Dict[str, Any]] = None,
) -> Tuple[List[ScoredEntry], str]:
    """2 档检索**纯文本核心**（A7：与 ctx 解耦，可离线测、可被召回测试台驱动）。

    返回 (带分候选, 命中方式)。关键词优先（score=1.0），未命中再走向量（score=cos）。
    cfg: 2 档运行参数（scan_msgs / threshold），缺省用模块默认值。
    """
    s = tier2_settings(cfg)
    lc = lifecycle_settings((cfg or {}).get("lifecycle"))
    if not _state["tier2"] or not text:
        return [], ""
    now = time.time()
    kw = _keyword_hits(text)
    if kw:
        return _order_by_heat([(1.0, "keyword", e) for e in kw], 2, now, lc), "keyword"
    vh = await _vector_hits(text, threshold=s["threshold"])
    return _order_by_heat([(float(sc), "vector", e) for sc, e in vh], 2, now, lc), "vector"


async def _match_tier2(
    ctx: Any,
    cfg: Optional[Dict[str, Any]] = None,
) -> Tuple[List[ScoredEntry], str]:
    """2 档检索（ctx 包装）：取最近消息原文后交给 _match_tier2_text。"""
    s = tier2_settings(cfg)
    if not _state["tier2"]:
        return [], ""
    text = await _recent_text(ctx, limit=int(s["scan_msgs"]))
    return await _match_tier2_text(text, s)


async def _match_tier3_text(
    text: str,
    cfg: Optional[Dict[str, Any]] = None,
) -> Tuple[List[ScoredEntry], str]:
    """3 档检索**纯文本核心**（A7）。独立阈值，不受好感度门控。"""
    cfg = cfg or {}
    items = _state["tier3"]
    if not items or not text:
        return [], ""
    lc = lifecycle_settings(cfg.get("lifecycle"))
    now = time.time()
    kw = _keyword_hits(text, items)
    if kw:
        return _order_by_heat([(1.0, "keyword", e) for e in kw], 3, now, lc), "keyword"
    vh = await _vector_hits(
        text, items, _state["tier3_vecs"], cfg.get("threshold", TIER3_SIM_THRESHOLD),
    )
    return _order_by_heat([(float(sc), "vector", e) for sc, e in vh], 3, now, lc), "vector"


async def _match_tier3(ctx: Any, cfg: Dict[str, Any]) -> Tuple[List[ScoredEntry], str]:
    """3 档检索（ctx 包装）：关键词优先，未命中再走向量。"""
    items = _state["tier3"]
    if not items:
        return [], ""
    text = await _recent_text(ctx, limit=int(cfg.get("scan_msgs", TIER3_SCAN_MSGS)))
    return await _match_tier3_text(text, cfg)


def _render_tier2(
    entries: List[Dict[str, Any]],
    max_entries: int = MAX_TIER2_ENTRIES,
    max_chars: int = MAX_TIER2_CHARS,
    injected_out: Optional[List[Dict[str, Any]]] = None,
) -> str:
    """渲染 2 档往事。

    injected_out（可选）：回填**真正进入输出**的条目（跳过空内容与超预算的）。
    供 render_memory_block 精确统计命中强化——只给实际注入的条目涨热度，
    避免「因超预算没注入的长条」虚涨热度挤掉真能注入的（#6）。
    默认 None 时行为与返回值与旧版逐字一致。
    """
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
        if injected_out is not None:
            injected_out.append(e)
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
    injected_out: Optional[List[Dict[str, Any]]] = None,
) -> str:
    """渲染 3 档世界知识。

    与 2 档措辞严格区分：这里是**客观见闻**，不是她的回忆。
    超预算口径与 2 档一致（v1.6.3）：跳过过长条目而非 break——旧实现一条过长
    世界知识会永久阻断其后所有条目，导致「长条在后时后续全丢」。
    injected_out（可选）：回填真正进入输出的条目，供命中强化精确统计（#6）。
    """
    lines: List[str] = []
    used = 0
    dropped = 0
    for e in entries[: max(1, int(max_entries))]:
        content = str(e.get("content", "")).strip()
        if not content:
            continue
        fam = str(e.get("familiarity", "")).strip()
        block = f"· 【{fam}】{content}" if fam else f"· {content}"
        if used + len(block) > max(1, int(max_chars)):
            dropped += 1
            continue
        lines.append(block)
        used += len(block)
        if injected_out is not None:
            injected_out.append(e)
    if dropped:
        logger.warning(
            f"[persona] 3 档超出单轮预算 {max_chars} 字符，"
            f"本轮跳过 {dropped} 条过长世界知识（请缩短 content 或提高 TIER3_MAX_CHARS）",
        )
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
    t3s = tier3_settings(tier3_cfg)

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

    # ---- 单次取最近文本（v1.6.2）----
    # 旧实现：_match_tier2 / _match_tier3 各自 _recent_text 读库一次、各自 _embed_query
    # 嵌一次，同一轮几乎相同的文本被读库 2 次、送嵌入 API 2 次（限流端点净浪费）。
    # 现在：只为**实际需要检索**的档位取文本；2/3 档 scan_msgs 相同则复用同一次读库，
    # query 向量再经 _embed_query 的 LRU 缓存天然去重 → 多数轮次嵌入调用从 2 次降到 1 次。
    # 行为等价：_match_tier2_text / _match_tier3_text 自带「空档 or 空文本 → ([], "")」守卫，
    # 与 ctx 包装的早返回一致；不检索的档位文本留空，_text 核心照样返回空。
    need2 = bool(_state["tier2"])
    need3 = bool(t3s["enabled"]) and bool(_state["tier3"])
    scan2 = int(t2s["scan_msgs"])
    scan3 = int(t3s["scan_msgs"])
    text2 = text3 = ""
    if need2 and need3 and scan2 == scan3:
        text2 = text3 = await _recent_text(ctx, limit=scan2)
    else:
        if need2:
            text2 = await _recent_text(ctx, limit=scan2)
        if need3:
            text3 = await _recent_text(ctx, limit=scan3)

    scored2, src = await _match_tier2_text(text2, t2s)
    if scored2:
        ok2, t2_blk = apply_gating(scored2, score, gating, tier=2)
        blocked.extend(t2_blk)
        t2_ok = [e for _s, _src, e in ok2]
        injected2: List[Dict[str, Any]] = []
        t2 = _render_tier2(t2_ok, t2s["max_entries"], t2s["max_chars"], injected2)
        if t2:
            # #6：names / last_hit / 命中强化都只用**实际注入**的条目（injected2），
            # 不含因超预算被渲染器跳过的——否则长条会虚涨热度、挤掉真能注入的条目，
            # 且「最近命中」会谎报注入过其实没进输出的条目。
            names = ",".join(str(e.get("id", "?")) for e in injected2)
            _state["last_hit"] = f"{src}:{names}"
            logger.info(f"[persona] 2 档命中({src}): {names}")
            parts.append(t2)
            # A4：仅在生命周期开启时强化命中条目（关闭时零副作用、不写 sidecar）
            if lifecycle_settings(t2s.get("lifecycle"))["enabled"]:
                _bump_hits(injected2, 2, time.time())

    # ---- 3 档：世界知识（扩展）----
    # 不参与好感度门控：无论和对方多熟，世界常识都可以聊。
    if t3s["enabled"]:
        scored3, src3 = await _match_tier3_text(text3, t3s)
        if scored3:
            ok3, _blk3 = apply_gating(scored3, score, gating, tier=3)
            hits3 = [e for _s, _src, e in ok3]
            injected3: List[Dict[str, Any]] = []
            t3 = _render_tier3(hits3, t3s["max_entries"], t3s["max_chars"], injected3)
            if t3:
                names3 = ",".join(str(e.get("id", "?")) for e in injected3)
                _state["last_hit_tier3"] = f"{src3}:{names3}"
                logger.info(f"[persona] 3 档命中({src3}): {names3}")
                parts.append(t3)
                if lifecycle_settings(t3s.get("lifecycle"))["enabled"]:
                    _bump_hits(injected3, 3, time.time())


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
