"""Compliance Validation Hosted Agent — MAF entry point.

Validates documentation completeness for prior authorization requests
using an 8-item checklist. Uses no external tools — pure reasoning
over the submitted request data.

Deployed as a Foundry Hosted Agent via azure.ai.agentserver.
No MCP connections required for this agent.
"""
import os
from pathlib import Path

from agent_framework import FileAgentSkillsProvider
from agent_framework.azure import AzureOpenAIResponsesClient
from azure.ai.agentserver.agentframework import from_agent_framework
from azure.identity import DefaultAzureCredential
from dotenv import load_dotenv

load_dotenv(override=True)  # override=True required for Foundry-deployed env vars


def main() -> None:
    # --- No MCP tools — compliance check is pure reasoning ---

    # --- Skills from local directory (FileAgentSkillsProvider replaces .claude/skills/) ---
    skills_provider = FileAgentSkillsProvider(
        skill_paths=str(Path(__file__).parent / "skills")
    )

    # --- Agent using Responses API on Azure AI Foundry ---
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
            "data provided in the prompt. "
            "CRITICAL: Your FINAL response MUST be a single valid JSON object "
            "inside a ```json code fence. No markdown commentary outside the fence."
        ),
        tools=[],
        context_providers=[skills_provider],
    )

    # --- Serve as HTTP endpoint for Foundry hosting ---
    from_agent_framework(agent).run()


if __name__ == "__main__":
    main()
