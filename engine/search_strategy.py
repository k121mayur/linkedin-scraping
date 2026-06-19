"""Build and manage an ordered queue of (query, location) search pairs from a SearchPlan."""

from __future__ import annotations

from dataclasses import dataclass, field

FALLBACK_LOCATIONS = ["India", "Remote", "Worldwide", "United States"]


@dataclass
class SearchItem:
    query: str
    location: str
    action: str = "seed"  # seed | broaden_query | widen_location | relax_filters


def build_queue(plan: dict) -> list[SearchItem]:
    """Primary queue: each role variant × each location.

    Role keywords are already specific, expanded title variants (e.g. "software
    developer", "software engineer", "full stack developer"), so we search them
    plainly — LinkedIn's keyword search handles plain titles far better than
    Boolean sector phrases, and relevance scoring (which sees the original
    prompt, including any sector) filters off-topic results afterwards.
    """
    roles = plan.get("role_keywords", ["jobs"])
    locations = plan.get("locations", ["India"])

    items = []
    for role in roles:
        for loc in locations:
            items.append(SearchItem(role, loc))
    return items


def build_relaxed_queue(plan: dict, attempts: int = 0) -> list[SearchItem]:
    """Broader fallback queue when primary combos are exhausted.

    Strategies tried in order as attempts increase:
    1. Drop sector keywords (pure role search)
    2. Expand to fallback locations
    3. Search with just role, no location
    4. Very broad single-word query
    """
    roles = plan.get("role_keywords", ["jobs"])
    locations = plan.get("locations", ["India"])
    exp_kws = plan.get("experience_keywords", [])

    items = []

    if attempts <= 3:
        # Drop sector: role only, original locations
        for role in roles:
            for loc in locations:
                items.append(SearchItem(role, loc, "broaden_query"))

    if attempts <= 6:
        # Use fallback locations
        for role in roles:
            for loc in FALLBACK_LOCATIONS:
                if loc not in locations:
                    items.append(SearchItem(role, loc, "widen_location"))

    if attempts <= 9:
        # No location filter
        for role in roles:
            items.append(SearchItem(role, "", "widen_location"))

    # Very broad: single keyword
    if roles:
        broad = roles[0].split()[0] if " " in roles[0] else roles[0]
        items.append(SearchItem(broad, "", "relax_filters"))

    # With experience keywords
    if exp_kws:
        for ek in exp_kws[:2]:
            items.append(SearchItem(f'"{ek}"', "", "relax_filters"))

    return items


def build_all(plan: dict) -> list[SearchItem]:
    """Primary + relaxed queue combined, for the orchestrator to consume."""
    primary = build_queue(plan)
    relaxed = build_relaxed_queue(plan)
    return primary + relaxed
