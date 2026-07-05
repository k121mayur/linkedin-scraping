"""Export grant-post results to xlsx, csv, or json format."""

from __future__ import annotations

import csv
import io
import json

from engine.database import get_run_grants

GRANT_EXPORT_COLUMNS = [
    "opportunity_title", "funder", "summary", "deadline", "grant_amount",
    "eligibility", "focus_areas", "geography", "how_to_apply",
    "application_link", "contact_email", "post_url", "author", "author_url",
    "posted_date", "posted_date_normalized", "external_links",
    "relevance_score", "relevance_reason", "keyword", "post_text",
    "image_text", "scraped_at",
]


def _flatten(grants: list[dict]) -> list[dict]:
    return [{col: str(g.get(col) or "")[:30000] for col in GRANT_EXPORT_COLUMNS}
            for g in grants]


def export_grants_json(run_id: int) -> str:
    return json.dumps(get_run_grants(run_id), indent=2, ensure_ascii=False)


def export_grants_csv(run_id: int) -> str:
    rows = _flatten(get_run_grants(run_id))
    if not rows:
        return ""
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=GRANT_EXPORT_COLUMNS)
    writer.writeheader()
    writer.writerows(rows)
    return buf.getvalue()


def export_grants_xlsx_bytes(run_id: int) -> bytes:
    import pandas as pd
    rows = _flatten(get_run_grants(run_id))
    df = pd.DataFrame(rows, columns=GRANT_EXPORT_COLUMNS)
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
        df.to_excel(writer, index=False, sheet_name="Grant Opportunities")
    return buf.getvalue()
