"""Helpers for invoking externally hosted agent runtimes.

This module provides the HTTP dispatch layer for forwarding requests
from the orchestrator backend to the four specialized hosted agent
containers (clinical, coverage, compliance, synthesis).
"""

import logging
from typing import Any

import httpx

from app.config import settings

logger = logging.getLogger(__name__)


def _build_headers() -> dict[str, str]:
    headers = {"Content-Type": "application/json"}
    if settings.HOSTED_AGENT_AUTH_TOKEN:
        value = settings.HOSTED_AGENT_AUTH_TOKEN
        if settings.HOSTED_AGENT_AUTH_SCHEME:
            value = f"{settings.HOSTED_AGENT_AUTH_SCHEME} {value}"
        headers[settings.HOSTED_AGENT_AUTH_HEADER] = value
    return headers


def _unwrap_payload(payload: Any) -> dict:
    """Normalize common hosted-agent response envelopes.

    Supported shapes:
    - direct result dict
    - {"result": {...}}
    - {"output": {...}}
    - {"data": {...}}
    """
    if isinstance(payload, dict):
        for key in ("result", "output", "data"):
            value = payload.get(key)
            if isinstance(value, dict):
                return value
        return payload
    return {"error": "Hosted agent returned a non-object payload", "tool_results": []}


async def invoke_hosted_agent(agent_name: str, url: str, payload: dict) -> dict:
    """Invoke a hosted agent over HTTP and normalize the response shape."""
    if not url:
        return {
            "error": f"Hosted runtime enabled but {agent_name} URL is not configured",
            "tool_results": [],
        }

    try:
        timeout = httpx.Timeout(settings.HOSTED_AGENT_TIMEOUT_SECONDS)
        async with httpx.AsyncClient(timeout=timeout, headers=_build_headers()) as client:
            response = await client.post(url, json=payload)
            response.raise_for_status()
            data = response.json()
            normalized = _unwrap_payload(data)
            logger.info("Hosted %s invocation succeeded via %s", agent_name, url)
            return normalized
    except httpx.HTTPStatusError as exc:
        detail = exc.response.text[:500] if exc.response is not None else str(exc)
        logger.warning("Hosted %s invocation failed: %s", agent_name, detail)
        return {
            "error": f"Hosted {agent_name} call failed ({exc.response.status_code}): {detail}",
            "tool_results": [],
        }
    except Exception as exc:
        logger.warning("Hosted %s invocation failed: %s", agent_name, exc)
        return {
            "error": f"Hosted {agent_name} call failed: {exc}",
            "tool_results": [],
        }