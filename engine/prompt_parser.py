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

# Heuristic role expansion: common layperson terms -> related job-title search variants.
# Used only when the LLM is unavailable, so the fallback search is still broad and on-topic.
ROLE_EXPANSIONS = {
    "software developer": ["software developer", "software engineer", "backend developer", "frontend developer", "full stack developer"],
    "software engineer": ["software engineer", "software developer", "backend engineer", "frontend engineer", "sde"],
    "full stack developer": ["full stack developer", "full stack engineer", "mern stack developer", "java full stack developer", "python full stack developer"],
    "full stack": ["full stack developer", "full stack engineer", "mern stack developer", "software engineer"],
    "backend developer": ["backend developer", "backend engineer", "software engineer", "api developer"],
    "frontend developer": ["frontend developer", "frontend engineer", "react developer", "ui developer"],
    "data scientist": ["data scientist", "machine learning engineer", "data analyst", "ai engineer"],
    "data analyst": ["data analyst", "business analyst", "data scientist", "bi analyst"],
    "finance": ["financial analyst", "finance associate", "accountant", "finance manager", "investment analyst"],
    "finance jobs": ["financial analyst", "finance associate", "accountant", "finance manager", "investment analyst"],
    "accountant": ["accountant", "accounts executive", "financial accountant", "audit associate"],
    "ui/ux": ["ui designer", "ux designer", "ui/ux designer", "product designer", "interaction designer"],
    "ui ux": ["ui designer", "ux designer", "ui/ux designer", "product designer"],
    "designer": ["ui designer", "ux designer", "graphic designer", "product designer"],
    "marketing": ["digital marketing", "marketing executive", "marketing manager", "content marketing"],
    "hr": ["hr executive", "human resources", "talent acquisition", "recruiter"],
}


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

    # Expand each extracted role into related title variants when we recognize it,
    # so even the no-LLM fallback searches broadly and on-topic.
    expanded: list[str] = []
    for rk in role_keywords:
        expanded.append(rk)
        for key, variants in ROLE_EXPANSIONS.items():
            if key in rk or rk in key:
                expanded.extend(variants)
                break
    # Dedup preserving order
    seen_rk = set()
    role_keywords = []
    for rk in expanded:
        if rk not in seen_rk:
            seen_rk.add(rk)
            role_keywords.append(rk)

    # Locations
    locations = [loc for loc in INDIAN_LOCATIONS if loc.lower() in lower]
    if not locations:
        locations = ["India", "Remote"]

    # Exclude keywords
    exclude = []
    if experience == "junior":
        exclude = ["senior", "manager", "director", "lead", "head"]

    return {
        "role_keywords": role_keywords[:8],
        "sector": sector,
        "sector_keywords": sector_keywords,
        "experience_level": experience,
        "experience_keywords": exp_keywords,
        "skills": [],
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
        if not isinstance(plan, dict):
            raise ValueError("parser did not return a JSON object")
        # Ensure required keys
        plan.setdefault("role_keywords", None)
        if not plan.get("role_keywords"):
            # LLM gave nothing usable — fall back to heuristic role extraction.
            plan["role_keywords"] = _heuristic_parse(prompt, max_jobs)["role_keywords"]
        plan.setdefault("sector", "general")
        plan.setdefault("sector_keywords", [])
        plan.setdefault("experience_level", "any")
        plan.setdefault("experience_keywords", [])
        plan.setdefault("skills", [])
        if not plan.get("locations"):
            plan["locations"] = ["India", "Remote"]
        plan.setdefault("exclude_keywords", [])
        # Normalize: drop empties, dedup, keep it focused.
        plan["role_keywords"] = _dedup_clean(plan["role_keywords"])[:8]
        plan["sector_keywords"] = _dedup_clean(plan.get("sector_keywords", []))
        plan["skills"] = _dedup_clean(plan.get("skills", []))
        plan["locations"] = _dedup_clean(plan["locations"])[:5]
        plan["exclude_keywords"] = _dedup_clean(plan.get("exclude_keywords", []))
        plan["max_jobs"] = max_jobs
        return plan
    except Exception:
        return _heuristic_parse(prompt, max_jobs)


def _dedup_clean(items) -> list[str]:
    """Lowercase-dedup a list of strings, dropping blanks, preserving order."""
    if not isinstance(items, list):
        return []
    seen = set()
    out = []
    for it in items:
        s = str(it).strip()
        if not s:
            continue
        key = s.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(s)
    return out
