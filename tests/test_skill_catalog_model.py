import tempfile
import unittest
from pathlib import Path

from docs.hooks.skill_catalog_model import (
    SkillCatalogError,
    compare_semver,
    discover_skill_paths,
    format_display_name,
    load_skill,
    load_skills,
    parse_frontmatter,
    parse_semver,
)


REPO_ROOT = Path(__file__).resolve().parents[1]


def write_skill(root: Path, slug: str, frontmatter: str) -> Path:
    skill_dir = root / "skills" / slug
    skill_dir.mkdir(parents=True)
    skill_path = skill_dir / "SKILL.md"
    skill_path.write_text(f"---\n{frontmatter.strip()}\n---\n\n# Body\n", encoding="utf-8")
    return skill_path


class SkillCatalogCorpusTests(unittest.TestCase):
    def test_all_current_skills_are_path_identified_and_strict_semver(self):
        records = load_skills(REPO_ROOT / "skills")

        self.assertEqual(30, len(records))
        self.assertEqual(sorted(record.id for record in records), [record.id for record in records])
        for record in records:
            self.assertEqual(record.id, record.name)
            self.assertEqual(record.id, record.source_path.parent.name)
            self.assertEqual(record.version, str(parse_semver(record.version)))

        versions = {record.id: record.version for record in records}
        self.assertEqual("2.6.0", versions["analytics-opensearch-expertise"])
        self.assertEqual("1.0.0", versions["database-rds-devops"])


class SkillCatalogModelTests(unittest.TestCase):
    def test_parses_multiline_quoted_fields_and_normalizes_dimensions(self):
        with tempfile.TemporaryDirectory() as directory:
            path = write_skill(
                Path(directory),
                "aws-example",
                """
name: aws-example
title: 'AWS Example'
description: >
  First line with a colon: yes.
  Second line.

  Final paragraph.
compatibility: "Any supported agent"
metadata:
  author: "one, two"
  version: "1.2.3"
  summary: 'Short summary'
  aws-devops-agent-skills.agent-types: "Chat tasks, Incident RCA, Chat tasks"
""",
            )

            fields = parse_frontmatter(path)
            record = load_skill(path)

            self.assertEqual("Any supported agent", fields["compatibility"])
            self.assertEqual(
                "First line with a colon: yes. Second line.\nFinal paragraph.",
                record.description,
            )
            self.assertEqual("AWS Example", record.title)
            self.assertEqual("Short summary", record.summary)
            self.assertEqual("one, two", record.author)
            self.assertEqual(
                ("Chat tasks", "Incident RCA"),
                record.dimensions["agent-types"],
            )
            with self.assertRaises(TypeError):
                record.dimensions["other"] = ("value",)

    def test_parses_folded_and_literal_metadata_summaries(self):
        cases = {
            ">": "First summary line. Second summary line.\nFinal paragraph.",
            "|": "First summary line.\nSecond summary line.\n\nFinal paragraph.",
        }
        for marker, expected in cases.items():
            with self.subTest(marker=marker), tempfile.TemporaryDirectory() as directory:
                path = write_skill(
                    Path(directory),
                    "summary-skill",
                    f"""
name: summary-skill
description: Model-facing description.
metadata:
  author: contributor
  version: 1.0.0
  summary: {marker}
    First summary line.
    Second summary line.

    Final paragraph.
""",
                )

                self.assertEqual(expected, load_skill(path).summary)

    def test_optional_title_and_summary_use_exact_fallbacks(self):
        with tempfile.TemporaryDirectory() as directory:
            description = "A" * 201
            path = write_skill(
                Path(directory),
                "aws-rds-rca",
                f"""
name: aws-rds-rca
description: {description}
metadata:
  author: contributor
  version: 1.0.0
  summary: ""
""",
            )

            record = load_skill(path)

            self.assertEqual("AWS RDS RCA", record.title)
            self.assertEqual(description, record.summary)

    def test_requires_a_real_skills_catalog_root(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            skills_dir = root / "skills"
            with self.assertRaisesRegex(SkillCatalogError, "must exist as a directory"):
                discover_skill_paths(skills_dir)

            write_skill(
                root,
                "outside-skill",
                "name: outside-skill\ndescription: Outside.\nmetadata:\n  author: test\n  version: 1.0.0",
            )
            catalog_source = root / "catalog-source"
            skills_dir.rename(catalog_source)
            skills_dir.symlink_to(catalog_source, target_is_directory=True)
            with self.assertRaisesRegex(SkillCatalogError, "must not be a symlink"):
                discover_skill_paths(skills_dir)

            skills_dir.unlink()
            skills_dir.write_text("not a directory\n", encoding="utf-8")
            with self.assertRaisesRegex(SkillCatalogError, "must be a directory"):
                discover_skill_paths(skills_dir)

    def test_discovers_only_real_skill_directories_in_sorted_order(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            second = write_skill(
                root,
                "zeta-skill",
                "name: zeta-skill\ndescription: Zeta.\nmetadata:\n  author: test\n  version: 1.0.0",
            )
            first = write_skill(
                root,
                "alpha-skill",
                "name: alpha-skill\ndescription: Alpha.\nmetadata:\n  author: test\n  version: 1.0.0",
            )
            (root / "skills" / "not-a-skill").mkdir()

            self.assertEqual((first, second), discover_skill_paths(root / "skills"))

            outside = write_skill(
                root / "outside",
                "linked-skill",
                "name: linked-skill\ndescription: Linked.\nmetadata:\n  author: test\n  version: 1.0.0",
            ).parent
            linked_root = root / "skills" / "linked-skill"
            linked_root.symlink_to(outside)
            with self.assertRaisesRegex(SkillCatalogError, "must not be a symlink"):
                discover_skill_paths(root / "skills")

            linked_root.unlink()
            linked_root.write_text("not a directory\n", encoding="utf-8")
            with self.assertRaisesRegex(SkillCatalogError, "must be a directory"):
                discover_skill_paths(root / "skills")

    def test_rejects_malformed_and_duplicate_fields(self):
        cases = {
            "malformed": "name: malformed\ndescription without colon\nmetadata:\n  author: test\n  version: 1.0.0",
            "duplicate-top": "name: duplicate-top\nname: duplicate-top\ndescription: Test.\nmetadata:\n  author: test\n  version: 1.0.0",
            "duplicate-metadata": "name: duplicate-metadata\ndescription: Test.\nmetadata:\n  author: test\n  author: other\n  version: 1.0.0",
        }
        for slug, frontmatter in cases.items():
            with self.subTest(slug=slug), tempfile.TemporaryDirectory() as directory:
                path = write_skill(Path(directory), slug, frontmatter)
                with self.assertRaises(SkillCatalogError):
                    load_skill(path)

    def test_rejects_typed_collections_and_nested_catalog_values(self):
        cases = {
            "flow-sequence": "description: [not, a, string]",
            "flow-mapping": "description: {text: value}",
            "boolean": "metadata:\n  author: true\n  version: 1.0.0",
            "numeric": "metadata:\n  author: 123\n  version: 1.0.0",
            "null": "metadata:\n  author: test\n  version: 1.0.0\n  summary: null",
            "nested-mapping": "description:\n  nested: value",
            "nested-sequence": "description:\n  - nested value",
        }
        for slug, invalid_field in cases.items():
            with self.subTest(slug=slug), tempfile.TemporaryDirectory() as directory:
                if invalid_field.startswith("metadata:"):
                    frontmatter = f"name: {slug}\ndescription: Test.\n{invalid_field}"
                elif invalid_field.startswith("description:"):
                    frontmatter = (
                        f"name: {slug}\n{invalid_field}\nmetadata:\n"
                        "  author: test\n  version: 1.0.0"
                    )
                path = write_skill(Path(directory), slug, frontmatter)
                with self.assertRaisesRegex(SkillCatalogError, "must be a string"):
                    load_skill(path)

    def test_rejects_name_that_does_not_match_path(self):
        with tempfile.TemporaryDirectory() as directory:
            path = write_skill(
                Path(directory),
                "path-name",
                "name: other-name\ndescription: Test.\nmetadata:\n  author: test\n  version: 1.0.0",
            )

            with self.assertRaisesRegex(SkillCatalogError, "must match directory"):
                load_skill(path)

    def test_bounds_explicit_title_and_summary(self):
        cases = (
            ("title: " + "x" * 101, "title must be at most 100"),
            ("", "metadata.summary must be at most 200"),
        )
        for title_line, message in cases:
            with self.subTest(message=message), tempfile.TemporaryDirectory() as directory:
                summary_line = "  summary: " + "x" * 201 if not title_line else ""
                path = write_skill(
                    Path(directory),
                    "bounded-skill",
                    f"""
name: bounded-skill
{title_line}
description: Test.
metadata:
  author: test
  version: 1.0.0
{summary_line}
""",
                )
                with self.assertRaisesRegex(SkillCatalogError, message):
                    load_skill(path)

    def test_semver_is_strict_and_compares_by_precedence(self):
        self.assertLess(parse_semver("1.0.0-alpha.1"), parse_semver("1.0.0"))
        self.assertEqual(0, compare_semver("1.0.0+first", "1.0.0+second"))
        self.assertEqual(1, compare_semver("2.0.0", "1.99.99"))
        self.assertEqual(-1, compare_semver("1.0.0-alpha.2", "1.0.0-alpha.10"))
        for value in ("1.0", "01.0.0", "1.0.0-01", "v1.0.0"):
            with self.subTest(value=value), self.assertRaises(SkillCatalogError):
                parse_semver(value)

    def test_display_formatter_preserves_established_acronyms(self):
        self.assertEqual("AWS EKS RDS RCA MCP CRM", format_display_name("aws-eks-rds-rca-mcp-crm"))
        self.assertEqual("Database Devops", format_display_name("database-devops"))


if __name__ == "__main__":
    unittest.main()
