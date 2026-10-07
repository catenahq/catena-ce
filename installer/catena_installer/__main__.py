"""`catena-installer`: install a Catena server from this machine, and reach
its panel.

    catena-installer                         the installer page, in a browser
    catena-installer --inventory clientco    the page, on that inventory
    catena-installer install --inventory clientco [-i FILE] [--release vX.Y.Z]
                                             install or re-apply, in the console
    catena-installer connect --inventory clientco
                                             forward the panel and Portainer here
    catena-installer init --inventory clientco
                                             write an inventory's .env to fill in
    catena-installer uninstall --inventory clientco
                                             hand OS updates back to Debian

The page and `install` run the same install (install.py): it reaches the
server over SSH, runs the install there with the release's own code, and
streams what the server prints back here.
"""

from __future__ import annotations

import argparse
import getpass
import os
import re
import sys
import tempfile
import threading
import webbrowser
from pathlib import Path

import yaml

from . import install, registry, remote, tree


def _inventory_dir(args: argparse.Namespace) -> Path:
    if args.inventory_path:
        return Path(args.inventory_path).expanduser()
    return tree.inventory_root() / args.inventory


def _add_inventory(parser: argparse.ArgumentParser, *, required: bool) -> None:
    group = parser.add_mutually_exclusive_group(required=required)
    group.add_argument("--inventory",
                       help="inventory name, a directory under the inventory root")
    group.add_argument("--inventory-path",
                       help="an inventory directory anywhere else")


def _address(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.:-]*", value):
        raise argparse.ArgumentTypeError(f"{value!r} is not an address")
    return value


def _add_target(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--address", type=_address,
                        help="reach the server at this address for this run, "
                             "such as its tailnet address once the panel's "
                             "Lockdown has closed public SSH")
    parser.add_argument("--reinstalled", action="store_true",
                        help="the server was reinstalled: trust the new host "
                             "key it presents")


def _seed(inv_dir: Path, args: argparse.Namespace) -> tuple[int, dict]:
    """seed.py writes the inventory's .env and hosts.yml from the input file
    or checks the existing .env, and returns the adopt map (an admin password
    the input file pins) through a 0600 file deleted here."""
    seed = tree.seed()
    fd, name = tempfile.mkstemp(prefix="catena-adopt-", suffix=".yml")
    os.close(fd)
    adopt_file = Path(name)
    argv = ["--inventory-path", str(inv_dir), "--secrets-out", str(adopt_file)]
    if args.input:
        argv += ["-i", args.input]
    if args.no_confirm:
        argv.append("--no-confirm")
    try:
        try:
            rc = seed.main(argv)
        except SystemExit as exc:
            rc = exc.code if isinstance(exc.code, int) else 1
        adopt = ((yaml.safe_load(adopt_file.read_text(encoding="utf-8")) or {})
                 if rc == 0 else {})
    finally:
        adopt_file.unlink(missing_ok=True)
    return rc, adopt


def _print(line: str) -> None:
    print(line, flush=True)


def _print_keyset(block: list[str]) -> None:
    print("\n".join((install.KEYSET_BEGIN, *block, install.KEYSET_END)), flush=True)


def cmd_install(args: argparse.Namespace) -> int:
    inv_dir = _inventory_dir(args)
    rc, adopt = _seed(inv_dir, args)
    if rc != 0:
        return rc
    password = ""
    if args.input:
        host = tree.seed().load_input(Path(args.input)).get("host") or {}
        password = str(host.get("initial_password") or "")
    opts = install.Options(
        release=args.release, image=args.image, ca_file=args.ca_file,
        insecure_http=args.insecure_http, address=args.address or "", tags=args.tags,
        reinstalled=args.reinstalled, keyset_json=args.keyset_json,
        password=password, adopt=adopt)
    out = install.Output(_print, _print_keyset)
    interactive = sys.stdin.isatty() and not args.no_confirm
    while True:
        try:
            return install.run(inv_dir, opts, out)
        except remote.HostKeyChanged as exc:
            _print(f"catena-installer: {exc}")
            if not interactive:
                _print("catena-installer: stopped. Run again with --reinstalled "
                       "if the server was reinstalled.")
                return 1
            if input("Was the server reinstalled? [y/N]: ").strip().lower() not in ("y", "yes"):
                return 1
            opts.reinstalled = True
        except install.PasswordNeeded as exc:
            _print(f"catena-installer: {exc}")
            if not interactive:
                return 1
            opts.password = getpass.getpass(
                "The provider's password for the server's initial login "
                "(blank to stop): ")
            if not opts.password:
                return 1


def cmd_connect(args: argparse.Namespace) -> int:
    try:
        forward = install.connect(_inventory_dir(args), args.address or "")
    except (remote.RemoteError, OSError) as exc:
        _print(f"catena-installer: {exc}")
        return 1
    _print(f"catena-admin  {forward.url(install.PANEL_PORT)}")
    _print(f"Portainer     {forward.url(install.PORTAINER_PORT)}")
    _print("catena-installer: forwarding until this window is closed (Ctrl-C)")
    if args.open:
        webbrowser.open(forward.url(install.PANEL_PORT))
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        pass
    forward.close()
    return 0


def cmd_init(args: argparse.Namespace) -> int:
    seed = tree.seed()
    inv_dir = _inventory_dir(args)
    if args.inventory_path:
        problem = "it already exists" if inv_dir.exists() else ""
    else:
        code = seed.inventory_name_problem(args.inventory, inv_dir.parent)
        problem = seed.INVENTORY_NAME_PROBLEMS[code] if code else ""
    if problem:
        _print(f"catena-installer: cannot create the inventory {inv_dir}: {problem}")
        return 1
    env = seed.write_env(inv_dir, {})
    _print(f"catena-installer: wrote {env}. Fill it in, then run "
           f"`catena-installer install --inventory-path {inv_dir}`.")
    return 0


def cmd_uninstall(args: argparse.Namespace) -> int:
    opts = install.Options(address=args.address or "", reinstalled=args.reinstalled)
    return install.uninstall(_inventory_dir(args), opts, _print)


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="catena-installer", description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--inventory", default="",
                    help="the page: open this inventory, or create it")
    ap.add_argument("--port", type=int, default=8765,
                    help="the page's loopback port")
    ap.add_argument("--no-browser", action="store_true",
                    help="serve the page without opening a browser")
    sub = ap.add_subparsers(dest="command")

    p = sub.add_parser("install", help="install or re-apply a server, in the console")
    _add_inventory(p, required=True)
    p.add_argument("-i", "--input",
                   help="a new server's answers: the .env keys, "
                        "host_initial_password, host_name, an admin_password pin")
    which = p.add_mutually_exclusive_group()
    which.add_argument("--release", default="",
                       help="move the server to this release (vX.Y.Z)")
    which.add_argument("--image", default="",
                       help="install from this catena-admin image reference")
    p.add_argument("--ca-file", default="",
                   help="a CA to trust for the image's registry")
    p.add_argument("--insecure-http", action="store_true",
                   help="reach the image's registry over plain http")
    p.add_argument("--tags", default="",
                   help="scope the converge to these roles (comma-separated)")
    p.add_argument("--keyset-json", action="store_true",
                   help="print the passwords as one JSON object per server")
    p.add_argument("--no-confirm", action="store_true",
                   help="ask nothing: no confirmation, no password, no reinstall question")
    _add_target(p)
    p.set_defaults(func=cmd_install)

    p = sub.add_parser("connect", help="forward the panel and Portainer to this machine")
    _add_inventory(p, required=True)
    p.add_argument("--address", type=_address)
    p.add_argument("--open", action="store_true", help="open the panel in a browser")
    p.set_defaults(func=cmd_connect)

    p = sub.add_parser("init", help="write an inventory's .env, every key at its default")
    _add_inventory(p, required=True)
    p.set_defaults(func=cmd_init)

    p = sub.add_parser("uninstall", help="hand the server's OS updates back to Debian")
    _add_inventory(p, required=True)
    _add_target(p)
    p.set_defaults(func=cmd_uninstall)
    return ap


def _page(args: argparse.Namespace) -> int:
    from . import run as run_mod
    from .server import serve

    doc = registry.load()
    current = None
    if args.inventory:
        seed = tree.seed()
        root = tree.inventory_root()
        if args.inventory not in seed.inventories(root):
            code = seed.inventory_name_problem(args.inventory, root)
            if code:
                raise SystemExit(f"--inventory {args.inventory!r}: "
                                 f"{seed.INVENTORY_NAME_PROBLEMS[code]}")
        current = run_mod.load(root / args.inventory, run_mod.secret_keys_from(doc))
        if not current.path.is_dir():
            current.save()
    url = f"http://127.0.0.1:{args.port}/"
    print(f"catena-installer: serving {url}", file=sys.stderr)
    print("catena-installer: closing the browser changes nothing; closing THIS "
          "window stops the install and the panel forward.", file=sys.stderr)
    if not args.no_browser:
        webbrowser.open(url)
    return serve(current, doc, port=args.port, inventory_root=tree.inventory_root())


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command is None:
            return _page(args)
        return args.func(args)
    except KeyboardInterrupt:
        print("\ncatena-installer: interrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
