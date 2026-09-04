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


def drives():
    """Windows drive letters, or / on anything else."""
    if os.name != "nt":
        return [{"name": "/", "path": "/"}]

    out = []

    for letter in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
        root = "{}:\\".format(letter)

        if os.path.exists(root):
            out.append({"name": root, "path": root})

    return out


def plan_folder(path, rules, recursive=False, extensions=None):
    """Preview for a real folder. Groups by directory so that collision checks
    are made against the right set of neighbours when running recursively."""
    files = list_folder(path, recursive, extensions)

    by_dir = {}

    for full in files:
        by_dir.setdefault(os.path.dirname(full), []).append(os.path.basename(full))

    rows = []

    for directory, names in by_dir.items():
        try:
            existing = os.listdir(directory)
        except OSError:
            existing = names

        for row in engine.plan(names, rules, existing=existing):
            row = dict(row)
            row["dir"] = directory
            row["old_path"] = os.path.join(directory, row["old"])
            row["new_path"] = os.path.join(directory, row["new"])
            rows.append(row)

    order = {full: i for i, full in enumerate(files)}
    rows.sort(key=lambda r: order.get(r["old_path"], 0))

    return rows


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
