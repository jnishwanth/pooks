"""The ADR convention, enforced rather than remembered.

`docs/adr/README.md` states that any change altering an architectural decision
must add or supersede an ADR. The judgement half of that rule cannot be checked
here — nothing can tell whether a diff *should* have carried one. The mechanical
half can, and it is the half that rots first: a number reused, an index entry
never added, a status typo that makes a superseded record look live.

These parse the documents into a model and assert on its meaning. The files are
the deliverable here, not a proxy for behaviour elsewhere — an ADR set that has
drifted out of its own index is broken as an ADR set.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import pytest

ADR_DIR = Path(__file__).resolve().parents[1] / "docs" / "adr"
INDEX = ADR_DIR / "README.md"

FILENAME = re.compile(r"^(\d{4})-[a-z0-9]+(?:-[a-z0-9]+)*\.md$")
TITLE = re.compile(r"^# (\d+)\. (.+)$")
STATUS = re.compile(r"^Status: (proposed|accepted|rejected|superseded by ADR (\d+))$")
REQUIRED_SECTIONS = ("## Context", "## Decision", "## Consequences")


@dataclass(frozen=True)
class Adr:
    path: Path
    number: int
    title: str
    status: str
    superseded_by: int | None
    body: str


def _parse(path: Path) -> Adr:
    lines = path.read_text().split("\n")
    title_match = TITLE.match(lines[0])
    if title_match is None:
        pytest.fail(f"{path.name}: first line must be '# <n>. <title>', got {lines[0]!r}")

    status_line = next((line for line in lines if line.startswith("Status:")), "")
    status_match = STATUS.match(status_line)
    if status_match is None:
        pytest.fail(f"{path.name}: bad or missing status line, got {status_line!r}")

    return Adr(
        path=path,
        number=int(title_match.group(1)),
        title=title_match.group(2),
        status=status_match.group(1),
        superseded_by=int(status_match.group(2)) if status_match.group(2) else None,
        body=path.read_text(),
    )


def _adrs() -> list[Adr]:
    return sorted(
        (_parse(p) for p in ADR_DIR.glob("*.md") if p.name != "README.md"),
        key=lambda adr: adr.number,
    )


def test_there_are_adrs_to_check() -> None:
    """Guards every other test here: a glob that matches nothing passes them
    all vacuously, so a directory renamed out from under this file would look
    like a clean run."""
    assert len(_adrs()) >= 1


@pytest.mark.parametrize("path", sorted(ADR_DIR.glob("*.md")), ids=lambda p: p.name)
def test_filename_is_a_numbered_slug(path: Path) -> None:
    if path.name == "README.md":
        pytest.skip("the index, not a record")
    assert FILENAME.match(path.name), "expected NNNN-kebab-case-slug.md"


def test_numbering_is_sequential_from_one() -> None:
    """A gap means a record was deleted rather than superseded, which loses the
    reasoning that applied at the time; a duplicate makes 'ADR 7' ambiguous in
    every place that cites one."""
    numbers = [adr.number for adr in _adrs()]
    assert numbers == list(range(1, len(numbers) + 1))


@pytest.mark.parametrize("adr", _adrs(), ids=lambda a: a.path.name)
def test_number_matches_the_filename(adr: Adr) -> None:
    assert adr.path.name.startswith(f"{adr.number:04d}-")


@pytest.mark.parametrize("adr", _adrs(), ids=lambda a: a.path.name)
def test_has_every_required_section(adr: Adr) -> None:
    missing = [section for section in REQUIRED_SECTIONS if section not in adr.body]
    assert not missing, f"{adr.path.name} is missing {missing}"


@pytest.mark.parametrize("adr", _adrs(), ids=lambda a: a.path.name)
def test_a_superseded_record_points_at_a_real_successor(adr: Adr) -> None:
    """'superseded' with nothing to follow to is worse than no status at all —
    the reader cannot find the decision that replaced it."""
    if adr.superseded_by is None:
        return
    assert adr.superseded_by != adr.number, "an ADR cannot supersede itself"
    assert adr.superseded_by in {other.number for other in _adrs()}


def test_every_record_appears_in_the_index() -> None:
    """The index is how anyone finds these, so a record missing from it is a
    decision nobody will read."""
    index = INDEX.read_text()
    for adr in _adrs():
        assert f"({adr.path.name})" in index, f"{adr.path.name} is not linked from the index"


def test_the_index_lists_no_record_that_is_gone() -> None:
    linked = set(re.findall(r"\((\d{4}-[a-z0-9-]+\.md)\)", INDEX.read_text()))
    assert linked == {adr.path.name for adr in _adrs()}


@pytest.mark.parametrize("adr", _adrs(), ids=lambda a: a.path.name)
def test_the_index_reports_the_records_own_status(adr: Adr) -> None:
    """Two copies of a status is one too many, and the stale one is always the
    index. Pinned so superseding a record cannot silently leave it listed as
    accepted."""
    row = next(line for line in INDEX.read_text().split("\n") if f"({adr.path.name})" in line)
    assert adr.status in row, f"index row for {adr.path.name} disagrees with the record"
