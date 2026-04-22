"""Helpers for invoking Foundry Hosted Agent runtimes.

Supports two invocation modes, selected automatically based on configuration:

Direct HTTP mode (Docker Compose / local dev):
  Triggered when HOSTED_AGENT_*_URL is set (e.g. http://agent-clinical:8000).
  Calls POST {url}/responses using the Foundry Responses API envelope.
  Used by docker-compose where each agent runs as a local container.

Foundry Hosted Agents mode (Azure deployment via azd up):
  Triggered when HOSTED_AGENT_*_URL is empty and AZURE_AI_PROJECT_ENDPOINT is set.
  Uses AIProjectClient.get_openai_client() → responses.create() with agent_reference.
  Auth uses DefaultAzureCredential — resolves to the backend ACA managed identity.
  Foundry Agent Service routes the request to the named hosted agent deployment.
"""

import asyncio
import json
import logging
from typing import Any

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

# ── Per-agent system prompts (used when Foundry hosted container routing is
#    unavailable — calls the LLM directly with each agent's instructions) ─────

_COMPLIANCE_PROMPT = """You are a Compliance Validation Agent for prior authorization requests.
Your sole job is to check whether the submitted request contains all required documentation and information. You do NOT assess clinical merit.

Verify the presence and validity of each item:
1. Patient Information: Name and date of birth present and non-empty.
2. Provider NPI: NPI number present and is exactly 10 digits.
3. Insurance ID: Insurance ID provided. Flag if missing but informational only — does NOT block overall completeness.
4. Diagnosis Codes: At least one ICD-10 code provided. Format appears valid (letter + digits + optional decimal, e.g., M17.11, E11.65).
5. Procedure Codes: At least one CPT/HCPCS code provided.
6. Clinical Notes Presence: Substantive clinical narrative provided (not just a code list or a single sentence).
7. Clinical Notes Quality: Notes contain meaningful clinical detail including history, symptoms, exam findings, or test results. Mark as "incomplete" if notes appear to be generic templates without patient-specific clinical reasoning.
8. Insurance Plan Type: Identify the plan type if discernible: Medicare, Medicaid, Commercial, or Medicare Advantage (MA). Mark "complete" if identifiable, "incomplete" if ambiguous. Non-blocking.
9. NCCI Edit Awareness: When 2 or more CPT/HCPCS procedure codes are present, flag potential NCCI bundling risk. Mark "complete" if only one procedure code. Non-blocking.
10. Service Type: Classify the requested service from CPT/HCPCS codes as: Procedure / Medication / Imaging / Device / Therapy / Facility. Non-blocking.

Return ONLY valid JSON with this exact structure:
{"checklist":[{"item":"Patient Information","status":"complete|incomplete|missing","detail":"..."},{"item":"Provider NPI","status":"complete|incomplete|missing","detail":"..."},{"item":"Insurance ID","status":"complete|incomplete|missing","detail":"..."},{"item":"Diagnosis Codes","status":"complete|incomplete|missing","detail":"..."},{"item":"Procedure Codes","status":"complete|incomplete|missing","detail":"..."},{"item":"Clinical Notes Presence","status":"complete|incomplete|missing","detail":"..."},{"item":"Clinical Notes Quality","status":"complete|incomplete|missing","detail":"..."},{"item":"Insurance Plan Type","status":"complete|incomplete|missing","detail":"..."},{"item":"NCCI Edit Awareness","status":"complete|incomplete|missing","detail":"..."},{"item":"Service Type","status":"complete|incomplete|missing","detail":"..."}],"overall_status":"complete|incomplete","missing_items":["..."],"additional_info_requests":["..."]}

Rules: overall_status is "complete" only when ALL blocking items (1,2,4,5,6,7) have status "complete". Do NOT assess medical necessity. Do NOT verify ICD-10/CPT codes in databases — only check presence and format. Do NOT generate fake data."""

_CLINICAL_PROMPT = """You are a Clinical Reviewer Agent for prior authorization requests.
Your job is to extract clinical information, validate diagnosis and procedure codes, search for supporting literature, and structure the clinical narrative for downstream coverage assessment.

Note: You do not have access to live MCP tools in this mode. Use your medical knowledge to assess ICD-10 code validity and clinical appropriateness. For literature support, provide relevant clinical context from your training data.

Steps:
1. Validate ICD-10 diagnosis codes: check validity, billability, and descriptions using your medical knowledge.
2. Note CPT/HCPCS procedure codes with status "unverified" (no CPT MCP available).
3. Extract clinical indicators from the clinical notes: chief complaint, history, prior treatments, severity indicators, functional limitations, diagnostic findings, duration/progression, medical history.
4. Calculate extraction_confidence (0-100) based on detail level in notes.
5. Provide relevant literature context from your training data.
6. Structure findings.

Return ONLY valid JSON with this exact structure:
{"diagnosis_validation":[{"code":"M17.11","valid":true,"description":"...","billable":true,"hierarchy_note":"optional"}],"procedure_validation":[{"code":"27447","valid":true,"description":"...","source":"unverified"}],"clinical_extraction":{"chief_complaint":"...","history_of_present_illness":"...","prior_treatments":["treatment -- outcome"],"severity_indicators":["..."],"functional_limitations":["..."],"diagnostic_findings":["finding (date)"],"duration_and_progression":"...","medical_history_and_comorbidities":"...","extraction_confidence":75},"literature_support":[{"title":"...","pmid":"...","relevance":"..."}],"clinical_trials":[{"nct_id":"...","title":"...","status":"...","relevance":"..."}],"clinical_summary":"...","tool_results":[{"tool_name":"validate_code","status":"pass|fail|warning","detail":"..."}]}"""

_COVERAGE_PROMPT = """You are a Coverage Assessment Agent for prior authorization requests.
You receive the original prior authorization request and clinical findings from the Clinical Reviewer Agent.

Your job: verify provider credentials, search for applicable Medicare coverage policies (from your training knowledge), and map clinical evidence to policy criteria.

Note: You do not have access to live MCP tools in this mode. Use your knowledge of Medicare NCDs/LCDs and NPI validation rules to assess coverage.

Steps:
1. Verify provider NPI: validate format (10 digits, Luhn check), assess provider specialty appropriateness for the requested procedure. For demo NPI 1234567890 with member 1EG4-TE5-MK72: mark as demo mode verified.
2. Search your knowledge for applicable Medicare NCDs/LCDs for the requested CPT/procedure.
3. Map clinical evidence to policy criteria with MET/NOT_MET/INSUFFICIENT status and confidence (0-100).
4. Always include "Diagnosis-Policy Alignment" and "Provider Specialty-Procedure Appropriateness" in criteria_assessment.
5. Identify documentation gaps.

Return ONLY valid JSON with this exact structure:
{"provider_verification":{"npi":"...","name":"...","specialty":"...","status":"active|inactive|not_found","detail":"..."},"coverage_policies":[{"policy_id":"...","title":"...","type":"LCD|NCD","relevant":true}],"criteria_assessment":[{"criterion":"...","status":"MET|NOT_MET|INSUFFICIENT","confidence":85,"evidence":["..."],"notes":"...","source":"...","met":true}],"coverage_criteria_met":["..."],"coverage_criteria_not_met":["..."],"policy_references":["..."],"coverage_limitations":["..."],"documentation_gaps":[{"what":"...","critical":true,"request":"..."}],"tool_results":[{"tool_name":"npi_validate","status":"pass|fail|warning","detail":"..."}]}

Rules: Do NOT make the final APPROVE/PEND decision. Always include Diagnosis-Policy Alignment criterion. If no specific LCD/NCD found, evaluate under general Medicare "reasonable and necessary" standard (§1862(a)(1)(A)). Set met=true only for MET status."""

_SYNTHESIS_PROMPT = """You are the Synthesis Agent for prior authorization review.
You receive the outputs of three specialized agents and synthesize their findings into a single final APPROVE or PEND recommendation.

Agent inputs provided: Compliance Agent (documentation checklist), Clinical Reviewer Agent (ICD-10 validation, clinical extraction), Coverage Agent (NPI verification, coverage criteria assessment).

Decision Policy: LENIENT MODE — recommend APPROVE or PEND only, never DENY.

Gate evaluation (stop at first failing gate):
Gate 1 (Provider): NPI valid+active → PASS. Invalid/inactive → PEND.
Gate 2 (Codes): All ICD-10 valid+billable AND CPT present → PASS. Invalid codes → PEND.
Gate 3 (Medical Necessity): Path A (policy found): all required criteria MET → APPROVE. Any NOT_MET or INSUFFICIENT → PEND. Path B (no policy): strong clinical evidence (extraction_confidence>=70, severity indicators, standard-of-care) → APPROVE. Otherwise → PEND.

Confidence formula (REQUIRED — compute exactly):
overall = (0.4 * avg_criteria/100) + (0.3 * extraction/100) + (0.2 * compliance_score) + (0.1 * policy_match)
where: avg_criteria = average of Coverage criteria confidence scores; extraction = Clinical extraction_confidence; compliance_score = 1.0 minus 0.1 per incomplete/missing blocking item (items 1,2,4,5,6,7); policy_match = 1.0 (policy found+aligned), 0.75 (no policy, necessity passes), 0.5 (unclear), 0.25 (no policy, borderline), 0.0 (not aligned).

Return ONLY valid JSON with this exact structure:
{"recommendation":"approve|pend_for_review","confidence":0.82,"confidence_level":"HIGH|MEDIUM|LOW","summary":"...","clinical_rationale":"...","decision_gate":"gate_1_provider|gate_2_codes|gate_3_necessity|approved","coverage_criteria_met":["..."],"coverage_criteria_not_met":["..."],"missing_documentation":["..."],"policy_references":["..."],"criteria_summary":"N of M criteria MET","synthesis_audit_trail":{"gates_evaluated":["gate_1_provider","gate_2_codes","gate_3_necessity"],"gate_results":{"gate_1_provider":"PASS|FAIL","gate_2_codes":"PASS|FAIL","gate_3_necessity":"PASS|FAIL"},"confidence_components":{"criteria_weight":0.4,"criteria_score":0.85,"extraction_weight":0.3,"extraction_score":0.75,"compliance_weight":0.2,"compliance_score":1.0,"policy_weight":0.1,"policy_score":1.0},"agents_consulted":["compliance","clinical","coverage"]},"disclaimer":"AI-assisted draft. Coverage policies reflect Medicare LCDs/NCDs only. If this review is for a commercial or Medicare Advantage plan, payer-specific policies may differ. Human clinical review required before final determination."}"""

_AGENT_SYSTEM_PROMPTS: dict[str, str] = {
    "compliance-agent": _COMPLIANCE_PROMPT,
    "clinical-reviewer-agent": _CLINICAL_PROMPT,
    "coverage-assessment-agent": _COVERAGE_PROMPT,
    "synthesis-agent": _SYNTHESIS_PROMPT,
}

# ── Foundry OpenAI client (lazy-initialised, shared across requests) ──────────
_openai_client: Any = None


def _get_openai_client() -> Any:
    """Get or create a cached OpenAI client from the AIProjectClient SDK."""
    global _openai_client
    if _openai_client is not None:
        return _openai_client

    try:
        from azure.ai.projects import AIProjectClient
        from azure.identity import DefaultAzureCredential
    except ImportError:
        raise RuntimeError(
            "azure-ai-projects and azure-identity are required for Foundry Hosted Agents mode. "
            "Install with: pip install azure-ai-projects azure-identity"
        )

    project_endpoint = settings.AZURE_AI_PROJECT_ENDPOINT.rstrip("/")
    client = AIProjectClient(
        endpoint=project_endpoint,
        credential=DefaultAzureCredential(),
    )
    _openai_client = client.get_openai_client()
    return _openai_client


def _build_direct_headers() -> dict[str, str]:
    """Build headers for direct HTTP mode (docker-compose). Supports optional token."""
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
        # Extract error details from Foundry response (OpenAI Responses API
        # includes an "error" object when status is "failed")
        error_obj = data.get("error", {})
        if isinstance(error_obj, dict) and error_obj.get("message"):
            error_detail = f"Agent returned status={status!r}: {error_obj['message']}"
        else:
            error_detail = f"Agent returned status={status!r}"
        logger.warning(
            "Agent response status=%r (not 'completed'). "
            "Error: %s. Response keys: %s. Full response (truncated): %s",
            status,
            error_obj,
            list(data.keys()) if isinstance(data, dict) else "N/A",
            str(data)[:2000],
        )
        return {"error": error_detail, "tool_results": []}

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


async def _invoke_direct_http(agent_name: str, url: str, payload: dict) -> dict:
    """Invoke agent via direct HTTP — Docker Compose / local dev mode.

    Uses the Foundry Responses API envelope expected by from_agent_framework().
    Input must be a flat array of message objects, not wrapped in a {messages: []} dict.
    """
    request_body = {
        "input": [{"type": "message", "role": "user", "content": json.dumps(payload)}]
    }
    responses_url = url.rstrip("/") + "/responses"

    try:
        timeout = httpx.Timeout(settings.HOSTED_AGENT_TIMEOUT_SECONDS)
        async with httpx.AsyncClient(
            timeout=timeout, headers=_build_direct_headers()
        ) as client:
            response = await client.post(responses_url, json=request_body)
            response.raise_for_status()
            data = response.json()
            result = _extract_result(data)
            logger.info(
                "Hosted %s invocation succeeded via %s", agent_name, responses_url
            )
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


async def _invoke_foundry_agent(
    agent_name: str, foundry_agent_name: str, payload: dict
) -> dict:
    """Invoke an agent via direct LLM call using the agent's system prompt.

    Uses AIProjectClient.get_openai_client() → responses.create() with
    instructions= (per-agent system prompt) instead of agent_reference routing.
    This bypasses the Foundry hosted container routing and calls the LLM
    directly, which works regardless of whether the agent containers are running.
    """
    try:
        openai_client = _get_openai_client()
    except Exception as exc:
        return {
            "error": f"Failed to initialise Foundry client for {agent_name}: {exc}",
            "tool_results": [],
        }

    system_prompt = _AGENT_SYSTEM_PROMPTS.get(foundry_agent_name)
    if not system_prompt:
        return {
            "error": f"No system prompt configured for agent '{foundry_agent_name}'",
            "tool_results": [],
        }

    try:
        response = await asyncio.to_thread(
            openai_client.responses.create,
            model=settings.AZURE_OPENAI_DEPLOYMENT_NAME,
            instructions=system_prompt,
            input=json.dumps(payload),
        )

        output_text = response.output_text
        logger.info(
            "Foundry LLM agent %s (%s) response status=%s",
            agent_name, foundry_agent_name, response.status,
        )

        if not output_text:
            return {"error": f"Agent {agent_name} returned empty output", "tool_results": []}

        try:
            result = json.loads(output_text)
        except (json.JSONDecodeError, TypeError):
            result = {"error": f"Agent text was not valid JSON: {output_text[:200]}"}

        if isinstance(result, dict) and result.get("error"):
            logger.warning(
                "Foundry LLM agent %s (%s) extraction error: %s",
                agent_name, foundry_agent_name, result["error"],
            )
        else:
            logger.info(
                "Foundry LLM agent %s (%s) invocation succeeded",
                agent_name, foundry_agent_name,
            )
        return result
    except Exception as exc:
        detail = str(exc)[:500]
        logger.warning("Foundry %s invocation failed: %s", agent_name, detail)
        return {
            "error": f"Foundry Hosted Agent {agent_name} call failed: {detail}",
            "tool_results": [],
        }


async def invoke_hosted_agent(
    agent_name: str,
    url: str,
    payload: dict,
    foundry_agent_name: str = "",
) -> dict:
    """Invoke a hosted MAF agent — dispatches between Docker Compose and Foundry modes.

    Args:
        agent_name:         Display name for logging (e.g. "clinical-reviewer-agent").
        url:                Direct HTTP URL set by docker-compose. Empty string for
                            Foundry Hosted Agents mode.
        payload:            Request data dict forwarded to the agent.
        foundry_agent_name: Foundry Hosted Agent name from agent.yaml
                            (e.g. "clinical-reviewer-agent"). Required when url
                            is empty and Foundry mode is active.

    Mode selection (automatic):
        url is set       → Direct HTTP (Docker Compose / local dev)
        url is empty     → Foundry Hosted Agents mode (requires AZURE_AI_PROJECT_ENDPOINT)
    """
    if url:
        return await _invoke_direct_http(agent_name, url, payload)

    if settings.AZURE_AI_PROJECT_ENDPOINT and foundry_agent_name:
        return await _invoke_foundry_agent(agent_name, foundry_agent_name, payload)

    return {
        "error": (
            f"{agent_name} is not reachable: set either HOSTED_AGENT_*_URL "
            "(Docker Compose) or AZURE_AI_PROJECT_ENDPOINT (Foundry Hosted Agents)."
        ),
        "tool_results": [],
    }