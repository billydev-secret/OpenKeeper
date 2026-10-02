# Disaster recovery runbook

**The document you want at 3am.** Restore the Dungeon Keeper database from a
backup, in order, with the commands.

Verified end-to-end on 2026-08-06 against the live 746 MB database; see
[reviews/2026-08-06-backup-disaster-recovery.md](reviews/2026-08-06-backup-disaster-recovery.md)
for the drill and its timings.

- **RTO ≈ 3–5 minutes.** Almost all of it is bot startup (the ML stack;
  `TimeoutStartSec=180`). Copying and migrating the data takes under a second.
- **RPO up to 6 hours**: the backup interval. If `dungeonkeeper.db` is still
  readable, RPO is ~0: prefer step 1 over restoring.

## Where everything is

| | |
|---|---|
| Live DB | `~/discord-bots/dungeon-keeper/dungeonkeeper.db` |
| Local backups | `~/discord-bots/dungeon-keeper/backups/`, **same disk as the DB**. Moves to `var/backups/` with the other runtime data; see *Runtime data layout* below |
| Schedule | every 6h (skipped if one was taken in the last 3h); newest 5 kept, nothing younger than 48h pruned |
| **Off-device backups** | the Synology NAS (`<nas-host>`), daily at 04:30, **14-day window** |
| Service | `dungeon-keeper.service` (also serves the dashboard) |
| Logs | `journalctl -u dungeon-keeper` (persistent). `log.txt` is **wiped on boot**; don't rely on it. |

**Which copy do you need?** If the disk is alive, use `backups/`: it is local
and newer. If the machine or disk is gone, everything comes from the NAS.

### Runtime data layout

Everything the bot writes beside the database (`backups/`, `models/`,
`guess_cache/`, `econ_icon_catalog/`, `econ_role_icons/`, `econ_emoji/`,
`quote_borders/`, `swatches/`) is gathering under one `var/` directory at
the repo root, so the root holds source and config only. **Two layouts are
valid, and which one is current is whether `var/` exists:**

| | where `backups/` is | status |
|---|---|---|
| flat (`var/` absent) | `<repo>/backups/` | **current in production** until the move below |
| gathered (`var/` present) | `<repo>/var/backups/` | the target layout |

Every writer and reader goes through one helper, `bot_modules/core/paths.py`
(`data_dir`), and its shell twin in `scripts/backup_to_nas.sh`: `var/<name>`
if `var/` exists, else `<name>`. The fallback to the flat layout is a
**deliberate anomaly** (CLAUDE.md): the move is an operator press with the
bot stopped, not something a merge or a restart does, so until that press
every path above stays exactly where it is. `tests/core/test_paths.py` fails
if the fallback is removed. Nothing creates `var/` on its own.

The move is one press, every one of those directories that exists together
(`econ_emoji/` is created on first use, so `ls` first and move it too if it is
there), because the NAS timer and every writer pick a layout per run:
half-moved, the bot writes snapshots to one place while the timer reads the
other. Four tables record relative paths into these directories
(`guess_rounds.original_path`, `econ_icon_catalog.image_path`,
`econ_personal_roles.icon_path` / `projected_icon_path`: these two hold
both `econ_role_icons/` uploads and `econ_icon_catalog/` picks;
`econ_emoji_submissions.image_path`) and `econ_color_catalog.image_path` points
into `swatches/` (a Sync would also rewrite it, but the shop shows no swatch art
until then), so the press rewrites those rows too:

```bash
sudo systemctl stop dungeon-keeper
cd ~/discord-bots/dungeon-keeper
mkdir var && mv backups models econ_icon_catalog econ_role_icons guess_cache quote_borders swatches var/
[ -d econ_emoji ] && mv econ_emoji var/
sqlite3 dungeonkeeper.db "UPDATE guess_rounds SET original_path = 'var/' || original_path WHERE original_path LIKE 'guess_cache/%';
                          UPDATE econ_icon_catalog SET image_path = 'var/' || image_path WHERE image_path LIKE 'econ_icon_catalog/%';
                          UPDATE econ_personal_roles SET icon_path = 'var/' || icon_path WHERE icon_path LIKE 'econ_role_icons/%' OR icon_path LIKE 'econ_icon_catalog/%';
                          UPDATE econ_personal_roles SET projected_icon_path = 'var/' || projected_icon_path WHERE projected_icon_path LIKE 'econ_role_icons/%' OR projected_icon_path LIKE 'econ_icon_catalog/%';
                          UPDATE econ_emoji_submissions SET image_path = 'var/' || image_path WHERE image_path LIKE 'econ_emoji/%';
                          UPDATE econ_color_catalog SET image_path = 'var/' || image_path WHERE image_path LIKE 'swatches/%';"
sudo install -m 644 deploy/dk-mcp.service /etc/systemd/system/dk-mcp.service   # the sandbox masks var/ by absolute path
sudo systemctl daemon-reload && sudo systemctl restart dk-mcp
sudo systemctl start dungeon-keeper
```

After it, every `backups/` in this runbook reads `var/backups/`.

## Are the backups actually running?

Ask this on a quiet day, not at 3am. Both copies are meant to be invisible, so
a stopped one changes nothing you would notice: the old files sit there looking
healthy while the newest ages out of usefulness.

- **Dashboard → Dev → System Stats → Backups** answers it in one glance: age and state
  of the local rotation and of the off-device copy, plus the last error if one
  failed. The home page's **Configuration Problems** card raises the same thing
  unasked once a copy goes stale (local: 12h, off-device: 48h).
- From a shell:

```bash
ls -lt backups/ | head -3                    # newest should be < 6h old (var/backups/ after the move)
systemctl status dk-nas-backup.service       # must end status=0/SUCCESS
systemctl list-timers dk-nas-backup.timer
cat ~/.local/state/dk-backup/last-nas-sync   # timestamp of the last NAS copy
```

**A green shell run of `backup_to_nas.sh` does not mean the timer works.** The
unit is subject to SELinux constraints your login shell is not, and for that
reason it silently never ran once in the four days after it was installed.
Verify with `systemctl start dk-nas-backup.service`; see `deploy/README.md`,
"The install trap".

---

## 0. Stop, before you touch anything

**Do not restore if you have not confirmed the live DB is actually unusable.**
A restore throws away every write since the backup: up to 6 hours of XP,
economy and activity data. Most "the bot is broken" incidents are not data
damage.

```bash
sqlite3 "file:~/discord-bots/dungeon-keeper/dungeonkeeper.db?mode=ro" \
  "PRAGMA quick_check;"        # expect: ok   (~15-25s on 746 MB)
```

- **`ok`** → the data is fine. This is not a restore. Check
  `journalctl -u dungeon-keeper -n 200` and fix the process instead.
- **Anything else, or the file is missing/unreadable** → continue.

## 1. Preserve the evidence first

Never overwrite the damaged database. You may need it, and it is the only copy
of the most recent writes.

```bash
cd ~/discord-bots/dungeon-keeper
sudo systemctl stop dungeon-keeper

# keep the corpse — including -wal/-shm, which hold the newest writes
mkdir -p ~/dk-incident-$(date +%Y%m%d_%H%M)
mv dungeonkeeper.db dungeonkeeper.db-wal dungeonkeeper.db-shm \
   ~/dk-incident-$(date +%Y%m%d_%H%M)/ 2>/dev/null
```

The `-wal` file matters: it can contain committed transactions not yet in the
main file, and it is often recoverable even when the main file is not.

## 2. Pick a backup — and verify it before you trust it

```bash
ls -lt ~/discord-bots/dungeon-keeper/backups/*.db     # var/backups/ after the move
```

Newest first. **Do not blindly take the top entry**: if a backup failed
part-way, a truncated file keeps the real name and the newest mtime (finding
B4). Always check it:

```bash
BAK=~/discord-bots/dungeon-keeper/backups/dungeonkeeper_YYYYMMDD_HHMMSS.db
sqlite3 "file:$BAK?mode=ro" "PRAGMA quick_check;"   # must print: ok
sqlite3 "file:$BAK?mode=ro" "SELECT COUNT(*) FROM messages;"
```

If it is not `ok`, or the row count is wildly low, move to the next one down.
Also note `dungeonkeeper-pre-tod-backfill-20260729.db` is a **hand-made snapshot
from 2026-07-29** that the rotation never prunes; usable, but very old, and it
predates any erasure run since that date.

## 3. Restore

```bash
cd ~/discord-bots/dungeon-keeper
cp "$BAK" dungeonkeeper.db      # instant: XFS reflink on this box
```

Do **not** copy any `-wal`/`-shm` from the backup directory: a backup-API file
is self-contained, and stale siblings are what makes a database come out
malformed.

## 4. Bring the schema forward

The backup was taken under whatever schema was live then; the code you are about
to run may be newer. The migration chain is idempotent and takes ~0.02s.

```bash
cd ~/discord-bots/dungeon-keeper
PYTHONPATH=src .venv/bin/python -c \
  "from migrations import apply_migrations_sync; \
   apply_migrations_sync('dungeonkeeper.db'); print('migrations OK')"
```

## 5. Sanity-check before starting the bot

```bash
sqlite3 "file:dungeonkeeper.db?mode=ro" "
  PRAGMA foreign_key_check;
  SELECT 'tables',     COUNT(*) FROM sqlite_master WHERE type='table';
  SELECT 'migrations', COUNT(*) FROM schema_version;
  SELECT 'messages',   COUNT(*) FROM messages;
  SELECT 'config',     COUNT(*) FROM config;
"
```

Reference values: a fresh migration run on 2026-10-01 gives 293 tables and 271
migrations. Row counts only grow; for scale, production held ~635k `messages`
rows and ~523 `config` rows on 2026-08-05 (765k `messages` rows by 2026-09-13;
since 2026-09-14 that table keeps metadata only, no message text).
`foreign_key_check` must return nothing.

## 6. Start, and watch it

```bash
sudo systemctl start dungeon-keeper
journalctl -u dungeon-keeper -f
```

Wait for the bot to report ready (can take ~1–3 minutes: the ML stack). Then
confirm in Discord that a slash command responds and the dashboard loads.

## 7. After the incident

- [ ] Force a fresh backup so the restored state has a copy; restarting the bot
      already takes one on startup; confirm it in `backups/`.
- [ ] Note in `docs/reviews/` what was lost (everything between the backup
      timestamp and the incident).
- [ ] **If a GDPR erasure ran after the backup was taken, re-run it**: the
      restore reinstated that user's rows. See
      [gdpr_runbook.md](gdpr_runbook.md) step 2.
- [ ] Keep `~/dk-incident-*/` until you are certain it is not needed,
      then delete it: it is a full copy of user data.

---

## Host freezes

**A progressive stall with no kernel errors is the disk until proven
otherwise.** On 2026-09-02 the host hung for about three and a half minutes and
then reset itself. The shape, which is what to recognize next time:

- Both bots' `heartbeat blocked` warnings escalating over minutes (10 s → 50 s),
  not one sudden stop.
- An event loop caught blocked inside `sqlite3.connect`, waiting on the disk.
- journald writing entries out of order, and reporting its journal corrupted
  after the reboot.
- No OOM, no `ata` / `I/O error` / `mce` / thermal lines, because the logger
  could not write them either.

**Read SMART first:** `sudo smartctl -H -A /dev/<disk>`, from a real terminal
(sudo needs a TTY, so not from a Claude `!` shell). That time the SSD was
failing; the drive was replaced and the bot moved to a different machine. An
earlier boot (2026-08-04 → 05) had also ended uncleanly and was never
explained; a second unclean end is a reason to check the disk, not to wait
for a third.

After a freeze, run step 0's read-only `PRAGMA quick_check` before trusting the
database; anything other than `ok` continues into this runbook.

---

## Taking a backup by hand

Before a risky migration or bulk update. This is exactly what the service does:

```bash
cd ~/discord-bots/dungeon-keeper
PYTHONPATH=src .venv/bin/python -c "
from pathlib import Path
from bot_modules.admin.db_backup import _run_backup
_run_backup(Path('dungeonkeeper.db'), Path('snapshots'), retention_count=999)
"
```

Write ad-hoc snapshots to **`snapshots/`, not `backups/`**: anything dropped in
`backups/` under a non-standard name is never pruned and lingers forever
(finding B5). Delete hand-made snapshots when the change is confirmed good.

Never `cp` the live DB while the bot is running: with WAL enabled the copy comes
out malformed. Use the backup API above, or a read-only URI connection:

```python
import sqlite3
src = sqlite3.connect("file:dungeonkeeper.db?mode=ro", uri=True)
src.backup(sqlite3.connect("snapshots/manual.db"))
```

---

## Total machine loss — rebuilding from the NAS

When the disk or the whole box is gone. Everything below comes off
the Synology NAS (`<nas-host>`); nothing is needed from the dead machine.
`<nas-host>` and `<nas-user>` are `NAS_HOST` / `NAS_USER` in
`~/.config/dk-backup/nas.conf` — a file on the machine this scenario assumes
lost — so keep both, with the backup passphrase, somewhere off-box (the
password-manager entry beside the passphrase).

> **`scp` does not work against this NAS.** DSM's sshd has no sftp subsystem
> (`subsystem request failed on channel 0`), and modern `scp` speaks SFTP.
> `rsync` fails too: DSM's setuid rsync refuses `--server` for a non-root uid.
> Pull files with an `ssh 'cat …'` pipe, as below. Verified against the live
> NAS 2026-08-07; do not "fix" these commands back to `scp`.

```bash
# 1. Fresh box: clone the repo and install per deploy/README.md.
git clone <repo> dungeon-keeper && cd dungeon-keeper

# 2. See what is there, and pick a copy. (The db dir is chmod 700 — use the
#    same DSM account the backup ran as.)
ssh <nas-user>@<nas-host> 'ls -la /volume1/Storage/botbackups/db'

#    Copies are named dungeonkeeper_YYYYMMDD_HHMMSS.db.gpg — AES256, same
#    passphrase as the secrets bundle. You cannot check one on the NAS any
#    more: DSM has sqlite3 but not the passphrase, and must not have it. Go
#    by the filename's timestamp and the file size.

# 3. Pull it down, decrypt, and check it locally. Order matters — the check
#    has to happen after the transfer, because it is the transfer that goes
#    wrong. gpg prompts for the passphrase (password manager; see below).
ssh <nas-user>@<nas-host> \
  'cat /volume1/Storage/botbackups/db/dungeonkeeper_YYYYMMDD_HHMMSS.db.gpg' > dungeonkeeper.db.gpg
gpg --decrypt --output dungeonkeeper.db dungeonkeeper.db.gpg
sqlite3 "file:dungeonkeeper.db?mode=ro" "PRAGMA quick_check;"   # expect: ok
rm -f dungeonkeeper.db.gpg

#    If gpg says "decryption failed: Bad session key", the passphrase is
#    wrong — not the file. Every copy on the NAS uses the same one, so trying
#    an older copy will not help.
#
#    A copy from before 2026-09-19 has no .gpg suffix and needs no decrypting:
#    pull it and check it in one step, as the old procedure did.

# 4. Recover the secrets bundle — .env plus the systemd units.
ssh <nas-user>@<nas-host> \
  'cat /volume1/Storage/botbackups/secrets/secrets-YYYYMMDD.tar.gz.gpg' > secrets.tar.gz.gpg
gpg --decrypt --output secrets.tar.gz secrets.tar.gz.gpg
tar -xzf secrets.tar.gz          # -> .env, dungeon-keeper.service, cloudflared.service
sudo cp dungeon-keeper.service cloudflared.service /etc/systemd/system/

#    The tunnel token is NOT in the bundle, on purpose: cloudflared.service
#    reads it from /etc/cloudflared/tunnel-token (--token-file), and that file
#    is not backed up. Re-issue it instead — Zero Trust dashboard -> Networks ->
#    Tunnels -> the tunnel -> Refresh token — and write it with part B of
#    "Cloudflare tunnel token" in deploy/README.md. Routing lives Cloudflare-
#    side, so every hostname comes back with the token; nothing else to set up.
#    A bundle from before the switch still has an inline --token. That token
#    is dead once refreshed, and --token beats --token-file, so swap the flag
#    first (part A, step A4) or the fresh token file is ignored:
grep -q -- '--token-file' /etc/systemd/system/cloudflared.service || \
  sudo sed -i -E 's#--token [A-Za-z0-9._=+/-]+#--token-file /etc/cloudflared/tunnel-token#' /etc/systemd/system/cloudflared.service
sudo systemctl daemon-reload

# 5. Bring the schema forward, then start (steps 4-6 above).
```

**The GPG passphrase is not on the NAS**: by design, and it was never on the
dead machine's disk alone if you followed the install note. It is in your
password manager.

Since 2026-09-19 it opens **the database as well as the secrets bundle**, and
that changes what losing it costs. It used to mean re-issuing credentials by
hand: tedious, survivable. It now means every off-device copy of the database
is unopenable, and nothing can rebuild those: not the NAS, not this repo, not
Discord. The passphrase is the single point of failure in this whole procedure.
Check it is in the password manager *now*, not when you need it.

(The trade is deliberate. Before it, the archive sat in the clear on a share
that is world-writable by DSM default, with a directory mode as its only
protection: one DSM update or permission reset from exposing every member's
data to any account on the LAN.)

### Still not backed up (deliberately)

| | Why it's fine |
|---|---|
| `models/` (4.5 GB; `var/models/` after the move) | Re-downloads from HuggingFace on first boot. Slow, not lost. Note `TimeoutStartSec=180` may not cover the first run. |
| `lavalink/Lavalink.jar` + plugins | Re-fetched per `deploy/README.md`. Music is silent until then. |
| Cloudflare tunnel token (`/etc/cloudflared/tunnel-token`) | Re-issued, not restored. It is one press in the Zero Trust dashboard (Refresh token), and all routing lives Cloudflare-side, so a fresh token brings every hostname back. Keeping it out of the bundle means the backup job (which runs as `ben`) never needs to read a root-only secret. Until 2026-09 the token sat inline in `cloudflared.service`, so the bundle did carry it, but it was also readable by any local user through `ps`. See "Cloudflare tunnel token" in `deploy/README.md`. |
| `assets/` | In git; recovers with the checkout. |
| `Discord Messages/` | Export archive, re-exportable. |

`guess_cache/`, `econ_icon_catalog/` and `econ_role_icons/` (under `var/` after
the move) **are** mirrored to the NAS, but as current state rather than versioned history; restoring a
14-day-old database may leave a handful of image rows pointing at files the
mirror no longer has. Rows survive; images 404. See finding B7.
