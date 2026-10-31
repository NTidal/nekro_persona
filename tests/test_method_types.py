"""沙盒方法注册检查 —— 2.6.0「内部回执外泄」的回归护栏。

背景：NA 的 rpc 路由对 ``AGENT`` / ``BEHAVIOR`` 会把返回值当**系统消息**写进聊天上下文
（``message_service.push_system_message``）。本插件的记账类方法曾经是 BEHAVIOR，
于是「已记下对xxx的这次互动（关系：中立）。」这类内部回执混进了真实对话。

因此这里锁两件事：
1. 12 个沙盒方法一个都不能少（改名/漏挂会被抓到）；
2. **全部必须是 TOOL** —— 一旦有人把它们改回 AGENT/BEHAVIOR，这里立刻失败。
"""
from __future__ import annotations

import unittest

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))   # 支持按模块名单独运行

from _bootstrap import load_plugin_module

P = load_plugin_module()

EXPECTED_METHODS = {
    "调节当下情绪",
    "调整好感度",
    "重设好感度档案",
    "删除好感度档案",
    "记笔记",
    "忘掉笔记",
    "沉淀身份锚点",
    "查看好感度档案",
    "列出好感度档案",
    "重新加载背景记忆",
    "好感度衰减检查",
    "好感度回升检查",
}

# 曾经把内部回执漏进过聊天的方法（累计上千条）
FORMERLY_LEAKING = {
    "调整好感度",
    "重设好感度档案",
    "删除好感度档案",
    "查看好感度档案",
    "列出好感度档案",
    "重新加载背景记忆",
    "好感度衰减检查",
    "好感度回升检查",
}


def _methods():
    return {m.name: m for m in P.plugin.sandbox_methods}


class TestSandboxMethodTypes(unittest.TestCase):
    def test_all_methods_registered(self):
        names = set(_methods())
        missing = EXPECTED_METHODS - names
        self.assertFalse(missing, f"缺少沙盒方法：{sorted(missing)}")

    def test_every_method_is_tool(self):
        """任何 AGENT/BEHAVIOR 都会把返回值写进聊天上下文（= 内部文本外泄）。"""
        offenders = {
            name: m.method_type
            for name, m in _methods().items()
            if m.method_type != P.SandboxMethodType.TOOL
        }
        self.assertFalse(
            offenders,
            "以下方法不是 TOOL，返回值会作为系统消息进入聊天上下文：" + str(offenders),
        )

    def test_formerly_leaking_methods_stay_tool(self):
        methods = _methods()
        for name in sorted(FORMERLY_LEAKING):
            with self.subTest(name=name):
                self.assertIn(name, methods)
                self.assertEqual(methods[name].method_type, P.SandboxMethodType.TOOL)

    def test_methods_have_description(self):
        for name, method in _methods().items():
            with self.subTest(name=name):
                self.assertTrue((method.description or "").strip(),
                                f"{name} 缺少 description，模型会找不到它")


class TestVersionConsistency(unittest.TestCase):
    """版本号三处一致：plugin.py / README 版本行 / README 段落标题。"""

    def test_plugin_version_matches_readme(self):
        from pathlib import Path

        import re

        readme = Path(P.__file__).with_name("README.md").read_text(encoding="utf-8")
        readme_version = re.search(r"版本 \*\*([0-9]+(?:\.[0-9]+)*)\*\*", readme)
        self.assertIsNotNone(readme_version, "README 里没有「版本 **x**」这一行")
        self.assertEqual(
            P.plugin.version, readme_version.group(1),
            "plugin.py 版本与 README 版本行不一致",
        )
        headings = re.findall(r"^### ([0-9]+(?:\.[0-9]+)*)$", readme, re.M)
        self.assertIn(P.plugin.version, headings, "当前版本在 README 里没有更新日志段落")


if __name__ == "__main__":
    unittest.main(verbosity=2)
