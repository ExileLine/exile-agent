from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from app.ai.exceptions import SkillConfigurationError
from app.ai.skills.models import SkillManifest
from app.core.config import BASE_DIR


class SkillLoader:
    """从文件系统加载 Anthropic-style `SKILL.md` 与项目扩展配置。"""

    def __init__(self, *, skills_dir: str | Path | None) -> None:
        self.skills_dir = self._resolve_skills_dir(skills_dir)

    def load_manifests(self) -> list[SkillManifest]:
        """扫描 skills 目录并加载全部启用中的 manifest。"""

        if self.skills_dir is None or not self.skills_dir.exists():
            return []

        manifests: list[SkillManifest] = []
        skill_dirs = {
            *[path.parent for path in self.skills_dir.rglob("SKILL.md")],
            *[path.parent for path in self.skills_dir.rglob("skill.yaml")],
        }
        for skill_dir in sorted(skill_dirs):
            manifests.append(self._load_manifest(skill_dir))
        return manifests

    def load_instruction_text(self, manifest: SkillManifest) -> str:
        """读取一个 skill 对应的正文说明。

        `SKILL.md` 的 YAML frontmatter 只用于元数据，不会注入给模型。
        其他 reference 文件按 `instruction_files` 顺序原样加载。
        """

        blocks: list[str] = []
        for instruction_path in manifest.instruction_paths():
            if not instruction_path.exists():
                raise SkillConfigurationError(
                    f"Skill `{manifest.name}` 缺少 instruction 文件: {instruction_path}"
                )
            raw_text = instruction_path.read_text(encoding="utf-8")
            if instruction_path.name == "SKILL.md":
                _frontmatter, body = _split_markdown_frontmatter(raw_text)
                blocks.append(body.strip())
            else:
                blocks.append(raw_text.strip())
        return "\n\n".join(block for block in blocks if block).strip()

    @staticmethod
    def _resolve_skills_dir(skills_dir: str | Path | None) -> Path | None:
        if skills_dir is None:
            return None

        raw_path = Path(str(skills_dir).strip())
        if not str(raw_path):
            return None
        if raw_path.is_absolute():
            return raw_path
        return BASE_DIR / raw_path

    def _load_manifest(self, skill_dir: Path) -> SkillManifest:
        skill_md_path = skill_dir / "SKILL.md"
        legacy_manifest_path = skill_dir / "skill.yaml"

        payload: dict[str, Any] = {}
        if legacy_manifest_path.exists():
            payload.update(_load_yaml_mapping(legacy_manifest_path, "Skill manifest"))

        if skill_md_path.exists():
            frontmatter, _body = _split_markdown_frontmatter(skill_md_path.read_text(encoding="utf-8"))
            if frontmatter:
                payload.update(_normalize_frontmatter(frontmatter, skill_md_path))
            elif not legacy_manifest_path.exists():
                raise SkillConfigurationError(f"SKILL.md frontmatter 缺少 `name` 和 `description`: {skill_md_path}")
        elif not legacy_manifest_path.exists():
            raise SkillConfigurationError(f"Skill 目录缺少 SKILL.md: {skill_dir}")

        if "name" not in payload or not payload["name"]:
            payload["name"] = skill_dir.name

        payload.setdefault("title", _title_from_name(str(payload["name"])))
        payload.setdefault("instruction_files", ["SKILL.md"])
        payload["root_dir"] = str(skill_dir.resolve())

        try:
            return SkillManifest.model_validate(payload)
        except Exception as exc:
            source = skill_md_path if skill_md_path.exists() else legacy_manifest_path
            raise SkillConfigurationError(f"Skill manifest 不符合约定: {source}") from exc


def _split_markdown_frontmatter(raw_text: str) -> tuple[dict[str, Any], str]:
    if not raw_text.startswith("---\n"):
        return {}, raw_text

    end_marker = raw_text.find("\n---", 4)
    if end_marker == -1:
        raise SkillConfigurationError("SKILL.md frontmatter 缺少结束标记 ---")

    frontmatter_text = raw_text[4:end_marker].strip()
    body = raw_text[end_marker + len("\n---") :].lstrip("\r\n")
    if not frontmatter_text:
        return {}, body
    try:
        frontmatter = yaml.safe_load(frontmatter_text) or {}
    except yaml.YAMLError as exc:
        raise SkillConfigurationError("SKILL.md frontmatter 解析失败") from exc
    if not isinstance(frontmatter, dict):
        raise SkillConfigurationError("SKILL.md frontmatter 必须是对象")
    return frontmatter, body


def _load_yaml_mapping(path: Path, label: str) -> dict[str, Any]:
    try:
        payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise SkillConfigurationError(f"{label} 解析失败: {path}") from exc
    if not isinstance(payload, dict):
        raise SkillConfigurationError(f"{label} 必须是对象: {path}")
    return payload


def _normalize_frontmatter(frontmatter: dict[str, Any], path: Path) -> dict[str, Any]:
    payload = dict(frontmatter)
    if "allowed-tools" in payload:
        payload["allowed_tools"] = payload.pop("allowed-tools")

    for required_field in ("name", "description"):
        if not payload.get(required_field):
            raise SkillConfigurationError(f"SKILL.md frontmatter 缺少 `{required_field}`: {path}")
    return payload


def _title_from_name(name: str) -> str:
    return " ".join(part.capitalize() for part in name.split("-") if part) or name
