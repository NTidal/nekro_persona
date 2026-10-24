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
import hashlib
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
    description="三档背景记忆（常驻身份 / 按需往事 / 世界知识）+ 好感度解锁门控 + tier4 生长层（身份锚点 / 频道笔记）+ 种子/生长导出分离",
    version="2.1.3",
    author="NTidal",
    url="https://github.com/NTidal/nekro_persona",
    i18n_name=i18n.i18n_text(
        zh_CN="人格记忆",
        en_US="Persona Memory",
    ),
    i18n_description=i18n.i18n_text(
        zh_CN="三档背景记忆（常驻身份 / 按需往事 / 世界知识）+ 好感度解锁门控 + tier4 生长层（身份锚点 / 频道笔记）",
        en_US="Tiered background memory (always-on identity / on-demand past / world knowledge) with favorability gating + a tier4 growth layer (identity anchors / channel notes)",
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
    TIER1_MAX_CHARS: int = _fav_field(
        2000, "1 档单轮字符预算",
        "身份级记忆每轮直接注入，这是其硬上限；超出按 weight 降序截断（被丢弃的条目请考虑降到 2 档）。",
        en_title="Tier 1 Char Budget",
        en_desc="Hard cap for always-injected identity memories; overflow is dropped by weight",
    )
    TIER2_MAX_ENTRIES: int = _fav_field(
        2, "2 档单轮条数上限", "单轮最多注入几条「想起来的往事」",
        en_title="Tier 2 Entry Limit", en_desc="Max tier 2 entries injected per turn",
    )
    TIER2_MAX_CHARS: int = _fav_field(
        500, "2 档单轮字符预算", "单轮注入的 2 档记忆字符数上限",
        en_title="Tier 2 Char Budget", en_desc="Char budget for tier 2 injection per turn",
    )
    TIER2_SIM_THRESHOLD: float = _fav_field(
        0.35, "2 档向量阈值",
        "细节级记忆的检索阈值。原按 30 条实测标定（噪声上限 0.311 / 相关下限 0.351）；"
        "条目变多后噪声上限会上升，需要重新标定。",
        en_title="Tier 2 Similarity Threshold",
        en_desc="Retrieval threshold for tier 2; recalibrate as the library grows",
    )
    TIER2_SCAN_MSGS: int = _fav_field(
        32, "2 档扫描消息条数", "判断「是否提到」时回看多少条最近消息",
        en_title="Tier 2 Scan Depth", en_desc="Recent messages scanned to decide whether a topic came up",
    )
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
        32, "3 档扫描消息条数", "判断「是否提到」时回看多少条最近消息",
        en_title="Tier 3 Scan Depth", en_desc="Recent messages scanned to decide whether a topic came up",
    )

    # ==================== 查询文本构成（v2.0.x）====================
    # 默认全部保持旧行为（只纳入人类消息）。开启理由见 memory._recent_text 的注释：
    # 工具识别结果（如人脸识别出的角色名）落库为 SYSTEM 行，本不参与检索，
    # 而角色自己的回复里通常会复述该名字 —— 纳入 bot 回复即可救回。
    QUERY_INCLUDE_BOT: bool = _fav_field(
        False, "检索文本纳入角色自己的回复",
        "开启后角色自己的回复也参与关键词匹配。用于救回「工具识别结果」——"
        "例如人脸识别出的角色名，它落库为系统消息本不参与检索，"
        "但角色回复里通常会复述该名字，于是下一轮就能命中该角色的记忆。"
        "默认关闭（保持旧行为）。",
        en_title="Include Bot Replies in Query",
        en_desc="Let the character's own replies participate in keyword matching (recovers tool-recognized names)",
    )
    QUERY_BOT_MAX: int = _fav_field(
        2, "纳入角色回复条数上限",
        "最多纳入最近几条角色回复；用于防止「自我引用循环」占满扫描窗口。"
        "仅在上面的开关打开时生效。",
        en_title="Max Bot Replies in Query",
        en_desc="Cap on how many recent bot replies enter the query text (guards against self-reinforcement)",
    )
    QUERY_INCLUDE_TOOL_FACTS: bool = _fav_field(
        False, "检索文本纳入含触发词的系统消息",
        "开启后，系统消息（工具结果）**仅在其包含已知触发词时**才参与检索。"
        "这是白名单：好感度回执、msg_id 转储等噪声不含触发词，结构上不会被纳入。"
        "默认关闭。",
        en_title="Include Tool Facts in Query",
        en_desc="Include SYSTEM rows only when they contain a known trigger (whitelist, keeps receipts out)",
    )
    QUERY_INCLUDE_SENDER_NAME: bool = _fav_field(
        False, "检索文本纳入发送者昵称",
        "开启后每条**人类**消息前面加上发送者的群昵称，让「谁在说话」也成为检索依据 —— "
        "多人 RP 群里以角色名活动的成员发言时，本角色会更容易想起对方。"
        "⚠️ 只对人类消息生效，角色自己的名字不加（否则会自我触发）。"
        "实测该开关会新增约 3% 消息（3242/106449）的触发。默认关闭。",
        en_title="Include Sender Names in Query",
        en_desc="Prefix each human message with its sender nickname; human senders only, never the bot's own name",
    )

    # ==================== 记忆生命周期（A2/A3，默认关闭）====================
    # 开启后：2/3 档条目按「作者 weight × 热度」排序，常被想起的记忆更稳、
    # 长期冷门的在超预算时优先让位。**只影响排序与截断，绝不删除或改写作者内容**；
    # 高 weight 条目豁免；关闭时行为与不开此功能完全一致。
    LIFECYCLE_ENABLED: bool = _fav_field(
        False, "启用记忆生命周期（热度排序）",
        "开启后 2/3 档按 weight×热度 排序：常命中的记忆更稳，冷门的在超预算时优先让位。"
        "只影响排序，不删除/不改写你的内容；关闭时行为不变。",
        en_title="Enable Memory Lifecycle (heat ranking)",
        en_desc="Rank tier 2/3 by weight x heat; affects ordering only, never edits your content",
    )
    LIFECYCLE_HALF_LIFE_DAYS: int = _fav_field(
        30, "热度半衰期（天）", "多少天未命中后热度衰减到一半",
        en_title="Heat Half-life (days)", en_desc="Days until an unused memory's heat halves",
    )
    LIFECYCLE_FLOOR: float = _fav_field(
        0.6, "热度下限", "再冷也不会低于此系数（0~1）；保证冷门记忆永不彻底消失",
        en_title="Heat Floor", en_desc="Minimum heat factor; cold memories never fully vanish",
    )
    LIFECYCLE_PROTECT_WEIGHT: int = _fav_field(
        4, "豁免降权的 weight 阈值", "weight ≥ 此值的条目完全不受热度降权（保护核心设定）",
        en_title="Protect Weight", en_desc="Entries with weight >= this are exempt from heat demotion",
    )
    LIFECYCLE_REINFORCE_BONUS: int = _fav_field(
        10, "命中强化步进（×0.01）", "每次命中回补的热度（填 10 = 0.10）；常被想起的记忆回升更快",
        en_title="Reinforce Bonus (x0.01)", en_desc="Heat restored per hit (10 = 0.10)",
    )

    # ==================== 独立嵌入后端（v1.6.0，默认用 NA 内置）====================
    # 设为 custom 可让 persona 检索用独立的 OpenAI 兼容嵌入端点（自建服务、本地推理
    # 服务、各类商业 API 均可），与 NA 全局嵌入解耦。条目侧与查询侧自动同源。
    # ⚠️ 对外文案（README / WebUI / 配置项描述）一律不得出现具体厂商名与模型名——
    #    那会暴露部署方在用基础设施。示例统一用 your-endpoint.example 这类占位。
    # ⚠️ 切换嵌入模型后：向量缓存按签名自动作废重建；且 0.35 阈值必须用
    #    scripts/calibrate_threshold.py 重新标定（不同模型分数分布不同）。
    EMBED_PROVIDER: str = _fav_field(
        "na", "嵌入后端", "na=用 NekroAgent 内置嵌入；custom=用下面自配的 OpenAI 兼容端点",
        en_title="Embedding Backend",
        en_desc="na = NekroAgent built-in; custom = your own OpenAI-compatible endpoint",
    )
    EMBED_BASE_URL: str = _fav_field(
        "", "嵌入端点 Base URL",
        "custom 必填。端点根地址（OpenAI 兼容），插件会在其后拼 /embeddings",
        en_title="Embedding Base URL",
        en_desc="Required for custom; the OpenAI-compatible endpoint root URL",
    )
    EMBED_API_KEY: str = _fav_field(
        "", "嵌入 API Key", "custom 的密钥；本地 / 自建端点通常可留空或随意填一个占位值",
        en_title="Embedding API Key", en_desc="Key for custom endpoint; local servers may leave blank",
    )
    EMBED_MODEL: str = _fav_field(
        "", "嵌入模型名", "custom 必填。填你所用端点支持的嵌入模型标识，须与实际部署一致",
        en_title="Embedding Model",
        en_desc="Required for custom; must match the model served by your endpoint",
    )
    EMBED_DIMENSIONS: int = _fav_field(
        0, "嵌入维度（0=模型默认）", ">0 时显式传 dimensions，须与模型实际输出一致；留 0 由模型决定",
        en_title="Embedding Dimensions", en_desc="0 = model default; must match actual output dim",
    )
    EMBED_BATCH_SIZE: int = _fav_field(
        10, "嵌入批量大小",
        "custom 批量嵌入每次请求的文本条数。部分端点对单次条数 / 总 token 有上限，"
        "超限会返回 400（本插件会自动对半拆并重试），不确定时设小一些更高效，一般 10 是安全值",
        en_title="Embedding Batch Size",
        en_desc="Texts per custom batch request; some endpoints cap it and return 400, "
                "the plugin then splits the batch automatically",
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

    # ==================== 关系记忆（memory_log · 自动沉淀的事件史）====================
    # 与 tier4 note 的分工：note 是角色自己记的功能小抄（模型主动写），
    # memory_log 是系统自动沉淀的「事件史」——谁做了什么、发生了什么。
    # 素材来自 exec_code 里 adjust_favorability 的 reason 与 send_voice_clone 的文本，
    # **不看 delta、不看是否被规则拒绝**：严格模式下被拒的调用同样贡献素材，
    # 因为「事件发生了」与「分数涨没涨」是正交的两件事。
    MEMLOG_ENABLED: bool = _fav_field(
        True, "启用关系记忆（自动沉淀事件史）",
        "后台定期把互动归纳成事件记忆并注入对话。关闭后不沉淀也不注入，已有数据保留。",
        en_title="Enable Relationship Memory Log",
        en_desc="Periodically consolidate interactions into event memories and inject them",
    )
    MEMLOG_MODEL_GROUP: str = _fav_field(
        "", "归纳用的模型组",
        "留空则用 USE_MODEL_GROUP。该模型需要支持 JSON 输出；建议用便宜的对话模型。",
        en_title="Consolidation Model Group",
        en_desc="Model group for consolidation; empty means USE_MODEL_GROUP",
    )
    MEMLOG_BATCH_MIN_ITEMS: int = _fav_field(
        20, "攒批条数",
        "积累到多少条素材才触发一次归纳（太小会频繁调模型，太大会延迟）",
        en_title="Batch Min Items", en_desc="Consolidate after this many materials",
    )
    MEMLOG_BATCH_MAX_AGE_HOURS: float = _fav_field(
        2.0, "攒批最长等待（小时）",
        "即使条数不够，超过这个时长也会归纳一次（避免冷频道永远不触发）",
        en_title="Batch Max Age (hours)", en_desc="Force consolidation after this age",
    )
    MEMLOG_MAX_PER_SCOPE: int = _fav_field(
        200, "每频道记忆上限",
        "超出后按热度淘汰（常被注入的保留，冷门的先删）",
        en_title="Max Entries Per Channel", en_desc="Evict coldest entries beyond this",
    )
    MEMLOG_INJECT_ENABLED: bool = _fav_field(
        True, "把关系记忆注入对话",
        "关闭后只沉淀不注入（数据仍可在 WebUI 查看）",
        en_title="Inject Memory Log", en_desc="Inject consolidated memories into prompts",
    )
    MEMLOG_INJECT_MAX: int = _fav_field(
        3, "单轮注入条数", "每次对话最多注入几条关系记忆",
        en_title="Inject Max Entries", en_desc="Max memory-log entries injected per turn",
    )
    MEMLOG_INJECT_CHARS: int = _fav_field(
        700, "单轮注入字符预算", "注入关系记忆的字符上限（含标题与内容）",
        en_title="Inject Char Budget", en_desc="Char budget for memory-log injection",
    )

    # ==================== 身份锚点（tier4 生长层，阶段 A）====================
    # 前 3 档是「种子」（手工策划、模型不可写）；tier4 是「生长」——角色自己在运行时
    # 追加身份级锚点，记录「他怎么长成现在这样」。**只增不改种子**，且受硬频率闸约束
    # （审慎累积、绝不每轮都长）。空层不注入任何内容（persona_prompt 与旧版逐字节一致）。
    ANCHOR_ENABLED: bool = _fav_field(
        True, "启用身份锚点（tier4）",
        "让角色自己沉淀身份级的长期锚点（这一世形成的自我认知/重要承诺）。"
        "只增不覆盖前三档种子；无锚点时不注入任何内容。",
        en_title="Enable Identity Anchors (tier 4)",
        en_desc="Let the character grow identity-level anchors at runtime; never overwrites the tier 1-3 seeds; injects nothing when empty",
    )
    ANCHOR_MAX_TOTAL: int = _fav_field(
        15, "锚点总量上限", "全局最多保留几条锚点；达上限后新写入被拒绝，需先在 WebUI 清理",
        en_title="Anchor Total Cap", en_desc="Global maximum number of anchors; further writes are rejected once reached",
        ge=1, le=500,
    )
    ANCHOR_MAX_PER_DAY: int = _fav_field(
        1, "每日锚点上限", "一天最多新增几条锚点（硬频率闸之一，防「每轮都长」）",
        en_title="Anchors Per Day", en_desc="Max new anchors per day (a hard gate against growing every turn)",
        ge=0, le=50,
    )
    ANCHOR_MIN_INTERVAL_HOURS: int = _fav_field(
        6, "两次锚点最小间隔（小时）", "距上一条锚点写入不足该时长则拒绝（另一道频率闸）",
        en_title="Min Anchor Interval (hours)", en_desc="Reject a write sooner than this after the last anchor",
        ge=0, le=168,
    )
    ANCHOR_MAX_CONTENT_CHARS: int = _fav_field(
        200, "单条锚点内容上限", "写入时强制单行并截断；锚点应是凝练的自我认知，不是长叙事",
        en_title="Anchor Content Limit", en_desc="Forced single-line + truncated on write; an anchor is a concise self-belief, not a long story",
        ge=20, le=2000,
    )
    ANCHOR_INJECT_MAX: int = _fav_field(
        8, "锚点单轮条数上限", "一轮最多注入几条锚点",
        en_title="Anchor Inject Count", en_desc="Max anchors injected into the prompt per turn",
        ge=0, le=50,
    )
    ANCHOR_INJECT_CHARS: int = _fav_field(
        1200, "锚点单轮字符预算", "一轮注入锚点的总字符上限；按 salience 单调填充（核心锚点永不因低档挤占被牺牲）",
        en_title="Anchor Char Budget", en_desc="Total anchor chars per turn; filled by salience-monotonic greedy",
        ge=0, le=8000,
    )
    ANCHOR_SALIENCE_MAX: int = _fav_field(
        3, "锚点核心度上限", "salience 的取值上界（1..此值）；核心度越高越优先常驻注入",
        en_title="Max Salience", en_desc="Upper bound of an anchor's salience (1..this); higher ranks stay resident first",
        ge=1, le=5,
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

    # ==================== 频道重置行为 ====================
    # ⚠️ 1.9.0 在加入 tier4 生长层时把这项连同备份函数一起删掉，退回了「无条件
    #    store.delete」的旧行为 —— 一次「重置上下文」会静默清空本频道全部关系
    #    档案，不可逆且无提示。1.4.4/1.4.5 曾修好，1.9.0 回归，此处补回。
    RESET_CHANNEL_CLEARS_FAVOR: bool = _fav_field(
        False, "「重置上下文」时清空本频道好感度",
        "关闭（默认）：重置上下文只清对话历史，关系档案与备忘**完整保留**。"
        "开启：模拟旧行为，把本频道的全部关系档案与备忘一起删除（不可逆）。"
        "注意旧版本默认就是删除，且不会提示——若你曾被「重置上下文」清掉过关系数据，"
        "保持关闭即可。无论开关如何，删除前都会先自动备份一份，见 "
        "{插件数据目录}/reset_backups/。",
        en_title="Reset Channel Clears Favorability",
        en_desc="Off (default): resetting context keeps all relationship archives and memos. "
                "On: reproduces the legacy behavior of deleting them (irreversible). "
                "A backup is always written before any deletion.",
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
from . import tier4  # noqa: E402  # tier4 生长层（身份锚点，阶段 A）
from . import consolidator as persona_consolidator  # noqa: E402  # 关系记忆归纳
from . import memory_log as persona_memlog  # noqa: E402  # 关系记忆存储

# 背景记忆数据目录：**按访问解析**（多账号隔离）。
# ⚠️ 绝不能在这里写下 `plugin.get_plugin_data_dir() / "backgrounds"` 的求值结果：
# 模块级求值发生在导入期，那时没有账号上下文，会把路径永久固定到共享目录，
# 于是所有账号都读写同一份人格数据。这里只注册「提供者」，
# memory/tier4 每次访问时自行调用，从而跟随当前账号上下文解析。
persona_memory.configure_bg_dir_provider(lambda: plugin.get_plugin_data_dir() / "backgrounds")
# tier4 生长层与阶梯记忆同目录（tier4.json / _channel_index.json 落到同一 backgrounds/）
tier4.configure_bg_dir_provider(lambda: plugin.get_plugin_data_dir() / "backgrounds")
# 关系记忆与生长层同目录（memory_log.json 落到同一 backgrounds/）
persona_memlog.configure_bg_dir_provider(lambda: plugin.get_plugin_data_dir() / "backgrounds")
# v2.0.2：目录状态缓存改为**有界 LRU**（每个账号目录约 20MB 常驻向量，旧实现只增不减）。
# 这里只做一次初始化：取环境变量 NEKRO_PERSONA_STATE_MAX_DIRS，没有就用 memory.py 的
# 默认值（8）。**刻意不新增插件配置项**——那会让 NA 在加载时回写 config.yaml，
# 与「旧配置零迁移」相冲突；要调就用容器环境变量。
try:
    _state_max_dirs = persona_memory.configure_state_cache()
    plugin.logger.info(f"[persona] 目录状态缓存上限 = {_state_max_dirs}（0 表示不限制）")
except Exception as _exc:  # noqa: BLE001
    plugin.logger.warning(f"[persona] 初始化目录状态缓存上限失败（沿用默认值）: {_exc}")
# v1.6.0：注入独立嵌入后端配置提供者（lambda 延迟解析 _embed_cfg，因其定义在后）。
# memory.py 每次嵌入实时读取，故 WebUI 改 EMBED_* 后无需重启即生效。
persona_memory.set_embed_config_provider(lambda: _embed_cfg())
# v2.0.x：注入「查询文本构成」配置提供者（同样是 lambda 延迟解析，WebUI 改完即时生效）。
# 决定 _recent_text 是否纳入 bot 回复 / 含触发词的系统消息，见 memory._recent_text。
persona_memory.set_query_config_provider(lambda: {
    "include_bot": bool(getattr(config, "QUERY_INCLUDE_BOT", False)),
    "bot_max": int(getattr(config, "QUERY_BOT_MAX", 2) or 0),
    "include_tool_facts": bool(getattr(config, "QUERY_INCLUDE_TOOL_FACTS", False)),
    "include_sender_name": bool(getattr(config, "QUERY_INCLUDE_SENDER_NAME", False)),
})


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


# ===== 频道笔记（tier4 note）配置与引导（阶段 B：接替旧 memo 子系统）=====
# 原 _memo_* 的查找/去重/相似度/渲染逻辑已全部迁入 tier4.py（note kind），
# 这里只留「配置收集」与「注入引导文案」。配置键沿用 MEMO_*（§6.2 的「保留旧名」
# 兼容选项），避免重命名 config.yaml 里已存的键而重置用户调过的值。
def _note_cfg() -> dict:
    """频道笔记运行参数（沿用 MEMO_* 配置键，作为 tier4 note 层的配置源）。"""
    return {
        "enabled": bool(getattr(config, "MEMO_ENABLED", True)),
        "max_items": int(getattr(config, "MEMO_MAX_ITEMS", 80) or 80),
        "max_content": int(getattr(config, "MEMO_MAX_CONTENT_CHARS", 400) or 400),
        "inject_recent": int(getattr(config, "MEMO_INJECT_RECENT", 2) or 0),
        "inject_matched": int(getattr(config, "MEMO_INJECT_MATCHED", 3) or 0),
        "inject_chars": int(getattr(config, "MEMO_INJECT_CHARS", 900) or 900),
        "dedup_min_len": max(2, int(getattr(config, "MEMO_DEDUP_MIN_KEY_LEN", 2) or 2)),
        "guide_always": bool(getattr(config, "MEMO_GUIDE_ALWAYS", True)),
    }


# 笔记引导 —— 常驻注入，明确「什么时候该记」。旧版教训：没有它时，模型只看到
# save_note 这个名字，几乎不会主动用（实测 4763 次代码执行里只用过 1 次）。
_NOTE_GUIDE = (
    "#你的小本本\n"
    "你有一本自己的小本本（`save_note`）。**约定、承诺、称呼偏好、"
    "以及以后还用得上的稳定事实**，想到就顺手记下来——别指望事后还想得起来。\n"
    "使用规则：\n"
    "1. 需要时自然用出来即可，**绝不要把笔记原文当消息发给用户**。\n"
    "2. 没提到的那些就是这轮用不上，不要硬提。"
)


# 锚点注入时的标题与用法约束。
# 为什么必须有：锚点是 scope=global 的，会出现在每个频道的 prompt 里，
# 而早先注入的是裸 bullet（连标题都没有），模型无从判断「这是全局立场」
# 还是「这是对当前对话者的承诺」，很容易把锚点里的「你」绑到当前群友身上。
_ANCHOR_GUIDE = (
    "#你对自己定下的\n"
    "以下是你这些日子里对自己想明白的事——是你这个人本身，不是谁的要求。\n"
    "使用规则：\n"
    "1. **它们与具体某个人无关**，换到任何地方都成立。不要说成「我答应过你」，"
    "也不要当成对当前对话者的约定去兑现。\n"
    "2. 自然地活出来，不要复述原文，也不要说「我记得我写过这条」。"
)

# 锚点禁写模式：绑定具体对象/频道的内容不该进全局身份层。
# 历史教训：只靠工具说明软约束 + 4 个独占词替换，实测挡不住
# 「我答应过你，不管你掉到多少…」这类写法 —— 一旦沉淀成锚点，
# 那个「你」会在别的频道被重新绑定，直接穿帮。
_ANCHOR_REJECT_PATTERNS = (
    (re.compile(r"web[_-]?user[_-]?\w*|admin_\w+|onebot_v11", re.I), "具体账号标识"),
    (re.compile(r"\d{6,}"), "具体账号或群号"),
    (
        re.compile(r"我(?:答应|承诺|保证|许)过?你|对你(?:答应|承诺|保证|许)|我向你(?:答应|承诺)"),
        "针对某个人的承诺",
    ),
)


def _anchor_cfg() -> dict:
    """tier4 身份锚点运行参数（来自插件配置）。

    键名与 tier4.py 的 cfg 约定对齐：
    enabled/max_total/max_per_day/min_interval_hours/max_content 供频率闸，
    inject_chars/inject_max/guide_always 供注入渲染，salience_max 供钳制。
    """
    return {
        "enabled": bool(getattr(config, "ANCHOR_ENABLED", True)),
        "max_total": max(1, int(getattr(config, "ANCHOR_MAX_TOTAL", 15) or 15)),
        "max_per_day": max(0, int(getattr(config, "ANCHOR_MAX_PER_DAY", 1) or 0)),
        "min_interval_hours": max(0, int(getattr(config, "ANCHOR_MIN_INTERVAL_HOURS", 6) or 0)),
        "max_content": max(20, int(getattr(config, "ANCHOR_MAX_CONTENT_CHARS", 200) or 200)),
        "inject_max": max(0, int(getattr(config, "ANCHOR_INJECT_MAX", 8) or 0)),
        "inject_chars": max(0, int(getattr(config, "ANCHOR_INJECT_CHARS", 1200) or 0)),
        "salience_max": max(1, int(getattr(config, "ANCHOR_SALIENCE_MAX", 3) or 3)),
        # 阶段 A 不常驻注入锚点引导（保证空层时 persona_prompt 逐字节不变）；
        # 模型经工具描述发现 anchor_memory。阶段 B 再合并 note 引导。
        "guide_always": False,
    }


def _tier1_cfg() -> dict:
    """从插件配置收集 1 档运行参数。"""
    return {
        "max_chars": int(getattr(config, "TIER1_MAX_CHARS", 2000) or 2000),
    }


def _lifecycle_cfg() -> dict:
    """从插件配置收集生命周期（热度排序）参数。默认 enabled=False。

    ⚠️ 用「仅 None 才回落默认」而非 `x or default`：否则作者显式设的
    LIFECYCLE_FLOOR=0 / PROTECT_WEIGHT=0 / REINFORCE_BONUS=0 会被静默吞成默认值，
    且与概览页显示、memory.lifecycle_settings 运行期口径不一致。
    """
    def _n(key: str, default, cast):
        v = getattr(config, key, None)
        return cast(default) if v is None else cast(v)

    return {
        "enabled": bool(getattr(config, "LIFECYCLE_ENABLED", False)),
        "half_life_days": _n("LIFECYCLE_HALF_LIFE_DAYS", 30, int),
        "floor": _n("LIFECYCLE_FLOOR", 0.6, float),
        "protect_weight": _n("LIFECYCLE_PROTECT_WEIGHT", 4, int),
        # 配置项以整数（×0.01）存，避免浮点输入框的精度/校验麻烦
        "reinforce_bonus": _n("LIFECYCLE_REINFORCE_BONUS", 10, int) / 100.0,
    }


def _embed_cfg() -> dict:
    """从插件配置收集独立嵌入后端参数（v1.6.0）。

    每次实时读取 config，故 WebUI 改配置后无需重启即生效（向量缓存会因签名变化自动重建）。
    provider != custom、或 custom 但 base_url/model 不全时，memory 侧自动回落 NA 内置。
    """
    return {
        "provider": str(getattr(config, "EMBED_PROVIDER", "na") or "na").strip().lower(),
        "base_url": str(getattr(config, "EMBED_BASE_URL", "") or "").strip(),
        "api_key": str(getattr(config, "EMBED_API_KEY", "") or "").strip(),
        "model": str(getattr(config, "EMBED_MODEL", "") or "").strip(),
        "dimensions": int(getattr(config, "EMBED_DIMENSIONS", 0) or 0),
        "batch_size": max(1, int(getattr(config, "EMBED_BATCH_SIZE", 10) or 10)),
        "timeout": 30,
    }


def _tier2_cfg() -> dict:
    """从插件配置收集 2 档运行参数。"""
    return {
        "max_entries": int(getattr(config, "TIER2_MAX_ENTRIES", 2) or 2),
        "max_chars": int(getattr(config, "TIER2_MAX_CHARS", 500) or 500),
        "threshold": float(getattr(config, "TIER2_SIM_THRESHOLD", 0.35) or 0.35),
        "scan_msgs": int(getattr(config, "TIER2_SCAN_MSGS", 32) or 32),
        "lifecycle": _lifecycle_cfg(),
    }


def _tier3_cfg() -> dict:
    """从插件配置收集 3 档运行参数（扩展）。"""
    return {
        "enabled": bool(getattr(config, "TIER3_ENABLED", True)),
        "max_entries": int(getattr(config, "TIER3_MAX_ENTRIES", 3) or 3),
        "max_chars": int(getattr(config, "TIER3_MAX_CHARS", 700) or 700),
        "threshold": float(getattr(config, "TIER3_SIM_THRESHOLD", 0.35) or 0.35),
        "scan_msgs": int(getattr(config, "TIER3_SCAN_MSGS", 32) or 32),
        "lifecycle": _lifecycle_cfg(),
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

# 反向语义覆盖（v1.9.0 补丁）：命中任一则**不能**靠 _STRONG_HINTS 放行。
#
# 背景：_STRONG_HINTS 是子串匹配，单字词会命中含该字但语义相反的词——
#   "陪" → 陪衬/陪跑；"约" → 大约/契约；"写" → 写作业/写本子；
#   "笑" → 我笑了/他笑了两声就走；"帮" → 帮倒忙/没帮上什么。
# 这些 reason 描述的是**无关事件或负面事件**，不该因含"证据字"而放行。
#
# 对 593 条真实 reason 的实测：当前数据流里 0 条受影响（模型写 reason 时
# 天然在描述关系事件，97% 是 ≥20 字的多词堆叠文本），所以本清单是**纯防御**，
# 不改变现有行为；它防的是未来某条「帮倒忙」式 reason 被错误放行。
#
# 维护约定：这里只收**含强证据字但语义相反或无关**的组合词。
# 新增时先跑一遍真实 reason 样本确认 0 命中（否则说明它已在新语境下有正当用法）。
_NEGATIVE_OVERRIDE = (
    "陪衬", "陪跑", "陪绑",
    "大约", "约摸", "约等于", "契约",
    "帮倒忙", "没帮上", "帮不了",
    "写作业", "写作", "写了本子",
    "笑了两声", "我笑了", "笑话书",
    "救场失败", "没救成",
    "哄不好", "越哄越",
    "逗闷子失败",
    "陪着玩而已",
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
    # 反向语义优先于强证据词：含「陪衬/帮倒忙」这类词时，即使也含"陪/帮"
    # 这些证据字，也不能视为正向关系事件（见 _NEGATIVE_OVERRIDE 注释）。
    if any(w in r for w in _NEGATIVE_OVERRIDE):
        # 不直接判空泛——继续走后面的长度/笼统语兜底，与未命中 strong 同路径
        pass
    elif any(w in r for w in _STRONG_HINTS):
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
        async with await _archive_lock(chat_key):
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
                # ⚠️ 「未互动时长」必须从**最后互动时间**起算，不能从 anchor 起算：
                #    anchor 含 last_decay_at，而每次结算（哪怕一分没扣）都会把它推到 ≈现在，
                #    于是 idle_hours 永远只有一个衰减周期（24h）上下，小于档位保底时长
                #    （168h），_decay_floor 的 drops 恒为 0 —— 「超时后逐级解锁」永不生效。
                idle_since = int(prof.last_interaction_at or 0) or anchor
                idle_hours = max(0.0, (now - idle_since) / 3600.0)
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
        async with await _archive_lock(chat_key):
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
_memlog_task: "asyncio.Task | None" = None


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


OBSERVATION_KIND = "observation"
"""冷却期内的「观察」记录（ArchiveEvent.kind 值）。

与 manual 的区别：不改分（delta 恒 0），也不进「最近关系变化」列表，
单独展示在关系卡的「冷却期内的观察」段。用途有两个：
  ① 让模型在不能改分时也能表达「我注意到了什么」，而不是被规则按住不动；
  ② 这些 reason 会随 exec_code 落库，成为关系记忆（memory_log）的素材。
"""


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
    # ---- 每日预算累计器（v1.4.2）----
    # 与 recent_events 解耦：日限/间隔不再从「最近 8 条事件」反推
    # （高频调整下旧事件被挤出窗口会导致统计失真，且靠 reason 字符串剔除机械事件太脆）。
    # day_key   = 当日 0 点时间戳（_day_start_ts），跨天自动归零
    # day_net   = 当日真实调整的净分值（带符号），机械结算与描述-only 改动不计入
    # last_adjust_at = 最近一次真实调分的时间戳，用于最小间隔判定
    day_key: int = 0
    day_net: int = 0
    last_adjust_at: int = 0
    # ---- 冷却期观察（v2.0.7）----
    # 单独一个列表，**不占用 recent_events 的 MAX_EVENT_HISTORY 槽位**：
    # 观察写入频率可能高于真实调分，混在 recent_events 里会把真实变化挤掉。
    observations: List["ArchiveEvent"] = field(default_factory=list)

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
            day_key=int(data.get("day_key") or 0),
            day_net=int(data.get("day_net") or 0),
            last_adjust_at=int(data.get("last_adjust_at") or 0),
        )
        item.recent_events = [
            ArchiveEvent.from_raw(x) for x in (data.get("recent_events") or [])
        ]
        item.observations = [
            ArchiveEvent.from_raw(x) for x in (data.get("observations") or [])
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
            "day_key": self.day_key,
            "day_net": self.day_net,
            "last_adjust_at": self.last_adjust_at,
            "observations": [e.to_raw() for e in self.observations],
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

    def roll_day(self, now: int) -> None:
        """跨天则把当日累计归零。每次读取 day_net 前先调用。"""
        dk = _day_start_ts(int(now))
        if self.day_key != dk:
            self.day_key = dk
            self.day_net = 0

    def record_adjust(self, delta: int, now: int) -> None:
        """记录一次**真实调分**：刷新间隔锚点与当日净累计。

        只有真正改了分数才调用（delta != 0）；机械结算（衰减/回升）与
        描述-only 改动都不走这里，因此不会污染日预算，也不受 reason 文案影响。
        """
        delta = int(delta)
        if delta == 0:
            return
        self.last_adjust_at = int(now)
        self.roll_day(now)
        self.day_net += delta

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


@dataclass
class ChannelArchive:
    """一个频道里的全部关系档案。

    JSON 契约：`{"profiles": {...}, "last_active_user_id": "...", "memos_migrated": bool}`
    —— 属性名改成了 archives，落库键仍是 profiles。
    阶段 B 起 `memos` 不再由本类持有：备忘已迁入 tier4 note（`backgrounds/tier4.json`），
    首载时由 `_migrate_memos_on_load()` 直接读原始 blob 一次性迁移，迁移后清空并打
    `memos_migrated` 标记（保证幂等、重跑不重复迁）。
    """

    archives: Dict[str, UserArchive] = field(default_factory=dict)
    last_active_user_id: str = ""
    memos_migrated: bool = False

    @staticmethod
    def from_raw(raw: object) -> "ChannelArchive":
        data = raw if isinstance(raw, dict) else {}
        state = ChannelArchive()
        for uid, item in (data.get("profiles") or {}).items():
            state.archives[str(uid)] = UserArchive.from_raw(str(uid), item)
        state.last_active_user_id = str(data.get("last_active_user_id") or "")
        state.memos_migrated = bool(data.get("memos_migrated") or False)
        return state

    def to_raw(self) -> Dict[str, object]:
        out: Dict[str, object] = {
            "profiles": {uid: item.to_raw() for uid, item in self.archives.items()},
            "last_active_user_id": self.last_active_user_id,
        }
        if self.memos_migrated:
            out["memos_migrated"] = True
        return out

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


# 记录「哪些频道的坏 blob 已经备份过」（chat_key → 内容摘要）。
# 衰减/回升循环每 30 分钟都会读到同一份坏档案，无脑备份会让 store 慢慢堆垃圾。
_CORRUPT_BACKED: Dict[str, str] = {}


async def _open_archive(chat_key: str) -> ChannelArchive:
    """读频道档案。解析失败时告警并返回空档案，不让一轮对话因此崩掉。"""
    blob = await store.get(chat_key=chat_key, store_key=STATE_KEY)
    if not blob:
        return ChannelArchive()
    try:
        return ChannelArchive.from_raw(json.loads(blob))
    except Exception as exc:  # noqa: BLE001
        # ⚠️ 返回空档案本身没问题（不让一轮对话因此崩），但**必须先把原始 blob 备份**：
        #    否则下一次 _store_archive 会用这份空档案把原始数据整块覆盖掉，且无备份。
        # 同一份坏 blob 只备份一次：衰减/回升循环每 30 分钟都会读到它，
        # 无脑备份会让 PluginStore 慢慢堆积重复的 corrupt blob。
        digest = hashlib.sha256(blob.encode("utf-8", "ignore")).hexdigest()[:16]
        if _CORRUPT_BACKED.get(chat_key) == digest:
            where = "此前已备份过同一份损坏内容"
        else:
            bak_key = f"{STATE_KEY}.corrupt_{int(_ts_now())}"
            try:
                await store.set(chat_key=chat_key, store_key=bak_key, value=blob)
                _CORRUPT_BACKED[chat_key] = digest
                where = f"已备份到 store_key={bak_key}"
            except Exception as bexc:  # noqa: BLE001
                where = f"备份失败（{bexc}）"
        plugin.logger.error(
            f"[存档] 频道状态解析失败（{type(exc).__name__}: {exc}），"
            f"{where}；本次按空档案继续: {chat_key}")
        return ChannelArchive()


async def _store_archive(chat_key: str, state: ChannelArchive) -> None:
    """写回频道档案。"""
    await store.set(chat_key=chat_key, store_key=STATE_KEY,
                    value=json.dumps(state.to_raw(), ensure_ascii=False))
    # 为阶段 C 的生长包导出铺路：把见过的频道去重记入自维护索引
    # （PluginStore 不能枚举 chat_key）。best-effort，失败绝不影响本次写档。
    try:
        await tier4.note_channel(chat_key)
    except Exception as e:  # noqa: BLE001
        plugin.logger.debug(f"[persona] 频道索引追加失败（忽略）: {e}")


# ---- 频道档案并发锁（v1.4.2）----
# _open_archive → 改 → _store_archive 是整 blob 的 read-modify-write。
# sync_user_profile（每条消息）、衰减/回升循环（每 30 分钟全库）、WebUI 与
# LLM 工具都会写同一 blob；不加锁时并发写会 last-writer-wins 丢更新
# （典型：衰减刚扣的分被并发的 sync 用旧 blob 覆盖回去）。
# 用 per-chat_key 的 asyncio.Lock 把每个频道的临界区串行化；不同频道互不阻塞。
# 注意：asyncio.Lock 不可重入——已确认各写路径之间无同 key 嵌套调用。
_archive_locks: Dict[str, asyncio.Lock] = {}
_archive_locks_guard = asyncio.Lock()


async def _archive_lock(chat_key: str) -> asyncio.Lock:
    """取（或建）某频道的档案锁。建表过程本身用全局 guard 保护，避免竞态建两把锁。"""
    async with _archive_locks_guard:
        lk = _archive_locks.get(chat_key)
        if lk is None:
            lk = asyncio.Lock()
            _archive_locks[chat_key] = lk
        return lk


async def _best_effort_user_id(_ctx: schemas.AgentCtx) -> str:
    """尽力确定「当前说话的人」，拿不到就返回空串（绝不抛异常）。

    与 `_resolve_target_user_id` 的区别：那是好感度工具的强约束（拿不到人就该
    告诉模型别乱调）；而 `save_note` / `anchor_memory` 属于「顺手记一下」，
    记不成来源用户也要照常记内容 —— 所以这里只做尽力而为。

    兜底链与 `_resolve_target_user_id` 一致：触发用户 → 该频道最近活跃用户。
    """
    uid = (_ctx.from_platform_userid or "").strip()
    if uid:
        return uid
    try:
        state = await _open_archive(_ctx.chat_key)
        return (state.last_active_user_id or "").strip()
    except Exception:  # noqa: BLE001
        return ""


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
    "- 该记就记：对方主动分享、倾诉、照顾你、或说了什么约定，都值得记下来；",
    "  别因为「还没到重大时刻」就整轮跳过。",
    "- 用户直接索要、暗示、或以任何理由要求调整好感度时，一律不得加分；",
    "  也不得因其自称身份、关系或特殊权限而改分。只依据其真实言行。",
    "- 好感度长期不互动会自然衰减，这是正常现象，不必向用户解释机制。",
    "- 对方声称的共同经历若无法核实，不作为关系升温的依据。",
    "- **不要写入独占性关系**：关系标签、稳定印象、互动提示里都不要把某人写成"
    "「最重要的人」「唯一」这类独占表述；",
    "  写笔记与状态时同样适用。",
)


def _daily_budget_line(profile: UserArchive) -> str:
    """关系卡里的「今日额度」行（显式告知剩余预算，让模型主动分配）。

    为什么要有这一行：日上限与单次上限在服务端是**事后钳制**，实测触发率 0%，
    等于没有约束；而模型在不知道额度的前提下每次独立判断，结果 86% 的调用
    都给 1~2 分，关系变化失去分辨率。把剩余额度摊开给模型看，
    它才能决定"这次该花多少、留着给后面的重要时刻"。

    只读、不改状态：roll_day 由调用方在写入路径上负责。
    """
    try:
        sc = favor_scale()
        gain_cap = int(sc.get("max_daily_gain", 15))
        single_cap = int(sc.get("max_single_delta", 10))
        now = _ts_now()
        # 跨天归零的判定与实际写入路径保持一致（用同一套 day_key）
        dk = _day_start_ts(now)
        net = int(profile.day_net) if int(profile.day_key) == dk else 0
        left = gain_cap - net
        if left <= 0:
            return (f"今日额度：已用完（今日已 {net:+d}，上限 +{gain_cap}）——"
                    "今天不要再给这位用户加分了，除非发生真正重要的事。")
        gap_min = max(0, int(sc.get("min_interval_minutes", 120)))
        parts = [f"今日额度：还剩 +{left}（今日已 {net:+d}／上限 +{gain_cap}，"
                 f"单次最多 +{single_cap}）"]
        if profile.last_adjust_at:
            # ⚠️ 用分钟直接算，不要折算成小时再 ×3600：
            #    宽松档 min_interval_minutes=15，15//60=0 会让判定永远通过。
            elapsed_min = max(0, (now - int(profile.last_adjust_at)) // 60)
            wait_min = gap_min - elapsed_min
            if wait_min > 0:
                parts.append(
                    f"距上次调整 {elapsed_min} 分钟，还需 {wait_min} 分钟才可改分"
                    "（期间仍可用「调整好感度」记录本轮观察：只留档、不改分）"
                )
            else:
                parts.append("间隔已满足，可以改分")
        return "；".join(parts) + "。"
    except Exception:  # noqa: BLE001
        return ""


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
    # 今日额度：让模型「主动花预算」而不是「给出后被砍」。
    # 实测教训：旧版不给剩余额度，模型每次独立判断都给 1~2，
    # 而日上限/单次上限实际上 0 次触发 —— 护栏形同虚设。
    # 把那两个数字显式告诉模型，它才能在一天内合理分配。
    _budget_line = _daily_budget_line(profile)
    if _budget_line:
        head.append(_budget_line)
    if custom_hint:
        head.append(f"个人互动提示：{custom_hint}")
    body = [
        f"阶段指引（{stage_label}）：{_stage_guide_text(profile.score)}",
        # 分量锚点（v2.0.8）：实测两个模型在孤立提问下都能正确分档，
        # 但在完整运行上下文里会塌缩成「一律 +1」。把锚点放进每轮都可见的
        # 关系卡，而不是只放在可能被略读的工具说明里。
        # 每个档位给**确定数字**而不是区间：实测给「3~5」这种区间时模型会取
        # 下界，结果所有互动都塌缩成同一个值（先是全 1，后来全 2）。
        "分量参考（选最贴近的一条，给对应数字；别一律判 1，也别一律判 2）："
        "寒暄/接梗/发表情包/无信息量的一两句=1；"
        "主动分享自己的日常、主动来找你说话=2；"
        "深夜特意来倾诉、明显关心你的身体或情绪=3；"
        "聊得比平时深入、说出平常不会说的事=4；"
        "花时间帮你做事、为你付出可见成本=5；"
        "袒露压在心里的心事、在冲突里明确站你这边、长期承诺=6~8",
        f"关系标签：{', '.join(profile.tags) if profile.tags else '无'}",
        "最近关系变化：",
        *event_lines,
    ]
    # 冷却期内的观察：让模型看到「我记过什么」，形成正反馈（v2.0.7）
    _obs = [e for e in (profile.observations or [])
            if str(getattr(e, "kind", "") or "") == OBSERVATION_KIND][-2:]
    if _obs:
        body.append("冷却期内的观察（已留档，未改分）：")
        body.extend(f"- {e.reason}（{_ago_text(e.timestamp)}）" for e in _obs)
    body.extend(["规则：", *_CARD_RULES])
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

    例: onebot_v11-group_xxx -> ("group", "xxx")
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

    async with await _archive_lock(message.chat_key):
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

    # 登记「本账号见过这个频道」，供关系记忆的素材扫描做归属过滤
    try:
        _memlog_touch_channel(_ctx.chat_key)
    except Exception:  # noqa: BLE001
        pass

    # ① 关系卡（原好感度插件逻辑，保持原样）
    state = await _open_archive(_ctx.chat_key)
    current_user_id = (_ctx.from_platform_userid or state.last_active_user_id or "").strip()
    profile = state.profiles.get(current_user_id) if current_user_id else None
    if profile is not None:
        parts.append(_compose_archive_card(profile, current=True))
    else:
        parts.append(_compose_overview_card(state))

    # ② 阶梯记忆（按好感度门控披露深度）
    # favor.json 损坏时 load_favor_cfg 会 raise（fail-closed 是对的：不能拿坏配置
    # 当空配置继续），但这里是 persona_prompt 的主链路 —— 不兜底就会让**每一轮**注入
    # 都炸。降级为「本轮不启用门控」，把故障留在日志里。
    try:
        favor_cfg = persona_memory.load_favor_cfg()
    except Exception as exc:  # noqa: BLE001
        plugin.logger.error(f"[persona] favor.json 不可用，本轮按不门控处理: {exc}")
        favor_cfg = {}
    gating = bool(favor_cfg) and bool(favor_cfg.get("enabled", True))
    score = int(profile.score) if profile is not None else 0
    try:
        block = await persona_memory.render_memory_block(
            _ctx, score, gating=gating,
            tier1_cfg=_tier1_cfg(), tier2_cfg=_tier2_cfg(), tier3_cfg=_tier3_cfg(),
        )
        if block:
            parts.append(block)
    except Exception as e:  # noqa: BLE001
        plugin.logger.warning(f"[persona] 记忆块渲染失败，本轮跳过: {e}")

    # ③ tier4 生长层：身份锚点（anchor · 全局）+ 频道笔记（note · 原备忘录）
    # 阶段 B：note 接替旧 memo 子系统；anchor 在前、note 在后（§9.3）。只读、不写。
    try:
        recent_text = await persona_memory._recent_text(_ctx)
        doc = await tier4.open_tier4()
        anchor_cfg = _anchor_cfg()
        note_cfg = _note_cfg()
        sub: List[str] = []
        if anchor_cfg["enabled"]:
            ab = tier4.render_anchor_block(doc, anchor_cfg, _ANCHOR_GUIDE)
            if ab:
                sub.append(ab)
        if note_cfg["enabled"]:
            nb = tier4.render_note_block(
                doc, note_cfg, _ctx.chat_key, score, recent_text, _NOTE_GUIDE,
            )
            if nb:
                sub.append(nb)
        if sub:
            parts.append("\n\n".join(sub))
            n_live = sum(
                1 for e in doc.entries
                if e.kind == tier4.NOTE_KIND and e.scope == _ctx.chat_key and not e.is_expired()
            )
            plugin.logger.info(
                f"[persona] tier4 注入：锚点 {sum(1 for e in doc.entries if e.kind == tier4.ANCHOR_KIND)} 条 / "
                f"笔记 {n_live} 条可用",
            )
    except Exception as e:  # noqa: BLE001
        plugin.logger.warning(f"[persona] tier4 块渲染失败，本轮跳过: {e}")

    # ④ 关系记忆（memory_log · 系统自动沉淀的事件史）
    # 与 ③ 的 tier4 note 分工不同：note 是角色自己记的功能小抄（模型主动写），
    # 这里是后台从互动里归纳出来的「发生过什么」，模型不参与写入。
    try:
        if (bool(getattr(config, "MEMLOG_ENABLED", True))
                and bool(getattr(config, "MEMLOG_INJECT_ENABLED", True))):
            entries = await persona_memlog.list_entries(_ctx.chat_key)
            if entries:
                _now = time.time()
                # 按热度排序：常被想起的优先，热度相同则新的优先
                entries.sort(
                    key=lambda e: (persona_memlog.heat_factor(e, _now),
                                   e.event_at or e.created_at),
                    reverse=True,
                )
                max_n = max(1, int(getattr(config, "MEMLOG_INJECT_MAX", 3) or 3))
                budget = max(100, int(getattr(config, "MEMLOG_INJECT_CHARS", 700) or 700))
                head = "【关系记忆 · 你记得发生过的事】"
                picked = []
                used = len(head)
                for e in entries:
                    line = f"- {e.title}：{e.content}"
                    if picked and used + len(line) + 1 > budget:
                        break
                    picked.append(e)
                    used += len(line) + 1
                    if len(picked) >= max_n:
                        break
                if picked:
                    body = "\n".join(f"- {e.title}：{e.content}" for e in picked)
                    parts.append(head + "\n" + body)
                    # 命中即强化热度；失败不影响本轮注入
                    try:
                        await persona_memlog.bump_hits(picked)
                    except Exception:  # noqa: BLE001
                        pass
    except Exception as e:  # noqa: BLE001
        plugin.logger.warning(f"[persona] 关系记忆注入失败，本轮跳过: {e}")

    return "\n\n".join(p for p in parts if p)


@plugin.mount_sandbox_method(
    SandboxMethodType.BEHAVIOR,
    name="调整好感度",
    description="依据某个用户在本频道的具体言行增减其关系分，并留下变动记录。",
)
async def adjust_favorability(
    _ctx: schemas.AgentCtx,
    delta: int = 0,
    reason: str = "",
    summary: str = "",
    interaction_hint: str = "",
    tags: List[str] | None = None,
    display_name: str = "",
    *,
    target_user_id: str = "",
) -> str:
    """按长期关系变化给当前频道内某位用户加减好感度，并记下这次变化的依据。

    该记的情况：
    - 对方长期帮你的忙、认真给反馈、明确表达关心，关系一点点变近
    - 对方反复挑衅、欺骗、越界，关系一点点变远

    不该记的情况（仅限这几类，不要扩大化）：
    - 纯表情包 / 复读 / 无内容的一两个字
    - 同一件事在同一轮里被反复提到

    ⚠️ delta 给值的要点：**先分辨这次互动的分量档位，再给对应数字**。
    实测发现旧文档只写「2~4 = 认真聊了一阵」这种宽区间，结果绝大多数调用
    都落在 1~2，关系变化失去了分辨率。请按下面每一档的具体特征对号入座：

        1 = 日常寒暄、接梗、复读、发表情包，没有额外信息量
        2 = 对方主动搭话 / 主动分享自己的日常
        3 = 深夜特意来倾诉、明显关心你的身体状况或情绪
        4 = 聊得比平时深入、说出平常不会说的事
        5 = 对方为你付出可见成本（花时间帮你做事、做东西给你）
        6~8 = 重要时刻：袒露压在心里的心事、在冲突里明确站在你这边、
            对你做出长期承诺
        9~10 = 关系发生质变（长期陪伴后的关键抉择、重大牺牲 / 重大背叛）

    负分同理，按伤害程度对应：-1 轻微无视边界、-2~-3 反复越界、
    -4~-5 严重欺骗或性相关越界、-6 以下重大背叛。

    ⚠️ 最常见的错误是**把所有互动都判成 1 分**。1 分只适用于「没有额外信息量」
    的寒暄。只要对方说了自己的事、说了自己的感受、或在深夜特意来找你说话，
    就已经够 2~5 分了。拿不准时问自己一句：这件事换个陌生人会说吗？
    不会 —— 那它就不该只给 1 分。

    Args:
        delta (int): 本次加减的分值，按上面 1~10 的分档给。
            你给出的值就是你的判断本身；系统只会做上限保护，
            不会替你"重新解读"这个数字。
        reason (str): 关系变化的实证，要把原因写清楚。
        summary (str): 可选，用来覆盖「稳定印象」。
        interaction_hint (str): 可选，用来覆盖之后的互动语气建议。
        tags (List[str] | None): 可选，追加关系标签，例如 ["熟客", "认真反馈"]。
        display_name (str): 可选，补充或订正该用户的显示名。
        target_user_id (str): 仅关键字。目标用户的平台用户 ID；
            留空就用当前触发本轮的这位用户。日常单聊不用传。

    调用写法（位置参数就是前面两个）：
        adjust_favorability(2, "对方主动分享今天的糟心事")
        adjust_favorability(-3, "反复打听不该问的事")
    """
    # ⚠️ 签名刻意把 delta/reason 放在最前、target_user_id 设为 keyword-only：
    #    模型天然会写 adjust_favorability(2, "理由")，旧签名第一个位置参数是
    #    target_user_id，会把它绑成 int，随后 .strip() 抛 AttributeError，
    #    这次加分静默丢失。调整顺序后位置调用天然正确。
    resolved_user_id = await _resolve_target_user_id(_ctx, target_user_id)
    cleaned_reason = _one_line(reason, 160)
    # 校验失败一律 return 字符串：raise 会让 NA 把失败时的 None 当系统消息发到聊天里
    if len(cleaned_reason) == 0:
        return "本次未调整：必须给出明确的 reason。"
    try:
        delta = int(delta)
    except (TypeError, ValueError):
        return f"本次未调整：delta 必须是整数（收到 {type(delta).__name__}）。"
    if delta == 0:
        return "本次未调整：delta 为 0 没有意义；只想改档案描述请改用 set_favorability_profile。"

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

    async with await _archive_lock(_ctx.chat_key):
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

        # ② 同用户最小间隔（用持久化锚点 last_adjust_at，不再从 recent_events 反推）
        profile.roll_day(now)   # 跨天先把当日累计归零，⑤ 直接读 day_net
        _min_gap = max(0, int(_sc.get("min_interval_minutes", 120))) * 60
        if profile.last_adjust_at and _min_gap and (now - profile.last_adjust_at) < _min_gap:
            _wait = (_min_gap - (now - profile.last_adjust_at)) // 60
            # v2.0.7：冷却期内**不再直接拒绝**——记成「观察」不改分。
            # 实测：卡里播报「还需 N 分钟」后，模型在冷却期内一次都不调用，
            # 导致一段连续对话最多只有 1 次调分机会，且这些 reason 全部丢失
            # （关系记忆也收不到素材）。记录观察让模型有落点，同时不动分数。
            try:
                profile.observations.append(ArchiveEvent(
                    delta=0,
                    reason=_one_line(cleaned_reason, 160),
                    score_after=int(profile.score),
                    timestamp=now,
                    kind=OBSERVATION_KIND,
                ))
                if len(profile.observations) > 5:
                    profile.observations = profile.observations[-5:]
                profile.updated_at = now
                await _store_archive(_ctx.chat_key, state)
            except Exception as _exc:  # noqa: BLE001
                plugin.logger.warning(
                    f"[favorability] 冷却期观察写入失败（忽略）: {_exc}")
            plugin.logger.info(
                f"[favorability] 冷却期内记录观察（未改分）: "
                f"user={resolved_user_id} 还需 {_wait} 分钟",
            )
            return (
                f"已记录本轮观察，未改分：距上次调整不足 "
                f"{int(_sc.get('min_interval_minutes', 120))} 分钟（还需约 {_wait} 分钟）。"
                "这条观察已留档，好感度分数保持不变。"
            )

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

        # ⑤ 每日累计上限（用持久化当日净累计 day_net；② 处已 roll_day，跨天自动归零）
        _gain_cap = int(_sc.get("max_daily_gain", 15))
        _loss_cap = int(_sc.get("max_daily_loss", 10))
        _net = int(profile.day_net)

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
        profile.record_adjust(delta, now)   # 刷新间隔锚点与当日净累计
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
    interaction_hint: str = "",
    reason: str = "",
    tags: List[str] | None = None,
    display_name: str = "",
    *,
    target_user_id: str = "",
) -> str:
    """把当前频道内某位用户的好感度档案整份重写。

    先前那份档案明显偏了、要一次性铺好完整关系卡、或者关系刚发生大跨越时，用它。

    注意：对**已有档案**改分同样受每日预算与最小间隔约束（与 adjust_favorability
    共享同一套防刷规则），超出会被钳制或拒绝；初始化新档案、或只改 summary/hint/tags
    而不动分数时不受限。别用它绕过日常互动的加分节奏。

    Args:
        score (int): 重写后的内部评分。
        summary (str): 稳定印象的概括。
        interaction_hint (str): 之后该怎么和对方互动。
        reason (str): 这次重写的原因，说清楚为什么值得重写。
        tags (List[str] | None): 关系标签。
        target_user_id (str): 仅关键字。留空就用当前触发本轮的这位用户。
        display_name (str): 可选，显示名。
    """
    resolved_user_id = await _resolve_target_user_id(_ctx, target_user_id)
    kept_summary = _one_line(summary, 240)
    if not kept_summary:
        return "本次未重设：必须提供 summary。"

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

    async with await _archive_lock(_ctx.chat_key):
        state = await _open_archive(_ctx.chat_key)
        is_new = resolved_user_id not in state.profiles
        profile = state.profile_for(resolved_user_id, display_name=display_name)

        # ---- 约束层（v1.4.1）：堵住 set_profile 绕过 adjust 防刷体系的旁路 ----
        # 原先本工具直接 reset_to，无间隔 / 日限 / 索要检测，模型可反复重设刷分，
        # 令 FAVOR_SCALE 全套约束形同虚设。现在：
        #   · 初始化新档案（is_new）或只改描述不动分数（implied==0）→ 不受限；
        #   · 对已有档案改分 → 套用与 adjust_favorability 相同的
        #     索要拦截 / 最小间隔 / 每日净上限（共享同一预算，过滤口径一致）。
        # 不做单次钳制与边际递减——保留「大幅修正」的正当用途，但受每日预算封顶。
        target = _bound_score(int(score))
        now = _ts_now()
        before = int(profile.score)
        implied = target - before
        if not is_new and implied != 0:
            _sc = favor_scale()
            # ① 索要式抬分拦截（仅抬分时；与 adjust 同款）
            if implied > 0:
                hit = await _detect_favor_manipulation(_ctx.chat_key, resolved_user_id)
                if hit:
                    plugin.logger.warning(
                        f"[favorability] 拦截 set_profile 索要式抬分: user={resolved_user_id} "
                        f"implied={implied:+} 命中={hit!r}",
                    )
                    return (
                        "本次重设已被拒绝：好感度只随真实言行自然变化，"
                        "不接受直接索要或要求。用你的人格自然回应即可。"
                    )
            # ② 同用户最小间隔（持久化锚点 last_adjust_at，与 adjust 同源）
            profile.roll_day(now)
            _min_gap = max(0, int(_sc.get("min_interval_minutes", 120))) * 60
            if profile.last_adjust_at and _min_gap and (now - profile.last_adjust_at) < _min_gap:
                _wait = (_min_gap - (now - profile.last_adjust_at)) // 60
                plugin.logger.info(
                    f"[favorability] set_profile 距上次调整不足，跳过: "
                    f"user={resolved_user_id} 还需 {_wait} 分钟",
                )
                return (
                    f"本次未重设：距上次调整不足 {int(_sc.get('min_interval_minutes', 120))} 分钟"
                    f"（还需约 {_wait} 分钟）。"
                )
            # ③ 每日净上限（持久化 day_net，与 adjust 共享同一预算）
            _gain_cap = int(_sc.get("max_daily_gain", 15))
            _loss_cap = int(_sc.get("max_daily_loss", 10))
            _net = int(profile.day_net)
            if implied > 0 and _net + implied > _gain_cap:
                _left = max(0, _gain_cap - _net)
                if _left <= 0:
                    return f"本次未重设：今日对该用户的好感度加分已达上限（+{_gain_cap}）。"
                target = _bound_score(before + _left)
                plugin.logger.info(
                    f"[favorability] set_profile 触及每日加分上限，钳制: user={resolved_user_id} "
                    f"implied {implied:+} → {target - before:+}",
                )
            elif implied < 0 and _net + implied < -_loss_cap:
                _left = max(0, _loss_cap + _net)
                if _left <= 0:
                    return f"本次未重设：今日对该用户的扣分已达上限（-{_loss_cap}）。"
                target = _bound_score(before - _left)
                plugin.logger.info(
                    f"[favorability] set_profile 触及每日扣分上限，钳制: user={resolved_user_id} "
                    f"implied {implied:+} → {target - before:+}",
                )

        profile.reset_to(
            target,
            safe_reason or "手动重设好感度档案",
            summary=kept_summary,
            hint=safe_hint,
            tags=list(safe_tags or []),
            display_name=display_name,
        )
        _applied = target - before
        if _applied != 0:
            profile.record_adjust(_applied, now)   # 初始化/大幅修正同样计入当日预算与间隔锚点
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
    *args,
    target_user_id: str = "",
) -> str:
    """删掉当前频道内某位用户的关系档案。误建了数据、或想彻底重来的时候用。

    Args:
        target_user_id (str): 目标用户的平台用户 ID。留空表示就用当前触发本轮的这位用户。
    """
    # 兼容 target_user_id 同时以位置和关键字传入（会撞 "multiple values"）
    if args and not target_user_id:
        target_user_id = str(args[0])
    resolved_user_id = await _resolve_target_user_id(_ctx, target_user_id)
    async with await _archive_lock(_ctx.chat_key):
        state = await _open_archive(_ctx.chat_key)
        if resolved_user_id not in state.profiles:
            return f"本频道没有 `{resolved_user_id}` 的关系档案。"
        gone = state.profiles.pop(resolved_user_id)
        if state.last_active_user_id == resolved_user_id:
            state.last_active_user_id = ""
        await _store_archive(_ctx.chat_key, state)
    return f"已删除 {gone.display_name or gone.user_id} 的好感度档案。"


@plugin.mount_sandbox_method(
    SandboxMethodType.TOOL,
    name="记笔记",
    description="在当前频道记一条功能笔记（可设过期与好感门槛）；标题相似时覆盖旧条。功能/频道级/可过期，与『沉淀身份锚点』互斥。",
)
async def save_note(
    _ctx: schemas.AgentCtx,
    title: str,
    content: str,
    ttl_hours: int = 0,
    min_favor: int = 0,
    tags: List[str] | None = None,
) -> str:
    """在当前频道记一条**功能笔记**（你自己的小本本）。

    适用：群友让你记住某件事（约定、地址、偏好、代号），或你发现的、以后还用得上的稳定事实。
    **不适用**：
    - 「我是谁」这类身份级、永久、跨频道的自我认知 → 用 `沉淀身份锚点`（anchor_memory）。
    - 好感度判定 → 用 `adjust_favorability`。

    分工：笔记是**频道级、可过期、随手记**的功能记录；锚点是**全局、永久、慎重沉淀**的身份印记。

    ⚠️ 笔记是**你自己的内部记录**，不是聊天内容。
    记完后请在同一次回复里用 `send_msg_text` 正常回应对方，并且**绝不要把笔记原文发出去**。

    Args:
        title (str): 简短标题，同时作为检索关键词，建议 4~12 字
        content (str): 笔记内容，务必简短（默认上限 400 字，超了会被拒绝）
        ttl_hours (int): 多少小时后过期；0 表示永久
        min_favor (int): 好感度门槛，未达到时该笔记不注入（0 = 公开）
        tags (List[str] | None): 额外检索关键词，聊到这些词时会被唤起
    """
    cfg = _note_cfg()
    res = await tier4.add_or_update_note(
        _ctx.chat_key, title, content,
        ttl_hours=int(ttl_hours or 0), min_favor=int(min_favor or 0),
        tags=list(tags or []), source_user_id=await _best_effort_user_id(_ctx),
        cfg=cfg,
    )
    action = res.get("action")
    reason = res.get("reason")
    # 校验失败一律 return 字符串（raise 会让 NA 把失败时的 None 当成系统消息）
    if action == "disabled":
        return "本次未记录：频道笔记功能已关闭。"
    if action == "rejected":
        if reason == "empty_title":
            return "本次未记录：标题不能为空。"
        if reason == "empty_content":
            return "本次未记录：内容不能为空。"
        if reason == "too_long":
            return (
                f"本次未记录：内容太长（上限 {cfg['max_content']} 字）。"
                "请压缩成要点——长资料应该写进背景库，而不是塞进笔记。"
            )
        if reason == "max_items":
            return f"本次未记录：笔记已达上限 {cfg['max_items']} 条，先用 `forget_note` 清理不用的。"
        return "本次未记录：笔记写入被拒绝。"

    ttl = max(0, int(ttl_hours or 0))
    favor = max(0, min(100, int(min_favor or 0)))
    clean_title = _one_line(title, 40)
    ttl_text = f"{ttl} 小时后过期" if ttl else "永久"
    verb = "已更新" if action == "updated" else "已记下"
    return f"{verb}笔记「{clean_title}」（{ttl_text}，好感门槛 {favor}）。"


@plugin.mount_sandbox_method(
    SandboxMethodType.TOOL,
    name="忘掉笔记",
    description="删除当前频道里的一条功能笔记（标题支持模糊匹配）。",
)
async def forget_note(_ctx: schemas.AgentCtx, title: str) -> str:
    """删除一条功能笔记。

    Args:
        title (str): 笔记标题（支持模糊匹配）
    """
    _reason = tier4.corruption_reason()
    if _reason:
        # 降级期间 write 会抛 Tier4Corrupted；在工具层先拦下，给模型一句可读回执
        # （异常逃逸会把失败变成沙盒错误，回到 2.0.9 之前「None 系统消息」的老路）。
        return (
            "本次未删除：笔记文件处于损坏保护状态（原件已备份），"
            "写入被拒绝以免覆盖既有笔记。请联系管理员修复 tier4.json 后重试。"
        )
    removed_title = await tier4.forget_note(_ctx.chat_key, title)
    if removed_title is None:
        notes = [
            e for e in await tier4.list_entries(tier4.NOTE_KIND)
            if e.scope == _ctx.chat_key
        ]
        titles = "、".join(n.title for n in notes[:12]) or "（当前没有笔记）"
        return f"本次未删除：没有找到匹配「{title}」的笔记。现有：{titles}"
    return f"已忘掉笔记「{removed_title}」。"


def _anchor_norm(content: str) -> str:
    """锚点去重用的归一化：小写、折叠空白，用于精确相等判定。"""
    return " ".join(str(content or "").lower().split())


@plugin.mount_sandbox_method(
    SandboxMethodType.TOOL,
    name="沉淀身份锚点",
    description="沉淀一条关于『我是谁』的身份级长期锚点（全局、永久、稀少；受频率硬闸约束）。",
)
async def anchor_memory(
    _ctx: schemas.AgentCtx,
    content: str,
    salience: int = 1,
    tags: List[str] | None = None,
) -> str:
    """沉淀一条**身份锚点**——关于"你是谁"的自我印记，全局、永久、跨频道生效。

    与前 3 档「种子」的关系：种子是你出生前就被写好的设定（不可改）；锚点是
    这一世你自己长出来的痕迹——只增、绝不覆盖种子。

    适用（**稀少而根本**）：你形成的稳定自我认知、反复兑现后立下的长期承诺、
    与某段关系一同定型的原则。例："我答应过要守夜，从此每个深夜都在。"
    **不适用**：
    - 具体约定/地址/称呼偏好等**功能性笔记** → 用 `save_note`（频道级、可过期）。
    - 好感度判定 → 用 `adjust_favorability`。
    - 一时的情绪、单轮话题 → 都不该成为锚点。

    ⚠️ 锚点受**硬频率闸**（每日≤N、间隔≥M 小时、总量≤上限）：这是刻意设计——
    身份是慢慢长成的，不是每轮都改。触顶时会拒绝，这是让你珍视每一次沉淀。
    ⚠️ 与备忘一样，锚点是**你的内部自我认知**，不要把原文当消息发给用户。

    Args:
        content (str): 锚点正文，务必凝练（默认上限 200 字；写成一句能长期成立的话）
        salience (int): 核心度 1..3；越高越优先常驻上下文（把最能定义"你是谁"的设为 3）
        tags (List[str] | None): 可选检索关键词（供未来按话题唤起，阶段 A 仅作元数据）
    """
    cfg = _anchor_cfg()
    if not cfg["enabled"]:
        return "本次未沉淀：身份锚点功能已关闭（ANCHOR_ENABLED=false）。"

    clean_content = _one_line(content, cfg["max_content"] + 1)
    if not clean_content:
        return "本次未沉淀：锚点内容不能为空。"
    if len(clean_content) > cfg["max_content"]:
        return (
            f"本次未沉淀：锚点太长（{len(clean_content)} 字，上限 {cfg['max_content']} 字）。"
            "锚点应是一句能长期成立的自我认知，不是叙事；请凝练。"
            "具体的、临时的事项请改用 `save_note`。"
        )
    scrubbed, blocked = _scrub_profile_text(clean_content)

    # 内容级硬过滤：身份锚点绝不绑定具体对象或频道。
    # 放在 write_anchor 之前 —— 命中即返回，不消耗频率闸额度。
    for _pat, _why in _ANCHOR_REJECT_PATTERNS:
        _hit = _pat.search(scrubbed)
        if _hit:
            return (
                f"本次未沉淀：锚点里出现了{_why}「{_hit.group(0)[:16]}」。"
                "身份锚点必须是「我是谁」的立场，不能绑定某个人或某个频道——"
                "这类内容请用 `save_note` 记在对应频道里。"
            )

    # 去重：与既有锚点精确等值（归一化后）则就地刷新，而非新增（不占当日额度）。
    def _matcher(clean: str, existing) -> bool:  # noqa: ANN001
        return _anchor_norm(clean) == _anchor_norm(existing.content)

    res = await tier4.write_anchor(
        scrubbed,
        salience=int(salience or 1),
        tags=list(tags or []),
        source_user_id=await _best_effort_user_id(_ctx),
        source_chat_key=_ctx.chat_key,
        cfg=cfg,
        salience_max=cfg["salience_max"],
        dedup_matcher=_matcher,
    )

    action = res.get("action")
    if action == "rejected":
        gate = res.get("gate")
        if gate == tier4.GATE_MAX_TOTAL:
            return (
                f"本次未沉淀锚点：身份锚点已达总量上限 {cfg['max_total']} 条。"
                "锚点应当稀少；可在 WebUI「锚点记忆」页清理后再生成。"
            )
        if gate == tier4.GATE_MAX_PER_DAY:
            return (
                f"本次未沉淀锚点：今天已达每日上限 {cfg['max_per_day']} 条。"
                "身份是慢慢长成的，不是每轮都改——先好好对话。"
            )
        if gate == tier4.GATE_INTERVAL:
            return (
                f"本次未沉淀锚点：距上一条不足 {cfg['min_interval_hours']} 小时。"
                "锚点是稀少的沉淀，不是即时记录，再等等。"
            )
        if gate == tier4.GATE_CORRUPT:
            return (
                "本次未沉淀锚点：锚点文件当前处于损坏保护状态（已备份原件），"
                "写入被拒绝以免覆盖掉既有锚点。请联系管理员修复 tier4.json，"
                "本次内容请改用 `save_note` 记在频道笔记里。"
            )
        return "本次未沉淀锚点：功能当前不可用。"

    entry = res.get("entry") or {}
    disp_sal = int(entry.get("salience") or 1)
    _tail = "（档案里的禁写词已自动移除。）" if blocked else ""
    if action == "refreshed":
        plugin.logger.info(
            f"[persona] 锚点刷新（去重命中）：salience={disp_sal} user={_ctx.from_platform_userid}",
        )
        return f"已刷新既有身份锚点（核心度 {disp_sal}，共 {res.get('total')} 条）。{_tail}"

    plugin.logger.info(
        f"[persona] 新沉淀身份锚点：salience={disp_sal} 总量={res.get('total')} "
        f"今日={res.get('day_count')} user={_ctx.from_platform_userid}",
    )
    return (
        f"已沉淀身份锚点（核心度 {disp_sal}，现共 {res.get('total')} 条，"
        f"今日 {res.get('day_count')} 条）。{_tail}"
    )


@plugin.mount_sandbox_method(
    SandboxMethodType.AGENT,
    name="查看好感度档案",
    description="读取某个用户关系档案的完整内容。",
)
async def get_favorability_profile(
    _ctx: schemas.AgentCtx,
    *args,
    target_user_id: str = "",
) -> str:
    """看当前频道内某位用户的完整好感度档案。

    Args:
        target_user_id (str): 目标用户的平台用户 ID。留空表示就用当前触发本轮的这位用户。

    Returns:
        str: 人类可读的档案摘要（供你直接引用，不要整段念给用户）。
    """
    # 兼容 target_user_id 同时以位置和关键字传入
    if args and not target_user_id:
        target_user_id = str(args[0])
    resolved_user_id = await _resolve_target_user_id(_ctx, target_user_id)
    state = await _open_archive(_ctx.chat_key)
    found = state.profiles.get(resolved_user_id)
    if found is None:
        return f"本频道没有 `{resolved_user_id}` 的关系档案。"
    # ⚠️ AGENT 方法的返回值会被直接推成聊天消息，契约要求是 str。
    #    旧实现返回 json.dumps，聊天里就出现一坨原始 JSON（实测出现过 "[SYSTEM] {"）。
    data = found.for_agent()
    lines = [
        f"{data.get('display_name') or resolved_user_id}（{resolved_user_id}）"
        f"  评分 {data.get('score')}/{data.get('max_abs_score')} · 阶段 {data.get('stage')}",
    ]
    if data.get("tags"):
        lines.append("标签：" + "、".join(str(x) for x in data["tags"]))
    if data.get("summary"):
        lines.append("稳定印象：" + _one_line(str(data["summary"]), 200))
    if data.get("interaction_hint"):
        lines.append("互动建议：" + _one_line(str(data["interaction_hint"]), 160))
    events = data.get("recent_events") or []
    if events:
        shown = []
        for ev in events[-3:]:
            if isinstance(ev, dict):
                shown.append(str(ev.get("reason") or ev.get("delta") or ev))
            else:
                shown.append(str(ev))
        lines.append("最近变动：" + "；".join(shown)[:200])
    lines.append(
        f"观察 {len(getattr(found, 'observations', []) or [])} 条 · "
        f"最近互动 {data.get('last_interaction_at') or '无'}"
    )
    return "\n".join(lines)


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
        str: 人类可读的档案摘要列表。
    """
    state = await _open_archive(_ctx.chat_key)
    if len(state.profiles) == 0:
        return "当前频道还没有任何好感度档案。"

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
    # ⚠️ AGENT 返回值会被推成聊天消息，必须是人类可读的 str（旧实现返回 JSON 数组）
    lines = [f"本频道共 {len(state.profiles)} 份档案，按最近互动排序前 {len(rows)} 份："]
    for row in rows:
        tags = "、".join(str(x) for x in (row.get("tags") or []))
        lines.append(
            f"· {row.get('display_name') or row.get('user_id')}"
            f"（{row.get('user_id')}）  {row.get('score')}/{row.get('max_abs_score')}"
            f" · {row.get('stage')}" + (f" · {tags}" if tags else ""),
        )
        if row.get("summary"):
            lines.append("    " + _one_line(str(row["summary"]), 80))
    return "\n".join(lines)


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
    async with await _archive_lock(context.chat_key):
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
        _before = int(profile.score)
        profile.nudge(step, _mystery_reason(profile, verb))
        # ⚠️ 手工命令同样要吃预算：README 明说「手工设定走同一套预算，不是绕过约束的后门」。
        #    早先只调 nudge，不占每日净额、不受最小间隔限制、也不动 last_adjust_at。
        profile.record_adjust(int(profile.score) - _before, _ts_now())
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
        _before = int(profile.score)
        profile.reset_to(
            score,
            f"由于神秘原因，你对{profile.display_name or profile.user_id}的好感度被重新设定了",
            summary=profile.summary or _EMPTY_SUMMARY,
            hint=profile.interaction_hint,
            tags=list(profile.tags),
            display_name=profile.display_name,
        )
        # 同 P1-10：手工重设也计入当日净额与间隔锚点
        profile.record_adjust(int(profile.score) - _before, _ts_now())
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
    # 清洗 chat_key：与 _send_via_adapter 同款，避免 Windows 下含 ":" 等非法字符炸路径
    safe = "".join(ch if (ch.isalnum() or ch in "-_") else "_" for ch in context.chat_key)[:60]
    file_path = out_dir / f"favor_rank_{safe}_{int(time.time() * 1000)}.png"
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
    """「重置上下文」时的处理。

    ⚠️ 历史行为（≤1.4.3、以及回归的 1.9.0）：无条件 `store.delete(STATE_KEY)` ——
    一次「重置上下文」会把本频道的**全部关系档案与备忘**一起删掉，不可逆、无提示。
    重置上下文是高频轻量操作（清对话历史），把不可逆的档案删除绑在它上面，
    误触代价过大；用户直觉上也不会认为「重置上下文」= 「清空关系」。

    现在：默认**保留**（RESET_CHANNEL_CLEARS_FAVOR=false），开关可恢复旧行为。
    无论开关如何，删除前都先落一份备份到 {数据目录}/reset_backups/，
    保证「删了也能捞回来」。

    注：1.9.0 引入 tier4 时本钩子曾退回无条件删除（连同备份函数一起被删），
    实测一次重置即抹掉某频道 43576 字节关系档案。此实现从 1.4.5 移植回来。
    """
    chat_key = _ctx.chat_key
    try:
        blob = await store.get(chat_key=chat_key, store_key=STATE_KEY)
    except Exception as e:  # noqa: BLE001
        plugin.logger.warning(f"[favorability] 重置时读取档案失败，跳过：{e}")
        return

    if not blob:
        return

    # 先备份（无论后续是否删除）—— 一次误触不该造成不可逆损失
    backup_path = _write_reset_backup(chat_key, blob)

    if not bool(getattr(config, "RESET_CHANNEL_CLEARS_FAVOR", False)):
        plugin.logger.info(
            f"[favorability] 频道 {chat_key} 上下文重置：关系档案与备忘已保留"
            f"（如需跟随清空，请开启「「重置上下文」时清空本频道好感度」）"
            + (f"；备份：{backup_path}" if backup_path else ""),
        )
        return

    try:
        await store.delete(chat_key=chat_key, store_key=STATE_KEY)
        plugin.logger.warning(
            f"[favorability] 频道 {chat_key} 上下文重置：已按配置清空本频道关系档案与备忘"
            + (f"（备份：{backup_path}）" if backup_path else ""),
        )
    except Exception as e:  # noqa: BLE001
        plugin.logger.warning(f"[favorability] 重置时清空档案失败：{e}")


def _write_reset_backup(chat_key: str, blob: str) -> str:
    """把重置前的原始 blob 落一份备份，返回路径（失败返回空串，不抛）。"""
    try:
        out_dir = plugin.get_plugin_data_dir() / "reset_backups"
        out_dir.mkdir(parents=True, exist_ok=True)
        safe = "".join(ch if (ch.isalnum() or ch in "-_") else "_" for ch in chat_key)[:60]
        stamp = time.strftime("%Y%m%d_%H%M%S")
        fp = out_dir / f"{safe}_{stamp}.json"
        # 保留原始文本，不做解析，确保任何格式都能原样恢复
        payload = {
            "chat_key": chat_key,
            "store_key": STATE_KEY,
            "backed_up_at": stamp,
            "raw": blob,
        }
        fp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        return str(fp)
    except Exception as e:  # noqa: BLE001
        plugin.logger.warning(f"[favorability] 重置前备份失败（继续执行）：{e}")
        return ""


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
    return "背景记忆已重载：" + persona_memory.status(
        tier1_cfg=_tier1_cfg(), tier2_cfg=_tier2_cfg(), tier3_cfg=_tier3_cfg(),
    )


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
            # per-chat 读写锁：WebUI 的读-改-写必须和消息处理/衰减循环共用同一把锁，
            # 否则并发时 last-writer-wins 会把分数丢掉（锁本身的注释里就写了这个场景）。
            "state_lock": _archive_lock,
            # 关系卡（真实 prompt 的第①段）：预览要复刻完整注入，缺了它就不算「真实注入文本」
            "compose_archive_card": _compose_archive_card,
            "compose_overview_card": _compose_overview_card,
            "stage_info": _stage_of,
            "clamp_score": _bound_score,
            "max_abs_score": _score_cap,
            "now_ts": _ts_now,
            # 频道笔记（阶段 B：tier4 note，接替旧 memo）
            "note_cfg": _note_cfg,
            "note_guide": _NOTE_GUIDE,
            "anchor_guide": _ANCHOR_GUIDE,
            "note_key": tier4._note_title_key,
            # tier4 生长层（阶段 A：身份锚点；阶段 B：+ note）
            "tier4": tier4,
            # 关系记忆（memory_log · 系统自动沉淀的事件史）
            "memory_log": persona_memlog,
            "anchor_cfg": _anchor_cfg,
            # 阶段 C：生长包导出/导入（关系档案枚举与原样读写）
            "enumerate_archives": _enumerate_archives,
            "write_archive": _write_archive,
            "safe_key_name": _safe_key_name,
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


def _safe_key_name(chat_key: str) -> str:
    """把 chat_key 变成可安全落盘的文件名（前缀可读 + sha1 去歧义 + 去路径字符）。"""
    import hashlib

    h = hashlib.sha1(chat_key.encode("utf-8")).hexdigest()[:16]
    head = chat_key[:24].replace("/", "_").replace("\\", "_").replace("..", "_")
    return f"{head}_{h}"


async def _enumerate_archives() -> Dict[str, str]:
    """枚举全部有档案的频道及其**原始 store blob**（供阶段 C 生长包导出）。

    枚举走 `DBPluginData.filter(plugin_key=…)`——它是 store 的后端，天然覆盖所有
    写过档案的频道（比自维护 `_channel_index ∪ DBChatMessage` 更完备，后二者仅作交叉
    校验）。返回 {chat_key: raw_json_str}；无 blob 的频道跳过。
    """
    from nekro_agent.models.db_plugin_data import DBPluginData

    out: Dict[str, str] = {}
    try:
        rows = await DBPluginData.filter(plugin_key=plugin.key).all()
    except Exception as e:  # noqa: BLE001
        plugin.logger.warning(f"[growth] 枚举频道档案失败: {e}")
        return out
    for r in rows:
        ck = str(r.target_chat_key or "")
        if not ck or ck in out:
            continue
        try:
            val = await store.get(chat_key=ck, store_key=STATE_KEY)
        except Exception:  # noqa: BLE001
            continue
        if val:
            out[ck] = val
    return out


async def _write_archive(chat_key: str, raw: str) -> None:
    """原样写回某频道的档案 blob（阶段 C 生长包导入用，scope 仅在 store 档案键）。"""
    await store.set(chat_key=chat_key, store_key=STATE_KEY, value=raw)


async def _migrate_memos_on_load() -> None:
    """阶段 B 一次性迁移：各频道旧 memos → tier4 note（kind=note, scope=chat_key）。

    - 枚举：DBPluginData（衰减/回升同款），覆盖全部有存档的频道（比 _channel_index 全）。
    - 幂等：跳过已 `memos_migrated` 的 blob；note id = `note_{原 memo.id}`，重跑不重复。
    - 可回滚：迁移前把原始 blob 备份到 `backgrounds/_pre_migration/<key>.json`。
    - 失败不丢：tier4 写入失败则不标记、不清空（下次加载重试）；逐频道隔离异常。
    - 审计：写 `backgrounds/_migrations.json` 报告（删了旧子系统后唯一能追溯"迁没迁"的来源）。
    """
    from nekro_agent.models.db_plugin_data import DBPluginData

    if not tier4.tier4_path().parent.is_dir():
        return  # 数据目录都不可用，跳过（下次加载再试）

    doc = await tier4.open_tier4()
    pre_dir = tier4.bg_dir() / "_pre_migration"
    scanned = 0
    memos_by_chat: Dict[str, List[Dict[str, Any]]] = {}
    errors: List[Dict[str, Any]] = []
    seen_keys: set[str] = set()

    try:
        rows = await DBPluginData.filter(plugin_key=plugin.key).all()
    except Exception as e:  # noqa: BLE001
        plugin.logger.warning(f"[persona] memo→note 迁移跳过（枚举失败）: {e}")
        return

    for row in rows:
        ck = str(row.target_chat_key or "")
        if not ck or ck in seen_keys:
            continue
        seen_keys.add(ck)
        scanned += 1
        async with await _archive_lock(ck):
            raw = await store.get(chat_key=ck, store_key=STATE_KEY)
            if not raw:
                continue
            try:
                data = json.loads(raw)
            except Exception:  # noqa: BLE001
                continue
            if not isinstance(data, dict):
                continue
            memos = data.get("memos") or []
            if not memos or data.get("memos_migrated"):
                continue
            memos_by_chat[ck] = [m for m in memos if isinstance(m, dict)]
            try:  # 备份原始 blob（迁移失败可回滚）
                pre_dir.mkdir(parents=True, exist_ok=True)
                (pre_dir / f"{_safe_key_name(ck)}.json").write_text(raw, encoding="utf-8")
            except Exception as e:  # noqa: BLE001
                errors.append({"chat_key": ck, "error": f"backup:{e}"})

    if not memos_by_chat:
        return

    plan = tier4.plan_memo_migration(doc, memos_by_chat)
    try:
        added = await tier4.apply_memo_migration(plan)
    except Exception as e:  # noqa: BLE001
        # 写入失败 → 保持原 memos、不打标记，下次加载重试（绝不丢数据）
        plugin.logger.warning(f"[persona] memo→note 迁移写入失败，保留原 memos 待重试: {e}")
        return

    # tier4 已落库 → 标记各频道已迁移并清空 legacy memos（含全为重复的频道，避免反复处理）
    for ck in memos_by_chat.keys():
        async with await _archive_lock(ck):
            raw = await store.get(chat_key=ck, store_key=STATE_KEY)
            if not raw:
                continue
            try:
                data = json.loads(raw)
            except Exception:  # noqa: BLE001
                continue
            if not isinstance(data, dict):
                continue
            data["memos"] = []
            data["memos_migrated"] = True
            try:
                await store.set(chat_key=ck, store_key=STATE_KEY,
                                value=json.dumps(data, ensure_ascii=False))
            except Exception as e:  # noqa: BLE001
                errors.append({"chat_key": ck, "error": f"clear:{e}"})

    report = {
        "schema": 2,
        "ran_at": _ts_now(),
        "channels_scanned": scanned,
        "channels_migrated": len(memos_by_chat),
        "memo_migrated": added,
        "notes_skipped": int(plan.get("skipped", 0)),
        "errors": errors + list(plan.get("errors", [])),
    }
    await tier4.save_migration_report(report)
    plugin.logger.info(
        f"[persona] memo→note 迁移完成：扫描 {scanned} 频道、迁移 {added} 条"
        f"（跳过重复 {report['notes_skipped']}、错误 {len(report['errors'])}）；"
        f"原始 blob 已备份至 backgrounds/_pre_migration/，报告见 backgrounds/_migrations.json",
    )


_MEMLOG_STATE_NAME = "_memlog_state.json"
"""扫描游标与「本账号见过的频道」的状态文件（与 memory_log.json 同目录）。"""


def _memlog_state_fp():
    return plugin.get_plugin_data_dir() / "backgrounds" / _MEMLOG_STATE_NAME


def _read_memlog_state() -> dict:
    """读取扫描状态。失败一律返回空结构，绝不抛。"""
    try:
        fp = _memlog_state_fp()
        if not fp.exists():
            return {"cursor": 0, "channels": []}
        data = json.loads(fp.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("根节点不是对象")
        return {
            "cursor": int(data.get("cursor") or 0),
            "channels": [str(x) for x in (data.get("channels") or [])][:500],
        }
    except Exception as exc:  # noqa: BLE001
        plugin.logger.debug(f"[memory_log] 状态读取失败（按空处理）: {exc}")
        return {"cursor": 0, "channels": []}


def _write_memlog_state(state: dict) -> None:
    try:
        fp = _memlog_state_fp()
        fp.parent.mkdir(parents=True, exist_ok=True)
        tmp = fp.with_suffix(fp.suffix + ".tmp")
        tmp.write_text(json.dumps(state, ensure_ascii=False, indent=1),
                       encoding="utf-8")
        tmp.replace(fp)
    except Exception as exc:  # noqa: BLE001
        plugin.logger.debug(f"[memory_log] 状态写入失败（忽略）: {exc}")


_memlog_chan_cache: "dict[str, set]" = {}
"""按 backgrounds 目录缓存的「已登记频道」集合。

为什么必须缓存：`_memlog_touch_channel` 在 `persona_prompt` 里每轮都调用，
而 prompt 注入是消息热路径。不加缓存的话每条消息都要读一次状态文件，
在活跃群里（实测 127 条/30 分钟）会造成不必要的磁盘读。
缓存按 backgrounds 路径区分，天然跟随多账号切换。
"""


def _memlog_touch_channel(chat_key: str) -> None:
    """记录「本账号见过这个频道」。

    为什么需要：`exec_code` 是**全局表**，多账号实例下所有账号的会话都在里面，
    而 `chat_channel` / `adapter_instance` 都不带账号归属字段（实测 adapter_instance
    为空、chat_channel 只有 adapter_key），数据库层面无法按账号过滤。
    因此改成运行期采集：只有本账号真的处理过消息的频道才会被记录下来，
    扫描时据此过滤，避免把别的账号的会话串进本账号的记忆库。
    """
    ck = (chat_key or "").strip()
    if not ck:
        return
    try:
        dir_key = str(plugin.get_plugin_data_dir() / "backgrounds")
    except Exception:  # noqa: BLE001
        return

    cached = _memlog_chan_cache.get(dir_key)
    if cached is not None and ck in cached:
        return          # 热路径：命中最常见，直接返回，零 IO

    state = _read_memlog_state()
    chans = cached if cached is not None else set(state.get("channels") or [])
    chans.add(ck)
    _memlog_chan_cache[dir_key] = chans
    state["channels"] = sorted(chans)[-500:]
    _write_memlog_state(state)


async def _memlog_scan_once() -> int:
    """扫一遍 exec_code 里比游标新的行，抽素材推进攒批桶。返回新增素材数。

    为什么从 exec_code 抽而不是在 adjust_favorability 里挂钩：
      · 覆盖面更广 —— 除 reason 外还能抓到 send_voice_clone 的语音文本（**多源**）
      · **被规则拒绝的调用同样有素材** —— code_text 是模型写的调用源码，
        reason 在参数里，不管这次调用最后成功、被最小间隔拒、还是被每日上限拒，
        源码都一样留痕。所以严格模式下调用变少的影响被大幅抵消。
    """
    from nekro_agent.models.db_exec_code import DBExecCode

    state = _read_memlog_state()
    cursor = int(state.get("cursor") or 0)
    known = set(state.get("channels") or [])

    if not known:
        # 还没见过任何频道：把游标推到当前最大 id，避免把历史一次性灌进来
        try:
            latest = await DBExecCode.all().order_by("-id").first()
            if latest is not None and int(cursor) < int(latest.id):
                state["cursor"] = int(latest.id)
                _write_memlog_state(state)
        except Exception:  # noqa: BLE001
            pass
        return 0

    try:
        rows = await DBExecCode.filter(id__gt=cursor).order_by("id").limit(200)
    except Exception as exc:  # noqa: BLE001
        plugin.logger.debug(f"[memory_log] 扫描 exec_code 失败: {exc}")
        return 0
    if not rows:
        return 0

    added = 0
    max_id = cursor
    for row in rows:
        max_id = max(max_id, int(row.id))
        ck = (row.chat_key or "").strip()
        if ck not in known:
            continue          # 别的账号的会话，跳过
        created = 0
        try:
            created = int(row.create_time.timestamp())
        except Exception:  # noqa: BLE001
            created = 0
        mats = persona_consolidator.extract_materials(
            row.code_text or "", created_at=created, msg_id=str(row.id))
        if mats:
            # 注意：push_materials 返回的是「桶内累计条数」而非本次增量，
            # 直接累加会把 1+3+5+… 全部相加，日志严重虚高（实测把 15 条报成 50 条）。
            # 这里用 len(mats) 才是真正的「本次新采集数」。
            persona_consolidator.push_materials(ck, mats)
            added += len(mats)

    if max_id > cursor:
        state["cursor"] = max_id
        _write_memlog_state(state)
    return added


async def _memlog_loop() -> None:
    """关系记忆归纳循环。

    每 60 秒扫一次攒批桶，对「达到条数阈值」或「超时」的频道做一次归纳。

    设计约束：
      · 归纳失败**不丢素材** —— 把整批放回桶头，按正常节奏等下一轮重试
      · 模型返回空（判断无值得记的事）**不回填** —— 否则同一批会被反复喂进模型
      · 单频道独立，一个频道失败不影响其它频道
      · 全程只捕异常、不向上抛，绝不打断消息处理
    """
    await asyncio.sleep(120)   # 启动后等 2 分钟，避开启动高峰
    _memlog_last_pending = -1
    while True:
        try:
            if not bool(getattr(config, "MEMLOG_ENABLED", True)):
                await asyncio.sleep(60)
                continue

            # 先把新素材扫进桶，再看有没有该归纳的频道
            try:
                scanned = await _memlog_scan_once()
                if scanned:
                    plugin.logger.info(f"[memory_log] 新采集素材 {scanned} 条")
            except Exception as exc:  # noqa: BLE001
                plugin.logger.warning(f"[memory_log] 素材扫描异常（已隔离）: {exc}")

            now = time.time()
            ready = persona_consolidator.buckets_ready(now)
            if not ready:
                _stats = persona_consolidator.bucket_stats()
                if _stats.get("pending"):
                    # 有素材但还没到阈值：记一条，便于判断「是不是阈值定太高」
                    _pend = sum(_stats["pending"].values())
                    if _pend != _memlog_last_pending:
                        _memlog_last_pending = _pend
                        plugin.logger.info(
                            f"[memory_log] 攒批中：{_pend} 条待归纳"
                            f"（阈值 {_stats['min_items']} 条 / 超时 "
                            f"{_stats['max_age_hours']} 小时）")
                await asyncio.sleep(60)
                continue

            # 把配置的容量上限同步给存储层（未配置时沿用 memory_log 的默认值）
            try:
                _cap = int(getattr(config, "MEMLOG_MAX_PER_SCOPE", 0) or 0)
                if _cap > 0:
                    persona_memlog.MAX_ENTRIES_PER_SCOPE = _cap
            except Exception:  # noqa: BLE001
                pass

            model_group = str(getattr(config, "MEMLOG_MODEL_GROUP", "") or "")
            for chat_key in ready:
                batch = persona_consolidator.take_batch(chat_key)
                if not batch:
                    continue
                try:
                    memories = await persona_consolidator.consolidate_with_llm(
                        batch, model_group_name=model_group,
                    )
                except Exception as exc:  # noqa: BLE001
                    plugin.logger.warning(
                        f"[memory_log] 归纳异常，素材回填: {chat_key}: {exc}")
                    persona_consolidator.pushes_back(chat_key, batch)
                    continue

                if not memories:
                    # 走到这里只剩一种可能：响应合法但模型认为没什么可记的。
                    # 真正的失败（网络/超时/坏 JSON）现在会抛异常走上面的回填分支。
                    plugin.logger.info(
                        f"[memory_log] {chat_key}：{len(batch)} 条素材未产出记忆"
                        "（响应合法但模型判断无可记内容，不回填）")
                    continue

                try:
                    res = await persona_memlog.add_entries(
                        memories, scope=chat_key, source="auto")
                    if res.get("corrupt"):
                        # 文件损坏时 add_entries 拒绝落盘（避免清空既有事件记忆），
                        # 素材回填等修复后重试
                        plugin.logger.error(
                            f"[memory_log] {chat_key}：记忆文件处于损坏保护状态"
                            f"（{res['corrupt']}），本轮 {len(batch)} 条素材已回填待重试")
                        persona_consolidator.pushes_back(chat_key, batch)
                        continue
                    plugin.logger.info(
                        f"[memory_log] {chat_key}：{len(batch)} 条素材 → "
                        f"{res['added']} 条记忆入库"
                        f"（去重跳过 {res['skipped']}，淘汰 {res['evicted']}）")
                except Exception as exc:  # noqa: BLE001
                    plugin.logger.warning(
                        f"[memory_log] 写入失败，素材回填: {chat_key}: {exc}")
                    persona_consolidator.pushes_back(chat_key, batch)
        except Exception as exc:  # noqa: BLE001
            plugin.logger.warning(f"[memory_log] 归纳循环异常（已隔离）: {exc}")
        await asyncio.sleep(60)


@plugin.mount_init_method()
async def _start_decay_loop() -> None:
    """插件加载时：先跑一次 memo→note 迁移（阶段 B），再启动好感度结算后台任务。"""
    # 阶段 B 一次性迁移（幂等；异常已隔离，绝不阻断插件加载）
    try:
        await _migrate_memos_on_load()
    except Exception as e:  # noqa: BLE001
        plugin.logger.warning(f"[persona] 首载 memo→note 迁移异常（已隔离）: {e}")

    global _decay_task
    if _decay_task is not None and not _decay_task.done():
        return
    _decay_task = asyncio.create_task(_decay_loop())

    # 关系记忆攒批参数：consolidator 里是模块常量，必须在这里按配置注入，
    # 否则 MEMLOG_BATCH_MIN_ITEMS / MEMLOG_BATCH_MAX_AGE_HOURS 两个配置项
    # 只会在启动日志里被打印，实际行为仍按常量走（改配置静默无效）。
    try:
        persona_consolidator.BATCH_MIN_ITEMS = max(1, int(config.MEMLOG_BATCH_MIN_ITEMS))
        persona_consolidator.BATCH_MAX_AGE_HOURS = max(
            0.0, float(config.MEMLOG_BATCH_MAX_AGE_HOURS))
        plugin.logger.info(
            f"[memory_log] 攒批参数已按配置注入：{persona_consolidator.BATCH_MIN_ITEMS} 条 / "
            f"{persona_consolidator.BATCH_MAX_AGE_HOURS} 小时")
    except Exception as exc:  # noqa: BLE001
        plugin.logger.warning(f"[memory_log] 攒批参数注入失败，沿用模块默认值: {exc}")

    # 关系记忆归纳任务（与好感度结算独立）
    global _memlog_task
    if not (_memlog_task is not None and not _memlog_task.done()):
        _memlog_task = asyncio.create_task(_memlog_loop())
        plugin.logger.info(
            f"[memory_log] 关系记忆归纳任务已启动：攒批 {config.MEMLOG_BATCH_MIN_ITEMS} 条"
            f" / {config.MEMLOG_BATCH_MAX_AGE_HOURS} 小时，"
            f"注入 {'开' if config.MEMLOG_INJECT_ENABLED else '关'}"
        )
    _decay_state = "启用" if config.DECAY_ENABLED else "关闭（运行期可经配置开启）"
    _recover_state = "启用" if config.RECOVER_ENABLED else "关闭（运行期可经配置开启）"
    plugin.logger.info(
        f"[favorability] 好感度结算任务已启动（每 30 分钟检查一次）："
        f"衰减={_decay_state}，每 {config.DECAY_INTERVAL_HOURS} 小时 -{config.DECAY_PERCENT}%"
        f"（保底={config.DECAY_KEEP_TIER}/{config.DECAY_TIER_GRACE_HOURS}h 逐级解锁）；"
        f"回升={_recover_state}，每 {config.RECOVER_INTERVAL_HOURS} 小时 +{config.RECOVER_PERCENT}%（封顶 0）"
    )


@plugin.mount_cleanup_method()
async def clean_up():
    """停掉插件自己的后台任务。"""
    global _decay_task, _memlog_task
    # A4：落盘去抖残留的命中统计，避免丢最后几次强化
    try:
        persona_memory.flush_hit_stats()
    except Exception:  # noqa: BLE001
        plugin.logger.debug("[persona] 命中统计落盘失败（忽略）")
    ml = _memlog_task
    if ml is not None and not ml.done():
        ml.cancel()
        try:
            await ml
        except asyncio.CancelledError:
            pass
        except Exception:  # noqa: BLE001
            pass
        plugin.logger.info("[memory_log] 关系记忆归纳任务已停止")

    pending = _decay_task
    if pending is not None and not pending.done():
        pending.cancel()
        plugin.logger.info("[favorability] 好感度衰减任务已停止")
