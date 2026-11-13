# meetingsAI

Private real-time AI meeting copilot.

- Portal: https://meetingsai.omchat.ovh
- Namespace: OMChat (domain), product is **meetingsAI**
- Architecture: laptop captures + transcribes; VPS aggregates, analyses, stores, and serves the live dashboard.

See `docs/ARCHITECTURE.md` and `docs/DEPLOYMENT.md`.

## Status

- Phase 1: repository skeleton, `/health`, `/ready`, Alembic baseline. No nginx, no systemd, no auth yet.
