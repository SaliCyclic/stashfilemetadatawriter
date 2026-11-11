#!/usr/bin/env python3
"""
File Metadata Writer - a Stash plugin.

Writes scene metadata from the Stash database into the scene's video files:
standard tags, Stash/stash-box IDs, scene markers as chapters, and the scene
cover as embedded art. Afterwards it keeps Stash consistent with the rewritten
files:

  * oshash / md5 fingerprints are recomputed and saved to Stash
    (phash is untouched: only container data changes, never a video frame)
  * generated files named after the old hash (previews, sprites, VTTs, marker
    previews, transcodes, heatmaps) are renamed to the new hash
  * a scan of only the changed files is queued at the end

A restore task puts back originals kept as backups, fixing Stash the same way.

Uses only the Python standard library plus ffmpeg/ffprobe (bundled with Stash),
and mkvpropedit (MKVToolNix) for fast in-place MKV edits when it is installed.
"""

import hashlib
import json
import os
import re
import shutil
import ssl
import struct
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from xml.sax.saxutils import escape as xml_escape

PLUGIN_ID = "fileMetadataWriter"
TMP_SUFFIX = ".fmwtmp"
BACKUP_SUFFIX = ".fmwbak"

# --------------------------------------------------------------------------- #
# Logging (Stash "raw" plugin interface: \x01 + level + \x02 + message on stderr)
# --------------------------------------------------------------------------- #


def _log(level, msg):
    for line in str(msg).splitlines() or [""]:
        sys.stderr.write(f"\x01{level}\x02{line}\n")
    sys.stderr.flush()


def log_debug(msg):
    _log("d", msg)


def log_info(msg):
    _log("i", msg)


def log_warn(msg):
    _log("w", msg)


def log_error(msg):
    _log("e", msg)


def log_progress(fraction):
    _log("p", f"{max(0.0, min(1.0, fraction)):.4f}")


def finish(output=None, error=None):
    out = {}
    if output is not None:
        out["output"] = output
    if error is not None:
        out["error"] = error
    print(json.dumps(out))
    sys.exit(0)


# --------------------------------------------------------------------------- #
# Stash API client
# --------------------------------------------------------------------------- #


class GraphQLError(Exception):
    pass


class FatalError(Exception):
    """A file was changed but Stash could not be brought back in line: stop the run."""


class Stash:
    def __init__(self, conn):
        scheme = conn.get("Scheme") or "http"
        host = conn.get("Host") or "localhost"
        if host in ("0.0.0.0", "::", "[::]", ""):
            host = "localhost"
        port = conn.get("Port") or 9999
        self.base = f"{scheme}://{host}:{port}"
        self.auth = {}
        cookie = conn.get("SessionCookie") or {}
        if cookie.get("Name") and cookie.get("Value"):
            self.auth["Cookie"] = f"{cookie['Name']}={cookie['Value']}"
        if conn.get("ApiKey"):
            self.auth["ApiKey"] = conn["ApiKey"]
        # The plugin talks to its own Stash instance; a self-signed cert is normal there.
        self.ssl_ctx = ssl._create_unverified_context() if scheme == "https" else None

    def call(self, query, variables=None):
        body = json.dumps({"query": query, "variables": variables or {}}).encode()
        headers = {"Content-Type": "application/json", "Accept": "application/json", **self.auth}
        req = urllib.request.Request(self.base + "/graphql", data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, context=self.ssl_ctx, timeout=300) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:500]
            raise GraphQLError(f"HTTP {e.code} from Stash: {detail}") from e
        except (urllib.error.URLError, OSError) as e:
            raise GraphQLError(f"can't reach Stash at {self.base}: {e}") from e
        if payload.get("errors"):
            raise GraphQLError("; ".join(err.get("message", str(err)) for err in payload["errors"]))
        return payload.get("data") or {}

    def get_bytes(self, path):
        req = urllib.request.Request(self.base + path, headers=dict(self.auth))
        try:
            with urllib.request.urlopen(req, context=self.ssl_ctx, timeout=120) as resp:
                return resp.read(), resp.headers.get("Content-Type", "")
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None, None
            raise


# --------------------------------------------------------------------------- #
# Hashing - must match Stash's implementations exactly
# --------------------------------------------------------------------------- #

OSHASH_CHUNK = 64 * 1024


def oshash(path):
    """Stash/OpenSubtitles hash: file size + uint64 sum of first and last 64 KiB."""
    size = os.path.getsize(path)
    if size < 8:
        raise ValueError("file too small to hash")
    chunk = min(OSHASH_CHUNK, size)
    total = size
    with open(path, "rb") as f:
        head = f.read(chunk)
        f.seek(max(0, size - chunk))
        tail = f.read(chunk)
    for buf in (head, tail):
        usable = len(buf) - (len(buf) % 8)
        for (word,) in struct.iter_unpack("<Q", buf[:usable]):
            total = (total + word) & 0xFFFFFFFFFFFFFFFF
    return f"{total:016x}"


def md5sum(path):
    h = hashlib.md5()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(8 * 1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


# --------------------------------------------------------------------------- #
# Containers and what goes into them
# --------------------------------------------------------------------------- #

# family: tag layout. cover: whether embedded cover art is supported alongside
# the metadata. MOV uses QuickTime key/value ("mdta") metadata because its classic
# atoms can't hold most fields; ffmpeg can't store cover art in that mode.
CONTAINERS = {
    ".mp4": {"family": "mp4", "muxer": "mp4", "args": [], "cover": True},
    ".m4v": {"family": "mp4", "muxer": "mp4", "args": [], "cover": True},
    ".mov": {"family": "mov", "muxer": "mov", "args": ["-movflags", "use_metadata_tags"], "cover": False},
    ".mkv": {"family": "mkv", "muxer": "matroska", "args": [], "cover": True},
    ".webm": {"family": "mkv", "muxer": "webm", "args": [], "cover": False},
}

# Container-level keys that describe the file itself, never removed by "clear stale".
TECH_KEYS = {"major_brand", "minor_version", "compatible_brands", "encoder", "creation_time"}

ID_FIELDS = {"stash_scene_id", "stashdb_id", "stashbox_ids"}

TAG_MAP = {
    # MP4/M4V: standard iTunes-style atoms read by Plex, Jellyfin, Windows, macOS,
    # VLC, MediaInfo and exiftool. MP4 can't hold custom keys, so the URL and
    # Stash IDs go into the comment as "key: value" lines (built in desired_tags).
    "mp4": {
        "title": ["title"],
        "date": ["date"],
        "studio": ["album_artist"],
        "performers": ["artist"],
        "tags": ["genre"],
        "details": ["description", "synopsis"],
        "director": ["composer"],
        "code": ["episode_id"],
        "group": ["album"],
    },
    # MOV: QuickTime key/value metadata, which also allows custom keys.
    "mov": {
        "title": ["title"],
        "date": ["date"],
        "studio": ["album_artist"],
        "performers": ["artist"],
        "tags": ["genre"],
        "details": ["description", "synopsis"],
        "url": ["comment"],
        "director": ["composer"],
        "code": ["episode_id"],
        "group": ["album"],
        "stash_scene_id": ["STASH_SCENE_ID"],
        "stashdb_id": ["STASHDB_ID"],
        "stashbox_ids": ["STASHBOX_IDS"],
    },
    # Matroska/WebM: official Matroska tag names plus custom keys for the IDs.
    "mkv": {
        "title": ["title"],
        "date": ["DATE_RELEASED"],
        "studio": ["PUBLISHER"],
        "performers": ["ARTIST", "ACTOR"],
        "tags": ["GENRE"],
        "details": ["DESCRIPTION"],
        "url": ["URL"],
        "director": ["DIRECTOR"],
        "code": ["CATALOG_NUMBER"],
        "group": ["COLLECTION"],
        "stash_scene_id": ["STASH_SCENE_ID"],
        "stashdb_id": ["STASHDB_ID"],
        "stashbox_ids": ["STASHBOX_IDS"],
    },
}


def scene_fields(scene, excluded_tag_ids):
    """Pull the values we write out of a scene, as plain strings (None = no value)."""

    def clean(v):
        if v is None:
            return None
        v = str(v).strip()
        return v or None

    performers = [p["name"] for p in scene.get("performers") or [] if p.get("name")]
    tags = sorted(
        t["name"] for t in scene.get("tags") or []
        if t.get("name") and str(t.get("id")) not in excluded_tag_ids
    )
    groups = [g["group"]["name"] for g in scene.get("groups") or [] if (g.get("group") or {}).get("name")]
    urls = [u for u in scene.get("urls") or [] if u]
    stash_ids = [
        (s.get("endpoint") or "", s["stash_id"]) for s in scene.get("stash_ids") or [] if s.get("stash_id")
    ]
    stashdb = next((sid for ep, sid in stash_ids if "stashdb.org" in ep), None)
    return {
        "title": clean(scene.get("title")),
        "date": clean(scene.get("date")),
        "studio": clean((scene.get("studio") or {}).get("name")),
        "performers": clean("; ".join(performers)),
        "tags": clean("; ".join(tags)),
        "details": clean(scene.get("details")),
        "url": clean(urls[0]) if urls else None,
        "director": clean(scene.get("director")),
        "code": clean(scene.get("code")),
        "group": clean(groups[0]) if groups else None,
        "stash_scene_id": str(scene["id"]),
        "stashdb_id": stashdb,
        "stashbox_ids": clean("; ".join(f"{ep} {sid}" for ep, sid in stash_ids)),
        "_stash_ids": stash_ids,
    }


def desired_tags(fields, family, include_ids):
    out = {}
    for field, keys in TAG_MAP[family].items():
        if field in ID_FIELDS and not include_ids:
            continue
        value = fields.get(field)
        if value is None:
            continue
        for key in keys:
            out[key] = value
    if family == "mp4":
        lines = [fields["url"]] if fields.get("url") else []
        if include_ids:
            lines.append(f"stash_scene_id: {fields['stash_scene_id']}")
            if fields.get("stashdb_id"):
                lines.append(f"stashdb_id: {fields['stashdb_id']}")
            lines += [f"stashbox_id: {ep} {sid}" for ep, sid in fields["_stash_ids"]]
        if lines:
            out["comment"] = "\n".join(lines)
    if family in ("mp4", "mov") and "album" not in out and fields.get("studio"):
        out["album"] = fields["studio"]
    return out


def normalise(value):
    return (value or "").replace("\r\n", "\n").strip()


def tag_diff(current, desired):
    """Return {key: (old, new)} for tags that differ. Key comparison is case-insensitive."""
    cur = {k.lower(): v for k, v in (current or {}).items()}
    diff = {}
    for key, new in desired.items():
        old = cur.get(key.lower())
        if normalise(old) != normalise(new):
            diff[key] = (old, new)
    return diff


def build_chapters(markers, duration):
    """Scene markers -> contiguous chapters. Markers at the same time are merged."""
    points = []
    for m in sorted(markers, key=lambda m: float(m.get("seconds") or 0)):
        start = max(0.0, float(m.get("seconds") or 0))
        title = (m.get("title") or "").strip() or ((m.get("primary_tag") or {}).get("name") or "").strip() or "Chapter"
        title = " ".join(title.split())
        if points and abs(points[-1][0] - start) < 0.001:
            if title not in points[-1][1].split(" / "):
                points[-1][1] += " / " + title
            continue
        points.append([start, title])
    if duration:
        points = [p for p in points if p[0] < duration - 0.05]
    chapters = []
    for i, (start, title) in enumerate(points):
        end = points[i + 1][0] if i + 1 < len(points) else (duration or start + 1.0)
        chapters.append({"start": start, "end": end, "title": title})
    return chapters


def chapter_key(chapters):
    return [(round(c["start"], 1), c["title"]) for c in chapters]


def ffmeta_escape(s):
    return re.sub(r"([=;#\\])", r"\\\1", s)


def write_ffmetadata(path, chapters):
    lines = [";FFMETADATA1"]
    for c in chapters:
        lines += [
            "[CHAPTER]", "TIMEBASE=1/1000",
            f"START={int(round(c['start'] * 1000))}",
            f"END={int(round(c['end'] * 1000))}",
            f"title={ffmeta_escape(c['title'])}",
        ]
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


def mkv_time(sec):
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return f"{int(h):02d}:{int(m):02d}:{s:012.9f}"


def write_mkv_chapters_xml(path, chapters):
    atoms = "".join(
        "<ChapterAtom>"
        f"<ChapterTimeStart>{mkv_time(c['start'])}</ChapterTimeStart>"
        f"<ChapterTimeEnd>{mkv_time(c['end'])}</ChapterTimeEnd>"
        f"<ChapterDisplay><ChapterString>{xml_escape(c['title'])}</ChapterString>"
        "<ChapterLanguage>und</ChapterLanguage></ChapterDisplay>"
        "</ChapterAtom>"
        for c in chapters
    )
    with open(path, "w", encoding="utf-8") as f:
        f.write('<?xml version="1.0" encoding="UTF-8"?>\n'
                f"<Chapters><EditionEntry>{atoms}</EditionEntry></Chapters>\n")


def write_mkv_tags_xml(path, tags):
    simples = "".join(
        f"<Simple><Name>{xml_escape(k)}</Name><String>{xml_escape(v)}</String></Simple>"
        for k, v in tags.items()
    )
    with open(path, "w", encoding="utf-8") as f:
        f.write('<?xml version="1.0" encoding="UTF-8"?>\n'
                "<Tags><Tag><Targets><TargetTypeValue>50</TargetTypeValue></Targets>"
                f"{simples}</Tag></Tags>\n")


# --------------------------------------------------------------------------- #
# Reading files
# --------------------------------------------------------------------------- #


class FileInfo:
    def __init__(self, data, family):
        fmt = data.get("format") or {}
        self.tags = fmt.get("tags") or {}
        try:
            self.duration = float(fmt.get("duration"))
        except (TypeError, ValueError):
            self.duration = None
        self.streams = []
        for s in data.get("streams") or []:
            tags = s.get("tags") or {}
            lower = {k.lower(): v for k, v in tags.items()}
            self.streams.append({
                "index": s.get("index"),
                "type": s.get("codec_type") or "unknown",
                "codec": s.get("codec_name") or "",
                "tag_string": s.get("codec_tag_string") or "",
                "pic": bool((s.get("disposition") or {}).get("attached_pic")),
                "filename": lower.get("filename") or "",
                "mimetype": lower.get("mimetype") or "",
            })
        self.chapters = [
            (round(float(c.get("start_time") or 0), 1), (c.get("tags") or {}).get("title") or "")
            for c in data.get("chapters") or []
        ]
        self.family = family

    def is_cover(self, s):
        if not s["pic"]:
            return False
        if self.family == "mkv":
            return s["filename"].lower().startswith("cover")
        return True

    @property
    def covers(self):
        return [s for s in self.streams if self.is_cover(s)]

    def count(self, kind):
        return sum(1 for s in self.streams if s["type"] == kind and not s["pic"])

    @property
    def other_pics(self):
        return sum(1 for s in self.streams if s["pic"] and not self.is_cover(s))


def probe(ffprobe, path, family):
    cmd = [ffprobe, "-v", "error", "-show_format", "-show_streams", "-show_chapters", "-of", "json", path]
    res = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if res.returncode != 0:
        raise RuntimeError(f"ffprobe failed: {res.stderr.strip()[:300]}")
    return FileInfo(json.loads(res.stdout or "{}"), family)


def extract_stream(ffmpeg, path, index):
    res = subprocess.run(
        [ffmpeg, "-v", "error", "-nostdin", "-i", path, "-map", f"0:{index}", "-c", "copy", "-f", "image2pipe", "-"],
        capture_output=True,
    )
    return res.stdout if res.returncode == 0 and res.stdout else None


# --------------------------------------------------------------------------- #
# Writing files
# --------------------------------------------------------------------------- #


class Plan:
    """Everything that should change in one file."""

    def __init__(self):
        self.tags = {}          # key -> value to write
        self.tag_changes = {}   # key -> (old, new)
        self.stale = []         # existing keys to drop (clear stale)
        self.chapters = None    # None = leave alone, [] = remove, [..] = write
        self.cover = None       # None = leave alone, ("set", bytes, ext, mime) or ("remove",)

    @property
    def empty(self):
        return not (self.tag_changes or self.stale or self.chapters is not None or self.cover)

    def describe(self):
        parts = [f"{k}: {short(o)!r} -> {short(n)!r}" for k, (o, n) in self.tag_changes.items()]
        if self.stale:
            parts.append("remove " + ", ".join(self.stale))
        if self.chapters:
            parts.append(f"{len(self.chapters)} chapter(s) from markers")
        elif self.chapters == []:
            parts.append("remove chapters")
        if self.cover:
            parts.append("embed cover" if self.cover[0] == "set" else "remove cover")
        return ", ".join(parts)


def remux(ffmpeg, src, dst, container, info, plan, clear_stale, workdir):
    """Rewrite src into dst with every stream copied untouched. Returns warnings."""
    family, muxer = container["family"], container["muxer"]
    inputs = ["-i", src]
    n_inputs = 1

    chapter_input = None
    if plan.chapters:
        chap_path = os.path.join(workdir, "chapters.txt")
        write_ffmetadata(chap_path, plan.chapters)
        inputs += ["-f", "ffmetadata", "-i", chap_path]
        chapter_input, n_inputs = n_inputs, n_inputs + 1

    cover_path = None
    if plan.cover and plan.cover[0] == "set":
        cover_path = os.path.join(workdir, f"cover.{plan.cover[2]}")
        with open(cover_path, "wb") as f:
            f.write(plan.cover[1])

    cover_input = None
    if cover_path and family == "mp4":
        inputs += ["-i", cover_path]
        cover_input, n_inputs = n_inputs, n_inputs + 1

    # Global metadata: keep what's there unless clearing stale tags.
    meta = ["-map_metadata", "0"]
    if clear_stale:
        meta = ["-map_metadata:g", "-1"]
        created = next((v for k, v in info.tags.items() if k.lower() == "creation_time"), None)
        if created:
            meta += ["-metadata", f"creation_time={created}"]
    else:
        # drop existing keys that only differ from ours by case, so they don't duplicate
        ours = {k.lower(): k for k in plan.tags}
        for k in info.tags:
            if k.lower() in ours and k != ours[k.lower()]:
                meta += ["-metadata", f"{k}="]
    for key, value in plan.tags.items():
        meta += ["-metadata", f"{key}={value}"]

    if plan.chapters:
        chaps = ["-map_chapters", str(chapter_input)]
    elif plan.chapters == []:
        chaps = ["-map_chapters", "-1"]
    else:
        chaps = ["-map_chapters", "0"]

    def build(minimal):
        maps, post, attach = [], [], []
        out_idx = 0
        for s in info.streams:
            # MP4/MOV chapter text track: ffmpeg regenerates it from the chapters
            if family in ("mp4", "mov") and s["type"] == "data" and s["codec"] == "bin_data" and s["tag_string"] == "text":
                continue
            if s["pic"]:
                if info.is_cover(s) and plan.cover:
                    continue  # replaced or removed
                if family == "mkv":
                    # Matroska image attachments must be re-attached, or the muxer
                    # turns them into video tracks.
                    data = extract_stream(ffmpeg, src, s["index"])
                    if data is None:
                        raise RuntimeError(f"couldn't read attachment {s['filename'] or s['index']}")
                    p = os.path.join(workdir, f"attachment_{s['index']}")
                    with open(p, "wb") as f:
                        f.write(data)
                    attach.append((p, s["filename"] or f"attachment_{s['index']}", s["mimetype"] or "image/jpeg"))
                    continue
            if minimal and s["type"] in ("data", "unknown"):
                continue
            maps += ["-map", f"0:{s['index']}"]
            out_idx += 1
        if cover_input is not None:
            maps += ["-map", f"{cover_input}:0"]
            post += [f"-disposition:{out_idx}", "attached_pic"]
            out_idx += 1
        if cover_path and family == "mkv":
            attach.append((cover_path, f"cover.{plan.cover[2]}", plan.cover[3]))
        attach_args = []
        for p, name, mime in attach:
            attach_args += ["-attach", p]
            post += [f"-metadata:s:{out_idx}", f"mimetype={mime}", f"-metadata:s:{out_idx}", f"filename={name}"]
            out_idx += 1
        return (
            [ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error", "-y"] + inputs + maps + attach_args
            + ["-c", "copy"] + meta + chaps + post + container["args"] + ["-f", muxer, dst]
        )

    last_err = ""
    for minimal in (False, True):
        res = subprocess.run(build(minimal), capture_output=True, text=True, encoding="utf-8", errors="replace")
        if res.returncode == 0:
            return ["data streams that can't be copied were left out (video, audio, subtitles and attachments were kept)"] if minimal else []
        last_err = res.stderr.strip()[:500]
        if os.path.exists(dst):
            os.remove(dst)
    raise RuntimeError(f"ffmpeg failed: {last_err}")


def edit_mkv_in_place(mkvpropedit, path, info, plan, clear_stale, workdir):
    """Apply the plan to an MKV/WebM file in place with mkvpropedit."""
    cmd = [mkvpropedit, path]
    current_title = next((k for k in info.tags if k.lower() == "title"), None)
    if "title" in plan.tags:
        cmd += ["--edit", "info", "--set", f"title={plan.tags['title']}"]
    elif clear_stale and current_title:
        cmd += ["--edit", "info", "--delete", "title"]

    ours = {k.lower() for k in plan.tags}
    final = {} if clear_stale else {
        k: v for k, v in info.tags.items() if k.lower() not in ours and k.lower() != "title"
    }
    final.update({k: v for k, v in plan.tags.items() if k != "title"})
    if final:
        tags_xml = os.path.join(workdir, "tags.xml")
        write_mkv_tags_xml(tags_xml, final)
        cmd += ["--tags", f"global:{tags_xml}"]
    else:
        cmd += ["--tags", "global:"]

    if plan.chapters:
        chap_xml = os.path.join(workdir, "chapters.xml")
        write_mkv_chapters_xml(chap_xml, plan.chapters)
        cmd += ["--chapters", chap_xml]
    elif plan.chapters == []:
        cmd += ["--chapters", ""]

    if plan.cover:
        for s in info.covers:
            if s["filename"]:
                cmd += ["--delete-attachment", f"name:{s['filename']}"]
        if plan.cover[0] == "set":
            cover_path = os.path.join(workdir, f"cover.{plan.cover[2]}")
            with open(cover_path, "wb") as f:
                f.write(plan.cover[1])
            cmd += ["--attachment-name", f"cover.{plan.cover[2]}", "--attachment-mime-type", plan.cover[3],
                    "--add-attachment", cover_path]

    res = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if res.returncode not in (0, 1):  # 1 = finished with warnings
        raise RuntimeError(f"mkvpropedit failed: {(res.stdout + res.stderr).strip()[:500]}")


def verify_output(ffprobe, ffmpeg, before, out_path, plan, family):
    after = probe(ffprobe, out_path, family)
    for kind in ("video", "audio"):
        if after.count(kind) != before.count(kind):
            raise RuntimeError(f"{kind} stream count changed ({before.count(kind)} -> {after.count(kind)})")
    if after.other_pics != before.other_pics:
        raise RuntimeError(f"attachment count changed ({before.other_pics} -> {after.other_pics})")
    a, b = before.duration, after.duration
    if a is not None and b is not None and abs(a - b) > 1.0:
        raise RuntimeError(f"duration changed ({a:.2f}s -> {b:.2f}s)")
    missing = tag_diff(after.tags, plan.tags)
    if missing:
        raise RuntimeError(f"tags did not stick: {', '.join(missing)}")
    if plan.chapters is not None and after.chapters != chapter_key(plan.chapters):
        raise RuntimeError("chapters did not come out as expected")
    if plan.cover and plan.cover[0] == "set":
        covers = after.covers
        if len(covers) != 1 or hashlib.md5(extract_stream(ffmpeg, out_path, covers[0]["index"]) or b"").digest() != hashlib.md5(plan.cover[1]).digest():
            raise RuntimeError("cover art did not embed correctly")
    elif plan.cover:
        if after.covers:
            raise RuntimeError("old cover art is still present")
    return after


# --------------------------------------------------------------------------- #
# Generated files (named after the scene hash)
# --------------------------------------------------------------------------- #

HASH_PREFIX = re.compile(r"^([0-9a-f]{32}|[0-9a-f]{16})(?![0-9a-f])")


class GeneratedIndex:
    """Index of generated files/folders keyed by the hash at the start of their name.

    Built once, lazily, so a big generated folder is listed only one time per run.
    Covers screenshots/ (previews, webp), vtt/ (sprites, thumbs vtt), markers/<hash>/,
    transcodes/, interactive_heatmaps/ and any future folder using the same naming.
    """

    SKIP_DIRS = {"tmp", "download_stage"}

    def __init__(self, root):
        self.root = root
        self.index = None

    def _build(self):
        self.index = {}
        if not self.root or not os.path.isdir(self.root):
            log_warn(f"Generated folder not found ({self.root!r}); generated files won't be renamed")
            return
        for sub in os.scandir(self.root):
            if not sub.is_dir() or sub.name in self.SKIP_DIRS:
                continue
            try:
                entries = list(os.scandir(sub.path))
            except OSError as e:
                log_warn(f"Can't list {sub.path}: {e}")
                continue
            for entry in entries:
                m = HASH_PREFIX.match(entry.name)
                if m:
                    self.index.setdefault(m.group(1), []).append(entry.path)

    def rename(self, old_hash, new_hash):
        """Rename every generated entry for old_hash to new_hash. Returns [(old, new)]."""
        if not old_hash or not new_hash or old_hash == new_hash:
            return []
        if self.index is None:
            self._build()
        moved = []
        for path in list(self.index.get(old_hash, [])):
            folder, name = os.path.split(path)
            target = os.path.join(folder, new_hash + name[len(old_hash):])
            if os.path.exists(target):
                log_warn(f"Not renaming generated {path}: {target} already exists")
                continue
            os.replace(path, target)
            moved.append((path, target))
        self.index.pop(old_hash, None)
        if moved:
            self.index.setdefault(new_hash, []).extend(t for _, t in moved)
        return moved


# --------------------------------------------------------------------------- #
# Stash queries
# --------------------------------------------------------------------------- #

SCENE_FIELDS = """
  id title code details director date organized urls
  studio { name }
  performers { name }
  tags { id name }
  groups { group { name } }
  stash_ids { endpoint stash_id }
  scene_markers { title seconds primary_tag { name } }
  files { id path size fingerprints { type value } }
"""

SCENES_QUERY = (
    "query FindScenes($filter: FindFilterType, $scene_filter: SceneFilterType) {"
    " findScenes(filter: $filter, scene_filter: $scene_filter) { count scenes {" + SCENE_FIELDS + "} } }"
)

SCENE_BY_PATH_QUERY = """
query ScenesByPath($f: SceneFilterType) {
  findScenes(scene_filter: $f, filter: {per_page: 10}) { scenes { id files { id path fingerprints { type value } } } }
}
"""

MISSING_COVER_QUERY = """
query MissingCovers($f: SceneFilterType) {
  findScenes(scene_filter: $f, filter: {per_page: -1}) { scenes { id } }
}
"""

FILE_FP_QUERY = """
query SceneFiles($id: ID!) {
  findScene(id: $id) { files { id fingerprints { type value } } }
}
"""

SET_FP_MUTATION = """
mutation SetFingerprints($input: FileSetFingerprintsInput!) {
  fileSetFingerprints(input: $input)
}
"""

SCAN_MUTATION = """
mutation Scan($input: ScanMetadataInput!) { metadataScan(input: $input) }
"""

ENTITIES = {
    "studio": ("findStudio", "findStudios", "studios", "StudioFilterType", "studio_filter"),
    "performer": ("findPerformer", "findPerformers", "performers", "PerformerFilterType", "performer_filter"),
    "tag": ("findTag", "findTags", "tags", "TagFilterType", "tag_filter"),
}


def load_config(stash):
    base = "generatedPath videoFileNamingAlgorithm calculateMD5"
    general, last = None, None
    for fields in (base + " ffmpegPath ffprobePath", base):
        try:
            general = stash.call(f"query {{ configuration {{ general {{ {fields} }} }} }}")["configuration"]["general"]
            break
        except GraphQLError as e:
            last = e
    if general is None:
        raise last
    try:
        plugins = stash.call("query { configuration { plugins } }")["configuration"]["plugins"] or {}
    except GraphQLError:
        plugins = {}
    return general, plugins.get(PLUGIN_ID) or {}


def resolve_entity(stash, kind, value):
    one, many, key, ftype, farg = ENTITIES[kind]
    value = value.strip()
    if value.isdigit():
        found = stash.call(f"query($id: ID!) {{ {one}(id: $id) {{ id name }} }}", {"id": value})[one]
        if found:
            return found
    for field in ("name", "aliases"):
        res = stash.call(
            f"query($f: {ftype}) {{ {many}({farg}: $f, filter: {{per_page: 10}}) {{ {key} {{ id name }} }} }}",
            {"f": {field: {"value": value, "modifier": "EQUALS"}}},
        )[many][key]
        if len(res) == 1:
            return res[0]
        if len(res) > 1:
            names = ", ".join(f"{r['name']} (ID {r['id']})" for r in res)
            raise ValueError(f"{kind.capitalize()} {value!r} matches several: {names}. Use the ID instead.")
    raise ValueError(f"No {kind} named {value!r} was found.")


def resolve_excluded_tags(stash, text):
    """Tag names/IDs (comma separated) -> IDs of those tags and all their sub-tags."""
    ids, names = set(), []
    for item in re.split(r"[,\n]", text or ""):
        item = item.strip()
        if not item:
            continue
        try:
            tag = resolve_entity(stash, "tag", item)
        except ValueError as e:
            log_warn(f"Excluded tag ignored: {e}")
            continue
        ids.add(str(tag["id"]))
        names.append(tag["name"])
    if ids:
        res = stash.call(
            "query($f: TagFilterType) { findTags(tag_filter: $f, filter: {per_page: -1}) { tags { id } } }",
            {"f": {"parents": {"value": sorted(ids), "modifier": "INCLUDES", "depth": -1}}},
        )["findTags"]["tags"]
        ids |= {str(t["id"]) for t in res}
        log_info(f"Excluding {len(ids)} tag(s) from the genre field: {', '.join(names)} and their sub-tags")
    return ids


def iter_scenes(stash, scene_filter, per_page=50):
    page = 1
    total = None
    while True:
        res = stash.call(
            SCENES_QUERY,
            {"filter": {"page": page, "per_page": per_page, "sort": "id", "direction": "ASC"},
             "scene_filter": scene_filter},
        )["findScenes"]
        if total is None:
            total = res["count"]
            yield total
        if not res["scenes"]:
            return
        for scene in res["scenes"]:
            yield scene
        if page * per_page >= total:
            return
        page += 1


def fp_map(fingerprints):
    return {fp["type"]: fp["value"] for fp in fingerprints or [] if fp.get("value") is not None}


def short(v, n=70):
    if v is None:
        return None
    v = str(v).replace("\n", " | ")
    return v if len(v) <= n else v[: n - 1] + "…"


# --------------------------------------------------------------------------- #
# The run
# --------------------------------------------------------------------------- #


class Run:
    def __init__(self, stash, general, settings, plugin_dir):
        self.stash = stash
        self.settings = settings
        self.dry_run = not bool(settings.get("writeChanges"))
        self.keep_backup = bool(settings.get("keepBackup"))
        self.skip_rescan = bool(settings.get("skipRescan"))
        self.clear_stale = bool(settings.get("clearStaleTags"))
        self.with_chapters = not bool(settings.get("skipChapters"))
        self.with_cover = not bool(settings.get("skipCover"))
        self.with_ids = not bool(settings.get("skipStashIds"))
        try:
            self.max_files = int(settings.get("maxFiles") or 0)
        except (TypeError, ValueError):
            self.max_files = 0
        self.ffmpeg = general.get("ffmpegPath") or shutil.which("ffmpeg") or "ffmpeg"
        self.ffprobe = general.get("ffprobePath") or shutil.which("ffprobe") or "ffprobe"
        self.mkvpropedit = None if settings.get("disableMkvpropedit") else shutil.which("mkvpropedit")
        self.naming = (general.get("videoFileNamingAlgorithm") or "OSHASH").upper()
        self.calc_md5 = bool(general.get("calculateMD5"))
        self.generated = GeneratedIndex(general.get("generatedPath"))
        self.journal_path = os.path.join(plugin_dir or ".", "fmw_journal.jsonl")
        self.excluded_tags = set()
        self.missing_cover = set()
        self.cover_cache = {}
        self.changed_paths = []
        self.stats = {
            "written": 0, "would_write": 0, "restored": 0, "would_restore": 0, "up_to_date": 0,
            "unsupported_format": 0, "changed_since_scan": 0, "missing": 0, "errors": 0,
        }

    # ---------------------------------------------------------------- helpers

    def journal(self, record):
        record["time"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        with open(self.journal_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record) + "\n")

    @property
    def limit_reached(self):
        done = sum(self.stats[k] for k in ("written", "would_write", "restored", "would_restore"))
        return self.max_files > 0 and done >= self.max_files

    def load_missing_covers(self, scene_filter):
        if not self.with_cover:
            return
        try:
            res = self.stash.call(MISSING_COVER_QUERY, {"f": {**scene_filter, "is_missing": "cover"}})
            self.missing_cover = {str(s["id"]) for s in res["findScenes"]["scenes"]}
        except GraphQLError as e:
            log_debug(f"Couldn't list scenes without covers ({e}); will check each one")

    def cover_for(self, scene_id):
        """The scene cover as (bytes, ext, mime), or None if the scene has none."""
        scene_id = str(scene_id)
        if scene_id in self.cover_cache:
            return self.cover_cache[scene_id]
        cover = None
        if scene_id not in self.missing_cover:
            data, ctype = self.stash.get_bytes(f"/scene/{scene_id}/screenshot")
            if data:
                if data[:3] == b"\xff\xd8\xff":
                    cover = (data, "jpg", "image/jpeg")
                elif data[:8] == b"\x89PNG\r\n\x1a\n":
                    cover = (data, "png", "image/png")
                else:  # webp, gif, etc.: convert to JPEG, which every reader supports
                    res = subprocess.run(
                        [self.ffmpeg, "-v", "error", "-nostdin", "-i", "pipe:0", "-frames:v", "1",
                         "-c:v", "mjpeg", "-q:v", "2", "-f", "image2pipe", "pipe:1"],
                        input=data, capture_output=True,
                    )
                    if res.returncode == 0 and res.stdout:
                        cover = (res.stdout, "jpg", "image/jpeg")
                    else:
                        log_warn(f"Scene {scene_id}: cover image ({ctype}) couldn't be converted; skipping cover")
        self.cover_cache = {scene_id: cover}  # only keep the current scene's cover in memory
        return cover

    def sync_stash(self, scene_id, f, path, old_fp, record):
        """After a file changed on disk: new fingerprints, generated files, verification."""
        record.update({"scene_id": scene_id, "file_id": f["id"], "path": path, "old": old_fp})
        try:
            new_fp = {"oshash": oshash(path)}
            if "md5" in old_fp or self.calc_md5:
                new_fp["md5"] = md5sum(path)
            record["new"] = new_fp

            gen_type = "md5" if self.naming == "MD5" else "oshash"
            moved = self.generated.rename(old_fp.get(gen_type), new_fp.get(gen_type))
            record["generated_renamed"] = moved

            self.stash.call(
                SET_FP_MUTATION,
                {"input": {"id": f["id"], "fingerprints": [{"type": t, "value": v} for t, v in new_fp.items()]}},
            )
            files = self.stash.call(FILE_FP_QUERY, {"id": scene_id})["findScene"]["files"]
            stored = fp_map(next((x["fingerprints"] for x in files if str(x["id"]) == str(f["id"])), []))
            for t, v in new_fp.items():
                if stored.get(t) != v:
                    raise RuntimeError(f"Stash reports {t}={stored.get(t)} after update, expected {v}")
            if old_fp.get("phash") and stored.get("phash") != old_fp["phash"]:
                self.stash.call(
                    SET_FP_MUTATION,
                    {"input": {"id": f["id"], "fingerprints": [{"type": "phash", "value": old_fp["phash"]}]}},
                )
                log_warn(f"phash was dropped by Stash and has been restored: {path}")
        except Exception as e:
            record["error"] = str(e)
            self.journal(record)
            raise FatalError(
                f"{path} changed on disk but updating Stash failed: {e}. "
                f"Details for fixing it by hand are in {self.journal_path}"
            ) from e
        self.journal(record)
        self.changed_paths.append(path)
        return new_fp, moved

    # ------------------------------------------------------------ write mode

    def plan_for(self, scene, container, info):
        family = container["family"]
        plan = Plan()
        fields = scene_fields(scene, self.excluded_tags)
        plan.tags = desired_tags(fields, family, self.with_ids)
        plan.tag_changes = tag_diff(info.tags, plan.tags)
        if self.clear_stale:
            ours = {k.lower() for k in plan.tags}
            plan.stale = [k for k in info.tags if k.lower() not in ours and k.lower() not in TECH_KEYS]

        if self.with_chapters:
            markers = scene.get("scene_markers") or []
            if markers:
                chapters = build_chapters(markers, info.duration)
                if chapter_key(chapters) != info.chapters:
                    plan.chapters = chapters
            elif self.clear_stale and info.chapters:
                plan.chapters = []

        if self.with_cover and container["cover"]:
            want = self.cover_for(scene["id"])
            have = info.covers
            if want:
                current = extract_stream(self.ffmpeg, info.path, have[0]["index"]) if len(have) == 1 else None
                if current is None or hashlib.md5(current).digest() != hashlib.md5(want[0]).digest():
                    plan.cover = ("set",) + want
            elif self.clear_stale and have:
                plan.cover = ("remove",)
        return plan

    def process_file(self, scene, f):
        path = f["path"]
        ext = os.path.splitext(path)[1].lower()
        container = CONTAINERS.get(ext)
        if not container:
            self.stats["unsupported_format"] += 1
            log_debug(f"Skip (format {ext or 'unknown'} has no usable metadata support): {path}")
            return
        if not os.path.isfile(path):
            self.stats["missing"] += 1
            log_warn(f"Skip (file not found on disk): {path}")
            return

        info = probe(self.ffprobe, path, container["family"])
        info.path = path
        plan = self.plan_for(scene, container, info)
        if plan.empty:
            self.stats["up_to_date"] += 1
            log_debug(f"Up to date: {path}")
            return

        # Safety net: the file on disk must be the file Stash knows about. This also
        # proves our oshash implementation matches Stash's before anything is rewritten.
        old_fp = fp_map(f.get("fingerprints"))
        if old_fp.get("oshash"):
            current = oshash(path)
            if current != old_fp["oshash"]:
                self.stats["changed_since_scan"] += 1
                log_warn(f"Skip (file differs from Stash's record - oshash {current} vs "
                         f"{old_fp['oshash']}; run a scan first): {path}")
                return
        else:
            log_warn(f"Scene {scene['id']} file has no oshash in Stash; can't check it first: {path}")

        if self.dry_run:
            self.stats["would_write"] += 1
            log_info(f"[DRY RUN] Would update {path}\n    {plan.describe()}")
            return
        log_info(f"Writing {path}\n    {plan.describe()}")

        workdir = tempfile.mkdtemp(prefix="fmw_")
        try:
            if container["family"] == "mkv" and self.mkvpropedit:
                done = self.write_in_place(scene, f, path, container, info, plan, old_fp, workdir)
                if done:
                    return
            self.write_remux(scene, f, path, container, info, plan, old_fp, workdir)
        finally:
            shutil.rmtree(workdir, ignore_errors=True)

    def make_backup(self, path, move):
        """Keep the very first original: an existing backup is never overwritten."""
        backup = path + BACKUP_SUFFIX
        if os.path.exists(backup):
            return backup, False
        if move:
            os.replace(path, backup)
        else:
            shutil.copy2(path, backup)
        return backup, True

    def write_remux(self, scene, f, path, container, info, plan, old_fp, workdir):
        tmp = path + TMP_SUFFIX
        if os.path.exists(tmp):
            os.remove(tmp)  # left over from an interrupted run
        st = os.stat(path)
        try:
            for w in remux(self.ffmpeg, path, tmp, container, info, plan, self.clear_stale, workdir):
                log_warn(f"{path}: {w}")
            verify_output(self.ffprobe, self.ffmpeg, info, tmp, plan, container["family"])
            shutil.copymode(path, tmp)
            try:
                os.chown(tmp, st.st_uid, st.st_gid)
            except (OSError, AttributeError):
                pass
        except Exception as e:
            if os.path.exists(tmp):
                os.remove(tmp)
            raise RuntimeError(f"{e}; original left untouched") from e

        backup = None
        if self.keep_backup:
            backup, created = self.make_backup(path, move=True)
            if not created:
                os.remove(path)
        os.replace(tmp, path)  # atomic on the same filesystem
        self.after_write(scene, f, path, old_fp, backup)

    def write_in_place(self, scene, f, path, container, info, plan, old_fp, workdir):
        """mkvpropedit route. Returns False to fall back to remuxing."""
        backup = None
        created = False
        if self.keep_backup:
            backup, created = self.make_backup(path, move=False)
        error = None
        try:
            edit_mkv_in_place(self.mkvpropedit, path, info, plan, self.clear_stale, workdir)
        except Exception as e:
            error = e
        changed = oshash(path) != old_fp.get("oshash")
        if error and not changed:
            log_warn(f"{error} - nothing was changed; using ffmpeg instead")
            if created:
                os.remove(backup)
            return False
        if changed:
            self.after_write(scene, f, path, old_fp, backup)
        if error:
            raise RuntimeError(f"{error} (Stash was updated to match the file as it is now)")
        try:
            verify_output(self.ffprobe, self.ffmpeg, info, path, plan, container["family"])
        except Exception as e:
            hint = " Use the restore task to put the original back." if backup else ""
            raise RuntimeError(f"edited in place but check failed: {e}.{hint}") from e
        return True

    def after_write(self, scene, f, path, old_fp, backup):
        new_fp, moved = self.sync_stash(scene["id"], f, path, old_fp, {"action": "write", "backup": backup})
        self.stats["written"] += 1
        log_info(
            f"Done: oshash {old_fp.get('oshash')} -> {new_fp['oshash']}"
            + (", md5 updated" if "md5" in new_fp else "")
            + (f", {len(moved)} generated file(s) renamed" if moved else "")
        )

    # ---------------------------------------------------------- restore mode

    def restore_all(self):
        paths = []
        if os.path.exists(self.journal_path):
            with open(self.journal_path, encoding="utf-8") as fh:
                for line in fh:
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if rec.get("action", "write") == "write" and rec.get("backup") and rec["path"] not in paths:
                        paths.append(rec["path"])
        paths = [p for p in paths if os.path.exists(p + BACKUP_SUFFIX)]
        log_info(f"{len(paths)} backup(s) to restore")
        for i, path in enumerate(paths, 1):
            if self.limit_reached:
                log_info(f"Reached the limit of {self.max_files} file(s) for this run")
                break
            try:
                self.restore_one(path)
            except FatalError:
                raise
            except Exception as e:
                self.stats["errors"] += 1
                log_error(f"{path}: {e}")
            log_progress(i / len(paths))

    def restore_one(self, path):
        backup = path + BACKUP_SUFFIX
        scenes = self.stash.call(SCENE_BY_PATH_QUERY, {"f": {"path": {"value": path, "modifier": "EQUALS"}}})
        match = None
        for sc in scenes["findScenes"]["scenes"]:
            for f in sc["files"]:
                if f["path"] == path:
                    match = (sc, f)
        if not match:
            self.stats["missing"] += 1
            log_warn(f"Skip (no scene in Stash has this file any more): {path}")
            return
        scene, f = match
        current_fp = fp_map(f.get("fingerprints"))
        if not os.path.isfile(path):
            self.stats["missing"] += 1
            log_warn(f"Skip (file not found on disk): {path}")
            return
        if current_fp.get("oshash") and oshash(path) != current_fp["oshash"]:
            self.stats["changed_since_scan"] += 1
            log_warn(f"Skip (file differs from Stash's record; run a scan first): {path}")
            return
        if self.dry_run:
            self.stats["would_restore"] += 1
            log_info(f"[DRY RUN] Would restore the original of {path}")
            return
        os.replace(backup, path)
        new_fp, moved = self.sync_stash(scene["id"], f, path, current_fp, {"action": "restore"})
        self.stats["restored"] += 1
        log_info(f"Restored {path} (oshash {current_fp.get('oshash')} -> {new_fp['oshash']}"
                 + (f", {len(moved)} generated file(s) renamed" if moved else "") + ")")

    # --------------------------------------------------------------- finish

    def queue_rescan(self):
        if self.dry_run or self.skip_rescan or not self.changed_paths:
            return
        try:
            job = self.stash.call(SCAN_MUTATION, {"input": {"paths": self.changed_paths}})
            log_info(f"Queued a scan of the {len(self.changed_paths)} changed file(s) (job {job.get('metadataScan')})")
        except GraphQLError as e:
            log_warn(f"Couldn't queue the rescan ({e}). Run a scan of these files when convenient.")

    def summary(self, mode):
        s = self.stats
        if mode == "restore":
            head = (f"DRY RUN: {s['would_restore']} file(s) would be restored" if self.dry_run
                    else f"{s['restored']} file(s) restored")
            return f"{head}, {s['changed_since_scan']} changed since last scan, {s['missing']} missing, {s['errors']} error(s)"
        head = (f"DRY RUN: {s['would_write']} file(s) would be updated" if self.dry_run
                else f"{s['written']} file(s) updated")
        return (f"{head}, {s['up_to_date']} already up to date, {s['unsupported_format']} unsupported format, "
                f"{s['changed_since_scan']} changed since last scan, {s['missing']} missing, {s['errors']} error(s)")


def build_scope(stash, mode, settings):
    """Scene filter and a description for the chosen task."""
    scene_filter = {}
    if mode == "studio":
        target = (settings.get("targetStudio") or "").strip()
        if not target:
            raise ValueError("Set 'Target studio' in this plugin's settings first.")
        studio = resolve_entity(stash, "studio", target)
        depth = -1 if settings.get("includeSubStudios") else 0
        scene_filter["studios"] = {"value": [studio["id"]], "modifier": "INCLUDES", "depth": depth}
        scope = f"studio {studio['name']}" + (" and its sub-studios" if depth else "")
    elif mode == "performer":
        target = (settings.get("targetPerformer") or "").strip()
        if not target:
            raise ValueError("Set 'Target performer' in this plugin's settings first.")
        performer = resolve_entity(stash, "performer", target)
        scene_filter["performers"] = {"value": [performer["id"]], "modifier": "INCLUDES"}
        scope = f"performer {performer['name']}"
    elif mode == "tag":
        target = (settings.get("targetTag") or "").strip()
        if not target:
            raise ValueError("Set 'Target tag' in this plugin's settings first.")
        tag = resolve_entity(stash, "tag", target)
        depth = -1 if settings.get("includeSubTags") else 0
        scene_filter["tags"] = {"value": [tag["id"]], "modifier": "INCLUDES", "depth": depth}
        scope = f"tag {tag['name']}" + (" and its sub-tags" if depth else "")
    else:
        scope = "the entire library"
    if settings.get("onlyOrganized"):
        scene_filter["organized"] = True
        scope += " (organized scenes only)"
    return scene_filter, scope


def main():
    raw = sys.stdin.read()
    try:
        plugin_input = json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError:
        finish(error="Could not read plugin input from Stash")

    conn = plugin_input.get("server_connection") or {}
    mode = (plugin_input.get("args") or {}).get("mode")
    if mode not in ("studio", "performer", "tag", "all", "restore"):
        finish(error=f"Unknown task mode {mode!r}")

    stash = Stash(conn)
    try:
        general, settings = load_config(stash)
    except GraphQLError as e:
        finish(error=f"Couldn't read Stash configuration: {e}")

    run = Run(stash, general, settings, conn.get("PluginDir"))
    for tool in (run.ffmpeg, run.ffprobe):
        if not shutil.which(tool) and not os.path.isfile(tool):
            finish(error=f"Can't find {tool}. Check the ffmpeg/ffprobe paths in Stash's settings.")

    dry = "DRY RUN - no files will be changed. " if run.dry_run else ""
    try:
        if mode == "restore":
            log_info(f"{dry}Restoring originals from backups.")
            run.restore_all()
        else:
            scene_filter, scope = build_scope(stash, mode, settings)
            run.excluded_tags = resolve_excluded_tags(stash, settings.get("excludedTags"))
            run.load_missing_covers(scene_filter)
            features = [n for n, on in (("chapters", run.with_chapters), ("cover art", run.with_cover),
                                        ("Stash IDs", run.with_ids), ("clearing stale tags", run.clear_stale)) if on]
            log_info(f"{dry}Processing scenes for {scope}. Including: {', '.join(features) or 'tags only'}. "
                     f"MKV edits: {'in place with mkvpropedit' if run.mkvpropedit else 'ffmpeg remux'}. "
                     f"Generated files are named by {run.naming}.")
            scenes = iter_scenes(stash, scene_filter)
            total = next(scenes)
            log_info(f"{total} scene(s) to check")
            done = 0
            for scene in scenes:
                for f in scene.get("files") or []:
                    if run.limit_reached:
                        break
                    try:
                        run.process_file(scene, f)
                    except FatalError:
                        raise
                    except Exception as e:
                        run.stats["errors"] += 1
                        log_error(f"Scene {scene['id']} - {f.get('path')}: {e}")
                done += 1
                log_progress(done / total if total else 1.0)
                if run.limit_reached:
                    log_info(f"Reached the limit of {run.max_files} file(s) for this run")
                    break
    except ValueError as e:
        finish(error=str(e))
    except FatalError as e:
        run.queue_rescan()
        log_error(str(e))
        finish(error=f"Stopped: {e}")
    except GraphQLError as e:
        run.queue_rescan()
        finish(error=f"Stash API error: {e}")

    run.queue_rescan()
    summary = run.summary(mode)
    log_info(summary)
    finish(output=summary)


if __name__ == "__main__":
    main()
