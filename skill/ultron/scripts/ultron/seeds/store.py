"""Writing verified seeds to the auto-seed file under a cross-process lock."""

from __future__ import annotations

import os
import shutil
import tempfile
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

import yaml

from ultron.core.ats.identity import seed_identity
from ultron.core.models import AtsBoardSeed
from ultron.seeds.inventory import auto_seeds_path, is_unconfirmed, make_seed_id, parse_rows


class SeedFileError(RuntimeError):
    """auto_seeds.yaml exists but cannot be read safely."""


def _read_raw_rows(path: Path) -> list:
    if not path.is_file():
        return []
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        rows = data.get("seeds", []) if isinstance(data, dict) else None
        if not isinstance(rows, list):
            raise TypeError("seeds is not a list")
    except (OSError, TypeError, ValueError, yaml.YAMLError) as exc:
        backup = path.with_name(f"{path.name}.corrupt-{datetime.now(UTC):%Y%m%dT%H%M%S%f}")
        try:
            shutil.copy2(path, backup)
        except OSError:
            backup = path
        raise SeedFileError(f"{path.name} is unreadable ({type(exc).__name__}); copy kept at {backup}") from exc
    return rows


# Writers hold an OS lock on ``auto_seeds.yaml.lock`` across read-modify-write. The operating system releases it
# when the holder exits or crashes, so there is no stale-lock recovery that could delete another writer's lock;
# the lock file itself is left in place. Waiting longer than SEED_LOCK_TIMEOUT raises SeedFileError.
SEED_LOCK_TIMEOUT = 15.0


def _try_lock(fd: int) -> bool:
    os.lseek(fd, 0, os.SEEK_SET)
    try:
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return False
    return True


def _unlock(fd: int) -> None:
    os.lseek(fd, 0, os.SEEK_SET)
    if os.name == "nt":
        import msvcrt

        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        fcntl.flock(fd, fcntl.LOCK_UN)


@contextmanager
def _seed_file_lock(path: Path) -> Iterator[None]:
    lock = path.with_name(path.name + ".lock")
    lock.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(lock, os.O_CREAT | os.O_RDWR)
    try:
        deadline = time.monotonic() + SEED_LOCK_TIMEOUT
        while not _try_lock(fd):
            if time.monotonic() >= deadline:
                raise SeedFileError(f"{path.name} is locked by another ULTRON process ({lock.name})")
            time.sleep(0.05)
        try:
            yield
        finally:
            _unlock(fd)
    finally:
        os.close(fd)


def add_seeds(seeds: list[AtsBoardSeed], *, profile_slug: str) -> int:
    """Persist seeds to ``auto_seeds.yaml`` atomically; return how many are new.

    The read-modify-write runs under a lock file, so concurrent writers (two runs of different profiles)
    never drop each other's rows. Raises ``SeedFileError`` (file untouched, backup copied) if the existing
    file is unparseable or the lock cannot be taken. Rows that fail validation are preserved verbatim on
    rewrite.
    """
    path = auto_seeds_path()
    with _seed_file_lock(path):
        return _add_seeds_locked(path, seeds, profile_slug)


def _add_seeds_locked(path: Path, seeds: list[AtsBoardSeed], profile_slug: str) -> int:
    out_rows: list = []
    index: dict[tuple[str, str], AtsBoardSeed] = {}
    for raw in _read_raw_rows(path):
        parsed = parse_rows([raw])
        if parsed:
            index[seed_identity(parsed[0].ats, parsed[0].board_identifier)] = parsed[0]
            out_rows.append(parsed[0])
        else:
            out_rows.append(raw)
    new = 0
    for seed in seeds:
        key = seed_identity(seed.ats, seed.board_identifier)
        current = index.get(key)
        if current is None:
            fresh = seed.model_copy(update={
                "seed_id": make_seed_id(seed.ats, seed.board_identifier),
                "profiles": list(dict.fromkeys([*seed.profiles, profile_slug])),
            })
            index[key] = fresh
            out_rows.append(fresh)
            new += 1
        elif is_unconfirmed(current) and not is_unconfirmed(seed):
            # A guessed board stored without proof of ownership, now verified: take the verified metadata and
            # keep every profile link.
            promoted = seed.model_copy(update={
                "seed_id": make_seed_id(seed.ats, seed.board_identifier),
                "profiles": list(dict.fromkeys([*current.profiles, *seed.profiles, profile_slug])),
            })
            index[key] = promoted
            out_rows[out_rows.index(current)] = promoted
        elif profile_slug not in current.profiles:
            current.profiles.append(profile_slug)
    text = yaml.safe_dump(
        {"seeds": [r.model_dump(mode="json") if isinstance(r, AtsBoardSeed) else r for r in out_rows]},
        allow_unicode=True, sort_keys=False,
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return new


