**Design for clarity.** Prefer units with clear boundaries and well-defined interfaces;
follow existing patterns; fold in *targeted* improvements to the code this quest touches.

**Cover the design.** Where relevant, be concrete about
architecture, components, data flow, error handling, and testing —
while keeping imp's four-part contract (Goal / Scope / Decisions summary /
Acceptance criteria) and its stress on concrete, testable Acceptance criteria.

**YAGNI ruthlessly.** Cut unnecessary features from every proposal; flag over-engineering.

**Make illegal states unrepresentable.** Prefer a design whose type or structure
rules out the bad case over one that admits it and then validates or falls back.
Push correctness into the shape of the data, not into runtime guards.
