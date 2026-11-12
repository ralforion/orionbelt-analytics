"""MCP resources: skills served over ``skill://`` URIs.

A skill is a folder holding a ``SKILL.md`` -- YAML frontmatter with ``name`` and
``description``, then markdown -- plus any supporting files it refers to. That
is the Agent Skills layout Claude Code and other agents read from their own
skills directories, so one folder works there and served from here alike.

FastMCP's :class:`SkillsDirectoryProvider` exposes each skill as

- ``skill://<name>/SKILL.md``: the skill, described by its frontmatter;
- ``skill://<name>/_manifest``: JSON listing the skill's files, sizes and hashes;
- ``skill://<name>/{path*}``: any supporting file, found through the manifest.

The shipped skills live in ``.claude/skills``; ``SKILLS_DIRS`` adds directories
of the operator's own. Skills are discovered once, at startup.

Kept out of ``main.py`` so server setup stays thin. Call
:func:`register_resources` with the FastMCP instance to wire these up.
"""

import logging
from collections.abc import Callable
from pathlib import Path

from fastmcp import FastMCP
from fastmcp.exceptions import ResourceError
from fastmcp.server.providers.skills import SkillsDirectoryProvider

from .paths import get_extra_skills_dirs, get_skills_dir

logger = logging.getLogger(__name__)

SKILL_FILE = "SKILL.md"

# The shipped guides were served at skill://<name> before they became SKILL.md
# folders. Those URIs keep resolving, so prompts and clients that name them
# still work.
LEGACY_SKILL_NAMES = (
    "fan-trap-prevention",
    "sql-best-practices",
    "chart-examples",
    "analytical-workflow",
)


def _skill_names(root: Path) -> list[str]:
    """Names of the skills directly under ``root``: subfolders with a SKILL.md."""
    try:
        return sorted(p.name for p in root.iterdir() if (p / SKILL_FILE).is_file())
    except OSError as e:
        logger.warning(f"Cannot read skills directory {root}: {e}")
        return []


def skill_roots() -> list[Path]:
    """Directories to serve skills from, the shipped one first.

    The provider keeps the first skill it finds under each name, so putting the
    shipped directory first means an operator's skill cannot replace one of
    ours -- the fan-trap guide in particular is held to the validator's
    behaviour by tests. Each conflict, and each configured directory that
    holds no skills at all, is logged: the provider itself drops them silently.

    Returns:
        The shipped skills directory, then every usable ``SKILLS_DIRS`` entry.
    """
    shipped = get_skills_dir()
    roots = [shipped]
    owner = dict.fromkeys(_skill_names(shipped), shipped)
    if not owner:
        logger.warning(
            f"No shipped skills found in {shipped}; this installation is " "incomplete."
        )

    for root in get_extra_skills_dirs():
        if root == shipped.resolve():
            continue
        names = _skill_names(root)
        if not names:
            hint = (
                " It holds a SKILL.md itself: list its parent folder instead."
                if (root / SKILL_FILE).is_file()
                else ""
            )
            logger.warning(
                f"SKILLS_DIRS entry {root} contains no skills (folders with a "
                f"{SKILL_FILE}); skipped.{hint}"
            )
            continue
        for name in names:
            if name in owner:
                logger.warning(
                    f"Skill '{name}' in {root} is ignored: {owner[name]} already "
                    "has a skill by that name."
                )
            else:
                owner[name] = root
        roots.append(root)

    logger.info(f"Serving {len(owner)} skill(s): {', '.join(sorted(owner))}")
    return roots


def _make_legacy_reader(name: str) -> Callable[[], str]:
    """Build the reader behind ``skill://<name>``, the URI before SKILL.md.

    FastMCP treats any function parameter as a URI-template variable, so the
    registered function must take no arguments; the name is captured here.
    """

    def _read() -> str:
        path = get_skills_dir() / name / SKILL_FILE
        try:
            return path.read_text(encoding="utf-8")
        except FileNotFoundError as e:
            raise ResourceError(
                f"Skill '{name}' is missing from this installation ({path})."
            ) from e

    return _read


def register_resources(mcp: FastMCP) -> None:
    """Serve every skill, plus the shipped guides' older URIs, on ``mcp``."""
    mcp.add_provider(SkillsDirectoryProvider(roots=skill_roots()))
    for name in LEGACY_SKILL_NAMES:
        canonical = f"skill://{name}/{SKILL_FILE}"
        mcp.resource(
            f"skill://{name}",
            name=f"{name} (older URI)",
            description=f"Same content as {canonical}, kept at its older URI.",
            mime_type="text/markdown",
        )(_make_legacy_reader(name))
