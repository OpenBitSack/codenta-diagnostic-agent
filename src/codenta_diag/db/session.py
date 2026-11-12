from __future__ import annotations

from contextlib import contextmanager
from typing import Any, Iterator

from sqlalchemy import create_engine, event, select, update
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from .models import AuditLogRow, Base, CacheEntryRow, CounterRow


class Database:
    def __init__(self, url: str) -> None:
        self.url = url
        kwargs: dict[str, Any] = {"future": True}
        if url.startswith("sqlite"):
            kwargs["connect_args"] = {"check_same_thread": False, "timeout": 30}
        else:
            kwargs.update(pool_size=10, max_overflow=20, pool_pre_ping=True)
        self.engine: Engine = create_engine(url, **kwargs)
        if url.startswith("sqlite"):
            @event.listens_for(self.engine, "connect")
            def _pragmas(conn: Any, _: Any) -> None:  # pragma: no cover - trivial
                cur = conn.cursor()
                if ":memory:" not in url:
                    cur.execute("PRAGMA journal_mode=WAL")
                cur.execute("PRAGMA busy_timeout=30000")
                cur.execute("PRAGMA foreign_keys=ON")
                cur.close()
        self.Session = sessionmaker(self.engine, expire_on_commit=False)

    def create_all(self) -> None:
        Base.metadata.create_all(self.engine)

    def drop_all(self) -> None:
        Base.metadata.drop_all(self.engine)

    @contextmanager
    def session(self) -> Iterator[Session]:
        s = self.Session()
        try:
            yield s
            s.commit()
        except Exception:
            s.rollback()
            raise
        finally:
            s.close()

    # ---- small shared helpers
    def audit(self, actor: str, action: str, investigation_id: str | None = None,
              details: dict[str, Any] | None = None) -> None:
        with self.session() as s:
            s.add(AuditLogRow(actor=actor, action=action, investigation_id=investigation_id, details=details or {}))

    def incr(self, name: str, by: int = 1) -> None:
        for _ in range(3):
            try:
                with self.session() as s:
                    res = s.execute(update(CounterRow).where(CounterRow.name == name)
                                    .values(value=CounterRow.value + by))
                    if res.rowcount == 0:
                        s.add(CounterRow(name=name, value=by))
                return
            except IntegrityError:
                continue

    def counter(self, name: str) -> int:
        with self.session() as s:
            row = s.get(CounterRow, name)
            return row.value if row else 0


class DbCache:
    """ResultCache backed by the database so cached replays/evaluations survive worker restarts."""

    def __init__(self, db: Database) -> None:
        self.db = db
        self._local: dict[str, dict[str, Any]] = {}

    def get(self, key: str) -> dict[str, Any] | None:
        if key in self._local:
            self.db.incr("cache_hits")
            return self._local[key]
        with self.db.session() as s:
            row = s.execute(select(CacheEntryRow).where(CacheEntryRow.key == key)).scalar_one_or_none()
            if row is None:
                return None
            self._local[key] = row.value
        self.db.incr("cache_hits")
        return self._local[key]

    def put(self, key: str, kind: str, value: dict[str, Any]) -> None:
        self._local[key] = value
        try:
            with self.db.session() as s:
                s.add(CacheEntryRow(key=key, kind=kind, value=value))
        except IntegrityError:
            pass  # another worker cached the same deterministic result
