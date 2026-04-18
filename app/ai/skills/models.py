from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

SkillLoadStrategy = Literal["summary_only", "full_on_match"]


def _dedupe_preserve_order(values: list[str]) -> list[str]:
    deduped: list[str] = []
    seen: set[str] = set()
    for value in values:
        normalized = value.strip()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        deduped.append(normalized)
    return deduped


class SkillManifest(BaseModel):
    """本地 filesystem skill 的注册清单。

    skill 不是另一个 Agent，而是一段可按需装配的能力描述：
    - 先暴露摘要，控制 token 开销
    - 命中后再展开正文指令
    - 可声明依赖的 toolsets / MCP servers
    """

    model_config = ConfigDict(extra="ignore")

    name: str = Field(description="skill 的稳定标识")
    title: str = Field(description="展示标题")
    description: str = Field(description="skill 摘要说明")
    tags: list[str] = Field(default_factory=list, description="能力标签")
    enabled: bool = Field(default=True, description="是否启用该 skill")
    priority: int = Field(default=0, description="排序优先级，越大越优先")
    load_strategy: SkillLoadStrategy = Field(default="summary_only", description="正文加载策略")
    allowed_agents: list[str] = Field(default_factory=list, description="允许使用该 skill 的 agent 列表")
    required_toolsets: list[str] = Field(default_factory=list, description="skill 依赖的 toolset id 列表")
    required_mcp_servers: list[str] = Field(default_factory=list, description="skill 依赖的 MCP server id 列表")
    instruction_files: list[str] = Field(default_factory=lambda: ["SKILL.md"], description="正文说明文件列表")
    route_keywords: list[str] = Field(default_factory=list, description="skill 自动命中关键词")
    summary: str | None = Field(default=None, description="可选的精简摘要")
    root_dir: str = Field(description="skill 根目录绝对路径")

    @model_validator(mode="after")
    def _normalize_fields(self) -> "SkillManifest":
        self.tags = _dedupe_preserve_order(self.tags)
        self.allowed_agents = _dedupe_preserve_order(self.allowed_agents)
        self.required_toolsets = _dedupe_preserve_order(self.required_toolsets)
        self.required_mcp_servers = _dedupe_preserve_order(self.required_mcp_servers)
        self.instruction_files = _dedupe_preserve_order(self.instruction_files or ["SKILL.md"])
        self.route_keywords = _dedupe_preserve_order(self.route_keywords)
        return self

    def allows_agent(self, agent_id: str) -> bool:
        """判断当前 skill 是否允许给指定 agent 使用。"""

        return not self.allowed_agents or agent_id in self.allowed_agents

    def instruction_paths(self) -> list[Path]:
        """把相对 instruction 文件路径解析成绝对路径。"""

        root_dir = Path(self.root_dir)
        return [root_dir / relative_path for relative_path in self.instruction_files]

    def summary_text(self) -> str:
        """构造运行时注入给模型的 skill 摘要。

        摘要比完整 `SKILL.md` 更短，适合作为默认注入层；
        只有真的命中时，才进一步展开正文。
        """

        if self.summary:
            return self.summary

        summary_parts = [f"Skill: {self.title}", self.description]
        if self.tags:
            summary_parts.append(f"Tags: {', '.join(self.tags)}")
        if self.required_mcp_servers:
            summary_parts.append(f"Required MCP: {', '.join(self.required_mcp_servers)}")
        if self.required_toolsets:
            summary_parts.append(f"Required Toolsets: {', '.join(self.required_toolsets)}")
        return " | ".join(summary_parts)
