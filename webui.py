"""nekro_persona 插件的 WebUI。

挂载在 `/api/plugins/{plugin_key}/`（由 `webui_path="/"` 声明）。

功能：
- 概览：档位配置、1/2 档占用、向量状态、门槛分布
- 背景记忆：查看/启停/调门槛（直接写回 backgrounds 下的 JSON）
- 好感度档案：按频道查看用户档案，可改分
- 注入预览：给定分数，预览**真实**注入文本（调参神器）

鉴权：插件路由不受 NA 全局鉴权保护，故提供可选访问密钥
（配置项 `WEBUI_ACCESS_KEY`，留空则不校验）。
"""

from __future__ import annotations

import glob
import hmac
import json
import os
import re
import shutil
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, Response

BG_DIR = Path(__file__).parent / "backgrounds"   # ⚠️ 已废弃：仅为兼容外部引用保留，内部一律走 _bg_dir()
# 合法档位（扩展：新增 tier3 世界知识）
_TIERS = ("tier1", "tier2", "tier3")


_NO_KEY_WARNED = False


async def _json_body(request: "Request") -> Dict[str, Any]:
    """解析 JSON 请求体；非 JSON / 非法 JSON 统一转 400。

    ⚠️ 12 个 POST 端点原先各自 `await request.json()`：客户端发空 body、表单编码或截断的
    JSON 时 Starlette 抛 JSONDecodeError → 整个请求 500，前端只能看到「服务器错误」。
    """
    try:
        data = await request.json()
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            status_code=400,
            detail=f"请求体必须是合法 JSON：{str(exc)[:120]}",
        ) from exc
    if not isinstance(data, dict):
        raise HTTPException(status_code=400, detail="请求体必须是 JSON 对象")
    return data


def _as_int(value: Any, default: int = 0) -> int:
    """把任意来源的值转成 int，坏值回落到 default（**绝不抛**）。

    背景记忆的 min_favor / weight 可能被手改成 "20分" 这类值。早期到处写
    `int(x or 0)`，一个坏字段就让 /api/overview、/api/entries 直接 500，
    整个管理台打不开、也没法在 UI 里修 —— 读取侧一律走这里。
    """
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    try:
        return int(float(str(value).strip()))
    except (TypeError, ValueError):
        return default


# 种子包里合法的人设文件名：不含路径分隔符、不以点开头（挡 `../` 之类穿越）
# 点开头（.hidden.json）与 "..%2f" 之类都不该被当成正规档位文件：前者是隐藏/临时文件，
# 后者是路径穿越的变形。显式用负向断言挡掉点开头。
_SAFE_TIER_FILE = re.compile(r"^(?!\.)[A-Za-z0-9_.\-\u4e00-\u9fff]+[.]json$")


def _validate_tier_entries(entries: List[Any]) -> str:
    """校验种子包条目的字段类型，返回第一处问题的描述（空串 = 通过）。

    原先只校验「entries 是列表」，一个手改的 `"min_favor": "20分"` 能被导入成功，
    随后整个 UI 打不开、也没法从 UI 里修 —— 所以在写盘前就拦住。
    """
    for i, e in enumerate(entries, 1):
        if not isinstance(e, dict):
            return f"第 {i} 条不是对象"
        for key in ("min_favor", "weight"):
            if key in e and e[key] is not None:
                v = e[key]
                ok_num = isinstance(v, (int, float)) and not isinstance(v, bool)
                ok_str = isinstance(v, str) and v.strip().lstrip("+-").isdigit()
                if not (ok_num or ok_str):
                    return f'第 {i} 条 {key}="{v}" 不是整数'
        if "enabled" in e and not isinstance(e["enabled"], bool):
            return f"第 {i} 条 enabled 不是布尔值"
        for key in ("id", "title", "content", "trigger", "file"):
            if key in e and e[key] is not None and not isinstance(e[key], str):
                return f"第 {i} 条 {key} 不是字符串"
        if "triggers" in e and e["triggers"] is not None:
            v = e["triggers"]
            if not isinstance(v, list) or any(not isinstance(t, str) for t in v):
                return f"第 {i} 条 triggers 必须是字符串数组"
    return ""
def _validate_favor_cfg(data: Any) -> str:
    # 校验 favor.json 的形状，返回第一处问题（空串 = 通过）。
    # 原先只检查 `"stages" in data`，于是 {"stages": "abc"} 也能通过并写盘；之后概览页
    # 与门控渲染都会对字符串调 .get() 抛 AttributeError，那两处不在 try 内 →
    # 整条 persona_prompt 处理链路挂掉，只能重写 gating 才能解锁。
    if not isinstance(data, dict):
        return "顶层必须是对象"
    stages = data.get("stages")
    if not isinstance(stages, list) or not stages:
        return "stages 必须是非空数组"
    for i, st in enumerate(stages, 1):
        if not isinstance(st, dict):
            return f"stages 第 {i} 项不是对象"
        if not isinstance(st.get("name", ""), str):
            return f"stages 第 {i} 项 name 不是字符串"
        mx = st.get("max")
        if mx is not None:
            ok_num = isinstance(mx, (int, float)) and not isinstance(mx, bool)
            if not ok_num:
                try:
                    int(float(str(mx)))
                except (TypeError, ValueError):
                    return f"stages 第 {i} 项 max 不是数字：{mx!r}"
        for key in ("unlock_hint", "guide", "desc"):
            if key in st and st[key] is not None and not isinstance(st[key], str):
                return f"stages 第 {i} 项 {key} 不是字符串"
    for key in ("rubric", "avoid", "scale", "decay", "lifecycle", "memo", "stage_guides"):
        if key in data and data[key] is not None and not isinstance(data[key], dict):
            return f"{key} 必须是对象"
    return ""


_TIER_LABEL = {"tier1": "1 档·身份", "tier2": "2 档·往事", "tier3": "3 档·世界知识"}


def build_router(plugin: Any, config: Any, memory: Any, helpers: Dict[str, Any]) -> APIRouter:
    """helpers: 由 plugin.py 传入的内部函数（load_state/save_state/stage_info/
    clamp_score/max_abs_score/now_ts）。不在此处 import 插件模块——
    包的 __init__ 用 `from .plugin import plugin` 遮蔽了同名子模块，
    且运行进程里模块绝对名不一定是 nekro_persona。"""
    router = APIRouter()

    # 背景记忆数据目录与注入链路共用同一份解析入口（memory 统一解析：
    # 跟随当前账号上下文 → 插件数据目录；账号目录首次使用由框架播种，
    # 包内种子由 memory._seed_pkg_files 在目录为空时补齐）。
    # ⚠️ 必须**按访问解析**：build_router 只在导入期调用一次，在这里取一次
    # `memory.bg_dir()` 会把路径固定成「导入期的共享目录」，所有账号的
    # 背景记忆编辑/删除/回收站/导入导出都会误写到共享目录 → 账号互相覆盖。
    def _bg_dir() -> Path:
        return Path(memory.bg_dir())

    def _trash_file() -> Path:
        return _bg_dir() / ".webui_trash.json"

    # ---------------- 作用域（"我在编辑谁的数据"） ----------------
    def _scope_info() -> Dict[str, Any]:
        """当前请求的账号作用域，以及**本次请求实际解析到**的背景数据目录。

        为什么必须按请求解析（v1.7.0）：
        · 账号前缀 `/plugins/{key}@{account}/` 由框架在路由依赖里
          `set_current_instance_key(account)` 绑定账号上下文，该前缀下的
          `memory.bg_dir()` → `<插件数据目录>/accounts/{account}/backgrounds`；
        · 默认前缀 `/plugins/{key}/` 没有账号上下文 → 共享目录
          `<插件数据目录>/backgrounds`（历史数据 / 新账号的播种模板）。
        一旦在导入期（build_router 只跑一次）取一次，就会把共享目录固化给所有账号，
        页面显示的作用域与实际写入的目录会不一致 —— 那正是本次要消除的误导。

        `dataDir` 取**相对插件数据目录**的 POSIX 路径（`backgrounds` 或
        `accounts/{account}/backgrounds`）：既能区分共享/账号，又不把宿主绝对
        路径暴露到浏览器。仅在数据目录解析到包内兜底目录等异常情形下才回落
        为绝对路径（此时它会以 `/` 开头，一眼可辨）。
        """
        try:
            from nekro_agent.services.account.context import get_current_instance_key

            raw_scope = get_current_instance_key()
            has_account_scoping = True
        except Exception:  # noqa: BLE001 - 老框架没有账号上下文模块 → 一律按共享处理
            raw_scope = None
            # 旧框架只有一个数据集，没有「共享/模板」概念：前端据此隐藏作用域条，
            # 否则会误报「这里的修改不会影响任何账号」——在旧框架上那份就是线上数据。
            has_account_scoping = False

        account_key: Optional[str] = None
        if raw_scope:
            try:
                from nekro_agent.services.account import normalize_account_key

                account_key = str(normalize_account_key(str(raw_scope))).strip() or None
            except Exception:  # noqa: BLE001 - 归一化不可用时退化为裸账号键
                account_key = str(raw_scope).rpartition("#")[2].strip() or None

        resolved = Path(_bg_dir())
        # 插件数据目录根：共享时 <root>/backgrounds；账号时 <root>/accounts/<key>/backgrounds
        root = resolved.parent
        if root.parent.name == "accounts":
            root = root.parent.parent
        try:
            data_dir = resolved.relative_to(root).as_posix()
        except ValueError:  # 不在插件数据目录下（包内兜底目录等）
            data_dir = str(resolved)

        return {
            "scope": {
                "kind": "account" if account_key else "shared",
                "accountKey": account_key,
                "accountScoping": has_account_scoping,
            },
            "dataDir": data_dir,
        }

    # ---------------- 鉴权 ----------------
    def _check_key(request: Request) -> None:
        """校验 WebUI 访问密钥。

        安全约束（v1.4.1）：
        · 只接受 `X-WebUI-Key` 请求头，不再接受 `?key=` 查询参数
          （查询参数会进访问日志 / 浏览器历史 / Referer，等于泄露密钥）。
        · 用 hmac.compare_digest 做常数时间比较，避免按字节短路泄露密钥长度与前缀。
        · 未配置密钥时：只读（GET）放行，写操作（非 GET）一律 403——
          防止插件路由在 NA 全局鉴权之外「裸奔」改人设记忆与人格预设。
        """
        expect = str(getattr(config, "WEBUI_ACCESS_KEY", "") or "").strip()
        got = str(request.headers.get("X-WebUI-Key") or "").strip()
        if not expect:
            # ⚠️ 未配置密钥时**不再只读放行**：插件路由挂在 NA 全局鉴权之外，
            #    放行 GET 等于任何能打开 NA WebUI 的人都能把全部人设记忆、关系档案、
            #    事件史整份拉走。现在默认拒绝并给出可操作的提示；
            #    页面壳（GET /）仍然公开，否则首次打开就是死页、连设密钥的入口都没有。
            global _NO_KEY_WARNED
            if not _NO_KEY_WARNED:
                _NO_KEY_WARNED = True
                plugin.logger.warning(
                    "[persona] 未配置 WEBUI_ACCESS_KEY：管理页的所有 /api 请求都会被拒绝（401）。"
                    "请在插件配置里设置访问密钥。"
                )
            raise HTTPException(
                status_code=401,
                detail="未配置 WEBUI_ACCESS_KEY：管理接口默认拒绝访问，请先在插件配置里设置访问密钥",
            )
        # ⚠️ hmac.compare_digest 对含非 ASCII 的两个 str 会抛
        #    TypeError: comparing strings with non-ASCII characters ...
        #    密钥里带汉字很常见，那会让**每个请求（含只读）**都 500。统一按 bytes 比。
        if not hmac.compare_digest(got.encode("utf-8"), expect.encode("utf-8")):
            raise HTTPException(status_code=401, detail="访问密钥不正确")

    # ---------------- 数据读写 ----------------
    # JSON 写入只允许在完整解析、持有进程锁并完成备份后进行。
    # 解析失败时 fail closed，绝不把坏文件当成空配置继续覆盖。
    _WRITE_LOCK = threading.RLock()

    def _entry_files(tier: str) -> List[Path]:
        return sorted(Path(p) for p in glob.glob(str(_bg_dir() / tier / "*.json")))

    def _read_json_strict(fp: Path) -> Optional[Dict[str, Any]]:
        if not fp.exists():
            return None
        try:
            data = json.loads(fp.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001
            raise ValueError(f"JSON 文件损坏：{fp.name}（{exc}）") from exc
        if not isinstance(data, dict):
            raise ValueError(f"JSON 根节点必须是对象：{fp.name}")
        return data

    def _read_entries(tier: str) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        for fp in _entry_files(tier):
            data = _read_json_strict(fp)
            if data is None:
                continue
            entries = data.get("entries", [])
            if not isinstance(entries, list):
                raise ValueError(f"背景文件的 entries 必须是数组：{fp.name}")
            for item in entries:
                if not isinstance(item, dict):
                    raise ValueError(f"背景文件存在非对象条目：{fp.name}")
                entry = dict(item)
                entry["_file"] = fp.name
                entry["_tier"] = tier
                out.append(entry)
        return out

    def _text_length(value: Any) -> int:
        return len(str(value or ""))

    def _entry_render_chars(entry: Dict[str, Any]) -> int:
        content = _text_length(entry.get("content"))
        trigger = _text_length(entry.get("trigger"))
        return len(f"· {trigger}\n  {content}") if trigger else len(f"· {content}")

    # 可写字段（扩展：上游只允许 min_favor/enabled/weight）
    # ⚠️ 必须与 api_entry_update 里接受的字段一一对应：漏一个就会出现
    #    「接口接受 + 回显成功 + 磁盘从未写入」的假成功（familiarity 就是这么丢的）。
    _WRITABLE = ("min_favor", "enabled", "weight", "title", "trigger", "triggers",
                 "content", "familiarity")

    def _dump(fp: Path, data: Dict[str, Any]) -> None:
        """带备份的原子 JSON 写入。"""
        with _WRITE_LOCK:
            fp.parent.mkdir(parents=True, exist_ok=True)
            if fp.exists():
                backup = fp.with_name(fp.name + ".bak")
                shutil.copy2(fp, backup)
            fd, tmp_name = tempfile.mkstemp(
                prefix=f".{fp.name}.",
                suffix=".tmp",
                dir=str(fp.parent),
            )
            try:
                with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
                    json.dump(data, handle, ensure_ascii=False, indent=2)
                    handle.write("\n")
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(tmp_name, fp)
                # rename 之后还要 fsync 目录才算真正落盘，否则掉电时可能出现
                # 「数据已写、目录项仍指向旧 inode」。Windows 不支持目录 fsync，忽略即可。
                try:
                    _dfd = os.open(str(fp.parent), os.O_RDONLY)
                    try:
                        os.fsync(_dfd)
                    finally:
                        os.close(_dfd)
                except OSError:
                    pass
            finally:
                try:
                    os.unlink(tmp_name)
                except FileNotFoundError:
                    pass

    def _entry_locations(tier: str, entry_id: str) -> List[tuple[Path, Dict[str, Any], Dict[str, Any]]]:
        locations: List[tuple[Path, Dict[str, Any], Dict[str, Any]]] = []
        for fp in _entry_files(tier):
            data = _read_json_strict(fp)
            if data is None:
                continue
            entries = data.get("entries", [])
            if not isinstance(entries, list):
                raise ValueError(f"背景文件的 entries 必须是数组：{fp.name}")
            for item in entries:
                if isinstance(item, dict) and str(item.get("id", "")) == str(entry_id):
                    locations.append((fp, data, item))
        return locations

    def _write_entry(tier: str, entry_id: str, patch: Dict[str, Any]) -> bool:
        """就地修改 JSON 里的某条；解析失败或 ID 跨文件重复时拒绝写入。"""
        with _WRITE_LOCK:
            locations = _entry_locations(tier, entry_id)
            if len(locations) > 1:
                raise ValueError(f"记忆 ID 重复：{entry_id}，请先处理重复条目")
            if not locations:
                return False
            fp, data, entry = locations[0]
            for key in _WRITABLE:
                if key in patch:
                    entry[key] = patch[key]
            _dump(fp, data)
            return True

    def _delete_entry(tier: str, entry_id: str) -> Optional[Dict[str, Any]]:
        """删除前写入回收站；文件空了就删掉文件。"""
        with _WRITE_LOCK:
            locations = _entry_locations(tier, entry_id)
            if len(locations) > 1:
                raise ValueError(f"记忆 ID 重复：{entry_id}，已拒绝删除")
            if not locations:
                return None
            fp, data, target = locations[0]
            trash_record = {
                "trash_id": uuid.uuid4().hex,
                "state": "pending",
                "tier": tier,
                "file": fp.name,
                "deleted_at": int(helpers["now_ts"]()),
                "entry": dict(target),
                "source_document": json.loads(json.dumps(data, ensure_ascii=False)),
            }
            records = _read_trash()
            records.append(trash_record)
            _write_trash(records)
            entries = data.get("entries", [])
            data["entries"] = [item for item in entries if item is not target]
            if data["entries"]:
                _dump(fp, data)
            else:
                shutil.copy2(fp, fp.with_name(fp.name + ".bak"))
                fp.unlink(missing_ok=True)
            trash_record["state"] = "committed"
            records[-1]["state"] = "committed"
            _write_trash(records)
            return trash_record

    def _tier1_cfg() -> Dict[str, Any]:
        """1 档运行参数（来自插件配置）。"""
        return {
            "max_chars": int(getattr(config, "TIER1_MAX_CHARS", 2000) or 2000),
        }

    def _lifecycle_cfg() -> Dict[str, Any]:
        """生命周期（热度排序）参数，与 plugin._lifecycle_cfg 同口径。

        ⚠️ 仅 None 才回落默认（不用 `x or default`），否则作者显式设的 0 会被吞。
        """
        def _n(key: str, default, cast):
            v = getattr(config, key, None)
            return cast(default) if v is None else cast(v)

        return {
            "enabled": bool(getattr(config, "LIFECYCLE_ENABLED", False)),
            "half_life_days": _n("LIFECYCLE_HALF_LIFE_DAYS", 30, int),
            "floor": _n("LIFECYCLE_FLOOR", 0.6, float),
            "protect_weight": _n("LIFECYCLE_PROTECT_WEIGHT", 4, int),
            "reinforce_bonus": _n("LIFECYCLE_REINFORCE_BONUS", 10, int) / 100.0,
        }

    def _tier2_cfg() -> Dict[str, Any]:
        """2 档运行参数（来自插件配置）。"""
        return {
            "max_entries": int(getattr(config, "TIER2_MAX_ENTRIES", 2) or 2),
            "max_chars": int(getattr(config, "TIER2_MAX_CHARS", 500) or 500),
            "threshold": float(getattr(config, "TIER2_SIM_THRESHOLD", 0.35) or 0.35),
            "scan_msgs": int(getattr(config, "TIER2_SCAN_MSGS", 6) or 6),
            "lifecycle": _lifecycle_cfg(),
        }

    def _t3_cfg() -> Dict[str, Any]:
        """3 档运行参数（来自插件配置，扩展）。"""
        return {
            "enabled": bool(getattr(config, "TIER3_ENABLED", True)),
            "max_entries": int(getattr(config, "TIER3_MAX_ENTRIES", 3) or 3),
            "max_chars": int(getattr(config, "TIER3_MAX_CHARS", 700) or 700),
            "threshold": float(getattr(config, "TIER3_SIM_THRESHOLD", 0.35) or 0.35),
            "scan_msgs": int(getattr(config, "TIER3_SCAN_MSGS", 6) or 6),
            "lifecycle": _lifecycle_cfg(),
        }

    def _new_entry(tier: str, entry: Dict[str, Any]) -> str:
        """新建条目；ID 在当前档位所有 JSON 文件中必须唯一。"""
        with _WRITE_LOCK:
            requested_id = str(entry.get("id") or "").strip()
            if requested_id and _entry_locations(tier, requested_id):
                raise ValueError(f"记忆 ID 已存在：{requested_id}，请使用编辑")
            eid = requested_id
            if not eid:
                for _ in range(10):
                    candidate = f"custom_{uuid.uuid4().hex[:12]}"
                    if not _entry_locations(tier, candidate):
                        eid = candidate
                        break
                if not eid:
                    raise ValueError("无法生成唯一记忆 ID，请重试")
            new_entry = dict(entry)
            new_entry["id"] = eid
            new_entry.setdefault("weight", 1)
            new_entry.setdefault("min_favor", 0)
            new_entry.setdefault("enabled", True)
            new_entry.setdefault("title", "")
            new_entry.setdefault("content", "")
            if tier == "tier1":
                new_entry.setdefault("trigger", "")
            else:
                new_entry.setdefault("triggers", [])
            if tier == "tier3":
                new_entry.setdefault("familiarity", "")
            fp = _bg_dir() / tier / "custom.json"
            data = _read_json_strict(fp)
            if data is None:
                data = {
                    "character": "custom",
                    "tier": tier,
                    "note": "由 WebUI 新建的条目（扩展）",
                    "entries": [],
                }
            entries = data.setdefault("entries", [])
            if not isinstance(entries, list):
                raise ValueError("custom.json 的 entries 必须是数组")
            if any(isinstance(item, dict) and str(item.get("id", "")) == eid for item in entries):
                raise ValueError(f"记忆 ID 已存在：{eid}")
            entries.append(new_entry)
            _dump(fp, data)
            return eid

    def _chat_kind(ck: str) -> str:
        """频道类型：group / private / other（扩展）。

        覆盖 onebot_v11-group_xxx / onebot_v11-private_xxx / qqoc-group:xxx 等格式。
        """
        s = str(ck or "").lower()
        if "group" in s:
            return "group"
        if "private" in s:
            return "private"
        return "other"

    def _read_gating() -> Dict[str, Any]:
        data = _read_json_strict(_bg_dir() / "favor.json")
        return data or {}

    def _write_gating(patch: Dict[str, Any]) -> None:
        data = _read_gating()
        for key in (
            "enabled", "stages", "avoid", "rubric",
            "stage_guides", "stage_guide_common",   # 扩展：分档互动指引
        ):
            if key in patch:
                data[key] = patch[key]
        _dump(_bg_dir() / "favor.json", data)

    # 回收站与背景数据同目录，按当前账号上下文解析（不要在此捕获固定路径）
    def _read_trash() -> List[Dict[str, Any]]:
        data = _read_json_strict(_trash_file())
        if data is None:
            return []
        items = data.get("items", [])
        if not isinstance(items, list):
            raise ValueError("WebUI 回收站格式损坏：items 必须是数组")
        return [dict(item) for item in items if isinstance(item, dict)]

    def _write_trash(items: List[Dict[str, Any]]) -> None:
        _dump(_trash_file(), {"version": 1, "items": items})

    def _trash_summary(item: Dict[str, Any]) -> Dict[str, Any]:
        entry = item.get("entry") or {}
        return {
            "trash_id": str(item.get("trash_id") or ""),
            "tier": str(item.get("tier") or ""),
            "file": str(item.get("file") or ""),
            "deleted_at": int(item.get("deleted_at") or 0),
            "state": str(item.get("state") or "committed"),
            "id": str(entry.get("id") or ""),
            "title": str(entry.get("title") or entry.get("topic") or entry.get("id") or "未命名条目"),
            "content": str(entry.get("content") or ""),
        }

    def _restore_trash_item(trash_id: str) -> Dict[str, Any]:
        with _WRITE_LOCK:
            records = _read_trash()
            record = next((item for item in records if str(item.get("trash_id")) == str(trash_id)), None)
            if record is None:
                raise ValueError("回收站记录不存在或已恢复")
            tier = str(record.get("tier") or "")
            if tier not in _TIERS:
                raise ValueError("回收站记录档位无效")
            entry = record.get("entry")
            if not isinstance(entry, dict) or not str(entry.get("id") or "").strip():
                raise ValueError("回收站记录内容无效")
            entry = dict(entry)
            entry_id = str(entry["id"])
            if _entry_locations(tier, entry_id):
                raise ValueError(f"记忆 ID 已存在，无法恢复：{entry_id}")
            file_name = str(record.get("file") or "custom.json")
            if Path(file_name).name != file_name or not file_name.endswith(".json"):
                raise ValueError("回收站记录文件路径无效")
            fp = _bg_dir() / tier / file_name
            data = _read_json_strict(fp)
            if data is None:
                # ⚠️ 目标文件已不存在时，把整份 source_document 搬回来会把当时同文件里的
                #    其他条目一起「复活」——那些条目可能后来被单独删除且没有回收站记录
                #   （删 X、删 Y（文件清空被删）、再恢复 X → Y 静默回归）。
                #    只保留文档头（character/tier 等元信息），entries 从空开始。
                source_document = record.get("source_document")
                header = {
                    k: json.loads(json.dumps(v, ensure_ascii=False))
                    for k, v in (source_document or {}).items()
                    if k != "entries"
                } if isinstance(source_document, dict) else {}
                header.setdefault("character", "restored")
                header.setdefault("tier", tier)
                data = {**header, "entries": []}
            entries = data.setdefault("entries", [])
            if not isinstance(entries, list):
                raise ValueError("恢复目标文件的 entries 必须是数组")
            data["entries"] = [item for item in entries if not (isinstance(item, dict) and str(item.get("id", "")) == entry_id)]
            entries = data["entries"]
            records = [item for item in records if str(item.get("trash_id")) != str(trash_id)]
            record["state"] = "restoring"
            _write_trash(records + [record])
            entries.append(entry)
            _dump(fp, data)
            _write_trash([item for item in records if str(item.get("trash_id")) != str(trash_id)])
            return {"tier": tier, "id": entry_id}

    def _remove_trash_item(trash_id: str) -> bool:
        with _WRITE_LOCK:
            records = _read_trash()
            remaining = [item for item in records if str(item.get("trash_id")) != str(trash_id)]
            if len(remaining) == len(records):
                return False
            _write_trash(remaining)
            return True

    def _public_detail(text: str) -> str:
        """把宿主机绝对路径换成占位符。

        这些 detail 会原样回给浏览器（前端直接 toast 出来）：一旦是 OSError
        （权限/只读/磁盘满），消息里就是「宿主机插件数据目录 + 文件名」这种绝对路径，
        既泄部署结构又对用户毫无意义。
        """
        out = str(text or "")
        try:
            _bases = {str(_bg_dir()), str(_bg_dir().parent), str(_bg_dir().parent.parent)}
        except Exception:  # noqa: BLE001
            _bases = set()
        for _p in sorted((b for b in _bases if b and len(b) > 3), key=len, reverse=True):
            out = out.replace(_p, "<数据目录>")
        return out

    def _data_error(exc: Exception) -> HTTPException:
        """数据层异常 → 合适的 HTTP 状态码。

        ⚠️ 旧实现用 `"ID" in message` 猜状态码：回收站的「记录不存在」等错误既不含 ID
        也不含档位 → 一律 500，前端把「找不到」当成「服务器炸了」，用户只会反复重试。
        """
        message = str(exc)
        if any(k in message for k in ("不存在", "找不到")):
            status = 404
        elif any(k in message for k in ("已存在", "重复", "ID", "档位")):
            status = 409
        else:
            # 其余都是「数据/参数不合法」（格式错、缺字段、越界）→ 400 更准确
            status = 400
        return HTTPException(status_code=status, detail=_public_detail(message))

    async def _reload_after_write() -> Dict[str, Any]:
        try:
            await memory.ensure_loaded(force=True)
            return {"reloaded": True}
        except Exception as exc:  # noqa: BLE001
            plugin.logger.exception("[persona] WebUI 写入成功但运行时重载失败")
            return {"reloaded": False, "reload_error": str(exc)[:240]}

    async def _all_channels() -> List[Dict[str, Any]]:
        from nekro_agent.models.db_plugin_data import DBPluginData

        rows = await DBPluginData.filter(plugin_key=plugin.key).all()
        out: List[Dict[str, Any]] = []
        for r in rows:
            ck = r.target_chat_key or ""
            try:
                d = json.loads(r.data_value or "{}")
            except Exception:  # noqa: BLE001
                d = {}
            profs = d.get("profiles") or {}
            scores: List[int] = []
            for profile in profs.values():
                if not isinstance(profile, dict):
                    continue
                try:
                    scores.append(int(profile.get("score", 0) or 0))
                except (TypeError, ValueError):
                    continue
            out.append(
                {
                    "chat_key": ck,
                    "count": len(profs),
                    "top": max(scores) if scores else 0,
                    "last_active": d.get("last_active_user_id") or "",
                },
            )
        out.sort(key=lambda x: (-x["count"], x["chat_key"]))
        return out

    # ---------------- 页面 ----------------
    @router.get("/", response_class=HTMLResponse, summary="人格记忆 管理页")
    async def index() -> HTMLResponse:
        # ⚠️ 必须禁止缓存：页面里的 JS 直接决定发给后端的 JSON 形状。
        #    一旦浏览器跑着**旧页面**、后端却是**新契约**，就会把结构化数据写坏
        #    （实测发生过两次：rubric.items 被写成逗号串、rubric.rules 被逐字拆开）。
        #    这里返回 no-store，保证每次打开都是与后端匹配的那一版。
        return HTMLResponse(
            content=PAGE_HTML,
            headers={
                "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
                "Pragma": "no-cache",
                "Expires": "0",
            },
        )

    # ---------------- API ----------------
    @router.get("/api/overview", summary="概览")
    async def api_overview(request: Request) -> Dict[str, Any]:
        _check_key(request)
        # 作用域与数据目录：**按本次请求**解析（见 _scope_info 的说明）。
        # 放在 ensure_loaded 之前也无妨：两者都走 memory.bg_dir() 的同一份上下文。
        scope_info = _scope_info()
        try:
            await memory.ensure_loaded()
            cfg = memory.load_favor_cfg()
        except ValueError as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc

        def _ov_num(key: str, default, cast):
            """读配置数值：仅 None/缺失才回落默认，honor 显式 0（与运行期同口径）。"""
            v = getattr(config, key, None)
            if v is None:
                return cast(default)
            try:
                return cast(v)
            except (TypeError, ValueError):
                return cast(default)

        def _dir_status() -> Dict[str, Any]:
            """数据目录解析状态（memory + tier4 + 状态缓存规模）。

            v2.0.2：目录解析失败时插件会回退**包内演示数据**运行，这是安全优先的
            降级；旧实现只打一次日志、且本进程内不再重试，属于静默故障。
            这里把降级暴露到概览页，作者一眼能看出「人设为什么变回了演示内容」。
            任何异常都不允许拖垮概览页。
            """
            out: Dict[str, Any] = {}
            try:
                out["memory"] = memory.bg_dir_status()
            except Exception as exc:  # noqa: BLE001
                out["memory"] = {"error": str(exc)}
            try:
                t4 = helpers.get("tier4")
                out["tier4"] = t4.bg_dir_status() if t4 is not None else {}
            except Exception as exc:  # noqa: BLE001
                out["tier4"] = {"error": str(exc)}
            try:
                out["stateCache"] = memory.state_cache_stats()
            except Exception as exc:  # noqa: BLE001
                out["stateCache"] = {"error": str(exc)}
            out["degraded"] = bool(
                (out.get("memory") or {}).get("degraded")
                or (out.get("tier4") or {}).get("degraded")
            )
            return out

        t1, t2 = memory._state()["tier1"], memory._state()["tier2"]
        t3 = memory._state().get("tier3", [])
        vecs3 = memory._state().get("tier3_vecs", [])
        t1s = memory.tier1_settings(_tier1_cfg())
        t2s = memory.tier2_settings(_tier2_cfg())
        t3s = memory.tier3_settings(_t3_cfg())
        t1_chars = sum(
            len(str(e.get("content", ""))) + len(str(e.get("trigger", ""))) for e in t1
        )
        t1_rendered_chars = sum(_entry_render_chars(e) for e in t1)
        t1_limit = int(t1s["max_chars"])
        vecs = memory._state()["tier2_vecs"]

        def _dist(entries: List[Dict[str, Any]]) -> Dict[str, int]:
            d: Dict[str, int] = {}
            for e in entries:
                k = str(_as_int(e.get("min_favor"), 0))
                d[k] = d.get(k, 0) + 1
            return dict(sorted(d.items(), key=lambda kv: int(kv[0])))

        return {
            "plugin": {
                "key": plugin.key,
                "name": plugin.name,
                "version": plugin.version,
                "enabled": plugin.is_enabled,
                "allow_sleep": plugin.allow_sleep,
            },
            # 作用域可见性（v1.7.0）：账号作用域 = 该账号自己的副本；
            # 共享作用域 = 历史/模板目录，**在它上面的修改不会影响任何账号**。
            # 前端顶部的"作用域条"据此渲染，账号名与路径都不写死。
            "scope": scope_info["scope"],
            "dataDir": scope_info["dataDir"],
            "tier1": {
                "count": len(t1),
                "chars": t1_chars,
                "rendered_chars": t1_rendered_chars,
                "limit": t1_limit,
                "remaining": max(0, t1_limit - t1_rendered_chars),
                "over_budget": t1_rendered_chars > t1_limit,
            },
            "tier2": {
                "count": len(t2),
                "vectors": sum(1 for v in vecs if v),
                "threshold": t2s["threshold"],
                "max_entries": t2s["max_entries"],
                "max_chars": t2s["max_chars"],
                "scan_msgs": t2s["scan_msgs"],
            },
            "tier3": {
                "count": len(t3),
                "vectors": sum(1 for v in vecs3 if v),
                "threshold": t3s["threshold"],
                "max_entries": t3s["max_entries"],
                "max_chars": t3s["max_chars"],
                "scan_msgs": t3s["scan_msgs"],
                "enabled": t3s["enabled"],
                "gated": False,
                "label": _TIER_LABEL["tier3"],
            },
            "gating": {
                "enabled": bool(cfg.get("enabled", True)),
                "stages": cfg.get("stages") or [],
                "rubric": cfg.get("rubric") or {},
                "avoid": cfg.get("avoid") or {},
                # 扩展：分档互动指引（上游只有未进 prompt 的 unlock_hint）
                "stage_guides": cfg.get("stage_guides") or {},
                "stage_guide_common": cfg.get("stage_guide_common") or "",
                "dist_tier1": _dist(t1),
                "dist_tier2": _dist(t2),
            },
            "last_hit": memory._state().get("last_hit", ""),
            # 写权限状态（v1.4.2）：未配置 WEBUI_ACCESS_KEY 时后端一律 403 拒绝写操作
            # （v1.4.1 安全约束：插件路由不受 NA 全局鉴权保护，匿名不得改人设）。
            # 前端据此把「保存」按钮置灰并给出可操作提示，而不是等提交后才吃 403。
            "writable": bool(str(getattr(config, "WEBUI_ACCESS_KEY", "") or "").strip()),
            "score_limit": {"max_abs": helpers["max_abs_score"]()},
            "scale": {
                "mode": int(getattr(config, "FAVOR_SCALE_MODE", 2) or 2),
                "presets": {
                    str(k): {"label": v.get("label", str(k)), "desc": v.get("desc", "")}
                    for k, v in (helpers.get("favor_scale_presets") or {}).items()
                },
            },
            "decay": {
                "enabled": bool(getattr(config, "DECAY_ENABLED", True)),
                "interval_hours": int(getattr(config, "DECAY_INTERVAL_HOURS", 8)),
                "percent": int(getattr(config, "DECAY_PERCENT", 5)),
                "keep_tier": bool(getattr(config, "DECAY_KEEP_TIER", True)),
                # 扩展：有期限的档位保底
                "tier_grace_hours": int(getattr(config, "DECAY_TIER_GRACE_HOURS", 168)),
                # 扩展：负分回升（镜像衰减）
                "recover_enabled": bool(getattr(config, "RECOVER_ENABLED", True)),
                "recover_interval_hours": int(getattr(config, "RECOVER_INTERVAL_HOURS", 12)),
                "recover_percent": int(getattr(config, "RECOVER_PERCENT", 5)),
            },
            "memo": {
                "enabled": bool(getattr(config, "MEMO_ENABLED", True)),
                "max_items": int(getattr(config, "MEMO_MAX_ITEMS", 80) or 80),
                "max_content": int(getattr(config, "MEMO_MAX_CONTENT_CHARS", 400) or 400),
                "inject_recent": int(getattr(config, "MEMO_INJECT_RECENT", 2) or 0),
                "inject_matched": int(getattr(config, "MEMO_INJECT_MATCHED", 3) or 0),
                "inject_chars": int(getattr(config, "MEMO_INJECT_CHARS", 900) or 900),
                "dedup_min_len": max(2, int(getattr(config, "MEMO_DEDUP_MIN_KEY_LEN", 2) or 2)),
            },
            "guard": {
                "patterns": 1,
                "desc": "索要好感度的正向调整会被拒绝",
            },
            # 记忆生命周期（A2/A3，1.5.0）：前端「高级设置」面板读写
            # ⚠️ 与运行期同源：仅 None 才回落默认（不用 `x or default`），
            # 否则作者显式设的 protect_weight=0 / reinforce_bonus=0 会被吞成默认，
            # 概览显示 0、运行期却按 4/0.1 生效 → 面板看到的与引擎实际用的对不上。
            "lifecycle": {
                "enabled": bool(getattr(config, "LIFECYCLE_ENABLED", False)),
                "half_life_days": _ov_num("LIFECYCLE_HALF_LIFE_DAYS", 30, int),
                "floor": _ov_num("LIFECYCLE_FLOOR", 0.6, float),
                "protect_weight": _ov_num("LIFECYCLE_PROTECT_WEIGHT", 4, int),
                "reinforce_bonus": _ov_num("LIFECYCLE_REINFORCE_BONUS", 10, int),
            },
            # 独立嵌入后端（v1.6.0）：API Key **不回传明文**，只给 has_key，
            # 前端留空即「不修改」，避免密钥泄漏到浏览器/概览响应里。
            # v1.6.3：并入 embed_health() 的降级状态——custom 端点连续失败时
            # degraded=true，概览页据此告警「检索已降级为纯关键词」。
            "embed": {
                "provider": str(getattr(config, "EMBED_PROVIDER", "na") or "na"),
                "base_url": str(getattr(config, "EMBED_BASE_URL", "") or ""),
                "model": str(getattr(config, "EMBED_MODEL", "") or ""),
                "dimensions": _ov_num("EMBED_DIMENSIONS", 0, int),
                "batch_size": _ov_num("EMBED_BATCH_SIZE", 10, int),
                "has_key": bool(str(getattr(config, "EMBED_API_KEY", "") or "").strip()),
                **memory.embed_health(),
            },
            # v2.0.2：数据目录降级可见化（见 _dir_status 的说明）。
            "dirStatus": _dir_status(),
        }

    @router.get("/api/entries", summary="背景记忆条目")
    async def api_entries(request: Request) -> Dict[str, Any]:
        _check_key(request)
        try:
            await memory.ensure_loaded()
            cfg = memory.load_favor_cfg()
        except ValueError as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        stages = cfg.get("stages") or []

        def _stage_of(score: int) -> str:
            for st in stages:
                try:
                    if score <= int(st.get("max", 999)):
                        return str(st.get("name", ""))
                except (TypeError, ValueError):
                    continue
            return ""

        # 热度标识（v2.0.3）：供列表在标题后标 🔥。
        # 判定：生命周期开启 **且** 该条目真的被命中过（hits > 0）。
        # 为什么加 hits > 0 这一条：热度开启后，从未命中的条目一律落在 floor（0.6），
        # 若按「heat != 1.0」标记，286 条会全部挂 🔥，标识就没有信息量了。
        _heat_snap = memory.hit_stats_snapshot()
        _heat_now = time.time()
        try:
            _lc_t2 = memory.lifecycle_settings(_tier2_cfg().get("lifecycle"))
        except Exception:  # noqa: BLE001
            _lc_t2 = {"enabled": False, "half_life_days": 30, "floor": 0.6,
                      "protect_weight": 4, "reinforce_bonus": 10}
        try:
            _lc_t3 = memory.lifecycle_settings(_t3_cfg().get("lifecycle"))
        except Exception:  # noqa: BLE001
            _lc_t3 = {"enabled": False, "half_life_days": 30, "floor": 0.6,
                      "protect_weight": 4, "reinforce_bonus": 10}

        def _heat_of(e: Dict[str, Any], tier: str) -> Dict[str, Any]:
            """该条目的热度信息。tier1 不参与热度排序，恒为关闭。"""
            if tier == "tier2":
                lc, num = _lc_t2, 2
            elif tier == "tier3":
                lc, num = _lc_t3, 3
            else:
                return {"hits": 0, "heat": 1.0, "heat_enabled": False,
                        "heat_on": False}
            eid = str(e.get("id", "") or "").strip()
            stat = _heat_snap.get(memory._hit_key(num, eid)) or {}
            try:
                hits = _as_int(stat.get("hits"), 0)
            except (TypeError, ValueError):
                hits = 0
            try:
                heat = round(float(memory.heat_factor(stat, _heat_now, lc)), 3)
            except Exception:  # noqa: BLE001
                heat = 1.0
            enabled = bool(lc.get("enabled"))
            return {
                "hits": hits,
                "heat": heat,
                "heat_enabled": enabled,
                "heat_on": bool(enabled and hits > 0),
            }

        def _deco(items: List[Dict[str, Any]], tier: str) -> List[Dict[str, Any]]:
            out = []
            for e in items:
                mf = _as_int(e.get("min_favor"), 0)
                out.append(
                    {
                        "tier": tier,
                        "id": e.get("id", ""),
                        "title": e.get("title") or e.get("topic") or "",
                        "weight": _as_int(e.get("weight"), 1),
                        "min_favor": mf,
                        "stage": "公开" if mf <= 0 else _stage_of(mf),
                        "enabled": bool(e.get("enabled", True)),
                        "trigger": e.get("trigger", ""),
                        "triggers": e.get("triggers") or [],
                        "familiarity": e.get("familiarity", ""),
                        "content": e.get("content", ""),
                        "content_chars": _text_length(e.get("content")),
                        "trigger_chars": _text_length(e.get("trigger")) + sum(_text_length(x) for x in (e.get("triggers") or [])),
                        "rendered_chars": _entry_render_chars(e) if tier == "tier1" else _text_length(e.get("content")),
                        "file": e.get("_file", ""),
                        **_heat_of(e, tier),
                    },
                )
            return out

        try:
            tier1_entries = _read_entries("tier1")
            tier2_entries = _read_entries("tier2")
            tier3_entries = _read_entries("tier3")
        except ValueError as exc:
            raise _data_error(exc) from exc
        return {
            "tier1": _deco(tier1_entries, "tier1"),
            "tier2": _deco(tier2_entries, "tier2"),
            "tier3": _deco(tier3_entries, "tier3"),
            "tier3_meta": {
                "gated": False,
                "label": _TIER_LABEL["tier3"],
                "familiarity_options": ["熟人", "听说", "仅传闻"],
            },
            # 带上阈值，前端用来做「门槛对照」提示（扩展）
            # favor.json 被手改坏时（max 写成 "20分"、档位不是对象）这里会直接 500，
            # 而这一页正是用户改档位的地方 —— 于是「打不开 → 改不了」死锁。
            "stages": [
                {"name": str(st.get("name") or ""), "max": _as_int(st.get("max"), 0)}
                for st in stages
                if isinstance(st, dict) and st.get("name") != "排斥"
            ],
        }

    @router.post("/api/entry", summary="修改条目（门槛/启停/权重）")
    async def api_entry_update(request: Request) -> Dict[str, Any]:
        _check_key(request)
        body = await _json_body(request)
        tier = str(body.get("tier") or "")
        eid = str(body.get("id") or "")
        if tier not in _TIERS or not eid:
            raise HTTPException(status_code=400, detail="tier/id 无效")
        # 删除
        if body.get("op") == "delete":
            try:
                deleted = _delete_entry(tier, eid)
            except ValueError as exc:
                raise _data_error(exc) from exc
            if not deleted:
                raise HTTPException(status_code=404, detail=f"找不到条目 {eid}")
            reload_state = await _reload_after_write()
            return {
                "ok": True,
                "persisted": True,
                "id": eid,
                "deleted": True,
                "trash_id": deleted.get("trash_id"),
                **reload_state,
            }

        patch: Dict[str, Any] = {}
        if "min_favor" in body:
            try:
                min_favor = int(body["min_favor"])
            except (TypeError, ValueError) as exc:
                raise HTTPException(status_code=400, detail="披露门槛必须是整数") from exc
            patch["min_favor"] = max(0, min_favor)
        if "enabled" in body:
            patch["enabled"] = bool(body["enabled"])
        if "weight" in body:
            try:
                weight = int(body["weight"])
            except (TypeError, ValueError) as exc:
                raise HTTPException(status_code=400, detail="权重必须是整数") from exc
            if weight < 1 or weight > 999:
                raise HTTPException(status_code=400, detail="权重必须在 1 到 999 之间")
            patch["weight"] = weight
        # 以下为扩展：触发与内容的可编辑
        if "title" in body:
            patch["title"] = str(body["title"])[:80]
        if "content" in body:
            patch["content"] = str(body["content"])[:2000]
        if "trigger" in body:
            patch["trigger"] = str(body["trigger"])[:200]
        if "triggers" in body:
            raw = body["triggers"]
            if isinstance(raw, str):
                raw = [x.strip() for x in raw.replace("，", ",").split(",")]
            patch["triggers"] = [str(x)[:24] for x in (raw or []) if str(x).strip()][:30]
        if "familiarity" in body:   # 扩展：3 档熟悉度分层
            fam = str(body["familiarity"])[:12]
            if fam and fam not in ("熟人", "听说", "仅传闻"):
                raise HTTPException(
                    status_code=400, detail="熟悉度只能是 熟人 / 听说 / 仅传闻",
                )
            patch["familiarity"] = fam
        if "content" in patch and not str(patch["content"]).strip():
            raise HTTPException(status_code=400, detail="记忆内容不能为空")
        if "title" in patch and not str(patch["title"]).strip():
            raise HTTPException(status_code=400, detail="标题不能为空")
        if "min_favor" in patch and patch["min_favor"] < 0:
            raise HTTPException(status_code=400, detail="披露门槛不能小于 0")
        if not patch:
            raise HTTPException(status_code=400, detail="没有要修改的字段")
        try:
            written = _write_entry(tier, eid, patch)
        except ValueError as exc:
            raise _data_error(exc) from exc
        if not written:
            raise HTTPException(status_code=404, detail=f"找不到条目 {eid}")
        reload_state = await _reload_after_write()
        return {"ok": True, "persisted": True, "id": eid, "patch": patch, **reload_state}

    @router.get("/api/trash", summary="背景记忆回收站")
    async def api_trash(request: Request) -> Dict[str, Any]:
        _check_key(request)
        try:
            records = [_trash_summary(item) for item in _read_trash()]
        except ValueError as exc:
            raise _data_error(exc) from exc
        records.sort(key=lambda item: item.get("deleted_at", 0), reverse=True)
        return {"items": records}

    @router.post("/api/trash/restore", summary="恢复回收站条目")
    async def api_trash_restore(request: Request) -> Dict[str, Any]:
        _check_key(request)
        body = await _json_body(request)
        trash_id = str(body.get("trash_id") or "")
        if not trash_id:
            raise HTTPException(status_code=400, detail="缺少 trash_id")
        try:
            restored = _restore_trash_item(trash_id)
        except (ValueError, OSError) as exc:
            raise _data_error(exc) from exc
        reload_state = await _reload_after_write()
        return {"ok": True, "persisted": True, **restored, **reload_state}

    @router.post("/api/trash/delete", summary="永久删除回收站条目")
    async def api_trash_delete(request: Request) -> Dict[str, Any]:
        _check_key(request)
        body = await _json_body(request)
        trash_id = str(body.get("trash_id") or "")
        if not trash_id:
            raise HTTPException(status_code=400, detail="缺少 trash_id")
        try:
            removed = _remove_trash_item(trash_id)
        except (ValueError, OSError) as exc:
            raise _data_error(exc) from exc
        if not removed:
            raise HTTPException(status_code=404, detail="回收站记录不存在")
        return {"ok": True, "deleted": True}

    @router.get("/api/channels", summary="有档案的频道")
    async def api_channels(request: Request) -> Dict[str, Any]:
        _check_key(request)
        return {"channels": await _all_channels()}

    @router.get("/api/index", summary="全部档案索引（按群聊/私聊分类，支持搜索）")
    async def api_index(request: Request) -> Dict[str, Any]:
        _check_key(request)
        q = (request.query_params.get("q") or "").strip().lower()
        kind = (request.query_params.get("kind") or "all").strip()
        stage_info = helpers["stage_info"]
        from nekro_agent.models.db_plugin_data import DBPluginData

        rows = await DBPluginData.filter(plugin_key=plugin.key).all()
        all_items: List[Dict[str, Any]] = []
        chan: Dict[str, Dict[str, Any]] = {}
        for r in rows:
            ck = r.target_chat_key or ""
            k = _chat_kind(ck)
            try:
                d = json.loads(r.data_value or "{}")
            except Exception:  # noqa: BLE001
                d = {}
            profs = d.get("profiles") or {}
            if not profs:
                continue
            chan[ck] = {"kind": k, "count": len(profs)}
            for uid, p in profs.items():
                if not isinstance(p, dict):
                    continue
                try:
                    sc = int(p.get("score", 0) or 0)
                    last_interaction_at = int(p.get("last_interaction_at", 0) or 0)
                    updated_at = int(p.get("updated_at", 0) or 0)
                except (TypeError, ValueError):
                    continue
                stage, _ = stage_info(sc)
                all_items.append({
                    "chat_key": ck,
                    "kind": k,
                    "user_id": str(uid),
                    "display_name": str(p.get("display_name") or ""),
                    "score": sc,
                    "stage": stage,
                    "tags": [str(x) for x in (p.get("tags") or [])],
                    "summary": str(p.get("summary") or ""),
                    "last_interaction_at": last_interaction_at,
                    "updated_at": updated_at,
                })

        # 未过滤的统计（用于概览）
        people: Dict[str, int] = {"group": 0, "private": 0, "other": 0}
        for it in all_items:
            people[it["kind"]] = people.get(it["kind"], 0) + 1
        chans: Dict[str, int] = {"group": 0, "private": 0, "other": 0}
        for c in chan.values():
            chans[c["kind"]] = chans.get(c["kind"], 0) + 1

        items = all_items
        if kind != "all":
            items = [x for x in items if x["kind"] == kind]
        if q:
            def _hit(x: Dict[str, Any]) -> bool:
                hay = " ".join([
                    x["display_name"], x["user_id"], x["summary"],
                    " ".join(x["tags"]), x["chat_key"],
                ]).lower()
                return q in hay

            items = [x for x in items if _hit(x)]

        items.sort(key=lambda x: (-x["score"], x["chat_key"]))
        return {
            "items": items,
            "channels": chan,
            "people": people,
            "chan_counts": chans,
            "max_abs": helpers["max_abs_score"](),
            "filtered": len(items),
            "total": len(all_items),
        }

    @router.get("/api/profiles", summary="频道内的用户档案")
    async def api_profiles(request: Request, chat_key: str = "", user_id: str = "") -> Dict[str, Any]:
        _check_key(request)
        if not chat_key:
            raise HTTPException(status_code=400, detail="缺少 chat_key")
        load_state = helpers["load_state"]
        stage_info = helpers["stage_info"]
        state = await load_state(chat_key)
        profs = []
        for uid, p in state.profiles.items():
            if user_id and str(uid) != str(user_id):
                continue
            stage, hint = stage_info(int(p.score))
            profs.append(
                {
                    "user_id": uid,
                    "display_name": p.display_name or "",
                    "score": int(p.score),
                    "stage": stage,
                    "hint": hint,
                    "tags": list(p.tags or []),
                    "summary": p.summary or "",
                    "last_reason": p.last_reason or "",
                    "interaction_hint": p.interaction_hint or "",
                    "events": [
                        {"delta": ev.delta, "reason": ev.reason}
                        for ev in (p.recent_events or [])[-5:]
                    ],
                },
            )
        profs.sort(key=lambda x: -x["score"])
        return {
            "chat_key": chat_key,
            "max_abs": helpers["max_abs_score"](),
            "profiles": profs,
        }

    @router.post("/api/score", summary="设置某用户好感度")
    async def api_score(request: Request) -> Dict[str, Any]:
        _check_key(request)
        body = await _json_body(request)
        ck = str(body.get("chat_key") or "")
        uid = str(body.get("user_id") or "")
        if not ck or not uid:
            raise HTTPException(status_code=400, detail="缺少 chat_key/user_id")
        raw_score = body.get("score")
        if isinstance(raw_score, float) and not raw_score.is_integer():
            raise HTTPException(status_code=400, detail="score 必须是整数")
        try:
            score = int(raw_score)
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail="score 必须是整数") from exc
        now = helpers["now_ts"]()
        # ⚠️ 读-改-写必须在 per-chat 锁内完成：消息处理与衰减/回升循环都会写同一份档案，
        #    无锁 RMW 在并发下会 last-writer-wins 把分数丢掉（锁的注释里就写了这个场景）。
        lock = await helpers["state_lock"](ck)
        async with lock:
            state = await helpers["load_state"](ck)
            prof = state.profile_for(uid, "")
            before = int(prof.score)
            final_score = helpers["clamp_score"](score)
            prof.score = final_score
            prof.updated_at = now
            prof.last_reason = str(body.get("reason") or "WebUI 手动调整")[:160]
            # 手工改分同样计入当日净额与间隔锚点（与工具、超管命令同一套预算）
            prof.record_adjust(final_score - before, now)
            await helpers["save_state"](ck, state)
        stage, _ = helpers["stage_info"](final_score)
        return {
            "ok": True,
            "user_id": uid,
            "score": final_score,
            "stage": stage,
            "max_abs": helpers["max_abs_score"](),
        }

    @router.post("/api/profile", summary="修改 / 删除某用户的关系档案（扩展）")
    async def api_profile(request: Request) -> Dict[str, Any]:
        """编辑关系档案的文字字段：稳定印象 / 标签 / 个人互动提示；可选同时改分数。

        body: {chat_key, user_id, action?: "update"|"delete",
               summary?, tags?, interaction_hint?, score?, reason?}
        """
        _check_key(request)
        body = await _json_body(request)
        ck = str(body.get("chat_key") or "")
        uid = str(body.get("user_id") or "")
        if not ck or not uid:
            raise HTTPException(status_code=400, detail="缺少 chat_key/user_id")
        action = str(body.get("action") or "update")
        if action not in ("update", "delete"):
            raise HTTPException(status_code=400, detail="action 只能是 update 或 delete")

        now = helpers["now_ts"]()
        # 同 /api/score：整个读-改-写要在同一把频道锁内，避免与消息处理并发丢更新
        lock = await helpers["state_lock"](ck)
        async with lock:
            state = await helpers["load_state"](ck)
            # ⚠️ 不能在这里 profile_for：它会**现建**一份档案，
            #    于是下面的「uid in profiles」永远为真 → 删一个不存在的用户也返回 ok，
            #    而 404 分支成了死代码。建档挪到 update 分支里。
            if action == "delete":
                profiles = getattr(state, "profiles", None)
                if isinstance(profiles, dict) and uid in profiles:
                    profiles.pop(uid, None)
                    await helpers["save_state"](ck, state)
                    plugin.logger.info(f"[persona] WebUI 删除关系档案 {ck} / {uid}")
                    return {"ok": True, "action": "delete", "user_id": uid}
                raise HTTPException(status_code=404, detail="该频道没有这个用户的档案")

            prof = state.profile_for(uid, "")
            _before_score = int(prof.score)
            changed: List[str] = []
            if "summary" in body:
                prof.summary = " ".join(str(body["summary"] or "").split())[:200]
                changed.append("summary")
            if "tags" in body:
                raw_tags = body["tags"]
                if isinstance(raw_tags, str):
                    raw_tags = raw_tags.replace("，", ",").replace("、", ",").split(",")
                if not isinstance(raw_tags, list):
                    raise HTTPException(status_code=400, detail="tags 必须是数组或逗号分隔的字符串")
                tags: List[str] = []
                for item in raw_tags:
                    tag = " ".join(str(item or "").split())[:16]
                    if tag and tag not in tags:
                        tags.append(tag)
                prof.tags = tags[:12]
                changed.append("tags")
            if "interaction_hint" in body:
                prof.interaction_hint = " ".join(str(body["interaction_hint"] or "").split())[:200]
                changed.append("interaction_hint")
            if "score" in body and body["score"] is not None and str(body["score"]) != "":
                raw_score = body["score"]
                if isinstance(raw_score, float) and not raw_score.is_integer():
                    raise HTTPException(status_code=400, detail="score 必须是整数")
                try:
                    prof.score = helpers["clamp_score"](int(raw_score))
                except (TypeError, ValueError) as exc:
                    raise HTTPException(status_code=400, detail="score 必须是整数") from exc
                changed.append("score")
            if not changed:
                raise HTTPException(status_code=400, detail="没有要修改的字段")

            prof.updated_at = now
            if str(body.get("reason") or "").strip():
                prof.last_reason = str(body["reason"]).strip()[:160]
            elif "score" in changed:
                prof.last_reason = "WebUI 手动调整"
            if "score" in changed:
                prof.record_adjust(int(prof.score) - _before_score, now)
            await helpers["save_state"](ck, state)
            stage, _hint = helpers["stage_info"](prof.score)
            snapshot = {
                "score": prof.score,
                "summary": prof.summary,
                "tags": list(prof.tags),
                "interaction_hint": prof.interaction_hint,
            }
        plugin.logger.info(f"[persona] WebUI 修改关系档案 {ck} / {uid}: {changed}")
        return {
            "ok": True,
            "action": "update",
            "user_id": uid,
            "changed": changed,
            "stage": stage,
            "max_abs": helpers["max_abs_score"](),
            **snapshot,
        }

    @router.get("/api/preview", summary="注入预览")
    async def api_preview(request: Request, chat_key: str = "", score: int = 0) -> Dict[str, Any]:
        _check_key(request)
        if not chat_key:
            raise HTTPException(status_code=400, detail="缺少 chat_key")
        from nekro_agent.api.schemas import AgentCtx

        requested_score = int(score)
        effective_score = helpers["clamp_score"](requested_score)
        stage, _ = helpers["stage_info"](effective_score)
        try:
            ctx = await AgentCtx.create_by_chat_key(chat_key=chat_key)
        except ValueError as exc:
            # 频道不存在时这里抛 ValueError：旧实现未捕获 → 500 + 前端把失败当成功、
            # 渲染出一份「看起来正常」的空预览，用户以为频道内容就是空的。
            raise HTTPException(status_code=404, detail=f"频道不存在：{chat_key}") from exc
        cfg = memory.load_favor_cfg()
        gating = bool(cfg) and bool(cfg.get("enabled", True))
        # ⚠️ dry_run=True：预览绝不能在真实数据上留副作用。
        #    render_memory_block 会写 st["last_hit"]、并在生命周期开启时 _bump_hits
        #    （去抖落盘 .hit_stats.json）——在预览页点来点去就会虚增热度、扭曲真实排序，
        #    与 /api/recall_test 的「只读」纪律相悖。
        block = await memory.render_memory_block(
            ctx, effective_score, gating=gating,
            tier1_cfg=_tier1_cfg(), tier2_cfg=_tier2_cfg(), tier3_cfg=_t3_cfg(),
            dry_run=True,
        )
        # 真实 prompt 的第 ① 段是「关系卡」（persona_prompt 里的 parts[0]）。
        # 早先预览从第 ② 段才开始，与「渲染真实注入文本」的契约不符。
        card = ""
        try:
            _arch = await helpers["load_state"](chat_key)
            _uid = (getattr(ctx, "from_platform_userid", "") or
                    getattr(_arch, "last_active_user_id", "") or "").strip()
            _prof = _arch.profiles.get(_uid) if _uid else None
            card = (helpers["compose_archive_card"](_prof, current=True)
                    if _prof is not None
                    else helpers["compose_overview_card"](_arch))
        except Exception as exc:  # noqa: BLE001
            plugin.logger.warning(f"[persona] 预览关系卡组装失败（已跳过）: {exc}")
            card = ""
        text = "\n\n".join(p for p in (card, block) if p)

        # 生长层 tier4：身份锚点（anchor · 全局）+ 频道笔记（note · 本频道）
        # ⚠️ 必须与 persona_prompt 的第 ③ 段同口径：preview 早先只实现了 ② 三档记忆
        #    和 ④ memory_log，漏掉 tier4，导致预览里看不到锚点和笔记，
        #    调试时会误判「锚点/笔记没生效」（实测踩过）。
        tier4_text = ""
        tier4_anchor_n = 0
        tier4_note_n = 0
        try:
            t4 = helpers["tier4"]
            acfg = helpers["anchor_cfg"]()
            ncfg = helpers["note_cfg"]()
            recent_text = await memory._recent_text(ctx)
            doc = await t4.open_tier4()
            subs: List[str] = []
            if acfg["enabled"]:
                ab = t4.render_anchor_block(doc, acfg, helpers["anchor_guide"])
                if ab:
                    subs.append(ab)
            if ncfg["enabled"]:
                nb = t4.render_note_block(
                    doc, ncfg, chat_key, effective_score, recent_text,
                    helpers["note_guide"],
                )
                if nb:
                    subs.append(nb)
            if subs:
                tier4_text = "\n\n".join(subs)
                text = (text + "\n\n" + tier4_text).strip()
            tier4_anchor_n = sum(1 for e in doc.entries if e.kind == t4.ANCHOR_KIND)
            tier4_note_n = sum(
                1 for e in doc.entries
                if e.kind == t4.NOTE_KIND and e.scope == chat_key and not e.is_expired()
            )
        except Exception:  # noqa: BLE001
            tier4_text = ""

        # 关系记忆（memory_log · 系统自动沉淀的事件史）—— 与 persona_prompt 同口径
        memlog_text = ""
        memlog_picked: List[Any] = []
        memlog_total = 0
        try:
            if (bool(getattr(config, "MEMLOG_ENABLED", True))
                    and bool(getattr(config, "MEMLOG_INJECT_ENABLED", True))):
                memlog = helpers["memory_log"]
                items = await memlog.list_entries(chat_key)
                memlog_total = len(items)
                if items:
                    _now = time.time()
                    items.sort(
                        key=lambda e: (memlog.heat_factor(e, _now),
                                       e.event_at or e.created_at),
                        reverse=True,
                    )
                    max_n = max(1, int(getattr(config, "MEMLOG_INJECT_MAX", 3) or 3))
                    budget = max(100, int(getattr(config, "MEMLOG_INJECT_CHARS", 700) or 700))
                    head = "【关系记忆 · 你记得发生过的事】"
                    used = len(head)
                    for e in items:
                        line = f"- {e.title}：{e.content}"
                        if memlog_picked and used + len(line) + 1 > budget:
                            break
                        memlog_picked.append(e)
                        used += len(line) + 1
                        if len(memlog_picked) >= max_n:
                            break
                    if memlog_picked:
                        memlog_text = head + "\n" + "\n".join(
                            f"- {e.title}：{e.content}" for e in memlog_picked)
                        text = (text + "\n\n" + memlog_text).strip()
        except Exception:  # noqa: BLE001
            memlog_text = ""

        return {
            "chat_key": chat_key,
            "score": effective_score,
            "requested_score": requested_score,
            "effective_score": effective_score,
            "stage": stage,
            "max_abs": helpers["max_abs_score"](),
            "length": len(text),
            "text": text,
            "debug": {
                "dry_run": True,
                "card_chars": len(card),
                "gating_enabled": gating,
                "tier1_count": len(memory._state().get("tier1", [])),
                "tier2_count": len(memory._state().get("tier2", [])),
                "tier2_hit": str(memory._state().get("last_hit") or ""),
                "tier3_count": len(memory._state().get("tier3", [])),
                "tier3_hit": str(memory._state().get("last_hit_tier3") or ""),
                "tier3_enabled": memory.tier3_settings(_t3_cfg())["enabled"],
                "message_scan_limit": int(memory.tier2_settings(_tier2_cfg())["scan_msgs"]),
                "tier4_anchor_count": tier4_anchor_n,
                "tier4_note_count": tier4_note_n,
                "memory_log_total": memlog_total,
                "memory_log_injected": len(memlog_picked),
            },
            "tier4_text": tier4_text,
            "memory_log_text": memlog_text,
        }

    @router.get("/api/memory_log", summary="关系记忆（自动沉淀的事件史）")
    async def api_memory_log(request: Request, chat_key: str = "",
                             limit: int = 100, q: str = "") -> Dict[str, Any]:
        """列出关系记忆。chat_key 为空时返回全部频道（按频道分组统计）。

        带 `q` 时按「频道号 / 标题 / 内容 / 标签 / id」做大小写不敏感子串匹配。
        注意：关系记忆条目本身**没有用户账号字段**（事件史由后台归纳），
        想按人搜只能命中正文里出现过的名字。
        """
        _check_key(request)
        memlog = helpers["memory_log"]
        entries = await memlog.list_entries(chat_key or None)
        entries.sort(key=lambda e: e.event_at or e.created_at, reverse=True)
        now = time.time()
        rows: List[Dict[str, Any]] = [
            {
                "id": e.id,
                "scope": e.scope,
                "title": e.title,
                "content": e.content,
                "tags": e.tags,
                "event_at": e.event_at,
                "created_at": e.created_at,
                "hits": e.hits,
                "last_hit": e.last_hit,
                "heat": round(memlog.heat_factor(e, now), 4),
            }
            for e in entries
        ]
        query = str(q or "").strip()
        if query:
            rows = [
                r for r in rows
                if _search_hit(query, r, ("scope", "title", "content", "tags", "id"))
            ]
        total = len(rows)
        n = max(1, min(500, int(limit or 100)))
        return {
            "chat_key": chat_key,
            "q": query,
            "total": total,
            "stats": await memlog.stats(chat_key or None),
            "items": rows[:n],
        }

    @router.post("/api/memory_log", summary="关系记忆操作（delete）")
    async def api_memory_log_op(request: Request) -> Dict[str, Any]:
        _check_key(request)
        body = await _json_body(request)
        op = str(body.get("op") or "").strip()
        memlog = helpers["memory_log"]
        if op == "delete":
            eid = str(body.get("id") or "").strip()
            if not eid:
                raise HTTPException(status_code=400, detail="缺少 id")
            ok = await memlog.delete_entry(eid)
            if not ok:
                # 未命中是「找不到」，不是「删成功」：200 + ok:false 会让前端提示
                # 「已删除」而列表刷新后条目还在（只有刷新才看得出矛盾）。
                raise HTTPException(status_code=404, detail=f"找不到关系记忆 {eid}")
            return {"ok": True, "id": eid}
        raise HTTPException(status_code=400, detail=f"未知操作: {op}")

    @router.post("/api/recall_test", summary="检索实验（A5：为什么命中/为什么落选）")
    async def api_recall_test(request: Request) -> Dict[str, Any]:
        """对一段模拟对话文本跑真实检索，逐条标注分数/来源/门控/预算/热度与去留原因。

        只读，不改任何数据。直接用 _match_tierN_text 纯文本核心（A7 解耦后无需 ctx）。
        """
        _check_key(request)
        body = await _json_body(request)
        text = str(body.get("text") or "").strip()
        tier = "tier3" if str(body.get("tier") or "tier2") == "tier3" else "tier2"
        if not text:
            raise HTTPException(status_code=400, detail="请输入模拟对话文本")
        try:
            _raw_score = int(body.get("score") or 0)
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail="score 需为整数") from exc
        score = helpers["clamp_score"](_raw_score)
        await memory.ensure_loaded()
        cfg_favor = memory.load_favor_cfg()
        gating = bool(body.get("gating", True)) and bool(cfg_favor.get("enabled", True))

        import time as _time
        now = _time.time()
        tier_num = 3 if tier == "tier3" else 2
        cfg = _t3_cfg() if tier_num == 3 else _tier2_cfg()
        tset = memory.tier3_settings(cfg) if tier_num == 3 else memory.tier2_settings(cfg)
        lc = memory.lifecycle_settings(cfg.get("lifecycle"))
        if tier_num == 3:
            scored, src = await memory._match_tier3_text(text, cfg)
        else:
            scored, src = await memory._match_tier2_text(text, cfg)

        max_entries = int(tset["max_entries"])
        max_chars = int(tset["max_chars"])
        ok_scored, _blocked = memory.apply_gating(scored, score, gating, tier=tier_num)

        def _blen(e: Dict[str, Any]) -> int:
            content = str(e.get("content", "")).strip()
            if tier_num == 3:
                fam = str(e.get("familiarity", "")).strip()
                return len(f"· 【{fam}】{content}" if fam else f"· {content}")
            return len(content)

        # 复刻渲染层预算：2/3 档一致=skip（跳过超长条目、继续处理其后）。
        # ⚠️ v1.6.3：3 档渲染器已从 break 改为 continue（#5），此处必须同步，
        # 否则召回测试台会给 3 档显示错误的去留原因（谎报「其后全停」）。
        kept_ids: List[str] = []
        used = 0
        for _s, _src, e in ok_scored[:max_entries]:
            content = str(e.get("content", "")).strip()
            if not content:
                continue
            if used + _blen(e) > max_chars:
                continue
            kept_ids.append(str(e.get("id", "?")))
            used += _blen(e)
        kept_set = set(kept_ids)

        # #9：用公开只读快照替代直接读私有 memory._state()["hit_stats"]（改善封装、防误改）。
        # 循环外取一次，避免每条候选都全量拷贝。
        hit_snap = memory.hit_stats_snapshot()
        rows: List[Dict[str, Any]] = []
        for s, source, e in scored:
            eid = str(e.get("id", "?"))
            try:
                need = int(e.get("min_favor", 0) or 0)
            except (TypeError, ValueError):
                need = 0
            gated_ok = (not gating) or tier_num == 3 or need <= score
            content = str(e.get("content", "")).strip()
            kept = eid in kept_set
            if kept:
                reason = "入选"
            elif not gated_ok:
                reason = f"门控拦截（min_favor={need} > score={score}）"
            elif not content:
                reason = "内容为空"
            elif eid not in [str(x[2].get("id", "?")) for x in ok_scored[:max_entries]]:
                reason = f"超出条数上限（前 {max_entries} 条才参与预算）"
            else:
                reason = f"超出字符预算（{max_chars}），跳过（其后条目仍继续参与）"
            stat = hit_snap.get(memory._hit_key(tier_num, eid)) or {}
            rows.append({
                "id": eid,
                "title": str(e.get("title", "")),
                "score": round(float(s), 4),
                "source": source,
                "gating_passed": gated_ok,
                "kept": kept,
                "heat": round(memory.heat_factor(stat, now, lc), 3),
                "hits": _as_int(stat.get("hits"), 0),
                "reason": reason,
            })
        return {
            "tier": tier, "text": text, "src": src, "score": score, "gating": gating,
            "lifecycle_enabled": lc["enabled"], "threshold": float(tset["threshold"]),
            "candidate_count": len(rows), "kept": kept_ids, "candidates": rows[:40],
        }

    @router.post("/api/config", summary="修改插件配置（好感度衰减等）")
    async def api_config(request: Request) -> Dict[str, Any]:
        _check_key(request)
        body = await _json_body(request)
        patch: Dict[str, Any] = {}
        limits = {
            "FAVOR_SCALE_MODE": (int, 1, 3),
            "DECAY_ENABLED": (bool, None, None),
            "DECAY_KEEP_TIER": (bool, None, None),
            "DECAY_INTERVAL_HOURS": (int, 1, 168),
            "DECAY_PERCENT": (int, 1, 50),
            "DECAY_TIER_GRACE_HOURS": (int, 0, 8760),
            # 扩展：回升
            "RECOVER_ENABLED": (bool, None, None),
            "RECOVER_INTERVAL_HOURS": (int, 1, 168),
            "RECOVER_PERCENT": (int, 1, 50),
            # 扩展：1/2 档预算与阈值（原先写死在 memory.py）
            "TIER1_MAX_CHARS": (int, 100, 10000),
            "TIER2_MAX_ENTRIES": (int, 1, 10),
            "TIER2_MAX_CHARS": (int, 50, 3000),
            "TIER2_SIM_THRESHOLD": (float, 0.0, 1.0),
            "TIER2_SCAN_MSGS": (int, 1, 200),
            # 扩展：3 档（世界知识）
            "TIER3_ENABLED": (bool, None, None),
            "TIER3_MAX_ENTRIES": (int, 1, 10),
            "TIER3_MAX_CHARS": (int, 100, 3000),
            "TIER3_SIM_THRESHOLD": (float, 0.0, 1.0),
            "TIER3_SCAN_MSGS": (int, 1, 200),
            # 扩展：记忆生命周期（A2/A3，1.5.0）
            "LIFECYCLE_ENABLED": (bool, None, None),
            "LIFECYCLE_HALF_LIFE_DAYS": (int, 1, 3650),
            "LIFECYCLE_FLOOR": (float, 0.0, 1.0),
            "LIFECYCLE_PROTECT_WEIGHT": (int, 0, 100),
            "LIFECYCLE_REINFORCE_BONUS": (int, 0, 100),
            # 扩展：独立嵌入后端（v1.6.0）
            "EMBED_PROVIDER": (str, None, None),
            "EMBED_BASE_URL": (str, None, None),
            "EMBED_API_KEY": (str, None, None),
            "EMBED_MODEL": (str, None, None),
            "EMBED_DIMENSIONS": (int, 0, 8192),
            "EMBED_BATCH_SIZE": (int, 1, 256),
            # 扩展：备忘录
            "MEMO_ENABLED": (bool, None, None),
            "MEMO_MAX_ITEMS": (int, 1, 500),
            "MEMO_MAX_CONTENT_CHARS": (int, 50, 2000),
            "MEMO_INJECT_RECENT": (int, 0, 10),
            "MEMO_INJECT_MATCHED": (int, 0, 10),
            "MEMO_INJECT_CHARS": (int, 100, 4000),
            "MEMO_DEDUP_MIN_KEY_LEN": (int, 2, 10),
            # 扩展：tier4 身份锚点（阶段 A v1.7.0）
            "ANCHOR_ENABLED": (bool, None, None),
            "ANCHOR_MAX_TOTAL": (int, 1, 500),
            "ANCHOR_MAX_PER_DAY": (int, 0, 50),
            "ANCHOR_MIN_INTERVAL_HOURS": (int, 0, 168),
            "ANCHOR_MAX_CONTENT_CHARS": (int, 20, 2000),
            "ANCHOR_INJECT_MAX": (int, 0, 50),
            "ANCHOR_INJECT_CHARS": (int, 0, 8000),
            "ANCHOR_SALIENCE_MAX": (int, 1, 5),
        }
        def _cfg_value(field: str, conv, raw):
            """配置项类型转换：bool 只认显式白名单，int/float 拒布尔与非数字。

            ⚠️ 旧实现直接 `conv(body[k])`：`bool("false")` 是 **True**（非空字符串），
            于是前端把开关「关闭」时反而写成开启（实测 6 个开关全中）；
            `int("abc")` 则抛 ValueError 直接 500，用户不知道是哪个字段错了。
            """
            if conv is bool:
                if isinstance(raw, bool):
                    return raw
                if isinstance(raw, (int, float)):
                    return bool(raw)
                s = str(raw).strip().lower()
                if s in ("true", "1", "yes", "on"):
                    return True
                if s in ("false", "0", "no", "off", ""):
                    return False
                raise HTTPException(
                    status_code=400, detail=f"{field} 必须是 true/false，收到 {raw!r}",
                )
            if conv is int:
                if isinstance(raw, bool):
                    raise HTTPException(status_code=400, detail=f"{field} 必须是整数，收到布尔值")
                try:
                    return int(raw)
                except (TypeError, ValueError):
                    try:
                        return int(float(str(raw).strip()))
                    except (TypeError, ValueError) as exc:
                        raise HTTPException(
                            status_code=400, detail=f"{field} 必须是整数，收到 {raw!r}",
                        ) from exc
            if conv is float:
                if isinstance(raw, bool):
                    raise HTTPException(status_code=400, detail=f"{field} 必须是数字，收到布尔值")
                try:
                    return float(raw)
                except (TypeError, ValueError) as exc:
                    raise HTTPException(
                        status_code=400, detail=f"{field} 必须是数字，收到 {raw!r}",
                    ) from exc
            return str(raw)

        for k, (conv, lo, hi) in limits.items():
            if k not in body:
                continue
            v = _cfg_value(k, conv, body[k])
            if lo is not None:
                if conv is float:
                    v = max(float(lo), min(float(hi), float(v)))
                else:
                    v = max(lo, min(hi, int(v)))
            patch[k] = v
        if not patch:
            raise HTTPException(status_code=400, detail="没有要修改的配置")
        cfg = plugin.get_config()
        for k, v in patch.items():
            setattr(cfg, k, v)
        plugin.save_config(cfg)
        def _redact(d: Dict[str, Any]) -> Dict[str, Any]:
            """日志脱敏：键名里带 KEY/TOKEN/SECRET/PASSWORD 的值一律打码。

            ⚠️ 之前这里直接打整个 patch：用户改一次嵌入设置，
            EMBED_API_KEY 就以明文进操作日志（日志常被导出、贴进 issue）。
            """
            out: Dict[str, Any] = {}
            for _k, _v in d.items():
                _ku = str(_k).upper()
                if any(t in _ku for t in ("KEY", "TOKEN", "SECRET", "PASSWORD", "PASSWD")):
                    out[_k] = "***" if _v else ""
                else:
                    out[_k] = _v
            return out

        plugin.logger.info(f"[persona] WebUI 修改配置: {_redact(patch)}")
        return {"ok": True, "patch": patch}

    @router.post("/api/gating", summary="修改好感度门控规则（档位/回避块/rubric）")
    async def api_gating(request: Request) -> Dict[str, Any]:
        _check_key(request)
        body = await _json_body(request)
        patch: Dict[str, Any] = {}
        if "enabled" in body:
            patch["enabled"] = bool(body["enabled"])
        if "stages" in body and isinstance(body["stages"], list):
            raw_stages = body["stages"]
            if not raw_stages and not bool(body.get("allow_empty_stages", False)):
                raise HTTPException(status_code=400, detail="至少保留一档；如需清空请明确勾选确认")
            stages: List[Dict[str, Any]] = []
            seen_max = set()
            for index, stage in enumerate(raw_stages, 1):
                if not isinstance(stage, dict):
                    raise HTTPException(status_code=400, detail=f"第 {index} 档格式无效")
                try:
                    maximum = int(stage.get("max", 999))
                except (TypeError, ValueError) as exc:
                    raise HTTPException(status_code=400, detail=f"第 {index} 档分数上限必须是整数") from exc
                name = str(stage.get("name") or "").strip()
                if not name:
                    raise HTTPException(status_code=400, detail=f"第 {index} 档名称不能为空")
                if maximum < -1000 or maximum > 999:
                    raise HTTPException(status_code=400, detail=f"第 {index} 档分数上限必须在 -1000 到 999 之间")
                if maximum in seen_max:
                    raise HTTPException(status_code=400, detail=f"分数上限重复：{maximum}")
                seen_max.add(maximum)
                stages.append({
                    "max": maximum,
                    "name": name[:12],
                    "unlock_hint": str(stage.get("unlock_hint") or "")[:120],
                })
            if len(stages) > 20:
                # 档位是给模型读的分层描述：几十档既用不上，又会让注入与 UI 双重膨胀
                raise HTTPException(
                    status_code=400,
                    detail=f"档位最多 20 个（当前 {len(stages)} 个）",
                )
            stages.sort(key=lambda item: item["max"])
            if stages or bool(body.get("allow_empty_stages", False)):
                patch["stages"] = stages
        if "avoid" in body and isinstance(body["avoid"], dict):
            patch["avoid"] = {
                "header": str(body["avoid"].get("header") or "")[:60],
                "note": str(body["avoid"].get("note") or "")[:600],
            }
        if "rubric" in body and isinstance(body["rubric"], dict):
            rubric = body["rubric"]
            _STALE_HINT = "（若你刚更新过插件，多半是浏览器仍在用缓存的旧页面，请 Ctrl+F5 强制刷新）"

            # ⚠️ 严格校验，不做「聪明」的容错。
            #    历史上这里曾经：items 直接迭代 → 传字符串时逐字符丢数据；
            #    后来改成「字符串按行解析」→ 逗号串被当成一整行收下，依旧写坏。
            #    结论：形状不对就 400，让调用方看到错误，绝不静默落盘。
            raw_items = rubric.get("items")
            if raw_items is None or raw_items == "":
                raw_items = []
            if not isinstance(raw_items, (list, tuple)):
                raise HTTPException(
                    status_code=400,
                    detail=f"rubric.items 必须是 [[分值, 说明], …] 数组{_STALE_HINT}",
                )
            items: List[List[str]] = []
            for item in raw_items:
                if not isinstance(item, (list, tuple)) or len(item) < 2:
                    raise HTTPException(
                        status_code=400,
                        detail=f"rubric.items 每一项都必须是 [分值, 说明] 二元组{_STALE_HINT}",
                    )
                items.append([str(item[0])[:16], str(item[1])[:200]])

            raw_rules = rubric.get("rules")
            if raw_rules is None or raw_rules == "":
                raw_rules = []
            if not isinstance(raw_rules, (list, tuple)):
                raise HTTPException(
                    status_code=400,
                    detail=f"rubric.rules 必须是字符串数组{_STALE_HINT}",
                )
            rules = [str(x)[:200] for x in raw_rules if str(x).strip()][:20]

            patch["rubric"] = {
                "header": str(rubric.get("header") or "")[:60],
                "intro": str(rubric.get("intro") or "")[:300],
                "items": items,
                "rules": rules,
            }
        # 扩展：分档互动指引（key = 档位名）
        if "stage_guides" in body and isinstance(body["stage_guides"], dict):
            names = {str(s.get("name") or "") for s in (patch.get("stages") or [])}
            guides: Dict[str, str] = {}
            for raw_name, raw_text in body["stage_guides"].items():
                name = str(raw_name or "").strip()[:12]
                if not name:
                    continue
                text = " ".join(str(raw_text or "").split())[:600]
                if not text:
                    continue
                if names and name not in names:
                    continue          # 档位已改名/删除 → 丢弃孤儿指引
                guides[name] = text
            patch["stage_guides"] = guides
        if "stage_guide_common" in body:
            patch["stage_guide_common"] = " ".join(
                str(body["stage_guide_common"] or "").split(),
            )[:600]
        if not patch:
            raise HTTPException(status_code=400, detail="没有要修改的规则")
        try:
            _write_gating(patch)
        except ValueError as exc:
            raise _data_error(exc) from exc
        reload_state = await _reload_after_write()
        plugin.logger.info(f"[persona] WebUI 修改门控规则: {list(patch)}")
        return {"ok": True, "persisted": True, "patch_keys": list(patch), **reload_state}

    @router.post("/api/entry/new", summary="新建背景记忆条目")
    async def api_entry_new(request: Request) -> Dict[str, Any]:
        _check_key(request)
        body = await _json_body(request)
        tier = str(body.get("tier") or "")
        if tier not in _TIERS:
            raise HTTPException(status_code=400, detail="tier 无效")
        try:
            entry = {
                "id": str(body.get("id") or "")[:48],
                "title": str(body.get("title") or "")[:80],
                "content": str(body.get("content") or "")[:2000],
                "weight": int(body.get("weight") or 1),
                "min_favor": max(0, int(body.get("min_favor") or 0)),
                "enabled": bool(body.get("enabled", True)),
            }
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail="权重和门槛必须是整数") from exc
        if entry["weight"] < 1 or entry["weight"] > 999:
            raise HTTPException(status_code=400, detail="权重必须在 1 到 999 之间")
        if entry["min_favor"] < 0:
            raise HTTPException(status_code=400, detail="披露门槛不能小于 0")
        if not entry["content"].strip():
            raise HTTPException(status_code=400, detail="记忆内容不能为空")
        # 标题是列表里唯一能认人的字段：空标题条目会变成「无名条目」，
        # 只能靠手动翻找改回来 —— 与「内容为空」同等级校验。
        if not entry["title"].strip():
            raise HTTPException(status_code=400, detail="标题不能为空")
        if tier == "tier1":
            entry["trigger"] = str(body.get("trigger") or "")[:200]
        else:
            trg = body.get("triggers") or []
            if isinstance(trg, str):       # 与 update 端同口径：逗号分隔的字符串
                trg = [x.strip() for x in trg.replace("，", ",").split(",")]
            entry["triggers"] = [str(x)[:24] for x in (trg or []) if str(x).strip()][:30]
        if tier == "tier3":
            fam = str(body.get("familiarity") or "")[:12]
            if fam and fam not in ("熟人", "听说", "仅传闻"):
                raise HTTPException(
                    status_code=400, detail="熟悉度只能是 熟人 / 听说 / 仅传闻",
                )
            entry["familiarity"] = fam
        try:
            eid = _new_entry(tier, entry)
        except ValueError as exc:
            raise _data_error(exc) from exc
        reload_state = await _reload_after_write()
        return {"ok": True, "persisted": True, "id": eid, "tier": tier, **reload_state}

    # ---------------- 频道笔记（阶段 B：tier4 note，接替旧 memo）----------------
    # 端点路径 /api/memos、/api/memo 保持不变（前端无需改），底层数据源从
    # ChannelArchive.memos 换成 tier4.json 里 kind=note、scope=chat_key 的条目。
    async def _tier4_writable_guard() -> None:
        """tier4 处于损坏保护态时，写操作直接 409（fail-closed + 可读提示）。

        tier4 的变更函数会抛 Tier4Corrupted；WebUI 若不拦，崩出来就是 500，
        用户看到的只是一个「服务器错误」，不知道是文件损坏、也不知道备份在哪。

        ⚠️ 先 `open_tier4()` 读一次：降级标记只由读路径设置，
        不先读的话第一次点击仍会走到「找不到条目」的 404。
        """
        t4 = helpers["tier4"]
        reason = ""
        try:
            await t4.open_tier4()
            reason = str(t4.corruption_reason() or "")
        except Exception:  # noqa: BLE001
            reason = ""
        if reason:
            raise HTTPException(
                status_code=409,
                detail=f"tier4.json 处于损坏保护状态（{reason}），写入已被拒绝。"
                       "同目录下已有 .corrupt_* 备份，修复或替换该文件后即可恢复写入。",
            )

    def _search_hit(query: str, row: Dict[str, Any], fields) -> bool:
        """大小写不敏感的跨字段子串匹配（列表搜索用）。

        列表字段（tags）拼成空格分隔的串一起匹配，行为与前端 matchQuery 保持一致。
        """
        low = str(query or "").strip().lower()
        if not low:
            return True
        for f in fields:
            val = row.get(f)
            if val is None:
                continue
            hay = " ".join(str(x) for x in val) if isinstance(val, (list, tuple)) else str(val)
            if low in hay.lower():
                return True
        return False

    def _note_items(chat_key: str, entries, now: int) -> List[Dict[str, Any]]:
        items: List[Dict[str, Any]] = []
        for n in entries:
            if n.scope != chat_key:
                continue
            expire_at = int(n.expire_at or 0)
            items.append(
                {
                    "id": str(n.id),
                    "title": str(n.title),
                    "content": str(n.content),
                    "tags": list(n.tags),
                    "min_favor": int(n.min_favor or 0),
                    "expire_at": expire_at,
                    "expired": bool(expire_at and expire_at <= now),
                    "created_at": int(n.created_at or 0),
                    "updated_at": int(n.updated_at or 0),
                    "source_user_id": str(n.source_user_id or ""),
                    # 跨频道聚合时前端要显示「这条属于哪个频道」
                    "chat_key": str(n.scope),
                },
            )
        items.sort(key=lambda x: -x["updated_at"])
        return items

    @router.get("/api/memos", summary="频道笔记")
    async def api_memos(request: Request, chat_key: str = "",
                        q: str = "") -> Dict[str, Any]:
        """列出频道笔记。

        - `chat_key` 为空时聚合**全部频道**（供页面跨频道搜索）；
        - 带 `q` 时按「频道号 / 用户账号 / 标题 / 内容 / 标签」做大小写不敏感子串匹配。
        原有「按单个 chat_key 取笔记」的行为与返回结构保持不变。
        """
        _check_key(request)
        t4 = helpers["tier4"]
        now = int(helpers["now_ts"]())
        entries = await t4.list_entries(t4.NOTE_KIND)
        if chat_key:
            items = _note_items(chat_key, entries, now)
        else:
            items = []
            for scope in sorted({str(n.scope) for n in entries}):
                items.extend(_note_items(scope, entries, now))
            items.sort(key=lambda x: -int(x.get("updated_at") or 0))
        query = str(q or "").strip()
        if query:
            items = [
                x for x in items
                if _search_hit(query, x, ("chat_key", "source_user_id",
                                          "title", "content", "tags"))
            ]
        ncfg = helpers["note_cfg"]()
        return {
            "chat_key": chat_key,
            "q": query,
            "items": items,
            "meta": {
                **ncfg,
                "total": len(items),
                "live": sum(1 for x in items if not x["expired"]),
                "scopes": len({str(x.get("chat_key") or "") for x in items}),
            },
        }

    @router.post("/api/memo", summary="新建 / 修改 / 删除频道笔记")
    async def api_memo_update(request: Request) -> Dict[str, Any]:
        _check_key(request)
        body = await _json_body(request)
        await _tier4_writable_guard()
        chat_key = str(body.get("chat_key") or "")
        if not chat_key:
            raise HTTPException(status_code=400, detail="缺少 chat_key")
        t4 = helpers["tier4"]
        ncfg = helpers["note_cfg"]()
        now = int(helpers["now_ts"]())

        async def _scope_total() -> int:
            return sum(
                1 for n in await t4.list_entries(t4.NOTE_KIND) if n.scope == chat_key
            )

        op = str(body.get("op") or "save")

        if op == "delete":
            mid = str(body.get("id") or "")
            if not await t4.delete_entry(mid):
                raise HTTPException(status_code=404, detail=f"找不到笔记 {mid}")
            return {"ok": True, "deleted": mid, "total": await _scope_total()}

        if op == "clear_expired":
            removed = await t4.clear_expired_notes(chat_key)
            return {"ok": True, "removed": removed, "total": await _scope_total()}

        title = str(body.get("title") or "").strip()[:40]
        content = str(body.get("content") or "").strip()
        _raw_tags = body.get("tags") or []
        if isinstance(_raw_tags, str):
            # 传字符串时旧代码 `for x in "天气"` 会得到 ['天','气'] 两个标签
            _raw_tags = [x.strip() for x in _raw_tags.replace("，", ",").split(",")]
        tags = [str(x).strip()[:12] for x in _raw_tags if str(x).strip()][:6]
        try:
            min_favor = max(0, min(100, int(body.get("min_favor") or 0)))
            ttl_hours = max(0, int(body.get("ttl_hours") or 0))
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail="门槛与有效期需为整数") from exc

        res = await t4.add_or_update_note(
            chat_key, title, content,
            ttl_hours=ttl_hours, min_favor=min_favor, tags=tags,
            source_user_id=str(body.get("source_user_id") or ""), cfg=ncfg,
        )
        action = res.get("action")
        reason = res.get("reason")
        if action == "disabled":
            raise HTTPException(status_code=400, detail="频道笔记功能已关闭")
        if action == "rejected":
            detail = {
                "empty_title": "标题不能为空",
                "empty_content": "内容不能为空",
                "too_long": f"内容超过上限 {ncfg['max_content']} 字",
                "max_items": f"已达上限 {ncfg['max_items']} 条，请先清理",
                "write_failed": "写入磁盘失败（权限或空间问题），本次没有保存",
            }.get(reason, "写入被拒绝")
            raise HTTPException(status_code=400, detail=detail)
        note = res.get("note") or {}
        return {
            "ok": True,
            "action": action,           # created | updated
            "id": str(note.get("id", "")),
            "total": await _scope_total(),
        }

    # ==================== tier4 生长层 · 身份锚点（阶段 A v1.7.0）====================
    # WebUI 侧只做「审计」：查看 / 删除 / 调 salience。锚点只能由模型经 anchor_memory
    # 工具新增（触发可见），WebUI 不代为写入。tier4.json 属**私密生长数据**，
    # 绝不进公开的种子导出包（见 §5.3），仅提供独立私有导出。
    @router.get("/api/tier4", summary="tier4 条目（默认列 anchor）")
    async def api_tier4(request: Request, kind: str = "anchor") -> Dict[str, Any]:
        _check_key(request)
        t4 = helpers["tier4"]
        k = (kind or "").strip() or None
        entries = await t4.list_entries(k)
        now = int(helpers["now_ts"]())
        rows: List[Dict[str, Any]] = []
        for e in entries:
            rows.append(
                {
                    "id": str(e.id),
                    "kind": str(e.kind),
                    "scope": str(e.scope),
                    "content": str(e.content),
                    "title": str(e.title),
                    "tags": list(e.tags),
                    "salience": int(e.salience),
                    "min_favor": int(e.min_favor),
                    "expire_at": int(e.expire_at),
                    "expired": bool(e.expire_at and e.expire_at <= now),
                    "source_user_id": str(e.source_user_id),
                    "source_chat_key": str(e.source_chat_key),
                    "created_at": int(e.created_at),
                    "updated_at": int(e.updated_at),
                },
            )
        rows.sort(key=lambda x: (-int(x["salience"]), -int(x["updated_at"])))
        acfg = helpers["anchor_cfg"]()
        meta = (await t4.open_tier4()).meta.to_raw()
        return {
            "kind": k or "all",
            "items": rows,
            "meta": {
                **acfg,
                "total": len(rows),
                "day_count": int(meta.get("day_count") or 0),
                "last_anchor_at": int(meta.get("last_anchor_at") or 0),
            },
        }

    @router.post("/api/tier4", summary="tier4 条目操作（delete / salience）")
    async def api_tier4_update(request: Request) -> Dict[str, Any]:
        _check_key(request)
        body = await _json_body(request)
        await _tier4_writable_guard()
        t4 = helpers["tier4"]
        acfg = helpers["anchor_cfg"]()
        op = str(body.get("op") or "")
        eid = str(body.get("id") or "")
        if not eid:
            raise HTTPException(status_code=400, detail="缺少 id")

        if op == "delete":
            if not await t4.delete_entry(eid):
                raise HTTPException(status_code=404, detail=f"找不到条目 {eid}")
            remaining = await t4.list_entries("anchor")
            plugin.logger.info(f"[persona] WebUI 删除锚点 {eid}")
            return {"ok": True, "op": "delete", "id": eid, "total": len(remaining)}

        if op == "salience":
            try:
                val = int(body.get("salience"))
            except (TypeError, ValueError):
                raise HTTPException(status_code=400, detail="salience 需为整数") from None
            if not await t4.set_salience(eid, val, salience_max=acfg["salience_max"]):
                raise HTTPException(status_code=404, detail=f"找不到锚点 {eid}")
            cur = next((e for e in await t4.list_entries("anchor") if e.id == eid), None)
            plugin.logger.info(f"[persona] WebUI 调整锚点 salience: {eid} → {val}")
            return {"ok": True, "op": "salience", "id": eid,
                    "salience": int(cur.salience) if cur else val}

        raise HTTPException(status_code=400, detail=f"未知操作: {op}")

    @router.get("/api/tier4/export", summary="导出生长层 tier4.json（私有，不进种子包）")
    async def api_tier4_export(request: Request):
        _check_key(request)
        from datetime import datetime as _datetime

        from fastapi.responses import Response as _Resp

        t4 = helpers["tier4"]
        fp = t4.tier4_path()
        if fp.is_file():
            raw = fp.read_bytes()
        else:
            doc = await t4.open_tier4()
            raw = json.dumps(doc.to_raw(), ensure_ascii=False, indent=1).encode("utf-8")
        stamp = _datetime.now().strftime("%Y%m%d_%H%M%S")
        return _Resp(
            content=raw,
            media_type="application/json",
            headers={
                "Content-Disposition": f'attachment; filename="tier4_{stamp}.json"',
            },
        )

    # ==================== 阶段 C：生长包（tier4 + 关系档案，私密）====================
    # 与公开「种子包」彻底分离：生长包是"这一世活过的痕迹"，用于实例间迁移/备份。
    # 只含 tier4.json + 各频道关系档案 + 迁移报告/频道索引；
    # **不含** tier1/2/3、favor.json、presets。关系档案用 `archives.json`（按 chat_key 建索引），
    # 因为档案 blob 本身不含 chat_key，按文件名 hash 无法反推——用字典才无损。
    #
    # ⚠️ 热度（.hit_stats.json）**刻意不导出/不导入**（v2.0.3 起）：
    #    命中统计是按 entry id 记的，一旦目标实例的 tier2/tier3 内容与来源不同
    #    （改了条目、换了人设、id 复用给了别的条目），旧的热度就会张冠李戴，
    #    表现为"某条完全没被聊过的记忆莫名很热"。热度是**本实例的使用痕迹**，
    #    应当由本实例自行累积；迁移人设时它毫无意义，因此从包里剔除。
    @router.get("/api/growth/export", summary="导出生长包（tier4 + 关系档案，私密）")
    async def api_growth_export(request: Request):
        _check_key(request)
        import io as _io
        import zipfile as _zipfile
        from datetime import datetime as _datetime

        from fastapi.responses import Response as _Resp

        t4 = helpers["tier4"]
        buf = _io.BytesIO()
        stamp = _datetime.now().strftime("%Y%m%d_%H%M%S")
        counts: Dict[str, Any] = {}
        with _zipfile.ZipFile(buf, "w", _zipfile.ZIP_DEFLATED) as zf:
            fp = t4.tier4_path()
            if fp.is_file():
                raw = fp.read_bytes()
                zf.writestr("backgrounds/tier4.json", raw)
                try:
                    counts["tier4_entries"] = len(json.loads(raw.decode("utf-8")).get("entries") or [])
                except Exception:  # noqa: BLE001
                    counts["tier4_entries"] = -1
            else:
                doc = await t4.open_tier4()
                zf.writestr(
                    "backgrounds/tier4.json",
                    json.dumps(doc.to_raw(), ensure_ascii=False, indent=1),
                )
                counts["tier4_entries"] = len(doc.entries)
            for name in ("_migrations.json", "_channel_index.json"):
                p = t4.bg_dir() / name
                if p.is_file():
                    zf.writestr(f"backgrounds/{name}", p.read_bytes())
            archives = await helpers["enumerate_archives"]()
            arch_objs: Dict[str, Any] = {}
            for ck, raw in archives.items():
                try:
                    obj = json.loads(raw)
                except Exception:  # noqa: BLE001
                    continue
                if isinstance(obj, dict):
                    arch_objs[ck] = obj
            zf.writestr("archives.json", json.dumps(arch_objs, ensure_ascii=False))
            counts["archives"] = len(arch_objs)
            meta = {
                "bundle_version": 1,
                "type": "growth",
                "plugin": str(plugin.key),
                "exported_at": _datetime.now().isoformat(timespec="seconds"),
                "counts": counts,
            }
            zf.writestr("growth.bundle.json", json.dumps(meta, ensure_ascii=False, indent=1))
        return _Resp(
            content=buf.getvalue(),
            media_type="application/zip",
            headers={"Content-Disposition": f'attachment; filename="growth_bundle_{stamp}.zip"'},
        )

    @router.post("/api/growth/import", summary="导入生长包（只写 tier4 + 关系档案，不碰种子）")
    async def api_growth_import(request: Request) -> Dict[str, Any]:
        _check_key(request)
        body = await request.body()
        _MAX_ZIP_BYTES = 32 * 1024 * 1024
        if len(body) > _MAX_ZIP_BYTES:
            raise HTTPException(
                status_code=413,
                detail=f"导入包过大（{len(body) // 1024 // 1024} MB），上限 32 MB",
            )
        if not body:
            raise HTTPException(status_code=400, detail="没有收到文件内容")
        import io as _io
        import zipfile as _zipfile
        from datetime import datetime as _datetime

        try:
            zf = _zipfile.ZipFile(_io.BytesIO(body))
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=400, detail=_public_detail(f"不是有效的 zip 包：{exc}")) from exc
        names = set(zf.namelist())
        t4 = helpers["tier4"]

        if not any(n.endswith("tier4.json") or n == "archives.json" for n in names):
            raise HTTPException(status_code=400, detail="包里没有可导入的生长数据（tier4.json / archives.json）")

        # 导入前备份当前生长状态到 _growth_import_backups/<stamp>/
        # ⚠️ archives.json 会**整 blob 覆写**各频道关系档案（分数 / 标签 / 稳定印象），
        #    早先这里只备份了 tier4.json（注释还写成备份了 hit_stats，实际没有），
        #    导错包就是不可逆丢失。现在把现有关系档案一并落盘备份。
        stamp = _datetime.now().strftime("%Y%m%d_%H%M%S")
        bdir = _bg_dir().parent / "_growth_import_backups" / stamp
        bdir.mkdir(parents=True, exist_ok=True)
        if t4.tier4_path().is_file():
            shutil.copy2(t4.tier4_path(), bdir / "tier4.json")
        backed_arch = 0
        try:
            _cur_arch = await helpers["enumerate_archives"]()
            _objs: Dict[str, Any] = {}
            for _ck, _blob in (_cur_arch or {}).items():
                try:
                    _obj = json.loads(_blob)
                except Exception:  # noqa: BLE001
                    continue
                if isinstance(_obj, dict):
                    _objs[str(_ck)] = _obj
            if _objs:
                (bdir / "archives.json").write_text(
                    json.dumps(_objs, ensure_ascii=False), encoding="utf-8")
            backed_arch = len(_objs)
        except Exception as exc:  # noqa: BLE001
            plugin.logger.warning(f"[growth] 备份现有关系档案失败（继续导入）: {exc}")

        applied: List[str] = []

        # 0) archives.json **先解析校验**：早先是「tier4 先覆盖写盘，再解析 archives」，
        #    后者坏 JSON 时返回 400「整体拒绝」，但 tier4 已经换血了。
        arch_src = next((n for n in names if n == "archives.json"), None)
        arch_items: List[tuple] = []
        if arch_src:
            try:
                arch_obj = json.loads(zf.read(arch_src).decode("utf-8"))
            except Exception as exc:  # noqa: BLE001
                raise HTTPException(status_code=400, detail=_public_detail(f"archives.json 不是合法 JSON：{exc}")) from exc
            if not isinstance(arch_obj, dict):
                raise HTTPException(status_code=400, detail="archives.json 顶层必须是对象（chat_key → 档案）")
            for ck, blob in arch_obj.items():
                # 结构校验：缺 profiles 的 blob 会让 _open_archive 解析失败 → 按空档案继续
                # → 下一次 _store_archive 用空档案覆盖原始数据，等于清空该频道的档案。
                if not isinstance(blob, dict) or not isinstance(blob.get("profiles"), dict):
                    raise HTTPException(
                        status_code=400,
                        detail=f"archives.json 里 {ck} 的结构不对（缺 profiles 对象），已拒绝导入",
                    )
                # 再往下钻一层：profiles 里的每份档案自己也得是对象，且 score/day_net 之类
                # 的数值字段不能是字符串 —— 结构性错误会让 _open_archive 解析失败，
                # 之后按空档案继续、下一次写入就把原始数据覆盖成空的。
                _bad_profile = ""
                for _uid, _p in (blob.get("profiles") or {}).items():
                    if not isinstance(_p, dict):
                        _bad_profile = f"用户 {_uid} 的档案不是对象"
                        break
                    for _k in ("score", "day_net", "day_key", "last_adjust_at",
                               "created_at", "updated_at"):
                        _v = _p.get(_k)
                        if _v is None:
                            continue
                        if isinstance(_v, bool) or not isinstance(_v, (int, float)):
                            _bad_profile = f"用户 {_uid} 的 {_k}={_v!r} 不是数字"
                            break
                    if _bad_profile:
                        break
                if _bad_profile:
                    raise HTTPException(
                        status_code=400,
                        detail=f"archives.json 里 {ck}：{_bad_profile}，已拒绝导入",
                    )
                arch_items.append((str(ck), blob))

        # 1) tier4.json —— 校验后覆盖（原子写：tmp + os.replace）
        t4_src = next((n for n in names if n.endswith("tier4.json")), None)
        if t4_src:
            raw = zf.read(t4_src)
            try:
                data = json.loads(raw.decode("utf-8"))
            except Exception as exc:  # noqa: BLE001
                raise HTTPException(status_code=400, detail=_public_detail(f"tier4.json 不是合法 JSON：{exc}")) from exc
            if not isinstance(data, dict) or not isinstance(data.get("entries"), list):
                raise HTTPException(status_code=400, detail="tier4.json 结构不对（缺 entries）")
            # 条目级严格校验：运行期 from_raw 是**宽松**的（坏值回落默认），
            # 导入则必须整包拒绝 —— 否则一条 salience=99 / min_favor="高" 的坏条目
            # 会被静默掰成默认值写进库，事后无从发现。
            for _idx, _item in enumerate(data["entries"], 1):
                _bad = t4.Tier4Entry.validate_raw(_item)
                if _bad:
                    raise HTTPException(
                        status_code=400,
                        detail=f"tier4.json 第 {_idx} 条不合法：{_bad}（已拒绝导入，未改动任何文件）",
                    )
            _t4p = t4.tier4_path()
            _tmp = _t4p.with_suffix(_t4p.suffix + ".tmp")
            _tmp.write_bytes(raw)
            os.replace(_tmp, _t4p)      # 原子替换：写一半崩掉也不会毁掉既有 tier4
            applied.append(f"tier4.json（{len(data['entries'])} 条）")

        # 2) archives.json —— 回填各频道关系档案（只写 store 档案键，不碰种子/文件）
        #    ⚠️ 必须逐频道取 per-chat 锁：导入期间该频道可能正在收消息（sync_user_profile
        #    在锁内 read-modify-write）或被衰减循环结算，裸写就是 last-writer-wins 丢分。
        n_arch = 0
        for _ck, _blob in arch_items:
            async with await helpers["state_lock"](_ck):
                await helpers["write_archive"](_ck, json.dumps(_blob, ensure_ascii=False))
            n_arch += 1
        if arch_items:
            applied.append(f"关系档案（{n_arch} 频道）")

        # 3) 热度（.hit_stats.json）**不迁移** —— 见 api_growth_export 的说明。
        #    旧包里若带了这个文件，直接忽略（向后兼容）。

        reload_state = await _reload_after_write()
        plugin.logger.info(f"[growth] WebUI 导入生长包: {applied}（种子数据未改动）")
        return {
            "ok": True,
            "applied": applied,
            "backup_dir": str(bdir),
            "backed_up_archives": backed_arch,
            "note": "关系档案与 tier4 即时生效；热度（命中统计）不随生长包迁移，由本实例自行累积。",
            **reload_state,
        }

    @router.get("/api/backup/export", summary="导出整套人设（zip 包）")
    async def api_backup_export(request: Request):
        _check_key(request)
        import io as _io
        import zipfile as _zipfile
        from datetime import datetime as _datetime

        from fastapi.responses import Response as _Resp

        try:
            await memory.ensure_loaded()
        except ValueError as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        # 各档目录下的**全部** json 都要打进去：
        #   core/lore/world 是手工策划的种子，custom.json 是 WebUI「新建条目」写的。
        # 早先这里只列了固定四个文件，于是 WebUI 建的条目导出后在新实例导入即消失
        # —— 而运行时 memory 是按 *.json 全量读的，两边口径必须一致。
        files: List[Any] = []
        for _tier in _TIERS:
            _tdir = _bg_dir() / _tier
            if _tdir.is_dir():
                for _fp in sorted(_tdir.glob("*.json")):
                    files.append((f"backgrounds/{_tier}/{_fp.name}", _fp))
        files.append(("backgrounds/favor.json", _bg_dir() / "favor.json"))
        buf = _io.BytesIO()
        stamp = _datetime.now().strftime("%Y%m%d_%H%M%S")
        counts: Dict[str, Any] = {}
        with _zipfile.ZipFile(buf, "w", _zipfile.ZIP_DEFLATED) as zf:
            meta: Dict[str, Any] = {
                "bundle_version": 1,
                "plugin": str(plugin.key),
                "exported_at": _datetime.now().isoformat(timespec="seconds"),
                "files": counts,
            }
            for arc, fp in files:
                if not fp.is_file():
                    continue
                raw = fp.read_bytes()
                zf.writestr(arc, raw)
                try:
                    counts[arc] = len(json.loads(raw.decode("utf-8")).get("entries") or [])
                except Exception:  # noqa: BLE001
                    counts[arc] = -1
            try:
                from nekro_agent.models.db_preset import DBPreset

                for row in await DBPreset.all():
                    text = str(row.content or "").replace("\\n", "\n")
                    if not text.strip():
                        continue
                    zf.writestr(f"presets/{row.id}.md", text)
                    meta.setdefault("presets", []).append(
                        {"id": row.id, "name": row.name, "chars": len(text)},
                    )
            except Exception:  # noqa: BLE001
                pass
            zf.writestr("persona.bundle.json", json.dumps(meta, ensure_ascii=False, indent=1))
        data = buf.getvalue()
        return _Resp(
            content=data,
            media_type="application/zip",
            headers={
                "Content-Disposition": f'attachment; filename="persona_bundle_{stamp}.zip"'
            },
        )

    @router.post("/api/backup/import", summary="导入整套人设（zip 包）")
    async def api_backup_import(request: Request) -> Dict[str, Any]:
        _check_key(request)
        body = await request.body()
        _MAX_ZIP_BYTES = 32 * 1024 * 1024
        if len(body) > _MAX_ZIP_BYTES:
            raise HTTPException(
                status_code=413,
                detail=f"导入包过大（{len(body) // 1024 // 1024} MB），上限 32 MB",
            )
        if not body:
            raise HTTPException(status_code=400, detail="没有收到文件内容")
        import io as _io
        import zipfile as _zipfile
        from datetime import datetime as _datetime

        try:
            zf = _zipfile.ZipFile(_io.BytesIO(body))
        except Exception as exc:
            raise HTTPException(status_code=400, detail=_public_detail(f"不是有效的 zip 包：{exc}")) from exc
        names = set(zf.namelist())

        def _pick(arc: str) -> str | None:
            for cand in (f"backgrounds/{arc}", arc):
                if cand in names:
                    return cand
            return None

        def _target_for(nm: str):
            """包内路径 → (档内相对路径, 本地目标文件)；不认识的返回 None。

            支持 `backgrounds/tierK/<name>.json` 与历史包的裸 `tierK/<name>.json`，
            这样「只打四个固定文件」时期导出的老包仍然能导。
            """
            arc = nm.replace("\\", "/")
            if arc.startswith("backgrounds/"):
                arc = arc[len("backgrounds/"):]
            if arc == "favor.json":
                return "favor.json", _bg_dir() / "favor.json"
            parts = arc.split("/")
            if (len(parts) == 2 and parts[0] in _TIERS
                    and _SAFE_TIER_FILE.match(parts[1])):
                return arc, _bg_dir() / parts[0] / parts[1]
            return None

        targets: Dict[str, Any] = {}
        for _nm in sorted(names):
            _hit = _target_for(_nm)
            if _hit is not None:
                targets[_hit[0]] = _hit[1]
        parsed = {}
        for arc, dst in targets.items():
            src = _pick(arc)
            if src is None:
                continue
            raw = zf.read(src)
            try:
                data = json.loads(raw.decode("utf-8"))
            except Exception as exc:
                raise HTTPException(status_code=400, detail=_public_detail(f"{arc} 不是合法 JSON：{exc}"))
            if arc == "favor.json":
                _favor_bad = _validate_favor_cfg(data)
                if _favor_bad:
                    raise HTTPException(
                        status_code=400,
                        detail=f"{arc} 校验失败：{_favor_bad}（已拒绝导入，未改动任何文件）",
                    )
                n = -1
            else:
                if not isinstance(data, dict) or not isinstance(data.get("entries"), list):
                    raise HTTPException(status_code=400, detail=f"{arc} 结构不对（缺 entries）")
                bad = _validate_tier_entries(data["entries"])
                if bad:
                    raise HTTPException(
                        status_code=400,
                        detail=f"{arc} 有非法条目：{bad}（已拒绝导入，未改动任何文件）",
                    )
                n = len(data["entries"])
            parsed[arc] = (raw, dst, n)
        if not parsed:
            raise HTTPException(
                status_code=400,
                detail="包里没有可导入的人设文件（需要 backgrounds/tier1、tier2、tier3 或 favor.json）",
            )

        # ⚠️ 预设文本必须**先解析完**再动盘：旧实现在人设 JSON 已写完之后才读 presets/*.md，
        #    文件名不是合法 id / DB 报错就直接 500 —— 用户以为整包没生效，实际人设已经被换掉了。
        preset_payloads = []
        # Windows 打的 zip 用反斜杠，早先这些条目会被静默跳过（包里明明有预设却导不进来）
        _preset_names = sorted(
            x for x in names
            if x.replace("\\", "/").startswith("presets/") and x.endswith(".md")
        )
        for _n in _preset_names:
            _norm = _n.replace("\\", "/")
            _base = _norm.split("/")[-1]
            if _base.startswith("."):
                raise HTTPException(
                    status_code=400,
                    detail=f"预设文件名不合法：{_n}（已拒绝导入，未改动任何文件）",
                )
            try:
                _pid = int(_base.split(".")[0])
            except (ValueError, IndexError) as exc:
                raise HTTPException(
                    status_code=400,
                    detail=f"预设文件名不是合法 id：{_n}（已拒绝导入，未改动任何文件）",
                ) from exc
            _ptext = zf.read(_n).decode("utf-8", "replace")
            if _ptext.strip():
                # ⚠️ 必须与导出**同向**：导出是「字面 \n → 真换行」（webui.py 的导出段），
                #    这里原来是反的（字面 \n → \\n），两者不互逆：md 里手写的字面 \n
                #    往返一次会变成两个反斜杠。现在两边同向，往返幂等，
                #    且对「库里存真换行」的现有预设完全无影响（本就不含字面 \n）。
                preset_payloads.append((_pid, _ptext.replace('\\n', '\n')))
        # 导入前先备份当前文件
        stamp = _datetime.now().strftime("%Y%m%d_%H%M%S")
        bdir = _bg_dir().parent / "persona_import_backups" / stamp
        bdir.mkdir(parents=True, exist_ok=True)
        for arc, dst in targets.items():
            if dst.is_file():
                shutil.copy2(dst, bdir / arc.replace("/", "_"))

        applied = []
        for arc, (raw, dst, n) in parsed.items():
            dst.parent.mkdir(parents=True, exist_ok=True)
            # 原子写：tmp + replace。原先直接 write_bytes，写到一半崩掉会留下半截
            # JSON，整个记忆系统就拒绝加载了（_dump 早就是原子写，这里是漏网路径）。
            _fd, _tmp = tempfile.mkstemp(prefix=f".{dst.name}.", suffix=".tmp", dir=str(dst.parent))
            try:
                with os.fdopen(_fd, "wb") as _fh:
                    _fh.write(raw)
                os.replace(_tmp, dst)
            except Exception:
                try:
                    os.unlink(_tmp)
                except OSError:
                    pass
                raise
            applied.append(f"{arc}（{n} 条）" if n >= 0 else f"{arc}")
        # 人设文件此时已经落盘：预设部分即便失败也不能再让整个请求 500
        #（否则用户会以为什么都没生效）。逐条记录成功/失败并如实返回。
        preset_applied = []
        preset_failed = []
        try:
            from nekro_agent.models.db_preset import DBPreset
        except Exception as exc:  # noqa: BLE001
            DBPreset = None
            preset_failed.append(f"无法加载 DBPreset：{exc}")
        if DBPreset is not None:
            for pid, content_db in preset_payloads:
                try:
                    row = await DBPreset.get_or_none(id=pid)
                    if row:
                        row.content = content_db
                        await row.save(update_fields=["content"])
                    else:
                        await DBPreset.create(id=pid, name=f"导入人设 {pid}", content=content_db)
                    preset_applied.append(pid)
                except Exception as exc:  # noqa: BLE001
                    preset_failed.append(f"{pid}: {exc}")
        if preset_failed:
            plugin.logger.error(f"[persona] 人设已导入，但预设写入失败：{preset_failed}")

        reload_state = await _reload_after_write()
        plugin.logger.info(
            f"[persona] WebUI 导入整套人设: {list(parsed)} preset={preset_applied}",
        )
        return {
            "ok": True,
            "applied": applied,
            "preset_applied": preset_applied,
            "preset_failed": preset_failed,
            "backup_dir": str(bdir),
            **reload_state,
        }

    @router.post("/api/heat/reset", summary="重置热度（清空命中统计）")
    async def api_heat_reset(request: Request) -> Dict[str, Any]:
        """清空命中统计（.hit_stats.json），2/3 档排序立刻回到「无热度」状态。

        只影响**当前作用域**（共享目录，或某个账号目录）—— 与热度本身的分目录语义一致。
        重置后所有条目 heat 回到 1.0，等下一次命中重新累积。
        """
        _check_key(request)
        st = memory._state()
        before = len(st.get("hit_stats") or {})
        st["hit_stats"] = {}
        st["hit_dirty"] = False
        st["query_cache"] = {}
        try:
            memory._save_hit_stats(st)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=500, detail=_public_detail(f"写入命中统计失败：{exc}")) from exc
        plugin.logger.info(f"[persona] WebUI 重置热度：清空 {before} 条命中统计（{st.get('dir')}）")
        return {"ok": True, "cleared": before, "dir": str(st.get("dir") or "")}

    @router.post("/api/reload", summary="重载背景记忆")
    async def api_reload(request: Request) -> Dict[str, Any]:
        _check_key(request)
        try:
            await memory.ensure_loaded(force=True)
        except ValueError as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        return {"ok": True, "status": memory.status()}

    return router


# 旧版内联页面保留为回退；新版深色极简界面位于同目录 webui.html。
_LEGACY_PAGE_HTML = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>人格记忆</title>
<style>
  :root{
    --bg:#f6f7f9; --card:#fff; --fg:#1f2328; --muted:#6b7280; --line:#e5e7eb;
    --accent:#c2410c; --accent-soft:#fff1e8; --ok:#15803d; --warn:#b45309;
  }
  @media (prefers-color-scheme: dark){
    :root{ --bg:#16181d; --card:#1e2127; --fg:#e6e8eb; --muted:#9aa1ab; --line:#2c3038;
           --accent:#fb923c; --accent-soft:#33261c; --ok:#4ade80; --warn:#fbbf24; }
  }
  *{box-sizing:border-box}
  body{margin:0;background:var(--bg);color:var(--fg);
    font:14px/1.6 -apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif}
  .wrap{max-width:1180px;margin:0 auto;padding:20px}
  h1{font-size:19px;margin:0 0 4px}
  .sub{color:var(--muted);font-size:12px;margin-bottom:16px}
  .tabs{display:flex;gap:6px;flex-wrap:wrap;margin-bottom:16px}
  .tab{padding:7px 14px;border-radius:8px;border:1px solid var(--line);background:var(--card);
    cursor:pointer;font-size:13px;color:var(--fg)}
  .tab.on{background:var(--accent);border-color:var(--accent);color:#fff}
  .card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:16px;margin-bottom:14px}
  .card h2{font-size:14px;margin:0 0 12px;font-weight:600}
  .grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px}
  .stat{background:var(--accent-soft);border-radius:9px;padding:10px 12px}
  .stat b{display:block;font-size:19px;line-height:1.3}
  .stat span{color:var(--muted);font-size:12px}
  table{width:100%;border-collapse:collapse;font-size:13px}
  th,td{text-align:left;padding:8px 9px;border-bottom:1px solid var(--line);vertical-align:top}
  th{color:var(--muted);font-weight:500;font-size:12px;white-space:nowrap}
  tr:hover td{background:rgba(128,128,128,.06)}
  .pill{display:inline-block;padding:1px 8px;border-radius:20px;font-size:11px;
    background:var(--accent-soft);color:var(--accent);border:1px solid var(--accent);white-space:nowrap}
  .pill.gray{background:transparent;color:var(--muted);border-color:var(--line)}
  .pill.ok{background:transparent;color:var(--ok);border-color:var(--ok)}
  button{font:inherit;font-size:12px;padding:5px 11px;border-radius:7px;
    border:1px solid var(--line);background:var(--card);color:var(--fg);cursor:pointer}
  button:hover{border-color:var(--accent);color:var(--accent)}
  button.primary{background:var(--accent);border-color:var(--accent);color:#fff}
  select,input{font:inherit;font-size:12px;padding:5px 8px;border-radius:7px;
    border:1px solid var(--line);background:var(--card);color:var(--fg)}
  pre{background:var(--bg);border:1px solid var(--line);border-radius:9px;padding:12px;
    white-space:pre-wrap;word-break:break-word;font-size:12px;line-height:1.65;margin:0;max-height:520px;overflow:auto}
  .muted{color:var(--muted)}
  .row{display:flex;gap:8px;align-items:center;flex-wrap:wrap}
  .hidden{display:none}
  .toast{position:fixed;right:18px;bottom:18px;background:var(--accent);color:#fff;
    padding:9px 15px;border-radius:9px;font-size:13px;opacity:0;transition:.25s;pointer-events:none}
  .toast.on{opacity:1}
</style>
</head>
<body>
<div class="wrap">
  <h1>人格记忆</h1>
  <div class="sub" id="sub">加载中…</div>

  <div class="tabs">
    <div class="tab on" data-t="ov">概览</div>
    <div class="tab" data-t="mem">背景记忆</div>
    <div class="tab" data-t="fav">好感度档案</div>
    <div class="tab" data-t="rule">规则与门控</div>
    <div class="tab" data-t="pv">注入预览</div>
  </div>

  <div id="ov"></div>

  <div id="mem" class="hidden">
    <div class="card">
      <div class="row" style="justify-content:space-between">
        <h2 style="margin:0">背景记忆条目</h2>
        <div class="row">
          <select id="tierSel"><option value="tier1">1 档（常驻）</option><option value="tier2">2 档（按需检索）</option></select>
          <button class="primary" onclick="reloadMem()">重载数据</button>
        </div>
      </div>
    </div>
    <div class="card">
      <table id="memTbl"></table>
      <div class="sub" id="memHint" style="margin:12px 0 0"></div>
    </div>

    <div class="card">
      <h2>新建条目（写入该档的 custom.json）</h2>
      <div class="row"><label class="muted" style="width:96px">ID</label>
        <input id="nwId" style="width:200px">
        <label class="muted" style="width:60px">标题</label>
        <input id="nwTitle" style="flex:1"></div>
      <div class="row" style="margin-top:8px"><label class="muted" style="width:96px">门槛</label>
        <input id="nwMin" type="number" value="0" min="0" max="100" style="width:80px">
        <span class="muted" style="font-size:12px">0 = 公开（任意用户可见）</span>
        <label class="muted" style="width:60px;margin-left:18px">权重</label>
        <input id="nwWeight" type="number" value="1" style="width:70px"></div>
      <div class="row" style="margin-top:8px"><label class="muted" style="width:96px">触发</label>
        <input id="nwTrig" style="flex:1"
         ></div>
      <div class="row" style="margin-top:8px;align-items:flex-start">
        <label class="muted" style="width:96px">内容</label>
        <textarea id="nwContent" style="flex:1;min-height:90px"></textarea></div>
      <div class="row" style="margin-top:8px">
        <button class="primary" onclick="newEntry()">创建条目</button></div>
    </div>
  </div>

  <div id="fav" class="hidden">
    <div class="card">
      <div class="row" style="justify-content:space-between">
        <h2 style="margin:0">好感度档案索引</h2>
        <button class="primary" onclick="loadIndex()">刷新</button>
      </div>
      <div class="row" style="margin-top:10px">
        <input id="ixQ" style="flex:1;min-width:220px"
          oninput="ixDebounced()">
        <select id="ixKind" onchange="loadIndex()">
          <option value="all">全部分类</option>
          <option value="group">群聊</option>
          <option value="private">私聊</option>
          <option value="other">其他</option>
        </select>
        <select id="ixSort" onchange="renderIndex()">
          <option value="score">按好感度</option>
          <option value="recent">按最近互动</option>
          <option value="name">按昵称</option>
          <option value="channel">按频道</option>
        </select>
      </div>
      <div class="sub" id="ixStat" style="margin:10px 0 0">加载中…</div>
    </div>
    <div class="card"><table id="favTbl"></table></div>
  </div>

  <div id="rule" class="hidden">
    <div class="card">
      <div class="row" style="justify-content:space-between">
        <h2 style="margin:0">好感度衰减</h2>
        <button class="primary" onclick="saveDecay()">保存</button>
      </div>
      <div class="row" style="margin-top:10px">
        <label class="muted" style="width:150px">启用衰减</label>
        <input type="checkbox" id="dcEnabled">
        <label class="muted" style="width:150px;margin-left:18px">不跌破当前档位</label>
        <input type="checkbox" id="dcKeep"></div>
      <div class="row" style="margin-top:8px">
        <label class="muted" style="width:150px">不互动即衰减</label>
        <input type="number" id="dcHours" min="1" max="168" style="width:90px">
        <span class="muted">小时</span>
        <label class="muted" style="width:110px;margin-left:24px">每次扣除</label>
        <input type="number" id="dcPercent" min="1" max="50" style="width:90px">
        <span class="muted">%（向上取整）</span></div>
      <div class="sub" style="margin:10px 0 0">好感度 ≤ 0 不参与衰减；后台每 30 分钟结算一次，判定粒度按上面的间隔。</div>
    </div>

    <div class="card">
      <div class="row" style="justify-content:space-between">
        <h2 style="margin:0">档位与解锁指引（门控）</h2>
        <div class="row">
          <label class="muted">启用门控</label><input type="checkbox" id="gtEnabled">
          <button class="primary" onclick="saveStages()">保存档位</button></div>
      </div>
      <div class="sub" style="margin:8px 0">档位是<b>分数区间 → 解锁提示</b>的映射；每条背景记忆的「门槛」决定它在哪个档位起可见。未达标的记忆不会注入正文，而是列入「回避块」。</div>
      <table id="stageTbl"></table>
    </div>

    <div class="card">
      <div class="row" style="justify-content:space-between">
        <h2 style="margin:0">回避块（未达标记忆的提示词）</h2>
        <button class="primary" onclick="saveAvoid()">保存</button>
      </div>
      <div class="row" style="margin-top:8px"><label class="muted" style="width:90px">块标题</label>
        <input id="avHeader" style="width:240px"></div>
      <div class="row" style="margin-top:8px;align-items:flex-start">
        <label class="muted" style="width:90px">说明</label>
        <textarea id="avNote" style="flex:1;min-height:80px"></textarea></div>
    </div>

    <div class="card">
      <div class="row" style="justify-content:space-between">
        <h2 style="margin:0">好感度增减 rubric（注入给模型）</h2>
        <button class="primary" onclick="saveRubric()">保存</button>
      </div>
      <div class="row" style="margin-top:8px"><label class="muted" style="width:90px">块标题</label>
        <input id="rbHeader" style="width:240px"></div>
      <div class="row" style="margin-top:8px"><label class="muted" style="width:90px">引导语</label>
        <input id="rbIntro" style="flex:1"></div>
      <table id="rbTbl" style="margin-top:10px"></table>
      <div class="row" style="margin-top:8px"><button onclick="addRubricRow()">+ 加一条量级</button></div>
      <div class="row" style="margin-top:12px;align-items:flex-start">
        <label class="muted" style="width:90px">规则<br><span style="font-size:11px">每行一条</span></label>
        <textarea id="rbRules" style="flex:1;min-height:80px"></textarea></div>
    </div>
  </div>

  <div id="pv" class="hidden">
    <div class="card">
      <div class="row">
        <h2 style="margin:0">注入预览</h2>
        <select id="pvCh" style="max-width:360px" onchange="doPreview()"></select>
        <label class="muted">好感度</label>
        <input id="pvScore" type="number" value="0" min="-100" max="100" style="width:80px" oninput="pvDebounced()">
        <button class="primary" onclick="doPreview()">预览</button>
      </div>
      <div class="sub" style="margin:10px 0 0">按给定分数渲染<b>真实</b>注入文本（含门控回避块与 rubric），用于调参。切换频道/改分数后会自动重新渲染。</div>
    </div>
    <div class="card"><pre id="pvOut">（点「预览」）</pre></div>
  </div>
</div>
<div class="toast" id="toast"></div>

<script>
const $ = s => document.querySelector(s);
const esc = s => String(s==null?'':s).replace(/[&<>"]/g, c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const KEY = () => localStorage.getItem('np_key') || '';

async function api(path, opt){
  opt = opt || {};
  opt.headers = Object.assign({'Content-Type':'application/json'}, opt.headers||{});
  if (KEY()) opt.headers['X-WebUI-Key'] = KEY();
  const r = await fetch('./api/'+path, opt);
  if (r.status === 401){
    const k = prompt('该插件 WebUI 需要访问密钥：');
    if (k){ localStorage.setItem('np_key', k); return api(path, opt); }
    throw new Error('未授权');
  }
  if (!r.ok) throw new Error((await r.text()).slice(0,200));
  return r.json();
}
function toast(m){ const t=$('#toast'); t.textContent=m; t.classList.add('on'); setTimeout(()=>t.classList.remove('on'),1800); }

function showTab(t){
  if (!['ov','mem','fav','rule','pv'].includes(t)) t = 'ov';
  document.querySelectorAll('.tab').forEach(x=>x.classList.toggle('on', x.dataset.t===t));
  ['ov','mem','fav','rule','pv'].forEach(k=>$('#'+k).classList.toggle('hidden', k!==t));
  if (t==='mem') loadMem();
  if (t==='fav') loadIndex();
  if (t==='rule') loadRule();
  if (t==='pv') loadChannels().then(doPreview);
}
document.querySelectorAll('.tab').forEach(el=>el.onclick=()=>{ location.hash = el.dataset.t; });
window.addEventListener('hashchange', ()=>showTab((location.hash||'#ov').slice(1)));

// ---------- 概览 ----------
async function loadOverview(){
  const d = await api('overview');
  $('#sub').textContent = `${d.plugin.name} · ${d.plugin.key} · v${d.plugin.version} · ` +
    `${d.plugin.enabled?'已启用':'已停用'} · allow_sleep=${d.plugin.allow_sleep}`;
  const g = d.gating;
  let html = `
  <div class="card"><h2>占用</h2><div class="grid">
    <div class="stat"><b>${d.tier1.count}</b><span>1 档条目</span></div>
    <div class="stat"><b>${d.tier1.chars}<span class="muted">/${d.tier1.limit}</span></b><span>1 档字符</span></div>
    <div class="stat"><b>${d.tier2.count}</b><span>2 档条目</span></div>
    <div class="stat"><b>${d.tier2.vectors}<span class="muted">/${d.tier2.count}</span></b><span>已向量化</span></div>
    <div class="stat"><b>${d.tier2.threshold}</b><span>向量阈值</span></div>
    <div class="stat"><b>${d.tier2.max_entries}<span class="muted">条/${d.tier2.max_chars}字</span></b><span>2 档单轮预算</span></div>
  </div></div>

  <div class="card"><h2>好感度衰减 ${d.decay.enabled ? '<span class="pill ok">已启用</span>' : '<span class="pill gray">已关闭</span>'}</h2>
  <div class="grid">
    <div class="stat"><b>${d.decay.interval_hours}<span class="muted"> 小时</span></b><span>不互动即衰减</span></div>
    <div class="stat"><b>-${d.decay.percent}<span class="muted">%</span></b><span>每次扣除（向上取整）</span></div>
    <div class="stat"><b>${d.decay.keep_tier ? '是' : '否'}</b><span>不跌破当前档位</span></div>
  </div>
  <div class="sub" style="margin:10px 0 0">好感度 ≤ 0 不参与衰减；后台每 30 分钟结算一次，判定粒度按上面间隔。
  <a href="#rule" style="margin-left:8px">去「规则与门控」修改 →</a></div>
  </div>

  <div class="card"><h2>防黑：索要好感度</h2>
  <div class="sub" style="margin:0">${esc(d.guard.desc)}。提示词层规则 + 代码层拦截双重防护；只拦正向调整，不拦扣分。</div>
  </div>

  <div class="card"><h2>好感度门控 ${g.enabled?'<span class="pill ok">已启用</span>':'<span class="pill gray">已关闭</span>'}</h2>
  <table><tr><th>档位</th><th>分数区间</th><th>互动指引</th></tr>
  ${(g.stages||[]).map((st,i,arr)=>{
    const prev = i===0 ? null : arr[i-1].max;
    const lo = prev===null ? '≤' : (prev+1)+' ~ ';
    const hi = st.max>=999 ? '∞' : st.max;
    const range = st.max>=999 ? (prev+1)+' ~ ∞' : (prev===null ? '≤ '+st.max : lo+hi);
    return `<tr><td><span class="pill">${esc(st.name)}</span></td><td>${range}</td>
      <td class="muted">${esc(st.unlock_hint||'')}</td></tr>`;
  }).join('')}
  </table>
  <div class="sub" style="margin:12px 0 0"><a href="#rule">去「规则与门控」修改档位与解锁指引 →</a><br>门槛分布 —— 1 档：${Object.entries(g.dist_tier1||{}).map(([k,v])=>`${k}分:${v}条`).join(' · ')}
   ｜ 2 档：${Object.entries(g.dist_tier2||{}).map(([k,v])=>`${k}分:${v}条`).join(' · ')}</div>
  </div>

  <div class="card"><h2>好感度增减 rubric（注入给模型）</h2>
  <table><tr><th>量级</th><th>场景</th></tr>
  ${((g.rubric||{}).items||[]).map(it=>`<tr><td><span class="pill">${esc(it[0])}</span></td><td>${esc(it[1])}</td></tr>`).join('')}
  </table>
  <div class="sub" style="margin:10px 0 0">${esc(((g.rubric||{}).rules||[]).join('；'))}</div>
  </div>`;
  $('#ov').innerHTML = html;
}

// ---------- 背景记忆 ----------
let ENTRY_CACHE = {};
let OPEN_ENTRY = {};          // tier:id -> 展开编辑
function toggleEntry(k){ OPEN_ENTRY[k] = !OPEN_ENTRY[k]; loadMem(); }

async function loadMem(){
  const d = await api('entries');
  ENTRY_CACHE = d;
  const tier = $('#tierSel').value;
  const list = d[tier] || [];
  let h = `<tr><th>ID</th><th>标题</th><th>门槛<br><span class="muted" style="font-size:11px;font-weight:400">0=公开</span></th>`
         + `<th>权重</th><th>启用</th><th>触发</th><th>操作</th></tr>`;
  for (const e of list){
    const k = e.tier+':'+e.id;
    const open = !!OPEN_ENTRY[k];
    const trig = e.tier==='tier1'
      ? (e.trigger? `<span class="muted">${esc(String(e.trigger).slice(0,26))}</span>` : '<span class="muted">—</span>')
      : ((e.triggers||[]).length
          ? (e.triggers||[]).slice(0,3).map(x=>`<span class="pill gray">${esc(x)}</span>`).join(' ')
            + ((e.triggers||[]).length>3?` <span class="muted">+${e.triggers.length-3}</span>`:'')
          : '<span class="muted">—</span>');
    h += `<tr>
      <td class="muted">${esc(e.id)}</td>
      <td><input value="${esc(e.title||'')}" style="width:120px"
           onchange="setEntry('${e.tier}','${e.id}','title',this.value)"></td>
      <td><input type="number" min="0" max="100" value="${Number(e.min_favor)||0}"
             style="width:66px" title="0 = 公开；也可填任意数值"
             onchange="setEntry('${e.tier}','${e.id}','min_favor',this.value)">
           <div class="muted" style="font-size:11px">${Number(e.min_favor)<=0 ? '公开' : esc(e.stage||'')}</div></td>
      <td><input type="number" value="${e.weight}" style="width:54px"
           onchange="setEntry('${e.tier}','${e.id}','weight',this.value)"></td>
      <td><input type="checkbox" ${e.enabled?'checked':''}
           onchange="setEntry('${e.tier}','${e.id}','enabled',this.checked)"></td>
      <td>${trig}</td>
      <td><button onclick="toggleEntry('${k}')">${open?'收起':'编辑'}</button>
          <button onclick="delEntry('${e.tier}','${e.id}')" style="color:#c0392b">删除</button></td>
    </tr>`;
    if (open){
      h += `<tr><td colspan="7" style="background:var(--bg2,#fafafa)">
        <div class="row" style="margin:6px 0">
          <label class="muted" style="width:110px">${e.tier==='tier1'?'触发说明':'触发词'}</label>
          <input style="flex:1" value="${
            e.tier==='tier1' ? esc(e.trigger||'') : esc((e.triggers||[]).join(','))
          }" onchange="setEntry('${e.tier}','${e.id}','${
            e.tier==='tier1' ? 'trigger' : 'triggers'
          }',this.value)">
        </div>
        <div class="row" style="align-items:flex-start;margin:6px 0">
          <label class="muted" style="width:110px">内容</label>
          <textarea style="flex:1;min-height:110px" onchange="setEntry('${e.tier}','${e.id}','content',this.value)">${esc(e.content||'')}</textarea>
        </div>
        <div class="sub">文件：${esc(e.file||'')} ｜ 档位：${esc(e.stage||'')} ｜ 修改后自动重载生效</div>
      </td></tr>`;
    }
  }
  $('#memTbl').innerHTML = h || '<tr><td class="muted">该档暂无条目</td></tr>';
  renderThresholdHint(d.stages || []);
}

function renderThresholdHint(stages){
  const box = $('#memHint');
  if (!box) return;
  const parts = ['<b>0</b> = 公开（任意用户可见）'];
  stages.forEach(st=>{
    const lo = 1;   // 档位区间下界由上一档推导，这里只给上界对照
    parts.push(`<b>${st.max}</b> = ${esc(st.name)}`);
  });
  box.innerHTML = '门槛对照：' + parts.join(' · ') +
    '　（档位区间与解锁指引可在「<a href="#rule">规则与门控</a>」里改）';
}

async function setEntry(tier,id,field,val){
  try{
    const body = {tier,id};
    if (field==='enabled') body[field] = !!val;
    else if (field==='title'||field==='content'||field==='trigger'||field==='triggers') body[field] = String(val);
    else body[field] = Number(val);
    await api('entry', {method:'POST', body: JSON.stringify(body)});
    toast(`已更新 ${id} · ${field}`);
    loadMem(); loadOverview();
  }catch(e){ toast('失败：'+e.message); }
}

async function delEntry(tier,id){
  if (!confirm(`确认删除条目「${id}」？条目会移入回收站，可稍后恢复。`)) return;
  try{
    await api('entry', {method:'POST', body: JSON.stringify({tier,id,op:'delete'})});
    toast(`已删除 ${id}`); delete OPEN_ENTRY[tier+':'+id]; loadMem(); loadOverview();
  }catch(e){ toast('失败：'+e.message); }
}

async function newEntry(){
  const tier = $('#tierSel').value;
  const trigRaw = $('#nwTrig').value.trim();
  const body = {
    tier,
    id: $('#nwId').value.trim(),
    title: $('#nwTitle').value.trim(),
    content: $('#nwContent').value.trim(),
    min_favor: Number($('#nwMin').value||0),
    weight: Number($('#nwWeight').value||1),
  };
  if (!body.content){ toast('内容不能为空'); return; }
  if (tier==='tier1') body.trigger = trigRaw;
  else body.triggers = trigRaw ? trigRaw.split(/[,，]/).map(s=>s.trim()).filter(Boolean) : [];
  try{
    const r = await api('entry/new', {method:'POST', body: JSON.stringify(body)});
    toast(`已创建 ${r.id}`);
    ['#nwId','#nwTitle','#nwTrig','#nwContent'].forEach(s=>$(s).value='');
    loadMem(); loadOverview();
  }catch(e){ toast('失败：'+e.message); }
}

async function reloadMem(){
  try{ await api('reload',{method:'POST'}); toast('已重载'); loadOverview(); }
  catch(e){ toast('失败：'+e.message); }
}

// ---------- 规则与门控 ----------
let RULE_CACHE = null;
async function loadRule(){
  const d = await api('overview');
  RULE_CACHE = d;
  const dec = d.decay || {}, g = d.gating || {};
  $('#dcEnabled').checked = !!dec.enabled;
  $('#dcKeep').checked = !!dec.keep_tier;
  $('#dcHours').value = dec.interval_hours;
  $('#dcPercent').value = dec.percent;
  $('#gtEnabled').checked = !!g.enabled;

  // 档位表
  const stages = g.stages || [];
  let sh = `<tr><th>分数上限</th><th>档位名</th><th>解锁指引（注入给模型）</th></tr>`;
  stages.forEach((st,i)=>{
    sh += `<tr>
      <td><input type="number" value="${st.max}" style="width:88px" data-si="${i}" data-sf="max"></td>
      <td><input value="${esc(st.name||'')}" style="width:110px" data-si="${i}" data-sf="name"></td>
      <td><input value="${esc(st.unlock_hint||'')}" style="flex:1;width:100%" data-si="${i}" data-sf="unlock_hint"></td>
    </tr>`;
  });
  $('#stageTbl').innerHTML = sh;

  // 回避块
  const av = g.avoid || {};
  $('#avHeader').value = av.header || '';
  $('#avNote').value = av.note || '';

  // rubric
  const rb = g.rubric || {};
  $('#rbHeader').value = rb.header || '';
  $('#rbIntro').value = rb.intro || '';
  renderRubricRows(rb.items || []);
  $('#rbRules').value = (rb.rules || []).join('\n');
}
function renderRubricRows(items){
  let h = `<tr><th style="width:130px">量级</th><th>场景</th><th style="width:60px"></th></tr>`;
  items.forEach((it,i)=>{
    h += `<tr>
      <td><input value="${esc(it[0]||'')}" style="width:110px" data-ri="${i}" data-rf="0"></td>
      <td><input value="${esc(it[1]||'')}" style="flex:1;width:100%" data-ri="${i}" data-rf="1"></td>
      <td><button onclick="delRubricRow(${i})">删</button></td></tr>`;
  });
  $('#rbTbl').innerHTML = h;
}
function addRubricRow(){
  const items = collectRubric();
  items.push(['', '']);
  renderRubricRows(items);
}
function delRubricRow(i){
  const items = collectRubric(); items.splice(i,1); renderRubricRows(items);
}
function collectRubric(){
  const out = [];
  document.querySelectorAll('#rbTbl input[data-ri]').forEach(el=>{
    const i = Number(el.dataset.ri), f = Number(el.dataset.rf);
    if (!out[i]) out[i] = ['',''];
    out[i][f] = el.value;
  });
  return out.filter(x=>x && (x[0]||x[1]));
}
function collectStages(){
  const out = [];
  document.querySelectorAll('#stageTbl input[data-si]').forEach(el=>{
    const i = Number(el.dataset.si), f = el.dataset.sf;
    if (!out[i]) out[i] = {max:999,name:'',unlock_hint:''};
    out[i][f] = (f==='max') ? Number(el.value||0) : el.value;
  });
  return out;
}

async function saveDecay(){
  try{
    await api('config', {method:'POST', body: JSON.stringify({
      DECAY_ENABLED: $('#dcEnabled').checked,
      DECAY_KEEP_TIER: $('#dcKeep').checked,
      DECAY_INTERVAL_HOURS: Number($('#dcHours').value||8),
      DECAY_PERCENT: Number($('#dcPercent').value||5),
    })});
    toast('衰减设置已保存'); loadRule(); loadOverview();
  }catch(e){ toast('失败：'+e.message); }
}
async function saveStages(){
  try{
    await api('gating', {method:'POST', body: JSON.stringify({
      enabled: $('#gtEnabled').checked, stages: collectStages(),
    })});
    toast('档位已保存'); loadRule(); loadOverview();
  }catch(e){ toast('失败：'+e.message); }
}
async function saveAvoid(){
  try{
    await api('gating', {method:'POST', body: JSON.stringify({
      avoid: {header: $('#avHeader').value, note: $('#avNote').value},
    })});
    toast('回避块已保存'); loadRule();
  }catch(e){ toast('失败：'+e.message); }
}
async function saveRubric(){
  try{
    await api('gating', {method:'POST', body: JSON.stringify({
      rubric: {
        header: $('#rbHeader').value,
        intro: $('#rbIntro').value,
        items: collectRubric(),
        rules: $('#rbRules').value.split('\n').map(s=>s.trim()).filter(Boolean),
      },
    })});
    toast('rubric 已保存'); loadRule();
  }catch(e){ toast('失败：'+e.message); }
}

// ---------- 好感度档案索引 ----------
let IDX_CACHE = null;
let _ixTimer = null;
function ixDebounced(){ clearTimeout(_ixTimer); _ixTimer = setTimeout(loadIndex, 300); }

async function loadIndex(){
  const q = $('#ixQ').value.trim();
  const kind = $('#ixKind').value;
  try{
    const d = await api(`index?q=${encodeURIComponent(q)}&kind=${kind}`);
    IDX_CACHE = d;
    renderIndex();
  }catch(e){ $('#ixStat').textContent = '加载失败：'+e.message; }
}

function ixTime(ts){
  ts = Number(ts||0); if (!ts) return '—';
  const diff = Date.now()/1000 - ts;
  if (diff < 60) return '刚刚';
  if (diff < 3600) return Math.floor(diff/60)+' 分钟前';
  if (diff < 86400) return Math.floor(diff/3600)+' 小时前';
  if (diff < 86400*30) return Math.floor(diff/86400)+' 天前';
  const d = new Date(ts*1000);
  return `${d.getFullYear()}-${String(d.getMonth()+1).padStart(2,'0')}-${String(d.getDate()).padStart(2,'0')}`;
}

function renderIndex(){
  const d = IDX_CACHE; if (!d) return;
  const KIND = {group:'群聊', private:'私聊', other:'其他'};
  const ORDER = {group:0, private:1, other:2};
  const ch = d.chan_counts||{}, pe = d.people||{};
  $('#ixStat').innerHTML =
    `<b>群聊</b> ${ch.group||0} 频道 / ${pe.group||0} 人　·　` +
    `<b>私聊</b> ${ch.private||0} 频道 / ${pe.private||0} 人` +
    ((ch.other||0) ? `　·　<b>其他</b> ${ch.other} 频道 / ${pe.other||0} 人` : '') +
    `　→　当前筛选 <b>${d.filtered}</b> / ${d.total} 条`;

  let items = (d.items||[]).slice();
  const sort = $('#ixSort').value;
  if (sort==='recent') items.sort((a,b)=>(b.last_interaction_at||0)-(a.last_interaction_at||0));
  else if (sort==='name') items.sort((a,b)=>String(a.display_name||a.user_id)
        .localeCompare(String(b.display_name||b.user_id),'zh'));
  else if (sort==='channel') items.sort((a,b)=>a.chat_key.localeCompare(b.chat_key)||b.score-a.score);
  else items.sort((a,b)=>b.score-a.score);

  if (!items.length){ $('#favTbl').innerHTML = '<tr><td class="muted">没有匹配的档案</td></tr>'; return; }

  const groups = {};
  items.forEach(it=>{ const k = it.kind+'|'+it.chat_key; (groups[k]=groups[k]||[]).push(it); });
  const keys = Object.keys(groups).sort((a,b)=>{
    const [ka,ca]=a.split('|'), [kb,cb]=b.split('|');
    return (ORDER[ka]??9)-(ORDER[kb]??9) || ca.localeCompare(cb);
  });

  let h = `<tr><th>用户</th><th>好感度</th><th>档位</th><th>标签 / 印象</th>`
        + `<th style="width:110px">最近互动</th><th style="width:96px"></th></tr>`;
  let lastKind = null;
  for (const key of keys){
    const i = key.indexOf('|');
    const k = key.slice(0,i), ck = key.slice(i+1);
    if (k !== lastKind){
      h += `<tr><td colspan="6" style="background:var(--surface-3,#eef1f4);font-weight:700;`
         + `padding:8px 10px">${KIND[k]||k}</td></tr>`;
      lastKind = k;
    }
    h += `<tr><td colspan="6" class="muted" style="background:var(--surface-2,#fafbfc);`
       + `font-size:12px;padding:5px 10px">${esc(ck)}　（${groups[key].length} 人）</td></tr>`;
    for (const it of groups[key]){
      h += `<tr>
        <td><b>${esc(it.display_name||'(无名)')}</b><div class="muted">${esc(it.user_id)}</div></td>
        <td><input type="number" value="${it.score}" min="-100" max="100" style="width:74px"
             onchange="setScore('${esc(it.chat_key)}','${esc(it.user_id)}',this.value)"></td>
        <td><span class="pill">${esc(it.stage)}</span></td>
        <td>${(it.tags||[]).map(x=>`<span class="pill gray">${esc(x)}</span>`).join(' ')}
            ${it.summary?`<div>${esc(it.summary)}</div>`:''}</td>
        <td class="muted">${ixTime(it.last_interaction_at)}</td>
        <td><button onclick="pvFor('${esc(it.chat_key)}',${it.score})">预览注入</button></td>
      </tr>`;
    }
  }
  $('#favTbl').innerHTML = h;
}

// ---------- 频道列表（供注入预览用） ----------
async function loadChannels(){
  const d = await api('channels');
  const KIND = ck => /group/i.test(ck) ? '群' : (/private/i.test(ck) ? '私' : '他');
  $('#pvCh').innerHTML = (d.channels||[]).map(c=>
    `<option value="${esc(c.chat_key)}">[${KIND(c.chat_key)}] ${esc(c.chat_key)}`
    + `（${c.count} 人 / 最高 ${c.top}）</option>`).join('');
  if (!$('#pvCh').value && d.channels[0]) $('#pvCh').value = d.channels[0].chat_key;
}
async function setScore(ck,uid,val){
  try{
    await api('score',{method:'POST',body:JSON.stringify({chat_key:ck,user_id:uid,score:Number(val)})});
    toast(`已设置 ${uid} = ${val}`); loadIndex();
  }catch(e){ toast('失败：'+e.message); }
}

// ---------- 注入预览 ----------
function pvFor(ck, score){
  document.querySelector('.tab[data-t="pv"]').click();
  $('#pvCh').value = ck; $('#pvScore').value = score; doPreview();
}
let _pvTimer = null;
function pvDebounced(){ clearTimeout(_pvTimer); _pvTimer = setTimeout(doPreview, 350); }
async function doPreview(){
  const ck = $('#pvCh').value, sc = Number($('#pvScore').value||0);
  if (!ck){ $('#pvOut').textContent = '（请先选择频道）'; return; }
  $('#pvOut').textContent = '渲染中…';
  try{
    const d = await api(`preview?chat_key=${encodeURIComponent(ck)}&score=${sc}`);
    $('#pvOut').textContent = `【${d.chat_key} · 好感度 ${d.score} · ${d.length} 字符】\n\n` + (d.text || '(空)');
  }catch(e){ $('#pvOut').textContent = '失败：'+e.message; }
}

$('#tierSel').onchange = loadMem;
loadOverview();
showTab((location.hash||'#ov').slice(1));
</script>
</body>
</html>
"""

# 新版界面单独维护，便于继续迭代；若部署时漏带 HTML 文件则自动回退旧版。
_WEBUI_HTML_PATH = Path(__file__).with_name("webui.html")
try:
    PAGE_HTML = _WEBUI_HTML_PATH.read_text(encoding="utf-8")
except OSError as exc:
    import warnings

    warnings.warn(
        f"无法读取新版 WebUI 页面 { _WEBUI_HTML_PATH }，回退到内置旧版：{exc}",
        RuntimeWarning,
        stacklevel=1,
    )
    PAGE_HTML = _LEGACY_PAGE_HTML
