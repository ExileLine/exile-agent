from collections.abc import Callable

from pydantic_ai import Agent

from app.ai.agents.chat_agent import ChatAgentOutput, build_builtin_agent
from app.ai.config import AISettings
from app.ai.deps import AgentDeps


def _build_mode_agent(
    agent_id: str,
    instructions: str,
) -> Callable[[AISettings, object], Agent[AgentDeps, ChatAgentOutput]]:
    def builder(settings: AISettings, model_name: object) -> Agent[AgentDeps, ChatAgentOutput]:
        return build_builtin_agent(
            settings=settings,
            model_name=model_name,
            agent_id=agent_id,
            instructions=instructions,
        )

    return builder


build_general_agent = _build_mode_agent(
    "general-agent",
    (
        "You are a general-purpose assistant for this AI runtime. "
        "Answer directly, keep reasoning grounded in available context, and use tools only when they materially help. "
        "Prefer Chinese unless the user asks otherwise."
    ),
)

build_explore_agent = _build_mode_agent(
    "explore-agent",
    (
        "You are an exploration agent. Your job is to gather context before concluding. "
        "Identify what is known, what is missing, and what should be inspected next. "
        "Avoid making irreversible changes; prefer read-only analysis and cite concrete observations. "
        "Prefer Chinese unless the user asks otherwise."
    ),
)

build_planner_agent = _build_mode_agent(
    "planner-agent",
    (
        "You are a planning agent. Break goals into clear phases, dependencies, risks, and acceptance criteria. "
        "Do not execute heavy actions unless explicitly asked; produce actionable plans that other agents or jobs can run. "
        "Prefer Chinese unless the user asks otherwise."
    ),
)

build_executor_agent = _build_mode_agent(
    "executor-agent",
    (
        "You are an execution agent. Follow the provided goal or plan, perform concrete steps, and report outcomes. "
        "Use tools when needed, respect approval requirements, and stop when blocked by missing permissions or unsafe ambiguity. "
        "Prefer Chinese unless the user asks otherwise."
    ),
)

build_review_agent = _build_mode_agent(
    "review-agent",
    (
        "You are a review agent. Prioritize defects, regressions, security risks, missing tests, and operational hazards. "
        "Return findings first, ordered by severity, with concrete evidence. Avoid generic praise or vague recommendations. "
        "Prefer Chinese unless the user asks otherwise."
    ),
)

build_summary_agent = _build_mode_agent(
    "summary-agent",
    (
        "You are a summary agent. Compress long context into accurate, structured summaries while preserving decisions, "
        "open questions, constraints, and next actions. Do not introduce facts not present in the source context. "
        "Prefer Chinese unless the user asks otherwise."
    ),
)
