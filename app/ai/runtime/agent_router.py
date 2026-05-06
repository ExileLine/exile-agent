from __future__ import annotations

from dataclasses import dataclass

from app.ai.runtime.resolved_config import ResolvedAgentRoute


@dataclass(frozen=True, slots=True)
class AgentRouteRule:
    """基于关键词的内置 Agent 路由规则。"""

    agent_id: str
    reason: str
    keywords: tuple[str, ...]


class AgentRouter:
    """轻量 Agent 路由器。

    当前只做确定性关键词路由，便于测试、解释和后续迁移到 DB/LLM router。
    """

    def __init__(self, rules: tuple[AgentRouteRule, ...] | None = None) -> None:
        self.rules = rules or DEFAULT_AGENT_ROUTE_RULES
        self.parallel_worker_agent_ids = {"explore-agent", "planner-agent", "review-agent"}
        self.parallel_aggregator_agent_id = "summary-agent"
        self.max_parallel_workers = 3

    def resolve(
        self,
        *,
        requested_agent_id: str | None,
        message: str | None,
        default_agent_id: str,
    ) -> ResolvedAgentRoute:
        if requested_agent_id:
            return ResolvedAgentRoute(
                requested_agent_id=requested_agent_id,
                selected_agent_id=requested_agent_id,
                source="explicit",
                reason="请求显式指定 agent_id",
            )

        normalized_message = _normalize_message(message or "")
        matches: list[tuple[AgentRouteRule, tuple[str, ...]]] = []
        for rule in self.rules:
            matched_keywords = tuple(keyword for keyword in rule.keywords if keyword in normalized_message)
            if matched_keywords:
                matches.append((rule, matched_keywords))

        if matches:
            candidate_agent_ids = _dedupe_agent_ids([rule.agent_id for rule, _ in matches])
            matched_keywords = tuple(keyword for _, keywords in matches for keyword in keywords)
            worker_agent_ids = tuple(
                agent_id for agent_id in candidate_agent_ids if agent_id in self.parallel_worker_agent_ids
            )[: self.max_parallel_workers]
            if len(worker_agent_ids) >= 2:
                return ResolvedAgentRoute(
                    requested_agent_id=None,
                    selected_agent_id="team:auto-parallel",
                    source="router",
                    reason="命中多个可并行协同的 Agent",
                    matched_keywords=matched_keywords,
                    mode="parallel",
                    candidate_agent_ids=tuple(candidate_agent_ids),
                    worker_agent_ids=worker_agent_ids,
                    aggregator_agent_id=self.parallel_aggregator_agent_id,
                )

            first_rule, first_keywords = matches[0]
            return ResolvedAgentRoute(
                requested_agent_id=None,
                selected_agent_id=first_rule.agent_id,
                source="router",
                reason=first_rule.reason,
                matched_keywords=first_keywords,
                candidate_agent_ids=tuple(candidate_agent_ids),
            )

        return ResolvedAgentRoute(
            requested_agent_id=None,
            selected_agent_id=default_agent_id,
            source="default",
            reason="未命中路由规则，使用默认 Agent",
        )


DEFAULT_AGENT_ROUTE_RULES: tuple[AgentRouteRule, ...] = (
    AgentRouteRule(
        agent_id="review-agent",
        reason="命中审查/风险类意图",
        keywords=(
            "review",
            "审查",
            "评审",
            "风险",
            "漏洞",
            "缺陷",
            "不足",
            "隐患",
        ),
    ),
    AgentRouteRule(
        agent_id="planner-agent",
        reason="命中规划/拆解类意图",
        keywords=(
            "plan",
            "planning",
            "roadmap",
            "规划",
            "计划",
            "方案",
            "拆解",
            "阶段",
            "步骤",
            "路线图",
        ),
    ),
    AgentRouteRule(
        agent_id="summary-agent",
        reason="命中总结/摘要类意图",
        keywords=(
            "summary",
            "summarize",
            "总结",
            "摘要",
            "归纳",
            "整理",
            "提炼",
            "纪要",
        ),
    ),
    AgentRouteRule(
        agent_id="explore-agent",
        reason="命中探索/调研类意图",
        keywords=(
            "explore",
            "research",
            "investigate",
            "探索",
            "调研",
            "调查",
            "了解",
            "分析现状",
            "看看",
        ),
    ),
    AgentRouteRule(
        agent_id="executor-agent",
        reason="命中执行/实现类意图",
        keywords=(
            "execute",
            "implement",
            "run",
            "执行",
            "实现",
            "开发",
            "开始做",
            "开始实现",
            "落地",
            "修复",
        ),
    ),
)


def _normalize_message(message: str) -> str:
    return message.strip().lower()


def _dedupe_agent_ids(agent_ids: list[str]) -> list[str]:
    deduped: list[str] = []
    seen: set[str] = set()
    for agent_id in agent_ids:
        if agent_id in seen:
            continue
        seen.add(agent_id)
        deduped.append(agent_id)
    return deduped
