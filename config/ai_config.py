import os
from config import LLM_PROVIDER, RELEVANCE_THRESHOLD

# --- Prompt Templates ---

PROMPT_PARSER_TEMPLATE = """
You are a LinkedIn search expert. Convert the user's natural language prompt into a structured search plan.
Return ONLY a JSON object.

User Prompt: "{prompt}"

Required JSON structure:
{{
    "role_keywords": ["list of specific job titles to search for"],
    "sector": "the primary industry/sector",
    "sector_keywords": ["synonyms for the sector, e.g., NGO, non-profit, charity"],
    "experience_level": "junior | mid | senior | any",
    "experience_keywords": ["phrases that indicate this level, e.g., '0-2 years', 'entry level'],",
    "locations": ["list of cities or countries to search in"],
    "exclude_keywords": ["terms that would disqualify a job, e.g., 'senior' for a junior role"],
    "max_jobs": {max_jobs}
}}
"""

PROMPT_RELEVANCE_TEMPLATE = """
You are an HR expert specializing in the {sector} sector. 
Score the following LinkedIn job descriptions based on their relevance to the user's request: "{original_prompt}".

For each job, provide a relevance score from 0.0 to 1.0 and a brief reason.

Jobs to evaluate:
{jobs_batch}

Return ONLY a JSON list of objects:
[
    {{ "job_id": "...", "score": 0.85, "reason": "Matches keywords X and Y" }},
    ...
]
"""

# --- AI Constants ---
AI_RELEVANCE_THRESHOLD = RELEVANCE_THRESHOLD
AI_MIN_SCORE = 0.0
AI_MAX_SCORE = 1.0
AI_BATCH_SIZE = 10  # How many jobs to send in one relevance call
