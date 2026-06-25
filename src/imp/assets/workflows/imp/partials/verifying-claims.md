When verifying what code emits / returns / writes, read the construction
(emit/return) line itself, not where the value is computed — code between the two
can discard or override it. And a `file:line` citation is not proof: it lowers
your guard, so re-derive it. (Absence in a log ≠ non-emission — read the
append/emit line.)
