"""Skills served over ``skill://``: the shipped ones, the operator's, old URIs.

Driven through a real FastMCP server and an in-memory client, because what
matters is what a client sees in ``resources/list`` and ``resources/read``.
"""

import logging
from pathlib import Path

import pytest
from fastmcp import Client, FastMCP
from fastmcp.exceptions import ResourceError
from fastmcp.server.providers.skills._common import parse_frontmatter

import src.resources as resources
from src.paths import get_skills_dir

SHIPPED = sorted(resources.LEGACY_SKILL_NAMES)


def _write_skill(root: Path, name: str, body: str, **files: str) -> Path:
    skill = root / name
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {name} for tests.\n---\n\n{body}\n",
        encoding="utf-8",
    )
    for rel, content in files.items():
        (skill / rel).write_text(content, encoding="utf-8")
    return skill


def _server() -> FastMCP:
    mcp = FastMCP("skills-test")
    resources.register_resources(mcp)
    return mcp


async def _text(client: Client, uri: str) -> str:
    contents = await client.read_resource(uri)
    return contents[0].text  # type: ignore[union-attr]


@pytest.fixture(autouse=True)
def _no_operator_skills(monkeypatch: pytest.MonkeyPatch) -> None:
    """A developer's own SKILLS_DIRS must not leak into these tests."""
    monkeypatch.delenv("SKILLS_DIRS", raising=False)


class TestShippedSkills:
    def test_every_shipped_skill_is_a_valid_skill_md(self):
        """Agent Skills: frontmatter name matches the folder, description set."""
        found = sorted(p.parent.name for p in get_skills_dir().glob("*/SKILL.md"))
        assert found == SHIPPED
        for name in found:
            text = (get_skills_dir() / name / "SKILL.md").read_text(encoding="utf-8")
            frontmatter, body = parse_frontmatter(text)
            assert frontmatter.get("name") == name
            description = frontmatter.get("description", "")
            assert 50 < len(description) <= 1024, name
            assert body.lstrip().startswith("# "), name

    def test_no_flat_guides_left_behind(self):
        """A stray <name>.md would be served by nobody and edited by someone."""
        assert list(get_skills_dir().glob("*.md")) == []

    async def test_listed_at_skill_md_uris_with_their_descriptions(self):
        async with Client(_server()) as client:
            listed = {str(r.uri): r for r in await client.list_resources()}
        for name in SHIPPED:
            main = listed[f"skill://{name}/SKILL.md"]
            assert main.mime_type == "text/markdown"
            frontmatter, _ = parse_frontmatter(
                (get_skills_dir() / name / "SKILL.md").read_text(encoding="utf-8")
            )
            assert main.description == frontmatter["description"]
            assert f"skill://{name}/_manifest" in listed

    async def test_older_uris_serve_the_same_content(self):
        async with Client(_server()) as client:
            for name in SHIPPED:
                canonical = await _text(client, f"skill://{name}/SKILL.md")
                assert canonical.startswith(f"---\nname: {name}\n")
                assert await _text(client, f"skill://{name}") == canonical

    def test_older_uri_reports_a_missing_file(self, monkeypatch, tmp_path):
        monkeypatch.setattr(resources, "get_skills_dir", lambda: tmp_path)
        reader = resources._make_legacy_reader("fan-trap-prevention")
        with pytest.raises(ResourceError, match="fan-trap-prevention"):
            reader()


class TestOperatorSkills:
    async def test_skills_dirs_entries_are_served(self, monkeypatch, tmp_path):
        _write_skill(
            tmp_path / "a",
            "glossary",
            "Revenue means net revenue.",
            **{"terms.md": "T"},
        )
        _write_skill(tmp_path / "b", "kpi-rules", "Churn is monthly.")
        monkeypatch.setenv("SKILLS_DIRS", f"{tmp_path / 'a'}, {tmp_path / 'b'}")

        async with Client(_server()) as client:
            listed = {str(r.uri) for r in await client.list_resources()}
            assert {"skill://glossary/SKILL.md", "skill://kpi-rules/SKILL.md"} <= listed
            assert "net revenue" in await _text(client, "skill://glossary/SKILL.md")
            # Supporting files stay out of the list and are read by path.
            assert "skill://glossary/terms.md" not in listed
            assert await _text(client, "skill://glossary/terms.md") == "T"
            manifest = await _text(client, "skill://glossary/_manifest")
            assert '"terms.md"' in manifest

    async def test_shipped_skill_wins_a_name_conflict(
        self, monkeypatch, tmp_path, caplog
    ):
        _write_skill(tmp_path, "fan-trap-prevention", "Impostor.")
        monkeypatch.setenv("SKILLS_DIRS", str(tmp_path))

        with caplog.at_level(logging.WARNING, logger="src.resources"):
            mcp = _server()
        assert "'fan-trap-prevention'" in caplog.text
        async with Client(mcp) as client:
            text = await _text(client, "skill://fan-trap-prevention/SKILL.md")
        assert "Impostor." not in text

    def test_unusable_entries_are_skipped_with_a_reason(
        self, monkeypatch, tmp_path, caplog
    ):
        single = _write_skill(tmp_path / "skills", "solo", "One skill.")
        empty = tmp_path / "empty"
        empty.mkdir()
        missing = tmp_path / "missing"
        monkeypatch.setenv("SKILLS_DIRS", f"{single},{empty},{missing},,")

        with caplog.at_level(logging.WARNING):
            roots = resources.skill_roots()

        assert roots == [get_skills_dir()]
        assert "list its parent folder instead" in caplog.text
        assert f"{empty} contains no skills" in caplog.text
        assert "'" + str(missing) + "' is not a directory" in caplog.text

    def test_repeated_and_shipped_entries_are_not_served_twice(
        self, monkeypatch, tmp_path
    ):
        _write_skill(tmp_path, "glossary", "G.")
        monkeypatch.setenv("SKILLS_DIRS", f"{tmp_path},{tmp_path}/.,{get_skills_dir()}")
        assert resources.skill_roots() == [get_skills_dir(), tmp_path.resolve()]
