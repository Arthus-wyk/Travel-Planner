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
            "agent/tools",
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
                    "agent/tools",
                    "infrastructure",
                    "service/storage",
                    "service/travel",
                    "service/mcp",
                } and path.name not in {"generation_jobs.py", "generation_model_log.py"}:
                    self.assertFalse(
                        any(
                            (name or "").split(".")[0] == "agent"
                            and not (name or "").startswith("agent.tools")
                            for name in imports
                        ),
                        str(path),
                    )

    def test_grouped_agent_modules_and_shared_tool_boundaries(self):
        for name in (
            "agent/chat/planner.py", "agent/chat/runtime/agent_loop.py",
            "agent/planning/planner.py", "agent/planning/runtime/agent_loop.py",
            "agent/tools/registry.py", "agent/tools/dispatcher.py", "agent/tools/retry.py",
            "agent/tools/schemas/inputs.py", "agent/tools/schemas/outputs.py",
            "agent/tools/schemas/errors.py",
        ):
            self.assertTrue((ROOT / name).is_file(), name)
        for path in (ROOT / "agent/tools").rglob("*.py"):
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                if isinstance(node, ast.ImportFrom):
                    self.assertFalse(
                        (node.module or "").startswith(("agent.chat", "agent.planning")),
                        str(path),
                    )
