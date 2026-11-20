"""Explicit, reviewable edits of a search profile (``ultron profile edit``).

An edit is a list of operations on named fields, applied in order to a copy of the profile:

- ``add`` / ``remove`` one term or a list of terms in a term-list field. Terms compare like the
  matcher does (ignoring case and accents), so adding a present term or removing an absent one
  changes nothing and is reported as such.
- ``set`` a field to a new value, ``unset`` an optional field.

Only the fields in ``EDITABLE`` can be edited, each with its type; ``slug`` and ``source_cv`` never
change here. The edited profile is still checked with ``profiles.validate_profile`` before it is saved.
"""

from __future__ import annotations

import copy
import difflib
import json
from pathlib import Path

from ultron.core.rules.term_matching import normalize_text
from ultron.profiles import TERM_LIST_FIELDS

LIST = "list"
TEXT = "text"
FLAG = "flag"
REMOTE_REQUIRED = ("full_remote", "any")

EDITABLE: dict[str, str] = {
    "name": TEXT,
    "description": TEXT,
    "prompt": TEXT,
    "enabled": FLAG,
    "criteria.role_family_label": TEXT,
    "criteria.seniority.required": FLAG,
    "criteria.remote_policy.required": TEXT,
    **dict.fromkeys(TERM_LIST_FIELDS, LIST),
}
OPS = ("add", "remove", "set", "unset")
MAX_OPERATIONS = 200


class EditError(ValueError):
    """One or more operations cannot be applied; nothing is changed."""

    def __init__(self, errors: list[str]) -> None:
        super().__init__("; ".join(errors))
        self.errors = errors


def parse_cli_operation(op: str, raw: str) -> dict:
    """``--add PATH=TERM``, ``--remove PATH=TERM``, ``--set PATH=VALUE`` (JSON or plain text), ``--unset PATH``."""
    if op == "unset":
        return {"op": "unset", "path": raw.strip()}
    path, sep, value = raw.partition("=")
    if not sep:
        raise EditError([f"--{op} needs PATH=VALUE, got {raw!r}"])
    if op == "set":
        try:
            parsed: object = json.loads(value)
        except ValueError:
            parsed = value
        if not isinstance(parsed, (list, bool, str)):  # numbers and null: keep the text the user typed
            parsed = value
        return {"op": "set", "path": path.strip(), "value": parsed}
    return {"op": op, "path": path.strip(), "value": value}


def load_operations(source: str) -> list[dict]:
    """Read ``{"operations": [...]}`` (or a bare list of operations) from a UTF-8 JSON file."""
    try:
        data = json.loads(Path(source).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        raise EditError([f"cannot read operations file: {exc}"]) from exc
    if isinstance(data, dict):
        data = data.get("operations")
    if not isinstance(data, list) or not all(isinstance(item, dict) for item in data):
        raise EditError(['operations file must hold {"operations": [{"op": ..., "path": ..., "value": ...}]}'])
    return data


def _norm(term: str) -> str:
    return " ".join(normalize_text(term).split())


def _terms(value: object, where: str, errors: list[str]) -> list[str]:
    items = value if isinstance(value, list) else [value]
    if not items or not all(isinstance(item, str) and item.strip() for item in items):
        errors.append(f"{where}: value must be a non-empty term or a list of non-empty terms")
        return []
    return [" ".join(item.split()) for item in items]


def _container(profile: dict, path: str, *, create: bool) -> tuple[dict | None, str]:
    """The mapping that holds the last key of ``path`` (created on demand) and that key."""
    *parents, key = path.split(".")
    node: object = profile
    for part in parents:
        if not isinstance(node, dict):
            return None, key
        if part not in node or node[part] is None:
            if not create:
                return None, key
            node[part] = {}
        node = node[part]
    return (node if isinstance(node, dict) else None), key


def _check_value(path: str, kind: str, value: object) -> str | None:
    if kind == LIST and not (isinstance(value, list) and all(isinstance(item, str) for item in value)):
        return f"{path}: set needs a JSON list of strings"
    if kind == FLAG and not isinstance(value, bool):
        return f"{path}: set needs true or false"
    if kind == TEXT and not isinstance(value, str):
        return f"{path}: set needs a text value"
    if path == "criteria.remote_policy.required" and value not in REMOTE_REQUIRED:
        return f"{path}: must be one of {', '.join(REMOTE_REQUIRED)}"
    return None


def _apply_terms(target: dict, key: str, op: str, terms: list[str]) -> list[dict]:
    current = target.get(key)
    current = list(current) if isinstance(current, list) else []
    results = []
    for term in terms:
        present = [item for item in current if isinstance(item, str) and _norm(item) == _norm(term)]
        if op == "add":
            if present:
                results.append({"value": term, "status": "already_present"})
            else:
                current.append(term)
                results.append({"value": term, "status": "added"})
        elif present:
            current = [item for item in current if item not in present]
            results.append({"value": term, "status": "removed", "matched": present})
        else:
            results.append({"value": term, "status": "not_found"})
    target[key] = current
    return results


def _apply_one(profile: dict, index: int, operation: dict, errors: list[str]) -> list[dict]:
    where = f"operation {index + 1}"
    op, path = operation.get("op"), operation.get("path")
    unknown = set(operation) - {"op", "path", "value"}
    if op not in OPS:
        errors.append(f"{where}: op must be one of {', '.join(OPS)}")
        return []
    if not isinstance(path, str) or path not in EDITABLE:
        hint = " (slug and source_cv cannot be edited)" if path in {"slug", "source_cv"} else ""
        errors.append(f"{where}: field {path!r} is not editable{hint}; see `ultron profile edit --help`")
        return []
    if unknown:
        errors.append(f"{where}: unknown keys {sorted(unknown)}")
        return []
    if op in {"add", "remove"}:
        return _term_operation(profile, where, op, path, operation.get("value"), errors)
    if op == "unset":
        target, key = _container(profile, path, create=False)
        if target is None or key not in target:
            return [{"op": op, "path": path, "status": "not_found"}]
        del target[key]
        return [{"op": op, "path": path, "status": "unset"}]
    if "value" not in operation:
        errors.append(f"{where}: set needs a value")
        return []
    return _set_operation(profile, where, path, operation["value"], errors)


def _term_operation(profile: dict, where: str, op: str, path: str, value: object, errors: list[str]) -> list[dict]:
    if EDITABLE[path] != LIST:
        errors.append(f"{where}: {op} only works on term lists; use set for {path}")
        return []
    terms = _terms(value, where, errors)
    if not terms:
        return []
    target, key = _container(profile, path, create=op == "add")
    if target is None or (op == "remove" and key not in target):
        if op == "add":
            errors.append(f"{where}: {path} sits under a value that is not a mapping")
            return []
        return [{"op": op, "path": path, "value": term, "status": "not_found"} for term in terms]
    if target.get(key) is not None and not isinstance(target[key], list):
        errors.append(f"{where}: {path} is not a list")
        return []
    return [{"op": op, "path": path, **result} for result in _apply_terms(target, key, op, terms)]


def _set_operation(profile: dict, where: str, path: str, value: object, errors: list[str]) -> list[dict]:
    kind = EDITABLE[path]
    if kind == LIST and isinstance(value, list):
        value = [" ".join(item.split()) if isinstance(item, str) else item for item in value]
    if (problem := _check_value(path, kind, value)) is not None:
        errors.append(f"{where}: {problem}")
        return []
    target, key = _container(profile, path, create=True)
    if target is None:
        errors.append(f"{where}: {path} sits under a value that is not a mapping")
        return []
    before = target.get(key)
    target[key] = value
    return [{"op": "set", "path": path, "status": "unchanged" if before == value else "set"}]


def apply_operations(profile: dict, operations: list[dict]) -> tuple[dict, list[dict]]:
    """Apply ``operations`` to a deep copy of ``profile``; raise ``EditError`` if any of them is invalid."""
    if not operations:
        raise EditError(["no operations given: use --add, --remove, --set, --unset or --from"])
    if len(operations) > MAX_OPERATIONS:
        raise EditError([f"too many operations ({len(operations)}); the maximum is {MAX_OPERATIONS}"])
    edited = copy.deepcopy(profile)
    errors: list[str] = []
    changes: list[dict] = []
    for index, operation in enumerate(operations):
        if not isinstance(operation, dict):
            errors.append(f"operation {index + 1}: must be an object")
            continue
        changes.extend(_apply_one(edited, index, operation, errors))
    if errors:
        raise EditError(errors)
    return edited, changes


def unified_diff(before: str, after: str, name: str) -> str:
    """A unified diff of two profile YAML texts ("" when they are equal)."""
    lines = difflib.unified_diff(
        before.splitlines(keepends=True), after.splitlines(keepends=True),
        fromfile=f"{name} (current)", tofile=f"{name} (proposed)",
    )
    return "".join(lines)
