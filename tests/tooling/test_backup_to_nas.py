"""The off-device backup encrypts the database, and can still get it back.

Row 3.7: the database archive used to land on the NAS in the clear, protected
only by a ``chmod 700`` directory on a share that is world-writable by DSM
default. It is AES256 now, with the same passphrase file the secrets bundle
already used.

Two kinds of check here, because two different things can rot:

* a **round trip** through the real functions, lifted out of the script and run
  against a local directory standing in for the NAS. Encryption whose restore
  path has never been exercised is not a backup.
* a handful of **text contracts** on the parts that fail silently. Adding the
  ``.gpg`` suffix without widening the prune globs would leave copies on the
  NAS forever while ``docs/gdpr_runbook.md`` goes on promising a 14-day window
  — an erasure that has propagated everywhere except where nobody is looking.

Skips where bash, gpg or the coreutils this needs are missing (the Windows test
runner), and reads nothing outside ``scripts/`` (it is the only tree besides
``src/`` and ``tests/`` that the remote runner syncs).
"""

from __future__ import annotations

import shutil
import sqlite3
import subprocess
from pathlib import Path

import pytest
from tests.paths import REPO_ROOT

SCRIPT = REPO_ROOT / "scripts" / "backup_to_nas.sh"
SOURCE = SCRIPT.read_text(encoding="utf-8")

needs_shell = pytest.mark.skipif(
    not all(shutil.which(t) for t in ("bash", "gpg", "sha256sum", "mkfifo", "tee")),
    reason="needs a POSIX shell with gpg and coreutils",
)


# ── text contracts ────────────────────────────────────────────────────


def test_the_database_goes_out_encrypted():
    """send_file would ship it in the clear; only the secrets bundle may use it."""
    assert 'send_encrypted "$newest"' in SOURCE
    assert 'send_file "$newest"' not in SOURCE


def test_the_local_dirs_follow_the_var_layout():
    """The shell twin of core.paths.data_dir: once the operator moves the runtime
    data under var/, the snapshot and the media mirror are read from there, and
    from the repo root until then. A missing fallback ships nothing on the old
    layout; a missing var/ branch ships a stale snapshot after the move."""
    assert 'if [[ -d "$DK_ROOT/var" ]]; then DK_DATA="$DK_ROOT/var"; else DK_DATA="$DK_ROOT"; fi' in SOURCE
    assert 'LOCAL_BACKUP_DIR="$DK_DATA/backups"' in SOURCE
    media = SOURCE.split("for dir in guess_cache econ_icon_catalog econ_role_icons; do")[1].split("done")[0]
    assert "$DK_ROOT/$dir" not in media and "$DK_DATA/$dir" in media


def test_every_copy_written_is_proved_to_decrypt():
    """A checksum proves the transfer, not that the archive opens."""
    assert 'prove_decryptable "$db_dest"' in SOURCE


@pytest.mark.parametrize("glob", ["'dungeonkeeper_*.db.gpg'", "'dungeonkeeper_*.db'"])
def test_the_prune_covers_both_names(glob):
    """The new name AND the pre-switch one — old copies must still age out."""
    assert f"prune_old \"$NAS_DB_DIR\" {glob}" in SOURCE


def test_the_kept_count_sees_both_names():
    """Otherwise the log reads 0 the day after the switch, like a wiped NAS."""
    kept = SOURCE.split('kept="$(')[1].split('")')[0]
    assert "dungeonkeeper_*.db.gpg" in kept and "-name 'dungeonkeeper_*.db'" in kept


def test_the_plaintext_is_never_staged_on_disk():
    """PrivateTmp puts mktemp in a tmpfs; 800 MB of ciphertext there is RAM."""
    body = SOURCE.split("send_encrypted() {")[1].split("\n}")[0]
    assert "--output -" in body, "gpg must stream, not write a file"
    assert "$staging/hash.fifo" in body, "the checksum is taken in flight"


# ── the round trip ────────────────────────────────────────────────────


HARNESS = """
set -euo pipefail
staging="$WORK/staging"; mkdir -p "$staging"
GPG_PASSPHRASE_FILE="$WORK/pass"; printf 'test-passphrase\\n' > "$GPG_PASSPHRASE_FILE"
log() { echo "log: $*"; }
die() { echo "FATAL: $*" >&2; exit 1; }
ssh_nas() { bash -c "$1"; }          # stands in for the ssh pipe
eval "$(sed -n '/^send_encrypted() {/,/^}/p' "$SCRIPT")"
eval "$(sed -n '/^prove_decryptable() {/,/^}/p' "$SCRIPT")"
"""


def _db(path: Path) -> None:
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE messages (id INTEGER PRIMARY KEY, body TEXT)")
    conn.executemany("INSERT INTO messages (body) VALUES (?)", [(f"m{i}",) for i in range(500)])
    conn.commit()
    conn.close()


def _run(script: str, work: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", "-c", HARNESS + script],
        env={"PATH": "/usr/bin:/bin", "WORK": str(work), "SCRIPT": str(SCRIPT), "HOME": str(work)},
        capture_output=True,
        text=True,
    )


@pytest.fixture
def work(tmp_path):
    (tmp_path / "nas").mkdir()
    _db(tmp_path / "source.db")
    return tmp_path


@needs_shell
def test_a_copy_round_trips_byte_for_byte(work):
    """The whole point: what lands on the NAS comes back as the same database."""
    out = _run(
        'send_encrypted "$WORK/source.db" "$WORK/nas/source.db.gpg"\n'
        'prove_decryptable "$WORK/nas/source.db.gpg"\n'
        'gpg --batch --quiet --passphrase-file "$GPG_PASSPHRASE_FILE" '
        '    --decrypt --output "$WORK/back.db" "$WORK/nas/source.db.gpg"\n'
        'sha256sum "$WORK/source.db" "$WORK/back.db" | cut -d" " -f1 | uniq | wc -l\n',
        work,
    )
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip().splitlines()[-1] == "1", "restored file differs from the source"
    assert (work / "nas" / "source.db.gpg").read_bytes()[:1] != b"S", "not encrypted!"


@needs_shell
def test_the_archive_is_not_readable_as_a_database(work):
    """The finding itself: a stranger on the LAN must not be able to open it."""
    _run('send_encrypted "$WORK/source.db" "$WORK/nas/source.db.gpg"', work)
    with pytest.raises(sqlite3.DatabaseError):
        sqlite3.connect(work / "nas" / "source.db.gpg").execute("SELECT 1 FROM messages")


@needs_shell
def test_a_second_run_skips_the_transfer(work):
    """Keyed on the filename now — the ciphertext differs every run."""
    _run('send_encrypted "$WORK/source.db" "$WORK/nas/source.db.gpg"', work)
    before = (work / "nas" / "source.db.gpg").read_bytes()
    out = _run('send_encrypted "$WORK/source.db" "$WORK/nas/source.db.gpg"', work)
    assert "already present" in out.stdout
    assert (work / "nas" / "source.db.gpg").read_bytes() == before


@needs_shell
def test_a_truncated_archive_is_refused(work):
    """prove_decryptable exists to catch exactly this, and must not pass it."""
    _run(
        'send_encrypted "$WORK/source.db" "$WORK/nas/source.db.gpg"\n'
        'head -c 512 "$WORK/nas/source.db.gpg" > "$WORK/nas/bad.gpg"',
        work,
    )
    out = _run('prove_decryptable "$WORK/nas/bad.gpg"', work)
    assert out.returncode != 0
    assert "did not decrypt to a database" in out.stderr


@needs_shell
def test_a_wrong_passphrase_is_refused_rather_than_silently_wrong(work):
    """A restore that 'succeeds' into junk is the worst outcome available."""
    _run('send_encrypted "$WORK/source.db" "$WORK/nas/source.db.gpg"', work)
    out = _run(
        'printf "wrong\\n" > "$WORK/bad"\n'
        'gpg --batch --quiet --passphrase-file "$WORK/bad" --decrypt '
        '    --output "$WORK/junk.db" "$WORK/nas/source.db.gpg"',
        work,
    )
    assert out.returncode != 0
    assert not (work / "junk.db").exists() or (work / "junk.db").stat().st_size == 0
