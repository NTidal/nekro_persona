"""测试引导：把插件目录接进 sys.path 并加载插件模块。

⚠️ 本插件依赖 NekroAgent 运行时（``nekro_agent.*``），**必须在 NA 容器里跑**：

    docker exec <容器> sh -c 'cd <插件目录> && /app/.venv/bin/python -m unittest discover -s tests -v'

如果环境里装了 pytest，同一套用例可以直接跑（用例是 unittest 风格，pytest 兼容）：

    /app/.venv/bin/python -m pytest tests -q
"""
from __future__ import annotations

import sys
from contextlib import contextmanager
from pathlib import Path


def load_plugin_module():
    """导入本插件的主模块（``nekro_persona.plugin``）。"""
    tests_dir = Path(__file__).resolve().parent      # <plugin>/tests
    plugin_dir = tests_dir.parent                    # <plugin>
    for path in (str(plugin_dir.parent), str(plugin_dir)):
        if path not in sys.path:
            sys.path.insert(0, path)
    import nekro_persona  # noqa: F401  （先建包，避免 namespace package 歧义）
    import nekro_persona.plugin  # noqa: F401

    # ⚠️ 不能用 ``from nekro_persona import plugin``：包里的 ``plugin`` 属性被插件实例
    #    （NekroPlugin）覆盖了，那样拿到的是实例而不是模块。统一从 sys.modules 取模块。
    return sys.modules["nekro_persona.plugin"]


@contextmanager
def temp_config(module, **overrides):
    """临时改插件配置项，退出时一定还原（config 是全局单例，串味会污染其它用例）。"""
    cfg = module.config
    saved = {}
    for key, value in overrides.items():
        saved[key] = getattr(cfg, key, None)
        setattr(cfg, key, value)
    try:
        yield cfg
    finally:
        for key, value in saved.items():
            setattr(cfg, key, value)


def today_start(module, ts: int) -> int:
    """当日 0 点时间戳 —— 沉淀用例必须先把它写进 profile.day_key，
    否则 roll_day() 会把 day_net 归零，预算断言会失效。"""
    return module._day_start_ts(int(ts))
