"""Rename rule engine.

Deliberately free of any filesystem access: everything here takes names and
returns names. That is what makes it testable, and it is why the preview the
user sees is produced by exactly the same code that later does the renaming -
there is no second implementation to drift out of step.

A rule is a plain dict with a "type" key. Rules run in order, each one taking
the output of the last, so a single pass can do what would otherwise be several.
"""

import os
import re
import unicodedata

# ---------------------------------------------------------------------------
# Windows naming limits. Getting these wrong is how a renamer produces a file
# the user can no longer open or delete.
# ---------------------------------------------------------------------------

ILLEGAL_CHARS = '<>:"/\\|?*'

RESERVED_NAMES = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}

MAX_COMPONENT = 255


# Noise that download names arrive covered in. Order matters: longer, more
# specific tags first so "WEB-DL" is not half-eaten by "WEB".
JUNK_TOKENS = [
    "2160p", "1080p", "1080i", "720p", "576p", "480p",
    "x264", "x265", "h264", "h265", "hevc", "xvid", "divx", "av1",
    "web-dl", "webrip", "web", "bluray", "blu-ray", "brrip", "bdrip",
    "dvdrip", "dvdscr", "hdrip", "hdtv", "hdcam", "camrip", "cam",
    "remux", "proper", "repack", "extended", "uncut", "unrated",
    "dts-hd", "dts", "truehd", "ddp5.1", "dd5.1", "ac3", "aac2.0", "aac",
    "flac", "mp3", "opus", "atmos", "5.1", "7.1", "2.0",
    "10bit", "8bit", "hdr10", "hdr", "sdr", "imax",
    "multi", "dual", "dubbed", "subbed", "sub", "ita", "eng", "nordic",
    "yify", "yts", "rarbg", "sparks", "evo", "fgt", "ntb", "cmrg", "galaxyrg",
    "amzn", "nf", "dsnp", "hmax", "atvp", "hulu",
    "official", "video", "audio", "lyrics", "lyric", "hq", "hd", "4k",
    "full", "album", "remastered", "remaster",
]

# S01E02 / s1e2 / 1x02 / Season 1 Episode 2 / 102
_EPISODE_PATTERNS = [
    re.compile(r"\bs\s*(?P<season>\d{1,2})\s*[\s._-]*e\s*(?P<episode>\d{1,3})\b", re.I),
    re.compile(r"\b(?P<season>\d{1,2})\s*x\s*(?P<episode>\d{1,3})\b", re.I),
    re.compile(r"\bseason\s*(?P<season>\d{1,2})\s*(?:,|-|\s)*\s*episode\s*(?P<episode>\d{1,3})\b", re.I),
]

# A bare 3-4 digit code like "102" = season 1 episode 02. Only trusted when it
# stands alone as a word, otherwise every resolution and year matches.
_BARE_EPISODE = re.compile(r"\b(?P<season>[1-9])(?P<episode>\d{2})\b")

_YEAR = re.compile(r"\b(19|20)\d{2}\b")

# A leading track number: "01 ", "01. ", "01 - ", "(01)"
_LEADING_TRACK = re.compile(r"^\s*[\(\[]?\s*(\d{1,3})\s*[\)\]]?\s*[-._)\]]*\s+")

# The various dashes people and websites use between artist and title.
_DASHES = "‐‑‒–—―⁃−"


def split_name(filename):
    """Split into (stem, ext). ext keeps its dot, or is '' when there is none."""
    stem, ext = os.path.splitext(filename)

    # A leading dot is part of the name, not an extension: ".gitignore"
    if not stem and ext:
        return ext, ""

    return stem, ext


def _collapse(text):
    """Squeeze runs of whitespace and tidy spacing around punctuation."""
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\s*-\s*-\s*", " - ", text)
    text = re.sub(r"\(\s*\)|\[\s*\]|\{\s*\}", "", text)
    text = re.sub(r"\s+([,.!?])", r"\1", text)

    return text.strip(" .-_")


def _normalise_separators(text):
    """Dots and underscores are separators in download names, not punctuation."""
    # Keep a decimal like 5.1 intact; only split dots that sit between letters
    # or at word boundaries.
    text = re.sub(r"(?<=[^\d])\.(?=[^\d])", " ", text)
    text = re.sub(r"(?<=\d)\.(?=[^\d\s])", " ", text)
    text = re.sub(r"(?<=[^\d\s])\.(?=\d)", " ", text)
    text = text.replace("_", " ")

    for dash in _DASHES:
        text = text.replace(dash, "-")

    return _collapse(text)


def strip_junk(text, drop_year=False, drop_brackets=True):
    """Remove release tags, bracketed noise and separator clutter."""
    text = _normalise_separators(text)

    if drop_brackets:
        # Bracketed groups in download names are nearly always tags, not title.
        text = re.sub(r"[\(\[\{][^\)\]\}]*[\)\]\}]", " ", text)

    for token in JUNK_TOKENS:
        text = re.sub(r"(?<![a-z0-9])" + re.escape(token) + r"(?![a-z0-9])", " ", text, flags=re.I)

    if drop_year:
        text = _YEAR.sub(" ", text)

    return _collapse(text)


def find_episode(text):
    """Return (show, season, episode) or None."""
    for pattern in _EPISODE_PATTERNS:
        match = pattern.search(text)

        if match:
            return (
                text[: match.start()],
                int(match.group("season")),
                int(match.group("episode")),
            )

    # Bare codes only after the obvious noise is gone, and never a year.
    cleaned = strip_junk(text)

    for match in _BARE_EPISODE.finditer(cleaned):
        if _YEAR.match(match.group(0)):
            continue

        return (
            cleaned[: match.start()],
            int(match.group("season")),
            int(match.group("episode")),
        )

    return None


def smart_title(text):
    """Title case that leaves small words and existing acronyms alone."""
    small = {
        "a", "an", "and", "as", "at", "but", "by", "for", "from", "in", "into",
        "nor", "of", "on", "onto", "or", "over", "the", "to", "up", "via", "with",
    }

    words = text.split(" ")
    out = []

    for i, word in enumerate(words):
        if not word:
            continue

        bare = re.sub(r"[^A-Za-z]", "", word)

        # Leave things like "BBC", "II", "S01E02" as they are.
        if bare and bare.isupper() and len(bare) > 1:
            out.append(word)
            continue

        lowered = word.lower()

        if 0 < i < len(words) - 1 and lowered in small:
            out.append(lowered)
        else:
            out.append(lowered[:1].upper() + lowered[1:])

    return " ".join(out)


# ---------------------------------------------------------------------------
# Presets
# ---------------------------------------------------------------------------


def preset_artist_song(stem):
    """Best effort at 'Artist - Song'."""
    text = strip_junk(stem)
    text = _LEADING_TRACK.sub("", text)

    parts = [p.strip() for p in re.split(r"\s-\s", text) if p.strip()]

    if len(parts) >= 2:
        artist = parts[0]
        song = " - ".join(parts[1:])
    else:
        # No separator to work with. Leave it recognisable rather than guessing.
        return smart_title(_collapse(text))

    return "{} - {}".format(smart_title(artist), smart_title(song))


def preset_show_episode(stem, style="S{season:02d}E{episode:02d}"):
    """Best effort at 'Showname S01E01'."""
    found = find_episode(stem)

    if not found:
        return smart_title(strip_junk(stem))

    show, season, episode = found
    show = strip_junk(show, drop_year=True)
    show = _collapse(show)

    if not show:
        show = "Unknown"

    marker = style.format(season=season, episode=episode)

    return "{} {}".format(smart_title(show), marker)


# ---------------------------------------------------------------------------
# Rules
# ---------------------------------------------------------------------------


def _rule_find_replace(stem, rule):
    find = rule.get("find", "")

    if not find:
        return stem

    replace = rule.get("replace", "")
    flags = 0 if rule.get("case_sensitive") else re.I

    if rule.get("regex"):
        try:
            return re.sub(find, replace, stem, flags=flags)
        except re.error:
            # A half-typed regex must not blow up the preview.
            return stem

    return re.sub(re.escape(find), replace.replace("\\", "\\\\"), stem, flags=flags)


def _rule_case(stem, rule):
    mode = rule.get("mode", "title")

    if mode == "lower":
        return stem.lower()
    if mode == "upper":
        return stem.upper()
    if mode == "title":
        return smart_title(stem)
    if mode == "sentence":
        lowered = stem.lower()
        return lowered[:1].upper() + lowered[1:]

    return stem


def _rule_insert(stem, rule):
    return "{}{}{}".format(rule.get("prefix", ""), stem, rule.get("suffix", ""))


def _rule_number(stem, rule, index):
    start = int(rule.get("start", 1))
    pad = int(rule.get("pad", 2))
    step = int(rule.get("step", 1))
    sep = rule.get("separator", " - ")
    number = str(start + index * step).zfill(pad)

    if rule.get("position") == "suffix":
        return "{}{}{}".format(stem, sep, number)

    return "{}{}{}".format(number, sep, stem)


def _rule_trim(stem, rule):
    count = int(rule.get("count", 0))

    if count <= 0:
        return stem

    if rule.get("side") == "end":
        return stem[:-count] if count < len(stem) else ""

    return stem[count:]


def apply_rules(stem, ext, rules, index=0):
    """Run every rule in order. Returns (stem, ext)."""
    for rule in rules or []:
        kind = rule.get("type")

        if kind == "preset_artist_song":
            stem = preset_artist_song(stem)
        elif kind == "preset_show_episode":
            stem = preset_show_episode(stem, rule.get("style", "S{season:02d}E{episode:02d}"))
        elif kind == "strip_junk":
            stem = strip_junk(stem, drop_year=bool(rule.get("drop_year")),
                              drop_brackets=rule.get("drop_brackets", True))
        elif kind == "find_replace":
            stem = _rule_find_replace(stem, rule)
        elif kind == "case":
            stem = _rule_case(stem, rule)
        elif kind == "insert":
            stem = _rule_insert(stem, rule)
        elif kind == "number":
            stem = _rule_number(stem, rule, index)
        elif kind == "trim":
            stem = _rule_trim(stem, rule)
        elif kind == "ext_lower":
            ext = ext.lower()
        elif kind == "set_ext":
            new = (rule.get("ext") or "").strip()
            if new:
                ext = new if new.startswith(".") else "." + new

        stem = stem.strip()

    return stem, ext


# ---------------------------------------------------------------------------
# Validation and planning
# ---------------------------------------------------------------------------


def name_problem(stem, ext):
    """Return a human-readable problem with this name, or None if it is fine."""
    name = stem + ext

    if not stem.strip():
        return "the new name would be empty"

    bad = sorted({c for c in name if c in ILLEGAL_CHARS})

    if bad:
        return "cannot contain {}".format(" ".join(bad))

    if any(ord(c) < 32 for c in name):
        return "contains a control character"

    if stem.upper() in RESERVED_NAMES or stem.split(".")[0].upper() in RESERVED_NAMES:
        return "'{}' is a name Windows reserves".format(stem)

    if name.endswith(" ") or name.endswith("."):
        return "cannot end with a space or a dot"

    if len(name) > MAX_COMPONENT:
        return "too long ({} characters, limit is {})".format(len(name), MAX_COMPONENT)

    return None


def plan(filenames, rules, existing=None):
    """Work out what every file would be renamed to.

    Args:
        filenames: names in the folder, in display order.
        rules: the rule list.
        existing: every name already in the folder, for collision checks.
                  Defaults to filenames.

    Returns a list of dicts, one per file, each with:
        old, new, status ('rename' | 'unchanged' | 'error'), reason.
    """
    existing_set = {n.lower() for n in (existing if existing is not None else filenames)}

    # Pass one: work out what each file wants to be called. Collisions cannot be
    # judged yet, because a name may be freed up by a file later in the list -
    # renaming 01,02,03 up to 02,03,04 is perfectly legal but every single step
    # looks like a clash if you only look backwards.
    rows = []

    for index, filename in enumerate(filenames):
        stem, ext = split_name(filename)

        try:
            new_stem, new_ext = apply_rules(stem, ext, rules, index)
        except Exception as exc:  # a bad rule must never kill the whole preview
            rows.append({"old": filename, "new": filename, "status": "error",
                         "reason": "rule failed: {}".format(exc)})
            continue

        new_name = "{}{}".format(new_stem, new_ext)

        if new_name == filename:
            rows.append({"old": filename, "new": filename, "status": "unchanged", "reason": ""})
            continue

        problem = name_problem(new_stem, new_ext)

        if problem:
            rows.append({"old": filename, "new": new_name, "status": "error", "reason": problem})
            continue

        rows.append({"old": filename, "new": new_name, "status": "rename", "reason": ""})

    # Pass two: now that every intention is known, judge the collisions.
    moving_away = {r["old"].lower() for r in rows if r["status"] == "rename"}

    wanted = {}

    for row in rows:
        if row["status"] == "rename":
            wanted.setdefault(row["new"].lower(), []).append(row)

    for key, claimants in wanted.items():
        if len(claimants) > 1:
            names = ", ".join("'{}'".format(r["old"]) for r in claimants)

            for row in claimants:
                row["status"] = "error"
                row["reason"] = "{} would all become '{}'".format(names, row["new"])

    for row in rows:
        if row["status"] != "rename":
            continue

        key = row["new"].lower()

        # Landing on an existing name is only safe if that file is itself
        # moving out of the way in this same batch.
        if key in existing_set and key != row["old"].lower() and key not in moving_away:
            row["status"] = "error"
            row["reason"] = "'{}' already exists in this folder".format(row["new"])

    return rows


def summarise(rows):
    return {
        "total": len(rows),
        "rename": sum(1 for r in rows if r["status"] == "rename"),
        "unchanged": sum(1 for r in rows if r["status"] == "unchanged"),
        "error": sum(1 for r in rows if r["status"] == "error"),
    }
