# TASK 007 — Detective UI & Live Event Streaming

## Read first
- `docs/API.md`
- `docs/PRODUCT.md`

## Goal
Create the memorable BugTrace demo surface without introducing a heavy frontend framework.

## Implement
- Jinja2 templates.
- homepage listing real seeded cases.
- investigation page.
- Server-Sent Events endpoint.
- vanilla JS SSE client.
- live investigation timeline.
- suspect board.
- evidence board.
- current hypothesis/root cause panel.
- restrained detective styling.

## Important
- no fake timeline events.
- no hard-coded suspect/evidence demo data.
- render only actual persisted backend events/state.
- no React.

## Acceptance criteria
Opening a real case shows the investigation updating live and ends with the actual root-cause state returned by the graph.
