"""API routes for prior authorization review."""

import asyncio
import json
import logging
import uuid

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse

from app.models.schemas import (
    PriorAuthRequest,
    ReviewResponse,
    ReviewSummary,
    AgentResults,
    AuditTrail,
    ComplianceResult,
    ClinicalResult,
    CoverageResult,
    DocumentationGap,
)
from app.agents.orchestrator import (
    run_multi_agent_review,
    store_review,
    get_review,
    list_reviews,
)

logger = logging.getLogger(__name__)

router = APIRouter()


def _build_review_response(request_id: str, result: dict) -> ReviewResponse:
    """Build a ReviewResponse from orchestrator output."""
    # Parse merged tool_results from all agents
    tool_results = []
    for tr in result.get("tool_results", []):
        tool_results.append({
            "tool_name": tr.get("tool_name", "unknown"),
            "status": tr.get("status", "warning"),
            "detail": tr.get("detail", ""),
        })

    # Parse per-agent results (best-effort — agent JSON may not match exactly)
    agent_raw = result.get("agent_results", {})
    agent_results = AgentResults(
        compliance=_safe_parse(ComplianceResult, agent_raw.get("compliance")),
        clinical=_safe_parse(ClinicalResult, agent_raw.get("clinical")),
        coverage=_safe_parse(CoverageResult, agent_raw.get("coverage")),
    )

    # Parse audit trail
    audit_raw = result.get("audit_trail")
    audit_trail = _safe_parse(AuditTrail, audit_raw)

    # Parse documentation gaps
    doc_gaps = []
    for g in result.get("documentation_gaps", []):
        parsed = _safe_parse(DocumentationGap, g)
        if parsed:
            doc_gaps.append(parsed)
    # Also pull from coverage agent if not in synthesis
    if not doc_gaps and agent_results.coverage and agent_results.coverage.documentation_gaps:
        doc_gaps = agent_results.coverage.documentation_gaps

    return ReviewResponse(
        request_id=request_id,
        recommendation=result.get("recommendation", "pend_for_review"),
        confidence=result.get("confidence", 0.0),
        confidence_level=result.get("confidence_level", ""),
        summary=result.get("summary", "Review completed."),
        tool_results=tool_results,
        clinical_rationale=result.get("clinical_rationale", ""),
        coverage_criteria_met=result.get("coverage_criteria_met", []),
        coverage_criteria_not_met=result.get("coverage_criteria_not_met", []),
        missing_documentation=result.get("missing_documentation", []),
        documentation_gaps=doc_gaps,
        policy_references=result.get("policy_references", []),
        disclaimer=result.get(
            "disclaimer",
            "AI-assisted draft. Coverage policies reflect Medicare LCDs/NCDs only. "
            "If this review is for a commercial or Medicare Advantage plan, "
            "payer-specific policies may differ. Human clinical review required "
            "before final determination.",
        ),
        agent_results=agent_results,
        audit_trail=audit_trail,
        audit_justification=result.get("audit_justification"),
        audit_justification_pdf=result.get("audit_justification_pdf"),
    )


@router.post("/review", response_model=ReviewResponse)
async def submit_review(request: PriorAuthRequest):
    """Submit a prior authorization request for multi-agent AI-assisted review.

    Three specialized agents (Compliance, Clinical Reviewer, Coverage) run
    in a fan-out/fan-in pattern. An orchestrator synthesizes their outputs
    into a final APPROVE or PEND recommendation using a gate-based decision
    rubric with confidence scoring.

    Returns the structured decision along with per-agent breakdowns, audit
    trail, and an audit justification document.
    """
    request_id = str(uuid.uuid4())

    try:
        result = await run_multi_agent_review(request.model_dump())
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Multi-agent review failed: {str(e)}",
        )

    response = _build_review_response(request_id, result)

    # Persist for later retrieval and decision-making
    store_review(request_id, request.model_dump(), response.model_dump())

    return response


@router.post("/review/stream")
async def submit_review_stream(request: PriorAuthRequest, http_request: Request):
    """Stream prior authorization review progress via Server-Sent Events.

    Emits progress events as the multi-agent pipeline runs, then sends
    the final ReviewResponse as an 'event: result' SSE event.
    """
    request_id = str(uuid.uuid4())
    queue: asyncio.Queue[dict | None] = asyncio.Queue()

    async def on_progress(event: dict) -> None:
        await queue.put(event)

    async def run_review() -> None:
        try:
            result = await run_multi_agent_review(
                request.model_dump(), on_progress=on_progress
            )
            response = _build_review_response(request_id, result)
            store_review(request_id, request.model_dump(), response.model_dump())
            await queue.put({"_type": "result", "data": response.model_dump()})
        except Exception as e:
            await queue.put({"_type": "error", "detail": str(e)})
        finally:
            await queue.put(None)  # Sentinel

    async def event_generator():
        task = asyncio.create_task(run_review())
        try:
            while True:
                # Check if client disconnected
                if await http_request.is_disconnected():
                    task.cancel()
                    break

                try:
                    event = await asyncio.wait_for(queue.get(), timeout=2.0)
                except asyncio.TimeoutError:
                    # Send keepalive comment
                    yield ": keepalive\n\n"
                    continue

                if event is None:
                    break  # Sentinel — pipeline done

                if event.get("_type") == "result":
                    data = json.dumps(event["data"], default=str)
                    yield f"event: result\ndata: {data}\n\n"
                elif event.get("_type") == "error":
                    data = json.dumps({"detail": event["detail"]})
                    yield f"event: error\ndata: {data}\n\n"
                else:
                    data = json.dumps(event, default=str)
                    yield f"event: progress\ndata: {data}\n\n"
        except asyncio.CancelledError:
            task.cancel()

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@router.get("/review/{request_id}", response_model=ReviewResponse)
async def get_review_by_id(request_id: str):
    """Retrieve a previously completed review by its request_id."""
    stored = get_review(request_id)
    if not stored:
        raise HTTPException(status_code=404, detail=f"Review {request_id} not found")
    return ReviewResponse(**stored["response"])


@router.get("/reviews", response_model=list[ReviewSummary])
async def get_all_reviews():
    """List all completed reviews (most recent first)."""
    reviews = list_reviews()
    return [
        ReviewSummary(
            request_id=r["request_id"],
            patient_name=r["request_data"].get("patient_name", ""),
            recommendation=r["response"].get("recommendation", ""),
            confidence_level=r["response"].get("confidence_level", ""),
            reviewed_at=r["stored_at"],
            decision_made=r["decision"] is not None,
        )
        for r in reviews
    ]


def _safe_parse(model_class, data):
    """Attempt to parse a dict into a Pydantic model, return None on failure.

    Three-stage approach:
    1. Direct validation (fast path for well-formed agent data)
    2. Sanitized validation (handles common type mismatches from LLM-generated JSON)
    3. Minimal fallback (preserves agent_name and error fields)
    """
    if not data or not isinstance(data, dict):
        return None
    try:
        return model_class.model_validate(data)
    except Exception as e:
        logger.warning("Parse %s failed (stage 1): %s", model_class.__name__, e)

    # Stage 2: sanitize and retry
    try:
        sanitized = _sanitize_agent_data(data)
        return model_class.model_validate(sanitized)
    except Exception as e:
        logger.warning("Parse %s failed (stage 2 sanitized): %s", model_class.__name__, e)

    # Stage 3: minimal model with error info
    try:
        minimal = {}
        if "agent_name" in data:
            minimal["agent_name"] = str(data["agent_name"])
        if "error" in data:
            minimal["error"] = str(data["error"])
        else:
            minimal["error"] = "Agent data could not be parsed into expected format"
        return model_class.model_validate(minimal)
    except Exception:
        return None


def _sanitize_agent_data(data: dict) -> dict:
    """Sanitize agent result dict to handle common type mismatches.

    LLM agents sometimes return:
    - list[str] fields as a single string, or list[dict] instead of list[str]
    - bool fields as "Yes"/"No"/"true"/"false" strings
    - int fields as "85%" strings or 0.85 floats
    - evidence fields as strings instead of lists

    This function normalizes these before Pydantic validation.
    """
    if not isinstance(data, dict):
        return data

    result = dict(data)

    # Fields that should be list[str] — coerce str→[str] and dict→str(dict)
    _STR_LIST_FIELDS = {
        "prior_treatments", "severity_indicators", "functional_limitations",
        "diagnostic_findings", "missing_items", "additional_info_requests",
        "coverage_criteria_met", "coverage_criteria_not_met",
        "policy_references", "coverage_limitations", "evidence",
        "data_sources",
    }
    for key in _STR_LIST_FIELDS:
        if key in result:
            result[key] = _coerce_str_list(result[key])

    # Fields that should be int — coerce "85%" or 0.85 float
    for key in ("extraction_confidence", "confidence", "extraction_confidence",
                "assessment_confidence"):
        if key in result and not isinstance(result[key], int):
            result[key] = _coerce_int(result[key])

    # Fields that should be bool — coerce "Yes"/"true"/etc.
    for key in ("valid", "billable", "critical", "relevant", "met"):
        if key in result and not isinstance(result[key], bool):
            result[key] = _coerce_bool(result[key])

    # Field aliasing — agents may use different names for the same field
    # DocumentationGap: "what" is required but agent may return "description"
    if "description" in result and "what" not in result:
        result["what"] = result.pop("description")
    # CriterionAssessment: "criterion" is required but agent may use "name"
    if "name" in result and "criterion" not in result:
        result["criterion"] = result.pop("name")

    # Recursively sanitize known nested dicts
    for nested_key in ("clinical_extraction", "provider_verification"):
        if nested_key in result and isinstance(result[nested_key], dict):
            result[nested_key] = _sanitize_agent_data(result[nested_key])

    # Recursively sanitize known list-of-dict fields
    for list_key in ("diagnosis_validation", "criteria_assessment",
                     "documentation_gaps", "coverage_policies",
                     "literature_support", "clinical_trials",
                     "checklist", "tool_results"):
        if list_key in result and isinstance(result[list_key], list):
            result[list_key] = [
                _sanitize_agent_data(item) if isinstance(item, dict) else item
                for item in result[list_key]
            ]

    return result


def _coerce_str_list(value) -> list:
    """Coerce value to list[str]."""
    if isinstance(value, str):
        return [value] if value else []
    if isinstance(value, list):
        return [
            str(item) if not isinstance(item, str) else item
            for item in value
        ]
    return []


def _coerce_int(value) -> int:
    """Coerce value to int, handling strings like '85%' and floats."""
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        # Likely 0.0-1.0 scale: convert to 0-100
        if 0.0 < value <= 1.0:
            return round(value * 100)
        return round(value)
    if isinstance(value, str):
        digits = "".join(c for c in value if c.isdigit() or c == ".")
        if digits:
            try:
                return round(float(digits))
            except ValueError:
                pass
    return 0


def _coerce_bool(value) -> bool:
    """Coerce various truthy/falsy values to bool."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.lower() in ("true", "yes", "1", "pass", "met", "active")
    return bool(value)
