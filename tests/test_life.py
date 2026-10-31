"""生活时间轴（life.py）的回归测试：校验 / 修补 / 抽签冷却 / 手感衰减 / 跨天滚动 / 渲染。

用例是 unittest 风格（pytest 也能直接跑）。
运行（必须在 NekroAgent 容器内）：
    docker exec <容器> sh -c 'cd <插件目录> && /app/.venv/bin/python -m unittest discover -s tests -v'

全部用例都在**不碰数据库、不碰模型**的前提下跑：Engine 的依赖是注入的假实现。
"""
from __future__ import annotations

import asyncio
import json
import sys
import unittest
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))   # 支持按模块名单独运行

from _bootstrap import load_plugin_module

load_plugin_module()
import nekro_persona.life as L   # noqa: E402  # 依赖 bootstrap 把包目录接进 sys.path


# ---------------- 测试替身 ----------------

class FakeConfig:
    """只带 life 相关字段的配置替身。"""

    def __init__(self, **over):
        self.LIFE_ENABLED = over.get("LIFE_ENABLED", True)
        self.LIFE_MODEL_GROUP = over.get("LIFE_MODEL_GROUP", "")
        self.LIFE_PLAN_MIN_SEGMENTS = over.get("LIFE_PLAN_MIN_SEGMENTS", 12)
        self.LIFE_PLAN_MAX_SEGMENTS = over.get("LIFE_PLAN_MAX_SEGMENTS", 18)
        self.LIFE_BLOCK_CAP = over.get("LIFE_BLOCK_CAP", 380)
        self.LIFE_PLAN_STYLE_HINT = over.get("LIFE_PLAN_STYLE_HINT", "")
        self.LIFE_INJECT_ENABLED = over.get("LIFE_INJECT_ENABLED", True)
        self.LIFE_PLAN_HORIZON = over.get("LIFE_PLAN_HORIZON", 1)


class FakeStore:
    """最简 KV：读写都经过它，用来观察 Engine 到底存了什么。"""

    def __init__(self):
        self.data = {}
        self.writes = 0
        self.reads = 0

    async def read(self, key):
        self.reads += 1
        raw = self.data.get(key)
        return json.loads(raw) if raw else None

    async def write(self, key, value):
        self.writes += 1
        self.data[key] = json.dumps(value, ensure_ascii=False)


def make_engine(*, reply=None, cfg=None, store=None):
    store = store or FakeStore()
    calls = []

    async def ask(prompt, *, system="", task="", max_tokens=None):
        calls.append(task)
        return reply(prompt) if callable(reply) else reply

    async def persona():
        return "她叫小满，习惯晚睡，说话很短。"

    engine = L.Engine(store_read=store.read, store_write=store.write,
                      config=cfg or FakeConfig(), ask_model=ask, persona_text=persona)
    return engine, store, calls


def run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def good_plan_json():
    return json.dumps({
        "summary": "一整天都慢，但下午亮了一下",
        "segments": [
            {"window": "00:00-07:30", "doing": "睡着，夜里翻了几次身", "feel": "沉"},
            {"window": "07:30-09:00", "doing": "醒了，坐着把水烧上", "feel": "有点钝"},
            {"window": "09:00-12:00", "doing": "整理柜子，翻出一叠旧纸", "feel": "专注"},
            {"window": "12:00-14:00", "doing": "吃了点东西，靠着椅背放空", "feel": "松"},
            {"window": "14:00-18:00", "doing": "把窗户擦了，手有点凉", "feel": "平平的"},
            {"window": "18:00-21:00", "doing": "听了会儿歌，什么也没做", "feel": "松下来"},
            {"window": "21:00-24:00", "doing": "躺下了，脑子里还在转", "feel": "安宁"},
        ],
    }, ensure_ascii=False)


# ---------------- 用例 ----------------

class TestBasics(unittest.TestCase):
    def test_squeeze_makes_single_line(self):
        self.assertEqual(L._squeeze("  a\n b\tc  "), "a b c")

    def test_midnight_end_is_valid(self):
        """回归：24:00 曾被判为非法 → 模型写的最后一段被整段丢掉。"""
        self.assertEqual(L._minute("24:00"), 24 * 60)
        self.assertIsNone(L._minute("24:30"))
        self.assertEqual(L._hhmm(24 * 60), "24:00")

    def test_minute_roundtrip(self):
        self.assertEqual(L._minute("09:05"), 545)
        self.assertEqual(L._hhmm(545), "09:05")
        self.assertIsNone(L._minute("24:00".replace("24", "25")))
        self.assertIsNone(L._minute("9"))

    def test_day_label_has_date_and_time(self):
        text = L.day_label(datetime(2026, 6, 15, 14, 30))
        self.assertIn("2026-06-15", text)
        self.assertIn("14:30", text)
        self.assertTrue(any(k in text for k in ("工作日", "周末", "假日", "节假日")))


class TestSegment(unittest.TestCase):
    def test_window_formatting(self):
        seg = L.Segment(9 * 60, 11 * 60 + 30, "整理东西")
        self.assertEqual(seg.window, "09:00-11:30")

    def test_covers_normal(self):
        seg = L.Segment(9 * 60, 11 * 60, "做事")
        self.assertTrue(seg.covers(9 * 60))
        self.assertTrue(seg.covers(10 * 60 + 59))
        self.assertFalse(seg.covers(11 * 60))

    def test_covers_cross_midnight(self):
        seg = L.Segment(23 * 60, 24 * 60 + 90, "熬夜")
        self.assertTrue(seg.covers(23 * 60 + 30))
        self.assertTrue(seg.covers(30))          # 次日 00:30
        self.assertFalse(seg.covers(12 * 60))


class TestValidation(unittest.TestCase):
    def test_template_plan_is_valid(self):
        plan = L.template_plan()
        ok, why = L.validate_plan(plan.segments)
        self.assertTrue(ok, why)
        self.assertEqual(plan.source, "template")
        self.assertGreaterEqual(len(plan.segments), 3)

    def test_reject_too_few(self):
        ok, why = L.validate_plan([L.Segment(0, 1440, "睡觉")])
        self.assertFalse(ok)
        self.assertIn("段数", why)

    def test_reject_big_gap(self):
        segs = [L.Segment(0, 300, "睡"), L.Segment(300, 400, "起来"), L.Segment(900, 1440, "待着")]
        ok, why = L.validate_plan(segs)
        self.assertFalse(ok)
        self.assertIn("空", why)

    def test_reject_short_coverage(self):
        segs = [L.Segment(i * 60, i * 60 + 60, "事") for i in range(3)]
        ok, why = L.validate_plan(segs)
        self.assertFalse(ok)
        self.assertIn("覆盖", why)

    def test_reject_empty_doing(self):
        segs = [L.Segment(0, 600, "睡"), L.Segment(600, 900, ""), L.Segment(900, 1440, "待着")]
        ok, why = L.validate_plan(segs)
        self.assertFalse(ok)
        self.assertIn("空白", why)


class TestCoverageMath(unittest.TestCase):
    """回归：覆盖率曾按「下一段起点 - 上一段起点」累加，把空档算成已覆盖。"""

    def test_gaps_do_not_count_as_coverage(self):
        segs = [L.Segment(0, 60, "a"), L.Segment(300, 360, "b"), L.Segment(600, 660, "c")]
        ok, why = L.validate_plan(segs)
        self.assertFalse(ok)
        self.assertIn("只覆盖 180 分钟", why)

    def test_small_gap_is_merged(self):
        segs = [L.Segment(0, 600, "睡"), L.Segment(610, 900, "起来"),
                L.Segment(900, 1200, "做事"), L.Segment(1200, 1440, "收尾")]
        fixed = L.repair_plan(segs)
        self.assertEqual(len(fixed), 3, "10 分钟的小缝应该被接上")
        self.assertEqual(fixed[0].end, 900, "接缝后第一段应延伸到 09:00... 也就是 900 分钟")
        self.assertTrue(all(a.end == b.start for a, b in zip(fixed, fixed[1:])), "接完不该留缝")
        ok, why = L.validate_plan(fixed)
        self.assertTrue(ok, why)

    def test_adjacent_segments_are_not_merged(self):
        """回归：gap == 0 的相邻段曾被一并合并，导致整份日程塌成一段。"""
        segs = [L.Segment(0, 480, "睡"), L.Segment(480, 900, "起来"), L.Segment(900, 1440, "待着")]
        self.assertEqual(len(L.repair_plan(segs)), 3)


class TestRepairNightGap(unittest.TestCase):
    """模型从早上开排（漏掉夜里）时，补一段睡觉，而不是把整份日程作废。"""

    def test_leading_gap_is_filled_with_sleep(self):
        segs = [L.Segment(8 * 60 + 10, 12 * 60, "起床做事", "平平的"),
                L.Segment(12 * 60, 18 * 60, "继续", "松"),
                L.Segment(18 * 60, 24 * 60, "收尾", "安宁")]
        ok, why = L.validate_plan(segs)
        self.assertFalse(ok, "原样应当不合格（开头缺 490 分钟）")
        fixed = L.repair_plan(segs)
        ok2, why2 = L.validate_plan(fixed)
        self.assertTrue(ok2, why2)
        self.assertEqual(fixed[0].start, 0, "第一段应当从 00:00 开始")
        self.assertIn("睡", fixed[0].doing)


class TestRepair(unittest.TestCase):
    def test_drops_bad_and_merges_adjacent(self):
        segs = [L.Segment(0, 300, "睡"), L.Segment(305, 600, "醒"),
                L.Segment(600, 600, "坏的"), L.Segment(700, 1440, "待着")]
        fixed = L.repair_plan(segs)
        self.assertGreaterEqual(len(fixed), 2)
        self.assertTrue(all(s.doing for s in fixed))
        self.assertTrue(all(s.end > s.start for s in fixed))

    def test_hopeless_returns_empty(self):
        self.assertEqual(L.repair_plan([L.Segment(0, 0, "x")]), [])

    def test_repair_then_validate(self):
        segs = [L.Segment(0, 400, "睡"), L.Segment(400, 800, "起来"),
                L.Segment(800, 1200, "做点事"), L.Segment(1210, 1440, "收尾")]
        fixed = L.repair_plan(segs)
        ok, _ = L.validate_plan(fixed)
        self.assertTrue(ok)


class TestCard(unittest.TestCase):
    """抽词：同一天抽到同一组词，地点/事件有冷却，组合数足够大。"""

    EMPTY = {"places": [], "events": []}

    def test_deterministic_for_same_date(self):
        a, _ = L.Engine(store_read=FakeStore().read, store_write=FakeStore().write,
                        config=FakeConfig()).roll_card("2026-06-15", dict(self.EMPTY))
        b, _ = L.Engine(store_read=FakeStore().read, store_write=FakeStore().write,
                        config=FakeConfig()).roll_card("2026-06-15", dict(self.EMPTY))
        self.assertEqual(a.place, b.place)
        self.assertEqual(a.verbs, b.verbs)
        self.assertEqual(a.events, b.events)

    def test_seed_has_all_facets(self):
        seed, _ = L.Engine(store_read=FakeStore().read, store_write=FakeStore().write,
                           config=FakeConfig()).roll_card("2026-06-15", dict(self.EMPTY))
        self.assertTrue(seed.place)
        self.assertTrue(seed.prop)
        self.assertEqual(len(seed.verbs), 2)
        self.assertEqual(len(seed.events), L.INTERLUDE_PER_DAY)
        self.assertEqual(len(seed.feels), 2)

    def test_place_cooldown_respected(self):
        engine, _, _ = make_engine()
        hist = dict(self.EMPTY)
        seen = []
        for day in range(1, L.SCENE_COOLDOWN + 1):
            seed, hist = engine.roll_card(f"2026-06-{day:02d}", hist)
            seen.append(seed.place)
        self.assertEqual(len(set(seen)), len(seen), "冷却期内不该重复抽到同一个地点")

    def test_history_is_capped(self):
        engine, _, _ = make_engine()
        hist = dict(self.EMPTY)
        for day in range(1, 21):
            _, hist = engine.roll_card(f"2026-06-{day:02d}", hist)
        self.assertLessEqual(len(hist["places"]), L.SCENE_COOLDOWN * 2)


class TestDoings(unittest.TestCase):
    def test_recent_doings_flattens_last_days(self):
        recent = [{"date": f"2026-06-{d:02d}", "doings": [f"事{d}a", f"事{d}b"]} for d in (10, 11, 12, 13)]
        out = L.recent_doings(recent, days=2)
        self.assertEqual(out, ["事12a", "事12b", "事13a", "事13b"])

    def test_avoid_hint_empty(self):
        self.assertEqual(L.avoid_hint([]), "")

    def test_avoid_hint_caps_items(self):
        hint = L.avoid_hint([f"第{i}件事" for i in range(20)], limit=3)
        self.assertEqual(hint.count("第"), 3)


class TestEnergy(unittest.TestCase):
    def test_curve_shape(self):
        self.assertLess(L.energy_target(3), L.energy_target(11))
        self.assertLess(L.energy_target(23), L.energy_target(16))
        for hour in range(24):
            self.assertTrue(0 <= L.energy_target(hour) <= 100)


class TestSerialization(unittest.TestCase):
    def test_day_state_roundtrip(self):
        state = L.DayState(date="2026-06-15", plan=L.template_plan(), vitals=L.Vitals(66, "发闷"),
                           dream=L.Dream("梦见走不完的楼梯", "恍惚", "醒来有点晕", "2026-06-15"),
                           seed=L.DaySeed("2026-06-15", "安静的屋子", "书", ["待着"], ["翻出旧东西"], ["松快"]),
                           recent=[{"date": "2026-06-15", "doings": ["a"]}])
        back = L.DayState.from_json(state.to_json())
        self.assertEqual(back.date, state.date)
        self.assertEqual(back.plan.source, "template")
        self.assertEqual(back.vitals.energy, 66)
        self.assertEqual(back.dream.mood, "恍惚")
        self.assertEqual(back.seed.place, "安静的屋子")
        self.assertEqual(len(back.recent), 1)

    def test_bad_json_degrades(self):
        self.assertEqual(L.DayState.from_json("nonsense").date, "")
        self.assertIsNone(L.DayPlan.from_json({"segments": "x"}))
        self.assertIsNone(L.Vitals.from_json(None))


class TestVitals(unittest.TestCase):
    def test_seed_is_deterministic_and_bounded(self):
        engine, _, _ = make_engine()
        a = engine.seed_vitals(L.DayState(), "2026-06-15", hour=11)
        b = engine.seed_vitals(L.DayState(), "2026-06-15", hour=11)
        self.assertEqual(a.energy, b.energy)
        self.assertTrue(15 <= a.energy <= 100)
        self.assertTrue(a.mood)
        for ache in a.aches:
            self.assertIn("label", ache)
            self.assertGreater(ache["until"], a.stamped)

    def test_tick_clears_expired_aches(self):
        engine, _, _ = make_engine()
        state = L.DayState(vitals=L.Vitals(70, "平静", [{"label": "手有点凉", "until": 1000}], 500))
        changed = engine.tick(state, now=2000)
        self.assertTrue(changed)
        self.assertEqual(state.vitals.aches, [])

    def test_tick_respects_min_interval(self):
        engine, _, _ = make_engine()
        state = L.DayState(vitals=L.Vitals(70, "平静", [], 1000))
        self.assertFalse(engine.tick(state, now=1000 + L.ENERGY_TICK_SECONDS - 1))
        self.assertEqual(state.vitals.energy, 70)

    def test_tick_moves_energy_toward_target(self):
        engine, _, _ = make_engine()
        noon = int(datetime(2026, 6, 15, 12, 0, 0).timestamp())
        state = L.DayState(vitals=L.Vitals(10, "平静", [], noon - L.ENERGY_TICK_SECONDS))
        engine.tick(state, now=noon)
        self.assertGreater(state.vitals.energy, 10)
        self.assertLessEqual(state.vitals.energy, L.energy_target(12))


class TestPlanGeneration(unittest.TestCase):
    def test_model_plan_is_used(self):
        engine, store, calls = make_engine(reply=good_plan_json())
        state = run(engine.rebuild_plan(L.DayState()))
        self.assertEqual(state.plan.source, "model")
        self.assertGreaterEqual(len(state.plan.segments), 6)
        self.assertTrue(any(t.startswith("life_plan") for t in calls))
        self.assertIn(L.STATE_KEY, store.data)

    def test_broken_json_falls_back_to_template(self):
        engine, _, _ = make_engine(reply="我看不懂你让我干嘛")
        state = run(engine.rebuild_plan(L.DayState()))
        self.assertEqual(state.plan.source, "template")

    def test_incomplete_plan_is_repaired_or_rejected(self):
        partial = json.dumps({"summary": "还行", "segments": [
            {"window": "00:00-08:00", "doing": "睡", "feel": "沉"},
            {"window": "08:00-12:00", "doing": "起来做事", "feel": "平平的"},
            {"window": "12:00-24:00", "doing": "继续", "feel": "松"},
        ]}, ensure_ascii=False)
        engine, _, _ = make_engine(reply=partial)
        state = run(engine.rebuild_plan(L.DayState()))
        self.assertIn(state.plan.source, ("model", "repaired", "template"))
        ok, why = L.validate_plan(state.plan.segments)
        self.assertTrue(ok, why)

    def test_no_model_still_produces_plan(self):
        engine, _, _ = make_engine(reply=None)
        state = run(engine.rebuild_plan(L.DayState()))
        self.assertEqual(state.plan.source, "template")
        self.assertTrue(state.seed is not None)

    def test_recent_keeps_at_most_seven(self):
        engine, _, _ = make_engine(reply=good_plan_json())
        state = L.DayState(recent=[{"date": f"2026-05-{d:02d}", "doings": ["x"]} for d in range(1, 11)])
        state = run(engine.rebuild_plan(state))
        self.assertLessEqual(len(state.recent), 7)


class TestDream(unittest.TestCase):
    def test_dream_parsed(self):
        payload = json.dumps({"text": "梦见楼下有扇门一直开着", "mood": "恍惚", "afterglow": "醒来心里发紧"},
                             ensure_ascii=False)
        engine, _, calls = make_engine(reply=payload)
        state = run(engine.rebuild_dream(L.DayState(plan=L.template_plan())))
        self.assertTrue(state.dream.text)
        self.assertEqual(state.dream.mood, "恍惚")
        self.assertIn("life_dream", calls)

    def test_dream_failure_is_quiet(self):
        engine, _, _ = make_engine(reply="")
        state = run(engine.rebuild_dream(L.DayState(plan=L.template_plan())))
        self.assertEqual(state.dream.text, "")


class TestEnsureToday(unittest.TestCase):
    def test_rolls_once_per_day(self):
        engine, store, calls = make_engine(reply=good_plan_json())
        first = run(engine.ensure_today())
        writes_after_first = store.writes
        second = run(engine.ensure_today())
        self.assertEqual(first.date, second.date)
        self.assertEqual(store.writes, writes_after_first, "同一天不该重复生成")
        plan_calls = [c for c in calls if c.startswith("life_plan")]
        self.assertEqual(len(plan_calls), 1)

    def test_rolls_again_when_date_is_stale(self):
        store = FakeStore()
        engine, _, _ = make_engine(reply=good_plan_json(), store=store)
        stale = L.DayState(date="2020-01-01", plan=L.template_plan(),
                           vitals=L.Vitals(50, "蔫"), dream=L.Dream("旧梦", "沉", "沉", "2020-01-01"))
        store.data[L.STATE_KEY] = json.dumps(stale.to_json(), ensure_ascii=False)
        state = run(engine.ensure_today())
        self.assertNotEqual(state.date, "2020-01-01")
        self.assertIsNotNone(state.plan)

    def test_disabled_engine_still_rolls_but_render_is_callable(self):
        engine, _, _ = make_engine(reply=good_plan_json(), cfg=FakeConfig(LIFE_ENABLED=False))
        self.assertFalse(engine.on)
        state = run(engine.ensure_today())
        self.assertIsNotNone(state.plan)


class TestRender(unittest.TestCase):
    def _state(self, source="model"):
        plan = L.template_plan() if source == "template" else L.DayPlan(
            "一整天都慢，但下午亮了一下",
            [L.Segment(i * 120, (i + 1) * 120, f"第{i}段在做的事", "平平的") for i in range(12)],
            source="model")
        return L.DayState(date=L._today(), plan=plan, vitals=L.Vitals(64, "发闷", [{"label": "肩膀有点酸",
                          "until": L._now() + 3600}]), dream=L.Dream("梦", "恍惚", "醒来有点晕", L._today()))

    def test_renders_key_lines(self):
        engine, _, _ = make_engine()
        text = engine.render(self._state(), chat_key="onebot_v11-group_1")
        self.assertIn("【你今天的生活】", text)
        self.assertIn("对话场景：群聊", text)
        self.assertIn("能量 64/100", text)
        self.assertIn("身上：肩膀有点酸", text)
        self.assertIn("别写进笔记", text)

    def test_template_day_is_vague(self):
        engine, _, _ = make_engine()
        text = engine.render(self._state(source="template"))
        self.assertIn("没什么安排", text)
        self.assertNotIn("此刻（", text)

    def test_cap_is_respected(self):
        engine, _, _ = make_engine(cfg=FakeConfig(LIFE_BLOCK_CAP=120))
        text = engine.render(self._state())
        body = text.split("\n")[0]
        self.assertLessEqual(len(body), 120)

    def test_no_plan_renders_empty(self):
        engine, _, _ = make_engine()
        self.assertEqual(engine.render(L.DayState()), "")

    def test_private_scene_label(self):
        engine, _, _ = make_engine()
        self.assertIn("对话场景：私聊", engine.render(self._state(), chat_key="onebot_v11-private_1"))


class TestBlockFor(unittest.TestCase):
    """B2：注入入口。三条硬约束 —— 关掉就空、跨天不阻塞、缓存期内不重复读库。"""

    def _fresh(self, **cfg):
        store = FakeStore()
        engine, _, _ = make_engine(reply=good_plan_json(), cfg=FakeConfig(**cfg), store=store)
        run(engine.ensure_today())          # 先把今天滚出来
        store.writes = 0
        store.reads = 0
        return engine, store

    def test_disabled_returns_empty(self):
        engine, _ = self._fresh(LIFE_ENABLED=False)
        self.assertEqual(run(engine.block_for("onebot_v11-group_1")), "")

    def test_inject_off_returns_empty(self):
        engine, _ = self._fresh(LIFE_INJECT_ENABLED=False)
        self.assertEqual(run(engine.block_for("onebot_v11-group_1")), "")

    def test_returns_block_when_on(self):
        engine, _ = self._fresh()
        text = run(engine.block_for("onebot_v11-group_1"))
        self.assertIn("【你今天的生活】", text)
        self.assertIn("能量", text)
        self.assertIn("别写进笔记", text)

    def test_cache_avoids_repeat_store_reads(self):
        engine, store = self._fresh()
        L._BLOCK_CACHE.clear()
        first = run(engine.block_for("onebot_v11-group_1"))
        reads_after_first = store.reads
        second = run(engine.block_for("onebot_v11-group_1"))
        self.assertEqual(first, second)
        self.assertEqual(store.reads, reads_after_first, "缓存期内不该再读 store")

    def test_cache_expires(self):
        engine, store = self._fresh()
        L._BLOCK_CACHE.clear()
        now = L._now()
        run(engine.block_for("onebot_v11-group_1", now=now))
        reads_after_first = store.reads
        run(engine.block_for("onebot_v11-group_1", now=now + L.BLOCK_CACHE_TTL + 1))
        self.assertGreater(store.reads, reads_after_first, "过了 TTL 应该重新读")

    def test_stale_day_does_not_block(self):
        """跨天那一轮必须立刻返回空，把生成丢到后台 —— 不能拖慢回复。"""
        store = FakeStore()
        engine, _, _ = make_engine(reply=good_plan_json(), store=store)
        stale = L.DayState(date="2020-01-01", plan=L.template_plan())
        store.data[L.STATE_KEY] = json.dumps(stale.to_json(), ensure_ascii=False)
        L._BLOCK_CACHE.clear()
        L._ROLLING.clear()
        out = run(engine.block_for("onebot_v11-group_1"))
        self.assertEqual(out, "", "跨天当轮不注入")
        self.assertTrue(L._ROLLING or store.writes > 0, "应当已经在后台滚动")

    def test_never_raises_on_broken_store(self):
        class Boom:
            async def read(self, key):
                raise RuntimeError("db down")

            async def write(self, key, value):
                raise RuntimeError("db down")

        engine = L.Engine(store_read=Boom().read, store_write=Boom().write, config=FakeConfig())
        L._BLOCK_CACHE.clear()
        self.assertEqual(run(engine.block_for("onebot_v11-group_1")), "")


class TestMoodBias(unittest.TestCase):
    """B3：当下状态怎么读这句话。只调幅度与耐久，不改符号。"""

    def _state(self, *, energy=70, summary="", feel="", doing="", now_seg=True):
        segs = [L.Segment(0, 24 * 60, doing or "待着", feel)]
        state = L.DayState(date=L._today(), plan=L.DayPlan(summary or "平常的一天", segs),
                           vitals=L.Vitals(energy, "平静"))
        return state

    def test_neutral_day_keeps_factor(self):
        bias = L.mood_bias(self._state(), score=40, mood=-20, now=datetime(2026, 6, 15, 15, 0))
        self.assertEqual(bias.factor, 1.0)
        self.assertEqual(bias.half_life_mult, 1.0)

    def test_late_night_softens_both_directions(self):
        neg = L.mood_bias(self._state(), score=40, mood=-20, now=datetime(2026, 6, 15, 2, 0))
        pos = L.mood_bias(self._state(), score=40, mood=20, now=datetime(2026, 6, 15, 2, 0))
        self.assertAlmostEqual(neg.factor, 0.8)
        self.assertAlmostEqual(pos.factor, 0.6)
        self.assertIn("深夜", neg.notes)

    def test_low_energy_softens(self):
        bias = L.mood_bias(self._state(energy=12), score=40, mood=-20,
                           now=datetime(2026, 6, 15, 15, 0))
        self.assertAlmostEqual(bias.factor, 0.8)
        self.assertTrue(any("体力" in n for n in bias.notes))

    def test_bad_day_amplifies_negative(self):
        bias = L.mood_bias(self._state(feel="发闷、有点烦"), score=40, mood=-20,
                           now=datetime(2026, 6, 15, 15, 0))
        self.assertAlmostEqual(bias.factor, 1.15)
        self.assertTrue(any("不顺" in n for n in bias.notes))

    def test_bad_day_does_not_amplify_positive(self):
        bias = L.mood_bias(self._state(feel="发闷、有点烦"), score=40, mood=20,
                           now=datetime(2026, 6, 15, 15, 0))
        self.assertAlmostEqual(bias.factor, 1.0)

    def test_close_relation_cushions_and_lingers(self):
        bias = L.mood_bias(self._state(), score=80, mood=-20, now=datetime(2026, 6, 15, 15, 0))
        self.assertAlmostEqual(bias.factor, 0.85)
        self.assertAlmostEqual(bias.half_life_mult, 1.5)

    def test_stranger_reads_harder_and_forgets_faster(self):
        bias = L.mood_bias(self._state(), score=5, mood=-20, now=datetime(2026, 6, 15, 15, 0))
        self.assertAlmostEqual(bias.factor, 1.15)
        self.assertAlmostEqual(bias.half_life_mult, 0.8)

    def test_factor_is_clamped(self):
        extreme = L.mood_bias(self._state(energy=5, feel="发闷"), score=5, mood=-60,
                              now=datetime(2026, 6, 15, 2, 0))
        self.assertLessEqual(extreme.factor, 1.35)
        self.assertGreaterEqual(extreme.factor, 0.5)

    def test_positive_mood_never_gets_amplified_by_tier(self):
        close = L.mood_bias(self._state(), score=80, mood=20, now=datetime(2026, 6, 15, 15, 0))
        self.assertEqual(close.factor, 1.0)
        self.assertEqual(close.half_life_mult, 1.0)


class TestHoldHint(unittest.TestCase):
    """B3：什么时候该收着说话。"""

    def _state(self, doing="待着", energy=70):
        return L.DayState(date=L._today(),
                          plan=L.DayPlan("平常", [L.Segment(0, 24 * 60, doing, "平静")]),
                          vitals=L.Vitals(energy, "平静"))

    def test_nothing_when_idle(self):
        self.assertEqual(L.hold_hint(self._state(), mood=0), "")

    def test_busy_activity_expects_short_reply(self):
        hint = L.hold_hint(self._state(doing="一直在做手上的事，忙得没抬头"), mood=0)
        self.assertIn("短一点", hint)

    def test_low_energy_expects_short_reply(self):
        self.assertIn("累", L.hold_hint(self._state(energy=10), mood=0))

    def test_bad_mood_expects_distance(self):
        self.assertIn("淡一点", L.hold_hint(self._state(), mood=-40))

    def test_urgent_words_are_recognised(self):
        self.assertTrue(L.is_urgent("救命，我出事了"))
        self.assertFalse(L.is_urgent("今天有点累"))


class TestTierVisibility(unittest.TestCase):
    """B3：关系不够深时只给模糊说法，够深才说具体在做什么。"""

    def _state(self):
        return L.DayState(date=L._today(),
                          plan=L.DayPlan("平常的一天",
                                         [L.Segment(0, 24 * 60, "在擦窗台，手有点凉", "平平的")]),
                          vitals=L.Vitals(70, "平静"))

    def test_stranger_gets_coarse_view(self):
        eng, _, _ = make_engine()
        text = eng.render(self._state(), score=5)
        self.assertIn("不太方便细说", text)
        self.assertNotIn("擦窗台", text)

    def test_friend_gets_details(self):
        eng, _, _ = make_engine()
        text = eng.render(self._state(), score=45)
        self.assertIn("擦窗台", text)

    def test_block_for_passes_score_through(self):
        store = FakeStore()
        eng, _, _ = make_engine(reply=good_plan_json(), store=store)
        run(eng.ensure_today())
        L._BLOCK_CACHE.clear()
        coarse = run(eng.block_for("ck1", score=5))
        L._BLOCK_CACHE.clear()
        fine = run(eng.block_for("ck1", score=60))
        self.assertNotEqual(coarse, fine)


class TestPromises(unittest.TestCase):
    """B4：约定识别。判不准就不提 —— 宁漏一次，也别天天念同一件事。"""

    TODAY = "2026-06-15"

    def test_terms_prefer_quoted_words(self):
        terms = L.promise_terms("答应过要「把书还给她」，别忘")
        self.assertIn("把书还给她", terms)

    def test_terms_drop_filler(self):
        terms = L.promise_terms("答应明天把照片发过去")
        self.assertTrue(terms)
        for t in terms:
            self.assertFalse(any(ch in t for ch in "的了呢吧啊我你"))

    def test_relative_day_makes_due_date(self):
        items = [{"title": "还书", "content": "答应明天把书还给她", "expire_at": 0}]
        got = L.extract_promises(items, self.TODAY)
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0].due, "2026-06-16")

    def test_expire_at_counts_when_already_expired(self):
        past = int(datetime(2026, 6, 10, 20, 0).timestamp())
        items = [{"title": "回消息", "content": "约好回消息", "expire_at": past}]
        got = L.extract_promises(items, self.TODAY)
        self.assertEqual(got[0].due, "2026-06-10")

    def test_future_expire_at_is_not_a_due_date(self):
        """回归：expire_at 是"笔记什么时候过期"，不是"约定该做的时候"。
        按它判会让一条挂 48 小时的笔记显示成后天到期，从此永不提醒。"""
        future = int(datetime(2026, 7, 1, 20, 0).timestamp())
        items = [{"title": "还书", "content": "答应把书还给她", "expire_at": future}]
        self.assertEqual(L.extract_promises(items, self.TODAY), [])

    def test_no_time_word_is_skipped(self):
        items = [{"title": "还书", "content": "答应过把书还给她", "expire_at": 0}]
        self.assertEqual(L.extract_promises(items, self.TODAY), [])

    def test_vague_time_is_skipped(self):
        items = [{"title": "见面", "content": "说好下周见面", "expire_at": 0}]
        self.assertEqual(L.extract_promises(items, self.TODAY), [])

    def test_non_promise_is_skipped(self):
        items = [{"title": "天气", "content": "今天下雨了", "expire_at": 0}]
        self.assertEqual(L.extract_promises(items, self.TODAY), [])

    def test_due_includes_overdue(self):
        past = int(datetime(2026, 6, 14, 20, 0).timestamp())
        items = [
            {"title": "a", "content": "答应把东西给她", "expire_at": past},
            {"title": "b", "content": "答应明天把东西给她", "expire_at": 0},
        ]
        promises = L.extract_promises(items, self.TODAY)
        due = L.due_promises(promises, self.TODAY)
        self.assertEqual([p.text[:1] for p in due], ["a"])

    def test_yesterday_counts_as_overdue(self):
        items = [{"title": "还书", "content": "答应昨天把书还给她", "expire_at": 0}]
        got = L.extract_promises(items, self.TODAY)
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0].due, "2026-06-14")

    def test_line_is_soft(self):
        p = L.Promise(due=self.TODAY, text="答应明天把书还给她", terms=["把书还给她"])
        line = L.promise_line(p)
        self.assertIn("你还记着", line)
        self.assertIn("别提就算了", line, "提醒不能变成催办")

    def test_hit_matches_terms(self):
        p = L.Promise(due=self.TODAY, text="答应明天把书还给她", terms=["把书还给她"], handle="还书")
        self.assertTrue(L.promise_hit("你不是说要把书还给她吗", p))
        self.assertFalse(L.promise_hit("今天天气不错", p))

    def test_hit_accepts_paraphrase(self):
        """实测对方很少原样重复原话，多半说「要还的那本书」。"""
        p = L.Promise(due=self.TODAY, text="答应昨天把那本借的书还给她", terms=["答应", "昨天"], handle="还书")
        self.assertTrue(L.promise_hit("上次说要还的那本书，你还记着吗", p))

    def test_hit_rejects_loose_overlap(self):
        p = L.Promise(due=self.TODAY, text="答应把围巾还给她", terms=["围巾"], handle="围巾还她")
        self.assertFalse(L.promise_hit("今天书店人好多", p))


class TestHorizon(unittest.TestCase):
    """多日日程：一次排 N 天，逐日校验，缺哪天补哪天。"""

    def _multi_reply(self, dates, *, bad_day=None):
        days = []
        for day in dates:
            segs = [{"window": "00:00-07:30", "doing": "睡着", "feel": "沉"},
                    {"window": "07:30-12:00", "doing": "醒着做事", "feel": "平平的"},
                    {"window": "12:00-18:00", "doing": "继续", "feel": "松"},
                    {"window": "18:00-24:00", "doing": "收尾", "feel": "安宁"}]
            if day == bad_day:
                segs = segs[:1]          # 只有一段 → 必然不合格
            days.append({"date": day, "summary": f"{day} 的一天", "segments": segs})
        return json.dumps({"days": days}, ensure_ascii=False)

    def test_horizon_three_fills_three_days(self):
        today = L._today()
        dates = [L._shift_date(today, i) for i in range(3)]
        store = FakeStore()
        engine, _, _ = make_engine(reply=self._multi_reply(dates),
                                   cfg=FakeConfig(LIFE_PLAN_HORIZON=3), store=store)
        state = run(engine.ensure_today())
        self.assertEqual(sorted(state.plans), sorted(dates))
        for day, plan in state.plans.items():
            ok, why = L.validate_plan(plan.segments)
            self.assertTrue(ok, f"{day}: {why}")
            self.assertEqual(plan.source, "model")

    def test_horizon_does_not_regenerate_same_day(self):
        today = L._today()
        dates = [L._shift_date(today, i) for i in range(3)]
        store = FakeStore()
        engine, _, calls = make_engine(reply=self._multi_reply(dates),
                                       cfg=FakeConfig(LIFE_PLAN_HORIZON=3), store=store)
        run(engine.ensure_today())
        writes = store.writes
        plan_calls = len([c for c in calls if c.startswith("life_plan")])
        run(engine.ensure_today())
        self.assertEqual(store.writes, writes, "同一天不该重复生成")
        self.assertEqual(len([c for c in calls if c.startswith("life_plan")]), plan_calls)

    def test_bad_day_falls_back_alone(self):
        today = L._today()
        dates = [L._shift_date(today, i) for i in range(3)]
        engine, _, _ = make_engine(reply=self._multi_reply(dates, bad_day=dates[1]),
                                   cfg=FakeConfig(LIFE_PLAN_HORIZON=3))
        state = run(engine.ensure_today())
        self.assertEqual(state.plans[dates[0]].source, "model")
        self.assertEqual(state.plans[dates[1]].source, "template", "坏的那天单独兜底")
        self.assertEqual(state.plans[dates[2]].source, "model")

    def test_single_day_format_still_works_with_horizon_one(self):
        engine, _, _ = make_engine(reply=good_plan_json(), cfg=FakeConfig(LIFE_PLAN_HORIZON=1))
        state = run(engine.ensure_today())
        self.assertEqual(list(state.plans), [L._today()])
        self.assertEqual(state.plans[L._today()].source, "model")

    def test_topup_only_generates_missing_day(self):
        today = L._today()
        tomorrow = L._shift_date(today, 1)
        store = FakeStore()
        engine, _, _ = make_engine(reply=self._multi_reply([tomorrow]),
                                   cfg=FakeConfig(LIFE_PLAN_HORIZON=2), store=store)
        seeded = L.DayState(date=today, plans={today: L.template_plan()},
                            vitals=L.Vitals(60, "平静"))
        store.data[L.STATE_KEY] = json.dumps(seeded.to_json(), ensure_ascii=False)
        state = run(engine.ensure_today())
        self.assertIn(today, state.plans)
        self.assertIn(tomorrow, state.plans)
        self.assertEqual(state.plans[tomorrow].source, "model")

    def test_old_days_are_pruned(self):
        today = L._today()
        old = L._shift_date(today, -5)
        store = FakeStore()
        engine, _, _ = make_engine(reply=good_plan_json(), store=store)
        seeded = L.DayState(date=today,
                            plans={old: L.template_plan(), today: L.template_plan()},
                            vitals=L.Vitals(60, "平静"))
        store.data[L.STATE_KEY] = json.dumps(seeded.to_json(), ensure_ascii=False)
        state = run(engine.ensure_today())
        self.assertNotIn(old, state.plans, "更早的骨架应当被裁掉")

    def test_legacy_single_plan_is_migrated(self):
        today = L._today()
        legacy = {"date": today, "plan": L.template_plan().to_json(), "recent": []}
        state = L.DayState.from_json(legacy)
        self.assertIn(today, state.plans)
        self.assertEqual(state.plan.source, "template")

    def test_token_budget_scales_with_days(self):
        engine, _, _ = make_engine()
        self.assertLess(engine._plan_tokens(1), engine._plan_tokens(3))
        self.assertLessEqual(engine._plan_tokens(7), 4096)

    def test_render_uses_today_not_last_day(self):
        today = L._today()
        dates = [L._shift_date(today, i) for i in range(3)]
        engine, _, _ = make_engine(reply=self._multi_reply(dates),
                                   cfg=FakeConfig(LIFE_PLAN_HORIZON=3))
        state = run(engine.ensure_today())
        state.plans[dates[0]].summary = "今天的底色"
        text = engine.render(state, score=60)
        self.assertIn("今天的底色", text)


class TestSalvage(unittest.TestCase):
    """被 max_tokens 截断时，完整的那几天必须还能用。"""

    def test_extract_complete_days_only(self):
        raw = ('{"days": [{"date": "2026-11-01", "summary": "a", "segments": [{"window": "00:00-24:00", '
               '"doing": "x", "feel": "y"}]}, {"date": "2026-11-02", "summary": "b", "segments": [{"window": '
               '"00:00-24:00", "doing": "x", "feel": "y"}]}, {"date": "2026-11-03", "summary": "c", "segr')
        got = L.extract_day_objects(raw)
        self.assertEqual([d["date"] for d in got], ["2026-11-01", "2026-11-02"])

    def test_truncated_third_day_falls_back_alone(self):
        today = L._today()
        dates = [L._shift_date(today, i) for i in range(3)]
        good = []
        for day in dates[:2]:
            good.append('{"date": "%s", "summary": "s", "segments": ['
                        '{"window": "00:00-07:30", "doing": "睡着", "feel": "沉"},'
                        '{"window": "07:30-12:00", "doing": "醒着", "feel": "平平的"},'
                        '{"window": "12:00-18:00", "doing": "做事", "feel": "松"},'
                        '{"window": "18:00-24:00", "doing": "收尾", "feel": "安宁"}]}' % day)
        raw = '{"days": [' + ",".join(good) + ', {"date": "%s", "summary": "s", "segr' % dates[2]
        engine, _, _ = make_engine(reply=raw, cfg=FakeConfig(LIFE_PLAN_HORIZON=3))
        state = run(engine.ensure_today())
        self.assertEqual(state.plans[dates[0]].source, "model")
        self.assertEqual(state.plans[dates[1]].source, "model")
        self.assertEqual(state.plans[dates[2]].source, "template")


class TestStyleNote(unittest.TestCase):
    """输出规范：格式约束单独成段，可逐条开关。"""

    def _cfg(self, **over):
        import importlib
        P = importlib.import_module("nekro_persona.plugin")
        cfg = P.config
        saved = {}
        for k, v in over.items():
            saved[k] = getattr(cfg, k, None)
            setattr(cfg, k, v)
        return P, saved

    def test_default_note_has_action_ban(self):
        P, saved = self._cfg(STYLE_ENABLED=True, STYLE_NO_ACTION=True,
                             STYLE_MULTI_MSG=True, STYLE_NO_META=True)
        try:
            note = P._style_note()
        finally:
            for k, v in saved.items():
                setattr(P.config, k, v)
        self.assertIn("说话方式", note)
        self.assertIn("不写动作", note)
        self.assertIn("多条独立短消息", note)
        self.assertIn("不报分数", note)

    def test_action_ban_can_be_turned_off(self):
        P, saved = self._cfg(STYLE_ENABLED=True, STYLE_NO_ACTION=False,
                             STYLE_MULTI_MSG=True, STYLE_NO_META=True)
        try:
            note = P._style_note()
        finally:
            for k, v in saved.items():
                setattr(P.config, k, v)
        self.assertNotIn("不写动作", note)
        self.assertIn("多条独立短消息", note)

    def test_disabled_returns_empty(self):
        P, saved = self._cfg(STYLE_ENABLED=False)
        try:
            self.assertEqual(P._style_note(), "")
        finally:
            for k, v in saved.items():
                setattr(P.config, k, v)


class TestWorldFit(unittest.TestCase):
    """种子式内容：抽词要抽在世界观内，生成后还要过一遍禁词校验。"""

    def test_seed_block_lists_every_drawn_word(self):
        seed = L.DaySeed("2026-06-15", "星炬学院的教室", "终端", ["听课", "抄录数据"],
                         ["终端收到一条旧消息"], ["松快"])
        block = L.seed_block(seed, L.BUILTIN_WORLD)
        for token in ("星炬学院的教室", "终端", "听课", "抄录数据", "终端收到一条旧消息"):
            self.assertIn(token, block)
        self.assertIn("不要引入这个世界没有的东西", block)

    def test_world_hits_finds_forbidden_words(self):
        hits = L.world_hits("下班路上顺便买了外卖", L.BUILTIN_WORLD)
        self.assertIn("下班", hits)
        self.assertIn("外卖", hits)
        self.assertEqual(L.world_hits("在学院抄录数据", L.BUILTIN_WORLD), [])

    def test_scrub_plan_rewrites_only_offending_segment(self):
        plan = L.DayPlan("下班回家的一天", [
            L.Segment(0, 480, "睡着", "沉"),
            L.Segment(480, 900, "下班路上顺路买了外卖", "累"),
            L.Segment(900, 1440, "在学院抄录数据", "松快"),
        ])
        fixed = L.scrub_plan(plan, L.BUILTIN_WORLD)
        self.assertEqual(fixed.source, "repaired")
        self.assertEqual(len(fixed.segments), 3, "不能整天作废")
        self.assertFalse(L.world_hits(fixed.summary, L.BUILTIN_WORLD))
        self.assertFalse(L.world_hits(
            " ".join(f"{s.doing} {s.feel}" for s in fixed.segments), L.BUILTIN_WORLD))
        self.assertEqual(fixed.segments[2].doing, "在学院抄录数据", "没问题的段不该被动")

    def test_offending_generation_triggers_retry_then_scrub(self):
        today = L._today()
        bad = json.dumps({"summary": "下班回家", "segments": [
            {"window": "00:00-07:30", "doing": "睡着", "feel": "沉"},
            {"window": "07:30-12:00", "doing": "下班路上买外卖", "feel": "累"},
            {"window": "12:00-18:00", "doing": "在学院听课", "feel": "平平的"},
            {"window": "18:00-24:00", "doing": "翻旧磁带", "feel": "安宁"}]}, ensure_ascii=False)
        engine, _, calls = make_engine(reply=bad)
        state = run(engine.ensure_today())
        plan = state.plans[today]
        self.assertFalse(L.world_hits(engine._plan_text(plan), L.BUILTIN_WORLD),
                         "落库前必须已经没有越界词")
        self.assertTrue(any("retry" in c for c in calls), "应当点名重抽过一次")


class TestAttach(unittest.TestCase):
    def test_attach_and_is_on(self):
        engine, _, _ = make_engine(cfg=FakeConfig(LIFE_ENABLED=True))
        L.attach(engine)
        try:
            self.assertIs(L.engine(), engine)
            self.assertTrue(L.is_on())
        finally:
            L.attach(None)
        self.assertFalse(L.is_on())


if __name__ == "__main__":
    unittest.main()
