"""Helpers for the Realizer unit tests that compare a moved function or a whole run with the frozen oracle."""
import copy
import json
import sys
import tempfile
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence

ROOT = Path(__file__).resolve().parents[3]
for _path in (ROOT, ROOT / "src"):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))
from tests.regression.realizer import canonical, oracle  # noqa: E402

canonical.modular()  # src/realizer, never a shadowing copy
from realizer import realizer as engine  # noqa: E402

oengine = oracle.engine()


class Opaque:
    """A value no code can serialize or interpret; unlike ``object()`` two instances compare equal and print alike, so a
    result that merely carries it can be compared between the two engines."""
    def __eq__(self, other):
        return isinstance(other, Opaque)

    def __hash__(self):
        return 0

    def __repr__(self):
        return "Opaque()"


def outcome(fn: Callable[..., Any], *args: Any, **kwargs: Any):
    """``("ok", value)``, or ``(exception type name, message)`` when *fn* raises."""
    try:
        return "ok", fn(*args, **kwargs)
    except Exception as exc:  # noqa: BLE001 - compared, not handled
        return type(exc).__name__, str(exc)


def fixture_package() -> Dict[str, Any]:
    return json.loads(next(canonical.PACKAGES.glob("PKG-*.json")).read_text(encoding="utf-8"))


def tree(out: Path) -> Dict[str, Optional[str]]:
    """Every file (text) and directory (``None``) under *out*, relative: what a run, finished or crashed, left behind."""
    return {p.relative_to(out).as_posix(): (p.read_text(encoding="utf-8") if p.is_file() else None) for p in sorted(out.rglob("*"))}


def run_sequence(module: Any, package: Mapping[str, Any], observations: Sequence[Optional[Mapping[str, Any]]], policy: Any = None):
    """Run each observation through *module*'s ``realize`` into one fresh directory, continuing past a failure.

    Returns ``(outcomes, tree)``: per observation ``("ok", snapshot id)`` or ``(exception type, message)``, and the
    files and directories left behind (so a crash is compared together with the ledger it left half written).
    """
    with tempfile.TemporaryDirectory(prefix="parity-") as tmp:
        out = Path(tmp)
        outcomes: List[Any] = []
        for observation in observations:
            kwargs = {} if policy is None else {"policy": module.RealizerPolicy.from_mapping(policy)}
            result = outcome(module.realize, copy.deepcopy(package), copy.deepcopy(observation), out, **kwargs)
            outcomes.append(("ok", result[1].snapshot_id) if result[0] == "ok" else result)
        return outcomes, tree(out)
