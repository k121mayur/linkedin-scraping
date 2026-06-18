"""Extract structured job data from LinkedIn search cards and detail pages."""

from __future__ import annotations

from typing import Optional
from config import MAX_DETAIL_CONCURRENCY
from engine.linkedin_client import get_job_detail


def extract_cards(cards: list[dict]) -> list[dict]:
    """Fast path: return card data as-is from search results. No detail fetch."""
    return cards


def detail_pass(cards: list[dict], limit: int, seen_ids: Optional[set[str]] = None) -> list[dict]:
    """Enrich job cards with full detail by visiting each job page.

    Skips already-seen jobs and respects the concurrency cap.
    Does NOT mutate the passed seen_ids set.
    """
    seen = set(seen_ids) if seen_ids else set()
    results = []
    count = 0

    for card in cards:
        if count >= limit:
            break

        job_id = card.get("job_id") or card.get("linkedin_job_id", "")
        if job_id in seen:
            continue

        detail = get_job_detail(card.get("link", ""))
        if detail is None:
            continue

        merged = {**card, **detail, "linkedin_job_id": job_id}
        results.append(merged)
        seen.add(job_id)
        count += 1

    return results
