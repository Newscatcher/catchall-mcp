"""Validation helpers and typed definitions for CatchAll MCP tools."""

from __future__ import annotations

import base64
import binascii
import json
from typing import Any, Literal

from typing_extensions import TypedDict

ENRICHMENT_TYPES = {"text", "number", "date", "option", "url", "company"}

# Enum value sets from the v1.5.3 / live API OpenAPI spec.
WEBHOOK_TYPES = {"generic", "slack", "teams", "custom"}
DELIVERY_MODES = {"full", "per_record"}
HTTP_METHODS = {"GET", "POST", "PUT", "PATCH", "DELETE"}
WEBHOOK_AUTH_TYPES = {"bearer", "api_key", "basic"}
# Resource types a webhook can be mapped to (MappableResourceType).
MAPPABLE_RESOURCE_TYPES = {"job", "monitor", "monitor_group"}
# Resource types a project can contain (ProjectResourceTypeEnum).
PROJECT_RESOURCE_TYPES = {"job", "monitor", "dataset", "monitor_group"}
OWNERSHIP_VALUES = {"all", "own", "shared"}
SORT_ORDERS = {"asc", "desc"}
DATASET_STATUSES = {"pending", "enriching", "ready", "failed"}
DATASET_SORT_BY = {"name", "created_at", "status"}
ENTITY_STATUSES = {"pending", "enriching", "ready", "failed"}
ENTITY_TYPES = {"company", "person"}
ENTITY_SORT_BY = {"created_at", "name", "status"}
# EntityAssociationType: how strongly a watchlist entity must appear in the event.
ED_ASSOCIATION_TYPES = {"event_associated", "mention"}


def validate_choice(value: str, allowed: set[str], field_name: str) -> str:
    """Validate that a string value is one of an allowed set."""
    if value not in allowed:
        options = ", ".join(sorted(allowed))
        raise ValueError(f"{field_name} must be one of: {options}.")
    return value


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


def validate_limit(limit: int) -> None:
    """Validate that a limit value is within the allowed range (minimum 10)."""
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


def validate_http_method(method: str) -> str:
    """Validate and normalize a webhook delivery HTTP method."""
    normalized = method.upper()
    return validate_choice(normalized, HTTP_METHODS, "method")


# Hard cap on inline CSV content (decoded bytes). The hosted server buffers
# the whole upload in memory, so uncapped input is a memory-DoS vector.
MAX_CSV_BYTES = 10 * 1024 * 1024  # 10 MB
# Base64 inflates content by ~4/3, so a string longer than this cannot decode
# to <= MAX_CSV_BYTES. Checked before decoding so over-cap input is rejected
# with a cheap len() instead of being buffered/decoded first.
_MAX_CSV_B64_CHARS = (MAX_CSV_BYTES * 4) // 3 + 4

_CSV_TOO_LARGE = (
    f"file is too large: inline CSV content is capped at "
    f"{MAX_CSV_BYTES // (1024 * 1024)} MB. Split the CSV and use "
    "append_csv_to_dataset for the remaining rows."
)


def coerce_csv_file_content(file: str) -> bytes:
    """Turn the `file` argument of a CSV upload tool into raw CSV bytes.

    Accepts either:
    - raw CSV text (anything containing a newline or a comma), or
    - a standard base64-encoded CSV (no line wrapping).

    Content is capped at MAX_CSV_BYTES (10 MB) after base64 decoding; the
    raw string length is checked before any decode so oversized input is
    rejected cheaply, then the decoded size is checked again.

    Server-side file paths are deliberately NOT accepted: this server may be
    hosted, and reading arbitrary local paths on behalf of a remote caller
    would leak server files.
    """
    if not isinstance(file, str) or not file.strip():
        raise ValueError("file is required: pass the CSV content as raw text or base64.")

    # Cheap pre-decode size guard (see _MAX_CSV_B64_CHARS above).
    if len(file) > _MAX_CSV_B64_CHARS:
        raise ValueError(_CSV_TOO_LARGE)

    # Raw CSV text: base64 never contains a newline or comma, CSV data does.
    if "\n" in file or "," in file:
        encoded = file.encode("utf-8")
        if len(encoded) > MAX_CSV_BYTES:
            raise ValueError(_CSV_TOO_LARGE)
        return encoded

    try:
        decoded = base64.b64decode(file, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError(
            "file must be raw CSV text or standard base64-encoded CSV content."
        ) from exc
    if not decoded:
        raise ValueError("file decoded to empty content; provide a non-empty CSV.")
    if len(decoded) > MAX_CSV_BYTES:
        raise ValueError(_CSV_TOO_LARGE)
    return decoded


def validate_webhook_auth(auth: dict[str, Any] | None) -> dict[str, Any] | None:
    """Validate a webhook auth object (bearer / api_key / basic).

    Matches the API's auth schema:
    - bearer:  {"type": "bearer",  "token": "..."}
    - api_key: {"type": "api_key", "header": "X-API-Key", "value": "..."}
    - basic:   {"type": "basic",   "username": "...", "password": "..."}
    """
    if auth is None:
        return None
    if not isinstance(auth, dict):
        raise ValueError("auth must be an object with a 'type' field.")

    auth_type = auth.get("type")
    if auth_type not in WEBHOOK_AUTH_TYPES:
        options = ", ".join(sorted(WEBHOOK_AUTH_TYPES))
        raise ValueError(f"auth.type must be one of: {options}.")

    required_by_type = {
        "bearer": ["token"],
        "api_key": ["header", "value"],
        "basic": ["username", "password"],
    }
    for field in required_by_type[auth_type]:
        value = auth.get(field)
        if not isinstance(value, str) or not value:
            raise ValueError(f"auth of type '{auth_type}' requires a non-empty '{field}'.")

    return auth
