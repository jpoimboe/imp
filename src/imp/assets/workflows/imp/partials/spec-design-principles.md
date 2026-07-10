**Design for clarity.** Prefer units with clear boundaries and well-defined
interfaces; follow the codebase's existing patterns; fold in *targeted*
improvements to the code this change touches.

**Cover the design.** Where relevant, the spec should be concrete about
architecture, components, data flow, error handling, and testing, while keeping
imp's four-part contract (Goal / Scope / Decisions summary / Acceptance
criteria) and its emphasis on concrete, testable Acceptance criteria.

**YAGNI ruthlessly.** Remove unnecessary features from all proposals. Flag scope that looks like over-engineering.

**Make illegal states unrepresentable.** Prefer a design where the type or
structure rules out the bad case over one that admits it and then validates or
falls back. Push correctness into the shape of the data, not into runtime guards.
