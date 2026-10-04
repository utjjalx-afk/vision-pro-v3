"""Append-only local repository interface and transactional SQLite implementation."""

import json
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from threading import RLock
from typing import Protocol

from vision.core.codec import utc_string
from vision.core.contracts import _identifier, _utc
from vision.core.instruments import digest

KINDS = frozenset(
    {
        "experiment",
        "signal",
        "intent",
        "paper",
        "exit_declaration",
        "trade_outcome",
        "failure",
        "prospective_registration",
        "directional_grade",
        "backtest",
        "backtest_suite",
        "strategy_artifact",
        "strategy_lifecycle",
        "strategy_result",
    }
)
GENESIS = digest({"journal": "vision-research-v1"})


def canonical(value):
    result = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
    if len(result.encode()) > 5000000:
        raise ValueError("Journal payload exceeds 5 MB")
    return result


@dataclass(frozen=True, slots=True)
class Pending:
    kind: str
    experiment_id: str
    key: str
    payload_json: str

    def __post_init__(self):
        for value in (self.experiment_id, self.key):
            _identifier(value)
        if self.kind not in KINDS or not isinstance(self.payload_json, str):
            raise ValueError("Known typed journal record required")
        if canonical(json.loads(self.payload_json)) != self.payload_json:
            raise ValueError("Canonical JSON required")

    @property
    def entry_id(self):
        return digest({"kind": self.kind, "experiment": self.experiment_id, "key": self.key})


@dataclass(frozen=True, slots=True)
class Entry:
    sequence: int
    entry_id: str
    experiment_id: str
    kind: str
    key: str
    recorded_at: datetime
    payload_json: str
    previous_hash: str
    entry_hash: str

    def body(self):
        return {
            "sequence": self.sequence,
            "entry_id": self.entry_id,
            "experiment_id": self.experiment_id,
            "kind": self.kind,
            "key": self.key,
            "recorded_at": _utc(self.recorded_at),
            "payload_json": self.payload_json,
            "previous_hash": self.previous_hash,
        }

    def to_dict(self):
        return {**self.body(), "entry_hash": self.entry_hash}

    @property
    def payload(self):
        # Return a copy: callers cannot mutate committed state.
        return json.loads(self.payload_json)


def verify(entries):
    if type(entries) is not tuple or len(entries) > 10000:
        raise ValueError("Bounded immutable journal required")
    previous, at, ids = GENESIS, None, set()
    for index, entry in enumerate(entries, 1):
        if not isinstance(entry, Entry):
            raise ValueError("Entry required")
        pending = Pending(entry.kind, entry.experiment_id, entry.key, entry.payload_json)
        if (
            entry.sequence != index
            or entry.entry_id != pending.entry_id
            or entry.entry_id in ids
            or entry.previous_hash != previous
            or entry.entry_hash != digest(entry.body())
            or at is not None
            and entry.recorded_at < at
        ):
            raise ValueError("Research journal chain/order mismatch")
        _utc(entry.recorded_at)
        previous, at = entry.entry_hash, entry.recorded_at
        ids.add(entry.entry_id)
    return previous


class Repository(Protocol):
    def entries(self) -> tuple[Entry, ...]: ...
    def transact(self, at: datetime, build: Callable[[tuple[Entry, ...]], Pending]) -> Entry: ...


class SQLiteRepository:
    """Each write validates under BEGIN IMMEDIATE; no UPDATE/DELETE application API."""

    def __init__(self, path, *, readonly=False):
        self.path = Path(path)
        self.readonly = readonly
        if type(readonly) is not bool:
            raise ValueError("Explicit readonly policy required")
        if readonly and not self.path.is_file():
            raise ValueError("Existing SQLite journal required")
        if not readonly:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = RLock()
        target = self.path.resolve().as_uri() + "?mode=ro" if readonly else self.path
        self._connection = sqlite3.connect(
            target, timeout=10, isolation_level=None, check_same_thread=False, uri=readonly
        )
        version = self._connection.execute("PRAGMA user_version").fetchone()[0]
        if version not in {0, 1}:
            self.close()
            raise ValueError("Unsupported journal database version")
        if readonly:
            if version != 1:
                self.close()
                raise ValueError("Existing versioned journal required")
            self.entries()
            return
        self._connection.execute("PRAGMA journal_mode=WAL")
        self._connection.execute("PRAGMA synchronous=FULL")
        self._connection.executescript("""
            CREATE TABLE IF NOT EXISTS research_journal (
                sequence INTEGER PRIMARY KEY, entry_id TEXT NOT NULL UNIQUE,
                experiment_id TEXT NOT NULL, kind TEXT NOT NULL, logical_key TEXT NOT NULL,
                recorded_at TEXT NOT NULL, payload_json TEXT NOT NULL,
                previous_hash TEXT NOT NULL, entry_hash TEXT NOT NULL);
            CREATE TRIGGER IF NOT EXISTS journal_no_update BEFORE UPDATE ON research_journal
                BEGIN SELECT RAISE(ABORT, 'research journal is append-only'); END;
            CREATE TRIGGER IF NOT EXISTS journal_no_delete BEFORE DELETE ON research_journal
                BEGIN SELECT RAISE(ABORT, 'research journal is append-only'); END;
            PRAGMA user_version=1;
        """)
        self.entries()

    def _read(self):
        cursor = self._connection.execute(
            "SELECT * FROM research_journal ORDER BY sequence LIMIT 10001"
        )
        entries = tuple(
            Entry(
                row[0], row[1], row[2], row[3], row[4], utc_string(row[5]), row[6], row[7], row[8]
            )
            for row in cursor
        )
        verify(entries)
        return entries

    def entries(self):
        with self._lock:
            return self._read()

    def transact(self, at, build):
        if self.readonly:
            raise ValueError("Readonly journal cannot append")
        _utc(at)
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                entries = self._read()
                pending = build(entries)
                if not isinstance(pending, Pending):
                    raise ValueError("Typed pending record required")
                previous_entry = next((e for e in entries if e.entry_id == pending.entry_id), None)
                if previous_entry is not None:
                    if previous_entry.payload_json != pending.payload_json:
                        raise ValueError("Journal identity collision")
                    self._connection.execute("COMMIT")
                    return previous_entry
                if len(entries) >= 10000 or entries and at < entries[-1].recorded_at:
                    raise ValueError("Journal capacity/clock regression")
                previous = entries[-1].entry_hash if entries else GENESIS
                body = {
                    "sequence": len(entries) + 1,
                    "entry_id": pending.entry_id,
                    "experiment_id": pending.experiment_id,
                    "kind": pending.kind,
                    "key": pending.key,
                    "recorded_at": _utc(at),
                    "payload_json": pending.payload_json,
                    "previous_hash": previous,
                }
                entry = Entry(
                    len(entries) + 1,
                    pending.entry_id,
                    pending.experiment_id,
                    pending.kind,
                    pending.key,
                    at,
                    pending.payload_json,
                    previous,
                    digest(body),
                )
                self._connection.execute(
                    "INSERT INTO research_journal VALUES (?,?,?,?,?,?,?,?,?)",
                    (*tuple(entry.body().values()), entry.entry_hash),
                )
                self._connection.execute("COMMIT")
                return entry
            except BaseException:
                if self._connection.in_transaction:
                    self._connection.execute("ROLLBACK")
                raise

    def close(self):
        with self._lock:
            self._connection.close()


def export(repository):
    entries = repository.entries()
    result = {"version": 1, "entries": [e.to_dict() for e in entries], "head": verify(entries)}
    if len(json.dumps(result).encode()) > 50000000:
        raise ValueError("Journal export exceeds 50 MB")
    return result


def entries_from_export(value):
    if (
        not isinstance(value, dict)
        or set(value) != {"version", "entries", "head"}
        or type(value["version"]) is not int
        or value["version"] != 1
        or not isinstance(value["entries"], list)
        or len(value["entries"]) > 10000
    ):
        raise ValueError("Strict versioned journal export required")
    fields = {
        "sequence",
        "entry_id",
        "experiment_id",
        "kind",
        "key",
        "recorded_at",
        "payload_json",
        "previous_hash",
        "entry_hash",
    }
    entries = []
    for row in value["entries"]:
        if not isinstance(row, dict) or set(row) != fields or type(row["sequence"]) is not int:
            raise ValueError("Strict journal entry shape required")
        row = dict(row)
        row["recorded_at"] = utc_string(row["recorded_at"])
        entries.append(Entry(**row))
    entries = tuple(entries)
    if verify(entries) != value["head"]:
        raise ValueError("Export head mismatch")
    return entries
