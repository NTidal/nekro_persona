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


class FakeStore:
    """最简 KV：读写都经过它，用来观察 Engine 到底存了什么。"""

    def __init__(self):
        self.data = {}
        self.writes = 0

    async def read(self, key):
        raw = self.data.get(key)
        return json.loads(raw) if raw else None

    async def write(self, key, value):
        self.writes += 1
        self.data[key] = json.dumps(value, ensure_ascii=False)


def make_engine(*, reply=None, cfg=None, store=None):
    store = store or FakeStore()
    calls = []

    async def ask(prompt, *, system="", task=""):
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
    def test_deterministic_for_same_date(self):
        a, _ = L.Engine(store_read=FakeStore().read, store_write=FakeStore().write,
                        config=FakeConfig()).roll_card("2026-06-15", {"scenes": [], "interludes": []})
        b, _ = L.Engine(store_read=FakeStore().read, store_write=FakeStore().write,
                        config=FakeConfig()).roll_card("2026-06-15", {"scenes": [], "interludes": []})
        self.assertEqual(a.scene_id, b.scene_id)
        self.assertEqual([i.id for i in a.interludes], [i.id for i in b.interludes])

    def test_interludes_are_two(self):
        card, _ = L.Engine(store_read=FakeStore().read, store_write=FakeStore().write,
                           config=FakeConfig()).roll_card("2026-06-15", {"scenes": [], "interludes": []})
        self.assertEqual(len(card.interludes), L.INTERLUDE_PER_DAY)

    def test_scene_cooldown_respected(self):
        engine, _, _ = make_engine()
        hist = {"scenes": [], "interludes": []}
        seen = []
        for day in range(1, L.SCENE_COOLDOWN + 1):
            card, hist = engine.roll_card(f"2026-06-{day:02d}", hist)
            seen.append(card.scene_id)
        self.assertEqual(len(set(seen)), len(seen), "冷却期内不该重复抽到同一个场景")

    def test_history_is_capped(self):
        engine, _, _ = make_engine()
        hist = {"scenes": [], "interludes": []}
        for day in range(1, 21):
            _, hist = engine.roll_card(f"2026-06-{day:02d}", hist)
        self.assertLessEqual(len(hist["scenes"]), L.SCENE_COOLDOWN * 2)


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
                           card=L.Card("2026-06-15", "quiet_room", "安静的屋子", "待着", []),
                           recent=[{"date": "2026-06-15", "doings": ["a"]}])
        back = L.DayState.from_json(state.to_json())
        self.assertEqual(back.date, state.date)
        self.assertEqual(back.plan.source, "template")
        self.assertEqual(back.vitals.energy, 66)
        self.assertEqual(back.dream.mood, "恍惚")
        self.assertEqual(back.card.scene_id, "quiet_room")
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
        self.assertTrue(state.card is not None)

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
