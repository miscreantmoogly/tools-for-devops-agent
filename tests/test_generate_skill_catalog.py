import hashlib
import json
import os
import stat
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = REPO_ROOT / ".github" / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from generate_skill_catalog import (  # noqa: E402
    ALLOWED_EXTENSIONS,
    FIXED_ZIP_TIMESTAMP,
    OutputDifferences,
    PackageEntry,
    PackageError,
    build_archive,
    build_skill_archive,
    check_managed_output,
    compare_managed_output,
    generate_managed_files,
    package_manifest,
    validate_archive_path,
    validate_package_entries,
    write_managed_output,
)


def write_fixture_skill(
    root: Path,
    slug: str = "example-skill",
    *,
    asset_files: dict[str, bytes] | None = None,
) -> Path:
    skill_dir = root / "skills" / slug
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        f"""---
name: {slug}
description: A reusable fixture description for deterministic catalog generation.
metadata:
  author: example-author
  version: 1.2.3
  summary: A concise fixture summary.
  aws-devops-agent-skills.technical-domains: Operations
  aws-devops-agent-skills.agent-types: Chat tasks, Incident RCA
---

# Fixture skill
""",
        encoding="utf-8",
    )
    for relative_name, content in (asset_files or {}).items():
        path = skill_dir / relative_name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    return skill_dir


class CatalogGenerationTests(unittest.TestCase):
    def test_real_catalog_is_sorted_normalized_and_has_exact_archive_paths(self):
        managed = generate_managed_files(REPO_ROOT)
        catalog_bytes = managed["skills.json"]
        records = json.loads(catalog_bytes)

        self.assertEqual(30, len(records))
        self.assertEqual(sorted(record["id"] for record in records), [r["id"] for r in records])
        self.assertEqual(
            (json.dumps(records, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode(),
            catalog_bytes,
        )
        self.assertEqual(31, len(managed))

        for record in records:
            self.assertEqual(record["id"], record["name"])
            self.assertEqual(sorted(record["dimensions"]), list(record["dimensions"]))
            archive_path = f"skills/{record['id']}/{record['sha256']}.zip"
            archive_bytes = managed[archive_path]
            self.assertEqual(record["sha256"], hashlib.sha256(archive_bytes).hexdigest())
        self.assertFalse(any("url" in key.lower() for record in records for key in record))

    def test_equivalent_sources_are_byte_identical_across_roots_and_mtimes(self):
        with tempfile.TemporaryDirectory() as first_directory, tempfile.TemporaryDirectory() as second_directory:
            files = {
                "references/nested/guide.md": b"guide\n",
                "assets/config.json": b'{"enabled": true}\n',
            }
            first = write_fixture_skill(Path(first_directory), asset_files=files)
            second = write_fixture_skill(
                Path(second_directory),
                asset_files=dict(reversed(tuple(files.items()))),
            )
            for index, path in enumerate(second.rglob("*"), start=1):
                if path.is_file():
                    os.utime(path, (1_600_000_000 + index, 1_600_000_000 + index))

            first_managed = generate_managed_files(Path(first_directory))
            second_managed = generate_managed_files(Path(second_directory))

            self.assertEqual(first_managed, second_managed)
            self.assertEqual(build_skill_archive(first), build_skill_archive(second))

    def test_manifest_includes_nested_positive_roots_and_excludes_repository_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            skill_dir = write_fixture_skill(
                root,
                asset_files={
                    "references/nested/guide.md": b"guide",
                    "assets/data/config.yaml": b"enabled: true\n",
                },
            )
            excluded = {
                "README.md": b"readme",
                "CHANGELOG.md": b"changes",
                "evals/evals.json": b"{}",
                ".skilleval.yaml": b"tests: []",
                ".claude/CLAUDE.md": b"tooling",
                "scripts/run.md": b"tooling",
                "generated/output.md": b"generated",
                "debug/trace.md": b"debug",
                "tooling/notes.md": b"tooling",
                "sibling.md": b"arbitrary",
            }
            for relative_name, content in excluded.items():
                path = skill_dir / relative_name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(content)

            manifest = package_manifest(skill_dir)
            names = [entry.archive_name for entry in manifest]

            self.assertEqual(
                ["SKILL.md", "assets/data/config.yaml", "references/nested/guide.md"],
                names,
            )
            archive_path = Path(directory) / "archive.zip"
            archive_path.write_bytes(build_archive(manifest))
            with zipfile.ZipFile(archive_path) as archive:
                self.assertEqual(names, archive.namelist())

    def test_archive_uses_fixed_stored_regular_file_metadata(self):
        with tempfile.TemporaryDirectory() as directory:
            skill_dir = write_fixture_skill(
                Path(directory),
                asset_files={"assets/image.png": b"not-a-real-image"},
            )
            archive_path = Path(directory) / "archive.zip"
            archive_path.write_bytes(build_skill_archive(skill_dir))

            with zipfile.ZipFile(archive_path) as archive:
                for info in archive.infolist():
                    self.assertFalse(info.is_dir())
                    self.assertEqual(FIXED_ZIP_TIMESTAMP, info.date_time)
                    self.assertEqual(zipfile.ZIP_STORED, info.compress_type)
                    self.assertEqual(3, info.create_system)
                    self.assertEqual(0o644, (info.external_attr >> 16) & 0o777)
                    self.assertTrue(stat.S_ISREG(info.external_attr >> 16))

    def test_every_supported_extension_is_accepted_under_positive_roots(self):
        with tempfile.TemporaryDirectory() as directory:
            files = {
                f"assets/file{extension}": b"content"
                for extension in ALLOWED_EXTENSIONS
            }
            skill_dir = write_fixture_skill(Path(directory), asset_files=files)

            names = {entry.archive_name for entry in package_manifest(skill_dir)}

            self.assertEqual(
                {"SKILL.md", *(f"assets/file{extension}" for extension in ALLOWED_EXTENSIONS)},
                names,
            )

    def test_rejects_unsupported_nonregular_executable_and_symlink_sources(self):
        cases = ("unsupported", "nonregular", "executable", "symlink")
        for case in cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as directory:
                skill_dir = write_fixture_skill(Path(directory))
                references = skill_dir / "references"
                references.mkdir()
                if case == "unsupported":
                    (references / "payload.exe").write_bytes(b"payload")
                    message = "unsupported package extension"
                elif case == "nonregular":
                    os.mkfifo(references / "pipe.md")
                    message = "regular file"
                elif case == "executable":
                    path = references / "run.md"
                    path.write_bytes(b"run")
                    path.chmod(0o755)
                    message = "executable files"
                else:
                    target = references / "target.md"
                    target.write_bytes(b"target")
                    (references / "link.md").symlink_to(target)
                    message = "symlinks"

                with self.assertRaisesRegex(PackageError, message):
                    package_manifest(skill_dir)

    def test_rejects_unsafe_archive_paths(self):
        unsafe_names = (
            "/absolute.md",
            "C:/absolute.md",
            "../escape.md",
            "assets/../escape.md",
            "assets//empty.md",
            "assets",
            "assets\\windows.md",
            "assets/control\n.md",
            "arbitrary/file.md",
        )
        for name in unsafe_names:
            with self.subTest(name=name), self.assertRaises(PackageError):
                validate_archive_path(name)

    def test_rejects_exact_and_casefold_name_collisions(self):
        with tempfile.TemporaryDirectory() as directory:
            skill_dir = write_fixture_skill(Path(directory))
            source = skill_dir / "SKILL.md"
            for names, message in (
                (("assets/file.md", "assets/file.md"), "duplicate"),
                (("assets/File.md", "assets/file.md"), "case-insensitive"),
            ):
                with self.subTest(names=names), self.assertRaisesRegex(PackageError, message):
                    validate_package_entries(
                        (
                            PackageEntry("SKILL.md", source),
                            PackageEntry(names[0], source),
                            PackageEntry(names[1], source),
                        )
                    )

    def test_check_mode_reports_stale_missing_and_extra_managed_files(self):
        with tempfile.TemporaryDirectory() as source_directory, tempfile.TemporaryDirectory() as output_directory:
            repo_root = Path(source_directory)
            output_dir = Path(output_directory)
            write_fixture_skill(repo_root)
            expected = write_managed_output(repo_root, output_dir)

            self.assertEqual(OutputDifferences(), check_managed_output(repo_root, output_dir))

            (output_dir / "skills.json").write_text("stale\n", encoding="utf-8")
            expected_archive = next(name for name in expected if name.endswith(".zip"))
            (output_dir / expected_archive).unlink()
            extra_path = output_dir / "skills" / "example-skill" / "old.zip"
            extra_path.write_bytes(b"old")

            self.assertEqual(
                OutputDifferences(
                    missing=(expected_archive,),
                    changed=("skills.json",),
                    extra=("skills/example-skill/old.zip",),
                ),
                compare_managed_output(expected, output_dir),
            )

    def test_generation_replaces_only_managed_outputs(self):
        with tempfile.TemporaryDirectory() as source_directory, tempfile.TemporaryDirectory() as output_directory:
            repo_root = Path(source_directory)
            output_dir = Path(output_directory)
            write_fixture_skill(repo_root)
            unrelated = output_dir / "keep.txt"
            unrelated.write_text("keep", encoding="utf-8")
            stale = output_dir / "skills" / "old" / "old.zip"
            stale.parent.mkdir(parents=True)
            stale.write_bytes(b"old")

            write_managed_output(repo_root, output_dir)

            self.assertEqual("keep", unrelated.read_text(encoding="utf-8"))
            self.assertFalse(stale.exists())
            self.assertFalse(check_managed_output(repo_root, output_dir))


if __name__ == "__main__":
    unittest.main()
