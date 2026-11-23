"""历史刷卡数据清退台账 —— Flask 网页应用。"""

from __future__ import annotations

import csv
import io
import json
import os
import tempfile
import threading
from datetime import datetime
from functools import wraps
from pathlib import Path

from flask import (
    Flask,
    flash,
    redirect,
    render_template,
    request,
    send_file,
    url_for,
)

import importer
import planner

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("DATA_DIR", BASE_DIR / "data"))
STATE_PATH = DATA_DIR / "state.json"

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 4 * 1024 * 1024
app.secret_key = os.environ.get("SECRET_KEY", "bus-clearance-ledger")

# 单进程、多线程部署时用进程内锁串行化“读取-修改-写回”的状态操作，
# 避免并发确认或并发导入互相覆盖。
_state_lock = threading.Lock()


def state_transaction(func):
    @wraps(func)
    def wrapper(*args, **kwargs):
        with _state_lock:
            return func(*args, **kwargs)

    return wrapper


def empty_state() -> dict:
    return {
        "inventory": [],
        "reports": [],
        "must_offline": [],
        "plan": None,
        "history": [],
    }


def load_state() -> dict:
    if not STATE_PATH.exists():
        return empty_state()
    try:
        with STATE_PATH.open("r", encoding="utf-8") as handle:
            state = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return empty_state()
    base = empty_state()
    base.update(state if isinstance(state, dict) else {})
    return base


def save_state(state: dict) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=str(DATA_DIR), prefix=".state-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(state, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, STATE_PATH)
    finally:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)


def read_upload(field: str) -> str:
    uploaded = request.files.get(field)
    if uploaded is not None and uploaded.filename:
        raw = uploaded.read()
        for encoding in ("utf-8-sig", "gb18030", "utf-16"):
            try:
                return raw.decode(encoding)
            except UnicodeDecodeError:
                continue
        raise ValueError("文件编码无法识别，请使用 UTF-8、GB18030 或 UTF-16")
    return request.form.get("text", "")


def current_fingerprint(state: dict) -> str:
    return importer.fingerprint(
        state["inventory"], state["reports"], state["must_offline"]
    )


def plan_is_stale(state: dict) -> bool:
    plan = state.get("plan")
    if not plan:
        return False
    return plan.get("fingerprint") != current_fingerprint(state)


def inventory_index(state: dict) -> dict[str, dict]:
    return {f["file_id"]: f for f in state["inventory"]}


@app.context_processor
def inject_helpers():
    return {
        "type_labels": planner.TYPE_LABELS,
        "scope_display": planner.scope_display,
        "max_days": importer.MAX_REPORT_DAYS,
    }


@app.get("/")
def index():
    state = load_state()
    stale = plan_is_stale(state)
    unknown_offline = [
        fid for fid in state["must_offline"] if fid not in inventory_index(state)
    ]
    files_by_id = inventory_index(state)
    return render_template(
        "index.html",
        state=state,
        stale=stale,
        unknown_offline=unknown_offline,
        files_by_id=files_by_id,
        report_groups=_group_reports(state["reports"]),
        history=state.get("history", [])[-5:][::-1],
        sizes={
            "inventory": sum(int(f["size_bytes"]) for f in state["inventory"]),
            "must_offline": sum(
                int(files_by_id[fid]["size_bytes"])
                for fid in state["must_offline"]
                if fid in files_by_id
            ),
        },
    )


def _group_reports(reports: list[dict]) -> list[dict]:
    groups: dict[str, list[dict]] = {}
    for report in reports:
        groups.setdefault(report["report_no"], []).append(report)
    result = []
    for report_no in sorted(groups):
        days = sorted(groups[report_no], key=lambda r: r["date"])
        result.append({"report_no": report_no, "days": days})
    return result


@app.post("/import/inventory")
@state_transaction
def import_inventory():
    state = load_state()
    try:
        text = read_upload("inventory_file")
    except ValueError as exc:
        flash(str(exc), "error")
        return redirect(url_for("index"))
    try:
        inventory = importer.parse_inventory(text)
    except importer.ImportError_ as exc:
        flash(exc.message, "error")
        for line in exc.errors[:20]:
            flash(line, "error")
        if len(exc.errors) > 20:
            flash(f"……另有 {len(exc.errors) - 20} 处问题未显示", "error")
        return redirect(url_for("index"))

    old_ids = inventory_index(state)
    new_ids = {f["file_id"] for f in inventory}
    dropped = sorted(set(old_ids) - new_ids)
    state["inventory"] = inventory
    state["must_offline"] = [fid for fid in state["must_offline"] if fid in new_ids]
    message = (
        f"已导入在库文件清单：{len(inventory)} 个文件，"
        f"合计 {sum(f['size_bytes'] for f in inventory):,} 字节。"
    )
    if dropped:
        message += f" 较上次少了 {len(dropped)} 个文件，清退单需重新核算。"
    save_state(state)
    flash(message, "success")
    return redirect(url_for("index"))


@app.post("/import/reports")
@state_transaction
def import_reports():
    state = load_state()
    try:
        text = read_upload("reports_file")
    except ValueError as exc:
        flash(str(exc), "error")
        return redirect(url_for("index"))
    try:
        reports = importer.parse_reports(text)
    except importer.ImportError_ as exc:
        flash(exc.message, "error")
        for line in exc.errors[:20]:
            flash(line, "error")
        return redirect(url_for("index"))

    existing_keys = {(r["report_no"], r["date"]) for r in state["reports"]}
    added, skipped = [], 0
    for report in reports:
        key = (report["report_no"], report["date"])
        if key in existing_keys:
            skipped += 1
            continue
        existing_keys.add(key)
        added.append(report)
    if len(state["reports"]) + len(added) > importer.MAX_REPORT_DAYS:
        flash(
            f"周报日记录最多 {importer.MAX_REPORT_DAYS} 条，"
            f"现有 {len(state['reports'])} 条，本次新增 {len(added)} 条，超出上限，未导入。",
            "error",
        )
        return redirect(url_for("index"))
    state["reports"].extend(added)
    message = f"已导入 {len(added)} 条周报日记录，当前共 {len(state['reports'])} 条。"
    if skipped:
        message += f" 跳过重复 {skipped} 条。"
    save_state(state)
    flash(message, "success")
    return redirect(url_for("index"))


@app.post("/reports/clear")
@state_transaction
def clear_reports():
    state = load_state()
    state["reports"] = []
    state["plan"] = None
    save_state(state)
    flash("已清空周报日记录，清退单已作废。", "success")
    return redirect(url_for("index"))


@app.post("/offline")
@state_transaction
def update_offline():
    state = load_state()
    selected = set(request.form.getlist("must_offline"))
    known = inventory_index(state)
    state["must_offline"] = sorted(fid for fid in selected if fid in known)
    if len(state["must_offline"]) != len(selected):
        flash("部分勾选的文件不在当前在库清单中，已忽略。", "info")
    save_state(state)
    flash("必须下线的文件已更新，如已生成清退单请重新核算。", "success")
    return redirect(url_for("index"))


@app.post("/plan")
@state_transaction
def create_plan():
    state = load_state()
    if not state["inventory"]:
        flash("请先导入在库文件清单。", "error")
        return redirect(url_for("index"))
    if not state["reports"]:
        flash("请先导入周报日记录。", "error")
        return redirect(url_for("index"))

    fingerprint = current_fingerprint(state)
    result = planner.build_plan(
        state["inventory"], state["reports"], state["must_offline"], fingerprint
    )
    result["created_at"] = datetime.now().isoformat(timespec="seconds")
    state["plan"] = result
    save_state(state)
    if result["status"] == "ok":
        flash(
            f"留存方案已生成：留存 {result['summary']['retained_count']} 个文件"
            f"（{result['summary']['retained_bytes']:,} 字节），"
            f"清退 {result['summary']['cleared_count']} 个文件"
            f"（{result['summary']['cleared_bytes']:,} 字节）。",
            "success",
        )
    else:
        flash(
            f"有 {len(result['missing'])} 个周报日缺少复算依据，已暂停清退。",
            "error",
        )
    return redirect(url_for("index"))


@app.post("/confirm")
@state_transaction
def confirm_plan():
    state = load_state()
    plan = state.get("plan")
    if not plan:
        flash("当前没有可确认的清退单，请先核算。", "error")
        return redirect(url_for("index"))
    if plan_is_stale(state):
        flash("周报或文件清单已变化，旧清退单须重算后才能确认。", "error")
        return redirect(url_for("index"))
    if plan.get("status") != "ok":
        flash("存在缺少复算依据的周报日，清退已暂停，不能确认。", "error")
        return redirect(url_for("index"))

    cleared_ids = {f["file_id"] for f in plan["cleared"]}
    retained = [f for f in state["inventory"] if f["file_id"] not in cleared_ids]
    removed_bytes = sum(
        int(f["size_bytes"]) for f in state["inventory"] if f["file_id"] in cleared_ids
    )
    stamp = datetime.now().isoformat(timespec="seconds")
    history = list(state.get("history", []))
    history.append(
        {
            "confirmed_at": stamp,
            "fingerprint": plan["fingerprint"],
            "cleared_count": len(cleared_ids),
            "cleared_bytes": removed_bytes,
            "retained_count": len(retained),
            "retained_bytes": sum(int(f["size_bytes"]) for f in retained),
            "cleared_files": sorted(cleared_ids),
            "shared_file_count": plan["summary"].get("shared_file_count", 0),
            "day_count": plan["summary"].get("day_count", 0),
        }
    )
    state["inventory"] = retained
    state["must_offline"] = []
    state["plan"] = None
    state["history"] = history[-50:]
    save_state(state)
    flash(
        f"已确认：在库登记移出 {len(cleared_ids)} 个文件（{removed_bytes:,} 字节），"
        f"现登记 {len(retained)} 个文件。下次核算按剩余文件进行。",
        "success",
    )
    return redirect(url_for("index"))


@app.post("/reset")
@state_transaction
def reset():
    save_state(empty_state())
    flash("已清空全部导入与清退记录。", "success")
    return redirect(url_for("index"))


@app.post("/demo")
@state_transaction
def load_demo():
    sample_dir = BASE_DIR / "static" / "samples"
    inventory = importer.parse_inventory(
        (sample_dir / "inventory.csv").read_text(encoding="utf-8")
    )
    reports = importer.parse_reports(
        (sample_dir / "reports.csv").read_text(encoding="utf-8")
    )
    state = empty_state()
    state["inventory"] = inventory
    state["reports"] = reports
    state["must_offline"] = sorted(
        f["file_id"]
        for f in inventory
        if f["file_id"] in {"LOG-2025-Q4.txt", "AGG-20250101-9-v2"}
    )
    save_state(state)
    flash("已载入示例数据。", "success")
    return redirect(url_for("index"))


@app.get("/export/<kind>.csv")
def export_csv(kind: str):
    state = load_state()
    plan = state.get("plan")
    if not plan:
        flash("请先核算清退方案。", "error")
        return redirect(url_for("index"))
    if kind == "cleared":
        rows = [["文件名", "类型", "字节数", "清退原因"]]
        for f in plan["cleared"]:
            rows.append(
                [
                    f["file_id"],
                    planner.TYPE_LABELS.get(f["type"], f["type"]),
                    f["size_bytes"],
                    f.get("clear_reason", ""),
                ]
            )
        filename = "清退清单.csv"
    elif kind == "basis":
        rows = [["周报编号", "日期", "车队范围", "字典版本", "复算依据", "文件", "字节数"]]
        for day in plan["days"]:
            for f in day["files"]:
                rows.append(
                    [
                        day["report_no"],
                        day["date"],
                        day["fleet_scope_display"],
                        day["dict_version"],
                        day["basis_label"],
                        f["file_id"],
                        f["size_bytes"],
                    ]
                )
        filename = "逐日复算依据.csv"
    elif kind == "missing":
        rows = [["周报编号", "日期", "车队范围", "字典版本", "缺失说明"]]
        for day in plan["missing"]:
            rows.append(
                [
                    day["report_no"],
                    day["date"],
                    day["fleet_scope_display"],
                    day["dict_version"],
                    "；".join(day["reasons"]),
                ]
            )
        filename = "缺失复算依据.csv"
    else:
        flash("未知的导出类型。", "error")
        return redirect(url_for("index"))

    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerows(rows)
    payload = "﻿" + buffer.getvalue()
    return send_file(
        io.BytesIO(payload.encode("utf-8")),
        mimetype="text/csv",
        as_attachment=True,
        download_name=filename,
    )


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8000")))
