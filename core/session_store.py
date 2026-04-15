"""Session Store — external persistence layer for task event streams.

This implements the second structural split from Anthropic's approach:
pulling Session out of the Harness process and into an external,
storage-engine-agnostic layer.

Core philosophy:
- Session is the source of truth for the task. It lives outside any running process.
- Brain (Harness) can crash and be replaced — a new Brain just reads Session and continues.
- Execution unit can fail and be replaced — the failure is recorded in Session as an event.
- The storage engine underneath (SQLite / Redis / PostgreSQL) can be swapped without
  changing any other layer, as long as these 4 interfaces are maintained:

    wake(session_id)        — Wake a task for continuation
    get_session(session_id) — Retrieve full session (all events in order)
    get_events(session_id, range) — Query events selectively (for large sessions)
    emit_event(session_id, event) — Append a new event to the stream

Storage-engine implementations:
- SessionStore (default: in-memory + SQLite checkpoint)
- RedisSessionStore (Redis-backed, distributed)
- PostgresSessionStore (PostgreSQL-backed, strong consistency)
"""

from __future__ import annotations

import json
import time
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator

from core.types import AgentRole, EventType, ExecutionResult, ProvisionContext, SessionEvent


# ─── Abstract Base ────────────────────────────────────────────────────────────

class SessionStore(ABC):
    """Abstract Session Store with four stable interfaces.

    The underlying storage engine is injectable. All concrete implementations
    must maintain these four interfaces — they are the stable contract between
    Brain (Harness) and Session persistence.
    """

    @abstractmethod
    async def create_session(self, project_id: str, task_description: str) -> str:
        """Create a new Session and return its ID.

        Also emits SESSION_CREATED as the first event.
        """
        ...

    @abstractmethod
    async def wake(self, session_id: str) -> bool:
        """Wake a suspended session — resume scheduling.

        Returns True if the session was found and woken, False otherwise.
        The scheduler uses this to resume a paused task.
        """
        ...

    @abstractmethod
    async def get_session(self, session_id: str) -> Session | None:
        """Retrieve the full session including all events in order.

        Returns None if the session does not exist.
        For very long sessions, prefer get_events() to load a slice.
        """
        ...

    @abstractmethod
    async def get_events(
        self,
        session_id: str,
        start_index: int = 0,
        limit: int | None = None,
        event_types: list[EventType] | None = None,
    ) -> list[SessionEvent]:
        """Query events selectively.

        Args:
            session_id: The session to query.
            start_index: Start from this event index (0-based).
            limit: Maximum number of events to return. None = all remaining.
            event_types: Filter to specific event types. None = all types.

        Returns events in chronological order.
        """
        ...

    @abstractmethod
    async def emit_event(self, session_id: str, event: SessionEvent) -> None:
        """Append a new event to the session's event stream.

        This is the only write interface — every forward step in the task
        is recorded as an event. Events are append-only.
        """
        ...

    @abstractmethod
    async def suspend(self, session_id: str) -> None:
        """Suspend a session — stop scheduling but preserve state.

        A suspended session can be woken by calling wake(session_id).
        """
        ...

    @abstractmethod
    async def get_session_summary(self, session_id: str) -> dict[str, Any]:
        """Get a lightweight summary of a session (no event stream).

        Useful for listing sessions without loading all events.
        """
        ...

    @abstractmethod
    async def list_sessions(self, status: str | None = None) -> list[dict[str, Any]]:
        """List all sessions, optionally filtered by status."""
        ...


# ─── In-Memory + SQLite Implementation ───────────────────────────────────────

@dataclass
class Session:
    """An in-memory snapshot of a session with all its events."""
    session_id: str
    project_id: str
    task_description: str
    created_at: float
    updated_at: float
    status: str = "active"  # "active" | "suspended" | "completed" | "failed"
    events: list[SessionEvent] = field(default_factory=list)
    current_plan: dict[str, Any] | None = None  # Latest plan state

    @property
    def event_count(self) -> int:
        return len(self.events)

    def get_context_window(self, max_events: int = 50) -> list[SessionEvent]:
        """Return the most recent N events — suitable for loading into Context Window.

        This is the key distinction: Session stores everything,
        but Context Window only gets a recent slice.
        """
        return self.events[-max_events:]


class SQLiteSessionStore(SessionStore):
    """Default Session Store: in-memory for speed, SQLite for durability.

    - Active sessions are kept in memory for fast read/write.
    - Periodically checkpointed to SQLite for crash recovery.
    - Suitable for single-process deployments.
    """

    def __init__(self, db_path: str | Path | None = None):
        self.db_path = Path(db_path) if db_path else Path("output/.codeforge/sessions.db")
        self._memory: dict[str, Session] = {}
        self._pending_writes: list[tuple] = []  # (session_id, event_json)
        self._init_db()

    def _init_db(self):
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        import sqlite3
        conn = sqlite3.connect(str(self.db_path))
        conn.execute("""
            CREATE TABLE IF NOT EXISTS sessions (
                session_id TEXT PRIMARY KEY,
                project_id TEXT NOT NULL,
                task_description TEXT,
                created_at REAL,
                updated_at REAL,
                status TEXT DEFAULT 'active'
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS events (
                event_id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL,
                timestamp REAL,
                event_type TEXT,
                agent_role TEXT,
                payload TEXT,
                parent_id TEXT,
                success INTEGER,
                error_message TEXT,
                FOREIGN KEY (session_id) REFERENCES sessions(session_id)
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_events_session ON events(session_id, timestamp)")
        conn.commit()
        conn.close()

    async def create_session(self, project_id: str, task_description: str) -> str:
        session_id = f"{project_id}-session-{uuid.uuid4().hex[:8]}"
        now = time.time()
        session = Session(
            session_id=session_id,
            project_id=project_id,
            task_description=task_description,
            created_at=now,
            updated_at=now,
            status="active",
            events=[],
        )
        self._memory[session_id] = session

        # Persist session to DB first
        import sqlite3
        conn = sqlite3.connect(str(self.db_path))
        conn.execute(
            "INSERT INTO sessions (session_id, project_id, task_description, created_at, updated_at, status) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (session_id, project_id, task_description, now, now, "active"),
        )
        conn.commit()
        conn.close()

        # Then emit the first event
        created_event = SessionEvent(
            event_type=EventType.SESSION_CREATED,
            payload={"project_id": project_id, "task_description": task_description},
        )
        await self._save_event_to_db(session_id, created_event)

        return session_id

    async def wake(self, session_id: str) -> bool:
        if session_id in self._memory:
            self._memory[session_id].status = "active"
            return True
        # Try loading from DB
        session = await self._load_from_db(session_id)
        if session:
            session.status = "active"
            self._memory[session_id] = session
            return True
        return False

    async def get_session(self, session_id: str) -> Session | None:
        if session_id in self._memory:
            return self._memory[session_id]
        return await self._load_from_db(session_id)

    async def get_events(
        self,
        session_id: str,
        start_index: int = 0,
        limit: int | None = None,
        event_types: list[EventType] | None = None,
    ) -> list[SessionEvent]:
        session = await self.get_session(session_id)
        if not session:
            return []

        events = session.events[start_index:]
        if event_types:
            type_names = {e.name for e in event_types}
            events = [e for e in events if e.event_type.name in type_names]
        if limit is not None:
            events = events[:limit]
        return events

    async def emit_event(self, session_id: str, event: SessionEvent) -> None:
        if session_id not in self._memory:
            return

        session = self._memory[session_id]
        session.events.append(event)
        session.updated_at = time.time()

        # Update status based on event type
        if event.event_type == EventType.TASK_COMPLETED:
            session.status = "completed"
        elif event.event_type == EventType.TASK_FAILED:
            session.status = "failed"

        await self._save_event_to_db(session_id, event)

    async def suspend(self, session_id: str) -> None:
        if session_id in self._memory:
            self._memory[session_id].status = "suspended"
            import sqlite3
            conn = sqlite3.connect(str(self.db_path))
            conn.execute(
                "UPDATE sessions SET status = 'suspended', updated_at = ? WHERE session_id = ?",
                (time.time(), session_id),
            )
            conn.commit()
            conn.close()

    async def get_session_summary(self, session_id: str) -> dict[str, Any]:
        session = await self.get_session(session_id)
        if not session:
            return {}
        return {
            "session_id": session.session_id,
            "project_id": session.project_id,
            "task_description": session.task_description,
            "status": session.status,
            "event_count": session.event_count,
            "created_at": session.created_at,
            "updated_at": session.updated_at,
        }

    async def list_sessions(self, status: str | None = None) -> list[dict[str, Any]]:
        import sqlite3
        conn = sqlite3.connect(str(self.db_path))
        if status:
            rows = conn.execute(
                "SELECT session_id, project_id, task_description, status, created_at, updated_at "
                "FROM sessions WHERE status = ? ORDER BY updated_at DESC",
                (status,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT session_id, project_id, task_description, status, created_at, updated_at "
                "FROM sessions ORDER BY updated_at DESC",
            ).fetchall()
        conn.close()
        return [
            {
                "session_id": r[0],
                "project_id": r[1],
                "task_description": r[2],
                "status": r[3],
                "created_at": r[4],
                "updated_at": r[5],
            }
            for r in rows
        ]

    async def _load_from_db(self, session_id: str) -> Session | None:
        import sqlite3
        conn = sqlite3.connect(str(self.db_path))
        row = conn.execute(
            "SELECT session_id, project_id, task_description, created_at, updated_at, status "
            "FROM sessions WHERE session_id = ?",
            (session_id,),
        ).fetchone()
        if not row:
            conn.close()
            return None

        event_rows = conn.execute(
            "SELECT event_id, timestamp, event_type, agent_role, payload, parent_id, success, error_message "
            "FROM events WHERE session_id = ? ORDER BY timestamp",
            (session_id,),
        ).fetchall()
        conn.close()

        events = []
        for er in event_rows:
            events.append(SessionEvent(
                event_id=er[0],
                timestamp=er[1],
                event_type=EventType[er[2]] if er[2] else EventType.BRAIN_STOPPED,
                agent_role=AgentRole(er[3]) if er[3] else None,
                payload=json.loads(er[4]) if er[4] else {},
                parent_id=er[5],
                success=bool(er[6]),
                error_message=er[7],
            ))

        return Session(
            session_id=row[0],
            project_id=row[1],
            task_description=row[2],
            created_at=row[3],
            updated_at=row[4],
            status=row[5],
            events=events,
        )

    async def _save_event_to_db(self, session_id: str, event: SessionEvent):
        import sqlite3
        conn = sqlite3.connect(str(self.db_path))
        conn.execute(
            "UPDATE sessions SET updated_at = ? WHERE session_id = ?",
            (time.time(), session_id),
        )
        conn.execute(
            "INSERT INTO events (event_id, session_id, timestamp, event_type, agent_role, payload, parent_id, success, error_message) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                event.event_id,
                session_id,
                event.timestamp,
                event.event_type.name,
                event.agent_role.value if event.agent_role else None,
                json.dumps(event.payload),
                event.parent_id,
                int(event.success),
                event.error_message,
            ),
        )
        conn.commit()
        conn.close()
