"""Read the artist and title out of a music file itself.

The client's point is a good one: an MP3 or FLAC nearly always carries the
artist in its tags, and a tag is a fact where a filename is only a guess. So
where the tags are there, they are used, and the guessing code is never asked.

Written from the file formats directly - no third-party library, because the
app ships a trimmed Python runtime and must keep working with nothing installed.
Only the header of a file is read (a few kilobytes), never the audio, so a
folder of 500 tracks is still instant.

Nothing here raises. A damaged or unusual file simply reports no tags, and the
name-based rules take over exactly as before.
"""

import os
import struct

# Only these are opened. Everything else is left alone.
AUDIO_EXTENSIONS = {"mp3", "flac", "ogg", "opus", "m4a", "mp4", "wav"}

_ID3_TEXT_FRAMES = {
    # v2.3 / v2.4          v2.2
    "TPE1": "artist", "TP1": "artist",
    "TIT2": "title", "TT2": "title",
    "TALB": "album", "TAL": "album",
    "TRCK": "track", "TRK": "track",
    "TPE2": "albumartist", "TP2": "albumartist",
    "TYER": "year", "TYE": "year",
    "TDRC": "year",
}

_VORBIS_KEYS = {
    "artist": "artist",
    "title": "title",
    "album": "album",
    "tracknumber": "track",
    "albumartist": "albumartist",
    "date": "year",
}

# Enough for the tag block of any sanely written file. A tag bigger than this is
# nearly always an embedded cover, which is of no use here.
_MAX_TAG = 2 * 1024 * 1024


def _decode(raw, encoding):
    """ID3 text frames carry their encoding in the first byte."""
    try:
        if encoding == 0:
            return raw.decode("latin-1")
        if encoding == 1:
            return raw.decode("utf-16")
        if encoding == 2:
            return raw.decode("utf-16-be")

        return raw.decode("utf-8")
    except (UnicodeDecodeError, LookupError):
        return raw.decode("latin-1", "replace")


def _clean(text):
    # Tags routinely end in a NUL, and some writers pad with them.
    return text.replace("\x00", " ").strip()


def _synchsafe(data):
    """ID3v2 sizes use 7 bits per byte, so the size can never contain 0xFF."""
    return (data[0] << 21) | (data[1] << 14) | (data[2] << 7) | data[3]


def _read_id3v2(handle):
    handle.seek(0)
    header = handle.read(10)

    if len(header) < 10 or header[:3] != b"ID3":
        return {}

    major = header[3]
    flags = header[5]
    size = _synchsafe(header[6:10])

    if size <= 0 or size > _MAX_TAG:
        return {}

    body = handle.read(size)

    if flags & 0x40:
        # Extended header: skip it. Its own length is the first four bytes.
        if major >= 4 and len(body) >= 4:
            body = body[_synchsafe(body[:4]):]
        elif len(body) >= 4:
            body = body[4 + struct.unpack(">I", body[:4])[0]:]

    out = {}
    pos = 0
    id_len = 3 if major == 2 else 4
    head_len = 6 if major == 2 else 10

    while pos + head_len <= len(body):
        frame_id = body[pos:pos + id_len].decode("latin-1", "replace")

        if not frame_id.strip("\x00 "):
            break  # padding: the rest of the tag is empty

        if major == 2:
            frame_size = (body[pos + 3] << 16) | (body[pos + 4] << 8) | body[pos + 5]
        elif major >= 4:
            frame_size = _synchsafe(body[pos + 4:pos + 8])
        else:
            frame_size = struct.unpack(">I", body[pos + 4:pos + 8])[0]

        pos += head_len

        if frame_size <= 0 or pos + frame_size > len(body):
            break

        field = _ID3_TEXT_FRAMES.get(frame_id)

        if field and field not in out:
            raw = body[pos:pos + frame_size]

            if raw:
                value = _clean(_decode(raw[1:], raw[0]))

                if value:
                    out[field] = value

        pos += frame_size

    return out


def _read_id3v1(handle):
    try:
        handle.seek(-128, os.SEEK_END)
    except OSError:
        return {}

    block = handle.read(128)

    if len(block) < 128 or block[:3] != b"TAG":
        return {}

    def field(start, length):
        return _clean(block[start:start + length].decode("latin-1", "replace"))

    out = {}

    for name, value in (("title", field(3, 30)), ("artist", field(33, 30)),
                        ("album", field(63, 30)), ("year", field(93, 4))):
        if value:
            out[name] = value

    # Track number lives in the last comment byte in ID3v1.1.
    if block[125] == 0 and block[126]:
        out["track"] = str(block[126])

    return out


def _read_vorbis_comment(block):
    out = {}

    try:
        vendor_len = struct.unpack("<I", block[:4])[0]
        pos = 4 + vendor_len
        count = struct.unpack("<I", block[pos:pos + 4])[0]
        pos += 4

        for _ in range(count):
            length = struct.unpack("<I", block[pos:pos + 4])[0]
            pos += 4
            entry = block[pos:pos + length].decode("utf-8", "replace")
            pos += length

            if "=" not in entry:
                continue

            key, value = entry.split("=", 1)
            field = _VORBIS_KEYS.get(key.strip().lower())

            if field and field not in out and value.strip():
                out[field] = value.strip()
    except (struct.error, IndexError, ValueError):
        return out

    return out


def _read_flac(handle):
    handle.seek(0)

    if handle.read(4) != b"fLaC":
        return {}

    while True:
        header = handle.read(4)

        if len(header) < 4:
            return {}

        last = bool(header[0] & 0x80)
        block_type = header[0] & 0x7F
        length = (header[1] << 16) | (header[2] << 8) | header[3]

        if length > _MAX_TAG:
            return {}

        if block_type == 4:
            return _read_vorbis_comment(handle.read(length))

        if last:
            return {}

        handle.seek(length, os.SEEK_CUR)


def read(path):
    """Return {'artist': ..., 'title': ..., 'track': ...} or {} - never raises."""
    try:
        ext = os.path.splitext(path)[1].lower().lstrip(".")

        if ext not in AUDIO_EXTENSIONS:
            return {}

        with open(path, "rb") as handle:
            if ext == "flac":
                found = _read_flac(handle)

                if found:
                    return found

                # Some FLAC files also carry an ID3 tag bolted on the front.
                return _read_id3v2(handle)

            found = _read_id3v2(handle)

            if not found.get("artist") or not found.get("title"):
                # Fall back to the older 128-byte tag at the end of the file.
                older = _read_id3v1(handle)

                for key, value in older.items():
                    found.setdefault(key, value)

            return found
    except (OSError, ValueError, struct.error, IndexError):
        return {}


def read_folder(paths):
    """Tags for a list of files, keyed by base name. Non-audio files are skipped."""
    out = {}

    for path in paths:
        found = read(path)

        if found:
            out[os.path.basename(path)] = found

    return out
