#!/usr/bin/env python3
"""Generate deterministic standalone metadata and installable skill archives.

The catalog and Pages adapter share the same normalized model so publication cannot
reinterpret skill metadata. Packaging follows a positive manifest: only SKILL.md and
supported regular files beneath references/ or assets/ become installable artifacts;
repository documentation, evaluations, and tooling remain outside the archive.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import shutil
import stat
import sys
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Iterable, Mapping


REPO_ROOT = Path(__file__).resolve().parents[2]
HOOKS_DIR = REPO_ROOT / "docs" / "hooks"
if str(HOOKS_DIR) not in sys.path:
    sys.path.insert(0, str(HOOKS_DIR))

from skill_catalog_model import SkillCatalogError, SkillRecord, load_skills  # noqa: E402


ALLOWED_EXTENSIONS = frozenset(
    {
        ".csv",
        ".gif",
        ".htm",
        ".html",
        ".jpeg",
        ".jpg",
        ".json",
        ".md",
        ".pdf",
        ".png",
        ".svg",
        ".tsv",
        ".txt",
        ".webp",
        ".xml",
        ".yaml",
        ".yml",
    }
)
PACKAGE_ROOTS = ("assets", "references")
_WINDOWS_INVALID_COMPONENT_CHARACTERS = frozenset('<>:"|?*')
_WINDOWS_RESERVED_BASENAMES = frozenset(
    {"aux", "con", "conin$", "conout$", "nul", "prn"}
    | {f"com{index}" for index in range(1, 10)}
    | {f"lpt{index}" for index in range(1, 10)}
)
FIXED_ZIP_TIMESTAMP = (1980, 1, 1, 0, 0, 0)
FIXED_ZIP_MODE = stat.S_IFREG | 0o644


class PackageError(ValueError):
    """Raised when a skill cannot be packaged safely and unambiguously."""


@dataclass(frozen=True)
class PackageEntry:
    """A validated archive name paired with its source file."""

    archive_name: str
    source_path: Path


@dataclass(frozen=True)
class OutputDifferences:
    """Missing, changed, and extra files in a managed output tree."""

    missing: tuple[str, ...] = ()
    changed: tuple[str, ...] = ()
    extra: tuple[str, ...] = ()

    def __bool__(self) -> bool:
        return bool(self.missing or self.changed or self.extra)


def _extractor_normalized_archive_path(name: str) -> str:
    """Approximate the case and trailing-character normalization of extractors."""
    return "/".join(component.rstrip(" .").casefold() for component in name.split("/"))


def validate_archive_path(name: str) -> None:
    """Reject archive names that are unsafe or platform-dependent."""
    if not name:
        raise PackageError("archive path must not be empty")
    if "\\" in name:
        raise PackageError(f"archive path {name!r} must use POSIX separators")
    if any(ord(character) < 32 or ord(character) == 127 for character in name):
        raise PackageError(f"archive path {name!r} contains a control character")

    path = PurePosixPath(name)
    if path.is_absolute() or PureWindowsPath(name).is_absolute():
        raise PackageError(f"archive path {name!r} must be relative")
    components = name.split("/")
    if any(component in {"", ".", ".."} for component in components):
        raise PackageError(f"archive path {name!r} contains traversal or an empty segment")
    for component in components:
        if any(character in _WINDOWS_INVALID_COMPONENT_CHARACTERS for character in component):
            raise PackageError(
                f"archive path {name!r} contains a character invalid on Windows"
            )
        if component.endswith((" ", ".")):
            raise PackageError(
                f"archive path {name!r} contains a component ending in a space or period"
            )
        basename = component.split(".", 1)[0].rstrip(" .").casefold()
        if basename in _WINDOWS_RESERVED_BASENAMES:
            raise PackageError(
                f"archive path {name!r} contains a Windows-reserved component"
            )

    if name != "SKILL.md" and (
        len(path.parts) < 2 or path.parts[0] not in PACKAGE_ROOTS
    ):
        raise PackageError(
            f"archive path {name!r} must be SKILL.md or beneath assets/ or references/"
        )


def _validate_source_file(entry: PackageEntry) -> None:
    try:
        mode = entry.source_path.lstat().st_mode
    except FileNotFoundError as error:
        raise PackageError(f"package source does not exist: {entry.source_path}") from error
    if stat.S_ISLNK(mode):
        raise PackageError(f"symlinks are not supported: {entry.source_path}")
    if not stat.S_ISREG(mode):
        raise PackageError(f"package source must be a regular file: {entry.source_path}")
    if mode & 0o111:
        raise PackageError(f"executable files are not supported: {entry.source_path}")
    archive_suffix = PurePosixPath(entry.archive_name).suffix
    if entry.archive_name != "SKILL.md" and archive_suffix not in ALLOWED_EXTENSIONS:
        raise PackageError(
            f"unsupported package extension {archive_suffix!r}: {entry.archive_name}"
        )


def validate_package_entries(entries: Iterable[PackageEntry]) -> tuple[PackageEntry, ...]:
    """Validate, de-duplicate, and sort a package manifest by POSIX name."""
    manifest = tuple(entries)
    exact_names: set[str] = set()
    folded_names: dict[str, str] = {}
    normalized_names: dict[str, str] = {}
    skill_count = 0

    for entry in manifest:
        if entry.archive_name in exact_names:
            raise PackageError(f"duplicate archive path: {entry.archive_name}")
        exact_names.add(entry.archive_name)

        folded = entry.archive_name.casefold()
        previous = folded_names.get(folded)
        if previous is not None:
            raise PackageError(
                f"case-insensitive archive path collision: {previous!r} and {entry.archive_name!r}"
            )
        folded_names[folded] = entry.archive_name

        normalized = _extractor_normalized_archive_path(entry.archive_name)
        previous = normalized_names.get(normalized)
        if previous is not None:
            raise PackageError(
                f"extractor-normalized archive path collision: {previous!r} and {entry.archive_name!r}"
            )
        normalized_names[normalized] = entry.archive_name

        validate_archive_path(entry.archive_name)
        _validate_source_file(entry)
        if entry.archive_name == "SKILL.md":
            skill_count += 1

    if skill_count != 1:
        raise PackageError("package manifest must contain exactly one root SKILL.md")
    return tuple(sorted(manifest, key=lambda entry: entry.archive_name))


def _walk_package_root(skill_dir: Path, root_name: str) -> list[PackageEntry]:
    package_root = skill_dir / root_name
    if not os.path.lexists(package_root):
        return []

    root_mode = package_root.lstat().st_mode
    if stat.S_ISLNK(root_mode):
        raise PackageError(f"symlinks are not supported: {package_root}")
    if not stat.S_ISDIR(root_mode):
        raise PackageError(f"package root must be a directory: {package_root}")

    entries: list[PackageEntry] = []
    for current_root, directory_names, file_names in os.walk(
        package_root, topdown=True, followlinks=False
    ):
        current = Path(current_root)
        directory_names.sort()
        file_names.sort()

        for directory_name in directory_names:
            directory = current / directory_name
            mode = directory.lstat().st_mode
            archive_name = directory.relative_to(skill_dir).as_posix()
            validate_archive_path(archive_name)
            if stat.S_ISLNK(mode):
                raise PackageError(f"symlinks are not supported: {directory}")
            if not stat.S_ISDIR(mode):
                raise PackageError(f"package path must be a directory: {directory}")

        for file_name in file_names:
            source_path = current / file_name
            entries.append(
                PackageEntry(
                    archive_name=source_path.relative_to(skill_dir).as_posix(),
                    source_path=source_path,
                )
            )
    return entries


def package_manifest(skill_dir: Path) -> tuple[PackageEntry, ...]:
    """Return the positive installable manifest for one real skill directory."""
    try:
        root_mode = skill_dir.lstat().st_mode
    except FileNotFoundError as error:
        raise PackageError(f"skill root does not exist: {skill_dir}") from error
    if stat.S_ISLNK(root_mode):
        raise PackageError(f"skill root must not be a symlink: {skill_dir}")
    if not stat.S_ISDIR(root_mode):
        raise PackageError(f"skill root must be a directory: {skill_dir}")

    entries = [PackageEntry("SKILL.md", skill_dir / "SKILL.md")]
    for root_name in PACKAGE_ROOTS:
        entries.extend(_walk_package_root(skill_dir, root_name))
    return validate_package_entries(entries)


def build_archive(entries: Iterable[PackageEntry]) -> bytes:
    """Build exact cross-platform ZIP bytes for a validated manifest."""
    manifest = validate_package_entries(entries)
    output = io.BytesIO()
    with zipfile.ZipFile(output, mode="w", compression=zipfile.ZIP_STORED) as archive:
        for entry in manifest:
            info = zipfile.ZipInfo(entry.archive_name, date_time=FIXED_ZIP_TIMESTAMP)
            info.compress_type = zipfile.ZIP_STORED
            info.create_system = 3
            info.external_attr = FIXED_ZIP_MODE << 16
            archive.writestr(info, entry.source_path.read_bytes())
    return output.getvalue()


def build_skill_archive(skill_dir: Path) -> bytes:
    """Build deterministic archive bytes for one skill directory."""
    return build_archive(package_manifest(skill_dir))


def catalog_record(record: SkillRecord, sha256: str) -> dict[str, object]:
    """Convert the shared model to the standalone catalog contract."""
    return {
        "id": record.id,
        "name": record.name,
        "title": record.title,
        "summary": record.summary,
        "description": record.description,
        "author": record.author,
        "version": record.version,
        "dimensions": {
            key: list(record.dimensions[key]) for key in sorted(record.dimensions)
        },
        "sha256": sha256,
    }


def generate_managed_files(repo_root: Path) -> dict[str, bytes]:
    """Generate the complete managed output tree in memory."""
    records: list[dict[str, object]] = []
    managed_files: dict[str, bytes] = {}
    for record in load_skills(repo_root / "skills"):
        archive_bytes = build_skill_archive(record.source_path.parent)
        digest = hashlib.sha256(archive_bytes).hexdigest()
        records.append(catalog_record(record, digest))
        managed_files[f"skills/{record.id}/{digest}.zip"] = archive_bytes

    managed_files["skills.json"] = (
        json.dumps(records, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")
    return managed_files


def _remove_managed_path(path: Path) -> None:
    if not os.path.lexists(path):
        return
    if path.is_symlink() or not path.is_dir():
        path.unlink()
    else:
        shutil.rmtree(path)


def write_managed_output(repo_root: Path, output_dir: Path) -> dict[str, bytes]:
    """Replace only skills.json and skills/ after successful in-memory generation."""
    managed_files = generate_managed_files(repo_root)
    if (output_dir / "skills").resolve() == (repo_root / "skills").resolve():
        raise PackageError("output directory must not replace the repository's source skills")
    output_dir.mkdir(parents=True, exist_ok=True)
    _remove_managed_path(output_dir / "skills.json")
    _remove_managed_path(output_dir / "skills")

    for relative_name, content in sorted(managed_files.items()):
        destination = output_dir / PurePosixPath(relative_name)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(content)
    return managed_files


def _read_actual_managed_files(output_dir: Path) -> dict[str, bytes | None]:
    actual: dict[str, bytes | None] = {}
    catalog_path = output_dir / "skills.json"
    if os.path.lexists(catalog_path):
        actual["skills.json"] = (
            catalog_path.read_bytes()
            if catalog_path.is_file() and not catalog_path.is_symlink()
            else None
        )

    skills_path = output_dir / "skills"
    if not os.path.lexists(skills_path):
        return actual
    if skills_path.is_symlink() or not skills_path.is_dir():
        actual["skills"] = None
        return actual

    for current_root, directory_names, file_names in os.walk(
        skills_path, topdown=True, followlinks=False
    ):
        current = Path(current_root)
        directory_names.sort()
        file_names.sort()
        for directory_name in tuple(directory_names):
            directory = current / directory_name
            if directory.is_symlink():
                relative_name = directory.relative_to(output_dir).as_posix()
                actual[relative_name] = None
                directory_names.remove(directory_name)
        for file_name in file_names:
            path = current / file_name
            relative_name = path.relative_to(output_dir).as_posix()
            actual[relative_name] = (
                path.read_bytes() if path.is_file() and not path.is_symlink() else None
            )
    return actual


def compare_managed_output(
    expected: Mapping[str, bytes], output_dir: Path
) -> OutputDifferences:
    """Compare expected bytes with the supplied directory's managed files."""
    actual = _read_actual_managed_files(output_dir)
    expected_names = set(expected)
    actual_names = set(actual)
    return OutputDifferences(
        missing=tuple(sorted(expected_names - actual_names)),
        changed=tuple(
            sorted(
                name
                for name in expected_names & actual_names
                if actual[name] != expected[name]
            )
        ),
        extra=tuple(sorted(actual_names - expected_names)),
    )


def check_managed_output(repo_root: Path, output_dir: Path) -> OutputDifferences:
    """Generate an independent expected tree, then compare managed output bytes."""
    with tempfile.TemporaryDirectory() as temporary_directory:
        expected_dir = Path(temporary_directory)
        write_managed_output(repo_root, expected_dir)
        expected = _read_actual_managed_files(expected_dir)
        return compare_managed_output(
            {name: content for name, content in expected.items() if content is not None},
            output_dir,
        )


def _print_differences(differences: OutputDifferences) -> None:
    print("Skill catalog output is not current:", file=sys.stderr)
    for label, names in (
        ("missing", differences.missing),
        ("changed", differences.changed),
        ("extra", differences.extra),
    ):
        for name in names:
            print(f"  {label}: {name}", file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output-dir",
        required=True,
        type=Path,
        help="Directory that owns the generated skills.json and skills/ outputs.",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Compare fresh expected artifacts without modifying the output directory.",
    )
    args = parser.parse_args(argv)

    try:
        if args.check:
            differences = check_managed_output(REPO_ROOT, args.output_dir)
            if differences:
                _print_differences(differences)
                return 1
            print("Skill catalog output is current.")
        else:
            managed_files = write_managed_output(REPO_ROOT, args.output_dir)
            archive_count = sum(name.endswith(".zip") for name in managed_files)
            print(f"Generated skills.json and {archive_count} skill archives.")
    except (OSError, PackageError, SkillCatalogError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
