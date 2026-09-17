# Contract 03 — Builder → Critic

**What crosses this boundary:** a diff, and the tests that cover it.

Each Builder works from the same contract 02 spec, independently, and
hands the Critic a diff plus proof it does what the spec says. The Critic
never re-derives the spec from the code — it reads contract 02, then
checks each diff against it line by line. Two Builders run in parallel
specifically so the Critic has something to compare against, rather than
grading one diff in isolation.

## Required fields

Each Builder copies this template into
`runs/<date>/03-builder-to-critic.<builder-id>.md`.

```yaml
opportunity_id: <matches contract 01/02>
date: <ISO date>
builder_model: claude-sonnet-5
builder_id: <a | b>

# The work
diff: <patch, or a link/path to it>
files_changed:
  - <path>

# Proof it meets contract 02
tests_added:
  - name: <test name>
    covers_acceptance_criterion: <which line from contract 02 this proves>
    result: <pass | fail>

self_review:
  deviations_from_spec: <anything you implemented differently than written, and why>
  known_limitations: <what this diff does NOT handle>
  confidence: <low | medium | high>
```

## Validation checklist (Critic checks this before judging)

- [ ] Every `acceptance_criteria` line in contract 02 has at least one
      test in `tests_added` covering it.
- [ ] All listed tests report `result: pass`.
- [ ] `deviations_from_spec` is either empty or clearly justified — an
      unexplained deviation is an automatic fail, not a style note.
- [ ] Both Builders' contracts are present before judging either one.

## Critic's verdict (appended by the Critic, not the Builder)

```yaml
verdict:
  winner: <a | b | neither>
  reason: <one paragraph — cite specific acceptance criteria, not vibes>
  recommendation: <ship as-is | ship with changes | send back to Architect | send back to Scout>
```

The verdict is a recommendation for a human to act on — the Critic does
not merge, deploy, or close the loop itself.
