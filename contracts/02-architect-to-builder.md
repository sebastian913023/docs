# Contract 02 — Architect → Builder

**What crosses this boundary:** the spec, with interfaces named.

The Architect's job is to turn an opportunity into something two
independent Builders can implement the same way without talking to each
other. If the spec is vague, the two parallel diffs will disagree in ways
that have nothing to do with implementation quality — that's a spec
failure, not a Builder failure.

## Required fields

Copy this template into `runs/<date>/02-architect-to-builder.md`.

```yaml
opportunity_id: <matches contract 01>
date: <ISO date>
architect_model: claude-opus-5

# The decision
go_no_go: <go | no-go>
go_no_go_reason: <one sentence>

# The spec (only required if go_no_go: go)
problem_statement: <2-3 sentences, no solution language>

interfaces:
  - name: <function / endpoint / component name>
    signature: <exact signature — args, types, return type>
    behavior: <what it must do, in plain language>
    error_cases: <what happens on bad input — must be explicit, not implied>

acceptance_criteria:
  - <a single, testable statement>
  - <another one>

out_of_scope:
  - <explicitly excluded, so Builders don't over-build>

constraints:
  - <performance budget, dependency limits, style rules, etc.>
```

## Validation checklist (Builder checks this before starting)

- [ ] Every `interfaces` entry has a concrete signature — not "something like."
- [ ] Every `acceptance_criteria` entry is a yes/no test, not a vibe.
- [ ] `out_of_scope` is non-empty, or the Architect explicitly wrote "none."
- [ ] No interface references a system, API, or file the Builder can't see.

If any box is unchecked, both Builders stop and send the same gap back to
the Architect rather than each guessing differently — a guess made twice,
independently, is how the two diffs stop being comparable.
