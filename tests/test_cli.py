"""Argument parsing and dispatch.

Nothing else in the suite imports `pooks.cli`, so a command wired to nothing —
or to the same handler twice — would only surface when someone ran it. The
parser is cheap to build and touches no database, which makes that checkable
here.
"""

from __future__ import annotations

import argparse
import inspect

import pytest

from pooks.cli import build_parser


def _command_names() -> list[str]:
    """Every registered subcommand.

    Read off the parser rather than listed again here: a second list would keep
    passing while a newly added command went unchecked. argparse exposes no
    public way to enumerate subcommands, hence `_actions`.
    """
    parser = build_parser()
    action = next(
        a for a in parser._actions if isinstance(a, argparse._SubParsersAction)
    )
    return list(action.choices)


def test_every_command_dispatches_to_a_distinct_coroutine() -> None:
    handlers = []
    for name in _command_names():
        args = build_parser().parse_args([name])
        assert inspect.iscoroutinefunction(args.handler), name
        handlers.append(args.handler)

    assert len(set(handlers)) == len(handlers)


def test_unknown_command_exits() -> None:
    with pytest.raises(SystemExit):
        build_parser().parse_args(["definitely-not-a-command"])


def test_missing_command_exits() -> None:
    with pytest.raises(SystemExit):
        build_parser().parse_args([])
