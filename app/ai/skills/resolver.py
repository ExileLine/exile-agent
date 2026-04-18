from __future__ import annotations

from dataclasses import dataclass

from app.ai.skills.loader import SkillLoader
from app.ai.skills.models import SkillManifest
from app.ai.skills.registry import SkillRegistry


@dataclass(frozen=True, slots=True)
class ResolvedSkill:
    """一次 run 最终命中的单个 skill。"""

    manifest: SkillManifest
    matched_by: tuple[str, ...]
    include_full_instructions: bool
    instructions: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class SkillResolution:
    """一次 run 的 skill 解析结果。"""

    skills: tuple[ResolvedSkill, ...]
    instructions: tuple[str, ...]
    required_toolset_ids: tuple[str, ...]
    required_mcp_server_ids: tuple[str, ...]

    @property
    def skill_names(self) -> list[str]:
        return [item.manifest.name for item in self.skills]


class SkillResolver:
    """根据 agent / message / 显式 skill 选择当前 run 应装配的 skills。"""

    def __init__(self, *, registry: SkillRegistry, loader: SkillLoader) -> None:
        self.registry = registry
        self.loader = loader

    def resolve(
        self,
        *,
        agent_id: str,
        message: str | None,
        skill_ids: list[str] | None = None,
        skill_tags: list[str] | None = None,
    ) -> SkillResolution:
        normalized_message = _normalize_text(message or "")
        requested_skill_ids = _dedupe(skill_ids or [])
        requested_skill_tags = {_normalize_text(tag) for tag in skill_tags or [] if _normalize_text(tag)}

        selected: list[ResolvedSkill] = []
        selected_names: set[str] = set()

        for skill_name in requested_skill_ids:
            skill = self.registry.require(skill_name)
            if not skill.enabled or not skill.allows_agent(agent_id):
                continue
            selected.append(self._build_resolved_skill(skill=skill, normalized_message=normalized_message, reason="id"))
            selected_names.add(skill.name)

        for skill in self.registry.list_skills(enabled_only=True, agent_id=agent_id):
            if skill.name in selected_names:
                continue

            matched_reasons: list[str] = []
            # tag 主要用于“缩小候选范围”，message 命中则代表这轮请求真的触达了 skill 场景。
            normalized_tags = {_normalize_text(tag) for tag in skill.tags}
            if requested_skill_tags and requested_skill_tags.intersection(normalized_tags):
                matched_reasons.append("tag")
            if self._matches_message(skill, normalized_message):
                matched_reasons.append("message")

            if not matched_reasons:
                continue

            selected.append(
                self._build_resolved_skill(
                    skill=skill,
                    normalized_message=normalized_message,
                    reason="+".join(matched_reasons),
                )
            )
            selected_names.add(skill.name)

        selected.sort(key=lambda item: (-item.manifest.priority, item.manifest.name))

        instructions: list[str] = []
        required_toolset_ids: list[str] = []
        required_mcp_server_ids: list[str] = []

        for skill in selected:
            instructions.extend(skill.instructions)
            required_toolset_ids.extend(skill.manifest.required_toolsets)
            required_mcp_server_ids.extend(skill.manifest.required_mcp_servers)

        return SkillResolution(
            skills=tuple(selected),
            instructions=tuple(_dedupe(instructions)),
            required_toolset_ids=tuple(_dedupe(required_toolset_ids)),
            required_mcp_server_ids=tuple(_dedupe(required_mcp_server_ids)),
        )

    def _build_resolved_skill(
        self,
        *,
        skill: SkillManifest,
        normalized_message: str,
        reason: str,
    ) -> ResolvedSkill:
        summary_instruction = (
            f"[Skill Summary | {skill.name}] {skill.summary_text()}"
        )

        instructions = [summary_instruction]
        include_full_instructions = skill.load_strategy == "full_on_match" and (
            reason != "tag" or self._matches_message(skill, normalized_message) or reason == "id"
        )
        if include_full_instructions:
            full_text = self.loader.load_instruction_text(skill)
            if full_text:
                instructions.append(f"[Skill Instructions | {skill.name}]\n{full_text}")

        return ResolvedSkill(
            manifest=skill,
            matched_by=tuple(reason.split("+")),
            include_full_instructions=include_full_instructions,
            instructions=tuple(instructions),
        )

    @staticmethod
    def _matches_message(skill: SkillManifest, normalized_message: str) -> bool:
        if not normalized_message:
            return False
        for keyword in _build_match_keywords(skill):
            if keyword in normalized_message:
                return True
        return False


def _build_match_keywords(skill: SkillManifest) -> list[str]:
    return _dedupe(
        [
            *[_normalize_text(keyword) for keyword in skill.route_keywords],
            *[_normalize_text(tag) for tag in skill.tags],
            _normalize_text(skill.name),
            _normalize_text(skill.title),
        ]
    )


def _dedupe(values: list[str]) -> list[str]:
    deduped: list[str] = []
    seen: set[str] = set()
    for value in values:
        normalized = value.strip()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        deduped.append(normalized)
    return deduped


def _normalize_text(value: str) -> str:
    return " ".join(value.casefold().split())
