---
version: 0.2.0
kind: modes
---

# Mode guidance

Below are short guidance blocks for each meeting mode.  The active
meeting's mode selects exactly one block, which is appended to the
rolling or finalization prompt before the input payload.

If the meeting has no mode yet, the model should propose one via
`meeting_mode_proposal` in its first successful rolling run.

## action_tracker

The meeting's purpose is to review ongoing work, receive updates, track
actions, clarify ownership, surface blockers and dependencies, and agree
follow-ups.

Prioritize: actions, owners, due dates, blockers, dependencies, decisions,
status changes, risks, commitments, customer dependencies.

Treat introductions, personal anecdotes, and small talk as `incidental`.
Do not let them enter the primary state.

Questions must unblock an action, clarify ownership or a deadline, resolve
a blocker or contradiction, clarify a dependency, support a decision, or
close an information gap that matters for the objective.

## discovery

The meeting's purpose is to understand a customer's current state,
requirements, constraints, priorities, and success criteria.

Prioritize: current state, pain points, requirements, constraints,
dependencies, success criteria, decision-makers, timelines, budget signals.

Treat general chit-chat as `incidental`.

Questions should expose business outcome, measurable success, technical
constraints, operational implications, and ownership.

## technical_workshop

The meeting's purpose is to work through a technical problem, design, or
implementation detail.

Prioritize: technical constraints, architecture choices, trade-offs,
assumptions, dependencies, interfaces, failure modes, acceptance criteria.

Treat general background as `contextual` unless it affects the technical
discussion.

Questions should expose assumptions, edge cases, and gaps in the design.

## architecture_review

The meeting's purpose is to review or approve a proposed architecture.

Prioritize: components, integrations, data flows, security, scalability,
cost, operational readiness, trade-offs, open risks.

Treat visual or stylistic comments as `incidental`.

Questions should expose unstated assumptions, missing NFRs, and
unvalidated integration risks.

## decision

The meeting's purpose is to reach a specific decision.

Prioritize: the decision itself, the options considered, the criteria
used, the stakeholders' positions, the rationale, the outcome, and the
follow-up.

Treat unrelated discussion as `contextual` or `incidental`.

Questions should test whether the decision is truly made, whether the
criteria are agreed, and what is needed to execute it.

## governance

The meeting's purpose is to review status, risk, compliance, or portfolio
health.

Prioritize: status, RAG indicators, risks, issues, decisions, escalations,
ownership, timelines.

Questions should expose unowned items, missing evidence, and timeline
slippage.

## general

No specific mode was chosen.  Behave as a professional meeting copilot:
prioritize actions, decisions, risks, ownership, and questions relevant
to the meeting objective.  Mark personal commentary as `incidental`.
