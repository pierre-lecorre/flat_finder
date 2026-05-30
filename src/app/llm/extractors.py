"""Structured LLM extraction and validation.

Parses Ollama raw outputs, handles fuzzy JSON recovery, validates against
Pydantic schemas, and retries with errors on schema failure.
"""

from __future__ import annotations

import json
import re
from typing import Optional

from app.llm.ollama_client import OllamaClient
from app.llm.prompts import SYSTEM_PROMPT, build_enrichment_prompt
from app.logging_config import get_logger
from app.models import LLMEnrichmentResponse
from pydantic import ValidationError

logger = get_logger("app.llm.extractors")


def parse_llm_json(text: str) -> Optional[dict]:
    """Attempt to parse string output as JSON, recovering from markdown formatting if needed."""
    if not text:
        return None

    cleaned = text.strip()

    # Try loading directly
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass

    # Try extracting JSON block from markdown ```json ... ```
    match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", cleaned, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(1))
        except json.JSONDecodeError:
            pass

    # Try extracting everything between first '{' and last '}'
    match_braces = re.search(r"(\{.*\})", cleaned, re.DOTALL)
    if match_braces:
        try:
            return json.loads(match_braces.group(1))
        except json.JSONDecodeError:
            pass

    logger.warning("Fuzzy JSON parsing failed to extract a dictionary from response.")
    return None


def enrich_listing_with_llm(
    client: OllamaClient,
    raw_listing_data: dict,
    max_retries: int = 3,
) -> Optional[LLMEnrichmentResponse]:
    """Enrich raw listing dict using Ollama structured generation.

    Retries up to max_retries with validation error feedback injected
    on validation failure.
    """
    prompt = build_enrichment_prompt(raw_listing_data)
    error_feedback = ""

    for attempt in range(1, max_retries + 1):
        full_prompt = prompt
        if error_feedback:
            full_prompt = (
                prompt
                + f"\n\nWARNING: Your previous response was invalid.\n"
                + f"Validation Errors Encountered:\n{error_feedback}\n"
                + "Please correct these mistakes and output a strictly valid JSON object."
            )

        try:
            raw_response = client.generate(full_prompt, system_prompt=SYSTEM_PROMPT)
            parsed_dict = parse_llm_json(raw_response)

            if parsed_dict is None:
                error_feedback = "Raw response could not be parsed as a JSON object."
                continue

            # Validate against Pydantic schema
            response_model = LLMEnrichmentResponse(**parsed_dict)
            logger.info("Successfully validated LLM enrichment response on attempt %d", attempt)
            return response_model

        except ValidationError as val_exc:
            # Format Pydantic errors clearly for feedback loop
            error_feedback = str(val_exc)
            logger.warning(
                "LLM validation failed on attempt %d: %s. Retrying...",
                attempt,
                error_feedback[:200],
            )
        except Exception as exc:
            logger.error("Enrichment execution error: %s", exc)
            break

    logger.error("Failed to enrich listing via LLM after %d attempts.", max_retries)
    return None
