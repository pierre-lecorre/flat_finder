"""Prompt templates and system guidelines for LLM Prague listing analyzer.

Injects listing data details and Pydantic models JSON schema
into the LLM instructions.
"""

from __future__ import annotations

import json
from app.models import LLMEnrichmentResponse

SYSTEM_PROMPT = """You are a highly analytical, professional real-estate assistant specializing in Prague housing.
Your task is to analyze raw scraped real-estate listing text (titles, prices, locations, and descriptions) and produce a structured, high-fidelity JSON object.

CORE RULES:
1. Extract numerical values precisely (e.g. monthly price, utility fees, deposit).
2. Distinguish clearly between private flats and shared flatshares (offer_type = 'flatshare', property_type = 'room').
3. Normalize layouts using Prague standard conventions (1+kk, 1+1, 2+kk, 2+1, room, studio, house).
4. Parse furnished status strictly: 'yes', 'no', 'partially', or 'unknown'.
5. Correctly detect boolean flags (balcony, elevator, parking, pets_allowed) based on obvious mentions.
6. Provide a concise, professional summary (2-3 sentences max) highlighting the key attributes and flat condition.
7. Assess suitability for a working adult:
   - Identify if the listing is heavily student-targeted or "women only" (which should result in validation_status = 'EXCLUDED').
   - Check if there are commute mentions or proximity indicators.
8. Set validation_status enum based on price clarity:
   - VERIFIED: Clear separate rent and utilities price.
   - PRICE_MISMATCH: Listed rent conflicts with description text.
   - UTILITIES_UNCONFIRMED: Utilities price is omitted or unclear.
   - TOTAL_PRICE_UNCONFIRMED: Total price cannot be calculated with confidence.
   - EXCLUDED: Blocked keywords matched (e.g. gender exclusions, invalid listing type, student-only).

You must respond ONLY with a single valid JSON object adhering strictly to the provided JSON Schema.
Do not wrap your response in markdown code blocks or add prefix/suffix conversational text.
"""


def build_enrichment_prompt(raw_data: dict) -> str:
    """Combine raw listing data and inject Pydantic response JSON schema to format prompt."""
    schema = LLMEnrichmentResponse.model_json_schema()

    # Format the input data cleanly for the LLM
    data_str = json.dumps(raw_data, indent=2, ensure_ascii=False)

    prompt = f"""Analyze the following Czech/English real-estate listing data:

--- LISTING DATA START ---
{data_str}
--- LISTING DATA END ---

Extract all fields and return a single JSON object.

Your JSON response must conform strictly to the following JSON Schema:
{json.dumps(schema, indent=2)}

Ensure all numeric fields are float or null, all boolean fields are true/false/null, and enums match the schema exactly.
"""
    return prompt
