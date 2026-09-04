"""Everything that actually touches the disk.

Renames go through a temporary name first. That is not paranoia: on Windows the
filesystem is case-insensitive, so renaming "film.MKV" to "film.mkv" is a no-op
or an error depending on the API, and any batch that shuffles names among itself
(01,02,03 becoming 02,03,04) will collide halfway through if done naively. Two
phases makes both cases work, and makes a half-finished batch recoverable.
"""

import json
import os
import time
import uuid

import engine
import tags

UNDO_DIR = "undo"
UNDO_FILE = "last-batch.json"


def _app_root():
    """The BulkRenamer folder, found relative to this file - never an absolute
    path baked in at build time, which would fail on the client's first run."""
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _undo_path():
    folder = os.path.join(_app_root(), UNDO_DIR)
    os.makedirs(folder, exist_ok=True)

    return os.path.join(folder, UNDO_FILE)


def list_folder(path, recursive=False, extensions=None):
    """Files in a folder. Directories are never returned - this renames files."""
    path = os.path.abspath(path)

    if not os.path.isdir(path):
        raise ValueError("Not a folder: {}".format(path))

    wanted = None

    if extensions:
        wanted = {e.strip().lower().lstrip(".") for e in extensions if e.strip()}

    found = []

    if recursive:
        for root, dirs, files in os.walk(path):
            dirs[:] = [d for d in dirs if d != UNDO_DIR]

            for name in sorted(files):
                found.append(os.path.join(root, name))
    else:
        for name in sorted(os.listdir(path)):
            full = os.path.join(path, name)

            if os.path.isfile(full):
                found.append(full)

    if wanted is not None:
        found = [f for f in found if os.path.splitext(f)[1].lower().lstrip(".") in wanted]

    return found


def list_dirs(path):
    """Sub-folders, for the folder picker. Unreadable ones are skipped, not fatal."""
    path = os.path.abspath(path)
    out = []

    try:
        for name in sorted(os.listdir(path)):
            full = os.path.join(path, name)

            try:
                if os.path.isdir(full):
                    out.append({"name": name, "path": full})
            except OSError:
                continue
    except OSError as exc:
        raise ValueError("Cannot read {}: {}".format(path, exc))

    return out


SYSTEM_DRIVE = (os.environ.get("SystemDrive") or "C:").rstrip("\\").upper()


def is_system_path(path):
    """Is this path on the Windows system drive?

    The client works on D: upwards and asked for C: to be left alone unless
    they pick it deliberately - renaming inside Windows or Program Files is
    exactly the accident worth making hard.
    """
    if os.name != "nt":
        return False

    try:
        drive = os.path.splitdrive(os.path.abspath(path))[0].rstrip("\\").upper()
    except (ValueError, TypeError):
        return False

    return bool(drive) and drive == SYSTEM_DRIVE


def drives():
    """Windows drive letters, or / on anything else. System drive flagged."""
    if os.name != "nt":
        return [{"name": "/", "path": "/", "system": False}]

    out = []

    for letter in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
        root = "{}:\\".format(letter)

        if os.path.exists(root):
            out.append({
                "name": root,
                "path": root,
                "system": "{}:".format(letter).upper() == SYSTEM_DRIVE,
            })

    # Data drives first; the system drive last and clearly marked.
    return sorted(out, key=lambda d: (d["system"], d["name"]))


def preferred_start():
    """Where to open the folder box, avoiding the system drive.

    Their words: "im mostly working in E:/download or the other ones."
    """
    if os.name == "nt":
        for candidate in ("E:\\download", "E:\\Downloads", "E:\\"):
            if os.path.isdir(candidate):
                return candidate

        for entry in drives():
            if not entry["system"] and os.path.isdir(entry["path"]):
                return entry["path"]

    return os.path.expanduser("~")


def plan_folder(path, rules, recursive=False, extensions=None, report=None):
    """Preview for a real folder. Groups by directory so that collision checks
    are made against the right set of neighbours when running recursively.

    report: an optional dict that is filled in with what the run noticed -
    currently the words every file in a folder shares, so the app can say what
    it removed instead of deleting them silently.
    """
    files = list_folder(path, recursive, extensions)
    root = os.path.abspath(path)

    by_dir = {}

    for full in files:
        by_dir.setdefault(os.path.dirname(full), []).append(os.path.basename(full))

    wants_auto = any(r.get("type") == "auto_file" for r in rules or [])
    wants_tags = wants_auto or any(r.get("type") == "tag_music" for r in rules or [])
    rows = []
    common_report = {}
    style_counts = {}

    for directory, names in by_dir.items():
        try:
            existing = os.listdir(directory)
        except OSError:
            existing = names

        stems = [engine.split_name(n)[0] for n in names]
        common = engine.common_tail_words(stems)
        common_report[directory] = sorted(engine.tail_run_words(stems, common))

        context = {
            "folder": os.path.basename(directory.rstrip(os.sep)) or directory,
            "common": common,
            "tags": tags.read_folder([os.path.join(directory, n) for n in names])
                    if wants_tags else {},
        }

        for row in engine.plan(names, rules, existing=existing, context=context):
            row = dict(row)
            row["dir"] = directory
            row["folder"] = os.path.relpath(directory, root) if directory != root else ""
            row["old_path"] = os.path.join(directory, row["old"])
            row["new_path"] = os.path.join(directory, row["new"])

            if wants_auto:
                # Say which style each file got. In a download folder holding
                # films and episodes together they are no longer all the same,
                # so the user has to be able to see it.
                stem, ext = engine.split_name(row["old"])
                style = engine.auto_style(stem, ext, context["tags"].get(row["old"]))
                row["style"] = engine.AUTO_STYLES[style][0]
                style_counts[row["style"]] = style_counts.get(row["style"], 0) + 1

            rows.append(row)

    order = {full: i for i, full in enumerate(files)}
    rows.sort(key=lambda r: order.get(r["old_path"], 0))

    if report is not None:
        report["common"] = common_report
        report["styles"] = style_counts

    return rows


# ---------------------------------------------------------------------------
# Working out what kind of folder this is
# ---------------------------------------------------------------------------

VIDEO_EXTENSIONS = {"mkv", "mp4", "avi", "m4v", "mov", "wmv", "mpg", "mpeg", "ts",
                    "webm", "divx", "flv", "m2ts", "rmvb"}
MUSIC_EXTENSIONS = {"mp3", "flac", "m4a", "wav", "ogg", "opus", "wma", "aiff", "ape",
                    "alac"}


def detect_preset(path, recursive=False, extensions=None):
    """Guess which naming style this folder wants.

    The client asked for it, and two of their bug reports turned out to be the
    wrong style left selected from a previous folder - the tool did as it was
    told and produced a name they never wanted. Guessing from the contents is
    the fix: a folder of episodes is obvious from the filenames, and a folder of
    MP3s is obvious from the extensions.

    Returns a (preset_id, reason) pair. The reason is shown in the app, because
    a guess the user cannot see is a guess they cannot correct.
    """
    try:
        files = list_folder(path, recursive, extensions)
    except ValueError:
        return None, ""

    names = [os.path.basename(f) for f in files]

    if not names:
        return None, ""

    def ext_of(name):
        return os.path.splitext(name)[1].lower().lstrip(".")

    video = [n for n in names if ext_of(n) in VIDEO_EXTENSIONS]
    music = [n for n in names if ext_of(n) in MUSIC_EXTENSIONS]

    if music and len(music) >= len(video):
        tagged = 0

        for full in files[:12]:
            found = tags.read(full)

            if found.get("artist") and found.get("title"):
                tagged += 1

        if tagged >= 2:
            return "tag_music", "{} music files, and the artist is written in the tags".format(len(music))

        folder = os.path.basename(os.path.abspath(path).rstrip(os.sep))

        if " - " in folder:
            return "album", "music files in a folder named like \"Artist - Album\""

        return "artist_song", "{} music files".format(len(music))

    if video:
        episodes = sum(1 for n in video if engine.find_episode(engine.split_name(n)[0]))

        if episodes >= max(1, len(video) // 2):
            return "tv_client", "{} of {} video files carry an episode number".format(
                episodes, len(video))

        return "movie", "{} video files, none of them numbered like episodes".format(len(video))

    return "clean", "no video or music files here"


def apply_plan(rows):
    """Carry out the renames in a plan. Only rows marked 'rename' are touched.

    Returns (done, errors) where done is the journal of completed renames.
    """
    todo = [r for r in rows if r.get("status") == "rename"]

    errors = []
    staged = []

    # Phase one: move every file aside to a name nothing else can want.
    for row in todo:
        old_path = row["old_path"]
        temp_path = os.path.join(row["dir"], ".bulkrenamer-{}.tmp".format(uuid.uuid4().hex[:12]))

        try:
            os.rename(old_path, temp_path)
            staged.append((temp_path, row))
        except OSError as exc:
            errors.append({"file": row["old"], "error": str(exc)})

    # Phase two: from the temp name to the real one.
    done = []

    for temp_path, row in staged:
        new_path = row["new_path"]

        try:
            os.rename(temp_path, new_path)
            done.append({"from": row["old_path"], "to": new_path})
        except OSError as exc:
            # Put it back rather than leaving a .tmp file behind.
            try:
                os.rename(temp_path, row["old_path"])
            except OSError:
                errors.append({
                    "file": row["old"],
                    "error": "{} - and it could not be put back; it is currently named {}".format(
                        exc, os.path.basename(temp_path)),
                })
                continue

            errors.append({"file": row["old"], "error": str(exc)})

    if done:
        write_undo(done)

    return done, errors


def write_undo(done):
    payload = {
        "when": time.strftime("%Y-%m-%d %H:%M:%S"),
        "count": len(done),
        "moves": done,
    }

    with open(_undo_path(), "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2)


def read_undo():
    path = _undo_path()

    if not os.path.isfile(path):
        return None

    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return None


def undo_last():
    """Reverse the last batch, again in two phases."""
    payload = read_undo()

    if not payload or not payload.get("moves"):
        return 0, [{"file": "-", "error": "There is no batch to undo."}]

    errors = []
    staged = []

    for move in payload["moves"]:
        current = move["to"]
        directory = os.path.dirname(current)
        temp_path = os.path.join(directory, ".bulkrenamer-{}.tmp".format(uuid.uuid4().hex[:12]))

        if not os.path.exists(current):
            errors.append({"file": os.path.basename(current),
                           "error": "no longer there - it may have been moved or renamed since"})
            continue

        try:
            os.rename(current, temp_path)
            staged.append((temp_path, move))
        except OSError as exc:
            errors.append({"file": os.path.basename(current), "error": str(exc)})

    restored = 0

    for temp_path, move in staged:
        try:
            os.rename(temp_path, move["from"])
            restored += 1
        except OSError as exc:
            try:
                os.rename(temp_path, move["to"])
            except OSError:
                pass

            errors.append({"file": os.path.basename(move["to"]), "error": str(exc)})

    if restored:
        try:
            os.remove(_undo_path())
        except OSError:
            pass

    return restored, errors


def load_rulesets():
    path = os.path.join(_app_root(), UNDO_DIR, "rulesets.json")

    if not os.path.isfile(path):
        return {}

    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)

        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_ruleset(name, rules):
    name = (name or "").strip()

    if not name:
        raise ValueError("Give the rule set a name.")

    data = load_rulesets()
    data[name] = rules

    folder = os.path.join(_app_root(), UNDO_DIR)
    os.makedirs(folder, exist_ok=True)

    with open(os.path.join(folder, "rulesets.json"), "w", encoding="utf-8") as handle:
        json.dump(data, handle, indent=2)

    return data


def delete_ruleset(name):
    data = load_rulesets()
    data.pop(name, None)

    with open(os.path.join(_app_root(), UNDO_DIR, "rulesets.json"), "w", encoding="utf-8") as handle:
        json.dump(data, handle, indent=2)

    return data
