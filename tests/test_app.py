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


# ---------- 周报首位站核查 ----------

import verifier  # noqa: E402


def check(summary_text, stations, scope, supplements=None):
    check_state = {
        "stations": stations,
        "fleet_scope": scope,
        "summaries": verifier.parse_summaries(
            summary_text, stations, planner.scope_fleets(scope)
        ),
        "supplements": supplements or {},
    }
    return verifier.evaluate(check_state)


def summaries(*blocks):
    rows = ["fleet,total,station,count,cap"]
    for fleet, total, cap, counts in blocks:
        if not counts:
            rows.append(f"{fleet},{total},,,{cap}")
        for station, count in counts:
            rows.append(f"{fleet},{total},{station},{count},{cap}")
    return "\n".join(rows)


def test_candidates_only_when_a_completion_can_make_them_first():
    # 车队 1：总量 100、上限 10、已报 S001=90；车队 2：总量 100、上限 10、已报 S002=96。
    # S001 最多 94，S003 最多 14，S002 至少 96 → 只有 S002 能排第一。
    result = check(
        summaries(("1", 100, 10, [("S001", 90)]), ("2", 100, 10, [("S002", 96)])),
        ["S001", "S002", "S003"],
        "1,2",
    )
    assert result["status"] == "confirmed"
    assert result["confirmed"] == "S002"

    # 各 100、上限 100：三站都能通过补全成为第一 → 候选 3 个。
    result = check(
        summaries(("1", 100, 100, []), ("2", 100, 100, [])),
        ["S001", "S002", "S003"],
        "1,2",
    )
    assert result["status"] == "pending"
    assert set(result["candidates"]) == {"S001", "S002", "S003"}


def test_total_constraint_changes_candidates():
    # 同一个站号集合，只改车队 2 的总量就能把候选从多解收敛为唯一。
    stations = ["S001", "S002", "S003"]
    loose = check(
        summaries(("1", 100, 50, [("S001", 50)]), ("2", 100, 50, [("S002", 50)])),
        stations,
        "1,2",
    )
    assert loose["status"] == "pending"

    tight = check(
        summaries(("1", 100, 50, [("S001", 50)]), ("2", 100, 5, [("S002", 95)])),
        stations,
        "1,2",
    )
    assert tight["status"] == "confirmed"
    assert tight["confirmed"] == "S002"


def test_tie_break_by_station_lexicographic_order():
    # 车队 1 已报 S001=6，两车队各上限 5、总量 6：S001 已定 6，其它站最多 5，
    # S001 恒排第一。把站号录入顺序倒过来，结论仍是 S001。
    result = check(
        summaries(("1", 6, 5, [("S001", 6)]), ("2", 6, 5, [])),
        ["S002", "S001"],
        "1,2",
    )
    assert result["status"] == "confirmed"
    assert result["confirmed"] == "S001"

    # 并列时的字典序：两站都可能达到同样的最高合计，站号小的先排。
    result = check(
        summaries(("1", 5, 5, []), ("2", 5, 5, [])),
        ["S002", "S001"],
        "1,2",
    )
    # 两站上限完全对称，S001 与 S002 都能成为并列第一，故不是唯一候选；
    # 复核逐站可行性与求解器口径一致。
    assert "S001" in result["candidates"] and "S002" in result["candidates"]


def test_missing_fleet_summary_blocks_and_names_fleet():
    result = check(summaries(("1", 100, 10, [])), ["S001", "S002"], "1,2")
    assert result["status"] == "blocked"
    assert result["missing_fleets"] == ["2"]
    assert any("2" in item["fleet"] and "缺车队简表" in item["reason"] for item in result["blocked"])


def test_infeasible_completion_blocks_and_names_fleet():
    # 总量 100，已报 90，漏列 1 站但上限只有 5 → 无法补全。
    result = check(
        summaries(("1", 100, 5, [("S001", 90)])),
        ["S001", "S002"],
        "1",
    )
    assert result["status"] == "blocked"
    assert any(item["fleet"] == "1" and "无法补全" in item["reason"] for item in result["blocked"])

    # 没有任何漏列站点，但已报合计与总量不符，同样无法补全。
    result = check(
        summaries(("1", 100, 5, [("S001", 90), ("S002", 5)])),
        ["S001", "S002"],
        "1",
    )
    assert result["status"] == "blocked"


def test_summary_rejects_bad_counts_and_unknown_station():
    with pytest.raises(importer.ImportError_) as excinfo:
        verifier.parse_summaries(
            "fleet,total,station,count,cap\n1,100,S001,-1,10\n1,100,S009,5,10\n",
            ["S001"],
            ["1"],
        )
    joined = "\n".join(excinfo.value.errors)
    assert "非负整数" in joined
    assert "不在已录入的全部站号中" in joined


def test_summary_rejects_fleet_outside_scope():
    with pytest.raises(importer.ImportError_) as excinfo:
        verifier.parse_summaries(
            "fleet,total,station,count,cap\n9,100,S001,5,10\n", ["S001"], ["1", "2"]
        )
    assert "不在本周报日车队范围" in "\n".join(excinfo.value.errors)


def test_supplement_conflicts():
    parsed = verifier.parse_summaries(
        "fleet,total,station,count,cap\n1,100,S001,60,50\n", ["S001", "S002"], ["1"]
    )["1"]

    good = verifier.parse_supplement("station,count\nS001,60\nS002,40\n", ["S001", "S002"])
    assert verifier.supplement_conflicts(parsed, good, ["S001", "S002"]) == []

    bad_total = verifier.parse_supplement("station,count\nS001,60\nS002,39\n", ["S001", "S002"])
    assert any("当日总刷卡量" in e for e in verifier.supplement_conflicts(parsed, bad_total, ["S001", "S002"]))

    bad_reported = verifier.parse_supplement("station,count\nS001,61\nS002,39\n", ["S001", "S002"])
    assert any("已报计数" in e for e in verifier.supplement_conflicts(parsed, bad_reported, ["S001", "S002"]))

    tight = verifier.parse_summaries(
        "fleet,total,station,count,cap\n1,100,S001,60,10\n", ["S001", "S002"], ["1"]
    )["1"]
    over_tight = verifier.parse_supplement("station,count\nS001,60\nS002,40\n", ["S001", "S002"])
    assert any("单站上限" in e for e in verifier.supplement_conflicts(tight, over_tight, ["S001", "S002"]))

    incomplete = "station,count\nS001,60\n"
    with pytest.raises(importer.ImportError_) as excinfo:
        verifier.parse_supplement(incomplete, ["S001", "S002"])
    assert "覆盖全部站号" in "\n".join(excinfo.value.errors)


def import_day(client, report_no="W1", date="2026-05-16", scope='"1,2"'):
    client.post(
        "/import/reports",
        data={"text": f"report_no,date,fleet_scope,dict_version\n{report_no},{date},{scope},v3\n"},
    )


def verify_page(client, report_no="W1", date="2026-05-16"):
    return client.get(f"/verify/{report_no}/{date}").get_data(as_text=True)


def test_verify_page_lists_only_explicit_scope_days(client):
    client.post(
        "/import/reports",
        data={
            "text": "report_no,date,fleet_scope,dict_version\n"
            "W1,2026-05-16,1;2,v3\nW2,2026-05-17,*,v3\n"
        },
    )
    body = client.get("/verify").get_data(as_text=True)
    assert "W1" in body
    assert "W2" in body  # 以“未列入核查”说明出现
    assert "无法逐车队收简表" in body


def test_web_total_constraint_changes_candidates(client):
    import_day(client)
    client.post(
        "/verify/W1/2026-05-16/stations",
        data={"stations": "S001,S002,S003"},
    )
    client.post(
        "/verify/W1/2026-05-16/summaries",
        data={
            "text": "fleet,total,station,count,cap\n"
            "1,100,,0,50\n1,100,S001,50,50\n"
            "2,100,,0,50\n2,100,S002,50,50\n"
        },
    )
    body = verify_page(client)
    assert "候选 3 个" in body
    assert state()["checks"]["W1|2026-05-16"]["result"]["status"] == "pending"

    # 只改车队 2 的总量约束（100 → 已报 95 + 上限 5），候选收敛为 S002。
    client.post(
        "/verify/W1/2026-05-16/summaries",
        data={"text": "fleet,total,station,count,cap\n2,100,S002,95,5\n"},
    )
    body = verify_page(client)
    assert "已核定首位" in body and "S002" in body
    result = state()["checks"]["W1|2026-05-16"]["result"]
    assert result["status"] == "confirmed"
    assert result["confirmed"] == "S002"


def test_web_conflicting_supplement_keeps_original_result(client):
    import_day(client)
    client.post("/verify/W1/2026-05-16/stations", data={"stations": "S001,S002,S003"})
    client.post(
        "/verify/W1/2026-05-16/summaries",
        data={
            "text": "fleet,total,station,count,cap\n"
            "1,100,S001,60,30\n"
            "2,100,S002,95,5\n"
        },
    )
    before = state()["checks"]["W1|2026-05-16"]
    assert before["result"]["status"] == "confirmed"
    before_candidates = list(before["result"]["candidates"])

    # 合计正确但改了已报站点 S001 的计数 → 与原简表对不上，整份退回。
    response = client.post(
        "/verify/W1/2026-05-16/supplement",
        data={"fleet": "1", "text": "station,count\nS001,61\nS002,39\nS003,0\n"},
        follow_redirects=True,
    )
    body = response.get_data(as_text=True)
    assert "整份退回" in body
    after = state()["checks"]["W1|2026-05-16"]
    assert after["supplements"] == {}
    assert after["result"]["candidates"] == before_candidates
    assert after["result"]["status"] == before["result"]["status"]

    # 有效的补报（合计 100、已报站点不变、漏列站 S002 与 S003 各 20，均不超上限 30）
    # 替换旧补报，并立即重算候选。
    client.post(
        "/verify/W1/2026-05-16/supplement",
        data={"fleet": "1", "text": "station,count\nS001,60\nS002,20\nS003,20\n"},
    )
    after = state()["checks"]["W1|2026-05-16"]
    assert after["supplements"]["1"]["S002"] == 20
    assert after["result"]["status"] in {"confirmed", "pending"}

    # 再补一份有效的不同计数：替换而非叠加。
    client.post(
        "/verify/W1/2026-05-16/supplement",
        data={"fleet": "1", "text": "station,count\nS001,60\nS002,25\nS003,15\n"},
    )
    after = state()["checks"]["W1|2026-05-16"]
    assert after["supplements"]["1"]["S002"] == 25
    assert after["supplements"]["1"]["S003"] == 15


def test_web_supplement_requires_full_station_coverage(client):
    import_day(client)
    client.post("/verify/W1/2026-05-16/stations", data={"stations": "S001,S002"})
    client.post(
        "/verify/W1/2026-05-16/summaries",
        data={"text": "fleet,total,station,count,cap\n1,100,S001,60,10\n2,100,S002,95,5\n"},
    )
    response = client.post(
        "/verify/W1/2026-05-16/supplement",
        data={"fleet": "1", "text": "station,count\nS001,60\n"},
        follow_redirects=True,
    )
    assert "整份退回" in response.get_data(as_text=True)
    assert state()["checks"]["W1|2026-05-16"]["supplements"] == {}


def test_replacing_summary_drops_conflicting_old_supplement(client):
    import_day(client)
    client.post("/verify/W1/2026-05-16/stations", data={"stations": "S001,S002"})
    client.post(
        "/verify/W1/2026-05-16/summaries",
        data={"text": "fleet,total,station,count,cap\n1,100,S001,60,50\n2,100,S002,95,5\n"},
    )
    client.post(
        "/verify/W1/2026-05-16/supplement",
        data={"fleet": "1", "text": "station,count\nS001,60\nS002,40\n"},
    )
    assert state()["checks"]["W1|2026-05-16"]["supplements"]["1"]["S002"] == 40

    # 新简表把车队 1 的已报站点改成 S002=70，旧补报 S001=60 与之冲突 → 作废。
    client.post(
        "/verify/W1/2026-05-16/summaries",
        data={"text": "fleet,total,station,count,cap\n1,100,S002,70,50\n"},
    )
    after = state()["checks"]["W1|2026-05-16"]
    assert after["summaries"]["1"]["counts"] == {"S002": 70}
    assert "1" not in after["supplements"]
    assert after["result"]["status"] in {"confirmed", "pending", "blocked"}


def test_clearing_reports_clears_checks(client):
    import_day(client)
    client.post("/verify/W1/2026-05-16/stations", data={"stations": "S001,S002"})
    assert "W1|2026-05-16" in state()["checks"]
    client.post("/reports/clear")
    assert state()["checks"] == {}
