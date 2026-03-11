# Technical Notes

## Architecture Overview

The backend is a **pure HTTP dispatcher** (FastAPI). It has no local AI runtime.
All specialist reasoning runs in four independent Foundry Hosted Agent containers.

```
Frontend (Next.js / ACA)
  └── POST /api/review/stream   (SSE)
        └── FastAPI Backend / Orchestrator (ACA)
              ├── POST http://agent-clinical/   → Clinical Reviewer Container
              ├── POST http://agent-compliance/ → Compliance Validation Container
              ├── POST http://agent-coverage/   → Coverage Assessment Container
              └── POST http://agent-synthesis/  → Synthesis Decision Container
```

Each agent container runs **Microsoft Agent Framework (MAF)** via
`azure.ai.agentserver.agentframework.from_agent_framework`, exposes an HTTP
endpoint, and is deployed to **Azure AI Foundry** as a Hosted Agent.

---

## MCP Header Injection

The DeepSense-hosted MCP servers require `User-Agent: claude-code/1.0` due
to CloudFront routing rules. This is injected in each agent container via a
single shared `httpx.AsyncClient`:

```python
_MCP_HTTP_CLIENT = httpx.AsyncClient(headers={"User-Agent": "claude-code/1.0"})

icd10_tool = MCPStreamableHTTPTool(
    name="icd10-codes",
    url=os.environ["MCP_ICD10_CODES"],
    http_client=_MCP_HTTP_CLIENT,
)
```

The MCP servers are **self-hosted** — no Foundry Tool MCP registration needed.
The containers connect directly to the MCP server URLs via environment variables.

---

## Agent Skills

Each agent loads its SKILL.md via `FileAgentSkillsProvider`:

```python
skills_provider = FileAgentSkillsProvider(
    skill_paths=str(Path(__file__).parent / "skills")
)
```

SKILL.md files live alongside the agent:

```
agents/
  clinical/skills/clinical-review/SKILL.md
  coverage/skills/coverage-assessment/SKILL.md
  compliance/skills/compliance-review/SKILL.md
  synthesis/skills/synthesis-decision/SKILL.md
```

---

## Structured Output and Response Normalization

The `pydantic_to_output_format()` helper in `backend/app/agents/_parse.py`
converts a Pydantic schema to a JSON schema dict for validation.
`parse_json_response()` normalizes the hosted agent HTTP response back
into the expected dict structure, accepting common payload envelopes:

- `{ "result": { ... } }`
- `{ "output": { ... } }`
- `{ "data": { ... } }`
- `{ ... }` (already-flat payload)

### Parse Resilience

| Strategy | Method |
|----------|--------|
| Primary  | `response.value` / `response.structured_output` |
| Fallback 1 | Markdown code fence extraction |
| Fallback 2 | Brace-matched backward extraction |
| Fallback 3 | First-`{` to last-`}` substring |

---

## Orchestration Flow

```
Phase 1 (parallel):   Compliance + Clinical agents
Phase 2 (sequential): Coverage agent (receives clinical findings)
Phase 3:              Synthesis agent (receives all three results)
Phase 4:              Audit trail + PDF generation
```

### Resilience

| Mechanism | Where | What it does |
|-----------|-------|-------------|
| Result validation | `_validate_agent_result()` | Checks expected top-level keys |
| Automatic retry | `_safe_run()` | Retries once if validation fails |
| SSE status warnings | Phase events | Reports status "warning" for incomplete results |
| Tool result normalization | `_normalize_tool_result()` | Maps non-standard status values |

### Decision Gate (LENIENT MODE)

Gate 1: Provider NPI verification → Gate 2: Code validation → Gate 3: Medical necessity

Default to **PEND** at any uncertain gate. Never DENY in LENIENT mode.

---

## Decision and Notification Flow

1. Review completes → stored in-memory (reviewed via `GET /api/reviews`)
2. Frontend shows Accept / Override panel
3. `POST /api/decision` prevents double-decisions (409)
4. Generates thread-safe authorization number (`PA-YYYYMMDD-XXXXX`)
5. Produces notification letter (approval or pend) in text and PDF

**Letter types:**
- **Approval** — auth number, 90-day validity, coverage criteria met, clinical rationale
- **Pend** — confidence level, missing documentation, 30-day deadline, appeal rights

---

## CPT/HCPCS Validation

Pre-flight step before agents execute:

1. **Format validation** — regex for CPT (5-digit) or HCPCS (letter + 4 digits)
2. **Curated lookup** — ~30 common PA-trigger codes
3. **Results injected** into synthesis prompt for Gate 2

---

## Sample Data

The frontend includes a **"Load Sample Case"** button for a CT-guided
transbronchial lung biopsy case:

| Field | Value |
|-------|-------|
| Patient | John Smith, DOB 1958-03-15 |
| Provider NPI | 1720180003 (active pulmonologist) |
| ICD-10 codes | R91.1, J18.9, R05.9 |
| CPT code | 31628 |
| Insurance ID | MCR-123456789A |

---

## Observability

The backend sends traces to **Azure Application Insights** via
`azure-monitor-opentelemetry`. Each agent container is also visible
in Foundry's built-in hosted agent evaluation dashboard.

### Trace Hierarchy

```
prior_auth_review (request_id)
  ├── phase_1_parallel
  │     ├── compliance_agent_dispatch
  │     └── clinical_agent_dispatch
  ├── phase_2_coverage
  │     └── coverage_agent_dispatch
  ├── phase_3_synthesis
  │     └── synthesis_agent_dispatch
  └── phase_4_audit
```

### Custom Span Attributes

| Span | Key attributes |
|------|---------------|
| `prior_auth_review` | `request_id` |
| `phase_1_parallel` | `compliance_status`, `clinical_status` |
| `phase_2_coverage` | `coverage_status` |
| `phase_3_synthesis` | `recommendation`, `confidence` |
| `phase_4_audit` | `confidence`, `confidence_level` |

Enable by setting:

```env
APPLICATION_INSIGHTS_CONNECTION_STRING=InstrumentationKey=<key>;IngestionEndpoint=...
```

---

## Hosted Agent Dispatch Settings

| Agent | Environment variable |
|-------|----------------------|
| Compliance | `HOSTED_AGENT_COMPLIANCE_URL` |
| Clinical | `HOSTED_AGENT_CLINICAL_URL` |
| Coverage | `HOSTED_AGENT_COVERAGE_URL` |
| Synthesis | `HOSTED_AGENT_SYNTHESIS_URL` |

Shared request configuration:

| Setting | Default |
|---------|---------|
| `HOSTED_AGENT_TIMEOUT_SECONDS` | 180 |
| `HOSTED_AGENT_AUTH_HEADER` | `Authorization` |
| `HOSTED_AGENT_AUTH_SCHEME` | `Bearer` |
| `HOSTED_AGENT_AUTH_TOKEN` | *(empty — Foundry injects at deploy time)* |

---

## Agent IDs (Foundry)

| Agent ID | Module |
|----------|--------|
| `compliance-agent` | `agents/compliance/main.py` |
| `clinical-reviewer-agent` | `agents/clinical/main.py` |
| `coverage-assessment-agent` | `agents/coverage/main.py` |
| `synthesis-decision-agent` | `agents/synthesis/main.py` |
