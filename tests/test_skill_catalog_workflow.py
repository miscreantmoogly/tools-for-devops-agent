import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "validate-skill-catalog.yml"


class SkillCatalogWorkflowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.text = WORKFLOW.read_text(encoding="utf-8")

    def test_has_stable_unfiltered_pull_request_status(self):
        self.assertIn("on:\n  pull_request:\n", self.text)
        self.assertNotIn("paths:", self.text)
        self.assertIn("  validate-skill-catalog:\n", self.text)
        self.assertIn("    name: validate-skill-catalog\n", self.text)

    def test_is_read_only_and_fork_safe(self):
        self.assertIn("permissions:\n  contents: read\n", self.text)
        for forbidden in (
            "pull_request_target",
            "contents: write",
            "id-token: write",
            "secrets.",
            "aws-actions/",
            "configure-aws-credentials",
            "s3://",
        ):
            self.assertNotIn(forbidden, self.text)
        self.assertIn("fetch-depth: 0", self.text)
        self.assertIn("persist-credentials: false", self.text)

    def test_uses_only_the_pinned_documentation_dependency(self):
        install_lines = [line.strip() for line in self.text.splitlines() if "pip install" in line]
        self.assertEqual(
            ["run: python3 -m pip install mkdocs-material==9.6.14"],
            install_lines,
        )
        self.assertIn('python-version: "3.12"', self.text)

    def test_runs_tests_validator_generator_check_and_strict_build(self):
        required = (
            "python3 -m unittest discover -s tests -p 'test_*catalog*.py' -v",
            "python3 .github/scripts/validate_skill_catalog.py",
            '--base-ref "origin/${BASE_REF}"',
            "python3 .github/scripts/generate_skill_catalog.py --output-dir \"$out\"",
            "python3 .github/scripts/generate_skill_catalog.py --output-dir \"$out\" --check",
            "mkdocs build --strict",
        )
        for command in required:
            with self.subTest(command=command):
                self.assertIn(command, self.text)

    def test_does_not_merge_with_eval_validation(self):
        self.assertNotIn("validate_skill_evals.py", self.text)
        self.assertNotIn("needs-evals", self.text)


if __name__ == "__main__":
    unittest.main()
