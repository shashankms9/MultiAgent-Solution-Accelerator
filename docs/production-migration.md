# Production Migration Path

The demo uses an in-memory Python dictionary for review storage and returns
generated PDFs inline as base64. When moving to production, two services
need to be introduced.

## Current Demo Architecture

| Concern | Demo Approach | Limitation |
|---------|--------------|------------|
| Review persistence | `_review_store` dict in `orchestrator.py` | Lost on restart; single-process |
| Decision storage | Same in-memory dict | Same as above |
| Generated PDFs | Base64 in JSON response | No long-term storage |
| Medical documents | Pasted into text field | No file upload |
| Audit trail | Embedded in response JSON | Not independently queryable |

## Why the Migration Is Straightforward

The store layer is abstracted behind four functions in `orchestrator.py`:

```python
store_review(request_id, request_data, response)
get_review(request_id)
list_reviews()
store_decision(request_id, decision)
```

No other module touches `_review_store` directly.

---

## PostgreSQL — Structured Data

Use PostgreSQL (or Azure Database for PostgreSQL — Flexible Server).

### Suggested Schema

```sql
CREATE TABLE reviews (
    request_id    UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    patient_name  TEXT NOT NULL,
    patient_dob   DATE NOT NULL,
    provider_npi  VARCHAR(10) NOT NULL,
    insurance_id  TEXT,
    diagnosis_codes TEXT[] NOT NULL,
    procedure_codes TEXT[] NOT NULL,
    clinical_notes TEXT NOT NULL,
    request_data  JSONB NOT NULL,
    response_data JSONB NOT NULL,
    recommendation VARCHAR(20) NOT NULL,
    confidence    NUMERIC(3,2),
    confidence_level VARCHAR(6),
    audit_justification TEXT,
    created_at    TIMESTAMPTZ DEFAULT now(),
    updated_at    TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE decisions (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    review_id       UUID NOT NULL REFERENCES reviews(request_id),
    action          VARCHAR(20) NOT NULL,
    override_decision VARCHAR(20),
    override_rationale TEXT,
    auth_number     VARCHAR(30) NOT NULL,
    letter_text     TEXT NOT NULL,
    letter_pdf_key  TEXT,
    decided_by      TEXT,
    created_at      TIMESTAMPTZ DEFAULT now(),
    CONSTRAINT one_decision_per_review UNIQUE (review_id)
);

CREATE TABLE audit_log (
    id          BIGSERIAL PRIMARY KEY,
    review_id   UUID NOT NULL REFERENCES reviews(request_id),
    event_type  VARCHAR(50) NOT NULL,
    event_data  JSONB NOT NULL,
    created_at  TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX idx_reviews_created ON reviews(created_at DESC);
CREATE INDEX idx_reviews_recommendation ON reviews(recommendation);
CREATE INDEX idx_reviews_provider ON reviews(provider_npi);
CREATE INDEX idx_audit_log_review ON audit_log(review_id);
```

### Migration Steps

1. Add `asyncpg` to `requirements.txt`
2. Add `DATABASE_URL` environment variable
3. Create `backend/app/services/database.py`
4. Update `orchestrator.py` imports
5. Run schema migration
6. Update `decision.py` for blob storage keys

---

## Azure Blob Storage — Unstructured Documents

### Container Layout

```
prior-auth-documents/
├── uploads/              # Original medical documents
│   └── {review_id}/
├── letters/              # Generated notification PDFs
│   └── {review_id}/
│       └── {auth_number}.pdf
└── audit/                # Archived audit justification docs
    └── {review_id}/
        └── audit-justification.md
```

### Documents Table

```sql
CREATE TABLE documents (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    review_id   UUID NOT NULL REFERENCES reviews(request_id),
    doc_type    VARCHAR(30) NOT NULL,
    filename    TEXT NOT NULL,
    blob_url    TEXT NOT NULL,
    content_type TEXT,
    size_bytes  BIGINT,
    uploaded_by TEXT,
    created_at  TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX idx_documents_review ON documents(review_id);
```

### Integration Steps

1. Add `azure-storage-blob` to `requirements.txt`
2. Add `AZURE_STORAGE_CONNECTION_STRING`
3. Create `backend/app/services/blob_storage.py`
4. Upload PDFs after generation
5. Store blob key in `decisions.letter_pdf_key`
6. Add `GET /api/documents/{review_id}` endpoint

---

## Additional Dependencies

| Package | Purpose |
|---------|---------|
| `asyncpg` | Async PostgreSQL driver |
| `sqlalchemy[asyncio]` | ORM layer (optional) |
| `alembic` | Database schema migrations |
| `azure-storage-blob` | Azure Blob Storage SDK |
| `azure-identity` | Managed identity auth |

## Environment Variables

```bash
# PostgreSQL
DATABASE_URL=postgresql+asyncpg://user:pass@host:5432/priorauth

# Azure Blob Storage — prefer managed identity (backend Container App has system-assigned identity)
AZURE_STORAGE_ACCOUNT_URL=https://<account>.blob.core.windows.net
# Fall back to connection string only if managed identity is not available:
# AZURE_STORAGE_CONNECTION_STRING=DefaultEndpointsProtocol=https;...
```

---

## Azure API Management — MCP Gateway

Currently each agent container calls its 3rd-party MCP servers directly over
the public internet. In production this creates several operational risks:
API keys scattered across Container App env vars, no central rate limiting,
no fallback if a 3rd-party endpoint goes down, and no audit trail of MCP
traffic.

APIM solves all of this by sitting between the agent containers and every
external MCP endpoint. **No agent code changes** — only the `MCP_*` env
vars in the Container Apps are updated to point at APIM proxy URLs instead
of the 3rd-party URLs directly.

### Architecture

```
agent-clinical  ──┐
agent-coverage  ──┤──► APIM (https://<apim>.azure-api.net/mcp/)
agent-compliance──┤         │
agent-synthesis ──┘         │── /icd10      → mcp.deepsense.ai/icd10_codes/mcp
                            │── /pubmed     → pubmed.mcp.claude.com/mcp
                            │── /trials     → mcp.deepsense.ai/clinical_trials/mcp
                            │── /npi        → mcp.deepsense.ai/npi_registry/mcp
                            └── /cms        → mcp.deepsense.ai/cms_coverage/mcp
```

### What APIM Adds

| Capability | How |
|---|---|
| API key storage | Named Values backed by Key Vault — never in Container App env vars |
| Rate limiting | `<rate-limit-by-key>` policy per MCP backend |
| Circuit breaker | `<retry>` + mock policy fallback if 3rd-party goes down |
| Upstream swap | Change the APIM backend URL without redeploying agents |
| Centralised monitoring | All MCP call volume, latency and failures in one App Insights |
| Network isolation | Agents call a private APIM endpoint; no direct internet egress needed |

### MCP Streamable HTTP Caveat

APIM buffers responses by default. MCP Streamable HTTP uses streaming POST
responses, so each API must have the following policy to pass the stream
through:

```xml
<inbound>
  <set-backend-service base-url="https://mcp.deepsense.ai/icd10_codes" />
  <!-- inject 3rd-party API key from Named Value if required -->
  <!-- <set-header name="X-API-Key" exists-action="override">
    <value>{{mcp-deepsense-api-key}}</value>
  </set-header> -->
</inbound>
<backend>
  <forward-request buffer-request-body="false" />
</backend>
<outbound>
  <!-- do not buffer the streamed response -->
</outbound>
```

`buffer-request-body="false"` is the critical line — without it APIM will
buffer the full streaming response before forwarding it to the agent, which
breaks MCP session establishment.

### Bicep Changes

Add `infra/modules/apim.bicep` with:

1. `Microsoft.ApiManagement/service` resource (Developer or Standard tier)
2. One `Microsoft.ApiManagement/service/backends` entry per MCP server
3. One `Microsoft.ApiManagement/service/apis` entry per MCP server with the
   streaming policy above
4. Named Values for any 3rd-party API keys (reference Key Vault secrets)

In `infra/main.bicep`, add the APIM module and update each agent Container
App's `MCP_*` env vars:

```bicep
// Before (direct):
{ name: 'MCP_ICD10_CODES', value: mcpIcd10CodesUrl }

// After (via APIM):
{ name: 'MCP_ICD10_CODES', value: '${apim.outputs.gatewayUrl}/mcp/icd10' }
```

### Agent Container App Env Var Changes

| Variable | Current value | APIM value |
|---|---|---|
| `MCP_ICD10_CODES` | `https://mcp.deepsense.ai/icd10_codes/mcp` | `https://<apim>.azure-api.net/mcp/icd10` |
| `MCP_PUBMED` | `https://pubmed.mcp.claude.com/mcp` | `https://<apim>.azure-api.net/mcp/pubmed` |
| `MCP_CLINICAL_TRIALS` | `https://mcp.deepsense.ai/clinical_trials/mcp` | `https://<apim>.azure-api.net/mcp/trials` |
| `MCP_NPI_REGISTRY` | `https://mcp.deepsense.ai/npi_registry/mcp` | `https://<apim>.azure-api.net/mcp/npi` |
| `MCP_CMS_COVERAGE` | `https://mcp.deepsense.ai/cms_coverage/mcp` | `https://<apim>.azure-api.net/mcp/cms` |

The agent code (`MCPStreamableHTTPTool` instantiation in each `main.py`)
does not change at all — it reads the URL from the environment variable.

---

## What NOT to Change

- **Agent containers** — the four MAF Hosted Agent containers (clinical, coverage, compliance, synthesis) call the Foundry Responses API and return JSON. They are completely unaware of the backend's storage layer.
- **Frontend** — the API contract stays the same
- **MCP server configuration** — independent of storage
- **Notification letter templates** — produce same output regardless of storage
