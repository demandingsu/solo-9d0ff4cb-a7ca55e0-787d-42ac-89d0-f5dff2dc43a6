"""客车队刷卡数据清退的复算依据规划。

核心规则（与页面说明一致）：
* 一个周报日（周报编号 / 日期 / 车队范围 / 归站字典版本）有且只有两类复算依据：
  1. 一条日汇总：日期、车队范围、字典版本三项完全吻合；
  2. 当天所需车队的全部刷卡分区 + 该字典版本的一份归站字典。
* 被勾选“必须下线”的文件不能出现在任何复算依据中。
* 所有周报日各选一种依据，使留存文件的并集字节数最小；共用文件只计一次。
* 任一周报日没有任何可用依据时，整体暂停清退，并逐条指出周报和日期。
"""

from __future__ import annotations

import re
from collections import defaultdict

FILE_TYPES = ("daily_agg", "tap", "dict", "other")
TYPE_LABELS = {
    "daily_agg": "日汇总",
    "tap": "刷卡分区",
    "dict": "归站字典",
    "other": "其他",
}
TYPE_ORDER = {"daily_agg": 0, "tap": 1, "dict": 2, "other": 3}


def normalize_scope(raw) -> str:
    """把车队范围规范成 ``*`` 或排序去重后的逗号列表。"""
    if raw is None:
        return ""
    text = str(raw).strip()
    if not text:
        return ""
    if text.upper() in {"*", "ALL", "ANY"} or text in {
        "全部",
        "全部车队",
        "所有",
        "所有车队",
    }:
        return "*"
    parts = [part.strip() for part in re.split(r"[,，、;；\s]+", text) if part.strip()]
    if not parts:
        return ""
    if "*" in parts or any(part in {"全部", "所有"} for part in parts):
        return "*"
    return ",".join(sorted(set(parts)))


def scope_fleets(scope: str) -> list[str]:
    if not scope or scope == "*":
        return []
    return scope.split(",")


def scopes_match(left: str, right: str) -> bool:
    """车队范围是否吻合。

    “吻合”按严格相等理解：``*``（全部车队）只与 ``*`` 相等，不与任何显式列表互换，
    以免把“某天恰好只有 1、2 车队刷卡”误当作“车队范围就是 1、2”。
    """
    return normalize_scope(left) == normalize_scope(right)


def scope_display(scope: str) -> str:
    if not scope:
        return "—"
    if scope == "*":
        return "全部车队"
    return "、".join(scope.split(","))


def _cheapest(files: list[dict], excluded: set[str]) -> dict | None:
    candidates = [f for f in files if f["file_id"] not in excluded]
    if not candidates:
        return None
    return min(candidates, key=lambda f: (int(f["size_bytes"]), f["file_id"]))


def _option(kind: str, files: list[dict], size_by_id: dict[str, int]) -> dict:
    ordered = sorted(
        files,
        key=lambda f: (TYPE_ORDER.get(f.get("type", "other"), 9), f["file_id"]),
    )
    file_ids = [f["file_id"] for f in ordered]
    return {
        "kind": kind,
        "basis_label": "日汇总" if kind == "daily_agg" else "刷卡分区 + 归站字典",
        "files": ordered,
        "file_ids": file_ids,
        "bytes": sum(size_by_id[fid] for fid in file_ids),
    }


def _dedupe_options(options: list[dict]) -> list[dict]:
    by_set: dict[tuple[str, ...], dict] = {}
    for option in options:
        key = tuple(sorted(option["file_ids"]))
        current = by_set.get(key)
        if current is None or option["bytes"] < current["bytes"]:
            by_set[key] = option
    return sorted(
        by_set.values(),
        key=lambda o: (o["bytes"], 0 if o["kind"] == "daily_agg" else 1, tuple(o["file_ids"])),
    )


def build_plan(
    inventory: list[dict],
    reports: list[dict],
    must_offline: list[str],
    fingerprint: str,
) -> dict:
    """生成可确认或阻断的清退方案。

    返回的字典可直接序列化保存；``status`` 为 ``ok`` 或 ``blocked``。
    """
    excluded = set(must_offline)
    size_by_id = {f["file_id"]: int(f["size_bytes"]) for f in inventory}
    inventory_ids = set(size_by_id)

    aggs_by_date_version: dict[tuple[str, str], list[dict]] = defaultdict(list)
    dict_by_version: dict[str, list[dict]] = defaultdict(list)
    taps_by_date_fleet: dict[tuple[str, str], list[dict]] = defaultdict(list)
    fleets_by_date: dict[str, set[str]] = defaultdict(set)

    for file in inventory:
        kind = file.get("type")
        if kind == "daily_agg":
            aggs_by_date_version[
                (file.get("date", ""), file.get("dict_version", ""))
            ].append(file)
        elif kind == "tap":
            date = file.get("date", "")
            fleet = file.get("fleet", "")
            taps_by_date_fleet[(date, fleet)].append(file)
            fleets_by_date[date].add(fleet)
        elif kind == "dict":
            dict_by_version[file.get("dict_version", "")].append(file)

    day_options: list[list[dict]] = []
    missing: list[dict] = []

    for report in reports:
        date = report.get("date", "")
        scope = report.get("fleet_scope", "")
        version = report.get("dict_version", "")
        report_no = report.get("report_no", "")

        agg_reason = ""
        matching_aggs = [
            file
            for file in aggs_by_date_version.get((date, version), [])
            if scopes_match(scope, file.get("fleet_scope", ""))
        ]
        agg_file = _cheapest(matching_aggs, excluded)
        agg_options: list[dict] = []
        if agg_file is not None:
            # 同日期同版本下，所有“吻合”的日汇总车队集合必然相同，
            # 能覆盖的周报日也完全相同，取最便宜的一份即可（支配关系）。
            agg_options = [_option("daily_agg", [agg_file], size_by_id)]
        elif matching_aggs:
            ids = "、".join(sorted(f["file_id"] for f in matching_aggs))
            agg_reason = f"匹配的日汇总已全部勾选必须下线：{ids}"
        else:
            agg_reason = "无日期、车队范围、字典版本三项完全吻合的日汇总"

        tap_reasons: list[str] = []
        tap_options: list[dict] = []

        if scope == "*":
            required_fleets = sorted(fleets_by_date.get(date, set()))
            if not required_fleets:
                tap_reasons.append(f"{date} 当天没有任何刷卡分区，无法确定“全部车队”")
        else:
            required_fleets = scope_fleets(scope)
            if not required_fleets:
                tap_reasons.append("车队范围为空")

        required_taps: list[dict] = []
        if not tap_reasons:
            for fleet in required_fleets:
                parts = taps_by_date_fleet.get((date, fleet), [])
                if not parts:
                    tap_reasons.append(f"缺少 {date} 车队 {fleet} 的刷卡分区")
                    continue
                offline_parts = [f for f in parts if f["file_id"] in excluded]
                if offline_parts:
                    ids = "、".join(sorted(f["file_id"] for f in offline_parts))
                    tap_reasons.append(f"车队 {fleet} 的刷卡分区已勾选必须下线：{ids}")
                    continue
                required_taps.extend(parts)

        dict_file = None
        if not tap_reasons:
            candidates = dict_by_version.get(version, [])
            dict_file = _cheapest(candidates, excluded)
            if dict_file is None:
                if candidates:
                    ids = "、".join(sorted(f["file_id"] for f in candidates))
                    tap_reasons.append(f"字典版本 {version} 的归站字典已全部勾选必须下线：{ids}")
                else:
                    tap_reasons.append(f"缺少字典版本 {version} 的归站字典")
            else:
                # 同版本字典对所有用该版本的周报日等价，取最便宜的一份即可。
                tap_options = [
                    _option("tap_dict", required_taps + [dict_file], size_by_id)
                ]

        options = _dedupe_options(agg_options + tap_options)
        if not options:
            missing.append(
                {
                    "report_no": report_no,
                    "date": date,
                    "fleet_scope": scope,
                    "fleet_scope_display": scope_display(scope),
                    "dict_version": version,
                    "reasons": [r for r in [agg_reason, *tap_reasons] if r],
                }
            )
            day_options.append([])
        else:
            day_options.append(options)

    summary = {
        "inventory_count": len(inventory),
        "inventory_bytes": sum(size_by_id.values()),
        "report_count": len({r.get("report_no", "") for r in reports}),
        "day_count": len(reports),
        "must_offline_count": sum(1 for fid in excluded if fid in inventory_ids),
        "must_offline_bytes": sum(size_by_id.get(fid, 0) for fid in excluded),
        "must_offline_not_in_inventory": sorted(excluded - inventory_ids),
        "missing_count": len(missing),
    }

    if missing:
        return {
            "status": "blocked",
            "fingerprint": fingerprint,
            "must_offline": sorted(excluded),
            "missing": missing,
            "days": [],
            "retained": [],
            "cleared": [],
            "shared": [],
            "summary": summary,
        }

    # 只把有依据的日交给求解器；下标与 reports 保持一致。
    effective = [(i, day_options[i]) for i in range(len(reports))]
    choices = _solve(
        [options for _, options in effective],
        size_by_id,
    )
    day_by_index = {}
    for (report_index, _), choice in zip(effective, choices):
        day_by_index[report_index] = choice

    retained_ids: set[str] = set()
    refs: dict[str, list[str]] = defaultdict(list)
    day_rows: list[dict] = []
    for index, report in enumerate(reports):
        option = day_by_index[index]
        label = f"{report.get('report_no', '')} / {report.get('date', '')}"
        for file in option["files"]:
            retained_ids.add(file["file_id"])
            refs[file["file_id"]].append(label)
        files = []
        for file in option["files"]:
            item = dict(file)
            item["shared_count"] = len(refs[file["file_id"]])
            files.append(item)
        day_rows.append(
            {
                "report_no": report.get("report_no", ""),
                "date": report.get("date", ""),
                "fleet_scope": report.get("fleet_scope", ""),
                "fleet_scope_display": scope_display(report.get("fleet_scope", "")),
                "dict_version": report.get("dict_version", ""),
                "basis_kind": option["kind"],
                "basis_label": option["basis_label"],
                "files": files,
                "bytes": option["bytes"],
            }
        )

    # 共用文件在逐日表里补全“与哪些日共用”，并把共用计数回填。
    for row in day_rows:
        label = f"{row['report_no']} / {row['date']}"
        for file in row["files"]:
            others = [ref for ref in refs[file["file_id"]] if ref != label]
            file["shared_count"] = len(refs[file["file_id"]])
            file["shared_with"] = sorted(set(others))

    retained = [f for f in inventory if f["file_id"] in retained_ids]
    retained.sort(
        key=lambda f: (
            TYPE_ORDER.get(f.get("type", "other"), 9),
            f.get("date", ""),
            f["file_id"],
        )
    )
    retained_total = sum(int(f["size_bytes"]) for f in retained)

    shared = []
    for file in retained:
        labels = refs.get(file["file_id"], [])
        if len(labels) > 1:
            shared.append(
                {
                    "file_id": file["file_id"],
                    "size_bytes": int(file["size_bytes"]),
                    "type": file.get("type", "other"),
                    "refs": sorted(set(labels)),
                    "saved_bytes": int(file["size_bytes"]) * (len(set(labels)) - 1),
                }
            )
    shared.sort(key=lambda x: (-x["size_bytes"], x["file_id"]))

    cleared = []
    for file in inventory:
        if file["file_id"] in retained_ids:
            continue
        item = dict(file)
        item["clear_reason"] = (
            "已勾选必须下线" if file["file_id"] in excluded else "未被任一复算依据引用"
        )
        cleared.append(item)
    cleared.sort(key=lambda f: (-int(f["size_bytes"]), f["file_id"]))
    cleared_total = sum(int(f["size_bytes"]) for f in cleared)

    naive_total = sum(row["bytes"] for row in day_rows)
    summary.update(
        {
            "retained_count": len(retained),
            "retained_bytes": retained_total,
            "cleared_count": len(cleared),
            "cleared_bytes": cleared_total,
            "shared_file_count": len(shared),
            "shared_saving_bytes": max(0, naive_total - retained_total),
            "release_ratio": (cleared_total / summary["inventory_bytes"] * 100)
            if summary["inventory_bytes"]
            else 0.0,
        }
    )

    return {
        "status": "ok",
        "fingerprint": fingerprint,
        "must_offline": sorted(excluded),
        "missing": [],
        "days": day_rows,
        "retained": retained,
        "cleared": cleared,
        "shared": shared,
        "summary": summary,
    }


def _solve(day_options: list[list[dict]], size_by_id: dict[str, int]) -> list[dict]:
    """为每一天选一个依据，使并集字节数最小。返回与 day_options 对齐的选择。"""
    n = len(day_options)
    if n == 0:
        return []
    choices: list[dict | None] = [None] * n

    # 按“可能共用文件”把天分组；组之间没有共享文件，可独立求最优。
    parent = list(range(n))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    file_to_days: dict[str, list[int]] = defaultdict(list)
    for i, options in enumerate(day_options):
        for option in options:
            for fid in option["file_ids"]:
                file_to_days[fid].append(i)
    for days in file_to_days.values():
        first = days[0]
        for other in days[1:]:
            union(first, other)

    components: dict[int, list[int]] = defaultdict(list)
    for i in range(n):
        components[find(i)].append(i)

    for members in components.values():
        component_choices = _solve_component(members, day_options, size_by_id)
        for index, option in component_choices.items():
            choices[index] = option

    return choices  # type: ignore[return-value]


def _solve_component(
    members: list[int],
    day_options: list[list[dict]],
    size_by_id: dict[str, int],
) -> dict[int, dict]:
    # 约束最强/最贵的先排，便于剪枝。
    order = sorted(
        members,
        key=lambda i: (len(day_options[i]), -min(o["bytes"] for o in day_options[i]), i),
    )

    def marginal(option: dict, chosen: set[str]) -> int:
        return sum(size_by_id[fid] for fid in option["file_ids"] if fid not in chosen)

    # 贪心给一个可行上界。
    greedy_chosen: set[str] = set()
    greedy_cost = 0
    greedy_choice: dict[int, dict] = {}
    for index in order:
        option = min(
            day_options[index],
            key=lambda o: (marginal(o, greedy_chosen), o["bytes"], tuple(o["file_ids"])),
        )
        add = marginal(option, greedy_chosen)
        greedy_cost += add
        greedy_chosen.update(option["file_ids"])
        greedy_choice[index] = option

    best_cost = greedy_cost
    best_choice = dict(greedy_choice)

    def recurse(
        position: int,
        chosen: set[str],
        cost: int,
        picked: dict[int, dict],
    ) -> None:
        nonlocal best_cost, best_choice
        if cost >= best_cost:
            return
        if position == len(order):
            best_cost = cost
            best_choice = dict(picked)
            return

        # 可采纳下界：剩余任一天若要成立，至少还得为它补一份文件的字节数；
        # 并集至少等于其中最大的一天所需，所以取最大值作为下界。
        # 注意不能把各天的下界相加——共用文件会使相加结果超过真实并集。
        lower = 0
        for future in order[position:]:
            day_min = min(
                sum(size_by_id[fid] for fid in option["file_ids"] if fid not in chosen)
                for option in day_options[future]
            )
            lower = max(lower, day_min)
        if cost + lower >= best_cost:
            return

        index = order[position]
        options = sorted(
            day_options[index],
            key=lambda o: (marginal(o, chosen), o["bytes"], tuple(o["file_ids"])),
        )
        for option in options:
            added = [fid for fid in option["file_ids"] if fid not in chosen]
            add = sum(size_by_id[fid] for fid in added)
            if cost + add >= best_cost:
                continue
            chosen.update(added)
            picked[index] = option
            recurse(position + 1, chosen, cost + add, picked)
            del picked[index]
            chosen.difference_update(added)

    recurse(0, set(), 0, {})
    return best_choice
