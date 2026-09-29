"""The browser UI, on loopback, serving the run this process owns.

LOOPBACK ONLY, and bound explicitly to 127.0.0.1 rather than left to a default.
The pages carry a client's cloud credentials in form fields; a server that
bound every interface would put them on whatever network the machine happens to
be on, including a cafe's.

THE BROWSER IS A VIEW. Every answer goes straight into the Run this process
holds, and the state that matters is in the inventory. Closing the tab loses
nothing checked; reopening it shows the same page.

TWO PAGES. The inventory picker, then one install page holding every section
in a single form: checking one section saves what was typed in all of them, so
nothing entered further down is lost to a check further up.

STDLIB, DELIBERATELY. An installer on loopback needs routing, forms and HTML
and nothing a framework adds beyond that -- and every dependency here is one a
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
nav a { padding: .25rem .6rem; border-radius: 4px; text-decoration: none;
        color: var(--muted); }
nav a.on { color: var(--fg); font-weight: 600; background: rgba(128,128,128,.14); }
h1 { font-size: 1.4rem; margin: 1.5rem 0 .25rem; }
h2 { font-size: 1.15rem; margin: 0 0 .25rem; }
section { border-top: 1px solid var(--line); margin-top: 2rem; padding-top: 1.25rem; }
fieldset { border: 0; margin: 0; padding: 0; min-width: 0; }
p.doc { color: var(--muted); white-space: pre-wrap; }
.field { margin: 1rem 0 0; }
.head { display: flex; align-items: baseline; gap: .4rem; flex-wrap: wrap; }
.k { font-family: ui-monospace, monospace; font-size: .85rem; }
.d { color: var(--muted); font-size: .85rem; }
.tip { position: relative; color: var(--muted); font-size: .85rem; cursor: help; }
.tip .tt { display: none; position: absolute; z-index: 10; left: 0; top: 1.5rem;
           width: min(26rem, 80vw); padding: .6rem .75rem; white-space: pre-line;
           font-size: .85rem; line-height: 1.45; color: var(--fg); background: var(--bg);
           border: 1px solid var(--line); border-radius: 4px;
           box-shadow: 0 4px 16px rgba(0,0,0,.2); }
.tip:hover .tt, .tip:focus .tt { display: block; }
input, select { width: 100%; padding: .45rem .5rem; margin-top: .3rem;
                border: 1px solid var(--line); border-radius: 4px;
                background: var(--bg); color: var(--fg); font: inherit; }
label.ack { display: flex; align-items: center; gap: .6rem; margin-top: 1.1rem; }
label.ack input { width: auto; margin: 0; flex: none; }
button { padding: .5rem 1.1rem; font: inherit;
         border: 1px solid var(--line); border-radius: 4px; cursor: pointer;
         background: rgba(128,128,128,.12); color: var(--fg); }
.act { display: flex; align-items: center; gap: .5rem; margin-top: 1.25rem; }
ul.inv { list-style: none; padding: 0; margin: .75rem 0; }
ul.inv li { margin: 0 0 .4rem; }
ul.inv button { display: block; width: 100%; text-align: left; }
ul.checks { list-style: none; padding: 0; margin: .75rem 0 0; }
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


def _nav(inventory: str, *, on_inventory: bool) -> str:
    out = ["<nav>", f'<a href="/inventory"{" class=on" if on_inventory else ""}>'
                    "Inventory</a>"]
    if inventory:
        out.append(f'<a href="/"{"" if on_inventory else " class=on"}>'
                   f"Install {html.escape(inventory)}</a>")
    out.append("</nav>")
    return "".join(out)


def _tip(text: str, about: str) -> str:
    """A `(?)` that shows `text` on hover, and on keyboard focus."""
    return (f'<span class=tip tabindex=0 aria-label="{html.escape(about)}">(?)'
            f'<span class=tt role=tooltip>{html.escape(text)}</span></span>')


def _field_html(field: steps_mod.Field, value: str, *, shown: bool,
                governed: bool) -> str:
    """One field: its name, `(optional)` and a `(?)` holding the explanation,
    above the control.

    Every field is rendered and a governed one is hidden rather than left out,
    so the page shows or hides it the moment its governing choice changes
    (`governed`: the governing field is on this page too). `shown` is the
    server's answer from what is saved, for a browser without scripts."""
    key = html.escape(field.key)
    fid = f"f-{key}"
    if field.options:
        opts = "".join(
            f'<option value="{html.escape(o)}"'
            f'{" selected" if o == value else ""}>{html.escape(o)}</option>'
            for o in field.options)
        control = f'<select id="{fid}" name="{key}">{opts}</select>'
    else:
        kind = "password" if field.secret else "text"
        placeholder = (f' placeholder="{html.escape(field.example)}"'
                       if field.example else "")
        control = (f'<input type="{kind}" id="{fid}" name="{key}" '
                   f'value="{html.escape(value)}"{placeholder} autocomplete="off">')
    notes = [field.doc] if field.doc else []
    if field.secret:
        notes.append("Not saved: entered again each time the installer is opened.")
    tip = _tip("\n\n".join(notes), f"About {field.key}") if notes else ""
    optional = " <span class=d>(optional)</span>" if field.optional else ""
    gov = (f' data-gov="{html.escape(field.governor)}"'
           f' data-vals="{html.escape(" ".join(field.governor_values))}"'
           if governed else "")
    return (f'<div class=field data-key="{key}"{gov}{"" if shown else " hidden"}>'
            f'<div class=head><label class=k for="{fid}">{key}</label>'
            f"{optional}{tip}</div>{control}</div>")


def _governors_first(fields: list[steps_mod.Field]) -> list[steps_mod.Field]:
    """The section's fields with each choice placed before the fields it
    governs, so changing a choice shows or hides fields below it rather than
    above it. Otherwise in registry order."""
    by_key = {f.key: f for f in fields}
    out: list[steps_mod.Field] = []

    def place(field: steps_mod.Field) -> None:
        if any(f.key == field.key for f in out):
            return
        governor = by_key.get(field.governor)
        if governor is not None:
            place(governor)
        out.append(field)

    for field in fields:
        place(field)
    return out


def _visible(field: steps_mod.Field, by_key: dict[str, steps_mod.Field],
             values: dict[str, str]) -> bool:
    """Whether a field shows, given what every field shows: its governor's
    value is one it applies to, and its governor shows too."""
    if not field.governor:
        return True
    governor = by_key.get(field.governor)
    if governor is not None and not _visible(governor, by_key, values):
        return False
    return values.get(field.governor, "") in field.governor_values


# The same rule as _visible, in the browser: shows or hides each governed field
# as its governing choice changes, and once on load.
_SCRIPT = """
(function () {
  function value(key) {
    var el = document.querySelector('[name="' + key + '"]');
    return el ? el.value : "";
  }
  function shown(box) {
    var gov = box.getAttribute("data-gov");
    var govBox = document.querySelector('.field[data-key="' + gov + '"]');
    if (govBox && govBox.hasAttribute("data-gov") && !shown(govBox)) return false;
    return box.getAttribute("data-vals").split(" ").indexOf(value(gov)) >= 0;
  }
  function apply() {
    document.querySelectorAll(".field[data-gov]").forEach(function (box) {
      box.hidden = !shown(box);
    });
  }
  document.addEventListener("change", apply);
  apply();
})();
"""


def _inventory_html(names: list[str], current: str, problem: str) -> str:
    """The first page: open an inventory under ansible/inventory/, one per
    line, or name a new one. Everything answered afterwards is saved into it."""
    rows = "".join(
        f'<li><button type=submit name=open value="{html.escape(n)}">'
        f'{html.escape(n)}{" (open now)" if n == current else ""}</button></li>'
        for n in names)
    existing = (f"<form method=post action=/inventory><p class=doc>Open one to "
                f"edit it or finish its install:</p><ul class=inv>{rows}</ul></form>"
                if names else "<p class=doc>No inventory yet.</p>")
    error = f"<p class=bad>{html.escape(problem)}</p>" if problem else ""
    return (
        "<h1>Inventory</h1><p class=doc>Each server has an inventory, a "
        "directory under ansible/inventory/ holding its settings. Answers are "
        "saved there each time a section is checked. Credentials never are: "
        "they are asked for again each time the installer is opened.</p>"
        f"{existing}{error}"
        "<form method=post action=/inventory><div class=field><div class=head>"
        "<label class=k for=f-create>new inventory</label>"
        + _tip("Lower-case letters, digits, dashes and underscores, starting "
               "with a letter or digit.", "About the inventory name")
        + "</div><input type=text id=f-create name=create autocomplete=off></div>"
        "<div class=act><button type=submit>Create</button></div></form>")


def _checks_html(checks: list[steps_mod.Check]) -> str:
    if not checks:
        return ""
    rows = []
    for check in checks:
        cls = "ok" if check.ok else ("bad" if check.blocking else "warn")
        mark = "&#10003;" if check.ok else ("&#10007;" if check.blocking else "!")
        detail = f": {html.escape(check.detail)}" if check.detail else ""
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
    """The section below the form once the install is under way.

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
    refresh = ('<meta http-equiv=refresh content="5;url=/#install">'
               if current.state == run_mod.STATE_INSTALLING else "")
    return (f"<section id=install>{refresh}<h2>Install output</h2>"
            f"<p class=doc>{html.escape(words[current.state])}</p>"
            f"<pre>{html.escape(chr(10).join(lines))}</pre></section>")


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
            self._render_install()
            return
        if path == "/inventory":
            self._render_inventory()
            return
        self._send(_page("Not found", "<main><h1>Not found</h1></main>"), 404)

    def _form(self) -> dict[str, list[str]]:
        length = int(self.headers.get("Content-Length") or 0)
        return urllib.parse.parse_qs(self.rfile.read(length).decode("utf-8"),
                                     keep_blank_values=True)

    def _render_inventory(self) -> None:
        current = self.run.inventory if self.run else ""
        body = (f"{_nav(current, on_inventory=True)}<main>"
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
        self._redirect(f"/#{opened.step}" if self._step(opened.step) else "/")

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
        if path != "/":
            self._send(_page("Not found", "<main><h1>Not found</h1></main>"), 404)
            return
        if self.run is None:
            self._redirect("/inventory")
            return
        if self.run.state != run_mod.STATE_ANSWERING:
            # The answers went to an install that already started; an edit now
            # would describe a host nobody is building.
            self._redirect("/#install")
            return
        form = self._form()
        for step in self.all_steps:
            for field in step.fields:
                if field.key in form:
                    self.run.answer(field.key, form[field.key][0], secret=field.secret)
        self.run.answer("_keyset_acknowledged",
                        "yes" if form.get("ack") else "", secret=False)

        # Check one section, or every section before the install. Install
        # starts only when none of them blocks, so there is no second
        # confirmation of the same decision.
        installing = bool(form.get("install"))
        checked = (form.get("check") or [""])[0]
        names = ([s.name for s in self.all_steps] if installing
                 else [checked] if self._step(checked) else [])
        for name in names:
            _Handler.last_checks[name] = steps_mod.validate(
                name, self.run.answers, self.run.secrets)
        blocked = next((n for n in names
                        if steps_mod.blocked(_Handler.last_checks[n])), "")
        self.run.step = blocked or checked or self.run.step
        self.run.save()
        if installing and not blocked:
            start_install(self.run, self.ansible_dir)
            self._redirect("/#install")
            return
        where = blocked or checked
        self._redirect(f"/#{where}" if where else "/")

    def _shown_values(self) -> dict[str, str]:
        """What each field shows: the answer, else the default, else for a
        choice the first option, which is what a select with no match shows."""
        return {f.key: (self.run.value(f.key) or f.default
                        or (f.options[0] if f.options else ""))
                for s in self.all_steps for f in s.fields}

    def _section_html(self, step: steps_mod.Step, started: bool) -> str:
        """One section: its fields, then its button with the results of its
        last check directly under it. The last section's button is Install."""
        by_key = {f.key: f for s in self.all_steps for f in s.fields}
        values = self._shown_values()
        rows = []
        for field in _governors_first(step.fields):
            rows.append(_field_html(field, values[field.key],
                                    shown=_visible(field, by_key, values),
                                    governed=field.governor in by_key))
        last = step.name == self.all_steps[-1].name
        if last:
            acked = self.run.value("_keyset_acknowledged") == "yes"
            rows.append(
                '<label class=ack><input type=checkbox name=ack value=yes'
                f'{" checked" if acked else ""}>'
                "<span>I have somewhere to save the three passwords and the "
                "journal key the installer shows once.</span></label>")
        if started:
            action = ""
        elif last:
            action = ("<div class=act><button type=submit name=install value=yes>"
                      "Install</button>"
                      + _tip("Every section is checked first, and the install "
                             "starts only when none of them fails.\n\n"
                             + step.validates, "What Install checks")
                      + "</div>")
        else:
            action = (f"<div class=act><button type=submit name=check "
                      f'value="{html.escape(step.name)}">Check</button>'
                      + _tip(step.validates, f"What {step.title} checks")
                      + "</div>")
        return (f'<section id="{html.escape(step.name)}">'
                f"<h2>{html.escape(step.title)}</h2>"
                f"<p class=doc>{html.escape(step.doc)}</p>"
                f'{"".join(rows)}{action}'
                f"{_checks_html(_Handler.last_checks.get(step.name) or [])}"
                "</section>")

    def _render_install(self) -> None:
        started = self.run.state != run_mod.STATE_ANSWERING
        sections = "".join(self._section_html(s, started) for s in self.all_steps)
        body = (
            f"{_nav(self.run.inventory, on_inventory=False)}<main>"
            f"<h1>Install {html.escape(self.run.inventory)}</h1>"
            "<p class=doc>Answers are saved to ansible/inventory/"
            f"{html.escape(self.run.inventory)}/.env each time a section is "
            "checked. Credentials are not.</p>"
            f'<form method=post action="/"><fieldset{" disabled" if started else ""}>'
            f"{sections}</fieldset></form>"
            f"{_install_html(self.run)}"
            f"<script>{_SCRIPT}</script></main>")
        self._send(_page(f"Install {self.run.inventory}", body))


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
