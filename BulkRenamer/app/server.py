"""Local web UI for Bulk Renamer.

A browser page is used instead of a desktop window for a practical reason: the
bundled Windows runtime has no Tk, and a browser gives a far better preview
table than a Tk grid would. Nothing leaves the machine - the server binds to
127.0.0.1 only, and every request must carry a token generated at startup, so
nothing else on the PC can drive it.
"""

import json
import os
import re
import secrets
import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import engine  # noqa: E402
import fileops  # noqa: E402
import lookup  # noqa: E402

TOKEN = secrets.token_urlsafe(24)
HERE = os.path.dirname(os.path.abspath(__file__))

def parse_table(text):
    """Parse the alias box.

    One per line:   Le Bureau Des Legendes = Bureau
    With a count:   Band of Brothers = Band Of Brothers | 10
    """
    out = []

    for line in (text or "").splitlines():
        line = line.strip()

        if not line or line.startswith("#"):
            continue

        if "=" in line:
            left, right = line.split("=", 1)
        else:
            left, right = line, line

        entry = {"match": left.strip(), "name": right.strip()}

        if "|" in entry["name"]:
            name, count = entry["name"].rsplit("|", 1)
            entry["name"] = name.strip()

            try:
                entry["episodes"] = int(count.strip())
                entry["seasons"] = 1
            except ValueError:
                pass

        if entry["match"]:
            out.append(entry)

    return out


def inject_tables(rules, shows, artists, protect=None, add_titles=False):
    """Give the table-driven rules their data. The engine stays offline."""
    out = []

    for rule in rules or []:
        rule = dict(rule)

        if rule.get("type") == "preset_tv":
            rule["shows"] = shows
            rule["add_titles"] = add_titles
        elif rule.get("type") in ("preset_artist_song", "tag_music"):
            rule["artists"] = artists

        if rule.get("type") in ("preset_tv", "preset_artist_song", "tag_music",
                                "strip_junk", "folder_artist"):
            rule["protect"] = protect

        out.append(rule)

    return out


PRESETS = [
    {
        "id": "tv_client",
        "label": "TV:  Showname S3E05 Title (your format)",
        "rules": [{"type": "preset_tv"}, {"type": "drop_the"}],
    },
    {
        "id": "tag_music",
        "label": "Music:  Artist - Song, read from the tags",
        "rules": [{"type": "tag_music"}],
    },
    {
        "id": "album",
        "label": "Music album:  Artist (from folder) - Song",
        "rules": [{"type": "folder_artist"}],
    },
    {
        "id": "artist_song",
        "label": "Music:  Artist - Song, from the filename",
        "rules": [{"type": "preset_artist_song"}],
    },
    {
        "id": "show_episode",
        "label": "TV:  Showname S01E01 (plain)",
        "rules": [{"type": "preset_show_episode"}],
    },
    {
        "id": "clean",
        "label": "Just clean up the junk",
        "rules": [{"type": "strip_junk"}, {"type": "case", "mode": "title"}],
    },
    {
        "id": "movie",
        "label": "Film:  Title (Year)",
        "rules": [
            {"type": "strip_junk"},
            {"type": "find_replace", "find": r"\s*((19|20)\d{2})\s*$", "replace": r" (\1)", "regex": True},
            {"type": "case", "mode": "title"},
        ],
    },
]


class Handler(BaseHTTPRequestHandler):
    server_version = "BulkRenamer"

    def log_message(self, *args):
        pass  # a console full of request lines helps nobody

    # -- helpers ------------------------------------------------------------

    def _send(self, code, body, content_type="application/json; charset=utf-8"):
        if isinstance(body, (dict, list)):
            body = json.dumps(body)

        if isinstance(body, str):
            body = body.encode("utf-8")

        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _authorised(self, query):
        return query.get("token", [""])[0] == TOKEN

    def _body(self):
        length = int(self.headers.get("Content-Length") or 0)

        if not length:
            return {}

        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except ValueError:
            return {}

    def _choose_rules(self, data):
        """The rules to run: the user's own, or the ones this folder asks for.

        Auto mode exists because the client twice reported a naming bug that was
        really a preset left over from the previous folder. The app now looks at
        what is actually in the folder and says which style it picked and why,
        so a wrong guess is visible instead of surprising.
        """
        rules = data.get("rules") or []

        if not data.get("auto"):
            return rules, None, ""

        preset_id, reason = fileops.detect_preset(
            data.get("path", ""), bool(data.get("recursive")),
            data.get("extensions") or None,
        )

        for preset in PRESETS:
            if preset["id"] == preset_id:
                return [dict(rule) for rule in preset["rules"]], preset_id, reason

        return rules, None, ""

    def _prepare(self, data, rules=None):
        """Resolve alias tables and any lookup, then hand the rules over."""
        shows = parse_table(data.get("shows_text"))
        artists = parse_table(data.get("artists_text"))
        report = []

        rules = data.get("rules") or [] if rules is None else rules
        needs_shows = any(r.get("type") == "preset_tv" for r in rules)

        if needs_shows:
            try:
                names = [os.path.basename(f) for f in fileops.list_folder(
                    data.get("path", ""),
                    bool(data.get("recursive")),
                    data.get("extensions") or None,
                )]
            except ValueError:
                names = []

            shows, report = lookup.resolve(
                names, shows, fileops._app_root(), bool(data.get("lookup"))
            )

        protect = [w.strip() for w in re.split(r"[,\n]", data.get("protect_text") or "") if w.strip()]

        return inject_tables(rules, shows, artists, protect,
                             bool(data.get("add_titles"))), report

    # -- routes -------------------------------------------------------------

    def do_GET(self):
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)

        if parsed.path == "/":
            if not self._authorised(query):
                self._send(403, "Open the app using the window it launched.", "text/plain; charset=utf-8")
                return

            with open(os.path.join(HERE, "ui.html"), "r", encoding="utf-8") as handle:
                page = handle.read().replace("__TOKEN__", TOKEN)

            self._send(200, page, "text/html; charset=utf-8")
            return

        if not self._authorised(query):
            self._send(403, {"error": "forbidden"})
            return

        if parsed.path == "/api/start":
            self._send(200, {
                "drives": fileops.drives(),
                "home": fileops.preferred_start(),
                "system_drive": fileops.SYSTEM_DRIVE,
                "presets": PRESETS,
                "rulesets": fileops.load_rulesets(),
                "undo": fileops.read_undo(),
                "sep": os.sep,
            })
            return

        if parsed.path == "/api/browse":
            path = query.get("path", [os.path.expanduser("~")])[0]

            try:
                parent = os.path.dirname(os.path.abspath(path).rstrip(os.sep)) or None
                self._send(200, {
                    "path": os.path.abspath(path),
                    "parent": parent if parent and parent != path else None,
                    "dirs": fileops.list_dirs(path),
                })
            except ValueError as exc:
                self._send(400, {"error": str(exc)})
            return

        self._send(404, {"error": "not found"})

    def do_POST(self):
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)

        if not self._authorised(query):
            self._send(403, {"error": "forbidden"})
            return

        data = self._body()

        if parsed.path in ("/api/preview", "/api/apply"):
            path = data.get("path", "")

            if path and fileops.is_system_path(path) and not data.get("allow_system"):
                self._send(200, {
                    "rows": [], "summary": {"total": 0, "rename": 0, "unchanged": 0, "error": 0},
                    "lookup": [], "done": 0, "errors": [],
                    "system_blocked": fileops.SYSTEM_DRIVE,
                })
                return

        try:
            if parsed.path == "/api/preview":
                chosen, auto_id, auto_reason = self._choose_rules(data)
                rules, report = self._prepare(data, chosen)
                info = {}
                rows = fileops.plan_folder(
                    data.get("path", ""),
                    rules,
                    bool(data.get("recursive")),
                    data.get("extensions") or None,
                    report=info,
                )
                self._send(200, {"rows": rows, "summary": engine.summarise(rows),
                                 "lookup": report, "auto": auto_id,
                                 "auto_reason": auto_reason, "rules": chosen,
                                 "common": info.get("common") or {}})
                return

            if parsed.path == "/api/apply":
                chosen, _auto_id, _auto_reason = self._choose_rules(data)
                rules, report = self._prepare(data, chosen)
                rows = fileops.plan_folder(
                    data.get("path", ""),
                    rules,
                    bool(data.get("recursive")),
                    data.get("extensions") or None,
                )

                only = data.get("only")

                if only:
                    keep = set(only)
                    rows = [r for r in rows if r["old_path"] in keep]

                # The plan is recomputed here rather than trusted from the
                # browser, which is right - but it means it can differ from the
                # table the user was looking at when they pressed the button
                # (they edited a field, or something changed on disk). Renaming
                # to something they were never shown is the one thing this tool
                # must never do, so a difference stops the batch instead.
                expect = data.get("expect") or {}

                if expect:
                    changed = [
                        {"old": row["old"], "shown": expect.get(row["old_path"]),
                         "now": row["new"]}
                        for row in rows
                        if row["old_path"] in expect and expect[row["old_path"]] != row["new"]
                    ]

                    if changed:
                        self._send(200, {"done": 0, "errors": [], "changed": changed,
                                         "undo": fileops.read_undo()})
                        return

                done, errors = fileops.apply_plan(rows)
                self._send(200, {"done": len(done), "errors": errors,
                                 "undo": fileops.read_undo()})
                return

            if parsed.path == "/api/undo":
                restored, errors = fileops.undo_last()
                self._send(200, {"restored": restored, "errors": errors,
                                 "undo": fileops.read_undo()})
                return

            if parsed.path == "/api/rulesets/save":
                data_out = fileops.save_ruleset(data.get("name"), data.get("rules") or [])
                self._send(200, {"rulesets": data_out})
                return

            if parsed.path == "/api/rulesets/delete":
                data_out = fileops.delete_ruleset(data.get("name"))
                self._send(200, {"rulesets": data_out})
                return

            if parsed.path == "/api/quit":
                self._send(200, {"ok": True})
                threading.Thread(target=self.server.shutdown, daemon=True).start()
                return

        except ValueError as exc:
            self._send(400, {"error": str(exc)})
            return
        except Exception as exc:  # never leave the UI hanging on a spinner
            self._send(500, {"error": "{}: {}".format(type(exc).__name__, exc)})
            return

        self._send(404, {"error": "not found"})


def main():
    port = int(os.environ.get("BULKRENAMER_PORT", "0"))
    httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    actual = httpd.server_address[1]
    url = "http://127.0.0.1:{}/?token={}".format(actual, TOKEN)

    # flush=True matters: without a terminal attached Python block-buffers, and
    # the address a user needs when the browser fails to open never appears.
    banner = [
        "",
        "  Bulk Renamer v1.5 is running.",
        "",
        "  If your browser did not open, paste this address into it:",
        "  " + url,
        "",
        "  Close this black window when you are finished.",
        "",
    ]

    for line in banner:
        print(line, flush=True)

    if os.environ.get("BULKRENAMER_NO_BROWSER") != "1":
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
