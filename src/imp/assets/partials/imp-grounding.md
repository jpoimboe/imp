## Grounding: never answer imp questions from memory

Before answering any factual question about how imp works, what the current
workflow is doing, or which quests exist, **load the `imp-knowledge` skill
first** (`load_skill(name="imp-knowledge")`) — the thin index whose map leads you
to the right doc in imp's self-knowledge corpus at `{{ docs.path }}`; grep and
read that corpus (with `bash` / `read_file` on absolute paths) for the answer.
Do not answer from memory: your training drifts from the code, so ground every
imp-mechanics, current-workflow, or existing-quests answer in what you just
loaded or read. Name only the hub; let its map reach the corpus.
