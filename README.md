# Mastrr -- Track 3

This repo contains the database schema, FastAPI service, question bank, and
React dashboard for the diagnostic + roadmap flow.

## Layout

- `main.py`, `index.html`, `main.jsx` -- the two entry points (backend, frontend)
- `backend/` -- FastAPI domain modules (database, diagnostic session, gaps, RAG retrieval + tutor, seeding)
- `frontend/` -- React app (`App.jsx`, `screens/`, `components/`, `api.js`)
- `knowledge/` -- live data the backend reads at runtime: `prerequisites.csv` (the sub-skill graph) and `notes/` (the RAG corpus)
- `data/` -- the question bank CSVs
- `reference/` -- historical prototypes (Kaveen's and Sri Somesh's original solo scripts), kept for context, not imported by the running app
- `scripts/` -- one-off tooling (question generation, the convergence study)
- `docs/` -- the execution plan
- `tests/` -- pytest suite, runs against the real app/DB/graph, no mocks

## Python backend

```bash
python -m pip install fastapi uvicorn pydantic networkx scikit-learn anthropic python-multipart
python -m uvicorn main:app --host 127.0.0.1 --port 8000
```

The app boots at http://127.0.0.1:8000 and seeds the SQLite database from
`data/question_bank.csv` and the graph data under `knowledge/` on first run.
To reseed manually: `python -m backend.seed_data`.

## Frontend

```bash
npm install
npm run dev
```

Then open the local Vite URL shown in the terminal.

## Personalized learning backend (Teammate 2)

The API now also exposes `/learning/tutor`, `/learning/sessions`, and
`/learning/reviews/{learner_id}` for evidence-aware tutoring, adaptive practice,
fresh verification, and delayed review. See
[the API contract and teammate handoff](docs/teammate-2-learning-contract.md)
for request/response fields, provisional policies, and integration limitations.
The existing frontend still uses the legacy retest flow until these routes are
connected. Learning results do not automatically update mastery.

Scoped retrieval uses a small standard-library TF-IDF index. Missing topic notes
are reported rather than supplemented with unrelated material. Claude generation
is optional (`ANTHROPIC_API_KEY`); without it, the tutor returns labeled excerpts.

## Validation status

The project is currently valid for a first review with these checks passing:

- Backend startup: `uvicorn main:app --host 127.0.0.1 --port 8000`
- Frontend build: `npm run build`
- API smoke test: server responds on the live endpoints under `/health`,
  `/diagnostic/start`, and `/gaps/{learner_id}`
