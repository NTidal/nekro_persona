#!/bin/sh
# 在 NekroAgent 容器里跑本插件的测试。
#
#   docker exec <容器> sh -c 'cd <插件目录> && ./scripts/run_tests.sh'
#
# 用 stdlib 的 unittest，容器里没装 pytest 也能跑；装了 pytest 时同一套用例同样适用：
#   /app/.venv/bin/python -m pytest tests -q
set -e
cd "$(dirname "$0")/.."
exec "${PYTHON:-/app/.venv/bin/python}" -m unittest discover -s tests -v "$@"