"""Export job results to xlsx, csv, or json format."""

from __future__ import annotations

import json
import csv
import io
from engine.database import get_run_jobs

EXPORT_COLUMNS = [
    "linkedin_job_id", "title", "company", "company_url", "location",
    "posted_date", "apply_url", "description", "sector", "experience_level",
    "relevance_score", "relevance_reason",
]


def _flatten(jobs: list[dict]) -> list[dict]:
    """Select only export columns, fill missing with empty string."""
    return [
        {col: str(job.get(col, ""))[:30000] for col in EXPORT_COLUMNS}
        for job in jobs
    ]


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
