# Blog style sheet — distilled from "I Built a Kubernetes Supply Chain Security Demo"

Reference for all three parts of the chaos-promotion series. The goal: sounds like Amogh,
not like a model. When in doubt, reread the supply-chain article.

## The section skeleton (adapt per post, don't force every slot)

1. **Personal origin opening** — no throat-clearing. Start from a real moment
   ("I was recently catching up on some of the tooling I worked with during my Co-op at
   Radius…"), end the opening with a rabbit-hole line ("I wanted to prove (to myself)
   that…").
2. **The Problem** — plain-language framing of the gap, ending in a **bolded question**
   the whole post answers.
3. **Why I Cared About This** — honest tie to the Radius co-op: "I'd used X at work,
   but never for Y. It was the missing piece."
4. **What I Built** — subsections; short numbered/bolded lists; key terms bolded
   mid-sentence; emphatic short-sentence pairs ("This is important. Tags are mutable.
   Digests are immutable.").
5. **What Happens in Practice** — terminal transcript or screenshot demo with
   `# BLOCKED:` / `# PASSES:`-style inline annotations.
6. **One concept deep-dive** — the "keyless signing" slot: take the one idea that sounds
   like marketing and show the actual mechanics.
7. **What I Learned Along the Way** — gotchas as bold-led paragraphs, symptom-first
   ("The first time I set it up, I thought I'd broken something because the UI stayed
   empty…"). Honest confusion is a feature.
8. **A mapping table** — "What's demonstrated | What it proves" style.
9. **What I'd Add Next** + **Try It Yourself** — deferred work, repo link, one practical
   closing recommendation to the reader.

## Voice rules

- First person, past tense for the build, present tense for how the system behaves.
- Parenthetical asides are in-voice: "(to myself)", "(Coming soon!)".
- Hedge honestly and keep the hedge: "I'm not claiming SLSA compliance here."
- One line of *why this matters* before every code block — never drop code cold.
- Bold used for emphasis inside sentences, not as decoration on headers.
- Specific numbers and durations over adjectives: "3.5 months latent", "~$14/mo",
  "p95 went from ~1.8s to ~700ms".

## Banned (anti-slop)

- **The antithesis flip.** The single worst tell. Any "not X, it's Y" / "X isn't the
  point, Y is" / "X is the excuse, Y is the point" / "stop being X, start being Y" /
  "from a word I used into a thing that holds" construction. It sounds profound and says
  nothing. Just state the thing plainly: "I kept the app simple so the decisions would sit
  in the platform" — not "the app is the excuse, the platform is the point."
- **Slogan fragments.** "One object, one controller." / "Five lines." / "The split
  matters." Punchy two-word sentences dropped for emphasis are an AI tic. Fold the point
  into a normal sentence.
- "In this article we will…", "Let's dive in", "In conclusion", "seamless", "robust",
  "leverage", "delve", "game-changer".
- Solution-first war stories. Always symptom → confusion → root cause → fix.
- Triads-with-em-dashes rhythm and other AI cadence tells.
- **Rhythm repetition across the whole doc.** Even when no single sentence is bad, reusing
  the same construction 3–4 times reads as AI. Watch especially the trailing "…, which is
  exactly / which does nothing / which is worse" clause and the "Individually X. Together Y."
  parallel. After a draft, grep for a construction you like — if it appears more than twice,
  vary the others (split into two sentences, restructure, cut).
- **Defensive self-assessment.** "This isn't just AI slop / a resume filler, I really worked
  on it" — asserting the work is legit undercuts it. Show the work (months, migrations, the
  bug you fixed, the commit history) and let the reader conclude it.
- **Personifying the system.** "the platform won't quietly feed the gate the wrong answer" —
  say the concrete mechanism instead ("the gate tested the config that's actually running").
- Claims without a number, screenshot, or snippet behind them.
- Code blocks longer than ~20 lines — trim to the interesting part, link the repo for the rest.

## Ways to kill the antithesis flip (when you catch yourself writing "not X but Y")

1. **Keep only the Y.** Delete the setup half. "It's not decoration, it's the gate" →
   "the gate reads these dashboards."
2. **Make it a fact with a number or an action**, not a contrast. "The platform is the
   point" → "I kept the app to ~200 lines so the chart and pipeline were the hard part."
3. **Say what happened instead of what it means.** Replace the abstract flip with the
   concrete event: "GitOps finally held" → "the next config change actually stopped in
   staging instead of hitting prod."
4. **If both halves are true, use one plain sentence with 'because' or 'so'** — cause,
   not contrast.

## Blog, not README (keep it narrative)

A README answers "what is this and how do I run it." A blog answers "here's a thing I
figured out, come along." Every part must read as the second.

Tells that it's drifting into README:
- Long bulleted inventories of components/specs with no story around them.
- Config/YAML dumped to explain *what the field is*, rather than to show a decision or a
  bug. Only show code that earns a "here's why this line exists" sentence.
- Step-by-step "first do X, then Y, then Z" setup sequences. Link the repo for those.
- Exhaustive coverage — every flag, every value, every namespace. Pick the 2–3 things that
  were interesting or non-obvious; the repo has the rest.
- Sections that stand alone like reference entries instead of flowing into the next.

Keep it a blog:
- Prose carries the piece; code and bullets are guests, not the host. Rough ratio: a reader
  should be able to follow the whole story without reading a single code block.
- Every code block gets one plain-language "why this matters" line before it, and ideally a
  "so what happened" after.
- One short orientation list is fine (the "cast of characters"); a page of them is not.
- Momentum over completeness — end each section pointing at the next thing, not trailing off.

## Per-post deep-dive slot assignments

- Part 1: the rendered-branches pattern (why "GitOps" wasn't actually gating config).
- Part 2: the x402/Permit2 payment flow mechanics (what "keyless"-sounding words like
  "facilitator" actually do).
- Part 3: the scorer + vacuous-pass guard ("who gates the gate?").
