from __future__ import annotations

import asyncio
import json
import os
import re
import shlex
import sys
import tempfile
from pathlib import Path
from typing import Any

from pydantic_ai import RunContext
from pydantic_ai.toolsets.function import FunctionToolset

from app.ai.deps import AgentDeps
from app.ai.exceptions import SkillConfigurationError
from app.ai.toolsets.conventions import create_function_toolset, validate_toolset_conventions
from app.ai.toolsets.metadata import build_tool_metadata, build_toolset_metadata

SKILL_SCRIPT_TOOLSET_ID = "skill-script-toolset"
MAX_TOOL_OUTPUT_CHARS = 12000
MAX_READ_FILE_CHARS = 20000
MAX_TIMEOUT_SECONDS = 30.0


def get_skill_script_toolset() -> FunctionToolset[AgentDeps]:
    """Build Claude-style controlled execution tools for filesystem skills."""

    toolset: FunctionToolset[AgentDeps] = create_function_toolset(
        id=SKILL_SCRIPT_TOOLSET_ID,
        metadata=build_toolset_metadata(
            toolset_id=SKILL_SCRIPT_TOOLSET_ID,
            kind="skill",
            owner="platform",
            readonly=False,
            risk="medium",
            approval_required=False,
            tags=["skill", "filesystem", "scripts"],
        ),
        instructions=(
            "Use these tools when a matched Skill references files or scripts in its directory. "
            "Only run Python scripts that are part of the matched Skill scripts/ directory. "
            "Pass generated or user files as arguments_json, never as script_path."
        ),
    )

    @toolset.tool(
        metadata=build_tool_metadata(
            category="skill-files",
            readonly=True,
            risk="low",
            tags=["skill", "filesystem", "readonly"],
        ),
    )
    def list_skill_files(
        ctx: RunContext[AgentDeps],
        skill_name: str,
        relative_dir: str = "",
        max_depth: int = 2,
    ) -> dict[str, Any]:
        """List files under a registered Skill directory.

        Args:
            skill_name: Active Skill name whose files should be listed.
            relative_dir: Directory path relative to the Skill root.
            max_depth: Maximum recursive depth to include from relative_dir.
        """

        try:
            skill_root = _resolve_skill_root(ctx, skill_name)
            target_dir = _safe_path(skill_root, relative_dir or ".")
        except SkillConfigurationError as exc:
            return _tool_error(skill_name=skill_name, error=str(exc), files=[])
        if not target_dir.exists() or not target_dir.is_dir():
            return {"skill_name": skill_name, "files": [], "error": f"directory not found: {relative_dir}"}

        depth = max(0, min(int(max_depth), 5))
        files: list[str] = []
        for path in sorted(target_dir.rglob("*")):
            if not path.is_file():
                continue
            relative_path = path.relative_to(skill_root)
            if len(relative_path.parts) > depth + len(Path(relative_dir or ".").parts):
                continue
            files.append(str(relative_path))
        return {"skill_name": skill_name, "files": files}

    @toolset.tool(
        metadata=build_tool_metadata(
            category="skill-files",
            readonly=True,
            risk="low",
            tags=["skill", "filesystem", "readonly"],
        ),
    )
    def get_skill_file_text(
        ctx: RunContext[AgentDeps],
        skill_name: str,
        relative_path: str,
        max_chars: int = MAX_READ_FILE_CHARS,
    ) -> dict[str, Any]:
        """Read a text file from a registered Skill directory.

        Args:
            skill_name: Active Skill name whose file should be read.
            relative_path: File path relative to the Skill root.
            max_chars: Maximum number of characters to return.
        """

        try:
            skill_root = _resolve_skill_root(ctx, skill_name)
            file_path = _safe_path(skill_root, relative_path)
        except SkillConfigurationError as exc:
            return _tool_error(skill_name=skill_name, path=relative_path, content=None, error=str(exc))
        if not file_path.exists() or not file_path.is_file():
            return {"skill_name": skill_name, "path": relative_path, "content": None, "error": "file not found"}

        limit = max(1, min(int(max_chars), MAX_READ_FILE_CHARS))
        content = file_path.read_text(encoding="utf-8", errors="replace")
        truncated = len(content) > limit
        return {
            "skill_name": skill_name,
            "path": str(file_path.relative_to(skill_root)),
            "content": content[:limit],
            "truncated": truncated,
        }

    @toolset.tool(
        metadata=build_tool_metadata(
            category="skill-scripts",
            readonly=False,
            risk="medium",
            tags=["skill", "filesystem", "execution"],
        ),
    )
    async def run_skill_script(
        ctx: RunContext[AgentDeps],
        skill_name: str,
        script_path: str,
        arguments_json: str = "[]",
        timeout_seconds: float = 10.0,
    ) -> dict[str, Any]:
        """Run a Python script from a registered Skill scripts directory.

        Args:
            skill_name: Active Skill name whose script should be executed.
            script_path: Python script path under scripts/, optionally followed by inline arguments.
            arguments_json: JSON string array of command-line arguments.
            timeout_seconds: Maximum execution time in seconds.
        """

        try:
            skill_root = _resolve_skill_root(ctx, skill_name)
            script, inline_arguments = _resolve_script_invocation(skill_root, script_path)
            _validate_script_path(skill_root, script)
            arguments = [*inline_arguments, *_parse_arguments(arguments_json)]
            _validate_arguments(arguments)
        except SkillConfigurationError as exc:
            return _tool_error(
                skill_name=skill_name,
                script_path=script_path,
                return_code=None,
                stdout="",
                stderr=str(exc),
                timed_out=False,
                error=str(exc),
            )

        timeout = max(1.0, min(float(timeout_seconds), MAX_TIMEOUT_SECONDS))
        env = dict(os.environ)
        scripts_dir = skill_root / "scripts"
        workdir = _skill_run_workdir(ctx)
        existing_pythonpath = env.get("PYTHONPATH")
        pythonpath_entries = _dedupe_path_entries([scripts_dir, script.parent])
        if existing_pythonpath:
            pythonpath_entries.append(existing_pythonpath)
        env["PYTHONPATH"] = os.pathsep.join(str(entry) for entry in pythonpath_entries)
        env["SKILL_ROOT"] = str(skill_root)
        env["SKILL_SCRIPTS_DIR"] = str(scripts_dir)
        env["SKILL_RUN_WORKDIR"] = str(workdir)

        proc = await asyncio.create_subprocess_exec(
            sys.executable,
            str(script),
            *arguments,
            cwd=str(workdir),
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout_bytes, stderr_bytes = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except TimeoutError:
            proc.kill()
            await proc.wait()
            return {
                "skill_name": skill_name,
                "script_path": str(script.relative_to(skill_root)),
                "workdir": str(workdir),
                "return_code": None,
                "stdout": "",
                "stderr": f"script timed out after {timeout:.1f}s",
                "timed_out": True,
            }

        stdout = stdout_bytes.decode("utf-8", errors="replace")
        stderr = stderr_bytes.decode("utf-8", errors="replace")
        return {
            "skill_name": skill_name,
            "script_path": str(script.relative_to(skill_root)),
            "workdir": str(workdir),
            "return_code": proc.returncode,
            "stdout": _truncate(stdout),
            "stderr": _truncate(stderr),
            "timed_out": False,
        }

    validate_toolset_conventions(toolset)
    return toolset


def _resolve_skill_root(ctx: RunContext[AgentDeps], skill_name: str) -> Path:
    if ctx.deps.skill_registry is None:
        raise SkillConfigurationError("Skill registry is not available for script execution")

    skill = ctx.deps.skill_registry.require(skill_name)
    if skill.name not in ctx.deps.resolved_skill_names:
        raise SkillConfigurationError(f"Skill `{skill_name}` is not active for the current run")
    return Path(skill.root_dir).resolve()


def _skill_run_workdir(ctx: RunContext[AgentDeps]) -> Path:
    safe_request_id = re.sub(r"[^a-zA-Z0-9_.-]+", "-", ctx.deps.request.request_id).strip("-") or "run"
    workdir = Path(tempfile.gettempdir()) / "exile-agent-skill-runs" / safe_request_id
    workdir.mkdir(parents=True, exist_ok=True)
    return workdir.resolve()


def _safe_path(root: Path, relative_path: str) -> Path:
    if not relative_path or relative_path.strip() in {".", "./"}:
        return root
    raw_path = Path(relative_path)
    if raw_path.is_absolute():
        raise SkillConfigurationError(f"absolute paths are not allowed: {relative_path}")
    resolved = (root / raw_path).resolve()
    if root not in (resolved, *resolved.parents):
        raise SkillConfigurationError(f"path escapes skill directory: {relative_path}")
    return resolved


def _resolve_script_invocation(skill_root: Path, script_path: str) -> tuple[Path, list[str]]:
    normalized, inline_arguments = _normalize_script_invocation(script_path)
    return _resolve_script_path(skill_root, normalized, original_path=script_path), inline_arguments


def _resolve_script_path(skill_root: Path, normalized: str, *, original_path: str) -> Path:
    raw_path = Path(normalized)
    if raw_path.is_absolute():
        raise SkillConfigurationError(f"absolute script paths are not allowed: {original_path}")

    scripts_dir = (skill_root / "scripts").resolve()
    root_relative = _safe_path(skill_root, normalized)
    if root_relative.exists() or scripts_dir in root_relative.parents:
        return root_relative

    scripts_relative = _safe_path(scripts_dir, normalized)
    if scripts_relative.exists():
        return scripts_relative

    if len(raw_path.parts) == 1:
        matches = sorted(scripts_dir.rglob(normalized))
        if len(matches) == 1:
            return matches[0].resolve()
        if len(matches) > 1:
            raise SkillConfigurationError(f"ambiguous skill script name: {original_path}")

    return scripts_relative


def _normalize_script_invocation(script_path: str) -> tuple[str, list[str]]:
    value = script_path.strip()
    if not value:
        raise SkillConfigurationError("script_path is required")

    try:
        tokens = shlex.split(value)
    except ValueError as exc:
        raise SkillConfigurationError("script_path is not a valid shell-like path") from exc

    python_binary_names = {"python", "python3", Path(sys.executable).name}
    if len(tokens) >= 2 and Path(tokens[0]).name in python_binary_names:
        tokens = tokens[1:]
    elif len(tokens) == 0:
        raise SkillConfigurationError("script_path is required")

    return tokens[0].removeprefix("./"), tokens[1:]


def _validate_script_path(skill_root: Path, script: Path) -> None:
    scripts_dir = (skill_root / "scripts").resolve()
    if scripts_dir not in script.parents:
        raise SkillConfigurationError("only scripts under the Skill scripts/ directory can be executed")
    if script.suffix != ".py":
        raise SkillConfigurationError("only Python skill scripts are supported")
    if not script.exists() or not script.is_file():
        raise SkillConfigurationError(f"skill script not found: {script.relative_to(skill_root)}")


def _parse_arguments(arguments_json: str) -> list[str]:
    try:
        payload = json.loads(arguments_json or "[]")
    except json.JSONDecodeError as exc:
        raise SkillConfigurationError("arguments_json must be a JSON string array") from exc
    if not isinstance(payload, list) or not all(isinstance(item, str) for item in payload):
        raise SkillConfigurationError("arguments_json must be a JSON string array")
    return payload


def _validate_arguments(arguments: list[str]) -> None:
    for argument in arguments:
        if "\x00" in argument:
            raise SkillConfigurationError("NUL bytes are not allowed in script arguments")
        path_like = "/" in argument or "\\" in argument or argument.startswith(".")
        if path_like:
            path = Path(argument)
            if path.is_absolute() or ".." in path.parts:
                raise SkillConfigurationError(f"unsafe path argument: {argument}")


def _tool_error(**payload: Any) -> dict[str, Any]:
    payload.setdefault("ok", False)
    return payload


def _dedupe_path_entries(values: list[Path]) -> list[Path]:
    deduped: list[Path] = []
    seen: set[str] = set()
    for value in values:
        resolved = str(value.resolve())
        if resolved in seen:
            continue
        seen.add(resolved)
        deduped.append(value)
    return deduped


def _truncate(value: str) -> str:
    if len(value) <= MAX_TOOL_OUTPUT_CHARS:
        return value
    return value[:MAX_TOOL_OUTPUT_CHARS] + "\n...[truncated]"
