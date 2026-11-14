# Reconcile roles

The roles a server runs against ITSELF, unattended, from the tree inside
the panel image. Everything they need comes from that image or from the
on-box store at `/etc/catena/config.json`; none of them reads the
operator's inventory.

`../../playbooks/reconcile.yml` runs exactly this list, in this order,
and `../../tests/unit/test_converge_boundary.py` holds the playbook and
`../../boundary.yml` to each other.

Each role has its own `README.md`, and the leading comment of its
`tasks/main.yml` says what it does in one line.
