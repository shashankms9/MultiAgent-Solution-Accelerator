"""Compliance Validation Hosted Agent — MAF entry point.

Validates documentation completeness for prior authorization requests
using an 8-item checklist. Uses no external tools — pure reasoning
over the submitted request data.

Deployed as a Foundry Hosted Agent via azure.ai.agentserver.
Structured output enforced via default_options={"response_format": ComplianceResult},
which from_agent_framework passes through to every agent.run() call.
"""
import os
from pathlib import Path

from agent_framework import SkillsProvider
from agent_framework.azure import AzureOpenAIResponsesClient
from azure.ai.agentserver.agentframework import from_agent_framework
from azure.identity import DefaultAzureCredential
from dotenv import load_dotenv

from schemas import ComplianceResult

load_dotenv(override=True)  # override=True required for Foundry-deployed env vars


def main() -> None:
    # --- Observability: env var setup for Foundry agentserver adapter ---
    _ai_conn = os.environ.get("APPLICATION_INSIGHTS_CONNECTION_STRING")
    if _ai_conn:
        os.environ.setdefault("APPLICATIONINSIGHTS_CONNECTION_STRING", _ai_conn)
        print("[observability] App Insights connection string set for agent-compliance")
    else:
        print("[observability] APPLICATION_INSIGHTS_CONNECTION_STRING not set — telemetry disabled")
    os.environ.setdefault("OTEL_SERVICE_NAME", "agent-compliance")

    # --- No MCP tools — compliance check is pure reasoning ---

    # --- Skills from local directory ---
    skills_provider = SkillsProvider(
        skill_paths=str(Path(__file__).parent / "skills")
    )

    # --- Agent using Responses API on Microsoft Foundry ---
    # default_options enforces ComplianceResult schema on every agent.run() call
    # made by from_agent_framework — token-level JSON constraint, no fence parsing.
    agent = AzureOpenAIResponsesClient(
        project_endpoint=os.environ["AZURE_AI_PROJECT_ENDPOINT"],
        deployment_name=os.environ["AZURE_OPENAI_DEPLOYMENT_NAME"],
        credential=DefaultAzureCredential(),
    ).as_agent(
        name="compliance-agent",
        instructions=(
            "You are a Compliance Validation Agent for prior authorization requests. "
            "Use your compliance-review skill to validate documentation completeness "
            "using the 8-item checklist. You have NO tools — analyze only the request "
            "data provided in the prompt."
        ),
        tools=[],
        context_providers=[skills_provider],
        default_options={"response_format": ComplianceResult},
    )

    # --- Serve as HTTP endpoint for Foundry hosting ---
    # Default port is 8088 (the Foundry Hosted Agent convention via DEFAULT_AD_PORT).
    from_agent_framework(agent).run()


if __name__ == "__main__":
    main()
