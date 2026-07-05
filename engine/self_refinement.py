"""Self-refinement orchestrator — the main search-and-score loop."""

from __future__ import annotations

import dataclasses
import sys
from config import FETCH_DETAILS, JOBS_PER_PAGE
from engine import database as db
from engine.search_strategy import build_queue, build_relaxed_queue
from engine.linkedin_client import (
    search as li_search,
    get_job_detail,
    canonical_view_url,
)
from engine.relevance import filter_relevant, _keyword_score


def _log(msg: str) -> None:
    """Emit a progress line to the terminal (stdout) so a scrape is observable
    live from the server shell / VS Code terminal, in both web and CLI mode.

    Job titles can contain characters (₹, •, em-dashes) that a Windows cp1252
    console can't encode; a bare print() then raises UnicodeEncodeError and
    kills the whole run. Degrade to ASCII instead of crashing.
    """
    line = f"[scrape] {msg}"
    try:
        print(line, flush=True)
    except UnicodeEncodeError:
        print(line.encode("ascii", "replace").decode("ascii"), flush=True)


def _company_url_for(job: dict) -> str:
    """Best-effort, never-empty company URL for export.

    Prefer the scraped company_url; otherwise derive a LinkedIn company-search
    URL from the company name so the Excel column is never blank.
    """
    url = (job.get("company_url") or "").strip()
    if url:
        return url
    company = (job.get("company") or "").strip()
    if company:
        from urllib.parse import quote
        return f"https://www.linkedin.com/search/results/companies/?keywords={quote(company)}"
    return ""


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


def run(prompt: str, parsed_plan: dict, max_jobs: int, run_id=None, should_stop=None):
    """Execute the full extraction pipeline. Yields Progress, returns final job list.

    Per query: gather cards (paginated, bounded by need) → score relevance on the
    card (cheap, title-driven) → enrich the relevant ones with full details (which
    also guarantees a valid link) → persist. Keeps going through the primary queue
    and, if still short, broadened queries, until the target count is reached or
    candidates are genuinely exhausted.

    ``should_stop`` is an optional zero-arg callable returning True when the user
    has requested a stop. It's checked cooperatively between passes and after each
    saved job, so a stop halts promptly while keeping everything collected so far
    (jobs are persisted incrementally, so they're immediately exportable).
    """
    stopped = should_stop if callable(should_stop) else (lambda: False)

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

    _log(f"Run {run_id} started - target {max_jobs} jobs | prompt: {prompt!r}")

    user_stopped = False

    while len(collected) < max_jobs and attempts < max_attempts:
        if stopped():
            user_stopped = True
            _log("Stop requested - halting before next pass.")
            break
        attempts += 1

        if not queue:
            queue = build_relaxed_queue(parsed_plan, relaxed_attempts)
            relaxed_attempts += 1
            if not queue:
                break

        item = queue.pop(0)
        db.log_attempt(run_id, item.query, item.location, action=item.action)
        need = max_jobs - len(collected)

        loc = f" in {item.location}" if item.location else ""
        _log(f"Attempt {attempts} [{item.action}] searching {item.query!r}{loc} "
             f"({len(collected)}/{max_jobs} collected)")

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
            _log(f"  ! search failed: {e}")
            yield Progress(
                run_id=run_id, collected=len(collected), target=max_jobs,
                attempts=attempts, current_query=item.query,
                current_location=item.location, status="error",
                error=f"Search failed: {e}",
            )
            continue

        _log(f"  found {len(cards)} card(s) on LinkedIn")

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

        # Cap how many cards go to (slow) LLM scoring: pre-rank by the cheap
        # keyword score and keep the most promising few multiples of what we
        # still need. Only bites on small targets — for large runs the cap
        # exceeds the page yield and everything is scored.
        cap = max(need * 4, 12)
        if len(fresh) > cap:
            fresh.sort(key=lambda c: _keyword_score(c, parsed_plan), reverse=True)
            fresh = fresh[:cap]

        # Score relevance on card data (title/company) — strict, no navigation.
        relevant = filter_relevant(fresh, parsed_plan)
        relevant.sort(key=lambda j: j.get("relevance_score", 0), reverse=True)
        to_take = relevant[:need]

        # Collect ONE job at a time: enrich it with full detail, persist, and
        # emit a Progress so the UI ticks up incrementally (1, 2, 3, …) rather
        # than jumping by the whole batch at once.
        for card in to_take:
            if stopped():
                user_stopped = True
                _log("Stop requested - halting mid-pass; keeping jobs saved so far.")
                break

            jid = card.get("job_id") or card.get("linkedin_job_id", "")
            if not jid or jid in seen:
                continue

            if FETCH_DETAILS:
                detail = get_job_detail(card.get("link", "")) or {}
                job = {**card, **detail, "linkedin_job_id": jid}
            else:
                job = {**card, "linkedin_job_id": jid}
            if not job.get("apply_url"):
                job["apply_url"] = card.get("link", "") or canonical_view_url(jid)
            # Guarantee the export's company_url column is never blank.
            job["company_url"] = _company_url_for(job)

            db.upsert_job(job, run_id, prompt)
            collected.append(job)
            seen.add(jid)

            _log(f"  + saved {len(collected)}/{max_jobs}: "
                 f"{(job.get('title') or 'Untitled')} @ {(job.get('company') or '?')}")

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

        if user_stopped:
            break

    # Finalize
    if user_stopped:
        status = "stopped"
    elif len(collected) >= max_jobs:
        status = "completed"
    else:
        status = "partial"
    db.finish_run(run_id, status=status, jobs_found=len(collected))
    _log(f"Run {run_id} {status} - {len(collected)}/{max_jobs} jobs in {attempts} attempt(s)")
    # The browser is deliberately left open: the next run reuses the warm,
    # already-authenticated session (~35s faster to the first job).
    # linkedin_client._ensure_auth() revalidates and relaunches if it died.

    # Emit a terminal Progress so the UI can render the final state (esp. a
    # user-requested stop) and reveal the download links for what was collected.
    yield Progress(
        run_id=run_id, collected=len(collected), target=max_jobs,
        attempts=attempts, status=status,
    )
    return collected
