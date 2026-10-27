"""字体能力体检：拿每个字体真渲染一批样本字符，判断它能当正文字体还是 emoji 字体。

用法（容器内）：
    /app/.venv/bin/python scripts/probe_fonts.py /path/to/fonts [--json]

输出每个字体的汉字覆盖率 / emoji 覆盖率 / 是否彩色 / 建议用途，
用来决定「哪个字体该放到正文字体、哪个该放到 emoji 字体」。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

# 允许直接 `python scripts/probe_fonts.py`：把插件包父目录塞进 sys.path
_PKG = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PKG.parent))

import nekro_persona  # noqa: E402,F401
from nekro_persona import card  # noqa: E402


def scan(font_dir: str) -> list:
    out = []
    root = Path(font_dir)
    if not root.is_dir():
        return out
    for ext in ("*.ttf", "*.ttc", "*.otf"):
        for f in sorted(root.rglob(ext)):
            cap = card.probe_font_capabilities(str(f))
            cap["name"] = f.name
            cap["path"] = str(f)
            out.append(cap)
    return out


def main(argv: list) -> int:
    args = [a for a in argv[1:] if not a.startswith("-")]
    as_json = "--json" in argv
    font_dir = args[0] if args else str(Path.home() / ".nekro_persona_fonts")
    rows = scan(font_dir)
    if as_json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
        return 0
    if not rows:
        print(f"没有找到字体文件: {font_dir}")
        return 1
    print(f"{'字体':<34}{'大小':>9}{'汉字':>8}{'emoji':>8}{'符号':>7}  彩色  建议用途")
    print("-" * 96)
    for r in rows:
        roles = "+".join(r["roles"]) or "（不建议）"
        mb = f"{r['bytes'] / 1024 / 1024:.1f} MB"
        print(
            f"{r['name']:<34}{mb:>9}{r['cjk'] * 100:>7.0f}%{r['emoji'] * 100:>7.0f}%"
            f"{r['symbols'] * 100:>6.0f}%  {'是' if r['color'] else '否'}   {roles}"
            + (f"  {r['note']}" if r.get("note") else "")
        )
    print()
    best_text = max((r for r in rows if "text" in r["roles"]), key=lambda r: r["cjk"], default=None)
    best_emoji = max(
        (r for r in rows if "emoji" in r["roles"]),
        key=lambda r: (r["color"], r["emoji"]), default=None,
    )
    print("推荐正文字体:", best_text["name"] if best_text else "（没有可用中文字体）")
    print("推荐 emoji 字体:", best_emoji["name"] if best_emoji else "（没有可用 emoji 字体）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
