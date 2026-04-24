"""
database.py — SQLite layer for Kebun Aira Bot (combined)

Tables:
  tasks              — assigned work items with full update history
  task_updates       — progressive updates per task
  finance_entries    — auto-detected + manual financial transactions
  messages           — stored group messages (for weekly recap context)
  weekly_state       — per-chat per-week plan config (budget, situasi, adjusted tasks)
  plan_task_status   — per-chat per-week task completion (selesai/lewati)
  bot_config         — key-value store (e.g. primary_chat_id for reminders)
"""

import sqlite3
from contextlib import contextmanager
from datetime import datetime


class Database:
    def __init__(self, db_path: str = "bot_data.db"):
        self.db_path = db_path
        self._init_db()

    @contextmanager
    def _conn(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _init_db(self):
        with self._conn() as conn:
            conn.executescript("""
                -- ── Task management ────────────────────────────────────────
                CREATE TABLE IF NOT EXISTS tasks (
                    id            INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_id       INTEGER NOT NULL,
                    assignee      TEXT    NOT NULL,
                    description   TEXT    NOT NULL,
                    assigned_by   TEXT    NOT NULL,
                    created_at    TEXT    DEFAULT (datetime('now')),
                    last_reminded TEXT,
                    completed_at  TEXT,
                    status        TEXT    DEFAULT 'pending'
                );

                CREATE TABLE IF NOT EXISTS task_updates (
                    id         INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id    INTEGER NOT NULL REFERENCES tasks(id),
                    username   TEXT    NOT NULL,
                    message    TEXT    NOT NULL,
                    created_at TEXT    DEFAULT (datetime('now'))
                );

                -- ── Finance ─────────────────────────────────────────────────
                CREATE TABLE IF NOT EXISTS finance_entries (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_id     INTEGER NOT NULL,
                    amount      REAL    NOT NULL,
                    type        TEXT    NOT NULL,
                    description TEXT,
                    recorded_by TEXT,
                    created_at  TEXT    DEFAULT (datetime('now'))
                );

                -- ── Message archive (for recap context) ─────────────────────
                CREATE TABLE IF NOT EXISTS messages (
                    id       INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_id  INTEGER NOT NULL,
                    username TEXT,
                    content  TEXT    NOT NULL,
                    sent_at  TEXT    DEFAULT (datetime('now'))
                );

                -- ── Weekly farm plan state ───────────────────────────────────
                -- One row per (chat_id, week_key). week_key = 'YYYY-Www'
                CREATE TABLE IF NOT EXISTS weekly_state (
                    id             INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_id        INTEGER NOT NULL,
                    week_key       TEXT    NOT NULL,
                    budget         INTEGER,
                    situasi        TEXT,
                    adjusted_tasks TEXT,   -- JSON array
                    updated_at     TEXT    DEFAULT (datetime('now')),
                    UNIQUE(chat_id, week_key)
                );

                -- ── Plan task status (selesai / lewati) ──────────────────────
                CREATE TABLE IF NOT EXISTS plan_task_status (
                    id         INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_id    INTEGER NOT NULL,
                    week_key   TEXT    NOT NULL,
                    task_num   INTEGER NOT NULL,
                    status     TEXT    NOT NULL,   -- 'selesai' | 'lewati'
                    updated_at TEXT    DEFAULT (datetime('now')),
                    UNIQUE(chat_id, week_key, task_num)
                );

                -- ── Bot config key-value ─────────────────────────────────────
                CREATE TABLE IF NOT EXISTS bot_config (
                    key   TEXT PRIMARY KEY,
                    value TEXT
                );

                -- ── Indexes ──────────────────────────────────────────────────
                CREATE INDEX IF NOT EXISTS idx_tasks_chat     ON tasks(chat_id, status);
                CREATE INDEX IF NOT EXISTS idx_finance_chat   ON finance_entries(chat_id, created_at);
                CREATE INDEX IF NOT EXISTS idx_messages_chat  ON messages(chat_id, sent_at);
                CREATE INDEX IF NOT EXISTS idx_updates_task   ON task_updates(task_id);
                CREATE INDEX IF NOT EXISTS idx_weekly_chat    ON weekly_state(chat_id, week_key);
                CREATE INDEX IF NOT EXISTS idx_planstatus     ON plan_task_status(chat_id, week_key);
            """)

    # ── Helpers ────────────────────────────────────────────────────────────────

    @staticmethod
    def week_key(d: datetime = None) -> str:
        return (d or datetime.now()).strftime("%Y-W%W")

    # ── Task management ────────────────────────────────────────────────────────

    def add_task(self, chat_id: int, assignee: str, description: str, assigned_by: str) -> int:
        with self._conn() as conn:
            cur = conn.execute(
                "INSERT INTO tasks (chat_id, assignee, description, assigned_by) VALUES (?,?,?,?)",
                (chat_id, assignee, description, assigned_by),
            )
            return cur.lastrowid

    def get_all_pending_tasks(self, chat_id: int) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM tasks WHERE chat_id=? AND status='pending' ORDER BY created_at",
                (chat_id,),
            ).fetchall()
            return [dict(r) for r in rows]

    def get_pending_tasks_for_user(self, chat_id: int, username: str) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM tasks WHERE chat_id=? AND assignee=? AND status='pending'",
                (chat_id, username),
            ).fetchall()
            return [dict(r) for r in rows]

    def get_completed_tasks(self, chat_id: int, days: int = 7) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM tasks WHERE chat_id=? AND status='done' "
                "AND datetime(completed_at) >= datetime('now', ?)",
                (chat_id, f"-{days} days"),
            ).fetchall()
            return [dict(r) for r in rows]

    def complete_task(self, task_id: int) -> dict | None:
        with self._conn() as conn:
            conn.execute(
                "UPDATE tasks SET status='done', completed_at=datetime('now') "
                "WHERE id=? AND status='pending'",
                (task_id,),
            )
            row = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
            return dict(row) if row else None

    def get_tasks_needing_reminder(self) -> list[dict]:
        """Pending tasks not reminded in the last 2 days."""
        with self._conn() as conn:
            rows = conn.execute("""
                SELECT * FROM tasks
                WHERE status = 'pending'
                  AND (
                      (last_reminded IS NULL     AND datetime(created_at)    <= datetime('now', '-2 days'))
                   OR (last_reminded IS NOT NULL AND datetime(last_reminded) <= datetime('now', '-2 days'))
                  )
            """).fetchall()
            return [dict(r) for r in rows]

    def update_last_reminded(self, task_id: int):
        with self._conn() as conn:
            conn.execute(
                "UPDATE tasks SET last_reminded=datetime('now') WHERE id=?",
                (task_id,),
            )

    def add_task_update(self, task_id: int, username: str, message: str) -> bool:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT id FROM tasks WHERE id=? AND status='pending'", (task_id,)
            ).fetchone()
            if not row:
                return False
            conn.execute(
                "INSERT INTO task_updates (task_id, username, message) VALUES (?,?,?)",
                (task_id, username, message),
            )
            return True

    def get_task_updates(self, task_id: int) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM task_updates WHERE task_id=? ORDER BY created_at",
                (task_id,),
            ).fetchall()
            return [dict(r) for r in rows]

    def get_task_by_id(self, task_id: int) -> dict | None:
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
            return dict(row) if row else None

    def get_all_active_chat_ids(self) -> list[int]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT DISTINCT chat_id FROM messages "
                "WHERE datetime(sent_at) >= datetime('now', '-30 days')"
            ).fetchall()
            return [r["chat_id"] for r in rows]

    # ── Finance ────────────────────────────────────────────────────────────────

    def add_finance_entry(self, chat_id: int, amount: float, type_: str, description: str, recorded_by: str):
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO finance_entries (chat_id, amount, type, description, recorded_by) "
                "VALUES (?,?,?,?,?)",
                (chat_id, amount, type_, description, recorded_by),
            )

    def get_finance_entries(self, chat_id: int, days: int = 7) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM finance_entries WHERE chat_id=? "
                "AND datetime(created_at) >= datetime('now', ?) ORDER BY created_at",
                (chat_id, f"-{days} days"),
            ).fetchall()
            return [dict(r) for r in rows]

    # ── Messages ───────────────────────────────────────────────────────────────

    def store_message(self, chat_id: int, username: str, content: str, sent_at):
        iso = sent_at.isoformat() if hasattr(sent_at, "isoformat") else str(sent_at)
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO messages (chat_id, username, content, sent_at) VALUES (?,?,?,?)",
                (chat_id, username, content, iso),
            )

    def get_messages(self, chat_id: int, days: int = 7) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM messages WHERE chat_id=? "
                "AND datetime(sent_at) >= datetime('now', ?) ORDER BY sent_at",
                (chat_id, f"-{days} days"),
            ).fetchall()
            return [dict(r) for r in rows]

    # ── Weekly plan state ──────────────────────────────────────────────────────

    def set_weekly_budget(self, chat_id: int, budget: int, week: str = None):
        wk = week or self.week_key()
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO weekly_state (chat_id, week_key, budget, updated_at) VALUES (?,?,?,datetime('now')) "
                "ON CONFLICT(chat_id, week_key) DO UPDATE SET budget=excluded.budget, updated_at=datetime('now')",
                (chat_id, wk, budget),
            )

    def set_weekly_situasi(self, chat_id: int, situasi: str, week: str = None):
        wk = week or self.week_key()
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO weekly_state (chat_id, week_key, situasi, updated_at) VALUES (?,?,?,datetime('now')) "
                "ON CONFLICT(chat_id, week_key) DO UPDATE SET situasi=excluded.situasi, updated_at=datetime('now')",
                (chat_id, wk, situasi),
            )

    def set_adjusted_tasks(self, chat_id: int, tasks_json: str, week: str = None):
        wk = week or self.week_key()
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO weekly_state (chat_id, week_key, adjusted_tasks, updated_at) VALUES (?,?,?,datetime('now')) "
                "ON CONFLICT(chat_id, week_key) DO UPDATE SET adjusted_tasks=excluded.adjusted_tasks, updated_at=datetime('now')",
                (chat_id, wk, tasks_json),
            )

    def get_weekly_state(self, chat_id: int, week: str = None) -> dict:
        wk = week or self.week_key()
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM weekly_state WHERE chat_id=? AND week_key=?", (chat_id, wk)
            ).fetchone()
            return dict(row) if row else {}

    def reset_weekly_state(self, chat_id: int, week: str = None):
        wk = week or self.week_key()
        with self._conn() as conn:
            conn.execute("DELETE FROM weekly_state WHERE chat_id=? AND week_key=?", (chat_id, wk))
            conn.execute("DELETE FROM plan_task_status WHERE chat_id=? AND week_key=?", (chat_id, wk))

    # ── Plan task status ───────────────────────────────────────────────────────

    def set_plan_task_status(self, chat_id: int, task_num: int, status: str, week: str = None):
        wk = week or self.week_key()
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO plan_task_status (chat_id, week_key, task_num, status, updated_at) "
                "VALUES (?,?,?,?,datetime('now')) "
                "ON CONFLICT(chat_id, week_key, task_num) DO UPDATE SET status=excluded.status, updated_at=datetime('now')",
                (chat_id, wk, task_num, status),
            )

    def get_plan_task_statuses(self, chat_id: int, week: str = None) -> dict[int, str]:
        wk = week or self.week_key()
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT task_num, status FROM plan_task_status WHERE chat_id=? AND week_key=?",
                (chat_id, wk),
            ).fetchall()
            return {r["task_num"]: r["status"] for r in rows}

    # ── Bot config ─────────────────────────────────────────────────────────────

    def set_config(self, key: str, value: str):
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO bot_config (key, value) VALUES (?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            )

    def get_config(self, key: str, default: str = None) -> str | None:
        with self._conn() as conn:
            row = conn.execute("SELECT value FROM bot_config WHERE key=?", (key,)).fetchone()
            return row["value"] if row else default
