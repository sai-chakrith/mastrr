# -*- coding: utf-8 -*-
"""
main.py -- Mastrr's API layer (Track 3, Step 1 + integration).

The execution plan's Step 1 says to stand these five endpoints up with
hardcoded fake JSON so the dashboard isn't blocked. That mock-first step
made sense in Week 1, before Kaveen's diagnostic_engine.py and Sri
Somesh's build_graph.py existed -- they do now (both are in this repo),
so every endpoint below calls the real thing instead of a fake one:

    /diagnostic/start, /diagnostic/answer  -> live version of Kaveen's
                                               staircase rule (diagnostic_session.py)
    /gaps/{learner_id}                     -> Kaveen's real detect_gaps()
                                               + Sri Somesh's real graph order
    /tutor                                 -> real TF-IDF retrieval over
                                               knowledge/notes/, real LLM call
                                               if ANTHROPIC_API_KEY is set
    /retest                                -> grades against the real
                                               question bank, closes the loop
    /roadmap/{learner_id}                  -> full graph + per-node status
                                               + current-focus, for the
                                               dashboard's Roadmap screen

Run with:  uvicorn main:app --reload
"""
import json
from datetime import datetime, timezone
from typing import List, Optional

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from backend import diagnostic_session, gaps_service
from backend.database import get_cursor, init_db, is_seeded, loads
from backend.graph_utils import graph_as_contract, load_graph
from backend.seed_data import seed_all
from backend.tutor import generate_tutoring_explanation
from backend.learning_api import router as learning_router
from backend.learning_store import init_learning_tables

app = FastAPI(title="Mastrr API", version="0.1.0")
app.include_router(learning_router)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # dev-only -- narrow this before any real deployment
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
def on_startup():
    init_db()
    init_learning_tables()
    if not is_seeded():
        seed_all()


# ---------------------------------------------------------------------------
# request/response bodies not already covered by schemas.py
# ---------------------------------------------------------------------------
class StartDiagnosticRequest(BaseModel):
    learner_id: str
    sub_skill: str


class AnswerDiagnosticRequest(BaseModel):
    session_id: str
    selected_answer: str


class TutorRequest(BaseModel):
    learner_id: str
    sub_skill: str


class RetestAnswer(BaseModel):
    question_id: str
    selected_answer: str


class RetestRequest(BaseModel):
    gap_id: str
    answers: List[RetestAnswer]


MASTERY_THRESHOLD = 0.5


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _append_learner_history(learner_id: str, event: dict):
    with get_cursor() as cur:
        cur.execute("SELECT stage, history FROM learner_state WHERE learner_id = ?", (learner_id,))
        row = cur.fetchone()
        history = loads(row["history"]) if row else []
        stage = row["stage"] if row else "not_started"
    history.append({**event, "at": _now()})
    with get_cursor(commit=True) as cur:
        cur.execute(
            """INSERT INTO learner_state (learner_id, stage, history) VALUES (?, ?, ?)
               ON CONFLICT(learner_id) DO UPDATE SET history=excluded.history, stage=excluded.stage""",
            (learner_id, event.get("stage", stage), json.dumps(history)),
        )


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/questions/batch")
def questions_batch(ids: str):
    """Small supporting endpoint (not one of the plan's five, but the
    dashboard's retest screen needs to render the retest questions
    themselves, and /tutor only hands back their ids). Never returns
    correct_answer -- same rule as the live diagnostic."""
    id_list = [i for i in ids.split(",") if i]
    if not id_list:
        return []
    with get_cursor() as cur:
        placeholders = ",".join("?" for _ in id_list)
        cur.execute(f"SELECT * FROM questions WHERE id IN ({placeholders})", tuple(id_list))
        rows = {r["id"]: r for r in cur.fetchall()}
    from backend.database import row_to_question_dict
    out = []
    for qid in id_list:
        if qid in rows:
            q = row_to_question_dict(rows[qid])
            q.pop("correct_answer")
            out.append(q)
    return out


# ---------------------------------------------------------------------------
# /diagnostic -- live staircase test (Checkpoint 2: real Kaveen engine)
# ---------------------------------------------------------------------------
@app.post("/diagnostic/start")
def diagnostic_start(body: StartDiagnosticRequest):
    try:
        result = diagnostic_session.start_session(body.learner_id, body.sub_skill)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    _append_learner_history(
        body.learner_id, {"event": "diagnostic_started", "sub_skill": body.sub_skill, "stage": "diagnosing"}
    )
    return result


@app.post("/diagnostic/answer")
def diagnostic_answer(body: AnswerDiagnosticRequest):
    try:
        result = diagnostic_session.submit_answer(body.session_id, body.selected_answer)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    if result["done"]:
        est = result["mastery_estimate"]
        with get_cursor(commit=True) as cur:
            cur.execute(
                """INSERT INTO mastery_estimates (learner_id, sub_skill, score, confidence, n_attempted, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT(learner_id, sub_skill) DO UPDATE SET
                     score=excluded.score, confidence=excluded.confidence,
                     n_attempted=excluded.n_attempted, updated_at=excluded.updated_at""",
                (est["learner_id"], est["sub_skill"], est["score"], est["confidence"],
                 est["n_attempted"], _now()),
            )
        gaps_service.recompute_gaps(est["learner_id"])
        _append_learner_history(
            est["learner_id"],
            {"event": "diagnostic_completed", "sub_skill": est["sub_skill"], "score": est["score"],
             "stage": "reviewing_roadmap"},
        )
    return result


# ---------------------------------------------------------------------------
# /gaps -- Kaveen's real detect_gaps() + Sri Somesh's real topological order
# ---------------------------------------------------------------------------
@app.get("/gaps/{learner_id}")
def get_gaps(learner_id: str):
    gaps = gaps_service.recompute_gaps(learner_id)
    return gaps


# ---------------------------------------------------------------------------
# /tutor -- real retrieval over knowledge/notes/, real (or fallback) generation
# ---------------------------------------------------------------------------
@app.post("/tutor")
def tutor(body: TutorRequest):
    gap_id = f"{body.learner_id}_{body.sub_skill}"

    with get_cursor() as cur:
        cur.execute(
            "SELECT * FROM gaps WHERE learner_id = ? AND sub_skill = ?",
            (body.learner_id, body.sub_skill),
        )
        gap = cur.fetchone()
    if gap is None:
        raise HTTPException(
            status_code=404,
            detail="No gap on record for this learner/sub_skill -- run /gaps first.",
        )

    generated = generate_tutoring_explanation(body.sub_skill, learner_id=body.learner_id)

    with get_cursor() as cur:
        cur.execute(
            "SELECT id FROM questions WHERE sub_skill = ? ORDER BY difficulty ASC LIMIT 3",
            (body.sub_skill,),
        )
        retest_ids = [r["id"] for r in cur.fetchall()]

    with get_cursor(commit=True) as cur:
        cur.execute(
            """INSERT INTO tutoring_content
               (gap_id, learner_id, sub_skill, explanation, source_chunks, retest_question_ids, generated_by)
               VALUES (?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(gap_id) DO UPDATE SET
                 explanation=excluded.explanation, source_chunks=excluded.source_chunks,
                 retest_question_ids=excluded.retest_question_ids, generated_by=excluded.generated_by""",
            (gap_id, body.learner_id, body.sub_skill, generated["explanation"],
             json.dumps(generated["source_chunks"]), json.dumps(retest_ids), generated["generated_by"]),
        )
        cur.execute(
            "UPDATE gaps SET status = 'in_tutoring' WHERE learner_id = ? AND sub_skill = ?",
            (body.learner_id, body.sub_skill),
        )

    _append_learner_history(
        body.learner_id, {"event": "tutoring_generated", "sub_skill": body.sub_skill, "stage": "tutoring"}
    )

    return {
        "gap_id": gap_id,
        "sub_skill": body.sub_skill,
        "explanation": generated["explanation"],
        "source_chunks": generated["source_chunks"],
        "retest_question_ids": retest_ids,
        "generated_by": generated["generated_by"],
    }


# ---------------------------------------------------------------------------
# /retest -- closes the loop: verify a gap is actually fixed
# ---------------------------------------------------------------------------
@app.post("/retest")
def retest(body: RetestRequest):
    with get_cursor() as cur:
        cur.execute("SELECT * FROM tutoring_content WHERE gap_id = ?", (body.gap_id,))
        tc = cur.fetchone()
    if tc is None:
        raise HTTPException(status_code=404, detail="No tutoring content on record for this gap_id")

    learner_id, sub_skill = tc["learner_id"], tc["sub_skill"]
    answer_map = {a.question_id: a.selected_answer for a in body.answers}

    with get_cursor() as cur:
        placeholders = ",".join("?" for _ in answer_map) or "''"
        cur.execute(f"SELECT id, correct_answer FROM questions WHERE id IN ({placeholders})",
                    tuple(answer_map.keys()))
        correct_answers = {r["id"]: r["correct_answer"] for r in cur.fetchall()}

    n_correct = sum(
        1 for qid, given in answer_map.items()
        if qid in correct_answers and str(given).strip() == str(correct_answers[qid]).strip()
    )
    n_total = len(answer_map)
    passed = n_total > 0 and (n_correct / n_total) >= 0.6

    new_status = "resolved" if passed else "in_tutoring"
    with get_cursor(commit=True) as cur:
        cur.execute(
            "UPDATE gaps SET status = ? WHERE learner_id = ? AND sub_skill = ?",
            (new_status, learner_id, sub_skill),
        )
        if passed:
            # Bring mastery_estimates back in line with the gap we just resolved.
            # Without this, gaps_service.recompute_gaps() -- which GET /gaps and
            # every future diagnostic run rely on -- still sees the old, failing
            # diagnostic score, decides the sub-skill is still weak, and silently
            # regresses this gap straight back to "detected" on the next call.
            # mastery_estimates, not gaps.status, is the source of truth that
            # recompute_gaps reads, so a passed retest has to update it too.
            cur.execute(
                """INSERT INTO mastery_estimates (learner_id, sub_skill, score, confidence, n_attempted, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT(learner_id, sub_skill) DO UPDATE SET
                     score=excluded.score, confidence=excluded.confidence,
                     n_attempted=excluded.n_attempted, updated_at=excluded.updated_at""",
                (learner_id, sub_skill, 1.0, 1.0, n_total, _now()),
            )

    _append_learner_history(
        learner_id,
        {"event": "retest_submitted", "sub_skill": sub_skill, "passed": passed,
         "score": f"{n_correct}/{n_total}", "stage": "reviewing_roadmap"},
    )

    return {
        "gap_id": body.gap_id,
        "sub_skill": sub_skill,
        "n_correct": n_correct,
        "n_total": n_total,
        "passed": passed,
        "status": new_status,
    }


# ---------------------------------------------------------------------------
# /roadmap -- full graph + per-node status + current focus, for the dashboard
# ---------------------------------------------------------------------------
@app.get("/roadmap/{learner_id}")
def roadmap(learner_id: str):
    G = load_graph()
    graph_contract = graph_as_contract(G)

    with get_cursor() as cur:
        cur.execute("SELECT * FROM mastery_estimates WHERE learner_id = ?", (learner_id,))
        mastery_by_skill = {r["sub_skill"]: dict(r) for r in cur.fetchall()}
        cur.execute("SELECT * FROM gaps WHERE learner_id = ?", (learner_id,))
        gaps_by_skill = {r["sub_skill"]: dict(r) for r in cur.fetchall()}
        cur.execute("SELECT stage, history FROM learner_state WHERE learner_id = ?", (learner_id,))
        state_row = cur.fetchone()

    nodes = []
    for name in graph_contract["nodes"]:
        gap = gaps_by_skill.get(name)
        mastery = mastery_by_skill.get(name)
        if gap is not None:
            if gap["status"] == "resolved":
                status = "mastered"
            elif gap["status"] == "in_tutoring":
                status = "gap_in_tutoring"
            else:
                status = "gap_unresolved"
        elif mastery is not None:
            status = "mastered" if mastery["score"] >= MASTERY_THRESHOLD else "gap_unresolved"
        else:
            status = "not_tested"
        nodes.append({"id": name, "status": status})

    active_gaps = sorted(
        (g for g in gaps_by_skill.values() if g["status"] in ("detected", "in_tutoring")),
        key=lambda g: (g["priority_rank"] is None, g["priority_rank"]),
    )
    if active_gaps:
        top = active_gaps[0]
        with get_cursor() as cur:
            cur.execute("SELECT retest_question_ids FROM tutoring_content WHERE gap_id = ?",
                        (f"{learner_id}_{top['sub_skill']}",))
            tc = cur.fetchone()
        if top["status"] == "in_tutoring" and tc:
            n_q = len(loads(tc["retest_question_ids"]))
            action = f"Retake a {n_q}-question check on {top['sub_skill']}"
        else:
            action = f"Review: {top['sub_skill']}, then start tutoring"
        current_focus = {"sub_skill": top["sub_skill"], "priority_rank": top["priority_rank"], "action": action}
    else:
        current_focus = None

    return {
        "learner_id": learner_id,
        "stage": state_row["stage"] if state_row else "not_started",
        "history": loads(state_row["history"]) if state_row else [],
        "graph": {"nodes": nodes, "edges": graph_contract["edges"]},
        "gaps": sorted(gaps_by_skill.values(), key=lambda g: (g["priority_rank"] is None, g["priority_rank"])),
        "current_focus": current_focus,
    }
