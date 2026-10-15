"""Spreadsheet tools: CSV load/save, filter, aggregate, schema-check.

Stdlib `csv` only. Rows are plain dicts; amounts are integer cents columns
(any column whose name ends in `_cents` is coerced to int on load).
Read tools are side-effect-free; `save` writes a file (side-effecting).
"""
from __future__ import annotations

import csv
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from .contracts import REGISTRY

CENTS_SUFFIX = "_cents"


def load_csv(path: str | Path) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    # utf-8-sig, not utf-8: Excel writes UTF-8 CSVs with a BOM by default, and
    # plain utf-8 leaves it attached to the first header, so `invoice_id`
    # arrives as `\ufeffinvoice_id` — a key no caller can guess, and one that
    # breaks every lookup, filter and schema check on that column. Reading with
    # utf-8-sig strips a BOM when present and is identical to utf-8 when not.
    with open(path, newline="", encoding="utf-8-sig") as fh:
        for raw in csv.DictReader(fh):
            row: Dict[str, Any] = {}
            for key, val in raw.items():
                if key is None:
                    continue
                if key.endswith(CENTS_SUFFIX):
                    row[key] = int(val or 0)
                else:
                    row[key] = val
            rows.append(row)
    return rows


def save_csv(path: str | Path, rows: List[Dict[str, Any]],
             columns: Optional[List[str]] = None) -> str:
    if not rows:
        columns = columns or []
    else:
        columns = columns or list(rows[0].keys())
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)
    return str(path)


def filter_rows(rows: List[Dict[str, Any]], **equals: Any) -> List[Dict[str, Any]]:
    out = []
    for row in rows:
        if all(str(row.get(k)) == str(v) for k, v in equals.items()):
            out.append(row)
    return out


def aggregate(rows: List[Dict[str, Any]], *, group_by: str,
              sum_column: str) -> Dict[str, int]:
    totals: Dict[str, int] = {}
    for row in rows:
        key = str(row.get(group_by, ""))
        totals[key] = totals.get(key, 0) + int(row.get(sum_column, 0) or 0)
    return totals


def check_schema(rows: List[Dict[str, Any]],
                 required: List[str]) -> List[Dict[str, Any]]:
    """Return a list of problems; empty list means the sheet is usable."""
    problems = []
    for i, row in enumerate(rows):
        for col in required:
            if col not in row or row[col] in (None, ""):
                problems.append({"row": i, "column": col, "problem": "missing"})
    return problems


def register(registry=REGISTRY) -> None:
    @registry.tool(
        "spreadsheet.load", "Load a CSV sheet as row dicts. Read-only.",
        {"path": "str"}, {"rows": "list[dict]", "count": "int"},
        side_effects=False, tags=("spreadsheet",),
    )
    def _load(path: str) -> Dict[str, Any]:
        rows = load_csv(path)
        return {"rows": rows, "count": len(rows)}

    @registry.tool(
        "spreadsheet.save", "Write row dicts to a CSV file. Side-effecting "
        "(touches the filesystem).",
        {"path": "str", "rows": "list[dict]", "columns": "list[str]|None"},
        {"path": "str"},
        side_effects=True, tags=("spreadsheet",),
    )
    def _save(path: str, rows: List[Dict[str, Any]],
              columns: Optional[List[str]] = None) -> Dict[str, str]:
        return {"path": save_csv(path, rows, columns)}

    @registry.tool(
        "spreadsheet.aggregate", "Group rows and sum a column. Read-only.",
        {"rows": "list[dict]", "group_by": "str", "sum_column": "str"},
        {"totals": "dict[str,int]"},
        side_effects=False, tags=("spreadsheet",),
    )
    def _aggregate(rows: List[Dict[str, Any]], group_by: str,
                   sum_column: str) -> Dict[str, Any]:
        return {"totals": aggregate(rows, group_by=group_by, sum_column=sum_column)}


def register_all(reg: Any = REGISTRY) -> None:
    register(reg)
