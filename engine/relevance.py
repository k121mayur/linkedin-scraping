"""Relevance scoring via batched LLM calls with keyword fallback."""

from __future__ import annotations

from typing import Optional
import json
from config import DRY_RUN, RELEVANCE_THRESHOLD
from config.ai_config import PROMPT_RELEVANCE_TEMPLATE, AI_BATCH_SIZE
from engine.llm_client import chat_json


def _contains(kw: str, hay: str) -> bool:
    """True if the phrase, or all of its significant words, appear in hay."""
    kw = kw.lower().strip()
    if not kw:
        return False
    if kw in hay:
        return True
    words = [w for w in kw.split() if len(w) > 2]
    return bool(words) and all(w in hay for w in words)


def _any_contains(kws: list[str], hay: str) -> bool:
    return any(_contains(kw, hay) for kw in kws)


def _fraction_present(kws: list[str], hay: str) -> float:
    if not kws:
        return 0.0
    hits = sum(1 for kw in kws if _contains(kw, hay))
    return hits / len(kws)


def _keyword_score(job: dict, plan: dict) -> float:
    """Graded keyword relevance in [0,1], aligned with the LLM's scale.

    Role match in the TITLE is the strongest signal; sector/skills add weight;
    excluded terms (wrong role/level) push the score down hard. This is the
    dependable fallback used whenever the LLM is unavailable.
    """
    title = str(job.get("title", "")).lower()
    body = " ".join(
        str(job.get(k, "")) for k in ("description", "snippet", "company", "location")
    ).lower()
    full = (title + " " + body).strip()
    if not full:
        return 0.0

    role_kws = plan.get("role_keywords", [])
    sector_kws = plan.get("sector_keywords", [])
    skill_kws = plan.get("skills", [])
    exp_kws = plan.get("experience_keywords", [])
    exclude_kws = plan.get("exclude_keywords", [])

    # Role is the dominant signal.
    if role_kws and _any_contains(role_kws, title):
        score = 0.75
    elif role_kws and _any_contains(role_kws, body):
        score = 0.55
    else:
        # Partial credit for significant-word overlap with any role phrase.
        best = 0.0
        for kw in role_kws:
            words = [w for w in kw.lower().split() if len(w) > 2]
            if words:
                best = max(best, sum(1 for w in words if w in full) / len(words))
        score = 0.35 * best

    # Supporting signals.
    score += 0.10 if (sector_kws and _any_contains(sector_kws, full)) else 0.0
    score += 0.12 * _fraction_present(skill_kws, full)
    if exp_kws and _any_contains(exp_kws, full):
        score += 0.08

    # Hard penalties for clearly-disqualifying terms (strongest in the title).
    for kw in exclude_kws:
        if _contains(kw, title):
            score -= 0.5
        elif _contains(kw, body):
            score -= 0.2

    return max(0.0, min(1.0, score))


def _batch_jobs(jobs: list[dict], plan: dict, batch_size: int) -> list[dict]:
    """Score jobs using LLM in batches. Returns jobs with scores added."""
    if not jobs:
        return []

    scored = []
    for i in range(0, len(jobs), batch_size):
        batch = jobs[i:i + batch_size]

        # Build compact batch representation
        batch_items = []
        for j in batch:
            desc = (j.get("description") or j.get("snippet") or "")[:500]
            batch_items.append({
                "job_id": j.get("job_id") or j.get("linkedin_job_id", ""),
                "title": j.get("title", ""),
                "company": j.get("company", ""),
                "description": desc,
                "location": j.get("location", ""),
            })

        try:
            prompt = PROMPT_RELEVANCE_TEMPLATE.format(
                sector=plan.get("sector", "general"),
                original_prompt=plan.get("_original_prompt", ""),
                jobs_batch=json.dumps(batch_items, indent=2),
            )
            result = chat_json(prompt)
        except Exception:
            result = []

        # Map scores back to jobs
        score_map = {}
        if isinstance(result, list):
            for item in result:
                if isinstance(item, dict):
                    score_map[item.get("job_id", "")] = item

        for j in batch:
            jid = j.get("job_id") or j.get("linkedin_job_id", "")
            info = score_map.get(jid, {})
            j["relevance_score"] = info.get("score", _keyword_score(j, plan))
            j["relevance_reason"] = info.get("reason", "keyword fallback")
            scored.append(j)

    return scored


def filter_relevant(jobs: list[dict], plan: dict, threshold: Optional[float] = None) -> list[dict]:
    """Score jobs and filter to those above the relevance threshold."""
    if threshold is None:
        threshold = RELEVANCE_THRESHOLD

    if DRY_RUN:
        for j in jobs:
            j["relevance_score"] = _keyword_score(j, plan)
            j["relevance_reason"] = "dry-run keyword match"
        return [j for j in jobs if j["relevance_score"] >= threshold]

    # Try LLM scoring; fall back to keyword on failure
    try:
        scored = _batch_jobs(jobs, plan, AI_BATCH_SIZE)
    except Exception:
        for j in jobs:
            j["relevance_score"] = _keyword_score(j, plan)
            j["relevance_reason"] = "keyword fallback (LLM error)"
        scored = jobs

    return [j for j in scored if j.get("relevance_score", 0) >= threshold]
