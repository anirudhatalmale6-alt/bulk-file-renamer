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


LONG_PREFIX = "\\\\?\\"


def _wanted_ext(name, extensions):
    """Does this file pass the "only these types" box? Blank box = everything."""
    if not extensions:
        return True

    wanted = {e.strip().lower().lstrip(".") for e in extensions if e.strip()}

    if not wanted:
        return True

    return os.path.splitext(name)[1].lower().lstrip(".") in wanted


def _long(path):
    """A form of the path that is not subject to the 260-character limit.

    Windows refuses ordinary paths longer than MAX_PATH, and a download folder
    reaches it easily: one long release name plus a "Subs" folder inside it is
    enough. os.walk() hands that refusal to its onerror callback, which is
    None by default - so the folder and everything under it just fails to
    appear, with nothing said. The \\\\?\\ prefix bypasses the limit entirely.
    """
    if os.name != "nt":
        return path

    if path.startswith(LONG_PREFIX):
        return path

    if path.startswith("\\\\"):                     # \\server\share
        return LONG_PREFIX + "UNC\\" + path[2:]

    return LONG_PREFIX + path


def _short(path):
    """Undo _long(), so nothing with \\\\?\\ in it is ever shown to the user."""
    if path.startswith(LONG_PREFIX + "UNC\\"):
        return "\\\\" + path[len(LONG_PREFIX) + 4:]

    if path.startswith(LONG_PREFIX):
        return path[len(LONG_PREFIX):]

    return path


def list_folder(path, recursive=False, extensions=None, skipped=None):
    """Files in a folder. Directories are never returned - this renames files.

    skipped: an optional list, filled in with the folders that could not be
    read. Anything dropped has to be reported - a preview that quietly misses
    a sub-folder is worse than one that refuses.
    """
    path = os.path.abspath(path)

    if not os.path.isdir(path):
        raise ValueError("Not a folder: {}".format(path))

    wanted = None

    if extensions:
        wanted = {e.strip().lower().lstrip(".") for e in extensions if e.strip()}

    found = []

    if recursive:
        def failed(exc):
            if skipped is not None:
                skipped.append({"path": _short(getattr(exc, "filename", "") or ""),
                                "why": getattr(exc, "strerror", None) or str(exc)})

        # followlinks=True on purpose. A download folder often reaches another
        # drive through a junction, and os.walk skips those by default - the
        # sub-folder is right there in Explorer and simply never appears here.
        # The cost is that a loop of links would walk for ever, so real paths
        # are remembered and a second visit ends that branch.
        seen = set()

        for root, dirs, files in os.walk(_long(path), onerror=failed, followlinks=True):
            dirs[:] = [d for d in dirs if d != UNDO_DIR]

            fresh = []

            for name in dirs:
                try:
                    key = os.path.realpath(os.path.join(root, name)).lower()
                except OSError:
                    continue

                if key in seen:
                    continue

                seen.add(key)
                fresh.append(name)

            dirs[:] = fresh

            for name in sorted(files):
                found.append(_short(os.path.join(root, name)))
    else:
        try:
            names = sorted(os.listdir(_long(path)))
        except OSError as exc:
            raise ValueError("Cannot read {}: {}".format(path, exc))

        for name in names:
            full = os.path.join(path, name)

            if os.path.isfile(_long(full)):
                found.append(full)

    if wanted is not None:
        found = [f for f in found if os.path.splitext(f)[1].lower().lstrip(".") in wanted]

    return found


def list_dirs(path):
    """Sub-folders, for the folder picker. Unreadable ones are skipped, not fatal."""
    path = os.path.abspath(path)
    out = []

    try:
        for name in sorted(os.listdir(_long(path))):
            full = os.path.join(path, name)

            try:
                if os.path.isdir(_long(full)):
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


def _quiet_windows_disk_errors():
    """Stop Windows popping "There is no disk in the drive" at us.

    Touching an empty card-reader or DVD letter raises that modal dialog from
    inside the OS, and this app has no window to put it in front of - so it
    waits for a click that can never happen and the whole start-up stalls.
    SEM_FAILCRITICALERRORS turns the dialog into an ordinary error return.
    """
    if os.name != "nt":
        return

    try:
        import ctypes

        ctypes.windll.kernel32.SetErrorMode(0x0001 | 0x0002 | 0x8000)
    except Exception:
        pass


def _letters_from_bitmask():
    """Drive letters straight out of the OS, without touching the drives.

    os.path.exists("A:\\") asks the drive itself, which for empty removable
    letters means spinning it up and waiting - seconds each, and the app looks
    dead while it happens. GetLogicalDrives is a bitmask held in memory: it
    answers instantly and never reaches a disk.
    """
    if os.name != "nt":
        return None

    try:
        import ctypes

        mask = ctypes.windll.kernel32.GetLogicalDrives()
    except Exception:
        return None

    if not mask:
        return None

    return [letter for index, letter in enumerate("ABCDEFGHIJKLMNOPQRSTUVWXYZ")
            if mask & (1 << index)]


def drives():
    """Windows drive letters, or / on anything else. System drive flagged."""
    if os.name != "nt":
        return [{"name": "/", "path": "/", "system": False}]

    _quiet_windows_disk_errors()

    letters = _letters_from_bitmask()
    probe = letters is None          # only fall back to asking the disks
    out = []

    for letter in letters if letters is not None else "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
        root = "{}:\\".format(letter)

        if probe and not os.path.exists(root):
            continue

        out.append({
            "name": root,
            "path": root,
            "system": "{}:".format(letter).upper() == SYSTEM_DRIVE,
        })

    # Data drives first; the system drive last and clearly marked.
    return sorted(out, key=lambda d: (d["system"], d["name"]))


DOWNLOAD_NAMES = ("download", "downloads", "downloaded", "dl")


def preferred_start():
    """Where to open the folder box, avoiding the system drive.

    Their words: "im mostly working in E:/download or the other ones.
    (D upwards)" - so E: is tried first, then every other data drive, and a
    download folder always beats a bare drive root. Windows compares names
    case-insensitively, so "Download" and "DOWNLOAD" are found too, but the
    listing is read rather than guessed: a folder called "Downloads new" would
    be missed by guessing and is found here.
    """
    if os.name != "nt":
        return os.path.expanduser("~")

    data = [d["path"] for d in drives() if not d["system"]]
    roots = ["E:\\"] + [d for d in data if d.upper() != "E:\\"]

    for root in roots:
        try:
            names = os.listdir(root)
        except OSError:
            continue                 # empty card reader, unformatted, no rights

        for name in sorted(names):
            if name.lower() in DOWNLOAD_NAMES:
                full = os.path.join(root, name)

                try:
                    if os.path.isdir(full):
                        return full
                except OSError:
                    continue

    for root in roots:
        try:
            if os.path.isdir(root):
                return root
        except OSError:
            continue

    return os.path.expanduser("~")


def plan_folder(path, rules, recursive=False, extensions=None, report=None):
    """Preview for a real folder. Groups by directory so that collision checks
    are made against the right set of neighbours when running recursively.

    report: an optional dict that is filled in with what the run noticed -
    currently the words every file in a folder shares, so the app can say what
    it removed instead of deleting them silently.
    """
    skipped = []

    # Read without the type filter first, so the count of files hidden by it is
    # known. "Include sub-folders does not show all my files" is indisguishable
    # from a forgotten filter unless the app says which it is.
    everything = list_folder(path, recursive, None, skipped=skipped)
    files = [f for f in everything if _wanted_ext(f, extensions)]
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

        # What was read, so "it is missing files" can be checked rather than
        # argued about: how many files, spread over how many folders, how many
        # the type filter hid, and every folder that could not be opened.
        report["found"] = {
            "files": len(files),
            "dirs": len({os.path.dirname(f) for f in files}),
            "hidden": len(everything) - len(files),
            "recursive": bool(recursive),
            "skipped": skipped[:20],
            "skipped_total": len(skipped),
        }

        if not rows and not recursive:
            # A download folder usually keeps each release in its own folder,
            # so the top level holds nothing but folders and the table comes
            # back empty. Saying "no files" there is true and useless - count
            # what is one level down so the app can point at the sub-folder
            # switch instead.
            report["nested"] = nested_count(path, extensions)

    return rows


def nested_count(path, extensions=None):
    """How many sub-folders there are, and how many files they hold between
    them. Only used to explain an empty table, so it stops early rather than
    walking a whole drive."""
    try:
        dirs = list_dirs(path)
    except ValueError:
        return {"dirs": 0, "files": 0}

    total = 0

    for entry in dirs:
        try:
            total += len(list_folder(entry["path"], True, extensions))
        except ValueError:
            continue

        if total >= 500:             # enough to make the point
            break

    return {"dirs": len(dirs), "files": total}


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
            os.rename(_long(old_path), _long(temp_path))
            staged.append((temp_path, row))
        except OSError as exc:
            errors.append({"file": row["old"], "error": str(exc)})

    # Phase two: from the temp name to the real one.
    done = []

    for temp_path, row in staged:
        new_path = row["new_path"]

        try:
            os.rename(_long(temp_path), _long(new_path))
            done.append({"from": row["old_path"], "to": new_path})
        except OSError as exc:
            # Put it back rather than leaving a .tmp file behind.
            try:
                os.rename(_long(temp_path), _long(row["old_path"]))
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

        if not os.path.exists(_long(current)):
            errors.append({"file": os.path.basename(current),
                           "error": "no longer there - it may have been moved or renamed since"})
            continue

        try:
            os.rename(_long(current), _long(temp_path))
            staged.append((temp_path, move))
        except OSError as exc:
            errors.append({"file": os.path.basename(current), "error": str(exc)})

    restored = 0

    for temp_path, move in staged:
        try:
            os.rename(_long(temp_path), _long(move["from"]))
            restored += 1
        except OSError as exc:
            try:
                os.rename(_long(temp_path), _long(move["to"]))
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
