"""Export job results to xlsx, csv, or json format."""

from __future__ import annotations

import json
import csv
import io
import re
from pathlib import Path
from config import LINKEDIN_JOB_VIEW_URL, EXPORTS_DIR
from engine.database import get_run, get_run_jobs

EXPORT_COLUMNS = [
    "linkedin_job_id", "title", "company", "company_url", "location",
    "posted_date", "job_url", "apply_url", "description", "sector",
    "experience_level", "relevance_score", "relevance_reason",
]


def _job_url(job: dict) -> str:
    """Always return a valid clickable LinkedIn link for the job."""
    url = job.get("apply_url") or job.get("link") or ""
    if url:
        return url
    jid = str(job.get("linkedin_job_id", "")).strip()
    if jid.isdigit():
        return f"{LINKEDIN_JOB_VIEW_URL.rstrip('/')}/{jid}"
    return ""


def _flatten(jobs: list[dict]) -> list[dict]:
    """Select only export columns, fill missing with empty string."""
    rows = []
    for job in jobs:
        enriched = {**job, "job_url": _job_url(job)}
        rows.append({col: str(enriched.get(col, ""))[:30000] for col in EXPORT_COLUMNS})
    return rows


def export_json(run_id: int) -> str:
    """Export jobs for a run as a JSON string."""
    jobs = get_run_jobs(run_id)
    return json.dumps(jobs, indent=2, ensure_ascii=False)


def export_csv(run_id: int) -> str:
    """Export jobs for a run as CSV string."""
    jobs = _flatten(get_run_jobs(run_id))
    if not jobs:
        return ""
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=EXPORT_COLUMNS)
    writer.writeheader()
    writer.writerows(jobs)
    return buf.getvalue()


def export_xlsx_bytes(run_id: int) -> bytes:
    """Export jobs for a run as xlsx binary."""
    import pandas as pd
    jobs = _flatten(get_run_jobs(run_id))
    df = pd.DataFrame(jobs, columns=EXPORT_COLUMNS)
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
        df.to_excel(writer, index=False, sheet_name="LinkedIn Jobs")
    return buf.getvalue()


# ── File exports (used by the CLI) ───────────────────────────

def _run_stem(run_id: int) -> str:
    """Filesystem-safe name derived from the run's prompt (mirrors app._download_stem)."""
    run = get_run(run_id)
    if run and run.get("prompt"):
        slug = re.sub(r"[^a-z0-9]+", "_", run["prompt"].strip().lower()).strip("_")[:60]
        if slug:
            return slug
    return f"run_{run_id}"


def export_to_files(run_id: int, formats: list[str], out_dir: Path | None = None) -> list[str]:
    """Write the requested export formats to disk. Returns the paths written."""
    out_dir = out_dir or EXPORTS_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = _run_stem(run_id)
    written: list[str] = []
    for fmt in formats:
        fmt = fmt.strip().lower()
        path = out_dir / f"{stem}.{fmt}"
        if fmt == "json":
            path.write_text(export_json(run_id), encoding="utf-8")
        elif fmt == "csv":
            path.write_text(export_csv(run_id), encoding="utf-8")
        elif fmt == "xlsx":
            path.write_bytes(export_xlsx_bytes(run_id))
        else:
            continue
        written.append(str(path))
    return written
