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

# An episode number with NO season: "-E01", "Ep 3", "Episode 12". The client's
# "Strike-The.Silkworm-E01.mp4" is this shape, and before v1.6 it matched
# nothing at all - so the episode number was treated as junk and thrown away,
# which silently gave two different episodes the same name. The season is
# reported as None rather than 1, because "the file does not say" and "the file
# says season one" lead to different numbering (see infer_episode_total).
_EPISODE_NO_SEASON = re.compile(
    r"(?:^|[\s._\-\[\(])e(?:p|pisode)?[\s._-]*(?P<episode>\d{1,3})(?=$|[\s._\-\]\)])", re.I)

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


def drop_quotes(text):
    """Take quote marks out of a finished name.

    The client: "Bodies 5of8 'We Are One Another's Ghosts'.mkv - note the '
    characters which i dont need". Those wrapping quotes come from the episode
    database, which writes every Bodies title as 'Like This'. Rather than guess
    which of the three apostrophes in that name was decoration and which was
    part of a word, all of them go - so "Another's" becomes "Anothers".
    """
    for char in "'\"" + "‘’‚‛“”„‟´ʼ":
        text = text.replace(char, "")

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

    # A dash with a space on one side only is a leftover from something removed
    # next to it: "Strike-The Silkworm" minus "The" leaves "Strike- Silkworm".
    # Give it space on both sides. A dash with no space either side is part of
    # the word ("Spider-Man", "AC-DC", "x264-GalaxyTV") and is left alone.
    text = re.sub(r"(?<=\S)-[ \t]+", " - ", text)
    text = re.sub(r"[ \t]+-(?=\S)", " - ", text)
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


def _words(text):
    return [w for w in _normalise_separators(text).split(" ") if w]


# Fewer than this many files is not evidence. With two names, any word they
# happen to share looks "common", and half of it would be a real title.
MIN_FILES_FOR_COMMON = 3


def common_tail_words(stems, minimum=MIN_FILES_FOR_COMMON):
    """Words that appear in EVERY name in the batch.

    The client's observation: "Lots of films and movies postfix a group name or
    resolution ... that is always the same across the episodes". A release group
    like "hpcmpc" or "SuSpEcT" is not on any junk list and never will be - there
    are thousands of them. But it is identifiable without a list: it is the text
    that does not change from episode to episode, while an episode title does.

    Only the words are returned; where they may be removed from is decided by
    drop_tail_words(), which never reaches past the first word that differs.
    """
    stems = [s for s in stems if s and s.strip()]

    if len(stems) < minimum:
        return set()

    sets = [{w.lower() for w in _words(stem)} for stem in stems]

    return set.intersection(*sets) if sets else set()


def drop_tail_words(text, words, protect=None, keep_letters=True):
    """Remove common words from the END of a name, stopping at the first that
    is not common.

    Working backwards is what keeps an episode title safe. In
    "S01E06 Family Limitation SuSpEcT 720p hpcmpc" the last three words are in
    every file, so they go; "Limitation" is not, so the walk stops there and
    "Family Limitation" survives untouched.

    keep_letters: refuse the whole removal if it would leave nothing but
    numbers. Three holiday photos called "1 Holiday", "2 Holiday", "3 Holiday"
    share the word "Holiday" at the end, and dropping it would rename them to
    "1", "2" and "3" - technically what the rule says, and obviously not what
    anybody wants. Switched off when trimming an episode title, where reducing
    the fragment to nothing is the correct answer.
    """
    if not words:
        return text

    protect = {w.strip().lower() for w in (protect or []) if w.strip()}
    parts = _words(text)
    kept = list(parts)

    while kept and kept[-1].lower() in words and kept[-1].lower() not in protect:
        kept.pop()

    if keep_letters and not any(re.search(r"[a-z]", w, re.I) for w in kept):
        return _collapse(" ".join(parts))

    return _collapse(" ".join(kept))


def tail_run_words(stems, words):
    """Of the words common to every file, the ones actually at the end of a name.

    The common set legitimately includes the show's own name - every episode of
    Boardwalk Empire contains "Boardwalk". Those are never removed, because the
    walk stops at the episode number, so listing them in the app would say the
    tool is deleting something it is not. This narrows the list to what really
    goes.
    """
    if not words:
        return set()

    out = set()

    for stem in stems:
        parts = _words(stem)

        while parts and parts[-1].lower() in words:
            out.add(parts.pop().lower())

    return out


def safe_filename_text(text):
    """Make text from a tag usable as a filename.

    Tags are written by people, so they contain the characters Windows forbids -
    "AC/DC" being the obvious one. Refusing the file would be pedantic when the
    fix is unambiguous.
    """
    text = straighten_quotes(str(text or ""))

    for char in "/\\":
        text = text.replace(char, "-")

    text = text.replace(":", " -")

    for char in ILLEGAL_CHARS:
        text = text.replace(char, "")

    text = "".join(c for c in text if ord(c) >= 32)

    return _collapse(text)


def strip_junk(text, drop_year=False, drop_brackets=True, drop_dates=True, protect=None,
               common=None):
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

    # Last, because a tag removed above may have been sitting between two
    # words that are common to the whole batch.
    if common:
        text = drop_tail_words(text, common, protect)

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


def title_words(text, fixes=None, trust_case=False):
    """Capitalise every word, which is the rule the client actually asked for.

    Deliberately not smart_title(): that lowercases small words, so "Band of
    Brothers" would never become "Band Of Brothers".

    trust_case: leave any word that already contains a capital exactly as it is.
    Used for text taken from a music tag, where somebody has already typed the
    name properly and flattening it does real damage - "AC/DC" would come back
    as "Ac-Dc". A tag written entirely in lower case is still capitalised, which
    is the case that needed fixing in the first place.
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

        if trust_case and re.search(r"[A-Z]", word):
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

    season is None when the filename gives an episode number but no season.

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

    match = _EPISODE_NO_SEASON.search(text)

    if match:
        # match.start() sits on the separator in front of the "E", which belongs
        # to neither side: "Strike-The.Silkworm-E01" gives a show of
        # "Strike-The.Silkworm" and an empty title.
        return (text[: match.start()], None, int(match.group("episode")),
                text[match.end():])

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

    match = _EPISODE_NO_SEASON.search(text)

    if match:
        # This preset always prints a season, so a file that names none is
        # season one - which is what "E01" means everywhere it is used.
        return (text[: match.start()], 1, int(match.group("episode")))

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


def infer_episode_total(stems):
    """How many episodes a batch holds, when the batch itself is the evidence.

    The client's case: two files called "...-E01" and "...-E02" should come out
    "1of2" and "2of2". Nothing in either filename says how many episodes there
    are - but the folder does, if it holds a complete run.

    Deliberately strict, because guessing this wrong renumbers a whole series:

      * every file that carries an episode number must carry NO season. A file
        that says S01 is stating its own structure, and that is trusted over a
        count of files in a folder - a folder holding season 1 of a long series
        must stay S1E01, not become "01of10".
      * the numbers must be a complete run from 1 with no gaps or repeats.
        Episodes 3 to 6 are part of something bigger, so "3of4" would be a lie.
      * they must all belong to the same show.
      * fewer than 20, which is the client's own threshold for this format.

    Returns the count, or None when any of that fails.
    """
    numbers = []
    shows = set()

    for stem in stems:
        found = find_episode_span(stem)

        if not found:
            continue        # artwork, subtitles, a stray nfo - not evidence

        before, season, episode, _after = found

        if season is not None:
            return None

        numbers.append(episode)
        shows.add(_normalise_separators(strip_junk(before, drop_year=True)).strip().lower())

    if len(shows) != 1 or not 2 <= len(numbers) < 20:
        return None

    if sorted(numbers) != list(range(1, len(numbers) + 1)):
        return None

    return len(numbers)


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


def preset_tv(stem, shows=None, fixes=None, protect=None, add_titles=False, common=None,
              batch_total=None):
    """Showname + episode code + episode title, to the client's spec.

    batch_total: an episode count worked out from the folder itself, used only
    when the filenames carry no season (see infer_episode_total).
    """
    found = find_episode_span(stem)

    if not found:
        return title_words(strip_junk(stem, drop_year=True, protect=protect, common=common), fixes)

    before, season, episode, after = found

    entry = lookup_show(stem, shows)

    total = None
    seasons = None
    known_title = ""

    if entry and entry.get("name"):
        show = entry["name"]
        total = entry.get("episodes")
        seasons = entry.get("seasons")

        # A count the user typed themselves always wins. A looked-up one does
        # not, when the filenames name no season and the folder holds a complete
        # run: the lookup would report every episode the series ever had, which
        # turns a two-part story into S1E01 instead of the 1of2 they asked for.
        if batch_total and not entry.get("user_count"):
            total, seasons = batch_total, 1

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
        total = batch_total
        seasons = 1 if batch_total else None

    # An episode title, when the filename actually carries one. Anything the
    # whole batch shares at the end of the name is release clutter, not a title -
    # and here it is right for that to consume the fragment entirely, since an
    # episode with no title should end at the episode number.
    title = strip_junk(after, drop_year=True, protect=protect)
    title = drop_tail_words(title, common, protect, keep_letters=False)
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


def preset_tag_music(stem, entry=None, fixes=None, artists=None, protect=None,
                     use_track=False):
    """'Artist - Song' taken from the file's own tags.

    The client's point: an MP3 or FLAC nearly always has the artist written
    inside it, and that is a fact rather than a guess at where a name splits.
    When the tags are missing or half-filled, this falls straight back to the
    filename rules, so a folder of mixed files still comes out sensibly.
    """
    entry = entry or {}

    artist = safe_filename_text(entry.get("artist") or entry.get("albumartist") or "")
    title = safe_filename_text(entry.get("title") or "")

    if not artist or not title:
        return preset_artist_song(stem, artists, fixes, protect)

    name = "{} - {}".format(title_words(artist, fixes, trust_case=True),
                            title_words(title, fixes, trust_case=True))

    if use_track:
        # "1/12" is a legal tag value; only the part before the slash is wanted.
        track = str(entry.get("track") or "").split("/")[0].strip()

        if track.isdigit():
            name = "{:02d} - {}".format(int(track), name)

    return _collapse(name)


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


def apply_rules(stem, ext, rules, index=0, context=None, filename=None):
    """Run every rule in order. Returns (stem, ext).

    context carries facts about where the file lives: the containing folder
    name (where an album's artist is written), the words every file in the
    batch shares, and any tags read from the files themselves.
    """
    context = context or {}
    tags = (context.get("tags") or {}).get(filename or "", {})

    for rule in rules or []:
        kind = rule.get("type")

        # A rule may switch the shared-word removal off; by default it is on
        # wherever it can help.
        common = context.get("common") if rule.get("drop_common", True) else None

        if kind == "preset_tv":
            stem = preset_tv(stem, rule.get("shows"), rule.get("fixes"), rule.get("protect"),
                             bool(rule.get("add_titles")), common,
                             context.get("episode_total"))
        elif kind == "preset_artist_song":
            stem = preset_artist_song(stem, rule.get("artists"), rule.get("fixes"), rule.get("protect"))
        elif kind == "tag_music":
            stem = preset_tag_music(stem, tags, rule.get("fixes"), rule.get("artists"),
                                    rule.get("protect"), bool(rule.get("use_track")))
        elif kind == "preset_show_episode":
            stem = preset_show_episode(stem, rule.get("style", "S{season:02d}E{episode:02d}"))
        elif kind == "strip_junk":
            stem = strip_junk(stem, drop_year=bool(rule.get("drop_year")),
                              drop_brackets=rule.get("drop_brackets", True),
                              protect=rule.get("protect"), common=common)
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

    # Last of all, and once only: quote marks are not wanted in a filename.
    # Only when something actually ran - with no rules at all the tool must
    # leave every name exactly as it found it.
    if rules:
        stem = _collapse(drop_quotes(stem))

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

    # Work out what the whole batch has in common before touching any of it -
    # that is the only place a release group like "hpcmpc" can be identified,
    # and it has to be the same set for every file or the results would differ
    # depending on where in the list a file sits.
    context = dict(context or {})

    # The app passes this in so it can also show the user what it found; when
    # the engine is used directly it works it out itself.
    stems = [split_name(f)[0] for f in filenames]

    if context.get("common") is None:
        context["common"] = common_tail_words(stems)

    if context.get("episode_total") is None:
        context["episode_total"] = infer_episode_total(stems)

    # Pass one: work out what each file wants to be called. Collisions cannot be
    # judged yet, because a name may be freed up by a file later in the list -
    # renaming 01,02,03 up to 02,03,04 is perfectly legal but every single step
    # looks like a clash if you only look backwards.
    rows = []

    for index, filename in enumerate(filenames):
        stem, ext = split_name(filename)

        try:
            new_stem, new_ext = apply_rules(stem, ext, rules, index, context, filename)
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
