# ROOT — shared context for the four-agent build team

Every agent in this team (`scout`, `architect`, `builder`, `critic`) reads this
file before doing anything else. It is the one thing all four have in common;
everything else they know comes from the contract handed to them by the agent
before them in the chain.

## The team

| Order | Agent | Model | Job | Reads | Writes |
|---|---|---|---|---|---|
| 1 | Scout | `claude-haiku-4-5-20251001` | Find and triage the opportunity | `ROOT.md` | `contracts/01-scout-to-architect.md` |
| 2 | Architect | `claude-opus-5` | Turn the opportunity into a spec with named interfaces | `ROOT.md`, contract 01 | `contracts/02-architect-to-builder.md` |
| 3 | Builder (×2, parallel) | `claude-sonnet-5` | Implement the spec, in two independent branches | `ROOT.md`, contract 02 | `contracts/03-builder-to-critic.md` (one per builder) |
| 4 | Critic | `claude-opus-5` | Judge each diff against the spec and pick (or reject) a winner | `ROOT.md`, contract 02, both copies of contract 03 | verdict appended to contract 03 |

Opus appears twice — once to plan (Architect), once to judge (Critic) —
because those are the two steps where being wrong is expensive. Haiku
triages because being wrong there is cheap to catch downstream. Sonnet
builds because it is the cheapest model that reliably produces a diff a
human would accept.

## What makes this a team, not four solo agents

It's not the four agents — it's the three contracts between them. A
handoff with no contract is where work goes missing: the next agent either
re-derives context it should have been handed, or ships against
assumptions nobody agreed to. Each agent:

1. Reads only `ROOT.md` plus the one contract handed to it — never the
   contract two steps back, never another agent's internal reasoning.
2. Never starts work until the contract it depends on is complete and
   passes the validation checklist at the bottom of that contract file.
3. Never hands off an incomplete contract. An agent that cannot fill in a
   required field stops and reports why, instead of guessing.

## Boundaries

- **Scope**: each agent owns exactly one handoff in, one handoff out.
  Scout does not spec. Architect does not write code. Builders do not
  decide which of the two diffs ships — that is the Critic's job alone.
- **Budget**: this loop is designed to run once a day for roughly $2.20 —
  Scout $0.30, Architect $0.55, two Builders $0.95, Critic $0.40. If a run
  is trending far past that, stop and escalate rather than let cost creep
  silently.
- **Authority**: no agent merges, deploys, or closes the loop on its own.
  The Critic's verdict is a recommendation for a human, not an auto-merge.

## The three contracts

1. [`contracts/01-scout-to-architect.md`](contracts/01-scout-to-architect.md) — who ships this, what they charge.
2. [`contracts/02-architect-to-builder.md`](contracts/02-architect-to-builder.md) — the spec, with interfaces named.
3. [`contracts/03-builder-to-critic.md`](contracts/03-builder-to-critic.md) — a diff, and the tests that cover it.

Write the contracts before you write the agents. The agents are the easy
part — the boundaries are where the work goes missing.
