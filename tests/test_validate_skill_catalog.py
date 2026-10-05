import contextlib
import hashlib
import io
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = REPO_ROOT / ".github" / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import validate_skill_catalog as validator  # noqa: E402
from generate_skill_catalog import build_skill_archive  # noqa: E402


def run_git(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=root, capture_output=True, text=True, check=True
    )
    return result.stdout.strip()


def write_skill(
    root: Path,
    slug: str = "example-skill",
    *,
    version: str = "1.0.0",
    body: str = "# Example\n",
    name: str | None = None,
) -> Path:
    skill_dir = root / "skills" / slug
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(
        f"""---
name: {name or slug}
description: A fixture skill used to validate deterministic catalog policy behavior.
metadata:
  author: example-author
  version: {version}
  aws-devops-agent-skills.agent-types: Chat tasks, Incident RCA
  aws-devops-agent-skills.aws-services: Amazon EC2
  aws-devops-agent-skills.technical-domains: Operations
---

{body}""",
        encoding="utf-8",
    )
    return skill_dir


class GitRepository:
    def __init__(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        run_git(self.root, "init", "-b", "main")
        run_git(self.root, "config", "user.name", "Catalog Test")
        run_git(self.root, "config", "user.email", "catalog@example.com")

    def commit(self, message: str) -> str:
        run_git(self.root, "add", "-A")
        run_git(self.root, "commit", "-m", message)
        return run_git(self.root, "rev-parse", "HEAD")

    def close(self):
        self.temporary_directory.cleanup()


class SkillCatalogValidatorTests(unittest.TestCase):
    def setUp(self):
        self.repo = GitRepository()

    def tearDown(self):
        self.repo.close()

    def initial_skill(self, slug: str = "example-skill") -> str:
        write_skill(self.repo.root, slug)
        return self.repo.commit("add skill")

    def test_irrelevant_pull_request_exits_without_skill_work(self):
        base = self.initial_skill()
        (self.repo.root / "README.md").write_text("docs\n", encoding="utf-8")
        head = self.repo.commit("docs")

        report = validator.validate_pull_request(self.repo.root, base, head)

        self.assertEqual(set(), report.skill_ids)
        self.assertEqual([], report.findings)

    def test_linked_skill_root_is_changed_and_blocked_in_pull_request_mode(self):
        base = self.initial_skill()
        outside = self.repo.root / "outside"
        outside.mkdir()
        (outside / "SKILL.md").write_text(
            "---\nname: linked-skill\ndescription: Linked fixture.\nmetadata:\n"
            "  author: test\n  version: 1.0.0\n---\n",
            encoding="utf-8",
        )
        (self.repo.root / "skills" / "linked-skill").symlink_to("../outside")
        head = self.repo.commit("add linked skill root")

        report = validator.validate_pull_request(self.repo.root, base, head)

        self.assertIn("linked-skill", report.skill_ids)
        self.assertTrue(any("skill root must not be a symlink" in f.message for f in report.blockers))

    def test_runtime_change_without_version_increase_warns(self):
        base = self.initial_skill()
        write_skill(self.repo.root, body="# Changed runtime\n")
        head = self.repo.commit("change runtime")

        report = validator.validate_pull_request(self.repo.root, base, head)

        self.assertFalse(report.blockers)
        self.assertTrue(
            any("runtime bytes changed without increasing version" in f.message for f in report.advisories)
        )

    def test_readme_and_eval_only_edits_do_not_warn_about_runtime(self):
        base = self.initial_skill()
        skill_dir = self.repo.root / "skills" / "example-skill"
        (skill_dir / "README.md").write_text("updated docs\n", encoding="utf-8")
        (skill_dir / "evals").mkdir()
        (skill_dir / "evals" / "evals.json").write_text("{}\n", encoding="utf-8")
        head = self.repo.commit("update repository files")

        report = validator.validate_pull_request(self.repo.root, base, head)

        self.assertFalse(report.findings)

    def test_semver_decrease_blocks(self):
        base = self.initial_skill()
        write_skill(self.repo.root, version="0.9.0")
        head = self.repo.commit("decrease version")

        report = validator.validate_pull_request(self.repo.root, base, head)

        self.assertTrue(any("version decreased" in f.message for f in report.blockers))

    def test_version_only_increase_is_advisory(self):
        base = self.initial_skill()
        write_skill(self.repo.root, version="1.0.1")
        head = self.repo.commit("increase version")

        report = validator.validate_pull_request(self.repo.root, base, head)

        self.assertFalse(report.blockers)
        self.assertTrue(any("with no other installable" in f.message for f in report.advisories))

    def test_deleted_and_renamed_identities_remain_visible(self):
        base = self.initial_skill("old-skill")
        run_git(self.repo.root, "mv", "skills/old-skill", "skills/new-skill")
        write_skill(self.repo.root, "new-skill")
        head = self.repo.commit("rename skill")

        report = validator.validate_pull_request(self.repo.root, base, head)
        messages = [finding.message for finding in report.advisories]

        self.assertIn("old-skill", report.skill_ids)
        self.assertIn("new-skill", report.skill_ids)
        self.assertTrue(any("likely directory rename" in message for message in messages))
        self.assertTrue(any("identity was deleted" in message for message in messages))

    def test_retired_path_reuse_blocks_with_full_history(self):
        write_skill(self.repo.root, "retired-skill")
        self.repo.commit("add retired skill")
        shutil.rmtree(self.repo.root / "skills" / "retired-skill")
        base = self.repo.commit("retire skill")
        write_skill(self.repo.root, "retired-skill")
        head = self.repo.commit("reuse retired path")

        report = validator.validate_pull_request(self.repo.root, base, head)

        self.assertTrue(any("retired skill identity" in f.message for f in report.blockers))

    def test_shallow_history_warns_instead_of_claiming_retired_proof(self):
        base = self.initial_skill()
        write_skill(self.repo.root, "new-skill")
        self.repo.commit("add another skill")
        with tempfile.TemporaryDirectory() as clone_directory:
            clone = Path(clone_directory) / "clone"
            subprocess.run(
                ["git", "clone", "--depth=2", f"file://{self.repo.root}", str(clone)],
                capture_output=True,
                text=True,
                check=True,
            )
            head = run_git(clone, "rev-parse", "HEAD")
            shallow_base = run_git(clone, "rev-parse", "HEAD^")

            report = validator.validate_pull_request(clone, shallow_base, head)

        self.assertTrue(any("repository is shallow" in f.message for f in report.advisories))

    def test_full_audit_collects_model_secret_and_package_findings(self):
        write_skill(self.repo.root, "wrong-path", name="different-name")
        secret_skill = write_skill(self.repo.root, "secret-skill")
        references = secret_skill / "references"
        references.mkdir()
        (references / "credentials.txt").write_text(
            "-----BEGIN PRIVATE KEY-----\nAKIAABCDEFGHIJKLMNOP\n",
            encoding="utf-8",
        )
        unsafe_skill = write_skill(self.repo.root, "unsafe-skill")
        assets = unsafe_skill / "assets"
        assets.mkdir()
        executable = assets / "run.md"
        executable.write_text("run\n", encoding="utf-8")
        executable.chmod(0o755)

        report = validator.validate_local(
            self.repo.root, ("wrong-path", "secret-skill", "unsafe-skill")
        )
        messages = [finding.message for finding in report.blockers]

        self.assertGreaterEqual(len(messages), 4)
        self.assertTrue(any("must match directory" in message for message in messages))
        self.assertTrue(any("PEM private-key" in message for message in messages))
        self.assertTrue(any("AWS access key ID" in message for message in messages))
        self.assertTrue(any("executable files" in message for message in messages))

    def test_unknown_dimension_and_invalid_semver_block(self):
        skill_dir = write_skill(self.repo.root)
        path = skill_dir / "SKILL.md"
        original = path.read_text(encoding="utf-8")
        path.write_text(
            original.replace("version: 1.0.0", 'version: "1.0"'),
            encoding="utf-8",
        )
        invalid_version = validator.validate_local(self.repo.root, ("example-skill",))
        self.assertTrue(
            any("strict Semantic Versioning" in f.message for f in invalid_version.blockers)
        )

        path.write_text(
            original.replace(
                "  aws-devops-agent-skills.technical-domains: Operations",
                "  aws-devops-agent-skills.unknown: Value",
            ),
            encoding="utf-8",
        )
        unknown_dimension = validator.validate_local(self.repo.root, ("example-skill",))
        self.assertTrue(
            any("unknown catalog dimension" in f.message for f in unknown_dimension.blockers)
        )

    def test_fingerprints_match_generator_and_ignore_only_version(self):
        skill_dir = write_skill(self.repo.root)
        report = validator.ValidationReport()
        original = validator.inspect_skill(self.repo.root, "example-skill", report)
        self.assertIsNotNone(original)
        self.assertEqual(
            hashlib.sha256(build_skill_archive(skill_dir)).hexdigest(),
            original.archive_sha256,
        )

        write_skill(self.repo.root, version="2.0.0")
        changed = validator.inspect_skill(
            self.repo.root, "example-skill", validator.ValidationReport()
        )
        self.assertNotEqual(original.archive_sha256, changed.archive_sha256)
        self.assertEqual(original.version_neutral_sha256, changed.version_neutral_sha256)

    def test_annotations_and_summary_escape_untrusted_text(self):
        report = validator.ValidationReport(skill_ids={"example-skill"})
        report.add(
            "blocker",
            "bad % value\nsecond line | cell",
            "example-skill",
            "skills/example-skill/file,name.md",
        )
        with tempfile.TemporaryDirectory() as directory:
            summary = Path(directory) / "summary.md"
            output = io.StringIO()
            with mock.patch.dict(
                os.environ,
                {"GITHUB_ACTIONS": "true", "GITHUB_STEP_SUMMARY": str(summary)},
                clear=False,
            ), contextlib.redirect_stdout(output):
                validator._emit_annotations(report)
                validator._write_summary(report)

            annotation = output.getvalue()
            summary_text = summary.read_text(encoding="utf-8")

        self.assertIn("%25", annotation)
        self.assertIn("%0A", annotation)
        self.assertIn("%2C", annotation)
        self.assertIn("\\|", summary_text)

    def test_main_uses_zero_one_and_two_exit_classes(self):
        write_skill(self.repo.root)
        with mock.patch.object(validator, "REPO_ROOT", self.repo.root):
            self.assertEqual(0, validator.main(["--all"]))
            references = self.repo.root / "skills" / "example-skill" / "references"
            references.mkdir()
            (references / "secret.txt").write_text(
                "AKIAABCDEFGHIJKLMNOP\n", encoding="utf-8"
            )
            self.assertEqual(1, validator.main(["--all"]))
            self.assertEqual(
                2,
                validator.main(
                    ["--base-ref", "missing-ref", "--head-ref", "HEAD"]
                ),
            )


if __name__ == "__main__":
    unittest.main()
