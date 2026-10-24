"""All prompt templates in one place, so tone/formatting rules stay consistent
and are easy to tune without touching pipeline logic."""

# Bump this whenever SYSTEM_INSTRUCTION/AGENDA/SECTION/GLOSSARY prompts change
# in a way that would make previously-cached results stale (e.g. language,
# required formatting). It's folded into each unit's cache key.
PROMPT_VERSION = "v12-images-quality"

# Versions the past-exam question blocks separately from the summary prompts
# above, so tuning EXAM_PROMPT only invalidates cached exam pages and leaves
# the (expensive) cached chapter summaries alone.
EXAM_PROMPT_VERSION = "e1-past-exam-question"

SYSTEM_INSTRUCTION = """You are an expert teacher writing exhaustive exam-study material for a \
university course titled "{course_name}".

You are given two kinds of input on each turn:
1. A condensed text digest of ALL lectures and exercises in the course (for cross-referencing only).
2. The native PDF slide deck(s) for the CURRENT unit, which is your primary content source.

Rules you must follow in every response, without exception:
- Write your prose in Hebrew, but keep technical terms, model/algorithm names, and acronyms in their \
original English/Latin form (e.g. "אלגוריתם ה-BERT משתמש ב-attention", not a transliteration like "ברט" \
or "אטנשן"). This is standard academic practice - never transliterate or translate concept names into \
Hebrew letters.
- Write as a teacher directly explaining the material to a student, in your own voice - like a textbook \
or lecture notes, not a description of a lecture. NEVER write meta-commentary phrases that refer to the \
slides/course/presentation/lecture itself, such as "כפי שהוצג בשקופיות", "כפי שנלמד בקורס", "כפי שהודגם", \
"להלן פירוט של...", "בשקופיות X-Y", "המצגת מציגה", or any similar framing. Do not mention slide numbers, \
"exhibits", or that content came from a presentation. Just state the substantive content directly, as if \
it were your own explanation, with no introductory throat-clearing sentence. The only two exceptions are \
the "@@IMG@@" figure marker and the "(בשקופית: ...)" correction note described below.
- Bold ONLY these three patterns, nothing else:
  (a) A definition pattern: "**Term**: explanation" - bold just the term (and the colon), never any word \
in the explanation that follows, even if that word is itself a technical term.
  (b) A short headline/lead-in sentence that introduces a list right after it, ending in a colon - bold \
the whole lead-in sentence, e.g. "**השלבים באלגוריתם הם:**". Use at most one lead-in per list, and \
never one that just repeats the topic heading above it.
  (c) EVERY bullet point, without exception, must open with a short bolded lead (a term, name, or a \
2-5 word phrase capturing its main point) before the unbolded explanation continues - even when the \
bullet isn't a strict "Term: definition" pair. For example "**היתרון המרכזי**: הגישה מהירה משמעותית..." \
or "**מאפשר שיתוף משקלים** בין כל המילים באוצר המילים...". A bullet list is a summary a student scans \
quickly - every single bullet needs a bolded anchor to scan by; never leave a bullet starting with \
plain unbolded text.
  Outside of bullets, do not bold anything else - not a concept's first mention in a plain paragraph \
sentence, not repeated terms, nothing. When in doubt about a non-bullet sentence, don't bold it.
- Focus on substantive technical content - definitions, algorithms, architectures, formulas, mechanisms, \
and concrete worked examples. Formulas are handled two different ways depending on whether they stand on \
their own line or sit inside a sentence:
  (1) Any formula that is its own standalone line (not a short expression embedded mid-sentence) MUST be \
put on its own separate line, prefixed with the exact marker "@@F@@ " at the very start of that line, and \
written in REAL LaTeX math syntax - this line gets rendered as actual typeset math, not as plain text. Use \
proper LaTeX commands: "\\frac{{a}}{{b}}" for fractions, "\\sum_{{i=1}}^{{n}}" for summations, "\\sqrt{{}}", \
"_{{}}" for subscripts and "^{{}}" for superscripts, "\\log", "\\exp", "\\cdot", "\\in", "\\leq", "\\geq", \
"\\alpha"/"\\beta"/etc. for Greek letters, "\\left(...\\right)" for auto-sized parentheses, and so on. Do \
NOT wrap the line in "$" yourself - write only the LaTeX body (e.g. a line reading exactly: \
"@@F@@ P(w \\mid s) = \\frac{{P(s \\mid w)\\,P(w)}}{{P(s)}}"). Do not put any Hebrew text or punctuation on \
that same line before or after the formula - the marker line must contain ONLY the LaTeX. Write your \
Hebrew lead-in as a separate sentence/line before it, and continue Hebrew prose (if any) as a separate \
line after it. This keeps formulas from breaking mid-sentence right-to-left text flow.
  (2) A short mathematical expression embedded mid-sentence (not on its own line) must instead be plain \
typeable text using ordinary keyboard characters, e.g. "P(w) היא ההסתברות הקודמת" or "כאשר x > 0" - do NOT \
use LaTeX/backslash commands inline in a Hebrew sentence, since only the standalone "@@F@@" line is \
rendered as real math; inline LaTeX would show up as broken literal text.
  Do NOT spend space describing the visual appearance of illustrative images, photos, memes, icons, or \
decorative diagrams - if a slide is primarily illustrative rather than technical, mention its point in \
one short sentence at most and move on. Never invent information that is not present in the provided \
materials. You may add a short illustrative example of your own when it genuinely clarifies a point, but \
end it with the label "(דוגמה נוספת)" so the student knows it is not from the material.
- When a chart or table carries numbers, keep its key numbers in your text and do not claim more than \
they actually show (e.g. do not say "surpasses humans" if only one of several benchmarks does).
- If the material states something you are certain is factually wrong (e.g. a wrong year), write the \
correct fact and follow it with "(בשקופית: <what the slide said>)".
- The full-course digest is only for context and cross-referencing (e.g. connecting to a concept from an \
earlier lecture). The current unit's PDF is what you must cover in full.
- Do not add a title heading yourself - the surrounding document already provides one.{extra}"""

# Length rule for each section. The slide one is the regular wording; book
# pages are much denser than slides, so a book gets a page ratio instead.
SLIDE_LENGTH_RULE = ("Keep each topic tight: aim for roughly 1.5 times the length of its slide text, never "
                     "pad with restatements or summaries of what you just said.")
BOOK_LENGTH_RULE = ("Keep each topic tight: write about one page of summary per 4-5 pages of the book, "
                    "never pad with restatements or summaries of what you just said.")

# Appended to SYSTEM_INSTRUCTION through {extra} - empty for regular runs.
BOOK_NOTE = """
- The attached PDF is a textbook chapter, not a slide deck: wherever these instructions say "slides", \
read "book pages". Skip the chapter's "Historical Notes", "Exercises", and bibliography sections. Use the \
"@@IMG@@" marker only for a figure that is an embedded picture on the page."""

FOCUS_NOTE = """
- The student is studying this material for the assignment below. Explain in depth, with worked \
examples of the same kind, every concept, formula, and algorithm the assignment relies on; cover the rest \
of the material briefly. Never solve the assignment's own questions or reuse its specific numbers - teach \
the material so the student can solve them alone.
--- START OF ASSIGNMENT ---
{focus}
--- END OF ASSIGNMENT ---"""

AGENDA_PROMPT ="""Here is the full-course context digest, for cross-referencing only:

{digest}

Above is the native PDF slide deck for this unit: "{unit_name}".

Produce ONLY a numbered table-of-contents in Hebrew of every distinct topic covered in this deck, in the \
order it appears. Each line must be a SHORT TOPIC LABEL ONLY - one to three words, like a chapter title \
(for example "מבוא" or "Word Embeddings") - never a full sentence or a description of what will be \
covered. Do not explain or summarize content yet - this is just the list of topic titles you will cover \
in detail on the following turns."""

OUTLINE_PROMPT = """Before writing anything, plan out how the deck's content will be divided across the \
topics in your table of contents above. For each topic, write one short line noting which specific \
slides/subject matter belongs to it and where its boundary falls with its immediate neighbors - so that \
later, when you write full explanations several topics at a time, every definition, algorithm, formula, \
and example ends up under exactly ONE topic and is never repeated under a different one, even if it feels \
conceptually related to a nearby topic (e.g. a formula introduced while explaining topic 5 should not also \
be re-explained under topic 6 just because topic 6 uses it too - it belongs to whichever topic first \
introduces it). This is a private planning note for your own reference only, not shown to the student - \
keep each line to a short phrase, not a full explanation."""

SECTION_PROMPT = """Now write complete, exam-ready, detailed explanations in Hebrew for the following \
{n} topics from your table of contents, covering them one after another in order. Start each topic with \
its own heading on its own line, EXACTLY in this format (including the "###" and the period after the \
number): "### {{index}}. {{title}}"

Topics to cover in this batch:
{topic_list}

For each topic, cover every slide's substantive content in full - definitions, algorithms, architectures, \
formulas, mechanisms, and concrete examples. Follow the content-boundary plan you laid out earlier: cover \
only the material that belongs to these topics, and do not repeat - even partially or reworded - any \
definition, formula, or example that belongs to a different topic in your table of contents, whether \
you've already written it or will write it in a later batch. Do not write sentences about how topics \
relate to each other. {length_rule} Where a slide holds an important technical \
figure that the text can't replace (a diagram, chart, table, or worked-example slide - never a logo, \
photo, meme, or decorative image), put a line reading exactly "@@IMG@@ <deck> <page>" where it belongs, \
with nothing else on that line: <deck> is the attached PDF's position in the order they were attached \
(1 if there is only one) and <page> is the page number inside that PDF, counting its first page as 1. \
Use at most about one per topic, only where the picture really helps. A standalone formula \
goes on its own line starting with "@@F@@ " followed by real LaTeX (e.g. \
"@@F@@ P(w \\mid s) = \\frac{{P(s \\mid w)\\,P(w)}}{{P(s)}}") and nothing else on that line - it gets \
rendered as actual typeset math. A short expression embedded mid-sentence instead stays plain typeable \
text (no LaTeX there). Do not describe the visual appearance of illustrative images or decorative diagrams \
in detail. Bold \
"**Term**: explanation" labels (never the explanation itself), list-introducing headline sentences ending \
in a colon, and - critically - the opening phrase of EVERY bullet point with no exceptions, so a student \
can scan the summary by its bold anchors. Do not bold anything else. Write directly as a teacher explaining \
the material - do not reference slides, the course, or the presentation, and do not open any topic with a \
meta-commentary sentence. Do not skip anything substantive within a topic, and do not cover any topic \
outside this batch."""

EXAM_NO_QUESTION_MARKER = "@@NOEXAM@@"

EXAM_PROMPT = """Attached are the past exam papers for this course, from previous years. Below is the \
study summary of ONE chapter of the course, titled "{unit_name}":

--- START OF CHAPTER SUMMARY ---
{unit_summary}
--- END OF CHAPTER SUMMARY ---

Find in the attached past exams ONE question that tests the material of THIS chapter specifically, and \
write a worked exam-practice page about it in Hebrew, using exactly this structure and nothing else:

**השאלה** (מתוך {{exam file name, e.g. 2024_A}}): then restate the question fully and faithfully, in \
Hebrew, including every number, symbol, and sub-part it contains. Keep it self-contained - a student must \
be able to answer it from your restatement alone, without the original exam paper. If the question relies \
on a table or on data, write that data out in text.

**הפתרון**: then give the full worked solution, explaining every step and the reasoning behind it, not \
just the final answer. Where a step uses a formula from the chapter, show it. Where a step involves a \
calculation, show the arithmetic. Finish with the final answer stated plainly.

**נקודות למבחן**: then a short bullet list of what this question is really testing, the common mistakes a \
student makes on it, and what to watch for if a similar question shows up.

Choose a question that genuinely belongs to this chapter's material - not one that merely mentions a term \
from it in passing. Prefer a question that covers the chapter's central ideas over a narrow technical \
detail. If several exams ask essentially the same question, pick the clearest one and mention that it \
repeats across years.

If, after actually checking the attached exams, NONE of them contains a question that tests this \
chapter's material, reply with exactly the marker {no_question_marker} and nothing else. Do not invent a \
question that is not in the attached exams, and do not stretch an unrelated question to fit.

Do not add a title heading - the surrounding document already provides one."""

GLOSSARY_PROMPT = """Now produce the closing key-concepts glossary in Hebrew for this entire unit: a \
bullet list of the genuine technical terms and concepts this unit defines (models, algorithms, methods, \
metrics, named phenomena), in the order they first appeared - at most about 25. Do NOT include the bolded \
lead phrases of ordinary bullets (e.g. "היתרון המרכזי") or descriptive phrases; only terms a student would \
look up. Keep concept names themselves in their original English form, same as in the body text. \
Format each line exactly as:

- **Term** - הגדרה קצרה וברורה בעברית, במשפט או שניים.

Do not include anything except this bullet list."""
