BULK RENAMER
============

A batch file renamer for Windows. Nothing to install.


GETTING STARTED
---------------

1. Unzip the whole BulkRenamer folder somewhere sensible - your Documents
   folder is fine. Keep the folder together; the parts inside it need
   each other.

2. Double-click "Rename Files.bat".

3. A black window opens and stays open - that is the program running. Your
   browser opens with the renamer in it. If the browser does not open by
   itself, copy the address shown in the black window and paste it into
   your browser.

4. When you are finished, close the black window.

The program runs entirely on your own PC. Nothing is uploaded anywhere, and
it does not need an internet connection. It brings its own copy of the
Python runtime in the "runtime" folder, so you do not have to install
anything at all.


USING IT
--------

STEP 1 - pick a folder.
Use the tree. Click the little arrow to open a folder, click its name to work
on it - one click, there is no second "use this folder" step. You can still
type or paste a path in the box instead. Tick "include sub-folders" to work
through everything underneath as well. The "only these types" box limits it
to certain files - type mkv, mp4, mp3 and it will leave everything else alone.

STEP 2 - build a rule.
Press one of the starting points:

  TV: Showname S3E05 Title    your format. Turns
                              "The Americans 2013 S02E01 HDTV XviD-FUM"
                              into "A - Americans S2E01 Comrades".
                              Season is not padded, episode is. If a series
                              has fewer than 20 episodes and one season, it
                              uses "05of10" instead. An episode name is kept
                              when the filename has one, and placeholder
                              names like "Episode 3.05" are dropped.


  Music album:             for an album folder. Takes the artist from the
  Artist (from folder)     folder name and puts it in front of every track:
                           a folder called "Creedence Clearwater Revival -
                           Chronicle The 20 Greatest Hits (2023) Mp3 320kbps
                           [PMEDIA]" containing "01. Susie Q. (Single
                           Edit).mp3" gives "Creedence Clearwater Revival -
                           Susie Q.mp3". Track numbers and bracketed notes go.

  Music: Artist - Song     turns "01 - Radiohead - Karma Police.mp3" and
                           "Radiohead_-_Karma_Police.mp3" into
                           "Radiohead - Karma Police.mp3"

  TV: Showname S01E01      turns "The.Bear.S03E07.1080p.WEB-DL.x265.mkv"
                           into "The Bear S03E07.mkv". It understands
                           S03E07, 3x07, "Season 3 Episode 7" and bare
                           codes like 307.

  Film: Title (Year)       turns "Dune.Part.Two.2024.2160p.WEB-DL.mkv"
                           into "Dune Part Two (2024).mkv"

  Just clean up the junk   strips resolution, codec and release-group tags,
                           turns dots and underscores back into spaces, and
                           fixes the capitalisation.

Then add more rules on top if you need them. Rules run top to bottom, each
one working on the result of the last, and you can reorder them with the
arrows. That is the point of the whole thing: one pass instead of several.

The TV preset also removes the word "The" anywhere in a name, so
"The Americans S01E01" becomes "Americans S1E01". Season numbers are not
padded - S1, S2, and S10 only when a show really has ten or more seasons.
If you ever want "The" kept, delete the "Remove the word The" rule from the
list after clicking the preset.

STEP 3 - check the preview.
Every file is listed with its current name and the name it would get. Green
means it will be renamed, grey means nothing changes, red means there is a
problem. Nothing on your disk has been touched at this stage.

Then press Rename at the bottom.


LOOKING TV SHOWS UP
-------------------

"Look TV shows up online" is ON. It has to be, for the numbering to work: a
filename never says how many episodes a series has, and "3of6" cannot be
worked out without knowing there are six. It asks TVmaze, a free TV database,
and shows you underneath which series it matched and in what year - worth a
glance, because two shows can share a name.

Turn it off and nothing leaves your PC; you then get S1E03 instead of 3of6
unless you type the episode count in yourself.

"Also add the episode name when it is known" is OFF. Turn it on and
"Secret Invasion 3of6" becomes "Secret Invasion 3of6 Betrayed". An episode
name already written into the filename is kept either way.


IF A NAME COMES OUT WRONG
-------------------------

Some of what you asked for is simply not in the text of the filename, and no
tool can invent it:

  - An episode name that isn't there. "The Americans 2013 S02E01 HDTV
    XviD-FUM" contains no trace of the word "Comrades".
  - How many episodes a series has. "05of10" needs to know there are ten.
  - Your own shorthand. Nothing in "Le Bureau Des Legendes" says you file it
    as "Bureau", or that "The Americans" is "A - Americans".
  - Where an artist's name stops. "brian.kennedy.you.raise.me.up" gives no
    clue whether the artist is two words or three.

So there are two boxes on the screen for exactly this.

SHOW NAMES - one per line:

    Le Bureau Des Legendes = Bureau
    The Americans = A - Americans
    Band of Brothers = Band Of Brothers | 10

The left side is what appears in the filename, the right side is what you
want. The optional "| 10" is the number of episodes, which switches that
series to the 05of10 form.

ARTISTS - one per line, so the tool knows where the name ends:

    brian kennedy = Brian Kennedy
    daft punk = Daft Punk

LOOKING THINGS UP
-----------------

There is a tick box, off by default: "Look episode titles and counts up
online". With it on, the program asks TVmaze (a free TV database, no account
needed) for the episode name, the episode count and the number of seasons.
That is what makes "Comrades" and "05of10" appear by themselves.

With it off, nothing whatsoever leaves your PC.

Answers are saved, so running the same folder again needs no connection. The
line under the tick box tells you which series it matched, with the year -
worth a glance, because two shows can share a name and you would not want
episode titles from the wrong one.

Your own show names always win. If you have written "The Americans =
A - Americans", a lookup will not overrule it; it only fills in the facts you
would otherwise have to type.


CHOOSING WHICH FILES
--------------------

Every row that will be renamed has a tick box on the left. Untick any you want
left alone - the Rename button counts only the ticked ones. The box in the
table header ticks or unticks everything at once.

Your ticks survive a refresh, so you can untick a few files and then still
adjust a rule without losing the selection.


DRIVES
------

The C: drive is left alone by default. It is shown at the end of the drive
list, marked "(system)", and if you point the program at a folder on it you
get a warning and a button to allow it for that folder. Everything on D:
upwards works normally, and the program opens on E:\download when that
exists.


KEEPING WORDS THE CLEANER WOULD REMOVE
--------------------------------------

The junk list removes things like "cam", "sub", "eng" and "web". Occasionally
that is somebody's name. Put such words in the "Never remove these words" box,
one per line or comma separated, and they are left alone everywhere.


SAFETY
------

Nothing is renamed until you press Rename and confirm.

Files that would end up with the same name are refused, not overwritten. If
two files would both become "Radiohead - Karma Police.mp3", both are marked
red and left alone, and the rest of the batch still goes ahead.

Names Windows will not accept are refused too - illegal characters, reserved
names like CON or LPT1, names ending in a space or a dot, and names that are
too long.

Undo puts the last batch back. It survives closing and reopening the
program, so you can come back to it. It cannot help once you have run a
second batch on top, so check the result before moving on.


SAVING RULES FOR NEXT TIME
--------------------------

Once a rule set does what you want, press "Save current..." and give it a
name. Next month, pick it from the dropdown and press Load. That turns a
recurring ten-minute job into two clicks.

Saved rule sets and the undo record live in the "undo" folder inside
BulkRenamer. Copying the whole folder to another PC brings them along.


IF SOMETHING GOES WRONG
-----------------------

The browser does not open
  Copy the address from the black window into your browser by hand.

"The bundled runtime folder is missing"
  The zip was not extracted completely. Extract the whole folder again.

A file says "already exists in this folder"
  Something with that name is already there. Rename or move it first, or
  adjust the rule so the new names differ.

A file could not be renamed
  Usually it is open in another program - a video player holding the file,
  or the folder open in something that has locked it. Close it and try
  again. Anything that fails is reported by name and left untouched.


WHAT IT DOES NOT DO
-------------------

It renames files only. It never moves, copies, deletes or edits the contents
of anything, and it never touches folder names.
