"""Coverage Assessment Hosted Agent — MAF entry point.

Verifies provider NPI, searches Medicare coverage policies via CMS MCP,
maps clinical findings to policy criteria with MET/NOT_MET/INSUFFICIENT
assessment, and returns a structured coverage evaluation.

Deployed as a Foundry Hosted Agent via azure.ai.agentserver.
MCP tools (NPI Registry, CMS Coverage) are managed by Foundry Agent Service
as project-level tool connections — no self-hosted MCP wiring needed in this code.
Structured output enforced via default_options={"response_format": CoverageResult},
which from_agent_framework passes through to every agent.run() call.
"""
import os
from pathlib import Path

from agent_framework import SkillsProvider
from agent_framework.azure import AzureOpenAIResponsesClient
from azure.ai.agentserver.agentframework import from_agent_framework
from azure.identity import DefaultAzureCredential
from dotenv import load_dotenv

from schemas import CoverageResult

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
            os.environ.setdefault("OTEL_SERVICE_NAME", "agent-coverage")
            configure_azure_monitor(connection_string=_ai_conn)
            enable_instrumentation()
        except Exception:  # best-effort — never crash the agent
            pass

    # --- MCP tools are managed by Foundry Agent Service ---
    # NPI Registry and CMS Coverage MCP servers are registered as Foundry
    # project tool connections (see scripts/register_agents.py). The Foundry
    # Agent Service proxies MCP calls through its managed infrastructure.
    # No MCPStreamableHTTPTool definitions needed here.

    # --- Skills from local directory ---
    skills_provider = SkillsProvider(
        skill_paths=str(Path(__file__).parent / "skills")
    )

    # --- Agent using Responses API on Microsoft Foundry ---
    # default_options enforces CoverageResult schema on every agent.run() call
    # made by from_agent_framework — token-level JSON constraint, no fence parsing.
    agent = AzureOpenAIResponsesClient(
        project_endpoint=os.environ["AZURE_AI_PROJECT_ENDPOINT"],
        deployment_name=os.environ["AZURE_OPENAI_DEPLOYMENT_NAME"],
        credential=DefaultAzureCredential(),
    ).as_agent(
        name="coverage-assessment-agent",
        instructions=(
            "You are a Coverage Assessment Agent for prior authorization requests. "
            "Use your coverage-assessment skill to verify provider credentials, search "
            "coverage policies, and map clinical evidence to policy criteria with "
            "MET/NOT_MET/INSUFFICIENT assessment and per-criterion confidence scoring."
        ),
        tools=[],  # MCP tools injected by Foundry Agent Service at runtime
        context_providers=[skills_provider],
        default_options={"response_format": CoverageResult},
    )

    # --- Serve as HTTP endpoint for Foundry hosting ---
    # Default port is 8088 (the Foundry Hosted Agent convention via DEFAULT_AD_PORT).
    from_agent_framework(agent).run()


if __name__ == "__main__":
    main()
