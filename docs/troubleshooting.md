# Troubleshooting

## "Failed to start Claude SDK client: Failed to start Claude Code:"

All three agents fail with an empty error message on Windows.

**Cause 1 — CMD bypass:** The Claude Code CLI is installed as a `.CMD`
batch file wrapper. When the SDK spawns it as a subprocess, `cmd.exe`
mangles newlines and special characters in the `--system-prompt` argument.

**Cause 2 — Missing Foundry auth:** The Claude Code CLI requires
Foundry-specific env vars (`CLAUDE_CODE_USE_FOUNDRY=true`,
`ANTHROPIC_FOUNDRY_API_KEY`, `ANTHROPIC_FOUNDRY_BASE_URL`) for Azure
authentication.

**Cause 3 — Wrong asyncio event loop:** On Windows, uvicorn with
`--reload` may use `SelectorEventLoop` which does not support
`asyncio.create_subprocess_exec()`.

**Fix:** The `app/patches/__init__.py` module patches all three issues
automatically. Restart the uvicorn server after pulling updates:

```bash
cd backend
uvicorn app.main:app --reload
```

Verify the patches are applied by checking the server log for:
```
[patches] Applying SDK patches...
[patches] Set WindowsProactorEventLoopPolicy (subprocess support)
[patches] Set ANTHROPIC_API_KEY from AZURE_FOUNDRY_API_KEY (len=84)
[patches] Set CLAUDE_CODE_USE_FOUNDRY=true + Foundry credentials
[patches] Applied Windows CLI patch: ...node.EXE ...cli.js (bypassing .CMD wrapper)
[patches] All patches applied.
```

> **Note:** These issues are Windows-only. Container deployments on
> Linux are not affected.

---

## Agents Return Empty Responses (cost $0)

Agents connect successfully but produce no output.

**Cause:** When running inside a Claude Code editor session (VS Code), the
environment contains a local-proxy API key that doesn't work for child
processes.

**Fix:** Ensure `AZURE_FOUNDRY_API_KEY` and `AZURE_FOUNDRY_ENDPOINT` are
set in `backend/.env`. Check for these log lines:

```
[patches] Set ANTHROPIC_API_KEY from AZURE_FOUNDRY_API_KEY (len=84)
[patches] Set CLAUDE_CODE_USE_FOUNDRY=true + Foundry credentials
```

---

## "Failed to proxy" / ECONNREFUSED / "Review failed"

The frontend shows an error when submitting a review.

**Cause:** The backend server is not running, or the frontend is not
configured to reach it.

**Fix:**

1. Ensure the backend is running:
   ```bash
   cd backend
   uvicorn app.main:app --reload
   ```

2. Ensure `frontend/.env.local` has the correct backend URL:
   ```
   NEXT_PUBLIC_API_BASE=http://localhost:8000/api
   ```

3. Restart the frontend dev server after changing `.env.local`:
   ```bash
   cd frontend
   npm run dev
   ```

---

## Port Stuck After Killing Server (Windows)

After killing a server process, the port remains in LISTENING state.

**Cause:** Windows TCP socket lingering.

**Fix:** Wait 2-4 minutes for the socket to clear, or use a different port.

---

## Agent Returns Truncated/Incomplete Response

One or more agents return partial data.

**Cause:** The `agent_framework_claude` package may not propagate
`structured_output` from the CLI's `ResultMessage` to `AgentResponse`.

**Symptoms in server logs:**

```
[parse] text length=414
[parse] Strategy 1: no fences found or none parsed
[parse] Strategy 2 (brace-match backward) succeeded
[DIAG] Saved Clinical raw result (4 keys)
```

A normal Clinical result has 6+ top-level keys and 2000-4000 characters.

**Mitigations (in place):**

1. **`max_turns`** — explicit limits (15 for Clinical/Coverage, 5 for Compliance/Synthesis)
2. **Result validation** — checks for expected top-level keys
3. **Automatic retry** — retries once if validation fails:
   ```
   WARNING: Clinical Reviewer Agent returned incomplete result (attempt 1/2).
   Missing keys: clinical_extraction, clinical_summary. Retrying...
   ```
4. **SSE warnings** — surfaces validation warnings to the frontend

---

## Troubleshooting Foundry Traces

If traces don't appear in Foundry:

- Verify the Foundry project has Application Insights configured
- If App Insights was added after agent registration, unregister and re-register
- Verify your backend sends traces to the **same** Application Insights resource
- Verify spans include `gen_ai.agent.id` matching the registered agent ID
