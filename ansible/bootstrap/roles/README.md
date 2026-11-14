# Bootstrap roles

The roles only the install applies, run by a person through the
installer and never by the host on a timer. A role is here when getting
it wrong would leave nothing on the box able to fix it -- the account
and the packages, the mesh the operator arrives over, the disk the data
sits on, the container engine, the account and key the panel dispatches
with, and the ansible-core the server reconciles itself with.

The line is declared in `../../boundary.yml` and enforced by
`../../tests/unit/test_converge_boundary.py`. The invariant that keeps
the two sides apart: a reconcile may not modify anything that would
remove the ability to run a reconcile.
