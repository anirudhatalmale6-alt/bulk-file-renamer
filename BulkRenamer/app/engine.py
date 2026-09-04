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
    "official", "video", "lyrics", "lyric", "hq", "hd", "4k",
    "full", "album", "remastered", "remaster",
    "xxx", "kitsune", "fum", "lol", "eztv", "ettv", "ddp2", "ddp",
    "h", "264", "265", "mp4", "mkv", "avi", "m4v", "wmv", "mpg",
]

# Phrases removed as a unit, before single tokens. "with Audio Description"
# must go whole: stripping the token "audio" on its own leaves "with Description"
# stranded in the middle of the title.
JUNK_PHRASES = [
    "with audio description", "audio description",
    "with commentary", "directors cut", "director's cut",
    "extended edition", "special edition",
]

# Audio and codec notation whose internal dot has already become a space:
# "DDP5.1" -> "DDP5 1", "H.264" -> "H 264". Removing the tokens one at a time
# leaves a stray "0" or "1" sitting in the middle of the title.
_CODEC_NOTATION = re.compile(
    r"(?<![a-z0-9])(?:ddp?|dd|aac|ac3|eac3|dts|truehd|h|x|mpeg)\s*\d{1,4}(?:[ .]\d{1,2})?(?![a-z0-9])",
    re.I,
)

# A release group left clinging to the end: "... 1080p MP4-KTR" becomes
# " -KTR" once the tags around it go. Requires NO space after the dash, which
# is what separates a group tag from a real "Artist - Song" title.
_TRAILING_GROUP = re.compile(r"\s*-([A-Za-z0-9]{2,12})\s*$")

# A date stamped into the name, as "21 06 13" or "2021 06 13" or "21.06.13".
_DATE_STAMP = re.compile(r"\b(\d{2}|\d{4})[ ._-](\d{2})[ ._-](\d{2})\b")

# A placeholder episode "title" that carries no information: "Episode 3.05",
# "Episode 12", "Ep 5".
_PLACEHOLDER_TITLE = re.compile(r"^(?:episode|ep|part|pt)\s*\.?\s*[\d.]+$", re.I)

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

# The word "The" wherever it appears. The client files "The Americans" under
# "Americans", so it goes - but only as a whole word, never inside one
# ("Theatre", "Theory" and "Them" must survive).
_THE = re.compile(r"(?<![a-z0-9])the(?![a-z0-9])", re.I)

# A leading track number: "01 ", "01. ", "01 - ", "(01)"
_LEADING_TRACK = re.compile(r"^\s*[\(\[]?\s*(\d{1,3})\s*[\)\]]?\s*[-._)\]]*\s+")

# The various dashes people and websites use between artist and title.
_DASHES = "‐‑‒–—―⁃−"

# Curly quotes, which arrive from episode databases and block plain matching.
# Single curly quotes become a plain apostrophe, which is legal in a Windows
# filename. Double ones are DROPPED, not converted: '"' is one of the characters
# Windows forbids, so converting would turn a good title into a rejected name.
_SMART_QUOTES = {"\u2018": "'", "\u2019": "'", "\u201a": "'", "\u201b": "'",
                 "\u201c": "", "\u201d": "", "\u201e": "", "\u201f": ""}


def straighten_quotes(text):
    for curly, plain in _SMART_QUOTES.items():
        text = text.replace(curly, plain)

    return text


# Only these are treated as extensions. os.path.splitext() alone is wrong for
# dot-separated names: it turns "brian.kennedy.you.raise.me.up" into a stem of
# "brian.kennedy.you.raise.me" plus an "extension" of ".up", and the last word
# of the song title is silently lost.
KNOWN_EXTENSIONS = {
    # video
    "mkv", "mp4", "avi", "m4v", "mov", "wmv", "mpg", "mpeg", "m2ts", "ts", "vob",
    "flv", "webm", "divx", "rmvb", "ogv", "3gp", "iso", "img",
    # audio
    "mp3", "flac", "wav", "m4a", "aac", "ogg", "opus", "wma", "aiff", "ape",
    "alac", "mid", "midi", "dsf", "mka",
    # subtitles and metadata
    "srt", "sub", "idx", "ssa", "ass", "vtt", "nfo", "sfv", "cue", "m3u", "m3u8",
    # images
    "jpg", "jpeg", "png", "gif", "bmp", "webp", "tif", "tiff", "heic", "svg",
    # documents and archives
    "pdf", "epub", "mobi", "azw3", "cbr", "cbz", "txt", "doc", "docx", "rtf",
    "odt", "xls", "xlsx", "csv", "ppt", "pptx", "zip", "rar", "7z", "tar", "gz",
    "bz2", "xz", "exe", "msi", "torrent",
}


def split_name(filename):
    """Split into (stem, ext). ext keeps its dot, or is '' when there is none.

    A trailing piece is only an extension if it actually looks like one.
    Anything else stays part of the name, so nothing is ever silently dropped.
    """
    stem, ext = os.path.splitext(filename)

    # A leading dot is part of the name, not an extension: ".gitignore"
    if not stem and ext:
        return ext, ""

    if ext and ext[1:].lower() in KNOWN_EXTENSIONS:
        return stem, ext

    return filename, ""


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


def drop_the(text):
    """Remove the word "The" anywhere in a name."""
    return _collapse(_THE.sub(" ", text))


def strip_junk(text, drop_year=False, drop_brackets=True, drop_dates=True, protect=None):
    """Remove release tags, bracketed noise, date stamps and separator clutter.

    protect: words that must survive whatever the junk list says. A performer,
    band or show name can collide with a tag - somebody called "Cam" or "Eve"
    would otherwise be deleted from their own filename.
    """
    protect = {w.strip().lower() for w in (protect or []) if w.strip()}
    text = _normalise_separators(text)

    if drop_brackets:
        # Bracketed groups in download names are nearly always tags, not title.
        text = re.sub(r"[\(\[\{][^\)\]\}]*[\)\]\}]", " ", text)

    # Phrases first: removing the token "audio" on its own would leave
    # "with Description" stranded in the middle of a title.
    for phrase in JUNK_PHRASES:
        text = re.sub(r"(?<![a-z0-9])" + re.escape(phrase).replace(r"\ ", r"\s+") + r"(?![a-z0-9])",
                      " ", text, flags=re.I)

    if drop_dates:
        text = _DATE_STAMP.sub(" ", text)

    text = _CODEC_NOTATION.sub(" ", text)

    for token in JUNK_TOKENS:
        if token in protect:
            continue

        text = re.sub(r"(?<![a-z0-9])" + re.escape(token) + r"(?![a-z0-9])", " ", text, flags=re.I)

    # Do this after the tags have gone, so the group tag is what is left at the
    # end. Never touch a protected word.
    trailing = _TRAILING_GROUP.search(text)

    if trailing and trailing.group(1).lower() not in protect:
        text = text[: trailing.start()]

    if drop_year:
        text = _YEAR.sub(" ", text)

    return _collapse(text)


# ---------------------------------------------------------------------------
# Capitalisation
# ---------------------------------------------------------------------------

# Words that keep a fixed spelling whatever the rule says. Seeded with the
# obvious brand shapes; the user can add their own in the app.
DEFAULT_WORD_FIXES = {
    "ftvgirls": "FTVgirls",
    "bbc": "BBC",
    "cnn": "CNN",
    "hbo": "HBO",
    "us": "US",
    "uk": "UK",
    "tv": "TV",
    "ii": "II",
    "iii": "III",
    "iv": "IV",
}


def _fix_word(word, fixes):
    bare = re.sub(r"[^A-Za-z0-9']", "", word).lower()

    if bare and bare in fixes:
        return word.replace(re.sub(r"[^A-Za-z0-9']", "", word), fixes[bare])

    return None


# Codes that already have a correct shape and must not be re-cased:
# S01E05, 3x07, 05of10.
_CODE_SHAPE = re.compile(r"^(?:s\d{1,2}e\d{1,3}|\d{1,2}x\d{1,3}|\d{1,3}of\d{1,3})$", re.I)


def title_words(text, fixes=None):
    """Capitalise every word, which is the rule the client actually asked for.

    Deliberately not smart_title(): that lowercases small words, so "Band of
    Brothers" would never become "Band Of Brothers".
    """
    fixes = dict(DEFAULT_WORD_FIXES, **(fixes or {}))
    text = straighten_quotes(text)
    out = []

    for word in text.split(" "):
        if not word:
            continue

        fixed = _fix_word(word, fixes)

        if fixed is not None:
            out.append(fixed)
            continue

        if _CODE_SHAPE.match(word):
            out.append(word)
            continue

        # Capitalise after an opening bracket or dash too: "(live)" -> "(Live)"
        # Capitalise the first letter, looking past an opening quote or
        # bracket, and after a dash - but NOT after an apostrophe, or "you're"
        # would become "You'Re".
        out.append(re.sub(r"(^[\"']?|[\(\[\{-])([a-zA-Z])",
                          lambda m: m.group(1) + m.group(2).upper(),
                          word.lower()))

    return " ".join(out)


def find_episode_span(text):
    """Return (before, season, episode, after) or None.

    The text after the code matters: that is where an episode title lives when
    the filename carries one, as in "... S01E05 Crossroads 1080p ...".
    """
    for pattern in _EPISODE_PATTERNS:
        match = pattern.search(text)

        if match:
            return (
                text[: match.start()],
                int(match.group("season")),
                int(match.group("episode")),
                text[match.end():],
            )

    cleaned = strip_junk(text)

    for match in _BARE_EPISODE.finditer(cleaned):
        if _YEAR.match(match.group(0)):
            continue

        return (
            cleaned[: match.start()],
            int(match.group("season")),
            int(match.group("episode")),
            cleaned[match.end():],
        )

    return None


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


def artist_from_folder(folder, fixes=None):
    """Work the artist out of an album folder name.

    "Creedence Clearwater Revival - Chronicle The 20 Greatest Hits (Remastered)
    (2023) Mp3 320kbps [PMEDIA]" -> "Creedence Clearwater Revival".

    A track file rarely names its own artist; the folder above it nearly always
    does, and that is the only place the information exists.
    """
    if not folder:
        return ""

    cleaned = strip_junk(folder, drop_year=True)

    # Everything before the first " - " is the artist; album details follow it.
    parts = re.split(r"\s-\s", cleaned, maxsplit=1)
    artist = parts[0].strip() if parts else cleaned

    # Strip anything that is not a letter, digit, space or the usual name
    # punctuation - stray stars and symbols from scene folders.
    artist = re.sub(r"[^\w\s'&.,!-]", " ", artist, flags=re.UNICODE)

    return title_words(_collapse(artist), fixes)


# ---------------------------------------------------------------------------
# Presets
# ---------------------------------------------------------------------------


def lookup_show(text, shows):
    """Find the entry in the shows table whose 'match' appears in text.

    Longest match wins, so "Le Bureau Des Legendes" beats a stray "Bureau".
    """
    if not shows:
        return None

    haystack = _normalise_separators(text).lower()
    best = None

    for entry in shows:
        needle = (entry.get("match") or "").strip().lower()

        if not needle:
            continue

        needle = _normalise_separators(needle).lower()

        if needle and needle in haystack:
            if best is None or len(needle) > len(best[0]):
                best = (needle, entry)

    return best[1] if best else None


def episode_code(season, episode, total=None, seasons=None):
    """The client's two formats.

    "Fewer than 20 episodes -> XofYY; more than 20, or divided into seasons,
    -> SXEYY." Both halves need the episode count and the season count, which a
    filename never carries - they come from the shows table or a lookup.

    Season is NOT zero-padded (S3E05), episode is padded to two.
    """
    single_season = (seasons is None and (season is None or season <= 1)) or seasons == 1

    if total and total < 20 and single_season:
        # Pad the episode to the width of the total, which is what the client's
        # own examples do: 6 episodes gives "3of6", 10 gives "03of10".
        width = len(str(total))

        return "{}of{}".format(str(episode).zfill(width), total)

    return "S{}E{:02d}".format(season if season else 1, episode)


def preset_tv(stem, shows=None, fixes=None, protect=None, add_titles=False):
    """Showname + episode code + episode title, to the client's spec."""
    found = find_episode_span(stem)

    if not found:
        return title_words(strip_junk(stem, drop_year=True, protect=protect), fixes)

    before, season, episode, after = found

    entry = lookup_show(stem, shows)

    total = None
    seasons = None
    known_title = ""

    if entry and entry.get("name"):
        show = entry["name"]
        total = entry.get("episodes")
        seasons = entry.get("seasons")

        # A titles map, if one was supplied or looked up. Only consulted when
        # the user asks for episode names to be added: their "Secret Invasion
        # 3of6" example deliberately carries no title, while an earlier
        # "... S2E01 Comrades" example wanted one. It is a preference, not a
        # fact about the file, so it is a switch.
        if add_titles:
            titles = entry.get("titles") or {}
            known_title = titles.get("{}x{}".format(season, episode), "") or ""
    else:
        show = title_words(strip_junk(before, drop_year=True, protect=protect), fixes)

    # An episode title, when the filename actually carries one.
    title = strip_junk(after, drop_year=True, protect=protect)
    title = re.sub(r"^[\s\-_.]+", "", title)

    if _PLACEHOLDER_TITLE.match(title.strip()):
        title = ""

    # A looked-up title wins: the filename's version is often truncated or
    # missing entirely, and this is the whole reason for having the table.
    #
    # But a looked-up title can be a placeholder too - plenty of series, French
    # ones especially, genuinely name every episode "Episode 5". Adding that
    # back after stripping it from the filename would be absurd, so it goes
    # through the same filter.
    if known_title and not _PLACEHOLDER_TITLE.match(known_title.strip()):
        title = known_title

    code = episode_code(season, episode, total, seasons)

    parts = [show, code]

    if title.strip():
        parts.append(title_words(title, fixes))

    return _collapse(" ".join(p for p in parts if p))


def preset_artist_song(stem, artists=None, fixes=None, protect=None):
    """Best effort at 'Artist - Song'.

    A dot-separated name like "brian.kennedy.you.raise.me.up" carries no clue
    about where the artist ends and the song begins - two words or three is
    unknowable from the text. So an optional artists table supplies the split
    point, the same way the shows table supplies a canonical show name.
    """
    text = strip_junk(stem, protect=protect)
    text = _LEADING_TRACK.sub("", text)

    if artists:
        lowered = text.lower()

        best = None

        for entry in artists:
            needle = (entry.get("match") or "").strip().lower()

            if needle and lowered.startswith(needle):
                if best is None or len(needle) > len(best[0]):
                    best = (needle, entry)

        if best:
            needle, entry = best
            song = text[len(needle):].strip(" -_.")
            name = entry.get("name") or title_words(needle, fixes)

            if song:
                return "{} - {}".format(name, title_words(song, fixes))

            return name

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


def apply_rules(stem, ext, rules, index=0, context=None):
    """Run every rule in order. Returns (stem, ext).

    context carries facts about where the file lives - currently just the
    containing folder name, which is where an album's artist is written.
    """
    context = context or {}
    for rule in rules or []:
        kind = rule.get("type")

        if kind == "preset_tv":
            stem = preset_tv(stem, rule.get("shows"), rule.get("fixes"), rule.get("protect"),
                             bool(rule.get("add_titles")))
        elif kind == "preset_artist_song":
            stem = preset_artist_song(stem, rule.get("artists"), rule.get("fixes"), rule.get("protect"))
        elif kind == "preset_show_episode":
            stem = preset_show_episode(stem, rule.get("style", "S{season:02d}E{episode:02d}"))
        elif kind == "strip_junk":
            stem = strip_junk(stem, drop_year=bool(rule.get("drop_year")),
                              drop_brackets=rule.get("drop_brackets", True),
                              protect=rule.get("protect"))
        elif kind == "find_replace":
            stem = _rule_find_replace(stem, rule)
        elif kind == "case":
            stem = _rule_case(stem, rule)
        elif kind == "case_every_word":
            stem = title_words(stem, rule.get("fixes"))
        elif kind == "insert":
            stem = _rule_insert(stem, rule)
        elif kind == "number":
            stem = _rule_number(stem, rule, index)
        elif kind == "trim":
            stem = _rule_trim(stem, rule)
        elif kind == "drop_the":
            stem = drop_the(stem)
        elif kind == "folder_artist":
            artist = rule.get("artist") or artist_from_folder(
                context.get("folder", ""), rule.get("fixes"))

            if artist:
                cleaned = _LEADING_TRACK.sub("", strip_junk(stem, protect=rule.get("protect")))
                cleaned = title_words(_collapse(cleaned), rule.get("fixes"))

                # Do not repeat the artist if the track already starts with it.
                if cleaned.lower().startswith(artist.lower()):
                    stem = cleaned
                else:
                    stem = "{} - {}".format(artist, cleaned) if cleaned else artist
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


def plan(filenames, rules, existing=None, context=None):
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
            new_stem, new_ext = apply_rules(stem, ext, rules, index, context)
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
