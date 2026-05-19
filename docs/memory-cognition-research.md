# Memory & Cognition — Research Synthesis

> **Status:** research pass, May 2026. No code, no cutover. This document is the
> deliverable of a "survey first" phase ([research-first SME workflow]); it
> feeds a later design decision. It does not propose an implementation.
>
> **Method:** nine parallel research subagents, ~150 sources, covering the
> neuroscience of memory, cognitive psychology, spreading-activation models,
> cognitive architectures, theories of consciousness, SOTA LLM-agent memory
> systems, long-context/compaction engineering, frontier neural-memory
> architectures, and graph/temporal knowledge memory.

## 1. Purpose and scope

The question: rethink **memory, history, context, and compaction** — especially
for long conversations — grounded in the actual science of human memory and
cognition, rather than ad hoc engineering.

The bottom line, stated up front so a skim catches it:

- The science is real, deep, and converges hard. Across neuroscience, cognitive
  psychology, cognitive architectures, and AI agent research, the *same* small
  set of principles keeps reappearing (§6). They are a genuine gold standard.
- **Most of the neuroscience-grounded SOTA is machinery** — spreading-activation
  graphs, activation/decay scoring, bi-temporal validity, consolidation
  schedulers, episodic projection tables. Ariel's memory subsystem was
  *crystallized two days ago* (`memory-cutover.md`, migration `0042`) by
  deliberately deleting a 9,400-line engine containing almost exactly that
  machinery, on the principle that **code owns no judgment** (`ai-first.md`).
- So the honest finding is not "build HippoRAG." It is: the neuroscience splits
  cleanly into ideas that are **prompt/architecture changes** (compatible with
  the crystallization — "a real product need is rewritten as a subagent prompt
  change") and ideas that are **machinery** (reopening the crystallization
  doctrine). §7–§9 separate them and lay out the decision.

## 2. How a subject-matter expert frames the problem

An expert's first move is to **refuse the monolith.** "Memory" is not one thing
and "compaction" is not one thing. The problem decomposes into five distinct
sub-problems, each with its own literature, failure modes, and design levers:

1. **Working memory / context** — what is active *right now*. In an LLM agent
   this is the context window. Scarce, capacity-limited, the substrate of all
   reasoning.
2. **Encoding** — deciding what, from the stream of experience, is worth keeping
   at all, and in what form.
3. **Consolidation** — transforming raw recent experience into durable,
   integrated, generalized knowledge over time.
4. **Retrieval** — getting the right past knowledge back into working memory
   when it is relevant, from a partial cue.
5. **Forgetting** — actively shedding what is no longer useful so the system
   does not drown.

The second move is a reframe that organizes everything below: **an LLM agent's
context window is not its memory — it is its *consciousness*.** It is the
global workspace (§3.8): the small, limited stage where reasoning happens.
Everything else — the fact store, the profile, the digest, past turns — is the
unconscious long-term system. "Compaction" is therefore not a storage problem;
it is the *attention* problem: deciding what gets to be conscious.

The questions an SME insists on answering before designing anything:

- Which of the five sub-problems is actually failing or about to fail? (For
  Ariel: §7 argues it is #1 and #3, driven by the agent-loop cutover.)
- What is the *unit* of memory — a turn, a fact, an event, an episode?
- Is there one store or several? Born how — episodic and abstracted later, or
  semantic immediately?
- Is recall a flat lookup or an associative traversal?
- Is forgetting designed-in or an accident?
- Does using a memory change it?
- What is offline (between turns/sessions) versus online (mid-turn)?

## 3. The science: the gold-standard picture

Nine domains were surveyed; their findings collapse into nine big ideas. Each is
stated with the mechanism that matters and the canonical source. This is the
"meta" — what is settled and load-bearing.

### 3.1 Two systems, not one

The most robust, most cross-validated idea in the whole field. Human memory is
not a single store; it is at least two, with *opposite* computational
properties:

- **Short-term / working memory** — small (~4 chunks, Cowan 2001; the famous
  "7±2" of Miller 1956 counts chunks *with* chunking), fast, volatile, the
  active workspace. Baddeley & Hitch's model adds the **episodic buffer**: the
  binding interface where long-term memory and current perception fuse into one
  coherent scene.
- **Long-term memory** — vast, slow to write, durable.

And critically, *within* long-term memory the same fast/slow split recurs.
**Complementary Learning Systems** theory (McClelland, McNaughton & O'Reilly
1995; updated Kumaran, Hassabis & McClelland 2016) is the keystone result: the
brain uses a **fast hippocampus** (one-shot, sparse, pattern-separated — encodes
a specific experience instantly) and a **slow neocortex** (many exposures,
distributed, generalizing — extracts statistical structure). The reason there
must be two is **catastrophic interference**: a single network that learns fast
overwrites what it knew. The slow system avoids that; the fast system buys
one-shot capture; replay (§3.3) bridges them. This theory directly inspired
experience replay in deep reinforcement learning.

**Design weight:** if you take one thing from the science, it is this. Do not
use one undifferentiated store for both fast capture and slow durable knowledge.

### 3.2 The long-term taxonomy and the episodic→semantic gradient

Long-term memory subdivides (Tulving 1972; Squire 2004):

- **Episodic** — specific events, bound to a time and place, re-experienced
  ("the meeting Tuesday where we chose Postgres").
- **Semantic** — decontextualized facts and concepts ("the project uses
  Postgres"), detached from when they were learned.
- **Procedural** — skills and routines, expressed by doing, not telling.

The crucial dynamic, strongly reaffirmed by 2024 work, is that episodic and
semantic are not separate boxes but the ends of a **continuum**, joined by
**semanticization**: repeated, consistent episodes are gradually abstracted into
context-free semantic knowledge during consolidation, while the original
episodes fade in accessibility. A memory is *born episodic* and *becomes
semantic*. This gradient is the natural model for compaction.

### 3.3 Consolidation: memory is written twice

A memory is not stored once. It is captured fast, then **re-processed offline**
into durable form. Two layers:

- **Synaptic consolidation** (hours) — LTP/LTD, Hebbian plasticity ("cells that
  fire together wire together"), protein synthesis. Notably, **synaptic
  tagging-and-capture** (Frey & Morris 1997): a weak memory can be rescued into
  permanence if a *salient* event occurs nearby in time — salience retroactively
  strengthens neighbors.
- **Systems consolidation** (days–years) — the **hippocampal–neocortical
  dialogue.** During sleep and quiet rest, the hippocampus *replays* recent
  experience to the neocortex (sharp-wave ripples, 10–20× time-compressed),
  interleaved with old memories, letting the neocortex integrate the new without
  catastrophic interference.

Replay is **prioritized**, not uniform: novelty, reward, and prediction error
raise an experience's replay priority (Kumaran et al. 2016). The brain does its
real memory work *offline*, *selectively*, and the product is *transformed* (the
episode becomes a schema), not copied.

**Design weight:** consolidation ≠ summarization. It is selective,
salience-weighted, transformative, and best done between episodes, not during
them.

### 3.4 Recall is spreading activation over a network

This is the user's stated intuition — "connections increasing activation, nodes"
— and it is correct and precisely formalized.

Memory is a **network of concept nodes joined by weighted associative links**
(semantic-network theory: Collins & Quillian 1969; spreading-activation theory:
Collins & Loftus 1975). Recall works by **spreading activation**: the current
context activates some nodes; activation propagates along links, decaying with
distance; nodes whose activation crosses a threshold are retrieved. This is why
a partial cue retrieves a whole memory (pattern completion), and why related
ideas prime each other.

**ACT-R** (Anderson) gives the gold-standard math. A chunk *i*'s activation:

```
A_i  =  B_i  +  Σ_j (W_j · S_ji)  +  partial-match  +  noise
```

- **Base-level activation** `B_i = ln(Σ_k t_k^−d)` — sums over every past use,
  each decaying as a **power law** of elapsed time `t_k` (decay `d ≈ 0.5`). This
  single term captures both **frequency** (more uses → higher) and **recency**
  (recent uses → higher).
- **Spreading activation** `Σ_j W_j·S_ji` — context elements *j* inject
  activation weighted by associative strength `S_ji`, with a **fan effect**: a
  concept linked to many things spreads its activation thinly (`S_ji = S −
  ln(fan_j)`). This is an attention budget.
- Activation maps to a **retrieval probability** `P = 1/(1+e^−((A_i−τ)/s))` and
  a latency.

Anderson & Schooler (1991) proved the deep point: this decay function is not a
flaw — it is **Bayesian-optimal**. Memory's accessibility tracks the statistical
probability that an item will be **needed** again, and real environments
(headlines, email, speech) have power-law re-use statistics. The brain's
forgetting *is* a rational prediction.

There is also a striking bridge to LLMs: **modern Hopfield networks** (Ramsauer
et al. 2020) proved that transformer **softmax attention is mathematically a
one-step associative-memory retrieval.** An LLM, attending over its context, is
*already* running a content-addressable memory. The context window is an
associative memory; the open question is only what is *in* it.

### 3.5 Forgetting is adaptive; decay is rational

Forgetting is not failure. It is interference management — depressing access to
rarely-relevant items so relevant ones win (Bjork & Bjork's **New Theory of
Disuse**). Their key construct: every memory has two independent strengths —
**storage strength** (how well learned; only ever increases) and **retrieval
strength** (how accessible right now; fluctuates, decays). "Forgotten" almost
always means low retrieval strength, not erased storage — confirmed at the
cellular level by **silent engrams** (Tonegawa lab): optogenetically
reactivating "forgotten" cells recovers the memory. Forgetting is largely
**retrieval failure and engram competition**, reversible, not deletion.

The applied form is spaced repetition: the Ebbinghaus curve `R = e^(−t/S)`
(strength `S` rises with each recall), and its modern successor **FSRS**, which
models each item by **D**ifficulty, **S**tability, and **R**etrievability and
schedules review when retrievability decays to a target. **Desirable
difficulties** (Bjork): retrieval that is *effortful* (after some forgetting)
strengthens memory far more than easy restudy.

**Design weight:** decay and forgetting should be deliberate. `forgotten` should
mean "deprioritized," recoverable — which is exactly how Ariel's
`status=forgotten` soft-delete already works.

### 3.6 Retrieval reconstructs and rewrites (reconsolidation)

Recall is not playback. It is **reconstruction** — the memory is rebuilt from
fragments plus schema-driven inference (Bartlett 1932; Loftus on misinformation;
the brain *reinstates* cortical patterns). This is why memory is systematically
distortable.

And retrieval is not read-only: **reconsolidation** (Nader, Schafe & LeDoux
2000) — recalling a memory makes it labile for hours, during which it can be
updated, before re-stabilizing. The trigger is **prediction error**: a memory is
re-opened for editing precisely when reality mismatches it.

**Design weight:** using a stored fact is the natural moment to *correct* it. A
recall→update loop, gated by contradiction, is biologically principled.

### 3.7 Encoding is gated by salience, surprise, and schema

Not everything is stored, and what is stored is not chosen by recency. What
sticks is governed by:

- **Depth of processing** (Craik & Lockhart 1972) — meaningfully, elaboratively
  processed material outlasts shallowly processed material.
- **Salience / emotion / consequence** — arousing or goal-relevant events get
  preferential consolidation (amygdala modulation; the tagging effect of §3.3).
- **Surprise / prediction error** — schema-*violating* information recruits the
  hippocampus for dedicated encoding; schema-*consistent* information is
  absorbed quickly into existing structure (van Kesteren et al.). Prediction
  error is the universal "this is worth encoding" signal — and, strikingly, the
  frontier AI architecture Titans (§5) uses exactly this.

### 3.8 The workspace: attention, consciousness, and the context window

**Global Workspace Theory** (Baars 1988; Dehaene's Global Neuronal Workspace) is
the single most useful theory for the *context* half of the problem. The mind
runs many parallel unconscious specialists; consciousness is a **limited-capacity
workspace** onto which one coalition of content is selected and then
**broadcast** to all specialists. Selection is a competition biased by **bottom-up
salience** and **top-down goal relevance** (Desimone & Duncan's biased
competition). **Predictive processing** (Friston, Clark) adds the formal cousin
of attention: **precision-weighting** — prediction errors are amplified or
suppressed by their estimated reliability.

The mapping to an LLM agent is structural, not poetic:

- The **context window IS the global workspace.** What is in it is "conscious";
  what is not is unconscious (in weights or external stores).
- **Compaction IS the selection problem.** Deciding what survives compaction is
  the same operation the brain performs to decide what is conscious: a
  multi-factor competition — salience + goal-relevance + recency + dependency.
- The expert-uncertainty caveat: the leading consciousness theories (GWT, IIT)
  were both *challenged* by the 2023 COGITATE adversarial study. Use GWT as a
  design metaphor, not settled mechanism.

### 3.9 Capacity, chunking, and two-speed cognition

Two more durable findings. **Chunking**: working-memory capacity is ~4 *chunks*,
but a chunk can be arbitrarily rich — expertise is largely better chunking.
Retrieved memory should be presented to the agent as a few named, coherent
chunks, not a flat list of forty facts. **Two-speed cognition** (Kahneman's
System 1/2): fast associative pattern-completion vs. slow deliberate reasoning.
For an agent: most recall can be fast and cheap; expensive deliberate retrieval
should be reserved for high-uncertainty, high-stakes, or novel moments.

## 4. The state of the art in AI agent memory

### 4.1 The convergent taxonomy

The AI field independently arrived at the §3 picture. The now-standard
agent-memory taxonomy (CoALA, Sumers et al. 2023) is: **working** (context
window) vs. long-term, and long-term splits into **episodic / semantic /
procedural** — the Tulving taxonomy, rediscovered. The governing pattern is a
**write → manage → read** loop, where "manage" (consolidation, conflict
resolution, forgetting) is a first-class stage, not an afterthought.

### 4.2 The canonical systems

| System | Core idea | What it contributes |
|---|---|---|
| **MemGPT / Letta** (2023) | Memory as OS virtual memory: core context vs. external store, self-edited via tool calls, paging under "memory pressure" | The OS metaphor; **sleep-time agents** — a background agent consolidates memory off the response path |
| **Generative Agents** (Stanford 2023) | A "memory stream"; retrieval score = **recency** (`0.995^hours`) + **importance** (LLM 1–10) + **relevance** (cosine); periodic **reflection** synthesizes higher-level memories | The most-copied retrieval formula; reflection = consolidation |
| **MemoryBank** (2023) | Ebbinghaus forgetting curve as the memory-strength update | First principled forgetting in an agent |
| **A-MEM** (2025) | Zettelkasten: atomic notes that **link** to each other and evolve as new notes arrive | Self-organizing associative network |
| **Mem0** (2025) | LLM-driven **ADD / UPDATE / DELETE / NOOP** pipeline; graph variant | CRUD + conflict resolution beats append-only |
| **Zep / Graphiti** (2025) | **Bi-temporal knowledge graph**: every fact carries validity intervals (event time vs. ingestion time); contradiction *invalidates*, never deletes | The reference design for facts that change over time |
| **HippoRAG** (NeurIPS 2024) | Explicit hippocampal-indexing model: LLM = neocortex (extracts a KG), encoder = parahippocampal region, **Personalized PageRank over the KG = the hippocampal index** doing pattern completion | The clearest neuroscience→retrieval bridge; multi-hop recall |
| **SYNAPSE / GAM / TiMem** (2025–26) | Spreading activation with fan effect + lateral inhibition; episodic/semantic layer separation; temporal hierarchy (turn→session→day→profile) | The §3 science implemented directly; reported SOTA on long-conversation benchmarks |

### 4.3 Long context and compaction

The context half of the problem has its own hard empirical findings:

- **Lost in the middle** (Liu et al. 2023) — accuracy is U-shaped in position;
  information in the middle of a long context is under-used.
- **Context rot** — every model degrades as input grows, well before the token
  limit; the practical usable fraction is well below the nominal window.
- **Attention sinks** (StreamingLLM) — the first few tokens act as structural
  anchors regardless of content; do not disturb the prefix.
- **Context engineering** (Anthropic's own guidance) — context is a finite,
  curated, attention-budgeted resource; the goal is "the smallest set of
  high-signal tokens." Strategies: **compaction**, **note-taking to external
  memory**, **sub-agent context isolation**.
- **Compaction technique** — recursive/hierarchical summarization beats flat
  one-shot summarization; *what to preserve* is the real question (decisions and
  their rationale, constraints, open threads, exact identifiers, errors, user
  corrections) vs. what to drop (resolved exploration, pleasantries,
  re-fetchable tool output).
- **KV-cache constraint** — mutating early context invalidates the prompt cache;
  compaction must keep the stable prefix stable.
- The deepest point, echoing §3.3: **compaction should be consolidation, not
  truncation** — selective, structured, salience-weighted; ideally run
  asynchronously and ahead of need, not as an emergency at the token wall.

### 4.4 The empirical bottom line

Three sobering facts temper any redesign:

1. **Long-context LLMs beat flat fact-extraction memory on accuracy.** A 2026
   cost-performance study put GPT-5-mini with full context at ~93% on LoCoMo vs.
   a flat extraction system at ~58%. Structured memory wins on *cost* (cheaper
   after ~10 turns) and on *scale*, not raw accuracy. Keeping more verbatim
   recent context is genuinely competitive.
2. **Temporal and multi-hop reasoning are the hardest unsolved problems.**
   "What did the user prefer before they changed their mind?" defeats most
   systems. This is what bi-temporal graphs and spreading activation exist to
   fix.
3. **The benchmarks are contested.** A public dispute over LoCoMo scores
   (alleged contamination) means "SOTA" numbers — many from 2026 arXiv preprints
   that are not yet peer-reviewed — should be read as directional, not gospel.

## 5. The frontier

Where the field is heading, for the "futuristic" part of the question:

- **Memory as weights updated at inference.** **Titans** (Google, 2024–25) adds
  a neural memory module whose weights are updated *at test time* by gradient
  descent — and the update is **gated by surprise**: the gradient magnitude *is*
  the prediction-error signal of §3.7. **Test-time training**, **Memory Layers**,
  and the **Nested Learning** framework (NeurIPS 2025) generalize this: memory,
  architecture, and optimization as one nested system at many timescales.
- **The retrieval-vs-weights dialectic.** The field oscillates between memory as
  *retrieved tokens* (RAG, MemGPT, HippoRAG) and memory as *updated weights*
  (Titans, TTT). The frontier is hybrids.
- **Surprise/novelty as the universal write-gate** — independently arrived at by
  Titans (gradient norm) and by agent-memory work (write-time salience scoring).
- **Self-organizing memory graphs** — graphs that maintain, link, and
  consolidate themselves (A-MEM, KARMA).

**Caveat for Ariel:** the weight-level frontier (Titans, TTT) needs model-level
access. Ariel uses a hosted LLM. These are *inspiration* — especially the
surprise-gated write — not directly implementable. The system-level analog of
surprise-gating is fully implementable.

## 6. Synthesis: one model, seven principles

Every domain — biological, cognitive, classical-AI, modern-AI — converges on one
architecture:

```
        ┌─────────────────────────────────────────────┐
        │  WORKING MEMORY  (the context window)        │
        │  = global workspace; what is "conscious"     │
        │  capacity-limited; admission is competitive  │
        └───────▲───────────────────────────┬─────────┘
   retrieval    │  (spreading activation)    │  encoding (salience-gated)
   = pattern    │                            ▼
   completion   │              ┌──────────────────────────┐
        ┌───────┴────────┐     │  EPISODIC store          │
        │  SEMANTIC store │◀────│  (recent, specific,      │
        │  (durable,      │     │   timestamped events)    │
        │   generalized,  │     └──────────────────────────┘
        │   networked)    │   consolidation (offline, selective,
        └─────────────────┘    salience-weighted, transformative)
```

The **seven principles** that survive all nine domains — the design invariants:

1. **Memory is plural — separate the stores.** Working/context ≠ episodic ≠
   semantic ≠ procedural. Above all, separate *fast specific capture* from *slow
   generalized knowledge*. One store for both is the original sin (catastrophic
   interference).
2. **Write twice.** Cheap fast capture, then slow **consolidation** that
   *transforms* episodic detail into semantic abstraction — done **offline**,
   between episodes.
3. **Recall is associative and graded.** Spreading activation over a network of
   linked nodes, weighted by similarity *and* recency *and* frequency *and*
   salience — not a flat top-k similarity lookup. Partial cues complete whole
   patterns.
4. **Forgetting is a feature.** Decay and interference are adaptive; tune decay
   to re-use statistics (power law, not exponential); "forgotten" means
   deprioritized and recoverable, not deleted.
5. **Retrieval is reconstructive and rewrites.** Using a memory is the moment to
   correct it; build a recall→reconsolidation loop, triggered by contradiction.
6. **Gate encoding by surprise.** Store the novel, the consequential, the
   prediction-violating. Salience, not volume.
7. **The context window is a global workspace.** Admission to it is the scarce
   resource. Compaction is salience-gated *selection + consolidation*, not
   truncation; do it early, structured, and ideally offline.

## 7. Ariel today, measured against the model

Ariel's current memory subsystem (`docs/modules/memory.md`, `memory.py`,
953 lines) is the product of a deliberate crystallization two days ago. It is:

- `memory_facts` — a **flat fact store**: rich plain-language statements, no
  kind/type/category, `status` of `active`/`forgotten`, `embedding`,
  `search_vector`, `last_recalled_at`.
- `memory_profile` — one **always-loaded document**: who the user is, how they
  work, durable preferences, privacy guardrails.
- `sessions.digest` — one **per-session document**: the working state of the
  current conversation.
- The **retriever** subagent — runs pre-turn, picks the relevant subset from an
  unranked candidate union (vector + keyword + recency).
- The **rememberer** subagent — runs as a background task after every turn, on
  rotation, and on a sweep; emits `write`/`edit`/`forget` plus optional profile
  and digest rewrites.

**What Ariel already gets right** — and it is a lot; the crystallization was
well-judged:

- The **two-subagent split** (retriever / rememberer) is validated SOTA. Letta's
  sleep-time agents, MIRIX's memory managers, and Mem0's pipeline all use
  dedicated memory-management LLM logic. The user's "should it (...maybe)" — yes,
  keep it. The question is what the subagents *do*, not whether they exist.
- The **rememberer is already asynchronous** (a post-turn background task). That
  is the "sleep-time consolidation" pattern (§3.3, §4.2) — Ariel has it.
- `write`/`edit`/`forget` is the **CRUD pattern** Mem0 found beats append-only.
- `status=forgotten` as a reversible soft-delete is **principle 4** done right —
  forgetting as deprioritization, not erasure (§3.5).
- The **profile** is a consolidated **semantic/schema** document; the **digest**
  is a **working-memory** document. Two of the §6 stores exist.
- `last_recalled_at` is already tracked on every fact.

**What the model says is missing or thin:**

- **No episodic layer; facts are born semantic.** There is no episodic→semantic
  gradient (§3.2). Raw turns *are* stored (`turns` table, `source_turn_id`), but
  they are not a *retrievable episodic memory* — recall reaches only distilled
  facts, the current session's verbatim recent turns, and the digest. The
  rememberer jumps straight from a raw turn to a durable semantic fact, with no
  notion of a recent-but-not-yet-durable episode.
- **Compaction was deleted, and a long-conversation pressure is arriving.** The
  cutover removed all summarization/compaction; the running context is "verbatim
  recent turns + digest," declared "inherently bounded." But the **agent-loop
  cutover** (`docs/agent-loop-cutover.md`) is about to make turns **long and
  adaptive** — and its own P2 flags that `emit_value` accumulates untrimmed
  within a turn. *Within-turn* context growth is a real, unsolved problem that
  "recent turns + digest" does not address. The user named "compaction" and
  "long conversations" precisely because this is the live gap.
- **Retrieval has no activation, recency, or association signal.** The candidate
  gather is an unranked union; the cutover deleted decay, RRF, and the graph.
  The retriever is a capable AI judge, but it works from flat vector/keyword
  similarity — which §3.4 and §4.4 both identify as the thing that misses
  **multi-hop / associative** recall. `last_recalled_at` is stored but unused.
- **No salience/surprise gate at encoding** (§3.7). The rememberer decides
  "durable knowledge worth keeping" by prompt alone — no novelty or
  prediction-error signal.
- **No spreading activation — "connections, nodes."** The graph was deleted.
  This is *exactly* the user's stated intuition, and it is the one major idea
  with no presence in the current design.
- **No explicit reconsolidation loop** (§3.6) — recall and correction are not
  tied; the rememberer can `edit`, but nothing says "a fact you just used and
  saw contradicted should be re-opened."

## 8. The central tension

Here is the finding that matters most, and the reason this is a research
document and not a cutover plan.

Ariel **crystallized** its memory subsystem two days ago. `memory-cutover.md`
deleted a 31-table, ~9,400-line "SOTA memory engine" and, with it,
**deliberately and by name**: the predicate registry, the conflict-set
lifecycle, RRF retrieval fusion and its seven signals, **bi-temporal validity**,
the **entity/relationship graph**, projection tables, topics, the
sensitivity/retention machinery, **all summarization and compaction code**, and
the memory eval suite. `ai-first.md` and `memory.md` then made it a standing
rule: *"New memory machinery — registries, scorers, projection tables, lifecycle
states, category fields, summarizers — is forbidden. A real product need is
rewritten as a subagent prompt change, not as code."*

**Most of the neuroscience-grounded SOTA in §3–§5 is exactly that machinery.**
Spreading-activation graphs, ACT-R activation/decay scores, bi-temporal
knowledge graphs, consolidation schedulers, episodic projection tables — a naive
"implement the science" rebuilds, almost line for line, the engine the
crystallization just removed.

This is not a contradiction to resolve by picking a side. The crystallization
was not anti-science; it was **anti-machinery and pro-AI-judgment**. The
reconciliation is to sort every neuroscience idea into two buckets:

**Bucket 1 — prompt / architecture changes (compatible with the
crystallization).** These need no new tables, scorers, or graphs; they sharpen
what the existing subagents and documents already do. They *are* the
crystallization's prescribed move — "rewrite a product need as a subagent prompt
change":

- *Episodic→semantic gradient* — instruct the rememberer to keep recent specific
  episodes as episodes and abstract them into semantic facts only once a pattern
  repeats. A staging concept inside the prompt, not a new table.
- *Salience/surprise-gated encoding* — instruct the rememberer to weight novelty
  and consequence, and to store *less*. A prompt change.
- *Reconsolidation* — instruct the rememberer to prefer `edit` of a contradicted
  fact, and feed it which recalled facts the turn contradicted.
- *Recency-aware recall* — give the retriever each fact's `created_at` and
  `last_recalled_at` (already stored) and let *it* weigh recency and frequency.
  No code ranks; the AI does.
- *Digest as a theory-grounded working-memory document* — make the rememberer
  prompt structure the digest as a true working memory: decisions + rationale,
  open threads, constraints, what was tried — the §4.3 "what to preserve" list.
- *Hierarchical / chunked recall* — instruct the rememberer to present recalled
  memory as a few named chunks (§3.9).

**Bucket 2 — machinery (reopens the crystallization doctrine).** These cannot be
prompt changes; they need real code, tables, or structure. Adopting any one is a
conscious decision to amend `ai-first.md`'s "no machinery" rule:

- A **spreading-activation graph** — needs nodes, weighted edges, and a
  traversal (Personalized PageRank). This is the user's "connections, nodes."
- **Activation/decay scores** as stored, computed data (ACT-R base-level).
- **Bi-temporal validity** — validity-interval columns on facts.
- A separate **episodic store** as its own retrievable tier.
- A **compaction/consolidation mechanism** for long turns — though note this one
  is also partly forced by the agent-loop cutover regardless, and a *minimal*
  version may be defensible as a rail rather than judgment.

The honest SME read: Bucket 1 alone would meaningfully improve Ariel's memory
and is fully consistent with the codebase's stated values — it should likely
happen regardless. Bucket 2 is a genuine strategic choice the user must make
with eyes open: the science says graph/activation memory measurably helps
*long-horizon, multi-hop* recall (§4.4), but the codebase just spent a cutover
removing it on principle. The benchmarks favoring elaborate memory are also
contested (§4.4), and long-context models keep raising the bar for "just keep it
in context."

## 9. Decision forks

What the research phase surfaces for decision (per the SME workflow: confirm the
forks before any design):

**Fork A — How far should a memory redesign go?**

- *Prompt-only.* Adopt Bucket 1; touch no schema or code structure. Maximally
  faithful to the crystallization. Lowest risk, real but bounded gains.
- *Targeted machinery.* Bucket 1 plus one or two genuinely-earned mechanisms
  (most likely: a real compaction/consolidation mechanism, since the agent-loop
  cutover forces the question anyway). A deliberate, scoped amendment to the "no
  machinery" rule.
- *Reopen the design.* Treat the flat store itself as the thing to reconsider,
  and scope a fresh cutover toward a lean spreading-activation / graph memory —
  accepting that this re-litigates a two-day-old decision.
- *Reference only.* Keep this document as the standing reference; change nothing
  now.

**Fork B — Which sub-problem leads the design phase?** (§2's five.)

- *Working memory / compaction* — the within-turn and cross-turn context
  problem. Couples tightly with the agent-loop cutover; the most time-sensitive.
- *Retrieval* — associative / spreading-activation recall; the "connections,
  nodes" idea; the biggest accuracy lever but the most machinery.
- *The episodic/semantic split* — add an episodic tier and a consolidation
  gradient.
- *Consolidation* — make the rememberer a genuine offline consolidator
  (salience-weighted, transformative).

**Fork C — Sequencing against the agent-loop cutover.** The agent-loop cutover
(P2: long adaptive loop + a host-side per-turn scratch store) is the *in-turn
working-memory* mechanism, and it is awaiting review. A memory redesign and that
cutover touch the same nerve — long conversations. Decide whether memory work
waits for it, merges with it, or proceeds independently.

## 10. Canonical reading list

The gold-standard sources, curated — what an SME would actually point to.

**Neuroscience of memory**
- Kandel, *In Search of Memory* (2006); Kandel, "The Molecular Biology of Memory
  Storage" (*Science* 2001) — molecular basis; short- vs. long-term.
- McClelland, McNaughton & O'Reilly (1995), "Why There Are Complementary
  Learning Systems…" (*Psych. Review*); Kumaran, Hassabis & McClelland (2016
  update) — the two-systems keystone; the AI bridge.
- Squire (2004), "Memory Systems of the Brain"; Tulving (1972), "Episodic and
  Semantic Memory" — the long-term taxonomy.
- Josselyn & Tonegawa (2020), "Memory Engrams" (*Science*) — engrams, silent
  memories.

**Cognitive psychology**
- Baddeley & Hitch (1974) + Baddeley (2000), the episodic buffer — working
  memory.
- Bjork & Bjork (1992), "A New Theory of Disuse" — storage vs. retrieval
  strength; adaptive forgetting.
- Craik & Lockhart (1972), levels of processing; Roediger & Karpicke (2006), the
  testing effect.
- Bartlett (1932), *Remembering* — reconstructive memory.

**Spreading activation & associative memory**
- Collins & Loftus (1975), "A Spreading-Activation Theory of Semantic
  Processing."
- Anderson & Schooler (1991), "Reflections of the Environment in Memory" — why
  decay is rational; the ACT-R activation model.
- Ramsauer et al. (2020), "Hopfield Networks Is All You Need" — attention *is*
  associative-memory retrieval.

**Cognitive architectures & consciousness**
- Sumers et al. (2023), "Cognitive Architectures for Language Agents" (CoALA).
- Newell, *Unified Theories of Cognition* (1990); the Common Model of Cognition
  (Laird, Lebiere & Rosenbloom 2017).
- Baars (1988) / Dehaene, *Consciousness and the Brain* (2014) — Global
  Workspace.
- Kahneman, *Thinking, Fast and Slow* (2011) — two-speed cognition.

**AI agent memory**
- Packer et al. (2023), MemGPT; Park et al. (2023), Generative Agents — the two
  foundational systems.
- Chhikara et al. (2025), Mem0; Rasmussen et al. (2025), Zep/Graphiti — CRUD and
  bi-temporal.
- Gutiérrez et al. (2024), HippoRAG — the neuroscience→retrieval bridge.

**Long context & compaction**
- Liu et al. (2023), "Lost in the Middle"; Xiao et al. (2023), StreamingLLM.
- Wu et al. (2021), "Recursively Summarizing Books."
- Anthropic Engineering, "Effective Context Engineering for AI Agents" (2025).

**Frontier**
- Behrouz et al. (2024–25), "Titans: Learning to Memorize at Test Time."
- Sun et al. (2024), test-time training; Google (2025), "Nested Learning."

---

*The nine full per-domain research reports — neuroscience, cognitive psychology,
spreading activation, cognitive architectures, consciousness, agent memory,
long-context, frontier architectures, graph/temporal memory — are preserved
verbatim in [`memory-cognition-research-appendix.md`](memory-cognition-research-appendix.md).*

[research-first SME workflow]: the May 2026 survey-first practice — research →
plan → confirm forks → implement.
