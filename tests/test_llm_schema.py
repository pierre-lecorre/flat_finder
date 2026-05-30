"""Unit tests for Pydantic LLM response models and schemas."""

from __future__ import annotations

import pytest
from app.models import LLMEnrichmentResponse, ValidationStatus
from pydantic import ValidationError


def test_llm_response_validation() -> None:
    # Full valid mapping
    data = {
        "property_type": "flat",
        "offer_type": "rent",
        "price_value": 20000.0,
        "currency": "CZK",
        "fees_value": 4000.0,
        "total_monthly_cost": 24000.0,
        "deposit_value": 20000.0,
        "district": "Libeň",
        "city": "Prague",
        "address": "Sokolovská 123",
        "layout": "2+kk",
        "area_m2": 50.0,
        "furnished_status": "yes",
        "balcony": True,
        "elevator": True,
        "parking": False,
        "pets_allowed": True,
        "flatshare": False,
        "room_private": True,
        "owner_or_agency": "agency",
        "available_from": "2026-06-01",
        "validation_status": "VERIFIED",
        "summary": "Beautiful modern apartment in Liben.",
        "suitability_notes": "Great location, direct tram commute to Palmovka.",
    }

    model = LLMEnrichmentResponse(**data)
    assert model.property_type == "flat"
    assert model.price_value == 20000.0
    assert model.balcony is True
    assert model.validation_status == ValidationStatus.VERIFIED.value


def test_llm_response_minimal() -> None:
    # All optional fields set to null should pass validation
    model = LLMEnrichmentResponse()
    assert model.property_type is None
    assert model.price_value is None


def test_llm_schema_generation() -> None:
    # Verify Pydantic JSON Schema outputs conform to Ollama requirements
    schema = LLMEnrichmentResponse.model_json_schema()
    assert "properties" in schema
    assert "property_type" in schema["properties"]
    assert "validation_status" in schema["properties"]

    # Enums/Types are defined correctly
    assert "type" in schema["properties"]["price_value"] or "anyOf" in schema["properties"]["price_value"]
