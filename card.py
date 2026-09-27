"""好感度排行榜图片卡片渲染（Pillow）。

暖色卡片主题（三段式渐变背景 + 圆角排行行）：
- 三段式竖向渐变背景（#dff4f4 → #fffaf1 → #ffe4d5）
- 强调色 #d96e64、深青文字 #294651
- 圆角排行行：名次徽标 + 昵称/ID + 阶段标签 + 好感度分值

Pillow 优先直接 import 主环境已装版本；缺失时通过 NA dynamic_import_pkg
多镜像动态安装。中文字体依次探测：配置指定 → 插件数据目录 fonts → 系统常见 CJK 字体。
任一环节不可用返回 None，调用方回退为文本。
"""

from __future__ import annotations

import importlib
import io
import math
import random
import time
import urllib.request
import site
from pathlib import Path
from typing import Any, List

from nekro_agent.api.plugin import dynamic_import_pkg
from nekro_agent.core.logger import get_sub_logger
from nekro_agent.core.os_env import PLUGIN_DYNAMIC_PACKAGE_DIR

logger = get_sub_logger("nekro_persona")

# MomoTune 暖色配色
_ACCENT = (217, 110, 100)         # #d96e64
_ACCENT_SOFT = (255, 240, 231)    # #fff0e7
_TEXT = (41, 70, 81)              # #294651
_SUBTEXT = (95, 133, 136)         # #5f8588
_MUTED = (141, 167, 164)          # #8da7a4
_BG_TOP = (223, 244, 244)         # #dff4f4
_BG_MID = (255, 250, 241)         # #fffaf1
_BG_BOTTOM = (255, 228, 213)      # #ffe4d5
_ROW_BG = (255, 255, 255)
_BORDER = (224, 139, 119)
_WHITE = (255, 255, 255)

# 各阶段标签配色 (背景, 前景)
_STAGE_STYLES = {
    "排斥": ((255, 228, 213), (191, 84, 62)),
    "保留": ((223, 244, 244), (57, 125, 134)),
    "中立": ((240, 240, 240), (95, 133, 136)),
    "亲近": ((221, 240, 225), (62, 142, 90)),
    "偏爱": ((255, 240, 231), (217, 110, 100)),
    "特别亲密": ((217, 110, 100), (255, 255, 255)),
}

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


def _find_font(font_path: str = "", font_dir: str = "") -> str | None:
    """查找可用的中文字体文件路径，找不到返回 None。"""
    global _font_cache, _font_checked
    if _font_checked:
        return _font_cache
    _font_checked = True

    candidates: list[str] = []
    if font_path.strip():
        candidates.append(font_path.strip())
    if font_dir.strip():
        candidates.append(font_dir.strip())
    candidates.extend(_FONT_CANDIDATES)

    for item in candidates:
        p = Path(item)
        if p.is_file() and p.suffix.lower() in (".ttf", ".ttc", ".otf"):
            _font_cache = str(p)
            return _font_cache
        if p.is_dir():
            for ext in ("*.ttf", "*.ttc", "*.otf"):
                for f in sorted(p.rglob(ext)):
                    name = f.name.lower()
                    if any(
                        k in name
                        for k in ("cjk", "wqy", "ming", "song", "hei", "yahei", "pingfang", "noto", "wenkai", "misans")
                    ):
                        _font_cache = str(f)
                        return _font_cache
    logger.warning("未找到中文字体，好感度排行榜卡片将回退为文本")
    return None


# ============ emoji 支持 ============
# Pillow 无字体回退能力，这里做「分段渲染」：emoji 段用彩色 emoji 字体
# （Segoe UI Emoji / Noto Color Emoji，embedded_color 上色），其余段用中文字体。
# 通过 _make_draw() 包装 ImageDraw，现有绘制代码无需改动。

_EMOJI_FONT_CANDIDATES = [
    "/usr/share/fonts/truetype/windows/seguiemj.ttf",
    "/usr/share/fonts/truetype/noto/NotoColorEmoji.ttf",
    "/usr/share/fonts/opentype/noto/NotoColorEmoji.ttf",
    "/usr/share/fonts/truetype/noto/NotoEmoji-Regular.ttf",
    "/usr/share/fonts/truetype/ancient-scripts/Symbola_hint.ttf",
]

_emoji_font_path: str | None = None
_emoji_font_checked = False
_emoji_font_cache: dict = {}


def _find_emoji_font(font_dir: str = "") -> str | None:
    """查找 emoji 字体：插件数据目录 fonts 下文件名含 emoji 的字体 → 系统常见路径。"""
    global _emoji_font_path, _emoji_font_checked
    if _emoji_font_checked:
        return _emoji_font_path
    _emoji_font_checked = True
    if font_dir.strip():
        d = Path(font_dir.strip())
        if d.is_dir():
            for ext in ("*.ttf", "*.ttc", "*.otf"):
                for f in sorted(d.rglob(ext)):
                    if "emoji" in f.name.lower():
                        _emoji_font_path = str(f)
                        return _emoji_font_path
    for item in _EMOJI_FONT_CANDIDATES:
        p = Path(item)
        if p.is_file():
            _emoji_font_path = str(p)
            return _emoji_font_path
    logger.warning("未找到 emoji 字体，卡片中的 emoji 将显示为豆腐块（可放 *emoji*.ttf 到数据目录 fonts/）")
    return None


def _is_emoji_cp(cp: int) -> bool:
    """粗略判断码点是否属于 emoji 区间（含 ZWJ / 变体选择符 / 肤色 / 国旗）。"""
    return (
        0x1F300 <= cp <= 0x1FAFF
        or 0x1F000 <= cp <= 0x1F2FF
        or 0x1F1E6 <= cp <= 0x1F1FF
        or 0x2600 <= cp <= 0x27BF
        or 0x2190 <= cp <= 0x21FF
        or 0x2900 <= cp <= 0x297F
        or 0x2B00 <= cp <= 0x2BFF
        or 0xFE00 <= cp <= 0xFE0F
        or 0x1F3FB <= cp <= 0x1F3FF
        or cp in (0x200D, 0x20E3, 0x3030, 0x303D, 0x3297, 0x3299, 0x00A9, 0x00AE, 0x2122)
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


def _emoji_font_for(font: Any) -> Any | None:
    """按主字体字号取 emoji 字体实例（同字号缓存）；不可用返回 None。"""
    if not _emoji_font_path:
        return None
    size = getattr(font, "size", None)
    if not size:
        return None
    key = int(size)
    if key not in _emoji_font_cache:
        try:
            from PIL import ImageFont

            _emoji_font_cache[key] = ImageFont.truetype(_emoji_font_path, key)
        except Exception:  # noqa: BLE001
            _emoji_font_cache[key] = None
    return _emoji_font_cache[key]


# 渲染倍率：卡片按逻辑尺寸（680 宽设计稿）布局，再以此倍率超采样渲染输出。
# 2.0 = Retina 级清晰度；调大更清晰但图片更大（3.0 时约 2.25 倍像素量）。
_RENDER_SCALE = 1.5   # 超采样倍率（扩展：2.0→1.5，文件约降 40%；可被配置覆盖）


class _EmojiDraw:
    """包装 ImageDraw：text/textlength/textbbox 自动分段处理 emoji，其余方法透传。"""

    def __init__(self, draw: Any, font_dir: str = ""):
        self._draw = draw
        _find_emoji_font(font_dir)

    def _emoji(self, text: Any, font: Any):
        if font is None or not isinstance(text, str) or not text:
            return None
        return _emoji_font_for(font)

    def text(self, xy, text, fill=None, font=None, **kwargs):
        ef = self._emoji(text, font)
        runs = _split_emoji_runs(text) if ef else None
        if not runs or not any(is_e for _s, is_e in runs):
            return self._draw.text(xy, text, fill=fill, font=font, **kwargs)
        x = xy[0]
        for seg, is_e in runs:
            if is_e:
                self._draw.text((x, xy[1]), seg, font=ef, embedded_color=True, **kwargs)
                x += self._draw.textlength(seg, font=ef)
            else:
                self._draw.text((x, xy[1]), seg, fill=fill, font=font, **kwargs)
                x += self._draw.textlength(seg, font=font)
        return None

    def textlength(self, text, font=None, **kwargs):
        ef = self._emoji(text, font)
        runs = _split_emoji_runs(text) if ef else None
        if not runs or not any(is_e for _s, is_e in runs):
            return self._draw.textlength(text, font=font, **kwargs)
        return sum(
            self._draw.textlength(seg, font=ef if is_e else font, **kwargs) for seg, is_e in runs
        )

    def textbbox(self, xy, text, font=None, **kwargs):
        ef = self._emoji(text, font)
        runs = _split_emoji_runs(text) if ef else None
        if not runs or not any(is_e for _s, is_e in runs):
            return self._draw.textbbox(xy, text, font=font, **kwargs)
        box = None
        x = xy[0]
        for seg, is_e in runs:
            f = ef if is_e else font
            bb = self._draw.textbbox((x, xy[1]), seg, font=f, **kwargs)
            box = bb if box is None else (min(box[0], bb[0]), min(box[1], bb[1]), max(box[2], bb[2]), max(box[3], bb[3]))
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
        key = id(font)
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
        if _find_emoji_font(font_dir):
            draw = _EmojiDraw(draw, font_dir)
    except Exception:  # noqa: BLE001
        logger.debug("emoji 包装失败，回退原生 draw", exc_info=True)
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
    """三段暖色竖向渐变，近似 MomoTune 模板 linear-gradient。"""
    img = PIL.Image.new("RGB", (width, height))
    px = img.load()
    stops = [(0.0, _BG_TOP), (0.49, _BG_MID), (1.0, _BG_BOTTOM)]
    for y in range(height):
        t = y / max(height - 1, 1)
        for i in range(len(stops) - 1):
            t0, c0 = stops[i]
            t1, c1 = stops[i + 1]
            if t0 <= t <= t1:
                k = (t - t0) / max(t1 - t0, 1e-9)
                col = tuple(int(c0[j] + (c1[j] - c0[j]) * k) for j in range(3))
                break
        else:
            col = _BG_BOTTOM
        for x in range(width):
            px[x, y] = col
    return img


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


def _clean_summary(value: Any) -> str:
    """取印象记录文本；空值或占位文案返回空串（调用方据此不渲染该标签）。"""
    s = str(value or "").strip()
    return "" if s in _EMPTY_SUMMARY_TEXTS else s


# 头像占位配色（扩展）：按 user_id 哈希稳定取色，同一人始终同色
_AVATAR_COLORS = [
    ((255, 228, 213), (191, 84, 62)),    # 暖橙
    ((223, 244, 244), (57, 125, 134)),   # 青
    ((221, 240, 225), (62, 142, 90)),    # 绿
    ((255, 240, 231), (217, 110, 100)),  # 粉
    ((232, 236, 246), (86, 104, 160)),   # 蓝紫
    ((245, 235, 250), (130, 92, 160)),   # 紫
    ((252, 240, 218), (170, 128, 40)),   # 金
    ((235, 242, 250), (70, 110, 150)),   # 灰蓝
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
            Path(avatar_dir).mkdir(parents=True, exist_ok=True)
            fp.write_bytes(data)
            return PIL.Image.open(io.BytesIO(data)).convert("RGB")
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
) -> bytes | None:
    """渲染好感度排行榜为 PNG 卡片，返回字节；不可用时返回 None。

    Args:
        entries: 已按 score 从高到低排序的档案摘要，每项含
            display_name / user_id / score / stage / updated_at。
        max_abs_score: 好感度绝对值上限，用于分值展示。
        limit: 卡片最多展示行数，超出部分在卡片中提示省略。
        font_path: 配置指定的中文字体文件路径。
        font_dir: 插件数据目录中的字体目录。
    """
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
        f_title = ImageFont.truetype(font_path_resolved, 32)
        f_hint = ImageFont.truetype(font_path_resolved, 16)
        f_name = ImageFont.truetype(font_path_resolved, 18)
        f_id = ImageFont.truetype(font_path_resolved, 15)
        f_score = ImageFont.truetype(font_path_resolved, 20)
        f_stage = ImageFont.truetype(font_path_resolved, 14)
        f_rank = ImageFont.truetype(font_path_resolved, 18)
        f_tag = ImageFont.truetype(font_path_resolved, 11)
        f_sub = ImageFont.truetype(font_path_resolved, 12)   # 副标题/页脚
        f_avatar = ImageFont.truetype(font_path_resolved, 18)   # 头像首字
        f_rank_small = ImageFont.truetype(font_path_resolved, 11)  # 名次小徽章
    except Exception:  # noqa: BLE001
        logger.exception("字体加载失败")
        return None

    # ---------------- 渲染全部（扩展：多列动态布局）----------------
    shown = list(entries) if not limit else list(entries[:limit])
    total = len(shown)

    MARGIN = 18          # 图片四周留白，让白色卡片"浮"在渐变背景上
    COL_W = 520          # 单列宽
    COL_GAP = 16         # 列间距
    pad = 24             # 卡片内边距
    header_h = 50        # 标题区（只剩标题）
    row_h = 68
    gap = 8
    bottom_pad = 22      # 底部内边距
    # 标签约束：控制单条长度与总条数，避免占满整行
    MAX_TAGS = 4        # 标签行最多几个（含 ID）
    MAX_TAG_CHARS = 7   # 关系标签 / 印象记录的字数上限
    MAX_ID_CHARS = 18   # ID 标签放宽（ID:web_user_1 是 13 字符，12 会截断）

    # 动态列数（扩展）：列数 = ceil(n/15)，列内**均摊余数**（各列最多差 1）。
    # ⚠️ 不能用"顺序填充 + ceil 列高"：那样在 n=15k+1 时会退化成 15×k + 1
    #    （如 n=211 → 14 列满 + 末列仅 1 个，视觉上是个空洞列）。
    # 例：15 → [15]；16 → [8,8]；46 → [12,12,11,11]；60 → [15,15,15,15]；
    #     108 → [14,14,14,14,13,13,13,13]；211 → [15,14×14]
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
    card_h = pad + header_h + rows_h - gap + bottom_pad
    img_w = card_w + MARGIN * 2
    img_h = card_h + MARGIN * 2
    S = float(scale) if scale and scale > 0 else _RENDER_SCALE

    img = _vertical_bg(PIL, int(img_w * S), int(img_h * S))

    # 卡片柔和投影（独立图层 + 高斯模糊；失败则静默跳过）
    try:
        from PIL import ImageFilter

        shadow = PIL.Image.new("RGBA", img.size, (0, 0, 0, 0))
        sd = PIL.ImageDraw.Draw(shadow)
        sd.rounded_rectangle(
            [
                int(MARGIN * S), int((MARGIN + 5) * S),
                int((MARGIN + width) * S), int((MARGIN + card_h + 5) * S),
            ],
            radius=int(24 * S), fill=(58, 72, 82, 70),
        )
        shadow = shadow.filter(ImageFilter.GaussianBlur(radius=9 * S))
        img = PIL.Image.alpha_composite(img.convert("RGBA"), shadow).convert("RGB")
    except Exception:  # noqa: BLE001
        logger.debug("卡片投影绘制失败，已跳过", exc_info=True)

    draw = _make_draw(img, ImageDraw, font_dir, S)

    # 卡片本体
    draw.rounded_rectangle(
        [MARGIN, MARGIN, MARGIN + card_w, MARGIN + card_h],
        radius=24, fill=(255, 255, 255),
    )

    hy = MARGIN + pad                  # 标题区顶部

    # ---------------- 标题区 ----------------
    hx = MARGIN + pad                  # 标题左边界（跨列统一）
    draw.rounded_rectangle([hx, hy + 5, hx + 5, hy + 33], radius=3, fill=_ACCENT)
    draw.text((hx + 16, hy), "好感度排行榜", font=f_title, fill=_TEXT)

    rank_colors = [(230, 168, 42), (186, 197, 205), (202, 150, 107)]   # 金/银/铜
    _av_fail = [0]   # 头像抓取连续失败计数（断网保护）
    row_base_y = hy + header_h
    TOP_BG = [(255, 250, 235), (246, 249, 251), (253, 245, 237)]        # 前三名淡底
    TOP_BORDER = [(240, 214, 150), (206, 216, 224), (232, 205, 178)]
    SOFT_BORDER = (237, 240, 242)
    TRACK = (238, 241, 243)

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

        # 行卡片微投影：多层偏移模拟柔和阴影（逐行高斯模糊太贵）
        for k, sc_col in ((3, (240, 243, 246)), (2, (234, 239, 243)), (1, (228, 234, 239))):
            draw.rounded_rectangle(
                [cx0, y + k, cx1, y + row_h + k], radius=14, fill=sc_col,
            )

        draw.rounded_rectangle(
            [cx0, y, cx1, y + row_h],
            radius=14,
            fill=TOP_BG[rank - 1] if is_top else (255, 255, 255),
            outline=TOP_BORDER[rank - 1] if is_top else SOFT_BORDER,
            width=1,
        )

        right_edge = cx1 - 12
        AVATAR_INSET = 10
        text_x = cx0 + AVATAR_INSET + 40 + 12   # 头像(40)右侧留 12px 间隙
        pad_in, gap_tag = 8, 5
        line2_y = y + 38           # 第二行（标签行）顶部
        pill_h = 20

        # ---------- 左：头像占位（昵称首字）+ 名次小徽章叠左下角 ----------
        uid_for_avatar = str(entry.get("user_id") or entry.get("display_name") or "")
        ax, ay, ar = cx0 + AVATAR_INSET + 20, y + row_h // 2, 20
        av_img = None
        # 连续失败 3 次就放弃后续抓取，避免断网时整张图卡住
        if avatar_dir and _av_fail[0] < 3:
            av_img = _load_avatar_image(PIL, uid_for_avatar, avatar_dir)
            if av_img is None:
                _av_fail[0] += 1
        if av_img is not None:
            side = int(ar * 2 * S)
            try:
                thumb = av_img.resize((side, side), PIL.Image.LANCZOS)
                mask = PIL.Image.new("L", (side, side), 0)
                PIL.ImageDraw.Draw(mask).ellipse([0, 0, side, side], fill=255)
                img.paste(thumb, (int((ax - ar) * S), int((ay - ar) * S)), mask)
                draw.ellipse([ax - ar, ay - ar, ax + ar, ay + ar], outline=(255, 255, 255), width=2)
            except Exception:  # noqa: BLE001
                av_img = None
        if av_img is None:
            av_fill, av_fg = _avatar_color(uid_for_avatar)
            draw.ellipse([ax - ar, ay - ar, ax + ar, ay + ar], fill=av_fill)
            av_ch = _avatar_initial(entry.get("display_name") or entry.get("user_id"))
            _draw_centered(
                draw, [ax - ar, ay - ar, ax + ar, ay + ar], av_ch, f_avatar, av_fg,
            )
        # 名次小徽章（白描边使其从头像上"浮"出来）
        rcx, rcy, rr = ax - 12, ay + 12, 11
        if rank <= 3:
            rank_fill, rank_fg = rank_colors[rank - 1], _WHITE
        else:
            rank_fill, rank_fg = (255, 255, 255), _ACCENT
        draw.ellipse(
            [rcx - rr, rcy - rr, rcx + rr, rcy + rr],
            fill=rank_fill, outline=(255, 255, 255), width=2,
        )
        rt = str(rank)
        _draw_centered(
            draw, [rcx - rr, rcy - rr, rcx + rr, rcy + rr], rt, f_rank_small, rank_fg,
        )

        # ---------- 左上：昵称（只需避开右侧好感度，宽度上限已恢复）----------
        name_max_w = max(48, right_edge - text_x - 104)
        raw_name = entry.get("display_name") or entry.get("user_id") or "未知"
        name = _truncate(draw, str(raw_name), f_name, name_max_w)
        nb = draw.textbbox((0, 0), name, font=f_name)
        draw.text((text_x, y + 10 - nb[1]), name, font=f_name, fill=_TEXT)

        # ---------- 右上：好感度 ----------
        score = int(entry.get("score", 0))
        score_text = f"+{score}" if score >= 0 else str(score)
        score_color = _ACCENT if score >= 0 else (57, 125, 134)
        sw = int(draw.textlength(score_text, font=f_score))
        draw.text((right_edge - sw, y + 7), score_text, font=f_score, fill=score_color)

        # 分数进度条（-max … +max 映射为 0…1，0 分正好半格）
        bar_w, bar_h = 74, 4
        bar_x = right_edge - bar_w
        bar_y = y + 31
        draw.rounded_rectangle(
            [bar_x, bar_y, bar_x + bar_w, bar_y + bar_h], radius=2, fill=TRACK,
        )
        denom = 2 * max_abs_score if max_abs_score else 0
        ratio = (max(0.0, min(1.0, (score + max_abs_score) / denom)) if denom else 0.0)
        fill_w = int(bar_w * ratio)
        if fill_w >= 4:
            draw.rounded_rectangle(
                [bar_x, bar_y, bar_x + fill_w, bar_y + bar_h], radius=2, fill=score_color,
            )

        # ---------- 右下：好感等级 ----------
        stage = str(entry.get("stage") or "").strip()
        stage_w = 0
        if stage:
            stage_bg, stage_fg = _STAGE_STYLES.get(stage, _STAGE_STYLES["中立"])
            stage_w = int(draw.textlength(stage, font=f_tag)) + pad_in * 2
            sx = right_edge - stage_w
            draw.rounded_rectangle(
                [sx, line2_y, sx + stage_w, line2_y + pill_h],
                radius=9, fill=stage_bg, outline=stage_bg, width=1,
            )
            _draw_centered(
                draw, [sx, line2_y, sx + stage_w, line2_y + pill_h], stage, f_tag, stage_fg,
            )

        # ---------- 左下：ID + 关系标签 + 印象记录（独占一行，空间充足）----------
        uid = str(entry.get("user_id") or "")
        summ = _clean_summary(entry.get("summary"))
        rel = [str(t).strip() for t in (entry.get("tags") or []) if str(t).strip()]
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
                tag_bg, tag_fg = (243, 245, 247), (124, 138, 148)
            elif kind == "SUM":
                tag_bg, tag_fg = (229, 243, 241), (54, 118, 114)
            else:
                tag_bg, tag_fg = _ACCENT_SOFT, _ACCENT
            draw.rounded_rectangle(
                [tx, line2_y, tx + pw, line2_y + pill_h],
                radius=9, fill=tag_bg, outline=tag_bg, width=1,
            )
            _draw_centered(draw, [tx, line2_y, tx + pw, line2_y + pill_h], tg, f_tag, tag_fg)
            tx += pw + gap_tag


    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()
