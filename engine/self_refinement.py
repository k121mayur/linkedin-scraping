"""Self-refinement orchestrator — the main search-and-score loop."""

from __future__ import annotations

import dataclasses
from config import FETCH_DETAILS, JOBS_PER_PAGE
from engine import database as db
from engine.search_strategy import build_queue, build_relaxed_queue
from engine.linkedin_client import search as li_search, close as li_close, get_job_detail
from engine.relevance import filter_relevant


@dataclasses.dataclass
class Progress:
    run_id: int
    collected: int
    target: int
    attempts: int
    current_query: str = ""
    current_location: str = ""
    status: str = "running"
    error: str = ""


def run(prompt: str, parsed_plan: dict, max_jobs: int, run_id=None):
    """Execute the full extraction pipeline. Yields Progress, returns final job list.

    Per query: gather cards (paginated, bounded by need) → score relevance on the
    card (cheap, title-driven) → enrich the relevant ones with full details (which
    also guarantees a valid link) → persist. Keeps going through the primary queue
    and, if still short, broadened queries, until the target count is reached or
    candidates are genuinely exhausted.
    """
    if run_id is None:
        run_id = db.create_run(prompt, max_jobs, parsed_plan)
    parsed_plan["_original_prompt"] = prompt

    collected: list[dict] = []
    seen: set[str] = db.seen_job_ids(run_id)   # persisted dedup (this run)
    examined: set[str] = set()                 # cards already scored this run
    attempts = 0
    max_attempts = 40

    queue = build_queue(parsed_plan)
    relaxed_attempts = 0

    while len(collected) < max_jobs and attempts < max_attempts:
        attempts += 1

        if not queue:
            queue = build_relaxed_queue(parsed_plan, relaxed_attempts)
            relaxed_attempts += 1
            if not queue:
                break

        item = queue.pop(0)
        db.log_attempt(run_id, item.query, item.location, action=item.action)
        need = max_jobs - len(collected)

        # Refresh the live "Searching: …" line (keeps the current count).
        yield Progress(
            run_id=run_id, collected=len(collected), target=max_jobs,
            attempts=attempts, current_query=item.query, current_location=item.location,
        )

        # Search (paginated, bounded so we don't over-scrape for a small target).
        try:
            cards = li_search(item.query, item.location, limit=max(need * 2, JOBS_PER_PAGE))
        except Exception as e:
            db.log_attempt(run_id, item.query, item.location, action=item.action, error=str(e))
            yield Progress(
                run_id=run_id, collected=len(collected), target=max_jobs,
                attempts=attempts, current_query=item.query,
                current_location=item.location, status="error",
                error=f"Search failed: {e}",
            )
            continue

        # Drop cards already scored/collected in this run.
        fresh = [c for c in cards if c["job_id"] not in examined and c["job_id"] not in seen]
        for c in fresh:
            examined.add(c["job_id"])

        if not fresh:
            db.log_attempt(run_id, item.query, item.location,
                           action=item.action, cards=len(cards), relevant=0)
            yield Progress(
                run_id=run_id, collected=len(collected), target=max_jobs,
                attempts=attempts, current_query=item.query, current_location=item.location,
            )
            continue

        # Score relevance on card data (title/company) — strict, no navigation.
        relevant = filter_relevant(fresh, parsed_plan)
        relevant.sort(key=lambda j: j.get("relevance_score", 0), reverse=True)
        to_take = relevant[:need]

        # Collect ONE job at a time: enrich it with full detail, persist, and
        # emit a Progress so the UI ticks up incrementally (1, 2, 3, …) rather
        # than jumping by the whole batch at once.
        for card in to_take:
            jid = card.get("job_id") or card.get("linkedin_job_id", "")
            if not jid or jid in seen:
                continue

            if FETCH_DETAILS:
                detail = get_job_detail(card.get("link", "")) or {}
                job = {**card, **detail, "linkedin_job_id": jid}
            else:
                job = {**card, "linkedin_job_id": jid}
            if not job.get("apply_url"):
                job["apply_url"] = card.get("link", "")

            db.upsert_job(job, run_id, prompt)
            collected.append(job)
            seen.add(jid)

            yield Progress(
                run_id=run_id,
                collected=len(collected),
                target=max_jobs,
                attempts=attempts,
                current_query=item.query,
                current_location=item.location,
            )
            if len(collected) >= max_jobs:
                break

        db.log_attempt(
            run_id, item.query, item.location,
            action=item.action, cards=len(cards), relevant=len(relevant),
        )

    # Finalize
    status = "completed" if len(collected) >= max_jobs else "partial"
    db.finish_run(run_id, status=status, jobs_found=len(collected))
    li_close()
    return collected
