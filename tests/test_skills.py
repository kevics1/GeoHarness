"""Tests for Phase 4: Skills integration.

Verifies that:
- 3 cognitive skills exist in the project skills directory
- SKILL.md files have valid YAML frontmatter
- Skill names match expected values (geo-perception, etc.)
- RuntimeBundle includes skills directory in extra_skill_dirs
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

# Project skills directory
SKILLS_DIR = Path(__file__).parent.parent / "skills"

# Expected skills
EXPECTED_SKILLS = {
    "geo-perception",
    "geo-comprehension",
    "geo-reasoning",
}


class TestSkillsExist:
    """Verify all 3 cognitive skills exist."""

    def test_skills_directory_exists(self):
        """The skills/ directory should exist at project root."""
        assert SKILLS_DIR.exists(), f"Skills directory not found: {SKILLS_DIR}"
        assert SKILLS_DIR.is_dir()

    def test_all_skill_directories_exist(self):
        """All 3 skill directories should exist."""
        for skill_name in EXPECTED_SKILLS:
            skill_dir = SKILLS_DIR / skill_name
            assert skill_dir.exists(), f"Skill directory missing: {skill_dir}"

    def test_all_skill_md_files_exist(self):
        """Each skill should have a SKILL.md file."""
        for skill_name in EXPECTED_SKILLS:
            skill_md = SKILLS_DIR / skill_name / "SKILL.md"
            assert skill_md.exists(), f"SKILL.md missing for {skill_name}"


class TestSkillFrontmatter:
    """Verify SKILL.md files have valid YAML frontmatter."""

    def _parse_frontmatter(self, skill_path: Path) -> dict:
        """Parse YAML frontmatter from a SKILL.md file."""
        content = skill_path.read_text(encoding="utf-8")
        assert content.startswith("---"), (
            f"SKILL.md should start with YAML frontmatter: {skill_path}"
        )
        parts = content.split("---", 2)
        assert len(parts) >= 3, (
            f"SKILL.md has invalid frontmatter format: {skill_path}"
        )
        return yaml.safe_load(parts[1])

    def test_geo_perception_frontmatter(self):
        """geo-perception SKILL.md should have valid frontmatter."""
        fm = self._parse_frontmatter(SKILLS_DIR / "geo-perception" / "SKILL.md")
        assert fm is not None
        assert "name" in fm or "description" in fm

    def test_geo_comprehension_frontmatter(self):
        """geo-comprehension SKILL.md should have valid frontmatter."""
        fm = self._parse_frontmatter(
            SKILLS_DIR / "geo-comprehension" / "SKILL.md"
        )
        assert fm is not None
        assert "name" in fm or "description" in fm

    def test_geo_reasoning_frontmatter(self):
        """geo-reasoning SKILL.md should have valid frontmatter."""
        fm = self._parse_frontmatter(SKILLS_DIR / "geo-reasoning" / "SKILL.md")
        assert fm is not None
        assert "name" in fm or "description" in fm

    def test_skill_names_in_frontmatter(self):
        """Skill frontmatter should contain the skill name."""
        for skill_name in EXPECTED_SKILLS:
            skill_md = SKILLS_DIR / skill_name / "SKILL.md"
            fm = self._parse_frontmatter(skill_md)
            fm_name = fm.get("name", "")
            assert skill_name in fm_name or fm_name == skill_name, (
                f"Skill name mismatch: expected '{skill_name}', "
                f"got '{fm_name}'"
            )


class TestSkillsContent:
    """Verify skills have substantive content."""

    def test_skill_content_not_empty(self):
        """Each SKILL.md should have non-trivial content."""
        for skill_name in EXPECTED_SKILLS:
            skill_md = SKILLS_DIR / skill_name / "SKILL.md"
            content = skill_md.read_text(encoding="utf-8")
            parts = content.split("---", 2)
            body = parts[2] if len(parts) >= 3 else content
            assert len(body.strip()) > 100, (
                f"SKILL.md body too short for {skill_name}: "
                f"{len(body.strip())} chars"
            )

    def test_skills_have_references(self):
        """Skills should have reference files."""
        for skill_name in EXPECTED_SKILLS:
            refs_dir = SKILLS_DIR / skill_name / "references"
            assert refs_dir.exists(), (
                f"references/ directory missing for {skill_name}"
            )
            ref_files = list(refs_dir.glob("*.md"))
            assert len(ref_files) > 0, (
                f"No reference files found for {skill_name}"
            )


class TestRuntimeIncludesSkills:
    """Verify runtime includes skills directory in extra_skill_dirs."""

    @pytest.mark.asyncio
    async def test_extra_skill_dirs_includes_project(
        self, tmp_path: Path
    ):
        """RuntimeBundle should include project skills dir in extra_skill_dirs."""
        from geoharness.config.settings import GeoConfig, McpServerConfig
        from geoharness.runtime import build_geo_runtime

        mock_config = GeoConfig(
            model="test",
            api_key="test-key",
            base_url="https://test.example.com/v1",
            mcp_servers={
                "test": McpServerConfig(
                    command="python", args=["-m", "test"]
                ),
            },
        )

        with patch(
            "geoharness.runtime.load_geo_config", return_value=mock_config
        ):
            bundle = await build_geo_runtime(cwd=tmp_path)

            # extra_skill_dirs should not be empty
            assert len(bundle.extra_skill_dirs) > 0

            # Should contain a path ending with "skills"
            skills_paths = [
                d for d in bundle.extra_skill_dirs if d.endswith("skills")
            ]
            assert len(skills_paths) > 0, (
                f"Skills dir not in extra_skill_dirs: "
                f"{bundle.extra_skill_dirs}"
            )

    @pytest.mark.asyncio
    async def test_data_catalog_in_tool_metadata(
        self, tmp_path: Path
    ):
        """tool_metadata should contain data_catalog."""
        from geoharness.config.settings import GeoConfig
        from geoharness.runtime import build_geo_runtime

        mock_config = GeoConfig(
            model="test",
            api_key="test-key",
            base_url="https://test.example.com/v1",
        )

        with patch(
            "geoharness.runtime.load_geo_config", return_value=mock_config
        ):
            bundle = await build_geo_runtime(cwd=tmp_path)
            assert "data_catalog" in bundle.engine.tool_metadata
            assert "geoharness_config" in bundle.engine.tool_metadata
