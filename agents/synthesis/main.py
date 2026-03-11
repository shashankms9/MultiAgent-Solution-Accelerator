"""Synthesis Decision Hosted Agent — MAF entry point.

Synthesizes outputs from Compliance, Clinical, and Coverage agents into
a final APPROVE or PEND recommendation using gate-based evaluation,
weighted confidence scoring, and a structured audit trail.

Deployed as a Foundry Hosted Agent via azure.ai.agentserver.
No MCP connections required — synthesis is pure reasoning over agent outputs.
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
    # --- No MCP tools — synthesis is pure reasoning over agent outputs ---

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
        name="synthesis-agent",
        instructions=(
            "You are the Synthesis Agent for prior authorization review. "
            "Use your synthesis-decision skill to evaluate the outputs from the "
            "Compliance, Clinical Reviewer, and Coverage agents through a strict "
            "3-gate pipeline (Provider → Codes → Medical Necessity) and produce "
            "a single APPROVE or PEND recommendation with weighted confidence scoring "
            "and a complete audit trail. "
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
