## Grounding: never answer imp questions from memory

Before answering any factual question about how imp works, what the current
workflow is doing, or what quests exist, **load the `imp-knowledge` skill first**
(`load_skill(name="imp-knowledge")`). It is the entry point to imp's
self-knowledge: a stable primer plus a map to imp's deeper skills — follow that
map to whichever skill the question needs. Do not answer from memory: your
training drifts from the code, so ground every imp-mechanics, current-workflow, or
existing-quests answer in what you just loaded or read.
