"""
Gemini AI Service Module
~~~~~~~~~~~~~~~~~~~~~~~~~
Handles all Google Gemini AI interactions for the College Management System.
Provides a clean, reusable interface for AI-powered features like
leave application extraction.
"""

import json
import logging
import re

from django.conf import settings

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Custom Exceptions
# ---------------------------------------------------------------------------

class GeminiError(Exception):
    """Base exception for Gemini-related errors."""
    pass


class GeminiConfigError(GeminiError):
    """Raised when Gemini API is not properly configured."""
    pass


class GeminiAPIError(GeminiError):
    """Raised when the Gemini API call itself fails."""
    pass


class GeminiParseError(GeminiError):
    """Raised when the AI response cannot be parsed into structured data."""
    pass


# ---------------------------------------------------------------------------
# Gemini Client Setup
# ---------------------------------------------------------------------------

def _get_gemini_model():
    """
    Lazily configure and return a Gemini GenerativeModel instance.
    Raises GeminiConfigError if the API key is missing or the package is
    not installed.
    """
    try:
        import google.generativeai as genai
    except ImportError:
        raise GeminiConfigError(
            "The 'google-generativeai' package is not installed. "
            "Run: pip install google-generativeai"
        )

    api_key = getattr(settings, 'GEMINI_API_KEY', '') or ''
    if not api_key:
        raise GeminiConfigError(
            "GEMINI_API_KEY is not set. Please add it to your .env file."
        )

    genai.configure(api_key=api_key)
    return genai.GenerativeModel('gemini-flash-lite-latest')


# ---------------------------------------------------------------------------
# System Prompt
# ---------------------------------------------------------------------------

LEAVE_EXTRACTION_PROMPT = """You are a smart assistant for a **College Management System**. \
Your job is to extract structured leave-application details from a student's \
natural-language request.

### Rules
1. Extract **date** (or date range) and **reason** for the leave.
2. If the student mentions relative dates like "tomorrow", "next Monday", \
   or "day after tomorrow", resolve them relative to today's date which will \
   be provided below.
3. Always format dates as **YYYY-MM-DD**. For a range use \
   "YYYY-MM-DD to YYYY-MM-DD".
4. If no clear reason is stated, set reason to "Personal".
5. Respond with **only** valid JSON — no markdown fences, no extra text.

### Output Format (strict JSON)
{"date": "<date or date range>", "reason": "<concise reason>"}

### Examples
| Student says | You respond |
|---|---|
| "I need leave on 25th March 2026 for a doctor's appointment" | {"date": "2026-03-25", "reason": "Doctor's appointment"} |
| "Leave from 1 April to 5 April because of a family function" | {"date": "2026-04-01 to 2026-04-05", "reason": "Family function"} |
| "I won't come tomorrow, feeling sick" | {"date": "<resolved date>", "reason": "Feeling sick"} |
| "Need a day off next Monday" | {"date": "<resolved date>", "reason": "Personal"} |
"""


def _clean_json_response(text: str) -> str:
    """Strip markdown code fences and whitespace from the AI response."""
    text = text.strip()
    # Remove ```json ... ``` or ``` ... ```
    text = re.sub(r'^```(?:json)?\s*', '', text)
    text = re.sub(r'\s*```$', '', text)
    return text.strip()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def extract_leave_details(user_prompt: str) -> dict:
    """
    Send the student's natural-language prompt to Gemini and return
    structured leave details.

    Returns:
        dict with keys 'date' and 'reason'.

    Raises:
        GeminiConfigError  – API key missing or package not installed.
        GeminiAPIError     – Gemini API call failed.
        GeminiParseError   – Could not parse the AI response as JSON.
    """
    if not user_prompt or not user_prompt.strip():
        raise GeminiParseError("The leave prompt cannot be empty.")

    model = _get_gemini_model()

    from datetime import date
    today = date.today().isoformat()

    full_prompt = (
        f"{LEAVE_EXTRACTION_PROMPT}\n"
        f"Today's date: {today}\n\n"
        f"Student's request: {user_prompt}"
    )

    try:
        response = model.generate_content(full_prompt)
        raw_text = response.text
    except Exception as exc:
        logger.exception("Gemini API call failed")
        raise GeminiAPIError(f"AI service error: {exc}") from exc

    cleaned = _clean_json_response(raw_text)

    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError:
        logger.error("Failed to parse Gemini response: %s", cleaned)
        raise GeminiParseError(
            "Could not understand the response from AI. "
            "Please rephrase your leave request with a clear date and reason."
        )

    # Validate expected keys
    leave_date = data.get('date', '').strip()
    reason = data.get('reason', '').strip()

    if not leave_date:
        raise GeminiParseError(
            "AI could not identify a date in your request. "
            "Please mention a specific date or date range."
        )

    return {
        'date': leave_date,
        'reason': reason or 'Personal',
    }
