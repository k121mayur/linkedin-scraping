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

CREATE TABLE IF NOT EXISTS grants (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    post_urn            TEXT UNIQUE NOT NULL,
    content_hash        TEXT,
    post_url            TEXT,
    author              TEXT,
    author_url          TEXT,
    posted_date         TEXT,
    posted_date_normalized TEXT,
    opportunity_title   TEXT,
    funder              TEXT,
    summary             TEXT,
    deadline            TEXT,
    grant_amount        TEXT,
    eligibility         TEXT,
    focus_areas         TEXT,
    geography           TEXT,
    how_to_apply        TEXT,
    application_link    TEXT,
    external_links      TEXT,
    contact_email       TEXT,
    post_text           TEXT,
    image_text          TEXT,
    external_site_summary TEXT,
    relevance_score     REAL,
    relevance_reason    TEXT,
    keyword             TEXT,
    prompt              TEXT,
    search_run_id       INTEGER NOT NULL,
    scraped_at          TEXT NOT NULL,
    raw_json            TEXT,
    FOREIGN KEY (search_run_id) REFERENCES search_runs(id)
);

CREATE INDEX IF NOT EXISTS idx_grants_run ON grants(search_run_id);
CREATE INDEX IF NOT EXISTS idx_grants_hash ON grants(content_hash);

CREATE TABLE IF NOT EXISTS users (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT NOT NULL,
    email           TEXT UNIQUE NOT NULL,
    password_hash   TEXT NOT NULL,
    created_at      TEXT NOT NULL
);
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
    # Migration: run_type distinguishes 'jobs' vs 'grants' runs on old DBs.
    try:
        c.execute("ALTER TABLE search_runs ADD COLUMN run_type TEXT NOT NULL DEFAULT 'jobs'")
    except sqlite3.OperationalError:
        pass  # column already exists
    c.commit()


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── search_runs ──────────────────────────────────────

def create_run(prompt: str, max_jobs: int, parsed_plan: Optional[dict] = None,
               run_type: str = "jobs") -> int:
    db = _conn()
    cur = db.execute(
        "INSERT INTO search_runs (prompt, parsed_plan_json, max_jobs, started_at, status, run_type) "
        "VALUES (?, ?, ?, ?, 'running', ?)",
        (prompt, json.dumps(parsed_plan) if parsed_plan else None, max_jobs, now_iso(), run_type),
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


# ── grants ───────────────────────────────────────────

_GRANT_FIELDS = [
    "post_urn", "content_hash", "post_url", "author", "author_url",
    "posted_date", "posted_date_normalized", "opportunity_title", "funder",
    "summary", "deadline", "grant_amount", "eligibility", "focus_areas",
    "geography", "how_to_apply", "application_link", "external_links",
    "contact_email", "post_text", "image_text", "external_site_summary",
    "relevance_score", "relevance_reason", "keyword",
]


def upsert_grant(grant: dict, run_id: int, prompt: str) -> bool:
    """Insert or update a grant post by post_urn. Returns True if newly inserted."""
    c = _conn()
    existing = c.execute(
        "SELECT id FROM grants WHERE post_urn=?", (grant["post_urn"],)
    ).fetchone()
    values = [grant.get(f) for f in _GRANT_FIELDS]
    if existing:
        sets = ", ".join(f"{f}=?" for f in _GRANT_FIELDS)
        c.execute(
            f"UPDATE grants SET {sets}, prompt=?, scraped_at=?, raw_json=?, search_run_id=? "
            "WHERE post_urn=?",
            (*values, prompt, now_iso(), json.dumps(grant, default=str), run_id, grant["post_urn"]),
        )
        c.commit()
        return False
    cols = ", ".join(_GRANT_FIELDS)
    marks = ",".join("?" * len(_GRANT_FIELDS))
    c.execute(
        f"INSERT INTO grants ({cols}, prompt, search_run_id, scraped_at, raw_json) "
        f"VALUES ({marks},?,?,?,?)",
        (*values, prompt, run_id, now_iso(), json.dumps(grant, default=str)),
    )
    c.commit()
    return True


def seen_grant_keys(run_id: Optional[int] = None) -> tuple[set[str], set[str]]:
    """Return (post_urns, content_hashes) already stored. Optionally scoped to a run."""
    c = _conn()
    if run_id is not None:
        rows = c.execute(
            "SELECT post_urn, content_hash FROM grants WHERE search_run_id=?", (run_id,)
        ).fetchall()
    else:
        rows = c.execute("SELECT post_urn, content_hash FROM grants").fetchall()
    urns = {r["post_urn"] for r in rows if r["post_urn"]}
    hashes = {r["content_hash"] for r in rows if r["content_hash"]}
    return urns, hashes


def get_run_grants(run_id: int) -> list[dict]:
    c = _conn()
    rows = c.execute(
        "SELECT * FROM grants WHERE search_run_id=? ORDER BY relevance_score DESC",
        (run_id,),
    ).fetchall()
    return [dict(r) for r in rows]


# ── users (role-based access) ────────────────────────

def add_user(name: str, email: str, password_hash: str) -> int:
    c = _conn()
    cur = c.execute(
        "INSERT INTO users (name, email, password_hash, created_at) VALUES (?,?,?,?)",
        (name, email.lower().strip(), password_hash, now_iso()),
    )
    c.commit()
    assert cur.lastrowid is not None
    return cur.lastrowid


def get_user_by_email(email: str) -> Optional[dict]:
    c = _conn()
    row = c.execute("SELECT * FROM users WHERE email=?", (email.lower().strip(),)).fetchone()
    return dict(row) if row else None


def list_users() -> list[dict]:
    c = _conn()
    rows = c.execute("SELECT id, name, email, created_at FROM users ORDER BY id").fetchall()
    return [dict(r) for r in rows]


def delete_user(user_id: int) -> bool:
    c = _conn()
    cur = c.execute("DELETE FROM users WHERE id=?", (user_id,))
    c.commit()
    return cur.rowcount > 0


# Auto-init on import
init_db()
