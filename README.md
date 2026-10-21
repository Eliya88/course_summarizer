# exam_prep_summarizer

Turns a course's lecture + exercise PDFs into one exam-study PDF, using the
Claude API (Claude Haiku 5.5). Works for any course with a similar folder layout - not just
Data Science.

## How it works

For each **unit** (one lecture, or a group of lectures on one subject) it:

1. Sends the unit's actual PDF(s) to Claude natively (so slide layout,
   diagrams, and visuals are read directly - no lossy text conversion).
2. Sends along a lightweight text digest of *every* lecture/exercise in the
   course (extracted locally, cheaply) so Claude has full-course context for
   cross-referencing, without re-uploading everything as images each time.
3. Cuts each PDF into page ranges (`PARTS_PER_FILE` in `src/pipeline.py`,
   currently 2) and, for each range, asks for a table of contents first,
   then one detailed, fully-bolded explanation **per section**, then a
   closing glossary - inside one chat session per range. Topic numbers keep
   counting across ranges, and the glossaries are merged. This is the
   fix for the "one summary is too short / misses material" problem: each
   section gets its own dedicated response instead of being squeezed into a
   single reply.
4. Optionally (`--with-tests`) finds a question from the course's past exam
   papers that tests that unit, and writes it up with a full worked
   solution as a practice page at the end of the unit.
5. Merges every unit into one Markdown document (cover page + table of
   contents, then one section per unit, each starting on a new page) and
   renders it to a styled, right-to-left **Hebrew** PDF with a handwritten
   look (Playpen Sans Hebrew, notebook-ruled background). Claude is
   instructed to write in Hebrew but keep technical terms/model names/
   acronyms in their original English form inline, matching normal academic
   convention (e.g. "אלגוריתם ה-BERT").

The rendered PDF carries the chapter's name in small type at the top of
every page, and the contents list on the cover is clickable - each entry
jumps straight to that chapter.

Results are cached per-unit under `output/<course>/cache/`, keyed by the
unit's source filenames + modified-times, so re-running after editing one
config or adding one new lecture doesn't re-call the API for units that
didn't change. Use `--no-cache` to force a full re-run.

## Setup

```
py -3.12 -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

Put your Anthropic API key in `.env` as `ANTHROPIC_API_KEY=...` (see
`.env.example`). Create the key and buy prepaid credits (minimum $5) at
https://console.anthropic.com - Settings > Billing.

## Usage

### Option A: point at a course folder (no config file needed)

If a course folder has a `Lectures` subfolder and/or an `Exercises` subfolder
(matching is case-insensitive and also accepts "Exrecises", "Tutorials"),
just run:

```
python main.py --course-dir "../NLP"
```

Every PDF found becomes its own unit, named after the file, in natural
filename order (`L2` sorts before `L10`). Course name and output folder are
taken from the directory name. To exclude a PDF (e.g. a duplicate/merged
file you don't want double-processed), rename it with a leading underscore,
e.g. `_exrecises.pdf` - the scanner skips those.

### Option B: hand-written YAML config

Use this if you want multiple lectures grouped into one unit, custom unit
names, or a non-standard folder layout. Copy `courses/data_science.yaml` as
a template, point `lectures_dir` / `exercises_dir` at the course's real
folders, and list which lecture files belong to which `unit`.

Helper to list PDFs in a folder (paste the output into a unit's
`lecture_files:`):
```
python main.py --list-pdfs "../NLP/Lectures"
```

Then run:
```
python main.py --course courses/data_science.yaml
```

Output PDF lands in `output/<course_name>/<course_name>_exam_prep.pdf`
either way.

Flags:
- `--no-cache` - re-call Claude for every unit even if cached.
- `--refresh-context` - rebuild the full-course text digest (only needed if
  you added/edited a source PDF and want the cross-reference context to
  reflect it immediately; it also auto-rebuilds when file mtimes change).
- `--no-exercises` - skip exercise/tutorial units, summarizing only the
  lecture units. Exercises are included by default.
- `--with-tests` - after each chapter, add a practice page holding one
  question from the course's past exams that tests that chapter, a full
  worked solution, and a short "what this is really testing" list. Off by
  default.

  The exam papers are found automatically: any folder in the course
  directory named `מבחנים`, `בחינות`, `tests`, or `exams` (case-insensitive),
  or an explicit `exams_dir:` in a YAML config. If the flag is passed but
  no such folder exists, the run just carries on without exam pages. When
  the exams contain nothing relevant to a given chapter, that chapter
  simply gets no practice page rather than an invented question.

  Cost note: the exam papers are sent with every chapter's request, so a
  large exam archive is the dominant cost of this flag - roughly 258 tokens
  per exam page, per chapter (17 papers / 418 pages across 19 chapters
  works out to about **$1.20** on top of the normal run). Exam pages are
  cached separately from the chapter summaries, so turning the flag on
  later doesn't re-summarize anything, and tweaking `EXAM_PROMPT` only
  invalidates the practice pages.

## Cost

The default model is `claude-haiku-5-5` ($0.10 / 1M input tokens and
$0.50 / 1M output tokens for prompts up to 100K tokens; $0.50 / $2.50
beyond that); set `model:` in the course YAML to use another. The model is
part of the cache key, so switching models regenerates every unit.

The dominant cost driver is the full-course text digest (50-60K tokens for
a typical course), which is resent as conversation history on *every* API
call within a chat. Each request turns on prompt caching, so after the first
call of a chat the digest and PDFs are read from cache at a fraction of the
input price. Haiku 5.5 thinks by default (billed as output tokens). No full
run has been measured on Claude yet - check the usage page in the Anthropic
Console after the first unit. Rerunning with the cache warm (no source
changes) costs nothing.

To cut cost, raise `SECTION_BATCH_SIZE` in `src/pipeline.py` (currently 4
topics/call) or set `PARTS_PER_FILE` back to 1 - fewer, larger calls means
fewer times the digest gets resent, which is where most of the cost comes
from. A single lecture with
~20 topics can mean ~9 sequential API calls and take 10-15 minutes
wall-clock - that's expected, not a hang.

Cached results are keyed by source files *and* a prompt version string, so
changing the language/formatting rules in `src/prompts.py` (bump
`PROMPT_VERSION`) automatically invalidates old cached results instead of
silently reusing stale output.

## Fonts

`assets/fonts/PlaypenSansHebrew.ttf` (OFL-licensed, from Google Fonts) is
embedded directly - it's the only font used, for both Hebrew and any inline
English/Latin terms, so mixed-language lines render consistently.

## Adding a new course

Just run `python main.py --course-dir "path/to/the/course/folder"` (see
Option A above) - no config file needed as long as the folder has a
Lectures/Exercises-style subfolder layout. Fall back to a YAML config
(Option B) only if you need custom unit grouping or a non-standard layout.
