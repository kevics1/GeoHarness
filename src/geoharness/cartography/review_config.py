"""Credential-free description of the effective independent image reviewer."""

from __future__ import annotations

from typing import Any

from geoharness.config.settings import GeoConfig


def review_configuration(config: GeoConfig) -> dict[str, Any]:
    """Describe actual fallback rules without returning credentials or endpoint URLs."""
    settings = config.map_review
    model = settings.model or config.model
    endpoint = settings.base_url or config.base_url
    key = settings.api_key or config.api_key
    errors = []
    if not model.strip() or "${" in model:
        errors.append("review_model_missing_or_unresolved")
    if "${" in endpoint:
        errors.append("review_endpoint_unresolved")
    if not key.strip() or "${" in key:
        errors.append("review_credential_missing_or_unresolved")
    return {
        "enabled": settings.enabled,
        "model": model,
        "model_source": "map_review" if settings.model else "main",
        "endpoint_source": "map_review" if settings.base_url else
                           "main" if config.base_url else "provider_default",
        "credential_source": "map_review" if settings.api_key else "main",
        "credential_configured": bool(key.strip()) and "${" not in key,
        "transport": "openai_compatible",
        "reasoning_effort": settings.reasoning_effort,
        "reasoning_source": "map_review" if settings.reasoning_effort is not None else
                            "request_extra_body" if any(name in settings.request_extra_body
                                for name in ("thinking", "reasoning_effort")) else
                            "provider_default",
        "response_format": settings.response_format,
        "max_tokens": settings.max_tokens,
        "timeout_seconds": settings.timeout_seconds,
        "read_timeout_seconds": settings.read_timeout_seconds,
        "configuration_errors": errors,
    }
