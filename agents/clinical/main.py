"""Clinical Reviewer Hosted Agent — MAF entry point.

Validates ICD-10 codes, extracts clinical indicators with confidence
scoring, searches PubMed literature and ClinicalTrials.gov, and returns
a structured clinical profile for downstream coverage assessment.

Deployed as a Foundry Hosted Agent via azure.ai.agentserver.
MCP tools (ICD-10, PubMed, ClinicalTrials) are managed by Foundry Agent Service
as project-level tool connections — no self-hosted MCP wiring needed in this code.
Structured output enforced via default_options={"response_format": ClinicalResult},
which from_agent_framework passes through to every agent.run() call.
"""
import os
from pathlib import Path

from agent_framework import SkillsProvider
from agent_framework.azure import AzureOpenAIResponsesClient
from azure.ai.agentserver.agentframework import from_agent_framework
from azure.identity import DefaultAzureCredential
from dotenv import load_dotenv

from schemas import ClinicalResult

load_dotenv(override=True)  # override=True required for Foundry-deployed env vars


def main() -> None:
    # --- Observability: export MAF spans to App Insights / Foundry portal traces ---
    _ai_conn = os.environ.get("APPLICATION_INSIGHTS_CONNECTION_STRING")
    if _ai_conn:
        try:
            from azure.monitor.opentelemetry import configure_azure_monitor
            from agent_framework.observability import enable_instrumentation
            # Sets the cloud role name shown on the Application Map node.
            # Use setdefault so an explicit OTEL_SERVICE_NAME env var always wins.
            os.environ.setdefault("OTEL_SERVICE_NAME", "agent-clinical")
            configure_azure_monitor(connection_string=_ai_conn)
            enable_instrumentation()
        except Exception:  # best-effort — never crash the agent
            pass

    # --- MCP tools are managed by Foundry Agent Service ---
    # ICD-10, PubMed, and ClinicalTrials MCP servers are registered as Foundry
    # project tool connections (see scripts/register_agents.py). The Foundry
    # Agent Service proxies MCP calls through its managed infrastructure,
    # passing any required headers (e.g. User-Agent for DeepSense servers).
    # No MCPStreamableHTTPTool definitions needed here.

    # --- Skills from local directory ---
    skills_provider = SkillsProvider(
        skill_paths=str(Path(__file__).parent / "skills")
    )

    # --- Agent using Responses API on Microsoft Foundry ---
    # default_options enforces ClinicalResult schema on every agent.run() call
    # made by from_agent_framework — token-level JSON constraint, no fence parsing.
    agent = AzureOpenAIResponsesClient(
        project_endpoint=os.environ["AZURE_AI_PROJECT_ENDPOINT"],
        deployment_name=os.environ["AZURE_OPENAI_DEPLOYMENT_NAME"],
        credential=DefaultAzureCredential(),
    ).as_agent(
        name="clinical-reviewer-agent",
        instructions=(
            "You are a Clinical Reviewer Agent for prior authorization requests. "
            "Use your clinical-review skill to validate ICD-10 codes, extract clinical "
            "indicators with confidence scoring, search supporting literature, and "
            "check for relevant clinical trials."
        ),
        tools=[],  # MCP tools injected by Foundry Agent Service at runtime
        context_providers=[skills_provider],
        default_options={"response_format": ClinicalResult},
    )

    # --- Serve as HTTP endpoint for Foundry hosting ---
    # Default port is 8088 (the Foundry Hosted Agent convention via DEFAULT_AD_PORT).
    from_agent_framework(agent).run()


if __name__ == "__main__":
    main()
