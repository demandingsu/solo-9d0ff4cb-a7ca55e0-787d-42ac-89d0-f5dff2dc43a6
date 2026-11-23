"""CSV 导入与校验：在库文件清单、周报日记录。"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import re
from datetime import date as date_cls
from decimal import Decimal

from planner import FILE_TYPES, normalize_scope

MAX_REPORT_DAYS = 12
MAX_FILE_ROWS = 5000

TYPE_ALIASES = {
    "daily_agg": "daily_agg",
    "dailyagg": "daily_agg",
    "agg": "daily_agg",
    "summary": "daily_agg",
    "日汇总": "daily_agg",
    "汇总": "daily_agg",
    "tap": "tap",
    "taps": "tap",
    "tap_partition": "tap",
    "partition": "tap",
    "刷卡分区": "tap",
    "刷卡": "tap",
    "分区": "tap",
    "dict": "dict",
    "dictionary": "dict",
    "station_dict": "dict",
    "归站字典": "dict",
    "字典": "dict",
    "other": "other",
    "其他": "other",
    "其它": "other",
}

INVENTORY_HEADERS = {
    "file_id": {"file_id", "file", "filename", "name", "文件", "文件名", "文件编号", "文件id"},
    "type": {"type", "file_type", "kind", "类型", "文件类型"},
    "size_bytes": {"size_bytes", "size", "bytes", "大小", "字节数", "文件大小"},
    "date": {"date", "day", "日期", "业务日期"},
    "fleet": {"fleet", "车队", "车队编号"},
    "fleet_scope": {"fleet_scope", "scope", "fleets", "车队范围"},
    "dict_version": {"dict_version", "dict", "version", "字典版本", "归站字典版本"},
}

REPORT_HEADERS = {
    "report_no": {"report_no", "report", "report_id", "周报编号", "周报号", "周报"},
    "date": {"date", "day", "日期", "业务日期"},
    "fleet_scope": {"fleet_scope", "scope", "fleets", "车队范围"},
    "dict_version": {"dict_version", "dict", "version", "字典版本", "归站字典版本"},
}

SIZE_PATTERN = re.compile(
    r"^(?P<num>\d+(?:\.\d+)?)\s*(?P<unit>B|KB|MB|GB|TB|KIB|MIB|GIB|TIB)?$",
    re.IGNORECASE,
)
SIZE_UNITS = {
    "": 1,
    "B": 1,
    "KB": 1000,
    "MB": 1000**2,
    "GB": 1000**3,
    "TB": 1000**4,
    "KIB": 1024,
    "MIB": 1024**2,
    "GIB": 1024**3,
    "TIB": 1024**4,
}


class ImportError_(ValueError):
    """带逐行错误的导入失败。"""

    def __init__(self, message: str, errors: list[str] | None = None):
        super().__init__(message)
        self.message = message
        self.errors = errors or []


def _split_lines(text: str) -> list[str]:
    return [line for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n") if line.strip()]


def _read_csv(text: str) -> tuple[list[str], list[list[str]]]:
    lines = _split_lines(text)
    if not lines:
        raise ImportError_("内容为空")
    sample = "\n".join(lines[:5])
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",\t;|")
        delimiter = dialect.delimiter
    except csv.Error:
        delimiter = "\t" if "\t" in lines[0] else ","
    reader = csv.reader(io.StringIO("\n".join(lines)), delimiter=delimiter)
    rows = [[cell.strip() for cell in row] for row in reader]
    header = rows[0]
    if len(header) == 1 and delimiter in header[0]:
        raise ImportError_("无法识别分隔符，请使用逗号、制表符或分号分隔")
    return header, rows[1:]


def _map_headers(header: list[str], mapping: dict[str, set[str]]) -> dict[str, int]:
    index: dict[str, int] = {}
    for position, raw in enumerate(header):
        key = raw.strip().lower().lstrip("﻿")
        key = re.sub(r"[\s_]+", "_", key)
        for field, aliases in mapping.items():
            normalized_aliases = {re.sub(r"[\s_]+", "_", alias.lower()) for alias in aliases}
            if key in normalized_aliases and field not in index:
                index[field] = position
                break
    return index


def parse_size(raw: str) -> int:
    text = str(raw or "").strip().replace(" ", "")
    if not text:
        raise ValueError("字节数为空")
    if re.fullmatch(r"\d{1,3}(,\d{3})+(\.\d+)?", text):
        text = text.replace(",", "")
    match = SIZE_PATTERN.match(text)
    if not match:
        raise ValueError(f"无法识别字节数 {raw!r}（可写 123456 或 1.5GiB）")
    value = Decimal(match.group("num")) * SIZE_UNITS[(match.group("unit") or "").upper()]
    if value != value.to_integral_value():
        raise ValueError(f"字节数 {raw!r} 不是整数")
    return int(value)


def parse_date(raw: str) -> str:
    text = str(raw or "").strip().replace("/", "-").replace(".", "-")
    if not text:
        raise ValueError("日期为空")
    try:
        parsed = date_cls.fromisoformat(text)
    except ValueError:
        raise ValueError(f"日期 {raw!r} 不是 YYYY-MM-DD") from None
    return parsed.isoformat()


def infer_type(file_id: str) -> str:
    """类型列留空时按文件名前缀猜测（TAP-/AGG-/DICT-）。"""
    prefix = re.split(r"[-_.]", file_id.strip(), maxsplit=1)[0].upper()
    return {
        "TAP": "tap",
        "AGG": "daily_agg",
        "SUM": "daily_agg",
        "DICT": "dict",
    }.get(prefix, "")


def parse_inventory(text: str) -> list[dict]:
    if not text or not text.strip():
        raise ImportError_("请上传或粘贴在库文件清单")
    header, rows = _read_csv(text)
    columns = _map_headers(header, INVENTORY_HEADERS)
    missing_required = [f for f in ("file_id", "size_bytes") if f not in columns]
    if missing_required:
        labels = {"file_id": "文件名/文件编号", "size_bytes": "字节数/大小"}
        raise ImportError_(
            "表头缺少：" + "、".join(labels[f] for f in missing_required)
            + "。可用列：file_id,type,size_bytes,date,fleet,fleet_scope,dict_version"
        )
    if len(rows) > MAX_FILE_ROWS:
        raise ImportError_(f"单次最多导入 {MAX_FILE_ROWS} 行文件")
    if not rows:
        raise ImportError_("清单没有数据行")

    result: list[dict] = []
    errors: list[str] = []
    seen: set[str] = set()
    for line_no, row in enumerate(rows, start=2):
        if not any(cell.strip() for cell in row):
            continue

        def cell(field: str) -> str:
            position = columns.get(field)
            if position is None or position >= len(row):
                return ""
            return row[position].strip()

        file_id = cell("file_id")
        if not file_id:
            errors.append(f"第 {line_no} 行：文件名为空")
            continue
        if file_id in seen:
            errors.append(f"第 {line_no} 行：文件名 {file_id} 重复")
            continue

        type_raw = cell("type").lower()
        if not type_raw:
            type_raw = infer_type(file_id)
        file_type = TYPE_ALIASES.get(type_raw) if type_raw else None
        if file_type is None:
            errors.append(
                f"第 {line_no} 行：缺少或无法识别类型（daily_agg / tap / dict / other）"
            )
            continue
        if file_type not in FILE_TYPES:
            errors.append(f"第 {line_no} 行：未知类型 {cell('type')!r}")
            continue

        try:
            size_bytes = parse_size(cell("size_bytes"))
        except ValueError as exc:
            errors.append(f"第 {line_no} 行：{exc}")
            continue
        if size_bytes < 0:
            errors.append(f"第 {line_no} 行：字节数不能为负")
            continue

        try:
            file_date = parse_date(cell("date")) if cell("date") else ""
        except ValueError as exc:
            errors.append(f"第 {line_no} 行：{exc}")
            continue

        file = {
            "file_id": file_id,
            "type": file_type,
            "size_bytes": size_bytes,
            "date": file_date,
            "fleet": cell("fleet"),
            "fleet_scope": normalize_scope(cell("fleet_scope")),
            "dict_version": cell("dict_version"),
        }

        if file_type == "daily_agg":
            if not file["date"]:
                errors.append(f"第 {line_no} 行：日汇总缺少日期")
                continue
            if not file["fleet_scope"]:
                errors.append(f"第 {line_no} 行：日汇总缺少车队范围")
                continue
            if not file["dict_version"]:
                errors.append(f"第 {line_no} 行：日汇总缺少字典版本")
                continue
        elif file_type == "tap":
            if not file["date"]:
                errors.append(f"第 {line_no} 行：刷卡分区缺少日期")
                continue
            if not file["fleet"]:
                errors.append(f"第 {line_no} 行：刷卡分区缺少车队")
                continue
        elif file_type == "dict":
            if not file["dict_version"]:
                errors.append(f"第 {line_no} 行：归站字典缺少字典版本")
                continue

        seen.add(file_id)
        result.append(file)

    if errors:
        raise ImportError_(f"文件清单有 {len(errors)} 处问题，未导入任何行", errors)
    return result


def parse_reports(text: str) -> list[dict]:
    if not text or not text.strip():
        raise ImportError_("请上传或粘贴周报日记录")
    header, rows = _read_csv(text)
    columns = _map_headers(header, REPORT_HEADERS)
    missing_required = [f for f in ("report_no", "date", "fleet_scope", "dict_version") if f not in columns]
    if missing_required:
        labels = {
            "report_no": "周报编号",
            "date": "日期",
            "fleet_scope": "车队范围",
            "dict_version": "字典版本",
        }
        raise ImportError_("表头缺少：" + "、".join(labels[f] for f in missing_required))

    result: list[dict] = []
    errors: list[str] = []
    seen: set[tuple[str, str]] = set()
    for line_no, row in enumerate(rows, start=2):
        if not any(cell_value.strip() for cell_value in row):
            continue

        def cell(field: str) -> str:
            position = columns.get(field)
            if position is None or position >= len(row):
                return ""
            return row[position].strip()

        report_no = cell("report_no")
        if not report_no:
            errors.append(f"第 {line_no} 行：周报编号为空")
            continue
        try:
            report_date = parse_date(cell("date"))
        except ValueError as exc:
            errors.append(f"第 {line_no} 行：{exc}")
            continue
        scope = normalize_scope(cell("fleet_scope"))
        if not scope:
            errors.append(f"第 {line_no} 行：车队范围为空")
            continue
        version = cell("dict_version")
        if not version:
            errors.append(f"第 {line_no} 行：字典版本为空")
            continue
        key = (report_no, report_date)
        if key in seen:
            errors.append(f"第 {line_no} 行：周报 {report_no} 的 {report_date} 重复")
            continue

        seen.add(key)
        result.append(
            {
                "report_no": report_no,
                "date": report_date,
                "fleet_scope": scope,
                "dict_version": version,
            }
        )

    if errors:
        raise ImportError_(f"周报记录有 {len(errors)} 处问题，未导入任何行", errors)
    if not result:
        raise ImportError_("周报记录没有数据行")
    if len(result) > MAX_REPORT_DAYS:
        raise ImportError_(f"周报日记录最多 {MAX_REPORT_DAYS} 条，本次 {len(result)} 条，超出上限")
    return result


def fingerprint(inventory: list[dict], reports: list[dict], must_offline: list[str]) -> str:
    payload = {
        "inventory": sorted(
            [
                {
                    "file_id": f["file_id"],
                    "type": f["type"],
                    "size_bytes": int(f["size_bytes"]),
                    "date": f.get("date", ""),
                    "fleet": f.get("fleet", ""),
                    "fleet_scope": f.get("fleet_scope", ""),
                    "dict_version": f.get("dict_version", ""),
                }
                for f in inventory
            ],
            key=lambda f: f["file_id"],
        ),
        "reports": sorted(
            [
                {
                    "report_no": r["report_no"],
                    "date": r["date"],
                    "fleet_scope": r["fleet_scope"],
                    "dict_version": r["dict_version"],
                }
                for r in reports
            ],
            key=lambda r: (r["report_no"], r["date"]),
        ),
        "must_offline": sorted(set(must_offline)),
    }
    blob = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()
