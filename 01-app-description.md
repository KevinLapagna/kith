# Kith — Name & Face Trainer
## Document 1 of 2: The Application, Described From the User's Side

> **Working title:** *Kith* — settled, and it only appears in strings, so changing it later is cheap.
>
> **Companion document:** `02-architecture.md` (stack, data model, scheduler, deployment).
> **Status:** Draft v0.2 — decisions in §12 settled with Kevin on 2026-10-01 (all defaults
> accepted except the UI language, now **English only**).

---

## 1. The problem, in one paragraph

You meet people, you remember their faces perfectly, and the name is gone. The face is
familiar, the name does not come. This is not a memory defect — it is the normal asymmetry
between *recognition* (faces, cheap, automatic) and *recall* (names, expensive, needs
cues). It gets worse for people you meet rarely, because between two encounters you have
had zero retrieval practice, and retrieval practice is the only thing that actually moves
a name into durable memory.

Vocabulary trainers already solved the scheduling half of this problem (spaced
repetition). Nobody solved the *ergonomics* half for the specific case of faces: a
trainer that lives on photographs, is answered by pointing and dragging, and spends your
scarce attention where your memory is actually weak. **Kith is that — a private,
single-file, point-and-click trainer for the names of the people in your life.**

---

## 2. What it is (and what it deliberately is not)

**It is:** a private web app on your own machine. One SQLite file — people, photos,
history, statistics — that you can copy to a USB stick and it *is* the whole app state.
Two modes: **Add** (file new people) and **Train** (practise). Optional groups so you can
drill "the wedding guests" or "the new team" without wading through 200 faces.

**It is not:** a social app, a CRM, a face-recognition system, a quiz game with points and
streaks that makes you feel bad, or anything that sends a photo of a real person to a
cloud service. No accounts, no telemetry, no "AI magic" required for v1.

---

## 3. Core concepts — the vocabulary of the app

| Term | Meaning |
|---|---|
| **Person** | One human you want to know. Has a first name (what you are asked for), an optional last name (shown small beside the answer, learned on the side), optional notes — and an optional *ask for* override for when the answer should be something other than the first name. |
| **Photo** | An image attached to a person. A person can have several (different days, angles, hair). One is the *primary* photo. |
| **Card** | A trainable unit derived from a person. A person may have **two cards**: *name → face* and *face → name*. Each carries its own scheduling state. |
| **Group** | A named set of people (overlapping allowed). Manual, or rule-based ("everyone tagged *Work*"). |
| **Due** | A card whose next review date has arrived. |
| **Session** | One sitting of training: a chosen group, a length, a set of allowed exercise modes. |
| **Exercise mode** | *How* you are asked (multiple choice, matching 3, free recall…). Modes are optional per session. |
| **Rating** | How well you did on one prompt: **Again / Hard / Good / Easy**. Usually derived automatically from your answer; you may override it with one tap. |
| **Leech** | A card you have failed repeatedly. The app stops just re-asking it and instead asks you to encode it better (mnemonic/feature/extra photo). |

---

## 4. The six principles that drive the whole design

Read this section; every feature below is traceable to one of these. Sources in §13.

1. **Retrieval beats re-reading.** Being asked and answering (even badly) builds memory;
   looking at a card does almost nothing. → The app is a *testing* machine, not a flashcard
   viewer. A "study" mode exists only for the first exposure of a brand-new person.
2. **Spacing beats cramming.** Repeated retrievals spread over days/weeks are far more
   durable than one block (effect size ~0.74 for spaced vs massed retrieval). → A real
   scheduler, not a "shuffle" button.
3. **Ask about the weak, rest the strong.** Two mechanisms: the scheduler surfaces items
   whose *predicted* recall probability has dropped, and within a session the items you
   got wrong are re-queued while the ones you nailed are dropped. → "What I always get
   right appears less; what I get wrong appears more", exactly as you asked.
4. **Interleave, do not block.** Mixing people forces you to *discriminate* — the actual
   skill, since the failure mode is "I know the face, I can name the wrong person's name".
   → Sessions mix people by default; there is a dedicated **confusion drill** for the pairs
   you keep swapping.
5. **Recognition first, production when ready.** Point-and-click multiple choice is easier
   (more cues); producing the name from the face is the real-world skill and is harder. The
   app climbs that ladder automatically per person (and you can pin a mode).
   *Interesting research detail:* unlike with ordinary vocabulary, making you **write or say
   the name adds nothing measurable for face–name pairs** — so writing stays strictly
   optional, and the default answer gesture is a tap or a drag. Your instinct here was right.
6. **Correct the error immediately, then re-ask soon.** Wrong answer → the correct name is
   shown at once, and the item comes back later in the same session (short, then longer
   gap), so the correction is retrieved, not just read. This is the clinical
   "spaced retrieval" protocol, which was originally developed *on exactly this task*:
   students learning names from photographs.

---

## 5. The three areas of the app

### 5.1 **Add** — the filing cabinet

**Flow for adding one person (target: under 20 seconds):**

```
┌─ New person ───────────────────────────────────────────────┐
│                                                            │
│   ┌──────────────┐   Name *     [ Anna                 ]   │
│   │              │   Surname    [ Brunner              ]   │
│   │   drop a     │   Called     [ Andi        ] (nickname) │
│   │   photo here │   Spoken     [ AH-na ]  ▶︎ (pronounce) │
│   │      or      │                                           
│   │  [choose…]   │   Where met  [ Fabienne's birthday ]     │
│   └──────────────┘   When       [ 2026-09-27 ]             │
│                      Groups     [ +Family ] [ +Zurich ]    │
│                                                            │
│   ▸ More: mnemonic · distinctive feature · notes · tags    │
│   ▸ Add another photo                                      │
│                                                            │
│              [ Save and add another ]  [ Save ]            │
└────────────────────────────────────────────────────────────┘
```

* **Photos:** drag & drop, phone camera roll, or paste from clipboard (⌘V). Multiple
  photos per person, mark one as *primary*. The app crops/scales automatically (max 1024 px
  long edge) and stores a thumbnail; originals optional.
* **Optional encoding fields** — this is the "make it stick on purpose" part, and it is
  what turns a repeated failure into a success:
  * **Mnemonic** — "Anna → *Anna-logue* watch: she wears one".
  * **Distinctive feature** — a free text note, e.g. "gap between front teeth". Better: let
    you **click a point on the photo** to drop a marker. When you later fail this person,
    the app shows the marker and the note.
  * **Where/when met**, plus free notes. Context is stored *with* the name, and the app can
    quiz you on it (`Where did I meet her?`) — recall of context frequently unlocks the name.
  * **Spoken form** — type how the name sounds, or record 2 seconds of your voice. Used by
    the optional listening mode.
* **Bulk import:** drop a folder / zip / a CSV (`filename,name,group,…`). Handy after an
  event when you photographed the seating plan. Filenames are matched to a name column.
* **Maintenance:** edit, archive (keeps history, hides from training), delete, **merge two
  people** (for when you accidentally filed someone twice), reorder photos, "pause until
  date" (for someone you will not see again for months).
* **Quick-add from anywhere:** a `+` button in the header, and a keyboard shortcut. Also
  available *during* training ("a face I do not have yet") without losing the session.

### 5.2 **Groups** — train on a subset

* A person can be in **any number** of groups; groups overlap freely.
* **Manual groups** (hand-picked) and **rule groups** (a saved filter): *"Photo ≥ 1 AND
  not seen since 60 days AND difficulty ≥ 0.8"*, or simply a tag. Rule groups keep
  themselves current — a "New this month" group is always correct without maintenance.
* Built-in smart groups, always present: **Due now**, **New (never asked)**, **Hardest
  20**, **My confusion pairs**, **Leeches**, **Paused**.
* One group can be flagged as the **default session** group, so "Train" is one click.

### 5.3 **Train** — the actual learning

**The session builder** (every field has a sensible default; you can always just hit *Start*):

```
┌─ Start a session ──────────────────────────────────────────┐
│ Train on:  ( ) Everything    (•) Group(s): [Family][Work]  │
│                                                [+ add]     │
│ Length:    (•) 10 min   ( ) 20 items   ( ) open-ended      │
│ New faces: [ 3 ]   (how many never-seen people to introduce)│
│                                                            │
│ Exercise modes (untick to exclude):                        │
│   [✓] Multiple choice — 4 names, 1 face                    │
│   [✓] Reverse — 4 faces, 1 name                            │
│   [ ] Match 3 faces ↔ 3 names (drag)                       │
│   [ ] Match 6 faces ↔ 6 names (drag)                       │
│   [✓] Name it yourself (think, then reveal)                │
│   [ ] Listening — hear the name, pick the face             │
│   [✓] Confusion drill (pairs I mix up)                     │
│   [ ] Study cards (no questions — first exposure only)     │
│                                                            │
│ Difficulty: ( ) Easy  (•) Adaptive  ( ) Stretch            │
│ Limits:     [✓] never repeat a face twice in a row         │
│             [✓] keep similar faces apart                   │
│                                                            │
│                     [ Start ]   [ Save as preset ]         │
└────────────────────────────────────────────────────────────┘
```

* **Presets:** "5-minute coffee break", "before the party (everyone going)",
  "new colleagues only", "hard cases".
* **Adaptive difficulty** does the thing you asked for:
  * Distractors are chosen to be **confusable** with the target — same group, similar age
    band, similar hair, or faces you have historically swapped. Harder distractors → more
    learning, as long as you still succeed often enough.
  * Three wrong in a row on one person → the app drops that person to an easier mode for
    the rest of the session (and picks them back up next time), rather than grinding you
    down.
  * Conversely, repeated easy successes quietly promote a person to the *produce the name*
    mode — that is the mode that matters in real life.

**In-session behaviour:**
* A thin progress bar, running score, and **always an "End session" button**. Target
  duration defaults to ~10 minutes; overrunning is opt-in.
* Wrong answer → correct name shown immediately, big, with the mnemonic/feature if you
  entered one → item is re-queued: first after ~5 other items, again later if needed
  (this is the "successive relearning" pattern: recall to a criterion, several times, spaced).
* Right answer → item is retired for this session once it has been recalled correctly the
  required number of times (default: **1**; configurable up to 3).
* **Hint ladder** instead of a dead end: `▶ Hint` reveals progressively — first letter →
  number of syllables → first two letters → half the name → full name. Using a hint costs
  you the "Easy" rating. (This is the "cueing hierarchy" used in speech therapy: never let
  the learner sit in failure, fade the cues instead.)
* **I have no idea** button — an honest "never seen it" that is logged differently from a
  wrong answer (it means *encoding*, not *retrieval*, is missing).
* **"I said it out loud and it was right"** — for the free-recall mode you rate yourself in
  one tap; no typing required, ever.
* Keyboard: `1–4` or `A–D` to answer, `Space` to reveal, `Enter` for next, `H` for hint,
  `Esc` to pause. Every single interaction is also reachable with mouse/finger.

**End-of-session summary:** what you got right, what you got wrong, the three names worth
a second look tonight, and when each person will next come up. One click to "drill my
mistakes from this session".

### 5.4 **Progress** — the quiet dashboard

* **Per person:** photo, name, how well known (a 0–100 "strength" number derived from the
  scheduler's stability), accuracy trend, number of reviews, last seen, next due,
  average time to answer, **who you confuse them with**.
* **Per group:** coverage (how many people in this group can you name without help?),
  weakest members, next-due forecast.
* **Overall:** reviews/day (small bar chart), an estimate of your retention, distribution
  over the "difficulty" spectrum, and a **confusion matrix** — a list of pairs like
  *"Anna ↔ Andrea: 7 mix-ups"* with a one-click "drill this pair".
* **Honest calibration:** after each answer you *may* tap a confidence face (😐/😀), and the
  dashboard shows where your confidence and your accuracy diverge — the "I was sure but
  wrong" cases are the most useful thing you can know about your own memory.
* **No streaks, no points, no guilt.** Progress is shown as capability ("you can now name
  68 of 92 people in *Work*"), not as a score to defend.

---

## 6. The exercise modes, in detail

Difficulty here means *how much retrieval it demands* — not how hard the UI is.

| # | Mode | Direction | Interaction | Demand | What it trains |
|---|---|---|---|---|---|
| 1 | **Multiple choice, 4 names** | face → name | tap one of 4 name chips under the photo | low | recognition, discrimination between *names* |
| 2 | **Reverse multiple choice** | name → face | tap one of 4 photos | low–mid | discrimination between *faces* (useful when two people look alike) |
| 3 | **Match 3 ↔ 3** | both | drag names onto faces (or tap-tap) | mid | simultaneous discrimination of a small cluster |
| 4 | **Match 6 ↔ 6** | both | as above, bigger board | mid–high | capacity + sustained discrimination |
| 5 | **Name it yourself** | face → name | think / say it out loud → `Space` to reveal → self-rate 1–4 | high | the real-world skill (production) |
| 6 | **Listening** | audio → face | 🔊 play (browser speech synthesis, or your recording) → pick the face | mid | phonology of names you only ever saw written |
| 7 | **Confusion drill** | face → name | the specific pairs you swap, asked back-to-back | high | the exact error you keep making |
| 8 | **Context quiz** | face → context | "Where did I meet her?" → multiple choice | low–mid | the *context* cue that unlocks the name |
| 9 | **Study cards** | — | flip through, no question, shows name + mnemonic + feature marker | none | first exposure only (must happen *before* the first retrieval) |
| 10 | **Photo variety** | face → name | same name, a *different* photo than usual | mid | generalisation (hair changed, different day) |

Rules that apply to every mode:
* One target, and never the same target twice in a row.
* Distractors come from the same group where possible; if the group is smaller than the
  number of options needed, the app widens the pool but prefers *plausible* faces.
* After a wrong answer the correct answer is displayed with the target's *primary* photo and
  full context, then re-queued.
* Every mode degrades gracefully: **tap-tap** is always available as an alternative to
  **drag** (critical on a phone, and for anyone who hates drag targets).
* Modes are opt-in per session (§5.3). Auto mode picks per item: new person → mode 9 first,
  then 1; weak → 3/5/7; strong → 5 with harder distractors.

---

## 7. Extra features I propose (my additions, with reasons)

These are beyond your brief; take or leave each.

| Feature | Why it earns its place |
|---|---|
| **Printable cheat sheet** | Before an event: one page, grid of faces + names for *exactly the people who will be there*, produced from the "guests" group. Cheap to build, absurdly useful in the hour before a party. Also exportable as PDF/PNG. PDF export via HTML→print CSS. |
| **Mnemonic + feature marker** | The single highest-leverage feature for the people you *keep* forgetting: a repeated failure is almost always an encoding failure, and these two fields are the evidence-based fix. The app prompts for them automatically when a card becomes a leech. |
| **Multiple photos per person** | Interleaving photos of the same person (varied practice) prevents the classic "I only recognise the photo I learned from". |
| **Confusion pairs** | Your actual error is rarely "I do not know him" but "I thought he was Thomas". Nothing else in a normal flashcard app captures *which* wrong answer you gave. Logging the chosen distractor makes this drivable. |
| **Leech rescue flow** | Rather than asking a hopeless card forever, the app stops and says: *"Third failure. Want to add a mnemonic, mark the distinctive feature, or add another photo?"* Then it resets that card to easy mode. |
| **Context quiz** | Where you met them is one of your strongest retrieval cues and it is nearly free to store. |
| **Pronunciation / listening mode** | Names are sounds; many are only ever seen in writing (Slack, email). Browser speech synthesis makes this free (no service, offline). |
| **"Not now / pause person"** | People leave your life. Training on them is waste; but deleting loses useful history. |
| **Quick-add mid-session** | The moment you realise someone is missing from the app is *while* training. Do not make it a context switch. |
| **Phone-friendly, installable (PWA)** | The realistic training moment is 3 minutes on the sofa or waiting for a train. Add-to-home-screen + offline caching. |
| **Copy the backup button** | "Download the .sqlite" — that *is* the backup and the export, and it proves the single-file promise. |
| **i18n from line one** | English-only for now, but every string comes from one table, so a German locale is a drop-in later — not a refactor. |
| **Accessibility** | Photos with alt text = the person's name, full keyboard operation, large hit targets, no reliance on colour alone. This is also just good design. |

---

## 8. Look & feel

**Metaphor:** a well-made card index on a quiet desk. Not a game, not an admin panel.

* **Layout:** one thing at a time. In training, the photo *is* the screen: centred, big,
  calm, with the answers in a single row beneath. No sidebar, no clock, no badges competing
  for attention.
* **Colour:** near-black background (default dark) or warm off-white, one accent for
  interactive elements. Feedback is a brief tint — soft green for correct, soft amber for
  "revealed/hint", soft red for wrong — never a full-screen flash, never confetti. A wrong
  answer should feel informational, not punishing.
* **Type:** one humanist sans, large sizes for names (a name being revealed is *the*
  moment — it should be typographically the biggest thing on screen), generous line height.
* **Motion:** 120–180 ms fades and slides only; the photo cross-fades when the next prompt
  appears. No bounce, no parallax. Respect `prefers-reduced-motion`.
* **Sound:** off by default; optional soft click on answer and a distinct tone for
  "wrong — here is the name". Nothing more.
* **Tone of voice:** low-key and dry. "Not quite — this is **Anna Brunner**." Not
  "Whoops! 🙈". No exclamation marks in feedback.
* **Responsive:** phone (one column, thumb-reachable answers), tablet (ideal — the app's
  best form factor for photo-first training), desktop with full keyboard control.
* **Empty states** do work: an empty cabinet shows the add form directly; an empty session
  says "nothing due in *Family* — train on everything instead?" with the button right there.

---

## 9. What happens on day one (three concrete scenarios)

1. **You are invited to a birthday on Saturday (40 guests, you know 12).** You create the
   group *Birthday* from the invitation, add the 28 faces from the shared photo album in
   bulk (folder import, ~6 minutes), then run **Study cards** once. Saturday morning you
   print the **cheat sheet** and skim it. At the party the app on your phone shows
   *Birthday · 4 min · adaptive*.
2. **New job, 30 colleagues, first two weeks.** You add each person the day you meet them
   (quick-add, 20 seconds). The default preset trains adaptive sessions of 10 minutes every
   other day. After ten days the dashboard says "you can name 27 of 30" and lists the three
   holdouts with their mnemonics.
3. **You keep calling two neighbours by each other's names.** You open *Confusion pairs*,
   see *Beat ↔ Beat (Weber ↔ Widmer): 5 mix-ups*, and drill that pair with mode 3 until it
   is boring.

---

## 10. Non-goals for v1 — explicitly out of scope

* Automatic face recognition / "AI tagging" from your photo library. (Optional stretch much
  later; it would drag in big dependencies and privacy questions.)
* Multiple users, sharing, cloud sync, mobile native apps.
* Any handwriting or forced typing.
* Social features, leaderboards, achievements.
* Pulling photos from any online service.

---

## 11. First-run: what has to be true for this to be pleasant

* Empty database → the add form, immediately, no wizard, no tutorial.
* First session works with **zero configuration**: one person added → "Train" → multiple
  choice on that one person is silly, so with fewer than 4 people the app uses free recall
  instead of multiple choice. (Small detail, but getting it wrong makes the first minute
  feel broken.)
* Total time from `podman build` + `systemctl --user start kith` to a first answered question: under two minutes.
* **Container-native:** the entire app is one container image plus one data directory. No
  Python, no package manager, no build tools and no `uv` on the machine that runs it — see
  the companion architecture document.

---

## 12. Decisions (settled 2026-10-01)

Every question below was decided on 2026-10-01: all of my defaults were accepted, with two
changes — the **UI language is English only**, and the container runs on **Podman/Quadlet**
(see the architecture document). Kept here so the reasoning stays visible; changing any of
them later is a small, contained edit.

| # | Question | Decision |
|---|---|---|
| Q1 | Name of the app | **Kith** |
| Q2 | UI language | **English only**, from one string table (German droppable later) |
| Q3 | Where the **container** is published: loopback only on one machine, or on the **Barn host behind Caddy** (`kith.barn.opq.ch`, its own password gate) so your phone can reach it? (It is a container either way.) | **Barn host + Caddy, app's own login** |
| Q4 | Face detection for auto-cropping photos | **Manual crop only** — no extra dependency in the image |
| Q5 | Typing mode | **No typing mode**; recall by speaking aloud + self-rate |
| Q6 | Default session length / new-face budget | **10 min / 3 new faces** |
| Q7 | Desired retention target | **90 %**, adjustable in settings |
| Q8 | Multiple photos per person in v1 | **Yes** — cheap and high value |

---

## 13. Appendix — the science, in plain language

Short version of the evidence behind §4, so you can check my reasoning.

* **Testing effect / retrieval practice.** Being tested on material produces better
  retention than re-studying it. Rated one of the two highest-utility study techniques in
  the widely cited Dunlosky et al. review; the basis for treating this app as a question
  machine. <https://pdf.retrievalpractice.org/spacing/InterleavedRetrievalPracticePromotesScienceLearning_SanaYan_2022.pdf>
* **Spacing effect.** Spaced retrieval practice beats massed practice with a substantial
  pooled effect (*g* ≈ 0.74 across 29 studies in Latimier, Peyre & Ramus 2021). The same
  meta-analysis found **expanding** intervals are *not* reliably better than uniform ones
  (*g* = 0.034) unless an item is tested many times — so I do **not** hard-code an expanding
  schedule; I use a proper scheduler (FSRS) and keep expansion only for the within-session
  correction loop, where it does help. <https://eric.ed.gov?id=EJ1310148>
* **Successive relearning** (Rawson & Dunlosky). Retrieval practice + spacing: recall each
  item correctly, then do it again in later spaced sessions. Three spaced sessions produced
  large gains (up to a letter grade in a real course; relearning potency *d* = 1.52–4.19
  vs single-session learning). Basis for "recall to a criterion, then leave it alone".
  <https://www.retrievalpractice.org/strategies/2018/successive-relearning>
* **Optimal gap** ≈ 10–20 % of how long you want to remember it (Cepeda et al.); for
  knowledge you want for years, intervals should reach weeks/months in the tail. Basis for
  the long intervals FSRS will produce. <https://pdf.retrievalpractice.org/spacing/Carpenter_etal_2012_EDPR.pdf>
* **Interleaving.** Mixing related items during practice beats blocking them, because it
  trains discrimination — precisely the face–name failure mode. Effect demonstrated on top
  of retrieval practice (Sana & Yan 2022, *d* ≈ 0.35 over blocked quizzes).
* **Recognition vs recall.** Recall involves fewer cues and is harder than recognition;
  recognition is the easier, more forgiving step. Basis for the difficulty ladder.
  <https://www.nngroup.com/articles/recognition-and-recall>
* **Production effect does not help face–name pairs.** Making the response producible
  (writing/saying) improves memory for words but *not* for face–name associations — the
  effect even reverses for such stimuli (Sondergaard et al. 2022,
  *Acta Psychologica*). This is the direct justification for the point-and-click design and
  for making writing optional rather than central.
  <https://www.sciencedirect.com/science/article/pii/S0001691822000464>
* **Spaced retrieval (clinical protocol).** Developed in the late 1970s *on face–name
  learning in healthy students*, then widely used in memory rehabilitation: ask → correct
  immediately on error → re-ask after progressively longer intervals, backing off to the
  last successful interval after a failure. Basis for the in-session re-queue and hint
  ladder. <https://tactustherapy.com/spaced-retrieval-training-memory>
* **Cueing hierarchy** (from aphasia therapy): fade cues instead of letting the learner
  fail repeatedly — first letter, then more. Basis for the hint ladder.
  <https://tactustherapy.com/cueing-hierarchy-word-finding-aphasia/>
* **Encoding matters more than drilling.** The classic face–name association technique
  (distinctive feature + name-mnemonic image + attention) is the standard mnemonic
  intervention for names, and simply *attending* to a distinctive feature already improves
  later recognition. Basis for the mnemonic and feature-marker fields.
  <https://artofmemory.com/wiki/Memorizing_Names_and_Faces>
* **Classroom practice with real names.** A teacher's documented protocol over ~6 days —
  diminishing cues (name tags gradually turned face-down), interleaved retrieval of the
  whole class list, deliberate acceptance of early failure, spacing across days — is
  essentially this app's session design, done by hand.
  <https://theeffortfuleducator.com/2024/01/11/learning-names-and-modeling-spaced-retrieval-practice>
