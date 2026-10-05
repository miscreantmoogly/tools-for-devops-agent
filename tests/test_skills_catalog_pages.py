import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from docs.hooks.skills_catalog import _build_catalog, _format_name, on_pre_build


REPO_ROOT = Path(__file__).resolve().parents[1]


def write_fixture_skill(
    root: Path,
    slug: str,
    *,
    description: str,
    readme: str | None = None,
    title: str | None = None,
) -> None:
    skill_dir = root / "skills" / slug
    skill_dir.mkdir(parents=True)
    title_field = f'\ntitle: "{title}"' if title else ""
    (skill_dir / "SKILL.md").write_text(
        f"""---
name: {slug}{title_field}
description: {description}
metadata:
  author: example-author
  version: "1.2.3"
  aws-devops-agent-skills.agent-types: "Chat tasks, Incident RCA"
  aws-devops-agent-skills.aws-services: "Amazon EC2"
---

# Instructions
""",
        encoding="utf-8",
    )
    if readme is not None:
        (skill_dir / "README.md").write_text(readme, encoding="utf-8")


def fixture_config(root: Path) -> dict:
    docs_dir = root / "docs"
    docs_dir.mkdir()
    return {
        "docs_dir": str(docs_dir),
        "nav": [
            {
                "Skills": [
                    {"Catalog": ["skills/index.md"]},
                ]
            }
        ],
    }


class ExistingPagesCompatibilityTests(unittest.TestCase):
    def test_current_corpus_has_exact_representative_records(self):
        catalog = _build_catalog(str(REPO_ROOT))

        self.assertEqual(30, len(catalog))
        self.assertEqual(
            {
                "id": "aws-health-events",
                "name": "AWS Health Events",
                "description": "This skill enables the AWS DevOps Agent to retrieve and analyze AWS Health events during incident investigation, root cause analysis, and operational troubleshooting.",
                "dimensions": {
                    "agent-types": ["Chat tasks", "Incident RCA"],
                    "aws-services": ["AWS Health"],
                    "technical-domains": ["Operations"],
                },
                "author": "udid-aws",
                "version": "1.0.0",
            },
            next(item for item in catalog if item["id"] == "aws-health-events"),
        )
        self.assertEqual(
            "This is a <strong>sample skill</strong> demonstrating how to write production investigation guidelines for the AWS DevOps Agent <strong>Incident Triage</strong> agent type. It shows how to encode application-specific troubleshooting knowledge — architecture details, incident isolation rules, and investigation procedures — into a skill that guides the agent during production incidents.",
            next(
                item
                for item in catalog
                if item["id"] == "crm-production-investigation-guidelines"
            )["description"],
        )
        self.assertTrue(all(len(item) == 6 for item in catalog))

    def test_acronym_formatter_remains_available(self):
        self.assertEqual("AWS EKS RCA", _format_name("aws-eks-rca"))


class PagesAdapterFixtureTests(unittest.TestCase):
    def test_readme_first_description_title_and_html_conversion(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_fixture_skill(
                root,
                "example-skill",
                description="Frontmatter fallback.",
                title="A Friendly Name",
                readme=(
                    "# README Title\n\n"
                    "Use **strong text** and [the guide](https://example.com/guide).\n"
                ),
            )

            self.assertEqual(
                [
                    {
                        "id": "example-skill",
                        "name": "A Friendly Name",
                        "description": 'Use <strong>strong text</strong> and <a href="https://example.com/guide" target="_blank" rel="noopener">the guide</a>.',
                        "dimensions": {
                            "agent-types": ["Chat tasks", "Incident RCA"],
                            "aws-services": ["Amazon EC2"],
                        },
                        "author": "example-author",
                        "version": "1.2.3",
                    }
                ],
                _build_catalog(str(root)),
            )

    def test_frontmatter_description_is_used_without_readme(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_fixture_skill(
                root,
                "aws-fallback",
                description="Use **fallback** metadata.",
            )

            record = _build_catalog(str(root))[0]

            self.assertEqual("AWS Fallback", record["name"])
            self.assertEqual("Use <strong>fallback</strong> metadata.", record["description"])

    def test_pre_build_generates_stubs_nav_and_skips_unchanged_writes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_fixture_skill(
                root,
                "zeta-skill",
                description="Zeta fallback.",
                readme="# Zeta README\n\nZeta introduction.\n",
            )
            write_fixture_skill(
                root,
                "alpha-skill",
                description="Alpha fallback.",
            )
            config = fixture_config(root)

            on_pre_build(config)

            data_path = root / "docs" / "javascripts" / "skills-data.json"
            data = json.loads(data_path.read_text(encoding="utf-8"))
            self.assertEqual(["alpha-skill", "zeta-skill"], [item["id"] for item in data])

            alpha_stub = (root / "docs" / "skills" / "alpha-skill.md").read_text(
                encoding="utf-8"
            )
            zeta_stub = (root / "docs" / "skills" / "zeta-skill.md").read_text(
                encoding="utf-8"
            )
            self.assertTrue(alpha_stub.startswith("# Alpha Skill\n\n"))
            self.assertTrue(zeta_stub.startswith("# Zeta README\n\n"))
            self.assertIn(
                "https://github.com/aws/tools-for-devops-agent/tree/main/skills/alpha-skill",
                alpha_stub,
            )
            self.assertIn("by <a href=\"https://github.com/example-author\"", alpha_stub)

            catalog_nav = config["nav"][0]["Skills"][0]["Catalog"]
            self.assertEqual(
                [
                    "skills/index.md",
                    {"Alpha Skill": "skills/alpha-skill.md"},
                    {"Zeta Skill": "skills/zeta-skill.md"},
                ],
                catalog_nav,
            )

            with patch("pathlib.Path.write_text", side_effect=AssertionError("unexpected write")):
                on_pre_build(config)
            self.assertEqual(3, len(catalog_nav))


if __name__ == "__main__":
    unittest.main()
