# Bulk Renamer

A batch file renamer for Windows. Point it at a folder, build a chain of rules,
check the preview, press Rename. Nothing to install.

Written because renaming a folder of downloads by hand — or in several passes
through a file manager — is tedious and easy to get wrong.

## Download

Grab `BulkRenamer.zip` from [Releases](../../releases), unzip it, and
double-click **Rename Files.bat**. It carries its own Python runtime, so there
is nothing to install and no internet connection required.

## What it does

- **Rule chain.** Rules run top to bottom, each on the result of the last, so a
  single pass replaces what would otherwise be several.
- **Compulsory preview.** Every file is shown as *current name → new name*
  before anything is touched.
- **Undo.** The last batch can be reversed, and the record survives closing the
  program.
- **Saved rule sets.** Name a rule chain once; reload it next month in two clicks.

Presets for the common shapes:

| Preset | Example |
| --- | --- |
| Music: Artist - Song | `01 - Radiohead - Karma Police.mp3` → `Radiohead - Karma Police.mp3` |
| TV: Showname S01E01 | `The.Bear.S03E07.1080p.WEB-DL.x265.mkv` → `The Bear S03E07.mkv` |
| Film: Title (Year) | `Dune.Part.Two.2024.2160p.WEB-DL.mkv` → `Dune Part Two (2024).mkv` |
| Just clean up the junk | strips resolution/codec/release tags, fixes separators and case |

The TV preset understands `S03E07`, `3x07`, `Season 3 Episode 7` and bare codes
like `307`, and it will not mistake a year for an episode number.

## Safety

The interesting part of a renamer is what it refuses to do.

- Two files that would land on the same name are **both** flagged and skipped —
  never silently overwritten. The rest of the batch still runs.
- Renaming onto a name that already exists is refused, *unless* that file is
  itself being renamed away in the same batch — so shifting `01,02,03` up to
  `02,03,04` works, while a genuine clash does not.
- Windows naming rules are enforced up front: illegal characters, reserved names
  (`CON`, `LPT1`, …), trailing dots or spaces, over-long names.
- Every rename goes via a temporary name, so **case-only** changes work on a
  case-insensitive filesystem and a half-finished batch cannot eat a file.
- A malformed regular expression leaves the preview alone rather than throwing.

## How it is put together

```
BulkRenamer/
  Rename Files.bat      launcher; uses the bundled runtime, falls back to PATH
  app/engine.py         the rule engine - pure functions, no filesystem at all
  app/fileops.py        everything that touches disk; two-phase rename; undo
  app/server.py         local HTTP API, 127.0.0.1 only, token-gated
  app/ui.html           the whole interface, no external assets
  runtime/              a trimmed Python 3.11 (not in this repo; in the release)
```

`engine.py` deliberately has no filesystem access. That is what makes it
testable, and it means the preview and the rename are produced by the same code
— there is no second implementation to drift out of step.

The UI is a local web page rather than a desktop window because the bundled
runtime ships without Tk, and a browser gives a far better preview table anyway.
The server binds to `127.0.0.1` only and every request must carry a token minted
at startup, so nothing else on the machine can drive it.

## Licence

MIT.
