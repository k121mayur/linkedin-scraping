"""Relevance scoring via batched LLM calls with keyword fallback."""

from __future__ import annotations

from typing import Optional
import json
from config import DRY_RUN, RELEVANCE_THRESHOLD
from config.ai_config import PROMPT_RELEVANCE_TEMPLATE, AI_BATCH_SIZE
from engine.llm_client import chat_json


def _keyword_score(job: dict, plan: dict) -> float:
    """Fallback keyword-match relevance score. Weighted: role=50%, sector=30%, exp=20%."""
    text = " ".join(str(v).lower() for v in job.values() if isinstance(v, str))
    if not text:
        return 0.0

    def _match_any(kws: list[str]) -> bool:
        for kw in kws:
            kw_lower = kw.lower()
            if kw_lower in text:
                return True
            parts = [w for w in kw_lower.split() if len(w) > 2]
            if parts and any(p in text for p in parts):
                return True
        return False

    score = 0.0
    role_kws = plan.get("role_keywords", [])
    sector_kws = plan.get("sector_keywords", [])
    exp_kws = plan.get("experience_keywords", [])
    exclude_kws = plan.get("exclude_keywords", [])

    if role_kws and _match_any(role_kws):
        score += 0.50
    if sector_kws and _match_any(sector_kws):
        score += 0.30
    if exp_kws and _match_any(exp_kws):
        score += 0.20
    # If no categories matched, give a base score for any keyword hit
    if score == 0.0 and (_match_any(role_kws) or _match_any(sector_kws + exp_kws)):
        score = 0.30

    # Penalize exclude keywords
    for kw in exclude_kws:
        if kw.lower() in text:
            score -= 0.30

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
