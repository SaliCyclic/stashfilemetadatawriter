# File Metadata Writer (Stash plugin) — v2.0

Writes each scene's metadata from Stash into the video file itself, so the file
carries its own description into any future library software. Then it brings
Stash's database back in line with the rewritten file, so nothing is orphaned or
re-imported.

What goes into each file:

- **Tags:** title, date, studio, performers, tags, details, URL, director, studio
  code and group.
- **IDs:** the Stash scene ID, plus the StashDB and other stash-box IDs, so the
  file can be matched exactly later.
- **Chapters:** scene markers, written as chapters.
- **Cover art:** the scene cover, embedded.

Requires Stash v0.27 or later, Python 3.8+ and the ffmpeg/ffprobe that Stash
already uses. No pip packages are needed. Installing MKVToolNix (`mkvpropedit`)
is optional; it makes MKV edits much faster.

## Install

1. Copy the `fileMetadataWriter` folder into your Stash plugins folder
   (`<Stash config folder>/plugins/`; in Docker this is usually `/root/.stash/plugins/`).
2. In Stash: **Settings → Plugins → Reload plugins**.
3. Open the plugin's settings, set your targets, and leave **Write changes** off
   for the first run.

## Tasks (Settings → Tasks → Plugin Tasks)

| Task | What it processes |
|---|---|
| Write metadata: Studio | Scenes from **Target studio** (plus child studios if **Include sub-studios** is on) |
| Write metadata: Performer | Scenes featuring **Target performer** |
| Write metadata: Tag | Scenes with **Target tag** (plus its sub-tags if **Include sub-tags** is on) |
| Write metadata: Entire library | Every scene |
| Restore originals from backups | Puts back every `.fmwbak` original and fixes Stash to match |

Targets accept a name, an alias or an ID. If a name is ambiguous, the task stops
and lists the matching IDs. Every task respects the dry-run setting.

## Settings

| Setting | Default | Effect |
|---|---|---|
| Write changes | off | **Off = dry run.** The log lists every file that would change and exactly what would change. Nothing is modified. |
| Target studio / performer / tag | — | Used by the matching task. |
| Include sub-studios / Include sub-tags | off | Widen the Studio / Tag task. |
| Only organized scenes | off | Skip scenes not marked organized. |
| Tags to leave out of the file | — | Comma-separated tag names, aliases or IDs. Each one **and all its sub-tags** stays out of the file. Put workflow tags under a parent like "Workflow" and list just that. |
| Clear stale metadata | off | Removes every existing title or tag that didn't come from Stash, such as release-group junk. Also removes chapters or cover art when Stash has none. Technical fields (brand, encoder, creation time) and per-stream info like audio language are always kept. |
| Don't write chapters | off | Chapters from markers are written by default. |
| Don't embed cover art | off | The cover is embedded by default (MP4, M4V, MKV). |
| Don't write Stash IDs | off | IDs are written by default. |
| Don't use mkvpropedit | off | Forces MKV/WebM through ffmpeg even when MKVToolNix is installed. |
| Keep a backup of each original | off | Keeps the original as `<file>.fmwbak` (Stash ignores this extension). Only the very first original is kept, even if the file is processed again later. |
| Skip the rescan afterwards | off | By default a scan of only the changed files is queued at the end. |
| Maximum files per run | 0 (no limit) | Good for trying a handful first. |

## What gets written where

| Stash data | MP4 / M4V | MOV | MKV / WebM |
|---|---|---|---|
| Title | title | title | segment title |
| Date | date | date | DATE_RELEASED |
| Studio | network (top-level parent studio) | network (top-level parent studio) | PUBLISHER (top-level parent studio) |
| Sub-studio (if the studio has a parent) | show | show | PRODUCTION_STUDIO |
| Performers | artist | artist | ARTIST (joined), plus one ACTOR per performer* |
| Tags (minus excluded) | genre, keywords, `tags: …` line at end of comment | genre, keywords, `tags: …` line at end of comment | GENRE, KEYWORDS, `tags: …` line at end of COMMENT |
| Details | description, synopsis | description, synopsis | DESCRIPTION |
| First URL | comment (line 1) | comment (line 1) | URL, and COMMENT (line 1) |
| Director | composer | composer | DIRECTOR |
| Studio code | episode_id | episode_id | CATALOG_NUMBER |
| First group (series) | grouping | grouping | TITLE at the collection level* |
| Rating | — | — | RATING (Stash's 0–100 converted to 0–5) |
| Stash scene ID | comment line `stash_scene_id: …` | STASH_SCENE_ID | STASH_SCENE_ID, and a COMMENT line |
| StashDB scene ID | comment line `stashdb_id: …` | STASHDB_ID | STASHDB_ID, and a COMMENT line |
| All stash-box IDs | comment lines `stashbox_id: <endpoint> <id>` | STASHBOX_IDS | STASHBOX_IDS, and COMMENT lines |
| Media type | Movie (`media_type` 9) | Movie (`media_type` 9) | CONTENT_TYPE = Movie |
| Markers | chapters | chapters | chapters |
| Cover | embedded cover art | — | `cover.jpg`/`cover.png` attachment |

\* **Needs MKVToolNix.** Matroska can hold repeated tags and tags that describe
the collection a file belongs to, but only `mkvpropedit` can write them. Without
it, the plugin writes a single joined `ACTOR` and a custom `COLLECTION` tag
instead. If you install MKVToolNix later, the next run converts those files to
the richer layout and removes the old `COLLECTION` tag. In-place edits keep any
existing MKV tags the plugin doesn't manage (unless **Clear stale metadata** is
on), including repeated, nested and track-specific tags.

Notes:

- **MP4 can't hold custom tag names** in the format that Plex, Windows and Apple
  read. So the URL and IDs go into the comment, one `key: value` per line, which
  is easy for any software or script to parse.
- **MOV** uses QuickTime's key/value metadata, which does allow custom names.
  ffmpeg can't embed cover art in that mode, so MOV files get everything except
  the cover.
- **WebM** gets everything except the cover, because the format doesn't allow
  attachments.
- **AVI, WMV, FLV, MPEG-TS** and other formats are skipped and counted in the summary.
- **Chapters:** each marker becomes a chapter, titled with the marker's title or,
  if it has none, its primary tag. Markers at the same moment are merged
  ("A / B"), and each chapter runs until the next one starts.
- **Covers** in formats other than JPEG or PNG (e.g. WebP) are converted to JPEG,
  which every reader supports.
- **Stash scene IDs** are local to your database. The StashDB ID is the one that
  stays meaningful forever.
- **Network and show:** for a sub-studio, the top-level studio of its chain is the
  network and the scene's own studio is the show. For a top-level studio, only
  network is set.
- **Upgrading from v2.0:** the `album` and `album_artist` values that v2.0 wrote
  are removed the next time a file is processed. Values that didn't come from
  the plugin are left alone.
- Multiple values are joined with `; `. Without **Clear stale metadata**, a field
  that's empty in Stash leaves whatever the file already had.
- Files that already match are skipped, so re-running a task only touches scenes
  you've edited since. Fingerprints aren't churned.

## How files are changed safely

**ffmpeg (MP4, MOV, and MKV/WebM without MKVToolNix):** every stream is copied
bit-for-bit into a temp file next to the original. Image attachments in MKVs,
such as posters, are carried over as attachments. The temp file is then
verified: same video, audio and attachment counts, same duration, and the tags,
chapters and cover all present. Only then is it swapped in atomically. If
anything fails, the original is never touched.

**mkvpropedit (MKV/WebM with MKVToolNix installed):** the file is edited in
place, so it isn't copied. This is much faster on big files, but the file is
modified directly, so turn on **Keep a backup** if you want an undo. If
mkvpropedit fails without changing anything, the plugin falls back to ffmpeg
automatically.

## How fingerprints stay correct

Changing a file's metadata changes its oshash and MD5. phash comes from the video
frames, which are never touched, so it doesn't change. For each file the plugin:

1. **Checks first.** The oshash on disk must match Stash's record, otherwise the
   file is skipped (it changed since the last scan). This also proves the
   plugin's hashing matches Stash's on your system before it relies on it.
2. **Moves generated files.** Previews, sprites, VTTs, marker previews,
   transcodes and heatmaps are renamed from the old hash to the new one,
   following whichever hash Stash uses for generated files. Nothing needs
   regenerating.
3. **Updates Stash.** The new fingerprints are saved, read back and checked;
   phash is confirmed intact. The scene keeps its file, stash IDs, play history,
   markers and everything else.
4. **Queues a rescan** of only the changed files, so Stash records the new size
   and modification time.

The restore task does the same in reverse. If updating Stash ever fails after a
file changed, the run stops immediately rather than leaving more files out of
sync. Every change is logged to `fmw_journal.jsonl` in the plugin folder: scene,
file, old and new hashes, renamed generated files and backup path.

**One unavoidable side effect:** the oshash and MD5 that StashDB or other
stash-boxes have on file won't match the rewritten files. phash matching keeps
working, and the StashDB ID is now inside the file anyway.

## Tips

- Don't run a library scan or generate task at the same time as this plugin.
- Without MKVToolNix, every rewrite copies the whole file. On a NAS, start with
  a studio or a small **Maximum files per run** to gauge speed.
- To install MKVToolNix in the official Docker image, use
  `apk add mkvtoolnix` (Alpine), or `apt install mkvtoolnix` on Debian/Ubuntu hosts.
- With **Keep a backup** on, delete the `.fmwbak` files once you're happy. After
  that, the restore task has nothing left to restore.
