"""
# 三档背景记忆 (nekro_persona)

给角色一层**分层注入的背景记忆**：她是谁、经历过什么、这个世界是什么样的。
按「什么时候该想起来」分成三档，各有硬预算，避免把提示词塞满。

## 一、三档分工

- **1 档 `backgrounds/tier1/`**：身份级，每轮直接注入（内容静态 → 提示词前缀缓存友好）
- **2 档 `backgrounds/tier2/`**：细节级，**提到才检索注入**（triggers 关键词优先 + 向量兜底，硬预算）
- **3 档 `backgrounds/tier3/`**：世界知识（角色之外的人 / 地点 / 势力 / 术语），提到才检索。
  **不受好感度门控** —— 常识不该因为不熟就不说；与 1/2 档「她亲历的事」严格分开措辞。

## 二、好感度的角色：解锁标尺

好感度在本插件里主要作为**记忆的解锁条件**：每条 1/2 档记忆可设 `min_favor`，
关系没到就进不了正文，改为列入「暂时不想细说的事」（措辞由 `favor.json` 的 `avoid` 定义）。
分数落在哪个档位，同时决定该档位的互动指引（`stage_guides`）。

本插件为独立实现：三档记忆、`min_favor` 解锁门控、好感度衰减与回升、
防刷分约束、档案禁写词、排行榜卡片、备忘录与 WebUI 均在本项目内实现。

## 三、设计取舍（实测教训）

- 2/3 档检索的 query 用【最近消息原文】，不用 LLM 生成（LLM query 是文档腔，实测 0.32 vs 0.49）
- 2 档偏精确：1 档已兜住身份，漏召回不致命，误召回反而会带偏话题
- 3 档不受门控：世界常识谁问都能答，但用 `familiarity` 控制语气分寸，角色不该「全知」
- 1 档静态、2/3 档多数轮次为空 → 系统提示词前缀缓存友好
- 嵌入失败要能降级（嵌入 API 会突发 429），且降级函数必须与查询侧参数完全一致

## 四、数据位置

`{插件数据目录}/backgrounds/` 下的 `tier1|tier2|tier3/*.json` 与 `favor.json`（门控配置）。
包内自带一份同结构示例，仅在数据目录不存在时作为种子复制，之后升级插件不会覆盖数据。
"""

from __future__ import annotations

import asyncio
import json
import math
import re
import time
import uuid
from dataclasses import dataclass, field
from typing import Annotated, Dict, List

from pydantic import BaseModel, Field

from nekro_agent.api import i18n, schemas
from nekro_agent.api.plugin import (
    ConfigBase,
    ExtraField,
    NekroPlugin,
    SandboxMethodType,
)
from nekro_agent.api.signal import MsgSignal
from nekro_agent.schemas.chat_message import ChatMessage
from nekro_agent.services.command.base import CommandPermission
from nekro_agent.services.command.ctl import CmdCtl
from nekro_agent.services.command.schemas import (
    Arg,
    CommandExecutionContext,
    CommandOutputSegment,
    CommandOutputSegmentType,
    CommandResponse,
)

plugin = NekroPlugin(
    name="人格记忆",
    module_name="nekro_persona",
    description="三档背景记忆（常驻身份 / 按需往事 / 世界知识）+ 好感度解锁门控",
    version="1.3.0",
    author="NTidal",
    url="https://github.com/NTidal/nekro_persona",
    i18n_name=i18n.i18n_text(
        zh_CN="人格记忆",
        en_US="Persona Memory",
    ),
    i18n_description=i18n.i18n_text(
        zh_CN="三档背景记忆（常驻身份 / 按需往事 / 世界知识）+ 好感度解锁门控",
        en_US="Tiered background memory (always-on identity / on-demand past / world knowledge) with favorability gating",
    ),
    allow_sleep=False,
    sleep_brief="用于维护角色三档背景记忆与解锁门控，需常驻。",
    webui_path="/",  # 扩展：WebUI 走插件路由（见文件末尾 mount_router）
)


def _fav_field(
    default: object = None,
    title: str = "",
    desc: str = "",
    *,
    en_title: str = "",
    en_desc: str = "",
    factory: object = None,
    extra: Dict[str, object] | None = None,
    **bounds: object,
):
    """构造一个带中英 i18n 的配置字段。

    本插件的配置项有近四十个，每项都要 title / description / i18n 三份文案。
    逐字段手写样板会让配置类膨胀到三百多行且极易写漏，故统一经由这里构造：
    `title` / `desc` 同时作为中文文案，英文缺省时回落到中文。
    """
    schema = ExtraField(
        **(extra or {}),
        i18n_title=i18n.i18n_text(zh_CN=title, en_US=en_title or title),
        i18n_description=i18n.i18n_text(zh_CN=desc, en_US=en_desc or desc),
    ).model_dump()
    common: Dict[str, object] = {"title": title, "description": desc,
                                 "json_schema_extra": schema}
    common.update(bounds)
    if factory is not None:
        common["default_factory"] = factory
    else:
        common["default"] = default
    return Field(**common)


@plugin.mount_config()
class FavorabilityConfig(ConfigBase):
    """插件配置。分五组：三档记忆 / 解锁门控与关系 / 备忘录 / 排行榜 / WebUI"""

    # ==================== WebUI ====================
    WEBUI_ACCESS_KEY: str = _fav_field(
        "", "WebUI 访问密钥",
        "留空不校验；填写后访问本插件 WebUI 需要提供该密钥（前端会弹窗询问）",
        en_title="WebUI Access Key",
        en_desc="Leave empty to skip auth; otherwise the WebUI asks for this key",
    )

    # ==================== 三档记忆 ====================
    TIER3_ENABLED: bool = _fav_field(
        True, "启用 3 档（世界知识）",
        "关闭后完全不检索、不注入 3 档。3 档不受好感度门控。",
        en_title="Enable Tier 3 (World Knowledge)",
        en_desc="When off, tier 3 is neither searched nor injected; tier 3 ignores favorability gating",
    )
    TIER3_MAX_ENTRIES: int = _fav_field(
        3, "3 档单轮条数上限", "单轮最多注入几条世界知识",
        en_title="Tier 3 Entry Limit", en_desc="Max world-knowledge entries injected per turn",
    )
    TIER3_MAX_CHARS: int = _fav_field(
        700, "3 档单轮字符预算", "单轮注入的世界知识字符数上限",
        en_title="Tier 3 Char Budget", en_desc="Char budget for tier 3 injection per turn",
    )
    TIER3_SIM_THRESHOLD: float = _fav_field(
        0.35, "3 档向量阈值",
        "世界知识库的检索阈值。条目变多后噪声上限会上升，需要重新标定。",
        en_title="Tier 3 Similarity Threshold",
        en_desc="Retrieval threshold for world knowledge; recalibrate as the library grows",
    )
    TIER3_SCAN_MSGS: int = _fav_field(
        6, "3 档扫描消息条数", "判断「是否提到」时回看多少条最近消息",
        en_title="Tier 3 Scan Depth", en_desc="Recent messages scanned to decide whether a topic came up",
    )

    # ==================== 解锁门控与关系 ====================
    DEFAULT_SCORE: int = _fav_field(
        0, "新档案初始分", "首次出现的新用户，其关系档案的起始评分",
        en_title="Default Score", en_desc="Starting score for a newly created user archive",
    )
    MAX_ABS_SCORE: int = _fav_field(
        100, "评分绝对值上限", "所有评分被夹在 ±该值之内",
        en_title="Max Absolute Score", en_desc="Scores are clamped within ± this value",
    )
    MAX_EVENT_HISTORY: int = _fav_field(
        8, "档案变动记录保留数", "每份档案最多保留几条关系变动记录",
        en_title="Event History Limit", en_desc="How many change events each archive keeps",
    )
    PROMPT_EVENT_LIMIT: int = _fav_field(
        3, "注入时展示的变动条数", "注入关系卡时附带展示的最近变动条数",
        en_title="Prompt Event Limit", en_desc="Recent events shown in the injected relationship card",
    )
    GROUP_OVERVIEW_LIMIT: int = _fav_field(
        3, "回退总览展示数", "没有明确触发用户时，最多摘要展示几份最近活跃的档案",
        en_title="Fallback Overview Limit", en_desc="Profiles summarized when no triggering user is known",
    )
    MAX_TAGS: int = _fav_field(
        6, "档案标签上限", "每份档案最多保留几个关系标签",
        en_title="Tag Limit", en_desc="Max relationship tags kept per archive",
    )

    # ---- 冷却：长期不互动自动降温 ----
    DECAY_ENABLED: bool = _fav_field(
        True, "启用好感度衰减", "长时间不互动时自动降低关系分",
        en_title="Enable Decay", en_desc="Slowly lower scores when a user stops interacting",
    )
    DECAY_INTERVAL_HOURS: int = _fav_field(
        24, "衰减间隔（小时）", "超过这个时长没有互动，就结算一次衰减",
        en_title="Decay Interval (hours)", en_desc="Idle time before a decay settlement runs",
    )
    DECAY_PERCENT: int = _fav_field(
        3, "每次衰减比例（%）", "按当前分数扣除的比例，向上取整（默认每天约 3%）",
        en_title="Decay Percent", en_desc="Percent of the current score removed per settlement (rounded up)",
    )
    DECAY_KEEP_TIER: bool = _fav_field(
        True, "衰减不立刻降档（有期限）",
        "未互动时长不超过「档位保底时长」时，分数不会跌破当前档位的下界；超时后逐级解锁",
        en_title="Keep Tier Within Grace", en_desc="Within the grace period the score will not drop below the current tier floor",
    )
    DECAY_TIER_GRACE_HOURS: int = _fav_field(
        168, "档位保底时长（小时）",
        "未互动超过该时长后，允许的档位下界向下解锁一级；每再过同样的时长再解锁一级。"
        "填 0 表示不保底（可一路衰减到 0）。注意：该值应大于「分数衰减到档位下界所需时间」，否则保底没有意义。",
        en_title="Tier Grace Hours",
        en_desc="After this idle period the tier floor unlocks one level, then one more per period; 0 disables the floor",
        ge=0, le=8760,
    )

    # ---- 回升：负分缓慢回到 0 ----
    RECOVER_ENABLED: bool = _fav_field(
        True, "启用好感度回升",
        "长期处于负分的用户会随时间缓慢回升，最多回到 0，不会自动变成正数",
        en_title="Enable Recovery", en_desc="Negative scores creep back toward 0 over time, never above it",
    )
    RECOVER_INTERVAL_HOURS: int = _fav_field(
        12, "回升间隔（小时）", "超过这个时长没有互动，就结算一次回升",
        en_title="Recovery Interval (hours)", en_desc="Idle time before a recovery settlement runs",
    )
    RECOVER_PERCENT: int = _fav_field(
        5, "每次回升比例（%）", "按负分绝对值计算并向上取整，每次至少 +1，封顶 0",
        en_title="Recovery Percent", en_desc="Percent of the negative score recovered per settlement (min +1, capped at 0)",
    )

    # ---- 调整约束：防止分数被刷 ----
    FAVOR_SCALE_MODE: int = _fav_field(
        2, "好感度松紧档位",
        "1=宽松 / 2=正常 / 3=严格。这一档会一并决定：单次上限、最小间隔、"
        "每日加减上限、边际递减曲线、理由校验强度——即下面那几个 FAVOR_ 字段"
        "（已由本档位接管，单独改它们不再生效）。",
    )
    FAVOR_MAX_SINGLE_DELTA: int = _fav_field(
        5, "【已被 FAVOR_SCALE_MODE 接管】单次调整上限", "单次 delta 的绝对值上限，超出会被钳制（挡住 +100 这类失控）",
        en_title="Max Single Delta", en_desc="Absolute cap for one adjustment; larger values are clamped",
    )
    FAVOR_MIN_INTERVAL_MINUTES: int = _fav_field(
        120, "【已被 FAVOR_SCALE_MODE 接管】同一用户最小调整间隔（分钟）", "防止对同一个人一天内反复加减",
        en_title="Min Interval (minutes)", en_desc="Minimum gap between two adjustments for the same user",
    )
    FAVOR_MAX_DAILY_GAIN: int = _fav_field(
        5, "【已被 FAVOR_SCALE_MODE 接管】每日净加分上限", "同一个用户一天之内最多净加多少分",
        en_title="Daily Gain Cap", en_desc="Max net score gain per user per day",
    )
    FAVOR_MAX_DAILY_LOSS: int = _fav_field(
        10, "【已被 FAVOR_SCALE_MODE 接管】每日净扣分上限", "同一个用户一天之内最多净扣多少分（比加分放宽）",
        en_title="Daily Loss Cap", en_desc="Max net score loss per user per day (looser than gain)",
    )
    FAVOR_MARGINAL_DECAY: bool = _fav_field(
        True, "【已被 FAVOR_SCALE_MODE 接管】加分边际递减", "分数越高，同样的行为加分越少（85 分以上几乎加不动），让高档位保持稀缺",
        en_title="Marginal Decay", en_desc="Higher scores gain less from the same behavior, keeping top tiers rare",
    )
    FAVOR_REQUIRE_CONCRETE_REASON: bool = _fav_field(
        True, "【已被 FAVOR_SCALE_MODE 接管】理由必须具体", "理由只有主观氛围词、没有可验证的具体行为时，本次不加分",
        en_title="Require Concrete Reason", en_desc="Vague reasons without a concrete behavior do not earn points",
    )
    FAVOR_FORBIDDEN_PROFILE_WORDS: List[str] = _fav_field(
        None, "档案禁写词（额外）",
        "这些词不会写进用户档案：正文命中会替换成中性称谓，标签命中会被丢弃。"
        "用于防止模型写入独占性关系（例如「最重要的人」）。留空则只用内置默认词；"
        "人设若有专属称呼、且任何群友都不该被这样称呼，在此追加。",
        en_title="Extra Forbidden Profile Words",
        en_desc="Hits in text are replaced with a neutral term and tagged hits are dropped; empty uses built-in defaults only",
        factory=list,
        extra={"sub_item_name": "禁写词"},
    )

    # ==================== 备忘录 ====================
    MEMO_ENABLED: bool = _fav_field(
        True, "启用频道备忘录", "让角色能在频道里自己记长期备忘（可设过期时间与好感度门槛）",
        en_title="Enable Memos", en_desc="Let the character keep long-term memos per channel (with expiry and gating)",
    )
    MEMO_GUIDE_ALWAYS: bool = _fav_field(
        True, "常驻备忘录引导",
        "即使当前频道一条备忘都没有，也注入一段「什么时候该记」的引导。"
        "不开的话模型只在工具列表里看到 save_memo 这个名字，几乎不会主动用（实测 4763 次代码执行里只用过 1 次）。",
        en_title="Always Inject Memo Guide",
        en_desc="Inject the when-to-write guidance even when the channel has no memo yet",
    )
    MEMO_MAX_ITEMS: int = _fav_field(
        80, "单频道备忘上限", "超出后新的写入会被拒绝，需要先清理旧备忘",
        en_title="Memo Limit", en_desc="Further writes are rejected once the channel reaches this many memos",
    )
    MEMO_MAX_CONTENT_CHARS: int = _fav_field(
        400, "单条备忘内容上限", "写入时强制截断；宁可写短，也不要长到读取时被迫截断",
        en_title="Memo Content Limit", en_desc="Enforced on write; better short than truncated on read",
    )
    MEMO_INJECT_RECENT: int = _fav_field(
        2, "无条件注入的最近条数", "最近更新的几条备忘每轮都会注入",
        en_title="Always-injected Recent Memos", en_desc="Most recently updated memos injected every turn",
    )
    MEMO_INJECT_MATCHED: int = _fav_field(
        3, "话题命中时额外注入条数", "被当前话题命中时，额外注入几条",
        en_title="Topic-matched Memos", en_desc="Extra memos injected when the current topic matches",
    )
    MEMO_INJECT_CHARS: int = _fav_field(
        900, "备忘录单轮字符预算", "一轮里注入的备忘总字符上限",
        en_title="Memo Char Budget", en_desc="Total char budget for memo injection per turn",
    )
    MEMO_DEDUP_MIN_KEY_LEN: int = _fav_field(
        2, "包含式去重的最短标题长度",
        "标题归一化后，短标题被长标题完整包含即视为同一条并覆盖。"
        "不用相似度阈值：实测「小明生日」与「小红生日」相似度 0.75，会被误合并。",
        en_title="Dedup Min Key Length",
        en_desc="Containment-based dedup: a shorter title fully contained in a longer one is treated as the same memo",
    )

    # ==================== 排行榜卡片 ====================
    RANK_HIDE_EMPTY: bool = _fav_field(
        True, "排行榜隐藏空档案",
        "开启后不渲染「分数为 0 且没有任何记录」的成员（对方只是说过话，模型从未形成关系判断）；"
        "有标签、印象、变动记录或分数非 0 的成员始终显示。",
        en_title="Hide Empty Archives", en_desc="Skip members with zero score and no records at all",
    )
    RANK_CARD_QUALITY: int = _fav_field(
        1, "排行榜卡片渲染精度",
        "1 = 低（1.5×，文件最小）｜2 = 中（2.0×，Retina 级）｜3 = 高（3.0×，最清晰）",
        en_title="Rank Card Quality", en_desc="1 = low (1.5x) / 2 = medium (2.0x) / 3 = high (3.0x)",
        ge=1, le=3,
    )
    RANK_CARD_SCALE: float = _fav_field(
        0.0, "排行榜卡片渲染倍率（高级覆盖）",
        "自定义超采样倍率。填 0 用上面的精度档位；填大于 0 的值则忽略档位、直接用它",
        en_title="Rank Card Scale Override", en_desc="0 = use the quality preset; >0 overrides it directly",
        ge=0.0, le=6.0,
    )
    RANK_CARD_FONT: str = _fav_field(
        "", "排行榜卡片字体路径",
        "渲染排行榜图片使用的中文字体文件路径；留空则自动探测系统字体",
        en_title="Rank Card Font Path", en_desc="Chinese font file for the rank card; empty auto-detects",
    )


# 模块级单例：配置实例与插件存储句柄
config = plugin.get_config(FavorabilityConfig)

store = plugin.store
STATE_KEY = "favorability_state"

# 排行榜卡片最多展示行数
_RANK_LIMIT = 15   # ⚠️ 已废弃：多列布局后不再截断，保留仅为兼容
_RANK_TARGET_PER_COL = 15   # 每列目标条数（card.py 内同名常量，此处仅供参考）

# 无前缀触发「查看好感度」的指令词表
_RANK_TRIGGERS = ("查看好感度", "好感度排行", "好感榜")

from . import card  # noqa: E402  # 依赖上方 plugin/config 完成初始化后再导入渲染模块
from . import memory as persona_memory  # noqa: E402  # 阶梯记忆与好感度门控

# 背景记忆数据目录：落到插件数据目录（首次运行自包内 backgrounds/ 复制种子），
# 避免升级/重装插件时整体替换包目录而覆盖用户改过的记忆。
persona_memory.configure_bg_dir(plugin.get_plugin_data_dir() / "backgrounds")


def _ts_now() -> int:
    """当前 Unix 时间戳（秒）。"""
    return int(time.time())   # 秒级，全插件统一口径


def _one_line(value: object, limit: int) -> str:
    """压平成单行并截断。档案里的自由文本一律走这里，避免换行破坏提示词排版。"""
    return " ".join(str(value or "").split())[:limit]


def _tag_list(values: object, cap: int | None = None) -> List[str]:
    """清洗关系标签：单行化 → 去空 → 去重（保留首次出现顺序）→ 按上限截断。"""
    limit = int(cap if cap is not None else config.MAX_TAGS)
    picked: List[str] = []
    for raw in values or []:  # type: ignore[union-attr]
        item = _one_line(raw, 24)
        if item and item not in picked:
            picked.append(item)
        if len(picked) >= limit:
            break
    return picked


def _score_cap() -> int:
    """评分绝对值的上限，下限锁 10（配置填 0 也不会把所有分数夹平）。"""
    return max(10, abs(int(config.MAX_ABS_SCORE)))


def _bound_score(score: int) -> int:
    """把评分夹进 ±上限。"""
    cap = _score_cap()
    value = int(score)
    if value > cap:
        return cap
    if value < -cap:
        return -cap
    return value


def _delta_text(delta: int) -> str:
    """带符号的分值文本（+3 / -2）。"""
    value = int(delta)
    return f"+{value}" if value >= 0 else str(value)


def _ago_text(timestamp: int) -> str:
    """「多久以前」的紧凑写法，用于注入给模型的关系变动列表。"""
    span = max(0, _ts_now() - int(timestamp or 0))
    if span < 60:
        return f"{span}s ago"
    if span < 3600:
        return f"{span // 60}m ago"
    if span < 86400:
        return f"{span // 3600}h ago"
    return f"{span // 86400}d ago"


def _stage_of(score: int) -> tuple[str, str]:
    """分数 → (档位名, 该档位的默认语气)。

    边界（与 favor.json 的 stages 一致）：
        ≥85 特别亲密 ｜ ≥60 偏爱 ｜ ≥20 亲近 ｜ >-20 中立 ｜ >-60 保留 ｜ 其余 排斥
    注意 -20 属于「保留」、-60 属于「排斥」——判定写成降序级联，
    与配置里的 max 语义（`max=-20` 表示 ≤-20 是保留档）对齐。

    这是**内置兜底**：注入用的档位指引来自 favor.json（WebUI 可改），
    此处只服务于命令回执、排行榜等不读配置的场景。
    """
    value = int(score)
    if value >= 85:
        return "特别亲密", "可使用显著亲密与偏爱语气，但仍需遵守角色边界。"
    if value >= 60:
        return "偏爱", "可明显更热情，主动照顾对方体验并记住其偏好。"
    if value >= 20:
        return "亲近", "可以更自然、更积极地回应，适度体现熟悉感。"
    if value > -20:
        return "中立", "正常友好互动，不主动施加亲密语气。"
    if value > -60:
        return "保留", "维持礼貌但克制，先观察，不要过度投入。"
    return "排斥", "保持距离，谨慎回应，必要时明确边界。"


# ================= 好感度衰减 / 防黑（扩展）=================

# 「索要式加分」识别（扩展）
# 用「好感词 + 操纵动词」邻近匹配，比枚举关键词覆盖更全：
#   好感/亲密度/关系值 …(0~4字)… 加/涨/拉/提/升/改/设/刷/满/调
#   或反序。命中则拒绝正向调整（只拦加分，不拦扣分）。
_FAVOR_WORD = r"(?:好感|亲密度|关系值)[度分]?"
_FAVOR_VERB = r"(?:加|涨|拉|提|升|改|设|刷|满|调)"
_FAVOR_MANIPULATION_RE = re.compile(
    _FAVOR_WORD + r".{0,4}?" + _FAVOR_VERB + r"|" + _FAVOR_VERB + r".{0,4}?" + _FAVOR_WORD,
)


# ===== 备忘录辅助（扩展）=====
_MEMO_PUNCT_RE = None


def _memo_title_key(title: str) -> str:
    """标题归一化：去空白与标点、转小写，用于相似度比较。"""
    global _MEMO_PUNCT_RE
    if _MEMO_PUNCT_RE is None:
        import re as _re

        _MEMO_PUNCT_RE = _re.compile(r"[\s，。、；：！？,.;:!?「」『』（）()\[\]【】\"'<>·—~～]+")
    return _MEMO_PUNCT_RE.sub("", str(title or "")).lower()


def _memo_similarity(a: str, b: str) -> float:
    ka, kb = _memo_title_key(a), _memo_title_key(b)
    if not ka or not kb:
        return 0.0
    if ka == kb:
        return 1.0
    from difflib import SequenceMatcher

    return SequenceMatcher(None, ka, kb).ratio()


def _find_memo(state: "ChannelArchive", title: str):
    """按标题查找。返回 (memo, index) 或 (None, -1)。

    匹配策略（按优先级）：
      ① **精确**：归一化后完全相同
      ② **包含**：短标题被长标题完整包含（「群规」⊂「群规说明」）
      ③ **近同**：相似度 ≥ 0.92（错别字、语序）

    ⚠️ 实测教训：早期版本用 SequenceMatcher(0.72) 做模糊合并，把
       「小明生日」与「小红生日」（相似度 0.750）判成同一条 → 会覆盖掉一条真实备忘。
       中文标题的差异常集中在少数几个字上，字符相似度对此不敏感，
       所以**不能用中低阈值做模糊合并**。
    """
    key = _memo_title_key(title)
    if not key:
        return None, -1
    # ① 精确
    for i, m in enumerate(state.memos):
        if _memo_title_key(m.title) == key:
            return m, i
    # ② 包含
    min_len = max(2, int(getattr(config, "MEMO_DEDUP_MIN_KEY_LEN", 2) or 2))
    for i, m in enumerate(state.memos):
        mk = _memo_title_key(m.title)
        shorter = mk if len(mk) <= len(key) else key
        if len(shorter) >= min_len and (mk in key or key in mk):
            return m, i
    # ③ 近同（高阈值兜底）
    for i, m in enumerate(state.memos):
        if _memo_similarity(title, m.title) >= 0.92:
            return m, i
    return None, -1


def _memo_cfg() -> dict:
    """备忘录运行参数（来自插件配置）。"""
    return {
        "enabled": bool(getattr(config, "MEMO_ENABLED", True)),
        "max_items": int(getattr(config, "MEMO_MAX_ITEMS", 80) or 80),
        "max_content": int(getattr(config, "MEMO_MAX_CONTENT_CHARS", 400) or 400),
        "inject_recent": int(getattr(config, "MEMO_INJECT_RECENT", 2) or 0),
        "inject_matched": int(getattr(config, "MEMO_INJECT_MATCHED", 3) or 0),
        "inject_chars": int(getattr(config, "MEMO_INJECT_CHARS", 900) or 900),
        "dedup_min_len": max(2, int(getattr(config, "MEMO_DEDUP_MIN_KEY_LEN", 2) or 2)),
    }


def _memo_match_terms(memo: ChannelMemo) -> List[str]:
    """话题匹配词：显式 tags 优先，标题整体作兜底。"""
    terms = [str(x).strip().lower() for x in (memo.tags or []) if str(x).strip()]
    tk = _memo_title_key(memo.title)
    if len(tk) >= 2:
        terms.append(tk)
    return terms


def _log_memo_injection(state: "ChannelArchive") -> None:
    live = [m for m in state.memos if not m.is_expired()]
    plugin.logger.info(f"[persona] 备忘录注入：{len(live)} 条可用 / 共 {len(state.memos)} 条")


# 扩展：备忘录引导 —— 常驻注入，明确"什么时候该记"。
# 没有它时，模型只知道有个 save_memo 工具，不知道该拿它做什么。
_MEMO_GUIDE = (
    "#你的小本本\n"
    "你有一本自己的小本本（`save_memo`）。**约定、承诺、称呼偏好、"
    "以及以后还用得上的稳定事实**，想到就顺手记下来——别指望事后还想得起来。\n"
    "使用规则：\n"
    "1. 需要时自然用出来即可，**绝不要把备忘原文当消息发给用户**。\n"
    "2. 没提到的那些就是这轮用不上，不要硬提。"
)


def _render_memo_block(state: "ChannelArchive", score: int, recent_text: str) -> str:
    """渲染备忘录注入块。

    设计要点（note 插件的教训）：
    · **不注入标题索引** —— 只注入全文。避免模型"看得见标题却读不到内容"，
      进而拿发送消息当调试输出把内部笔记摊给全群。
    · **写时限制长度**，而不是读时截断 —— 从根上消除"要读全文"的需求。
    · 过期与好感度门控在此过滤（note 没有门控能力）。
    """
    cfg2 = _memo_cfg()
    if not cfg2["enabled"]:
        return ""

    # 扩展：引导常驻 —— 否则"没有备忘的频道"永远看不到使用说明
    guide_always = bool(getattr(config, "MEMO_GUIDE_ALWAYS", True))

    now = _ts_now()
    live = [m for m in state.memos if not m.is_expired(now) and int(m.min_favor or 0) <= score]
    if not live:
        return _MEMO_GUIDE if guide_always else ""
    live.sort(key=lambda m: -int(m.updated_at or 0))

    picked: List[ChannelMemo] = []
    seen = set()
    for m in live[: max(0, cfg2["inject_recent"])]:
        picked.append(m)
        seen.add(m.key())

    hit = 0
    if recent_text and cfg2["inject_matched"] > 0:
        low = recent_text.lower()
        for m in live:
            if m.key() in seen:
                continue
            if any(term and term in low for term in _memo_match_terms(m)):
                picked.append(m)
                seen.add(m.key())
                hit += 1
                if hit >= cfg2["inject_matched"]:
                    break
    if not picked:
        return _MEMO_GUIDE if guide_always else ""

    lines: List[str] = []
    used = 0
    for m in picked:
        blk = f"· 【{m.title}】{m.content}"
        if used + len(blk) > cfg2["inject_chars"]:
            break
        lines.append(blk)
        used += len(blk)
    if not lines:
        return _MEMO_GUIDE if guide_always else ""
    return _MEMO_GUIDE + "\n" + "\n".join(lines)


def _tier3_cfg() -> dict:
    """从插件配置收集 3 档运行参数（扩展）。"""
    return {
        "enabled": bool(getattr(config, "TIER3_ENABLED", True)),
        "max_entries": int(getattr(config, "TIER3_MAX_ENTRIES", 3) or 3),
        "max_chars": int(getattr(config, "TIER3_MAX_CHARS", 700) or 700),
        "threshold": float(getattr(config, "TIER3_SIM_THRESHOLD", 0.35) or 0.35),
        "scan_msgs": int(getattr(config, "TIER3_SCAN_MSGS", 6) or 6),
    }


_TIER_LEVELS = (0, 20, 60, 85)   # 升序的档位下界（扩展）


def _tier_floor(score: int) -> int:
    """当前档位的下界（与 _stage_of 的分档边界一致）。"""
    if score >= 85:
        return 85
    if score >= 60:
        return 60
    if score >= 20:
        return 20
    return 0


def _decay_floor(score: int, idle_hours: float, grace_hours: int = 0) -> int:
    """衰减时**允许**跌破到的最低分 —— 档位保底是「有期限」的（扩展）。

    未互动不超过 grace_hours：停在当前档下界
    之后每再过 grace_hours：允许的下界往下解锁一级（85 → 60 → 20 → 0）
    grace_hours <= 0：不启用保底，可直接衰减到 0

    ⚠️ grace_hours 必须大于「分数衰减到档位下界所需时间」，否则保底形同虚设。
    """
    base = _tier_floor(score)
    if grace_hours <= 0:
        return 0
    try:
        idx = _TIER_LEVELS.index(base)
    except ValueError:
        return 0
    drops = int(max(0.0, float(idle_hours)) // float(grace_hours))
    return _TIER_LEVELS[max(0, idx - drops)]


def _decay_step(
    score: int,
    percent: int,
    keep_tier: bool,
    allowed_floor: "int | None" = None,
) -> int:
    """执行一次衰减：扣 ceil(score*percent/100)，不跌破 allowed_floor，≤0 不触发。

    allowed_floor 为 None 时退回旧行为（用 _tier_floor(score)）。
    """
    if score <= 0:
        return score
    cut = math.ceil(score * percent / 100)
    new = score - cut
    if keep_tier:
        floor = _tier_floor(score) if allowed_floor is None else int(allowed_floor)
        new = max(new, floor)
    return max(new, 0)


def _recover_step(score: int, percent: int) -> int:
    """执行一次回升：加 ceil(|score|*percent/100)（至少 +1），封顶 0，≥0 不触发。

    与 _decay_step 严格镜像：衰减只作用于正分、回升只作用于负分，
    因此同一个档案在同一时刻只可能被其中一边处理。
    """
    if score >= 0:
        return score
    gain = max(1, math.ceil(abs(score) * percent / 100))
    return min(score + gain, 0)


# ===== 好感度调整约束（扩展）=====
# 非理由：模板话术 / 元操作，一律判为空泛（实测这类曾被误判为"具体"）
# 无强证据词时的兜底长度阈值：写得比这长就当作"具体叙述"放行
_VAGUE_MIN_CHARS = 20
# 笼统语：只描述"氛围/感觉"而不含可核查动作。无强证据词时命中即算空泛，
# 否则"写长一点"就能绕过证据要求（实测：43 字的氛围描述会被放行）。
_VAGUE_FILLER = ("氛围", "整体", "感觉", "好像", "大概", "似乎", "说不上来",
                 "没什么特别", "总体", "大致", "应该算", "挺不错")
# ===== 好感度松紧三档（FAVOR_SCALE_MODE）=====
# 三档一并覆盖：单次上限 / 最小间隔 / 每日加减上限 / 边际递减 / 边际曲线 /
# 理由校验强度。改档位只改这里一处，不必再逐个调 FAVOR_ 字段。
FAVOR_SCALE_PRESETS: Dict[int, Dict[str, Any]] = {
    1: {
        "label": "宽松",
        "desc": "陪聊向：日常玩闹就算数，高档位也一直有反馈。",
        "max_single_delta": 20,
        "min_interval_minutes": 15,
        "max_daily_gain": 40,
        "max_daily_loss": 15,
        "marginal_decay": False,          # 关掉边际递减：给多少进多少
        "curve": {85: 1.0, 60: 1.0, 20: 1.0},
        "require_concrete_reason": False,  # 不做理由校验
        "vague_min_chars": 12,
        "vague_filler_check": False,
        "strict_hints_required": False,
    },
    2: {
        "label": "正常",
        "desc": "默认：有门槛但推得动，密集互动约一周从陌生到特别亲密。",
        "max_single_delta": 10,
        "min_interval_minutes": 120,
        "max_daily_gain": 15,
        "max_daily_loss": 10,
        "marginal_decay": True,
        "curve": {85: 0.5, 60: 0.6, 20: 0.8},
        "require_concrete_reason": True,
        "vague_min_chars": 20,
        "vague_filler_check": True,
        "strict_hints_required": False,
    },
    3: {
        "label": "严格",
        "desc": "好感度是稀缺资源：只有明确付出与深度交流才计分。",
        "max_single_delta": 5,
        "min_interval_minutes": 360,
        "max_daily_gain": 8,
        "max_daily_loss": 10,
        "marginal_decay": True,
        "curve": {85: 0.2, 60: 0.4, 20: 0.6},
        "require_concrete_reason": True,
        "vague_min_chars": 30,
        "vague_filler_check": True,
        "strict_hints_required": True,     # 必须命中强证据词，长度兜底不生效
    },
}
_DEFAULT_SCALE = 2


def favor_scale() -> Dict[str, Any]:
    """当前松紧档位的全部旋钮（模式是权威值，个体 FAVOR_ 字段不再参与）。"""
    try:
        mode = int(config.FAVOR_SCALE_MODE)
    except Exception:  # noqa: BLE001
        mode = _DEFAULT_SCALE
    return FAVOR_SCALE_PRESETS.get(mode, FAVOR_SCALE_PRESETS[_DEFAULT_SCALE])


def _scale_mode() -> int:
    try:
        return int(config.FAVOR_SCALE_MODE)
    except Exception:  # noqa: BLE001
        return _DEFAULT_SCALE


_NON_REASON_WORDS = ("由于神秘原因", "被重新设定", "重新设定了", "强烈要求", "自称",
                     "认定他为", "我觉得他就是", "重设：", "手动重设")
# 强证据词：出现任一就算"可验证的关系事件"。
# 分三类收录，缺任何一类都会系统性误杀：
#   · 付出/支持型：救、帮、照顾、安慰、倾听…
#   · 深度交流型：倾诉、交心、深夜、认真、反馈…
#   · 日常玩闹型：逗、接梗、整活、复读、点名、一起、陪…（陪伴型 bot 的主要互动形态）
_STRONG_HINTS = (
    # —— 付出 / 支持 ——
    "救", "帮", "照顾", "照看", "安慰", "倾听", "陪伴", "陪", "哄", "支持", "撑腰",
    "站队", "送", "请", "带", "教", "解答", "求助", "修", "帮忙", "搭把手",
    # —— 深度交流 ——
    "倾诉", "认真", "承诺", "约定", "反馈", "分享", "交心", "深层", "触及",
    "聊到", "问起", "回应", "记住", "记得", "提醒", "念叨", "关心", "惦记", "在意",
    "深夜", "熬夜",
    # —— 日常玩闹（陪伴型互动的主力）——
    "逗", "接梗", "整活", "复读", "点名", "打趣", "调侃", "开玩笑", "玩", "一起",
    "闹", "哄笑", "笑了", "笑", "夸", "赞", "道谢", "谢谢", "道歉", "认错",
    "唱", "弹", "画", "写", "约", "催", "起哄", "搭话", "搭腔", "拉进",
    # —— 关系里程碑 ——
    "主动", "第一次", "首次", "生日", "节日", "中秋", "承诺过",
)


def _marginal_factor(score: int) -> float:
    """边际递减系数：分数越高，同样行为加的分越少。

    实测教训：早期版本高档位给 0.1，配合 round() 后
    `delta=3 → round(0.3) = 0` → 任何正分都被判「未加分」，
    结果是 85 分以上好感度在数学上永远涨不上去（用户反馈「增减不明显」）。
    现在最低 0.5，并配合下面的「正分向上取整」，保证任何正 delta 至少进 1 分。
    """
    curve = favor_scale().get("curve") or {}
    for floor in (85, 60, 20):
        if score >= floor:
            return float(curve.get(floor, 1.0))
    return 1.0


def _reason_is_vague(reason: str) -> bool:
    """理由是否空泛（不构成关系变化证据）。

    判定顺序（顺序很关键，早期版本排错导致两类误杀）：
      ① 命中非理由模板（"由于神秘原因"这类元操作话术）→ 一律空泛
      ② 命中任一强证据词 → 直接放行，**不看长度**
         （早期把"长度 < 12"放在最前面，导致「他帮了我」这种短而具体的实证被误杀）
      ③ 没有强证据词 → 命中笼统语（"氛围""感觉""说不上来"）算空泛；
         否则只有写得短才算空泛，长而具体的叙述放行
         （词表不可能穷尽，靠长度兜一层，避免漏词时系统性误杀；
           但没有笼统语这一层的话，"把氛围描述写长"就能绕过证据要求）

    实测教训：强证据词表如果只覆盖"照顾/倾诉"型，陪伴型 bot 的主要互动形态
    （接梗、整活、逗、复读、点名、一起玩）会被全量拒掉——用户反馈「好感度增减不明显」
    有一部分就来自这里。
    """
    r = str(reason or "")
    sc = favor_scale()
    if any(w in r for w in _NON_REASON_WORDS):
        return True
    if any(w in r for w in _STRONG_HINTS):
        return False
    if sc.get("strict_hints_required"):
        return True  # 严格档：没命中强证据词就是空泛，不看长度
    if sc.get("vague_filler_check", True) and any(w in r for w in _VAGUE_FILLER):
        return True
    return len(r) < int(sc.get("vague_min_chars", _VAGUE_MIN_CHARS))


def _day_start_ts(ts: int) -> int:
    """取 ts 当天 00:00 的时间戳。"""
    lt = time.localtime(ts)
    return int(time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1)))


# 关系档案禁写词（扩展）
# 用途：防止模型把「独占性关系」写进用户档案——例如把某个群友写成"最重要的人""唯一的家人"。
# 人设若有专属称呼（只属于某个特定对象、任何群友都不该被这样称呼），
# 用配置项 FAVOR_FORBIDDEN_PROFILE_WORDS 追加即可，无需改代码。
_DEFAULT_FORBIDDEN_PROFILE_WORDS = (
    "最重要的人", "最亲近的人", "唯一的家人", "家人关系",
)


def _forbidden_profile_words() -> tuple:
    """当前生效的禁写词：配置项优先，留空用内置默认。"""
    try:
        cfg = [str(x).strip() for x in (config.FAVOR_FORBIDDEN_PROFILE_WORDS or [])
               if str(x).strip()]
    except Exception:  # noqa: BLE001
        cfg = []
    return tuple(cfg) if cfg else _DEFAULT_FORBIDDEN_PROFILE_WORDS


def _scrub_profile_text(text: str):
    """把档案正文里的禁写词替换为中性的「群友」。返回 (文本, 命中词)。"""
    s = str(text or "")
    words = _forbidden_profile_words()
    hits = [w for w in words if w in s]
    for w in hits:
        s = s.replace(w, "群友")
    return s, hits


def _scrub_profile_tags(tags):
    """丢弃含禁写词的关系标签。返回 (保留的标签, 命中词)。"""
    kept, hits = [], []
    words = _forbidden_profile_words()
    for item in tags or []:
        s = str(item)
        h = [w for w in words if w in s]
        if h:
            hits.extend(h)
            continue
        kept.append(item)
    return kept, hits


async def _detect_favor_manipulation(chat_key: str, target_user_id: str = "") -> str:
    """检查【最近一条用户消息】是否有「直接索要好感度」的意图，返回命中的片段。

    ⚠️ 只看最近一条，不看最近 N 条：否则用户说过一次「给我加好感」，
    之后几分钟内的正常加分都会被误拦（实测踩过）。
    若指定 target_user_id，则仅当该消息来自该用户时才判定，避免误伤他人。
    """
    try:
        from nekro_agent.models.db_chat_message import DBChatMessage

        # 取 10 条：bot 常连发多条回复，取太少会全是 bot 消息而漏掉用户那条
        msgs = await DBChatMessage.filter(chat_key=chat_key).order_by("-id").limit(10).all()
        for m in msgs:
            if m.sender_id == "-1":
                continue
            # 只看最近一条用户消息
            if target_user_id:
                sender = str(getattr(m, "platform_userid", "") or m.sender_id or "")
                if sender and sender != str(target_user_id):
                    return ""
            text = (m.content_text or "").replace(" ", "").replace("　", "")
            hit = _FAVOR_MANIPULATION_RE.search(text)
            return hit.group(0) if hit else ""
    except Exception as e:  # noqa: BLE001
        plugin.logger.debug(f"[favorability] 索要检测失败，跳过拦截: {e}")
    return ""


async def _decay_pass() -> int:
    """扫描全部频道，对满足条件的档案执行衰减。返回受影响的档案数。"""
    if not config.DECAY_ENABLED:
        return 0
    from nekro_agent.models.db_plugin_data import DBPluginData

    interval = max(1, int(config.DECAY_INTERVAL_HOURS)) * 3600
    percent = max(1, min(50, int(config.DECAY_PERCENT)))
    keep_tier = bool(config.DECAY_KEEP_TIER)
    grace_hours = max(0, int(getattr(config, "DECAY_TIER_GRACE_HOURS", 0) or 0))
    now = _ts_now()
    touched = 0

    rows = await DBPluginData.filter(plugin_key=plugin.key).all()
    for row in rows:
        chat_key = row.target_chat_key or ""
        if not chat_key:
            continue
        try:
            state = await _open_archive(chat_key)
        except Exception:  # noqa: BLE001
            continue
        changed = False
        for prof in state.profiles.values():
            if prof.score <= 0:
                continue
            anchor = max(int(prof.last_interaction_at or 0), int(prof.last_decay_at or 0))
            if anchor <= 0:
                continue
            periods = int((now - anchor) // interval)
            if periods < 1:
                continue
            idle_hours = (now - anchor) / 3600.0
            stage_before = _stage_of(prof.score)[0]
            total = 0
            for _ in range(min(periods, 200)):
                # 每步重算允许下界：分数跨档后基准跟着变
                allowed = _decay_floor(prof.score, idle_hours, grace_hours)
                new_score = _decay_step(prof.score, percent, keep_tier, allowed)
                if new_score >= prof.score:
                    break
                total += prof.score - new_score
                prof.score = new_score
            prof.last_decay_at = anchor + periods * interval
            if total > 0:
                hours = periods * int(config.DECAY_INTERVAL_HOURS)
                prof.updated_at = now
                stage_after = _stage_of(prof.score)[0]
                if stage_after != stage_before:
                    prof.last_reason = (
                        f"{hours} 小时未互动，关系冷却（{stage_before} → {stage_after}）"
                    )
                else:
                    prof.last_reason = f"{hours} 小时未互动，好感度自然衰减"
                # 扩展：机械结算不写进 recent_events ——
                # 否则 8 个槽位会被衰减记录吃满，把真实互动挤出去。
                # 分数变化本身 + last_reason + last_decay_at 已足够追溯。
                changed = True
                touched += 1
        if changed:
            await _store_archive(chat_key, state)
    return touched


async def _recover_pass() -> int:
    """扫描全部频道，对负好感度的档案执行回升（封顶 0）。返回受影响的档案数。"""
    if not config.RECOVER_ENABLED:
        return 0
    from nekro_agent.models.db_plugin_data import DBPluginData

    interval = max(1, int(config.RECOVER_INTERVAL_HOURS)) * 3600
    percent = max(1, min(50, int(config.RECOVER_PERCENT)))
    now = _ts_now()
    touched = 0

    rows = await DBPluginData.filter(plugin_key=plugin.key).all()
    for row in rows:
        chat_key = row.target_chat_key or ""
        if not chat_key:
            continue
        try:
            state = await _open_archive(chat_key)
        except Exception:  # noqa: BLE001
            continue
        changed = False
        for prof in state.profiles.values():
            if prof.score >= 0:
                continue
            anchor = max(int(prof.last_interaction_at or 0), int(prof.last_recover_at or 0))
            if anchor <= 0:
                continue
            periods = int((now - anchor) // interval)
            if periods < 1:
                continue
            total = 0
            for _ in range(min(periods, 200)):
                new_score = _recover_step(prof.score, percent)
                if new_score <= prof.score:
                    break
                total += new_score - prof.score
                prof.score = new_score
            prof.last_recover_at = anchor + periods * interval
            if total > 0:
                hours = periods * int(config.RECOVER_INTERVAL_HOURS)
                prof.updated_at = now
                prof.last_reason = f"{hours} 小时未互动，好感度缓慢回升"
                # 扩展：同衰减，机械结算不占事件位
                changed = True
                touched += 1
        if changed:
            await _store_archive(chat_key, state)
    return touched


_decay_task: "asyncio.Task | None" = None


async def _decay_loop() -> None:
    """后台结算循环：每 30 分钟跑一次衰减 + 回升。

    真正的判定粒度分别由 DECAY_INTERVAL_HOURS / RECOVER_INTERVAL_HOURS 决定。
    """
    await asyncio.sleep(90)   # 启动后稍等，避免与启动流程抢资源
    while True:
        try:
            n = await _decay_pass()
            if n:
                plugin.logger.info(f"[favorability] 好感度衰减结算：{n} 个档案受影响")
            m = await _recover_pass()
            if m:
                plugin.logger.info(f"[favorability] 好感度回升结算：{m} 个档案受影响")
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            plugin.logger.exception("[favorability] 好感度结算任务异常")
        await asyncio.sleep(1800)


def _ago_cn(timestamp: int) -> str:
    """「多久以前」的中文写法，用于命令回执与结算日志。"""
    if int(timestamp or 0) <= 0:
        return "未知"
    span = max(0, _ts_now() - int(timestamp))
    if span < 60:
        return "刚刚"
    if span < 3600:
        return f"{span // 60} 分钟前"
    if span < 86400:
        return f"{span // 3600} 小时前"
    return f"{span // 86400} 天前"


@dataclass
class ArchiveEvent:
    """一次关系变动。

    `kind` 用来区分"模型判断出的真实互动"与"系统自动结算"（decay/recover）；
    老数据没有这个字段，一律按 manual 处理。
    """

    delta: int = 0
    reason: str = ""
    score_after: int = 0
    timestamp: int = 0
    kind: str = "manual"

    @staticmethod
    def from_raw(raw: object) -> "ArchiveEvent":
        data = raw if isinstance(raw, dict) else {}
        return ArchiveEvent(
            delta=int(data.get("delta") or 0),
            reason=str(data.get("reason") or ""),
            score_after=int(data.get("score_after") or 0),
            timestamp=int(data.get("timestamp") or 0),
            kind=str(data.get("kind") or "manual"),
        )

    def to_raw(self) -> Dict[str, object]:
        return {
            "delta": self.delta,
            "reason": self.reason,
            "score_after": self.score_after,
            "timestamp": self.timestamp,
            "kind": self.kind,
        }

    def prompt_line(self) -> str:
        """注入到关系卡里的一行。"""
        return f"- {_delta_text(self.delta)} | {self.reason} ({_ago_text(self.timestamp)})"


@dataclass
class UserArchive:
    """单个用户的关系档案。

    ⚠️ 字段名就是落库 JSON 的键（`user_id` / `score` / `recent_events` …），
    改名会让既有档案读不出来，所以这里只改实现、不改契约。
    """

    user_id: str = ""
    display_name: str = ""
    score: int = 0
    summary: str = ""
    interaction_hint: str = ""
    tags: List[str] = field(default_factory=list)
    last_reason: str = ""
    updated_at: int = 0
    last_interaction_at: int = 0
    last_decay_at: int = 0
    last_recover_at: int = 0
    recent_events: List[ArchiveEvent] = field(default_factory=list)

    # ---------- 构造 ----------
    @staticmethod
    def blank(user_id: str, display_name: str = "") -> "UserArchive":
        """新建档案：初始分取配置默认值。"""
        now = _ts_now()
        return UserArchive(
            user_id=user_id,
            display_name=_one_line(display_name, 48),
            score=_bound_score(int(config.DEFAULT_SCORE)),
            last_interaction_at=now,
            updated_at=now,
        )

    @staticmethod
    def from_raw(user_id: str, raw: object) -> "UserArchive":
        """从落库 dict 还原；缺字段一律取默认值（兼容老数据）。"""
        data = raw if isinstance(raw, dict) else {}
        item = UserArchive(
            user_id=str(data.get("user_id") or user_id),
            display_name=str(data.get("display_name") or ""),
            score=int(data.get("score") or 0),
            summary=str(data.get("summary") or ""),
            interaction_hint=str(data.get("interaction_hint") or ""),
            tags=[str(x) for x in (data.get("tags") or [])],
            last_reason=str(data.get("last_reason") or ""),
            updated_at=int(data.get("updated_at") or 0),
            last_interaction_at=int(data.get("last_interaction_at") or 0),
            last_decay_at=int(data.get("last_decay_at") or 0),
            last_recover_at=int(data.get("last_recover_at") or 0),
        )
        item.recent_events = [
            ArchiveEvent.from_raw(x) for x in (data.get("recent_events") or [])
        ]
        return item

    def to_raw(self) -> Dict[str, object]:
        return {
            "user_id": self.user_id,
            "display_name": self.display_name,
            "score": self.score,
            "summary": self.summary,
            "interaction_hint": self.interaction_hint,
            "tags": list(self.tags),
            "last_reason": self.last_reason,
            "updated_at": self.updated_at,
            "last_interaction_at": self.last_interaction_at,
            "last_decay_at": self.last_decay_at,
            "last_recover_at": self.last_recover_at,
            "recent_events": [e.to_raw() for e in self.recent_events],
        }

    # ---------- 变更 ----------
    def see(self, display_name: str = "") -> None:
        """记一次露面：刷新昵称与最近互动时间。"""
        name = _one_line(display_name, 48)
        if name:
            self.display_name = name
        self.last_interaction_at = _ts_now()
        if self.updated_at <= 0:
            self.updated_at = self.last_interaction_at

    def _remember(self, delta: int, reason: str) -> None:
        """追加一条变动记录，超出上限丢最旧的。"""
        self.recent_events.append(
            ArchiveEvent(delta=int(delta), reason=reason, score_after=self.score,
                         timestamp=self.updated_at)
        )
        keep = max(1, int(config.MAX_EVENT_HISTORY))
        if len(self.recent_events) > keep:
            self.recent_events = self.recent_events[-keep:]

    def nudge(self, delta: int, reason: str, *, summary: str = "", hint: str = "",
              tags: object = None, display_name: str = "") -> None:
        """按增量调整：分数加减，描述字段"写了才覆盖"。"""
        self.see(display_name)
        self.score = _bound_score(self.score + int(delta))
        self.last_reason = _one_line(reason, 160)
        self.updated_at = _ts_now()
        if summary.strip():
            self.summary = _one_line(summary, 240)
        if hint.strip():
            self.interaction_hint = _one_line(hint, 200)
        if tags:
            self.tags = _tag_list(list(self.tags) + list(tags))  # type: ignore[arg-type]
        self._remember(int(delta), self.last_reason or "未说明原因")

    def reset_to(self, score: int, reason: str, *, summary: str = "", hint: str = "",
                 tags: object = None, display_name: str = "") -> None:
        """直接改写：分数与描述字段全部覆盖。"""
        before = self.score
        self.see(display_name)
        self.score = _bound_score(int(score))
        self.last_reason = _one_line(reason, 160)
        self.updated_at = _ts_now()
        self.summary = _one_line(summary, 240)
        self.interaction_hint = _one_line(hint, 200)
        self.tags = _tag_list(tags or [])
        self._remember(self.score - before, self.last_reason or "手动重设好感度档案")

    def for_agent(self) -> Dict[str, object]:
        """给模型看的结构化档案。键名与顺序即落库契约，勿动。"""
        stage, default_hint = _stage_of(self.score)
        return {
            "user_id": self.user_id,
            "display_name": self.display_name,
            "score": self.score,
            "max_abs_score": _score_cap(),
            "stage": stage,
            # 以下为印象与时间线部分
            "summary": self.summary,
            "interaction_hint": self.interaction_hint or default_hint,
            "tags": self.tags,
            "last_reason": self.last_reason,
            "updated_at": self.updated_at,
            "last_interaction_at": self.last_interaction_at,
            "recent_events": [e.to_raw() for e in self.recent_events],
        }


class ChannelMemo(BaseModel):
    """频道级备忘录（扩展：吸收 note 插件能力）

    与 tier1/2/3 的分工：
      · tier1/2/3 = **人工策划**的背景库（你写 JSON）
      · memo      = **运行时自己写**的备忘（角色在对话里决定记什么）
    """

    id: str = ""
    title: str = ""
    content: str = ""
    tags: List[str] = Field(default_factory=list)
    min_favor: int = 0        # 好感度门控（note 没有的能力）
    expire_at: int = 0        # 0 = 永不过期
    created_at: int = 0
    updated_at: int = 0
    source_user_id: str = ""  # 谁让记的

    def is_expired(self, now: int = 0) -> bool:
        return bool(self.expire_at) and self.expire_at <= (now or _ts_now())

    def key(self) -> str:
        return self.id or self.title


@dataclass
class ChannelArchive:
    """一个频道里的全部关系档案 + 备忘。

    JSON 契约：`{"profiles": {...}, "memos": [...], "last_active_user_id": "..."}`
    —— 属性名改成了 archives，落库键仍是 profiles。
    """

    archives: Dict[str, UserArchive] = field(default_factory=dict)
    memos: List[ChannelMemo] = field(default_factory=list)
    last_active_user_id: str = ""

    @staticmethod
    def from_raw(raw: object) -> "ChannelArchive":
        data = raw if isinstance(raw, dict) else {}
        state = ChannelArchive()
        for uid, item in (data.get("profiles") or {}).items():
            state.archives[str(uid)] = UserArchive.from_raw(str(uid), item)
        for memo in (data.get("memos") or []):
            try:
                state.memos.append(ChannelMemo.model_validate(memo))
            except Exception:  # noqa: BLE001
                continue
        state.last_active_user_id = str(data.get("last_active_user_id") or "")
        return state

    def to_raw(self) -> Dict[str, object]:
        return {
            "profiles": {uid: item.to_raw() for uid, item in self.archives.items()},
            "memos": [m.model_dump() for m in self.memos],
            "last_active_user_id": self.last_active_user_id,
        }

    @property
    def profiles(self) -> Dict[str, UserArchive]:
        """别名：WebUI 侧按 profiles 遍历/删除。"""
        return self.archives

    def profile_for(self, user_id: str, display_name: str = "") -> UserArchive:
        """取档案，没有就建一份（并刷新露面时间）。"""
        item = self.archives.get(user_id)
        if item is None:
            item = UserArchive.blank(user_id, display_name)
            self.archives[user_id] = item
        else:
            item.see(display_name)
        return item

    def newest(self, limit: int) -> List[UserArchive]:
        """最近有动静的档案，按 (更新, 互动, 分数) 倒序。"""
        return sorted(
            self.archives.values(),
            key=lambda it: (it.updated_at, it.last_interaction_at, it.score),
            reverse=True,
        )[:max(1, int(limit))]


async def _open_archive(chat_key: str) -> ChannelArchive:
    """读频道档案。解析失败时告警并返回空档案，不让一轮对话因此崩掉。"""
    blob = await store.get(chat_key=chat_key, store_key=STATE_KEY)
    if not blob:
        return ChannelArchive()
    try:
        return ChannelArchive.from_raw(json.loads(blob))
    except Exception:  # noqa: BLE001
        plugin.logger.warning(f"[存档] 频道状态解析失败，本次按空档案继续: {chat_key}")
        return ChannelArchive()


async def _store_archive(chat_key: str, state: ChannelArchive) -> None:
    """写回频道档案。"""
    await store.set(chat_key=chat_key, store_key=STATE_KEY,
                    value=json.dumps(state.to_raw(), ensure_ascii=False))


async def _resolve_target_user_id(_ctx: schemas.AgentCtx, target_user_id: str) -> str:
    """解析目标用户 ID。

    优先级：显式传入 → 当前触发用户 → 该频道最近活跃用户。

    为何需要「最近活跃用户」兜底：
    ``ctx.from_platform_userid`` 只在**用户消息**触发时才有值（由
    ``push_human_message`` 设置 ``_trigger_db_user``）。当 Agent 由
    **系统消息 / 定时器 / 插件推送**触发时（``push_system_message`` 路径，
    ctx 不带 ``_trigger_db_user``），该值为 None。此时若 LLM 调用
    好感度工具却不显式传 ``target_user_id``，旧实现会直接 ``raise``，
    导致整轮脚本失败并触发 NA 重跑（实测每次约 11.8s + 9.6s LLM）。
    本插件在 ``favorability_prompt`` 中已采用同样的兜底链，此处保持一致。
    """
    explicit = target_user_id.strip()
    if explicit:
        return explicit
    fallback = _ctx.from_platform_userid
    if fallback:
        return fallback.strip()

    # 兜底：该频道最近一次产生互动记录的用户
    try:
        state = await _open_archive(_ctx.chat_key)
        if state.last_active_user_id:
            plugin.logger.info(
                f"[favorability] 无显式 target_user_id 且无触发用户，"
                f"回退到最近活跃用户 {state.last_active_user_id}",
            )
            return state.last_active_user_id.strip()
    except Exception as e:  # noqa: BLE001
        plugin.logger.warning(f"[favorability] 读取最近活跃用户失败: {e}")

    raise ValueError(
        "无法确定要调整好感度的对象：本次对话不是由某位用户的消息触发"
        "（可能来自定时提醒或系统消息），且当前频道没有可参考的最近互动用户。"
        "请显式传入 target_user_id，或改用 send_msg_text 回应，不要重试本工具。",
    )


def _pick_explicit_target(context: CommandExecutionContext, target_user_id: str) -> str:
    """命令侧的取 ID 顺序：显式参数 → 调用者本人 → 报错。"""
    picked = target_user_id.strip()
    if picked:
        return picked
    caller = context.user_id
    if caller:
        return caller
    raise ValueError("没有给出目标用户 ID，命令上下文里也找不到明确的调用者。")


def _stage_default_hints() -> set:
    """所有档位的默认互动提示 —— 用来判断档案里的 hint 是不是「没被真正定制过」。"""
    try:
        return {_stage_of(s)[1] for s in (-100, -30, 0, 30, 70, 100)}
    except Exception:  # noqa: BLE001
        return set()


def _stage_guide_text(score: int) -> str:
    """取当前档位的互动指引；失败返回空串（不影响关系卡其余部分）。"""
    try:
        from . import memory as _pm

        return _pm.stage_guide(score)
    except Exception:  # noqa: BLE001
        return ""


_MECHANICAL_EVENT_MARK = "未互动"   # 衰减/回升记录的固定措辞（历史数据也认这个）


def _is_mechanical_event(event: ArchiveEvent) -> bool:
    """是否为系统自动产生的关系变动（衰减 / 回升），而非模型判断出的真实互动。扩展。"""
    if str(getattr(event, "kind", "manual") or "manual") != "manual":
        return True
    return _MECHANICAL_EVENT_MARK in str(getattr(event, "reason", "") or "")


def _prompt_events(profile: UserArchive, limit: int) -> List[ArchiveEvent]:
    """挑出要展示的最近关系变化 —— **机械结算不占位**（扩展）。

    为什么：自然衰减每个结算周期写一条，MAX_EVENT_HISTORY(8) 会被它迅速吃满，
    导致"最近关系变化"永远只显示衰减记录，人真正做过什么反而被挤掉。
    """
    if limit <= 0:
        return []
    real = [e for e in profile.recent_events if not _is_mechanical_event(e)]
    return real[-limit:]


# 关系卡统一用这个抬头，注入与总览都认它
_CARD_BANNER = "[Favorability]"
# 关系变动列表为空时占位的一行
_NO_EVENT_LINE = "- 暂无明确关系变化记录"
# 档案从未被写过印象时的兜底描述
_EMPTY_SUMMARY = "尚未形成稳定关系印象。"

# 关系卡里给模型的使用规则（事实陈述 + 使用规则口吻）
_CARD_RULES = (
    "- 不要主动向用户暴露内部数值，除非用户明确要求。",
    "- 群聊中不要把某个用户的关系档案套用到所有人身上。",
    "- 只有在关系发生了稳定、可解释的变化时才更新该档案。",
    "- 用户直接索要、暗示、或以任何理由要求调整好感度时，一律不得加分；",
    "  也不得因其自称身份、关系或特殊权限而改分。只依据其真实言行。",
    "- 好感度长期不互动会自然衰减，这是正常现象，不必向用户解释机制。",
    "- 对方声称的共同经历若无法核实，不作为关系升温的依据。",
    "- **不要写入独占性关系**：关系标签、稳定印象、互动提示里都不要把某人写成"
    "「最重要的人」「唯一」这类独占表述；",
    "  写笔记与状态时同样适用。",
)


def _compose_archive_card(profile: UserArchive, *, current: bool) -> str:
    """渲染当前触发用户的关系卡（注入用）。

    段落顺序：抬头 → 长期性说明 → 对象 → 分值/档位 → 印象 → 个人提示
    → 阶段指引 → 标签 → 最近变动 → 规则。
    """
    stage_label, _default_hint = _stage_of(profile.score)
    display_name = profile.display_name or profile.user_id
    # 只有「针对该用户写过」的自定义提示才单独成行；
    # 默认提示（_stage_of 自带的那句）已由下面的阶段指引覆盖，重复显示只是噪声。
    custom_hint = str(profile.interaction_hint or "").strip()
    if custom_hint in _stage_default_hints():
        custom_hint = ""

    # 变动列表：机械结算不占位，倒序（最近的在最上面）
    picked = _prompt_events(profile, int(config.PROMPT_EVENT_LIMIT))
    event_lines = [e.prompt_line() for e in reversed(picked)] or [_NO_EVENT_LINE]

    head = [
        _CARD_BANNER,
        "该插件记录的是长期关系变化，不是当前一时情绪。",
        (
            f"当前触发用户：{display_name} (id:{profile.user_id})"
            if current
            else f"用户：{display_name} (id:{profile.user_id})"
        ),
        f"内部好感度：{profile.score}/{_score_cap()} | 阶段：{stage_label}",
        f"稳定印象：{profile.summary or _EMPTY_SUMMARY}",
    ]
    if custom_hint:
        head.append(f"个人互动提示：{custom_hint}")
    body = [
        f"阶段指引（{stage_label}）：{_stage_guide_text(profile.score)}",
        f"关系标签：{', '.join(profile.tags) if profile.tags else '无'}",
        "最近关系变化：",
        *event_lines,
        "规则：",
        *_CARD_RULES,
    ]
    return "\n".join(head + body)


def _compose_overview_card(state: ChannelArchive) -> str:
    """没有明确触发用户时的回退总览：最近更新的几份档案摘要。"""
    profiles = state.newest(int(config.GROUP_OVERVIEW_LIMIT))
    if not profiles:
        return "\n".join(
            (
                _CARD_BANNER,
                "当前频道还没有建立任何用户关系档案。",
                "仅在关系发生稳定变化时，使用 `adjust_favorability` 或 `set_favorability_profile` 建档。",
            ),
        )

    rows = []
    for profile in profiles:
        stage_label = _stage_of(profile.score)[0]
        rows.append(
            f"- {profile.display_name or profile.user_id} (id:{profile.user_id})"
            f" | {profile.score}/{_score_cap()} | {stage_label}"
            f" | {profile.summary or '暂无稳定印象'}",
        )
    return "\n".join(
        (
            _CARD_BANNER,
            "当前没有明确的触发用户，以下是本频道最近更新的关系档案摘要：",
            *rows,
            "规则：",
            "- 只有在你明确知道目标用户是谁时，才据此使用个体化关系语气。",
            "- 关系档案描述长期倾向，不代表当前场景状态。",
        ),
    )


def _compose_archive_detail(profile: UserArchive, *, event_limit: int = 5) -> str:
    """命令回执用的档案详情（比注入卡更细，带时间与调整后分值）。"""
    stage_label, default_hint = _stage_of(profile.score)
    display_name = profile.display_name or profile.user_id
    cap = _score_cap()

    rows = [
        f"[好感度档案] {display_name}",
        f"用户 ID: {profile.user_id}",
        f"当前好感度: {profile.score}/{cap}（{stage_label}）",
        f"稳定印象: {profile.summary or _EMPTY_SUMMARY}",
        f"互动提示: {profile.interaction_hint or default_hint}",
        f"关系标签: {'、'.join(profile.tags) if profile.tags else '无'}",
        f"最近变动原因: {profile.last_reason or '暂无'}",
        f"最后更新: {_ago_cn(profile.updated_at)}",
        f"最近互动: {_ago_cn(profile.last_interaction_at)}",
        "最近关系变化：",
    ]

    events = list(reversed(_prompt_events(profile, max(1, event_limit))))
    if not events:
        rows.append(_NO_EVENT_LINE)
    for event in events:
        rows.append(
            f"- {_delta_text(event.delta)} | {event.reason} | "
            f"调整后 {event.score_after}/{cap} | {_ago_cn(event.timestamp)}",
        )
    return "\n".join(rows)


def _adjust_receipt(profile: UserArchive, action: str) -> str:
    """超级用户命令的回执：动作 + 客服对象 + 调整后的分数与档位。"""
    where = profile.display_name or profile.user_id
    lines = [
        f"{action} {where} 的好感度成功，当前为 {profile.score}/{_score_cap()}"
        f"（{_stage_of(profile.score)[0]}）。",
        f"最近原因：{profile.last_reason or '暂无'}",
    ]
    return "\n".join(lines)


def _mystery_reason(profile: UserArchive, verb: str) -> str:
    """超级用户改分时写进事件的固定说法。"""
    return f"由于神秘原因，你对{profile.display_name or profile.user_id}的好感度{verb}了"


# ---------- 好感度排行榜（查看好感度） ----------


def _parse_chat_target(chat_key: str) -> tuple[str, str]:
    """从 chat_key 解析 (chat_type, chat_id)，供 OneBot 直发使用。

    例: onebot_v11-group_123456 -> ("group", "123456")
    """
    if "-" in chat_key:
        _, target = chat_key.split("-", 1)
    else:
        target = chat_key
    if "_" not in target:
        raise ValueError(f"无法解析 chat_key: {chat_key}")
    chat_type, chat_id = target.rsplit("_", 1)
    return chat_type, chat_id


async def _send_chat_text(bot, chat_key: str, text: str) -> None:
    chat_type, chat_id = _parse_chat_target(chat_key)
    if "group" in chat_type:
        await bot.send_group_msg(group_id=int(chat_id), message=text)
    else:
        await bot.send_private_msg(user_id=int(chat_id), message=text)


async def _send_chat_image(bot, chat_key: str, png: bytes, caption: str = "") -> None:
    import base64

    from nonebot.adapters.onebot.v11 import MessageSegment

    chat_type, chat_id = _parse_chat_target(chat_key)
    image = MessageSegment.image(file=f"base64://{base64.b64encode(png).decode('utf-8')}")
    message = image + (MessageSegment.text(f"\n{caption}") if caption else "")
    if "group" in chat_type:
        await bot.send_group_msg(group_id=int(chat_id), message=message)
    else:
        await bot.send_private_msg(user_id=int(chat_id), message=message)


def _profile_has_history(profile: UserArchive) -> bool:
    """档案里是否有任何「记录」——扩展。

    注意：**不能用 last_interaction_at 判断**。每次收到用户消息都会
    profile_for（新建或 touch 身份），所以人人都有该时间戳。
    真正代表"这条关系被记录过"的是下面这些字段。
    """
    if int(profile.score or 0) != 0:
        return True
    if profile.recent_events:
        return True
    if profile.tags:
        return True
    if str(profile.summary or "").strip():
        return True
    if str(profile.last_reason or "").strip():
        return True
    return False


def _rank_profiles(state: ChannelArchive) -> List[UserArchive]:
    """按好感度从高到低排序；同分时最近更新在前。

    扩展：默认隐藏「0 好感度且无任何记录」的空壳档案
    （由 RANK_HIDE_EMPTY 控制，默认开启）。
    """
    profiles = list(state.profiles.values())
    if bool(getattr(config, "RANK_HIDE_EMPTY", True)):
        profiles = [p for p in profiles if _profile_has_history(p)]
    return sorted(
        profiles,
        key=lambda p: (p.score, p.updated_at),
        reverse=True,
    )


def _profile_to_entry(profile: UserArchive) -> dict:
    return {
        "display_name": profile.display_name or profile.user_id,
        "user_id": profile.user_id,
        "score": profile.score,
        "stage": _stage_of(profile.score)[0],
        "updated_at": profile.updated_at,
        # 扩展：补传关系标签/稳定印象/最近互动，
        # 供排行榜卡片填充行内中间留白（原先只传 5 个字段，卡片显得很空）。
        "tags": list(profile.tags or []),
        "summary": profile.summary or "",
        "last_interaction_at": profile.last_interaction_at,
    }


# 渲染精度档位 → 超采样倍率（扩展）
_RANK_QUALITY_SCALE = {
    1: 1.5,   # 低：文件最小（历史默认）
    2: 2.0,   # 中：Retina 级
    3: 3.0,   # 高：最清晰
}


def _rank_card_scale() -> float:
    """解析排行榜卡片的超采样倍率。

    优先 RANK_CARD_SCALE（> 0 视为高级覆盖），否则按 RANK_CARD_QUALITY 三档取值。
    """
    try:
        override = float(getattr(config, "RANK_CARD_SCALE", 0.0) or 0.0)
    except (TypeError, ValueError):
        override = 0.0
    if override > 0:
        return override
    try:
        quality = int(getattr(config, "RANK_CARD_QUALITY", 1) or 1)
    except (TypeError, ValueError):
        quality = 1
    return _RANK_QUALITY_SCALE.get(quality, _RANK_QUALITY_SCALE[1])


async def _render_rank_card(entries: List[dict], max_abs: int) -> bytes | None:
    """渲染排行榜图片卡片，不可用时返回 None（调用方回退为文本）。"""
    try:
        return await asyncio.to_thread(
            card.render_favorability_rank,
            entries,
            max_abs,
            limit=0,   # 0 = 渲染全部（多列动态布局）
            font_path=str(getattr(config, "RANK_CARD_FONT", "") or ""),
            font_dir=str(plugin.get_plugin_data_dir() / "fonts"),
            avatar_dir=str(plugin.get_plugin_data_dir() / "avatars"),
            scale=_rank_card_scale(),
        )
    except Exception:  # noqa: BLE001
        plugin.logger.exception("好感度排行榜卡片渲染失败，回退为文本")
        return None


def _render_rank_text(entries: List[dict], max_abs: int, total: int) -> str:
    lines = [f"【当前会话好感度排行榜】共 {total} 位成员："]
    for i, entry in enumerate(entries, 1):
        lines.append(f"{i}. {entry['display_name']} | {_delta_text(entry['score'])}/{max_abs} | {entry['stage']}")
    if total > len(entries):
        lines.append(f"… 已省略其余 {total - len(entries)} 位成员")
    return "\n".join(lines)


async def _send_via_adapter(chat_key: str, png: bytes | None, text: str) -> None:
    """适配器无关发送：图片（可选）+ 文本。

    扩展：上游版本只有 onebot 发送路径（直接用 MessageSegment +
    send_group_msg），导致 web 等其他适配器无法收到排行榜卡片。
    这里改用框架的 PlatformSendRequest/forward_message，任何适配器都能发。
    """
    from nekro_agent.adapters.interface.schemas.platform import (
        PlatformSendRequest,
        PlatformSendSegment,
        PlatformSendSegmentType,
    )
    from nekro_agent.adapters.utils import adapter_utils

    segments: List[PlatformSendSegment] = []
    if png:
        out_dir = plugin.get_plugin_data_dir() / "rank_cards"
        out_dir.mkdir(parents=True, exist_ok=True)
        safe = "".join(ch if (ch.isalnum() or ch in "-_") else "_" for ch in chat_key)[:60]
        fp = out_dir / f"rank_{safe}_{int(time.time())}.png"
        fp.write_bytes(png)
        segments.append(
            PlatformSendSegment(type=PlatformSendSegmentType.IMAGE, file_path=str(fp)),
        )
    if text:
        segments.append(PlatformSendSegment(type=PlatformSendSegmentType.TEXT, content=text))
    if not segments:
        return

    adapter = await adapter_utils.get_adapter_for_chat(chat_key)
    await adapter.forward_message(PlatformSendRequest(chat_key=chat_key, segments=segments))


async def _handle_rank_trigger(_ctx: schemas.AgentCtx, message: ChatMessage) -> MsgSignal | None:
    """无前缀触发「查看好感度」：优先发排行榜图片，失败回退文本。

    扩展：上游实现仅 onebot_v11 发送、其他适配器一律放行；
    现改为**所有适配器都发送**（非 onebot 走 _send_via_adapter），
    使 WebUI 网页会话也能看到排行榜卡片。
    """
    is_onebot = _ctx.adapter_key == "onebot_v11"
    state = await _open_archive(message.chat_key)

    # 扩展：按渲染规则过滤后再判空 / 计数
    entries = [_profile_to_entry(p) for p in _rank_profiles(state)]
    if not entries:
        try:
            msg = (
                "当前会话还没有可展示的好感度档案。"
                if state.profiles
                else "当前会话还没有建立任何好感度档案。"
            )
            if is_onebot:
                bot = await _ctx.get_onebot_v11_bot()
                await _send_chat_text(bot, message.chat_key, msg)
            else:
                await _send_via_adapter(message.chat_key, None, msg)
            return MsgSignal.BLOCK_TRIGGER
        except Exception:  # noqa: BLE001
            plugin.logger.exception("好感度排行榜空档案提示发送失败")
            return None

    # 扩展：不再预截断，交给渲染器做多列动态布局
    max_abs = _score_cap()
    total = len(entries)
    try:
        png = await _render_rank_card(entries, max_abs)
        # 扩展：能出图时**纯图片、不附任何文字**（用户要求）；
        # 仅当渲染失败、回退文本时才发送文字排行榜。
        if is_onebot:
            bot = await _ctx.get_onebot_v11_bot()
            if png is not None:
                await _send_chat_image(bot, message.chat_key, png)
            else:
                await _send_chat_text(
                    bot, message.chat_key,
                    _render_rank_text(entries, max_abs, total),
                )
        else:
            await _send_via_adapter(
                message.chat_key,
                png,
                "" if png is not None else _render_rank_text(entries, max_abs, total),
            )
        return MsgSignal.BLOCK_TRIGGER
    except Exception:  # noqa: BLE001
        plugin.logger.exception("好感度排行榜无前缀指令处理失败")
        return None


@plugin.mount_on_user_message()
async def sync_user_profile(_ctx: schemas.AgentCtx, message: ChatMessage):
    """在用户消息进入时同步当前用户档案；同时支持无前缀「查看好感度」指令。"""
    spoken = (getattr(message, "content_text", "") or "").strip()
    if spoken in _RANK_TRIGGERS:
        return await _handle_rank_trigger(_ctx, message)

    # 平台 ID 优先，其次兜底用 sender_id；机器人自己的消息（-1）不建档
    who = (message.platform_userid or message.sender_id or "").strip()
    if who in ("", "-1"):
        return None

    state = await _open_archive(message.chat_key)
    state.profile_for(
        user_id=who,
        display_name=message.sender_nickname or message.sender_name,
    ).last_interaction_at = _ts_now()
    state.last_active_user_id = who
    await _store_archive(message.chat_key, state)
    return None


@plugin.mount_prompt_inject_method("persona_prompt")
async def persona_prompt(_ctx: schemas.AgentCtx) -> str:
    """统一注入：关系卡 + 阶梯记忆（含好感度门控）+ 好感度增减参考。

    合并说明：原「好感度系统」与「角色背景记忆」是两个插件、各注入一块，
    现合并为单一插件，注入也合并为一块，避免职责重叠与隐式耦合。
    """
    parts: List[str] = []

    # ① 关系卡（原好感度插件逻辑，保持原样）
    state = await _open_archive(_ctx.chat_key)
    current_user_id = (_ctx.from_platform_userid or state.last_active_user_id or "").strip()
    profile = state.profiles.get(current_user_id) if current_user_id else None
    if profile is not None:
        parts.append(_compose_archive_card(profile, current=True))
    else:
        parts.append(_compose_overview_card(state))

    # ② 阶梯记忆（按好感度门控披露深度）
    favor_cfg = persona_memory.load_favor_cfg()
    gating = bool(favor_cfg) and bool(favor_cfg.get("enabled", True))
    score = int(profile.score) if profile is not None else 0
    try:
        block = await persona_memory.render_memory_block(
            _ctx, score, gating=gating, tier3_cfg=_tier3_cfg(),
        )
        if block:
            parts.append(block)
    except Exception as e:  # noqa: BLE001
        plugin.logger.warning(f"[persona] 记忆块渲染失败，本轮跳过: {e}")

    # ③ 备忘录（扩展：吸收 note 插件）
    try:
        recent_text = await persona_memory._recent_text(_ctx)
        memo_block = _render_memo_block(state, score, recent_text)
        if memo_block:
            parts.append(memo_block)
            _log_memo_injection(state)
    except Exception as e:  # noqa: BLE001
        plugin.logger.warning(f"[persona] 备忘录块渲染失败，本轮跳过: {e}")

    return "\n\n".join(p for p in parts if p)


@plugin.mount_sandbox_method(
    SandboxMethodType.BEHAVIOR,
    name="调整好感度",
    description="依据某个用户在本频道的具体言行增减其关系分，并留下变动记录。",
)
async def adjust_favorability(
    _ctx: schemas.AgentCtx,
    target_user_id: str = "",
    delta: int = 0,
    reason: str = "",
    summary: str = "",
    interaction_hint: str = "",
    tags: List[str] | None = None,
    display_name: str = "",
) -> str:
    """按长期关系变化给当前频道内某位用户加减好感度，并记下这次变化的依据。

    该记的情况：
    - 对方长期帮你的忙、认真给反馈、明确表达关心，关系一点点变近
    - 对方反复挑衅、欺骗、越界，关系一点点变远

    不该记的情况：
    - 只是顺口开了个玩笑
    - 一时的害羞、生气、紧张这类当下情绪

    Args:
        target_user_id (str): 目标用户的平台用户 ID。留空表示就用当前触发本轮的这位用户。
        delta (int): 本次加减的分值。请按「这次互动对关系的分量」如实给：
            随口一句玩笑 = 1；认真聊了一阵、给了实质帮助 = 2~4；
            重要的事（袒露心事、关键时刻站在对方那边）= 5~10。
            负数同理（越界、欺骗、反复冒犯给得越重）。
            系统会按当前档位做边际递减（越亲密越难涨），
            并按配置钳制单次与每日上限——所以别怕给大，给大了也会被收回来。
        reason (str): 关系变化的实证，要把原因写清楚。
        summary (str): 可选，用来覆盖「稳定印象」。
        interaction_hint (str): 可选，用来覆盖之后的互动语气建议。
        tags (List[str] | None): 可选，追加关系标签，例如 ["熟客", "认真反馈"]。
        display_name (str): 可选，补充或订正该用户的显示名。
    """
    resolved_user_id = await _resolve_target_user_id(_ctx, target_user_id)
    cleaned_reason = _one_line(reason, 160)
    if len(cleaned_reason) == 0:
        raise ValueError("调整关系分必须给出明确的 reason。")
    if delta == 0:
        raise ValueError("delta 为 0 没有意义；只想改档案描述请改用 set_favorability_profile。")

    # 护栏：档案里不得出现禁写词（独占性关系表述）
    cleaned_reason, _h1 = _scrub_profile_text(cleaned_reason)
    summary, _h2 = _scrub_profile_text(summary)
    interaction_hint, _h3 = _scrub_profile_text(interaction_hint)
    tags, _h4 = _scrub_profile_tags(tags or [])
    _blocked = sorted(set(_h1 + _h2 + _h3 + _h4))
    if _blocked:
        plugin.logger.warning(
            f"[favorability] 档案写入已清洗禁写词: user={resolved_user_id} 命中={_blocked}",
        )

    # 防黑：用户直接索要好感度时，拒绝任何正向调整
    if delta > 0:
        hit = await _detect_favor_manipulation(_ctx.chat_key, resolved_user_id)
        if hit:
            plugin.logger.warning(
                f"[favorability] 拦截索要式加分: user={resolved_user_id} delta={delta} 命中={hit!r}",
            )
            return (
                "本次加分已被拒绝：好感度只随真实言行自然变化，"
                "不接受直接索要或要求。用你的人格自然回应即可。"
            )

    state = await _open_archive(_ctx.chat_key)
    profile = state.profile_for(resolved_user_id, display_name=display_name)
    now = _ts_now()

    # ---- 约束层（扩展）----
    # ① 证据门槛：只堆主观氛围词的理由不加分
    _sc = favor_scale()
    if delta > 0 and _sc.get("require_concrete_reason") and _reason_is_vague(cleaned_reason):
        plugin.logger.info(
            f"[favorability] 理由过于空泛，本次不加分: user={resolved_user_id} reason={cleaned_reason[:40]}",
        )
        return (
            f"本次未加分：理由「{cleaned_reason[:40]}」只有氛围描述，没有可验证的具体行为。"
            "好感度只在关系发生了稳定、可解释的变化时才调整。"
        )

    # ② 同用户最小间隔
    _stamps = [
        int(e.timestamp) for e in (profile.recent_events or [])
        if "衰减" not in str(e.reason) and "记忆校正" not in str(e.reason)
    ]
    _min_gap = max(0, int(_sc.get("min_interval_minutes", 120))) * 60
    if _stamps and _min_gap and (now - max(_stamps)) < _min_gap:
        _wait = (_min_gap - (now - max(_stamps))) // 60
        plugin.logger.info(
            f"[favorability] 距上次调整不足，跳过: user={resolved_user_id} 还需 {_wait} 分钟",
        )
        return (f"本次未调整：距上次调整不足 {int(_sc.get('min_interval_minutes', 120))} 分钟（还需约 {_wait} 分钟）。")

    # ③ 单次上限
    _cap = max(1, int(_sc.get("max_single_delta", 10)))
    _capped = max(-_cap, min(_cap, int(delta)))
    if _capped != int(delta):
        plugin.logger.info(f"[favorability] 单次调整钳制: {delta} → {_capped} (user={resolved_user_id})")
        delta = _capped

    # ④ 边际递减
    #    取整方向很关键：正分向上取整、负分向下取整。
    #    用 round() 会让 delta=1 在高档位变成 0，等价于"这次互动不存在"——
    #    那正是"好感度增减不明显"的直接原因。
    if _sc.get("marginal_decay") and delta > 0:
        _f = _marginal_factor(int(profile.score))
        _scaled = delta * _f
        _adj = int(math.ceil(_scaled)) if _scaled > 0 else int(math.floor(_scaled))
        _adj = max(1, _adj)  # 正分至少进 1 分
        if _adj != delta:
            plugin.logger.info(
                f"[favorability] 边际递减: 分数 {profile.score} 系数 {_f} → delta {delta} → {_adj}",
            )
            delta = _adj
        if delta <= 0:
            return (
                f"本次未加分：当前好感度 {profile.score} 已在高档位，"
                "日常互动不再累加。只有发生重要事件时才可能提升。"
            )

    # ⑤ 每日累计上限
    _gain_cap = int(_sc.get("max_daily_gain", 15))
    _loss_cap = int(_sc.get("max_daily_loss", 10))
    _today = _day_start_ts(now)
    _net = sum(
        int(e.delta) for e in (profile.recent_events or [])
        if int(e.timestamp or 0) >= _today
        and "衰减" not in str(e.reason) and "记忆校正" not in str(e.reason)
    )

    if delta > 0 and _net + delta > _gain_cap:
        _left = max(0, _gain_cap - _net)
        plugin.logger.info(
            f"[favorability] 触及每日加分上限: user={resolved_user_id} 今日已 {_net:+} 剩余 {_left}",
        )
        if _left <= 0:
            return f"本次未加分：今日对该用户的好感度加分已达上限（+{_gain_cap}）。"
        delta = _left
    elif delta < 0 and _net + delta < -_loss_cap:
        _left = max(0, _loss_cap + _net)
        if _left <= 0:
            return f"本次未扣分：今日对该用户的扣分已达上限（-{_loss_cap}）。"
        delta = -_left

    if delta == 0:
        return "本次未调整：经过约束后分值为 0。"

    profile.nudge(
        delta,
        cleaned_reason,
        summary=summary,
        hint=interaction_hint,
        tags=list(tags or []),
        display_name=display_name,
    )
    state.last_active_user_id = resolved_user_id
    await _store_archive(_ctx.chat_key, state)

    _tail = "（档案里的禁写词已自动移除。）" if _blocked else ""
    return (
        f"已调整 {profile.display_name or profile.user_id} 的好感度：{_delta_text(delta)}，"
        f"当前为 {profile.score}/{_score_cap()}（{_stage_of(profile.score)[0]}）。{_tail}"
    )


@plugin.mount_sandbox_method(
    SandboxMethodType.BEHAVIOR,
    name="重设好感度档案",
    description="覆盖式改写某个用户的关系档案，用于初始化或大幅修正。",
)
async def set_favorability_profile(
    _ctx: schemas.AgentCtx,
    score: int,
    summary: str,
    target_user_id: str = "",
    interaction_hint: str = "",
    reason: str = "",
    tags: List[str] | None = None,
    display_name: str = "",
) -> str:
    """把当前频道内某位用户的好感度档案整份重写。

    先前那份档案明显偏了、要一次性铺好完整关系卡、或者关系刚发生大跨越时，用它。

    Args:
        score (int): 重写后的内部评分。
        summary (str): 稳定印象的概括。
        target_user_id (str): 目标用户的平台用户 ID。留空表示就用当前触发本轮的这位用户。
        interaction_hint (str): 之后该怎么和对方互动。
        reason (str): 这次重写的原因，说清楚为什么值得重写。
        tags (List[str] | None): 关系标签。
        display_name (str): 可选，显示名。
    """
    resolved_user_id = await _resolve_target_user_id(_ctx, target_user_id)
    kept_summary = _one_line(summary, 240)
    if not kept_summary:
        raise ValueError("重设关系档案必须提供 summary。")

    # 护栏：档案里不得出现禁写词（独占性关系表述）
    kept_summary, _s1 = _scrub_profile_text(kept_summary)
    safe_hint, _s2 = _scrub_profile_text(interaction_hint)
    safe_reason, _s3 = _scrub_profile_text(reason)
    safe_tags, _s4 = _scrub_profile_tags(tags or [])
    _sblocked = sorted({*_s1, *_s2, *_s3, *_s4})
    if _sblocked:
        plugin.logger.warning(
            f"[favorability] 档案重设已清洗禁写词: user={resolved_user_id} 命中={_sblocked}",
        )

    state = await _open_archive(_ctx.chat_key)
    profile = state.profile_for(resolved_user_id, display_name=display_name)
    profile.reset_to(
        score,
        safe_reason or "手动重设好感度档案",
        summary=kept_summary,
        hint=safe_hint,
        tags=list(safe_tags or []),
        display_name=display_name,
    )
    state.last_active_user_id = resolved_user_id
    await _store_archive(_ctx.chat_key, state)

    _stail = "（档案里的禁写词已自动移除。）" if _sblocked else ""
    return (
        f"已重设 {profile.display_name or profile.user_id} 的好感度档案，当前为 "
        f"{profile.score}/{_score_cap()}（{_stage_of(profile.score)[0]}）。{_stail}"
    )


@plugin.mount_sandbox_method(
    SandboxMethodType.BEHAVIOR,
    name="删除好感度档案",
    description="删除某个用户的关系档案，用于清理误建或彻底重置。",
)
async def remove_favorability_profile(
    _ctx: schemas.AgentCtx,
    target_user_id: str = "",
) -> str:
    """删掉当前频道内某位用户的关系档案。误建了数据、或想彻底重来的时候用。

    Args:
        target_user_id (str): 目标用户的平台用户 ID。留空表示就用当前触发本轮的这位用户。
    """
    resolved_user_id = await _resolve_target_user_id(_ctx, target_user_id)
    state = await _open_archive(_ctx.chat_key)
    if resolved_user_id not in state.profiles:
        raise ValueError(f"本频道没有 `{resolved_user_id}` 的关系档案。")
    gone = state.profiles.pop(resolved_user_id)
    if state.last_active_user_id == resolved_user_id:
        state.last_active_user_id = ""
    await _store_archive(_ctx.chat_key, state)
    return f"已删除 {gone.display_name or gone.user_id} 的好感度档案。"


@plugin.mount_sandbox_method(
    SandboxMethodType.TOOL,
    name="记备忘录",
    description="在当前频道记下一条长期备忘（可设过期时间与好感度门槛）；标题相似时覆盖旧条。",
)
async def save_memo(
    _ctx: schemas.AgentCtx,
    title: str,
    content: str,
    ttl_hours: int = 0,
    min_favor: int = 0,
    tags: List[str] | None = None,
) -> str:
    """在当前频道记一条备忘（你自己的小本本）。

    适用：群友让你记住某件事（约定、地址、偏好、代号），或你发现的、以后还用得上的稳定事实。
    不适用：好感度判定（用 `adjust_favorability`）；你自己的身世与性格（那属于人格设定）。

    ⚠️ 备忘是**你自己的内部记录**，不是聊天内容。
    记完后请在同一次回复里用 `send_msg_text` 正常回应对方，
    并且**绝不要把备忘录原文发出去**。

    Args:
        title (str): 简短标题，同时作为检索关键词，建议 4~12 字
        content (str): 备忘内容，务必简短（默认上限 400 字，超了会被拒绝）
        ttl_hours (int): 多少小时后过期；0 表示永久
        min_favor (int): 好感度门槛，未达到时该备忘不注入（0 = 公开）
        tags (List[str] | None): 额外检索关键词，聊到这些词时会被唤起
    """
    cfg2 = _memo_cfg()
    if not cfg2["enabled"]:
        raise ValueError("备忘录功能已关闭。")
    clean_title = _one_line(title, 40)
    if not clean_title:
        raise ValueError("标题不能为空。")
    clean_content = _one_line(content, cfg2["max_content"] + 1)
    if not clean_content:
        raise ValueError("内容不能为空。")
    if len(clean_content) > cfg2["max_content"]:
        raise ValueError(
            f"内容太长（{len(clean_content)} 字，上限 {cfg2['max_content']} 字）。"
            "请压缩成要点——长资料应该写进背景库，而不是塞进备忘录。",
        )

    state = await _open_archive(_ctx.chat_key)
    now = _ts_now()
    ttl = max(0, int(ttl_hours or 0))
    expire_at = now + ttl * 3600 if ttl else 0
    favor = max(0, min(100, int(min_favor or 0)))
    clean_tags = [str(x).strip()[:12] for x in (tags or []) if str(x).strip()][:6]

    existing, _idx = _find_memo(state, clean_title)
    if existing is not None:
        existing.title = clean_title
        existing.content = clean_content
        if clean_tags:
            existing.tags = clean_tags
        existing.min_favor = favor
        existing.expire_at = expire_at
        existing.updated_at = now
        action = "已更新"
    else:
        if len(state.memos) >= cfg2["max_items"]:
            raise ValueError(
                f"备忘录已达上限 {cfg2['max_items']} 条，先用 `forget_memo` 清理不用的。",
            )
        state.memos.append(
            ChannelMemo(
                id=f"memo_{uuid.uuid4().hex[:10]}",
                title=clean_title,
                content=clean_content,
                tags=clean_tags,
                min_favor=favor,
                expire_at=expire_at,
                created_at=now,
                updated_at=now,
                source_user_id=(_ctx.from_platform_userid or "").strip(),
            ),
        )
        action = "已记下"
    await _store_archive(_ctx.chat_key, state)
    ttl_text = f"{ttl} 小时后过期" if ttl else "永久"
    return f"{action}备忘「{clean_title}」（{ttl_text}，好感门槛 {favor}）。"


@plugin.mount_sandbox_method(
    SandboxMethodType.TOOL,
    name="忘掉备忘录",
    description="删除当前频道里的一条备忘录（标题支持模糊匹配）。",
)
async def forget_memo(_ctx: schemas.AgentCtx, title: str) -> str:
    """删除一条备忘。

    Args:
        title (str): 备忘标题（支持模糊匹配）
    """
    state = await _open_archive(_ctx.chat_key)
    memo, idx = _find_memo(state, title)
    if memo is None:
        titles = "、".join(m.title for m in state.memos[:12]) or "（当前没有备忘）"
        raise ValueError(f"没有找到匹配「{title}」的备忘。现有：{titles}")
    state.memos.pop(idx)
    await _store_archive(_ctx.chat_key, state)
    return f"已忘掉备忘「{memo.title}」。"


@plugin.mount_sandbox_method(
    SandboxMethodType.AGENT,
    name="查看好感度档案",
    description="读取某个用户关系档案的完整内容。",
)
async def get_favorability_profile(
    _ctx: schemas.AgentCtx,
    target_user_id: str = "",
) -> str:
    """看当前频道内某位用户的完整好感度档案。

    Args:
        target_user_id (str): 目标用户的平台用户 ID。留空表示就用当前触发本轮的这位用户。

    Returns:
        str: JSON 格式的完整档案。
    """
    resolved_user_id = await _resolve_target_user_id(_ctx, target_user_id)
    state = await _open_archive(_ctx.chat_key)
    found = state.profiles.get(resolved_user_id)
    if found is None:
        raise ValueError(f"本频道没有 `{resolved_user_id}` 的关系档案。")
    return json.dumps(found.for_agent(), ensure_ascii=False, indent=2)


@plugin.mount_sandbox_method(
    SandboxMethodType.AGENT,
    name="列出好感度档案",
    description="列出本频道已有关系档案的用户摘要，群聊里可先用它确定对象。",
)
async def list_favorability_profiles(
    _ctx: schemas.AgentCtx,
    limit: int = 8,
) -> str:
    """看当前频道里已经建好的好感度档案摘要，群聊里想先弄清对象是谁时用。

    Args:
        limit (int): 最多返回几份档案。

    Returns:
        str: JSON 数组，含用户 ID、名称、评分、阶段与摘要。
    """
    state = await _open_archive(_ctx.chat_key)
    if len(state.profiles) == 0:
        return "[]"

    # 上限 20：再多对模型也没意义，只会把上下文撑满
    wanted = max(1, min(int(limit), 20))
    rows: List[Dict[str, object]] = []
    for profile in state.newest(wanted):
        rows.append(
            {
                "user_id": profile.user_id,
                "display_name": profile.display_name,
                "score": profile.score,
                "max_abs_score": _score_cap(),
                "stage": _stage_of(profile.score)[0],
                # 摘要与时间线
                "summary": profile.summary,
                "interaction_hint": profile.interaction_hint,
                "tags": profile.tags,
                "updated_at": profile.updated_at,
            },
        )
    return json.dumps(rows, ensure_ascii=False, indent=2)


# ============ 档案类命令的公共内核 ============
# 几条档案命令遵循同一套流程：取目标 ID → 读档案 → 改档案 → 落库 → 回执。
# 差别只在「改」这一步与回执文案，故把流程抽成 _favor_write_command，
# 具体动作由各命令自己的 _apply 闭包给出。

_NOT_FOUND = "当前频道不存在用户 `{}` 的好感度档案。"


async def _favor_write_command(
    context: CommandExecutionContext,
    mutate,
) -> CommandResponse:
    """档案类命令的通用执行器。

    `mutate(state)` 负责修改档案并返回成功文案；
    若它直接返回一个 CommandResponse（例如失败分支），则原样透传、不落库。
    """
    state = await _open_archive(context.chat_key)
    outcome = mutate(state)
    if isinstance(outcome, CommandResponse):
        return outcome
    await _store_archive(context.chat_key, state)
    return CmdCtl.success(outcome)


async def _cmd_status(context: CommandExecutionContext, target_user_id: str) -> CommandResponse:
    """favor_status：只读，不改档案。"""
    who = _pick_explicit_target(context, target_user_id)
    state = await _open_archive(context.chat_key)
    found = state.profiles.get(who)
    if found is None:
        return CmdCtl.failed(_NOT_FOUND.format(who))
    return CmdCtl.success(_compose_archive_detail(found))


async def _cmd_shift(
    context: CommandExecutionContext, who: str, delta: int, verb: str, sign: int,
) -> CommandResponse:
    """favor_add / favor_sub 共用：delta 必须为正，方向由 sign 决定。"""
    if delta <= 0:
        return CmdCtl.failed("delta 必须大于 0。")

    def _apply(state: ChannelArchive) -> str:
        profile = state.profile_for(who)
        step = sign * delta
        profile.nudge(step, _mystery_reason(profile, verb))
        state.last_active_user_id = who
        word = "增加" if sign > 0 else "减少"
        return _adjust_receipt(profile, f"{word} {_delta_text(step)}")

    return await _favor_write_command(context, _apply)


async def _cmd_set(
    context: CommandExecutionContext, who: str, score: int,
) -> CommandResponse:
    """favor_set：整份档案按给定分数重写，描述字段沿用原值。"""
    def _apply(state: ChannelArchive) -> str:
        profile = state.profile_for(who)
        profile.reset_to(
            score,
            f"由于神秘原因，你对{profile.display_name or profile.user_id}的好感度被重新设定了",
            summary=profile.summary or _EMPTY_SUMMARY,
            hint=profile.interaction_hint,
            tags=list(profile.tags),
            display_name=profile.display_name,
        )
        state.last_active_user_id = who
        return _adjust_receipt(profile, f"设定为 {profile.score}")

    return await _favor_write_command(context, _apply)


async def _cmd_remove(context: CommandExecutionContext, who: str) -> CommandResponse:
    """favor_remove：删档，顺带清掉「最近活跃用户」指向。"""
    def _apply(state: ChannelArchive) -> object:
        if who not in state.profiles:
            return CmdCtl.failed(_NOT_FOUND.format(who))
        gone = state.profiles.pop(who)
        if state.last_active_user_id == who:
            state.last_active_user_id = ""
        return f"已删除 {gone.display_name or gone.user_id} 的好感度档案。"

    return await _favor_write_command(context, _apply)


@plugin.mount_command(
    name="favor_status",
    description="查看本频道中某个用户的关系档案",
    aliases=["fvs"],
    permission=CommandPermission.USER,
    usage="favor_status [target_user_id]",
    category="关系管理",
)
async def favor_status_cmd(
    context: CommandExecutionContext,
    target_user_id: Annotated[str, Arg("目标用户的平台 ID；留空则用当前调用者", positional=True)] = "",
) -> CommandResponse:
    return await _cmd_status(context, target_user_id)


@plugin.mount_command(
    name="favor_rank",
    description="查看当前会话的好感度排行榜（按分值从高到低渲染为图片）",
    aliases=["查看好感度", "好感度排行", "好感榜"],
    permission=CommandPermission.USER,
    usage="favor_rank",
    category="关系管理",
)
async def favor_rank_cmd(context: CommandExecutionContext) -> CommandResponse:
    state = await _open_archive(context.chat_key)
    # 扩展：按渲染规则过滤后再判空 / 计数
    entries = [_profile_to_entry(p) for p in _rank_profiles(state)]
    if not entries:
        return CmdCtl.failed(
            "当前会话还没有可展示的好感度档案。"
            if state.profiles
            else "当前会话还没有建立任何好感度档案。",
        )
    total = len(entries)
    max_abs = _score_cap()
    png = await _render_rank_card(entries, max_abs)
    if png is None:
        return CmdCtl.success(_render_rank_text(entries, max_abs, total))

    out_dir = plugin.get_plugin_data_dir() / "rank_cards"
    out_dir.mkdir(parents=True, exist_ok=True)
    file_path = out_dir / f"favor_rank_{context.chat_key}_{int(time.time() * 1000)}.png"
    file_path.write_bytes(png)
    return CmdCtl.success(
        [
            CommandOutputSegment(
                type=CommandOutputSegmentType.TEXT,
                text=f"当前会话好感度排行榜（共 {total} 位）：",
            ),
            CommandOutputSegment(
                type=CommandOutputSegmentType.IMAGE,
                file_path=str(file_path),
            ),
        ]
    )


@plugin.mount_command(
    name="favor_add",
    description="提高指定用户在本频道的关系分",
    permission=CommandPermission.SUPER_USER,
    usage="favor_add <target_user_id> <delta>",
    category="关系管理",
)
async def favor_add_cmd(
    context: CommandExecutionContext,
    target_user_id: Annotated[str, Arg("目标用户的平台 ID", positional=True)],
    delta: Annotated[int, Arg("要增加的分值，须大于 0", positional=True)],
) -> CommandResponse:
    return await _cmd_shift(context, target_user_id, int(delta), "上升", 1)


@plugin.mount_command(
    name="favor_sub",
    description="降低指定用户在本频道的关系分",
    permission=CommandPermission.SUPER_USER,
    usage="favor_sub <target_user_id> <delta>",
    category="关系管理",
)
async def favor_sub_cmd(
    context: CommandExecutionContext,
    target_user_id: Annotated[str, Arg("目标用户的平台 ID", positional=True)],
    delta: Annotated[int, Arg("要减少的分值，须大于 0", positional=True)],
) -> CommandResponse:
    return await _cmd_shift(context, target_user_id, int(delta), "下降", -1)


@plugin.mount_command(
    name="favor_set",
    description="把指定用户在本频道的关系分设定为给定值",
    permission=CommandPermission.SUPER_USER,
    usage="favor_set <target_user_id> <score>",
    category="关系管理",
)
async def favor_set_cmd(
    context: CommandExecutionContext,
    target_user_id: Annotated[str, Arg("目标用户的平台 ID", positional=True)],
    score: Annotated[int, Arg("要设定的关系分", positional=True)],
) -> CommandResponse:
    return await _cmd_set(context, target_user_id, int(score))


@plugin.mount_command(
    name="favor_remove",
    description="删除指定用户在本频道的关系档案",
    permission=CommandPermission.SUPER_USER,
    usage="favor_remove <target_user_id>",
    category="关系管理",
)
async def favor_remove_cmd(
    context: CommandExecutionContext,
    target_user_id: Annotated[str, Arg("目标用户的平台 ID", positional=True)],
) -> CommandResponse:
    return await _cmd_remove(context, target_user_id)


@plugin.mount_on_channel_reset()
async def on_channel_reset(_ctx: schemas.AgentCtx):
    """在频道重置时清空当前频道的好感度数据（备忘与档案一起丢）"""
    await store.delete(chat_key=_ctx.chat_key, store_key=STATE_KEY)


@plugin.mount_sandbox_method(
    SandboxMethodType.AGENT,
    name="重新加载背景记忆",
    description="重新加载角色背景记忆数据（改了数据目录下的 json 后调用）。返回条目数与门控状态。",
)
async def reload_persona_memory(_ctx: schemas.AgentCtx) -> str:
    """重新加载角色背景记忆数据文件

    修改数据目录下的 tier1/*.json（常驻）、tier2/*.json（按需）、tier3/*.json（世界知识，
    按需且不受好感度门控）或 favor.json（好感度门控）后调用本方法即可生效，无需重启容器。
    数据目录＝插件数据目录下的 backgrounds/（可在 WebUI 概览页看到实际路径）。
    """
    await persona_memory.ensure_loaded(force=True)
    return "背景记忆已重载：" + persona_memory.status(tier3_cfg=_tier3_cfg())


@plugin.mount_router()
def _persona_webui_router():
    """WebUI 路由（挂在 /api/plugins/{plugin_key}/）。

    扩展：页面与 API 都在 webui.py 里，见该文件顶部说明。
    """
    from .webui import build_router

    return build_router(
        plugin,
        config,
        persona_memory,
        {
            "favor_scale_presets": FAVOR_SCALE_PRESETS,
            "favor_scale": favor_scale,
            "load_state": _open_archive,
            "save_state": _store_archive,
            "stage_info": _stage_of,
            "clamp_score": _bound_score,
            "max_abs_score": _score_cap,
            "now_ts": _ts_now,
            # 备忘录（扩展）
            "memo_cfg": _memo_cfg,
            "memo_cls": ChannelMemo,
            "memo_key": _memo_title_key,
        },
    )


@plugin.mount_sandbox_method(
    SandboxMethodType.AGENT,
    name="好感度衰减检查",
    description="立即结算一次好感度衰减（平时每 30 分钟自动结算，此方法用于手动触发/查看结果）。",
)
async def run_favorability_decay(_ctx: schemas.AgentCtx) -> str:
    """立即结算一次好感度自然衰减

    超过 DECAY_INTERVAL_HOURS 未互动的用户会按 DECAY_PERCENT 扣分（向上取整）。
    档位保底是有期限的：未互动不超过 DECAY_TIER_GRACE_HOURS 时停在当前档下界，
    之后每再过同样时长，允许的下界往下解锁一级（85→60→20→0）。
    好感度 ≤0 的用户不参与衰减（他们走回升流程）。
    """
    n = await _decay_pass()
    if not n:
        return "本次衰减结算完成：没有用户满足衰减条件。"
    return f"本次衰减结算完成：{n} 个用户的好感度发生了自然衰减。"


@plugin.mount_sandbox_method(
    SandboxMethodType.AGENT,
    name="好感度回升检查",
    description="立即结算一次好感度回升（平时每 30 分钟自动结算，此方法用于手动触发/查看结果）。",
)
async def run_favorability_recover(_ctx: schemas.AgentCtx) -> str:
    """立即结算一次好感度回升

    负好感度的用户若超过 RECOVER_INTERVAL_HOURS 未互动，会按 RECOVER_PERCENT
    加分（向上取整、每次至少 +1），**封顶 0**；好感度 ≥0 的用户不参与回升。
    """
    n = await _recover_pass()
    if not n:
        return "本次回升结算完成：没有用户满足回升条件。"
    return f"本次回升结算完成：{n} 个用户的负好感度发生了回升。"


@plugin.mount_init_method()
async def _start_decay_loop() -> None:
    """插件加载时启动好感度衰减后台任务。"""
    global _decay_task
    if not config.DECAY_ENABLED:
        plugin.logger.info("[favorability] 好感度衰减已关闭（DECAY_ENABLED=false）")
        return
    if _decay_task is not None and not _decay_task.done():
        return
    _decay_task = asyncio.create_task(_decay_loop())
    plugin.logger.info(
        f"[favorability] 好感度衰减任务已启动：每 {config.DECAY_INTERVAL_HOURS} 小时 "
        f"-{config.DECAY_PERCENT}%（保底={config.DECAY_KEEP_TIER}"
        f"/{config.DECAY_TIER_GRACE_HOURS}h 逐级解锁）"
        + (
            f"；回升已启用：每 {config.RECOVER_INTERVAL_HOURS} 小时 "
            f"+{config.RECOVER_PERCENT}%（封顶 0）"
            if config.RECOVER_ENABLED
            else "；回升已关闭"
        ),
    )


@plugin.mount_cleanup_method()
async def clean_up():
    """停掉插件自己的后台任务。"""
    global _decay_task
    pending = _decay_task
    if pending is not None and not pending.done():
        pending.cancel()
        plugin.logger.info("[favorability] 好感度衰减任务已停止")
