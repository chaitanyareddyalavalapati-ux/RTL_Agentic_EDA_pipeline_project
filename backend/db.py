import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from backend.config import WORKSPACE_ROOT

DB_PATH = Path(__file__).resolve().parent.parent / "eda_pipeline.db"


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


@contextmanager
def _conn():
    """Commit/rollback like sqlite3's own context manager, but also close.

    `with sqlite3.connect(...)` manages only the transaction, never the
    connection, so every call here used to leak a connection and its file
    descriptors for the lifetime of the process.
    """
    conn = _connect()
    try:
        yield conn
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    WORKSPACE_ROOT.mkdir(parents=True, exist_ok=True)
    with _conn() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS jobs (
                job_id TEXT PRIMARY KEY,
                prompt TEXT NOT NULL,
                status TEXT NOT NULL,
                plan_json TEXT NOT NULL DEFAULT '{}',
                report_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS steps (
                job_id TEXT NOT NULL,
                name TEXT NOT NULL,
                status TEXT NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0,
                started_at TEXT,
                ended_at TEXT,
                output TEXT NOT NULL DEFAULT '',
                metrics_json TEXT NOT NULL DEFAULT '{}',
                error TEXT,
                PRIMARY KEY (job_id, name),
                FOREIGN KEY (job_id) REFERENCES jobs(job_id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS artifacts (
                job_id TEXT NOT NULL,
                path TEXT NOT NULL,
                file_path TEXT NOT NULL,
                PRIMARY KEY (job_id, path),
                FOREIGN KEY (job_id) REFERENCES jobs(job_id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS repair_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id TEXT NOT NULL,
                stage TEXT,
                attempt INTEGER,
                diagnosis_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL,
                FOREIGN KEY (job_id) REFERENCES jobs(job_id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id TEXT NOT NULL,
                event_type TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY (job_id) REFERENCES jobs(job_id) ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS idx_events_job_id ON events(job_id, id);
            CREATE INDEX IF NOT EXISTS idx_jobs_updated_at ON jobs(updated_at DESC);
            """
        )


def recover_interrupted_jobs() -> int:
    """Mark jobs left active by a stopped single-process app as interrupted."""
    now = datetime.now(timezone.utc).isoformat()
    message = 'Job was interrupted because the application stopped before the flow completed.'
    recovered = 0
    with _conn() as conn:
        jobs = conn.execute(
            "SELECT job_id FROM jobs WHERE status IN ('created', 'planning', 'running')"
        ).fetchall()
        for row in jobs:
            job_id = row['job_id']
            report = {
                'status': 'failed',
                'interrupted': True,
                'reason': message,
            }
            conn.execute(
                "UPDATE jobs SET status='failed', report_json=?, updated_at=? WHERE job_id=?",
                (json.dumps(report), now, job_id),
            )
            conn.execute(
                "UPDATE steps SET status='failed', ended_at=?, error=? "
                "WHERE job_id=? AND status='running'",
                (now, message, job_id),
            )
            event = {
                'type': 'pipeline_failed',
                'job_id': job_id,
                'timestamp': now,
                'error': message,
                'report': report,
            }
            cursor = conn.execute(
                "INSERT INTO events(job_id,event_type,payload_json,created_at) VALUES (?,?,?,?)",
                (job_id, 'pipeline_failed', json.dumps(event), now),
            )
            event['event_id'] = cursor.lastrowid
            conn.execute(
                "UPDATE events SET payload_json=? WHERE id=?",
                (json.dumps(event), cursor.lastrowid),
            )
            recovered += 1
    return recovered


def upsert_job(job_id: str, prompt: str, status: str, plan: dict, report: dict, now: str) -> None:
    with _conn() as conn:
        conn.execute(
            """
            INSERT INTO jobs(job_id, prompt, status, plan_json, report_json, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(job_id) DO UPDATE SET
                prompt=excluded.prompt,
                status=excluded.status,
                plan_json=excluded.plan_json,
                report_json=excluded.report_json,
                updated_at=excluded.updated_at
            """,
            (job_id, prompt, status, json.dumps(plan), json.dumps(report), now, now),
        )


def save_snapshot(snapshot: dict, now: str) -> None:
    job_id = snapshot["job_id"]
    with _conn() as conn:
        conn.execute(
            """
            INSERT INTO jobs(job_id, prompt, status, plan_json, report_json, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(job_id) DO UPDATE SET
                prompt=excluded.prompt,
                status=excluded.status,
                plan_json=excluded.plan_json,
                report_json=excluded.report_json,
                updated_at=excluded.updated_at
            """,
            (
                job_id,
                snapshot["prompt"],
                snapshot["status"],
                json.dumps(snapshot["plan"]),
                json.dumps(snapshot["report"]),
                now,
                now,
            ),
        )

        for name, step in snapshot["steps"].items():
            conn.execute(
                """
                INSERT INTO steps(job_id,name,status,attempts,started_at,ended_at,output,metrics_json,error)
                VALUES (?,?,?,?,?,?,?,?,?)
                ON CONFLICT(job_id,name) DO UPDATE SET
                    status=excluded.status,
                    attempts=excluded.attempts,
                    started_at=excluded.started_at,
                    ended_at=excluded.ended_at,
                    output=excluded.output,
                    metrics_json=excluded.metrics_json,
                    error=excluded.error
                """,
                (
                    job_id,
                    name,
                    step.get("status", "pending"),
                    step.get("attempts", 0),
                    step.get("started_at"),
                    step.get("ended_at"),
                    step.get("output", ""),
                    json.dumps(step.get("metrics", {})),
                    step.get("error"),
                ),
            )

        conn.execute("DELETE FROM artifacts WHERE job_id = ?", (job_id,))
        conn.executemany(
            "INSERT INTO artifacts(job_id,path,file_path) VALUES (?,?,?)",
            [(job_id, path, file_path) for path, file_path in snapshot["artifacts"].items()],
        )

        conn.execute("DELETE FROM repair_history WHERE job_id = ?", (job_id,))
        conn.executemany(
            """
            INSERT INTO repair_history(job_id,stage,attempt,diagnosis_json,created_at)
            VALUES (?,?,?,?,?)
            """,
            [
                (
                    job_id,
                    item.get("stage"),
                    item.get("attempt"),
                    json.dumps(item.get("diagnosis", {})),
                    now,
                )
                for item in snapshot["repair_history"]
            ],
        )


def add_event(job_id: str, event: dict, now: str) -> int:
    with _conn() as conn:
        cur = conn.execute(
            "INSERT INTO events(job_id,event_type,payload_json,created_at) VALUES (?,?,?,?)",
            (job_id, event["type"], json.dumps(event), now),
        )
        return int(cur.lastrowid)


def get_job(job_id: str) -> dict | None:
    with _conn() as conn:
        job = conn.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
        if not job:
            return None

        steps = conn.execute(
            "SELECT * FROM steps WHERE job_id = ? ORDER BY rowid", (job_id,)
        ).fetchall()
        artifacts = conn.execute(
            "SELECT path,file_path FROM artifacts WHERE job_id = ? ORDER BY path", (job_id,)
        ).fetchall()
        repairs = conn.execute(
            "SELECT stage,attempt,diagnosis_json FROM repair_history WHERE job_id = ? ORDER BY id", (job_id,)
        ).fetchall()

        return {
            "job_id": job["job_id"],
            "prompt": job["prompt"],
            "status": job["status"],
            "plan": json.loads(job["plan_json"] or "{}"),
            "steps": {
                row["name"]: {
                    "name": row["name"],
                    "status": row["status"],
                    "attempts": row["attempts"],
                    "started_at": row["started_at"],
                    "ended_at": row["ended_at"],
                    "output": row["output"],
                    "metrics": json.loads(row["metrics_json"] or "{}"),
                    "error": row["error"],
                }
                for row in steps
            },
            "artifacts": {row["path"]: row["file_path"] for row in artifacts},
            "repair_history": [
                {
                    "stage": row["stage"],
                    "attempt": row["attempt"],
                    "diagnosis": json.loads(row["diagnosis_json"] or "{}"),
                }
                for row in repairs
            ],
            "report": json.loads(job["report_json"] or "{}"),
        }


def list_jobs(limit: int = 50) -> list[dict]:
    with _conn() as conn:
        rows = conn.execute(
            "SELECT job_id,prompt,status,created_at,updated_at FROM jobs ORDER BY updated_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(row) for row in rows]


def get_events(job_id: str, after_id: int = 0) -> list[dict]:
    with _conn() as conn:
        rows = conn.execute(
            "SELECT id,payload_json FROM events WHERE job_id=? AND id>? ORDER BY id",
            (job_id, after_id),
        ).fetchall()
        result = []
        for row in rows:
            event = json.loads(row["payload_json"])
            event["event_id"] = row["id"]
            result.append(event)
        return result
