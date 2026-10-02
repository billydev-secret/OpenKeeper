#!/usr/bin/env bash
#
# Off-device backup to the Synology NAS.
#
# Closes finding B1 from docs/reviews/2026-08-06-backup-disaster-recovery.md:
# the bot's own backups land in <db-parent>/backups (var/backups once the
# runtime data has moved) on the *same physical disk*
# as the database, which defends against logical damage but not against disk
# failure. This ships a verified copy to the Synology NAS over SSH.
#
# TRANSPORT: a plain `ssh 'cat > file'` pipe, NOT rsync and NOT scp. That is
# forced by DSM, and both alternatives were tried against the live NAS on
# 2026-08-07:
#   * rsync    -- DSM ships a setuid-root rsync that refuses --server mode for
#                 a non-root uid ("Permission denied, please try again." from
#                 the far end). DSM 7 also disables root SSH, so there is no
#                 account to run it as.
#   * scp/sftp -- DSM's sshd has no sftp subsystem enabled ("subsystem request
#                 failed on channel 0"), and modern scp speaks SFTP.
# Both could be unblocked by flipping DSM service checkboxes, but a DSM update
# that reset one would silently break the backup. A pipe depends on nothing but
# sshd being up.
#
# Losing rsync costs delta transfer and resume. Neither matters much here: the
# SQLite file changes throughout, so a delta would be close to a full copy
# anyway, and a dropped LAN transfer just re-runs next timer. What rsync *was*
# providing that we must now provide ourselves is end-to-end verification --
# hence the explicit sha256 compare in send_file(), which is strictly stronger
# than the far-end size check this script previously relied on.
#
# Runs as `ben` from a systemd timer (deploy/dk-nas-backup.{service,timer}) --
# deliberately NOT inside the bot, whose ProtectHome=read-only hardening should
# stay intact.
#
# Config lives in ~/.config/dk-backup/nas.conf (see deploy/README.md).
# Exit non-zero on any failure so the systemd unit goes `failed` and shows up
# in `systemctl --failed` -- that is this job's failure visibility.

set -euo pipefail

CONF="${DK_NAS_CONF:-$HOME/.config/dk-backup/nas.conf}"
if [[ ! -r "$CONF" ]]; then
    echo "FATAL: config not found at $CONF (see deploy/README.md)" >&2
    exit 78  # EX_CONFIG
fi
# shellcheck source=/dev/null
source "$CONF"

: "${NAS_HOST:?NAS_HOST must be set in $CONF}"
: "${NAS_USER:?NAS_USER must be set in $CONF}"
: "${NAS_DB_DIR:?NAS_DB_DIR must be set in $CONF}"
: "${NAS_SECRET_DIR:?NAS_SECRET_DIR must be set in $CONF}"
: "${GPG_PASSPHRASE_FILE:?GPG_PASSPHRASE_FILE must be set in $CONF}"
RETENTION_DAYS="${RETENTION_DAYS:-14}"
DK_ROOT="${DK_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
# The runtime data dirs (backups and the media mirrored in step 4) live under
# $DK_ROOT/var once the operator has moved them there, and at $DK_ROOT until
# then -- the shell twin of bot_modules.core.paths.data_dir. The bot must be
# stopped and all of them moved together, or a run between the two halves
# ships a stale snapshot.
if [[ -d "$DK_ROOT/var" ]]; then DK_DATA="$DK_ROOT/var"; else DK_DATA="$DK_ROOT"; fi
LOCAL_BACKUP_DIR="$DK_DATA/backups"

SSH_OPTS=(-o BatchMode=yes -o ConnectTimeout=10 -o StrictHostKeyChecking=accept-new)
ssh_nas() { ssh "${SSH_OPTS[@]}" "$NAS_USER@$NAS_HOST" "$@"; }

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*"; }
die() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] FATAL: $*" >&2; exit 1; }

# send_file <local> <remote> -- copy over an SSH pipe and prove it arrived.
#
# Writes to <remote>.partial and renames only after the sha256 matches, so a
# truncated transfer can never occupy the real filename on the NAS. That is
# deliberately the same discipline finding B4 imposed on the local backup: the
# newest-looking file must never be a partial one.
#
# Skips the transfer when the far end already holds a byte-identical copy,
# which makes a same-day re-run cheap instead of another 790 MB.
send_file() {
    local src="$1" dest="$2"
    local local_sum remote_sum remote_size local_size
    local_size="$(stat -c%s "$src")"
    local_sum="$(sha256sum "$src" | cut -d' ' -f1)"

    remote_size="$(ssh_nas "stat -c%s '$dest' 2>/dev/null || echo 0")"
    if [[ "$remote_size" == "$local_size" ]]; then
        remote_sum="$(ssh_nas "sha256sum '$dest' 2>/dev/null | cut -d' ' -f1")"
        if [[ "$remote_sum" == "$local_sum" ]]; then
            # Re-assert the mode on the skip path too, so a copy left behind by
            # an earlier version of this script (or by a share-wide permission
            # change on the NAS) is corrected rather than kept forever at
            # whatever it happens to be.
            ssh_nas "chmod 600 '$dest'" || true
            log "  already present and verified ($(basename "$dest"))"
            return 0
        fi
    fi

    ssh_nas "cat > '$dest.partial'" < "$src" \
        || die "transfer failed: $(basename "$src")"

    remote_sum="$(ssh_nas "sha256sum '$dest.partial' | cut -d' ' -f1")"
    if [[ "$remote_sum" != "$local_sum" ]]; then
        ssh_nas "rm -f '$dest.partial'" || true
        die "checksum mismatch after transfer of $(basename "$src"): local=$local_sum remote=$remote_sum"
    fi

    # 600, not the share's inherited 777 -- see the chmod note at step 3.
    ssh_nas "mv '$dest.partial' '$dest' && chmod 600 '$dest'" \
        || die "could not finalise $dest"
    log "  verified sha256 ${local_sum:0:16}… ($local_size bytes)"
}

# send_encrypted <local-plaintext> <remote-dest.gpg> -- same discipline as
# send_file (.partial, sha256 both ends, atomic rename) but the bytes are
# encrypted on the way out and the plaintext never leaves this machine.
#
# Closes finding 3.7: the database copy used to land on the NAS in the clear,
# with a chmod 700 directory as its only protection. The share itself is
# drwxrwxrwx by DSM default, so one permission reset -- or a DSM update, or
# anyone with any account on that NAS -- exposed every member's data. The
# secrets bundle was already encrypted here; this uses the same passphrase file
# and the same cipher rather than introducing a second scheme.
#
# WHY STREAM instead of "gpg to a temp file, then send_file it": the unit runs
# with PrivateTmp=true under ProtectSystem=strict, so mktemp -d lands in a
# tmpfs. Staging ~800 MB of ciphertext there is ~800 MB of RAM on the box that
# also carries the ML stack. Streaming keeps the peak at a pipe buffer.
#
# A side effect worth knowing: gpg compresses by default, and a SQLite file
# compresses well. Measured 2026-09-19 on a real 826 MB backup: 220 MB of
# ciphertext, 10 s to produce. The nightly LAN transfer got smaller, not
# bigger, and the 14-day window on the NAS costs about a quarter of what it did.
#
# WHY A FIFO for the checksum: gpg output is not reproducible -- a fresh random
# session key and fresh headers every run -- so there is no local file to hash
# afterwards and no way to re-derive the same bytes. The hash has to be taken
# from the stream as it goes past. `wait` on the reader is what makes that
# safe: process substitution would let the compare read a half-written sum.
send_encrypted() {
    local src="$1" dest="$2"
    local fifo="$staging/hash.fifo" sumfile="$staging/hash.out"
    local local_sum remote_sum remote_size

    # The skip path can no longer be a content compare, because the ciphertext
    # differs every run. It keys on the filename instead, which is sound here
    # for two reasons: the name carries the source backup's timestamp, so the
    # same name means the same plaintext; and the real name only ever appears
    # after the checksum compare below, so a file sitting under it is by
    # construction a verified one.
    remote_size="$(ssh_nas "stat -c%s '$dest' 2>/dev/null || echo 0")"
    if (( remote_size > 0 )); then
        ssh_nas "chmod 600 '$dest'" || true
        log "  already present and verified ($(basename "$dest"), $remote_size bytes)"
        return 0
    fi

    rm -f "$fifo" "$sumfile"
    mkfifo "$fifo"
    ( sha256sum < "$fifo" | cut -d' ' -f1 > "$sumfile" ) &
    local hash_pid=$!

    if ! gpg --batch --yes --quiet \
             --passphrase-file "$GPG_PASSPHRASE_FILE" \
             --symmetric --cipher-algo AES256 \
             --output - "$src" \
           | tee "$fifo" \
           | ssh_nas "cat > '$dest.partial'"
    then
        wait "$hash_pid" 2>/dev/null || true
        rm -f "$fifo" "$sumfile"
        ssh_nas "rm -f '$dest.partial'" || true
        die "encrypted transfer failed: $(basename "$src")"
    fi

    wait "$hash_pid"          # the sum file is only complete once this returns
    local_sum="$(cat "$sumfile")"
    rm -f "$fifo" "$sumfile"

    remote_sum="$(ssh_nas "sha256sum '$dest.partial' | cut -d' ' -f1")"
    if [[ "$remote_sum" != "$local_sum" ]]; then
        ssh_nas "rm -f '$dest.partial'" || true
        die "checksum mismatch after transfer of $(basename "$dest"): local=$local_sum remote=$remote_sum"
    fi

    ssh_nas "mv '$dest.partial' '$dest' && chmod 600 '$dest'" \
        || die "could not finalise $dest"
    remote_size="$(ssh_nas "stat -c%s '$dest'")"
    log "  verified sha256 ${local_sum:0:16}… ($remote_size bytes encrypted)"
}

# prove_decryptable <remote-dest.gpg> -- pull the head back and open it.
#
# A matching sha256 proves the transfer, not that the file is a restorable
# database. Encrypting the archive moves the failure mode from "readable by the
# LAN" to "unopenable in a disaster", and the only defence against the second
# is decrypting it on a normal day rather than on the worst one. So every run
# pulls the first few MB back, decrypts it with the same passphrase file a
# restore would use, and checks SQLite's magic bytes.
#
# gpg exits non-zero on a truncated stream (no MDC to check) -- expected, and
# why its status is ignored. What is NOT ignored is the plaintext it produced
# on the way: if the passphrase, the cipher or the transfer were wrong, there
# is no "SQLite format 3" at the front of it.
prove_decryptable() {
    local dest="$1" magic
    magic="$(ssh_nas "head -c 4194304 '$dest'" 2>/dev/null \
             | gpg --batch --quiet --ignore-mdc-error \
                   --passphrase-file "$GPG_PASSPHRASE_FILE" --decrypt 2>/dev/null \
             | head -c 15 || true)"
    [[ "$magic" == "SQLite format 3" ]] \
        || die "the copy on the NAS did not decrypt to a database (got: '${magic:-nothing}'). \
The transfer verified, so suspect $GPG_PASSPHRASE_FILE."
    log "  decrypts to a SQLite database (checked against $GPG_PASSPHRASE_FILE)"
}

# --- 1. reachability -------------------------------------------------------
log "Checking $NAS_USER@$NAS_HOST ..."
ssh_nas true || die "cannot reach the NAS over SSH (key not authorised, or NAS down)"

# --- 2. pick the newest LOCAL backup and verify it before shipping ---------
# Never push a corrupt file off-device: a bad backup that overwrites a good one
# on the NAS is worse than no backup at all.
newest="$(find "$LOCAL_BACKUP_DIR" -maxdepth 1 -name 'dungeonkeeper_*.db' -printf '%T@ %p\n' \
          | sort -rn | head -1 | cut -d' ' -f2-)"
[[ -n "$newest" ]] || die "no local backup found in $LOCAL_BACKUP_DIR"

# `immutable=1`, not just `mode=ro`. A backup carries journal_mode=wal in its
# header, and opening a WAL database requires shared memory -- so a plain
# read-only open CREATES <name>.db-shm and <name>.db-wal beside the file it is
# only supposed to be reading. That is how the first NAS run ended up shipping a
# -shm/-wal pair next to the database: junk at best, and at worst a 3am trap,
# because a restorer who copies all three gets a malformed database (which is
# exactly what docs/disaster_recovery_runbook.md warns against).
#
# immutable=1 tells SQLite the file cannot change underneath it, so it skips the
# shared-memory machinery entirely. Safe here precisely because these are
# finished backup-API outputs: dst.close() checkpointed them, so the whole
# database is in the main file and there is no WAL content to miss.
#
# This is load-bearing, not cosmetic. Under the systemd unit's
# ProtectSystem=strict the repo is READ-ONLY, so a plain mode=ro open cannot
# create its -shm and dies with "attempt to write a readonly database" -- at the
# integrity check, i.e. the first thing this script does with the file. Verified
# 2026-08-10 against a chmod a-w directory: mode=ro exits 8, immutable=1 exits 0.
# Removing immutable=1 breaks the timed run while leaving manual runs working,
# which is the worst possible way for it to fail.
log "Verifying $(basename "$newest") before transfer ..."
check="$(sqlite3 "file:${newest}?mode=ro&immutable=1" 'PRAGMA quick_check;' 2>&1 || true)"
[[ "$check" == "ok" ]] || die "local backup failed integrity check: $check"

rows="$(sqlite3 "file:${newest}?mode=ro&immutable=1" 'SELECT COUNT(*) FROM messages;')"
log "Integrity ok (messages=$rows, $(du -h "$newest" | cut -f1))"

# --- 3. ship the database, encrypted ---------------------------------------
# The passphrase is needed here now, not just for the secrets bundle in step 4,
# so it is checked before anything is transferred: failing at the last step
# after pushing 800 MB would be a waste, and failing after DELETING the
# plaintext predecessor would be worse.
[[ -r "$GPG_PASSPHRASE_FILE" ]] || die "GPG passphrase file unreadable: $GPG_PASSPHRASE_FILE"

staging="$(mktemp -d)"
trap 'rm -rf "$staging"' EXIT

# Both directories go 700, not just the secrets one. The share itself is
# world-writable (drwxrwxrwx, DSM default), and a database copy inherits that:
# the first run landed a 755 MB file holding 646k rows of member message
# content as -rwxrwxrwx. Directory mode is now the SECOND line of defence
# rather than the only one -- the archive itself is AES256 from 2026-09-19 --
# but it stays, because a mode is what stops the file being listed at all.
ssh_nas "mkdir -p '$NAS_DB_DIR' '$NAS_SECRET_DIR' \
         && chmod 700 '$NAS_DB_DIR' '$NAS_SECRET_DIR'"

# The media mirror (step 4) needs the same treatment and did not get it until
# 2026-08-11: `mkdir -p` inside the share inherits DSM's 777, and the swap
# preserves it, so db/media sat drwxrwxrwx holding guess_cache -- member-
# submitted photos -- readable by every account on the LAN. Created here so the
# mode is asserted before anything is written into it, then re-asserted after
# each swap below.
NAS_MEDIA_DIR="${NAS_MEDIA_DIR:-$NAS_DB_DIR/media}"
ssh_nas "mkdir -p '$NAS_MEDIA_DIR' && chmod 700 '$NAS_MEDIA_DIR'"

# The .gpg suffix is load-bearing in three places: the prune globs below, the
# restore procedure in docs/disaster_recovery_runbook.md, and the retention
# table in docs/gdpr_runbook.md. Change the name and all three need changing.
db_dest="$NAS_DB_DIR/$(basename "$newest").gpg"
log "Encrypting and syncing $(basename "$newest") -> $NAS_HOST:$db_dest"
send_encrypted "$newest" "$db_dest"
prove_decryptable "$db_dest"
log "Transfer verified"

# --- 4. ship the things a DB-only restore cannot rebuild -------------------
# .env is the single highest-leverage file here (finding B7): 4 KB, gitignored,
# and losing it means re-issuing the bot token and every API key by hand.
# Encrypted client-side so the NAS never holds plaintext secrets. (The
# passphrase check and the staging dir moved up to step 3, which now needs
# both.) This one is small enough to stage on disk, unlike the database.
log "Encrypting secrets bundle ..."
tar -czf "$staging/secrets.tar.gz" \
    -C "$DK_ROOT" .env \
    -C /etc/systemd/system dungeon-keeper.service cloudflared.service 2>/dev/null \
  || tar -czf "$staging/secrets.tar.gz" -C "$DK_ROOT" .env

gpg --batch --yes --quiet \
    --passphrase-file "$GPG_PASSPHRASE_FILE" \
    --symmetric --cipher-algo AES256 \
    --output "$staging/secrets.tar.gz.gpg" \
    "$staging/secrets.tar.gz"

send_file "$staging/secrets.tar.gz.gpg" \
          "$NAS_SECRET_DIR/secrets-$(date +%Y%m%d).tar.gz.gpg"
log "Secrets bundle synced (AES256, passphrase from $GPG_PASSPHRASE_FILE)"

# Small unbacked media that a restored DB still points at (finding B7).
#
# This is a MIRROR of current state, not a versioned backup: a file removed
# locally (by the guess_repo cleanup, or by an erasure) is removed here too.
# That keeps deletions propagating for GDPR, at the cost of not being able to
# recover a file you deleted by mistake. The database copies are the real
# backup; this is 5 MB of accompanying images.
#
# Without rsync --delete, mirror semantics come from replacing the directory
# wholesale: extract into a fresh <dir>.new, then swap it into place. The swap
# is what keeps this safe -- the old copy is only removed once the new one has
# been fully extracted, so a transfer that dies midway leaves the previous
# mirror intact rather than a half-deleted one.
# NAS_MEDIA_DIR is created and chmod 700'd in step 3, before anything lands in it.
for dir in guess_cache econ_icon_catalog econ_role_icons; do
    [[ -d "$DK_DATA/$dir" ]] || continue
    ssh_nas "rm -rf '$NAS_MEDIA_DIR/$dir.new' && mkdir -p '$NAS_MEDIA_DIR/$dir.new'"
    tar -C "$DK_DATA/$dir" -czf - . \
        | ssh_nas "tar -C '$NAS_MEDIA_DIR/$dir.new' -xzf -" \
        || die "media mirror failed for $dir"
    # Strip group/other from the extracted tree before the swap: tar restores
    # the local modes, and anything that was 644 locally is world-readable on a
    # share other LAN accounts can reach. Re-asserted every run so a mirror
    # written by an older version of this script heals instead of staying open.
    ssh_nas "chmod -R go-rwx '$NAS_MEDIA_DIR/$dir.new'"
    ssh_nas "rm -rf '$NAS_MEDIA_DIR/$dir' \
             && mv '$NAS_MEDIA_DIR/$dir.new' '$NAS_MEDIA_DIR/$dir'"
    log "  mirrored $dir ($(find "$DK_DATA/$dir" -type f | wc -l) files)"
done
ssh_nas "chmod 700 '$NAS_MEDIA_DIR'"
log "Media directories mirrored"

# --- 5. prune the NAS to the agreed window ---------------------------------
# 14 days. This number is load-bearing: it is what docs/gdpr_runbook.md
# states as the point at which an erasure has propagated to every copy. Change
# it in both places or not at all.
#
# `find -delete` is avoided deliberately: DSM ships a busybox find on some
# versions, where -delete does not exist. print0 | xargs rm is portable, and
# the -maxdepth/-type/-name triple keeps the blast radius to our own files.
prune_old() {  # $1 = directory, $2 = filename glob -> echoes the count removed
    local dir="$1" glob="$2"
    local n
    n="$(ssh_nas "find '$dir' -maxdepth 1 -type f -name '$glob' -mtime +$RETENTION_DAYS | wc -l")"
    if (( n > 0 )); then
        ssh_nas "find '$dir' -maxdepth 1 -type f -name '$glob' -mtime +$RETENTION_DAYS -print0 \
                 | xargs -0 -r rm -f"
    fi
    echo "$n"
}

# Two globs for the database, not one. The archive is 'dungeonkeeper_*.db.gpg'
# now, and a prune that still matched only 'dungeonkeeper_*.db' would quietly
# stop deleting anything -- leaving copies on the NAS forever while the runbook
# and the GDPR retention table both go on promising 14 days. That is the whole
# failure: an erasure that has "propagated everywhere" except to the copies
# nobody is pruning. The plaintext glob stays so that pre-2026-09-19 copies
# still age out on their own.
log "Pruning NAS copies older than ${RETENTION_DAYS}d ..."
pruned_enc="$(prune_old "$NAS_DB_DIR" 'dungeonkeeper_*.db.gpg')"
pruned_plain="$(prune_old "$NAS_DB_DIR" 'dungeonkeeper_*.db')"
pruned=$(( pruned_enc + pruned_plain ))
pruned_sec="$(prune_old "$NAS_SECRET_DIR" 'secrets-*.tar.gz.gpg')"
log "Pruned $pruned database copies ($pruned_enc encrypted, $pruned_plain plaintext)" \
    "and $pruned_sec secrets bundles"

# Stale .partial files. send_file removes its own on a checksum mismatch, but a
# run killed mid-transfer (reboot, network drop, systemctl stop) leaves one
# behind -- and at ~790 MB each they would accumulate silently and forever.
# The +1 day floor guarantees this can never touch a transfer still in flight.
for d in "$NAS_DB_DIR" "$NAS_SECRET_DIR"; do
    stale="$(ssh_nas "find '$d' -maxdepth 1 -type f -name '*.partial' -mtime +1 -print0 \
                      | xargs -0 -r rm -vf | wc -l")"
    (( stale > 0 )) && log "Removed $stale stale .partial file(s) from $d"
done

# Stray -shm/-wal siblings. Nothing here creates them any more (see the
# immutable=1 note at step 2), but the first runs did, and a restorer who copies
# a .db together with a stale -wal gets a database SQLite refuses to open. They
# are never load-bearing next to a backup-API file, so sweeping them is safe.
for d in "$NAS_DB_DIR"; do
    sidecars="$(ssh_nas "find '$d' -maxdepth 1 -type f \\( -name '*.db-shm' -o -name '*.db-wal' \\) | wc -l")"
    if (( sidecars > 0 )); then
        ssh_nas "find '$d' -maxdepth 1 -type f \\( -name '*.db-shm' -o -name '*.db-wal' \\) -print0 \
                 | xargs -0 -r rm -f"
        log "Removed $sidecars stray -shm/-wal file(s) from $d"
    fi
done

# Counts both names for the same reason the prune does: a count that only saw
# the old one would read 0 the day after the switch and look like a wiped NAS.
kept="$(ssh_nas "find '$NAS_DB_DIR' -maxdepth 1 -type f \
                 \\( -name 'dungeonkeeper_*.db' -o -name 'dungeonkeeper_*.db.gpg' \\) | wc -l")"
log "NAS now holds $kept database copies (window: ${RETENTION_DAYS}d)"

# --- 6. leave a local breadcrumb so staleness is detectable ----------------
mkdir -p "$(dirname "${STATUS_FILE:=$HOME/.local/state/dk-backup/last-nas-sync}")"
date -Iseconds > "$STATUS_FILE"

log "Off-device backup complete."
