"""Self-refinement orchestrator — the main search-and-score loop."""

from __future__ import annotations

import dataclasses
from engine import database as db
from engine.search_strategy import build_queue, build_relaxed_queue
from engine.linkedin_client import search as li_search, close as li_close
from engine.job_extractor import detail_pass
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
    """Execute the full extraction pipeline. Yields Progress, returns final job list."""
    if run_id is None:
        run_id = db.create_run(prompt, max_jobs, parsed_plan)
    collected: list[dict] = []
    seen: set[str] = db.seen_job_ids(run_id)
    attempts = 0
    max_attempts = 25

    # Build the search queue
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

        # Search
        try:
            cards = li_search(item.query, item.location)
        except Exception as e:
            db.log_attempt(run_id, item.query, item.location, action=item.action, error=str(e))
            yield Progress(
                run_id=run_id, collected=len(collected), target=max_jobs,
                attempts=attempts, current_query=item.query,
                current_location=item.location, status="error",
                error=f"Search failed: {e}",
            )
            continue

        if not cards:
            db.log_attempt(run_id, item.query, item.location, action=item.action, cards=0, relevant=0)
            continue

        # Enrich with details
        limit = max_jobs - len(collected)
        jobs = detail_pass(cards, limit, seen)

        # Filter by relevance
        parsed_plan["_original_prompt"] = prompt
        relevant = filter_relevant(jobs, parsed_plan)

        # Save to DB
        for job in relevant:
            if job.get("linkedin_job_id") not in seen:
                db.upsert_job(job, run_id, prompt)
                collected.append(job)
                seen.add(job["linkedin_job_id"])

        db.log_attempt(
            run_id, item.query, item.location,
            action=item.action, cards=len(cards), relevant=len(relevant),
        )

        yield Progress(
            run_id=run_id,
            collected=len(collected),
            target=max_jobs,
            attempts=attempts,
            current_query=item.query,
            current_location=item.location,
        )

    # Finalize
    status = "completed" if len(collected) >= max_jobs else "partial"
    db.finish_run(run_id, status=status, jobs_found=len(collected))
    li_close()
    return collected
