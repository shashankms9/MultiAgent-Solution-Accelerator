# Technical Notes

## Windows Claude SDK Patches (`app/patches/__init__.py`)

On Windows, the Claude Agent SDK encounters three issues when spawning the
Claude Code CLI as a subprocess. The `app/patches/__init__.py` module fixes
all three, applied automatically at server startup in `main.py`.

### Patch 1 — `.CMD` Batch File Bypass

On Windows, the Claude Code CLI is installed as a `.CMD` batch file wrapper.
When Python's `subprocess` module runs a `.CMD` file, it routes through
`cmd.exe /c`, which interprets newlines and special characters inside
`--system-prompt` arguments as command separators.

**Fix:** Monkey-patches `SubprocessCLITransport._build_command` to replace
the `.CMD` wrapper with a direct `node.exe cli.js` invocation.

### Patch 2 — API Credential Override + Foundry Auth

When running inside a Claude Code editor session, the environment inherits
invalid local-proxy credentials.

**Fix:** Overrides `ANTHROPIC_API_KEY` and `ANTHROPIC_BASE_URL` with the
real Microsoft AI Foundry credentials, and sets Foundry-specific env vars.

### Patch 3 — Model Mapping

The Claude Agent SDK reads the model from `CLAUDE_AGENT_MODEL`, not `CLAUDE_MODEL`.

**Fix:** Maps `CLAUDE_MODEL` to `CLAUDE_AGENT_MODEL`.

### Patch 4 — Windows Asyncio Event Loop (ProactorEventLoop)

On Windows with `--reload`, uvicorn may use `SelectorEventLoop` which doesn't
support `asyncio.create_subprocess_exec()`.

**Fix:** Sets `asyncio.WindowsProactorEventLoopPolicy()`.

### When Patches Activate

| Patch | Activates when | In Linux/Docker? |
|-------|---------------|-----------------|
| CMD bypass | `os.name == "nt"` and `.CMD` file detected | No |
| API credentials + Foundry auth | `AZURE_FOUNDRY_API_KEY` is set | No |
| Model mapping | `CLAUDE_MODEL` is set | Yes (harmless) |
| ProactorEventLoop | `os.name == "nt"` | No |

---

## MCP Header Injection

The DeepSense-hosted MCP servers require `User-Agent: claude-code/1.0` due
to CloudFront routing rules. This header is injected via:

- **`McpHttpServerConfig.headers`** — for Claude SDK agents (production path)
- **`httpx.AsyncClient` custom headers** — for `MCPStreamableHTTPTool` (model-agnostic path)

Azure OpenAI's Responses API native MCP support does **not** work because
Azure's proxy does not forward the `User-Agent` header.

---

## Structured Output

Agents are configured with structured output via the `output_format` option
in `ClaudeAgentOptions`. This constrains the agent's response to match the
Pydantic model's JSON schema.

### How It Works

The `pydantic_to_output_format()` helper in `_parse.py` converts a Pydantic
model to the format the SDK expects:

```python
from app.agents._parse import pydantic_to_output_format
from app.models.schemas import ClinicalResult

output_format = pydantic_to_output_format(ClinicalResult)
# Returns: {"type": "json_schema", "schema": <JSON Schema dict>}

agent = ClaudeAgent(
    instructions="...",
    default_options={
        "output_format": output_format,
        "permission_mode": "bypassPermissions",
    },
)
```

### JSON Output Enforcement

The `output_format` option is passed to the Claude Code CLI as `--json-schema`,
constraining the model to produce valid JSON before the response completes.
Since PR #4137, `structured_output` is properly propagated to `AgentResponse.value`.

### Resilience Mechanisms

| Mechanism | Where | What it does |
|-----------|-------|-------------|
| `max_turns` | Agent config | Ensures agents have enough turns (15 for Clinical/Coverage, 5 for Compliance/Synthesis) |
| Result validation | `_validate_agent_result()` | Checks expected top-level keys |
| Automatic retry | `_safe_run()` | Retries once if validation fails |
| SSE status warnings | Phase events | Reports `"status": "warning"` for incomplete results |
| Tool result normalization | `_normalize_tool_result()` | Maps non-standard status values to pass/fail/warning |

### Parse Strategies

`parse_json_response()` uses a multi-strategy approach:

| Strategy | Method | Status |
|----------|--------|--------|
| **Strategy 0** | `response.value` / `response.structured_output` | **Primary path** |
| Strategy 1 | Markdown code fence extraction | Defense-in-depth fallback |
| Strategy 2 | Brace-matched backward extraction | Fallback |
| Strategy 3 | First-`{` to last-`}` substring | Legacy fallback |

---

## Prompt Caching

Agent instructions consume ~1,200-1,500 input tokens per agent (~5,000 total
per review). In skills mode, prompts are loaded on demand. Anthropic's
prompt caching can reduce this cost by ~90%.

---

## Decision & Notification Flow

1. Review completes → stored in-memory
2. Frontend shows Accept / Override panel
3. `POST /api/decision` validates review, prevents double-decisions (409)
4. Generates thread-safe authorization number (`PA-YYYYMMDD-XXXXX`)
5. Produces notification letter (approval or pend) in text and PDF
6. PDF available for preview and download

**Notification letter types:**
- **Approval** — auth number, 90-day validity, coverage criteria met, clinical rationale
- **Pend** — confidence level, missing documentation, 30-day deadline, appeal rights

**PDF generation** (`fpdf2`):
- Custom `_LetterPDF` subclass with branded header/footer
- Color-coded titles: green for approvals, amber for pends
- Base64-encoded for JSON transport

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

## Observability — Azure Application Insights

The backend integrates with **Azure Application Insights** via
`azure-monitor-opentelemetry`.

### Trace Hierarchy

```
prior_auth_review (request_id)
  ├── phase_1_parallel
  │     ├── compliance_agent
  │     └── clinical_agent
  ├── phase_2_coverage
  │     └── coverage_agent
  ├── phase_3_synthesis
  │     └── synthesis_agent
  └── phase_4_audit
```

### Custom Span Attributes

| Span | Attributes |
|------|-----------|
| `prior_auth_review` | `request_id` |
| `phase_1_parallel` | `compliance_status`, `clinical_status` |
| `phase_2_coverage` | `coverage_status` |
| `phase_3_synthesis` | `recommendation`, `confidence` |
| `phase_4_audit` | `confidence`, `confidence_level` |

### Enabling Observability

```env
APPLICATION_INSIGHTS_CONNECTION_STRING=InstrumentationKey=<key>;IngestionEndpoint=...
```

### What You See in Application Insights

- **Application Map** — backend with dependency arrows to AI Foundry and MCP servers
- **Transaction Search** — filter by `prior_auth_review`
- **Live Metrics** — real-time request rate and latency
- **Performance** — percentile latency by phase

---

## Foundry Agent Registration

Register agents as **custom external agents** in Foundry Control Plane for
centralized observability and governance.

### Agent IDs

| Agent ID | Display Name | File |
|----------|-------------|------|
| `compliance-agent` | Compliance Validation Agent | `compliance_agent.py` |
| `clinical-reviewer-agent` | Clinical Reviewer Agent | `clinical_agent.py` |
| `coverage-assessment-agent` | Coverage Assessment Agent | `coverage_agent.py` |
| `synthesis-decision-agent` | Synthesis Decision Agent | `orchestrator.py` |

### Prerequisites

1. Foundry project at [ai.azure.com](https://ai.azure.com/)
2. AI Gateway configured
3. Application Insights linked (same resource as backend)
4. Deployed backend reachable from Foundry

### Registration Steps

See [Register and manage custom agents](https://learn.microsoft.com/en-us/azure/ai-foundry/control-plane/register-custom-agent).

---

## Known Limitations

### Gap 1 — MAF `ClaudeAgent` Inheritance (Bug Filed)

`ClaudeAgent` inherits from `BaseAgent` instead of `Agent`, skipping
`AgentTelemetryLayer`. Agent-level spans don't appear in App Insights.

### Gap 2 — Claude CLI/Agent SDK Tracing (Feature Request Filed)

The Claude Agent SDK has no OpenTelemetry span support. No visibility
into agent internals.

### Gap 3 — Trace Context Propagation

Requires both Gap 1 and Gap 2 fixes. Without it, spans float as separate traces.

### What Works Today

| Telemetry | Status |
|-----------|--------|
| Custom orchestrator phase spans | Working |
| FastAPI request traces | Working |
| Application Map, Live Metrics | Working |
| Agent-level spans | Blocked by Gap 1 |
| Agent internal spans | Blocked by Gap 2 |
| Connected trace tree | Blocked by Gap 3 |
