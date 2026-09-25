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


def _nav(all_steps: list[steps_mod.Step], current: str) -> str:
    out = ["<nav>"]
    for step in all_steps:
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
        control = (f'<input type="{kind}" name="{key}" '
                   f'value="{html.escape(value)}" autocomplete="off">')
    optional = "" if not field.optional else " <span class=d>optional</span>"
    return (f'<label><span class=k>{key}</span>{optional}'
            f'{f"<span class=d>{doc}</span>" if doc else ""}{control}</label>')


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
    """Run `catena install` in the background and stream it to the run's log.

    IN A THREAD, because the install takes tens of minutes and the browser
    cannot hold a request open for it -- and because the console process owns
    the job either way. The page polls the log; closing the tab does not stop
    the install, and neither does losing the network the browser is on.

    The install.yaml is removed whatever happens. It carries the client's cloud
    credentials, and a file that outlives the install is one nothing ever comes
    back to delete.
    """
    target = current.path / "install.yaml"
    render.write_install_yaml(target, render.install_yaml(
        inventory=current.inventory, answers=current.answers,
        secrets=current.secrets))
    argv = render.install_command(ansible_dir, target, current.inventory)
    current.state = run_mod.STATE_INSTALLING
    current.save()

    def body() -> None:
        try:
            with open(current.log_path, "wb") as log:
                rc = subprocess.run(argv, stdout=log, stderr=subprocess.STDOUT,
                                    check=False).returncode
        finally:
            target.unlink(missing_ok=True)
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
    try:
        tail = current.log_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        tail = "(nothing logged yet)"
    lines = tail.splitlines()[-40:]
    words = {
        run_mod.STATE_INSTALLING: "Installing. This continues if you close "
                                  "this tab; closing the console window stops "
                                  "it.",
        run_mod.STATE_DONE: "Finished. The installer printed the three "
                            "passwords once, in the console window.",
        run_mod.STATE_FAILED: "The install stopped. Nothing was left "
                              "half-applied; the last lines say where.",
    }
    refresh = ('<meta http-equiv=refresh content=5>'
               if current.state == run_mod.STATE_INSTALLING else "")
    return (f"{refresh}<p class=doc>{html.escape(words[current.state])}</p>"
            f'<pre>{html.escape(chr(10).join(lines))}</pre>')


class _Handler(http.server.BaseHTTPRequestHandler):
    # Filled in by serve(). Class attributes rather than constructor arguments
    # because BaseHTTPRequestHandler constructs one instance per request.
    run: run_mod.Run
    doc: dict
    ansible_dir: Path
    all_steps: list[steps_mod.Step]
    last_checks: dict[str, list[steps_mod.Check]] = {}

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
            first = self.run.step or self.all_steps[0].name
            self._redirect(f"/step/{first}")
            return
        if path.startswith("/step/"):
            self._render_step(path[len("/step/"):])
            return
        self._send(_page("Not found", "<main><h1>Not found</h1></main>"), 404)

    def do_POST(self) -> None:  # noqa: N802
        path = urllib.parse.urlparse(self.path).path
        if not path.startswith("/step/"):
            self._send(_page("Not found", "<main><h1>Not found</h1></main>"), 404)
            return
        name = path[len("/step/"):]
        step = self._step(name)
        if step is None:
            self._send(_page("Not found", "<main><h1>Not found</h1></main>"), 404)
            return
        length = int(self.headers.get("Content-Length") or 0)
        form = urllib.parse.parse_qs(self.rfile.read(length).decode("utf-8"))
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
            acked = self.run.answers.get("_keyset_acknowledged") == "yes"
            rows.append(
                '<label><input type=checkbox name=ack value=yes'
                f'{" checked" if acked else ""}> '
                "<span>I have somewhere to save the three passwords the "
                "installer shows once.</span></label>")
        started = self.run.state != run_mod.STATE_ANSWERING
        form = "" if (last and started) else (
            f'<form method=post action="/step/{html.escape(name)}">'
            f'{"".join(rows)}<button type=submit>'
            f'{"Install" if last else "Check and continue"}</button></form>')
        body = (
            f"{_nav(self.all_steps, name)}<main>"
            f"<h1>{html.escape(step.title)}</h1>"
            f"<p class=doc>{html.escape(step.doc)}</p>"
            f"<p class=doc><strong>Checked before this page advances:</strong> "
            f"{html.escape(step.validates)}</p>"
            f"{_checks_html(_Handler.last_checks.get(name) or [])}"
            f"{form}"
            f"{_install_html(self.run) if last else ''}</main>")
        self._send(_page(step.title, body))


def serve(current: run_mod.Run, doc: dict, *, port: int, ansible_dir: Path) -> int:
    """Serve until the console process is stopped.

    ThreadingHTTPServer so a probe that takes ten seconds -- and one of them
    reaches Cloudflare -- does not make the rest of the UI look hung.
    """
    _Handler.run = current
    _Handler.doc = doc
    _Handler.ansible_dir = ansible_dir
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
