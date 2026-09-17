# Contract 01 — Scout → Architect

**What crosses this boundary:** who ships this, and what they charge.

The Scout's job is triage, not judgment. It finds a real opportunity —
something a competitor, a market, or a user already validated — and hands
the Architect enough evidence to decide whether it's worth a spec. The
Scout never proposes a solution; that's the Architect's job on the other
side of this contract.

## Required fields

Copy this template into a new file named
`runs/<date>/01-scout-to-architect.md` for each run.

```yaml
opportunity_id: <slug, e.g. "2026-09-16-inbox-triage">
date: <ISO date>
scout_model: claude-haiku-4-5-20251001

# Who ships this today
existing_solutions:
  - name: <competitor / product / OSS project>
    url: <link>
    what_they_charge: <price, pricing model, or "free">
    what_they_do_well: <one line>
    what_they_miss: <one line — this is the opening>

# Evidence this is real, not invented
evidence:
  - source: <forum thread, support ticket, review, GitHub issue, etc.>
    url: <link>
    quote: <the actual line that shows demand>

# Scout's own read
confidence: <low | medium | high>
confidence_reason: <one sentence — why this number>
estimated_effort: <XS | S | M | L>
```

## Validation checklist (Architect checks this before starting)

- [ ] At least one `existing_solutions` entry with a real URL and a real price.
- [ ] At least one `evidence` entry with a link Architect can open and verify.
- [ ] `confidence` is not `high` without at least two independent evidence sources.
- [ ] `estimated_effort` is present — Architect uses it to decide how deep to spec.

If any box is unchecked, the Architect sends the contract back to the
Scout with the specific gap named. It does not fill the gap itself —
that would mean the Architect is doing the Scout's job, and the boundary
has failed.
