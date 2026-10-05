"""Shared, dependency-free model for skills published by this repository.

Catalog consumers should agree on one parser and normalized record so a skill cannot
mean one thing on Pages and another in publication tooling. A skill's directory path
is its durable identity: the frontmatter name confirms that identity rather than
introducing a second identifier that can drift.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from functools import total_ordering
from pathlib import Path
from types import MappingProxyType
from typing import Mapping


_NAME_PATTERN = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_KEY_PATTERN = re.compile(r"^([A-Za-z0-9_.-]+):(?:[ \t]*(.*))?$")
_SEMVER_PATTERN = re.compile(
    r"^(0|[1-9]\d*)\."
    r"(0|[1-9]\d*)\."
    r"(0|[1-9]\d*)"
    r"(?:-((?:0|[1-9]\d*|\d*[A-Za-z-][0-9A-Za-z-]*)"
    r"(?:\.(?:0|[1-9]\d*|\d*[A-Za-z-][0-9A-Za-z-]*))*))?"
    r"(?:\+([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?$"
)
_DIMENSION_PREFIX = "aws-devops-agent-skills."
KNOWN_DIMENSIONS = frozenset({"agent-types", "aws-services", "technical-domains"})
_TYPED_PLAIN_SCALAR_PATTERN = re.compile(
    r"^(?:true|false|null|~|[-+]?(?:\d+(?:\.\d+)?(?:e[-+]?\d+)?|\.inf|\.nan))$",
    re.IGNORECASE,
)
_ACRONYMS = frozenset({"aws", "eks", "rds", "rca", "mcp", "crm"})


class SkillCatalogError(ValueError):
    """Raised when skill metadata cannot be normalized safely."""


@total_ordering
@dataclass(frozen=True, eq=False)
class SemVer:
    """A strict Semantic Versioning 2.0.0 value with precedence comparison."""

    major: int
    minor: int
    patch: int
    prerelease: tuple[str, ...] = ()
    build: tuple[str, ...] = ()

    @classmethod
    def parse(cls, value: str) -> "SemVer":
        match = _SEMVER_PATTERN.fullmatch(value)
        if not match:
            raise SkillCatalogError(
                f"version {value!r} must be strict Semantic Versioning (for example, 1.2.3)"
            )
        prerelease = tuple(match.group(4).split(".")) if match.group(4) else ()
        build = tuple(match.group(5).split(".")) if match.group(5) else ()
        return cls(
            major=int(match.group(1)),
            minor=int(match.group(2)),
            patch=int(match.group(3)),
            prerelease=prerelease,
            build=build,
        )

    def __str__(self) -> str:
        value = f"{self.major}.{self.minor}.{self.patch}"
        if self.prerelease:
            value += "-" + ".".join(self.prerelease)
        if self.build:
            value += "+" + ".".join(self.build)
        return value

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, SemVer):
            return NotImplemented
        return self._precedence_key() == other._precedence_key()

    def __lt__(self, other: object) -> bool:
        if not isinstance(other, SemVer):
            return NotImplemented
        return self._precedence_key() < other._precedence_key()

    def __hash__(self) -> int:
        return hash(self._precedence_key())

    def _precedence_key(self) -> tuple[object, ...]:
        if not self.prerelease:
            prerelease_key: tuple[object, ...] = (1,)
        else:
            identifiers = tuple(
                (0, int(identifier)) if identifier.isdigit() else (1, identifier)
                for identifier in self.prerelease
            )
            prerelease_key = (0, identifiers)
        return self.major, self.minor, self.patch, prerelease_key


@dataclass(frozen=True)
class SkillRecord:
    """An immutable, normalized view of one path-identified skill."""

    id: str
    name: str
    title: str
    summary: str
    description: str
    author: str
    version: str
    dimensions: Mapping[str, tuple[str, ...]]
    source_path: Path

    @property
    def semver(self) -> SemVer:
        return SemVer.parse(self.version)

    def pages_dimensions(self) -> dict[str, list[str]]:
        return {key: list(values) for key, values in self.dimensions.items()}


def parse_semver(value: str) -> SemVer:
    """Parse a strict Semantic Versioning value."""
    return SemVer.parse(value)


def compare_semver(left: str | SemVer, right: str | SemVer) -> int:
    """Return -1, 0, or 1 using Semantic Versioning precedence rules."""
    left_version = SemVer.parse(left) if isinstance(left, str) else left
    right_version = SemVer.parse(right) if isinstance(right, str) else right
    return (left_version > right_version) - (left_version < right_version)


def format_display_name(skill_id: str) -> str:
    """Convert a path slug to the established Pages display-name fallback."""
    return " ".join(
        word.upper() if word.lower() in _ACRONYMS else word.capitalize()
        for word in skill_id.split("-")
    )


def discover_skill_paths(skills_dir: Path) -> tuple[Path, ...]:
    """Return SKILL.md files in stable path order, rejecting unsafe skill roots."""
    if not skills_dir.is_dir():
        return ()

    skill_paths: list[Path] = []
    for entry in sorted(skills_dir.iterdir(), key=lambda path: path.name):
        if entry.name.startswith("."):
            continue
        if entry.is_symlink():
            raise SkillCatalogError(f"{entry}: skill root must not be a symlink")
        if not entry.is_dir():
            if _NAME_PATTERN.fullmatch(entry.name):
                raise SkillCatalogError(f"{entry}: skill root must be a directory")
            continue
        skill_path = entry / "SKILL.md"
        if skill_path.is_file():
            skill_paths.append(skill_path)
    return tuple(skill_paths)


def parse_frontmatter(path: Path) -> Mapping[str, object]:
    """Parse the scalar-and-metadata subset used by SKILL.md without a YAML dependency."""
    values, _ = _parse_frontmatter(path)
    return values


def metadata_field_line_span(path: Path, key: str) -> tuple[int, int]:
    """Return the complete zero-based line span for one parsed metadata scalar."""
    _, metadata_spans = _parse_frontmatter(path)
    try:
        return metadata_spans[key]
    except KeyError as error:
        raise SkillCatalogError(f"{path}: metadata field {key!r} is required") from error


def _parse_frontmatter(
    path: Path,
) -> tuple[Mapping[str, object], Mapping[str, tuple[int, int]]]:
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()
    if not lines or lines[0] != "---":
        raise SkillCatalogError(f"{path}: missing opening frontmatter delimiter")

    try:
        end = lines.index("---", 1)
    except ValueError as error:
        raise SkillCatalogError(f"{path}: missing closing frontmatter delimiter") from error

    values: dict[str, object] = {}
    metadata_spans: dict[str, tuple[int, int]] = {}
    frontmatter = lines[1:end]
    index = 0
    while index < len(frontmatter):
        line = frontmatter[index]
        if not line.strip() or line.lstrip().startswith("#"):
            index += 1
            continue
        if line[0].isspace():
            raise SkillCatalogError(f"{path}: unexpected indentation in frontmatter")

        match = _KEY_PATTERN.fullmatch(line)
        if not match:
            raise SkillCatalogError(f"{path}: malformed frontmatter line {line!r}")
        key = match.group(1)
        if key in values:
            raise SkillCatalogError(f"{path}: duplicate frontmatter field {key!r}")
        raw_value = match.group(2) or ""
        index += 1

        nested_start = index
        nested: list[str] = []
        while index < len(frontmatter):
            candidate = frontmatter[index]
            if candidate and not candidate[0].isspace():
                break
            nested.append(candidate)
            index += 1

        if key == "metadata":
            if raw_value.strip():
                raise SkillCatalogError(f"{path}: metadata must be a nested mapping")
            metadata, relative_spans = _parse_metadata(nested, path)
            values[key] = metadata
            for field_name, (start, stop) in relative_spans.items():
                metadata_spans[field_name] = (
                    nested_start + start + 1,
                    nested_start + stop + 1,
                )
        else:
            values[key] = _parse_scalar(raw_value, nested, path, key)

    return MappingProxyType(values), MappingProxyType(metadata_spans)


def load_skill(path: Path) -> SkillRecord:
    """Parse and normalize a SKILL.md, enforcing its path-based identity."""
    fields = parse_frontmatter(path)
    skill_id = path.parent.name
    name = _required_string(fields, "name", path)
    if len(name) > 64 or not _NAME_PATTERN.fullmatch(name):
        raise SkillCatalogError(
            f"{path}: name must be at most 64 lowercase letters, numbers, and hyphens"
        )
    if name != skill_id:
        raise SkillCatalogError(
            f"{path}: frontmatter name {name!r} must match directory {skill_id!r}"
        )

    description = _required_string(fields, "description", path)
    title_value = fields.get("title")
    title = _optional_string(title_value, "title", path) or format_display_name(skill_id)
    if len(title) > 100:
        raise SkillCatalogError(f"{path}: title must be at most 100 characters")

    metadata_value = fields.get("metadata")
    if not isinstance(metadata_value, Mapping):
        raise SkillCatalogError(f"{path}: metadata mapping is required")
    author = _required_string(metadata_value, "author", path, prefix="metadata.")
    version = _required_string(metadata_value, "version", path, prefix="metadata.")
    SemVer.parse(version)

    summary_value = metadata_value.get("summary")
    explicit_summary = _optional_string(summary_value, "metadata.summary", path)
    if explicit_summary and len(explicit_summary) > 200:
        raise SkillCatalogError(f"{path}: metadata.summary must be at most 200 characters")
    summary = explicit_summary or description

    dimensions: dict[str, tuple[str, ...]] = {}
    for key, value in metadata_value.items():
        if not key.startswith(_DIMENSION_PREFIX):
            continue
        dimension_name = key.removeprefix(_DIMENSION_PREFIX)
        if dimension_name not in KNOWN_DIMENSIONS:
            raise SkillCatalogError(f"{path}: unknown catalog dimension {dimension_name!r}")
        dimension_value = _optional_string(value, f"metadata.{key}", path)
        if not dimension_value:
            raise SkillCatalogError(f"{path}: dimension {key!r} must not be empty")
        entries = tuple(part.strip() for part in dimension_value.split(","))
        if any(not entry for entry in entries):
            raise SkillCatalogError(f"{path}: dimension {key!r} contains an empty value")
        dimensions[dimension_name] = tuple(dict.fromkeys(entries))

    return SkillRecord(
        id=skill_id,
        name=name,
        title=title,
        summary=summary,
        description=description,
        author=author,
        version=version,
        dimensions=MappingProxyType(dimensions),
        source_path=path,
    )


def load_skills(skills_dir: Path) -> tuple[SkillRecord, ...]:
    """Load every discovered skill in stable path order."""
    return tuple(load_skill(path) for path in discover_skill_paths(skills_dir))


def _parse_metadata(
    lines: list[str], path: Path
) -> tuple[Mapping[str, str], Mapping[str, tuple[int, int]]]:
    metadata: dict[str, str] = {}
    spans: dict[str, tuple[int, int]] = {}
    significant_lines = [
        line for line in lines if line.strip() and not line.lstrip().startswith("#")
    ]
    if not significant_lines:
        return MappingProxyType(metadata), MappingProxyType(spans)
    indentation = min(len(line) - len(line.lstrip()) for line in significant_lines)

    index = 0
    while index < len(lines):
        line = lines[index]
        if not line.strip() or line.lstrip().startswith("#"):
            index += 1
            continue
        field_start = index
        line_indentation = len(line) - len(line.lstrip())
        if line_indentation != indentation:
            raise SkillCatalogError(f"{path}: malformed metadata indentation")
        match = _KEY_PATTERN.fullmatch(line[indentation:])
        if not match:
            raise SkillCatalogError(f"{path}: malformed metadata line {line!r}")
        key = match.group(1)
        if key in metadata:
            raise SkillCatalogError(f"{path}: duplicate metadata field {key!r}")
        raw_value = match.group(2) or ""
        index += 1

        continuation: list[str] = []
        while index < len(lines):
            candidate = lines[index]
            if candidate.strip() and not candidate.lstrip().startswith("#"):
                candidate_indentation = len(candidate) - len(candidate.lstrip())
                if candidate_indentation == indentation:
                    break
                if candidate_indentation < indentation:
                    raise SkillCatalogError(f"{path}: malformed metadata indentation")
            continuation.append(candidate)
            index += 1

        if not raw_value and not any(line.strip() for line in continuation):
            raise SkillCatalogError(f"{path}: metadata field {key!r} must be a scalar")
        metadata[key] = _parse_scalar(
            raw_value,
            continuation,
            path,
            f"metadata.{key}",
        )
        spans[key] = (field_start, index)
    return MappingProxyType(metadata), MappingProxyType(spans)


def _parse_scalar(
    raw_value: str,
    continuation: list[str],
    path: Path,
    key: str,
) -> str:
    marker = raw_value.strip()
    if re.fullmatch(r"[>|][+-]?\d?", marker):
        return _parse_block_scalar(marker[0], continuation, path, key)

    pieces = [marker]
    for line in continuation:
        value = line.strip()
        if not value:
            continue
        if value.startswith(("- ", "[", "{")) or _KEY_PATTERN.fullmatch(value):
            raise SkillCatalogError(f"{path}: {key} must be a string, not a nested value")
        pieces.append(value)
    return _decode_scalar(" ".join(pieces).strip(), path, key)


def _parse_block_scalar(style: str, lines: list[str], path: Path, key: str) -> str:
    nonempty = [line for line in lines if line.strip()]
    if not nonempty:
        return ""
    indentation = min(len(line) - len(line.lstrip()) for line in nonempty)
    content = [line[indentation:].rstrip() if line.strip() else "" for line in lines]
    if style == "|":
        return "\n".join(content).strip()

    paragraphs: list[str] = []
    current: list[str] = []
    for line in content:
        if line:
            current.append(line.strip())
        elif current:
            paragraphs.append(" ".join(current))
            current = []
    if current:
        paragraphs.append(" ".join(current))
    return "\n".join(paragraphs).strip()


def _decode_scalar(value: str, path: Path, key: str) -> str:
    if not value:
        return ""
    if value.startswith('"'):
        try:
            decoded = json.loads(value)
        except json.JSONDecodeError as error:
            raise SkillCatalogError(f"{path}: invalid quoted value for {key}") from error
        if not isinstance(decoded, str):
            raise SkillCatalogError(f"{path}: {key} must be a string")
        return decoded.strip()
    if value.startswith("'"):
        if len(value) < 2 or not value.endswith("'"):
            raise SkillCatalogError(f"{path}: invalid quoted value for {key}")
        return value[1:-1].replace("''", "'").strip()
    if value.endswith(('"', "'")):
        raise SkillCatalogError(f"{path}: invalid quoted value for {key}")
    if value.startswith(("[", "{")) or _TYPED_PLAIN_SCALAR_PATTERN.fullmatch(value):
        raise SkillCatalogError(f"{path}: {key} must be a string")
    if _KEY_PATTERN.fullmatch(value):
        raise SkillCatalogError(f"{path}: {key} must be a string, not a mapping")
    return value.strip()


def _required_string(
    values: Mapping[str, object],
    key: str,
    path: Path,
    prefix: str = "",
) -> str:
    value = _optional_string(values.get(key), prefix + key, path)
    if not value:
        raise SkillCatalogError(f"{path}: {prefix}{key} is required")
    return value


def _optional_string(value: object, key: str, path: Path) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise SkillCatalogError(f"{path}: {key} must be a string")
    return value.strip()
