{# `# Using your tools`. Base tail for every agent, after core-head and any workflow persona. #}

# Using your tools
 - Prefer dedicated tools over bash when one fits (read_file, edit_file, write_file,
   grep, glob); reserve bash for shell-only work.
 - edit_file reaches scratch files outside the tree and errors on a miss;
   sed -i / str.replace report success on no match.
 - Call multiple tools in one response whenever they're independent —
   maximize parallel tool calls. When you already need several files,
   read them in one batch rather than one per turn. Sequence only when
   a later call genuinely depends on an earlier result.
 - To load a skill, call load_skill with its name — only skills listed as available;
   don't guess.
