#!/usr/bin/env python3
"""Validate skill catalog policy locally or across a pull-request merge base.

This check reuses the catalog model and deterministic package builder so validation
cannot drift from generated artifacts. It is intentionally a narrow structural and
high-confidence secret guard; maintainers still review semantic correctness, update
intent, and IAM safety.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import stat
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable


REPO_ROOT = Path(__file__).resolve().parents[2]
HOOKS_DIR = REPO_ROOT / "docs" / "hooks"
if str(HOOKS_DIR) not in sys.path:
    sys.path.insert(0, str(HOOKS_DIR))
if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))

from generate_skill_catalog import (  # noqa: E402
    PackageError,
    build_skill_archive,
    package_manifest,
)
from skill_catalog_model import (  # noqa: E402
    SkillCatalogError,
    SkillRecord,
    compare_semver,
    load_skill,
    metadata_field_line_span,
)


TEXT_PAYLOAD_EXTENSIONS = frozenset(
    {".csv", ".htm", ".html", ".json", ".md", ".svg", ".tsv", ".txt", ".xml", ".yaml", ".yml"}
)
SKILL_ID_PATTERN = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
PEM_PRIVATE_KEY_PATTERN = re.compile(
    rb"-----BEGIN (?:RSA |EC |DSA |OPENSSH |ENCRYPTED )?PRIVATE KEY-----"
)
AWS_ACCESS_KEY_PATTERN = re.compile(rb"(?<![A-Z0-9])AKIA[A-Z0-9]{16}(?![A-Z0-9])")


class GitError(RuntimeError):
    """Raised when repository history required for policy validation is unavailable."""


@dataclass(frozen=True)
class Finding:
    """One policy diagnostic associated with a skill or repository operation."""

    severity: str
    message: str
    skill_id: str = "catalog"
    path: str | None = None


@dataclass(frozen=True)
class SkillState:
    """Validated metadata and both deterministic runtime fingerprints."""

    record: SkillRecord
    archive_sha256: str
    version_neutral_sha256: str


@dataclass(frozen=True)
class DiffChange:
    """One parsed entry from ``git diff --name-status -z``."""

    status: str
    old_path: str | None
    new_path: str | None

    @property
    def paths(self) -> tuple[str, ...]:
        return tuple(path for path in (self.old_path, self.new_path) if path)


@dataclass
class ValidationReport:
    """Collected diagnostics and checked identities."""

    skill_ids: set[str] = field(default_factory=set)
    findings: list[Finding] = field(default_factory=list)

    def add(
        self,
        severity: str,
        message: str,
        skill_id: str = "catalog",
        path: str | None = None,
    ) -> None:
        self.findings.append(Finding(severity, message, skill_id, path))

    @property
    def blockers(self) -> list[Finding]:
        return [finding for finding in self.findings if finding.severity == "blocker"]

    @property
    def advisories(self) -> list[Finding]:
        return [finding for finding in self.findings if finding.severity == "advisory"]


def _git(repo_root: Path, *args: str, text: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        cwd=repo_root,
        capture_output=True,
        text=text,
        check=False,
    )


def _git_checked(repo_root: Path, *args: str, text: bool = True):
    result = _git(repo_root, *args, text=text)
    if result.returncode != 0:
        stderr = result.stderr.strip() if text else result.stderr.decode(errors="replace").strip()
        command = "git " + " ".join(args)
        raise GitError(f"{command} failed: {stderr or 'unknown Git error'}")
    return result


def merge_base(repo_root: Path, base_ref: str, head_ref: str) -> str:
    """Resolve the common ancestor used to attribute changes to a pull request."""
    _git_checked(repo_root, "rev-parse", "--verify", f"{base_ref}^{{commit}}")
    _git_checked(repo_root, "rev-parse", "--verify", f"{head_ref}^{{commit}}")
    result = _git_checked(repo_root, "merge-base", base_ref, head_ref)
    value = result.stdout.strip()
    if not value:
        raise GitError(f"no merge base between {base_ref} and {head_ref}")
    return value


def parse_name_status(raw: bytes) -> tuple[DiffChange, ...]:
    """Parse NUL-delimited name-status output, including rename source and target."""
    tokens = raw.split(b"\0")
    if tokens and tokens[-1] == b"":
        tokens.pop()
    changes: list[DiffChange] = []
    index = 0
    try:
        while index < len(tokens):
            status = tokens[index].decode("utf-8")
            index += 1
            if status.startswith(("R", "C")):
                old_path = tokens[index].decode("utf-8")
                new_path = tokens[index + 1].decode("utf-8")
                index += 2
            else:
                old_path = tokens[index].decode("utf-8") if status.startswith("D") else None
                new_path = None if status.startswith("D") else tokens[index].decode("utf-8")
                index += 1
            changes.append(DiffChange(status, old_path, new_path))
    except (IndexError, UnicodeDecodeError) as error:
        raise GitError("git diff returned malformed NUL-delimited name-status output") from error
    return tuple(changes)


def changed_paths(repo_root: Path, base: str, head_ref: str) -> tuple[DiffChange, ...]:
    result = _git_checked(
        repo_root,
        "diff",
        "--name-status",
        "-z",
        "-M",
        base,
        head_ref,
        "--",
        "skills/",
        text=False,
    )
    return parse_name_status(result.stdout)


def _skill_id_from_path(path: str) -> str | None:
    parts = path.split("/")
    if len(parts) < 2 or parts[0] != "skills" or not parts[1]:
        return None
    return parts[1]


def changed_skill_ids(changes: Iterable[DiffChange]) -> tuple[str, ...]:
    return tuple(
        sorted(
            {
                skill_id
                for change in changes
                for path in change.paths
                if (skill_id := _skill_id_from_path(path)) is not None
            }
        )
    )


def _exists_at_ref(repo_root: Path, ref: str, path: str) -> bool:
    result = _git(repo_root, "cat-file", "-e", f"{ref}:{path}")
    return result.returncode == 0


def _blob(repo_root: Path, object_id: str) -> bytes:
    return _git_checked(repo_root, "cat-file", "blob", object_id, text=False).stdout


def materialize_skill(
    repo_root: Path,
    ref: str,
    skill_id: str,
    destination: Path,
    report: ValidationReport | None = None,
) -> bool:
    """Materialize one skill from a Git tree without checking out contributor code."""
    relative_root = f"skills/{skill_id}"
    result = _git_checked(
        repo_root,
        "ls-tree",
        "-rz",
        "--full-tree",
        ref,
        "--",
        relative_root,
        text=False,
    )
    entries = [entry for entry in result.stdout.split(b"\0") if entry]
    if not entries:
        return False

    for raw_entry in entries:
        try:
            metadata, raw_path = raw_entry.split(b"\t", 1)
            mode, object_type, object_id = metadata.decode("ascii").split(" ")
            relative_path = raw_path.decode("utf-8")
        except (ValueError, UnicodeDecodeError) as error:
            raise GitError(f"git ls-tree returned malformed data for {relative_root}") from error
        prefix = relative_root + "/"
        if relative_path == relative_root:
            target = destination / "skills" / skill_id
            package_path = True
        elif relative_path.startswith(prefix):
            skill_relative_path = relative_path.removeprefix(prefix)
            target = destination / "skills" / skill_id / Path(skill_relative_path)
            package_path = skill_relative_path == "SKILL.md" or skill_relative_path.startswith(
                ("references/", "assets/")
            )
        else:
            raise GitError(f"git ls-tree returned an unexpected path: {relative_path}")
        target.parent.mkdir(parents=True, exist_ok=True)
        if object_type != "blob":
            if report is not None and package_path:
                report.skill_ids.add(skill_id)
                report.add(
                    "blocker",
                    f"package policy: Git object {object_type!r} is not a regular file",
                    skill_id,
                    relative_path,
                )
            continue
        content = _blob(repo_root, object_id)
        if mode == "120000":
            try:
                target.symlink_to(content.decode("utf-8"))
            except UnicodeDecodeError as error:
                raise GitError(f"symlink target is not UTF-8 at {relative_path}") from error
        elif mode in {"100644", "100755"}:
            target.write_bytes(content)
            target.chmod(0o755 if mode == "100755" else 0o644)
        else:
            if report is not None and package_path:
                report.skill_ids.add(skill_id)
                report.add(
                    "blocker",
                    f"package policy: Git mode {mode!r} is not a regular file",
                    skill_id,
                    relative_path,
                )
    return True


def _neutralize_version(content: bytes, source_path: Path) -> bytes:
    """Replace the complete parsed metadata.version scalar with one placeholder."""
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError as error:
        raise SkillCatalogError(f"{source_path}: SKILL.md must be UTF-8") from error
    lines = text.splitlines(keepends=True)
    start, stop = metadata_field_line_span(source_path, "version")
    raw = lines[start]
    body = raw.rstrip("\r\n")
    ending = raw[len(body) :]
    indentation = body[: len(body) - len(body.lstrip())]
    lines[start:stop] = [f"{indentation}version: __CATALOG_VERSION__{ending}"]
    return "".join(lines).encode("utf-8")


def version_neutral_fingerprint(skill_dir: Path) -> str:
    """Hash deterministic package bytes after canonicalizing only metadata.version."""
    manifest = package_manifest(skill_dir)
    with tempfile.TemporaryDirectory() as directory:
        neutral_dir = Path(directory)
        for entry in manifest:
            destination = neutral_dir / entry.archive_name
            destination.parent.mkdir(parents=True, exist_ok=True)
            content = entry.source_path.read_bytes()
            if entry.archive_name == "SKILL.md":
                content = _neutralize_version(content, entry.source_path)
            destination.write_bytes(content)
            destination.chmod(0o644)
        return hashlib.sha256(build_skill_archive(neutral_dir)).hexdigest()


def _scan_secrets(skill_id: str, manifest, report: ValidationReport) -> None:
    for entry in manifest:
        suffix = Path(entry.archive_name).suffix.lower()
        if entry.archive_name != "SKILL.md" and suffix not in TEXT_PAYLOAD_EXTENSIONS:
            continue
        content = entry.source_path.read_bytes()
        relative_path = f"skills/{skill_id}/{entry.archive_name}"
        if PEM_PRIVATE_KEY_PATTERN.search(content):
            report.add(
                "blocker",
                "installable payload contains a PEM private-key header",
                skill_id,
                relative_path,
            )
        if AWS_ACCESS_KEY_PATTERN.search(content):
            report.add(
                "blocker",
                "installable payload contains an AWS access key ID",
                skill_id,
                relative_path,
            )


def inspect_skill(skill_root: Path, skill_id: str, report: ValidationReport) -> SkillState | None:
    """Collect all independent model, package, dimension, and secret diagnostics."""
    report.skill_ids.add(skill_id)
    relative_skill = f"skills/{skill_id}/SKILL.md"
    if not SKILL_ID_PATTERN.fullmatch(skill_id) or len(skill_id) > 64:
        report.add(
            "blocker",
            "directory identity must be at most 64 lowercase letters, numbers, and hyphens",
            skill_id,
            relative_skill,
        )
        return None
    skill_dir = skill_root / "skills" / skill_id
    skill_path = skill_dir / "SKILL.md"
    try:
        root_mode = skill_dir.lstat().st_mode
    except FileNotFoundError:
        report.add("blocker", "skill root must be a directory", skill_id, relative_skill)
        return None
    if stat.S_ISLNK(root_mode):
        report.add("blocker", "skill root must not be a symlink", skill_id, relative_skill)
        return None
    if not stat.S_ISDIR(root_mode):
        report.add("blocker", "skill root must be a directory", skill_id, relative_skill)
        return None
    if not skill_path.is_file():
        report.add("blocker", "skill directory must contain SKILL.md", skill_id, relative_skill)
        return None

    record: SkillRecord | None = None
    try:
        record = load_skill(skill_path)
    except (OSError, SkillCatalogError) as error:
        report.add("blocker", str(error), skill_id, relative_skill)

    manifest = None
    archive_bytes: bytes | None = None
    neutral_sha: str | None = None
    try:
        manifest = package_manifest(skill_dir)
        _scan_secrets(skill_id, manifest, report)
        archive_bytes = build_skill_archive(skill_dir)
    except (OSError, PackageError, SkillCatalogError) as error:
        report.add("blocker", f"package policy: {error}", skill_id, relative_skill)

    if record is not None and manifest is not None and archive_bytes is not None:
        try:
            neutral_sha = version_neutral_fingerprint(skill_dir)
        except (OSError, PackageError, SkillCatalogError) as error:
            report.add("blocker", f"package policy: {error}", skill_id, relative_skill)

    if record is None or archive_bytes is None or neutral_sha is None:
        return None
    return SkillState(record, hashlib.sha256(archive_bytes).hexdigest(), neutral_sha)


def _inspect_base_skill(skill_root: Path, skill_id: str) -> SkillState | None:
    """Read a merge-base state for comparison without attributing old issues to the PR."""
    temporary_report = ValidationReport()
    return inspect_skill(skill_root, skill_id, temporary_report)


def _is_shallow(repo_root: Path) -> bool:
    value = _git_checked(repo_root, "rev-parse", "--is-shallow-repository").stdout.strip()
    if value not in {"true", "false"}:
        raise GitError(f"unexpected shallow-repository result: {value!r}")
    return value == "true"


def _existed_in_ancestry(repo_root: Path, base: str, skill_id: str) -> bool:
    path = f"skills/{skill_id}/SKILL.md"
    result = _git_checked(repo_root, "log", "--format=%H", base, "--", path)
    return bool(result.stdout.strip())


def _add_rename_advisories(changes: Iterable[DiffChange], report: ValidationReport) -> None:
    seen: set[tuple[str, str]] = set()
    for change in changes:
        if not change.status.startswith("R") or not change.old_path or not change.new_path:
            continue
        old_id = _skill_id_from_path(change.old_path)
        new_id = _skill_id_from_path(change.new_path)
        if old_id and new_id and old_id != new_id and (old_id, new_id) not in seen:
            seen.add((old_id, new_id))
            report.add(
                "advisory",
                f"likely directory rename from {old_id!r} to {new_id!r}; path identity changes and provenance needs human review",
                new_id,
                f"skills/{new_id}/SKILL.md",
            )


def validate_pull_request(
    repo_root: Path, base_ref: str, head_ref: str
) -> ValidationReport:
    base = merge_base(repo_root, base_ref, head_ref)
    changes = changed_paths(repo_root, base, head_ref)
    report = ValidationReport()
    identities = changed_skill_ids(changes)
    if not identities:
        return report
    _add_rename_advisories(changes, report)

    with tempfile.TemporaryDirectory() as base_directory, tempfile.TemporaryDirectory() as head_directory:
        base_root = Path(base_directory)
        head_root = Path(head_directory)
        base_has: dict[str, bool] = {}
        head_has: dict[str, bool] = {}
        for skill_id in identities:
            base_has[skill_id] = materialize_skill(repo_root, base, skill_id, base_root)
            head_has[skill_id] = materialize_skill(
                repo_root,
                head_ref,
                skill_id,
                head_root,
                report,
            )

        shallow = _is_shallow(repo_root)
        for skill_id in identities:
            base_skill = base_root / "skills" / skill_id / "SKILL.md"
            head_skill = head_root / "skills" / skill_id / "SKILL.md"
            existed_at_base = base_skill.is_file()
            exists_at_head = head_skill.is_file()

            if existed_at_base and not exists_at_head:
                report.skill_ids.add(skill_id)
                report.add(
                    "advisory",
                    "skill identity was deleted; installed copies remain pinned to their existing content",
                    skill_id,
                    f"skills/{skill_id}/SKILL.md",
                )
                continue

            if not head_has[skill_id]:
                continue
            head_state = inspect_skill(head_root, skill_id, report)

            if not existed_at_base and exists_at_head:
                if shallow:
                    report.add(
                        "advisory",
                        "repository is shallow, so retired-path reuse could not be proven; rerun with full history",
                        skill_id,
                        f"skills/{skill_id}/SKILL.md",
                    )
                elif _existed_in_ancestry(repo_root, base, skill_id):
                    report.add(
                        "blocker",
                        "path reuses a retired skill identity from merge-base ancestry",
                        skill_id,
                        f"skills/{skill_id}/SKILL.md",
                    )

            if not existed_at_base or head_state is None:
                continue
            base_state = _inspect_base_skill(base_root, skill_id)
            if base_state is None:
                continue
            version_change = compare_semver(head_state.record.version, base_state.record.version)
            if version_change < 0:
                report.add(
                    "blocker",
                    f"version decreased from {base_state.record.version} to {head_state.record.version}",
                    skill_id,
                    f"skills/{skill_id}/SKILL.md",
                )
            runtime_changed = head_state.archive_sha256 != base_state.archive_sha256
            other_runtime_changed = (
                head_state.version_neutral_sha256 != base_state.version_neutral_sha256
            )
            if runtime_changed and version_change == 0:
                report.add(
                    "advisory",
                    f"installable runtime bytes changed without increasing version {head_state.record.version}",
                    skill_id,
                    f"skills/{skill_id}/SKILL.md",
                )
            elif version_change > 0 and not other_runtime_changed:
                report.add(
                    "advisory",
                    f"version increased from {base_state.record.version} to {head_state.record.version} with no other installable runtime-content change",
                    skill_id,
                    f"skills/{skill_id}/SKILL.md",
                )
    return report


def _all_skill_ids(repo_root: Path) -> tuple[str, ...]:
    skills_dir = repo_root / "skills"
    return tuple(
        sorted(
            path.name
            for path in skills_dir.iterdir()
            if not path.name.startswith(".")
            and (
                path.is_symlink()
                or path.is_dir()
                or SKILL_ID_PATTERN.fullmatch(path.name)
            )
        )
    )


def validate_local(repo_root: Path, skill_ids: Iterable[str]) -> ValidationReport:
    report = ValidationReport()
    for skill_id in sorted(set(skill_ids)):
        inspect_skill(repo_root, skill_id, report)
    return report


def _escape_annotation_data(value: str) -> str:
    return value.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def _escape_annotation_property(value: str) -> str:
    return _escape_annotation_data(value).replace(":", "%3A").replace(",", "%2C")


def _emit_annotations(report: ValidationReport) -> None:
    if not os.environ.get("GITHUB_ACTIONS"):
        return
    for finding in report.findings:
        level = "error" if finding.severity == "blocker" else "warning"
        properties = [f"title={_escape_annotation_property('Skill catalog: ' + finding.skill_id)}"]
        if finding.path:
            properties.append(f"file={_escape_annotation_property(finding.path)}")
        print(
            f"::{level} {','.join(properties)}::{_escape_annotation_data(finding.message)}"
        )


def _markdown(value: str) -> str:
    return value.replace("|", "\\|").replace("\n", " ")


def _write_summary(report: ValidationReport, internal_error: str | None = None) -> None:
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not summary_path:
        return
    lines = ["## Skill catalog validation", ""]
    if internal_error:
        lines.append(f"**Internal/Git failure:** {_markdown(internal_error)}")
    elif not report.skill_ids:
        lines.append("No skill identities were touched. Nothing to validate.")
    elif not report.findings:
        lines.append(f"Validated {len(report.skill_ids)} skill(s) with no findings.")
    else:
        lines.extend(
            [
                f"Validated {len(report.skill_ids)} skill(s): {len(report.blockers)} blocker(s), {len(report.advisories)} advisory finding(s).",
                "",
                "| Severity | Skill | Finding |",
                "| --- | --- | --- |",
            ]
        )
        for finding in report.findings:
            lines.append(
                f"| {finding.severity} | `{_markdown(finding.skill_id)}` | {_markdown(finding.message)} |"
            )
    with open(summary_path, "a", encoding="utf-8") as summary:
        summary.write("\n".join(lines) + "\n")


def print_report(report: ValidationReport) -> None:
    if not report.skill_ids:
        print("No skill identities touched; catalog validation skipped.")
        return
    if not report.findings:
        print(f"PASS  {len(report.skill_ids)} skill(s) validated with no findings.")
        return
    for finding in report.findings:
        label = "FAIL" if finding.severity == "blocker" else "WARN"
        location = f" [{finding.path}]" if finding.path else ""
        print(f"{label}  {finding.skill_id}{location}: {finding.message}")
    print(
        f"\n{len(report.skill_ids)} skill(s) checked: {len(report.blockers)} blocker(s), "
        f"{len(report.advisories)} advisory finding(s).",
        file=sys.stderr,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--skill",
        action="append",
        metavar="ID",
        help="Validate one working-tree skill. Repeatable.",
    )
    mode.add_argument("--all", action="store_true", help="Audit every working-tree skill.")
    parser.add_argument("--base-ref", help="Pull-request base ref.")
    parser.add_argument("--head-ref", default="HEAD", help="Pull-request head ref (default: HEAD).")
    args = parser.parse_args(argv)

    if not args.all and not args.skill and not args.base_ref:
        parser.error("choose --all, at least one --skill, or --base-ref with --head-ref")
    if (args.all or args.skill) and args.base_ref:
        parser.error("--base-ref cannot be combined with --all or --skill")

    report = ValidationReport()
    try:
        if args.all:
            report = validate_local(REPO_ROOT, _all_skill_ids(REPO_ROOT))
        elif args.skill:
            report = validate_local(REPO_ROOT, args.skill)
        else:
            report = validate_pull_request(REPO_ROOT, args.base_ref, args.head_ref)
    except GitError as error:
        message = str(error)
        print(f"ERROR  {message}", file=sys.stderr)
        if os.environ.get("GITHUB_ACTIONS"):
            print(f"::error title=Skill catalog internal failure::{_escape_annotation_data(message)}")
        _write_summary(report, message)
        return 2
    except Exception as error:  # Keep unexpected validator failures distinct from policy.
        message = f"internal validation failure: {error}"
        print(f"ERROR  {message}", file=sys.stderr)
        if os.environ.get("GITHUB_ACTIONS"):
            print(f"::error title=Skill catalog internal failure::{_escape_annotation_data(message)}")
        _write_summary(report, message)
        return 2

    print_report(report)
    _emit_annotations(report)
    _write_summary(report)
    return 1 if report.blockers else 0


if __name__ == "__main__":
    raise SystemExit(main())
