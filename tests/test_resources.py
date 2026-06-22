"""Coverage tests for skill resource registration."""

import unittest
from unittest.mock import patch

import src.resources as resources


class _FakeMcp:
    """Captures resource registrations like FastMCP.resource()(fn)."""

    def __init__(self):
        self.registered = {}

    def resource(self, uri):
        def deco(fn):
            self.registered[uri] = fn
            return fn

        return deco


class TestResources(unittest.TestCase):
    def test_register_resources_registers_all_skills(self):
        mcp = _FakeMcp()
        resources.register_resources(mcp)
        self.assertEqual(set(mcp.registered), set(resources._SKILLS))
        self.assertEqual(len(mcp.registered), 4)

    def test_loader_reads_existing_skill(
        self,
    ):
        loader = resources._make_skill_loader("fan-trap-prevention.md", "title")
        self.assertEqual(loader.__doc__, "title")
        self.assertTrue(loader.__name__.endswith("_skill"))
        # The real skill file ships with the package.
        self.assertIsInstance(loader(), str)

    def test_loader_missing_file_message(self):
        with patch.object(resources, "get_skills_dir") as gsd:
            gsd.return_value.__truediv__ = lambda self, other: _MissingPath()
            text = resources._read_skill("does-not-exist.md")
        self.assertIn("does-not-exist.md", text)


class _MissingPath:
    def exists(self):
        return False


if __name__ == "__main__":
    unittest.main()
