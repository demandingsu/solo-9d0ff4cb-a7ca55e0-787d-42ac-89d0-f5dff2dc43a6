"""周报首位站核查：车队简表、合法补全与候选首位站。

业务口径：
* 只在“车队范围明确”的周报日（显式车队列表）上核查。
* 每个车队当日给出：总刷卡量、部分站点计数、漏列站点的**单站**上限。
* 一个合法补全给每个漏列站点分配 0..上限 的整数计数，使该车队各站计数合计
  恰好等于当日总刷卡量；已报站点计数不再变化。
* 站点当日客流 = 各车队在该站计数之和；按该合计降序排名，同量按站号字典序升序。
* 某站“可排第一”当且仅当存在一种合法补全，使它在上述排名中居首。
* 只有候选唯一时才核定首位。
"""

from __future__ import annotations

import re
from collections import deque

import importer
import planner

MAX_STATIONS = 200
MAX_SUMMARY_ROWS = 5000

SUMMARY_HEADERS = {
    "fleet": {"fleet", "fleets", "车队", "车队编号", "车队号"},
    "total": {
        "total",
        "total_taps",
        "day_total",
        "总量",
        "当日总量",
        "当日总刷卡量",
        "总刷卡量",
        "刷卡总量",
    },
    "station": {"station", "station_id", "station_no", "站点", "站号", "站点编号"},
    "count": {"count", "counts", "taps", "计数", "站点计数", "刷卡量", "刷卡次数"},
    "cap": {
        "cap",
        "limit",
        "upper",
        "upper_bound",
        "漏列上限",
        "单站上限",
        "上限",
    },
}

SUPPLEMENT_HEADERS = {
    "station": {"station", "station_id", "station_no", "站点", "站号", "站点编号"},
    "count": {"count", "counts", "taps", "计数", "站点计数", "刷卡量", "刷卡次数"},
}


class ImportError_(importer.ImportError_):
    """沿用导入模块的逐条错误结构。"""


def _parse_int(raw: str, label: str) -> int:
    text = str(raw or "").strip().replace(" ", "").replace("，", ",")
    if not text:
        raise ValueError(f"{label}为空")
    if re.fullmatch(r"\d{1,3}(?:,\d{3})+", text):
        text = text.replace(",", "")
    if not re.fullmatch(r"\d+", text):
        raise ValueError(f"{label} {raw!r} 不是非负整数")
    return int(text)


def parse_stations(text: str) -> list[str]:
    """把站号文本拆成去重、按字典序排序的列表。"""
    tokens = [
        token
        for token in re.split(r"[,，、;；\s]+", str(text or "").strip())
        if token
    ]
    if not tokens:
        raise ImportError_("请录入全部站号")
    stations = sorted(set(tokens))
    if len(stations) > MAX_STATIONS:
        raise ImportError_(f"站号最多 {MAX_STATIONS} 个，本次 {len(stations)} 个")
    return stations


def parse_summaries(
    text: str, stations: list[str], allowed_fleets: list[str]
) -> dict[str, dict]:
    """解析各车队简表。

    长表格式，列：fleet,total,station,count,cap。同一车队的 total/cap 必须在
    每行一致；station 留空表示该车队没有上报任何站点（此时 count 须留空或 0）。
    """
    if not text or not text.strip():
        raise ImportError_("请上传或粘贴各车队简表")
    header, rows = importer._read_csv(text)
    columns = importer._map_headers(header, SUMMARY_HEADERS)
    labels = {
        "fleet": "车队",
        "total": "当日总刷卡量",
        "station": "站号",
        "count": "站点计数",
        "cap": "漏列单站上限",
    }
    missing_required = [f for f in ("fleet", "total", "cap") if f not in columns]
    if missing_required:
        raise ImportError_(
            "表头缺少："
            + "、".join(labels[f] for f in missing_required)
            + "。可用列：fleet,total,station,count,cap"
        )
    if "station" in columns and "count" not in columns:
        raise ImportError_("表头有站号列但没有站点计数列")
    if "count" in columns and "station" not in columns:
        raise ImportError_("表头有站点计数列但没有站号列")
    if len(rows) > MAX_SUMMARY_ROWS:
        raise ImportError_(f"单次最多导入 {MAX_SUMMARY_ROWS} 行简表")

    station_set = set(stations)
    allowed = set(allowed_fleets)
    errors: list[str] = []
    meta: dict[str, tuple[int, int]] = {}
    counts: dict[str, dict[str, int]] = {}

    for line_no, row in enumerate(rows, start=2):
        if not any(cell.strip() for cell in row):
            continue

        def cell(field: str) -> str:
            position = columns.get(field)
            if position is None or position >= len(row):
                return ""
            return row[position].strip()

        fleet = cell("fleet")
        if not fleet:
            errors.append(f"第 {line_no} 行：车队为空")
            continue
        if fleet not in allowed:
            scope = "、".join(allowed_fleets) or "（空）"
            errors.append(f"第 {line_no} 行：车队 {fleet} 不在本周报日车队范围（{scope}）内")
            continue
        try:
            total = _parse_int(cell("total"), "当日总刷卡量")
            cap = _parse_int(cell("cap"), "漏列单站上限")
        except ValueError as exc:
            errors.append(f"第 {line_no} 行：{exc}")
            continue

        if fleet in meta and meta[fleet] != (total, cap):
            old_total, old_cap = meta[fleet]
            errors.append(
                f"第 {line_no} 行：车队 {fleet} 的总量/上限与前面不一致"
                f"（{total}/{cap}，前面为 {old_total}/{old_cap}）"
            )
            continue
        meta[fleet] = (total, cap)

        station = cell("station")
        count_raw = cell("count")
        bucket = counts.setdefault(fleet, {})
        if not station:
            if count_raw:
                try:
                    stray = _parse_int(count_raw, "站点计数")
                except ValueError as exc:
                    errors.append(f"第 {line_no} 行：{exc}")
                    continue
                if stray != 0:
                    errors.append(f"第 {line_no} 行：站号为空但填了计数 {stray}")
            continue
        if station not in station_set:
            errors.append(f"第 {line_no} 行：站号 {station} 不在已录入的全部站号中")
            continue
        try:
            count = _parse_int(count_raw, "站点计数")
        except ValueError as exc:
            errors.append(f"第 {line_no} 行：{exc}")
            continue
        if station in bucket:
            errors.append(f"第 {line_no} 行：车队 {fleet} 的站号 {station} 重复")
            continue
        bucket[station] = count

    if errors:
        raise ImportError_(f"车队简表有 {len(errors)} 处问题，未保存任何内容", errors)
    if not meta:
        raise ImportError_("车队简表没有数据行")
    return {
        fleet: {"total": total, "cap": cap, "counts": dict(counts.get(fleet, {}))}
        for fleet, (total, cap) in meta.items()
    }


def parse_supplement(text: str, stations: list[str]) -> dict[str, int]:
    """解析某车队的全站计数补报，列：station,count，必须覆盖全部站号。"""
    if not text or not text.strip():
        raise ImportError_("请上传或粘贴全站计数")
    header, rows = importer._read_csv(text)
    columns = importer._map_headers(header, SUPPLEMENT_HEADERS)
    missing_required = [f for f in ("station", "count") if f not in columns]
    if missing_required:
        labels = {"station": "站号", "count": "站点计数"}
        raise ImportError_(
            "表头缺少：" + "、".join(labels[f] for f in missing_required)
            + "。可用列：station,count"
        )
    if len(rows) > MAX_SUMMARY_ROWS:
        raise ImportError_(f"单次最多导入 {MAX_SUMMARY_ROWS} 行补报")

    station_set = set(stations)
    errors: list[str] = []
    counts: dict[str, int] = {}
    for line_no, row in enumerate(rows, start=2):
        if not any(cell.strip() for cell in row):
            continue

        def cell(field: str) -> str:
            position = columns.get(field)
            if position is None or position >= len(row):
                return ""
            return row[position].strip()

        station = cell("station")
        if not station:
            errors.append(f"第 {line_no} 行：站号为空")
            continue
        if station not in station_set:
            errors.append(f"第 {line_no} 行：站号 {station} 不在已录入的全部站号中")
            continue
        if station in counts:
            errors.append(f"第 {line_no} 行：站号 {station} 重复")
            continue
        try:
            counts[station] = _parse_int(cell("count"), "站点计数")
        except ValueError as exc:
            errors.append(f"第 {line_no} 行：{exc}")

    missing = [station for station in stations if station not in counts]
    if missing:
        shown = "、".join(missing[:10])
        suffix = f" 等 {len(missing)} 个" if len(missing) > 10 else ""
        errors.append(f"补报必须覆盖全部站号，缺少：{shown}{suffix}")
    if errors:
        raise ImportError_(f"补报有 {len(errors)} 处问题，整份退回", errors)
    return counts


def supplement_conflicts(
    summary: dict, counts: dict[str, int], stations: list[str]
) -> list[str]:
    """补报须与原简表对得上；返回全部冲突，空列表表示有效。"""
    errors: list[str] = []
    actual_total = sum(counts[station] for station in stations)
    if actual_total != summary["total"]:
        errors.append(
            f"全站计数合计 {actual_total} 与原简表的当日总刷卡量 {summary['total']} 不符"
        )
    for station, reported in summary["counts"].items():
        if station in counts and counts[station] != reported:
            errors.append(
                f"站号 {station} 补交 {counts[station]} 与原简表已报计数 {reported} 不符"
            )
    cap = summary["cap"]
    for station in stations:
        if station not in summary["counts"] and counts[station] > cap:
            errors.append(
                f"站号 {station} 补交 {counts[station]} 超过原简表的漏列单站上限 {cap}"
            )
    return errors


class _MaxFlow:
    """小规模 Dinic，用于判断补全是否可行。"""

    def __init__(self, size: int):
        self.size = size
        self.graph: list[list[list[int]]] = [[] for _ in range(size)]

    def add_edge(self, source: int, target: int, capacity: int) -> None:
        self.graph[source].append([target, capacity, len(self.graph[target])])
        self.graph[target].append([source, 0, len(self.graph[source]) - 1])

    def max_flow(self, source: int, sink: int) -> int:
        flow = 0
        inf = float("inf")
        while True:
            level = [-1] * self.size
            level[source] = 0
            queue = deque([source])
            while queue:
                node = queue.popleft()
                for target, capacity, _ in self.graph[node]:
                    if capacity > 0 and level[target] < 0:
                        level[target] = level[node] + 1
                        queue.append(target)
            if level[sink] < 0:
                return flow
            cursor = [0] * self.size

            def push(node: int, amount: float) -> int:
                if node == sink:
                    return int(amount)
                while cursor[node] < len(self.graph[node]):
                    edge = self.graph[node][cursor[node]]
                    target, capacity, reverse = edge
                    if capacity > 0 and level[target] == level[node] + 1:
                        sent = push(target, min(amount, capacity))
                        if sent:
                            edge[1] -= sent
                            self.graph[target][reverse][1] += sent
                            return sent
                    cursor[node] += 1
                return 0

            while True:
                sent = push(source, inf)
                if not sent:
                    break
                flow += sent


def _can_be_first(
    station: str,
    stations: list[str],
    infos: dict[str, dict],
    reported_totals: dict[str, int],
) -> bool:
    """固定该站取最大可取值，判断其余站点能否全部被压在它之下。"""
    base = reported_totals[station]
    supplies: dict[str, tuple[int, list[str]]] = {}

    for fleet, info in infos.items():
        if station in info["fixed"]:
            # 已报站点（含已补交全站计数）不随补全变化，已在 reported_totals 中。
            supplies[fleet] = (info["rem"], list(info["unlisted"]))
            continue
        maximum = min(info["cap"], info["rem"])
        base += maximum
        supplies[fleet] = (
            info["rem"] - maximum,
            [item for item in info["unlisted"] if item != station],
        )

    # 其余站点能接受的上限：必须排在 station 之后。
    capacities: dict[str, int] = {}
    for other in stations:
        if other == station:
            continue
        strict = 1 if station > other else 0
        capacity = base - strict - reported_totals[other]
        if capacity < 0:
            return False
        capacities[other] = capacity

    total_supply = sum(supply for supply, _ in supplies.values())
    if total_supply == 0:
        return True

    fleets = list(supplies)
    other_stations = [item for item in stations if item != station]
    fleet_node = {fleet: index + 1 for index, fleet in enumerate(fleets)}
    station_offset = 1 + len(fleets)
    station_node = {
        item: station_offset + index for index, item in enumerate(other_stations)
    }
    sink = station_offset + len(other_stations)
    flow = _MaxFlow(sink + 1)
    source = 0

    for fleet in fleets:
        supply, variables = supplies[fleet]
        if supply <= 0:
            continue
        flow.add_edge(source, fleet_node[fleet], supply)
        for item in variables:
            flow.add_edge(fleet_node[fleet], station_node[item], infos[fleet]["cap"])
    for item in other_stations:
        flow.add_edge(station_node[item], sink, capacities[item])

    return flow.max_flow(source, sink) == total_supply


def _fleet_rows(
    fleets: list[str],
    stations: list[str],
    summaries: dict[str, dict],
    supplements: dict[str, dict],
    infos: dict[str, dict],
    reasons: dict[str, str],
) -> list[dict]:
    rows = []
    for fleet in fleets:
        summary = summaries.get(fleet)
        if summary is None:
            rows.append(
                {
                    "fleet": fleet,
                    "status": "missing",
                    "reason": "缺车队简表",
                    "total": None,
                    "cap": None,
                    "reported_sum": None,
                    "reported_count": 0,
                    "unlisted_count": len(stations),
                    "remaining": None,
                    "supplement": False,
                    "supplement_total": None,
                    "counts": [],
                    "supplement_counts": [],
                }
            )
            continue
        info = infos[fleet]
        counts = sorted(summary["counts"].items())
        supplement_counts = sorted(supplements.get(fleet, {}).items())
        rows.append(
            {
                "fleet": fleet,
                "status": "infeasible" if fleet in reasons else "ok",
                "reason": reasons.get(fleet, ""),
                "total": summary["total"],
                "cap": summary["cap"],
                "reported_sum": sum(summary["counts"].values()),
                "reported_count": len(summary["counts"]),
                "unlisted_count": len(info["unlisted"]),
                "remaining": info["rem"],
                "supplement": fleet in supplements,
                "supplement_total": sum(supplements[fleet].values())
                if fleet in supplements
                else None,
                "counts": counts,
                "supplement_counts": supplement_counts,
            }
        )
    return rows


def _station_rows(
    stations: list[str],
    infos: dict[str, dict],
    reported_totals: dict[str, int],
    candidates: set[str],
) -> list[dict]:
    rows = []
    for station in stations:
        low = reported_totals[station]
        high = reported_totals[station]
        for info in infos.values():
            if station in info["fixed"]:
                continue
            other_unlisted = len(info["unlisted"]) - 1
            forced = max(0, info["rem"] - info["cap"] * other_unlisted)
            low += forced
            high += min(info["cap"], info["rem"])
        rows.append(
            {
                "station": station,
                "reported_total": reported_totals[station],
                "min_total": low,
                "max_total": high,
                "candidate": station in candidates,
            }
        )
    return rows


def evaluate(check: dict) -> dict:
    """按当前站号、简表与补报重算候选首位站。"""
    stations = list(check.get("stations", []))
    fleets = planner.scope_fleets(check.get("fleet_scope", ""))
    summaries: dict[str, dict] = check.get("summaries", {})
    supplements: dict[str, dict] = check.get("supplements", {})

    reasons: dict[str, str] = {}
    infos: dict[str, dict] = {}
    blocked: list[dict] = []

    for fleet in fleets:
        if fleet not in summaries:
            continue
        summary = summaries[fleet]
        if fleet in supplements:
            counts = {station: int(supplements[fleet].get(station, 0)) for station in stations}
            infos[fleet] = {
                "fixed": counts,
                "rem": 0,
                "cap": 0,
                "unlisted": [],
                "total": summary["total"],
            }
            continue
        fixed = {station: int(count) for station, count in summary["counts"].items()}
        unlisted = [station for station in stations if station not in fixed]
        reported_sum = sum(fixed.values())
        remaining = summary["total"] - reported_sum
        reason = ""
        if remaining < 0:
            reason = (
                f"已报站点计数合计 {reported_sum} 超过当日总刷卡量 {summary['total']}"
            )
        elif not unlisted and remaining != 0:
            reason = (
                f"已报站点已覆盖全部站号，但计数合计 {reported_sum} "
                f"与当日总刷卡量 {summary['total']} 不符"
            )
        elif unlisted and remaining > summary["cap"] * len(unlisted):
            reason = (
                f"漏列 {len(unlisted)} 个站点、单站上限 {summary['cap']}，"
                f"最多只能补 {summary['cap'] * len(unlisted)}，"
                f"但总量与已报计数相差 {remaining}，无法补全"
            )
        if reason:
            reasons[fleet] = reason
        infos[fleet] = {
            "fixed": fixed,
            "rem": remaining,
            "cap": summary["cap"],
            "unlisted": unlisted,
            "total": summary["total"],
        }

    missing_fleets = [fleet for fleet in fleets if fleet not in summaries]
    for fleet in missing_fleets:
        blocked.append({"fleet": fleet, "reason": "缺车队简表"})
    for fleet in fleets:
        if fleet in reasons:
            blocked.append({"fleet": fleet, "reason": reasons[fleet]})

    reported_totals = {station: 0 for station in stations}
    for info in infos.values():
        for station, count in info["fixed"].items():
            reported_totals[station] += count

    candidates: list[str] = []
    if not blocked:
        candidates = [
            station
            for station in stations
            if _can_be_first(station, stations, infos, reported_totals)
        ]
        if not candidates:
            blocked.append(
                {
                    "fleet": "（全部车队）",
                    "reason": "没有任何站点能在合法补全中排第一，请核对站号与简表",
                }
            )

    candidates_set = set(candidates)
    if blocked:
        status = "blocked"
        confirmed = None
    elif len(candidates) == 1:
        status = "confirmed"
        confirmed = candidates[0]
    else:
        status = "pending"
        confirmed = None

    return {
        "status": status,
        "confirmed": confirmed,
        "candidates": candidates,
        "blocked": blocked,
        "missing_fleets": missing_fleets,
        "fleet_rows": _fleet_rows(
            fleets, stations, summaries, supplements, infos, reasons
        ),
        "station_rows": _station_rows(stations, infos, reported_totals, candidates_set),
        "station_count": len(stations),
        "fleet_count": len(fleets),
    }
