"""好感度排行榜图片卡片渲染（Pillow）。

暖色卡片主题 v3「焦糖奶油 · 毛玻璃」（三段式渐变 + 暖色光斑 + 磨砂玻璃面板）：
- 三段式竖向渐变背景（#fdf6ec 奶油白 → #fbe9d2 杏黄 → #f6d5b8 蜜桃）+ 暖色装饰光斑
- 强调色 #c46a4a 赤陶橙、暖棕文字 #4a3728
- 毛玻璃卡片与排行行：GaussianBlur 模糊背后内容 + 暖白半透明叠加 + 圆角遮罩
  + 玻璃描边/顶部高光（独立图层 alpha 混合）
- 圆角排行行：名次徽标 + 昵称/ID + 阶段标签 + 好感度分值

Pillow 优先直接 import 主环境已装版本；缺失时通过 NA dynamic_import_pkg
多镜像动态安装。中文字体依次探测：配置指定 → 插件数据目录 fonts → 系统常见 CJK 字体。
任一环节不可用返回 None，调用方回退为文本。
"""

from __future__ import annotations

import importlib
import io
import json
import math
import os
import shutil
import threading
import random
import time
import urllib.request
import site
from pathlib import Path
from typing import Any, Dict, List

from nekro_agent.api.plugin import dynamic_import_pkg
from nekro_agent.core.logger import get_sub_logger
from nekro_agent.core.os_env import PLUGIN_DYNAMIC_PACKAGE_DIR

logger = get_sub_logger("nekro_persona")

# MomoTune 暖色配色 v2「焦糖奶油」：赤陶橙 + 暖棕 + 奶油米黄
_ACCENT = (196, 106, 74)         # #c46a4a 赤陶橙（主强调）
_ACCENT_LIGHT = (224, 148, 92)   # #e0945c 琥珀（渐变亮端）
_ACCENT_SOFT = (252, 240, 228)    # #fcf0e4 浅杏（标签底）
_TEXT = (74, 55, 40)              # #4a3728 暖棕（主文字）
_SUBTEXT = (140, 110, 88)         # #8c6e58 驼色（次级文字）
_MUTED = (176, 152, 132)          # #b09884 沙色（弱化文字）
_NEUTRAL = (160, 140, 122)        # 0 分中性色（避免把 0 渲染成"正分"赤陶色）
_COOL = (110, 128, 138)           # #6e808a 灰蓝（负分，暖调体系里的冷色对照）
_BG_TOP = (253, 246, 236)         # #fdf6ec 奶油白
_BG_MID = (251, 233, 210)         # #fbe9d2 杏黄
_BG_BOTTOM = (246, 213, 184)      # #f6d5b8 蜜桃
_BORDER = (232, 190, 160)
_WHITE = (255, 255, 255)

# 毛玻璃背景装饰光斑 (cx_frac_w, cy_frac_h, r_frac_min, RGBA)
# 固定构图（不用随机），保证每次渲染一致；给磨砂玻璃提供"可模糊"的内容——
# 纯平渐变上毛玻璃效果几乎不可见。
_BLOB_SPECS = [
    (0.18, 0.10, 0.20, (224, 148, 92, 150)),   # 琥珀（左上）
    (0.78, 0.06, 0.24, (196, 106, 74, 110)),   # 赤陶（右上）
    (0.92, 0.45, 0.20, (240, 180, 140, 140)),  # 蜜桃（右中）
    (0.08, 0.55, 0.18, (250, 220, 190, 170)),  # 奶油（左中）
    (0.45, 0.97, 0.22, (232, 160, 110, 100)),  # 焦糖（底部）
    (0.62, 0.35, 0.15, (246, 200, 160, 85)),   # 浅杏（中部）
]

# 各阶段标签配色 (背景, 前景) —— 全部落在暖色系，靠明度/色相微差区分层级
# v2.1.10：排序卡是「快速扫视」场景，标签承担关键信息，必须一眼能分。
# 旧配色有两组撞色：①「排斥」浅底 + 深赤陶字与「特别亲密」赤陶实底同色相，
# 只差明度；②「偏爱 / 保留 / 中立」三个浅暖底饱和度接近，并排要仔细看。
# 现在按**色相家族**分开，明度也排成阶梯：
#   排斥（冷灰蓝，呼应负分 _COOL，六个浅底里最暗）
#   → 中立（无彩中性灰）→ 亲近（鼠尾草绿）→ 保留（暖沙黄）→ 偏爱（蜜桃橙，提饱和）
#   → 特别亲密（唯一实底，保持最高层级）
_STAGE_STYLES = {
    "排斥": ((188, 210, 236), (36, 66, 102)),       # 冷蓝（与负分 _COOL 同族，色相与其余五档都不同）
    "保留": ((252, 243, 196), (108, 84, 16)),       # 暖沙黄（纯黄相）
    "中立": ((233, 232, 235), (80, 82, 90)),        # 中性灰（无彩色）
    "亲近": ((212, 239, 204), (46, 98, 46)),        # 鼠尾草绿（暖体系里的自然色）
    "偏爱": ((253, 208, 186), (146, 58, 24)),       # 蜜桃橙（降黄提红，与暖沙拉开）
    "特别亲密": ((178, 92, 62), (255, 255, 255)),   # 赤陶实底（唯一实底，最高层级）
}

# 前三名名次徽章配色 (底色, 字色) —— 金 / 银 / 铜（统一暖金属调）
_RANK_METALS = [
    ((216, 160, 52), (255, 255, 255)),     # 暖金
    ((190, 182, 172), (255, 255, 255)),     # 暖银
    ((198, 138, 94), (255, 255, 255)),      # 暖铜
]

# 字体文件名关键字，分两段：
#   LEGACY —— 历史名单，先匹配，保证既有实例的字体选择不变
#   EXTRA  —— 兜底名单，补充项目自带但旧名单漏掉的名字（关键：msyh）
_FONT_HINTS_LEGACY = (
    "cjk", "wqy", "ming", "song", "hei", "yahei",
    "pingfang", "noto", "wenkai", "misans",
)
_FONT_HINTS_EXTRA = (
    "msyh", "lxgw", "deng", "source", "harmony", "alibaba", "siyuan", "kai",
)

_FONT_CANDIDATES = [
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/opentype/noto/NotoSerifCJK-Regular.ttc",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
    "/usr/share/fonts/truetype/droid/DroidSansFallbackFull.ttf",
    "/usr/share/fonts/truetype/arphic/uming.ttc",
    "/System/Library/Fonts/PingFang.ttc",
    "C:/Windows/Fonts/msyh.ttc",
]

_PIL_MIRRORS = ["https://mirrors.aliyun.com/pypi/simple/", "https://pypi.org/simple", None]

_font_cache: str | None = None
_font_checked = False
_font_key: tuple | None = None


def _load_pil() -> Any | None:
    """加载 PIL（Pillow）。

    优先直接 import（命中主环境已装的 Pillow，完全绕开 pip）；找不到才走
    NA 动态安装并多镜像重试。任何失败返回 None（调用方回退文本）。
    """
    try:
        repo_dir = Path(str(PLUGIN_DYNAMIC_PACKAGE_DIR))
        for p in (str(repo_dir), str(repo_dir / "site-packages")):
            site.addsitedir(p)
    except Exception:  # noqa: BLE001
        pass
    try:
        return importlib.import_module("PIL")
    except ImportError:
        pass
    for mirror in _PIL_MIRRORS:
        try:
            return dynamic_import_pkg("pillow>=10.0.0", "PIL", mirror=mirror)
        except Exception:  # noqa: BLE001
            try:
                return importlib.import_module("PIL")
            except ImportError:
                continue
    logger.error("PIL 不可用，好感度排行榜卡片将回退为文本")
    return None


# 字体查找缓存的锁：卡片渲染可能在线程池里并发执行（图像生成），而
# _font_cache / _font_key / _font_checked 是模块级可变状态 —— 无锁时两个线程
# 可能同时走完查找并写入不一致的三元组（key 与 cache 不配对 → 换配置后命中错字体）。
_FONT_LOCK = threading.RLock()


# ============ 字体落地（pin）============
# 探测到的字体（系统字体 / 配置里指向外部路径的字体）会被复制一份到插件数据目录，
# 之后一律指向这份副本：
#   · 容器重建、系统字体被清掉、镜像换基座，卡片字体都不会突然变样或消失；
#   · 副本跟插件数据一起备份/迁移，路径稳定；
#   · 只在第一次（或副本丢失时）复制，之后读标记文件直接命中，不重复拷贝。
# 关掉：环境变量 NEKRO_PERSONA_FONT_PIN=0
_FONT_PIN_MARKER = ".font_pin.json"
# emoji 字体单独的落地标记（正文与 emoji 是两条独立的探测链，标记不能互相覆盖）
_EMOJI_PIN_MARKER = ".emoji_font_pin.json"


def _font_pin_enabled() -> bool:
    return str(os.environ.get("NEKRO_PERSONA_FONT_PIN", "1")).strip().lower() not in (
        "0", "false", "no", "off",
    )


def _pin_font(src: str, pin_dir: str, marker_name: str = _FONT_PIN_MARKER) -> str:
    """把字体复制到插件数据目录并返回副本路径；失败或禁用时原样返回 src。

    marker_name 区分「正文/标题字体」与「emoji 字体」两条独立的落地记录。
    """
    src_path = Path(src)
    if not pin_dir or not src_path.is_file() or not _font_pin_enabled():
        return src
    try:
        pin_root = Path(pin_dir)
        # 已经在插件数据目录里 → 不用复制
        try:
            src_path.resolve().relative_to(pin_root.resolve())
            return str(src_path)
        except Exception:  # noqa: BLE001
            pass

        marker = pin_root / marker_name
        if marker.is_file():
            try:
                data = json.loads(marker.read_text(encoding="utf-8"))
                pinned = str(data.get("pinned") or "")
                if pinned and Path(pinned).is_file():
                    return pinned
            except Exception:  # noqa: BLE001
                logger.debug("字体标记文件损坏，重新落地", exc_info=True)

        pin_root.mkdir(parents=True, exist_ok=True)
        dst = pin_root / src_path.name
        if dst.exists():
            # 同名但内容不同（比如换了个同名系统字体）→ 加后缀区分
            if dst.stat().st_size != src_path.stat().st_size:
                dst = pin_root / f"{src_path.stem}-pinned{src_path.suffix}"
        tmp = dst.with_name(dst.name + ".tmp")
        shutil.copyfile(src_path, tmp)
        if tmp.stat().st_size != src_path.stat().st_size:
            tmp.unlink(missing_ok=True)
            raise OSError("复制后大小不一致")
        tmp.replace(dst)
        try:
            marker.write_text(
                json.dumps(
                    {"pinned": str(dst), "source": str(src_path),
                     "size": dst.stat().st_size},
                    ensure_ascii=False, indent=1,
                ),
                encoding="utf-8",
            )
        except Exception:  # noqa: BLE001
            logger.debug("字体标记文件写入失败（不影响使用）", exc_info=True)
        logger.info(f"字体已落地到插件数据目录: {dst.name}（来源 {src_path}）")
        return str(dst)
    except Exception:  # noqa: BLE001
        logger.warning(f"字体落地失败，继续使用原路径: {src}", exc_info=True)
        return src


# ============ 字体能力探测 ============
# 「这个字体能渲染什么」不靠文件名猜，直接拿它**渲染一批样本字符**看有没有字形：
#   · 汉字样本（GB2312 一级高频字，另加标点/全角）→ 判断能不能当正文字体
#   · emoji 样本（彩色 emoji + 符号）→ 判断能不能当 emoji 字体，以及是不是彩色
# 结果用于 WebUI 的下拉过滤与「推荐」标注，也供 scripts/probe_fonts.py 单独跑。
_CJK_PROBE = (
    "的一是在不了有和人这中大为上个国我以要他时来用们生到作地于出就分对成会可主发年动"
    "同工也能下过子说产种面而方后多定行学法所民得经十三之进着等部度家电力里如水化高自"
    "二理起小物现实加量都两体制机当使点从业本去把性好应开它合还因由其些然前外天政四日"
    "那社义事平形相全表间样与关各重新线内数正心反你明看原又么利比或但质气第向道命此变"
    "条只没结解问意建月公无系军很情者最立代想已通并提直题程展五果料象员位入常文总次品"
)
_CJK_PROBE_EXTRA = "，。、；：？！（）《》“”‘’…—·「」【】～　"
_EMOJI_PROBE = "⚽🏀😀😄✨🌸🌙🎉👍🎈🍀🔥💡🎵🐱👌🎁"
_EMOJI_PROBE_SYMBOL = "❤☀★☆✔✖➡⏰"


def probe_font_capabilities(path: str) -> dict:
    """探测一个字体文件能渲染什么（按文件大小+修改时间无关，纯内容判定）。

    返回:
        {"ok","bytes","cjk","emoji","symbols","color","roles","note"}
        cjk / emoji / symbols 是 0~1 的字形覆盖率；roles 是建议用途。
    """
    out = {
        "ok": False, "bytes": 0, "cjk": 0.0, "emoji": 0.0, "symbols": 0.0,
        "color": False, "color_emoji": False, "mono_emoji": False,
        "roles": [], "kind": "other", "note": "",
    }
    p = Path(str(path or ""))
    try:
        if not p.is_file():
            out["note"] = "文件不存在"
            return out
        out["bytes"] = p.stat().st_size
        out["ok"] = True
    except Exception:  # noqa: BLE001
        out["note"] = "读不到文件"
        return out

    def ratio(sample: str) -> float:
        chars = [c for c in dict.fromkeys(sample) if c.strip()]
        if not chars:
            return 0.0
        hit = sum(1 for c in chars if _char_in_font(str(p), c))
        return hit / len(chars)

    try:
        out["cjk"] = round(ratio(_CJK_PROBE + _CJK_PROBE_EXTRA), 3)
        out["emoji"] = round(ratio(_EMOJI_PROBE), 3)
        out["symbols"] = round(ratio(_EMOJI_PROBE_SYMBOL), 3)
        out["color"] = bool(_font_has_color(str(p)))
    except Exception:  # noqa: BLE001
        out["note"] = "字形探测失败"
        return out

    # ⚠️ 很多中文字体也带 emoji 码点的**单色**字形（实测 LXGW / simhei / 思源黑体都有），
    #    只看「有没有字形」会把它们全判成 emoji 字体。所以彩色单独成一个能力位：
    #    color_emoji = 既含 emoji 字形、又是彩色字体 —— 这才是能当 emoji 字体的那种。
    out["color_emoji"] = bool(out["color"] and out["emoji"] >= 0.85)
    out["mono_emoji"] = bool((not out["color"]) and out["emoji"] >= 0.85)

    # 用途判定除了字形覆盖，再看文件名（与「数据目录里 *emoji* 即 emoji 字体」的老约定一致）：
    # 名字带 emoji 的字体不当正文字体，即使它顺带覆盖了汉字（emoji-segoe.ttf 就是这种）。
    is_emoji_name = "emoji" in p.name.lower()
    roles = []
    if out["cjk"] >= 0.90 and not is_emoji_name:
        roles.append("text")
    if out["color_emoji"] and (is_emoji_name or out["cjk"] < 0.90):
        roles.append("emoji")
    elif out["mono_emoji"]:
        roles.append("emoji-mono")
    if not roles:
        if out["cjk"] >= 0.5:
            out["note"] = "汉字覆盖偏低，不建议当正文字体"
        elif out["emoji"] > 0:
            out["note"] = "emoji 覆盖偏低"
        else:
            out["note"] = "既不像中文正文，也不像 emoji 字体"
    out["roles"] = roles
    if "emoji" in roles:
        out["kind"] = "emoji"
    elif "text" in roles:
        out["kind"] = "text"
    elif "emoji-mono" in roles:
        out["kind"] = "emoji-mono"
    else:
        out["kind"] = "other"
    return out


# ============ 渲染后端能力探测 ============
# Pillow 这类**编译依赖**没法像字体那样「放一份进来就用」：`PIL` / `PIL._imaging` 是全局唯一
# 模块名，NA 自己也用它出图，插件再塞一份只会互相踩。能做的就是同一套思路的另一半 ——
# 像探测字体能力那样探测**渲染后端能干什么**，把结论缓存下来、代码按能力选路径。
_BACKEND_CACHE_FILE = "render_backend.json"
_backend_report: dict | None = None


def probe_imaging_backend() -> dict:
    """探测成像后端能力（Pillow / FreeType / raqm / 可用的字形判定手段）。"""
    out: dict = {
        "python": "",
        "pillow": "",
        "freetype": "",
        "raqm": False,
        "char_index": False,       # Pillow 的 FT2Font 是否还有 get_char_index（Pillow 12 已移除）
        "mask_notdef": False,      # getmask 对未映射码点是否返回 None（否则掩码比对不可靠）
        "color_emoji": False,      # 能否渲染彩色 emoji（COLR/SVG/CBDT 任一）
        "fonttools": "",
        "coverage_source": "mask",  # cmap=fontTools 精确判定；mask=掩码比对兜底
    }
    try:
        import platform
        import PIL
        from PIL import features

        out["python"] = platform.python_version()
        out["pillow"] = str(PIL.__version__)
        try:
            out["freetype"] = str(features.version("freetype2"))
        except Exception:  # noqa: BLE001
            out["freetype"] = ""
        try:
            out["raqm"] = bool(features.check("raqm"))
        except Exception:  # noqa: BLE001
            out["raqm"] = False
    except Exception:  # noqa: BLE001
        pass

    try:
        from fontTools import version as _ft_version

        out["fonttools"] = str(_ft_version)
        out["coverage_source"] = "cmap"
    except Exception:  # noqa: BLE001
        out["fonttools"] = ""
        out["coverage_source"] = "mask"

    # 拿一个真实字体试两种判定手段（都用系统里第一个能找到的 CJK 字体）
    probe_font = None
    for cand in _FONT_CANDIDATES:
        try:
            if Path(cand).is_file():
                probe_font = cand
                break
        except Exception:  # noqa: BLE001
            continue
    if probe_font:
        try:
            from PIL import ImageFont

            f = ImageFont.truetype(probe_font, 20)
            out["char_index"] = hasattr(getattr(f, "font", None), "get_char_index")
            try:
                out["mask_notdef"] = f.getmask("\ufffe").getbbox() is None
            except Exception:  # noqa: BLE001
                out["mask_notdef"] = False
            try:
                # 彩色字形的字体用 getmask 通常拿不到 → 反过来证明「掩码判定会误判」
                out["color_emoji"] = bool(f.getmask("\U0001f600").getbbox())
            except Exception:  # noqa: BLE001
                out["color_emoji"] = False
        except Exception:  # noqa: BLE001
            pass
    return out


def backend_report(font_dir: str = "", refresh: bool = False) -> dict:
    """带缓存的探测结果；首次或后端版本变化时写 `<数据目录>/render_backend.json`。"""
    global _backend_report
    if _backend_report is not None and not refresh:
        return _backend_report
    rep = probe_imaging_backend()
    _backend_report = rep
    try:
        if font_dir.strip():
            root = Path(font_dir.strip()).parent
            if root.is_dir():
                import json as _json

                path = root / _BACKEND_CACHE_FILE
                old = {}
                try:
                    if path.is_file():
                        old = _json.loads(path.read_text(encoding="utf-8")) or {}
                except Exception:  # noqa: BLE001
                    old = {}
                if old.get("pillow") != rep["pillow"] or old.get("freetype") != rep["freetype"]:
                    path.write_text(_json.dumps(rep, ensure_ascii=False, indent=1), encoding="utf-8")
                    logger.info(
                        f"渲染后端: Pillow {rep['pillow']} / FreeType {rep['freetype'] or '?'}"
                        f" / 字形判定={rep['coverage_source']}",
                    )
    except Exception:  # noqa: BLE001
        logger.debug("渲染后端探测结果写入失败（忽略）", exc_info=True)
    return rep


def reset_font_caches() -> None:
    """清掉字体相关缓存，让下次渲染重新探测。

    WebUI 里换字体 / 上传字体后必须调用：`_find_font_impl` 的缓存按 (font_path, font_dir)
    命中，同一路径换了文件内容（例如重新上传同名字体）不会自动失效。
    """
    global _font_cache, _font_checked, _font_key
    global _FALLBACK_READY, _emoji_font_checked, _emoji_font_key
    with _FONT_LOCK:
        _font_cache, _font_checked, _font_key = None, False, None
        _FALLBACK_READY = False
        try:
            _FALLBACK_FONT_PATHS.clear()
        except Exception:  # noqa: BLE001
            pass
        _emoji_font_checked = False
        _emoji_font_key = None
        try:
            _emoji_font_cache.clear()
        except Exception:  # noqa: BLE001
            pass
        try:
            _cov_cache.clear()
            _cmap_cache.clear()
            _fallback_font_cache.clear()
        except Exception:  # noqa: BLE001
            pass


def _find_font(font_path: str = "", font_dir: str = "") -> str | None:
    """线程安全入口（实现见 _find_font_impl）。"""
    with _FONT_LOCK:
        return _find_font_impl(font_path, font_dir)


def _find_font_impl(font_path: str = "", font_dir: str = "") -> str | None:
    """查找可用的中文字体文件路径，找不到返回 None。

    ⚠️ 缓存按 (font_path, font_dir) 做 key，且**只缓存成功结果**。
       旧实现用一个全局 bool：① 换了配置不生效（参数被完全忽略）；
       ② 首次查找失败后 _font_checked 已置位，即使之后把字体放进
       fonts/ 也永远返回 None，只能重启进程。
    """
    global _font_cache, _font_checked, _font_key
    _key = (font_path.strip(), font_dir.strip())
    if _font_checked and _font_key == _key:
        return _font_cache

    # 显式指定的字体文件：只要存在就用，**不再按后缀过滤** ——
    # 旧实现把显式路径和自动扫描混在一个循环里，`/path/MyFont`（无扩展名）
    # 或 `.TTF` 之外的扩展名会被静默跳过，用户会以为自己配置没生效。
    # 真打不开 PIL 会在加载阶段报错，比「静默忽略」好定位。
    _explicit = font_path.strip()
    if _explicit:
        _ep = Path(_explicit)
        if _ep.is_file():
            _picked = _pin_font(str(_ep), font_dir.strip())
            _font_cache, _font_key, _font_checked = _picked, _key, True
            return _font_cache
        logger.warning(f"指定的字体文件不存在，继续自动查找：{_explicit}")

    candidates: list[str] = []
    if font_dir.strip():
        candidates.append(font_dir.strip())
    candidates.extend(_FONT_CANDIDATES)

    for item in candidates:
        p = Path(item)
        if p.is_file() and p.suffix.lower() in (".ttf", ".ttc", ".otf"):
            _picked = _pin_font(str(p), font_dir.strip())
            _font_cache, _font_key, _font_checked = _picked, _key, True
            return _font_cache
        if p.is_dir():
            # ⚠️ 两段匹配必须是「名单在外层、文件在内层」：
            #    第一段用历史名单扫一遍整个目录，保证既有实例选中的字体不变
            #    （把两个名单写进文件循环内部会让 Deng.ttf 按字母序压过
            #     LXGWWenKai，画风静默改变）；只有第一段全目录都没命中，
            #    才用补充名单再扫一遍 —— 兜底「只放了 msyh.ttc 的账号」。
            for _hints in (_FONT_HINTS_LEGACY, _FONT_HINTS_EXTRA):
                for ext in ("*.ttf", "*.ttc", "*.otf"):
                    for f in sorted(p.rglob(ext)):
                        if any(k in f.name.lower() for k in _hints):
                            _picked = _pin_font(str(f), font_dir.strip())
                            _font_cache, _font_key, _font_checked = _picked, _key, True
                            return _font_cache
    # 2.3.1 起**不再内置字体**：包内那套 Noto Sans SC 子集（扩到 GB18030 后 10.8 MB）
    # 让安装包从 1.2 MB 涨到 9.4 MB，而绝大多数 NA 镜像自带中文字体。
    # 现在只按「配置指定 → 数据目录 fonts/ → 系统 CJK 候选」找；找不到就明确回退文本，
    # 并把「怎么修」写进日志（而不是往包里塞一个巨大字体）。
    logger.warning(
        "未找到中文字体，关系档案/排行榜卡片将回退为文本；"
        "把任意中文字体（.ttf/.ttc/.otf）放到插件数据目录 fonts/ 即可开启卡片出图",
    )
    return None


# ============ emoji 支持 ============
# Pillow 无字体回退能力，这里做「分段渲染」：emoji 段用彩色 emoji 字体
# （Segoe UI Emoji / Noto Color Emoji，embedded_color 上色），其余段用中文字体。
# 通过 _make_draw() 包装 ImageDraw，现有绘制代码无需改动。

_EMOJI_FONT_CANDIDATES = [
    "/usr/share/fonts/truetype/windows/seguiemj.ttf",
    "C:/Windows/Fonts/seguiemj.ttf",   # Windows 原生（COLR 矢量彩字，任意字号可彩色渲染）
    "/usr/share/fonts/truetype/noto/NotoColorEmoji.ttf",
    "/usr/share/fonts/opentype/noto/NotoColorEmoji.ttf",
    "/usr/share/fonts/truetype/noto/NotoEmoji-Regular.ttf",
    "/usr/share/fonts/truetype/ancient-scripts/Symbola_hint.ttf",
]

# 手动指定的 emoji 字体（配置项 RANK_CARD_EMOJI_FONT，空 = 自动探测）。
# 进程内单值：一个插件实例只有一份配置，渲染线程共用同一份，语义与 _emoji_font_path 一致。
_emoji_font_override: str = ""
_emoji_font_path: str | None = None
_emoji_font_checked = False
_emoji_font_key: tuple | None = None
_emoji_font_cache: dict = {}
# 选中的 emoji 字体是否含彩色图层（决定要不要传 embedded_color）
_emoji_font_color = True


def _font_has_color(path: str) -> bool:
    """粗判字体是否含彩色图层（COLR / CBDT / sbix / SVG）。

    单色字体（NotoEmoji-Regular、Symbola 之类）传 embedded_color=True
    不会出颜色，部分 Pillow 版本直接报错，所以要先分辨。
    判断失败一律当作彩色，保持旧行为。
    """
    try:
        with open(path, "rb") as fh:
            head = fh.read(12)
            if len(head) < 12:
                return True
            if head[:4] == b"ttcf":      # 字体集合：表目录分散，不深究
                return True
            num = int.from_bytes(head[4:6], "big")
            tags = set()
            for _ in range(min(num, 64)):
                rec = fh.read(16)
                if len(rec) < 16:
                    break
                tags.add(rec[:4])
            return bool(tags & {b"COLR", b"CBDT", b"sbix", b"SVG "})
    except Exception:  # noqa: BLE001
        return True


def _pick_emoji_font(paths: list) -> str | None:
    """在候选里优先挑彩色字体；没有彩色字体时退回第一个存在的（单色也能显示）。"""
    exists = [p for p in paths if Path(p).is_file()]
    for p in exists:
        if _font_has_color(p):
            return p
    return exists[0] if exists else None


def _scan_system_emoji_fonts() -> list:
    """扫系统字体目录里文件名含 emoji 的字体。

    ⚠️ 2.3.4 之前只认 `_EMOJI_FONT_CANDIDATES` 里的固定路径，而本项目的镜像把
       emoji 字体放在 `/usr/share/fonts/truetype/nekro/emoji-segoe.ttf` ——
       不在候选表里，于是「数据目录没放 emoji 字体」的实例会白白判定「没有 emoji 字体」，
       卡片里的 emoji 全成豆腐块。现在先按名字扫目录，扫不到才退固定候选。
    """
    roots = [
        "/usr/share/fonts",
        "/usr/local/share/fonts",
        "/usr/share/fonts/truetype",
        str(Path.home() / ".local/share/fonts"),
        "C:/Windows/Fonts",
    ]
    out: list = []
    seen = set()
    for root in roots:
        try:
            rp = Path(root)
            if not rp.is_dir():
                continue
            for ext in ("*.ttf", "*.ttc", "*.otf"):
                for f in sorted(rp.rglob(ext)):
                    low = f.name.lower()
                    if "emoji" in low and str(f) not in seen:
                        seen.add(str(f))
                        out.append(str(f))
        except Exception:  # noqa: BLE001
            continue
    return out


def set_emoji_font_override(path: str = "") -> None:
    """设置手动指定的 emoji 字体（空 = 自动探测）；值变了才清缓存。"""
    global _emoji_font_override, _emoji_font_checked, _emoji_font_key
    new = str(path or "").strip()
    if new == _emoji_font_override:
        return
    _emoji_font_override = new
    with _FONT_LOCK:
        _emoji_font_checked = False
        _emoji_font_key = None
        try:
            _emoji_font_cache.clear()
        except Exception:  # noqa: BLE001
            pass


def _find_emoji_font(font_dir: str = "") -> str | None:
    """查找 emoji 字体：数据目录 fonts 下 *emoji* → 系统目录扫 *emoji* → 固定候选。

    找到后会像正文字体一样**落地**（复制进插件数据目录 + 写 `.emoji_font_pin.json`），
    这样容器重建 / 换镜像后 emoji 不会突然变豆腐块。
    缓存按 font_dir 做 key，失败不锁死。
    """
    global _emoji_font_path, _emoji_font_checked, _emoji_font_key, _emoji_font_color
    _key = (font_dir.strip(), _emoji_font_override)
    if _emoji_font_checked and _emoji_font_key == _key:
        return _emoji_font_path

    found: str | None = None
    # ① 手动指定优先（配置项 RANK_CARD_EMOJI_FONT / WebUI 选择）
    if _emoji_font_override:
        ov = Path(_emoji_font_override)
        if ov.is_file():
            found = str(ov)
        else:
            logger.warning(f"指定的 emoji 字体不存在，回退自动探测: {_emoji_font_override}")
    if not found and font_dir.strip():
        d = Path(font_dir.strip())
        if d.is_dir():
            local = [
                str(f)
                for ext in ("*.ttf", "*.ttc", "*.otf")
                for f in sorted(d.rglob(ext))
                if "emoji" in f.name.lower()
            ]
            found = _pick_emoji_font(local)
    if not found:
        found = _pick_emoji_font(_scan_system_emoji_fonts())
    if not found:
        found = _pick_emoji_font(list(_EMOJI_FONT_CANDIDATES))

    if not found:
        logger.warning("未找到 emoji 字体，卡片中的 emoji 将显示为豆腐块（可放 *emoji*.ttf 到数据目录 fonts/）")
        return None

    # 落地：系统 emoji 字体复制进数据目录，之后一律用副本
    found = _pin_font(found, font_dir.strip(), _EMOJI_PIN_MARKER)
    _emoji_font_path = found
    _emoji_font_color = _font_has_color(found)
    _emoji_font_key = _key
    _emoji_font_checked = True
    if not _emoji_font_color:
        logger.info(f"emoji 字体为单色（{Path(found).name}），将按单色渲染")
    return _emoji_font_path


# 散布在"非 emoji 区"里、但确实算 emoji 的孤立符号
_EMOJI_EXTRA = frozenset({
    0x2194, 0x2195, 0x2196, 0x2197, 0x2198, 0x2199,   # ↔ ↕ ↖ ↗ ↘ ↙
    0x21A9, 0x21AA,                                    # ↩ ↪
    0x2934, 0x2935,                                    # ⤴ ⤵
    0x2B05, 0x2B06, 0x2B07,                            # ⬅ ⬆ ⬇
    0x2B1B, 0x2B1C, 0x2B50, 0x2B55,                    # ⬛ ⬜ ⭐ ⭕
    0x200D, 0x20E3, 0x3030, 0x303D, 0x3297, 0x3299,
    0x00A9, 0x00AE, 0x2122,
})


def _is_emoji_cp(cp: int) -> bool:
    """判断码点是否属于 emoji（含 ZWJ / 变体选择符 / 肤色 / 国旗）。

    ⚠️ 不要用 0x2190–0x21FF（箭头）、0x2900–0x297F 这类宽区间：
       它们绝大多数不是 emoji（← → ↑ ↓ ⇒ …），交给 emoji 字体渲染会变
       豆腐块或风格错乱，而中文字体本来能画好。这里只覆盖真正的 emoji 区，
       外加一个显式白名单 _EMOJI_EXTRA。
    """
    return (
        0x1F300 <= cp <= 0x1FAFF
        or 0x1F000 <= cp <= 0x1F2FF
        or 0x1F1E6 <= cp <= 0x1F1FF
        or 0x2600 <= cp <= 0x27BF
        or 0xFE00 <= cp <= 0xFE0F
        or 0x1F3FB <= cp <= 0x1F3FF
        or cp in _EMOJI_EXTRA
    )


def _split_emoji_runs(text: str) -> list:
    """把文本切成 (片段, 是否 emoji) 列表。"""
    runs: list = []
    buf = ""
    cur = False
    for ch in text:
        e = _is_emoji_cp(ord(ch))
        if buf and e != cur:
            runs.append((buf, cur))
            buf = ""
        cur = e
        buf += ch
    if buf:
        runs.append((buf, cur))
    return runs


# 同字号下彩色 emoji 的墨迹明显大于汉字（实测 25px 时 ⚽ 宽 34px / 汉字 25px），
# 直接同字号画出来会「大一号」，所以按这个比例缩一点。
_EMOJI_SIZE_RATIO = 0.86


def _emoji_font_for(font: Any) -> Any | None:
    """按主字体字号取 emoji 字体实例（缩放后同字号缓存）；不可用返回 None。"""
    if not _emoji_font_path:
        return None
    size = getattr(font, "size", None)
    if not size:
        return None
    key = max(8, int(round(float(size) * _EMOJI_SIZE_RATIO)))
    if key not in _emoji_font_cache:
        try:
            from PIL import ImageFont

            _emoji_font_cache[key] = ImageFont.truetype(_emoji_font_path, key)
        except Exception:  # noqa: BLE001
            _emoji_font_cache[key] = None
    return _emoji_font_cache[key]


# ============ 缺字回退（按字借字体）============
# 场景：包里自带的是 NotoSansSC **子集**（2.3.0 起已扩到 GB18030 双字节区全量）。
# 2.2.32 之前它只有 GB2312 一级 3755 字，昵称里的二级字（娅 / 傩 / 喵 / 來 / ー …）
# 没有字形、PIL 直接画空白，于是「辰光归娅」印成「辰光归」——用户看到的就是
# 「有些字没渲染出来」。GB18030 之外的生僻字仍靠这里的逐字借字兜底。
# 这里做逐字回退：主字体缺哪个字，就从备选字体（数据目录 fonts/、系统 CJK 字体、
# 包内子集）里找有这个字的来画；emoji 仍走彩色 emoji 字体。
_FALLBACK_FONT_PATHS: list = []
_FALLBACK_READY = False
_fallback_font_cache: dict = {}     # (path, size) -> font
_cov_cache: dict = {}               # (path, ch) -> bool
_unresolved_chars: set = set()      # 主字体+备选都没字形的字（只警告一次/字）


# path → cmap 码点集合（读不到时为 None，表示要用兜底判定）
_cmap_cache: dict = {}


def _font_cmap(path: str):
    """读字体的 cmap 码点集合（TTC 取第一个）；读不到返回 None。

    ⚠️ 2.3.10 之前这里用 `ImageFont` 的 `get_char_index()` —— 本项目的 Pillow 构建里
       `FreeTypeFont.font` **根本没有这个方法**（新版 Pillow 把它去掉了），异常被吞进兜底
       分支后又拿 `getmask()` 判断，而带 `.notdef` 轮廓的字体对任何码点都返回非空 mask，
       于是「有字形」恒为真：能力探测把 Noto Color Emoji 判成「汉字 100%、emoji 0%」，
       逐字借字回退也一直是死的。改用 fontTools 直接读 cmap，准确且与字号无关。
    """
    key = str(path)
    if key in _cmap_cache:
        return _cmap_cache[key]
    cmap = None
    try:
        from fontTools.ttLib import TTCollection, TTFont

        if key.lower().endswith(".ttc"):
            fonts = list(TTCollection(key, lazy=True).fonts[:1])
        else:
            fonts = [TTFont(key, lazy=True)]
        acc = set()
        for f in fonts:
            try:
                acc.update(f.getBestCmap().keys())
            except Exception:  # noqa: BLE001
                continue
        cmap = frozenset(acc) if acc else None
    except Exception:  # noqa: BLE001
        cmap = None
    _cmap_cache[key] = cmap
    return cmap


def _char_in_font(path: str, ch: str) -> bool:
    """该字体是否含这个字的字形（按 (路径, 字) 缓存，字号无关）。"""
    key = (path, ch)
    hit = _cov_cache.get(key)
    if hit is not None:
        return hit

    ok = False
    cmap = _font_cmap(path)
    if cmap is not None:
        ok = ord(ch) in cmap
    else:
        # 兜底：把目标字与「私有区未映射字」（必然落到 .notdef）的 mask 比一比。
        # 只判断「非空」是不够的 —— 那正是之前恒为真的原因。
        try:
            from PIL import ImageFont

            probe = _fallback_font_cache.get((path, 16))
            if probe is None:
                probe = ImageFont.truetype(path, 16)
                _fallback_font_cache[(path, 16)] = probe
            try:
                mine = probe.getmask(ch).getbbox()
                notdef = probe.getmask("\ufffe").getbbox()
                ok = mine is not None and mine != notdef
            except Exception:  # noqa: BLE001
                ok = False
        except Exception:  # noqa: BLE001
            ok = False
    _cov_cache[key] = ok
    return ok


def _configure_fallback_fonts(font_dir: str = "") -> list:
    """收集备选字体路径（数据目录 fonts/ → 系统 CJK 候选目录）。"""
    global _FALLBACK_FONT_PATHS, _FALLBACK_READY
    if _FALLBACK_READY:
        return _FALLBACK_FONT_PATHS
    roots = []
    if font_dir.strip():
        roots.append(Path(font_dir))
    for cand in _FONT_CANDIDATES:
        p = Path(cand)
        roots.append(p if p.is_dir() else p.parent)
    # 2.3.1 起包里不带字体，备选链只有数据目录 + 系统目录
    paths, seen = [], set()
    for root in roots:
        try:
            if not root.is_dir():
                continue
            for ext in ("*.ttf", "*.ttc", "*.otf"):
                for f in sorted(root.rglob(ext)):
                    sp = str(f)
                    if sp not in seen:
                        seen.add(sp)
                        paths.append(sp)
        except Exception:  # noqa: BLE001
            continue
    _FALLBACK_FONT_PATHS = paths
    _FALLBACK_READY = True
    return paths


def _fallback_font_for(font: Any, ch: str) -> Any | None:
    """给某个字找个有字形的字体实例（与主字体同字号）；找不到返回 None。"""
    size = getattr(font, "size", None)
    if not size:
        return None
    primary = getattr(font, "path", None)
    for path in _FALLBACK_FONT_PATHS:
        if path == primary or not _char_in_font(path, ch):
            continue
        key = (path, int(size))
        f = _fallback_font_cache.get(key)
        if f is None:
            try:
                from PIL import ImageFont

                f = ImageFont.truetype(path, int(size))
            except Exception:  # noqa: BLE001
                f = None
            _fallback_font_cache[key] = f
        if f is not None:
            return f
    return None


# 渲染倍率：卡片按逻辑尺寸（680 宽设计稿）布局，再以此倍率超采样渲染输出。
# 2.0 = Retina 级清晰度；调大更清晰但图片更大（3.0 时约 2.25 倍像素量）。
_RENDER_SCALE = 1.5   # 超采样倍率（扩展：2.0→1.5，文件约降 40%；可被配置覆盖）


class _EmojiDraw:
    """包装 ImageDraw：text/textlength/textbbox 自动按**字体覆盖**切段。

    · emoji 段 → 彩色 emoji 字体（embedded_color 上色）
    · 主字体缺字形的字 → 备选字体（逐字借字，解决子集字体印不出二级字）
    · 其余 → 主字体
    其余方法透传。
    """

    def __init__(self, draw: Any, font_dir: str = ""):
        self._draw = draw
        self._font_dir = font_dir or ""
        _find_emoji_font(self._font_dir)
        _configure_fallback_fonts(self._font_dir)

    @staticmethod
    def _covered(font: Any, ch: str) -> bool:
        path = getattr(font, "path", None)
        if not path:
            return True
        return _char_in_font(str(path), ch)

    def _runs(self, text: Any, font: Any):
        """切成 [(片段, 字体, 是否emoji)]；整段都用主字体时返回 None（走快路径）。"""
        if font is None or not isinstance(text, str) or not text:
            return None
        ef = _emoji_font_for(font)
        out: list = []
        buf = ""
        cur_font = None
        cur_emoji = False
        changed = False
        for ch in text:
            f, is_e = font, False
            # emoji 先判，且**不看主字体有没有单色字形**：⚽ 🏀 ✨ 这类符号在 msyh/LXGW/
            # 本包子集里都有黑白字形，若让主字体先命中，彩色 emoji 就全变成黑白或空白。
            if ef is not None and _is_emoji_cp(ord(ch)):
                f, is_e, changed = ef, True, True
            elif not self._covered(font, ch):
                alt = _fallback_font_for(font, ch)
                if alt is not None:
                    f, changed = alt, True
                elif ch not in _unresolved_chars:
                    # 所有候选字体都没有这个字形 —— 以前会静默画成空白，无人知晓。
                    _unresolved_chars.add(ch)
                    logger.warning(
                        f"字体缺字形且无备选可用: {ch!r} (U+{ord(ch):04X}) —— "
                        f"把含该字的完整字体放到插件数据目录 fonts/ 即可修复",
                    )
            if cur_font is f and cur_emoji == is_e:
                buf += ch
            else:
                if buf:
                    out.append((buf, cur_font, cur_emoji))
                buf, cur_font, cur_emoji = ch, f, is_e
        if buf:
            out.append((buf, cur_font, cur_emoji))
        return out if changed else None

    def _emoji_dy(self, seg: str, ef: Any, base_font: Any) -> float:
        """把 emoji 的墨迹垂直居中到汉字墨迹上。

        ⚠️ 同字号下 emoji 的 ink 顶部比汉字高一截（实测 25px 时 辰=5..27 / ⚽=-3..23），
           直接按同一基线画会整体偏上、看着"飘"在文字上方。
        """
        try:
            bb_e = self._draw.textbbox((0, 0), seg, font=ef)
            bb_b = self._draw.textbbox((0, 0), "国", font=base_font)
            return ((bb_b[1] + bb_b[3]) - (bb_e[1] + bb_e[3])) / 2
        except Exception:  # noqa: BLE001
            return 0

    def text(self, xy, text, fill=None, font=None, **kwargs):
        runs = self._runs(text, font)
        if not runs:
            return self._draw.text(xy, text, fill=fill, font=font, **kwargs)
        x = xy[0]
        for seg, f, is_e in runs:
            dy = self._emoji_dy(seg, f, font) if is_e else 0
            if is_e and _emoji_font_color:
                self._draw.text((x, xy[1] + dy), seg, font=f, embedded_color=True, **kwargs)
            else:
                self._draw.text((x, xy[1] + dy), seg, fill=fill, font=f, **kwargs)
            x += self._draw.textlength(seg, font=f)
        return None

    def textlength(self, text, font=None, **kwargs):
        runs = self._runs(text, font)
        if not runs:
            return self._draw.textlength(text, font=font, **kwargs)
        return sum(self._draw.textlength(seg, font=f, **kwargs) for seg, f, _e in runs)

    def textbbox(self, xy, text, font=None, **kwargs):
        runs = self._runs(text, font)
        if not runs:
            return self._draw.textbbox(xy, text, font=font, **kwargs)
        box = None
        x = xy[0]
        for seg, f, _e in runs:
            _dy = self._emoji_dy(seg, f, font) if _e else 0
            bb = self._draw.textbbox((x, xy[1] + _dy), seg, font=f, **kwargs)
            box = bb if box is None else (
                min(box[0], bb[0]), min(box[1], bb[1]),
                max(box[2], bb[2]), max(box[3], bb[3]),
            )
            x += self._draw.textlength(seg, font=f)
        return box

    def __getattr__(self, name):
        return getattr(self._draw, name)


class _ScaledDraw:
    """超采样包装：外部按逻辑坐标绘制，内部放大 scale 倍输出高分辨率。

    - 绘制类方法：坐标 / radius / 线宽 / 字体字号 全部 × scale
    - textlength / textbbox：输入按逻辑坐标转换，返回值 ÷ scale（保持逻辑坐标系一致）
    """

    def __init__(self, draw: Any, scale: float, image_font_mod: Any):
        self._draw = draw
        self._s = float(scale)
        self._font_mod = image_font_mod
        self._font_cache: dict = {}

    def _font(self, font: Any) -> Any:
        if font is None:
            return None
        # ⚠️ 不能用 id(font) 做 key：_fit_font 在缩字号循环里不断造新字体，
        #    旧对象立即被回收，CPython 会复用 id → 命中上一轮的缓存，
        #    拿到错误字号的字体（实测 id 确实会重复）。用 (path, size) 才稳。
        _p = getattr(font, "path", None)
        _sz = getattr(font, "size", None)
        key = (_p, _sz) if (_p and _sz) else ("id", id(font))
        if key in self._font_cache:
            return self._font_cache[key]
        scaled = font
        path = getattr(font, "path", None)
        size = getattr(font, "size", None)
        if path and size:
            try:
                scaled = self._font_mod.truetype(path, size * self._s)
            except Exception:  # noqa: BLE001
                scaled = font
        self._font_cache[key] = scaled
        return scaled

    def _xy(self, xy: Any) -> Any:
        """坐标缩放：支持 (x,y) / [x0,y0,x1,y1] / [(x,y), ...]，结果取整（PIL 图形 API 要求 int）。"""
        s = self._s
        if isinstance(xy, (list, tuple)):
            if len(xy) and isinstance(xy[0], (list, tuple)):
                return [self._xy(p) for p in xy]
            return tuple(int(round(v * s)) for v in xy)
        return xy

    def _v(self, v: Any) -> Any:
        """标量缩放（radius / 线宽等，PIL 要求 int）。"""
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            return int(round(v * self._s))
        return v

    # ---- 文本 ----
    def text(self, xy, text, fill=None, font=None, stroke_width=0, **kw):
        return self._draw.text(
            self._xy(xy), text, fill=fill, font=self._font(font), stroke_width=self._v(stroke_width), **kw
        )

    def textlength(self, text, font=None, **kw):
        return self._draw.textlength(text, font=self._font(font), **kw) / self._s

    def textbbox(self, xy, text, font=None, stroke_width=0, **kw):
        bb = self._draw.textbbox(
            self._xy(xy), text, font=self._font(font), stroke_width=self._v(stroke_width), **kw
        )
        s = self._s
        return (bb[0] / s, bb[1] / s, bb[2] / s, bb[3] / s)

    # ---- 图形 ----
    def rounded_rectangle(self, xy, radius=0, fill=None, outline=None, width=1, **kw):
        return self._draw.rounded_rectangle(
            self._xy(xy), radius=self._v(radius), fill=fill, outline=outline, width=self._v(width), **kw
        )

    def rectangle(self, xy, fill=None, outline=None, width=1, **kw):
        return self._draw.rectangle(
            self._xy(xy), fill=fill, outline=outline, width=self._v(width), **kw
        )

    def ellipse(self, xy, fill=None, outline=None, width=1, **kw):
        return self._draw.ellipse(self._xy(xy), fill=fill, outline=outline, width=self._v(width), **kw)

    def line(self, xy, fill=None, width=0, **kw):
        return self._draw.line(self._xy(xy), fill=fill, width=self._v(width), **kw)

    def polygon(self, xy, fill=None, outline=None, width=1, **kw):
        return self._draw.polygon(
            self._xy(xy), fill=fill, outline=outline, width=self._v(width), **kw
        )

    def __getattr__(self, name):
        return getattr(self._draw, name)


def _make_draw(img: Any, image_draw_mod: Any, font_dir: str = "", scale: float = 1.0) -> Any:
    """创建支持 emoji + 高分辨率缩放（超采样）的 draw 对象。

    scale > 1 时：绘制坐标与字号放大 scale 倍，而 textlength/textbbox 返回
    逻辑坐标系的数值（除以 scale），因此现有布局代码无需任何改动。
    """
    draw = image_draw_mod.Draw(img)
    try:
        # ⚠️ 必须**无条件**包装：以前只有找到 emoji 字体才包，于是「裸机上没有 emoji 字体」
        #    连缺字回退也一起失效（两件事被绑在一起了）。
        draw = _EmojiDraw(draw, font_dir)
    except Exception:  # noqa: BLE001
        logger.debug("draw 包装失败，回退原生 draw", exc_info=True)
    if scale and scale != 1:
        try:
            from PIL import ImageFont

            return _ScaledDraw(draw, scale, ImageFont)
        except Exception:  # noqa: BLE001
            logger.debug("缩放包装失败，回退原倍率", exc_info=True)
    return draw


def _draw_centered(draw: Any, box: list, text: str, font: Any, fill: Any) -> None:
    """在矩形 box=[x0,y0,x1,y1] 内水平+垂直居中绘制文本。

    ⚠️ 必须减去 ink bbox 的左上偏移：`textbbox((0,0), ...)` 的 top 通常是
    ascender 空隙（实测雅黑 11px 下 top=3~4、8px 字高），不减会让文字明显偏下
    （旧代码用 `-1` 手感补偿，实测偏下 2~3px）。left 也一并减，兼容带左边距的字体。
    """
    x0, y0, x1, y1 = box
    bb = draw.textbbox((0, 0), text, font=font)
    w, h = bb[2] - bb[0], bb[3] - bb[1]
    draw.text(
        (x0 + (x1 - x0 - w) / 2 - bb[0], y0 + (y1 - y0 - h) / 2 - bb[1]),
        text, font=font, fill=fill,
    )


def _truncate(draw: Any, text: str, font: Any, max_width: int) -> str:
    """按像素宽度截断文本，超出加省略号。"""
    if draw.textlength(text, font=font) <= max_width:
        return text
    while text and draw.textlength(text + "…", font=font) > max_width:
        text = text[:-1]
    return text + "…"


def _vertical_bg(PIL, width: int, height: int):
    """三段暖色竖向渐变。

    先算 1px 宽的色列再 NEAREST 横向拉伸（每行是纯色，拉伸无损失），
    比逐像素双循环快约两个数量级。
    """
    stops = [(0.0, _BG_TOP), (0.49, _BG_MID), (1.0, _BG_BOTTOM)]
    col = PIL.Image.new("RGB", (1, height))
    px = col.load()
    for y in range(height):
        t = y / max(height - 1, 1)
        c = _BG_BOTTOM
        for i in range(len(stops) - 1):
            t0, c0 = stops[i]
            t1, c1 = stops[i + 1]
            if t0 <= t <= t1:
                k = (t - t0) / max(t1 - t0, 1e-9)
                c = tuple(int(c0[j] + (c1[j] - c0[j]) * k) for j in range(3))
                break
        px[0, y] = c
    return col.resize((width, height), PIL.Image.NEAREST)


# 是否正在用「自定义图片」当背景（线程内标记，面板据此加厚 tint）
_bg_ctx = threading.local()
# (路径, mtime, w, h, blur, dim) → 已处理好的背景图（排行榜/总览高度随人数变，会命中多个尺寸）
_BG_CACHE: dict = {}
_BG_CACHE_MAX = 8
# 图片背景的全局薄纱浓度（0 = 不蒙）。0.22 是实测出来的折中：标题区亮度够、照片还看得出内容
_BG_VEIL_ALPHA = 0.22


def reset_background_cache() -> None:
    """换了背景图或调了参数后清缓存（webui 改配置时调用）。"""
    _BG_CACHE.clear()


def _cover_fit(PIL, img, width: int, height: int):
    """等比铺满再居中裁剪（cover），保证不拉伸、不变形。"""
    iw, ih = img.size
    if iw <= 0 or ih <= 0:
        return img
    k = max(width / iw, height / ih)
    nw, nh = max(1, int(round(iw * k))), max(1, int(round(ih * k)))
    img = img.resize((nw, nh), PIL.Image.LANCZOS)
    left = max(0, (nw - width) // 2)
    top = max(0, (nh - height) // 2)
    return img.crop((left, top, left + width, top + height))


def _mean_luma(PIL, img) -> float:
    """平均亮度（0~1，抽样算，别整图遍历）。"""
    small = img.convert("L").resize((32, 32))
    px = list(small.getdata())
    return (sum(px) / len(px) / 255.0) if px else 0.5


def _background(PIL, width: int, height: int, *, path: str = "", blur: float = 14.0,
                dim: int = 0, scale: float = 1.0):
    """卡片背景：有自定义图就「cover-fit + 模糊 + 压暗/提亮」，否则回落到暖色渐变。

    dim: 0 = 自动（按背景平均亮度往「浅底」方向调，专门保文字可读）；
         负数压暗、正数提亮，绝对值越大越狠（上限 100）。
    任何异常都回落到渐变 —— 背景图绝不能把整张卡搞挂。
    """
    _bg_ctx.is_image = False
    src = str(path or "").strip()
    if not src:
        return _vertical_bg(PIL, width, height)
    try:
        p = Path(src)
        if not p.is_file():
            logger.warning(f"自定义背景不存在，回落到渐变: {src}")
            return _vertical_bg(PIL, width, height)
        key = (str(p), int(p.stat().st_mtime), int(width), int(height),
               round(float(blur), 1), int(dim))
        hit = _BG_CACHE.get(key)
        if hit is not None:
            _bg_ctx.is_image = True
            return hit.copy()
        from PIL import ImageFilter, ImageOps

        img = ImageOps.exif_transpose(PIL.Image.open(p)).convert("RGB")
        limit = max(width, height) * 2
        if max(img.size) > limit:          # 4K/8K 原图先缩，避免每帧 decode 大图
            img.thumbnail((limit, limit), PIL.Image.LANCZOS)
        bg = _cover_fit(PIL, img, width, height)
        radius = float(blur) * max(float(scale), 1.0)
        if radius > 0.4:
            bg = bg.filter(ImageFilter.GaussianBlur(radius=radius))
        weight = _mean_luma(PIL, bg)
        d = int(dim)
        if d == 0:
            # 目标亮度 0.72：卡片是「浅底 + 深色字 + 磨砂面板」，偏暗会脏、偏亮会飘
            d = int(round(max(-22.0, min(26.0, (0.72 - weight) * 100))))
        if d:
            overlay = PIL.Image.new("RGB", bg.size, (255, 255, 255) if d > 0 else (0, 0, 0))
            # 最多 40% 混合：再多就把照片压成一片灰白
            bg = PIL.Image.blend(bg, overlay, min(abs(d), 100) / 250.0)
        # 全局薄纱（暖白）：标题/页脚这类**直接压在照片上**的文字不会带面板，
        # 光靠面板加厚救不了它们，所以整张图层再蒙一层薄纱保底对比
        if _BG_VEIL_ALPHA > 0:
            veil = PIL.Image.new("RGB", bg.size, (255, 252, 246))
            bg = PIL.Image.blend(bg, veil, min(float(_BG_VEIL_ALPHA), 0.6))
        if len(_BG_CACHE) >= _BG_CACHE_MAX:
            try:
                _BG_CACHE.pop(next(iter(_BG_CACHE)))
            except Exception:  # noqa: BLE001
                _BG_CACHE.clear()
        _BG_CACHE[key] = bg.copy()
        _bg_ctx.is_image = True
        return bg
    except Exception:  # noqa: BLE001
        logger.warning(f"自定义背景渲染失败，回落到渐变: {src}", exc_info=True)
        _bg_ctx.is_image = False
        return _vertical_bg(PIL, width, height)


# ============ 图层合成（2.4.0）============
# 卡片外观不再是一堆散装参数，而是一串**图层**（JSON 数组），按顺序从底到顶合成：
#   back  阶段：gradient / image / veil / scrim / blobs / tint
#   front 阶段：border / text / sticker(image) / grain
# 为什么分层：2.3.12 只有「一张背景图 + 模糊 + 明暗」三个旋钮，想加个 logo、想给不同卡配不同
# 底图、想给照片压一条渐变压暗带，都得再加参数；层模型把这些统一成「一层一个 dict」。
#
# 坐标约定：x/y/w/h 一律用 0~1 的相对值（跟渲染倍率、卡片尺寸无关），像素换算在合成时做。

_LAYER_DEFAULT = [
    {"type": "gradient", "stage": "back", "stops": [[0.0, "#fdf6ec"], [0.49, "#fbe9d2"], [1.0, "#f6d5b8"]]},
    {"type": "blobs", "stage": "back", "alpha": 1.0},
]
# 用户自己写了 image 层但没写 veil 时，自动补这层薄纱（否则浅色面板上文字容易被照片纹路吃掉）
_LAYER_AUTO_VEIL = {"type": "veil", "stage": "back", "color": "#fffcf6", "alpha": 0.18}
_LAYER_TYPES = (
    "gradient", "image", "veil", "scrim", "blobs", "tint",
    "border", "text", "sticker", "grain",
)


def _hex_to_rgb(value, fallback=(255, 255, 255)):
    """#rgb / #rrggbb / #rrggbbaa → (r,g,b,a)。解析失败回落 fallback（alpha=255）。"""
    try:
        t = str(value or "").strip().lstrip("#")
        if len(t) == 3:
            t = "".join(c * 2 for c in t)
        if len(t) not in (6, 8):
            return (fallback[0], fallback[1], fallback[2], 255)
        r, g, b = int(t[0:2], 16), int(t[2:4], 16), int(t[4:6], 16)
        a = int(t[6:8], 16) if len(t) == 8 else 255
        return (r, g, b, a)
    except Exception:  # noqa: BLE001
        return (fallback[0], fallback[1], fallback[2], 255)


def _alpha(value, default=1.0) -> float:
    try:
        return max(0.0, min(1.0, float(value)))
    except (TypeError, ValueError):
        return default


def _rel(value, total: int, default: float = 0.0) -> int:
    """0~1 相对坐标 → 像素（也接受已经大于 1 的绝对像素值）。"""
    try:
        v = float(value)
    except (TypeError, ValueError):
        v = default
    return int(round(v * total)) if v <= 1.0 else int(round(v))


def normalize_layers(layers) -> list:
    """清洗层列表：丢掉不认识/结构不对的层，rest 用默认值补全。"""
    out: list = []
    if not isinstance(layers, (list, tuple)):
        return out
    for raw_layer in layers:
        if not isinstance(raw_layer, dict):
            continue
        t = str(raw_layer.get("type") or "").strip().lower()
        if t not in _LAYER_TYPES:
            continue
        layer = dict(raw_layer)
        layer["type"] = t
        layer["stage"] = "front" if str(raw_layer.get("stage") or "back").lower() == "front" else "back"
        if not layer.get("enabled", True):
            continue
        out.append(layer)
    return out


def resolve_layer_sources(layers, base_dir: str = "") -> list:
    """把 image / sticker 层的相对路径解析成绝对路径。

    模板里只存「文件名」最稳（换机器、换挂载点都不怕），渲染前按数据目录解析：
    先试 `<base>/<src>`，再试 `<base>/backgrounds_custom/<src>`。
    """
    out: list = []
    base = str(base_dir or "").strip()
    for layer in normalize_layers(layers):
        item = dict(layer)
        if item["type"] in ("image", "sticker"):
            src = str(item.get("src") or "").strip()
            if src and base and not Path(src).is_absolute():
                for cand in (Path(base) / src, Path(base) / "backgrounds_custom" / src):
                    if cand.is_file():
                        item["src"] = str(cand)
                        break
        out.append(item)
    return out


def _layer_list(layers):
    """把外部传进来的 layers 统一成「已清洗的列表」；None 表示调用方没给模板。

    接受：None / list / {"layers": [...]} / JSON 字符串。
    """
    if layers is None:
        return None
    if isinstance(layers, str):
        try:
            layers = json.loads(layers or "[]")
        except Exception:  # noqa: BLE001
            return None
    if isinstance(layers, dict):
        layers = layers.get("layers") or []
    if not isinstance(layers, (list, tuple)):
        return None
    return normalize_layers(layers)


def _split_layers(layers, has_bg: bool = False):
    """拆成 (back, front) 两份。

    `layers=None`（没配模板）时用内置默认：有自定义背景图就只留空 back（照片本身就是背景），
    否则沿用「暖色渐变 + 光斑」，保证老配置升级后观感不变。
    """
    lay = _layer_list(layers)
    if lay is None:
        return ([] if has_bg else [dict(x) for x in _LAYER_DEFAULT]), []
    back = [x for x in lay if x["stage"] == "back"]
    front = [x for x in lay if x["stage"] == "front"]
    # 自己写了图片层却没写薄纱 → 自动补一层（可显式写 veil 覆盖/调淡）
    if any(x["type"] == "image" for x in back) and not any(x["type"] == "veil" for x in back):
        idx = max(i for i, x in enumerate(back) if x["type"] == "image")
        back.insert(idx + 1, dict(_LAYER_AUTO_VEIL))
    return back, front


def _compose_layers(PIL, img, layers: list, *, stage: str, base_ctx: dict | None = None):
    """把指定 stage 的图层按顺序叠到 img 上，返回新图。任何一层出错只跳过该层。"""
    ctx = base_ctx or {}
    for layer in layers:
        if layer.get("stage") != stage:
            continue
        t = layer["type"]
        try:
            alpha = _alpha(layer.get("alpha"), 1.0)
            if alpha <= 0:
                continue
            if t == "gradient":
                stops = layer.get("stops") or [[0.0, "#fdf6ec"], [1.0, "#f6d5b8"]]
                horizontal = bool(layer.get("horizontal"))
                layer_img = _gradient_layer(PIL, img.size, stops, horizontal)
                img = PIL.Image.blend(img, layer_img, alpha) if alpha < 1 else layer_img
            elif t == "image":
                src = str(layer.get("src") or "").strip()
                if not src or not Path(src).is_file():
                    continue
                _bg_ctx.is_image = True   # 面板据此加厚 tint
                lay = _image_layer(PIL, img.size, src, layer, ctx.get("scale", 1.0))
                if lay is None:
                    continue
                img = PIL.Image.blend(img, lay, alpha) if alpha < 1 else lay
            elif t == "veil":
                color = _hex_to_rgb(layer.get("color") or "#fffcf6")
                lay = PIL.Image.new("RGB", img.size, color[:3])
                img = PIL.Image.blend(img, lay, _alpha(layer.get("alpha"), 0.22))
            elif t == "scrim":
                img = _scrim_layer(PIL, img, layer)
            elif t == "blobs":
                blobs = _decorative_blobs(PIL, img.size[0], img.size[1])
                img = PIL.Image.blend(img, blobs, alpha) if alpha < 1 else blobs
            elif t == "tint":
                color = _hex_to_rgb(layer.get("color") or "#ffd9b0")
                lay = PIL.Image.new("RGB", img.size, color[:3])
                img = PIL.Image.blend(img, lay, _alpha(layer.get("alpha"), 0.1))
            elif t == "border":
                img = _border_layer(PIL, img, layer)
            elif t == "text":
                img = _text_layer(PIL, img, layer, ctx)
            elif t == "sticker":
                img = _sticker_layer(PIL, img, layer, ctx)
            elif t == "grain":
                img = _grain_layer(PIL, img, layer)
        except Exception:  # noqa: BLE001
            logger.warning(f"图层合成失败（已跳过该层）: {layer.get('type')}", exc_info=True)
            continue
    return img


def _gradient_layer(PIL, size, stops, horizontal: bool = False):
    """任意 stop 数的线性渐变（竖向或横向），先算 1px 再拉伸。"""
    w, h = size
    length = w if horizontal else h
    parsed = []
    for st in stops:
        try:
            pos = max(0.0, min(1.0, float(st[0])))
            parsed.append((pos, _hex_to_rgb(st[1])[:3]))
        except Exception:  # noqa: BLE001
            continue
    if not parsed:
        parsed = [(0.0, (253, 246, 236)), (1.0, (246, 213, 184))]
    parsed.sort(key=lambda x: x[0])
    strip = PIL.Image.new("RGB", (1, length)) if not horizontal else PIL.Image.new("RGB", (length, 1))
    px = strip.load()
    for i in range(length):
        t = i / max(length - 1, 1)
        c = parsed[-1][1]
        if t <= parsed[0][0]:
            c = parsed[0][1]
        else:
            for k in range(len(parsed) - 1):
                p0, c0 = parsed[k]
                p1, c1 = parsed[k + 1]
                if p0 <= t <= p1:
                    f = (t - p0) / max(p1 - p0, 1e-9)
                    c = tuple(int(c0[j] + (c1[j] - c0[j]) * f) for j in range(3))
                    break
        if horizontal:
            px[i, 0] = c
        else:
            px[0, i] = c
    return strip.resize((w, h), PIL.Image.NEAREST)


def _image_layer(PIL, size, src: str, layer: dict, scale: float):
    """图片层：cover/contain/stretch 三种填充 + 可选模糊。"""
    from PIL import ImageFilter, ImageOps

    w, h = size
    img = ImageOps.exif_transpose(PIL.Image.open(src)).convert("RGB")
    limit = max(w, h) * 2
    if max(img.size) > limit:
        img.thumbnail((limit, limit), PIL.Image.LANCZOS)
    fit = str(layer.get("fit") or "cover").lower()
    if fit == "stretch":
        out = img.resize((w, h), PIL.Image.LANCZOS)
    elif fit == "contain":
        k = min(w / img.size[0], h / img.size[1])
        nw, nh = max(1, int(img.size[0] * k)), max(1, int(img.size[1] * k))
        out = PIL.Image.new("RGB", (w, h), _hex_to_rgb(layer.get("pad") or "#000000")[:3])
        out.paste(img.resize((nw, nh), PIL.Image.LANCZOS), ((w - nw) // 2, (h - nh) // 2))
    else:  # cover
        out = _cover_fit(PIL, img, w, h)
    radius = float(layer.get("blur") or 0) * max(float(scale), 1.0)
    if radius > 0.4:
        out = out.filter(ImageFilter.GaussianBlur(radius=radius))
    # 图片层的明暗补偿：没写 dim 就自动（与 _background 的口径一致，保文字可读）
    raw_dim = layer.get("dim")
    if raw_dim is None or str(raw_dim).strip() == "":
        weight = _mean_luma(PIL, out)
        dim = int(round(max(-22.0, min(26.0, (0.72 - weight) * 100))))
    else:
        try:
            dim = int(raw_dim)
        except (TypeError, ValueError):
            dim = 0
    if dim:
        over = PIL.Image.new("RGB", out.size, (255, 255, 255) if dim > 0 else (0, 0, 0))
        out = PIL.Image.blend(out, over, min(abs(dim), 100) / 250.0)
    return out


def _scrim_layer(PIL, img, layer: dict):
    """线性压暗/提亮带：从某一边往中间渐隐，用来压住标题/页脚。"""
    w, h = img.size
    color = _hex_to_rgb(layer.get("color") or "#000000")[:3]
    strength = _alpha(layer.get("alpha"), 0.35)
    where = str(layer.get("from") or "top").lower()
    span = _alpha(layer.get("size"), 0.35)
    mask = PIL.Image.new("L", (w, h), 0)
    mpx = mask.load()
    length = max(1, int((h if where in ("top", "bottom") else w) * span))
    vert = where in ("top", "bottom")
    for i in range(length):
        f = 1.0 - (i / max(length - 1, 1))
        v = int(255 * strength * f)
        if vert:
            y = i if where == "top" else h - 1 - i
            for x in range(w):
                mpx[x, y] = v
        else:
            x = i if where == "left" else w - 1 - i
            for y in range(h):
                mpx[x, y] = v
    overlay = PIL.Image.new("RGB", (w, h), color)
    # composite(image1, image2, mask)：mask=255 处取 image2（压暗带），0 处保留原图
    return PIL.Image.composite(img, overlay, mask)


def _border_layer(PIL, img, layer: dict):
    """圆角描边层（画在卡片外框内侧）。"""
    w, h = img.size
    color = _hex_to_rgb(layer.get("color") or "#ffffff")
    width = max(1, _rel(layer.get("width") or 2, w, 2))
    radius = _rel(layer.get("radius") or 0.03, min(w, h), 24)
    inset = _rel(layer.get("inset") or 0.011, min(w, h), 8)
    overlay = PIL.Image.new("RGBA", (w, h), (0, 0, 0, 0))
    from PIL import ImageDraw

    d = ImageDraw.Draw(overlay)
    d.rounded_rectangle([inset, inset, w - 1 - inset, h - 1 - inset],
                        radius=radius, outline=(color[0], color[1], color[2], int(color[3] * _alpha(layer.get("alpha"), 1.0))),
                        width=width)
    return PIL.Image.alpha_composite(img.convert("RGBA"), overlay).convert("RGB")


def _text_layer(PIL, img, layer: dict, ctx: dict):
    """文字层（水印 / 角标）。x/y 是相对锚点，align 决定左右，font 取正文字体。"""
    from PIL import ImageDraw, ImageFont

    w, h = img.size
    text = str(layer.get("text") or "").strip()
    if not text:
        return img
    scale = float(ctx.get("scale") or 1.0)
    size = int(round(float(layer.get("size") or 11) * scale))
    font_path = ctx.get("font_path") or ""
    try:
        font = ImageFont.truetype(font_path, max(8, size)) if font_path else ImageFont.load_default()
    except Exception:  # noqa: BLE001
        font = ImageFont.load_default()
    overlay = PIL.Image.new("RGBA", (w, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(overlay)
    color = _hex_to_rgb(layer.get("color") or "#00000066")
    fill = (color[0], color[1], color[2], int(color[3] * _alpha(layer.get("alpha"), 1.0)))
    x = _rel(layer.get("x"), w, 0.5)
    y = _rel(layer.get("y"), h, 0.97)
    align = str(layer.get("align") or "center").lower()
    try:
        bw = d.textbbox((0, 0), text, font=font)[2]
    except Exception:  # noqa: BLE001
        bw = 0
    if align == "center":
        x -= bw // 2
    elif align in ("right", "end"):
        x -= bw
    d.text((x, y), text, font=font, fill=fill)
    return PIL.Image.alpha_composite(img.convert("RGBA"), overlay).convert("RGB")


def _sticker_layer(PIL, img, layer: dict, ctx: dict):
    """贴图层：按相对矩形贴一张图（logo / 角标 / 水印图）。"""
    from PIL import ImageOps

    w, h = img.size
    src = str(layer.get("src") or "").strip()
    if not src or not Path(src).is_file():
        return img
    logo = ImageOps.exif_transpose(PIL.Image.open(src)).convert("RGBA")
    tw = _rel(layer.get("w") or 0.08, w, 0.08)
    if layer.get("h"):
        th = _rel(layer.get("h"), h, tw)
    else:
        th = max(1, int(round(logo.size[1] * (tw / max(logo.size[0], 1)))))
    logo = logo.resize((max(1, tw), max(1, th)), PIL.Image.LANCZOS)
    alpha = _alpha(layer.get("alpha"), 1.0)
    if alpha < 1:
        r, g, b, a = logo.split()
        a = a.point(lambda v: int(v * alpha))
        logo = PIL.Image.merge("RGBA", (r, g, b, a))
    base = img.convert("RGBA")
    base.alpha_composite(logo, (_rel(layer.get("x"), w, 0.03), _rel(layer.get("y"), h, 0.03)))
    return base.convert("RGB")


def _grain_layer(PIL, img, layer: dict):
    """噪点层（弱化的胶片颗粒，照片背景时能压住色带）。"""
    import random as _random

    w, h = img.size
    strength = _alpha(layer.get("alpha"), 0.05)
    noise = PIL.Image.new("L", (max(1, w // 3), max(1, h // 3)))
    px = noise.load()
    rnd = _random.Random(20260101)
    for yy in range(noise.size[1]):
        for xx in range(noise.size[0]):
            px[xx, yy] = 128 + rnd.randint(-40, 40)
    noise = noise.resize((w, h), PIL.Image.BILINEAR)
    overlay = PIL.Image.merge("RGB", (noise, noise, noise))
    return PIL.Image.blend(img, overlay, strength)


def _decorative_blobs(PIL, width: int, height: int):
    """暖色装饰光斑层：给毛玻璃提供"可模糊"的背景内容。

    固定构图（_BLOB_SPECS），不用随机，保证每次渲染一致。
    """
    from PIL import ImageFilter

    layer = PIL.Image.new("RGBA", (width, height), (0, 0, 0, 0))
    d = PIL.ImageDraw.Draw(layer)
    mn = min(width, height)
    for fx, fy, fr, col in _BLOB_SPECS:
        cx, cy, r = fx * width, fy * height, fr * mn
        d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=col)
    return layer.filter(ImageFilter.GaussianBlur(radius=max(1, int(0.09 * mn))))


def _frosted_panel(PIL, base, box, radius, *, blur=14, tint=(255, 250, 242), tint_alpha=70, scale=1.0):
    # 图片背景比渐变「花」，面板要更实一点，否则浅色面板上的深色字会被背景纹路吃掉
    if getattr(_bg_ctx, "is_image", False):
        tint_alpha = min(255, int(tint_alpha) + 16)
    """毛玻璃面板：裁剪背后区域 → 高斯模糊 → 暖白半透明叠加 → 圆角遮罩贴回。

    box 为逻辑坐标 [x0, y0, x1, y1]（内部 × scale 转设备像素）。
    裁剪外扩一个模糊半径的边距，否则面板边缘会露出未模糊的底图。
    任何失败回退为实底圆角矩形，保证内容始终可读。
    """
    x0, y0, x1, y1 = [int(v * scale) for v in box]
    w, h = x1 - x0, y1 - y0
    if w <= 0 or h <= 0:
        return base
    m = max(1, int(blur * scale))
    cx0, cy0 = max(0, x0 - m), max(0, y0 - m)
    cx1, cy1 = min(base.width, x1 + m), min(base.height, y1 + m)
    try:
        from PIL import ImageFilter

        region = base.crop((cx0, cy0, cx1, cy1))
        blurred = region.filter(ImageFilter.GaussianBlur(radius=blur * scale))
        overlay = PIL.Image.new("RGBA", blurred.size, tint + (tint_alpha,))
        panel = PIL.Image.alpha_composite(blurred.convert("RGBA"), overlay)
        center = panel.crop((x0 - cx0, y0 - cy0, x0 - cx0 + w, y0 - cy0 + h))
        if center.mode != base.mode:
            center = center.convert(base.mode)
        mask = PIL.Image.new("L", (w, h), 0)
        PIL.ImageDraw.Draw(mask).rounded_rectangle(
            [0, 0, w - 1, h - 1], radius=int(radius * scale), fill=255,
        )
        base.paste(center, (x0, y0), mask)
    except Exception:  # noqa: BLE001
        logger.debug("毛玻璃面板绘制失败，回退实底", exc_info=True)
        try:
            fill = tint if base.mode == "RGB" else tint + (255,)
            PIL.ImageDraw.Draw(base).rounded_rectangle(
                [x0, y0, x1 - 1, y1 - 1], radius=int(radius * scale), fill=fill,
            )
        except Exception:  # noqa: BLE001
            pass
    return base


def _glass_edges(PIL, base, panels, scale=1.0):
    """玻璃边缘：描边 + 顶部高光线，独立图层 alpha 混合后合成。

    panels: [(box, radius, outline_rgba, highlight_rgba_or_None), ...]
    ⚠️ ImageDraw 直接画在 RGBA 图上是**替换像素**而非混合，必须走独立图层。
    ⚠️ 返回**新图像**：调用后不可再用此前创建的 draw 对象继续绘制。
    """
    try:
        edge = PIL.Image.new("RGBA", base.size, (0, 0, 0, 0))
        d = PIL.ImageDraw.Draw(edge)
        lw = max(1, int(1.2 * scale))
        for box, radius, outline, hl in panels:
            x0, y0, x1, y1 = [int(v * scale) for v in box]
            d.rounded_rectangle(
                [x0, y0, x1 - 1, y1 - 1], radius=int(radius * scale), outline=outline, width=lw,
            )
            if hl:
                inset = radius * scale * 0.7
                d.line([(x0 + inset, y0 + 1), (x1 - inset, y0 + 1)], fill=hl, width=lw)
        return PIL.Image.alpha_composite(base.convert("RGBA"), edge)
    except Exception:  # noqa: BLE001
        logger.debug("玻璃边缘绘制失败，已跳过", exc_info=True)
        return base


# 「无印象」的占位文案——不应作为标签渲染
# 扩展：无印象记录时不渲染该行，也不输出占位文字。
# 实测有 3 个档案的 summary 里存的就是插件自己的占位句，需一并过滤。
_EMPTY_SUMMARY_TEXTS = {
    "",
    "尚未形成稳定关系印象。",
    "尚未形成稳定关系印象",
    "暂无印象记录",
    "暂无印象",
    "暂无",
    "无",
}


def _as_int(value: Any, default: int = 0) -> int:
    """宽松取整数：展示层遇到脏数据不应抛异常（卡片是"尽力渲染"）。"""
    try:
        return int(value)
    except (TypeError, ValueError):
        pass
    try:
        return int(float(str(value).strip()))
    except (TypeError, ValueError):
        return default


def _clean_summary(value: Any) -> str:
    """取印象记录文本；空值或占位文案返回空串（调用方据此不渲染该标签）。"""
    s = str(value or "").strip()
    return "" if s in _EMPTY_SUMMARY_TEXTS else s


# 头像占位配色（暖色系为主，保留少量低饱和冷色作区分）：按 user_id 哈希稳定取色
_AVATAR_COLORS = [
    ((246, 222, 208), (176, 84, 50)),    # 赤陶
    ((250, 240, 222), (168, 122, 48)),    # 琥珀
    ((240, 236, 226), (140, 110, 80)),    # 驼色
    ((248, 230, 224), (190, 96, 96)),     # 玫瑰
    ((238, 236, 246), (120, 106, 160)),   # 灰紫（少量冷色，避免全暖发闷）
    ((246, 238, 228), (150, 100, 60)),    # 焦糖
    ((250, 244, 230), (170, 140, 70)),    # 暖金
    ((244, 238, 232), (130, 100, 90)),    # 摩卡
]


def _avatar_color(key: str) -> tuple:
    """按 key 稳定取一组 (底色, 文字色)。"""
    h = 0
    for ch in str(key or ""):
        h = (h * 131 + ord(ch)) & 0xFFFFFFFF
    return _AVATAR_COLORS[h % len(_AVATAR_COLORS)]


def _avatar_initial(name: str) -> str:
    """取昵称首个「有意义」字符作头像字（跳过 emoji / 符号）。"""
    for ch in str(name or "").strip():
        if ch.isalnum() or "\u4e00" <= ch <= "\u9fff":
            return ch
    return "?"


def _fit_font(draw: Any, text: str, font: Any, max_w: float, max_h: float) -> Any:
    """按可用内接尺寸缩小字号，保证头像首字完整落在圆内。

    旧实现直接用固定字号居中，字形顶到圆边会被圆形裁掉一角（"爱"/"老" 这类
    方块字尤其明显，看着像残字）。这里按圆形内接矩形逐级缩到刚好放得下。
    """
    try:
        from PIL import ImageFont

        path = getattr(font, "path", None)
        if not path:
            return font
        size = int(getattr(font, "size", 17))
        best = font
        while size > 8:
            bb = draw.textbbox((0, 0), text, font=best)
            if (bb[2] - bb[0]) <= max_w and (bb[3] - bb[1]) <= max_h:
                break
            size = max(8, int(size * 0.88))
            best = ImageFont.truetype(path, size)
        return best
    except Exception:  # noqa: BLE001
        return font


_AVATAR_TTL = 7 * 24 * 3600   # 头像磁盘缓存 7 天
_AVATAR_TIMEOUT = 4           # 单次抓取超时（秒）


def _load_avatar_image(PIL: Any, user_id: str, avatar_dir: str):
    """取用户头像（QQ 号走 qlogo，磁盘缓存 7 天）。失败返回 None。

    ⚠️ PIL 必须由调用方传入：它是 render_favorability_rank 的局部变量，
    模块级函数直接引用会 NameError（会被下面的 except 静默吞掉）。

    仅对**纯数字 ID**（QQ 号）尝试；其它 ID 返回 None，由调用方回退首字占位。
    """
    if not avatar_dir or not user_id or not user_id.isdigit():
        return None
    try:
        d = Path(avatar_dir)
        fp = d / f"{user_id}.jpg"
        if fp.is_file() and (time.time() - fp.stat().st_mtime) < _AVATAR_TTL:
            return PIL.Image.open(fp).convert("RGB")
    except Exception:  # noqa: BLE001
        pass

    for url in (
        f"https://q1.qlogo.cn/g?b=qq&nk={user_id}&s=140",
        f"https://q.qlogo.cn/headimg_dl?dst_uin={user_id}&spec=140",
    ):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=_AVATAR_TIMEOUT) as resp:
                data = resp.read()
            if len(data) < 100:
                continue
            # 先解析再落盘：下载到半张图/错误页时直接当失败（旧实现先 write_bytes，
            # 坏文件会一直留在缓存里，之后 7 天内每次打开都失败）。
            img = PIL.Image.open(io.BytesIO(data)).convert("RGB")
            Path(avatar_dir).mkdir(parents=True, exist_ok=True)
            # 原子写：并发渲染同一用户时不会互相截断
            tmp = fp.with_suffix(".jpg.tmp")
            try:
                tmp.write_bytes(data)
                tmp.replace(fp)
            except Exception:  # noqa: BLE001
                try:
                    tmp.unlink()
                except OSError:
                    pass
                raise
            return img
        except Exception:  # noqa: BLE001
            continue
    return None


def render_favorability_rank(
    entries: List[dict],
    max_abs_score: int,
    *,
    limit: int = 0,
    font_path: str = "",
    font_dir: str = "",
    avatar_dir: str = "",
    scale: float = 0.0,
    bg_path: str = "",
    bg_blur: float = 14.0,
    bg_dim: int = 0,
    layers: Any = None,
) -> bytes | None:
    """渲染好感度排行榜为 PNG 卡片，返回字节；不可用时返回 None。

    ⚠️ 这是**兜底包装**：契约承诺「不可用返回 None」，但内部实现有几十处
       int()/字体/粘贴操作，任何一处被脏数据触发都会抛异常（实测
       score="很高" 直接 ValueError）。调用方 plugin._render_rank_card 虽
       也包了 try，但那会让整张卡片消失、回退成纯文本。这里统一兜住，
       保证「要么出图，要么安静返回 None」。
    """
    try:
        return _render_favorability_rank_impl(
            entries, max_abs_score, limit=limit, font_path=font_path,
            font_dir=font_dir, avatar_dir=avatar_dir, scale=scale,
            bg_path=bg_path, bg_blur=bg_blur, bg_dim=bg_dim, layers=layers,
        )
    except Exception:  # noqa: BLE001
        logger.exception("好感度排行榜卡片渲染异常，回退为文本")
        return None


def _render_favorability_rank_impl(
    entries: List[dict],
    max_abs_score: int,
    *,
    limit: int = 0,
    font_path: str = "",
    font_dir: str = "",
    avatar_dir: str = "",
    scale: float = 0.0,
    bg_path: str = "",
    bg_blur: float = 14.0,
    bg_dim: int = 0,
    layers: Any = None,
) -> bytes | None:
    """真正的渲染实现（由 render_favorability_rank 兜底调用）。

    Args:
        entries: 已按 score 从高到低排序的档案摘要，每项含
            display_name / user_id / score / stage / updated_at。
        max_abs_score: 好感度绝对值上限，用于分值展示。
        limit: 卡片最多展示行数，超出部分在卡片中提示省略。
        font_path: 配置指定的中文字体文件路径。
        font_dir: 插件数据目录中的字体目录。
    """
    back_layers, front_layers = _split_layers(layers, has_bg=bool(str(bg_path or "").strip()))
    PIL = _load_pil()
    if PIL is None:
        return None
    try:
        from PIL import ImageDraw, ImageFont
    except Exception:  # noqa: BLE001
        logger.warning("Pillow 子模块导入失败，排行榜卡片回退为文本", exc_info=True)
        return None

    font_path_resolved = _find_font(font_path=font_path, font_dir=font_dir)
    if not font_path_resolved:
        return None

    try:
        f_brand = ImageFont.truetype(font_path_resolved, 14)
        f_title = ImageFont.truetype(font_path_resolved, 30)
        f_hint = ImageFont.truetype(font_path_resolved, 16)
        f_name = ImageFont.truetype(font_path_resolved, 18)
        f_id = ImageFont.truetype(font_path_resolved, 15)
        f_score = ImageFont.truetype(font_path_resolved, 19)
        f_stage = ImageFont.truetype(font_path_resolved, 14)
        f_rank = ImageFont.truetype(font_path_resolved, 18)
        f_tag = ImageFont.truetype(font_path_resolved, 11)
        f_sub = ImageFont.truetype(font_path_resolved, 12)   # 副标题/页脚
        f_avatar = ImageFont.truetype(font_path_resolved, 17)   # 头像首字
        f_meta = ImageFont.truetype(font_path_resolved, 13)     # 头部统计
        f_rank_small = ImageFont.truetype(font_path_resolved, 11)  # 名次小徽章
    except Exception:  # noqa: BLE001
        logger.exception("字体加载失败")
        return None

    # ---------------- 渲染全部（扩展：多列动态布局）----------------
    shown = list(entries) if not limit else list(entries[:limit])
    total = len(shown)

    MARGIN = 18          # 图片四周留白，让白色卡片"浮"在渐变背景上
    COL_W = 528          # 单列宽
    COL_GAP = 16         # 列间距
    pad = 24             # 卡片内边距
    header_h = 72        # 标题 + 头部统计 + 分隔线
    row_h = 70
    gap = 8
    foot_h = 32          # 页脚（统计 + 水印）
    bottom_pad = 18      # 底部内边距
    BADGE = 26           # 名次徽章直径（独立槽位，不叠在头像上）
    AV_R = 21            # 头像半径
    # 标签约束：控制单条长度与总条数，避免占满整行
    MAX_TAGS = 4        # 标签行最多几个（含 ID）
    MAX_TAG_CHARS = 7   # 关系标签 / 印象记录的字数上限
    MAX_ID_CHARS = 18   # ID 标签放宽（ID:web_user_1 是 13 字符，12 会截断）

    # 动态列数（扩展）：列数 = ceil(n/15)，列内**均摊余数**（各列最多差 1）。
    # ⚠️ 不能用"顺序填充 + ceil 列高"：那样在 n=15k+1 时会退化成 15×k + 1
    #    （如 n=211 → 14 列满 + 末列仅 1 个，视觉上是个空洞列）。
    # 例：15 → [15]；16 → [8,8]；46 → [12,12,11,11]；60 → [15,15,15,15]；
    #     108 → [14,14,14,14,13,13,13,13]；211 → [15,14×14]
    #
    # 注：调用方 plugin.py 的 _rank_profiles 已按 RANK_HIDE_EMPTY 过滤掉
    # 「0 分且无任何记录」的空壳档案（实测 363 → 76，最多 20 人 / 2 列），
    # 因此这里不再自行截断 —— 加了也永远触发不到，只会平白增加复杂度。
    n_all = len(entries)      # 头部「共 N 人」用全量，与页脚「另有 X 人未显示」对齐
    n = len(shown)
    TARGET_PER_COL = 15
    if n <= 0:
        cols, col_sizes = 1, [0]
    else:
        cols = max(1, math.ceil(n / TARGET_PER_COL))
        _base, _rem = divmod(n, cols)
        col_sizes = [_base + 1] * _rem + [_base] * (cols - _rem)
    per_col = max(col_sizes) if col_sizes else 0

    rows_h = max(per_col, 1) * (row_h + gap)
    card_w = pad * 2 + cols * COL_W + (cols - 1) * COL_GAP
    # 页脚只在真的截断了人时才出现（见渲染末尾），没截断就不预留这条高度，
    # 否则列表下方会多出一条空白带。
    _need_foot = max(0, len(entries) - len(shown)) > 0
    card_h = pad + header_h + rows_h - gap + (foot_h if _need_foot else 0) + bottom_pad
    img_w = card_w + MARGIN * 2
    img_h = card_h + MARGIN * 2
    S = float(scale) if scale and scale > 0 else _RENDER_SCALE

    _bg_ctx.is_image = bool(str(bg_path or "").strip())
    img = _compose_layers(
        PIL,
        _background(PIL, int(img_w * S), int(img_h * S),
                    path=bg_path, blur=bg_blur, dim=bg_dim, scale=S),
        back_layers, stage="back", base_ctx={"scale": S, "font_path": font_path_resolved},
    )

    # 暖色装饰光斑：毛玻璃的"可模糊内容"（纯平渐变上看不出磨砂感）
    try:
        img = PIL.Image.alpha_composite(
            img.convert("RGBA"), _decorative_blobs(PIL, img.width, img.height)
        ).convert("RGB")
    except Exception:  # noqa: BLE001
        logger.debug("背景光斑绘制失败，已跳过", exc_info=True)

    # 卡片柔和投影（独立图层 + 高斯模糊；失败则静默跳过）
    # ⚠️ 这里必须用 card_w：旧代码误写成未定义的 `width`，一旦本 except
    #    被触发就是 NameError，然后被 except 吞掉 → 投影静默失效。
    try:
        from PIL import ImageFilter

        shadow = PIL.Image.new("RGBA", img.size, (0, 0, 0, 0))
        sd = PIL.ImageDraw.Draw(shadow)
        sd.rounded_rectangle(
            [
                int(MARGIN * S), int((MARGIN + 6) * S),
                int((MARGIN + card_w) * S), int((MARGIN + card_h + 6) * S),
            ],
            radius=int(26 * S), fill=(96, 64, 40, 70),
        )
        shadow = shadow.filter(ImageFilter.GaussianBlur(radius=10 * S))
        img = PIL.Image.alpha_composite(img.convert("RGBA"), shadow).convert("RGB")
    except Exception:  # noqa: BLE001
        logger.debug("卡片投影绘制失败，已跳过", exc_info=True)

    draw = _make_draw(img, ImageDraw, font_dir, S)

    # 毛玻璃卡片本体（背后是渐变 + 暖色光斑，模糊后叠暖白半透明）
    _card_box = [MARGIN, MARGIN, MARGIN + card_w, MARGIN + card_h]
    img = _frosted_panel(PIL, img, _card_box, 24, blur=16, tint_alpha=80, scale=S)

    hy = MARGIN + pad                  # 标题区顶部

    # ---------------- 标题区 ----------------
    hx = MARGIN + pad                  # 标题左边界（跨列统一）
    # 红标与标题**按字形实际墨迹对齐**，而不是按 draw.text 的逻辑位置。
    # ⚠️ 实测（msyh/LXGW 30px）：textbbox 返回 (0, 3, 180, 31) —— 顶部有 3px
    #    的 ascender 空隙，逻辑中心与肉眼看到的墨迹中心差 3px。旧实现按
    #    「逻辑 y + 固定偏移」画红标，肉眼看就是红标偏高一点、没跟标题齐平。
    #    现在直接取 ink 上下沿，红标高度也贴合 ink 高度，视觉上真正对齐。
    _ink = draw.textbbox((0, 0), "好感度排行榜", font=f_title)
    _ink_top, _ink_bot = _ink[1], _ink[3]
    _ink_h = max(1, _ink_bot - _ink_top)
    _bar_x = hx
    _bar_w = 5
    _bar_top = hy + 2 + _ink_top
    # 标题红标改为「赤陶 → 琥珀」竖向渐变 pill（超采样坐标系下直接贴图）
    try:
        _bw, _bh = max(1, int(_bar_w * S)), max(1, int(_ink_h * S))
        grad = PIL.Image.new("RGB", (_bw, _bh))
        gpx = grad.load()
        for yy in range(_bh):
            k = yy / max(_bh - 1, 1)
            col = tuple(int(_ACCENT[j] + (_ACCENT_LIGHT[j] - _ACCENT[j]) * k) for j in range(3))
            for xx in range(_bw):
                gpx[xx, yy] = col
        gmask = PIL.Image.new("L", (_bw, _bh), 0)
        PIL.ImageDraw.Draw(gmask).rounded_rectangle(
            [0, 0, _bw - 1, _bh - 1], radius=max(1, int(2.5 * S)), fill=255,
        )
        img.paste(grad, (int(_bar_x * S), int(_bar_top * S)), gmask)
    except Exception:  # noqa: BLE001
        logger.debug("标题渐变条绘制失败，回退纯色", exc_info=True)
        draw.rounded_rectangle(
            [_bar_x, _bar_top, _bar_x + _bar_w, _bar_top + _ink_h],
            radius=2.5, fill=_ACCENT,
        )
    draw.text((hx + 15, hy + 2), "好感度排行榜", font=f_title, fill=_TEXT)

    # 头部统计：填补原先空荡的标题右侧，同时让卡片一眼有"榜"的感觉
    if shown:
        _scores = [_as_int(e.get("score"), 0) for e in shown]
        _hi, _lo = max(_scores), min(_scores)
        _stats = [f"共 {n_all} 人", f"最高 {_hi:+d}" if _hi else "最高 0"]
        if _lo < 0:
            _stats.append(f"最低 {_lo}")
        meta_text = " · ".join(_stats)
    else:
        meta_text = "暂无数据"
    _mw = int(draw.textlength(meta_text, font=f_meta))
    # 统计文字按自身 ink 垂直居中对齐到标题的 ink 中心，而不是拍脑袋给个 y
    _mk = draw.textbbox((0, 0), meta_text, font=f_meta)
    _mcy = hy + 2 + (_ink_top + _ink_bot) / 2
    draw.text(
        (hx + card_w - pad * 2 - _mw, _mcy - (_mk[1] + _mk[3]) / 2),
        meta_text, font=f_meta, fill=_MUTED,
    )

    # 标题区分隔线（暖米色）
    _line_y = hy + header_h - 16
    draw.rectangle([hx, _line_y, hx + card_w - pad * 2, _line_y], fill=(244, 236, 226))

    _av_fail = [0]   # 头像抓取连续失败计数（断网保护）
    row_base_y = hy + header_h
    TRACK = (243, 235, 226)
    # 玻璃边缘收集（卡片 + 各行），渲染结束后统一合成一层
    _edge_panels = [(_card_box, 24, (255, 255, 255, 150), (255, 255, 255, 210))]

    for i, entry in enumerate(shown):
        rank = i + 1
        # 多列定位：按列优先；列高可能相差 1（前 _rem 列多 1 个）
        if _rem and i < _rem * (_base + 1):
            _col, _row = i // (_base + 1), i % (_base + 1)
        elif _base:
            _j = i - _rem * (_base + 1)
            _col, _row = _rem + _j // _base, _j % _base
        else:
            _col, _row = 0, i
        cx0 = MARGIN + pad + _col * (COL_W + COL_GAP)
        cx1 = cx0 + COL_W
        y = row_base_y + _row * (row_h + gap)
        is_top = rank <= 3

        # 毛玻璃排行行：前三名 tint 略强、边缘用名次金属色，其余暖白细边
        _row_box = [cx0, y, cx1, y + row_h]
        if is_top:
            img = _frosted_panel(
                PIL, img, _row_box, 14, blur=10,
                tint=(255, 246, 236), tint_alpha=64, scale=S,
            )
            _edge_panels.append((_row_box, 14, _RANK_METALS[rank - 1][0] + (170,), None))
        else:
            img = _frosted_panel(PIL, img, _row_box, 14, blur=10, tint_alpha=56, scale=S)
            _edge_panels.append((_row_box, 14, (255, 255, 255, 90), None))

        score = _as_int(entry.get("score"), 0)
        # 0 分走中性色：旧实现 score>=0 一律珊瑚色，把 0 画成了"正分"
        if score > 0:
            score_color = _ACCENT
        elif score < 0:
            score_color = _COOL
        else:
            score_color = _NEUTRAL

        # 左侧竖条：前三名用名次金/银/铜，其余按分数正负着色。
        # 旧实现用三层偏移的"假阴影"叠出行卡片，边缘发灰且各行不一致。
        stripe_c = _RANK_METALS[rank - 1][0] if is_top else score_color
        draw.rounded_rectangle(
            [cx0 + 5, y + 13, cx0 + 8, y + row_h - 13], radius=2, fill=stripe_c,
        )

        right_edge = cx1 - 14

        # ---------- 名次徽章：独立槽位 ----------
        # ⚠️ 旧实现把徽章圆心放在 (ax-12, ay+12)、半径 11，圆的一部分压在头像上、
        #    且紧贴行卡片左边界 → 名次 4/5/6 的数字左侧被裁掉（只剩右半边）。
        #    现在徽章独立占一个槽位，任何名次都完整可见。
        bx = cx0 + 14 + BADGE // 2
        by = y + row_h // 2
        if is_top:
            b_fill, b_fg = _RANK_METALS[rank - 1]
        else:
            b_fill, b_fg = (246, 248, 249), (140, 154, 160)
        draw.ellipse(
            [bx - BADGE // 2, by - BADGE // 2, bx + BADGE // 2, by + BADGE // 2],
            fill=b_fill,
        )
        _draw_centered(
            draw,
            [bx - BADGE // 2, by - BADGE // 2, bx + BADGE // 2, by + BADGE // 2],
            str(rank), f_rank_small, b_fg,
        )

        # ---------- 头像：昵称首字，按内接尺寸缩放 ----------
        uid_for_avatar = str(entry.get("user_id") or entry.get("display_name") or "")
        # ⚠️ 昵称必须 strip 后判空：`" "` 在 Python 里是 truthy，
        #    旧实现 `entry.get("display_name") or entry.get("user_id")` 对「两个空格」
        #    的昵称直接渲染出空白名字 —— 整行看起来像个空洞（实测确认）。
        _disp_name = str(entry.get("display_name") or "").strip()
        _uid_clean = str(entry.get("user_id") or "").strip()
        display_name = _disp_name or _uid_clean or "未知"
        ax = bx + BADGE // 2 + 12 + AV_R
        ay = by
        ar = AV_R
        av_img = None
        # 连续失败 3 次就放弃后续抓取，避免断网时整张图卡住。
        # ⚠️ 只有纯数字 ID（QQ 号）才有头像源：非数字 ID 直接跳过，
        #    且**不计入失败**。旧实现把它当失败，导致混合频道里前 3 个
        #    网页/管理员用户就把额度用光，后面所有 QQ 用户都没有头像
        #    （实测 6 条混合数据只尝试了 3 次）。
        if avatar_dir and _av_fail[0] < 3 and uid_for_avatar.isdigit():
            av_img = _load_avatar_image(PIL, uid_for_avatar, avatar_dir)
            if av_img is None:
                _av_fail[0] += 1
            else:
                _av_fail[0] = 0      # 成功即归零，与「连续失败」的语义一致
        if av_img is not None:
            side = int(ar * 2 * S)
            try:
                thumb = av_img.resize((side, side), PIL.Image.LANCZOS)
                mask = PIL.Image.new("L", (side, side), 0)
                PIL.ImageDraw.Draw(mask).ellipse([0, 0, side, side], fill=255)
                img.paste(thumb, (int((ax - ar) * S), int((ay - ar) * S)), mask)
                draw.ellipse([ax - ar, ay - ar, ax + ar, ay + ar], outline=(255, 250, 243), width=2)
            except Exception:  # noqa: BLE001
                av_img = None
        if av_img is None:
            av_fill, av_fg = _avatar_color(uid_for_avatar)
            draw.ellipse([ax - ar, ay - ar, ax + ar, ay + ar], fill=av_fill)
            # 与昵称用同一个值，避免「名字回退成 ID、头像却是 ?」的不一致
            av_ch = _avatar_initial(display_name)
            # 内接缩放：方形字顶到圆边会被裁掉一角
            f_av = _fit_font(draw, av_ch, f_avatar, ar * 2 - 8, ar * 2 - 10)
            _draw_centered(
                draw, [ax - ar, ay - ar, ax + ar, ay + ar], av_ch, f_av, av_fg,
            )

        text_x = ax + ar + 12
        pad_in, gap_tag = 8, 5
        line2_y = y + 45       # 第二行（标签行）顶部
        pill_h = 19

        # ---------- 左上：昵称 ----------
        # 分值 + 进度条的固定预留宽度；缩小它可让昵称更长、少留白
        name_max_w = max(60, right_edge - text_x - 104)
        raw_name = display_name
        name = _truncate(draw, str(raw_name), f_name, name_max_w)
        nb = draw.textbbox((0, 0), name, font=f_name)
        draw.text((text_x, y + 11 - nb[1]), name, font=f_name, fill=_TEXT)

        # ---------- 右上：好感度 ----------
        score_text = f"{score:+d}" if score != 0 else "0"
        sw = int(draw.textlength(score_text, font=f_score))
        draw.text((right_edge - sw, y + 10), score_text, font=f_score, fill=score_color)

        # ---------- 双向进度条：以 0 为中点 ----------
        # ⚠️ 旧实现把 -max…+max 线性映射到 0…1，负分自然落在条子左半边，
        #    看起来像"分数很小但为正"，语义完全错乱。
        bar_w, bar_h = 96, 5
        bar_x = right_edge - bar_w
        bar_y = y + 35
        mid_x = bar_x + bar_w // 2
        draw.rounded_rectangle(
            [bar_x, bar_y, bar_x + bar_w, bar_y + bar_h], radius=2.5, fill=TRACK,
        )
        denom = max_abs_score if max_abs_score else 0
        if denom:
            ratio = max(0.0, min(1.0, abs(score) / denom))
            fw = int((bar_w // 2) * ratio)
            if fw >= 2:
                if score >= 0:
                    draw.rounded_rectangle(
                        [mid_x, bar_y, mid_x + fw, bar_y + bar_h],
                        radius=2.5, fill=score_color,
                    )
                else:
                    draw.rounded_rectangle(
                        [mid_x - fw, bar_y, mid_x, bar_y + bar_h],
                        radius=2.5, fill=score_color,
                    )
        # 中点刻度：让"零"在条上可见（暖棕刻度）
        draw.rectangle([mid_x, bar_y - 1, mid_x + 1, bar_y + bar_h], fill=(224, 210, 194))

        # ---------- 右下：好感等级 ----------
        stage = str(entry.get("stage") or "").strip()
        stage_w = 0
        if stage:
            stage_bg, stage_fg = _STAGE_STYLES.get(stage, _STAGE_STYLES["中立"])
            stage_w = int(draw.textlength(stage, font=f_tag)) + pad_in * 2
            sx = right_edge - stage_w
            draw.rounded_rectangle(
                [sx, line2_y, sx + stage_w, line2_y + pill_h],
                radius=9.5, fill=stage_bg,
            )
            _draw_centered(
                draw, [sx, line2_y, sx + stage_w, line2_y + pill_h], stage, f_tag, stage_fg,
            )

        # ---------- 左下：ID + 关系标签 + 印象记录（独占一行，空间充足）----------
        uid = str(entry.get("user_id") or "")
        summ = _clean_summary(entry.get("summary"))
        _raw_tags = entry.get("tags") or []
        if isinstance(_raw_tags, str):      # 后端/历史数据可能是逗号分隔的字符串
            _raw_tags = _raw_tags.replace("，", ",").replace("、", ",").split(",")
        rel = [str(t).strip() for t in _raw_tags if str(t).strip()]
        # 关系标签超过 3 个时随机取 3 个（避免总是展示同几条，也让卡片每次有变化）
        if len(rel) > 3:
            rel = random.sample(rel, 3)

        # 候选池 → 选取最多 MAX_TAGS 个（含 ID）
        # 优先级：ID > 印象记录 > 关系标签；展示顺序：ID → 关系标签 → 印象记录
        pool: list = []
        if uid:
            # 只有长数字 ID（如 QQ 号）才截后四位；短/非数字 ID 取后四位会切出
            # 无意义碎片（admin_1 → in_1），故原样显示。
            short = uid[-4:] if (uid.isdigit() and len(uid) > 4) else uid
            # ⚠️ 昵称已经回退成这个 ID 时跳过：否则同一标识在同一行出现两次
            #    （名字区 `123456789` + 胶囊 `ID:6789`），既冗余又难看。
            if str(raw_name) != uid:
                pool.append(("ID", f"ID:{short}"))
        if summ:
            pool.append(("SUM", summ))
        for t in sorted(rel, key=len):
            pool.append(("REL", t))   # 按长度排序：短标签在前，视觉更整齐

        picked: list = []
        for kind in ("ID", "SUM", "REL"):
            for kk, vv in pool:
                if kk == kind and len(picked) < MAX_TAGS:
                    picked.append((kk, vv))
        picked.sort(key=lambda kv: {"ID": 0, "REL": 1, "SUM": 2}[kv[0]])

        limit_x = right_edge - stage_w - (10 if stage_w else 0)
        tx = text_x
        for k, (kind, tg) in enumerate(picked):
            avail = limit_x - tx - (gap_tag if k else 0)
            if avail < 28:
                break
            # 字数上限（ID 放宽）
            mchars = MAX_ID_CHARS if kind == "ID" else MAX_TAG_CHARS
            if len(tg) > mchars:
                tg = tg[:mchars] + "…"
            w_txt = int(draw.textlength(tg, font=f_tag))
            if w_txt + pad_in * 2 > avail:
                # 空间不足时只有印象记录允许再按宽度截断，其余跳过
                if kind != "SUM":
                    continue
                tg = _truncate(draw, tg, f_tag, max(10, avail - pad_in * 2))
                w_txt = int(draw.textlength(tg, font=f_tag))
                if w_txt < 18:
                    break
            pw = w_txt + pad_in * 2
            # 三类标签三种配色，形成视觉层级：
            #   ID   → 中性灰（辅助信息，弱化）
            #   关系 → 珊瑚（分类标签，强调）
            #   印象 → 淡青（一句话描述，与标签区分）
            if kind == "ID":
                tag_bg, tag_fg = (244, 238, 230), (140, 118, 98)
            elif kind == "SUM":
                tag_bg, tag_fg = (247, 240, 226), (154, 120, 62)
            else:
                tag_bg, tag_fg = _ACCENT_SOFT, _ACCENT
            draw.rounded_rectangle(
                [tx, line2_y, tx + pw, line2_y + pill_h],
                radius=9.5, fill=tag_bg,
            )
            _draw_centered(draw, [tx, line2_y, tx + pw, line2_y + pill_h], tg, f_tag, tag_fg)
            tx += pw + gap_tag

    # ---------------- 页脚 ----------------
    # 只在**真的截断了人**时才提示「另有 N 人未显示」——
    # 平时不写平均分/亲近人数那行统计：信息与头部「共 N 人 · 最高 +X」重复，
    # 且在列表结束处横向铺一行小字显得杂乱。limit 为 0（默认全量）时完全不出现。
    _omitted = max(0, len(entries) - len(shown))
    if _omitted:
        fy = MARGIN + card_h - bottom_pad - foot_h + 9
        tip = f"另有 {_omitted} 人未显示"
        draw.text((hx + 1, fy), tip, font=f_sub, fill=_MUTED)

    # 玻璃边缘（描边 + 顶部高光）最后统一合成：返回新图像，之后不再绘制
    img = _glass_edges(PIL, img, _edge_panels, scale=S)

    img = _compose_layers(PIL, img, front_layers, stage="front",
                          base_ctx={"scale": S, "font_path": font_path_resolved})
    buf = io.BytesIO()
    img.convert("RGB").save(buf, format="PNG")
    return buf.getvalue()


# ==================== 单人关系档案卡片（2.2.19） ====================


def _wrap_text(draw: Any, text: str, font: Any, max_w: float) -> List[str]:
    """按像素宽度折行：保留原有换行；ASCII 连续串整体不拆，CJK 逐字。

    排行榜的行内「印象记录」只能截断成一句话，这条路径要把**完整**文本铺开，
    所以必须真的按宽度折行（不能靠 truncate）。
    """
    out: List[str] = []
    for para in str(text or "").replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if not para.strip():
            out.append("")
            continue
        buf = ""
        i, n = 0, len(para)
        while i < n:
            ch = para[i]
            ascii_word = ch.isascii() and (ch.isalnum() or ch in "_-@./:%+")
            if ascii_word:
                j = i
                while j < n and para[j].isascii() and (para[j].isalnum() or para[j] in "_-@./:%+"):
                    j += 1
                token = para[i:j]
            else:
                token, j = ch, i + 1
            if buf and draw.textlength(buf + token, font=font) > max_w:
                out.append(buf)
                buf = token if token.strip() else token.lstrip()
            else:
                buf += token
            i = j
        out.append(buf)
    return out or [""]


def render_profile_card(
    entry: Dict[str, Any],
    max_abs_score: int,
    *,
    font_path: str = "",
    font_dir: str = "",
    avatar_dir: str = "",
    scale: float = 0.0,
    bg_path: str = "",
    bg_blur: float = 14.0,
    bg_dim: int = 0,
    layers: Any = None,
) -> bytes | None:
    """渲染**单人**关系档案卡片，返回 PNG 字节；不可用返回 None。

    与排行榜同款视觉（竖向渐变 + 暖色光斑 + 毛玻璃面板 + 玻璃边缘高光），差别：
      · 没有名次、没有列表；
      · 「稳定印象」按卡片宽度折行**完整渲染**，不截断（排行榜行内只放得下一句话，
        长印象在列表里永远显示不全，这张卡片就是为看全它而存在）。

    entry 字段：display_name / user_id / score / stage / tags / summary /
                updated_at / last_interaction_at
    """
    try:
        return _render_profile_card_impl(
            entry, max_abs_score, font_path=font_path, font_dir=font_dir,
            avatar_dir=avatar_dir, scale=scale,
            bg_path=bg_path, bg_blur=bg_blur, bg_dim=bg_dim, layers=layers,
        )
    except Exception:  # noqa: BLE001
        logger.exception("关系档案卡片渲染异常，回退为文本")
        return None


def _render_profile_card_impl(
    entry: Dict[str, Any],
    max_abs_score: int,
    *,
    font_path: str = "",
    font_dir: str = "",
    avatar_dir: str = "",
    scale: float = 0.0,
    bg_path: str = "",
    bg_blur: float = 14.0,
    bg_dim: int = 0,
    layers: Any = None,
) -> bytes | None:
    back_layers, front_layers = _split_layers(layers, has_bg=bool(str(bg_path or "").strip()))
    PIL = _load_pil()
    if PIL is None:
        return None
    try:
        from PIL import ImageDraw, ImageFont
    except Exception:  # noqa: BLE001
        logger.warning("Pillow 子模块导入失败，档案卡片回退为文本", exc_info=True)
        return None

    fp = _find_font(font_path=font_path, font_dir=font_dir)
    if not fp:
        return None
    try:
        f_title = ImageFont.truetype(fp, 27)
        f_name = ImageFont.truetype(fp, 25)
        f_meta = ImageFont.truetype(fp, 13)
        f_id = ImageFont.truetype(fp, 13)
        f_score = ImageFont.truetype(fp, 32)
        f_unit = ImageFont.truetype(fp, 14)
        f_tag = ImageFont.truetype(fp, 12)
        f_label = ImageFont.truetype(fp, 14)
        f_body = ImageFont.truetype(fp, 16)
        f_avatar = ImageFont.truetype(fp, 32)
        f_foot = ImageFont.truetype(fp, 11)
    except Exception:  # noqa: BLE001
        logger.exception("字体加载失败（档案卡片）")
        return None

    # ---------------- 取数（展示层不抛异常）----------------
    score = _as_int(entry.get("score"), 0)
    uid = str(entry.get("user_id") or "").strip()
    _disp = str(entry.get("display_name") or "").strip()
    display_name = _disp or uid or "未知"
    stage = str(entry.get("stage") or "").strip()
    summary = _clean_summary(entry.get("summary")) or ""
    raw_tags = entry.get("tags") or []
    if isinstance(raw_tags, str):      # 历史数据可能是逗号分隔字符串
        raw_tags = raw_tags.replace("，", ",").replace("、", ",").split(",")
    tags = [str(t).strip() for t in raw_tags if str(t).strip()][:12]
    updated_at = _as_int(entry.get("updated_at"), 0)
    last_at = _as_int(entry.get("last_interaction_at"), 0)

    MARGIN = 18
    CARD_W = 724
    pad = 28
    header_h = 64
    AV_R = 44
    S = float(scale) if scale and scale > 0 else _RENDER_SCALE
    inner_w = CARD_W - pad * 2

    # 先按逻辑坐标量出所有文本，再算卡片高度（完整印象决定高度）
    _measure_img = PIL.Image.new("RGB", (10, 10))
    mdraw = _make_draw(_measure_img, ImageDraw, font_dir, 1.0)

    body_lines = _wrap_text(mdraw, summary, f_body, inner_w) if summary else []
    line_h = 27
    if not body_lines:
        body_lines = ["（这个频道还没有关于 TA 的稳定印象）"]
    body_h = len(body_lines) * line_h

    # 标签折行（整行铺开，不省略）
    tag_rows: List[List[str]] = []
    _cur: List[str] = []
    _cur_w = 0.0
    for tg in tags:
        w = float(mdraw.textlength(tg, font=f_tag)) + 20
        if _cur and _cur_w + 8 + w > inner_w:
            tag_rows.append(_cur)
            _cur, _cur_w = [tg], w
        else:
            _cur.append(tg)
            _cur_w += (8 if _cur[:-1] else 0) + w
    if _cur:
        tag_rows.append(_cur)
    tags_h = len(tag_rows) * 28 if tag_rows else 0

    profile_h = AV_R * 2 + 18
    sum_label_h = 36          # 「稳定印象」标题 + 与正文的呼吸
    foot_h = 30
    card_h = (pad + header_h + profile_h + (tags_h + 8 if tags_h else 0)
              + sum_label_h + body_h + foot_h + pad)
    img_w = CARD_W + MARGIN * 2
    img_h = card_h + MARGIN * 2

    _bg_ctx.is_image = bool(str(bg_path or "").strip())
    img = _compose_layers(
        PIL,
        _background(PIL, int(img_w * S), int(img_h * S),
                    path=bg_path, blur=bg_blur, dim=bg_dim, scale=S),
        back_layers, stage="back", base_ctx={"scale": S, "font_path": fp},
    )
    try:
        img = PIL.Image.alpha_composite(
            img.convert("RGBA"), _decorative_blobs(PIL, img.width, img.height)
        ).convert("RGB")
    except Exception:  # noqa: BLE001
        logger.debug("档案卡背景光斑绘制失败，已跳过", exc_info=True)

    try:
        from PIL import ImageFilter

        shadow = PIL.Image.new("RGBA", img.size, (0, 0, 0, 0))
        PIL.ImageDraw.Draw(shadow).rounded_rectangle(
            [int(MARGIN * S), int((MARGIN + 6) * S),
             int((MARGIN + CARD_W) * S), int((MARGIN + card_h + 6) * S)],
            radius=int(26 * S), fill=(96, 64, 40, 70),
        )
        img = PIL.Image.alpha_composite(
            img.convert("RGBA"), shadow.filter(ImageFilter.GaussianBlur(radius=10 * S))
        ).convert("RGB")
    except Exception:  # noqa: BLE001
        logger.debug("档案卡投影绘制失败，已跳过", exc_info=True)

    card_box = [MARGIN, MARGIN, MARGIN + CARD_W, MARGIN + card_h]
    # tint 比排行榜略薄（62 vs 80）：这张卡只有一列，面板占画面比例大，
    # 80 会把底下的暖色渐变糊成一片灰米色，看不出「焦糖奶油」的层次。
    img = _frosted_panel(PIL, img, card_box, 24, blur=12, tint_alpha=62, scale=S)
    draw = _make_draw(img, ImageDraw, font_dir, S)

    hx = MARGIN + pad
    right_edge = MARGIN + CARD_W - pad

    # ---------------- 标题：赤陶渐变竖条 + 「关系档案」 ----------------
    hy = MARGIN + pad
    _ink = draw.textbbox((0, 0), "关系档案", font=f_title)
    _ink_top, _ink_bot = _ink[1], _ink[3]
    _ink_h = max(1, _ink_bot - _ink_top)
    try:
        _bw, _bh = max(1, int(5 * S)), max(1, int(_ink_h * S))
        grad = PIL.Image.new("RGB", (_bw, _bh))
        gpx = grad.load()
        for yy in range(_bh):
            k = yy / max(_bh - 1, 1)
            col = tuple(int(_ACCENT[i] + (_ACCENT_LIGHT[i] - _ACCENT[i]) * k) for i in range(3))
            for xx in range(_bw):
                gpx[xx, yy] = col
        gmask = PIL.Image.new("L", (_bw, _bh), 0)
        PIL.ImageDraw.Draw(gmask).rounded_rectangle(
            [0, 0, _bw - 1, _bh - 1], radius=max(1, int(2.5 * S)), fill=255,
        )
        img.paste(grad, (int(hx * S), int((hy + 2 + _ink_top) * S)), gmask)
    except Exception:  # noqa: BLE001
        draw.rounded_rectangle([hx, hy + 2 + _ink_top, hx + 5, hy + 2 + _ink_top + _ink_h],
                               radius=2.5, fill=_ACCENT)
    draw.text((hx + 15, hy + 2), "关系档案", font=f_title, fill=_TEXT)

    # 右上：最近互动 / 更新时间（弱化）
    _meta_bits = []
    if last_at:
        _meta_bits.append("最近互动 " + time.strftime("%m-%d %H:%M", time.localtime(last_at)))
    elif updated_at:
        _meta_bits.append("更新于 " + time.strftime("%m-%d %H:%M", time.localtime(updated_at)))
    if max_abs_score:
        # 时间戳缺失时也要给一行右上角信息，否则标题右侧空一大块
        _meta_bits.append(f"上限 ±{max_abs_score}")
    meta_text = " · ".join(_meta_bits)
    if meta_text:
        _mw = int(draw.textlength(meta_text, font=f_meta))
        _mk = draw.textbbox((0, 0), meta_text, font=f_meta)
        _mcy = hy + 2 + (_ink_top + _ink_bot) / 2
        draw.text((right_edge - _mw, _mcy - (_mk[1] + _mk[3]) / 2),
                  meta_text, font=f_meta, fill=_MUTED)

    # 标题区分隔线
    _line_y = hy + header_h - 14
    draw.rectangle([hx, _line_y, right_edge, _line_y], fill=(244, 236, 226))

    # ---------------- 档案主体：头像 + 昵称 + ID + 阶段 + 分数 ----------------
    py = hy + header_h
    score_color = _ACCENT if score > 0 else (_COOL if score < 0 else _NEUTRAL)

    ax, ay = hx + AV_R, py + AV_R
    av_img = None
    if avatar_dir and uid.isdigit():
        av_img = _load_avatar_image(PIL, uid, avatar_dir)
    if av_img is not None:
        side = int(AV_R * 2 * S)
        try:
            thumb = av_img.resize((side, side), PIL.Image.LANCZOS)
            mask = PIL.Image.new("L", (side, side), 0)
            PIL.ImageDraw.Draw(mask).ellipse([0, 0, side, side], fill=255)
            img.paste(thumb, (int((ax - AV_R) * S), int((ay - AV_R) * S)), mask)
            draw.ellipse([ax - AV_R, ay - AV_R, ax + AV_R, ay + AV_R],
                         outline=(255, 250, 243), width=2)
        except Exception:  # noqa: BLE001
            av_img = None
    if av_img is None:
        av_fill, av_fg = _avatar_color(uid or display_name)
        draw.ellipse([ax - AV_R, ay - AV_R, ax + AV_R, ay + AV_R], fill=av_fill)
        f_av = _fit_font(draw, _avatar_initial(display_name), f_avatar,
                         AV_R * 2 - 10, AV_R * 2 - 12)
        _draw_centered(draw, [ax - AV_R, ay - AV_R, ax + AV_R, ay + AV_R],
                       _avatar_initial(display_name), f_av, av_fg)

    text_x = ax + AV_R + 18
    score_text = f"{score:+d}" if score != 0 else "0"
    _sw = int(draw.textlength(score_text, font=f_score))
    _uw = int(draw.textlength(f"/{max_abs_score}", font=f_unit)) if max_abs_score else 0
    score_x = right_edge - _sw - _uw
    draw.text((score_x, py - 4), score_text, font=f_score, fill=score_color)
    if max_abs_score:
        draw.text((score_x + _sw + 2, py + 18), f"/{max_abs_score}", font=f_unit, fill=_MUTED)

    # 双向进度条（以 0 为中点；与排行榜同款语义）
    bar_w, bar_h = 200, 6
    bar_x = right_edge - bar_w
    bar_y = py + 40
    mid_x = bar_x + bar_w // 2
    draw.rounded_rectangle([bar_x, bar_y, bar_x + bar_w, bar_y + bar_h], radius=3,
                           fill=(243, 235, 226))
    if max_abs_score:
        ratio = max(0.0, min(1.0, abs(score) / max_abs_score))
        fw = int((bar_w // 2) * ratio)
        if fw >= 2:
            if score >= 0:
                draw.rounded_rectangle([mid_x, bar_y, mid_x + fw, bar_y + bar_h],
                                       radius=3, fill=score_color)
            else:
                draw.rounded_rectangle([mid_x - fw, bar_y, mid_x, bar_y + bar_h],
                                       radius=3, fill=score_color)
    draw.rectangle([mid_x, bar_y - 1, mid_x + 1, bar_y + bar_h], fill=(224, 210, 194))

    # 昵称
    name_max_w = max(80, score_x - text_x - 16)
    draw.text((text_x, py - 6), _truncate(draw, display_name, f_name, name_max_w),
              font=f_name, fill=_TEXT)

    # ID 胶囊 + 阶段胶囊
    pill_y = py + 30
    pill_h = 22
    tx = text_x
    if uid:
        _id_txt = f"ID {uid}"
        _w = int(draw.textlength(_id_txt, font=f_id)) + 20
        draw.rounded_rectangle([tx, pill_y, tx + _w, pill_y + pill_h], radius=9.5,
                               fill=(244, 238, 230))
        _draw_centered(draw, [tx, pill_y, tx + _w, pill_y + pill_h], _id_txt, f_id,
                       (140, 118, 98))
        tx += _w + 8
    if stage:
        stage_bg, stage_fg = _STAGE_STYLES.get(stage, _STAGE_STYLES["中立"])
        _w = int(draw.textlength(stage, font=f_tag)) + 20
        if tx + _w <= right_edge:
            draw.rounded_rectangle([tx, pill_y, tx + _w, pill_y + pill_h], radius=9.5,
                                   fill=stage_bg)
            _draw_centered(draw, [tx, pill_y, tx + _w, pill_y + pill_h], stage, f_tag, stage_fg)

    y = py + profile_h

    # ---------------- 标签（全部铺开、按宽度折行）----------------
    if tag_rows:
        for row in tag_rows:
            tx = hx
            for tg in row:
                _w = int(draw.textlength(tg, font=f_tag)) + 20
                draw.rounded_rectangle([tx, y, tx + _w, y + pill_h], radius=9.5,
                                       fill=_ACCENT_SOFT)
                _draw_centered(draw, [tx, y, tx + _w, y + pill_h], tg, f_tag, _ACCENT)
                tx += _w + 8
            y += 28
        y += 14

    # ---------------- 稳定印象（完整渲染）----------------
    # 「稳定印象」前的赤陶色块：按**字形实际墨迹**对齐，而不是拍脑袋给 y+9。
    # ⚠️ 14px 字体的 textbbox 顶部有 ~3px ascender 空隙，按逻辑 y 偏移画条子会明显偏下
    #    （实测肉眼就是「色块没跟标题齐平」）。与排行榜标题红标同款处理：取 ink 上下沿。
    _lb = draw.textbbox((0, 0), "稳定印象", font=f_label)
    _lab_top, _lab_bot = _lb[1], _lb[3]
    # textbbox 给的是排版墨盒，14px 下比实际笔画略矮（实测条子明显短一截），
    # 这里上下各放 1~2px 让色块把整行字的视觉高度盖住，与标题红标观感一致。
    _lab_h = max(9, _lab_bot - _lab_top + 1)
    draw.rounded_rectangle(
        [hx, y + _lab_top, hx + 4, y + _lab_top + _lab_h], radius=2, fill=_ACCENT,
    )
    draw.text((hx + 12, y), "稳定印象", font=f_label, fill=_SUBTEXT)
    y += sum_label_h

    _empty = not summary
    for line in body_lines:
        draw.text((hx, y), line, font=f_body, fill=(_MUTED if _empty else (88, 68, 52)))
        y += line_h

    # ---------------- 页脚 ----------------
    fy = MARGIN + card_h - pad - foot_h + 10
    draw.text((hx, fy), "nekro_persona · 关系档案", font=f_foot, fill=_MUTED)

    img = _glass_edges(PIL, img, [(card_box, 24, (255, 255, 255, 150), (255, 255, 255, 210))], scale=S)
    img = _compose_layers(PIL, img, front_layers, stage="front",
                          base_ctx={"scale": S, "font_path": fp})
    buf = io.BytesIO()
    img.convert("RGB").save(buf, format="PNG")
    return buf.getvalue()


# ==================== 印象总览卡片（2.2.23） ====================

# 总览里每人最多给几行印象（超出打省略号）：单张卡片要能一屏看完，
# 想看全请用「查看@TA印象」那张单人卡。
_OVERVIEW_SUMMARY_LINES = 2        # 总览里每人最多 2 行印象（看全用单人卡）
_OVERVIEW_MAX_ITEMS = 14           # 人数上限（高度预算之外的第二道闸）
# 高度预算（逻辑像素）：QQ 里长图会被缩到看不清，所以按预算收人而不是固定人数。
# 实测 3000px 左右（scale 2.0 下 1500 逻辑像素）在手机上仍能滚着看清。
_OVERVIEW_HEIGHT_BUDGET = 1900
# 每列目标人数（与排行榜的 TARGET_PER_COL 同义）：超过它就开始并列，最多 3 列
_OVERVIEW_TARGET_PER_COL = 4


def render_impression_overview(
    entries: List[dict],
    max_abs_score: int,
    *,
    limit: int = 0,
    font_path: str = "",
    font_dir: str = "",
    avatar_dir: str = "",
    scale: float = 0.0,
    bg_path: str = "",
    bg_blur: float = 14.0,
    bg_dim: int = 0,
    layers: Any = None,
) -> bytes | None:
    """渲染「印象总览」：每人一行，带头像/昵称/ID/标签/稳定印象/好感度。不可用返回 None。"""
    try:
        return _render_impression_overview_impl(
            entries, max_abs_score, limit=limit, font_path=font_path,
            font_dir=font_dir, avatar_dir=avatar_dir, scale=scale,
            bg_path=bg_path, bg_blur=bg_blur, bg_dim=bg_dim, layers=layers,
        )
    except Exception:  # noqa: BLE001
        logger.exception("印象总览卡片渲染异常，回退为文本")
        return None


def _render_impression_overview_impl(
    entries: List[dict],
    max_abs_score: int,
    *,
    limit: int = 0,
    font_path: str = "",
    font_dir: str = "",
    avatar_dir: str = "",
    scale: float = 0.0,
    bg_path: str = "",
    bg_blur: float = 14.0,
    bg_dim: int = 0,
    layers: Any = None,
) -> bytes | None:
    """印象总览：按人数自动分列，**每格竖向节奏与「查看印象」单人卡一致**。

    单人卡的节奏（2.2.26 抄过来）：内边距 16 / 头像直径 44 / 名字块 30 /
    标签高 20 行距 6 / 「稳定印象」标题 30 / 正文 15px·25px 行距 / 底部内边距 16。
    上一版把这些数压成 14/26/18/24/13·20，中文行被压到字脚贴边，看着像被盖住。
    """
    back_layers, front_layers = _split_layers(layers, has_bg=bool(str(bg_path or "").strip()))
    PIL = _load_pil()
    if PIL is None:
        return None
    try:
        from PIL import ImageDraw, ImageFont
    except Exception:  # noqa: BLE001
        logger.warning("Pillow 子模块导入失败，印象总览回退为文本", exc_info=True)
        return None

    fp = _find_font(font_path=font_path, font_dir=font_dir)
    if not fp:
        return None
    try:
        f_title = ImageFont.truetype(fp, 27)
        f_meta = ImageFont.truetype(fp, 13)
        f_name = ImageFont.truetype(fp, 23)
        f_id = ImageFont.truetype(fp, 12)
        f_score = ImageFont.truetype(fp, 26)
        f_unit = ImageFont.truetype(fp, 12)
        f_tag = ImageFont.truetype(fp, 12)
        f_label = ImageFont.truetype(fp, 13)
        f_body = ImageFont.truetype(fp, 15)
        f_avatar = ImageFont.truetype(fp, 26)
        f_foot = ImageFont.truetype(fp, 11)
    except Exception:  # noqa: BLE001
        logger.exception("字体加载失败（印象总览）")
        return None

    # ── 竖向节奏（与单人卡对齐）──────────────────────────────────────
    MARGIN = 18
    pad = 26
    COL_W = 528                    # 与排行榜一致
    COL_GAP = 16
    header_h = 62
    cell_pad = 16
    AV_R = 22                      # 头像直径 44
    name_h = 56                     # 名字块 = 名字 30 + 胶囊 20 + 间距 6
    pill_h = 20
    tag_h = 20
    tag_gap = 6
    label_h = 30
    body_line_h = 25
    head_gap = 12                  # 名字块与标签之间
    tag_gap_after = 8              # 标签与「稳定印象」之间
    cell_gap = 14
    S = float(scale) if scale and scale > 0 else _RENDER_SCALE

    cap = int(limit) if limit and limit > 0 else _OVERVIEW_MAX_ITEMS
    pool = list(entries)[:cap]

    _measure = _make_draw(PIL.Image.new("RGB", (10, 10)), ImageDraw, font_dir, 1.0)
    inner_w = COL_W - cell_pad * 2
    text_x_off = AV_R * 2 + 14

    # ── 量每一格 ────────────────────────────────────────────────────
    cells = []
    for entry in pool:
        uid = str(entry.get("user_id") or "").strip()
        name = str(entry.get("display_name") or "").strip() or uid or "未知"
        if not any(ch.isalnum() or "\u4e00" <= ch <= "\u9fff" for ch in name):
            name = (uid[-8:] if uid else "") or name
        score = _as_int(entry.get("score"), 0)
        stage = str(entry.get("stage") or "").strip()
        raw_tags = entry.get("tags") or []
        if isinstance(raw_tags, str):
            raw_tags = raw_tags.replace("，", ",").replace("、", ",").split(",")
        tags = [str(t).strip() for t in raw_tags if str(t).strip()]

        tag_rows: List[List[str]] = []
        cur: List[str] = []
        cur_w = 0.0
        overflow = 0
        for tg in tags:
            w = float(_measure.textlength(tg, font=f_tag)) + 18
            if cur and cur_w + tag_gap + w > inner_w:
                tag_rows.append(cur)
                cur, cur_w = [tg], w
            else:
                cur.append(tg)
                cur_w += (tag_gap if cur[:-1] else 0) + w
        if cur:
            tag_rows.append(cur)
        if len(tag_rows) > 2:
            overflow = sum(len(r) for r in tag_rows[2:])
            tag_rows = tag_rows[:2]

        summary = _clean_summary(entry.get("summary")) or ""
        body_lines = _wrap_text(_measure, summary, f_body, inner_w) if summary else []
        if len(body_lines) > _OVERVIEW_SUMMARY_LINES:
            body_lines = body_lines[:_OVERVIEW_SUMMARY_LINES]
            body_lines[-1] = _truncate(_measure, body_lines[-1] + "…", f_body, inner_w)
        if not body_lines:
            body_lines = ["（暂无稳定印象）"]

        # ⚠️ 每格高度**固定**：预留 2 行标签 + 2 行正文。
        #    按实际内容算高度会让同一张卡里格子高低不一（用户反馈「高度不一样」），
        #    分列后列与列的行也对不齐，看着很乱。
        h = (
            cell_pad + name_h + head_gap
            + (2 * tag_h + tag_gap + tag_gap_after)      # 预留两行标签
            + label_h
            + _OVERVIEW_SUMMARY_LINES * body_line_h      # 预留两行正文
            + cell_pad
        )
        cells.append({
            "entry": entry, "uid": uid, "name": name, "score": score, "stage": stage,
            "tags": tag_rows, "overflow": overflow, "body": body_lines,
            "empty_body": not summary, "h": h,
        })

    if not cells:
        return None

    # ── 分列 + 高度预算 ────────────────────────────────────────────
    import math as _math

    def layout(items: List[dict], cols: int) -> List[List[dict]]:
        n = len(items)
        base, rem = divmod(n, cols)
        out, idx = [], 0
        for c in range(cols):
            size = base + (1 if c < rem else 0)
            out.append(items[idx:idx + size])
            idx += size
        return out

    def max_col_h(grid: List[List[dict]]) -> int:
        return max(
            (sum(c["h"] for c in col) + cell_gap * max(len(col) - 1, 0)) for col in grid
        ) if grid else 0

    shown = list(cells)
    while True:
        cols = max(1, min(3, _math.ceil(len(shown) / _OVERVIEW_TARGET_PER_COL)))
        grid = layout(shown, cols)
        if max_col_h(grid) <= _OVERVIEW_HEIGHT_BUDGET or len(shown) <= cols:
            break
        shown.pop()
    cols = max(1, min(3, _math.ceil(len(shown) / _OVERVIEW_TARGET_PER_COL)))
    grid = layout(shown, cols)
    omitted = len(entries) - len(shown)

    content_w = cols * COL_W + (cols - 1) * COL_GAP
    card_w = content_w + pad * 2
    rows_h = max_col_h(grid)
    foot_h = 26
    card_h = pad + header_h + max(rows_h, 60) + foot_h + pad
    img_w = card_w + MARGIN * 2
    img_h = card_h + MARGIN * 2

    _bg_ctx.is_image = bool(str(bg_path or "").strip())
    img = _compose_layers(
        PIL,
        _background(PIL, int(img_w * S), int(img_h * S),
                    path=bg_path, blur=bg_blur, dim=bg_dim, scale=S),
        back_layers, stage="back", base_ctx={"scale": S, "font_path": fp},
    )
    try:
        img = PIL.Image.alpha_composite(
            img.convert("RGBA"), _decorative_blobs(PIL, img.width, img.height)
        ).convert("RGB")
    except Exception:  # noqa: BLE001
        logger.debug("印象总览背景光斑绘制失败，已跳过", exc_info=True)

    try:
        from PIL import ImageFilter

        shadow = PIL.Image.new("RGBA", img.size, (0, 0, 0, 0))
        PIL.ImageDraw.Draw(shadow).rounded_rectangle(
            [int(MARGIN * S), int((MARGIN + 6) * S),
             int((MARGIN + card_w) * S), int((MARGIN + card_h + 6) * S)],
            radius=int(26 * S), fill=(96, 64, 40, 70),
        )
        img = PIL.Image.alpha_composite(
            img.convert("RGBA"), shadow.filter(ImageFilter.GaussianBlur(radius=10 * S))
        ).convert("RGB")
    except Exception:  # noqa: BLE001
        logger.debug("印象总览投影绘制失败，已跳过", exc_info=True)

    card_box = [MARGIN, MARGIN, MARGIN + card_w, MARGIN + card_h]
    img = _frosted_panel(PIL, img, card_box, 24, blur=12, tint_alpha=62, scale=S)
    draw = _make_draw(img, ImageDraw, font_dir, S)

    hx = MARGIN + pad
    right_edge = MARGIN + card_w - pad

    # ── 标题 ───────────────────────────────────────────────────────
    hy = MARGIN + pad
    _ink = draw.textbbox((0, 0), "印象总览", font=f_title)
    _ink_top, _ink_bot = _ink[1], _ink[3]
    _ink_h = max(1, _ink_bot - _ink_top)
    try:
        _bw, _bh = max(1, int(5 * S)), max(1, int(_ink_h * S))
        grad = PIL.Image.new("RGB", (_bw, _bh))
        gpx = grad.load()
        for yy in range(_bh):
            k = yy / max(_bh - 1, 1)
            col = tuple(int(_ACCENT[i] + (_ACCENT_LIGHT[i] - _ACCENT[i]) * k) for i in range(3))
            for xx in range(_bw):
                gpx[xx, yy] = col
        gmask = PIL.Image.new("L", (_bw, _bh), 0)
        PIL.ImageDraw.Draw(gmask).rounded_rectangle(
            [0, 0, _bw - 1, _bh - 1], radius=max(1, int(2.5 * S)), fill=255,
        )
        img.paste(grad, (int(hx * S), int((hy + 2 + _ink_top) * S)), gmask)
    except Exception:  # noqa: BLE001
        draw.rounded_rectangle([hx, hy + 2 + _ink_top, hx + 5, hy + 2 + _ink_top + _ink_h],
                               radius=2.5, fill=_ACCENT)
    draw.text((hx + 15, hy + 2), "印象总览", font=f_title, fill=_TEXT)

    meta_text = f"共 {len(entries)} 人（有标签）"
    if omitted:
        meta_text += f" · 本张显示 {len(shown)} 位"
    _mw = int(draw.textlength(meta_text, font=f_meta))
    _mk = draw.textbbox((0, 0), meta_text, font=f_meta)
    _mcy = hy + 2 + (_ink_top + _ink_bot) / 2
    draw.text((right_edge - _mw, _mcy - (_mk[1] + _mk[3]) / 2), meta_text, font=f_meta, fill=_MUTED)

    _line_y = hy + header_h - 14
    draw.rectangle([hx, _line_y, right_edge, _line_y], fill=(244, 236, 226))

    # ── 逐格 ───────────────────────────────────────────────────────
    row_base_y = hy + header_h
    _av_fail = [0]
    _edges = [(card_box, 24, (255, 255, 255, 150), (255, 255, 255, 210))]
    for c, col in enumerate(grid):
        cx0 = hx + c * (COL_W + COL_GAP)
        cx1 = cx0 + COL_W
        y = row_base_y
        for r in col:
            box = [cx0, y, cx1, y + r["h"]]
            img = _frosted_panel(PIL, img, box, 14, blur=10, tint_alpha=44, scale=S)
            draw = _make_draw(img, ImageDraw, font_dir, S)

            score = r["score"]
            score_color = _ACCENT if score > 0 else (_COOL if score < 0 else _NEUTRAL)
            draw.rounded_rectangle([cx0 + 7, y + 16, cx0 + 10, y + r["h"] - 16],
                                   radius=2, fill=score_color)

            # 头像
            ax, ay = cx0 + cell_pad + AV_R, y + cell_pad + AV_R
            av_img = None
            if avatar_dir and r["uid"].isdigit() and _av_fail[0] < 3:
                av_img = _load_avatar_image(PIL, r["uid"], avatar_dir)
                _av_fail[0] = 0 if av_img is not None else _av_fail[0] + 1
            if av_img is not None:
                side = int(AV_R * 2 * S)
                try:
                    thumb = av_img.resize((side, side), PIL.Image.LANCZOS)
                    mask = PIL.Image.new("L", (side, side), 0)
                    PIL.ImageDraw.Draw(mask).ellipse([0, 0, side, side], fill=255)
                    img.paste(thumb, (int((ax - AV_R) * S), int((ay - AV_R) * S)), mask)
                    draw.ellipse([ax - AV_R, ay - AV_R, ax + AV_R, ay + AV_R],
                                 outline=(255, 250, 243), width=2)
                except Exception:  # noqa: BLE001
                    av_img = None
            if av_img is None:
                av_fill, av_fg = _avatar_color(r["uid"] or r["name"])
                draw.ellipse([ax - AV_R, ay - AV_R, ax + AV_R, ay + AV_R], fill=av_fill)
                f_av = _fit_font(draw, _avatar_initial(r["name"]), f_avatar,
                                 AV_R * 2 - 8, AV_R * 2 - 9)
                _draw_centered(draw, [ax - AV_R, ay - AV_R, ax + AV_R, ay + AV_R],
                               _avatar_initial(r["name"]), f_av, av_fg)

            text_x = cx0 + cell_pad + text_x_off

            # 分数 + 上限（右上）
            score_text = f"{score:+d}" if score != 0 else "0"
            _sw = int(draw.textlength(score_text, font=f_score))
            _uw = int(draw.textlength(f"/{max_abs_score}", font=f_unit)) if max_abs_score else 0
            score_x = cx1 - cell_pad - _sw - _uw
            draw.text((score_x, y + cell_pad - 4), score_text, font=f_score, fill=score_color)
            if max_abs_score:
                draw.text((score_x + _sw + 1, y + cell_pad + 12), f"/{max_abs_score}",
                          font=f_unit, fill=_MUTED)

            # 昵称
            name_max_w = max(70, score_x - text_x - 10)
            draw.text((text_x, y + cell_pad - 6),
                      _truncate(draw, r["name"], f_name, name_max_w), font=f_name, fill=_TEXT)

            # ID / 阶段胶囊：顶边必须落在名字墨迹之下（23px 名字的 ink 底 ≈ cell_pad+28），
            # 否则胶囊会盖住名字下半截（上一版就是这么糊的）。
            pill_y = y + cell_pad + 30
            tx = text_x
            if r["uid"]:
                _id_txt = f"ID {r['uid']}"
                _w = int(draw.textlength(_id_txt, font=f_id)) + 16
                if tx + _w <= cx1 - cell_pad:
                    draw.rounded_rectangle([tx, pill_y, tx + _w, pill_y + pill_h], radius=9,
                                           fill=(244, 238, 230))
                    _draw_centered(draw, [tx, pill_y, tx + _w, pill_y + pill_h], _id_txt, f_id,
                                   (140, 118, 98))
                    tx += _w + 6
            if r["stage"]:
                stage_bg, stage_fg = _STAGE_STYLES.get(r["stage"], _STAGE_STYLES["中立"])
                _w = int(draw.textlength(r["stage"], font=f_tag)) + 16
                if tx + _w <= cx1 - cell_pad:
                    draw.rounded_rectangle([tx, pill_y, tx + _w, pill_y + pill_h], radius=9,
                                           fill=stage_bg)
                    _draw_centered(draw, [tx, pill_y, tx + _w, pill_y + pill_h], r["stage"],
                                   f_tag, stage_fg)

            # 标签
            ty = y + cell_pad + max(AV_R * 2, name_h) + head_gap
            for trow in r["tags"]:
                tx = cx0 + cell_pad
                for tg in trow:
                    _w = int(draw.textlength(tg, font=f_tag)) + 18
                    draw.rounded_rectangle([tx, ty, tx + _w, ty + tag_h], radius=9,
                                           fill=_ACCENT_SOFT)
                    _draw_centered(draw, [tx, ty, tx + _w, ty + tag_h], tg, f_tag, _ACCENT)
                    tx += _w + tag_gap
                if r["overflow"]:
                    _more = f"+{r['overflow']}"
                    _w = int(draw.textlength(_more, font=f_tag)) + 18
                    if tx + _w <= cx1 - cell_pad:
                        draw.rounded_rectangle([tx, ty, tx + _w, ty + tag_h], radius=9,
                                               fill=(244, 238, 230))
                        _draw_centered(draw, [tx, ty, tx + _w, ty + tag_h], _more, f_tag,
                                       (140, 118, 98))
                ty += tag_h + tag_gap
            if r["tags"]:
                ty += tag_gap_after - tag_gap

            # 「稳定印象」标题（色块按字形墨迹对齐）
            _lb = draw.textbbox((0, 0), "稳定印象", font=f_label)
            _lab_top, _lab_bot = _lb[1], _lb[3]
            _lab_h = max(9, _lab_bot - _lab_top + 1)
            _lx = cx0 + cell_pad
            draw.rounded_rectangle([_lx, ty + _lab_top, _lx + 4, ty + _lab_top + _lab_h],
                                   radius=2, fill=_ACCENT)
            draw.text((_lx + 11, ty), "稳定印象", font=f_label, fill=_SUBTEXT)
            ty += label_h

            for line in r["body"]:
                draw.text((cx0 + cell_pad, ty), line, font=f_body,
                          fill=(_MUTED if r["empty_body"] else (92, 72, 56)))
                ty += body_line_h

            _edges.append((box, 14, (255, 255, 255, 90), None))
            y += r["h"] + cell_gap

    # ── 页脚 ───────────────────────────────────────────────────────
    fy = MARGIN + card_h - pad - foot_h + 8
    if omitted:
        draw.text((hx, fy), f"另有 {omitted} 位有标签的人未显示（按好感度取前 {len(shown)} 位）",
                  font=f_foot, fill=_MUTED)
    _wm = "nekro_persona · 印象总览"
    draw.text((right_edge - int(draw.textlength(_wm, font=f_foot)), fy), _wm,
              font=f_foot, fill=_MUTED)

    img = _glass_edges(PIL, img, _edges, scale=S)
    img = _compose_layers(PIL, img, front_layers, stage="front",
                          base_ctx={"scale": S, "font_path": fp})
    buf = io.BytesIO()
    img.convert("RGB").save(buf, format="PNG")
    return buf.getvalue()
