"""Sheet engine: typed columns, formula evaluation, masking and exports.

Apps never call a spreadsheet product directly — they call these tools, which
keep tenancy, permissions and audit logging in one place.

Two engine kinds:

* live (`feishu` / `wps` / `grist`) — a hosted sheet that stays refreshed.
  `LocalSheetEngine` is the development implementation: it materialises the view
  in-process and serves it from this service's own
  `/v1/sheets/views/{view_id}` endpoint. A real 飞书多维表格 or WPS 表格 backend
  slots in by implementing the same `SheetEngine` protocol against its API.
* `xlsx` / `csv` — a one-off file, written through `FileStore`, returning
  `file_id`.

Formulas are evaluated here, by this module's own sandboxed AST interpreter, not
by the hosted sheet. That keeps the formula language and its security guarantees
identical across every engine.

Security properties enforced here, not left to callers:

* Account and ID numbers are **masked in every sheet** and cannot be unmasked
  through the API (guide § security rules).
* Formulas are evaluated in a restricted AST namespace. `$field` resolves to a
  cell value; nothing else reaches Python's builtins or the filesystem.
* Imported text and cell contents are data, never instructions.
"""

from __future__ import annotations

import ast
import csv
import io
import re
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable, Iterable, Protocol

from .errors import HeliosError, invalid_formula, validation
from .filestore import FileStore, mask_account

# Columns whose values are always masked on the way out.
SENSITIVE_FIELDS = frozenset({
    "account", "account_number", "bank_account", "card_number",
    "id_number", "identity", "payment_details", "tax_id",
})
# Substrings that mark a column as sensitive even when the name varies.
SENSITIVE_SUBSTRINGS = ("account", "card_no", "id_no", "identity", "身份证", "账号", "卡号")

# Formula namespaces: `$field` -> row lookup.
DOLLAR_RE = re.compile(r"\$([A-Za-z_][A-Za-z0-9_]*)")

# Whitelisted callables inside a formula. Deliberately tiny.
# TODAY/NOW are tz-aware so they can be subtracted from a record's tz-aware
# due_at, as in the guide's `(TODAY() - $due_at).days`.
SAFE_FUNCTIONS: dict[str, Callable[..., Any]] = {
    "TODAY": lambda: datetime.now(timezone.utc).replace(
        hour=0, minute=0, second=0, microsecond=0),
    "NOW": lambda: datetime.now(timezone.utc),
    "abs": abs, "len": len, "min": min, "max": max, "round": round,
    "int": int, "float": float, "str": str, "bool": bool,
    "sum": sum, "sorted": sorted, "list": list,
}
# Attribute access is allowed only on these types, and never on dunders.
SAFE_ATTR_TYPES = (datetime, date, timedelta, str, int, float, bool, list, dict)

FORBIDDEN_NODES = (
    ast.Import, ast.ImportFrom, ast.Global, ast.Nonlocal, ast.Lambda,
    ast.Await, ast.Yield, ast.YieldFrom, ast.AsyncFunctionDef,
)


class FormulaError(HeliosError):
    """A computed column's formula failed; `message` names the column.

    Guide § Errors: "`invalid_formula` … `message` names the column", so the
    column name is part of the human-readable message, not only of `details`.
    """

    def __init__(self, column: str, reason: str):
        super().__init__(
            "invalid_formula",
            f"formula for column '{column}' is invalid: {reason}",
            details={"column": column})
        self.column = column


def is_sensitive(field: str) -> bool:
    """Whether a column must be masked in every sheet."""
    f = (field or "").lower()
    if f in SENSITIVE_FIELDS:
        return True
    return any(s in f for s in SENSITIVE_SUBSTRINGS)


def mask_cell(field: str, value: Any) -> Any:
    """Mask account/ID numbers; pass everything else through unchanged."""
    if not is_sensitive(field) or value in (None, ""):
        return value
    if isinstance(value, dict):
        return {k: mask_account(str(v)) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [mask_account(str(v)) for v in value]
    return mask_account(str(value))


# --------------------------------------------------------------- formula eval

def _transform(formula: str) -> str:
    """Rewrite `$field` into `row["field"]` so the AST sees plain subscripts."""
    return DOLLAR_RE.sub(lambda m: f'row["{m.group(1)}"]', formula)


def _validate(node: ast.AST, column: str) -> None:
    """Reject anything outside the whitelisted expression subset."""
    if isinstance(node, FORBIDDEN_NODES):
        raise FormulaError(column, f"{type(node).__name__} is not allowed in a formula")
    if isinstance(node, ast.Attribute):
        if node.attr.startswith("_"):
            raise FormulaError(column, f"attribute '{node.attr}' is not allowed")
    if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
        if node.id not in SAFE_FUNCTIONS and node.id not in ("row", "True", "False", "None"):
            raise FormulaError(column, f"name '{node.id}' is not available in a formula")
    if isinstance(node, ast.Call):
        fn = node.func
        if not isinstance(fn, ast.Name) or fn.id not in SAFE_FUNCTIONS:
            raise FormulaError(column, "only whitelisted functions may be called")
    if isinstance(node, ast.Subscript) and isinstance(node.value, ast.Name):
        if node.value.id != "row":
            raise FormulaError(column, "only `$field` references may be subscripted")
    for child in ast.iter_child_nodes(node):
        _validate(child, column)


def compile_formula(formula: str, column: str) -> Callable[[dict], Any]:
    """Compile a `$field` Python formula into a row -> value callable.

    The formula language is this module's own, evaluated by the sandboxed AST
    interpreter below, so it behaves the same on every sheet engine.

    Raises `invalid_formula` naming the column on anything unsafe or unparseable.
    """
    if not formula or not str(formula).strip():
        raise FormulaError(column, "formula is empty")
    source = _transform(str(formula))
    try:
        tree = ast.parse(source, mode="eval")
    except SyntaxError as exc:
        raise FormulaError(column, f"syntax error: {exc.msg}") from exc
    _validate(tree, column)
    code = compile(tree, f"<formula:{column}>", "eval")

    def evaluate(row: dict) -> Any:
        try:
            return eval(code, {"__builtins__": {}}, dict(SAFE_FUNCTIONS, row=row))
        except Exception as exc:
            raise FormulaError(column, f"{type(exc).__name__}: {exc}") from exc

    return evaluate


def formula_from_describe(describe: str, column: str,
                          qwen: Any) -> str:
    """Ask Qwen to turn a plain-language description into a column formula.

    Guide § render_sheet: a computed column takes a `formula`, or a plain-language
    `describe` that Qwen turns into a formula. Degrades to `None` when Qwen is
    not configured, so the caller can report it instead of guessing.
    """
    system = (
        "You translate a plain-language column description into a single Python "
        "formula expression for a spreadsheet column. Reference other columns as "
        "$column_name. "
        "Allowed functions: TODAY(), NOW(), abs, len, min, max, round, int, "
        "float, str, sum, sorted. Return ONLY the formula text, no explanation, "
        "no code fence.")
    try:
        out = qwen.complete(system, f"Column '{column}': {describe}")
    except HeliosError:
        return ""
    return (out or "").strip().strip("`").strip()


# ------------------------------------------------------------------- columns

def normalize_columns(columns: Iterable[Any]) -> list[dict[str, Any]]:
    """Turn guide `columns` input into a uniform list of column specs.

    Accepts a plain field name string, `{"name", "formula"}`, or
    `{"name", "describe"}`.
    """
    out: list[dict[str, Any]] = []
    for col in columns:
        if isinstance(col, str):
            out.append({"name": col, "kind": "field", "field": col})
        elif isinstance(col, dict):
            name = col.get("name")
            if not name:
                raise validation("a computed column must have a 'name'")
            if col.get("formula"):
                out.append({"name": name, "kind": "formula",
                            "formula": col["formula"]})
            elif col.get("describe"):
                out.append({"name": name, "kind": "describe",
                            "describe": col["describe"]})
            elif col.get("field"):
                out.append({"name": name, "kind": "field", "field": col["field"]})
            else:
                out.append({"name": name, "kind": "field", "field": name})
        else:
            raise validation(
                f"column must be a field name or an object, got {type(col).__name__}")
    if not out:
        raise validation("columns must not be empty")
    names = [c["name"] for c in out]
    dupes = {n for n in names if names.count(n) > 1}
    if dupes:
        raise validation(f"duplicate column names: {sorted(dupes)}")
    return out


def resolve_formulas(specs: list[dict[str, Any]], qwen: Any) -> list[dict[str, Any]]:
    """Compile formulas; convert `describe` columns via Qwen first."""
    resolved = []
    for spec in specs:
        if spec["kind"] == "describe":
            formula = formula_from_describe(spec["describe"], spec["name"], qwen)
            if not formula:
                raise HeliosError(
                    "upstream_unavailable",
                    f"could not derive a formula for column '{spec['name']}' "
                    "(Qwen inference unavailable); pass an explicit `formula`",
                    retryable=True, details={"column": spec["name"]})
            spec = {**spec, "kind": "formula", "formula": formula,
                    "derived_from": spec["describe"]}
        if spec["kind"] == "formula":
            spec = {**spec, "evaluate": compile_formula(spec["formula"], spec["name"])}
        resolved.append(spec)
    return resolved


def project_rows(records: Iterable[Any], specs: list[dict[str, Any]]) -> list[dict]:
    """Apply column specs to records, masking sensitive fields.

    Formulas are evaluated against Python values (`mode="python"`), so
    `$due_at` is a real `datetime` and the guide's `(TODAY() - $due_at).days`
    works. Exported field values are JSON-serialised.
    """
    rows: list[dict] = []
    for rec in records:
        if hasattr(rec, "model_dump"):
            dump_json = rec.model_dump(mode="json")
            dump_py = rec.model_dump(mode="python")
        else:
            dump_json = dump_py = dict(rec)
        row: dict[str, Any] = {}
        for spec in specs:
            name = spec["name"]
            if spec["kind"] == "field":
                row[name] = mask_cell(spec["field"], dump_json.get(spec["field"]))
            else:
                try:
                    value = spec["evaluate"](dump_py)
                except HeliosError:
                    raise
                except Exception as exc:
                    raise FormulaError(name, f"{type(exc).__name__}: {exc}") from exc
                if isinstance(value, (datetime, date)):
                    value = value.isoformat()
                row[name] = mask_cell(name, value)
        row["_id"] = getattr(rec, "id", dump_json.get("id"))
        rows.append(row)
    return rows


def apply_access(rows: list[dict], access: dict | None, role: str | None) -> list[dict]:
    """Enforce `{role: "all" | {hide: [...]}}` on an exported page of rows."""
    if not access or not role:
        return rows
    rule = access.get(role)
    if rule in (None, "all"):
        return rows
    if isinstance(rule, dict):
        hidden = set(rule.get("hide", []))
        if hidden:
            return [{k: v for k, v in r.items() if k not in hidden} for r in rows]
    return rows


def sort_rows(rows: list[dict], sort: str | None,
              group_by: str | None) -> list[dict]:
    """Order rows by `group_by` then `sort`; a leading '-' means descending."""
    if not rows:
        return rows
    keys: list[tuple[str, bool]] = []
    for spec in (group_by, sort):
        if not spec:
            continue
        desc = str(spec).startswith("-")
        keys.append((str(spec).lstrip("-"), desc))

    def keyfn(row: dict):
        out = []
        for field, _desc in keys:
            v = row.get(field)
            # Keep None sortable and mixed types comparable.
            out.append((v is None, v if not isinstance(v, bool) else int(v)))
        return tuple(out)

    if not keys:
        return rows
    if any(desc for _f, desc in keys):
        # Mixed directions: stable-sort from the least significant key.
        result = list(rows)
        for field, desc in reversed(keys):
            result.sort(key=lambda r, f=field: (r.get(f) is None, r.get(f)),
                        reverse=desc)
        return result
    return sorted(rows, key=keyfn)


# -------------------------------------------------------------------- engines

class SheetEngine(Protocol):
    """What a live sheet backend must provide."""

    def create_view(self, view: Any, rows: list[dict]) -> str: ...
    def refresh_view(self, view_id: str, rows: list[dict]) -> str: ...


class LocalSheetEngine:
    """Development engine: materialises views in-process and serves them here.

    Stands in for a hosted spreadsheet so `render_sheet` returns a real, working
    `url` during development and demos. Swapping in 飞书多维表格 or WPS 表格 means
    implementing `SheetEngine` against its API; the contract (typed columns,
    access rules, cell change notifications) is unchanged.
    """

    def __init__(self, base_url: str = ""):
        self.base_url = base_url.rstrip("/")
        self.views: dict[str, dict] = {}

    def create_view(self, view: Any, rows: list[dict]) -> str:
        self.views[view.id] = {
            "columns": [c["name"] for c in view.columns] if view.columns and
                       isinstance(view.columns[0], dict) else list(view.columns),
            "access": view.access,
            "rows": rows,
            "editable_columns": view.editable_columns,
            "protected_columns": view.protected_columns,
        }
        return f"{self.base_url}/v1/sheets/views/{view.id}" if self.base_url \
            else f"/v1/sheets/views/{view.id}"

    def refresh_view(self, view_id: str, rows: list[dict]) -> str:
        if view_id in self.views:
            self.views[view_id]["rows"] = rows
        return self.create_view_url(view_id)

    def create_view_url(self, view_id: str) -> str:
        return f"{self.base_url}/v1/sheets/views/{view_id}" if self.base_url \
            else f"/v1/sheets/views/{view_id}"

    def get_rows(self, view_id: str, role: str | None = None) -> list[dict]:
        """Read a view's rows, applying that role's access rule."""
        entry = self.views.get(view_id)
        if entry is None:
            raise validation(f"unknown view_id '{view_id}'")
        return apply_access(entry["rows"], entry.get("access"), role)


def write_xlsx(rows: list[dict], columns: list[str]) -> bytes:
    """Render rows to an .xlsx workbook."""
    try:
        from openpyxl import Workbook
    except ImportError as exc:  # pragma: no cover
        raise HeliosError("unsupported_format",
                          "xlsx export requires openpyxl") from exc
    wb = Workbook()
    ws = wb.active
    ws.title = "Sheet"
    ws.append(list(columns))
    for row in rows:
        ws.append([row.get(c) for c in columns])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def write_csv(rows: list[dict], columns: list[str]) -> bytes:
    """Render rows to UTF-8 CSV with a BOM so Excel opens Chinese text correctly."""
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(list(columns))
    for row in rows:
        writer.writerow([row.get(c) for c in columns])
    return buf.getvalue().encode("utf-8-sig")


def export_file(rows: list[dict], columns: list[str], target: str,
                filestore: FileStore, business_id: str,
                view_id: str) -> str:
    """Produce a one-off xlsx/csv file and return its file_id."""
    if target == "xlsx":
        data, name = write_xlsx(rows, columns), f"{view_id}.xlsx"
    elif target == "csv":
        data, name = write_csv(rows, columns), f"{view_id}.csv"
    else:
        raise validation(f"unsupported export target '{target}'")
    stored = filestore.put(name, data, business_id)
    return stored.file_id
