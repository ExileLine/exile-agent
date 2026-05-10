from pathlib import Path

from fastapi.testclient import TestClient
from pydantic_ai import models
from pydantic_ai.messages import ModelRequest, ModelResponse, TextPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from app.ai.skills import SkillLoader, SkillRegistry, SkillResolver
from app.ai.toolsets.builtin import BUILTIN_RUNTIME_TOOLSET_ID
from app.main import app

models.ALLOW_MODEL_REQUESTS = False


def _write_skill(
    root_dir: Path,
    *,
    name: str = "custom-ops-skill",
    title: str = "Custom Ops Skill",
    description: str = "处理运行时状态检查。",
    tags: list[str] | None = None,
    required_mcp_servers: list[str] | None = None,
    required_toolsets: list[str] | None = None,
    route_keywords: list[str] | None = None,
) -> None:
    skill_dir = root_dir / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    skill_dir.joinpath("skill.yaml").write_text(
        "\n".join(
            [
                f"name: {name}",
                f"title: {title}",
                f"description: {description}",
                "tags:",
                *[f"  - {tag}" for tag in (tags or ["ops"])],
                "enabled: true",
                "priority: 10",
                "load_strategy: full_on_match",
                "allowed_agents:",
                "  - chat-agent",
                "required_toolsets:",
                *[f"  - {toolset_id}" for toolset_id in (required_toolsets or [])],
                "required_mcp_servers:",
                *[f"  - {server_id}" for server_id in (required_mcp_servers or [])],
                "instruction_files:",
                "  - SKILL.md",
                "route_keywords:",
                *[f"  - {keyword}" for keyword in (route_keywords or ["健康"])],
            ]
        ),
        encoding="utf-8",
    )
    skill_dir.joinpath("SKILL.md").write_text(
        "\n".join(
            [
                "---",
                f"name: {name}",
                "description: Provides runtime status checks when users ask about health or service state.",
                "allowed-tools:",
                "  - get_runtime_config_summary",
                "---",
                "",
                "# Custom Ops Skill",
                "",
                "请优先依据工具事实判断运行时状态，不要主观猜测。",
            ]
        ),
        encoding="utf-8",
    )


def test_skill_loader_and_resolver(tmp_path: Path) -> None:
    _write_skill(
        tmp_path,
        required_toolsets=[BUILTIN_RUNTIME_TOOLSET_ID],
        required_mcp_servers=["demo"],
        route_keywords=["健康", "状态"],
    )

    loader = SkillLoader(skills_dir=tmp_path)
    manifests = loader.load_manifests()
    assert len(manifests) == 1
    assert manifests[0].name == "custom-ops-skill"
    assert manifests[0].allowed_tools == ["get_runtime_config_summary"]
    assert "runtime status checks" in manifests[0].description

    registry = SkillRegistry(manifests)
    resolver = SkillResolver(registry=registry, loader=loader)
    resolution = resolver.resolve(
        agent_id="chat-agent",
        message="请帮我检查当前健康状态",
        skill_tags=["ops"],
    )

    assert resolution.skill_names == ["custom-ops-skill"]
    assert resolution.required_toolset_ids == (BUILTIN_RUNTIME_TOOLSET_ID,)
    assert resolution.required_mcp_server_ids == ("demo",)
    assert any(item.startswith("[Skill Summary | custom-ops-skill]") for item in resolution.instructions)
    assert any("不要主观猜测" in item for item in resolution.instructions)
    assert all("allowed-tools" not in item for item in resolution.instructions)


def test_skill_loader_supports_skill_md_without_legacy_yaml(tmp_path: Path) -> None:
    skill_dir = tmp_path / "report-writer"
    skill_dir.mkdir(parents=True)
    skill_dir.joinpath("SKILL.md").write_text(
        "\n".join(
            [
                "---",
                "name: report-writer",
                "description: Helps write concise operational reports when users ask for status summaries.",
                "allowed-tools: Read",
                "---",
                "",
                "# Report Writer",
                "",
                "Use short sections and evidence-backed conclusions.",
            ]
        ),
        encoding="utf-8",
    )

    loader = SkillLoader(skills_dir=tmp_path)
    manifests = loader.load_manifests()

    assert len(manifests) == 1
    assert manifests[0].name == "report-writer"
    assert manifests[0].title == "Report Writer"
    assert manifests[0].allowed_tools == ["Read"]
    assert loader.load_instruction_text(manifests[0]).startswith("# Report Writer")


def test_skill_loader_keeps_legacy_yaml_compatibility(tmp_path: Path) -> None:
    skill_dir = tmp_path / "legacy-skill"
    skill_dir.mkdir(parents=True)
    skill_dir.joinpath("skill.yaml").write_text(
        "\n".join(
            [
                "name: legacy-skill",
                "title: Legacy Skill",
                "description: Handles legacy skill definitions that still use skill.yaml metadata.",
                "load_strategy: full_on_match",
                "route_keywords:",
                "  - legacy",
            ]
        ),
        encoding="utf-8",
    )
    skill_dir.joinpath("SKILL.md").write_text(
        "# Legacy Skill\n\nThis body has no YAML frontmatter.",
        encoding="utf-8",
    )

    loader = SkillLoader(skills_dir=tmp_path)
    manifests = loader.load_manifests()

    assert len(manifests) == 1
    assert manifests[0].name == "legacy-skill"
    assert loader.load_instruction_text(manifests[0]).startswith("# Legacy Skill")


def test_list_skills_endpoint() -> None:
    with TestClient(app) as client:
        response = client.get("/api/v1/agents/skills")

    assert response.status_code == 200
    body = response.json()
    assert body["code"] == 200
    assert any(item["name"] == "ops-observer" for item in body["data"])


def test_agent_chat_endpoint_injects_skill_instructions() -> None:
    def skill_model(messages: list[ModelRequest | ModelResponse], info: AgentInfo) -> ModelResponse:
        request_texts: list[str] = []
        for message in messages:
            if not isinstance(message, ModelRequest):
                continue
            for part in message.parts:
                content = getattr(part, "content", None)
                if isinstance(content, str):
                    request_texts.append(content)

        prompt_text = "\n".join(request_texts)
        instructions = info.instructions or ""
        if (
            "请帮我检查当前运行时健康状态" in prompt_text
            and "Runtime Ops Observer" in instructions
            and "不要主观猜测服务状态" in instructions
        ):
            return ModelResponse(parts=[TextPart(content="skills injected")])
        return ModelResponse(parts=[TextPart(content="skills missing")])

    with TestClient(app) as client:
        agent = client.app.state.ai_agent_manager.get_agent("chat-agent")
        with agent.override(model=FunctionModel(skill_model)):
            response = client.post(
                "/api/v1/agents/chat",
                json={"agent_id": "chat-agent", "message": "请帮我检查当前运行时健康状态", "skill_tags": ["ops"]},
                headers={"x-user-id": "tester"},
            )

    assert response.status_code == 200
    body = response.json()
    assert body["code"] == 200
    assert body["data"]["message"] == "skills injected"
    assert body["data"]["meta"]["skills"] == ["ops-observer"]
