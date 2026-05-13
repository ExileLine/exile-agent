from pathlib import Path

from fastapi.testclient import TestClient
from pydantic_ai import models
from pydantic_ai import RunContext
from pydantic_ai.messages import ModelRequest, ModelResponse, TextPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from app.ai.config import AISettings
from app.ai.deps import AgentDeps, RequestContext
from app.ai.skills import SkillLoader, SkillRegistry, SkillResolver
from app.ai.toolsets.builtin import BUILTIN_RUNTIME_TOOLSET_ID
from app.ai.toolsets.catalog import build_registered_toolsets
from app.ai.toolsets.skill_scripts import SKILL_SCRIPT_TOOLSET_ID, get_skill_script_toolset
from app.ai.services.tool_audit import ToolAuditService
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
    assert manifests[0].load_strategy == "full_on_match"
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


def test_docx_skill_matches_description_triggers_and_loads_body() -> None:
    loader = SkillLoader(skills_dir="app/ai/skills/catalog")
    registry = SkillRegistry(loader.load_manifests())
    resolver = SkillResolver(registry=registry, loader=loader)

    resolution = resolver.resolve(
        agent_id="chat-agent",
        message="Please create a Word document report with headings.",
    )

    assert resolution.skill_names == ["docx"]
    assert resolution.skills[0].include_full_instructions is True
    assert SKILL_SCRIPT_TOOLSET_ID in resolution.required_toolset_ids
    assert any("DOCX Skill" in item for item in resolution.instructions)


def test_docx_skill_matches_normal_business_document_requests() -> None:
    loader = SkillLoader(skills_dir="app/ai/skills/catalog")
    registry = SkillRegistry(loader.load_manifests())
    resolver = SkillResolver(registry=registry, loader=loader)

    resolution = resolver.resolve(
        agent_id="chat-agent",
        message="帮我生成一份季度报告，包含业务摘要、里程碑和下季度计划。",
    )

    assert resolution.skill_names == ["docx"]
    assert any("Runtime Rules" in item for item in resolution.instructions)


class _NoopHTTPClient:
    async def aclose(self) -> None:
        return None


def _build_run_context(registry: SkillRegistry, skill_name: str) -> RunContext[AgentDeps]:
    return RunContext(
        deps=AgentDeps(
            request=RequestContext(request_id="skill-script-test"),
            settings=AISettings(),
            db_session_factory=None,
            redis=None,
            http_client=_NoopHTTPClient(),  # type: ignore[arg-type]
            tool_audit=ToolAuditService(),
            mcp_manager=None,
            skill_registry=registry,
            resolved_skill_names=(skill_name,),
        ),
        model=None,  # type: ignore[arg-type]
        usage=None,  # type: ignore[arg-type]
        prompt=None,
    )


def test_skill_script_toolset_lists_reads_and_runs_skill_scripts(tmp_path: Path) -> None:
    skill_dir = tmp_path / "script-skill"
    scripts_dir = skill_dir / "scripts"
    scripts_dir.mkdir(parents=True)
    skill_dir.joinpath("SKILL.md").write_text(
        "\n".join(
            [
                "---",
                "name: script-skill",
                "description: Runs safe helper scripts when users ask for script skill checks.",
                "---",
                "",
                "# Script Skill",
            ]
        ),
        encoding="utf-8",
    )
    scripts_dir.joinpath("echo_args.py").write_text(
        "import json, sys\nprint(json.dumps(sys.argv[1:], ensure_ascii=False))\n",
        encoding="utf-8",
    )
    scripts_dir.joinpath("nested").mkdir()
    scripts_dir.joinpath("nested", "helper.py").write_text(
        "VALUE = 'office-helper-ok'\n",
        encoding="utf-8",
    )
    scripts_dir.joinpath("nested", "validate_docx.py").write_text(
        "import json, sys\nfrom helper import VALUE\n"
        "print(json.dumps({'argv': sys.argv[1:], 'value': VALUE}, ensure_ascii=False))\n",
        encoding="utf-8",
    )

    loader = SkillLoader(skills_dir=tmp_path)
    manifests = loader.load_manifests()
    assert manifests[0].required_toolsets == [SKILL_SCRIPT_TOOLSET_ID]

    registry = SkillRegistry(manifests)
    toolset = get_skill_script_toolset()
    ctx = _build_run_context(registry, "script-skill")

    files_result = toolset.tools["list_skill_files"].function(ctx, "script-skill", "scripts", 1)
    assert "scripts/echo_args.py" in files_result["files"]

    text_result = toolset.tools["get_skill_file_text"].function(ctx, "script-skill", "SKILL.md", 2000)
    assert "# Script Skill" in text_result["content"]

    run_result = toolset.tools["run_skill_script"].function(
        ctx,
        "script-skill",
        "scripts/echo_args.py",
        '["hello", "world"]',
        5.0,
    )
    import asyncio

    output = asyncio.run(run_result)
    assert output["return_code"] == 0
    assert '["hello", "world"]' in output["stdout"]

    nested_result = toolset.tools["run_skill_script"].function(
        ctx,
        "script-skill",
        "nested/validate_docx.py",
        '["doc.docx"]',
        5.0,
    )
    nested_output = asyncio.run(nested_result)
    assert nested_output["return_code"] == 0
    assert '"argv": ["doc.docx"]' in nested_output["stdout"]
    assert '"value": "office-helper-ok"' in nested_output["stdout"]

    command_like_result = toolset.tools["run_skill_script"].function(
        ctx,
        "script-skill",
        "python scripts/echo_args.py",
        '["hello"]',
        5.0,
    )
    command_like_output = asyncio.run(command_like_result)
    assert command_like_output["return_code"] == 0
    assert '["hello"]' in command_like_output["stdout"]

    inline_arguments_result = toolset.tools["run_skill_script"].function(
        ctx,
        "script-skill",
        "scripts/echo_args.py inline.docx",
        '["from-json"]',
        5.0,
    )
    inline_arguments_output = asyncio.run(inline_arguments_result)
    assert inline_arguments_output["return_code"] == 0
    assert '["inline.docx", "from-json"]' in inline_arguments_output["stdout"]

    escaped_list_result = toolset.tools["list_skill_files"].function(ctx, "script-skill", "../", 1)
    assert escaped_list_result["ok"] is False
    assert "path escapes skill directory" in escaped_list_result["error"]

    escaped_read_result = toolset.tools["get_skill_file_text"].function(
        ctx,
        "script-skill",
        "../create_report.js",
        2000,
    )
    assert escaped_read_result["ok"] is False
    assert "path escapes skill directory" in escaped_read_result["error"]

    invalid_script_result = toolset.tools["run_skill_script"].function(
        ctx,
        "script-skill",
        "../create_report.js",
        "[]",
        5.0,
    )
    invalid_script_output = asyncio.run(invalid_script_result)
    assert invalid_script_output["ok"] is False
    assert invalid_script_output["return_code"] is None
    assert "path escapes skill directory" in invalid_script_output["stderr"]


def test_registered_toolsets_can_build_skill_script_toolset() -> None:
    toolsets = build_registered_toolsets([SKILL_SCRIPT_TOOLSET_ID])
    assert [toolset.id for toolset in toolsets] == [SKILL_SCRIPT_TOOLSET_ID]


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
