"""Grants orchestrator — searches LinkedIn *posts* for funding opportunities.

Pipeline per keyword: search posts (content filter) → dedup by post URN and
content hash → enrich (image OCR via vision LLM, external-website fetch) →
LLM analysis into structured grant fields → persist → yield Progress.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser

from config import (
    DRY_RUN,
    GRANT_ANALYZE_IMAGES, GRANT_FOLLOW_LINKS,
    GRANT_MAX_LINKS_PER_POST, GRANT_MAX_IMAGES_PER_POST,
    GRANT_RELEVANCE_THRESHOLD,
    GRANT_DEFAULT_GEOGRAPHY, GRANT_REQUIRE_INDIA_ELIGIBILITY,
    GRANT_ALLOW_GLOBAL, GRANT_DATE_POSTED,
)
from config.ai_config import (
    GRANT_KEYWORDS_TEMPLATE, GRANT_ANALYSIS_TEMPLATE, GRANT_IMAGE_OCR_PROMPT,
)
from dateutil import parser as date_parser
from engine import database as db
from engine.linkedin_client import search_posts, fetch_image_b64
from engine.llm_client import chat_json, chat_vision
from engine.self_refinement import Progress


def _log(msg: str) -> None:
    # Post text/titles can contain characters a Windows cp1252 console can't
    # encode; degrade to ASCII instead of letting print() kill the run.
    line = f"[grants] {msg}"
    try:
        print(line, flush=True)
    except UnicodeEncodeError:
        print(line.encode("ascii", "replace").decode("ascii"), flush=True)


# ── keyword planning ─────────────────────────────────────────

# Hard wall-clock cap for a single grants run — mirrors the max_attempts=40
# integer cap in self_refinement.py but expressed as elapsed seconds so
# a run with many keywords and slow LLM calls still terminates predictably.
# 8 minutes matches the spec's mitigation-table value.
_GRANTS_MAX_RUN_SECONDS = 480


_DEFAULT_KEYWORDS = [
    "grants for NGOs India",
    "CSR funding India NGO",
    "call for proposals India NGO",
    "grant opportunity India nonprofit",
    "funding opportunity India NGO",
    "seed funding nonprofit India",
]


def clean_keyword(kw: str) -> str:
    """Clean and sanitize search phrases for LinkedIn content search.
    Removes quotes, boolean operators, outdated years, and limits excessive word count.
    """
    # Remove quotation marks, brackets, and colons
    cleaned = re.sub(r'["\'\[\]():]+', ' ', kw)
    # Remove standalone years like 2020-2025
    cleaned = re.sub(r'\b202[0-5]\b', '', cleaned)
    # Remove boolean operators
    cleaned = re.sub(r'\b(AND|OR|NOT)\b', ' ', cleaned)
    # Normalize whitespace
    cleaned = re.sub(r'\s+', ' ', cleaned).strip()
    # Keep at most 4-5 words to avoid over-constraining LinkedIn search
    words = cleaned.split()
    if len(words) > 5:
        cleaned = " ".join(words[:5])
    return cleaned


def plan_keywords(prompt: str) -> list[str]:
    """Turn the user's request into LinkedIn post-search phrases (LLM, with a
    dependable default list as fallback)."""
    if not DRY_RUN:
        try:
            result = chat_json(GRANT_KEYWORDS_TEMPLATE.format(prompt=prompt))
            raw_kws = [str(k).strip() for k in result.get("keywords", []) if str(k).strip()]
            cleaned_kws = [clean_keyword(k) for k in raw_kws if clean_keyword(k)]
            # Deduplicate preserving order
            seen = set()
            dedup_kws = []
            for k in cleaned_kws:
                if k.lower() not in seen:
                    seen.add(k.lower())
                    dedup_kws.append(k)
            if dedup_kws:
                return dedup_kws[:5]
        except Exception as e:
            _log(f"keyword planning fell back to defaults: {e}")
    # Heuristic: defaults, seeded with the user's own words.
    extra = clean_keyword(prompt.strip())
    if extra and "india" not in extra.lower() and GRANT_REQUIRE_INDIA_ELIGIBILITY:
        extra = f"{extra} India"
    kws = list(_DEFAULT_KEYWORDS)
    if extra and extra.lower() not in [k.lower() for k in kws]:
        kws.insert(0, extra[:80])
    return kws[:5]


# ── helpers ──────────────────────────────────────────────────

def content_hash(text: str) -> str:
    """Stable hash of the normalized post text — catches reposts under new URNs."""
    norm = re.sub(r"\s+", " ", (text or "").lower()).strip()
    return hashlib.sha256(norm.encode()).hexdigest()


_REL_UNITS = {
    "m": "minutes", "h": "hours", "d": "days", "w": "weeks", "mo": "months", "yr": "years",
}


def normalize_posted(rel: str) -> str:
    """Turn LinkedIn's relative stamp ('2w', '3d', '1mo') into an absolute ISO date."""
    rel = (rel or "").strip().lower()
    m = re.match(r"(\d+)\s*(mo|yr|[mhdw])", rel)
    if not m:
        return ""
    n, unit = int(m.group(1)), m.group(2)
    now = datetime.now(timezone.utc)
    if unit == "mo":
        dt = now - timedelta(days=30 * n)
    elif unit == "yr":
        dt = now - timedelta(days=365 * n)
    elif unit == "w":
        dt = now - timedelta(weeks=n)
    elif unit == "d":
        dt = now - timedelta(days=n)
    elif unit == "h":
        dt = now - timedelta(hours=n)
    else:  # minutes
        dt = now - timedelta(minutes=n)
    return dt.date().isoformat()


_URL_RE = re.compile(r"https?://[^\s\"'<>)\]]+", re.IGNORECASE)


def extract_urls(text: str) -> list[str]:
    """External URLs mentioned in the post text (LinkedIn-internal links skipped;
    lnkd.in short links kept — they redirect to the external site)."""
    urls = []
    for u in _URL_RE.findall(text or ""):
        u = u.rstrip(".,;:!?")
        host = re.sub(r"^https?://(www\.)?", "", u.lower()).split("/")[0]
        if host.endswith("linkedin.com"):
            continue
        if u not in urls:
            urls.append(u)
    return urls


class _TextExtractor(HTMLParser):
    _SKIP = {"script", "style", "noscript", "svg", "head"}

    def __init__(self):
        super().__init__()
        self._skip_depth = 0
        self.chunks: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag in self._SKIP:
            self._skip_depth += 1

    def handle_endtag(self, tag):
        if tag in self._SKIP and self._skip_depth:
            self._skip_depth -= 1

    def handle_data(self, data):
        if not self._skip_depth and data.strip():
            self.chunks.append(data.strip())


def fetch_site_text(url: str, max_chars: int = 6000) -> tuple[str, str]:
    """Fetch an external page and return (resolved_url, visible_text) (best effort)."""
    import urllib.request
    final_url = url
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                           "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"),
            "Accept": "text/html,application/xhtml+xml",
        })
        with urllib.request.urlopen(req, timeout=20) as resp:
            final_url = resp.geturl() or url
            ctype = resp.headers.get("Content-Type", "")
            if "html" not in ctype and "text" not in ctype:
                return final_url, ""
            body = resp.read(1_500_000).decode("utf-8", errors="replace")
    except Exception:
        return final_url, ""
    parser = _TextExtractor()
    try:
        parser.feed(body)
    except Exception:
        pass
    text = re.sub(r"\s+", " ", " ".join(parser.chunks))
    return final_url, text[:max_chars]


# ── analysis ─────────────────────────────────────────────────

_FUNDING_TERMS = (
    "grant", "funding", "fund", "call for proposals", "cfp", "rfp", "fellowship",
    "apply", "application", "proposal", "donor", "csr", "seed fund", "award",
)
_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
_DEADLINE_RE = re.compile(
    r"(?:deadline|apply by|last date|closes? on|due(?: date)?|before)[:\s]*"
    r"([A-Za-z0-9 ,/-]{4,40}?)(?:\.|\n|$)", re.IGNORECASE)

_EXCLUSION_PATTERNS = [
    re.compile(r"\b501\(c\)\(3\)\s*(?:only|status\s+required|required|organizations?\s+only)\b", re.IGNORECASE),
    re.compile(r"\b(?:us|usa|united states)\s+(?:only|citizens?\s+only|applicants?\s+only|entities\s+only|nonprofits?\s+only)\b", re.IGNORECASE),
    re.compile(r"\b(?:uk|united kingdom)\s+(?:only|registered\s+charit(?:y|ies)\s+only)\b", re.IGNORECASE),
    re.compile(r"\b(?:sub-saharan\s+africa|african\s+countries|latin\s+america)\s+only\b", re.IGNORECASE),
    re.compile(r"\bfor\s+(?:us|uk|canadian|australian)\s+(?:nonprofits?|charities|ngos)\s+only\b", re.IGNORECASE),
]

_INDIA_SIGNALS = {
    "india", "indian", "csr", "fcra", "80g", "12a", "12ab", "delhi", "mumbai", "bangalore",
    "bengaluru", "hyderabad", "chennai", "pune", "kolkata", "ahmedabad", "noida",
    "gurgaon", "gurugram", "maharashtra", "karnataka", "tamil nadu", "uttar pradesh",
    "bihar", "rajasthan", "gujarat", "kerala", "telangana", "andhra", "madhya pradesh",
    "west bengal", "odisha", "assam", "jharkhand", "niti aayog", "darpan", "section 8",
}

_GLOBAL_SIGNALS = {
    "global", "worldwide", "international", "any country", "open globally",
    "all countries", "developing countries", "global south", "south asia",
}


def is_ineligible_geography(text: str) -> bool:
    """Fast pre-filter: returns True if the text is explicitly restricted to a non-Indian region."""
    if not GRANT_REQUIRE_INDIA_ELIGIBILITY:
        return False
    low = (text or "").lower()
    # If India is explicitly mentioned, do not reject at the pre-filter stage
    if "india" in low or "indian" in low:
        return False
    for pat in _EXCLUSION_PATTERNS:
        if pat.search(low):
            return True
    return False


def is_geography_relevant(analysis: dict, post_text: str = "") -> bool:
    """Validate whether the post and its analysis represent an India-eligible opportunity."""
    if not GRANT_REQUIRE_INDIA_ELIGIBILITY:
        return True

    geo = (analysis.get("geography") or "").lower()
    combined = f"{geo} {(analysis.get('eligibility') or '')} {(analysis.get('summary') or '')} {post_text}".lower()

    # Direct India signals
    if any(sig in combined for sig in _INDIA_SIGNALS):
        return True

    # If global / international is permitted
    if GRANT_ALLOW_GLOBAL and any(sig in geo or sig in combined for sig in _GLOBAL_SIGNALS):
        if not any(pat.search(combined) for pat in _EXCLUSION_PATTERNS):
            return True

    # If LLM extracted India explicitly in geography
    if "india" in geo or "south asia" in geo:
        return True

    return False


_MONTH_NAMES = ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "sept", "oct", "nov", "dec")
_DATE_NUM_RE = re.compile(r"\b(?:\d{1,2}[/-]\d{1,2}[/-]\d{2,4}|\d{4}[/-]\d{1,2}[/-]\d{1,2})\b")
_ROLLING_TERMS = ("rolling", "ongoing", "open until", "open-ended", "always open", "continuous", "n/a", "none", "tbd")


def is_deadline_expired(deadline_str: str) -> bool:
    """Check if an extracted deadline date has already passed relative to today.
    Returns True ONLY if a definitive date in the past was identified.
    Returns False for open-ended / rolling deadlines or unparseable text.
    """
    if not deadline_str:
        return False
    clean = deadline_str.strip()
    low = clean.lower()
    if any(term in low for term in _ROLLING_TERMS):
        return False

    has_month = any(m in low for m in _MONTH_NAMES)
    has_date_num = bool(_DATE_NUM_RE.search(clean))
    if not (has_month or has_date_num):
        return False

    today = datetime.now(timezone.utc).date()
    try:
        dt = date_parser.parse(clean, fuzzy=True)
        return dt.date() < today
    except Exception:
        return False


def detect_date_filter(prompt: str, explicit: str | None = None) -> str:
    """Determine the datePosted LinkedIn search facet from user parameter or prompt text."""
    if explicit and explicit.strip():
        return explicit.strip().lower()
    low = (prompt or "").lower()
    if any(k in low for k in ("24h", "24 hours", "24 hour", "today", "yesterday", "past day", "last 24")):
        return "past-24h"
    if any(k in low for k in ("past week", "last week", "this week", "past 7 days", "7 days")):
        return "past-week"
    if any(k in low for k in ("past month", "last month", "this month", "past 30 days", "30 days")):
        return "past-month"
    return GRANT_DATE_POSTED


def _heuristic_analysis(full_text: str, prompt: str, profile: str = "") -> dict:
    """Keyword-based fallback when the LLM is unavailable (or in DRY_RUN)."""
    low = (full_text or "").lower()
    hits = sum(1 for t in _FUNDING_TERMS if t in low)
    india_hits = sum(1 for t in _INDIA_SIGNALS if t in low)

    if is_ineligible_geography(full_text):
        return {
            "is_funding_opportunity": False,
            "relevance_score": 0.0,
            "relevance_reason": "ineligible geography (restricted to non-India region)",
            "opportunity_title": "",
            "funder": "",
            "summary": (full_text or "")[:400],
            "deadline": "",
            "grant_amount": "",
            "eligibility": "",
            "focus_areas": "",
            "geography": "Excluded (non-India)",
            "how_to_apply": "",
            "application_link": "",
            "contact_email": "",
        }

    email = _EMAIL_RE.search(full_text or "")
    deadline = _DEADLINE_RE.search(full_text or "")
    deadline_val = deadline.group(1).strip() if deadline else ""

    if is_deadline_expired(deadline_val):
        return {
            "is_funding_opportunity": False,
            "relevance_score": 0.0,
            "relevance_reason": f"deadline expired ({deadline_val})",
            "opportunity_title": "",
            "funder": "",
            "summary": (full_text or "")[:400],
            "deadline": deadline_val,
            "grant_amount": "",
            "eligibility": "",
            "focus_areas": "",
            "geography": "",
            "how_to_apply": "",
            "application_link": "",
            "contact_email": "",
        }

    score = min(1.0, 0.15 + 0.12 * hits + (0.15 if india_hits else 0.0)) if hits else 0.0
    urls = extract_urls(full_text)
    first_line = next((ln.strip() for ln in (full_text or "").split("\n") if ln.strip()), "")
    geo = "India" if india_hits else ("Global" if any(g in low for g in _GLOBAL_SIGNALS) else "")
    return {
        "is_funding_opportunity": hits >= 2,
        "relevance_score": round(score, 2),
        "relevance_reason": f"keyword fallback ({hits} funding term(s), {india_hits} India signal(s) matched)",
        "opportunity_title": first_line[:140],
        "funder": "",
        "summary": (full_text or "")[:400],
        "deadline": deadline_val,
        "grant_amount": "",
        "eligibility": "",
        "focus_areas": "",
        "geography": geo,
        "how_to_apply": "",
        "application_link": urls[0] if urls else "",
        "contact_email": email.group(0) if email else "",
    }


def analyze_post(post_text: str, image_text: str, external_text: str, prompt: str,
                 profile: str = "") -> dict:
    """LLM structured extraction over everything gathered for one post.

    ``profile`` is an optional organisation-profile document (plain text / markdown).
    When provided it is included in the LLM payload so the model can judge fit;
    when absent the pipeline behaves exactly as before — strictly additive.
    """
    combined = "\n".join(filter(None, [post_text, image_text, external_text]))
    if DRY_RUN:
        return _heuristic_analysis(combined, prompt, profile)
    try:
        current_date = datetime.now(timezone.utc).strftime("%d %B %Y")
        result = chat_json(GRANT_ANALYSIS_TEMPLATE.format(
            prompt=prompt,
            current_date=current_date,
            profile=(profile or "")[:4000],
            post_text=(post_text or "")[:6000],
            image_text=(image_text or "")[:3000],
            external_text=(external_text or "")[:6000],
        ))
        if isinstance(result, dict) and "relevance_score" in result:
            result["relevance_score"] = float(result.get("relevance_score") or 0.0)
            return result
    except Exception as e:
        _log(f"  ! LLM analysis failed, using heuristic: {e}")
    return _heuristic_analysis(combined, prompt, profile)


def read_post_images(image_urls: list[str]) -> str:
    """OCR the post's attached images through the vision LLM. Empty on failure."""
    if DRY_RUN or not GRANT_ANALYZE_IMAGES or not image_urls:
        return ""
    b64s = []
    for url in image_urls[:GRANT_MAX_IMAGES_PER_POST]:
        b64 = fetch_image_b64(url)
        if b64:
            b64s.append(b64)
    if not b64s:
        return ""
    try:
        text = chat_vision(GRANT_IMAGE_OCR_PROMPT, b64s)
        return (text or "").strip()[:4000]
    except Exception as e:
        _log(f"  ! image OCR failed: {e}")
        return ""


def read_external_sites(post_text: str) -> tuple[str, str]:
    """Fetch external sites linked in the post. Returns (joined_urls, site_text)."""
    urls = extract_urls(post_text)
    if not urls:
        return "", ""
    if DRY_RUN or not GRANT_FOLLOW_LINKS:
        return ", ".join(urls), ""
    texts = []
    resolved_urls = []
    for url in urls[:GRANT_MAX_LINKS_PER_POST]:
        final_url, t = fetch_site_text(url)
        resolved_urls.append(final_url or url)
        if t:
            texts.append(f"[{final_url or url}]\n{t}")
    all_urls = resolved_urls + urls[len(resolved_urls):]
    dedup = []
    for u in all_urls:
        if u and u not in dedup:
            dedup.append(u)
    return ", ".join(dedup), "\n\n".join(texts)


# ── orchestrator ─────────────────────────────────────────────

def run(prompt: str, max_posts: int, run_id=None, should_stop=None, profile: str = "",
        date_posted: str | None = None):
    """Execute the grants pipeline. Yields Progress, returns the collected list.

    ``profile`` is an optional organisation-profile document forwarded verbatim to
    ``analyze_post`` for every post in this run. Omitting it keeps behaviour
    identical to the current production default (prompt-only evaluation).
    """
    stopped = should_stop if callable(should_stop) else (lambda: False)

    if run_id is None:
        run_id = db.create_run(prompt, max_posts, run_type="grants")

    collected: list[dict] = []
    seen_urns, seen_hashes = db.seen_grant_keys(run_id)
    examined: set[str] = set()
    attempts = 0

    active_date_posted = detect_date_filter(prompt, date_posted)
    keywords = plan_keywords(prompt)
    _log(f"Run {run_id} started - target {max_posts} grant posts | "
         f"date filter: {active_date_posted} | keywords: {keywords}")

    user_stopped = False

    timed_out = False
    run_start = time.monotonic()

    for keyword in keywords:
        elapsed = time.monotonic() - run_start
        if elapsed >= _GRANTS_MAX_RUN_SECONDS:
            _log(f"Wall-clock timeout ({_GRANTS_MAX_RUN_SECONDS}s) reached after "
                 f"{elapsed:.0f}s — stopping gracefully with {len(collected)} result(s).")
            timed_out = True
            break
        if len(collected) >= max_posts or stopped():
            user_stopped = stopped()
            break
        attempts += 1
        db.log_attempt(run_id, keyword, "", action="grant_posts")
        _log(f"Pass {attempts}: searching posts for {keyword!r} (filter: {active_date_posted}) "
             f"({len(collected)}/{max_posts})")

        yield Progress(run_id=run_id, collected=len(collected), target=max_posts,
                       attempts=attempts, current_query=keyword)

        need = max_posts - len(collected)
        try:
            posts = search_posts(keyword, limit=max(need * 2, 10), date_posted=active_date_posted)
        except Exception as e:
            db.log_attempt(run_id, keyword, "", action="grant_posts", error=str(e))
            _log(f"  ! post search failed: {e}")
            continue

        # If a narrow query returned 0 posts (common with past-24h), retry with a broader 2-3 word query
        if len(posts) == 0 and len(keyword.split()) > 3:
            simplified = " ".join(keyword.split()[:3])
            if "india" not in simplified.lower() and GRANT_REQUIRE_INDIA_ELIGIBILITY:
                simplified += " India"
            _log(f"  ! 0 posts for {keyword!r}; retrying with broader query: {simplified!r}")
            try:
                broader_posts = search_posts(simplified, limit=max(need * 2, 10), date_posted=active_date_posted)
                if broader_posts:
                    posts = broader_posts
            except Exception:
                pass

        _log(f"  found {len(posts)} post(s)")

        relevant_count = 0
        for post in posts:
            if stopped():
                user_stopped = True
                break
            if len(collected) >= max_posts:
                break

            urn = post["post_urn"]
            chash = content_hash(post.get("text", ""))
            # Duplicates ignored by unique post URN and by content hash (reposts).
            if urn in examined or urn in seen_urns or (post.get("text") and chash in seen_hashes):
                continue
            examined.add(urn)

            # Fast geographic pre-filter (skip non-India posts before vision/site enrichment)
            if is_ineligible_geography(post.get("text", "")):
                _log(f"    - skipping post {urn[:25]}... (ineligible non-India geography)")
                continue

            image_text = read_post_images(post.get("image_urls", []))
            external_links, external_text = read_external_sites(post.get("text", ""))

            analysis = analyze_post(post.get("text", ""), image_text, external_text, prompt,
                                     profile=profile)
            score = float(analysis.get("relevance_score") or 0.0)
            if not analysis.get("is_funding_opportunity") or score < GRANT_RELEVANCE_THRESHOLD:
                continue

            # Programmatic deadline expiration check
            deadline = analysis.get("deadline", "").strip()
            if is_deadline_expired(deadline):
                _log(f"    - skipping post {urn[:25]}... (deadline '{deadline}' has expired)")
                continue

            # Post-analysis geographic verification
            if not is_geography_relevant(analysis, post.get("text", "")):
                _log(f"    - skipping post {urn[:25]}... (geography '{analysis.get('geography')}' not eligible for India)")
                continue

            # Resolve unshortened application link
            app_link = (analysis.get("application_link", "") or "").strip()
            if not app_link or "lnkd.in" in app_link:
                first_resolved = external_links.split(", ")[0].strip() if external_links else ""
                if first_resolved and "lnkd.in" not in first_resolved:
                    app_link = first_resolved

            grant = {
                "post_urn": urn,
                "content_hash": chash,
                "post_url": post.get("post_url", ""),
                "author": post.get("author", ""),
                "author_url": post.get("author_url", ""),
                "posted_date": post.get("posted", ""),
                "posted_date_normalized": normalize_posted(post.get("posted", "")),
                "opportunity_title": analysis.get("opportunity_title", ""),
                "funder": analysis.get("funder", ""),
                "summary": analysis.get("summary", ""),
                "deadline": analysis.get("deadline", ""),
                "grant_amount": analysis.get("grant_amount", ""),
                "eligibility": analysis.get("eligibility", ""),
                "focus_areas": analysis.get("focus_areas", ""),
                "geography": analysis.get("geography", ""),
                "how_to_apply": analysis.get("how_to_apply", ""),
                "application_link": app_link or (external_links.split(", ")[0] if external_links else ""),
                "external_links": external_links,
                "contact_email": analysis.get("contact_email", ""),
                "post_text": post.get("text", ""),
                "image_text": image_text,
                "external_site_summary": external_text[:2000],
                "relevance_score": score,
                "relevance_reason": analysis.get("relevance_reason", ""),
                "keyword": keyword,
            }

            db.upsert_grant(grant, run_id, prompt)
            collected.append(grant)
            seen_urns.add(urn)
            seen_hashes.add(chash)
            relevant_count += 1

            _log(f"  + saved {len(collected)}/{max_posts}: "
                 f"{(grant.get('opportunity_title') or 'Untitled')[:70]}")

            yield Progress(run_id=run_id, collected=len(collected), target=max_posts,
                           attempts=attempts, current_query=keyword)

        db.log_attempt(run_id, keyword, "", action="grant_posts",
                       cards=len(posts), relevant=relevant_count)
        if user_stopped:
            break

    if user_stopped:
        status = "stopped"
    elif timed_out:
        status = "partial"
    elif len(collected) >= max_posts:
        status = "completed"
    else:
        status = "partial"
    db.finish_run(run_id, status=status, jobs_found=len(collected))
    _log(f"Run {run_id} {status} - {len(collected)}/{max_posts} grant posts "
         f"in {attempts} pass(es)")
    # Browser deliberately left open for the next run (warm session reuse);
    # linkedin_client._ensure_auth() revalidates and relaunches if it died.

    yield Progress(run_id=run_id, collected=len(collected), target=max_posts,
                   attempts=attempts, status=status)
    return collected
