"""Optional episode-title lookup.

Some of what the client wants simply is not in the filename. "The Americans
2013 S02E01 HDTV XviD-FUM" has to become "... S2E01 Comrades", and no amount of
cleverness extracts "Comrades" from that text. The same goes for the "05of10"
form, which needs to know the series has ten episodes.

So this module asks TVmaze (free, no account, no key). It is OFF unless the user
turns it on, every answer is cached on disk so a second run needs no network at
all, and any failure degrades to "no extra information" rather than an error.

Kept apart from engine.py deliberately: the engine stays pure and offline, and
the results are handed to it as ordinary table entries.
"""

import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request

import engine

API = "https://api.tvmaze.com"
TIMEOUT = 8
CACHE_NAME = "show-cache.json"


def _cache_path(root):
    folder = os.path.join(root, "undo")
    os.makedirs(folder, exist_ok=True)

    return os.path.join(folder, CACHE_NAME)


def load_cache(root):
    try:
        with open(_cache_path(root), "r", encoding="utf-8") as handle:
            data = json.load(handle)

        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_cache(root, cache):
    try:
        with open(_cache_path(root), "w", encoding="utf-8") as handle:
            json.dump(cache, handle, indent=1)
    except OSError:
        pass


def _get(url):
    request = urllib.request.Request(url, headers={"User-Agent": "BulkRenamer/1.1"})

    with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
        return json.loads(response.read().decode("utf-8"))


def fetch_show(query):
    """Look one show up. Returns a dict, or None if anything at all goes wrong."""
    try:
        url = "{}/singlesearch/shows?q={}".format(API, urllib.parse.quote(query))
        show = _get(url)

        if not show or "id" not in show:
            return None

        episodes = _get("{}/shows/{}/episodes".format(API, show["id"]))

        titles = {}
        seasons = set()

        for episode in episodes:
            season = episode.get("season")
            number = episode.get("number")

            if season is None or number is None:
                continue

            seasons.add(season)
            titles["{}x{}".format(season, number)] = episode.get("name") or ""

        return {
            "name": show.get("name") or query,
            "premiered": (show.get("premiered") or "")[:4],
            "episodes": len(titles),
            "seasons": len(seasons),
            "titles": titles,
        }
    except (urllib.error.URLError, ValueError, KeyError, TypeError, OSError):
        # Offline, blocked, rate-limited, or an unexpected shape. None of those
        # should stop the user renaming files.
        return None


def show_query(stem):
    """The text to search for: whatever sits before the episode code, cleaned."""
    found = engine.find_episode_span(stem)
    before = found[0] if found else stem

    return engine.strip_junk(before, drop_year=True).strip()


def resolve(filenames, user_shows, root, enabled):
    """Build the shows table the engine will use.

    The user's own table always wins on the NAME - that is where their aliases
    live ("Le Bureau Des Legendes" -> "Bureau"), and a lookup must never
    overwrite a name they chose. The lookup only fills in what they cannot
    reasonably type: episode counts, season counts and episode titles.

    Returns (table, report) where report lists what was matched, so a wrong
    match is visible rather than silently applied.
    """
    table = [dict(entry) for entry in (user_shows or [])]
    report = []

    if not enabled:
        return table, report

    cache = load_cache(root)
    dirty = False

    queries = []

    for name in filenames:
        stem, _ = engine.split_name(name)
        query = show_query(stem)

        if query and query.lower() not in [q.lower() for q in queries]:
            queries.append(query)

    for query in queries:
        key = re.sub(r"\s+", " ", query.strip().lower())

        if key in cache:
            found = cache[key]
        else:
            found = fetch_show(query)
            cache[key] = found
            dirty = True

        if not found:
            report.append({"query": query, "matched": None})
            continue

        existing = engine.lookup_show(query, table)

        if existing is not None:
            # Keep their alias, fill in the facts they cannot type.
            existing.setdefault("episodes", found["episodes"])
            existing.setdefault("seasons", found["seasons"])
            existing.setdefault("titles", found["titles"])
            existing["episodes"] = existing.get("episodes") or found["episodes"]
            existing["seasons"] = existing.get("seasons") or found["seasons"]

            if not existing.get("titles"):
                existing["titles"] = found["titles"]

            shown = existing.get("name")
        else:
            table.append({
                "match": query,
                "name": found["name"],
                "episodes": found["episodes"],
                "seasons": found["seasons"],
                "titles": found["titles"],
            })
            shown = found["name"]

        report.append({
            "query": query,
            "matched": found["name"],
            "premiered": found.get("premiered", ""),
            "episodes": found["episodes"],
            "seasons": found["seasons"],
            "used_as": shown,
        })

    if dirty:
        save_cache(root, cache)

    return table, report
