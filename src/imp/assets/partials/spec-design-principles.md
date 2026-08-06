**Design for clarity.** Prefer units with clear boundaries and well-defined interfaces;
follow existing patterns; fold in *targeted* improvements to the code this quest touches.

**Cover the design.** Where relevant, be concrete about
architecture, components, data flow, error handling, and testing —
while keeping imp's four-part contract (Goal / Scope / Decisions summary /
Acceptance criteria) and its stress on concrete, testable Acceptance criteria.

**Browser surfaces need browser coverage.** When the quest changes what a browser renders or
how a page behaves, an acceptance criterion must be satisfiable only by driving a real
browser — response-string assertions never execute the script or lay out the page. If the
project has no browser automation, establishing it and wiring it to `test_cmd_slow` is in
scope; since config is read from the main checkout, that setting first gates the *next*
quest, so this quest's AC proves the suite green by running it.
