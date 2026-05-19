# Memory & Cognition — Research Appendix

> The nine full per-domain research reports behind
> [`memory-cognition-research.md`](memory-cognition-research.md). Raw output of
> nine parallel research subagents, May 2026, preserved as a reference. The
> synthesis, the Ariel-specific analysis, and the design forks live in the
> synthesis doc; this file is the source material — the science, the
> gold-standard sources, the SOTA, and the frontier, in full.

## Contents

1. Neuroscience of biological memory
2. Cognitive psychology of memory & learning
3. Spreading activation & associative memory models
4. Cognitive architectures
5. Consciousness, attention & thinking
6. SOTA LLM-agent memory systems
7. LLM long-context, context engineering & compaction
8. Memory-augmented neural architectures (frontier)
9. Graph-structured & temporal knowledge memory

---

## Domain 1 — Neuroscience of biological memory

### 1. Core theories and concepts

**The multi-store (modal) model — Atkinson & Shiffrin (1968).** Three
sequentially linked stores: *sensory memory* (raw input, high fidelity, decays
in a fraction of a second without attention), *short-term memory* (~7±2 items,
15–30s without rehearsal, acoustic encoding), *long-term memory* (essentially
unlimited, durable, semantic). Transfer between stores is governed by *control
processes* — rehearsal, coding, retrieval strategy. The model's passive
box-and-arrow architecture proved inadequate: neuropsychological dissociations
(patient KF — digit span ~2 yet intact long-term learning) showed STM and LTM
are separable, driving Baddeley & Hitch's reformulation.

**Working memory — Baddeley & Hitch (1974), expanded 2000.** An active,
multicomponent system that simultaneously stores and manipulates information.
The *central executive* is the supervisory attentional controller (not a store)
— coordinates subsystems, directs attention, switches tasks; maps to prefrontal
cortex. The *phonological loop* holds speech-coded traces ~1–2s with an
articulatory rehearsal process refreshing them. The *visuospatial sketchpad*
holds visual/spatial information. The *episodic buffer* (added 2000) is a
limited-capacity multimodal store that **binds** information from the other
subsystems and from long-term memory into coherent episodes — it is the
interface between working memory and LTM. Capacity: Miller's "7±2" overestimates
true capacity; Cowan (2001) puts the real limit at ~4 chunks once rehearsal is
controlled. Working-memory capacity predicts higher-order cognition (reasoning,
comprehension) better than almost any other measure.

**Long-term memory taxonomy (Squire; Tulving).** *Declarative/explicit* —
consciously reportable: *episodic memory* (personally experienced events located
in space and time, requiring autonoetic consciousness — subjective mental time
travel; most vulnerable to hippocampal damage, most reconstructive) and
*semantic memory* (context-free world knowledge — facts, concepts; persists
under hippocampal damage; distributed neocortical networks). *Non-declarative/
implicit* — unconsciously expressed: *procedural memory* (skills; basal ganglia,
cerebellum; intact in amnesia), *priming*, *classical conditioning* (amygdala
for fear), *non-associative learning* (habituation, sensitization).

**Synaptic consolidation — LTP, Hebbian plasticity, synaptic
tagging-and-capture.** Hebb's 1949 postulate ("neurons that fire together wire
together"); its correlate is *Long-Term Potentiation* (Bliss & Lømo 1973) —
sustained increase in synaptic efficacy, input-specific, associative,
NMDA-receptor-gated. *Long-Term Depression* weakens synapses, enabling forgetting
and refinement. Early LTP lasts hours; conversion to lasting late LTP needs new
protein synthesis. The *Synaptic Tagging-and-Capture* hypothesis (Frey & Morris
1997): weak stimulation sets a transient *synaptic tag* (1–3h); a nearby strong
stimulus triggers cell-wide *plasticity-related proteins*; tagged synapses
*capture* them, converting transient tags into lasting change. This is the
synaptic basis of the behavioral fact that a salient event rescues memory for
neutral events encoded within hours of it. Kandel (Nobel 2000, *Aplysia*):
short-term memory is covalent modification of existing proteins; long-term
memory requires gene expression (CREB), de novo protein synthesis, and
structural synapse remodeling.

**Systems consolidation — the hippocampal–neocortical dialogue.** Slower than
synaptic consolidation (weeks–years). *Standard Model* (Squire, Alvarez 1995):
new episodic memories depend on the hippocampus; over time, repeated
hippocampal–neocortical dialogue (especially during sleep, via replay)
strengthens cortical representations until memories become
hippocampus-independent. *Multiple Trace Theory* (Nadel & Moscovitch 1997):
every retrieval creates a new hippocampal trace; rich contextual episodic
memories remain hippocampus-dependent permanently; only semantic/gist memories
become independent. *Trace Transformation Theory* (Winocur & Moscovitch 2011):
traces transform — rich contextual episodic traces gradually become
schematized, semantic, cortically stored. The debate is unresolved; consensus is
that the episodic/semantic distinction is crucial to consolidation trajectory.

**Complementary Learning Systems — McClelland, McNaughton & O'Reilly (1995).**
One of the most influential papers in cognitive neuroscience. Motivated by
*catastrophic interference*: a single network learning fast overwrites prior
knowledge. The brain's solution: two systems with opposite properties. The
*hippocampal system* — fast, sparse, pattern-separated, one-shot, arbitrary
associations, limited generalization (a rapidly writable buffer). The
*neocortical system* — slow, distributed, overlapping representations,
generalizing, requires interleaved exposure to avoid interference. Binding them:
*replay* — the hippocampus replays recent experiences offline (sleep, rest),
interleaved with old memories, letting the neocortex integrate without
catastrophic interference. The 2016 update (Kumaran, Hassabis & McClelland)
adds *prioritized replay* (salient/surprising/goal-relevant memories replayed
preferentially) and *bidirectional* hippocampal-cortical interaction; it
explicitly connects to AI — experience replay in Deep Q-Networks was inspired by
CLS theory.

**Pattern separation and pattern completion.** Two complementary hippocampal
operations. *Pattern separation* (dentate gyrus): transforms cortical inputs
into sparse, decorrelated representations — similar inputs produce maximally
different DG codes, reducing interference; aided by adult neurogenesis.
*Pattern completion* (CA3): extensive recurrent collaterals form an
autoassociative network — a partial cue reactivates the full stored pattern.
The DG/CA3 balance is regulated by acetylcholine (high during encoding → more
separation; low during sleep → more completion/replay).

**The engram — Semon to Tonegawa.** Semon (1904) coined "engram" (the enduring
physical trace) and "ecphory" (retrieval). Modern engram science (Tonegawa,
Josselyn): Liu et al. (2012, *Nature*) tagged dentate-gyrus neurons active
during fear conditioning; optogenetic reactivation induced the memory. An engram
cell is (1) activated during learning, (2) physically modified by it, (3)
required for retrieval. Key findings: *memory allocation by CREB* — neuron
excitability at encoding determines recruitment (competition for allocation);
*silent engrams* — memories can exist latent, retrievable only by artificial
reactivation (amnesia is often inaccessibility, not erasure); *memory linking* —
events within ~5h share overlapping engram cells (linked); *engram competition*
— forgetting is increasingly understood as active competition between engrams,
reversible.

**Sleep-dependent consolidation — replay and sharp-wave ripples.** During
slow-wave sleep and quiet wakefulness, the hippocampus generates sharp-wave
ripples; during them, neurons *replay* recent waking spike sequences at 10–20×
compressed speed. The neocortex generates slow oscillations and spindles that
nest with hippocampal ripples. The dialogue is *bidirectional* — cortical
activation can cue hippocampal replay content. Replay is *selective*: novelty,
reward, and behavioral relevance increase replay priority. *Targeted Memory
Reactivation* — replaying a learning-associated cue during sleep biases replay
toward that memory and selectively enhances it (and can promote forgetting).
Forward replay supports prospective planning; reverse replay supports credit
assignment.

**Reconsolidation — Nader, Schafe & LeDoux (2000).** Retrieving a memory
*destabilizes* it; it requires re-stabilization over hours, during which it is
again vulnerable to modification. Mechanism: retrieval triggers proteasome-
dependent protein degradation at synapses, opening a window (~1–6h) requiring
new protein synthesis. *Prediction error* — a mismatch between expectation and
reality during retrieval — appears to be the key trigger: updating happens
precisely when the world differs from the memory. For AI: **retrieval is not
passive readout — it is active re-encoding.**

**Reconstructive memory and distortion.** Bartlett (1932): memory is
reconstruction, not a recording — people fill gaps with schema-consistent
content. Loftus: post-event misinformation is readily incorporated; false
memories are recalled as vividly as real ones; confidence does not track
accuracy. Mechanism: retrieval involves *cortical reinstatement* — reactivating
encoding-time regions — inherently susceptible to blending current knowledge,
schema, and post-event information. Source monitoring (distinguishing
experienced vs. heard vs. imagined) is prefrontal and fails under stress and
divided attention.

**Hippocampal Memory Indexing Theory — Teyler & DiScenna (1986).** The
hippocampus does not store memory content (that lives in distributed neocortical
representations); it stores an *index* — a record of which cortical areas were
co-active. A partial cue activates the index, which reinstates the full cortical
pattern. (This theory is the explicit basis of HippoRAG — Domain 9.)

**Schema theory.** Schemas — organized frameworks from repeated experience —
shape encoding/consolidation/retrieval; the medial prefrontal cortex is the hub.
Schema-*consistent* information is assimilated rapidly (mPFC–hippocampal), can
bypass slow consolidation; schema-*inconsistent* information needs stronger
hippocampal engagement and more time. Novelty/incongruity triggers dopamine that
tags information for consolidation.

**Adaptive forgetting.** Forgetting is active, not failure: *retrieval-induced
forgetting* (retrieving one item suppresses competitors; prefrontal inhibitory
control), *engram competition* (dominant engram suppresses others, reversibly),
*sleep-based triage*. Forgetting reduces interference clutter.

### 2. Canonical sources

Atkinson & Shiffrin (1968), the multi-store model. Baddeley & Hitch (1974) +
Baddeley (2000), working memory and the episodic buffer. Tulving (1972, 1985),
episodic/semantic memory, autonoetic consciousness. Squire (2004), memory
systems of the brain. McClelland, McNaughton & O'Reilly (1995), CLS theory.
Kumaran, Hassabis & McClelland (2016), CLS updated and connected to AI. Kandel
(2001), molecular biology of memory storage (Nobel lecture); Kandel, *In Search
of Memory* (2006). Bliss & Lømo (1973), LTP. Frey & Morris (1997), synaptic
tagging-and-capture. Nader, Schafe & LeDoux (2000), reconsolidation. Teyler &
DiScenna (1986), hippocampal indexing theory. Loftus & Palmer (1974),
misinformation effects. Josselyn & Tonegawa (2020), memory engrams. Nadel &
Moscovitch (1997), Multiple Trace Theory. Winocur & Moscovitch (2011), Trace
Transformation Theory.

### 3. Frontier (2023–2026)

Engram competition as a universal forgetting mechanism (Trends in Neurosciences
2025) — "forgotten" memories persist latent; optogenetic reactivation recovers
them (eLife 2024); Rac1 mediates active suppression. Cognitive rejuvenation of
engram cells (Neuron 2025) — partial reprogramming of aged/Alzheimer's engram
neurons restored plasticity and memory. Longitudinal engram tracking tools
(2024). Silent engrams in Alzheimer's models — early pathology impairs retrieval
before storage. Bidirectional hippocampal-cortical replay models (2024–25) —
cortical oscillations cue hippocampal replay; recent salient memories crowd out
old ones. Memory prosthetics — USC/Wake Forest hippocampal MIMO model driving
CA1 stimulation improved recall in human patients. "Learning to Forget:
Sleep-Inspired Memory Consolidation for Resolving Proactive Interference in
LLMs" (2025) — a direct neuroscience→AI translation. Schema-guided memory update
(*Phil. Trans. R. Soc. B* 2024) — congruent information updates schemas via
mPFC; incongruent creates new episodic traces.

### 4. Design implications

(1) **Two-track architecture (CLS):** a fast-write episodic buffer for raw
contextual conversation events + a slow-update semantic store updated by offline
consolidation, not in real time. A real-time "rememberer" doing both jobs at
once is architecturally wrong by the CLS account. (2) **Prioritized replay:**
flag salient/surprising/prediction-error events for high consolidation priority;
low-priority episodes decay without ever reaching the semantic store. (3)
**Reconsolidation:** retrieving and using a memory is a chance to update it;
within a short window after retrieval, prefer updating the retrieved memory over
creating a parallel one. (4) **Pattern separation/completion:** rich contextual
tags at encoding orthogonalize similar episodes; associative vector retrieval
reconstructs the full episode from a partial cue. (5) **Episodic buffer = the
context window:** capacity-limited, integrative; on overflow keep the
goal/plan + high-salience recent events, compress the middle. (6)
**Reconstructive retrieval:** tag retrieved memories with provenance,
confidence, recency; never treat them as ground truth. (7) **Schema
assimilation:** a schema layer (user profile/preferences) updated separately;
schema-consistent observations reinforce it, schema-violations trigger
reconsolidation. (8) **Temporal context:** every memory carries a when/
what-context tag. (9) **Active forgetting:** decay, interference suppression,
selective strengthening designed in. (10) **Offline consolidation:** do memory
management *between* conversations, not during them — the analog of sleep.

### 5. Open questions

The MTT vs. Standard Model debate is unresolved (does rich episodic memory ever
become hippocampus-independent?). The mechanism of replay prioritization is
incompletely understood — no ground-truth importance formula. The boundary
conditions of reconsolidation are unclear. How far rodent fear-conditioning
engram logic scales to complex human memory is contested. Whether interleaved
replay is the only solution to catastrophic interference (vs. EWC, generative
replay, meta-learning) is open. Whether semantic memory is a truly separate
system or consolidated episodic residue is debated. Working-memory capacity
("what is a chunk?") remains theoretically underspecified.

---

## Domain 2 — Cognitive psychology of memory & learning

### 1. Core theories and concepts

**The Ebbinghaus foundation (1885).** Working with nonsense syllables,
Ebbinghaus established: the *forgetting curve* — retention decays as a power-law
(steep then flattening; later refined as Wickelgren's power law); the *savings
method* — relearning is faster than fresh learning even when explicit recall
fails entirely (memories become inaccessible, not erased; replicated by Murre &
Dros 2015); *overlearning* — practice beyond mastery slows later decay; the
*spacing effect* — distributed practice beats massed.

**Levels of processing — Craik & Lockhart (1972).** Memory is a by-product of
processing; the *depth* of processing determines durability. Shallow
(structural) → fragile; intermediate (phonological) → moderate; deep (semantic)
→ durable. *Elaboration* is the mechanism — forming connections to existing
knowledge creates multiple retrieval pathways. The *self-reference effect*
(Rogers et al. 1977) and *generation effect* (Slamecka & Graf 1978; generating
an answer beats reading it, d≈0.40) follow. Criticism: "depth" is hard to define
independently; *transfer-appropriate processing* (Morris, Bransford & Franks
1977) showed deep processing does not always win — what matters is the match
between encoding and retrieval operations.

**Encoding specificity — Tulving & Thomson (1973).** What is remembered is a
function of what was encoded *together* with the target. Retrieval depends on
overlap between encoding and retrieval context. A strong cue can fail if absent
at encoding; a weak cue present at encoding can succeed. Context-dependent
memory: environmental, internal-state, and mood context at encoding act as
implicit retrieval cues. **The format/framing in which a memory is recorded
should match the format in which it will be queried.**

**The New Theory of Disuse — Bjork & Bjork (1992).** The most powerful current
framework. Two independent strengths per memory: *storage strength* (how
thoroughly learned; only ever increases — essentially permanent) and *retrieval
strength* (how currently accessible; fluctuates, decays). The paradox: when
retrieval strength is high, retrieval adds little to storage strength; when
retrieval strength is low, successful retrieval produces a large storage-strength
gain — why effortful retrieval after a gap strengthens memory far more than easy
restudy. Forgetting is *adaptive* interference management. *Desirable
difficulties* (Bjork 2011): conditions that slow immediate performance but
enhance long-term retention — spacing, interleaving, testing, generation,
variability — provided retrieval still succeeds.

**The spacing and testing effects.** The spacing effect is, per Bjork, "the most
robust finding in all of cognitive psychology." Optimal spacing is ~10–20% of
the retention interval for short goals, falling toward 5–10% for year-scale
goals — the optimal inter-review interval *grows* with mastery. Interleaving
(mixing problem types) beats blocking for transfer. The *testing effect*
(Roediger & Karpicke 2006): a study session followed by a test produces better
long-term retention than repeated restudy; production formats beat recognition;
benefits compound. *Retrieval-induced forgetting* (Anderson 2003): practicing
some items from a category impairs recall of non-practiced items from the same
category — selective review casts a shadow over the unreviewed.

**Episodic vs. semantic memory and semanticization.** Tulving's distinction;
2024 work argues they form a *continuum*, joined by *semanticization* — episodic
memories are gradually abstracted into context-free semantic knowledge during
consolidation. Repeated similar events become "repisodic" — partially
semanticized stereotyped memories.

**Schema theory and reconstructive memory — Bartlett (1932).** "War of the
Ghosts": recall is reconstructive — assimilation (unfamiliar → familiar),
leveling (detail loss), sharpening (prominent details retained). "Effort after
meaning" — people actively make sense of input. Memory errors are systematic and
predictable (DRM false-memory paradigm).

**Working memory, chunking, metamemory.** Miller (1956) — span ~7±2 *chunks*;
Cowan (2001) — true attentional capacity ~4 chunks without chunking. Decay is
not the primary working-memory forgetting mechanism — interference is.
*Metamemory*: judgments of learning are biased — the *fluency illusion* (easily
processed material feels well-learned) causes premature stopping; desirable
difficulties counteract it. Salience/emotion inflate predicted memorability;
importance and future relevance selectively enhance encoding.

### 2. Canonical sources

Ebbinghaus (1885), *Memory*. Bartlett (1932), *Remembering*. Miller (1956), the
magical number seven. Craik & Lockhart (1972), levels of processing. Tulving
(1972, 1983), episodic/semantic; Tulving & Thomson (1973), encoding specificity.
Morris, Bransford & Franks (1977), transfer-appropriate processing. Bjork &
Bjork (1992), the New Theory of Disuse; Bjork (2011), desirable difficulties.
Roediger & Karpicke (2006), the power of testing. Cowan (2001), the magical
number four. Anderson (2003), retrieval-induced forgetting. Cepeda et al.
(2006, 2008), spacing-effect reviews. Murre & Dros (2015), Ebbinghaus
replication.

### 3. Frontier (2023–2026)

**FSRS (Free Spaced Repetition Scheduler)** — SOTA spaced-repetition algorithm,
in Anki since 2023; outperforms SM-2 for ~99.6% of users; 15–40% fewer reviews
at equal retention. Models each item by **D**ifficulty, **S**tability (days
until recall probability drops to 90%), **R**etrievability. Forgetting curve:
`R(t,S) = (1 + FACTOR·t/S)^DECAY` (power law, DECAY = −0.5). Schedules review by
inverting the curve to the target retention. **Half-life regression** (Settles &
Meeder, Duolingo, 2016): estimates a per-(learner,item) half-life; `p_recall =
2^(−Δ/h)`; 45%+ error reduction over Leitner baseline. The episodic–semantic
continuum (2024 *Phil. Trans. R. Soc. B* special issue). "FOREVER" (2025) —
Ebbinghaus-curve-scheduled replay for LLM continual learning. Human-like
forgetting curves observed in deep neural networks (2025). Computational
metamemory and adaptive importance scoring converging on importance-weighted,
decay-modulated retrieval.

### 4. Design implications

(1) **Two independent dimensions per memory:** a *storage weight* (importance;
increments on retrieval; does not decay — drives consolidate/compress/drop
decisions) and a *retrieval score* (accessibility; decays; resets on access —
drives what surfaces into context now). (2) **Per-memory half-life:** assign
each memory a stability; increase it on retrieval, more when retrievability was
low (desirable difficulty); a background process re-surfaces memories whose
retrievability is dropping. (3) **Encode with rich context, retrieve with
context match.** (4) **Distinguish episodic from semantic; manage the
lifecycle** — recent specific records vs. abstracted generalizations; after N
consistent observations synthesize a semantic fact and downweight the episodes.
(5) **Do not compress uniformly at overflow** — keep recent episodic detail,
compress mid-age to structured episodic summaries, compress old to semantic
abstractions, never drop high-storage-weight items. (6) **Active retrieval
strengthens** — a successful recall should raise the memory's stability. (7)
**Handle interference** — mark a contradicted old fact superseded, don't just
add the new one. (8) **Weight encoding by salience/importance**, not recency.
(9) **Chunk** retrieved memory into a few named groups. (10) **Encoding
variability** — store confirmed knowledge in multiple frames for multiple
retrieval routes.

### 5. Open questions

The nature of "depth" remains underspecified. Whether storage strength is truly
monotonic is debated. The mechanism of retrieval-induced forgetting (inhibition
vs. blocking) is unresolved. Optimal spacing is person- and material-specific;
inferring individual forgetting rates without explicit testing is speculative.
The episodic→semantic threshold and whether semanticization is gradual or
threshold-gated is unknown. Schemas: reconstructive (backward-looking gap-filling)
vs. generative (forward-looking prediction) — not cleanly separated. LLM
"confidence" calibration to memory accuracy is an open problem. The optimal
retrievability target threshold (FSRS uses 90%) is a design choice, not derived.

---

## Domain 3 — Spreading activation & associative memory models

### 1. Core theories and concepts

**Hierarchical semantic networks — Collins & Quillian (1969).** The first formal
computational model of semantic memory (the Teachable Language Comprehender):
*nodes* for concepts, *property links* for attributes, organized as a strict
taxonomic hierarchy with *cognitive economy* (each property stored at the
highest applicable level). Predicted reaction time scales with hierarchy levels
traversed. Failed on *typicality effects* (a robin is verified a bird faster
than a penguin, though both are one link away), category-size violations, and
evidence that people actually store properties redundantly — driving the move
to spreading activation.

**Spreading activation theory — Collins & Loftus (1975).** Replaced the rigid
tree with a *network of nodes connected by weighted links*, where link length
encodes semantic distance. When a node is processed it becomes *activated*;
activation *spreads outward* along links, *decaying* with distance; multiple
context concepts each spread activation simultaneously; retrieving a target
requires its activation to cross a threshold. Question-answering and priming are
explained by *intersection* of spreading activation fronts. Typicality is
natural: typical members have stronger, shorter links. Priming (BREAD activates
BUTTER) is the empirical signature; automatic spreading operates below ~250ms
SOA.

**The ACT-R declarative memory model (Anderson).** The most mathematically
elaborated theory. All declarative knowledge is stored in *chunks* (typed
slot-value records). The activation of chunk *i*:

```
A_i = B_i + Σ_j (W_j · S_ji) + Σ_k Δ_k·PM_k + ε_i
```

*Base-level activation* — the jewel of ACT-R:

```
B_i = ln( Σ_{j=1..n} t_j^(−d) )
```

where *n* is the number of prior uses, *t_j* the time since the j-th use,
*d ≈ 0.5* the decay parameter. Each use contributes an additive term that decays
as a power function of elapsed time — capturing **recency** (a recent use → large
term) and **frequency** (many old uses still sum) at once; this is the *power
law of forgetting*. An efficient approximation: `B_i ≈ ln(n/(1−d)) − d·ln(L)`,
*L* = chunk lifetime.

*Spreading activation*: each active context element *j* sends activation
`W_j·S_ji`, where `W_j = W/N_j` (source activation split over the *N_j* active
elements) and `S_ji = S − ln(fan_j)`. The **fan effect**: a context element
associated with many chunks (large *fan_j*) has low associative strength to any
one — activation spreads thin. This is an attention budget; it mirrors human
data (facts about heavily-associated entities verify slower).

*Retrieval probability* (logistic, with threshold τ and noise s):

```
P(retrieval) = 1 / (1 + e^(−(A_i − τ)/s))
```

*Retrieval latency*: `RT = F·e^(−A_i)`. *Partial matching*: mismatching slots
contribute negative penalties, enabling graceful degradation — imperfect cues
still retrieve relevant content.

**The rational analysis — Anderson & Schooler (1991).** "Reflections of the
Environment in Memory." Memory's decay function is *Bayesian-optimal*: it should
maximize the probability of retrieving information when *needed*. Analysis of
natural corpora (NYT headlines, child-directed speech, email) showed the
probability an item reappears follows a power law of time since last appearance,
`P(need) ∝ t^(−d)`, d ≈ 0.5. ACT-R's `B_i = ln(Σ t_j^(−d))` is exactly the
log-odds of need under that power-law environment. **Memory decay is not a bug —
it is an adaptive prediction of future need.**

**Hopfield networks (1982).** The first tractable model of content-addressable
associative memory. N fully-connected binary neurons; symmetric weights set by
the Hebbian outer-product rule `w_ij = (1/N) Σ_μ ξ_i^μ ξ_j^μ`. Dynamics minimize
an energy function `E = −½ Σ w_ij s_i s_j`, converging to attractor states. A
noisy/partial input converges to the nearest stored pattern — *pattern
completion*. Classical capacity: ~0.14·N patterns; beyond that, spurious
attractors (analogs of false memories). Maps to LTP — Hebbian learning between
co-active neurons.

**Sparse Distributed Memory — Kanerva (1988).** Long binary patterns
(~1000-bit); ~10⁶ random "hard locations" sparsely sample the 2^N address space;
writes/reads activate all locations within a Hamming radius; majority-vote
readout. Noise-tolerant; reading near a stored address *converges* to it.
Extended to *hyperdimensional computing* — binding, bundling, permutation over
high-dimensional random vectors — a bridge between distributed and symbolic
representation.

### 2. Canonical sources

Collins & Quillian (1969), hierarchical semantic networks. Collins & Loftus
(1975), "A Spreading-Activation Theory of Semantic Processing" — the theoretical
foundation. Rosch (1975), typicality / prototype theory. Anderson & Bower
(1973), *Human Associative Memory*. Anderson (1983), *The Architecture of
Cognition*. Anderson & Schooler (1991), "Reflections of the Environment in
Memory" — the rational justification of power-law decay. Anderson & Lebiere
(1998), *The Atomic Components of Thought* — the full ACT-R specification.
Hopfield (1982), content-addressable associative memory. Kanerva (1988), *Sparse
Distributed Memory*; Kanerva (2009), hyperdimensional computing. Ramsauer et al.
(2020), "Hopfield Networks is All You Need."

### 3. Frontier (2023–2026)

**Modern Hopfield networks — Ramsauer et al. (2020).** A continuous-state
Hopfield network whose update rule is `ξ_new = X·softmax(β·X^T·ξ)` — *exactly
transformer self-attention* (Q=ξ, K=V=X). Storage capacity jumps from linear to
*exponential* in N; retrieval in a single step. **Transformer attention is a
one-step associative-memory retrieval** — an LLM attending over its context is
already running a content-addressable memory. The Energy Transformer (NeurIPS
2023) extends this to full attention layers. **HippoRAG** (NeurIPS 2024)
implements spreading activation via Personalized PageRank over a knowledge graph
(see Domain 9). **SYNAPSE** (2025) implements spreading activation directly:
episodic + semantic graph nodes; activation initialized at query-matched
anchors; propagation `u_i^(t+1) = (1−δ)a_i^(t) + Σ_{j∈N(i)} (S·w_ji·a_j^(t))/
fan(j)` (fan-effect normalization); *lateral inhibition* (top nodes suppress
weaker competitors — winner-take-all); sigmoid firing; converges in 3
iterations; a meta-cognitive gate refuses to answer below an activation
threshold ("feeling of knowing"). Reported 32% improvement on multi-hop tasks at
95% token reduction. **FadeMem** (2025) — Weibull differential decay across a
long-term and short-term layer. **SAMPL** — spreading activation + non-monotonic
plasticity reproduces retrieval-induced forgetting.

### 4. Design implications

(1) **ACT-R-style chunk activation as the core memory score.** Each memory
carries a continuously-updated activation `A_i = B_i + Σ_j(W_j·S_ji)`. Track
every access time; compute `B_i = ln(Σ_k (t_now−t_k)^(−0.5))` — this single
principled formula replaces both an ad hoc importance score and recency scoring.
Gate inclusion with the logistic retrieval probability. Power-law decay (not
exponential) keeps intermittent-but-important facts alive. (2) **Spreading
activation for context-driven retrieval.** Build a weighted association graph
among memories (semantic similarity, temporal adjacency, entity co-occurrence);
on a query, initialize activation at query-similar nodes and spread for ~3
iterations with fan-effect normalization and lateral inhibition; final score
combines base-level + similarity + spread activation. This enables multi-hop
recall that flat vector similarity cannot. (3) **The fan effect as an attention
budget** — prefer specific, less-connected facts over generic hub-connected
ones. (4) **Compaction as Hopfield consolidation** — what survives compaction is
the attractor states (the patterns the conversation returned to). (5) **Tiered
time-layered memory** — hot/warm/cold with different decay regimes; migration
on activation threshold. (6) **Personalized PageRank** as a cheap global-prior
first pass before local spreading activation. (7) **Calibrate decay to the
conversation's re-use statistics** (Anderson & Schooler). (8) **Partial matching
via embedding similarity** for fuzzy retrieval.

### 5. Open questions

Whether spreading activation is a real mechanism or a metaphor (priming evidence
is solid only below ~250ms SOA). The fan effect at scale — `S_ji = S − ln(fan)`
may be too aggressive for very large graphs. Spurious attractors / false
memories in modern Hopfield networks (metastable states). Whether base-level and
spreading activation should combine additively or multiplicatively. Temporal
asymmetry of associations (forward stronger than reverse) vs. symmetric Hopfield
weights. The right chunk granularity (sentence? turn? fact triple?). Whether
dynamically learning associative strengths stabilizes or destabilizes a
long-running graph. Which framing — attention as associative memory, or as
vector-symbolic binding/unbinding — best guides design.

---

## Domain 4 — Cognitive architectures

### 1. Core theories and concepts

A *cognitive architecture* is the task-independent infrastructure of an
intelligent agent — the fixed substrate specifying which memory systems exist,
how decision-making cycles, what learning mechanisms are available. Newell's
*Unified Theories of Cognition* (1990) argued intelligence cannot be explained
piecemeal; architectures are computational instantiations of that ambition.

**Soar** (Newell, Laird, Rosenbloom; def. ref. Laird 2012). Working memory is a
symbolic graph; production rules match it in parallel and all matching rules
fire. The unit of deliberation is the *operator* (the Problem Space Hypothesis —
all goal-directed behavior is search through problem spaces). *Impasses*: when
knowledge is insufficient to select or apply an operator, Soar *automatically*
creates a subgoal and reasons recursively — meta-reasoning uses the same
mechanism as object-level reasoning. *Chunking*: when a subgoal produces a
result, Soar compiles a new production rule from the dependency trace — slow
deliberate reasoning becomes fast reactive behavior (the System 2 → System 1
transition). Later additions: *semantic memory* (fact graphs with base-level +
spreading activation), *episodic memory* (automatic snapshots of working memory
each cycle, a temporal stream), reinforcement learning.

**ACT-R** (Anderson; def. ref. *How Can the Human Mind Occur…* 2007). Organized
into modules mapped to brain regions: declarative (hippocampus), procedural
(basal ganglia), goal (PFC), imaginal (parietal), perceptual/motor. Each module
is accessed through a single-slot *buffer*; modules run in parallel but the
procedural module fires exactly one production per ~50ms cycle — the model of
the central cognitive bottleneck. Declarative retrieval is activation-based and
probabilistic (the Domain 3 math). *Knowledge compilation* — declarative
instructions become embedded in productions with practice (proceduralization),
converting slow declarative-dependent performance into fast automatic
performance. Validated against fMRI.

**LIDA** (Franklin) — the most explicit computational implementation of Global
Workspace Theory. The richest memory taxonomy: sensory memory, perceptual
associative memory, *transient episodic memory* (decays in hours), declarative
memory (formed *offline* by consolidation from transient episodic memory —
analogous to hippocampal→cortical transfer during sleep), procedural memory. The
*cognitive cycle* (~10 Hz): understanding (build the Current Situational Model)
→ consciousness (attention codelets form coalitions that compete for the Global
Workspace; the winner is broadcast to all systems) → action selection and
learning (the broadcast triggers learning across all memory systems — the
Conscious Learning Hypothesis).

**CLARION** (Sun) — the dual representational hypothesis: every subsystem
(action-centered, non-action-centered, metacognitive, motivational) has an
explicit symbolic top level and an implicit subsymbolic bottom level.
Bottom-up learning extracts explicit rules from implicit networks; top-down
learning internalizes rules into implicit associations. Distinctive: a dedicated
*metacognitive subsystem* and a *motivational subsystem*.

**Sigma** (Rosenbloom) — an attempt at "functionally elegant grand unification":
factor graphs as a single formalism for symbolic and subsymbolic processing.

**The Common Model of Cognition** (Laird, Lebiere & Rosenbloom 2017) — the
consensus across 40 years: working memory (capacity-limited active state),
procedural memory (a production system firing one production per ~50ms cycle),
declarative memory (large long-term store retrieved on demand), perception and
motor modules, learning by procedural compilation + reinforcement + declarative
addition. Peripheral modules run parallel; the central executive is serial.
Validated by fMRI (2020).

### 2. Canonical sources

Newell (1990), *Unified Theories of Cognition*. Laird (2012), *The Soar
Cognitive Architecture*. Anderson (2007), *How Can the Human Mind Occur in the
Physical Universe?* Laird, Lebiere & Rosenbloom (2017), "A Standard Model of the
Mind." Franklin (2007), LIDA / Global Workspace. Sun, CLARION. Rosenbloom et al.
(2016), the Sigma architecture. Sumers, Yao, Narasimhan & Griffiths (2023),
"Cognitive Architectures for Language Agents" (CoALA).

### 3. Frontier (2023–2026)

**CoALA** — maps LLM agents onto cognitive-architecture structure: memory
(working / episodic / semantic / procedural), action space (external; internal:
retrieval, reasoning, learning), decision procedure (plan → execute). It found a
developmental gradient — early agents have only procedural memory; sophisticated
ones (Voyager, Generative Agents) use all four memory types. **NL2GenSym**
(2025) — LLM generates Soar rules; smaller models *with* the architecture beat
larger models without it. **SYNAPSE** (2025) — spreading activation over an
episodic-semantic graph with fan effect and lateral inhibition (Domain 3).
**MIRIX** (2025) — a six-memory multi-agent system (core, episodic, semantic,
procedural, resource, knowledge vault) with a meta memory manager routing to six
dedicated managers; reported 85.4% on LoCoMo. **CogMem** (2024) — a three-layer
hierarchy (long-term distilled strategies / session working notes / per-turn
focus-of-attention). **EM-LLM** (ICLR 2025) — event segmentation by Bayesian
surprise (the hippocampal prediction-error analog) handling 10M-token contexts.
**Sculptor** (ICLR 2026) — RL-trained active context management tools
(fragment, summarize, hide, restore) — the LLM analog of Soar's impasse-driven
subgoaling. **MemAgent** (2025) — RL-trained segment-and-summarize working
memory. ACT-R-inspired LLM memory architectures implement base-level + spreading
activation directly.

### 4. Design implications

(1) **Four distinct memory stores, not one context buffer** — working
(context), episodic (timestamped events, retained as episodes), semantic
(abstracted facts, extracted by periodic consolidation), procedural (recurring
patterns / workflows). (2) **Distinguish transient episodic from long-term
declarative** (LIDA) — keep recent raw episodes in a fast buffer; at session
end, run a consolidation pass deciding what to promote to durable episodic /
semantic / procedural stores — the sleep analog. (3) **Activation-based
retrieval** (base-level + spreading activation), not pure semantic similarity.
(4) **Active working-memory management** (Sculptor) — compress completed
threads, archive retrievable content, foreground relevant past content; manage
proactively, don't wait for overflow. (5) **Impasse detection and subgoaling** —
detect when stuck and trigger explicit sub-procedures; chunk learned
resolutions. (6) **Model the decision cycle explicitly** — propose → evaluate →
select → learn. (7) **A metacognitive layer** (CLARION) — confidence tracking,
retrieval-failure detection, self-monitoring. (8) **Principled forgetting via
activation decay.** (9) **Compaction as consolidation, not truncation** —
identify episodes worth preserving, semantic facts worth extracting, patterns
worth encoding; migrate to long-term stores; keep a restoration cue in context.

### 5. Open questions

The right granularity for episodic storage (turn? thread? task?). How
consolidation should be triggered (session end? pressure? schedule?) — no
validated theory. Whether the fan effect is a feature or a bug for an agent.
How explicitly-stored procedures should relate to (and override) the LLM's
implicit procedural knowledge in weights. Whether adding an explicit
metacognitive module helps or conflicts with LLMs' emergent metacognition. How
many memory types are necessary and sufficient. Whether automatically learned
production rules / procedures can be trusted (overfitting, conflicts).

---

## Domain 5 — Consciousness, attention & thinking

### 1. Core theories and concepts

**Global Workspace Theory — Baars (1988).** The mind runs many specialized,
modular, unconscious processes in parallel; consciousness is a bottleneck
resource — the *global workspace* — that holds a small amount of information and
*broadcasts* it to all processors. The theater metaphor: what falls in the
spotlight of attention is conscious; backstage activity is unconscious.
Unconscious specialist coalitions *compete* for the workspace; competition is
resolved by bottom-up factors (salience, intensity, novelty, prediction error)
and top-down factors (goals, task context). The winner is broadcast globally,
enabling cross-domain coordination no single module could achieve. The workspace
is serial and capacity-limited — a bottleneck that acts as an information-routing
hub.

**Global Neuronal Workspace — Dehaene & Changeux.** The neural instantiation:
local processors plus a workspace of long-range pyramidal neurons connecting
prefrontal, parietal, temporal, precuneus hubs. *Ignition*: when a stimulus
crosses a threshold (bottom-up intensity + top-down attention), recurrent loops
switch into a nonlinear self-sustaining state — an all-or-none event at
~200–300ms producing a P300 wave, gamma oscillations, long-distance
synchronization. Subliminal stimuli produce only early, localized activity.
The *C0/C1/C2 framework* (Dehaene, Lau & Kouider 2017): C0 = unconscious
processing; C1 = global availability (information broadcast to the workspace,
available for report, decision, working memory); C2 = metacognitive
self-monitoring (reflecting on one's own states, confidence, error detection).

**Predictive processing / free energy — Friston, Clark, Hohwy.** The brain is a
hypothesis-testing machine. Each cortical level generates *top-down predictions*;
only the *prediction error* propagates up. The *free energy principle*: the brain
minimizes variational free energy (an upper bound on "surprise"), equivalent to
maximizing evidence for its generative model. Perception updates beliefs to
explain prediction error; *active inference* changes sensory input itself.
*Precision-weighting* — weighting each prediction error by its estimated
reliability — is the formal cousin of attention: high precision = amplified
influence = attending; low precision = suppressed = ignoring.

**Integrated Information Theory — Tononi.** Starts from phenomenology; derives
what a substrate must satisfy. Φ quantifies *integrated information* — the
information a system generates as a whole beyond the sum of its parts.
Consciousness requires both integration (irreducible) and differentiation
(astronomically many distinguishable states). Predicts feed-forward
architectures have Φ≈0 regardless of behavior — a controversial decoupling of
intelligence from consciousness.

**Attention Schema Theory — Graziano.** The brain builds a simplified internal
model of its own attention process (an *attention schema*), just as it builds a
body schema. Awareness is the self-attribution of attention based on this useful
but mechanistically-impoverished model. Explicitly framed as a foundation for
engineering machine attention-modeling.

**Dual-process theory — Kahneman, Stanovich.** *System 1*: fast, automatic,
parallel, associative, intuitive, pattern-driven, prone to bias. *System 2*:
slow, deliberate, serial, rule-governed, effortful, conscious — and lazy,
defaulting to endorsing System 1 unless engaged. System 2's key function is
detecting when System 1 is wrong. The default mode network ↔ System 1; the
executive control network ↔ System 2 (anti-correlated).

**Biased competition — Desimone & Duncan (1995).** Stimuli compete via lateral
inhibition; competition is biased by bottom-up salience and top-down
task-relevant templates held in working memory. Attention is not a pre-filter —
it is an ongoing dynamic resolved in real time. The salience network (dACC,
anterior insula) monitors for high-priority signals and reorients the workspace.

**Memory, imagination, prospection — Schacter & Addis.** The *constructive
episodic simulation hypothesis*: episodic memory exists to flexibly recombine
stored elements into simulations of possible futures. Remembering and imagining
share the default mode network. Memory is constructive, not reproductive — the
source of both distortion and imaginative flexibility.

### 2. Canonical sources

Baars (1988), *A Cognitive Theory of Consciousness*. Dehaene (2014),
*Consciousness and the Brain*; Dehaene, Lau & Kouider (2017), "What is
consciousness, and could machines have it?" Friston (2010), the free-energy
principle. Clark (2016), *Surfing Uncertainty*; Hohwy (2013), *The Predictive
Mind*. Kahneman (2011), *Thinking, Fast and Slow*. Desimone & Duncan (1995),
biased competition. Tononi (2008), IIT. Graziano & Kastner (2011), Attention
Schema Theory. Schacter & Addis (2007), constructive memory.

### 3. Frontier (2023–2026)

Butlin, Long et al. (2023), "Consciousness in Artificial Intelligence" — derives
indicator properties from five consciousness theories; no current AI is
conscious, but no obvious technical barrier exists. The COGITATE adversarial
collaboration (2023) directly tested GWT vs. IIT in humans — *neither* prediction
was cleanly confirmed; the field has no validated mechanistic theory of
conscious access. "Theater of Mind for LLMs" (2026) — a GWT-based multi-agent
LLM architecture with a select-broadcast "cognitive tick" and entropy-based
novelty drive. "Cognitive Workspace" (2025) — four-tier buffer architecture with
54–60% memory reuse and anticipatory retrieval. The Predictive Global Neuronal
Workspace (Whyte & Smith) — formally unifies GNW with active inference.

### 4. Design implications

**The context window is the global workspace** — what is in it is "conscious"
(available for reasoning, generation, action); what is not is unconscious.
**Compaction is the selection problem the brain solves to decide what is
conscious.** (1) Treat the context window as a limited workspace with an explicit
*gating mechanism*: a multi-factor salience score = bottom-up salience + top-down
goal relevance (a target template, per biased competition) + precision-weighting
(prefer surprising/uncertain content) + dependency structure (what is a
prerequisite for current reasoning) + recency. (2) *Ignition-analogous
thresholds* — compact on a cognitive-load threshold (entropy dropping →
compress; high-novelty/high-dependency content → defer or compress finely), not
a fixed token count. (3) *Two-speed processing* — a fast associative path for
routine queries, a slow deliberative path (retrieve, load dependency chains,
verify) gated by uncertainty/novelty/stakes. (4) A *C2 metacognitive layer* —
after retrieval/compaction, self-check "does my context contain what I need? is
anything inconsistent?" (5) A *salience-network analog* — an always-on monitor
for topic shifts, new constraints, corrections that should reorganize the
workspace immediately. (6) *Constructive memory* — recalled content should be
synthesized for the current task, not replayed verbatim; store schemas and
relational structure. (7) *Precision-weighted forgetting* — compress
low-precision content (already confirmed, schema-consistent, unreferenced); keep
high-precision content (commitments, corrections, surprises, hard-won results).
(8) A *default-mode-network analog* — scheduled off-task background consolidation.

### 5. Open questions

Functional theories (GWT, GNW, AST, predictive processing) explain access
consciousness, not phenomenal experience. The COGITATE results challenged both
GWT and IIT — there is no validated mechanism. Whether chain-of-thought is truly
System 2 or System 1 with a verbalized narrative (CoT is often unfaithful).
IIT's claim that transformers cannot be conscious. How to operationalize
precision-weighting without a full generative model. When to schedule
background consolidation for an always-on agent. The optimal compression rate is
content-dependent and unknown. Whether attention truly equals consciousness
(they can dissociate).

## Domain 6 — SOTA LLM-agent memory systems

### 1. Core systems and concepts

**The standard taxonomy (CoALA, Sumers et al. 2023):** by temporal scope —
*working/in-context* (the context window: high bandwidth, zero retrieval cost,
capacity-limited, vulnerable to "lost in the middle") vs. *long-term external*;
by functional form — *episodic* (timestamped events — "what happened"),
*semantic* (abstracted facts/preferences — "what is true"), *procedural*
(skills/workflows — "how to do things"). The governing pattern is a
**write → manage → read** loop, where "manage" (consolidation, update,
forgetting) is a first-class stage.

**MemGPT / Letta (2023).** Memory as OS virtual memory. Three tiers: *core
memory* (always in-context, small, fixed-size, self-edited via
`core_memory_append`/`core_memory_replace`), *recall storage* (full conversation
history, searchable), *archival storage* (unlimited vector store). A FIFO queue
of recent messages; on overflow, evicted messages are replaced by a *recursive
summary*. *Memory pressure* triggers eviction. The 2024–26 evolution adds
*sleep-time agents* — a background agent sharing memory blocks, consolidating
asynchronously off the response path.

**Stanford Generative Agents (2023).** A *memory stream* — an append-only log of
timestamped observations. The field's most-copied retrieval formula:
`score = recency + importance + relevance`, where *recency* = `0.995^(hours
since last access)` (exponential decay), *importance* = an LLM-assigned 1–10
poignancy score fixed at write time, *relevance* = cosine similarity. *Reflection*
— periodically (when summed importance of recent events crosses a threshold) the
agent synthesizes higher-level memories: generate salient questions → retrieve
relevant memories → extract insights with citations → store as new memory
objects. This builds a reflection tree (raw observations → abstractions). No
forgetting mechanism — the stream grows unbounded.

**MemoryBank (2023).** Long-term memory with an *Ebbinghaus forgetting curve*:
retention `R = e^(−t/S)`, strength `S` starting at 1; on recall, `S += 1` and
`t` resets — the spacing effect. Three tiers: conversation, daily event
summaries, personality. Acknowledged as "exploratory and highly simplified."

**A-MEM / Agentic Memory (2025).** Zettelkasten-inspired. Each memory note has
content, timestamp, LLM-generated keywords/tags/contextual-description,
embedding, and a *link set*. New notes trigger embedding-based + LLM-driven
*linking* to related notes, and *memory evolution* — existing related notes'
descriptions/tags can be regenerated. A living, self-organizing network.

**Mem0 (2025).** LLM-driven extract → update pipeline. Each candidate fact is
compared against semantically similar existing memories; the LLM applies
**ADD / UPDATE / DELETE / NOOP**. The graph variant (Mem0g) represents memory as
entities + relationship triplets; contradictory relationships are marked
*invalid* with timestamps rather than deleted, enabling temporal reasoning. 2026
algorithm: multi-signal retrieval (semantic + BM25 keyword + entity matching);
reported 92.5 on LoCoMo with ~75% lower token use than full-context.

**Reflexion (2023).** Verbal reinforcement learning — the agent generates
natural-language self-criticism after a failed attempt, stores it in an episodic
buffer, and prepends accumulated reflections on the next trial. Memory as
accumulated failure analysis.

**ExpeL (2023), Voyager (2023), Agent Workflow Memory (2024).** Procedural /
experiential memory: ExpeL stores successful trajectories + extracts general
*insights* with a self-correcting importance vote; Voyager maintains an
ever-growing *skill library* of executable, retrievable, composable programs;
AWM extracts reusable abstract *workflows* (task-specific values abstracted out).

**Zep / Graphiti (2025).** A bi-temporal knowledge graph — see Domain 9.

**TiMem (2026).** A five-level *temporal* memory tree (segment → session → daily
→ weekly → profile) with stratified consolidation and complexity-aware retrieval.
Reported SOTA on LoCoMo and LongMemEval with 52% less retrieved context —
temporal hierarchy outperforming semantic topic clustering.

**MemoryOS / MemOS (2025).** A memory operating system — three heat-scored tiers
(short/mid/long-term) with frequency×recency×importance promotion; the MemCube
abstraction unifies plaintext, activation, and parametric memory.

**ACON (2025).** Failure-driven context compression — analyzes trajectory pairs
where the agent succeeds with full context but fails with compressed context to
learn what must be preserved; distillable to a small compressor. Finding:
smaller models with optimized compressed context can beat larger models with
full uncompressed context.

**SCM — Sleep-Consolidated Memory (2026).** Distinct online (wake) and offline
(sleep) phases. Working memory capped at 7 items. Importance tagged on four
dimensions (novelty 0.30, task relevance 0.35, emotional valence 0.20,
repetition 0.15). NREM consolidation strengthens co-occurring concept pairs
(Hebbian) and downscales all edges 20%; REM "dreaming" generates novel
associative edges. Adaptive-threshold forgetting toward a target graph size.

### 2. Canonical sources

Packer et al. (2023), MemGPT. Park et al. (2023), Generative Agents. Zhong et
al. (2023), MemoryBank. Shinn et al. (2023), Reflexion. Zhao et al. (2023),
ExpeL. Wang et al. (2023), Voyager. Sumers et al. (2023), CoALA. Wang et al.
(2024), Agent Workflow Memory. Xu et al. (2025), A-MEM. Chhikara et al. (2025),
Mem0. Rasmussen et al. (2025), Zep/Graphiti. Pan et al. (2026), TiMem. Wu et al.
(2024), LongMemEval; Maharana et al. (2024), LoCoMo. "Memory for Autonomous LLM
Agents" survey (2026).

### 3. Frontier (2024–2026)

Async / sleep-time consolidation (Letta sleep-time agents; SCM's NREM/REM
phases) — moving memory management off the response path. Temporal reasoning as
a first-class concern — the hardest remaining problem (Zep's bi-temporal model,
TiMem's temporal hierarchy). Hierarchical consolidation outperforming semantic
clustering. Self-evolving memory (A-MEM, Evo-Memory). Principled forgetting
(adaptive thresholds; A-MAC admission control on five factors). Reflective /
iterative retrieval (MemR3 — retrieve, reflect, identify evidence gaps, re-query).
Memory security — memory poisoning (MINJA, MemoryGraft) is a real attack surface;
66% of poisoned entries evade LLM detectors. **Long-context LLMs vs. memory
systems** — a 2026 cost-performance study put GPT-5-mini full-context at ~93% on
LoCoMo vs. flat extraction memory at ~58%; memory systems win on *cost* (cheaper
after ~10 turns) and *scale*, not raw accuracy. Multi-agent memory coordination
(MIRIX — 85.4% LoCoMo).

### 4. Design implications

(1) Maintain at minimum four separated stores: working/in-context (small,
structured, self-edited, bounded), episodic (timestamped, full-fidelity, never
lossy-summarized), semantic (distilled facts — flat or graph), procedural. (2)
**Temporal hierarchy** for long conversations (turn → session → cross-session →
profile); complexity-aware retrieval. (3) **Consolidation asynchronous** — write
raw episodic records synchronously and cheaply; run summarization/reflection/
extraction/dedup asynchronously (session end, idle, volume). (4) **The
"rememberer" subagent pattern is validated SOTA** (Letta sleep-time agents,
MIRIX managers, Mem0's pipeline). Keep it, but evolve it: run it asynchronously
post-session; upgrade from save/don't-save to ADD/UPDATE/DELETE/NOOP with
explicit conflict resolution; add temporal metadata; do heavy processing at
write time; add admission control with an importance threshold. (5) **Compaction
and memory extraction are different operations** — compaction preserves
conversational flow within the window; extraction distills durable facts; use
different prompts and triggers; extract *before* compacting. (6) **Conflict
resolution cannot be an afterthought** — mark superseded facts invalid with
timestamps rather than overwriting. (7) **Retrieval quality is the bottleneck**
— multi-signal (semantic + keyword + entity) beats embedding-only; iterative
retrieval for multi-hop. (8) **NOOP matters** — explicitly decide *not* to store.

### 5. Open questions

Whether the long-context vs. structured-memory accuracy gap is closing
(hierarchical systems may narrow it). The right consolidation trigger. Whether
LLM-generated reflections can be trusted (they can entrench wrong beliefs).
Selective forgetting vs. forget-nothing (safety-critical forgetting is
unsolved). Parametric vs. external memory tradeoffs. Multi-hop / causal
retrieval remains unsolved. Memory security has no mature defense. Benchmarks
are contested (a public LoCoMo-score dispute) — scores, many from non-peer-
reviewed 2026 preprints, are directional only.

## Domain 7 — LLM long-context, context engineering & compaction

### 1. Core findings and techniques

**Lost in the middle — Liu et al. (2023).** Accuracy is U-shaped in the position
of relevant information: models retrieve reliably from the beginning and end of
a long context, but accuracy drops sharply — 30%+ on multi-document QA — when
relevant information is in the middle. Persists across models and context
lengths. Architectural: early tokens accumulate cross-layer influence; natural
text places key information at edges; softmax dumps unresolved attention onto
initial tokens.

**Context rot.** A 2025 study of 18 frontier models: every model degrades as
input length grows — well before the token limit. Degradation accelerates as
query–target semantic similarity decreases (real tasks are worse than clean
needle-in-a-haystack tests); even single distractors reduce accuracy. A 2024
study showed degradation persists *even with perfect retrieval* — context length
itself is harmful. A critical-threshold study found catastrophic collapse around
~43% of the max context window — practical usable context is well below the
nominal window. Needle-in-a-haystack tests are unrepresentative — BABILong and
LongMemEval show models effectively use only 10–20% of stated context for
multi-step reasoning.

**Attention sinks — StreamingLLM (Xiao et al. 2023).** Models trained with
sliding-window attention fail catastrophically when the cache is evicted —
perplexity spikes by orders of magnitude. The cause: softmax requires weights to
sum to 1; with no relevant tokens, the model "dumps" attention onto initial
tokens, which become trained "sinks." The content is irrelevant — replacing
sink tokens with newlines restores performance; *position* matters. Keep ~4
initial tokens + a sliding window → stable generation over millions of tokens.
The system prompt at the start is a structural stabilizer, not just semantic.

**Context engineering.** Anthropic's framing: context is a finite, curated,
attention-budgeted resource — find "the smallest set of high-signal tokens that
maximize the likelihood of desired outcomes." The n² attention cost makes every
token non-free; each token dilutes attention on others and degrades accuracy.
Strategies: *compaction*, *context offloading* (note-taking to external memory),
*sub-agent context isolation* (specialized sub-agents with isolated windows
returning ~1–2K-token summaries), *just-in-time retrieval* (lightweight
identifiers, dynamic loading). Failure modes: context poisoning, distraction,
confusion, clash.

**Compaction and summarization.** *Recursive/hierarchical summarization* (Wu et
al. 2021, "Recursively Summarizing Books") — divide into chunks, summarize each,
recursively summarize the summaries; quality scales with recursion depth and
chunk quality, and each level is verifiable. *Sliding window + running summary*
— keep recent N turns verbatim, fold older turns into a rolling summary; cascaded
summary-of-summary degrades fidelity multiplicatively. *Proactive/background
compaction* — start summarizing at a soft threshold so the final compaction is a
swap to an already-computed summary. *Salience-aware compaction* — what to
preserve: exact identifiers, error messages verbatim, user corrections,
constraints, decisions and their rationale, specific values; what to compress:
completed actions (outcome not trace), superseded reasoning; what to drop:
pleasantries, re-fetchable tool output, failed-attempt traces. Claude Code uses a
nine-section structured compaction (intent, concepts, file changes, errors
resolved, problem-solving logic, user corrections, open threads, pending tasks,
next steps).

**KV-cache / prompt caching as a constraint.** Caches are prefix-dependent — any
change to any token before the cache breakpoint invalidates the entire
downstream cache. Dynamic content (timestamps, IDs) in the system prompt breaks
the cache every request; tool-definition changes invalidate it; naive compaction
that restructures history eliminates cache benefit. Cache-preserving compaction:
a breakpoint after the static system prompt; dynamic values at the end; keep
message-sequence prefixes stable across compaction.

**RAG vs. long context.** Long-context models outperform RAG when resources are
ample and context is static; RAG is 8–82× cheaper; a hybrid (route simple
retrieval to RAG, complex synthesis to long context) is optimal. RAG is evolving
from "find a chunk" to "curate a context."

**Active / autonomous context management.** A cluster of 2025–26 work: *Focus*
(`start_focus`/`complete_focus` primitives — declare a scope, explore, summarize,
prune the raw trace) — 22.7% token reduction, no accuracy loss; *Sculptor* (tools
to fragment, summarize, hide/restore, search context); *MemAct* (working-memory
management as a learned RL policy); *CMV* (DAG-versioned context, lossless
trimming of tool-result overhead). Key finding: "LLMs do not naturally optimize
for context efficiency — they require scaffolding that makes compression a
first-class part of the workflow."

**Soft / learned compression.** Gist tokens (Mu et al. 2023 — compress prompts
into a few tokens via attention-mask modification, 26× compression);
In-Context Autoencoder; LLMLingua (token-level extractive compression). All
require fine-tuning, are not interpretable, and realize gains only at
generation, not encoding.

### 2. Canonical sources

Liu et al. (2023), "Lost in the Middle." Xiao et al. (2023), StreamingLLM /
attention sinks. Wu et al. (2021), "Recursively Summarizing Books." Anthropic
Engineering (2025), "Effective Context Engineering for AI Agents"; Anthropic
compaction API docs. The Chroma context-rot study (2025). Mu et al. (2023),
gist tokens. "Don't Break the Cache" (2026). "Memory for Autonomous LLM Agents"
survey (2026). LongMemEval (2024); BABILong (2024).

### 3. Frontier (2024–2026)

Learned context-management policies (MemAct — RL over context-modifying agents).
Active context-management tools (Sculptor, ICLR 2026). DAG-versioned context
(CMV) — git-like snapshots, branches, lossless trimming. Conversation-tree
architectures — multi-branch DAGs, structural prevention of context poisoning.
Extreme soft compression (480× via KV values). Anthropic Managed Agents memory
(2026) — persistent files at `/mnt/memory/`, cross-session, audited. The
LoCoMo/MemoryArena gap — models scoring 95% on passive recall drop to 40–60% on
active decision-relevant memory; retrieval accuracy is not the right metric —
whether retrieved information appropriately influences decisions is.

### 4. Design implications

(1) **Trigger compaction early and proactively** — at 40–50% context use, not
90%+; quality degrades before the token wall and compaction quality degrades on
a bloated window; run it in the background so the swap is instant. (2) **Treat
content categories differently** — never summarize decisions/constraints/
identifiers/errors/open-threads (keep verbatim or in a structured pinned log);
summarize completed sub-tasks to outcome; drop re-fetchable tool output and
filler. (3) **Hierarchical, not flat summarization** — divide a long
conversation into "chapters," summarize each independently and verifiably,
compose a meta-summary; avoid cascaded summary-of-summary. (4) **Protect the
KV-cache prefix** — stable system prompt first and cached; the compaction block
second; conversation history append-only; never restructure. (5) **Position
high-signal content at context boundaries** (the lost-in-the-middle curve). (6)
**Give the agent active context-management tools** (Focus-style begin/end
primitives) — compression must be a first-class, scaffolded part of the
workflow. (7) **Externalize long-term memory to files**, loaded just-in-time. (8)
**Maintain a separate, append-only thread log** of decisions/open-threads that
survives compaction independently and is verified against after each compaction.
(9) **Never cascade compaction without checking for loss.** (10) Evaluate with
realistic benchmarks (LongMemEval, BABILong), not single-needle tests.

### 5. Open questions

Whether longer context actually improves long-running agent tasks (no
large-scale study of full-history vs. managed-compaction agents). The right
compaction granularity (turn / phase / capacity-triggered). Whether soft
compression can be trusted for high-stakes information (not interpretable). How
to handle contradictions across compaction boundaries. The right summary for
conversations whose topic shifts. Whether autonomous context management works
without scaffolding (passive prompting yields ~6% savings, aggressive ~23%).
Whether compression strategies transfer across task types. The privacy
implications of persistent compaction summaries.

## Domain 8 — Memory-augmented neural architectures (frontier)

### 1. Core architectures and concepts

**Memory Networks (Weston et al. 2014) and End-to-End Memory Networks (2015).**
A neural system with a separated, addressable external memory: components I
(input encoding), G (memory update), O (output via memory read), R (response).
End-to-End Memory Networks made it differentiable via *soft attention* over
memory slots, and introduced *multi-hop reading* — the query is iteratively
refined over multiple passes through memory (anticipating chain-of-thought).

**Neural Turing Machines (2014) and the Differentiable Neural Computer
(Graves et al., Nature 2016).** A neural controller with an addressable
read/write memory matrix; *content-based addressing* (cosine similarity) plus
*location-based addressing* (convolutional shift). The DNC added usage-based
allocation, a temporal link matrix (recording write order — enabling sequential
playback, an episodic-memory analog), and multiple read heads.

**Memory-augmented transformers.** *Transformer-XL* (2019) — segment-level
recurrence: cache the previous segment's hidden states, with relative positional
encoding. *Compressive Transformer* (2020) — a second, compressed memory tier:
aged activations are compressed rather than discarded (compression-as-memory-
management). *Memorizing Transformers* (2022) — a kNN lookup into a large
non-differentiable cache of past key-value pairs, blended with local attention
by a learned gate; effective context to 262K tokens; enables immediate
inference-time use of newly-defined facts without weight updates. *RETRO* (2021)
— retrieval from a 2-trillion-token database via chunked cross-attention;
matched GPT-3 (175B) with 7.5B parameters by offloading factual knowledge to the
database. *Infini-attention* (2024) — local attention plus a compressive
linear-attention memory within one transformer block, bounded footprint
regardless of sequence length.

**Memory Layers at scale (Meta, 2024).** Trainable sparse key-value memory
layers (product-key addressing) replacing FFN layers, up to 128B memory
parameters; outperform dense models requiring 2× compute on factual tasks —
separating "knowing" capacity from "thinking" capacity.

**Titans (Google, 2024–25).** The most conceptually rich frontier entry. A
*neural long-term memory module* — a small MLP whose weights are updated by
gradient descent **at test time**. The update is **gated by surprise**: the
memory is trained to predict values from keys; the gradient of that loss is the
surprise signal — a *surprising* (badly-predicted) token produces a large
gradient and a large memory update; an expected token produces a negligible
update. This formalizes the neuroscience intuition that surprising events are
more memorable. Momentum accumulates surprise; a decay term prevents staleness.
Three variants: Memory-as-Context, Memory-as-Gate, Memory-as-Layer. The MIRAS
framework unifies linear RNNs, SSMs, transformers, and Titans as variations of
*associative memory* with four design axes (memory architecture, attentional
bias, retention gate, memory algorithm).

**Test-time training (TTT layers, 2024).** The recurrent hidden state *is* a
model; its update rule *is* a gradient step on a self-supervised objective
computed on the current input — the model learns during inference.

**Nested Learning (Google, NeurIPS 2025).** Reframes neural networks as nested
optimization problems at multiple timescales — architecture, optimization, and
memory as one principle; the Continuum Memory System formalizes the spectrum
from volatile to stable memory.

**Modern Hopfield networks (2020)** — softmax attention is a one-step Hopfield
retrieval (Domain 3). **Mamba / selective SSMs (2023–24)** — input-dependent
recurrence; fixed-capacity compressed memory that saturates on long contexts
beyond ~16K tokens — the limitation Titans and TTT target.

### 2. Canonical sources

Weston, Chopra & Bordes (2014), Memory Networks. Sukhbaatar et al. (2015),
End-to-End Memory Networks. Graves et al. (2014, 2016), NTM and the
Differentiable Neural Computer. Dai et al. (2019), Transformer-XL. Rae et al.
(2020), Compressive Transformers. Wu et al. (2022), Memorizing Transformers.
Borgeaud et al. (2021), RETRO. Munkhdalai et al. (2024), Infini-attention.
Meta FAIR (2024), Memory Layers at Scale. Behrouz et al. (2024–25), Titans.
Sun et al. (2024), test-time training. Ramsauer et al. (2020), modern Hopfield
networks. Google (NeurIPS 2025), Nested Learning.

### 3. Frontier — where this is heading

The field is a dialectic between two poles: **memory as retrieved tokens**
(Memory Networks → Memorizing Transformers → RETRO → RAG — interpretable,
updatable, unbounded, but with retrieval latency and context-window pressure)
and **memory as updated weights** (NTM/DNC → Memory Layers → TTT → Titans —
seamless, no retrieval overhead, but fixed capacity and catastrophic-forgetting
risk). The frontier is **hybrids** — layered systems with different memory types
at different timescales. **Surprise/novelty as the universal write-gate** has
been independently arrived at by Titans (gradient magnitude) and by agent-memory
work (write-time salience scoring). **Test-time learning** — inference and
training converging; a model processing a long conversation is also learning
from it. **Memory as an OS layer** (MemOS) — parametric, activation, and
plaintext memory under one scheduler.

### 4. Design implications

Ariel uses a hosted LLM and cannot update weights at inference, so Titans/TTT/
Memory-Layers are **inspiration, not directly implementable**. But the
*principles* translate to system-level design: (1) **Surprise/novelty-gated
memory writes** — before writing a fact, measure its semantic divergence from
the nearest existing facts; high divergence (surprise) → write; low → skip or
update in place. Weight write priority by novelty × importance × source
reliability. This is the Titans surprise signal realized without model access.
(2) **A tiered memory architecture** — in-context (full fidelity) → compressed
episodic (per-conversation summaries) → semantic/factual store → cold archive
with versioned supersession (never truly deleted). (3) **RETRO-style chunked
retrieval over conversation history** — store past conversation chunks as
embeddings; retrieve relevant chunks rather than relying solely on lossy
sequential summarization (retrieval degrades gracefully; summarization distorts
systematically). (4) **Landmark-style segment indexing** — a concise summary per
conversation segment; coarse-to-fine two-stage retrieval. (5) **Temporal decay**
on retrieval weight; frequently-accessed facts stay "warm." (6) **For every
piece of information, decide: stored as a retrievable discrete fact, or
compressed into a running abstract?** — and consider storing both, letting
retrieval decide which to use.

### 5. Open questions

Whether test-time gradient memory generalizes reliably at 10M+ tokens
(catastrophic forgetting within the memory module). What gets retrieved and how
the model uses it remains poorly understood. The optimal compression function
is unknown. Catastrophic forgetting in any weight-updating memory module.
Write-time vs. read-time curation. The scaling laws of memory vs. compute.
Interpretability of parametric memory. The "null hypothesis" problem — plain
long-context attention often matches specialized memory architectures on
standard benchmarks.

## Domain 9 — Graph-structured & temporal knowledge memory

### 1. Core systems and concepts

**GraphRAG (Microsoft, Edge et al. 2024).** Solves "global sensemaking"
questions that standard chunk-retrieval RAG cannot. Offline: an LLM extracts
entities, relationships, and claims from document chunks → a knowledge graph;
the Leiden algorithm produces a multi-level hierarchy of *communities*; the LLM
generates a structured summary for each community at each level (higher levels
summarize sub-community summaries — token-efficient recursion). Online: *global
search* (map-reduce over community summaries — for whole-corpus themes), *local
search* (an entity's neighborhood subgraph). Beats vector RAG 72–83% on
comprehensiveness for sensemaking. Limitation: designed for static corpora; no
incremental update.

**HippoRAG (Gutiérrez et al., NeurIPS 2024) — the clearest neuroscience→retrieval
bridge.** Built explicitly on the *hippocampal indexing theory* (Teyler &
DiScenna 1986): the hippocampus stores not memory content but an *index* of
which neocortical areas were co-active; a partial cue activates the index, which
reinstates the full cortical pattern. HippoRAG's mapping: the **LLM = neocortex**
(extracts an open knowledge graph from text — NER then OpenIE triples); the
**retrieval encoder = parahippocampal region** (adds synonymy edges between
similar entities); **Personalized PageRank over the KG = the hippocampal index**.
Retrieval: extract query entities → map to graph nodes (weighted by *node
specificity* — rare entities upweighted) → run Personalized PageRank from those
seed nodes → score passages by accumulated PageRank. PageRank propagates
activation through the graph — a query about entity A retrieves content about C
when A→B→C is a path, even with no shared surface terms — **computational
pattern completion / multi-hop associative retrieval**. Up to 20% better on
multi-hop QA, 10–30× cheaper than iterative methods. HippoRAG 2 (2025) adds
passage nodes, dense+sparse seed fusion, LLM recognition filtering.

**Zep / Graphiti (Rasmussen et al. 2025).** A temporally-aware knowledge graph
for agent memory. Three tiers: *episode subgraph* (raw conversational data,
immutable — the provenance layer), *semantic entity subgraph* (extracted
entities and relationship facts), *community subgraph* (clusters with
summaries). **Bi-temporal modeling** is the key differentiator: every entity
edge carries four timestamps — `t_valid`/`t_invalid` (when the fact was true in
the world) and `t_created`/`t_expired` (when the system learned it) — answering
"what did we know about X as of last week?" **Edge invalidation**: when new
information contradicts an existing edge, an LLM detects the contradiction and
the old edge is *invalidated* (`t_invalid` set), **not deleted** — historical
queries still see it; current-state queries do not. Incremental, episode-by-
episode updates (unlike GraphRAG's batch rebuild). Hybrid retrieval: cosine +
BM25 + graph traversal + reciprocal rank fusion, no LLM call at query time
(~300ms). Reported 94.8% on Deep Memory Retrieval vs. MemGPT's 93.4%; on
LongMemEval, 15–18.5% accuracy improvement with ~90% latency reduction and
context cut from ~115K to ~1.6K tokens.

**Personalized PageRank — the mechanism of associative retrieval.** A random
walk with restart: with probability α follow an edge, with probability (1−α)
teleport back to the seed (query-matched) nodes. The steady-state distribution
captures direct and multi-hop connectivity; nodes densely connected to the seeds
accumulate high probability. Unlike one-shot cosine/BM25, PPR propagates through
graph structure — this is what enables multi-hop retrieval. α controls the
activation "radius."

**SYNAPSE (2025)** — spreading activation as the primary retrieval mechanism
(Domain 3): an episodic-semantic graph, energy injected at anchor nodes,
propagation with fan-effect attenuation and lateral inhibition, a meta-cognitive
rejection gate. **GAM (2026)** — a two-layer graph (a Topic Associative Network
as global semantic memory; an Event Progression Graph as a real-time episodic
buffer consolidated into the global layer on topic shift) — explicit episodic/
semantic separation. **TiMem, MemTier, Chronos** — temporal hierarchies and
temporal-event graphs (Domain 6). **KARMA (2025)** — multi-agent automated
knowledge-graph enrichment with a debate-based conflict-resolution agent.

**General temporal-KG principles.** Bi-temporal modeling (event time vs.
transaction time). Edge validity windows. *Invalidation, never deletion* —
deletion destroys the audit trail. Entity resolution / deduplication (embedding
+ BM25 + LLM disambiguation). Contradiction detection. Community detection for
efficiency (Leiden — hierarchical; label propagation — incremental).

### 2. Canonical sources

Edge et al. (2024), GraphRAG. Gutiérrez et al. (2024), HippoRAG; HippoRAG 2
(2025). Rasmussen et al. (2025), Zep/Graphiti. Teyler & DiScenna (1986) and
Teyler & Rudy (2007), the hippocampal indexing theory. SYNAPSE (2025). GAM
(2026). KARMA (NeurIPS 2025). MemTier (2026). Chronos (2025). LongMemEval (2024).

### 3. Frontier (2024–2026)

Self-maintaining knowledge graphs (KARMA; Graphiti's incremental updates).
Graph-based spreading activation as retrieval (SYNAPSE, GAAMA). Hierarchical
graph memory with explicit episodic/semantic separation (GAM, Hindsight).
Temporal event-specific graph schemas (Chronos — SVO tuples with resolved
datetime ranges — beats general KG construction for conversational memory).
Hybrid graph+vector as the dominant pattern (pure graph traversal too slow, pure
vector misses multi-hop). Dense retrieval as the next bottleneck (MemTier — BM25
is the current ceiling). Graph-memory governance — no production system yet has
a constitutional/validation layer.

### 4. Design implications

(1) **A knowledge graph is a structural upgrade to a flat fact store, not a
replacement** — it adds typed relationships between facts, associative multi-hop
retrieval (PPR / spreading activation), and temporal validity. The cost: LLM
calls for entity extraction, resolution, and contradiction detection (Graphiti
does this incrementally at practical latency). (2) **A temporal graph** —
long-running conversations are exactly where bi-temporal modeling matters;
without it the agent confidently recalls stale facts. Model each fact as an edge
with `(t_valid, t_invalid)`; on a change, invalidate the old edge and create the
new one — both remain, only the new is "current." (3) **The community layer IS
compaction** — hierarchical community summaries, updated incrementally, replace
ad hoc periodic summarization. (4) **PPR / spreading-activation retrieval** —
extract query entities, seed PPR with node-specificity weighting, retrieve by
accumulated score, augment with embedding similarity; α adaptive (wide net for
vague queries, tight for specific). (5) **Layered episodic/semantic separation**
(GAM) — an episodic buffer consolidated to a semantic graph on topic shift.

### 5. Open questions

Whether graph traversal is necessary in practice or over-engineering (the 2026
trend is toward lightweight entity-linking without a full graph). LLM-based
entity resolution reliability (~83%, costly, systematic errors). The optimal
graph schema for conversations (general triples vs. temporal events vs. typed
roles). Incremental community detection at scale. When old facts should be
forgotten vs. preserved. Benchmark contamination disputes. Cross-agent shared-
graph write coordination. Dense retrieval trained on task-success signals as the
next frontier.

---

*End of appendix. The synthesis, the Ariel-specific analysis, the seven design
principles, and the decision forks are in
[`memory-cognition-research.md`](memory-cognition-research.md).*


