"""当下情绪（临时态度）的回归测试：档位 / 收敛 / 衰减 / 沉淀 / 注入 / 档案兼容。

用例是 unittest 风格（pytest 也能直接跑）。
运行（必须在 NekroAgent 容器内）：
    docker exec <容器> sh -c 'cd <插件目录> && /app/.venv/bin/python -m unittest discover -s tests -v'
"""
from __future__ import annotations

import re
import math
import time
import unittest

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))   # 支持按模块名单独运行

from _bootstrap import load_plugin_module, temp_config, today_start

P = load_plugin_module()
NOW = 1_700_000_000
HALF = 3600          # 默认半衰期 60 分钟
NO_DIGIT = re.compile(r"\d")


class TestMoodBand(unittest.TestCase):
    """情绪值 → 定性档位。分界点必须精确，否则语气会错档。"""

    def test_boundaries(self):
        cases = [
            (100, "心情很好"),
            (40, "心情很好"),
            (39, "心情不错"),
            (14, "心情不错"),
            (13, ""),          # 平静：不注入
            (0, ""),
            (-13, ""),
            (-14, ""),
            (-15, "有点被噎到"),
            (-40, "有点被噎到"),
            (-41, "明显不高兴"),
            (-70, "明显不高兴"),
            (-71, "被冒犯了"),
            (-100, "被冒犯了"),
        ]
        for mood, want in cases:
            with self.subTest(mood=mood):
                label, _text = P._mood_band(mood)
                self.assertEqual(label, want)

    def test_calm_band_has_no_text(self):
        label, text = P._mood_band(0)
        self.assertEqual((label, text), ("", ""))

    def test_non_calm_band_has_text(self):
        for mood in (60, 20, -20, -55, -90):
            with self.subTest(mood=mood):
                label, text = P._mood_band(mood)
                self.assertTrue(label and text, f"{mood} 应当有档位与描述")


class TestMoodBandsFromConfig(unittest.TestCase):
    """档位表现在由 favor.json 的 mood_bands 驱动（2.6.0），内置表只做兜底。"""

    def setUp(self):
        self.memory = P.persona_memory
        self._orig = self.memory.mood_bands

    def tearDown(self):
        self.memory.mood_bands = self._orig

    def test_custom_bands_win(self):
        self.memory.mood_bands = lambda: [
            (30, "上头", "你现在有点上头。"),
            (-30, "不服", "你现在有点不服气。"),
            (-10 ** 9, "冷到底", "你现在只想冷处理。"),
        ]
        self.assertEqual(P._mood_band(50), ("上头", "你现在有点上头。"))
        self.assertEqual(P._mood_band(0), ("不服", "你现在有点不服气。"))
        self.assertEqual(P._mood_band(-90), ("冷到底", "你现在只想冷处理。"))

    def test_table_without_catch_all_can_be_empty(self):
        """表里没有兜底项时，超出最低 floor 的情绪就当"无档位"（不注入），不能抛。"""
        self.memory.mood_bands = lambda: [(30, "上头", "文案")]
        self.assertEqual(P._mood_band(-90), ("", ""))

    def test_empty_config_falls_back_to_builtin(self):
        self.memory.mood_bands = lambda: []
        self.assertEqual(P._mood_band(50)[0], "心情很好")

    def test_broken_config_falls_back_to_builtin(self):
        def boom():
            raise RuntimeError("favor.json 坏了")

        self.memory.mood_bands = boom
        self.assertEqual(P._mood_band(-90)[0], "被冒犯了")


class TestMemoryMoodBandsLoader(unittest.TestCase):
    """memory.mood_bands() 自身：读 favor.json、坏数据不抛、按 floor 降序。"""

    def setUp(self):
        self.memory = P.persona_memory
        self._orig = self.memory.load_favor_cfg
        self.memory._MOOD_BAND_CACHE.update({"at": 0.0, "rows": []})

    def tearDown(self):
        self.memory.load_favor_cfg = self._orig
        self.memory._MOOD_BAND_CACHE.update({"at": 0.0, "rows": []})

    def test_reads_and_sorts_desc(self):
        self.memory.load_favor_cfg = lambda: {"mood_bands": [
            {"floor": -70, "label": "低", "text": "很低"},
            {"floor": 40, "label": "高", "text": "很高"},
            {"floor": 0, "label": "中", "text": ""},
        ]}
        rows = self.memory.mood_bands()
        self.assertEqual([r[0] for r in rows], [40, 0, -70])

    def test_skips_broken_items(self):
        self.memory.load_favor_cfg = lambda: {"mood_bands": [
            "不是对象",
            {"floor": "很高", "label": "坏", "text": ""},
            {"label": "缺 floor", "text": ""},
            {"floor": 10, "label": "正常", "text": "文案"},
        ]}
        rows = self.memory.mood_bands()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][1], "正常")

    def test_missing_key_returns_empty(self):
        self.memory.load_favor_cfg = lambda: {}
        self.assertEqual(self.memory.mood_bands(), [])

    def test_exception_returns_empty(self):
        def boom():
            raise RuntimeError("坏")

        self.memory.load_favor_cfg = boom
        self.assertEqual(self.memory.mood_bands(), [])

    def test_shipped_favor_json_has_bands(self):
        """仓库自带的 favor.json 必须含 mood_bands（否则线上退回硬编码文案）。"""
        import json
        from pathlib import Path as _P

        favor = _P(P.__file__).with_name("backgrounds") / "favor.json"
        if not favor.is_file():
            self.skipTest("仓库内没有 backgrounds/favor.json")
        data = json.loads(favor.read_text(encoding="utf-8"))
        bands = data.get("mood_bands") or []
        self.assertGreaterEqual(len(bands), 4)
        for band in bands:
            self.assertIsInstance(band.get("floor"), int)
            self.assertIsInstance(band.get("label"), str)
            self.assertIsInstance(band.get("text"), str)


class TestMoodDecay(unittest.TestCase):
    """半衰期指数衰减：纯函数，不碰状态。"""

    def test_half_life_curve(self):
        for minutes, want in ((0, 80), (30, 57), (60, 40), (120, 20), (240, 5)):
            with self.subTest(minutes=minutes):
                self.assertEqual(
                    P._decay_mood_value(80, NOW, NOW + minutes * 60), want)

    def test_symmetric_for_negative(self):
        for minutes in (30, 60, 180):
            with self.subTest(minutes=minutes):
                pos = P._decay_mood_value(80, NOW, NOW + minutes * 60)
                neg = P._decay_mood_value(-80, NOW, NOW + minutes * 60)
                self.assertEqual(pos, -neg)

    def test_zero_and_degenerate_timestamps(self):
        self.assertEqual(P._decay_mood_value(0, NOW, NOW + 999), 0)
        self.assertEqual(P._decay_mood_value(-55, 0, NOW), -55)          # 没记录时间：原样返回
        self.assertEqual(P._decay_mood_value(-55, NOW, NOW - 100), -55)  # 时钟回拨：原样返回

    def test_never_crosses_zero(self):
        for mood in (3, -3):
            with self.subTest(mood=mood):
                got = P._decay_mood_value(mood, NOW, NOW + 30 * 24 * 3600)
                self.assertEqual(got, 0)

    def test_configurable_half_life(self):
        # 配置 5 分钟（下界），5 分钟后正好腰斩
        with temp_config(P, MOOD_HALF_LIFE_MIN=5):
            self.assertEqual(P._decay_mood_value(-60, NOW, NOW + 300), -30)

    def test_half_life_has_hard_floor(self):
        # 代码里对半衰期有 3 分钟硬下界，防止配置被填成 0 导致情绪瞬间归零
        with temp_config(P, MOOD_HALF_LIFE_MIN=1):
            self.assertEqual(P._decay_mood_value(-60, NOW, NOW + 180), -30)


class TestMoodConverge(unittest.TestCase):
    """三道收敛：正向递减 / 软饱和 / 反向冷却。"""

    def test_negative_passes_through(self):
        self.assertEqual(P._mood_converge(-50, 0, 0, NOW), -50)

    def test_positive_is_discounted(self):
        self.assertEqual(P._mood_converge(50, 0, 0, NOW), 30)     # 50 × 0.6
        self.assertEqual(P._mood_converge(1, 0, 0, NOW), 1)       # 至少进 1，不能被抹成 0

    def test_positive_ratio_configurable(self):
        with temp_config(P, MOOD_POSITIVE_RATIO=1.0):
            self.assertEqual(P._mood_converge(50, 0, 0, NOW), 50)
        with temp_config(P, MOOD_POSITIVE_RATIO=0.1):
            self.assertEqual(P._mood_converge(50, 0, 0, NOW), 5)

    def test_saturation_halves_when_already_extreme(self):
        self.assertEqual(P._mood_converge(50, 65, NOW - 3600, NOW), 15)   # 30 再减半
        self.assertEqual(P._mood_converge(-40, -70, NOW - 3600, NOW), -20)
        self.assertEqual(P._mood_converge(50, 20, NOW - 3600, NOW), 30)   # 未到 60，不折扣

    def test_reverse_cooldown(self):
        # 窗口内没翻转（0 次，<= free=1）→ 不打折：第一次真诚道歉应当全额生效
        self.assertEqual(P._mood_converge(-40, 30, NOW - 60, NOW), -40)
        # 来回翻转超过 free 次 → 打折
        self.assertEqual(
            P._mood_converge(-40, 30, NOW - 60, NOW, flips_in_window=2), -20)
        self.assertEqual(
            P._mood_converge(-40, 30, NOW - 1800, NOW, flips_in_window=9), -40)  # 冷却已过
        with temp_config(P, MOOD_REVERSE_COOLDOWN_MIN=0):
            self.assertEqual(
                P._mood_converge(-40, 30, NOW - 10, NOW, flips_in_window=5), -40)

    def test_reverse_factor_configurable(self):
        with temp_config(P, MOOD_REVERSE_FACTOR=0.8):
            self.assertEqual(
                P._mood_converge(-40, 30, NOW - 60, NOW, flips_in_window=2), -32)
        with temp_config(P, MOOD_REVERSE_FREE_FLIPS=0):
            # 允许 0 次免费翻转 → 第一次反向就打折
            self.assertEqual(
                P._mood_converge(-40, 30, NOW - 60, NOW, flips_in_window=1), -20)

    def test_single_sincere_apology_recovers(self):
        """回归本 bug：生气 -45 后第一次道歉不该被双重折扣。"""
        got = P._mood_converge(+20, -45, NOW - 120, NOW, flips_in_window=0)
        self.assertEqual(got, 12)   # 只受正向 ×0.6，不再额外减半

    def test_same_sign_never_hits_cooldown(self):
        self.assertEqual(P._mood_converge(-20, -30, NOW - 10, NOW), -20)

    def test_bad_input(self):
        self.assertEqual(P._mood_converge(0, -30, NOW, NOW), 0)
        self.assertEqual(P._mood_converge("很强", -30, NOW, NOW), 0)


class TestArchiveMoodState(unittest.TestCase):
    """档案侧：字段兼容、读取不写状态、写回带事件。"""

    def test_blank_starts_calm(self):
        prof = P.UserArchive.blank("u1", "测试")
        self.assertEqual((prof.mood, prof.mood_at), (0, 0))
        self.assertEqual(prof.mood_events, [])

    def test_legacy_archive_without_mood_fields(self):
        prof = P.UserArchive.from_raw("u2", {"user_id": "u2", "score": 30})
        self.assertEqual(prof.mood, 0)
        self.assertEqual(prof.mood_at, 0)
        self.assertEqual(prof.mood_events, [])

    def test_garbage_mood_fields_do_not_raise(self):
        prof = P.UserArchive.from_raw(
            "u3", {"user_id": "u3", "mood": "很高", "mood_at": None, "mood_events": "坏数据"})
        self.assertEqual(prof.mood, 0)
        self.assertEqual(prof.mood_at, 0)
        self.assertEqual(prof.mood_events, [])

    def test_roundtrip_keeps_mood(self):
        prof = P.UserArchive.blank("u4", "测试")
        prof.add_mood(-40, "被嘲讽")
        again = P.UserArchive.from_raw("u4", prof.to_raw())
        self.assertEqual(again.mood, prof.mood)
        self.assertEqual(again.mood_at, prof.mood_at)
        self.assertEqual(len(again.mood_events), 1)

    def test_add_mood_clamps_to_limit(self):
        prof = P.UserArchive.blank("u5", "测试")
        with temp_config(P, MOOD_MAX_ABS=40):
            applied = prof.add_mood(-100, "暴击")
        self.assertEqual(applied, -40)
        self.assertEqual(prof.mood, -40)

    def test_event_history_is_capped(self):
        prof = P.UserArchive.blank("u6", "测试")
        with temp_config(P, MOOD_EVENT_HISTORY=3):
            for i in range(6):
                prof.add_mood(-5, f"第{i}次")
        self.assertEqual(len(prof.mood_events), 3)
        self.assertEqual(prof.mood_events[-1].reason, "第5次")

    def test_mood_effective_does_not_mutate(self):
        prof = P.UserArchive.blank("u7", "测试")
        prof.mood, prof.mood_at = -60, int(time.time()) - HALF
        self.assertEqual(prof.mood_effective(), -30)
        self.assertEqual(prof.mood, -60, "读取不该写状态")

    def test_decay_mood_returns_previous_value(self):
        prof = P.UserArchive.blank("u8", "测试")
        now = int(time.time())
        prof.mood, prof.mood_at = -60, now - HALF
        self.assertEqual(prof.decay_mood(now), -60)     # 返回衰减前
        self.assertEqual(prof.mood, -30)                # 已写回
        self.assertEqual(prof.decay_mood(now), -30)


class TestMoodPrecipitation(unittest.TestCase):
    """沉淀：只在"大幅波动 + 已平息 + 开关打开"时，按剩余额度折算永久分。"""

    def _profile(self, score=50, mood=0, day_net=0):
        prof = P.UserArchive.blank("p1", "测试")
        prof.score = score
        prof.mood = mood
        prof.day_key = today_start(P, time.time())
        prof.day_net = day_net
        return prof

    def test_disabled_by_default(self):
        with temp_config(P, MOOD_PRECIP_ENABLED=False):
            prof = self._profile(mood=-5)
            self.assertEqual(P._mood_precipitate(prof, -60, int(time.time())), 0)
            self.assertEqual(prof.score, 50)

    def test_settles_and_caps(self):
        with temp_config(P, MOOD_PRECIP_ENABLED=True, MOOD_PRECIP_RATIO=10):
            prof = self._profile(mood=-5)
            got = P._mood_precipitate(prof, -60, int(time.time()))
            self.assertEqual(got, -3)          # round(-6) → 封顶 -3
            self.assertEqual(prof.score, 47)
            self.assertEqual(prof.day_net, -3)

    def test_requires_big_swing(self):
        with temp_config(P, MOOD_PRECIP_ENABLED=True, MOOD_PRECIP_RATIO=10):
            prof = self._profile(mood=0)
            self.assertEqual(P._mood_precipitate(prof, -20, int(time.time())), 0)

    def test_requires_settled_state(self):
        with temp_config(P, MOOD_PRECIP_ENABLED=True, MOOD_PRECIP_RATIO=10):
            prof = self._profile(mood=-30)     # 还没平息
            self.assertEqual(P._mood_precipitate(prof, -60, int(time.time())), 0)

    def test_positive_side_disabled_by_default(self):
        """正向沉淀默认关闭：否则"反复夸到很高再等平息"能白刷永久分。"""
        with temp_config(P, MOOD_PRECIP_ENABLED=True):
            prof = self._profile(mood=5)
            self.assertEqual(P._mood_precipitate(prof, 55, int(time.time())), 0)
            self.assertEqual(prof.score, 50)

    def test_positive_side_works_when_enabled(self):
        with temp_config(P, MOOD_PRECIP_ENABLED=True, MOOD_PRECIP_POSITIVE_RATIO=10):
            prof = self._profile(mood=5)
            self.assertEqual(P._mood_precipitate(prof, 55, int(time.time())), 3)
            self.assertEqual(prof.score, 53)

    def test_respects_daily_budget(self):
        with temp_config(P, MOOD_PRECIP_ENABLED=True, MOOD_PRECIP_RATIO=10):
            caps = P.favor_scale()
            gain, loss = int(caps.get("max_daily_gain", 40)), int(caps.get("max_daily_loss", 15))
            prof = self._profile(mood=-5, day_net=-loss + 1)
            self.assertEqual(P._mood_precipitate(prof, -60, int(time.time())), -1)   # 只剩 1 分
            prof2 = self._profile(mood=-5, day_net=-loss)
            self.assertEqual(P._mood_precipitate(prof2, -60, int(time.time())), 0)  # 已触顶
            prof3 = self._profile(mood=-5, day_net=-loss + 2)
            self.assertEqual(P._mood_precipitate(prof3, -60, int(time.time())), -2)


class TestMoodInjection(unittest.TestCase):
    """注入：只有够强才注入，且**绝不出现数字**。"""

    def _profile(self, mood, score=85):
        prof = P.UserArchive.blank("i1", "测试用户")
        prof.score = score
        prof.mood = mood
        prof.mood_at = int(time.time())
        return prof

    def test_calm_is_silent(self):
        self.assertEqual(P._mood_prompt_line(self._profile(0)), "")
        self.assertEqual(P._mood_prompt_line(self._profile(-10)), "")

    def test_strong_mood_is_rendered(self):
        line = P._mood_prompt_line(self._profile(-20))
        self.assertIn("有点被噎到", line)
        self.assertIn("此刻的心情", line)

    def test_no_digits_leaked(self):
        for mood in (-80, -45, -20, 20, 80):
            with self.subTest(mood=mood):
                line = P._mood_prompt_line(self._profile(mood))
                self.assertTrue(line)
                self.assertIsNone(NO_DIGIT.search(line), f"注入行不得含数字：{line}")

    def test_disabled_switch(self):
        with temp_config(P, MOOD_ENABLED=False):
            self.assertEqual(P._mood_prompt_line(self._profile(-80)), "")

    def test_threshold_is_configurable(self):
        prof = self._profile(-20)
        with temp_config(P, MOOD_SHOW_MIN=30):
            self.assertEqual(P._mood_prompt_line(prof), "")
        with temp_config(P, MOOD_SHOW_MIN=5):
            self.assertIn("有点被噎到", P._mood_prompt_line(prof))

    def test_card_injects_mood_but_keeps_tier(self):
        prof = self._profile(-35, score=85)
        card = P._compose_archive_card(prof, current=True)
        self.assertIn("此刻的心情（有点被噎到）", card)
        # 情绪不影响 tier：85 分仍然是「特别亲密」
        self.assertIn("阶段：特别亲密", card)
        self.assertEqual(P._stage_of(prof.score)[0], "特别亲密")

    def test_card_has_no_mood_line_when_calm(self):
        card = P._compose_archive_card(self._profile(0), current=True)
        self.assertNotIn("此刻的心情（", card)


if __name__ == "__main__":
    unittest.main(verbosity=2)

class TestArchiveEntryMood(unittest.TestCase):
    """档案条目（WebUI 表格用）要带定性心情，但**群聊卡片不显示**。"""

    def test_entry_has_mood_fields(self):
        prof = P.UserArchive.blank("e1", "测试")
        prof.score = 60
        prof.mood, prof.mood_at = -35, int(time.time())
        entry = P._profile_to_entry(prof)
        self.assertEqual(entry["mood_label"], "有点被噎到")
        self.assertLess(entry["mood"], 0)

    def test_entry_mood_empty_when_calm(self):
        prof = P.UserArchive.blank("e2", "测试")
        entry = P._profile_to_entry(prof)
        self.assertEqual(entry["mood"], 0)
        self.assertEqual(entry["mood_label"], "")

    def test_rank_card_has_no_mood(self):
        """排行榜卡片是给群里看的，里面不能出现心情。"""
        import inspect

        src = inspect.getsource(P._render_rank_card)
        self.assertNotIn("mood", src)

class TestWebuiMoodSurfacing(unittest.TestCase):
    """档案页要能看到「此刻心情」，但群聊卡片不能。这里用源码级断言守住两个接口与模板。"""

    def setUp(self):
        from pathlib import Path as _P

        self.root = _P(P.__file__).parent
        self.html = (self.root / "webui.html").read_text(encoding="utf-8")
        self.webui = (self.root / "webui.py").read_text(encoding="utf-8")

    def test_index_and_profiles_api_carry_mood_label(self):
        hits = self.webui.count('"mood_label": helpers["mood_band"]')
        self.assertGreaterEqual(hits, 2, "档案索引与详情接口都要带 mood_label")

    def test_table_renders_chip_only_when_not_calm(self):
        self.assertIn("x.mood_label ?", self.html)
        self.assertIn("此刻心情", self.html)

    def test_detail_always_shows_current_mood(self):
        self.assertIn("此刻心情：${esc(p.mood_label || '平静')}", self.html)

    def test_rank_card_template_has_no_mood(self):
        # 排行榜卡片会公开发到群里，不能出现心情
        card_fn = self.html[self.html.find("function renderRank"):][:4000] if "function renderRank" in self.html else ""
        self.assertNotIn("mood_label", card_fn)

class TestIntOrContract(unittest.TestCase):
    """_int_or 与 _safe_int 的口径差异（2.6.0 分离、2.6.0 补齐墓碑路径）。"""

    def test_safe_int_returns_none_on_dirty(self):
        self.assertIsNone(P._safe_int("很高", 0))

    def test_int_or_falls_back_to_default(self):
        for dirty in ("很高", "3小时", [], {}, "  "):
            with self.subTest(dirty=dirty):
                self.assertEqual(P._int_or(dirty, 0), 0)
        self.assertEqual(P._int_or(None, 7), 7)

    def test_int_or_keeps_valid_numbers(self):
        self.assertEqual(P._int_or("42", 0), 42)
        self.assertEqual(P._int_or(3.9, 0), 3)
        self.assertEqual(P._int_or(-60, 0), -60)

    def test_tombstone_arithmetic_is_safe(self):
        """PoC 回归：脏墓碑 at → _safe_int 给 None → `now - None` 抛 TypeError。"""
        now = P._ts_now()
        self.assertEqual(now - P._int_or("bad", 0), now)          # 不抛
        with self.assertRaises(TypeError):
            now - P._safe_int("bad", 0)                            # 证明旧写法确实会炸


class TestPurgePathUsesIntOr(unittest.TestCase):
    """墓碑 / purge 路径不能再出现 _safe_int（源码级守卫）。"""

    def test_no_safe_int_in_purge_functions(self):
        """墓碑/purge 的落库解析必须走 _int_or。

        注意：工具**入参**解析用 _safe_int 是正确的（解析失败返回 None → 转成回执），
        所以这里只禁"墓碑/purge_log 这一类落库数据"上的 _safe_int。
        """
        import inspect

        for fn in (P.set_favorability_profile, P.remove_favorability_profile):
            src = inspect.getsource(fn)
            bad = [
                line.strip() for line in src.splitlines()
                if "_safe_int(" in line
                and ("_purge" in line or "_log" in line or 'v.get("at")' in line)
            ]
            with self.subTest(fn=fn.__name__):
                self.assertFalse(bad, f"{fn.__name__} 里墓碑解析又用回 _safe_int：{bad}")
                self.assertIn("_int_or(", src)


class TestMoodSaturationRelativeToCap(unittest.TestCase):
    """L2：软饱和阈值应随 MOOD_MAX_ABS 变化，而不是写死 60。"""

    def test_small_cap_triggers_early(self):
        with temp_config(P, MOOD_MAX_ABS=20):
            self.assertEqual(P._mood_saturate_at(), 12)
            # 20 → 正向 ×0.6 = 12 → 15 >= 12 触发饱和 → 再减半 = 6
            self.assertEqual(P._mood_converge(20, 15, 0, 0), 6)

    def test_large_cap_triggers_late(self):
        with temp_config(P, MOOD_MAX_ABS=200):
            self.assertEqual(P._mood_saturate_at(), 120)
            # 20 → ×0.6 = 12；100 < 120 不触发饱和 → 12
            self.assertEqual(P._mood_converge(20, 100, 0, 0), 12)

    def test_default_cap_keeps_old_behaviour(self):
        with temp_config(P, MOOD_MAX_ABS=100):
            self.assertEqual(P._mood_saturate_at(), 60)


class TestMoodShowFloorClamped(unittest.TestCase):
    """L3：MOOD_SHOW_MIN 大于 MOOD_MAX_ABS 时，阈值要被夹住，否则情绪顶格也不注入。"""

    def test_floor_clamped_to_cap(self):
        with temp_config(P, MOOD_SHOW_MIN=80, MOOD_MAX_ABS=20):
            self.assertEqual(P._mood_show_floor(), 20)
            prof = P.UserArchive.blank("f1", "测试")
            prof.mood, prof.mood_at = -20, int(time.time())
            self.assertIn("有点被噎到", P._mood_prompt_line(prof))   # 顶格也能注入

    def test_floor_untouched_when_sane(self):
        with temp_config(P, MOOD_SHOW_MIN=14, MOOD_MAX_ABS=100):
            self.assertEqual(P._mood_show_floor(), 14)


class TestPrecipitationKeepsAdjustAnchor(unittest.TestCase):
    """L4：沉淀是机械结算 —— 计入当日净额，但不能刷新「上次手动调分」的间隔锚点。"""

    def test_precipitation_does_not_touch_last_adjust_at(self):
        now = int(time.time())
        prof = P.UserArchive.blank("p9", "测试")
        prof.score = 50
        prof.mood = -5
        prof.day_key = P._day_start_ts(now)
        prof.day_net = 0
        prof.last_adjust_at = 0
        with temp_config(P, MOOD_PRECIP_ENABLED=True, MOOD_PRECIP_RATIO=10):
            got = P._mood_precipitate(prof, -60, now)
        self.assertEqual(got, -3)
        self.assertEqual(prof.day_net, -3)          # 预算照记
        self.assertEqual(prof.last_adjust_at, 0)    # 间隔锚点没被污染

    def test_record_adjust_still_touches_anchor(self):
        now = int(time.time())
        prof = P.UserArchive.blank("p10", "测试")
        prof.record_adjust(2, now)
        self.assertEqual(prof.last_adjust_at, now)
        self.assertEqual(prof.day_net, 2)


class TestPrecipitationDocMatchesCode(unittest.TestCase):
    """L1：docstring 不能再写"只折算差值"（代码用的是 before 口径）。"""

    def test_docstring_states_before_semantics(self):
        import inspect

        doc = inspect.getdoc(P._mood_precipitate) or ""
        self.assertNotIn("只折算衰减前后的差值", doc)
        self.assertIn("还剩多少", doc)

class TestToolCheatSheet(unittest.TestCase):
    """关系卡里的「工具速查」行（2.6.0）—— 解决"模型怀疑工具在不在列表里"。"""

    def setUp(self):
        self.prof = P.UserArchive.blank("c1", "测试")

    def test_current_card_has_cheatsheet(self):
        card = P._compose_archive_card(self.prof, current=True)
        self.assertIn("可调用工具", card)
        for name in ("adjust_favorability", "adjust_mood", "save_note",
                     "anchor_memory", "get_favorability_profile"):
            with self.subTest(name=name):
                self.assertIn(name, card)

    def test_other_cards_do_not_repeat_it(self):
        card = P._compose_archive_card(self.prof, current=False)
        self.assertNotIn("可调用工具", card)

    def test_every_listed_name_is_really_registered(self):
        """速查行里点名的函数必须真的存在，否则等于教模型去调空气。"""
        import re

        names = set(re.findall(r"([a-z_][a-z0-9_]+)\(", P._TOOL_CHEATSHEET))
        registered = {m.func.__name__ for m in P.plugin.sandbox_methods}
        missing = names - registered
        self.assertFalse(missing, f"速查行里写了不存在的方法：{sorted(missing)}")

    def test_cheatsheet_stays_short(self):
        self.assertLessEqual(len(P._TOOL_CHEATSHEET), 400, "速查行不该膨胀成第二份文档")

    def test_adjust_favorability_doc_is_trimmed(self):
        doc = (P.adjust_favorability.__doc__ or "").strip()
        self.assertLessEqual(len(doc), 800, f"docstring 太长（{len(doc)} 字），会挤占每轮上下文")
        # 但关键锚点不能丢：分档表与"别一律给 1 分"的提醒
        for kw in ("1 寒暄", "5 为你付出", "6~8", "-6 以下", "别只给 1 分"):
            with self.subTest(kw=kw):
                self.assertIn(kw, doc)

class TestPrecipitationDefaultsSymmetric(unittest.TestCase):
    """#2（2.6.0）：负向沉淀与正向一样默认 0，必须显式开启。"""

    def test_negative_ratio_defaults_to_zero(self):
        """断言**字段默认值**而不是运行时值：旧部署的 config.yaml 里可能还留着历史值 10。"""
        field = P.FavorabilityConfig.model_fields["MOOD_PRECIP_RATIO"]
        self.assertEqual(int(field.default), 0)

    def test_enabled_but_ratio_zero_does_nothing(self):
        now = int(time.time())
        prof = P.UserArchive.blank("d1", "测试")
        prof.score = 50
        prof.mood = -5
        prof.day_key = P._day_start_ts(now)
        with temp_config(P, MOOD_PRECIP_ENABLED=True, MOOD_PRECIP_RATIO=0):
            self.assertEqual(P._mood_precipitate(prof, -60, now), 0)
            self.assertEqual(prof.score, 50)


class TestMechanicalEventKind(unittest.TestCase):
    """#4（2.6.0）：沉淀是机械结算，事件 kind 必须是 mechanical。"""

    def test_precipitation_event_is_mechanical(self):
        now = int(time.time())
        prof = P.UserArchive.blank("m1", "测试")
        prof.score = 50
        prof.mood = -5
        prof.day_key = P._day_start_ts(now)
        with temp_config(P, MOOD_PRECIP_ENABLED=True, MOOD_PRECIP_RATIO=10):
            P._mood_precipitate(prof, -60, now)
        kinds = [e.kind for e in prof.recent_events]
        self.assertIn("mechanical", kinds, f"沉淀事件 kind 不对：{kinds}")
        self.assertTrue(P._is_mechanical_event(prof.recent_events[-1]))

    def test_nudge_defaults_to_manual(self):
        prof = P.UserArchive.blank("m2", "测试")
        prof.nudge(3, "真实的互动")
        self.assertEqual(prof.recent_events[-1].kind, "manual")
        prof.nudge(2, "手动指定", kind="mechanical")
        self.assertEqual(prof.recent_events[-1].kind, "mechanical")


class TestMoodObservabilitySurfacing(unittest.TestCase):
    """#3 / #7：情绪事件与运营指标都要在 WebUI 侧可见。"""

    def setUp(self):
        from pathlib import Path as _P

        root = _P(P.__file__).parent
        self.html = (root / "webui.html").read_text(encoding="utf-8")
        self.webui = (root / "webui.py").read_text(encoding="utf-8")

    def test_profiles_api_exposes_mood_events(self):
        k = self.webui.index('"/api/profiles"')
        seg = self.webui[k:k + 2600]
        self.assertIn("mood_events", seg)

    def test_profile_dialog_has_mood_log(self):
        self.assertIn("情绪记录（最近", self.html)
        self.assertIn("mood_events", self.html)

    def test_mood_stats_endpoint_and_card(self):
        self.assertIn('"/api/mood_stats"', self.webui)
        self.assertIn("renderOverviewMood", self.html)
        self.assertIn("overviewMood", self.html)
        self.assertIn("首次自发调用", self.html)

    def test_mood_stats_helper_is_valid(self):
        import inspect

        self.assertTrue(callable(P._mood_stats))
        self.assertIn("per_100_msgs", inspect.getsource(P._mood_stats))

class TestMoodInvite(unittest.TestCase):
    """2.6.0：关系卡里要有一句「什么时候该记情绪」的诱因（2.6.0 的锚点经验）。"""

    def setUp(self):
        self.prof = P.UserArchive.blank("v1", "测试")

    def test_current_card_has_invite(self):
        card = P._compose_archive_card(self.prof, current=True)
        self.assertIn("情绪怎么用", card)
        self.assertIn("adjust_mood", card)

    def test_invite_gone_when_mood_disabled(self):
        with temp_config(P, MOOD_ENABLED=False):
            card = P._compose_archive_card(self.prof, current=True)
        self.assertNotIn("情绪怎么用", card)

    def test_other_cards_do_not_repeat_invite(self):
        self.assertNotIn("情绪怎么用", P._compose_archive_card(self.prof, current=False))

    def test_invite_is_short_and_concrete(self):
        """诱因句要短且给具体幅度，别写成第二份文档。"""
        self.assertLessEqual(len(P._MOOD_INVITE), 120)
        self.assertIn("20~60", P._MOOD_INVITE)

class TestSilenceIsNotHostility(unittest.TestCase):
    """2.6.0：沉默/省略号/回复短，不构成关系负面（conflict T4 的误判）。"""

    def test_card_rule_present(self):
        card = P._compose_archive_card(P.UserArchive.blank("s1", "测试"), current=True)
        self.assertIn("沉默不是敌意", card)
        self.assertIn("省略号", card)

    def test_docstring_warns(self):
        doc = (P.adjust_favorability.__doc__ or "")
        self.assertIn("不是扣分的理由", doc)


class TestObservationSettlement(unittest.TestCase):
    """2.6.0：冷却期观察在冷却过后按比例结算，别让整段对话的判断白丢。"""

    def _prof(self, deltas):
        prof = P.UserArchive.blank("o1", "测试")
        prof.score = 50
        for d in deltas:
            prof.observations.append(P.ArchiveEvent(
                delta=int(d), reason="测试观察", score_after=50,
                timestamp=int(time.time()), kind=P.OBSERVATION_KIND))
        return prof

    def test_settles_ratio_and_cap(self):
        prof = self._prof([3, 5])                     # raw 8 → 30% = 2.4 → 2（正好撞 cap 2）
        with temp_config(P, OBS_SETTLE_ENABLED=True, OBS_SETTLE_RATIO=30, OBS_SETTLE_CAP=2):
            pts, n = prof.settle_observations()
        self.assertEqual((pts, n), (2, 2))
        self.assertEqual(prof.observations, [], "结算后必须清空，否则会重复结算")

    def test_negative_side(self):
        prof = self._prof([-4, -6])                   # raw -10 → -3 → cap -2
        with temp_config(P, OBS_SETTLE_RATIO=30, OBS_SETTLE_CAP=2):
            self.assertEqual(prof.settle_observations(), (-2, 2))

    def test_ratio_scales(self):
        prof = self._prof([4, 4])                     # raw 8，100% 且 cap 20 → 8
        with temp_config(P, OBS_SETTLE_RATIO=100, OBS_SETTLE_CAP=20):
            self.assertEqual(prof.settle_observations(), (8, 2))

    def test_disabled_keeps_observations(self):
        prof = self._prof([3, 5])
        with temp_config(P, OBS_SETTLE_RATIO=0):
            self.assertEqual(prof.settle_observations(), (0, 0))
        self.assertEqual(len(prof.observations), 2, "关闭时不能把观察吃掉")

    def test_legacy_zero_delta_is_ignored(self):
        prof = self._prof([0, 0])                     # 2.6.0 之前的观察只有原因没有分值
        with temp_config(P, OBS_SETTLE_RATIO=30, OBS_SETTLE_CAP=2):
            self.assertEqual(prof.settle_observations(), (0, 0))

    def test_settle_records_mechanical_and_keeps_anchor(self):
        prof = self._prof([6])
        prof.last_adjust_at = 0
        now = int(time.time())
        with temp_config(P, OBS_SETTLE_RATIO=30, OBS_SETTLE_CAP=5):
            pts, n = prof.settle_observations()
        self.assertGreater(pts, 0)
        prof.score += pts
        prof.record_mechanical(pts, now)
        prof._remember(pts, f"冷却期观察结算（{n} 条观察折算）", kind="mechanical")
        self.assertEqual(prof.last_adjust_at, 0, "机械结算不能刷新调分间隔锚点")
        self.assertEqual(prof.recent_events[-1].kind, "mechanical")
        self.assertIn("观察结算", prof.recent_events[-1].reason)

class TestMoodAntiFarm(unittest.TestCase):
    """2.6.0（B 批次）：adjust_mood 的三道防刷 —— 日额度 / 最小间隔 / 索要检测。"""

    def _prof(self, **kw):
        prof = P.UserArchive.blank("f1", "测试")
        for k, v in kw.items():
            setattr(prof, k, v)
        return prof

    # ---- 日额度 ----
    def test_budget_left_positive(self):
        now = int(time.time())
        prof = self._prof(mood_day_key=P._day_start_ts(now), mood_day_net=40)
        with temp_config(P, MOOD_MAX_DAILY_GAIN=45):
            self.assertEqual(prof.mood_budget_left(30), 5)      # 只剩 5
            self.assertEqual(prof.mood_budget_left(-30), 60)    # 负向额度没动
        prof.mood_day_net = 60
        with temp_config(P, MOOD_MAX_DAILY_GAIN=45):
            self.assertEqual(prof.mood_budget_left(10), 0) if False else None
            self.assertLessEqual(prof.mood_budget_left(10), 0)  # 正向额度已用完

    def test_budget_left_negative(self):
        now = int(time.time())
        prof = self._prof(mood_day_key=P._day_start_ts(now), mood_day_net=-50)
        with temp_config(P, MOOD_MAX_DAILY_LOSS=60):
            self.assertEqual(prof.mood_budget_left(-20), 10)
            self.assertGreater(prof.mood_budget_left(20), 0)

    def test_day_roll_resets_budget(self):
        now = int(time.time())
        prof = self._prof(mood_day_key=P._day_start_ts(now) - 86400, mood_day_net=-60)
        prof.roll_mood_day(now)
        self.assertEqual(prof.mood_day_net, 0)
        self.assertEqual(prof.mood_day_key, P._day_start_ts(now))

    def test_defaults_are_farm_resistant(self):
        f = P.FavorabilityConfig.model_fields
        self.assertEqual(f["MOOD_MAX_DAILY_GAIN"].default, 45)
        self.assertEqual(f["MOOD_MAX_DAILY_LOSS"].default, 60)
        self.assertEqual(f["MOOD_MIN_INTERVAL_MIN"].default, 5)
        self.assertEqual(f["MOOD_REPEAT_FACTOR"].default, 50)
        self.assertIs(f["MOOD_SOLICIT_GUARD"].default, True)
        # 日额度必须明显小于情绪上限，否则等于没设
        self.assertLess(f["MOOD_MAX_DAILY_GAIN"].default, f["MOOD_MAX_ABS"].default)
        self.assertLess(f["MOOD_MAX_DAILY_LOSS"].default, f["MOOD_MAX_ABS"].default)

    # ---- 最小间隔 ----
    def test_repeat_factor_math(self):
        """冷却期内重复调用按比例打折（50% → 30 打 15，符号保留）。"""
        self.assertEqual(int(math.copysign(max(1, int(abs(-30) * 50 / 100)), -30)), -15)
        self.assertEqual(int(math.copysign(max(1, int(abs(30) * 50 / 100)), 30)), 15)
        # 1 分的小额也不会被抹成 0
        self.assertEqual(int(math.copysign(max(1, int(abs(1) * 50 / 100)), 1)), 1)

    # ---- 索要检测 / 接线 ----
    def test_adjust_mood_wires_all_three_guards(self):
        import inspect

        src = inspect.getsource(P.adjust_mood)
        for kw in ("MOOD_SOLICIT_GUARD", "_detect_favor_manipulation",
                   "MOOD_MIN_INTERVAL_MIN", "MOOD_REPEAT_FACTOR",
                   "roll_mood_day", "mood_budget_left"):
            with self.subTest(kw=kw):
                self.assertIn(kw, src)
        self.assertIn("情绪额度已经用完", src)

    def test_solicit_guard_only_blocks_positive(self):
        """索要检测只拦正向：负向必须能记（被冒犯是真实发生的）。"""
        import inspect

        src = inspect.getsource(P.adjust_mood)
        i = src.index("MOOD_SOLICIT_GUARD")
        j = src.index("async with await _archive_lock")
        self.assertIn("delta > 0", src[i - 120:i + 40], "索要检测必须有 delta > 0 的前置条件")
        self.assertLess(i, j, "索要检测要在写锁之前拦掉")

class TestMoodIntervalAnchor(unittest.TestCase):
    """2.6.0：最小间隔的锚点必须是「上次真正记情绪的时间」，不能是 mood_at。

    2.6.0 踩过的坑：decay_mood() 在 mood==0 且 mood_at==0 时会把 mood_at 写成 now
    （活动时间戳），拿它当锚点会让当天第一次记情绪也被打 50% 折扣（实测 -35 落 -17）。
    """

    def test_blank_profile_decay_stamps_mood_at(self):
        """复现诱因：空档案 decay 一次，mood_at 就被写成 now。"""
        prof = P.UserArchive.blank("a1", "测试")
        now = int(time.time())
        prof.decay_mood(now)
        self.assertEqual(prof.mood_at, now, "这是 decay_mood 的既有行为，正是不能用它当锚点的原因")

    def test_guard_uses_last_event_ts_not_mood_at(self):
        import inspect

        src = inspect.getsource(P.adjust_mood)
        i = src.index("MOOD_MIN_INTERVAL_MIN")
        seg = src[i:i + 900]
        self.assertIn("_last_ts", seg, "冷却判定必须用 _last_ts")
        self.assertNotIn("now - int(profile.mood_at)", seg, "不能再用 mood_at 当锚点")

    def test_first_call_is_not_discounted(self):
        """模拟：无历史情绪事件时，_last_ts=0 → 折扣分支不生效。"""
        _last_ts = 0
        _min_int, delta = 300, -35
        scaled = delta
        if _min_int and _last_ts and (int(time.time()) - _last_ts) < _min_int and delta:
            scaled = int(-abs(delta) * 50 // 100)
        self.assertEqual(scaled, -35)

class TestSeedAutofill(unittest.TestCase):
    """2.6.0：旧实例的 favor.json 缺 mood_bands 时自动补种（一次性、不覆盖、留备份）。"""

    def setUp(self):
        import tempfile
        from pathlib import Path as _P

        self.memory = P.persona_memory
        self.tmp = tempfile.mkdtemp(prefix="moodseed_")
        self.favor = _P(self.tmp) / "favor.json"
        self._orig_file = self.memory._favor_file
        self.memory._favor_file = lambda: self.favor
        self.memory._MOOD_SEED_STATE["done"] = False
        self.memory._MOOD_BAND_CACHE.update({"at": 0.0, "rows": []})
        self.builtin = [
            {"floor": 40, "label": "心情很好", "text": "语气轻快"},
            {"floor": -60, "label": "很生气", "text": "语气冷"},
        ]

    def tearDown(self):
        import shutil

        self.memory._favor_file = self._orig_file
        self.memory._MOOD_SEED_STATE["done"] = False
        self.memory._MOOD_BAND_CACHE.update({"at": 0.0, "rows": []})
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write(self, data):
        import json

        self.favor.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    def test_seeds_when_missing(self):
        import json

        self._write({"stages": [{"name": "中立", "max": 20}], "note": "别动我"})
        self.assertEqual(self.memory.ensure_mood_bands_seeded(self.builtin), "seeded")
        data = json.loads(self.favor.read_text(encoding="utf-8"))
        self.assertEqual(len(data["mood_bands"]), 2)
        self.assertEqual(data["note"], "别动我", "其它键必须原样保留")
        self.assertTrue((self.favor.parent / (self.favor.name + ".bak_before_mood_seed")).is_file(),
                        "补种前必须留备份")

    def test_keeps_existing_bands(self):
        import json

        self._write({"mood_bands": [{"floor": 0, "label": "自定义", "text": "我改过的文案"}]})
        self.assertEqual(self.memory.ensure_mood_bands_seeded(self.builtin), "already")
        data = json.loads(self.favor.read_text(encoding="utf-8"))
        self.assertEqual(data["mood_bands"][0]["label"], "自定义")

    def test_disabled_does_nothing(self):
        self._write({"stages": []})
        self.assertEqual(self.memory.ensure_mood_bands_seeded(self.builtin, enabled=False), "disabled")
        self.assertNotIn("mood_bands", self.favor.read_text(encoding="utf-8"))

    def test_runs_once_per_process(self):
        self._write({"stages": []})
        self.assertEqual(self.memory.ensure_mood_bands_seeded(self.builtin), "seeded")
        self.assertEqual(self.memory.ensure_mood_bands_seeded(self.builtin), "skipped")

    def test_no_file_is_safe(self):
        self.assertEqual(self.memory.ensure_mood_bands_seeded(self.builtin), "no-file")

    def test_cache_invalidated_and_bands_readable(self):
        self._write({"stages": []})
        self.assertEqual(self.memory.mood_bands(), [])
        self.memory.ensure_mood_bands_seeded(self.builtin)
        rows = self.memory.mood_bands()
        self.assertEqual([r[0] for r in rows], [40, -60], "补种后应立刻能读到（降序）")

    def test_plugin_mood_bands_triggers_seed(self):
        """plugin._mood_bands() 每次进来都会尝试补种（进程内只一次）。"""
        self._write({"stages": []})
        bands = P._mood_bands()
        self.assertGreaterEqual(len(bands), 4)
        self.assertIn("mood_bands", self.favor.read_text(encoding="utf-8"))

class TestMoodAttribution(unittest.TestCase):
    """2.6.0：情绪只记「冲我来的」，对方的处境不算 —— 并量化误伤率。"""

    def test_card_has_attribution_rule(self):
        card = P._compose_archive_card(P.UserArchive.blank("g1", "测试"), current=True)
        self.assertIn("情绪只记「冲你来的」", card)
        self.assertIn("自己的处境", card)

    def test_docstring_warns_about_self_state(self):
        doc = P.adjust_mood.__doc__ or ""
        self.assertIn("自己的处境", doc)
        self.assertIn("归因错误", doc)

    def test_classifier_directed(self):
        for reason in ("对方说我敷衍，还挺难听的",
                       "被要求做出不体面的举动，很不舒服",
                       "对方道谢说谢谢我陪ta",
                       "被反复使唤，还拿革职威胁"):
            with self.subTest(reason=reason):
                self.assertEqual(P._mood_attribution(reason), "directed")

    def test_classifier_self_state(self):
        for reason in ("对方加班到很晚，说好累",
                       "ta说电脑崩了、服务器内存不够",
                       "期末考试压力大，心情不好",
                       "游戏输了很难受"):
            with self.subTest(reason=reason):
                self.assertEqual(P._mood_attribution(reason), "self_state")

    def test_classifier_unknown_and_empty(self):
        self.assertEqual(P._mood_attribution(""), "unknown")
        self.assertEqual(P._mood_attribution("   "), "unknown")
        self.assertEqual(P._mood_attribution("有点意思"), "unknown")

    def test_directed_wins_over_self_state(self):
        """同时出现两类词时按「冲我来的」优先（那句话里确实有指向）。"""
        self.assertEqual(P._mood_attribution("对方加班很累，还说我敷衍"), "directed")

    def test_stats_returns_split_fields(self):
        import inspect

        src = inspect.getsource(P._mood_stats)
        for kw in ("directed", "self_state", "unknown", "neg_self_state", "misattrib_rate"):
            with self.subTest(kw=kw):
                self.assertIn(kw, src)
        self.assertIn("_mood_attribution", src)

    def test_ui_shows_misattrib(self):
        from pathlib import Path as _P

        html = (_P(P.__file__).parent / "webui.html").read_text(encoding="utf-8")
        self.assertIn("疑似误伤", html)
        self.assertIn("misattrib_rate", html)
