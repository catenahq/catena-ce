"""helpers/tree_hash.py: a catena-ce tree named by what an image ships of it.

catena-admin's vendor step copies the files this lists and records the hash it
computes; reconcile/roles/payload compares the tree running a converge with
that record. The tests build a throwaway git checkout and a staged tree.

Run: uv run pytest tests/unit/test_tree_hash.py
"""
from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

ANSIBLE_DIR = Path(__file__).resolve().parents[2]
if str(ANSIBLE_DIR) not in sys.path:
    sys.path.insert(0, str(ANSIBLE_DIR))

from helpers import tree_hash  # noqa: E402

TRACKED = {
    "README.md": "outside ansible/\n",
    "ansible/ansible.cfg": "[defaults]\n",
    "ansible/playbooks/reconcile.yml": "---\n- hosts: all\n",
    "ansible/reconcile/roles/r/tests/kept.yml": "---\n",
    "ansible/tests/unit/test_x.py": "def test(): pass\n",
}


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(root), "-c", "user.name=t",
                    "-c", "user.email=t@example.com", *args],
                   check=True, capture_output=True)


@pytest.fixture
def checkout(tmp_path: Path) -> Path:
    root = tmp_path / "catena-ce"
    for rel, body in TRACKED.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(body)
    _git(root.parent, "init", "-q", str(root))
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "fixture")
    return root


def _expected(root: Path, paths: list[str]) -> str:
    lines = "".join(
        f"{p}\0{hashlib.sha256((root / p).read_bytes()).hexdigest()}\n"
        for p in sorted(paths))
    return hashlib.sha256(lines.encode()).hexdigest()


def test_an_image_ships_what_git_tracks_under_ansible_minus_its_tests(checkout):
    """A deeper tests/ directory belongs to a role and ships; only the suite
    at ansible/tests/ does not, and nothing outside ansible/ does."""
    (checkout / "ansible" / "inventory" / "prod").mkdir(parents=True)
    (checkout / "ansible" / "inventory" / "prod" / ".env").write_text("X=1\n")
    assert tree_hash.shipped_files(checkout) == [
        "ansible/ansible.cfg",
        "ansible/playbooks/reconcile.yml",
        "ansible/reconcile/roles/r/tests/kept.yml",
    ]


def test_the_hash_is_one_line_per_shipped_file_sorted_by_path(checkout):
    got = tree_hash.identify(checkout)["tree_sha256"]
    assert got == _expected(checkout, tree_hash.shipped_files(checkout))


def test_an_uncommitted_edit_is_a_different_tree(checkout):
    """The vendor step copies the bytes on disk, so the bench's image of a
    working tree carries its uncommitted edits, and the hash has to as well."""
    before = tree_hash.identify(checkout)["tree_sha256"]
    (checkout / "ansible" / "ansible.cfg").write_text("[defaults]\nforks = 5\n")
    assert tree_hash.identify(checkout)["tree_sha256"] != before


def test_files_an_image_does_not_ship_do_not_change_the_hash(checkout):
    before = tree_hash.identify(checkout)["tree_sha256"]
    (checkout / "ansible" / "untracked.yml").write_text("---\n")
    (checkout / "ansible" / "tests" / "unit" / "test_x.py").write_text("# edited\n")
    (checkout / "README.md").write_text("edited\n")
    assert tree_hash.identify(checkout)["tree_sha256"] == before


def test_a_tracked_file_deleted_from_the_working_tree_does_not_ship(checkout, capsys):
    """The bench builds images from working trees. A deletion not yet
    committed is left out of the list the vendor step copies and of the hash
    both sides compute, which is the hash of the tree once it is committed."""
    (checkout / "ansible" / "ansible.cfg").unlink()

    assert tree_hash.main(["--files", str(checkout)]) == 0
    assert capsys.readouterr().out.split("\0")[:-1] == [
        "ansible/playbooks/reconcile.yml",
        "ansible/reconcile/roles/r/tests/kept.yml",
    ]
    assert tree_hash.main(["--sha256", str(checkout)]) == 0
    working = capsys.readouterr().out.strip()

    _git(checkout, "commit", "-q", "-am", "delete")
    assert working == tree_hash.identify(checkout)["tree_sha256"]


def test_a_checkout_is_described_the_way_the_version_stamp_is(checkout):
    _git(checkout, "tag", "v1.2.3")
    assert tree_hash.identify(checkout)["describe"] == "v1.2.3"


def test_a_staged_tree_is_named_by_the_record_it_came_with(tmp_path):
    """A tree copied out of an image is not a checkout and carries no file
    list; the image's own record names it."""
    (tmp_path / "ansible").mkdir()
    (tmp_path / "VENDOR.json").write_text(json.dumps(
        {"describe": "v1.2.3", "tree_sha256": "ab" * 32, "commit": "c0ffee"}))
    assert tree_hash.identify(tmp_path) == {
        "tree_sha256": "ab" * 32, "describe": "v1.2.3"}


def test_a_record_from_before_the_hash_names_no_tree(tmp_path):
    (tmp_path / "VENDOR.json").write_text(json.dumps({"describe": "v0.8.1"}))
    assert tree_hash.identify(tmp_path)["tree_sha256"] == ""


def test_the_command_line_serves_the_vendor_step_and_the_role(checkout, capsys):
    assert tree_hash.main(["--files", str(checkout)]) == 0
    assert capsys.readouterr().out.split("\0")[:-1] == tree_hash.shipped_files(checkout)

    assert tree_hash.main(["--sha256", str(checkout)]) == 0
    assert capsys.readouterr().out.strip() == tree_hash.identify(checkout)["tree_sha256"]

    assert tree_hash.main([str(checkout)]) == 0
    assert json.loads(capsys.readouterr().out) == tree_hash.identify(checkout)


def test_a_directory_that_is_neither_fails_with_a_message(tmp_path, capsys):
    assert tree_hash.main([str(tmp_path)]) == 1
    assert "cannot name the tree" in capsys.readouterr().err
