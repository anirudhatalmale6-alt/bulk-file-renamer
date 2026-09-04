"""Local web UI for Bulk Renamer.

A browser page is used instead of a desktop window for a practical reason: the
bundled Windows runtime has no Tk, and a browser gives a far better preview
table than a Tk grid would. Nothing leaves the machine - the server binds to
127.0.0.1 only, and every request must carry a token generated at startup, so
nothing else on the PC can drive it.
"""

import json
import os
import secrets
import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import engine  # noqa: E402
import fileops  # noqa: E402

TOKEN = secrets.token_urlsafe(24)
HERE = os.path.dirname(os.path.abspath(__file__))

PRESETS = [
    {
        "id": "artist_song",
        "label": "Music:  Artist - Song",
        "rules": [{"type": "preset_artist_song"}],
    },
    {
        "id": "show_episode",
        "label": "TV:  Showname S01E01",
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
            home = os.path.expanduser("~")
            self._send(200, {
                "drives": fileops.drives(),
                "home": home,
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

        try:
            if parsed.path == "/api/preview":
                rows = fileops.plan_folder(
                    data.get("path", ""),
                    data.get("rules") or [],
                    bool(data.get("recursive")),
                    data.get("extensions") or None,
                )
                self._send(200, {"rows": rows, "summary": engine.summarise(rows)})
                return

            if parsed.path == "/api/apply":
                rows = fileops.plan_folder(
                    data.get("path", ""),
                    data.get("rules") or [],
                    bool(data.get("recursive")),
                    data.get("extensions") or None,
                )

                only = data.get("only")

                if only:
                    keep = set(only)
                    rows = [r for r in rows if r["old_path"] in keep]

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
        "  Bulk Renamer is running.",
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
