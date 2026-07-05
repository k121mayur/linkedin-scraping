import os
from config import LLM_PROVIDER, RELEVANCE_THRESHOLD

# --- Prompt Templates ---

PROMPT_PARSER_TEMPLATE = """
You are a senior LinkedIn recruiter. A non-expert user typed a short, casual job request.
Your job is to understand what they REALLY mean and expand it into a precise search plan.

Think about the role like a domain expert:
- Infer the equivalent and adjacent job TITLES that recruiters actually post for this role.
- Infer the core skills/technologies that define this role.
- Keep everything tightly ON-TOPIC for the user's intent. Do NOT drift into unrelated roles.

Examples of good expansion (titles only):
- "software developer" -> ["software developer", "software engineer", "backend developer", "frontend developer", "full stack developer", "sde"]
- "full stack developer" -> ["full stack developer", "full stack engineer", "mern stack developer", "java full stack developer", "python full stack developer"]
- "finance jobs" -> ["financial analyst", "finance associate", "accountant", "finance manager", "investment analyst"]
- "ui/ux" -> ["ui designer", "ux designer", "ui/ux designer", "product designer", "interaction designer"]

User Prompt: "{prompt}"

Return ONLY a JSON object (no markdown, no commentary) with EXACTLY these keys:
{{
    "role_keywords": ["4-8 concrete job-title search phrases, most specific first; these become LinkedIn searches"],
    "sector": "the primary industry/sector in one or two words (e.g. technology, finance, design, healthcare, general)",
    "sector_keywords": ["synonyms/related terms for the sector; [] if the prompt is purely about a role"],
    "experience_level": "junior | mid | senior | any",
    "experience_keywords": ["phrases that signal this level, e.g. 'entry level', '0-2 years', 'senior'; [] if any"],
    "skills": ["core skills/technologies that define this role"],
    "locations": ["cities/countries the user named; if none given use [\\"India\\", \\"Remote\\"]"],
    "exclude_keywords": ["titles/terms that would make a job clearly OFF-topic or wrong-level for this request"],
    "max_jobs": {max_jobs}
}}
"""

PROMPT_RELEVANCE_TEMPLATE = """
You are a strict, expert technical recruiter. Decide how well each job matches what the user asked for.

The user's request (their own words): "{original_prompt}"
Primary sector: {sector}

Judge each job by its TITLE and DESCRIPTION using real-world understanding of the role
(equivalent titles, the skills/technologies it implies). Be strict:
- 0.9-1.0 = clearly this exact role.
- 0.7-0.89 = the same role family or a strong equivalent/adjacent title.
- 0.4-0.69 = related but a notable mismatch in specialization or level.
- 0.0-0.39 = different role, wrong level, or off-topic.
Penalize jobs whose core function is a DIFFERENT profession from what the user wants.

Jobs to evaluate (JSON):
{jobs_batch}

Return ONLY a JSON list, one object per job, no markdown:
[
    {{ "job_id": "...", "score": 0.85, "reason": "short reason grounded in the title/description" }}
]
"""

GRANT_KEYWORDS_TEMPLATE = """
You are a fundraising expert for NGOs. A user wants to find funding/grant
opportunities posted on LinkedIn. Expand their request into concrete LinkedIn
post-search phrases that funders and intermediaries actually use.

User request: "{prompt}"

Return ONLY a JSON object:
{{
    "keywords": ["5-8 short search phrases, most specific first, e.g. 'grant opportunity NGO', 'call for proposals nonprofit India', 'funding opportunity CSR'"],
    "focus": "one-line summary of what the user wants funded"
}}
"""

GRANT_ANALYSIS_TEMPLATE = """
You are an expert NGO fundraising analyst. Below is the full content gathered
from one LinkedIn post (post text, plus any text read from attached images and
any content fetched from external websites linked in the post).

User's request: "{prompt}"

POST TEXT:
{post_text}

TEXT EXTRACTED FROM IMAGES (may be empty):
{image_text}

CONTENT FROM EXTERNAL WEBSITES LINKED IN THE POST (may be empty):
{external_text}

Decide whether this post announces a real, actionable FUNDING / GRANT
opportunity (grants, calls for proposals, fellowships with funding, CSR
funding, seed funding for NGOs/nonprofits). Score relevance to the user's
request from 0.0 to 1.0. Be accurate: only report facts present in the
content; use "" for anything not stated. Do not invent deadlines or amounts.

Return ONLY a JSON object:
{{
    "is_funding_opportunity": true,
    "relevance_score": 0.85,
    "relevance_reason": "short reason",
    "opportunity_title": "concise title of the opportunity",
    "funder": "organization providing the money",
    "summary": "2-3 sentence factual summary",
    "deadline": "application deadline as stated, else \\"\\"",
    "grant_amount": "amount/range as stated, else \\"\\"",
    "eligibility": "who can apply, else \\"\\"",
    "focus_areas": "comma-separated sectors/themes funded",
    "geography": "countries/regions covered",
    "how_to_apply": "application instructions as stated",
    "application_link": "the URL to apply / learn more, else \\"\\"",
    "contact_email": "contact email if given, else \\"\\""
}}
"""

GRANT_IMAGE_OCR_PROMPT = (
    "Read ALL text visible in the attached image(s) of a LinkedIn post about a "
    "funding/grant opportunity. Transcribe every detail exactly: title, funder, "
    "deadline, amounts, eligibility, application links/emails. Return plain text "
    "only — no commentary. If there is no readable text, return an empty response."
)

# --- AI Constants ---
AI_RELEVANCE_THRESHOLD = RELEVANCE_THRESHOLD
AI_MIN_SCORE = 0.0
AI_MAX_SCORE = 1.0
AI_BATCH_SIZE = 20  # How many jobs to send in one relevance call (fewer round-trips)
