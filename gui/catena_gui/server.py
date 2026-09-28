"""The browser UI, on loopback, serving the run this process owns.

LOOPBACK ONLY, and bound explicitly to 127.0.0.1 rather than left to a default.
The pages carry a client's cloud credentials in form fields; a server that
bound every interface would put them on whatever network the machine happens to
be on, including a cafe's.

THE BROWSER IS A VIEW. Every answer goes straight into the Run this process
holds, and the state that matters is in the run directory. Closing the tab
loses nothing; reopening it shows the same page.

STDLIB, DELIBERATELY. A wizard on loopback needs routing, forms and HTML and
nothing a framework adds beyond that -- and every dependency here is one a
client installs before they can install anything else.
"""

from __future__ import annotations

import html
import http.server
import subprocess
import sys
import threading
import urllib.parse
from pathlib import Path

from . import render, run as run_mod, steps as steps_mod

_STYLE = """
:root { color-scheme: light dark; --fg: #1a1a1a; --bg: #fdfdfc; --muted: #666;
        --line: #d8d8d4; --bad: #a11; --warn: #a60; --ok: #161; }
@media (prefers-color-scheme: dark) {
  :root { --fg: #e8e8e6; --bg: #17181a; --muted: #9a9a96; --line: #34363a;
          --bad: #f77; --warn: #db4; --ok: #6c6; }
}
* { box-sizing: border-box; }
body { margin: 0; font: 16px/1.6 system-ui, sans-serif; color: var(--fg);
       background: var(--bg); }
main { max-width: 42rem; margin: 0 auto; padding: 2rem 1rem 4rem; }
nav { display: flex; gap: .5rem; flex-wrap: wrap; border-bottom: 1px solid var(--line);
      padding: .75rem 1rem; }
nav a, nav span { padding: .25rem .6rem; border-radius: 4px; text-decoration: none;
                  color: var(--muted); }
nav a.on { color: var(--fg); font-weight: 600; background: rgba(128,128,128,.14); }
nav span.off { opacity: .45; cursor: not-allowed; }
h1 { font-size: 1.4rem; margin: 1.5rem 0 .25rem; }
p.doc { color: var(--muted); white-space: pre-wrap; }
label { display: block; margin: 1.1rem 0 0; }
label .k { font-family: ui-monospace, monospace; font-size: .85rem; }
label .d { display: block; color: var(--muted); font-size: .85rem; }
input, select { width: 100%; padding: .45rem .5rem; margin-top: .3rem;
                border: 1px solid var(--line); border-radius: 4px;
                background: var(--bg); color: var(--fg); font: inherit; }
button { margin-top: 1.5rem; padding: .5rem 1.1rem; font: inherit;
         border: 1px solid var(--line); border-radius: 4px; cursor: pointer;
         background: rgba(128,128,128,.12); color: var(--fg); }
ul.checks { list-style: none; padding: 0; }
ul.checks li { padding: .2rem 0; }
.bad { color: var(--bad); } .warn { color: var(--warn); } .ok { color: var(--ok); }
pre { white-space: pre-wrap; border: 1px solid var(--line); border-radius: 4px;
      padding: .75rem; overflow-x: auto; }
"""


def _page(title: str, body: str) -> bytes:
    return (
        "<!doctype html><html lang=en><head><meta charset=utf-8>"
        "<meta name=viewport content='width=device-width,initial-scale=1'>"
        f"<title>{html.escape(title)} -- Catena installer</title>"
        f"<style>{_STYLE}</style></head><body>{body}</body></html>"
    ).encode("utf-8")


def _nav(all_steps: list[steps_mod.Step], current: str, inventory: str) -> str:
    cls = " class=on" if current == "" else ""
    label = f"Inventory: {inventory}" if inventory else "Inventory"
    out = ["<nav>", f'<a href="/inventory"{cls}>{html.escape(label)}</a>']
    for step in all_steps:
        if not inventory:
            out.append(f"<span class=off>{html.escape(step.title)}</span>")
            continue
        cls = " class=on" if step.name == current else ""
        out.append(f'<a href="/step/{step.name}"{cls}>{html.escape(step.title)}</a>')
    out.append("</nav>")
    return "".join(out)


def _field_html(field: steps_mod.Field, value: str) -> str:
    key = html.escape(field.key)
    doc = html.escape(field.doc)
    if field.options:
        opts = "".join(
            f'<option value="{html.escape(o)}"'
            f'{" selected" if o == value else ""}>{html.escape(o)}</option>'
            for o in field.options)
        control = f'<select name="{key}">{opts}</select>'
    else:
        kind = "password" if field.secret else "text"
        placeholder = (f' placeholder="{html.escape(field.example)}"'
                       if field.example else "")
        control = (f'<input type="{kind}" name="{key}" '
                   f'value="{html.escape(value)}"{placeholder} autocomplete="off">')
    optional = "" if not field.optional else " <span class=d>optional</span>"
    unsaved = (" <span class=d>not saved: entered again each time the "
               "installer is opened</span>" if field.secret else "")
    return (f'<label><span class=k>{key}</span>{optional}{unsaved}'
            f'{f"<span class=d>{doc}</span>" if doc else ""}{control}</label>')


def _inventory_html(names: list[str], current: str, problem: str) -> str:
    """The first page: open an inventory under ansible/inventory/, or name a
    new one. Everything answered afterwards is saved into it."""
    opened = "".join(
        f'<button type=submit name=open value="{html.escape(n)}">'
        f'{html.escape(n)}{" (open now)" if n == current else ""}</button> '
        for n in names)
    existing = (f"<form method=post action=/inventory><p class=doc>Open one to "
                f"edit it or finish its install:</p>{opened}</form>"
                if names else "<p class=doc>No inventory yet.</p>")
    error = f"<p class=bad>{html.escape(problem)}</p>" if problem else ""
    return (
        "<h1>Inventory</h1><p class=doc>Each server has an inventory, a "
        "directory under ansible/inventory/ holding its settings. The "
        "answers on every page are saved there as you go. Credentials never "
        "are: they are asked for again each time the installer is opened.</p>"
        f"{existing}{error}"
        "<form method=post action=/inventory><label><span class=k>new "
        "inventory</span><span class=d>lower-case letters, digits, dashes and "
        "underscores</span><input type=text name=create autocomplete=off>"
        "</label><button type=submit>Create</button></form>")


def _checks_html(checks: list[steps_mod.Check]) -> str:
    if not checks:
        return ""
    rows = []
    for check in checks:
        cls = "ok" if check.ok else ("bad" if check.blocking else "warn")
        mark = "&#10003;" if check.ok else ("&#10007;" if check.blocking else "!")
        detail = f" &mdash; {html.escape(check.detail)}" if check.detail else ""
        rows.append(f'<li class={cls}>{mark} {html.escape(check.label)}{detail}</li>')
    return f'<ul class=checks>{"".join(rows)}</ul>'


def start_install(current: run_mod.Run, ansible_dir: Path) -> threading.Thread:
    """Run `catena-cli install` in the background, streaming what it prints to
    the console window and to the run's in-memory tail.

    IN A THREAD, because the install takes tens of minutes and the browser
    cannot hold a request open for it -- and because the console process owns
    the job either way. The page polls the tail; closing the tab does not stop
    the install, and neither does losing the network the browser is on.

    Nothing it prints is written to a file: it ends with the passwords the
    install shows once. The install.yaml is removed whatever happens, for the
    same reason: it carries the client's cloud credentials.
    """
    body_yaml = render.install_yaml(inventory=current.inventory,
                                    answers=current.answers,
                                    secrets=current.secrets)
    current.state = run_mod.STATE_INSTALLING
    current.log.clear()
    current.save()

    def body() -> None:
        rc = 1
        with render.transient_install_yaml(body_yaml) as target:
            argv = render.install_command(ansible_dir, target, current.inventory)
            proc = subprocess.Popen(argv, stdout=subprocess.PIPE,
                                    stderr=subprocess.STDOUT, text=True,
                                    errors="replace")
            assert proc.stdout is not None
            for line in proc.stdout:
                sys.stderr.write(line)
                current.log.append(line.rstrip("\n"))
            rc = proc.wait()
        current.state = (run_mod.STATE_DONE if rc == 0
                         else run_mod.STATE_FAILED)
        current.save()

    thread = threading.Thread(target=body, daemon=True)
    thread.start()
    return thread


def _install_html(current: run_mod.Run) -> str:
    """What the last page shows once the install is under way.

    The log TAIL rather than a spinner. "Installing" answers nothing a client
    can act on, and the thing they want when it stops is which task failed --
    which would otherwise only exist in a console they may have closed.
    """
    if current.state == run_mod.STATE_ANSWERING:
        return ""
    lines = list(current.log) or [
        "(this installer was opened again after the install ran: its output "
        "went to the console window it ran in, and was not kept)"]
    words = {
        run_mod.STATE_INSTALLING: "Installing. This continues if you close "
                                  "this tab; closing the console window stops "
                                  "it.",
        run_mod.STATE_DONE: "Finished. The passwords the installer shows once "
                            "are in the last lines below and in the console "
                            "window. Nothing kept a copy: save them now.",
        run_mod.STATE_FAILED: "The install stopped. The last lines say where.",
    }
    refresh = ('<meta http-equiv=refresh content=5>'
               if current.state == run_mod.STATE_INSTALLING else "")
    return (f"{refresh}<p class=doc>{html.escape(words[current.state])}</p>"
            f'<pre>{html.escape(chr(10).join(lines))}</pre>')


class _Handler(http.server.BaseHTTPRequestHandler):
    # Filled in by serve(). Class attributes rather than constructor arguments
    # because BaseHTTPRequestHandler constructs one instance per request.
    # `run` is None until an inventory is opened or created.
    run: run_mod.Run | None = None
    doc: dict
    ansible_dir: Path
    inventory_root: Path
    all_steps: list[steps_mod.Step]
    last_checks: dict[str, list[steps_mod.Check]] = {}
    inventory_problem: str = ""

    def log_message(self, *_args) -> None:
        """Quiet. The console prints what the install is doing; a request log
        would bury it under one line per form field."""

    def _send(self, body: bytes, status: int = 200) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _redirect(self, where: str) -> None:
        self.send_response(303)
        self.send_header("Location", where)
        self.end_headers()

    def _step(self, name: str) -> steps_mod.Step | None:
        return next((s for s in self.all_steps if s.name == name), None)

    def do_GET(self) -> None:  # noqa: N802 -- the stdlib's spelling
        path = urllib.parse.urlparse(self.path).path
        if path == "/":
            if self.run is None:
                self._redirect("/inventory")
                return
            first = self.run.step or self.all_steps[0].name
            self._redirect(f"/step/{first}")
            return
        if path == "/inventory":
            self._render_inventory()
            return
        if path.startswith("/step/"):
            if self.run is None:
                self._redirect("/inventory")
                return
            self._render_step(path[len("/step/"):])
            return
        self._send(_page("Not found", "<main><h1>Not found</h1></main>"), 404)

    def _form(self) -> dict[str, list[str]]:
        length = int(self.headers.get("Content-Length") or 0)
        return urllib.parse.parse_qs(self.rfile.read(length).decode("utf-8"),
                                     keep_blank_values=True)

    def _render_inventory(self) -> None:
        current = self.run.inventory if self.run else ""
        body = (f"{_nav(self.all_steps, '', current)}<main>"
                f"{_inventory_html(run_mod.inventories(self.inventory_root), current, _Handler.inventory_problem)}"
                "</main>")
        _Handler.inventory_problem = ""
        self._send(_page("Inventory", body))

    def _open(self, name: str) -> None:
        """Make `name` the run: its `.env` read back, its credentials asked for
        again. Anything typed for the previous inventory stays with it."""
        opened = run_mod.load(self.inventory_root / name,
                              run_mod.secret_keys_from(self.doc))
        opened.save()
        _Handler.run = opened
        _Handler.last_checks = {}
        self._redirect(f"/step/{opened.step or self.all_steps[0].name}")

    def _post_inventory(self) -> None:
        form = self._form()
        name = (form.get("open") or [""])[0].strip()
        if name:
            if name not in run_mod.inventories(self.inventory_root):
                _Handler.inventory_problem = f"there is no inventory {name!r}"
                self._redirect("/inventory")
                return
            self._open(name)
            return
        name = (form.get("create") or [""])[0].strip()
        problem = run_mod.new_name_problem(name, self.inventory_root)
        if problem:
            _Handler.inventory_problem = f"{name!r}: {problem}"
            self._redirect("/inventory")
            return
        self._open(name)

    def do_POST(self) -> None:  # noqa: N802
        path = urllib.parse.urlparse(self.path).path
        if path == "/inventory":
            self._post_inventory()
            return
        if not path.startswith("/step/"):
            self._send(_page("Not found", "<main><h1>Not found</h1></main>"), 404)
            return
        if self.run is None:
            self._redirect("/inventory")
            return
        name = path[len("/step/"):]
        step = self._step(name)
        if step is None:
            self._send(_page("Not found", "<main><h1>Not found</h1></main>"), 404)
            return
        form = self._form()
        for field in step.fields:
            if field.key in form:
                self.run.answer(field.key, form[field.key][0], secret=field.secret)
        if name == "keyset":
            self.run.answer("_keyset_acknowledged",
                            "yes" if form.get("ack") else "", secret=False)
        self.run.step = name
        self.run.save()

        checks = steps_mod.validate(name, self.run.answers, self.run.secrets)
        _Handler.last_checks[name] = checks
        if steps_mod.blocked(checks):
            self._redirect(f"/step/{name}")
            return
        nxt = self._next_step(name)
        if nxt:
            self._redirect(f"/step/{nxt}")
            return
        # The last step, checked and acknowledged. Starting the install is the
        # one thing left, and a separate button for it would be a second
        # confirmation of the same decision.
        if self.run.state == run_mod.STATE_ANSWERING:
            start_install(self.run, self.ansible_dir)
        self._redirect(f"/step/{name}")

    def _next_step(self, name: str) -> str:
        names = [s.name for s in self.all_steps]
        i = names.index(name)
        return names[i + 1] if i + 1 < len(names) else ""

    def _render_step(self, name: str) -> None:
        step = self._step(name)
        if step is None:
            self._send(_page("Not found", "<main><h1>Not found</h1></main>"), 404)
            return
        rows = []
        for field in step.fields:
            if not field.shown_for(self.run.answers):
                continue
            value = self.run.value(field.key) or field.default
            rows.append(_field_html(field, value))
        last = name == self.all_steps[-1].name
        if name == "keyset":
            acked = self.run.value("_keyset_acknowledged") == "yes"
            rows.append(
                '<label><input type=checkbox name=ack value=yes'
                f'{" checked" if acked else ""}> '
                "<span>I have somewhere to save the three passwords and the "
                "journal key the installer shows once.</span></label>")
        started = self.run.state != run_mod.STATE_ANSWERING
        form = "" if (last and started) else (
            f'<form method=post action="/step/{html.escape(name)}">'
            f'{"".join(rows)}<button type=submit>'
            f'{"Install" if last else "Check and continue"}</button></form>')
        body = (
            f"{_nav(self.all_steps, name, self.run.inventory)}<main>"
            f"<h1>{html.escape(step.title)}</h1>"
            f"<p class=doc>{html.escape(step.doc)}</p>"
            f"<p class=doc><strong>Checked before this page advances:</strong> "
            f"{html.escape(step.validates)}</p>"
            f"{_checks_html(_Handler.last_checks.get(name) or [])}"
            f"{form}"
            f"{_install_html(self.run) if last else ''}</main>")
        self._send(_page(step.title, body))


def serve(current: run_mod.Run | None, doc: dict, *, port: int,
          ansible_dir: Path) -> int:
    """Serve until the console process is stopped. With no `current` run the
    first page is the inventory picker.

    ThreadingHTTPServer so a probe that takes ten seconds -- and one of them
    reaches Cloudflare -- does not make the rest of the UI look hung.
    """
    _Handler.run = current
    _Handler.doc = doc
    _Handler.ansible_dir = ansible_dir
    _Handler.inventory_root = ansible_dir / "inventory"
    _Handler.all_steps = steps_mod.build(doc)
    _Handler.last_checks = {}
    server = http.server.ThreadingHTTPServer(("127.0.0.1", port), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        thread.join()
    except KeyboardInterrupt:
        server.shutdown()
    return 0


__all__ = ["serve", "start_install"]
