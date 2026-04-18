from __future__ import annotations

from pathlib import Path

import yaml

from app.ai.exceptions import SkillConfigurationError
from app.ai.skills.models import SkillManifest
from app.core.config import BASE_DIR


class SkillLoader:
    """从文件系统加载 skill manifest 与说明文件。"""

    def __init__(self, *, skills_dir: str | Path | None) -> None:
        self.skills_dir = self._resolve_skills_dir(skills_dir)

    def load_manifests(self) -> list[SkillManifest]:
        """扫描 skills 目录并加载全部启用中的 manifest。"""

        if self.skills_dir is None or not self.skills_dir.exists():
            return []

        manifests: list[SkillManifest] = []
        for manifest_path in sorted(self.skills_dir.rglob("skill.yaml")):
            manifests.append(self._load_manifest(manifest_path))
        return manifests

    def load_instruction_text(self, manifest: SkillManifest) -> str:
        """读取一个 skill 对应的正文说明。

        当前正文由 `instruction_files` 决定；
        默认情况下就是 skill 根目录下的 `SKILL.md`。
        """

        blocks: list[str] = []
        for instruction_path in manifest.instruction_paths():
            if not instruction_path.exists():
                raise SkillConfigurationError(
                    f"Skill `{manifest.name}` 缺少 instruction 文件: {instruction_path}"
                )
            blocks.append(instruction_path.read_text(encoding="utf-8").strip())
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

    def _load_manifest(self, manifest_path: Path) -> SkillManifest:
        try:
            payload = yaml.safe_load(manifest_path.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError as exc:
            raise SkillConfigurationError(f"Skill manifest 解析失败: {manifest_path}") from exc

        if not isinstance(payload, dict):
            raise SkillConfigurationError(f"Skill manifest 必须是对象: {manifest_path}")

        if "name" not in payload or not payload["name"]:
            payload["name"] = manifest_path.parent.name

        payload["root_dir"] = str(manifest_path.parent.resolve())

        try:
            return SkillManifest.model_validate(payload)
        except Exception as exc:
            raise SkillConfigurationError(f"Skill manifest 不符合约定: {manifest_path}") from exc
