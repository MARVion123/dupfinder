# Duplicate File Finder for Synology NAS

A self-hosted web app that finds duplicate and near-duplicate files anywhere
under a directory you choose, rates how similar they are, explains which copies
are safe to remove, and deletes the ones you tick.

The scanner is **pure Python standard library** — no compiled extensions, no
web framework — because installing wheels on a NAS is a chore. The AI
suggestion layer and perceptual image matching are optional extras; everything
else works without them.

---

## What it does

**Pick any directory.** A folder browser in the UI walks the volumes you allow.
Nothing outside those roots can be browsed, scanned or deleted.

**Find duplicates in six escalating passes**, each cheap enough to make the next
one affordable:

| Pass | What it does | Cost per file |
|---|---|---|
| 1. Index | Walk the tree, skip `@eaDir`/`#recycle`/snapshots, collapse hardlinks | a `stat` |
| 2. Size | Only sizes that occur more than once can be duplicates | free |
| 3. Quick hash | MD5 of first 64 KiB + last 64 KiB + size | 128 KiB read |
| 4. Full MD5 | Whole-file content hash of whatever survived | full read |
| 5. Verify | **Byte-for-byte** comparison of every MD5 match | full read |
| 6. Fuzzy | CTPH (ssdeep-style) + perceptual image hashing for *near* duplicates | full read |

Pass 5 is the "if not sure, go deeper" step. MD5 collisions are astronomically
unlikely, but deletion is irreversible, so a match is proven rather than
assumed. Groups that pass it are marked **✔ verified**.

Pass 6 is what produces a *rating* rather than a yes/no. A pure-Python
context-triggered piecewise hash scores content similarity 0–100; that score is
blended with size ratio and filename similarity, and — if Pillow is installed —
a 64-bit perceptual hash catches re-encoded or resized photos that share no
bytes at all.

**No abort limit.** A scan runs until it finishes. It reports live progress
(phase, current path, bytes hashed, cache reuse), can be **stopped at any
moment** with partial results kept, and can be **run again** — a persistent hash
cache keyed on path+size+mtime means a re-run only pays for what actually
changed.

**Sortable results.** Every column sorts (similarity, copies, file size,
reclaimable bytes, folders spanned, suggestion confidence), with filters for
type, minimum similarity, minimum file size, and a path search.

**AI suggestions.** Claude reads the group metadata — never file contents — and
proposes which single copy to keep, which are safe to delete, which need a
human look, and what a **merge across folders** would look like. It is told to
be conservative: never propose deleting a whole group, always flag anything
under a `backup`/`archive`/`snapshot` path, and mark sub-100% matches as
*review* when the paths suggest deliberately different versions.

When no API key is configured the same UI is filled by a local rule engine
(path depth, folder quality, `copy`/`(1)`/`~` markers, age), so the app is fully
usable offline.

**Deletion is opt-in and reversible.** Tick individual files, or use
*Select suggested deletions* per group / for the whole page. Three modes:

- **Quarantine** (default) — moved to `<scan-root>/.dupfinder-trash/scan-N/`,
  restorable with one click from the action log.
- **Recycle** — moved to the DSM share recycle bin (`#recycle`).
- **Permanent** — unlinked.

A group can never be emptied: if you select every copy, the last one is kept
and reported as skipped. Every move and delete is logged with source,
destination and outcome.

---

## Install

### Recommended: Package Center (`.spk`)

Build the package on any machine with Python 3:

```sh
python3 install/spk/build_spk.py     # -> install/spk/dist/dupfinder-1.0.0-0001.spk
```

In DSM: **Package Center → Settings → Trust Level → Any publisher**, then
**Manual Install** and upload the `.spk`. Requires **Python 3** from Package
Center — the install refuses early with a message if it is missing.

Config and database live in `/var/packages/dupfinder/var`; the log is
`/var/packages/dupfinder/var/dupfinder.log`.

**Permissions.** DSM 7 refuses to install third-party packages that ask to run
as `root`, so the service runs as its own package user. That user has no access
to your shares until you grant it: *Control Panel → Shared Folder → \<share\> →
Edit → Permissions → System internal user → `dupfinder` → Read/Write*. Do this
for every share you want to scan — the scan silently skips what it cannot read,
and deletion fails on what it cannot write.

If you would rather not manage per-share permissions, use the systemd install
below instead; it runs as `root` and sees everything.

### Alternative: systemd service

Copy the project to the NAS (File Station, `scp`, or a git clone), then over SSH:

```sh
sudo sh install/install-dsm.sh
```

The script checks for Python 3.8+, copies the package to
`/volume1/apps/dupfinder`, installs the optional extras if `pip` is available,
writes `/etc/systemd/system/dupfinder.service`, and starts it.

Open `http://<nas-ip>:8777`.

```sh
journalctl -u dupfinder -f        # logs
sudo sh install/install-dsm.sh uninstall
```

### Keeping the NAS up to date from git

`install/deploy.sh` pulls this repository on the NAS and rolls the new version
into the running service. One-time setup over SSH as root:

```sh
mkdir -p /volume1/apps/src
git clone https://github.com/MARVion123/dupfinder.git /volume1/apps/src/dupfinder
chmod +x /volume1/apps/src/dupfinder/install/deploy.sh
/volume1/apps/src/dupfinder/install/deploy.sh --force
```

From then on:

```sh
/volume1/apps/src/dupfinder/install/deploy.sh          # deploy if the remote moved
/volume1/apps/src/dupfinder/install/deploy.sh --check  # show what would change
/volume1/apps/src/dupfinder/install/deploy.sh --force  # redeploy the current commit
```

The script does nothing when the remote has not moved, so it is safe to run on
a schedule. Before it stops the service it runs `compileall` over the new
checkout, and if the new version does not answer within 15 seconds it puts the
previous copy back and restarts. It reads the interpreter and the port out of
the existing unit file, so switching to a virtualenv or another port needs no
change here.

To have it check nightly:

```sh
cp /volume1/apps/src/dupfinder/install/dupfinder-deploy.{service,timer} \
   /etc/systemd/system/
systemctl daemon-reload
systemctl enable dupfinder-deploy.timer
systemctl start  dupfinder-deploy.timer
systemctl list-timers dupfinder-deploy --no-pager
```

`git` has to be on the NAS — Package Center → Git Server, or Entware. The
script says so plainly if it is missing.

### Alternative: Container Manager

```sh
docker compose -f install/docker-compose.yml up -d
```

Mount every volume you want to scan **at the same path inside the container**,
otherwise the paths in the UI will not match the paths on the NAS.

### Alternative: DSM Task Scheduler

If you would rather not touch systemd, create a *Triggered Task → Boot-up*
running as `root`:

```sh
cd /volume1/apps/dupfinder && /bin/env python3 -m dupfinder serve >> /var/log/dupfinder.log 2>&1
```

### Without installing anything

```sh
python3 -m dupfinder serve --port 8777
python3 -m dupfinder scan /volume1/photo      # headless
python3 -m dupfinder report --top 25
```

---

## Configure

Settings live in `<data-dir>/config.json` and are editable from the ⚙ dialog.
The defaults that matter:

| Key | Default | Notes |
|---|---|---|
| `roots_allowlist` | volumes + homes | **The security boundary.** Nothing outside these paths is reachable. |
| `auth_token` | *empty* | Set it if the port is reachable beyond your LAN. |
| `verify_bytes` | `true` | Byte-for-byte proof of every match. |
| `near_duplicates` | `true` | The fuzzy pass. Turn off for a pure exact-duplicate scan. |
| `near_threshold` | `70` | Similarity cutoff for reporting a near pair. |
| `fuzzy_max_bytes` | `16777216` | Bytes read per file for the fuzzy hash; `0` reads every file in full. CTPH is pure Python at ~2.5 MiB/s, so this is what decides whether a scan over films finishes in minutes or hours. The trade is that two files which only start to differ past this point are reported as similar — the similarity column only. Exact duplicates go through the full MD5 plus a byte-for-byte comparison and are unaffected. |
| `delete_mode` | `quarantine` | `quarantine` \| `recycle` \| `permanent`. |
| `protect_last_copy` | `true` | Refuse any selection that would empty a group. |
| `dry_run` | `false` | Rehearsal mode. Every check runs and the log fills up, but no file is touched. Toggled from the action bar with *Simulate only*, and it sticks. |
| `ai_model` | `claude-opus-5` | |
| `ai_effort` | `high` | `low` … `max`. |
| `anthropic_api_key` | *empty* | Falls back to `ANTHROPIC_API_KEY`. Never sent to the browser. |

### Security

The service has no user accounts — it is a single-admin tool. Before exposing
it beyond your LAN:

1. Set `auth_token` (or `--token`); every API call then needs it.
2. Put it behind the DSM reverse proxy with HTTPS.
3. Keep `roots_allowlist` as narrow as the job needs.

Path handling is defensive: every path from the browser or the model is
`realpath`-resolved and checked against the allowlist before any filesystem
call, which defeats `..`, symlink escapes and encoded traversal.

### Permissions

The service must read the shares you scan and write to delete. Running as
`root` is simplest and is what the unit file does. To run as a normal user
instead, set `User=`/`Group=` in `install/dupfinder.service` and give that
account access to the shares — the scan will silently skip anything it cannot
read.

### AI suggestions

```sh
python3 -m pip install anthropic          # provides the SDK and pydantic
export ANTHROPIC_API_KEY=sk-ant-...       # or paste it into Settings
```

Groups are sent in batches with `claude-opus-5`, adaptive thinking, and a typed
JSON schema, so the response is validated before it reaches the database. If a
batch fails for any reason the local rule engine fills that batch in, so the
suggestion column is never empty. Only **metadata** leaves the NAS — paths,
sizes, timestamps, similarity scores. File contents are never sent.

### Perceptual image matching

```sh
python3 -m pip install Pillow
```

Without it, images are still compared by fuzzy content hash; with it, resized
and re-encoded copies of the same photo are matched too. Pillow also supplies
the previews and the EXIF line described below.

### Previews and file details

Expanding a group shows a thumbnail of every image in it, next to the
dimensions, the EXIF capture date and the camera that took it — the things you
actually need to tell two similar photos apart. Previews are generated on
demand by `/api/thumb`, honour the EXIF orientation flag, and go through the
same path allowlist as everything else. A file whose preview cannot be
rendered simply shows no thumbnail.

Reading EXIF means opening the file, so it happens once per group when you
expand it, never while listing results.

### Rehearsing a deletion

Tick **Simulate only** in the action bar. The button turns into *Simulate
deletion*, and pressing it runs the whole thing — allowlist check, last-copy
protection, destination arithmetic — and stops just short of the filesystem
call. The action log fills with entries marked `simulated`, which carry no
Restore button because nothing ever moved.

It is the same code path as a real deletion, not a separate one, so what it
reports is what a real run would do. The setting is stored in `config.json`, so
it survives a reload: leave it on until you trust the suggestions.

---

## Testing the UI

A headless-browser smoke test drives the real interface end to end — folder
picker, scan, filters, sorting, expanding a group, selecting, quarantine
delete, restore from the log, settings, dark mode and a 420px viewport. It
builds its own throwaway tree of duplicates and starts its own server, so it
touches nothing outside a temp directory.

```sh
cd tests/ui
npm install
npx playwright install chromium
npm test                 # 33 checks; screenshots land in tests/ui/screenshots/
```

It fails the run on any uncaught JS exception, `console.error`, or HTTP 4xx/5xx
the page triggers, not just on a failed assertion.

## Performance notes

- The scanner is deliberately single-threaded. On a NAS the disk is the
  bottleneck, and the unit file runs it at `Nice=10` with idle I/O priority so
  Samba and DSM services stay responsive.
- Pass 3 kills the overwhelming majority of candidates after 128 KiB, so full
  hashing usually touches a small fraction of the library.
- The fuzzy pass is the expensive one. It is bucketed by CTPH block size (only
  comparable signatures are ever compared) and capped per bucket, which keeps
  it far away from O(n²) on large libraries. Turn it off for a fast exact-only
  run.
- Re-running a scan on a mostly-unchanged tree is dominated by the walk, not by
  hashing, thanks to the hash cache.

---

## Layout

```
dupfinder/
  config.py     settings + defaults
  db.py         SQLite schema, per-thread connections, hash cache
  safety.py     path allowlist - every filesystem call goes through here
  hashing.py    quick hash, MD5, byte compare, CTPH fuzzy hash, image dHash
  scanner.py    the six-pass engine, cancellation, grouping
  ai.py         Claude suggestions + local heuristic fallback
  actions.py    quarantine / recycle / delete / restore, all logged
  server.py     JSON API + static file serving
  static/       the UI (no build step)
install/        systemd unit, DSM installer, Dockerfile, compose file
  deploy.sh     git pull -> verify -> swap -> health check -> roll back
  spk/          Synology package sources + build_spk.py
tests/ui/       headless-browser smoke test (Playwright)
```
