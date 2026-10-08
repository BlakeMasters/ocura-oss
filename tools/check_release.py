# SPDX-License-Identifier: MPL-2.0

"""Check that a release's version, changelog, documentation, tag, and built files agree.

Run from a repository checkout:

    python tools/check_release.py
    python tools/check_release.py --tag v0.4.0 --dist dist
    python tools/check_release.py --notes
"""

from __future__ import annotations

import argparse
import email
import email.message
import re
import sys
import tarfile
import tomllib
import zipfile
from collections.abc import Sequence
from pathlib import Path

WHEEL_BUDGET_BYTES = 64 * 1024
UNRELEASED = "Unreleased"

_VERSION_LINE = re.compile(r'^__version__ = "([^"]+)"$', re.MULTILINE)
_CHANGELOG_HEADING = re.compile(r"^## (\S+) - (\S+)$", re.MULTILINE)
_DOCUMENT_VERSION = re.compile(r"^Version (\S+)\.$", re.MULTILINE)
_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")


class ReleaseError(Exception):
    """The checkout is missing something every other check depends on."""


def source_version(repo: Path) -> str:
    """Return the one declared package version."""
    path = repo / "src" / "ocura_oss" / "__init__.py"
    match = _VERSION_LINE.search(path.read_text(encoding="utf-8"))
    if match is None:
        raise ReleaseError("src/ocura_oss/__init__.py does not assign __version__")
    return match.group(1)


def changelog_entry(repo: Path) -> tuple[str, str, str]:
    """Return the newest changelog entry as ``(version, date, notes)``."""
    text = (repo / "CHANGELOG.md").read_text(encoding="utf-8")
    headings = list(_CHANGELOG_HEADING.finditer(text))
    if not headings:
        raise ReleaseError("CHANGELOG.md has no '## VERSION - DATE' heading")
    newest = headings[0]
    end = headings[1].start() if len(headings) > 1 else len(text)
    return newest.group(1), newest.group(2), text[newest.end() : end].strip()


def check_sources(repo: Path, *, tag: str | None = None) -> list[str]:
    """Compare the declared version with packaging metadata, changelog, and documents.

    Passing *tag* applies the stricter rules for a release: the tag must name
    the declared version and the changelog entry must carry its release date.
    """
    problems: list[str] = []
    version = source_version(repo)

    project = tomllib.loads((repo / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    if "version" in project or "version" not in project.get("dynamic", ()):
        problems.append("pyproject.toml must take its version from ocura_oss.__version__")
    if project.get("dependencies"):
        problems.append("pyproject.toml declares runtime dependencies")

    entry_version, entry_date, notes = changelog_entry(repo)
    if entry_version != version:
        problems.append(
            f"CHANGELOG.md starts at {entry_version}, but the package version is {version}"
        )
    if entry_date != UNRELEASED and _DATE.fullmatch(entry_date) is None:
        problems.append(f"CHANGELOG.md date {entry_date!r} is neither YYYY-MM-DD nor {UNRELEASED}")
    if not notes:
        problems.append(f"CHANGELOG.md has no notes under {entry_version}")

    for path, required in _documents(repo):
        relative = path.relative_to(repo).as_posix()
        stated = _DOCUMENT_VERSION.findall(path.read_text(encoding="utf-8"))
        if not stated and required:
            problems.append(f"{relative} has no 'Version X.Y.Z.' line")
        for found in stated:
            if found != version:
                problems.append(
                    f"{relative} states version {found}, but the package version is {version}"
                )

    if tag is not None:
        if tag != f"v{version}":
            problems.append(f"tag {tag} does not match package version {version}")
        if entry_date == UNRELEASED:
            problems.append(f"CHANGELOG.md still marks {entry_version} as {UNRELEASED}")
    return problems


def check_distributions(dist: Path, version: str) -> list[str]:
    """Check the built wheel and source distribution for one version."""
    wheel = dist / f"ocura_oss-{version}-py3-none-any.whl"
    sdist = dist / f"ocura_oss-{version}.tar.gz"
    found = sorted(item.name for item in dist.iterdir()) if dist.is_dir() else []
    if found != sorted((wheel.name, sdist.name)):
        return [
            f"{dist} must contain exactly {wheel.name} and {sdist.name};"
            f" found: {', '.join(found) or 'nothing'}"
        ]

    problems: list[str] = []
    size = wheel.stat().st_size
    if size > WHEEL_BUDGET_BYTES:
        problems.append(f"wheel is {size} bytes; the budget is {WHEEL_BUDGET_BYTES}")
    info = f"ocura_oss-{version}.dist-info/"
    with zipfile.ZipFile(wheel) as archive:
        members = archive.namelist()
        metadata = email.message_from_bytes(archive.read(f"{info}METADATA"))
    unexpected = [name for name in members if not name.startswith(("ocura_oss/", info))]
    if unexpected:
        problems.append(f"wheel contains unexpected files: {', '.join(unexpected)}")
    problems += _metadata_problems("wheel", metadata, version)

    # The archive's name is not evidence of what it installs: pip reads PKG-INFO.
    package_info = f"ocura_oss-{version}/PKG-INFO"
    with tarfile.open(sdist) as source:
        names = source.getnames()
        member = source.extractfile(package_info) if package_info in names else None
        source_metadata = None if member is None else email.message_from_bytes(member.read())
    if source_metadata is None:
        problems.append(f"source distribution has no {package_info}")
    else:
        problems += _metadata_problems("source distribution", source_metadata, version)
    if any(name.startswith(f"ocura_oss-{version}/examples/") for name in names):
        problems.append("source distribution includes repository-only examples")
    return problems


def _metadata_problems(label: str, metadata: email.message.Message, version: str) -> list[str]:
    """Check one distribution's core metadata for the version and for dependencies."""
    problems: list[str] = []
    if metadata["Version"] != version:
        problems.append(
            f"{label} metadata states version {metadata['Version']}, expected {version}"
        )
    if metadata.get_all("Requires-Dist"):
        problems.append(f"{label} metadata declares runtime dependencies")
    return problems


def _documents(repo: Path) -> list[tuple[Path, bool]]:
    """Return ``(path, version_line_required)`` for each versioned document."""
    references = [(path, True) for path in sorted((repo / "docs").glob("*.md"))]
    guides = [(path, False) for path in sorted((repo / "examples").glob("*/README.md"))]
    return references + guides


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--repo",
        type=Path,
        default=Path(__file__).resolve().parents[1],
        help="repository checkout (default: the checkout containing this script)",
    )
    parser.add_argument("--tag", help="release tag that must match, such as v0.4.0")
    parser.add_argument("--dist", type=Path, help="directory holding the built wheel and sdist")
    parser.add_argument(
        "--notes",
        action="store_true",
        help="print the newest changelog entry's notes and exit",
    )
    args = parser.parse_args(argv)

    try:
        if args.notes:
            print(changelog_entry(args.repo)[2])
            return 0
        version = source_version(args.repo)
        problems = check_sources(args.repo, tag=args.tag)
        if args.dist is not None:
            problems += check_distributions(args.dist, version)
    except (
        ReleaseError,
        OSError,
        KeyError,
        tomllib.TOMLDecodeError,
        zipfile.BadZipFile,
        tarfile.TarError,
    ) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    for problem in problems:
        print(f"problem: {problem}", file=sys.stderr)
    if problems:
        return 1
    print(f"release check passed: ocura-oss {version}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
