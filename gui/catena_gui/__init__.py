"""The graphical installer for Catena.

WHERE IT RUNS. On the client's own machine, as a console process serving a
browser UI on loopback. Whoever runs Ansible holds the SSH private key and
every vendor credential, so a hosted installer would make the operator
custodian of every client's cloud accounts -- which inverts the whole point of
a client-owned configuration.

THE CONSOLE OWNS THE JOB; THE BROWSER IS A VIEW. Closing the browser changes
nothing. Closing the console abandons the run, and the run directory says how
to resume. That is structural rather than cosmetic: an install contains two
unbounded waits -- a server being delivered, and a domain being activated at
its registrar -- and neither fits inside a page load.

IT HOLDS NO KNOB KNOWLEDGE. Which questions each page asks comes from
catena-ce/ansible/helpers/knobs.json, the same registry the on-box store, the
installer and the settings page read. That is what makes "the doors cannot
drift" true rather than aspirational: adding a knob there puts it here.

IT ADDS NO MIDDLE LAYER. `install.yaml` plus `catena install -i ... --no-confirm`
is already the declarative, non-interactive contract, and seed.py already
live-probes credentials. This is a THIRD producer of that contract, beside a
person writing the file and the test bench rendering it.
"""

__all__ = ["registry", "run", "steps"]
