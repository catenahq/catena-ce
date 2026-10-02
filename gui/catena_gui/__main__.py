"""`catena-gui`: the console process that owns an install.

    catena-gui                          open the installer in a browser; its
                                        first page opens or creates an
                                        inventory under ansible/inventory/
    catena-gui --inventory clientco     open (or create) that inventory first
    catena-gui --inventory clientco --answers answers.yaml --no-browser
                                        validate and install with no UI

THE THIRD MODE checks the same sections, runs the same probes and produces the same
install.yaml, so a green there is a shape a client can reach -- a
non-interactive path that skipped the validation would prove an install nobody
could repeat through the UI.

CLOSING THE BROWSER CHANGES NOTHING. This process owns the job. Closing IT
abandons the run, and the inventory keeps what was answered -- which matters
because an install can start with a wait nobody can time: a server being
delivered.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import webbrowser
from pathlib import Path

import yaml

from . import registry, render, run as run_mod, steps as steps_mod


def _load_answers_file(path: Path, run: run_mod.Run, doc: dict) -> None:
    """Fill a run from a YAML answers file.

    Each value goes to the side of the line the REGISTRY puts it on, not the
    side this file guesses, and the provider's password to the secret side.
    That is the same classification seed applies when it splits a flat
    install.yaml, so a credential cannot be filed as config by one and as a
    secret by the other. The inventory is the run's directory, so an
    `inventory:` key in the file is not an answer.
    """
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise SystemExit(f"{path}: expected a mapping of answers")
    for key, value in raw.items():
        if key == "inventory":
            continue
        run.answer(str(key), "" if value is None else str(value),
                   secret=(registry.is_secret(doc, str(key))
                           or str(key) == steps_mod.PROVIDER_PASSWORD))


def _report(checks: list[steps_mod.Check]) -> None:
    for check in checks:
        mark = "+" if check.ok else ("x" if check.blocking else "!")
        suffix = f" -- {check.detail}" if check.detail else ""
        print(f"  {mark} {check.label}{suffix}", file=sys.stderr)


def walk(run: run_mod.Run, doc: dict) -> int:
    """Validate every step in order. Returns the number of blocking failures.

    IN ORDER, and it does not stop at the first. A client fixing one wrong
    answer wants to know about the other two before they try again, and a
    launcher that reported them one install at a time would spend three
    sittings saying what it could have said in one.
    """
    problems = 0
    built = steps_mod.build(doc)
    values = {f.key: (run.value(f.key) or f.default) for s in built for f in s.fields}
    for step in built:
        print(f"\n== {step.title}", file=sys.stderr)
        missing = steps_mod.missing_required(step, values)
        checks = missing or steps_mod.validate(step.name, run.probed(), run.secrets)
        _report(checks)
        problems += sum(1 for check in checks if check.blocks)
    return problems


def install(run: run_mod.Run, ansible_dir: Path) -> int:
    """Write the contract, run it, and record where it got to.

    The install.yaml is removed whatever happens. It can carry the provider's
    password, and a file that outlives the install is one nothing ever comes
    back to delete.

    No stdin, as on the page: the answers are the whole input, and a question
    the install would have asked takes its default.
    """
    run.state = run_mod.STATE_INSTALLING
    run.save()
    body = render.install_yaml(inventory=run.inventory, answers=run.answers,
                               secrets=run.secrets)
    with render.transient_install_yaml(body) as target:
        argv = render.install_command(ansible_dir, target, run.inventory)
        print(f"\n== install: {' '.join(argv)}", file=sys.stderr)
        rc = subprocess.run(argv, stdin=subprocess.DEVNULL, check=False).returncode
    run.state = run_mod.STATE_DONE if rc == 0 else run_mod.STATE_FAILED
    run.save()
    return rc


def _open(name: str, doc: dict) -> run_mod.Run:
    """The inventory `name` as a run: opened when it exists, created when the
    name is a valid new one."""
    if name not in run_mod.inventories():
        problem = run_mod.new_name_problem(name)
        if problem:
            raise SystemExit(f"--inventory {name!r}: {problem}")
    return run_mod.load(run_mod.inventory_root() / name,
                        run_mod.secret_keys_from(doc))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--inventory", default="",
                    help="inventory under ansible/inventory/ to open, or to "
                         "create when it does not exist yet")
    ap.add_argument("--answers",
                    help="a YAML file of answers, for a run with no UI")
    ap.add_argument("--no-browser", action="store_true",
                    help="do not open a browser. With --answers this is the "
                         "whole non-interactive path.")
    ap.add_argument("--validate-only", action="store_true",
                    help="run every step's checks and stop, installing nothing")
    ap.add_argument("--port", type=int, default=8765,
                    help="loopback port for the browser UI")
    args = ap.parse_args(argv)

    doc = registry.load()
    current = _open(args.inventory, doc) if args.inventory else None
    if args.answers:
        if current is None:
            raise SystemExit("--inventory is required with --answers: it names "
                             "the directory the answers are saved into")
        _load_answers_file(Path(args.answers).expanduser(), current, doc)
    # An existing inventory is written when something was answered, never just
    # for being opened: the `.env` is a file its client may have edited.
    if current is not None and (args.answers or not current.path.is_dir()):
        current.save()

    if not args.answers and not args.no_browser:
        from .server import serve

        url = f"http://127.0.0.1:{args.port}/"
        print(f"catena-gui: serving {url}", file=sys.stderr)
        print("catena-gui: closing the browser changes nothing; closing THIS "
              "window abandons the run.", file=sys.stderr)
        webbrowser.open(url)
        return serve(current, doc, port=args.port, ansible_dir=registry.ANSIBLE_DIR)

    if current is None:
        raise SystemExit("--inventory is required for a run with no UI: it "
                         "names the directory the install writes into")

    problems = walk(current, doc)
    if problems:
        print(f"\ncatena-gui: {problems} blocking problem(s); nothing was "
              "installed.", file=sys.stderr)
        return 1
    if args.validate_only:
        print("\ncatena-gui: every step checks out.", file=sys.stderr)
        return 0
    return install(current, registry.ANSIBLE_DIR)


if __name__ == "__main__":
    raise SystemExit(main())
