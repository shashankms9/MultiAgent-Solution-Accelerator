# Technical Notes

## Architecture Overview

The backend is a **pure HTTP dispatcher** (FastAPI). It has no local AI runtime.
All specialist reasoning runs in four independent Foundry Hosted Agent containers.

```
Frontend (Next.js / ACA)
  └── POST /api/review/stream   (SSE)
        └── FastAPI Backend / Orchestrator (ACA)
              │
              ├─── [Docker Compose — local dev] ──────────────────────────────────────
              │    POST http://agent-{name}/responses   (HOSTED_AGENT_*_URL)
              │    → Clinical / Compliance / Coverage / Synthesis Container
              │
              └─── [Foundry Hosted Agents — production (azd up)] ──────────────────
                   POST {AZURE_AI_PROJECT_ENDPOINT}/responses
                   with  agent_reference: {name, type: "agent_reference"}
                         Authorization: Bearer <DefaultAzureCredential>
                   → Foundry Agent Service → registered agent containers
```

Each agent container runs **Microsoft Agent Framework (MAF)** via
`azure.ai.agentserver.agentframework.from_agent_framework`, exposes an HTTP
endpoint, and is registered with **Microsoft Foundry** as a Hosted Agent via
`scripts/register_agents.py`.

---

## MCP Tool Connections

MCP tools use a **hybrid wiring** strategy:

1. **In-container wiring** — each agent's `main.py` creates `MCPStreamableHTTPTool`
   instances with a shared `httpx.AsyncClient` (including `User-Agent: claude-code/1.0`
   for DeepSense CloudFront routing). Tools are passed via `tools=[...]` to `.as_agent()`
   and called directly during inference.
2. **Foundry project connections** — `scripts/register_agents.py` also registers `MCPTool`
   references as Foundry project-level tool connections for portal visibility and proxy
   routing.

This dual approach ensures agents work in both Docker Compose (direct HTTP) and
Foundry Hosted Agent (managed proxy) modes.

---

## Agent Skills

Each agent loads its SKILL.md via `SkillsProvider`:

```python
skills_provider = SkillsProvider(
    skill_paths=str(Path(__file__).parent / "skills")
)
```

SKILL.md files live alongside the agent:

```
agents/
  clinical/skills/clinical-review/SKILL.md      # ICD-10 validation, clinical extraction (< 60% warning), literature + trials
  coverage/skills/coverage-assessment/SKILL.md  # Provider NPI, specialty-procedure match, CMS policy, criteria mapping
  compliance/skills/compliance-review/SKILL.md  # 10-item checklist (items 9: NCCI, 10: service type are non-blocking)
  synthesis/skills/synthesis-decision/SKILL.md  # Gate rubric, weighted confidence, synthesis_audit_trail output
```

---

## Structured Output

Each agent container declares a local Pydantic model in `schemas.py` and
passes it via MAF's `default_options` parameter:

```python
agent = (
    AzureOpenAIResponsesClient(...)
    .as_agent(
        name="clinical-reviewer-agent",
        id="clinical-reviewer-agent",  # Must match registered name for Foundry Traces
        tools=[...],
        default_options={"response_format": ClinicalResult},
    )
)
```

MAF enforces the schema as a token-level JSON constraint at inference time —
no post-processing or regex extraction needed. The backend dispatcher reads
the text payload from the OpenAI SDK response:

```python
# hosted_agents.py
output_text = response.output_text
return json.loads(output_text)
```

The Pydantic models live in each agent container:

| Agent | Schema file | Root model |
|-------|-------------|------------|
| Clinical | `agents/clinical/schemas.py` | `ClinicalResult` |
| Compliance | `agents/compliance/schemas.py` | `ComplianceResult` |
| Coverage | `agents/coverage/schemas.py` | `CoverageResult` |
| Synthesis | `agents/synthesis/schemas.py` | `SynthesisOutput` (includes `synthesis_audit_trail: str` — JSON-encoded audit trail with `gate_results` and `confidence_components`; parsed back to `dict` by the orchestrator) |

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

All five processes — the FastAPI backend and all four agent containers — export
OpenTelemetry traces and metrics to **Azure Application Insights** via
`azure-monitor-opentelemetry`. Agent traces are also visible in the Foundry
portal's built-in Traces view when App Insights is linked to the Foundry project.

### Process Roles

| Process | `OTEL_SERVICE_NAME` | What it instruments |
|---------|---------------------|---------------------|
| FastAPI backend | `prior-auth-backend` | HTTP requests/responses, outgoing httpx calls to agents, logs, exceptions, live metrics |
| Clinical agent | `agent-clinical` | MAF `invoke_agent`, `chat`, `execute_tool` spans, token metrics |
| Coverage agent | `agent-coverage` | Same as above |
| Compliance agent | `agent-compliance` | Same as above |
| Synthesis agent | `agent-synthesis` | Same as above |

Each process configures observability differently based on its role:

- **Backend** (`observability.py`): Calls `configure_azure_monitor()` directly
  before the FastAPI app starts. This is the standard Azure Monitor SDK pattern
  for non-MAF applications.
- **Agent containers**: Do NOT call `configure_azure_monitor()` manually.
  Instead, the Foundry agentserver adapter's `init_tracing()` method (called
  internally by `from_agent_framework(agent).run()`) handles the full OTel
  setup — creating exporters, calling `configure_otel_providers()`, and
  enabling MAF instrumentation. Agent code only sets env vars before the
  adapter runs.

> **Why agents don't call `configure_azure_monitor()` directly:** The adapter's
> `init_tracing()` calls `configure_otel_providers()` which **replaces** any
> existing OTel providers. If agent code calls `configure_azure_monitor()` first,
> the adapter overwrites it — creating a conflict where traces go to App Insights
> but the Foundry portal can't correlate them (Trace ID = "--", Duration = "--").
> Letting the adapter handle everything avoids this conflict.

### Content Recording (Sensitive Data)

The adapter's `configure_otel_providers()` hard-codes `enable_sensitive_data=True`,
which records full LLM prompts, tool arguments, and results in telemetry spans.
This cannot currently be overridden via environment variable — the MAF env var
`ENABLE_SENSITIVE_DATA` is ignored because the adapter passes the value explicitly.

> **⚠️ Production consideration:** With `enable_sensitive_data=True`, PA request
> content (patient names, DOBs, diagnoses, clinical notes) will be stored in
> Application Insights telemetry. Ensure your App Insights resource has
> appropriate access controls and data retention policies. If this is a concern,
> contact the `azure-ai-agentserver` team about making this configurable, or
> reduce App Insights data retention to the minimum required period.

### Application Map

Because `OTEL_SERVICE_NAME` is set in every process, App Insights
**Application Map** renders a clean 5-node topology:

```
prior-auth-backend
  ├──► agent-compliance
  ├──► agent-clinical
  ├──► agent-coverage
  └──► agent-synthesis
```

Edges are drawn from the backend's auto-instrumented outgoing httpx dependency
spans. W3C trace context headers propagate across process boundaries so App
Insights stitches the end-to-end call graph automatically — no manual
correlation ID wiring is needed.

`OTEL_SERVICE_NAME` is set via `os.environ.setdefault(...)` so an explicit
env var configured in the Container App (e.g., via Bicep or the ACA portal)
always overrides the in-code default.

### Trace Hierarchy (backend layer)

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

### MAF Spans (agent layer — all four containers)

| Span | Emitted by | Key attributes |
|------|-----------|----------------|
| `invoke_agent` | MAF | agent name, status |
| `chat` | MAF | model deployment, turn index |
| `execute_tool` | MAF | tool name, tool result status |

These spans are children of the backend `*_agent_dispatch` dependency spans,
creating an end-to-end trace from HTTP request → backend orchestration → agent
tool calls.

### Custom Backend Span Attributes

| Span | Key attributes |
|------|---------------|
| `prior_auth_review` | `request_id` |
| `phase_1_parallel` | `compliance_status`, `clinical_status` |
| `phase_2_coverage` | `coverage_status` |
| `phase_3_synthesis` | `recommendation`, `confidence` |
| `phase_4_audit` | `confidence`, `confidence_level` |

### Enabling Observability

Set the same connection string in all five containers (Bicep injects this
automatically from the shared `monitoring` module output):

```env
APPLICATION_INSIGHTS_CONNECTION_STRING=InstrumentationKey=<key>;IngestionEndpoint=...
```

**Important: Dual env var names for agent containers.** The Foundry agentserver
adapter reads a different env var name than the Azure Monitor SDK:

| Package | Env var name | Convention |
|---------|-------------|------------|
| `azure-monitor-opentelemetry` (backend) | `APPLICATION_INSIGHTS_CONNECTION_STRING` | Azure Monitor SDK |
| `azure-ai-agentserver` (Foundry adapter) | `APPLICATIONINSIGHTS_CONNECTION_STRING` | Azure App Service |

Agent code bridges this by calling `os.environ.setdefault("APPLICATIONINSIGHTS_CONNECTION_STRING", ...)`
when `APPLICATION_INSIGHTS_CONNECTION_STRING` is set. Both env vars are also
passed to agent containers via `register_agents.py`. Without the adapter-expected
name, the adapter's `init_tracing()` skips setup entirely and the Foundry portal
shows empty Trace ID / Duration / Tokens and "0/3 monitoring features enabled."

Locally (docker-compose), the variable is intentionally absent — all
observability blocks are no-ops so the app runs without App Insights.

---

## Hosted Agent Dispatch Settings

`hosted_agents.py` automatically selects the dispatch mode based on environment:

- **URL set** (`HOSTED_AGENT_*_URL`): direct HTTP to the container — Docker Compose mode
- **URL empty + `AZURE_AI_PROJECT_ENDPOINT` set**: Foundry `agent_reference` routing — production mode

**Docker Compose mode** — `HOSTED_AGENT_*_URL` vars (defaults already in `docker-compose.yml`):

| Agent | Variable | Default |
|-------|----------|---------| 
| Clinical | `HOSTED_AGENT_CLINICAL_URL` | `http://agent-clinical:8088` |
| Compliance | `HOSTED_AGENT_COMPLIANCE_URL` | `http://agent-compliance:8088` |
| Coverage | `HOSTED_AGENT_COVERAGE_URL` | `http://agent-coverage:8088` |
| Synthesis | `HOSTED_AGENT_SYNTHESIS_URL` | `http://agent-synthesis:8088` |

Shared: `HOSTED_AGENT_TIMEOUT_SECONDS` (default `180`).

**Foundry Hosted Agents mode** — injected automatically by Bicep via `azd up`:

| Variable | Value |
|----------|-------|
| `AZURE_AI_PROJECT_ENDPOINT` | `https://<account>.services.ai.azure.com/api/projects/<project>` |
| `HOSTED_AGENT_CLINICAL_NAME` | `clinical-reviewer-agent` |
| `HOSTED_AGENT_COMPLIANCE_NAME` | `compliance-agent` |
| `HOSTED_AGENT_COVERAGE_NAME` | `coverage-assessment-agent` |
| `HOSTED_AGENT_SYNTHESIS_NAME` | `synthesis-agent` |

Token acquisition uses `azure.identity.aio.DefaultAzureCredential` — no manual token configuration needed.

The following RBAC roles are automatically assigned during `azd up`:

| **Role** | **Principal** | **Scope** | **How Assigned** | **Purpose** |
|----------|---------------|-----------|------------------|-------------|
| Cognitive Services OpenAI User | Backend Container App managed identity | Foundry account | `role-assignments.bicep` (provision) | Orchestrator calls Foundry Responses API with `agent_reference` routing |
| AcrPull | Foundry project managed identity | Container Registry | `role-assignments.bicep` (provision) | Foundry Agent Service pulls agent container images from ACR |
| Cognitive Services OpenAI Contributor | Foundry project managed identity | Foundry account | `role-assignments.bicep` (provision) | Hosted agent containers call gpt-5.4 via the Responses API |
| Azure AI User | Foundry project managed identity | Foundry account | `role-assignments.bicep` (provision) | Hosted agent containers use Foundry Agent Service data actions |
| Azure AI User | Deployer (user running `azd up`) | Foundry project | `az role assignment create` (postprovision hook) | `register_agents.py` registers agents via Foundry Agent Service API |
| Azure AI User | Backend Container App managed identity | Foundry project | `az role assignment create` (postprovision hook) | Backend calls Foundry Hosted Agents at runtime via `DefaultAzureCredential` |

The first four roles are assigned by `infra/modules/role-assignments.bicep` during `azd provision`. The remaining Azure AI User roles are assigned via `az role assignment create` in the postprovision hook — this is intentionally outside Bicep because the CLI command is natively idempotent (no error if the role was previously granted manually).

> **First-run note:** Azure RBAC propagation can take up to several minutes after a new role assignment. On the very first `azd up` (when the Azure AI User role is newly created), the postprovision hook automatically retries `register_agents.py` every 10 seconds (up to 12 attempts) until the permission propagates. On subsequent runs the role already exists and no retries are needed.

---

## Agent Registration

After `azd provision`, `scripts/register_agents.py` (called from the `azure.yaml` postprovision hook)
registers all four agents with Foundry:

1. Calls `azure-ai-projects` SDK `client.agents.create_version()` with the ACR container image
   and resource specs from `agent.yaml`
2. Calls `az cognitiveservices agent start` to start each agent under Foundry management

Resource specs (defined in each `agents/<name>/agent.yaml`):

| Agent | CPU | Memory |
|-------|-----|--------|
| `clinical-reviewer-agent` | `1` | `2Gi` |
| `coverage-assessment-agent` | `1` | `2Gi` |
| `compliance-agent` | `0.5` | `1Gi` |
| `synthesis-agent` | `1` | `2Gi` |

---

## Agent IDs (Foundry)

| Agent ID | Module |
|----------|--------|
| `compliance-agent` | `agents/compliance/main.py` |
| `clinical-reviewer-agent` | `agents/clinical/main.py` |
| `coverage-assessment-agent` | `agents/coverage/main.py` |
| `synthesis-agent` | `agents/synthesis/main.py` |
