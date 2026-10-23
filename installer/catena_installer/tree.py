"""Where the installer finds the catena-ce files it uses on this machine, and
where it keeps inventories.

The installer carries a few files of the Ansible tree: seed.py and the knob
registry, which write and read an inventory's `.env`, and the helpers it sends
to the server before the release is there (fetch_release.py and what it
imports). A wheel carries them under catena_installer/tree; a catena-ce
checkout has them in ansible/.
"""

from __future__ import annotations

import importlib
import os
import sys
from pathlib import Path

PACKAGE = Path(__file__).resolve().parent
_BUNDLED = PACKAGE / "tree"
_CHECKOUT = PACKAGE.parents[1] / "ansible"
TREE = _BUNDLED if (_BUNDLED / "seed.py").is_file() else _CHECKOUT

STARTER = PACKAGE / "starter.sh"

# Sent to the server with the starter on the leg that fetches the release,
# side by side: the fetcher and the modules it imports from beside it.
FETCHER = ("helpers/fetch_release.py", "helpers/catena_admin_release.py",
           "playbooks/filter_plugins/image_pin.py")


def module(name: str):
    """A module of the tree (seed, helpers.render_knobs, ...), imported from
    there."""
    root = str(TREE)
    if root not in sys.path:
        sys.path.insert(0, root)
    return importlib.import_module(name)


def seed():
    """seed.py: the one reader and writer of an inventory's `.env`."""
    return module("seed")


def inventory_root() -> Path:
    """CATENA_INVENTORY_ROOT, else ansible/inventory in a checkout, else the
    installer's directory in this user's application data."""
    configured = os.environ.get("CATENA_INVENTORY_ROOT", "").strip()
    if configured:
        return Path(configured).expanduser()
    if TREE == _CHECKOUT:
        return _CHECKOUT / "inventory"
    return _data_dir() / "inventory"


def _data_dir() -> Path:
    if sys.platform == "win32":
        return Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming") / "catena"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "catena"
    base = os.environ.get("XDG_DATA_HOME", "").strip()
    return (Path(base) if base else Path.home() / ".local" / "share") / "catena"
