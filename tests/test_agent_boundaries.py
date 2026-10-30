"""约束依赖方向，防止旧核心和第二套入口重新长回来。"""

import ast
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]


class ArchitectureTests(unittest.TestCase):
    def test_old_core_is_removed_and_lower_layers_do_not_import_agent(self):
        self.assertFalse((ROOT / "service/agent").exists())
        for folder in [
            "agent",
            "tooling",
            "infrastructure",
            "service/storage",
            "service/travel",
            "service/mcp",
        ]:
            for path in (ROOT / folder).rglob("*.py"):
                tree = ast.parse(path.read_text(encoding="utf-8-sig"))
                imports = [
                    node.module
                    for node in ast.walk(tree)
                    if isinstance(node, ast.ImportFrom)
                ]
                imports += [
                    alias.name
                    for node in ast.walk(tree)
                    if isinstance(node, ast.Import)
                    for alias in node.names
                ]
                self.assertFalse(
                    any((name or "").startswith("service.agent") for name in imports),
                    str(path),
                )
                if folder in {
                    "tooling",
                    "infrastructure",
                    "service/storage",
                    "service/travel",
                    "service/mcp",
                }:
                    self.assertFalse(
                        any((name or "").split(".")[0] == "agent" for name in imports),
                        str(path),
                    )
