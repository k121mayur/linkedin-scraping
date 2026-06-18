"""Parse a natural-language prompt into a structured SearchPlan via LLM + heuristic fallback."""

from __future__ import annotations

from typing import Optional
import json
import re
from config import DRY_RUN, MAX_JOBS_DEFAULT
from config.ai_config import PROMPT_PARSER_TEMPLATE
from engine.llm_client import chat_json

# Known sectors for heuristic fallback
SECTORS = {
    "ngo": ["ngo", "non-profit", "nonprofit", "charity", "foundation", "humanitarian", "social impact"],
    "tech": ["software", "tech", "it", "developer", "engineer", "startup"],
    "finance": ["finance", "banking", "accounting", "investment", "financial"],
    "healthcare": ["healthcare", "medical", "hospital", "pharma", "clinical"],
    "education": ["education", "teaching", "university", "academic", "school"],
    "retail": ["retail", "ecommerce", "e-commerce", "sales"],
    "government": ["government", "public sector", "govt"],
}

# Common Indian locations
INDIAN_LOCATIONS = [
    "India", "Bangalore", "Mumbai", "Delhi", "Hyderabad",
    "Pune", "Chennai", "Kolkata", "Remote", "Gurgaon", "Noida",
]

# Role-to-experience mapping
SENIOR_TERMS = {"senior", "sr.", "sr", "lead", "principal", "head", "director", "vp", "president", "manager"}
JUNIOR_TERMS = {"junior", "jr.", "jr", "entry level", "entry-level", "fresher", "associate", "trainee", "intern"}


def _heuristic_parse(prompt: str, max_jobs: int) -> dict:
    """Keyword-based fallback parser when LLM is unavailable."""
    lower = prompt.lower()

    # Sector detection
    sector = "general"
    sector_keywords = []
    for sname, keywords in SECTORS.items():
        if any(kw in lower for kw in keywords):
            sector = sname
            sector_keywords = keywords
            break

    # Experience level
    if any(t in lower for t in SENIOR_TERMS):
        experience = "senior"
        exp_keywords = list(SENIOR_TERMS & set(lower.split()))
    elif any(t in lower for t in JUNIOR_TERMS):
        experience = "junior"
        exp_keywords = list(JUNIOR_TERMS & set(lower.split()))
    else:
        experience = "any"
        exp_keywords = []

    # Extract role keywords (nouns that look like job titles)
    # Simple: split by commas/and, filter stopwords
    parts = re.split(r"[,;&]|\band\b|\bor\b", prompt)
    role_keywords = []
    stopwords = {"extract", "the", "jobs", "job", "for", "in", "a", "an", "role", "roles", "sector", "level", "find", "search", "get", "list", "scrape", "me", "i", "want", "need", "looking"}
    for part in parts:
        clean = " ".join(w for w in part.strip().lower().split() if w not in stopwords)
        if clean and 2 <= len(clean) <= 40:
            role_keywords.append(clean)
    if not role_keywords:
        role_keywords = ["jobs"]

    # Locations
    locations = [loc for loc in INDIAN_LOCATIONS if loc.lower() in lower]
    if not locations:
        locations = ["India", "Remote"]

    # Exclude keywords
    exclude = []
    if experience == "junior":
        exclude = ["senior", "manager", "director", "lead", "head"]

    return {
        "role_keywords": role_keywords[:5],
        "sector": sector,
        "sector_keywords": sector_keywords,
        "experience_level": experience,
        "experience_keywords": exp_keywords,
        "locations": locations[:5],
        "exclude_keywords": exclude,
        "max_jobs": max_jobs,
    }


def parse(prompt: str, max_jobs: Optional[int] = None) -> dict:
    """Parse a user prompt into a SearchPlan dict.

    Uses LLM when available; falls back to heuristic keyword parsing.
    """
    if max_jobs is None:
        max_jobs = MAX_JOBS_DEFAULT

    if DRY_RUN:
        return _heuristic_parse(prompt, max_jobs)

    try:
        template = PROMPT_PARSER_TEMPLATE.format(prompt=prompt, max_jobs=max_jobs)
        plan = chat_json(template)
        # Ensure required keys
        plan.setdefault("role_keywords", ["jobs"])
        plan.setdefault("sector", "general")
        plan.setdefault("sector_keywords", [])
        plan.setdefault("experience_level", "any")
        plan.setdefault("experience_keywords", [])
        plan.setdefault("locations", ["India", "Remote"])
        plan.setdefault("exclude_keywords", [])
        plan["max_jobs"] = max_jobs
        return plan
    except Exception:
        return _heuristic_parse(prompt, max_jobs)
