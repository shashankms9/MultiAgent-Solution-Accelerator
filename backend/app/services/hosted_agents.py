"""Helpers for invoking externally hosted agent runtimes.

This module is the HTTP dispatch layer between the FastAPI orchestrator and the
four MAF agent containers (clinical, coverage, compliance, synthesis).

Protocol: Foundry Responses API
  POST {url}/responses
  Body:  {"input": {"messages": [{"role": "user", "content": "<json prompt>"}]}}
  Reply: {"id": "...", "output": [{"type": "message", "content": [{"type": "text", "text": "..."}]}], "status": "completed"}

from_agent_framework(agent).run() inside each agent container implements this
protocol automatically.  The same protocol is used both in docker-compose
(container-to-container) and when agents are deployed to Azure Foundry as
hosted agents — so this caller works unchanged in both environments.
"""

import json
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


def _extract_result(data: Any) -> dict:
    """Parse a Foundry Responses API reply into a plain result dict.

    Expected shape:
        {
            "status": "completed",
            "output": [
                {
                    "type": "message",
                    "content": [{"type": "text", "text": "<json string>"}]
                }
            ]
        }

    The agent emits structured output (MAF default_options response_format),
    so `text` is already a JSON-serialised Pydantic model — parse it directly.
    Falls back gracefully if the shape is unexpected.
    """
    if not isinstance(data, dict):
        return {"error": "Agent returned a non-object response", "tool_results": []}

    status = data.get("status", "")
    if status not in ("completed", ""):  # empty string = local test adapter
        return {"error": f"Agent returned status={status!r}", "tool_results": []}

    output = data.get("output", [])
    for item in output if isinstance(output, list) else []:
        if not isinstance(item, dict):
            continue
        for block in item.get("content", []) if isinstance(item.get("content"), list) else []:
            if isinstance(block, dict) and block.get("type") == "text":
                text = block.get("text", "")
                try:
                    return json.loads(text)
                except (json.JSONDecodeError, TypeError):
                    return {"error": f"Agent text was not valid JSON: {text[:200]}"}

    # Fallback: some adapters return the result directly under known keys
    for key in ("result", "data"):
        value = data.get(key)
        if isinstance(value, dict):
            return value

    return {"error": f"Could not extract result from agent response: {str(data)[:300]}"}


async def invoke_hosted_agent(agent_name: str, url: str, payload: dict) -> dict:
    """Invoke a hosted MAF agent using the Foundry Responses API protocol.

    Calls POST {url}/responses with the standard Responses API envelope.
    Works identically for docker-compose container-to-container calls and
    for agents deployed to Azure Foundry as hosted agents.
    """
    if not url:
        return {
            "error": f"Hosted runtime enabled but {agent_name} URL is not configured",
            "tool_results": [],
        }

    # Foundry Responses API envelope — from_agent_framework expects this shape.
    request_body = {
        "input": {
            "messages": [
                {"role": "user", "content": json.dumps(payload)}
            ]
        }
    }

    responses_url = url.rstrip("/") + "/responses"

    try:
        timeout = httpx.Timeout(settings.HOSTED_AGENT_TIMEOUT_SECONDS)
        async with httpx.AsyncClient(timeout=timeout, headers=_build_headers()) as client:
            response = await client.post(responses_url, json=request_body)
            response.raise_for_status()
            data = response.json()
            result = _extract_result(data)
            logger.info("Hosted %s invocation succeeded via %s", agent_name, responses_url)
            return result
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