"""The browser UI, on loopback, serving the run this process owns.

LOOPBACK ONLY, and bound explicitly to 127.0.0.1 rather than left to a default.
The pages can carry the provider's password for a client's server; a server
that bound every interface would put it on whatever network the machine happens
to be on, including a cafe's.

THE BROWSER IS A VIEW. Every answer goes straight into the Run this process
holds, and the state that matters is in the inventory. Closing the tab loses
nothing; reopening it shows the same page.

TWO TABS. Inventory opens an inventory or creates one. Installation is one
page: the target and the configuration, one button that verifies them and
starts the install, the install's output, and then the passwords the server
generated and the way into its panel.

STDLIB, DELIBERATELY. An installer on loopback needs routing, forms and HTML
and nothing a framework adds beyond that -- and every dependency here is one a
client installs before they can install anything else.
"""

from __future__ import annotations

import html
import http.server
import json
import re
import subprocess
import sys
import threading
import urllib.parse
from pathlib import Path

from . import registry, render, run as run_mod, steps as steps_mod

# The terminal colour codes the installer prints, which a page shows as noise.
_ANSI = re.compile(r"\x1b\[[0-9;]*m")

# Where a client reports an install that failed.
ISSUES_URL = "https://github.com/catenahq/catena-ce"

# The values `catena-cli install --keyset-json` prints, in the order the access
# section shows them, with what each is called there and, when the name alone
# misleads, what it is for.
KEYSET_FIELDS = (
    ("admin_password", "Admin password", ""),
    ("console_recovery_password", "'ops' user password",
     "For the provider's web console or a physical keyboard only: SSH accepts "
     "keys, never this password."),
    ("journal_verification_key", "Journal verification key",
     "Proves the server's own log of administrative actions was not altered. "
     "The server keeps no copy."),
)
KEYSET_PENDING = "Shown here during the installation."
# A re-run on an installed server: the journal key exists only at the first.
KEYSET_NOT_AGAIN = "Shown at the first installation only."

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
main { max-width: 46rem; margin: 0 auto; padding: 2rem 1rem 4rem; }
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
button { padding: .5rem 1.1rem; font: inherit;
         border: 1px solid var(--line); border-radius: 4px; cursor: pointer;
         background: rgba(128,128,128,.12); color: var(--fg); }
.act { display: flex; align-items: center; gap: .5rem; margin-top: 1.25rem; }
ul.inv { list-style: none; padding: 0; margin: .75rem 0; }
ul.inv li { margin: 0 0 .4rem; }
ul.inv button { display: block; width: 100%; text-align: left; }
code { font-family: ui-monospace, monospace; font-size: .9rem; }
ul.checks { list-style: none; padding: 0; margin: .75rem 0 0; }
ul.checks li { padding: .2rem 0; }
.bad { color: var(--bad); } .warn { color: var(--warn); } .ok { color: var(--ok); }
pre { white-space: pre-wrap; border: 1px solid var(--line); border-radius: 4px;
      padding: .75rem; overflow-x: auto; }
pre.log { max-height: 60vh; overflow-y: auto; }
dl.keys { margin: 1rem 0; }
dl.keys dt { margin-top: .75rem; }
dl.keys dd { margin: .2rem 0 0; }
dl.keys code { display: block; padding: .4rem .5rem; border: 1px solid var(--line);
               border-radius: 4px; word-break: break-all; user-select: all; }
"""

# While the install runs, the page polls /progress and updates in place, which
# keeps the scroll position and a selection in the passwords being copied. It
# reloads once when the install ends, for the page that says how it ended. The
# output box follows new lines while it is scrolled to the bottom, and stays
# put while someone reads further up.
_POLL = """<script>
(function () {
  var log = document.getElementById("log");
  log.scrollTop = log.scrollHeight;
  if (log.dataset.state !== "installing") return;
  function poll() {
    fetch("/progress").then(function (r) { return r.json(); }).then(function (p) {
      if (p.state !== "installing") { location.reload(); return; }
      var atEnd = log.scrollHeight - log.scrollTop - log.clientHeight < 40;
      log.textContent = p.log;
      if (atEnd) log.scrollTop = log.scrollHeight;
      Object.keys(p.keyset).forEach(function (name) {
        var el = document.getElementById("k-" + name);
        if (el && el.textContent !== p.keyset[name]) el.textContent = p.keyset[name];
      });
    }).catch(function () {}).then(function () { setTimeout(poll, 3000); });
  }
  setTimeout(poll, 3000);
})();
</script>"""


def _page(title: str, body: str) -> bytes:
    return (
        "<!doctype html><html lang=en><head><meta charset=utf-8>"
        "<meta name=viewport content='width=device-width,initial-scale=1'>"
        f"<title>{html.escape(title)} -- Catena installer</title>"
        f"<style>{_STYLE}</style></head><body>{body}</body></html>"
    ).encode("utf-8")


def _nav(inventory: str, *, on: str) -> str:
    """`on` is the tab shown: "inventory" or "install"."""
    def link(href: str, name: str, text: str) -> str:
        return f'<a href="{href}"{" class=on" if on == name else ""}>{text}</a>'

    out = ["<nav>", link("/inventory", "inventory", "Inventory")]
    if inventory:
        out.append(link("/", "install", "Installation"))
    out.append("</nav>")
    return "".join(out)


def _tip(text: str, about: str) -> str:
    """A `(?)` that shows `text` on hover, and on keyboard focus."""
    return (f'<span class=tip tabindex=0 aria-label="{html.escape(about)}">(?)'
            f'<span class=tt role=tooltip>{html.escape(text)}</span></span>')


def _field_html(field: steps_mod.Field, value: str) -> str:
    """One field: its label, `(optional)` and a `(?)` holding the explanation,
    above the control.

    A field limited to a list is a select, and a saved value missing from the
    list stays selected as an extra option, so opening the page never changes
    it. A field with suggestions is free text with the suggestions offered
    beside it: the key the provider installed is usually one of the pairs
    already on this machine, and sometimes it is not."""
    key = html.escape(field.key)
    fid = f"f-{key}"
    choices = list(field.options)
    if value and choices and value not in choices:
        choices.append(value)
    if choices:
        opts = "".join(
            f'<option value="{html.escape(o)}"'
            f'{" selected" if o == value else ""}>{html.escape(o) or "(none)"}</option>'
            for o in choices)
        control = f'<select id="{fid}" name="{key}">{opts}</select>'
    else:
        kind = "password" if field.secret else "text"
        placeholder = (f' placeholder="{html.escape(field.example)}"'
                       if field.example else "")
        listed = f' list="s-{key}"' if field.suggestions else ""
        control = (f'<input type="{kind}" id="{fid}" name="{key}" '
                   f'value="{html.escape(value)}"{placeholder}{listed} '
                   'autocomplete="off">')
        if field.suggestions:
            control += (f'<datalist id="s-{key}">'
                        + "".join(f'<option value="{html.escape(s)}">'
                                  for s in field.suggestions)
                        + "</datalist>")
    notes = [field.doc] if field.doc else []
    if field.secret:
        notes.append("Not saved: entered again each time the installer is opened.")
    tip = _tip("\n\n".join(notes), f"About {field.label}") if notes else ""
    optional = " <span class=d>(optional)</span>" if field.optional else ""
    return (f'<div class=field data-key="{key}">'
            f'<div class=head><label for="{fid}">{html.escape(field.label)}</label>'
            f"{optional}{tip}</div>{control}</div>")


def _inventory_html(names: list[str], current: str, problem: str) -> str:
    """The Inventory tab: open an inventory under ansible/inventory/, one per
    line, or name a new one."""
    rows = "".join(
        f'<li><button type=submit name=open value="{html.escape(n)}">'
        f'{html.escape(n)}{" (open now)" if n == current else ""}</button></li>'
        for n in names)
    existing = (f"<form method=post action=/inventory><ul class=inv>{rows}</ul></form>"
                if names else "<p class=doc>No inventory yet.</p>")
    error = f"<p class=bad>{html.escape(problem)}</p>" if problem else ""
    return (
        "<h1>Inventory</h1>"
        "<p>Each server is defined by an \"inventory\": a folder that holds its "
        "identity. Catena's inventory system saves non-secret information "
        "only. Secrets, such as passwords and API tokens, must be saved to a "
        "password manager.</p>"
        "<p>Choose an existing inventory to install or converge / repair, or "
        "create one for a new installation.</p>"
        f"{existing}{error}"
        "<form method=post action=/inventory><div class=field><div class=head>"
        "<label for=f-create>New inventory name</label>"
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


def _step_html(step: steps_mod.Step, values: dict[str, str]) -> str:
    """One section of the form: its explanation, its fields, and its note."""
    rows = "".join(_field_html(field, values[field.key]) for field in step.fields)
    note = f"<p class=doc>{html.escape(step.note)}</p>" if step.note else ""
    return (f'<section id="{html.escape(step.name)}">'
            f"<h2>{html.escape(step.title)}</h2>"
            f"<p class=doc>{html.escape(step.doc)}</p>{rows}{note}</section>")


def _process_html(current: run_mod.Run, checks: list[steps_mod.Check]) -> str:
    """The button that verifies every section and starts the install, what the
    verification found, and the install's output.

    The log TAIL rather than a spinner. "Installing" answers nothing a client
    can act on, and the thing they want when it stops is which task failed --
    which would otherwise only exist in a console they may have closed.
    """
    status = {
        run_mod.STATE_ANSWERING: "",
        run_mod.STATE_INSTALLING: "Installing. Closing this tab does not stop "
                                  "it; closing the console window does.",
        run_mod.STATE_DONE: "Finished.",
        run_mod.STATE_FAILED: "The installation stopped. The output says where.",
    }[current.state]
    if current.log:
        lines = "\n".join(current.log)
    elif current.state == run_mod.STATE_ANSWERING:
        lines = "The installation output appears here."
    else:
        lines = ("(this installer was opened again after the install ran: its "
                 "output went to the console window it ran in, and was not kept)")
    return (
        "<section id=install><h2>Installation process</h2>"
        "<p class=doc>The installation will run for several minutes and its "
        "output will be shown below. Several secrets will be displayed in the "
        "next section as soon as the server has generated them, a few minutes "
        "in. In case of error during installation, please create an issue at "
        f'<a href="{ISSUES_URL}">{ISSUES_URL}</a> and copy the errors there.</p>'
        "<div class=act><button type=submit name=install value=yes>Verify "
        "configuration and start installation</button></div>"
        f"{_checks_html(checks)}"
        f'{f"<p class=doc>{html.escape(status)}</p>" if status else ""}'
        f'<pre id=log class=log data-state="{html.escape(current.state)}">'
        f"{html.escape(lines)}</pre></section>")


def keyset_shown(current: run_mod.Run) -> dict[str, str]:
    """What the access section shows for each password: the value, or why there
    is none to show."""
    if not current.keyset:
        return {name: KEYSET_PENDING for name, _, _ in KEYSET_FIELDS}
    return {name: current.keyset.get(name) or KEYSET_NOT_AGAIN
            for name, _, _ in KEYSET_FIELDS}


def _access_html(current: run_mod.Run, values: dict[str, str]) -> str:
    """The passwords the install printed, and the way into the panel through
    the server and key the form shows."""
    shown = keyset_shown(current)
    keys = "".join(
        f"<dt>{html.escape(label)}{' ' + _tip(about, f'About {label}') if about else ''}"
        f'</dt><dd><code id="k-{name}">{html.escape(shown[name])}</code></dd>'
        for name, label, about in KEYSET_FIELDS)
    panel = f"http://localhost:{steps_mod.PANEL_PORT}"
    portainer = f"http://localhost:{steps_mod.PORTAINER_PORT}"
    return (
        "<section id=access><h2>Catena server access</h2>"
        "<p>Copy these keys to your password manager and use them to access "
        "your new installation.</p>"
        f"<dl class=keys>{keys}</dl>"
        "<p>Run the following command in your console/terminal to establish a "
        "connection to your new Catena server:</p>"
        f"<pre>{html.escape(steps_mod.forward_command(values))}</pre>"
        "<p>You can reach the Catena administration panel in your browser at "
        f'<a href="{panel}">{panel}</a>. Log in using the admin email and '
        "password.</p>"
        "<p>You can reach the Portainer (applications management) panel in your "
        f'browser at <a href="{portainer}">{portainer}</a>. Log in using user '
        "'admin' and the admin password.</p>"
        "<p>Visit the Settings tab in the admin panel to configure Domain and "
        "DNS access (Cloudflare), Administrator tunnel (Tailnet), outgoing "
        "emails, backups, and more.</p></section>")


def start_install(current: run_mod.Run, ansible_dir: Path) -> threading.Thread:
    """Run `catena-cli install` in the background, streaming what it prints to
    the console window and to the run's in-memory tail.

    IN A THREAD, because the install takes tens of minutes and the browser
    cannot hold a request open for it -- and because the console process owns
    the job either way. The page polls the tail; closing the tab does not stop
    the install, and neither does losing the network the browser is on.

    Nothing it prints is written to a file: it carries the passwords the
    install shows once. The CLI prints them as JSON between its marker lines;
    they go to the run's keyset, which the page shows in its own section, and
    not to the console window. Every other line goes to both. The install.yaml
    is removed whatever happens, for the same reason: it can carry the
    provider's password.

    NO STDIN. Nothing on the page can answer a question, so the install runs
    with no input, and anything it would have asked takes its default.
    """
    cli = registry.ansible_module("catena_cli")
    body_yaml = render.install_yaml(inventory=current.inventory,
                                    answers=current.answers,
                                    secrets=current.secrets)
    current.state = run_mod.STATE_INSTALLING
    current.log.clear()
    current.save()

    def body() -> None:
        rc = 1
        with render.transient_install_yaml(body_yaml) as target:
            argv = render.install_command(ansible_dir, target, current.inventory,
                                          keyset_json=True)
            proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL,
                                    stdout=subprocess.PIPE,
                                    stderr=subprocess.STDOUT, text=True,
                                    errors="replace")
            assert proc.stdout is not None
            keyset: list[str] | None = None
            for line in proc.stdout:
                text = _ANSI.sub("", line.rstrip("\n"))
                if text == cli.KEYSET_BEGIN:
                    keyset = []
                elif keyset is None:
                    sys.stderr.write(line)
                    current.log.append(text)
                elif text == cli.KEYSET_END:
                    current.keyset = {str(k): str(v) for k, v
                                      in json.loads(keyset[0]).items()}
                    sys.stderr.write("catena-gui: the passwords are shown in "
                                     "the browser\n")
                    keyset = None
                else:
                    keyset.append(text)
            rc = proc.wait()
        current.state = (run_mod.STATE_DONE if rc == 0
                         else run_mod.STATE_FAILED)
        current.save()

    thread = threading.Thread(target=body, daemon=True)
    thread.start()
    return thread


class _Handler(http.server.BaseHTTPRequestHandler):
    # Filled in by serve(). Class attributes rather than constructor arguments
    # because BaseHTTPRequestHandler constructs one instance per request.
    # `run` is None until an inventory is opened or created.
    run: run_mod.Run | None = None
    doc: dict
    ansible_dir: Path
    inventory_root: Path
    all_steps: list[steps_mod.Step]
    last_checks: list[steps_mod.Check] = []
    inventory_problem: str = ""

    def log_message(self, *_args) -> None:
        """Quiet. The console prints what the install is doing; a request log
        would bury it under one line per form field."""

    def _send(self, body: bytes, status: int = 200,
              content_type: str = "text/html; charset=utf-8") -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _redirect(self, where: str) -> None:
        self.send_response(303)
        self.send_header("Location", where)
        self.end_headers()

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
        if path == "/progress" and self.run is not None:
            # What the installation page's poll reads while the install runs.
            self._send(json.dumps({"state": self.run.state,
                                   "log": "\n".join(self.run.log),
                                   "keyset": keyset_shown(self.run)}).encode("utf-8"),
                       content_type="application/json")
            return
        self._send(_page("Not found", "<main><h1>Not found</h1></main>"), 404)

    def _form(self) -> dict[str, list[str]]:
        length = int(self.headers.get("Content-Length") or 0)
        return urllib.parse.parse_qs(self.rfile.read(length).decode("utf-8"),
                                     keep_blank_values=True)

    def _render_inventory(self) -> None:
        current = self.run.inventory if self.run else ""
        body = (f"{_nav(current, on='inventory')}<main>"
                f"{_inventory_html(run_mod.inventories(self.inventory_root), current, _Handler.inventory_problem)}"
                "</main>")
        _Handler.inventory_problem = ""
        self._send(_page("Inventory", body))

    def _open(self, name: str) -> None:
        """Make `name` the run: its `.env` read back, its credentials asked for
        again. Anything typed for the previous inventory stays with it."""
        opened = run_mod.load(self.inventory_root / name,
                              run_mod.secret_keys_from(self.doc))
        # Created on disk when new. An existing one is written by an attempt
        # to install, never just for being opened.
        if not opened.path.is_dir():
            opened.save()
        _Handler.run = opened
        _Handler.last_checks = []
        self._redirect("/")

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
        """Save the form, verify every section, and start the install when none
        of them blocks. The answers are saved whatever the verification finds,
        so a closed installer reopens on them."""
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
        if self.run.state == run_mod.STATE_INSTALLING:
            # The answers went to an install that is running; an edit now
            # would describe a host nobody is building.
            self._redirect("/#install")
            return
        form = self._form()
        for step in self.all_steps:
            for field in step.fields:
                if field.key in form:
                    self.run.answer(field.key, form[field.key][0], secret=field.secret)
        values = self._shown_values()
        checks: list[steps_mod.Check] = []
        for step in self.all_steps:
            missing = steps_mod.missing_required(step, values)
            checks += missing or steps_mod.validate(
                step.name, self.run.answers, self.run.secrets)
        _Handler.last_checks = checks
        self.run.save()
        if not steps_mod.blocked(checks):
            start_install(self.run, self.ansible_dir)
        self._redirect("/#install")

    def _shown_values(self) -> dict[str, str]:
        """What each field shows: the answer, else the default, else for a
        choice the first option, which is what a select with no match shows."""
        return {f.key: (self.run.value(f.key) or f.default
                        or (f.options[0] if f.options else ""))
                for s in self.all_steps for f in s.fields}

    def _render_install(self) -> None:
        installing = self.run.state == run_mod.STATE_INSTALLING
        values = self._shown_values()
        sections = "".join(_step_html(s, values) for s in self.all_steps)
        body = (
            f"{_nav(self.run.inventory, on='install')}<main>"
            f"<h1>Install: {html.escape(self.run.inventory)}</h1>"
            f"<p class=doc>Settings are saved to <code>{html.escape(str(self.run.path))}"
            "</code>, excluding secrets and passwords.</p>"
            f'<form method=post action="/"><fieldset{" disabled" if installing else ""}>'
            f"{sections}{_process_html(self.run, _Handler.last_checks)}"
            "</fieldset></form>"
            f"{_access_html(self.run, values)}</main>{_POLL}")
        self._send(_page(f"Install: {self.run.inventory}", body))


def serve(current: run_mod.Run | None, doc: dict, *, port: int,
          ansible_dir: Path) -> int:
    """Serve until the console process is stopped. With no `current` run the
    first tab is the inventory picker.

    ThreadingHTTPServer so a probe that takes ten seconds -- an SSH login
    waiting on a server still booting -- does not make the rest of the UI look
    hung.
    """
    _Handler.run = current
    _Handler.doc = doc
    _Handler.ansible_dir = ansible_dir
    _Handler.inventory_root = ansible_dir / "inventory"
    _Handler.all_steps = steps_mod.build(doc)
    _Handler.last_checks = []
    server = http.server.ThreadingHTTPServer(("127.0.0.1", port), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        thread.join()
    except KeyboardInterrupt:
        server.shutdown()
    return 0


__all__ = ["serve", "start_install"]
