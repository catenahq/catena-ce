"""Console-script shim for the `catena` CLI.

The real implementation is the extensionless `catena` script next to this
file. It stays extensionless because the test bench and the docs invoke it as
`./catena`; this module lets `[project.scripts]` expose the same code as a
bare `catena` command so `uv run catena install` works from ansible/.
"""
from __future__ import annotations

from importlib.machinery import SourceFileLoader
from importlib.util import module_from_spec, spec_from_loader
from pathlib import Path

_path = Path(__file__).resolve().parent / "catena"
_loader = SourceFileLoader("catena_impl", str(_path))
_spec = spec_from_loader("catena_impl", _loader)
_impl = module_from_spec(_spec)
_loader.exec_module(_impl)


def _entry(argv: list[str] | None = None) -> int:
    return _impl._entry(argv)
