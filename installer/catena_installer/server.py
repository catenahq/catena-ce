"""The browser UI, on loopback, serving the run this process owns.

LOOPBACK ONLY, and bound explicitly to 127.0.0.1 rather than left to a default.
The pages can carry the provider's password for a client's server; a server
that bound every interface would put it on whatever network the machine happens
to be on, including a cafe's.

EVERY PAGE IN ENGLISH OR FRENCH. The switcher's `?lang=` sets a cookie; without
one the browser's Accept-Language decides (i18n.resolve).
"""

from __future__ import annotations

import html
import http.cookies
import http.server
import json
import re
import sys
import threading
import urllib.parse
from pathlib import Path

from . import i18n, install, keys, remote, run as run_mod, steps as steps_mod

# The terminal colour codes a server prints, which a page shows as noise.
_ANSI = re.compile(r"\x1b\[[0-9;]*m")

# Where a client reports an install that failed.
ISSUES_URL = "https://github.com/catenahq/catena-ce"

# The cookie the language switcher sets.
LANG_COOKIE = "catena_lang"

# The values the install prints as JSON between its KEYSET markers, in the
# order the access section shows them. Each is named by `keyset.<name>` in the
# translations, and explained by `keyset.<name>.about` when the name alone
# misleads.
KEYSET_FIELDS = ("admin_password", "console_recovery_password",
                 "journal_verification_key")

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
nav .langs { margin-left: auto; display: flex; gap: .25rem; }
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
.field.choice label.opt { display: block; }
.field.choice label.opt input { width: auto; margin: 0 .4rem 0 0; }
.field.choice:has(input[value="public"]:checked) .addr { display: none; }
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
ul.checks label input { width: auto; margin: 0 .4rem 0 0; }
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


def _h(lang: str, key: str, **markup: str) -> str:
    """A translation as HTML: its text escaped, then `markup` (already HTML)
    filled into its placeholders."""
    return html.escape(i18n.TEXT[lang][key]).format(**markup)


def _page(lang: str, title: str, body: str) -> bytes:
    return (
        f"<!doctype html><html lang={lang}><head><meta charset=utf-8>"
        "<meta name=viewport content='width=device-width,initial-scale=1'>"
        f"<title>{_h(lang, 'page.title', title=html.escape(title))}</title>"
        f"<style>{_STYLE}</style></head><body>{body}</body></html>"
    ).encode("utf-8")


def _nav(lang: str, inventory: str, *, on: str) -> str:
    """`on` is the tab shown: "inventory" or "install". The language switcher
    sits at the end, each language named in itself."""
    def link(href: str, name: str, text: str, current: str) -> str:
        return f'<a href="{href}"{" class=on" if current == name else ""}>{text}</a>'

    out = ["<nav>", link("/inventory", "inventory", _h(lang, "nav.inventory"), on)]
    if inventory:
        out.append(link("/", "install", _h(lang, "nav.install"), on))
    out.append(f'<span class=langs aria-label="{_h(lang, "nav.language")}">')
    for code in i18n.LANGS:
        current = " class=on" if code == lang else ""
        out.append(f'<a href="?lang={code}" hreflang={code} lang={code}{current}>'
                   f"{html.escape(i18n.NAMES[code])}</a>")
    out.append("</span></nav>")
    return "".join(out)


def _tip(text: str, about: str) -> str:
    """A `(?)` that shows `text` on hover, and on keyboard focus."""
    return (f'<span class=tip tabindex=0 aria-label="{html.escape(about)}">(?)'
            f'<span class=tt role=tooltip>{html.escape(text)}</span></span>')


def _field_html(field: steps_mod.Field, value: str, lang: str) -> str:
    """One field: its label, `(optional)` and a `(?)` holding the help, above
    the control.

    A field limited to a list is a select, and a saved value missing from the
    list stays selected as an extra option, so opening the page never changes
    it. A field with suggestions is free text with the suggestions offered
    beside it: the key the provider installed is usually one of the pairs
    already on this machine, and sometimes it is not."""
    key = html.escape(field.key)
    fid = f"f-{key}"
    label = field.label[lang]
    choices = list(field.options)
    if value and choices and value not in choices:
        choices.append(value)
    if choices:
        opts = "".join(
            f'<option value="{html.escape(o)}"'
            f'{" selected" if o == value else ""}>'
            f'{html.escape(o) or _h(lang, "field.none")}</option>'
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
    notes = [field.help[lang]]
    if field.secret:
        notes.append(i18n.text(lang, "field.not_saved"))
    tip = _tip("\n\n".join(notes), i18n.text(lang, "field.about", label=label))
    if field.choice:
        # The choice in front of the field: `public` hides it (and the save
        # empties it), `private` shows it and requires it. A value already
        # saved means `private` was chosen.
        via = html.escape(steps_mod.via_name(field.key))
        chosen = "private" if value else "public"
        radios = "".join(
            f'<label class=opt><input type=radio name="{via}" value="{option}"'
            f'{" checked" if option == chosen else ""}> '
            f"{html.escape(field.choice[option][lang])}</label>"
            for option in steps_mod.CHOICES)
        return (f'<div class="field choice" data-key="{key}">{radios}'
                f'<div class=addr><div class=head><label for="{fid}">'
                f"{html.escape(label)}</label>{tip}</div>{control}</div></div>")
    optional = f' <span class=d>{_h(lang, "field.optional")}</span>' if field.optional else ""
    return (f'<div class=field data-key="{key}">'
            f'<div class=head><label for="{fid}">{html.escape(label)}</label>'
            f"{optional}{tip}</div>{control}</div>")


def _inventory_html(lang: str, names: list[str], current: str,
                    problem: tuple[str, str] | None) -> str:
    """The Inventory tab: open an inventory under the inventory root, one per
    line, or name a new one. `problem` is a translation key and the name it is
    about."""
    rows = "".join(
        f'<li><button type=submit name=open value="{html.escape(n)}">'
        f'{html.escape(n)}{" " + _h(lang, "inventory.open_now") if n == current else ""}'
        "</button></li>"
        for n in names)
    existing = (f"<form method=post action=/inventory><ul class=inv>{rows}</ul></form>"
                if names else f"<p class=doc>{_h(lang, 'inventory.none')}</p>")
    error = (f"<p class=bad>{_h(lang, problem[0], name=html.escape(repr(problem[1])))}</p>"
             if problem else "")
    return (
        f"<h1>{_h(lang, 'inventory.title')}</h1>"
        f"<p>{_h(lang, 'inventory.intro')}</p>"
        f"<p>{_h(lang, 'inventory.choose')}</p>"
        f"{existing}{error}"
        "<form method=post action=/inventory><div class=field><div class=head>"
        f"<label for=f-create>{_h(lang, 'inventory.new_name')}</label>"
        + _tip(i18n.text(lang, "inventory.name_tip"),
               i18n.text(lang, "inventory.name_tip_about"))
        + "</div><input type=text id=f-create name=create autocomplete=off></div>"
        f"<div class=act><button type=submit>{_h(lang, 'inventory.create')}</button>"
        "</div></form>")


def _checks_html(checks: list[steps_mod.Check], lang: str) -> str:
    """What the verification found, each check with the box that settles it
    when it has one."""
    if not checks:
        return ""
    rows = []
    for check in checks:
        cls = "ok" if check.ok else ("bad" if check.blocking else "warn")
        mark = "&#10003;" if check.ok else ("&#10007;" if check.blocking else "!")
        detail = f": {html.escape(check.detail)}" if check.detail else ""
        rows.append(f'<li class={cls}>{mark} {html.escape(check.label)}{detail}</li>')
        if check.confirm:
            name = html.escape(check.confirm)
            rows.append(f'<li><label><input type=checkbox name="{name}" value=yes> '
                        f"{_h(lang, f'confirm.{check.confirm}')}</label></li>")
    return f'<ul class=checks>{"".join(rows)}</ul>'


def _step_html(step: steps_mod.Step, values: dict[str, str], lang: str) -> str:
    """One section of the form: its explanation, its fields, and its note."""
    rows = "".join(_field_html(field, values[field.key], lang) for field in step.fields)
    note = (f"<p class=doc>{html.escape(step.note[lang])}</p>"
            if step.note else "")
    return (f'<section id="{html.escape(step.name)}">'
            f"<h2>{html.escape(step.title[lang])}</h2>"
            f"<p class=doc>{html.escape(step.doc[lang])}</p>{rows}{note}</section>")


def _process_html(current: run_mod.Run, checks: list[steps_mod.Check],
                  lang: str) -> str:
    """The button that verifies every section and starts the install, what the
    verification found, and the install's output.

    The log TAIL rather than a spinner. "Installing" answers nothing a client
    can act on, and the thing they want when it stops is which task failed --
    which would otherwise only exist in a console they may have closed.
    """
    status = {
        run_mod.STATE_ANSWERING: "",
        run_mod.STATE_INSTALLING: _h(lang, "process.installing"),
        run_mod.STATE_DONE: _h(lang, "process.done"),
        run_mod.STATE_FAILED: _h(lang, "process.failed"),
    }[current.state]
    if current.log:
        lines = html.escape("\n".join(current.log))
    elif current.state == run_mod.STATE_ANSWERING:
        lines = _h(lang, "process.empty")
    else:
        lines = _h(lang, "process.reopened")
    issues = f'<a href="{ISSUES_URL}">{ISSUES_URL}</a>'
    return (
        f"<section id=install><h2>{_h(lang, 'process.title')}</h2>"
        f"<p class=doc>{_h(lang, 'process.intro', issues=issues)}</p>"
        "<div class=act><button type=submit name=install value=yes>"
        f"{_h(lang, 'process.button')}</button></div>"
        f"{_checks_html(checks, lang)}"
        f'{f"<p class=doc>{status}</p>" if status else ""}'
        f'<pre id=log class=log data-state="{html.escape(current.state)}">'
        f"{lines}</pre></section>")


def keyset_shown(current: run_mod.Run, lang: str) -> dict[str, str]:
    """What the access section shows for each password: the value, or why there
    is none to show."""
    if not current.keyset:
        return {name: i18n.text(lang, "keyset.pending") for name in KEYSET_FIELDS}
    return {name: current.keyset.get(name) or i18n.text(lang, "keyset.not_again")
            for name in KEYSET_FIELDS}


def _link(url: str) -> str:
    """A link that opens beside the installer page."""
    return f'<a href="{html.escape(url)}" target=_blank>{html.escape(url)}</a>'


def _access_html(current: run_mod.Run, values: dict[str, str], lang: str) -> str:
    """The passwords the install printed, and the way into the panel: the
    forward this process holds open, or the button that opens it."""
    shown = keyset_shown(current, lang)
    rows = []
    for name in KEYSET_FIELDS:
        label = i18n.text(lang, f"keyset.{name}")
        about = i18n.TEXT[lang].get(f"keyset.{name}.about")
        tip = (" " + _tip(about, i18n.text(lang, "field.about", label=label))
               if about else "")
        rows.append(f"<dt>{html.escape(label)}{tip}</dt>"
                    f'<dd><code id="k-{name}">{html.escape(shown[name])}</code></dd>')
    forward = current.forward
    if isinstance(forward, remote.Forward):
        panel = _link(forward.url(install.PANEL_PORT))
        portainer = _link(forward.url(install.PORTAINER_PORT))
        way_in = (f"<p>{_h(lang, 'access.panel', url=panel)}</p>"
                  f"<p>{_h(lang, 'access.portainer', url=portainer)}</p>"
                  f"<p class=doc>{_h(lang, 'access.forward_open')}</p>")
    else:
        way_in = (
            "<form method=post action=/connect><div class=act><button type=submit>"
            f"{_h(lang, 'access.connect')}</button></div></form>"
            f"<p class=doc>{_h(lang, 'access.connect_about')}</p>")
    return (
        f"<section id=access><h2>{_h(lang, 'access.title')}</h2>"
        f"<p>{_h(lang, 'access.save')}</p>"
        f"<dl class=keys>{''.join(rows)}</dl>"
        f"{way_in}"
        f"<p class=doc>{_h(lang, 'access.forward')}</p>"
        f"<pre>{html.escape(steps_mod.forward_command(values))}</pre>"
        f"<p>{_h(lang, 'access.settings')}</p></section>")


def open_forward(current: run_mod.Run) -> str:
    """Open the panel forward for the run's server, unless one is open: ""
    when it is, else why it could not be."""
    if isinstance(current.forward, remote.Forward):
        return ""
    try:
        current.forward = install.connect(current.path)
    except (remote.RemoteError, OSError) as exc:
        return str(exc)
    return ""


def start_install(current: run_mod.Run, reinstalled: bool = False) -> threading.Thread:
    """Run the install in the background (install.run), its output to the
    console window and to the run's in-memory tail. `reinstalled` is the
    client's word that the server was reinstalled (steps.REINSTALLED).

    IN A THREAD, because the install takes tens of minutes and the browser
    cannot hold a request open for it -- and because the console process owns
    the job either way. The page polls the tail; closing the tab does not stop
    the install, and neither does losing the network the browser is on.

    Nothing it prints is written to a file: it carries the passwords the
    server shows once. The server prints them as JSON between its marker
    lines; they go to the run's keyset, which the page shows in its own
    section, and not to the console window. Every other line goes to both. The
    provider's password and an admin password pin go from memory into the
    install, never to a file here.

    When it ends well the panel forward opens, so the page's links work.
    """
    current.state = run_mod.STATE_INSTALLING
    current.log.clear()
    current.save()
    secrets = dict(current.secrets)
    opts = install.Options(
        reinstalled=reinstalled, keyset_json=True,
        password=secrets.pop(steps_mod.PROVIDER_PASSWORD, ""),
        adopt={k: v for k, v in secrets.items() if v.strip()})

    def line(text: str) -> None:
        text = _ANSI.sub("", text)
        sys.stderr.write(text + "\n")
        current.log.append(text)

    def keyset(block: list[str]) -> None:
        if block:
            current.keyset = {str(k): str(v) for k, v in json.loads(block[0]).items()}
        sys.stderr.write("catena-installer: the passwords are shown in the browser\n")

    def body() -> None:
        try:
            rc = install.run(current.path, opts, install.Output(line, keyset))
        except remote.RemoteError as exc:
            line(f"catena-installer: {exc}")
            rc = 1
        current.state = run_mod.STATE_DONE if rc == 0 else run_mod.STATE_FAILED
        current.save()
        if rc == 0:
            problem = open_forward(current)
            if problem:
                line(f"catena-installer: the panel forward did not open: {problem}")

    thread = threading.Thread(target=body, daemon=True)
    thread.start()
    return thread


class _Handler(http.server.BaseHTTPRequestHandler):
    # Filled in by serve(). Class attributes rather than constructor arguments
    # because BaseHTTPRequestHandler constructs one instance per request.
    # `run` is None until an inventory is opened or created.
    run: run_mod.Run | None = None
    doc: dict
    inventory_root: Path
    all_steps: list[steps_mod.Step]
    last_checks: list[steps_mod.Check] = []
    # A translation key and the name it is about, shown once on the Inventory tab.
    inventory_problem: tuple[str, str] | None = None

    def log_message(self, *_args) -> None:
        """Quiet. The console prints what the install is doing; a request log
        would bury it under one line per form field."""

    def _lang(self) -> str:
        cookie = http.cookies.SimpleCookie(self.headers.get("Cookie") or "")
        chosen = cookie[LANG_COOKIE].value if LANG_COOKIE in cookie else ""
        return i18n.resolve(cookie=chosen,
                            accept_language=self.headers.get("Accept-Language") or "")

    def _send(self, body: bytes, status: int = 200,
              content_type: str = "text/html; charset=utf-8") -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _redirect(self, where: str, cookie: str = "") -> None:
        self.send_response(303)
        self.send_header("Location", where)
        if cookie:
            self.send_header("Set-Cookie", cookie)
        self.end_headers()

    def _not_found(self, lang: str) -> None:
        title = i18n.text(lang, "page.not_found")
        self._send(_page(lang, title, f"<main><h1>{html.escape(title)}</h1></main>"), 404)

    def do_GET(self) -> None:  # noqa: N802 -- the stdlib's spelling
        url = urllib.parse.urlparse(self.path)
        path = url.path
        chosen = (urllib.parse.parse_qs(url.query).get("lang") or [""])[0]
        if chosen in i18n.LANGS:
            # The switcher: remember the choice, and show the same page in it.
            self._redirect(path, f"{LANG_COOKIE}={chosen}; Path=/; "
                                 "SameSite=Strict; Max-Age=31536000")
            return
        lang = self._lang()
        if path == "/":
            if self.run is None:
                self._redirect("/inventory")
                return
            self._render_install(lang)
            return
        if path == "/inventory":
            self._render_inventory(lang)
            return
        if path == "/progress" and self.run is not None:
            # What the installation page's poll reads while the install runs.
            self._send(json.dumps({"state": self.run.state,
                                   "log": "\n".join(self.run.log),
                                   "keyset": keyset_shown(self.run, lang)}).encode("utf-8"),
                       content_type="application/json")
            return
        self._not_found(lang)

    def _form(self) -> dict[str, list[str]]:
        length = int(self.headers.get("Content-Length") or 0)
        return urllib.parse.parse_qs(self.rfile.read(length).decode("utf-8"),
                                     keep_blank_values=True)

    def _render_inventory(self, lang: str) -> None:
        current = self.run.inventory if self.run else ""
        names = run_mod.seed().inventories(self.inventory_root)
        body = (f"{_nav(lang, current, on='inventory')}<main>"
                f"{_inventory_html(lang, names, current, _Handler.inventory_problem)}"
                "</main>")
        _Handler.inventory_problem = None
        self._send(_page(lang, i18n.text(lang, "inventory.title"), body))

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
        seed = run_mod.seed()
        name = (form.get("open") or [""])[0].strip()
        if name:
            if name not in seed.inventories(self.inventory_root):
                _Handler.inventory_problem = ("inventory.problem.unknown", name)
                self._redirect("/inventory")
                return
            self._open(name)
            return
        name = (form.get("create") or [""])[0].strip()
        problem = seed.inventory_name_problem(name, self.inventory_root)
        if problem:
            _Handler.inventory_problem = (f"inventory.problem.{problem}", name)
            self._redirect("/inventory")
            return
        self._open(name)

    def do_POST(self) -> None:  # noqa: N802
        """Save the form, verify every section, and start the install when none
        of them blocks. The answers are saved whatever the verification finds,
        so a closed installer reopens on them."""
        lang = self._lang()
        path = urllib.parse.urlparse(self.path).path
        if path == "/inventory":
            self._post_inventory()
            return
        if path not in ("/", "/connect"):
            self._not_found(lang)
            return
        if self.run is None:
            self._redirect("/inventory")
            return
        if path == "/connect":
            # The way into an installed server's panel, without installing.
            problem = open_forward(self.run)
            if problem:
                self.run.log.append(f"catena-installer: the panel forward did "
                                    f"not open: {problem}")
            self._redirect("/#access")
            return
        if self.run.state == run_mod.STATE_INSTALLING:
            # The answers went to an install that is running; an edit now
            # would describe a host nobody is building.
            self._redirect("/#install")
            return
        form = self._form()
        private: set[str] = set()
        for step in self.all_steps:
            for field in step.fields:
                if field.key not in form:
                    continue
                value = form[field.key][0]
                if field.choice:
                    via = (form.get(steps_mod.via_name(field.key)) or ["public"])[0]
                    if via == "private":
                        private.add(field.key)
                    else:
                        value = ""
                self.run.answer(field.key, value, secret=field.secret)
        values = self._shown_values()
        reinstalled = (form.get(steps_mod.REINSTALLED) or [""])[0] == "yes"
        key = steps_mod.key_path(self.run.answers)
        if (form.get(steps_mod.CREATE_KEY) or [""])[0] == "yes" and key and not key.exists():
            try:
                keys.create(key)
            except keys.KeyPairError as exc:
                self.run.log.append(f"catena-installer: {exc}")
        checks: list[steps_mod.Check] = []
        for step in self.all_steps:
            missing = steps_mod.missing_required(step, values, lang, frozenset(private))
            checks += missing or steps_mod.validate(
                step.name, self.run.answers, self.run.secrets, lang, reinstalled)
        _Handler.last_checks = checks
        self.run.save()
        if not steps_mod.blocked(checks):
            start_install(self.run, reinstalled)
        self._redirect("/#install")

    def _shown_values(self) -> dict[str, str]:
        """What each field shows: the answer, else the default, else for a
        choice the first option, which is what a select with no match shows."""
        return {f.key: (self.run.value(f.key) or f.default
                        or (f.options[0] if f.options else ""))
                for s in self.all_steps for f in s.fields}

    def _render_install(self, lang: str) -> None:
        installing = self.run.state == run_mod.STATE_INSTALLING
        values = self._shown_values()
        sections = "".join(_step_html(s, values, lang) for s in self.all_steps)
        title = i18n.text(lang, "install.title", name=self.run.inventory)
        saved_to = f"<code>{html.escape(str(self.run.path))}</code>"
        body = (
            f"{_nav(lang, self.run.inventory, on='install')}<main>"
            f"<h1>{html.escape(title)}</h1>"
            f"<p class=doc>{_h(lang, 'install.saved_to', path=saved_to)}</p>"
            f'<form method=post action="/"><fieldset{" disabled" if installing else ""}>'
            f"{sections}{_process_html(self.run, _Handler.last_checks, lang)}"
            "</fieldset></form>"
            f"{_access_html(self.run, values, lang)}</main>{_POLL}")
        self._send(_page(lang, title, body))


def serve(current: run_mod.Run | None, doc: dict, *, port: int,
          inventory_root: Path) -> int:
    """Serve until the console process is stopped. With no `current` run the
    first tab is the inventory picker.

    ThreadingHTTPServer so a probe that takes ten seconds -- an SSH login
    waiting on a server still booting -- does not make the rest of the UI look
    hung.
    """
    _Handler.run = current
    _Handler.doc = doc
    _Handler.inventory_root = inventory_root
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
