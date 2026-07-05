"""Grants orchestrator — searches LinkedIn *posts* for funding opportunities.

Pipeline per keyword: search posts (content filter) → dedup by post URN and
content hash → enrich (image OCR via vision LLM, external-website fetch) →
LLM analysis into structured grant fields → persist → yield Progress.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser

from config import (
    DRY_RUN,
    GRANT_ANALYZE_IMAGES, GRANT_FOLLOW_LINKS,
    GRANT_MAX_LINKS_PER_POST, GRANT_MAX_IMAGES_PER_POST,
    GRANT_RELEVANCE_THRESHOLD,
)
from config.ai_config import (
    GRANT_KEYWORDS_TEMPLATE, GRANT_ANALYSIS_TEMPLATE, GRANT_IMAGE_OCR_PROMPT,
)
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

_DEFAULT_KEYWORDS = [
    "grant opportunity NGO",
    "funding opportunity nonprofit",
    "call for proposals NGO",
    "grants for NGOs India",
    "CSR funding NGO",
    "seed funding nonprofit",
]


def plan_keywords(prompt: str) -> list[str]:
    """Turn the user's request into LinkedIn post-search phrases (LLM, with a
    dependable default list as fallback)."""
    if not DRY_RUN:
        try:
            result = chat_json(GRANT_KEYWORDS_TEMPLATE.format(prompt=prompt))
            kws = [str(k).strip() for k in result.get("keywords", []) if str(k).strip()]
            if kws:
                return kws[:8]
        except Exception as e:
            _log(f"keyword planning fell back to defaults: {e}")
    # Heuristic: defaults, seeded with the user's own words.
    extra = prompt.strip()
    kws = list(_DEFAULT_KEYWORDS)
    if extra and extra.lower() not in [k.lower() for k in kws]:
        kws.insert(0, extra[:80])
    return kws


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


def fetch_site_text(url: str, max_chars: int = 6000) -> str:
    """Fetch an external page and return its visible text (best effort)."""
    import urllib.request
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                           "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"),
            "Accept": "text/html,application/xhtml+xml",
        })
        with urllib.request.urlopen(req, timeout=20) as resp:
            ctype = resp.headers.get("Content-Type", "")
            if "html" not in ctype and "text" not in ctype:
                return ""
            body = resp.read(1_500_000).decode("utf-8", errors="replace")
    except Exception:
        return ""
    parser = _TextExtractor()
    try:
        parser.feed(body)
    except Exception:
        pass
    text = re.sub(r"\s+", " ", " ".join(parser.chunks))
    return text[:max_chars]


# ── analysis ─────────────────────────────────────────────────

_FUNDING_TERMS = (
    "grant", "funding", "fund", "call for proposals", "cfp", "rfp", "fellowship",
    "apply", "application", "proposal", "donor", "csr", "seed fund", "award",
)
_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
_DEADLINE_RE = re.compile(
    r"(?:deadline|apply by|last date|closes? on|due(?: date)?|before)[:\s]*"
    r"([A-Za-z0-9 ,/-]{4,40}?)(?:\.|\n|$)", re.IGNORECASE)


def _heuristic_analysis(full_text: str, prompt: str) -> dict:
    """Keyword-based fallback when the LLM is unavailable (or in DRY_RUN)."""
    low = (full_text or "").lower()
    hits = sum(1 for t in _FUNDING_TERMS if t in low)
    score = min(1.0, 0.15 + 0.12 * hits) if hits else 0.0
    email = _EMAIL_RE.search(full_text or "")
    deadline = _DEADLINE_RE.search(full_text or "")
    urls = extract_urls(full_text)
    first_line = next((ln.strip() for ln in (full_text or "").split("\n") if ln.strip()), "")
    return {
        "is_funding_opportunity": hits >= 2,
        "relevance_score": round(score, 2),
        "relevance_reason": f"keyword fallback ({hits} funding term(s) matched)",
        "opportunity_title": first_line[:140],
        "funder": "",
        "summary": (full_text or "")[:400],
        "deadline": deadline.group(1).strip() if deadline else "",
        "grant_amount": "",
        "eligibility": "",
        "focus_areas": "",
        "geography": "",
        "how_to_apply": "",
        "application_link": urls[0] if urls else "",
        "contact_email": email.group(0) if email else "",
    }


def analyze_post(post_text: str, image_text: str, external_text: str, prompt: str) -> dict:
    """LLM structured extraction over everything gathered for one post."""
    combined = "\n".join(filter(None, [post_text, image_text, external_text]))
    if DRY_RUN:
        return _heuristic_analysis(combined, prompt)
    try:
        result = chat_json(GRANT_ANALYSIS_TEMPLATE.format(
            prompt=prompt,
            post_text=(post_text or "")[:6000],
            image_text=(image_text or "")[:3000],
            external_text=(external_text or "")[:6000],
        ))
        if isinstance(result, dict) and "relevance_score" in result:
            result["relevance_score"] = float(result.get("relevance_score") or 0.0)
            return result
    except Exception as e:
        _log(f"  ! LLM analysis failed, using heuristic: {e}")
    return _heuristic_analysis(combined, prompt)


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
    for url in urls[:GRANT_MAX_LINKS_PER_POST]:
        t = fetch_site_text(url)
        if t:
            texts.append(f"[{url}]\n{t}")
    return ", ".join(urls), "\n\n".join(texts)


# ── orchestrator ─────────────────────────────────────────────

def run(prompt: str, max_posts: int, run_id=None, should_stop=None):
    """Execute the grants pipeline. Yields Progress, returns the collected list."""
    stopped = should_stop if callable(should_stop) else (lambda: False)

    if run_id is None:
        run_id = db.create_run(prompt, max_posts, run_type="grants")

    collected: list[dict] = []
    seen_urns, seen_hashes = db.seen_grant_keys(run_id)
    examined: set[str] = set()
    attempts = 0

    keywords = plan_keywords(prompt)
    _log(f"Run {run_id} started - target {max_posts} grant posts | "
         f"keywords: {keywords}")

    user_stopped = False

    for keyword in keywords:
        if len(collected) >= max_posts or stopped():
            user_stopped = stopped()
            break
        attempts += 1
        db.log_attempt(run_id, keyword, "", action="grant_posts")
        _log(f"Pass {attempts}: searching posts for {keyword!r} "
             f"({len(collected)}/{max_posts})")

        yield Progress(run_id=run_id, collected=len(collected), target=max_posts,
                       attempts=attempts, current_query=keyword)

        need = max_posts - len(collected)
        try:
            posts = search_posts(keyword, limit=max(need * 2, 10))
        except Exception as e:
            db.log_attempt(run_id, keyword, "", action="grant_posts", error=str(e))
            _log(f"  ! post search failed: {e}")
            continue

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

            image_text = read_post_images(post.get("image_urls", []))
            external_links, external_text = read_external_sites(post.get("text", ""))

            analysis = analyze_post(post.get("text", ""), image_text, external_text, prompt)
            score = float(analysis.get("relevance_score") or 0.0)
            if not analysis.get("is_funding_opportunity") or score < GRANT_RELEVANCE_THRESHOLD:
                continue

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
                "application_link": (analysis.get("application_link", "")
                                     or (external_links.split(", ")[0] if external_links else "")),
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
