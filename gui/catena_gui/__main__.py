"""`catena-gui`: the console process that owns an install.

    catena-gui                          open the wizard in a browser
    catena-gui --run-dir ~/.catena/run1 resume that run
    catena-gui --answers answers.yaml --no-browser
                                        validate and install with no UI

THE THIRD MODE IS WHAT THE BENCH DRIVES, and it is not a shortcut around the
wizard: it walks the same steps, runs the same probes and produces the same
install.yaml. A non-interactive path that skipped the validation would let the
bench prove an install shape no client can reach.

CLOSING THE BROWSER CHANGES NOTHING. This process owns the job. Closing IT
abandons the run, and the run directory says how to resume -- which matters
because an install contains two waits nobody can time: a server being
delivered, and a domain being activated by its registrar.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import webbrowser
from pathlib import Path

import yaml

from . import registry, render, run as run_mod, steps as steps_mod

DEFAULT_RUN_DIR = Path.home() / ".catena" / "gui"


def _load_answers_file(path: Path, run: run_mod.Run, doc: dict) -> None:
    """Fill a run from a YAML answers file.

    Each value goes to the side of the line the REGISTRY puts it on, not the
    side this file guesses. That is the same classification seed applies when
    it splits a flat install.yaml, so a credential cannot be filed as config by
    one and as a secret by the other.
    """
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise SystemExit(f"{path}: expected a mapping of answers")
    for key, value in raw.items():
        if key == "inventory":
            run.inventory = str(value)
            continue
        run.answer(str(key), "" if value is None else str(value),
                   secret=registry.is_secret(doc, str(key)))


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
    for step in steps_mod.build(doc):
        print(f"\n== {step.title}", file=sys.stderr)
        checks = steps_mod.validate(step.name, run.answers, run.secrets)
        _report(checks)
        problems += sum(1 for check in checks if check.blocks)
    return problems


def install(run: run_mod.Run, doc: dict, ansible_dir: Path) -> int:
    """Write the contract, run it, and record where it got to.

    The install.yaml is removed whatever happens. It carries the client's cloud
    credentials, and a file that outlives the install is one nothing ever comes
    back to delete.
    """
    target = run.path / "install.yaml"
    render.write_install_yaml(target, render.install_yaml(
        inventory=run.inventory, answers=run.answers, secrets=run.secrets))
    run.state = run_mod.STATE_INSTALLING
    run.save()
    argv = render.install_command(ansible_dir, target, run.inventory)
    print(f"\n== install: {' '.join(argv)}", file=sys.stderr)
    try:
        rc = subprocess.run(argv, check=False).returncode
    finally:
        target.unlink(missing_ok=True)
    run.state = run_mod.STATE_DONE if rc == 0 else run_mod.STATE_FAILED
    run.save()
    return rc


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--run-dir", default=str(DEFAULT_RUN_DIR),
                    help="where this run's answers live. Resuming means "
                         "pointing at the same directory again.")
    ap.add_argument("--inventory", default="",
                    help="inventory name to create under ansible/inventory/")
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
    current = run_mod.load(Path(args.run_dir).expanduser(),
                           run_mod.secret_keys_from(doc))
    if args.inventory:
        current.inventory = args.inventory
    if args.answers:
        _load_answers_file(Path(args.answers).expanduser(), current, doc)
    current.save()

    if not args.answers and not args.no_browser:
        from .server import serve

        url = f"http://127.0.0.1:{args.port}/"
        print(f"catena-gui: serving {url}", file=sys.stderr)
        print("catena-gui: closing the browser changes nothing; closing THIS "
              "window abandons the run.", file=sys.stderr)
        webbrowser.open(url)
        return serve(current, doc, port=args.port, ansible_dir=registry.ANSIBLE_DIR)

    if not current.inventory:
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
    return install(current, doc, registry.ANSIBLE_DIR)


if __name__ == "__main__":
    raise SystemExit(main())
