import ast
import unittest

from tooling.check_architecture import (
    dependency_violations,
    imported_modules,
)
from tooling.check_architecture import violations as architecture_violations
from tooling.check_commit_message import validate
from tooling.check_naming import violations as naming_violations


class RepositoryGovernanceTests(unittest.TestCase):
    def test_commit_header_requires_conventional_type_and_scope(self):
        self.assertIsNone(validate("feat(scout): add proposal replay"))
        self.assertIsNotNone(validate("add proposal replay"))
        self.assertIsNotNone(validate("feat: add proposal replay"))

    def test_architecture_checker_resolves_relative_imports_and_rejects_inward_edge(
        self,
    ):
        tree = ast.parse("from ...application import ScoutApplication")
        imports = imported_modules(tree, "scout.adapters.postgres.repository")
        self.assertEqual(imports, {"scout.application"})
        errors = dependency_violations(imports, "scout.adapters", "repository.py")
        self.assertEqual(
            errors, ["repository.py: scout.adapters may not import scout.application"]
        )

    def test_current_source_graph_and_names_are_valid(self):
        self.assertEqual(architecture_violations(), [])
        self.assertEqual(naming_violations(), [])


if __name__ == "__main__":
    unittest.main()
