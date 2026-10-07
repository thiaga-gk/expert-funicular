"""Realizer errors: a refused run, as opposed to a bug.

``RealizerError`` is a run the Realizer refuses for a reason the operator can fix (a package or observation it does not
read, an unreadable input file, a policy file it does not accept, a ledger it cannot read). The CLI prints its message
and exits with status 2; any other exception is a bug and keeps its traceback. Messages name files, fields and ids,
never input values such as costs or configuration.
"""
from __future__ import annotations


class RealizerError(Exception):
    """A run the Realizer refuses (CLI exit status 2)."""


class InputError(RealizerError, ValueError):
    """An input the Realizer does not accept: the package, the observation, the policy file, a replay cut, a file."""


class LedgerError(RealizerError):
    """A realization ledger that cannot be read as the Realizer wrote it."""
