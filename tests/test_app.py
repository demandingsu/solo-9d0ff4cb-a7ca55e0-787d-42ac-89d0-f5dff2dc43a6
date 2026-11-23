"""自测：共用分区、缺件、旧清退单被拒，以及求解最优性。"""

from __future__ import annotations

import itertools
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import app as webapp  # noqa: E402
import importer  # noqa: E402
import planner  # noqa: E402


def file(fid, kind, size, date="", fleet="", scope="", version=""):
    return {
        "file_id": fid,
        "type": kind,
        "size_bytes": size,
        "date": date,
        "fleet": fleet,
        "fleet_scope": scope,
        "dict_version": version,
    }


def report(no, date, scope, version):
    return {
        "report_no": no,
        "date": date,
        "fleet_scope": scope,
        "dict_version": version,
    }


def run(inventory, reports, offline=()):
    return planner.build_plan(
        inventory, reports, list(offline), importer.fingerprint(inventory, reports, list(offline))
    )


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(webapp, "DATA_DIR", tmp_path)
    monkeypatch.setattr(webapp, "STATE_PATH", tmp_path / "state.json")
    webapp.app.config.update(TESTING=True)
    with webapp.app.test_client() as test_client:
        yield test_client


def sample_inventory():
    return importer.parse_inventory((ROOT / "static/samples/inventory.csv").read_text(encoding="utf-8"))


def sample_reports():
    return importer.parse_reports((ROOT / "static/samples/reports.csv").read_text(encoding="utf-8"))


# ---------- 求解正确性 ----------

def test_shared_tap_partition_counted_once():
    inventory = [
        file("T1", "tap", 100, date="2026-05-01", fleet="1"),
        file("T2", "tap", 100, date="2026-05-01", fleet="2"),
        file("D1", "dict", 50, version="v1"),
        file("A1", "daily_agg", 900, date="2026-05-01", scope="1,2", version="v1"),
    ]
    reports = [
        report("W1", "2026-05-01", "1,2", "v1"),
        report("W2", "2026-05-01", "1,2", "v1"),
    ]
    result = run(inventory, reports)
    assert result["status"] == "ok"
    # 两个周报日共用 T1/T2/D1，共 250 字节；日汇总 900 更贵。
    assert result["summary"]["retained_bytes"] == 250
    assert {f["file_id"] for f in result["retained"]} == {"T1", "T2", "D1"}
    shared_ids = {s["file_id"] for s in result["shared"]}
    assert shared_ids == {"T1", "T2", "D1"}
    assert result["summary"]["shared_file_count"] == 3
    assert {f["file_id"] for f in result["cleared"]} == {"A1"}


def test_basis_choice_uses_daily_agg_when_cheaper_and_must_offline_excluded():
    inventory = [
        file("T1", "tap", 100, date="2026-05-01", fleet="1"),
        file("D1", "dict", 50, version="v1"),
        file("A1", "daily_agg", 60, date="2026-05-01", scope="1", version="v1"),
    ]
    result = run(inventory, [report("W1", "2026-05-01", "1", "v1")])
    assert result["status"] == "ok"
    assert [f["file_id"] for f in result["days"][0]["files"]] == ["A1"]
    assert result["summary"]["retained_bytes"] == 60

    # 勾选日汇总下线后，必须回退到刷卡分区 + 字典，而不是报缺件。
    result = run(inventory, [report("W1", "2026-05-01", "1", "v1")], offline=["A1"])
    assert result["status"] == "ok"
    assert {f["file_id"] for f in result["days"][0]["files"]} == {"T1", "D1"}


def test_missing_tap_partition_blocks_with_report_and_date():
    inventory = [
        file("T1", "tap", 100, date="2026-05-01", fleet="1"),
        file("D1", "dict", 50, version="v1"),
    ]
    result = run(inventory, [report("W9", "2026-05-01", "1,2", "v1")])
    assert result["status"] == "blocked"
    assert result["days"] == []
    assert result["cleared"] == []
    assert len(result["missing"]) == 1
    miss = result["missing"][0]
    assert miss["report_no"] == "W9"
    assert miss["date"] == "2026-05-01"
    assert any("车队 2" in reason for reason in miss["reasons"])


def test_missing_dict_blocks():
    inventory = [
        file("T1", "tap", 100, date="2026-05-01", fleet="1"),
        file("D1", "dict", 50, version="v9"),
    ]
    result = run(inventory, [report("W1", "2026-05-01", "1", "v1")])
    assert result["status"] == "blocked"
    assert any("字典版本 v1" in reason for reason in result["missing"][0]["reasons"])


def test_daily_agg_scope_must_match_exactly():
    inventory = [
        file("A1", "daily_agg", 10, date="2026-05-01", scope="1", version="v1"),
        file("T1", "tap", 1000, date="2026-05-01", fleet="1"),
        file("T2", "tap", 1000, date="2026-05-01", fleet="2"),
        file("D1", "dict", 50, version="v1"),
    ]
    # 周报要 1、2 两车队，单车队日汇总不能顶替。
    result = run(inventory, [report("W1", "2026-05-01", "1,2", "v1")])
    assert result["status"] == "ok"
    assert [f["file_id"] for f in result["days"][0]["files"]] == ["T1", "T2", "D1"]


def test_scope_normalization_equivalences():
    assert planner.normalize_scope("1, 2") == "1,2"
    assert planner.normalize_scope("2;1") == "1,2"
    assert planner.normalize_scope("全部车队") == "*"
    assert planner.scopes_match("*", "*")
    assert not planner.scopes_match("*", "1,2")
    assert planner.scopes_match("2,1", "1, 2")


def test_all_fleets_scope_needs_star_agg_and_taps_every_fleet():
    inventory = [
        file("T1", "tap", 10, date="2026-05-01", fleet="1"),
        file("T2", "tap", 10, date="2026-05-01", fleet="2"),
        file("T3", "tap", 10, date="2026-05-01", fleet="3"),
        file("D1", "dict", 5, version="v1"),
        file("A1", "daily_agg", 12, date="2026-05-01", scope="1,2,3", version="v1"),
        file("A2", "daily_agg", 14, date="2026-05-01", scope="*", version="v1"),
    ]
    result = run(inventory, [report("W1", "2026-05-01", "*", "v1")])
    assert result["status"] == "ok"
    # “全部车队”的日汇总只认 *；显式列出 1,2,3 的不算吻合。
    assert [f["file_id"] for f in result["days"][0]["files"]] == ["A2"]

    result = run(inventory, [report("W1", "2026-05-01", "*", "v1")], offline=["A2"])
    assert result["status"] == "ok"
    assert {f["file_id"] for f in result["days"][0]["files"]} == {"T1", "T2", "T3", "D1"}
    inventory = [
        file("A1", "daily_agg", 50, date="2026-05-01", scope="1", version="v1"),
        file("A2", "daily_agg", 40, date="2026-05-01", scope="1", version="v1"),
        file("T1", "tap", 30, date="2026-05-01", fleet="1"),
        file("T2", "tap", 30, date="2026-05-02", fleet="1"),
        file("D1", "dict", 20, version="v1"),
        file("B1", "daily_agg", 25, date="2026-05-02", scope="1", version="v1"),
    ]
    reports = [
        report("W1", "2026-05-01", "1", "v1"),
        report("W2", "2026-05-02", "1", "v1"),
    ]
    result = run(inventory, reports)
    assert result["status"] == "ok"

    # 暴力穷举两天的所有依据组合，取并集最小。
    options_per_day = [
        [{"A1"}, {"A2"}, {"T1", "D1"}],
        [{"B1"}, {"T2", "D1"}],
    ]
    sizes = {f["file_id"]: f["size_bytes"] for f in inventory}
    best = min(
        sum(sizes[x] for x in set().union(*combo))
        for combo in itertools.product(*options_per_day)
    )
    assert result["summary"]["retained_bytes"] == best


def test_offline_part_makes_tap_basis_unusable():
    inventory = [
        file("T1", "tap", 10, date="2026-05-01", fleet="1"),
        file("T2", "tap", 10, date="2026-05-01", fleet="2"),
        file("D1", "dict", 5, version="v1"),
    ]
    result = run(inventory, [report("W1", "2026-05-01", "1,2", "v1")], offline=["T2"])
    assert result["status"] == "blocked"
    assert any("T2" in reason for reason in result["missing"][0]["reasons"])


def test_sample_data_plan_is_stable():
    result = run(sample_inventory(), sample_reports())
    assert result["status"] == "ok"
    assert result["summary"]["day_count"] == 6
    assert result["summary"]["retained_bytes"] == 60_100_000
    assert result["summary"]["cleared_bytes"] == 123_000_000
    # 每个被留存文件至少被一个周报日引用。
    referenced = {
        f["file_id"] for day in result["days"] for f in day["files"]
    }
    assert {f["file_id"] for f in result["retained"]} == referenced


# ---------- 导入校验 ----------

def test_reports_cap_is_12():
    rows = ["report_no,date,fleet_scope,dict_version"]
    for i in range(13):
        rows.append(f"W{i},2026-05-{i + 1:02d},1,v1")
    with pytest.raises(importer.ImportError_) as excinfo:
        importer.parse_reports("\n".join(rows))
    assert "12" in excinfo.value.message


def test_inventory_rejects_duplicate_and_bad_type():
    text = (
        "file_id,type,size_bytes,date,fleet\n"
        "A,tap,1MB,2026-05-01,1\n"
        "A,tap,2MB,2026-05-01,1\n"
        "B,weird,3MB,2026-05-01,1\n"
    )
    with pytest.raises(importer.ImportError_) as excinfo:
        importer.parse_inventory(text)
    joined = "\n".join(excinfo.value.errors)
    assert "重复" in joined
    assert "无法识别类型" in joined


def test_chinese_headers_and_size_units():
    text = "文件名,类型,大小,日期,车队,车队范围,字典版本\nT1,刷卡分区,1.5GiB,2026-05-01,1,,\n"
    rows = importer.parse_inventory(text)
    assert rows[0]["size_bytes"] == int(1.5 * 1024**3)


# ---------- Web 流程：确认只改登记、旧单被拒 ----------

def import_all(client):
    inventory = (ROOT / "static/samples/inventory.csv").read_text(encoding="utf-8")
    reports = (ROOT / "static/samples/reports.csv").read_text(encoding="utf-8")
    client.post("/import/inventory", data={"text": inventory})
    client.post("/import/reports", data={"text": reports})


def state():
    return webapp.load_state()


def test_confirm_only_updates_registry(client):
    import_all(client)
    client.post("/plan")
    before = {f["file_id"]: f["size_bytes"] for f in state()["inventory"]}
    plan = state()["plan"]
    cleared = {f["file_id"] for f in plan["cleared"]}
    retained = {f["file_id"] for f in plan["retained"]}

    client.post("/confirm")
    after = {f["file_id"]: f["size_bytes"] for f in state()["inventory"]}
    assert set(after) == retained
    assert set(before) - set(after) == cleared
    assert state()["plan"] is None
    assert state()["history"][0]["cleared_count"] == len(cleared)
    # 磁盘文件（示例清单）不受影响
    assert (ROOT / "static/samples/inventory.csv").exists()

    # 下次核算按剩余文件进行：剩余文件刚好覆盖全部周报日，无可再清退。
    client.post("/plan")
    again = state()["plan"]
    assert again["status"] == "ok"
    assert again["summary"]["cleared_count"] == 0
    assert again["summary"]["retained_count"] == len(retained)
    assert again["summary"]["retained_bytes"] == sum(before[fid] for fid in retained)


def test_confirm_blocked_when_missing_basis(client):
    client.post(
        "/import/inventory",
        data={"text": "file_id,type,size_bytes,date,fleet,dict_version\nT1,tap,100,2026-05-01,1,1\n"},
    )
    client.post(
        "/import/reports",
        data={"text": "report_no,date,fleet_scope,dict_version\nW1,2026-05-01,1,2,v1\n"},
    )
    # 上一条车队范围列错位，改用正确行
    state_now = state()
    state_now["reports"] = [report("W1", "2026-05-01", "1", "v1")]
    webapp.save_state(state_now)
    client.post("/plan")
    assert state()["plan"]["status"] == "blocked"
    client.post("/confirm")
    assert state()["plan"]["status"] == "blocked"
    assert len(state()["inventory"]) == 1


def test_stale_plan_rejected_after_report_change(client):
    import_all(client)
    client.post("/plan")
    assert state()["plan"]["status"] == "ok"

    client.post(
        "/import/reports",
        data={"text": "report_no,date,fleet_scope,dict_version\nW2026-30,2026-05-16,1,2,v3\n"},
    )
    assert webapp.plan_is_stale(state())

    response = client.post("/confirm", follow_redirects=True)
    body = response.get_data(as_text=True)
    assert "重算后才能确认" in body
    assert len(state()["inventory"]) == 21  # 未确认
    assert state()["plan"] is not None


def test_stale_plan_rejected_after_inventory_change(client):
    import_all(client)
    client.post("/plan")
    inventory = (ROOT / "static/samples/inventory.csv").read_text(encoding="utf-8")
    client.post(
        "/import/inventory",
        data={"text": inventory + "EXTRA-1,other,1234,,,,\n"},
    )
    assert webapp.plan_is_stale(state())
    client.post("/confirm")
    assert len(state()["inventory"]) == 22


def test_stale_plan_rejected_after_offline_change(client):
    import_all(client)
    client.post("/plan")
    client.post("/offline", data={"must_offline": ["LOG-2025-Q4.txt"]})
    assert webapp.plan_is_stale(state())
    client.post("/confirm")
    assert len(state()["inventory"]) == 21


def test_page_renders_basis_and_cleared_list(client):
    import_all(client)
    client.post("/plan")
    body = client.get("/").get_data(as_text=True)
    assert "逐日复算依据" in body
    assert "清退清单" in body
    assert "共用文件" in body
    assert "TAP-20260518-F1" in body


def test_export_csv(client):
    import_all(client)
    client.post("/plan")
    response = client.get("/export/cleared.csv")
    assert response.status_code == 200
    text = response.get_data(as_text=True)
    assert "文件名" in text and "清退原因" in text
    response = client.get("/export/basis.csv")
    assert "复算依据" in response.get_data(as_text=True)
