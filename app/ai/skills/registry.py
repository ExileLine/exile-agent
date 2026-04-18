from __future__ import annotations

from collections.abc import Sequence

from app.ai.exceptions import SkillConfigurationError, SkillNotFoundError
from app.ai.skills.models import SkillManifest


class SkillRegistry:
    """Skill 注册表。

    loader 负责“从哪里读”，registry 负责“系统里当前有哪些 skill”。
    """

    def __init__(self, skills: Sequence[SkillManifest] | None = None) -> None:
        self._skills_by_name: dict[str, SkillManifest] = {}
        for skill in skills or []:
            self.register(skill)

    def register(self, skill: SkillManifest) -> None:
        if skill.name in self._skills_by_name:
            raise SkillConfigurationError(f"重复的 skill name: {skill.name}")
        self._skills_by_name[skill.name] = skill

    def list_skills(
        self,
        *,
        enabled_only: bool = False,
        agent_id: str | None = None,
    ) -> list[SkillManifest]:
        skills = list(self._skills_by_name.values())
        if enabled_only:
            skills = [skill for skill in skills if skill.enabled]
        if agent_id is not None:
            skills = [skill for skill in skills if skill.allows_agent(agent_id)]
        return sorted(skills, key=lambda item: (-item.priority, item.name))

    def get(self, skill_name: str) -> SkillManifest | None:
        return self._skills_by_name.get(skill_name)

    def require(self, skill_name: str) -> SkillManifest:
        skill = self.get(skill_name)
        if skill is None:
            raise SkillNotFoundError(f"未找到 skill: {skill_name}")
        return skill
