#!/usr/bin/env python3
"""检索阈值标定工具（A6b）。

在真实 backgrounds + 真实嵌入上量化三档向量检索的阈值是否合适，输出三类信号：
  1) 噪声天花板：无关闲聊 query 对全库的最高 cos —— 阈值的**下界**参考。
  2) 语义密度：随机条目对 cos —— 库越拥挤，绝对阈值越难切分。
  3) 判别力：用条目 title 当 query，测「命中自己」vs「最佳他者」的可分区间与 top-1 率。

为何不直接外推一个「推荐阈值」：当正负分布重叠（本插件的人设库通常如此，
条目彼此语义相关），绝对 cos 阈值是弱杠杆；此时 top-1 命中率高说明该靠
**排序**（访问强化 / 热度 / 多样性）而非抬高阈值。脚本会据此给出区间与建议，
而不是一个可能损伤召回的魔法数字。

用法（在 NekroAgent 容器内，用插件 venv 跑）：
    python scripts/calibrate_threshold.py --bg <插件数据目录>/backgrounds [--sample 40]
会创建向量缓存 `.vec_cache.json`（首次跑要重新嵌入），但**不改**
背景条目内容、不动 tier4/关系档案。

⚠️ 后端一致性（v1.6.3）：标定必须在**与生产检索相同的向量空间**里做，否则结论失真。
脚本会自动读取 <bg>/../config.yaml 的 EMBED_* 配置：provider=custom 时注入到 memory，
query 侧统一走 memory._embed_query（与条目侧 _embed_entries 同源）；provider=na 时用
NA 内置。开头会打印实际后端与向量签名，杜绝「拿 NA 空间标定 custom 检索」的静默错误。
"""
import sys, asyncio, random, argparse, re
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import memory as M  # noqa: E402

NEG = ["今天天气不错", "在吗", "哈哈哈哈笑死", "晚上吃什么好", "我先去洗澡了",
       "随便聊点什么", "666", "早上好呀", "真的无语了", "这游戏真好玩",
       "周末去哪玩", "我饿了", "好困想睡觉", "你在干嘛呢", "发个表情包",
       "今天好累", "有人吗", "嘿嘿嘿", "这也太离谱了", "我去吃饭了"]


def _apply_embed_backend(bg_dir: str) -> str:
    """读 <bg>/../config.yaml 的 EMBED_*，custom 时注入 memory 的嵌入配置提供者。

    返回一行后端描述用于打印。任何异常/缺配置都安全回落 NA 内置（与生产同口径）。
    """
    cfg_path = Path(bg_dir).parent / "config.yaml"
    raw = {}
    try:
        if cfg_path.is_file():
            for line in cfg_path.read_text(encoding="utf-8").splitlines():
                m = re.match(r"^(EMBED_[A-Z_]+):\s*(.*)$", line.strip())
                if m:
                    raw[m.group(1)] = m.group(2).strip().strip("'\"")
    except Exception as exc:  # noqa: BLE001
        return f"na（读取 config.yaml 失败，回落 NA 内置：{exc}）"

    provider = str(raw.get("EMBED_PROVIDER", "na") or "na").strip().lower()
    if provider != "custom":
        return "na（NekroAgent 内置嵌入）"
    base_url = str(raw.get("EMBED_BASE_URL", "") or "").strip()
    model = str(raw.get("EMBED_MODEL", "") or "").strip()
    if not base_url or not model:
        return "custom 配置不全（缺 base_url/model）→ memory 侧会自动回落 NA 内置"

    cfg = {
        "provider": "custom",
        "base_url": base_url,
        "api_key": str(raw.get("EMBED_API_KEY", "") or "").strip(),
        "model": model,
        "dimensions": int(raw.get("EMBED_DIMENSIONS", 0) or 0),
        "batch_size": max(1, int(raw.get("EMBED_BATCH_SIZE", 10) or 10)),
        "timeout": 30,
    }
    M.set_embed_config_provider(lambda: cfg)
    return f"custom（{model} @ {base_url}, dim={cfg['dimensions'] or '模型默认'}）"


def pct(xs, p):
    if not xs:
        return 0.0
    xs = sorted(xs); k = (len(xs) - 1) * p / 100.0
    lo = int(k); hi = min(lo + 1, len(xs) - 1)
    return xs[lo] + (xs[hi] - xs[lo]) * (k - lo)


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--bg", required=True, help="backgrounds 目录（插件数据目录下）")
    ap.add_argument("--sample", type=int, default=40, help="判别力采样条目数")
    ap.add_argument("--current", type=float, default=0.35, help="当前阈值，用于判定")
    args = ap.parse_args()

    M.configure_bg_dir(args.bg)
    # ⚠️ 必须在 ensure_loaded 之前注入后端：条目向量在 ensure_loaded→_vectorize 时嵌入，
    # query 向量在下面用 _embed_query 嵌入，两者必须同一后端/同一空间，标定才有意义。
    backend = _apply_embed_backend(args.bg)
    await M.ensure_loaded(force=True)
    print("嵌入后端: %s" % backend)
    print("向量签名: %s" % M._embed_signature())
    print("（标定与生产检索同空间；如与线上 EMBED_PROVIDER 不符，结论不可用）")
    S = M._state()      # ⚠️ v2.0.2 起 _state 是函数：漏掉括号会让下面 S["tier2"] 直接 TypeError
    qvecs = [(q, v) for q, v in zip(NEG, [await M._embed_query(q) for q in NEG]) if v]
    random.seed(7)

    for label, items, vecs in (("TIER2", S["tier2"], S["tier2_vecs"]),
                               ("TIER3", S["tier3"], S["tier3_vecs"])):
        valid = [(e, v) for e, v in zip(items, vecs) if v]
        print("\n===== %s（%d 条 / 有效向量 %d）=====" % (label, len(items), len(valid)))
        if not valid:
            print("  无有效向量，跳过"); continue

        neg_max = []
        for q, qv in qvecs:
            neg_max.append(max((M._cos(qv, v) for _e, v in valid), default=0.0))
        n = len(valid); pairs = []; tries = 0
        cap = min(3000, n * (n - 1) // 2)
        while len(pairs) < cap and tries < 30000:
            tries += 1
            i, j = random.randrange(n), random.randrange(n)
            if i != j:
                pairs.append(M._cos(valid[i][1], valid[j][1]))
        titled = [(e, v) for e, v in valid if str(e.get("title", "")).strip()]
        sample = random.sample(titled, min(args.sample, len(titled))) if titled else []
        own, other, rank1 = [], [], 0
        for e, _v in sample:
            qv = await M._embed_query(str(e.get("title", "")).strip())
            if not qv:
                continue
            scored = sorted(((M._cos(qv, vv), ee.get("id")) for ee, vv in valid), key=lambda x: -x[0])
            sid = e.get("id")
            own.append(next((s for s, i in scored if i == sid), 0.0))
            other.append(max((s for s, i in scored if i != sid), default=0.0))
            if scored and scored[0][1] == sid:
                rank1 += 1

        print("噪声天花板(无关query最高cos): P90=%.4f P95=%.4f MAX=%.4f" % (
            pct(neg_max, 90), pct(neg_max, 95), max(neg_max, default=0.0)))
        print("语义密度(随机条目对cos):    P90=%.4f P95=%.4f MAX=%.4f" % (
            pct(pairs, 90), pct(pairs, 95), max(pairs, default=0.0)))
        if own:
            print("判别力(title当query,采样%d): 自己P5=%.4f 最佳他者P95=%.4f top1=%.0f%%" % (
                len(own), pct(own, 5), pct(other, 95), 100.0 * rank1 / len(own)))
            lo, hi = pct(other, 95), pct(own, 5)
            if hi > lo:
                print("=> 可分区间 (%.3f, %.3f)，中点 %.2f 可试；当前 %.2f %s" % (
                    lo, hi, (lo + hi) / 2, args.current,
                    "偏低" if args.current < lo else "在区间内" if args.current <= hi else "偏高(伤召回)"))
            else:
                print("=> 正负重叠：绝对阈值是弱杠杆。当前 %.2f 相对噪声顶(%.3f)%s。" % (
                    args.current, max(neg_max),
                    "合理(略高于闲聊噪声)" if args.current >= pct(neg_max, 95) else "偏低(放进闲聊噪声)"))
                print("   top-1=%.0f%% 说明该靠**排序**(访问强化/热度/多样性)而非抬阈值；" % (100.0 * rank1 / len(own)))
                print("   抬到 %.2f 以上会开始误杀正确条目(自己P5=%.3f)。" % (round(pct(own, 5), 2), pct(own, 5)))


if __name__ == "__main__":
    asyncio.run(main())
