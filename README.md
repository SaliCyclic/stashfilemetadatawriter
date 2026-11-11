# File Metadata Writer for Stash

A [Stash](https://github.com/stashapp/stash) plugin that writes everything you've
curated about a scene **into the video file itself**. Your metadata then travels
with the file into any media server or library tool you use later, even without
Stash.

## What it writes

- **Tags:** title, date, studio, performers, tags, details, URL, director, studio
  code and group, using the standard fields that Plex, Jellyfin, Kodi, Windows,
  macOS, VLC, MediaInfo and exiftool read.
- **IDs:** the Stash scene ID and StashDB / stash-box IDs, so the file can be
  matched exactly later.
- **Chapters:** scene markers, written as chapters.
- **Cover art:** the scene cover, embedded.

Supported formats: **MP4, M4V, MOV, MKV and WebM**. Other formats are skipped and
listed in the task summary.

## Why it's safe to run on a real library

Changing a file's metadata changes the fingerprints Stash uses to recognise it.
The plugin handles that, so nothing gets orphaned or re-imported:

- **It checks first.** A file is only touched if it still matches Stash's record.
- **Video and audio are never re-encoded.** Every stream is copied bit-for-bit,
  and the result is verified before it replaces the original.
- **Stash is updated to match.** New fingerprints are saved, and generated
  previews, sprites and marker videos are renamed to follow the file. The phash
  doesn't change, since the video frames are untouched.
- **It's a dry run by default.** The log shows exactly what would change until
  you switch on **Write changes**.
- **Changes can be undone.** With **Keep a backup** on, a restore task puts the
  originals back and reverses the Stash changes.

## Tasks

- **Write metadata: Studio**, **Performer** or **Tag**: process scenes for the
  target set in the plugin settings.
- **Write metadata: Entire library**: process every scene.
- **Restore originals from backups**: undo previous writes.

Settings let you leave specific tags (and their sub-tags) out of the file, strip
leftover junk metadata such as release-group titles, limit a run to a few files,
and turn chapters, covers or IDs off. MKV files are edited in place, which is much
faster, if [MKVToolNix](https://mkvtoolnix.download/) is installed.

## Install

1. In Stash, go to **Settings → Plugins → Available Plugins → Add source**.
2. Add this URL:
   ```
   https://salicyclic.github.io/stashfilemetadatawriter/main/index.yml
   ```
3. Expand the new source, tick **File Metadata Writer**, and click **Install**.

Requires Stash v0.27 or later and Python 3. No extra Python packages are needed.

## Full documentation

See the [plugin README](plugins/fileMetadataWriter/README.md) for every setting,
the exact tag written in each format, and how fingerprints are kept in sync.

## License

[AGPL-3.0](LICENCE)
