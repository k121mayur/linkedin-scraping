"""SQLite database layer for LinkedIn extraction engine."""

from __future__ import annotations

import sqlite3
import threading
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from config import DATA_DIR

DB_PATH = DATA_DIR / "jobs.db"

_local = threading.local()

SCHEMA = """
CREATE TABLE IF NOT EXISTS search_runs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    prompt          TEXT NOT NULL,
    parsed_plan_json TEXT,
    max_jobs        INTEGER NOT NULL,
    started_at      TEXT NOT NULL,
    finished_at     TEXT,
    status          TEXT NOT NULL DEFAULT 'running',
    jobs_found      INTEGER DEFAULT 0,
    error_message   TEXT
);

CREATE TABLE IF NOT EXISTS jobs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    linkedin_job_id TEXT UNIQUE NOT NULL,
    title           TEXT,
    company         TEXT,
    company_url     TEXT,
    location        TEXT,
    posted_date     TEXT,
    apply_url       TEXT,
    description     TEXT,
    sector          TEXT,
    experience_level TEXT,
    relevance_score REAL,
    relevance_reason TEXT,
    prompt          TEXT,
    search_run_id   INTEGER NOT NULL,
    scraped_at      TEXT NOT NULL,
    raw_json        TEXT,
    FOREIGN KEY (search_run_id) REFERENCES search_runs(id)
);

CREATE INDEX IF NOT EXISTS idx_jobs_run ON jobs(search_run_id);
CREATE INDEX IF NOT EXISTS idx_jobs_relevance ON jobs(search_run_id, relevance_score DESC);

CREATE TABLE IF NOT EXISTS search_attempts (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    search_run_id   INTEGER NOT NULL,
    query           TEXT NOT NULL,
    location        TEXT NOT NULL,
    cards_extracted INTEGER,
    jobs_relevant   INTEGER,
    refinement_action TEXT,
    error           TEXT,
    attempted_at    TEXT NOT NULL,
    FOREIGN KEY (search_run_id) REFERENCES search_runs(id)
);

CREATE INDEX IF NOT EXISTS idx_attempts_run ON search_attempts(search_run_id);
"""


def _conn() -> sqlite3.Connection:
    """Get a thread-local connection with WAL mode and foreign keys."""
    if not hasattr(_local, "conn") or _local.conn is None:
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.row_factory = sqlite3.Row
        _local.conn = conn
    return _local.conn


def init_db():
    """Create schema if it doesn't exist. Idempotent."""
    c = _conn()
    c.executescript(SCHEMA)
    c.commit()


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── search_runs ──────────────────────────────────────

def create_run(prompt: str, max_jobs: int, parsed_plan: Optional[dict] = None) -> int:
    db = _conn()
    cur = db.execute(
        "INSERT INTO search_runs (prompt, parsed_plan_json, max_jobs, started_at, status) "
        "VALUES (?, ?, ?, ?, 'running')",
        (prompt, json.dumps(parsed_plan) if parsed_plan else None, max_jobs, now_iso()),
    )
    db.commit()
    assert cur.lastrowid is not None
    return cur.lastrowid


def finish_run(run_id: int, status: str = "completed", jobs_found: int = 0, error: Optional[str] = None):
    c = _conn()
    c.execute(
        "UPDATE search_runs SET finished_at=?, status=?, jobs_found=?, error_message=? WHERE id=?",
        (now_iso(), status, jobs_found, error, run_id),
    )
    c.commit()


def get_run(run_id: int) -> Optional[dict]:
    c = _conn()
    row = c.execute("SELECT * FROM search_runs WHERE id=?", (run_id,)).fetchone()
    return dict(row) if row else None


def get_run_jobs(run_id: int) -> list[dict]:
    c = _conn()
    rows = c.execute(
        "SELECT * FROM jobs WHERE search_run_id=? ORDER BY relevance_score DESC",
        (run_id,),
    ).fetchall()
    return [dict(r) for r in rows]


# ── jobs ─────────────────────────────────────────────

def upsert_job(job: dict, run_id: int, prompt: str) -> bool:
    """Insert or update a job by linkedin_job_id. Returns True if inserted (new), False if updated."""
    c = _conn()
    existing = c.execute(
        "SELECT id FROM jobs WHERE linkedin_job_id=?", (job["linkedin_job_id"],)
    ).fetchone()
    if existing:
        c.execute(
            """UPDATE jobs SET title=?, company=?, company_url=?, location=?, posted_date=?,
               apply_url=?, description=?, sector=?, experience_level=?,
               relevance_score=?, relevance_reason=?, prompt=?, scraped_at=?,
               raw_json=?, search_run_id=?
               WHERE linkedin_job_id=?""",
            (
                job.get("title"), job.get("company"), job.get("company_url"),
                job.get("location"), job.get("posted_date"), job.get("apply_url"),
                job.get("description"), job.get("sector"), job.get("experience_level"),
                job.get("relevance_score"), job.get("relevance_reason"),
                prompt, now_iso(), json.dumps(job), run_id,
                job["linkedin_job_id"],
            ),
        )
        c.commit()
        return False
    else:
        c.execute(
            """INSERT INTO jobs (linkedin_job_id, title, company, company_url, location,
               posted_date, apply_url, description, sector, experience_level,
               relevance_score, relevance_reason, prompt, search_run_id, scraped_at, raw_json)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                job["linkedin_job_id"], job.get("title"), job.get("company"),
                job.get("company_url"), job.get("location"), job.get("posted_date"),
                job.get("apply_url"), job.get("description"), job.get("sector"),
                job.get("experience_level"), job.get("relevance_score"),
                job.get("relevance_reason"), prompt, run_id, now_iso(),
                json.dumps(job),
            ),
        )
        c.commit()
        return True


def upsert_many(jobs: list[dict], run_id: int, prompt: str) -> int:
    """Upsert a list of job dicts. Returns count of newly inserted rows."""
    inserted = 0
    for job in jobs:
        if upsert_job(job, run_id, prompt):
            inserted += 1
    return inserted


def seen_job_ids(run_id: Optional[int] = None) -> set[str]:
    """Return set of all linkedin_job_ids already in DB. Optionally scope to run_id."""
    c = _conn()
    if run_id is not None:
        rows = c.execute("SELECT linkedin_job_id FROM jobs WHERE search_run_id=?", (run_id,)).fetchall()
    else:
        rows = c.execute("SELECT linkedin_job_id FROM jobs").fetchall()
    return {r["linkedin_job_id"] for r in rows}


# ── search_attempts ──────────────────────────────────

def log_attempt(run_id: int, query: str, location: str,
                action: str = "seed", cards: Optional[int] = None,
                relevant: Optional[int] = None, error: Optional[str] = None):
    c = _conn()
    c.execute(
        """INSERT INTO search_attempts (search_run_id, query, location, cards_extracted,
           jobs_relevant, refinement_action, error, attempted_at)
           VALUES (?,?,?,?,?,?,?,?)""",
        (run_id, query, location, cards, relevant, action, error, now_iso()),
    )
    c.commit()


# Auto-init on import
init_db()
