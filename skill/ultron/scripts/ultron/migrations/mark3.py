"""One-time import of profiles, seeds and job history from a MARK III installation.

The MARK III database is never opened in place (a WAL database would grow -shm/-wal sidecars even
read-only): the database and WAL are copied to a temp folder if stable and queried there.

Column mapping for ``jobs`` (MARK III -> ULTRON): ``first_seen_at`` -> ``first_seen`` and
``last_seen_at`` -> ``last_seen``; ``identity_key``, ``canonical_url``, ``title``, ``company``,
``location``, ``source_type``, ``source_key``, ``first_seen_run_id``, ``last_seen_run_id``,
``times_seen``, ``review_state``, ``review_changed_at``, ``notes``, ``match_score`` and
``match_band`` keep their names (missing columns fall back to ULTRON defaults);
``profile_slug`` is left empty. MARK III's other columns are dropped. ``job_feedback`` is
joined through ``job_id`` -> ``jobs.id`` -> ``identity_key``. Seeds without any profile are
linked to ``mark3-import`` so they stay in the inventory file.
"""

from __future__ import annotations

import json
import re
import shutil
import sqlite3
import tempfile
from contextlib import closing, contextmanager
from pathlib import Path

from ultron import seeds
from ultron.core.ats.identity import seed_identity
from ultron.core.vocabulary import load_packs
from ultron.infra import home
from ultron.profiles import store as profiles
from ultron.storage.ledger import connect

DB_NAMES = ("mark_iii.sqlite3", "mark_ii.sqlite3")
SEEDS_FILE = "ats_auto_seeds.yaml"
UNLINKED_PROFILE = "mark3-import"
AI_PACK = "ai-ml"

_JOB_COLUMNS = (
    "identity_key", "canonical_url", "title", "company", "location", "source_type", "source_key",
    "first_seen_run_id", "last_seen_run_id", "times_seen", "review_state", "review_changed_at",
    "notes", "match_score", "match_band",
)
_RENAMED = {"first_seen": "first_seen_at", "last_seen": "last_seen_at"}  # ULTRON name -> MARK III name


def _locate(source: Path) -> tuple[Path, Path]:
    """Return ``(data_dir, db_path)``; accepts the data folder or a checkout containing ``data/``."""
    for folder in (source, source / "data"):
        for name in DB_NAMES:
            if (folder / name).is_file():
                return folder, folder / name
    raise FileNotFoundError(f"no MARK III database ({' or '.join(DB_NAMES)}) in {source} or {source / 'data'}")


def _columns(conn: sqlite3.Connection, table: str) -> list[str]:
    return [row[1] for row in conn.execute(f"PRAGMA table_info({table})")]


def _json(value: object, default):
    if isinstance(value, (list, dict)):
        return value
    try:
        loaded = json.loads(value) if value else default
    except (TypeError, ValueError):
        return default
    return loaded if isinstance(loaded, type(default)) else default


def _word_in(needle: str, haystack: str) -> bool:
    return bool(needle) and re.search(rf"(?<!\w){re.escape(needle)}(?!\w)", haystack) is not None


def _is_ai_profile(focus: object, role_terms: set[str], ambiguity_terms: set[str]) -> bool:
    """A focus term matches when a pack role/ambiguity term is a whole word in it, or it is one in a role term."""
    if not isinstance(focus, list):
        return False
    for term in (str(t).strip().lower() for t in focus):
        if any(_word_in(p, term) for p in role_terms | ambiguity_terms):
            return True
        if any(_word_in(term, p) for p in role_terms):
            return True
    return False


def _profile_data(row: dict, ai_terms: set[str], ambiguity: set[str]) -> dict:
    criteria = _json(row.get("criteria"), {})
    if _is_ai_profile(criteria.get("role_focus_terms"), ai_terms, ambiguity):
        packs = criteria.get("vocabulary_packs")
        criteria["vocabulary_packs"] = list(dict.fromkeys([*(packs if isinstance(packs, list) else []), AI_PACK]))
    data = {
        "slug": row.get("slug"),
        "name": row.get("name"),
        "description": row.get("description") or "",
        "prompt": row.get("prompt") or "",
        "criteria": criteria,
        "query_templates": _json(row.get("query_templates"), {}),
        "source_kind": row.get("source_kind") or "manual",
        "enabled": bool(row.get("enabled", 1)),
        "created_at": row.get("created_at"),
    }
    if str(row.get("source_cv_profile") or "").strip():
        data["source_cv"] = str(row["source_cv_profile"])
    if "worked_examples" in row:
        data["worked_examples"] = _json(row.get("worked_examples"), [])
    return data


def _import_profiles(src: sqlite3.Connection, dry_run: bool, missing: list[str]) -> dict:
    out: dict = {"imported": 0, "skipped": 0, "skipped_slugs": [], "invalid": [], "vocabulary_pack_added": []}
    if "worked_examples" not in _columns(src, "search_profiles"):
        missing.append("search_profiles.worked_examples")
    pack = load_packs([AI_PACK])
    ai_terms = {t.lower() for t in pack.role_terms}
    ambiguity = {t.lower() for t in pack.ambiguity_terms}
    src.row_factory = sqlite3.Row
    try:
        rows = [dict(r) for r in src.execute("SELECT * FROM search_profiles ORDER BY id")]
    finally:
        src.row_factory = None
    for raw in rows:
        data = _profile_data(raw, ai_terms, ambiguity)
        slug = str(data["slug"] or "")
        errors = profiles.validate_profile(data)
        if errors:
            out["invalid"].append({"slug": slug, "errors": errors})
        elif (home.home_path() / "profiles" / f"{slug}.yaml").exists():
            out["skipped"] += 1
            out["skipped_slugs"].append(slug)
        else:
            if not dry_run:
                profiles.save_profile(data)
            out["imported"] += 1
            if AI_PACK in data["criteria"].get("vocabulary_packs", []):
                out["vocabulary_pack_added"].append(slug)
    return out


def _import_seeds(data_dir: Path, dry_run: bool) -> dict:
    found = seeds.read_yaml_seeds(data_dir / SEEDS_FILE)
    seed_path = home.home_path() / "seeds" / "auto_seeds.yaml"
    existing = {seed_identity(s.ats, s.board_identifier) for s in seeds.read_yaml_seeds(seed_path)}
    keys = {seed_identity(s.ats, s.board_identifier) for s in found}
    new = keys - existing
    if not dry_run:
        for seed in found:  # one call per linked profile keeps every profile of the seed
            for slug in seed.profiles or [UNLINKED_PROFILE]:
                seeds.add_seeds([seed], profile_slug=slug)
    return {"imported": len(new), "skipped": len(keys) - len(new)}


def _known_keys() -> tuple[set[str], set[str]]:
    """Identity keys already in the ULTRON ledger (read-only peek; used by dry runs)."""
    path = home.home_path() / "ultron.sqlite3"
    if not path.is_file():
        return set(), set()
    with _snapshot(path) as peek:
        try:
            return (
                {r[0] for r in peek.execute("SELECT identity_key FROM jobs")},
                {r[0] for r in peek.execute("SELECT identity_key FROM job_feedback")},
            )
        except sqlite3.DatabaseError:
            return set(), set()


def _import_history(src: sqlite3.Connection, dry_run: bool, missing: list[str]) -> tuple[dict, dict]:
    jobs_out = {"imported": 0, "skipped": 0}
    fb_out = {"imported": 0, "skipped": 0}
    have = set(_columns(src, "jobs"))
    mapping = [(c, c) for c in _JOB_COLUMNS if c in have]
    mapping += [(u, m) for u, m in _RENAMED.items() if m in have]
    names = [u for u, _ in mapping]
    if "identity_key" not in names or "canonical_url" not in names:
        missing.append("jobs.identity_key/canonical_url")
        return jobs_out, fb_out
    jobs = src.execute(f"SELECT {', '.join(m for _, m in mapping)} FROM jobs ORDER BY id").fetchall()
    tables = {r[0] for r in src.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    feedback: list = []
    if "job_feedback" in tables:
        feedback = src.execute(
            "SELECT j.identity_key, f.relevance, f.reason_code, f.note, f.labeled_at "
            "FROM job_feedback f JOIN jobs j ON j.id = f.job_id ORDER BY f.job_id"
        ).fetchall()
    else:
        missing.append("job_feedback")
    if dry_run:
        known, known_fb = _known_keys()
        new_jobs = {r[names.index("identity_key")] for r in jobs} - known
        new_fb = {r[0] for r in feedback} - known_fb
        return (
            {"imported": len(new_jobs), "skipped": len(jobs) - len(new_jobs)},
            {"imported": len(new_fb), "skipped": len(feedback) - len(new_fb)},
        )
    with closing(connect()) as conn, conn:
        for row in jobs:
            values = dict(zip(names, row, strict=True))
            for col in ("first_seen", "last_seen"):  # NOT NULL in ULTRON
                values[col] = values.get(col) or ""
            cols = list(values)
            cur = conn.execute(
                f"INSERT OR IGNORE INTO jobs ({', '.join(cols)}, profile_slug) "
                f"VALUES ({', '.join('?' * len(cols))}, '')",
                [values[c] for c in cols],
            )
            jobs_out["imported" if cur.rowcount else "skipped"] += 1
        for row in feedback:
            cur = conn.execute(
                "INSERT OR IGNORE INTO job_feedback (identity_key, relevance, reason_code, note, labeled_at) "
                "SELECT ?, ?, ?, ?, ? WHERE EXISTS (SELECT 1 FROM jobs WHERE identity_key = ?)",
                [*row, row[0]],
            )
            fb_out["imported" if cur.rowcount else "skipped"] += 1
    return jobs_out, fb_out


def import_mark3(source: Path, *, dry_run: bool = False) -> dict:
    """Import profiles, company seeds, and job history from a MARK III directory."""
    data_dir, db_path = _locate(Path(source))
    missing: list[str] = []
    result: dict = {"source": str(db_path), "dry_run": dry_run}
    with _snapshot(db_path) as src:
        result.update(_import_from(src, data_dir, dry_run, missing))
    result["missing_tables"] = missing
    return result


def _file_stamp(path: Path) -> tuple[int, ...] | None:
    try:
        stat = path.stat()
    except FileNotFoundError:
        return None
    return stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns


def _has_active_journal(path: Path) -> bool:
    # SQLite may leave a truncated or zeroed journal after a clean commit.
    try:
        with path.open("rb") as journal:
            return journal.read(1) not in (b"", b"\x00")
    except FileNotFoundError:
        return False


@contextmanager
def _snapshot(db_path: Path):
    """Read a stable copy without opening or creating SQLite sidecars at the original path."""
    sources = (db_path, db_path.with_name(db_path.name + "-wal"))
    journal = db_path.with_name(db_path.name + "-journal")
    message = "database changed during snapshot or has a rollback journal; retry when the database is idle"
    before = [_file_stamp(path) for path in sources]
    if before[0] is None or _has_active_journal(journal):
        raise sqlite3.OperationalError(message)
    with tempfile.TemporaryDirectory(prefix="ultron-mark3-", ignore_cleanup_errors=True) as tmp:
        folder = Path(tmp)
        try:
            for path, stamp in zip(sources, before, strict=True):
                if stamp is not None:
                    shutil.copyfile(path, folder / path.name)
        except FileNotFoundError as exc:
            raise sqlite3.OperationalError(message) from exc
        if before != [_file_stamp(path) for path in sources] or _has_active_journal(journal):
            raise sqlite3.OperationalError(message)
        # SQLite may rebuild SHM beside the private copy, with the database opened read-only.
        uri = (folder / db_path.name).as_uri()
        with closing(sqlite3.connect(f"{uri}?mode=ro", uri=True)) as conn:
            yield conn


def _import_from(src: sqlite3.Connection, data_dir: Path, dry_run: bool, missing: list[str]) -> dict:
    empty = {"imported": 0, "skipped": 0}
    result: dict = {}
    tables = {r[0] for r in src.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if "search_profiles" in tables:
        result["profiles"] = _import_profiles(src, dry_run, missing)
    else:
        missing.append("search_profiles")
        result["profiles"] = {**empty, "skipped_slugs": [], "invalid": [], "vocabulary_pack_added": []}
    result["seeds"] = _import_seeds(data_dir, dry_run)
    if "jobs" in tables:
        result["jobs"], result["feedback"] = _import_history(src, dry_run, missing)
    else:
        missing.append("jobs")
        result["jobs"], result["feedback"] = dict(empty), dict(empty)
    return result
