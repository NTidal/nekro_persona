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
import shutil
import tempfile
import threading
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, Response

BG_DIR = Path(__file__).parent / "backgrounds"   # 兜底；实际在 build_router 里按插件数据目录覆盖
# 合法档位（扩展：新增 tier3 世界知识）
_TIERS = ("tier1", "tier2", "tier3")
_TIER_LABEL = {"tier1": "1 档·身份", "tier2": "2 档·往事", "tier3": "3 档·世界知识"}


def build_router(plugin: Any, config: Any, memory: Any, helpers: Dict[str, Any]) -> APIRouter:
    """helpers: 由 plugin.py 传入的内部函数（load_state/save_state/stage_info/
    clamp_score/max_abs_score/now_ts）。不在此处 import 插件模块——
    包的 __init__ 用 `from .plugin import plugin` 遮蔽了同名子模块，
    且运行进程里模块绝对名不一定是 nekro_persona。"""
    router = APIRouter()

    # 背景记忆数据目录与注入链路共用同一份（由 memory 统一解析：
    # 插件数据目录，首次运行自包内种子复制），避免 WebUI 与注入读写到不同文件。
    global BG_DIR
    try:
        BG_DIR = Path(memory.bg_dir())
    except Exception:  # noqa: BLE001
        pass

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
            if request.method.upper() != "GET":
                raise HTTPException(
                    status_code=403,
                    detail="未配置 WEBUI_ACCESS_KEY，写操作已禁用；请在插件配置中设置访问密钥后再试",
                )
            return
        if not hmac.compare_digest(got, expect):
            raise HTTPException(status_code=401, detail="访问密钥不正确")

    # ---------------- 数据读写 ----------------
    # JSON 写入只允许在完整解析、持有进程锁并完成备份后进行。
    # 解析失败时 fail closed，绝不把坏文件当成空配置继续覆盖。
    _WRITE_LOCK = threading.RLock()

    def _entry_files(tier: str) -> List[Path]:
        return sorted(Path(p) for p in glob.glob(str(BG_DIR / tier / "*.json")))

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
    _WRITABLE = ("min_favor", "enabled", "weight", "title", "trigger", "triggers", "content")

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

    def _tier2_cfg() -> Dict[str, Any]:
        """2 档运行参数（来自插件配置）。"""
        return {
            "max_entries": int(getattr(config, "TIER2_MAX_ENTRIES", 2) or 2),
            "max_chars": int(getattr(config, "TIER2_MAX_CHARS", 500) or 500),
            "threshold": float(getattr(config, "TIER2_SIM_THRESHOLD", 0.35) or 0.35),
            "scan_msgs": int(getattr(config, "TIER2_SCAN_MSGS", 6) or 6),
        }

    def _t3_cfg() -> Dict[str, Any]:
        """3 档运行参数（来自插件配置，扩展）。"""
        return {
            "enabled": bool(getattr(config, "TIER3_ENABLED", True)),
            "max_entries": int(getattr(config, "TIER3_MAX_ENTRIES", 3) or 3),
            "max_chars": int(getattr(config, "TIER3_MAX_CHARS", 700) or 700),
            "threshold": float(getattr(config, "TIER3_SIM_THRESHOLD", 0.35) or 0.35),
            "scan_msgs": int(getattr(config, "TIER3_SCAN_MSGS", 6) or 6),
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
            fp = BG_DIR / tier / "custom.json"
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
        data = _read_json_strict(BG_DIR / "favor.json")
        return data or {}

    def _write_gating(patch: Dict[str, Any]) -> None:
        data = _read_gating()
        for key in (
            "enabled", "stages", "avoid", "rubric",
            "stage_guides", "stage_guide_common",   # 扩展：分档互动指引
        ):
            if key in patch:
                data[key] = patch[key]
        _dump(BG_DIR / "favor.json", data)

    _TRASH_FILE = BG_DIR / ".webui_trash.json"

    def _read_trash() -> List[Dict[str, Any]]:
        data = _read_json_strict(_TRASH_FILE)
        if data is None:
            return []
        items = data.get("items", [])
        if not isinstance(items, list):
            raise ValueError("WebUI 回收站格式损坏：items 必须是数组")
        return [dict(item) for item in items if isinstance(item, dict)]

    def _write_trash(items: List[Dict[str, Any]]) -> None:
        _dump(_TRASH_FILE, {"version": 1, "items": items})

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
            fp = BG_DIR / tier / file_name
            data = _read_json_strict(fp)
            if data is None:
                source_document = record.get("source_document")
                if isinstance(source_document, dict):
                    data = json.loads(json.dumps(source_document, ensure_ascii=False))
                else:
                    data = {"character": "restored", "tier": tier, "entries": []}
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

    def _data_error(exc: Exception) -> HTTPException:
        message = str(exc)
        status = 409 if "ID" in message or "档位" in message else 500
        return HTTPException(status_code=status, detail=message)

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
    async def index() -> str:
        return PAGE_HTML

    # ---------------- API ----------------
    @router.get("/api/overview", summary="概览")
    async def api_overview(request: Request) -> Dict[str, Any]:
        _check_key(request)
        try:
            await memory.ensure_loaded()
            cfg = memory.load_favor_cfg()
        except ValueError as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        t1, t2 = memory._state["tier1"], memory._state["tier2"]
        t3 = memory._state.get("tier3", [])
        vecs3 = memory._state.get("tier3_vecs", [])
        t1s = memory.tier1_settings(_tier1_cfg())
        t2s = memory.tier2_settings(_tier2_cfg())
        t3s = memory.tier3_settings(_t3_cfg())
        t1_chars = sum(
            len(str(e.get("content", ""))) + len(str(e.get("trigger", ""))) for e in t1
        )
        t1_rendered_chars = sum(_entry_render_chars(e) for e in t1)
        t1_limit = int(t1s["max_chars"])
        vecs = memory._state["tier2_vecs"]

        def _dist(entries: List[Dict[str, Any]]) -> Dict[str, int]:
            d: Dict[str, int] = {}
            for e in entries:
                k = str(int(e.get("min_favor", 0) or 0))
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
            "last_hit": memory._state.get("last_hit", ""),
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

        def _deco(items: List[Dict[str, Any]], tier: str) -> List[Dict[str, Any]]:
            out = []
            for e in items:
                mf = int(e.get("min_favor", 0) or 0)
                out.append(
                    {
                        "tier": tier,
                        "id": e.get("id", ""),
                        "title": e.get("title") or e.get("topic") or "",
                        "weight": int(e.get("weight", 1) or 1),
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
            "stages": [
                {"name": str(st.get("name") or ""), "max": int(st.get("max", 0) or 0)}
                for st in stages if st.get("name") != "排斥"
            ],
        }

    @router.post("/api/entry", summary="修改条目（门槛/启停/权重）")
    async def api_entry_update(request: Request) -> Dict[str, Any]:
        _check_key(request)
        body = await request.json()
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
        body = await request.json()
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
        body = await request.json()
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
        body = await request.json()
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
        state = await helpers["load_state"](ck)
        prof = state.profile_for(uid, "")
        final_score = helpers["clamp_score"](score)
        prof.score = final_score
        prof.updated_at = helpers["now_ts"]()
        prof.last_reason = str(body.get("reason") or "WebUI 手动调整")[:160]
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
        body = await request.json()
        ck = str(body.get("chat_key") or "")
        uid = str(body.get("user_id") or "")
        if not ck or not uid:
            raise HTTPException(status_code=400, detail="缺少 chat_key/user_id")
        action = str(body.get("action") or "update")
        if action not in ("update", "delete"):
            raise HTTPException(status_code=400, detail="action 只能是 update 或 delete")

        state = await helpers["load_state"](ck)
        prof = state.profile_for(uid, "")

        if action == "delete":
            profiles = getattr(state, "profiles", None)
            if isinstance(profiles, dict) and uid in profiles:
                profiles.pop(uid, None)
                await helpers["save_state"](ck, state)
                plugin.logger.info(f"[persona] WebUI 删除关系档案 {ck} / {uid}")
                return {"ok": True, "action": "delete", "user_id": uid}
            raise HTTPException(status_code=404, detail="该频道没有这个用户的档案")

        changed: List[str] = []
        if "summary" in body:
            prof.summary = " ".join(str(body["summary"] or "").split())[:200]
            changed.append("summary")
        if "tags" in body:
            raw = body["tags"]
            if isinstance(raw, str):
                raw = raw.replace("，", ",").replace("、", ",").split(",")
            if not isinstance(raw, list):
                raise HTTPException(status_code=400, detail="tags 必须是数组或逗号分隔的字符串")
            tags: List[str] = []
            for item in raw:
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

        prof.updated_at = helpers["now_ts"]()
        if str(body.get("reason") or "").strip():
            prof.last_reason = str(body["reason"]).strip()[:160]
        elif "score" in changed:
            prof.last_reason = "WebUI 手动调整"
        await helpers["save_state"](ck, state)
        stage, _hint = helpers["stage_info"](prof.score)
        plugin.logger.info(f"[persona] WebUI 修改关系档案 {ck} / {uid}: {changed}")
        return {
            "ok": True,
            "action": "update",
            "user_id": uid,
            "changed": changed,
            "score": prof.score,
            "stage": stage,
            "summary": prof.summary,
            "tags": list(prof.tags),
            "interaction_hint": prof.interaction_hint,
            "max_abs": helpers["max_abs_score"](),
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
        ctx = await AgentCtx.create_by_chat_key(chat_key=chat_key)
        cfg = memory.load_favor_cfg()
        gating = bool(cfg) and bool(cfg.get("enabled", True))
        text = await memory.render_memory_block(
            ctx, effective_score, gating=gating,
            tier1_cfg=_tier1_cfg(), tier2_cfg=_tier2_cfg(), tier3_cfg=_t3_cfg(),
        )
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
                "gating_enabled": gating,
                "tier1_count": len(memory._state.get("tier1", [])),
                "tier2_count": len(memory._state.get("tier2", [])),
                "tier2_hit": str(memory._state.get("last_hit") or ""),
                "tier3_count": len(memory._state.get("tier3", [])),
                "tier3_hit": str(memory._state.get("last_hit_tier3") or ""),
                "tier3_enabled": memory.tier3_settings(_t3_cfg())["enabled"],
                "message_scan_limit": int(memory.tier2_settings(_tier2_cfg())["scan_msgs"]),
            },
        }

    @router.post("/api/config", summary="修改插件配置（好感度衰减等）")
    async def api_config(request: Request) -> Dict[str, Any]:
        _check_key(request)
        body = await request.json()
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
            "TIER2_SCAN_MSGS": (int, 1, 30),
            # 扩展：3 档（世界知识）
            "TIER3_ENABLED": (bool, None, None),
            "TIER3_MAX_ENTRIES": (int, 1, 10),
            "TIER3_MAX_CHARS": (int, 100, 3000),
            "TIER3_SIM_THRESHOLD": (float, 0.0, 1.0),
            "TIER3_SCAN_MSGS": (int, 1, 30),
            # 扩展：备忘录
            "MEMO_ENABLED": (bool, None, None),
            "MEMO_MAX_ITEMS": (int, 1, 500),
            "MEMO_MAX_CONTENT_CHARS": (int, 50, 2000),
            "MEMO_INJECT_RECENT": (int, 0, 10),
            "MEMO_INJECT_MATCHED": (int, 0, 10),
            "MEMO_INJECT_CHARS": (int, 100, 4000),
            "MEMO_DEDUP_MIN_KEY_LEN": (int, 2, 10),
        }
        for k, (conv, lo, hi) in limits.items():
            if k not in body:
                continue
            v = conv(body[k])
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
        plugin.logger.info(f"[persona] WebUI 修改配置: {patch}")
        return {"ok": True, "patch": patch}

    @router.post("/api/gating", summary="修改好感度门控规则（档位/回避块/rubric）")
    async def api_gating(request: Request) -> Dict[str, Any]:
        _check_key(request)
        body = await request.json()
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
            items = []
            for item in rubric.get("items") or []:
                if isinstance(item, (list, tuple)) and len(item) >= 2:
                    items.append([str(item[0])[:16], str(item[1])[:200]])
            patch["rubric"] = {
                "header": str(rubric.get("header") or "")[:60],
                "intro": str(rubric.get("intro") or "")[:300],
                "items": items,
                "rules": [str(x)[:200] for x in (rubric.get("rules") or [])][:20],
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
        body = await request.json()
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
        if tier == "tier1":
            entry["trigger"] = str(body.get("trigger") or "")[:200]
        else:
            trg = body.get("triggers") or []
            entry["triggers"] = [str(x)[:24] for x in trg][:30]
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

    # ---------------- 备忘录（扩展：吸收 note 插件）----------------
    @router.get("/api/memos", summary="频道备忘录")
    async def api_memos(request: Request, chat_key: str = "") -> Dict[str, Any]:
        _check_key(request)
        if not chat_key:
            raise HTTPException(status_code=400, detail="缺少 chat_key")
        state = await helpers["load_state"](chat_key)
        now = int(helpers["now_ts"]())
        items: List[Dict[str, Any]] = []
        for m in getattr(state, "memos", []) or []:
            expire_at = int(getattr(m, "expire_at", 0) or 0)
            items.append(
                {
                    "id": str(getattr(m, "id", "") or ""),
                    "title": str(getattr(m, "title", "") or ""),
                    "content": str(getattr(m, "content", "") or ""),
                    "tags": list(getattr(m, "tags", None) or []),
                    "min_favor": int(getattr(m, "min_favor", 0) or 0),
                    "expire_at": expire_at,
                    "expired": bool(expire_at and expire_at <= now),
                    "created_at": int(getattr(m, "created_at", 0) or 0),
                    "updated_at": int(getattr(m, "updated_at", 0) or 0),
                    "source_user_id": str(getattr(m, "source_user_id", "") or ""),
                },
            )
        items.sort(key=lambda x: -x["updated_at"])
        mcfg = helpers["memo_cfg"]()
        return {
            "chat_key": chat_key,
            "items": items,
            "meta": {
                **mcfg,
                "total": len(items),
                "live": sum(1 for x in items if not x["expired"]),
            },
        }

    @router.post("/api/memo", summary="新建 / 修改 / 删除备忘录")
    async def api_memo_update(request: Request) -> Dict[str, Any]:
        _check_key(request)
        body = await request.json()
        chat_key = str(body.get("chat_key") or "")
        if not chat_key:
            raise HTTPException(status_code=400, detail="缺少 chat_key")
        state = await helpers["load_state"](chat_key)
        now = int(helpers["now_ts"]())
        mcfg = helpers["memo_cfg"]()
        op = str(body.get("op") or "save")

        if op == "delete":
            mid = str(body.get("id") or "")
            before = len(state.memos)
            state.memos = [m for m in state.memos if str(getattr(m, "id", "")) != mid]
            if len(state.memos) == before:
                raise HTTPException(status_code=404, detail=f"找不到备忘 {mid}")
            await helpers["save_state"](chat_key, state)
            return {"ok": True, "deleted": mid, "total": len(state.memos)}

        if op == "clear_expired":
            before = len(state.memos)
            state.memos = [m for m in state.memos if not m.is_expired(now)]
            removed = before - len(state.memos)
            await helpers["save_state"](chat_key, state)
            return {"ok": True, "removed": removed, "total": len(state.memos)}

        title = str(body.get("title") or "").strip()[:40]
        content = str(body.get("content") or "").strip()
        if not title:
            raise HTTPException(status_code=400, detail="标题不能为空")
        if not content:
            raise HTTPException(status_code=400, detail="备忘内容不能为空")
        if len(content) > int(mcfg["max_content"]):
            raise HTTPException(
                status_code=400, detail=f"内容超过上限 {mcfg['max_content']} 字",
            )
        tags = [str(x).strip()[:12] for x in (body.get("tags") or []) if str(x).strip()][:6]
        min_favor = max(0, min(100, int(body.get("min_favor") or 0)))
        ttl_hours = max(0, int(body.get("ttl_hours") or 0))
        expire_at = now + ttl_hours * 3600 if ttl_hours else 0

        mid = str(body.get("id") or "")
        target = next(
            (m for m in state.memos if mid and str(getattr(m, "id", "")) == mid), None,
        )
        if target is None:   # 标题去重（与 save_memo 保持一致）
            key = helpers["memo_key"](title)
            target = next(
                (m for m in state.memos if helpers["memo_key"](m.title) == key), None,
            )
        if target is not None:
            target.title = title
            target.content = content
            target.tags = tags
            target.min_favor = min_favor
            target.expire_at = expire_at
            target.updated_at = now
            action = "updated"
            result_id = str(getattr(target, "id", "") or "")
        else:
            if len(state.memos) >= int(mcfg["max_items"]):
                raise HTTPException(
                    status_code=400, detail=f"已达上限 {mcfg['max_items']} 条，请先清理",
                )
            import uuid as _uuid

            memo = helpers["memo_cls"](
                id=f"memo_{_uuid.uuid4().hex[:10]}",
                title=title,
                content=content,
                tags=tags,
                min_favor=min_favor,
                expire_at=expire_at,
                created_at=now,
                updated_at=now,
            )
            state.memos.append(memo)
            action = "created"
            result_id = memo.id
        await helpers["save_state"](chat_key, state)
        return {
            "ok": True,
            "action": action,
            "id": result_id,
            "total": len(state.memos),
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
        files = [
            ("backgrounds/tier1/core.json", BG_DIR / "tier1" / "core.json"),
            ("backgrounds/tier2/lore.json", BG_DIR / "tier2" / "lore.json"),
            ("backgrounds/tier3/world.json", BG_DIR / "tier3" / "world.json"),
            ("backgrounds/favor.json", BG_DIR / "favor.json"),
        ]
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
        if not body:
            raise HTTPException(status_code=400, detail="没有收到文件内容")
        import io as _io
        import zipfile as _zipfile
        from datetime import datetime as _datetime

        try:
            zf = _zipfile.ZipFile(_io.BytesIO(body))
        except Exception as exc:
            raise HTTPException(status_code=400, detail=f"不是有效的 zip 包：{exc}") from exc
        names = set(zf.namelist())

        def _pick(arc: str) -> str | None:
            for cand in (f"backgrounds/{arc}", arc):
                if cand in names:
                    return cand
            return None

        targets = {
            "tier1/core.json": BG_DIR / "tier1" / "core.json",
            "tier2/lore.json": BG_DIR / "tier2" / "lore.json",
            "tier3/world.json": BG_DIR / "tier3" / "world.json",
            "favor.json": BG_DIR / "favor.json",
        }
        parsed = {}
        for arc, dst in targets.items():
            src = _pick(arc)
            if src is None:
                continue
            raw = zf.read(src)
            try:
                data = json.loads(raw.decode("utf-8"))
            except Exception as exc:
                raise HTTPException(status_code=400, detail=f"{arc} 不是合法 JSON：{exc}")
            if arc == "favor.json":
                if not isinstance(data, dict) or "stages" not in data:
                    raise HTTPException(status_code=400, detail=f"{arc} 缺少 stages，拒绝导入")
                n = -1
            else:
                if not isinstance(data, dict) or not isinstance(data.get("entries"), list):
                    raise HTTPException(status_code=400, detail=f"{arc} 结构不对（缺 entries）")
                n = len(data["entries"])
            parsed[arc] = (raw, dst, n)
        # 只有 backgrounds 而没有 preset 也能导入；反过来（只导入预设文本）同样应该允许：
        # 导出包允许「只要人设文本」这一用法，此处不该拦。
        has_presets = any(x.startswith('presets/') and x.endswith('.md') for x in names)
        if not parsed and not has_presets:
            raise HTTPException(
                status_code=400,
                detail="包里没有可导入的人设文件（需要 backgrounds/tier1、tier2、tier3、"
                       "favor.json，或 presets/*.md）",
            )

        # 导入前先备份当前文件
        stamp = _datetime.now().strftime("%Y%m%d_%H%M%S")
        bdir = BG_DIR.parent / "persona_import_backups" / stamp
        bdir.mkdir(parents=True, exist_ok=True)
        for arc, dst in targets.items():
            if dst.is_file():
                shutil.copy2(dst, bdir / arc.replace("/", "_"))

        applied = []
        for arc, (raw, dst, n) in parsed.items():
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_bytes(raw)
            applied.append(f"{arc}（{n} 条）" if n >= 0 else f"{arc}")
        preset_applied = []
        preset_failed = []
        try:
            from nekro_agent.models.db_preset import DBPreset

            # 从 bundle 元信息里取预设名（导出时写过），找不到就用回退名。
            # 目的：新建的 preset 记录 name/title 不该是「导入人设 N」这种占位。
            bundle_presets: Dict[int, str] = {}
            if "persona.bundle.json" in names:
                try:
                    _meta = json.loads(zf.read("persona.bundle.json").decode("utf-8"))
                    for item in (_meta.get("presets") or []):
                        try:
                            bundle_presets[int(item.get("id"))] = str(item.get("name") or "")
                        except (TypeError, ValueError):
                            continue
                except Exception:  # noqa: BLE001
                    bundle_presets = {}

            for n_ in sorted(x for x in names if x.startswith('presets/') and x.endswith('.md')):
                try:
                    pid = int(n_.split('/')[-1].split('.')[0])
                except (ValueError, IndexError):
                    continue
                try:
                    ptext = zf.read(n_).decode('utf-8')
                    if not ptext.strip():
                        continue
                    content_db = ptext.replace('\\n', '\\\\n')
                    row = await DBPreset.get_or_none(id=pid)
                    if row:
                        row.content = content_db
                        await row.save(update_fields=['content'])
                    else:
                        # ⚠️ DBPreset 除 content 外还有多个 NOT NULL 列
                        #    （name/title/avatar/description/tags/author），
                        #    只传 id+name+content 会抛「title: Value must not be None」，
                        #    导致整个导入 500 —— 而 backgrounds 其实已经写入了。
                        #    这里补齐全部必填字段，缺省用空串/回退名。
                        pname = (bundle_presets.get(pid) or "").strip() or f'导入人设 {pid}'
                        await DBPreset.create(
                            id=pid,
                            name=pname,
                            title=pname,
                            avatar="",
                            content=content_db,
                            description="",
                            tags="",
                            author="persona-import",
                            on_shared=False,
                        )
                    preset_applied.append(pid)
                except Exception as exc:  # noqa: BLE001
                    # 单条预设失败不牵连其它预设，也不让整体 500
                    plugin.logger.warning(f"[persona] 预设 {n_} 导入失败：{exc}")
                    preset_failed.append(f"{n_}: {exc}")
        except Exception as exc:  # noqa: BLE001
            # 预设导入失败**不再**让整个导入返回 500：backgrounds 已经落盘，
            # 报整体失败会让用户误以为什么都没导入、进而重复导入。
            # 改为逐条记录失败原因，随响应返回，由前端提示。
            plugin.logger.warning(f"[persona] 预设文本导入部分失败：{exc}")
            preset_failed.append(str(exc))

        reload_state = await _reload_after_write()
        plugin.logger.info(
            f"[persona] WebUI 导入整套人设: {list(parsed)} "
            f"preset={preset_applied} preset_failed={len(preset_failed)}",
        )
        return {
            "ok": True,
            "applied": applied,
            "preset_applied": preset_applied,
            "preset_failed": preset_failed,
            "backup_dir": str(bdir),
            **reload_state,
        }

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
