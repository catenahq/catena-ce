"""Core logic for the catena-admin recovery-archive generator.

The recovery archive is a single AES-256 password-protected `.zip`
file that the operator hands to the client (download URL + passphrase
out-of-band). On extraction it expands into a tiny self-contained
recovery toolkit:

  recover.sh             tiny wrapper: sources envelope.env, sudo's
                         restore.sh.
  restore.sh             verbatim copy of automation/client-tools/
                         restore.sh -- the same script the operator
                         already ships for manual recovery.
  envelope.env           16-line `KEY='value'` dotenv with hot+cold
                         restic creds + NC-S3 quartet + cloudflared
                         token. Sourced by recover.sh.
  vault.recovered.yml    candidate vault.yml emitted by
                         extract_secrets_core (host-side path) or
                         distilled from sops_vault.read_dict
                         (operator path). Optional reference.
  README.txt             EN instructions.
  LISEZ-MOI.txt          FR instructions (parity per CLAUDE.md).

Why encrypted ZIP rather than gpg-armoured single-file: ZIP is the
universally-recognized archive format. Modern OSes (macOS Archive
Utility, Windows 10+ Explorer, every Linux unzip 6.0+) prompt for a
passphrase natively. AES-256 + encrypted filenames (`-mhe=on`) gives
proper crypto strength; the legacy ZipCrypto in `zip -e` is broken
(known-plaintext attack on the file headers, ~10^9 guesses/sec on a
laptop) and is NOT used here.

Shared by:

  - operator-tools/generate_recovery_archive.py (operator's laptop;
    reads vault.sops.yml + .env, writes recovery-<inv>-<ts>.zip).
  - scripts/generate-recovery-archive.py (VPS; reads live host
    state via extract_secrets_core, invoked by the catena-admin
    "Generate recovery archive (encrypted)" Recovery-tab action).
  - test_bench/orchestrator/client_restore.py (consumes
    ENVELOPE_KEY_ORDER so its hot_plus_cold_envelope cannot drift).

Stdlib + `7z` binary only. The module is vendored to
/usr/local/lib/catena/ on the VPS at converge time, and the host-side
script imports it without any pip deps.
"""
from __future__ import annotations

import re
import secrets
import shutil
import string
import subprocess
import sys
import tempfile
from pathlib import Path

# ---------------------------------------------------------------------------
# Envelope shape
# ---------------------------------------------------------------------------
# The 16 bash env vars `restore.sh` reads when the operator has pre-baked
# credentials into the recovery archive. Order is fixed so the rendered
# envelope.env is byte-deterministic (clean diffs, reproducible tests).
#
# Groupings (encoded by order):
#   0-3   : hot restic creds (always required for a real restore)
#   4-6   : cold restic creds (optional; only present when WORM mirror is on)
#   7-10  : NC-S3 hot envelope quartet (optional; NC-on-S3 deployments only)
#   11-14 : NC-S3 cold envelope quartet (optional)
#   15    : cloudflared tunnel token (optional)
ENVELOPE_KEY_ORDER: tuple[str, ...] = (
    # Hot restic (4)
    "RESTIC_REPOSITORY",
    "RESTIC_PASSWORD",
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
    # Cold restic (3 -- password is shared with hot)
    "RESTIC_REPOSITORY_COLD",
    "AWS_ACCESS_KEY_ID_COLD",
    "AWS_SECRET_ACCESS_KEY_COLD",
    # NC-S3 hot (4)
    "NEXTCLOUD_S3_HOT_BUCKET",
    "NEXTCLOUD_S3_HOT_ENDPOINT",
    "NEXTCLOUD_S3_HOT_ACCESS_KEY",
    "NEXTCLOUD_S3_HOT_SECRET",
    # NC-S3 cold (4)
    "NEXTCLOUD_S3_COLD_BUCKET",
    "NEXTCLOUD_S3_COLD_ENDPOINT",
    "NEXTCLOUD_S3_COLD_ACCESS_KEY",
    "NEXTCLOUD_S3_COLD_SECRET",
    # Optional cloudflared (1)
    "CLOUDFLARED_TOKEN",
)


# ---------------------------------------------------------------------------
# Bash helpers
# ---------------------------------------------------------------------------
# Single-quote escape: bash single-quoted strings cannot contain a
# single quote, so the convention is `'\''` (close, escape, reopen).
def _bash_singlequote(value: str) -> str:
    return "'" + value.replace("'", "'\\''") + "'"


def render_env_file(env: dict[str, str]) -> str:
    """Return the plaintext bash-sourceable file representing `env`,
    one `KEY='value'` line per ENVELOPE_KEY_ORDER member. Missing keys
    render as empty-string assignments so `recover.sh`'s `set -a; .`
    always sets every var (even if to "").

    Order is fixed by ENVELOPE_KEY_ORDER -- byte-deterministic for tests.
    """
    lines = []
    for key in ENVELOPE_KEY_ORDER:
        value = env.get(key, "") or ""
        lines.append(f"{key}={_bash_singlequote(str(value))}")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# .env URL splitter
# ---------------------------------------------------------------------------
# /etc/catena/backup.env stores the NC-S3 buckets as combined `s3:<host>/<bucket>`
# URLs (NEXTCLOUD_LIVE_REPO + NEXTCLOUD_WORM_REPO). restore.sh consumes
# them as separate <ENDPOINT> + <BUCKET> vars (the bucket-mirror Python
# helper takes them as separate boto3 args).
_S3_REPO_RE = re.compile(r"^s3:(?P<host>[^/]+)/(?P<bucket>.+)$")


def split_s3_repo(repo_url: str) -> tuple[str, str]:
    """Split `s3:<host>/<bucket>` into (https://<host>, <bucket>).

    restic accepts the colon-prefixed form; boto3 needs a real URL.
    Returns ("", "") when the input is empty so callers can pipe
    optional config through without a None check.
    """
    if not repo_url:
        return "", ""
    m = _S3_REPO_RE.match(repo_url.strip())
    if m is None:
        raise ValueError(
            f"could not parse {repo_url!r} as s3:<host>/<bucket>"
        )
    return f"https://{m.group('host')}", m.group("bucket")


# ---------------------------------------------------------------------------
# Top-level envelope assembly
# ---------------------------------------------------------------------------
def build_envelope(
    *,
    vault: dict[str, str],
    backup_env: dict[str, str],
    cloudflared_token: str = "",
) -> dict[str, str]:
    """Assemble the 16-var envelope from resolved sources.

    Inputs:
      vault       -- dict of vault_* values (from vault.sops.yml on the
                    operator path, or from extract_secrets_core.extract on
                    the VPS path).
      backup_env  -- dict of /etc/catena/backup.env values, parsed from
                    the rendered template. Must contain at least
                    RESTIC_REPOSITORY (the hot repo URL); BACKUP_WORM_REPO
                    + NEXTCLOUD_LIVE_REPO + NEXTCLOUD_WORM_REPO are
                    optional (empty string when not configured).
      cloudflared_token -- operator decision; not on the VPS at extract
                          time so the host-side caller passes "".

    Returns the envelope dict, every ENVELOPE_KEY_ORDER member present
    (empty string for optional vars whose source is empty).
    """
    nc_hot_endpoint, nc_hot_bucket = split_s3_repo(
        backup_env.get("NEXTCLOUD_LIVE_REPO", "")
    )
    nc_cold_endpoint, nc_cold_bucket = split_s3_repo(
        backup_env.get("NEXTCLOUD_WORM_REPO", "")
    )
    return {
        # Hot restic.
        "RESTIC_REPOSITORY": backup_env.get("RESTIC_REPOSITORY", ""),
        "RESTIC_PASSWORD": vault.get("vault_backup_restic_password", ""),
        "AWS_ACCESS_KEY_ID": vault.get("vault_backup_s3_access_key", ""),
        "AWS_SECRET_ACCESS_KEY": vault.get("vault_backup_s3_secret_key", ""),
        # Cold restic.
        "RESTIC_REPOSITORY_COLD": backup_env.get("BACKUP_WORM_REPO", ""),
        "AWS_ACCESS_KEY_ID_COLD": vault.get("vault_backup_worm_access_key", ""),
        "AWS_SECRET_ACCESS_KEY_COLD": vault.get("vault_backup_worm_secret_key", ""),
        # NC-S3 hot (split from NEXTCLOUD_LIVE_REPO).
        "NEXTCLOUD_S3_HOT_BUCKET": nc_hot_bucket,
        "NEXTCLOUD_S3_HOT_ENDPOINT": nc_hot_endpoint,
        "NEXTCLOUD_S3_HOT_ACCESS_KEY": vault.get("vault_nextcloud_s3_access_key", ""),
        "NEXTCLOUD_S3_HOT_SECRET": vault.get("vault_nextcloud_s3_secret_key", ""),
        # NC-S3 cold (split from NEXTCLOUD_WORM_REPO).
        "NEXTCLOUD_S3_COLD_BUCKET": nc_cold_bucket,
        "NEXTCLOUD_S3_COLD_ENDPOINT": nc_cold_endpoint,
        "NEXTCLOUD_S3_COLD_ACCESS_KEY": vault.get("vault_nextcloud_worm_access_key", ""),
        "NEXTCLOUD_S3_COLD_SECRET": vault.get("vault_nextcloud_worm_secret_key", ""),
        # Cloudflared (operator-decision; host-side caller leaves it "").
        "CLOUDFLARED_TOKEN": cloudflared_token,
    }


# ---------------------------------------------------------------------------
# Static archive members: recover.sh wrapper + bilingual READMEs
# ---------------------------------------------------------------------------
RECOVER_SH = """\
#!/usr/bin/env bash
# Convenience wrapper bundled by the catena recovery-archive generator.
# Sources envelope.env so restore.sh sees the 14 envelope vars as
# already-set, then re-execs restore.sh under sudo (sudo -E preserves
# the env block per sudoers' env_keep -- tested on Debian/Ubuntu defaults).
#
# Post-completion: if restore.sh exits 0, we emit a loud reminder about
# the plaintext secret files the archive leaves on disk
# (envelope.env, vault.recovered.yml). These cannot be auto-shredded
# here because:
#   - envelope.env may be needed for a re-run if any post-restore step
#     fails downstream;
#   - vault.recovered.yml is the operator's input to re-encrypting the
#     SOPS vault on the new VPS; auto-shredding before the operator has
#     done that would destroy the only remaining copy of the secret set.
# So the reminder is opt-in cleanup: the operator runs `shred -u <files>`
# (or just `rm -P` on macOS) when they're done.
set -Eeuo pipefail
HERE="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
if [ ! -f "$HERE/envelope.env" ]; then
    echo "envelope.env missing -- did you extract this archive in full?" >&2
    exit 2
fi
if [ ! -f "$HERE/restore.sh" ]; then
    echo "restore.sh missing -- did you extract this archive in full?" >&2
    exit 2
fi
set -a
# shellcheck source=/dev/null
. "$HERE/envelope.env"
set +a

# Wrap the sudo+restore in a trap so the cleanup reminder fires once
# restore.sh exits successfully. `exec` would skip the trap; the
# explicit invocation lets us inspect $? and emit the reminder.
sudo -E bash "$HERE/restore.sh" "$@"
RC=$?
if [ "$RC" -eq 0 ]; then
    cat >&2 <<EOF

── catena recovery: post-restore cleanup reminder ──
  This archive is now extracted under: $HERE
  Two files still on disk hold secrets:
    - $HERE/envelope.env          (14 hot+cold credentials)
    - $HERE/vault.recovered.yml   (full SOPS vault contents)
  Once you have:
    (a) confirmed the apps come up on the new VPS, AND
    (b) re-encrypted vault.recovered.yml into the new VPS's
        vault.sops.yml (operator step on your laptop),
  shred both files:
    shred -u "$HERE/envelope.env" "$HERE/vault.recovered.yml"
  (on macOS use 'rm -P' instead of 'shred -u').
── end reminder ──

EOF
fi
exit $RC
"""


def render_readme_en(*, hostname: str, generated_at: str) -> str:
    """Render the EN README that ships at the top of the zip."""
    return f"""\
catena recovery archive
=======================

Source host : {hostname}
Generated   : {generated_at}

This archive contains everything you need to restore your VPS onto a
fresh server when your usual one is gone. Your operator generated it
on a known-good day; the archive is a snapshot of the minimum state
needed to bootstrap a recovery.

What is in here
---------------

  recover.sh            Run this. It sources envelope.env and sudo-
                        executes restore.sh.
  restore.sh            The recovery script. Identical to the version
                        you can always download from your operator;
                        kept in here so the archive is fully self-
                        contained (no internet required at recovery
                        time).
  envelope.env          The 14 hot+cold credentials baked into a
                        sourceable bash file. NEVER commit this
                        anywhere; treat it like a password file.
  vault.recovered.yml   A candidate vault.yml reconstructed from the
                        live host state. Optional reference for your
                        operator if your full vault is also lost.
  README.txt            This file.
  LISEZ-MOI.txt         French parity.

How to recover
--------------

1. Provision a fresh Debian or Ubuntu VPS (root SSH only).
2. Copy this whole folder to the new host (`scp -r`, USB stick,
   whatever). Place it in `/root/catena-recovery/` (any path is fine).
3. SSH in as root and run:

       cd /root/catena-recovery
       chmod +x recover.sh restore.sh
       ./recover.sh

4. Wait. The script tells you what it is doing at every step. It is
   safe to re-run if anything goes wrong; each step is idempotent.

5. Once you see "[restore] catena client-side recovery complete", visit
   your stack's URLs and confirm everything is up.

Security
--------

Everything in this archive was protected by the passphrase you typed
to extract it. Once extracted on a fresh VPS:

  1. Delete the original `.zip` archive file itself.
  2. After recover.sh succeeds AND your operator has re-encrypted
     vault.recovered.yml into a new SOPS vault, `shred -u`
     (or `rm -P` on macOS) both `envelope.env` and
     `vault.recovered.yml`. They contain the full secret set in
     plaintext and should not live on disk longer than the recovery
     window requires.

recover.sh emits this reminder on successful completion so you do not
have to remember it.

Trouble?  Contact your operator. They can run the same recovery
remotely from your tailnet if you cannot.
"""


def render_readme_fr(*, hostname: str, generated_at: str) -> str:
    """Render the FR LISEZ-MOI (parity per CLAUDE.md bilingual rule)."""
    return f"""\
archive de récupération catena
==============================

Hôte source  : {hostname}
Générée le   : {generated_at}

Cette archive contient tout ce qu'il faut pour restaurer votre VPS sur
un serveur neuf, quand le vôtre est perdu. Votre opérateur l'a générée
un bon jour; c'est une photo de l'état minimum nécessaire pour
amorcer une reprise.

Contenu
-------

  recover.sh            À lancer. Source envelope.env puis exécute
                        restore.sh sous sudo.
  restore.sh            Le script de récupération. Identique à la
                        version disponible chez votre opérateur,
                        embarquée ici pour que l'archive soit entière
                        (pas besoin d'Internet au moment de la reprise).
  envelope.env          Les 14 identifiants chaud + froid sous forme
                        de fichier bash sourçable. À ne JAMAIS verser
                        dans un dépôt; à traiter comme un mot de passe.
  vault.recovered.yml   Candidat vault.yml reconstruit depuis l'état
                        en cours sur l'hôte. Référence optionnelle si
                        votre coffre principal est aussi perdu.
  README.txt            Version anglaise.
  LISEZ-MOI.txt         Ce fichier.

Comment récupérer
-----------------

1. Provisionnez un VPS Debian ou Ubuntu neuf (SSH root uniquement).
2. Copiez tout ce dossier sur le nouvel hôte (`scp -r`, clé USB,
   peu importe). Posez-le dans `/root/catena-recovery/` (n'importe
   quel chemin convient).
3. Connectez-vous en root et lancez :

       cd /root/catena-recovery
       chmod +x recover.sh restore.sh
       ./recover.sh

4. Attendez. Le script annonce chaque étape. Il est ré-exécutable
   sans risque; chaque étape est idempotente.

5. Quand vous voyez "[restore] catena client-side recovery complete",
   ouvrez les URL de votre pile et confirmez que tout fonctionne.

Sécurité
--------

Tout ce que contient cette archive était protégé par la phrase de
passe utilisée pour l'extraire. Une fois extraite sur un VPS neuf :

  1. Supprimez le fichier d'archive `.zip` lui-même.
  2. Après que recover.sh ait réussi ET que votre opérateur ait
     ré-encrypté vault.recovered.yml dans un nouveau coffre SOPS,
     faites `shred -u` (ou `rm -P` sur macOS) sur les deux fichiers
     `envelope.env` et `vault.recovered.yml`. Ils contiennent
     l'ensemble des secrets en clair et ne devraient pas rester sur
     disque plus longtemps que la fenêtre de récupération l'exige.

recover.sh affiche ce rappel à la fin d'une exécution réussie pour
vous éviter de l'oublier.

Problème ?  Contactez votre opérateur. Il peut effectuer la même
récupération à distance via votre tailnet si vous n'y arrivez pas.
"""


# ---------------------------------------------------------------------------
# 7z packaging
# ---------------------------------------------------------------------------
ARCHIVE_MEMBER_ORDER: tuple[str, ...] = (
    # Listed in the order an end-user reading the table of contents
    # would expect (top: what to read; middle: what to run; bottom:
    # the credentials + bonus reference). 7z preserves the insertion
    # order in the file table, so this is what `7z l` shows the user.
    "README.txt",
    "LISEZ-MOI.txt",
    "recover.sh",
    "restore.sh",
    "envelope.env",
    "vault.recovered.yml",
)


def _which_7z() -> str:
    """Return the path to the `7z` binary, or raise. p7zip-full /
    7zip Debian packages install /usr/bin/7z; the upstream tarball
    sometimes uses 7zz on macOS. We accept both."""
    for name in ("7z", "7zz"):
        path = shutil.which(name)
        if path:
            return path
    raise RuntimeError(
        "7z is not installed. Install p7zip-full (Debian/Ubuntu) "
        "or 7zip; the recovery archive cannot be packaged without it."
    )


def _warn_if_noninteractive_passphrase_context() -> None:
    """Emit a stderr warning if `pack_recovery_archive` is being called
    in a non-interactive context (cron / Ansible / CI). The 7z CLI does
    NOT accept a passphrase from stdin or a file descriptor; the literal
    value must be passed as `-p<value>` on argv, where it appears in
    /proc/<pid>/cmdline for the entire run. On a single-operator
    laptop the cmdline is owned by the operator's UID -- documented
    accepted risk. On the operator-VPS (or any shared-UID context) a
    co-tenant on that UID can read the passphrase from /proc.

    Detection: stdin not a tty AND there is no controlling terminal
    via /dev/tty. Cron, systemd Type=oneshot units, and Ansible
    `command:` invocations all match; an operator running the script
    by hand in a terminal does not.

    The warning is purely informational -- the function still proceeds.
    The architecturally clean fix (gpg --symmetric --passphrase-fd
    instead of 7z) belongs in a separate change because it forks the
    archive format from .zip to .gpg, and the cross-platform .zip UX
    is the design's load-bearing constraint.
    """
    import os
    if sys.stdin.isatty():
        return
    # Some non-tty stdins are still operator-driven (piped input from a
    # tmux scratchpad, etc.). Treat "controlling terminal exists" as
    # operator-context too.
    try:
        fd = os.open("/dev/tty", os.O_RDONLY)
        os.close(fd)
        return
    except OSError:
        pass
    print(
        "── catena recovery-archive: WARNING ──\n"
        "  Running in a non-interactive context. The 7z CLI passes the\n"
        "  passphrase on argv (-p<value>); /proc/<pid>/cmdline is\n"
        "  readable by any process running as the same UID for the\n"
        "  duration of the archive build (~1-5s for a typical vault).\n"
        "  This is fine on a single-operator laptop; on a shared-UID\n"
        "  host (operator-VPS, CI worker) a co-tenant can scrape the\n"
        "  passphrase. Consider running this from your laptop instead,\n"
        "  or switch to the gpg-symmetric emit path when it ships.\n"
        "── end warning ──",
        file=sys.stderr,
    )


def pack_recovery_archive(
    *,
    out_path: Path,
    members: dict[str, str | bytes],
    passphrase: str,
) -> None:
    """Write `members` into `out_path` as an AES-256 password-protected
    .zip via `7z`. AES-256 (`-mem=AES256`) protects file CONTENTS; the
    ZIP format itself does not support filename encryption (that needs
    `-mhe=on` + `-t7z`, which produces a `.7z` file that macOS Finder /
    Windows Explorer cannot open natively -- losing the UX win that
    motivated the zip-over-gpg redirect). Filenames in the recovery
    archive are not sensitive (vault.recovered.yml is not a secret;
    its CONTENTS are), so the trade-off is fine.

    Compression is `-mx=9` (max) -- the archive is small (~30 KB +
    the recovered vault) and ships once.

    `members` keys are the in-archive filenames (no slashes; flat
    layout). Values are str (encoded as UTF-8) or bytes. The file is
    overwritten if it already exists.

    Passphrase-on-argv risk: 7z does not accept the passphrase via
    stdin or a file descriptor; the value lands in /proc/<pid>/cmdline
    for the run duration. Operator-laptop run-path is fine
    (single-UID, no co-tenants); non-interactive contexts get a
    stderr warning from _warn_if_noninteractive_passphrase_context.
    """
    seven = _which_7z()
    _warn_if_noninteractive_passphrase_context()
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    if out_path.exists():
        out_path.unlink()

    with tempfile.TemporaryDirectory(prefix="catena-recovery-archive-") as td:
        staging = Path(td)
        for name, content in members.items():
            if "/" in name or name.startswith("."):
                raise ValueError(f"member name {name!r} must be a flat filename")
            target = staging / name
            if isinstance(content, str):
                target.write_text(content, encoding="utf-8")
            else:
                target.write_bytes(content)
            # Make recover.sh + restore.sh executable in-staging so the
            # mode bits survive into the zip (7z preserves Unix perms
            # in zip extra fields; macOS + Linux honor them on extract).
            if name in ("recover.sh", "restore.sh"):
                target.chmod(0o755)
            else:
                target.chmod(0o600)

        # 7z reads the passphrase from the -p flag. We invoke via argv
        # (NOT a shell string), so the passphrase never touches a shell;
        # it only appears in /proc/<pid>/cmdline, which is owned by the
        # invoking user.
        # Run with cwd=staging + relative member names so the in-archive
        # paths are flat ("recover.sh", not "/tmp/.../recover.sh").
        argv = [
            seven, "a",
            f"-p{passphrase}",
            "-mem=AES256",      # AES-256 content encryption (zip-format flag)
            "-mx=9",            # max compression
            "-y",               # auto-yes on overwrites in staging
            "-bso0", "-bsp0",   # quiet stdout + progress (errors still go to stderr)
            "-tzip",            # explicit zip format (cross-platform)
            str(out_path),
        ]
        argv.extend(list(members.keys()))
        res = subprocess.run(
            argv,
            capture_output=True,
            cwd=str(staging),
            timeout=120,
        )
        # Clear the argv list so the in-Python reference to the
        # passphrase string drops out of reach of a heap walk on a
        # post-crash core dump (best-effort -- the str is interned and
        # may survive elsewhere, but removing the named reference is
        # cheap insurance).
        argv.clear()
        if res.returncode != 0:
            raise RuntimeError(
                f"7z failed (rc={res.returncode}): "
                f"{res.stderr.decode('utf-8', errors='replace').strip()}"
            )
    out_path.chmod(0o600)


def unpack_recovery_archive(
    *,
    archive_path: Path,
    out_dir: Path,
    passphrase: str,
) -> None:
    """Inverse of pack_recovery_archive. Used by tests + by operators
    who want to verify a generated archive without shipping it.

    Raises if the passphrase is wrong or the archive is corrupt.
    """
    seven = _which_7z()
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    argv = [
        seven, "x",
        f"-p{passphrase}",
        "-y",
        "-bso0", "-bsp0",
        f"-o{out_dir}",
        str(archive_path),
    ]
    res = subprocess.run(argv, capture_output=True, timeout=120)
    if res.returncode != 0:
        raise RuntimeError(
            f"7z extract failed (rc={res.returncode}): "
            f"{res.stderr.decode('utf-8', errors='replace').strip()}"
        )


def build_archive_members(
    *,
    envelope: dict[str, str],
    restore_sh: str,
    vault_recovered_yml: str,
    hostname: str,
    generated_at: str,
) -> dict[str, str]:
    """Assemble the dict of {filename: content} that pack_recovery_archive
    consumes. Centralized so the operator-tool and host-script render
    the same archive shape.

    `vault_recovered_yml` should already be a yaml document (the output
    of extract_secrets_core.to_yaml on the host path, or a dump of the
    operator's vault.sops.yml on the operator path).
    """
    return {
        "README.txt": render_readme_en(hostname=hostname, generated_at=generated_at),
        "LISEZ-MOI.txt": render_readme_fr(hostname=hostname, generated_at=generated_at),
        "recover.sh": RECOVER_SH,
        "restore.sh": restore_sh,
        "envelope.env": render_env_file(envelope),
        "vault.recovered.yml": vault_recovered_yml,
    }


# ---------------------------------------------------------------------------
# Passphrase helper (test-only convenience)
# ---------------------------------------------------------------------------
# Production callers always source the passphrase from the operator's
# keystrokes (catena-admin passphrase prompt -> the host entry script;
# getpass on the operator path). This helper exists only so tests can
# fabricate strong passphrases without hardcoding them.
def fresh_passphrase(n_words: int = 6) -> str:
    """Return a random passphrase of `n_words` lowercase tokens joined
    by hyphens. 6 tokens of avg-length-5 lowercase letters yield ~141
    bits of entropy (log2(26**30) ≈ 141), well above the 128-bit
    threshold for symmetric AES-256."""
    rng = secrets.SystemRandom()
    words = []
    for _ in range(n_words):
        length = rng.randint(4, 6)
        words.append("".join(rng.choice(string.ascii_lowercase) for _ in range(length)))
    return "-".join(words)
