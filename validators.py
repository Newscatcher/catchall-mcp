"""Validation helpers and typed definitions for CatchAll MCP tools."""

from __future__ import annotations

import json
from typing import Any, Literal

from typing_extensions import TypedDict

ENRICHMENT_TYPES = {"text", "number", "date", "option", "url", "company"}


class ValidatorDefinition(TypedDict):
    """Schema for a custom validator."""

    name: str
    description: str
    type: Literal["boolean"]


class EnrichmentDefinition(TypedDict):
    """Schema for a custom enrichment."""

    name: str
    description: str
    type: Literal["text", "number", "date", "option", "url", "company"]


def validate_page_params(page: int, page_size: int, max_page_size: int = 1000) -> None:
    """Validate pagination parameters."""
    if page < 1:
        raise ValueError("page must be >= 1.")
    if page_size < 1 or page_size > max_page_size:
        raise ValueError(f"page_size must be between 1 and {max_page_size}.")


def validate_sort(sort: str) -> str:
    """Validate monitor jobs sort order."""
    if sort not in {"asc", "desc"}:
        raise ValueError("sort must be either 'asc' or 'desc'.")
    return sort


def validate_new_limit(new_limit: int) -> None:
    """Validate continue_job new_limit."""
    if new_limit < 1:
        raise ValueError("new_limit must be >= 1.")


def validate_monitor_limit(limit: int) -> None:
    """Validate monitor run limit."""
    if limit < 10:
        raise ValueError("limit must be >= 10.")


def coerce_definition_list(value: Any, field_name: str) -> list[dict[str, Any]] | None:
    """Accept list input or JSON-string list input for definitions."""
    if value is None:
        return None

    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return None
        try:
            parsed = json.loads(stripped)
        except json.JSONDecodeError as exc:
            raise ValueError(
                f"{field_name} must be valid JSON array when provided as string."
            ) from exc
        value = parsed

    if not isinstance(value, list):
        raise ValueError(f"{field_name} must be a JSON array when provided.")

    return value


def validate_validator_definitions(
    validators: list[dict[str, Any]] | None,
) -> list[ValidatorDefinition] | None:
    """Validate and normalize custom validators."""
    if validators is None:
        return None

    normalized: list[ValidatorDefinition] = []
    for idx, validator in enumerate(validators):
        if not isinstance(validator, dict):
            raise ValueError(f"validators[{idx}] must be an object.")

        name = validator.get("name")
        description = validator.get("description")
        validator_type = validator.get("type", "boolean")

        if not isinstance(name, str) or not name.strip():
            raise ValueError(f"validators[{idx}].name must be a non-empty string.")
        if not isinstance(description, str) or not description.strip():
            raise ValueError(f"validators[{idx}].description must be a non-empty string.")
        if validator_type != "boolean":
            raise ValueError(f"validators[{idx}].type must be 'boolean'.")

        normalized.append(
            {
                "name": name.strip(),
                "description": description.strip(),
                "type": "boolean",
            }
        )

    return normalized


def validate_enrichment_definitions(
    enrichments: list[dict[str, Any]] | None,
) -> list[EnrichmentDefinition] | None:
    """Validate custom enrichments."""
    if enrichments is None:
        return None

    normalized: list[EnrichmentDefinition] = []
    for idx, enrichment in enumerate(enrichments):
        if not isinstance(enrichment, dict):
            raise ValueError(f"enrichments[{idx}] must be an object.")

        name = enrichment.get("name")
        description = enrichment.get("description")
        enrichment_type = enrichment.get("type")

        if not isinstance(name, str) or not name.strip():
            raise ValueError(f"enrichments[{idx}].name must be a non-empty string.")
        if not isinstance(description, str) or not description.strip():
            raise ValueError(f"enrichments[{idx}].description must be a non-empty string.")
        if not isinstance(enrichment_type, str) or enrichment_type not in ENRICHMENT_TYPES:
            allowed = ", ".join(sorted(ENRICHMENT_TYPES))
            raise ValueError(f"enrichments[{idx}].type must be one of: {allowed}.")

        normalized.append(
            {
                "name": name.strip(),
                "description": description.strip(),
                "type": enrichment_type,  # type: ignore[typeddict-item]
            }
        )

    return normalized


def validate_mode(mode: str) -> str:
    """Validate submit_query job mode."""
    if mode not in {"lite", "base"}:
        raise ValueError("mode must be 'lite' or 'base'.")
    return mode


def validate_webhook_method(webhook_method: str) -> str:
    """Validate webhook HTTP method."""
    normalized = webhook_method.upper()
    if normalized not in {"POST", "PUT"}:
        raise ValueError("webhook_method must be 'POST' or 'PUT'.")
    return normalized


def validate_webhook_auth(webhook_auth: list[str] | None) -> None:
    """Validate webhook basic auth tuple."""
    if webhook_auth is None:
        return
    if len(webhook_auth) != 2:
        raise ValueError("webhook_auth must contain exactly two values: [username, password].")
    if not all(isinstance(item, str) and item for item in webhook_auth):
        raise ValueError("webhook_auth values must be non-empty strings.")


def build_webhook_payload(
    webhook_url: str,
    webhook_method: str,
    webhook_headers: dict[str, str] | None,
    webhook_params: dict[str, str] | None,
    webhook_auth: list[str] | None,
) -> dict[str, Any] | None:
    """Build and validate optional webhook payload."""
    has_webhook_extras = (
        webhook_headers is not None
        or webhook_params is not None
        or webhook_auth is not None
        or webhook_method.upper() != "POST"
    )

    if not webhook_url:
        if has_webhook_extras:
            raise ValueError(
                "webhook_url is required when providing webhook_method, "
                "webhook_headers, webhook_params, or webhook_auth."
            )
        return None

    normalized_method = validate_webhook_method(webhook_method)
    validate_webhook_auth(webhook_auth)

    webhook: dict[str, Any] = {"url": webhook_url, "method": normalized_method}
    if webhook_headers:
        webhook["headers"] = webhook_headers
    if webhook_params:
        webhook["params"] = webhook_params
    if webhook_auth:
        webhook["auth"] = webhook_auth

    return webhook
