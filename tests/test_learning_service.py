"""Learning policies exercised against an isolated SQLite database and synthetic items."""
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import pytest

from backend import learning_service as learning
from backend.database import get_cursor
from backend.learning_context import learner_context
from backend.learning_store import init_learning_tables


@pytest.fixture()
def learning_bank(test_db):
    init_learning_tables()
    with get_cursor(commit=True) as cur:
        cur.executemany("INSERT INTO sub_skill_graph_nodes VALUES (?)", [("Percentages",), ("Number Systems",)])
        cur.execute("INSERT INTO sub_skill_graph_edges VALUES ('Number Systems','Percentages')")
        cur.executemany("INSERT INTO questions VALUES (?,?,?,?,?,?,?,?,?)", [
            (f"p_{d}_{i:02}", "Percentages", d, f"Synthetic percentage question {d}-{i}", "A", "B", "C", "D", "A")
            for d in range(1, 6) for i in range(10)
        ])
    return test_db


def finish(session, correct=True):
    while session["status"] == "in_progress":
        session = learning.submit_learning_answer(session["learner_id"], session["session_id"],
                                                  session["question"]["id"], "A" if correct else "B")
    return session


def test_practice_adapts_using_actual_difficulty_and_records_answer(learning_bank):
    session = learning.start_learning_session("alice", "Percentages")
    assert session["question"]["difficulty"] == 3
    assert "correct_answer" not in session["question"]
    response = learning.submit_learning_answer("alice", session["session_id"], session["question"]["id"], "A")
    assert response["question"]["difficulty"] == 4
    assert response["feedback"]["correct"] is True
    response = learning.submit_learning_answer("alice", response["session_id"], response["question"]["id"], "B")
    assert response["question"]["difficulty"] == 3
    context = learner_context("alice", "Percentages")
    assert context["observed_errors"][0]["selected_answer"] == "B"
    assert context["observed_errors"][0]["difficulty"] == 4


def test_practice_does_not_certify_mastery_or_schedule_retention(learning_bank):
    result = finish(learning.start_learning_session("alice", "Percentages"))["result"]
    assert result["passed"] is True
    assert result["next_action"] == "verify_with_fresh_questions"
    assert result["mastery_updated"] is False
    assert learning.list_reviews("alice") == []
    with get_cursor() as cur:
        assert cur.execute("SELECT COUNT(*) FROM mastery_estimates").fetchone()[0] == 0


def test_verification_excludes_diagnostics_legacy_retests_and_practice(learning_bank):
    with get_cursor(commit=True) as cur:
        cur.execute("INSERT INTO diagnostic_sessions VALUES ('diag','alice','Percentages',3,?,'[]','p_3_00','complete')", (json.dumps(["p_3_00"]),))
        cur.execute("INSERT INTO tutoring_content VALUES ('gap','alice','Percentages','notes','[]',?,'extractive_fallback')", (json.dumps(["p_3_01"]),))
    practice = finish(learning.start_learning_session("alice", "Percentages"))
    with get_cursor() as cur:
        seen = learning._seen_ids(cur, "alice")
    verification = learning.start_learning_session("alice", "Percentages", "verification")
    assert verification["question"]["id"] not in seen
    while verification["status"] == "in_progress":
        assert verification["question"]["id"] not in seen
        verification = learning.submit_learning_answer("alice", verification["session_id"], verification["question"]["id"], "A")
        assert verification["feedback"] is None
    assert verification["result"]["passed"] is True
    assert verification["result"]["review"]["interval_days"] == 1
    assert practice["session_id"] != verification["session_id"]


def test_retest_accuracy_is_actual_and_not_written_as_mastery(learning_bank):
    session = learning.start_learning_session("alice", "Percentages", "verification")
    for answer in ["A", "A", "B"]:
        session = learning.submit_learning_answer("alice", session["session_id"], session["question"]["id"], answer)
    assert session["result"]["accuracy"] == 2 / 3
    assert session["result"]["passed"] is True
    assert session["result"]["mastery_updated"] is False


def test_session_resume_is_stable_and_other_learner_cannot_read_or_answer(learning_bank):
    session = learning.start_learning_session("alice", "Percentages")
    assert learning.start_learning_session("alice", "Percentages")["session_id"] == session["session_id"]
    assert learning.get_learning_session("alice", session["session_id"]) == session
    with pytest.raises(LookupError):
        learning.get_learning_session("bob", session["session_id"])
    with pytest.raises(LookupError):
        learning.submit_learning_answer("bob", session["session_id"], session["question"]["id"], "A")
    with pytest.raises(learning.LearningConflict):
        learning.start_learning_session("alice", "Percentages", "verification")


def test_wrong_question_invalid_option_and_replay_do_not_change_state(learning_bank):
    session = learning.start_learning_session("alice", "Percentages")
    with pytest.raises(learning.LearningConflict):
        learning.submit_learning_answer("alice", session["session_id"], "p_1_00", "A")
    with pytest.raises(ValueError):
        learning.submit_learning_answer("alice", session["session_id"], session["question"]["id"], "not an option")
    assert learning.get_learning_session("alice", session["session_id"]) == session
    response = learning.submit_learning_answer("alice", session["session_id"], session["question"]["id"], "A")
    with pytest.raises(learning.LearningConflict):
        learning.submit_learning_answer("alice", session["session_id"], session["question"]["id"], "A")
    assert learning.get_learning_session("alice", session["session_id"])["question"] == response["question"]
    final = finish(response)
    with pytest.raises(learning.LearningConflict):
        learning.submit_learning_answer("alice", final["session_id"], session["question"]["id"], "A")


def test_concurrent_starts_resume_one_session(learning_bank):
    with ThreadPoolExecutor(max_workers=2) as executor:
        sessions = list(executor.map(lambda _: learning.start_learning_session("alice", "Percentages"), range(2)))
    assert sessions[0]["session_id"] == sessions[1]["session_id"]


def test_concurrent_answer_cannot_grade_the_next_question(learning_bank):
    session = learning.start_learning_session("alice", "Percentages")

    def answer(_):
        try:
            return learning.submit_learning_answer("alice", session["session_id"], session["question"]["id"], "A")["status"]
        except learning.LearningConflict:
            return "conflict"

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(answer, range(2)))
    assert sorted(results) == ["conflict", "in_progress"]
    with get_cursor() as cur:
        answers = json.loads(cur.execute("SELECT answers FROM learning_sessions").fetchone()[0])
    assert len(answers) == 1


def test_coverage_shortage_does_not_recycle_seen_questions(learning_bank):
    with get_cursor(commit=True) as cur:
        cur.execute("DELETE FROM questions WHERE id NOT IN ('p_3_00','p_3_01')")
    response = learning.start_learning_session("alice", "Percentages", "verification")
    assert response["status"] == "insufficient_coverage"
    assert response["available_fresh_count"] == 2
    assert response["session_id"] is None
    with get_cursor() as cur:
        assert cur.execute("SELECT COUNT(*) FROM learning_exposures").fetchone()[0] == 0


def test_duplicate_text_and_invalid_items_do_not_count_as_fresh_coverage(learning_bank):
    with get_cursor(commit=True) as cur:
        cur.execute("UPDATE questions SET text='Same question'")
    response = learning.start_learning_session("alice", "Percentages", "verification")
    assert response["available_fresh_count"] == 1
    with get_cursor(commit=True) as cur:
        cur.execute("UPDATE questions SET correct_answer='missing option'")
    assert learning.start_learning_session("alice", "Percentages")["available_fresh_count"] == 0


def test_abandonment_preserves_exposure_and_reservations(learning_bank):
    session = learning.start_learning_session("alice", "Percentages", "verification")
    with get_cursor() as cur:
        reserved = learning._seen_ids(cur, "alice")
    learning.abandon_learning_session("alice", session["session_id"])
    next_session = learning.start_learning_session("alice", "Percentages")
    assert next_session["question"]["id"] not in reserved


def test_repeated_failure_changes_strategy_and_recommends_prerequisite_check(learning_bank):
    finish(learning.start_learning_session("alice", "Percentages"), correct=False)
    result = finish(learning.start_learning_session("alice", "Percentages"), correct=False)["result"]
    assert result["next_action"] == "assess_prerequisites"
    assert result["prerequisites_to_check"] == ["Number Systems"]
    context = learner_context("alice", "Percentages")
    assert context["teaching_strategy"] == "revisit_prerequisites"
    assert context["prerequisites"][0]["assessed"] is False
    assert context["suggested_practice_difficulty"] == 1


def test_due_reviews_use_clock_and_extend_only_after_delayed_success(learning_bank, monkeypatch):
    now = datetime(2026, 10, 15, 10, tzinfo=timezone.utc)
    monkeypatch.setattr(learning, "utcnow", lambda: now)
    result = finish(learning.start_learning_session("alice", "Percentages", "verification"))["result"]
    assert result["review"]["due_at"] == (now + timedelta(days=1)).isoformat()
    assert learning.list_reviews("alice")[0]["due"] is False
    with pytest.raises(learning.LearningConflict):
        learning.start_learning_session("alice", "Percentages", "review")
    now += timedelta(days=1)
    assert learning.list_reviews("alice")[0]["due"] is True
    result = finish(learning.start_learning_session("alice", "Percentages", "review"))["result"]
    assert result["review"]["interval_days"] == 3
    now += timedelta(days=3)
    result = finish(learning.start_learning_session("alice", "Percentages", "review"), correct=False)["result"]
    assert result["review"]["interval_days"] == 1


def test_reverification_does_not_postpone_due_review(learning_bank, monkeypatch):
    now = datetime(2026, 10, 15, 10, tzinfo=timezone.utc)
    monkeypatch.setattr(learning, "utcnow", lambda: now)
    first = finish(learning.start_learning_session("alice", "Percentages", "verification"))["result"]
    now += timedelta(hours=20)
    second = finish(learning.start_learning_session("alice", "Percentages", "verification"))["result"]
    assert second["review"] == first["review"]


@pytest.mark.parametrize("purpose,count", [("bad", 3), ("practice", 0), ("practice", 7), ("verification", 1), ("review", 6)])
def test_invalid_session_requests_rejected(learning_bank, purpose, count):
    with pytest.raises(ValueError):
        learning.start_learning_session("alice", "Percentages", purpose, count)


def test_content_disappearing_mid_practice_does_not_produce_pass(learning_bank):
    session = learning.start_learning_session("alice", "Percentages")
    with get_cursor(commit=True) as cur:
        cur.execute("DELETE FROM questions WHERE id != ?", (session["question"]["id"],))
    response = learning.submit_learning_answer("alice", session["session_id"], session["question"]["id"], "A")
    assert response["status"] == "insufficient_coverage"
    assert response["result"]["passed"] is None
    assert response["result"]["next_action"] == "request_content"


@pytest.fixture()
def learning_client(learning_bank, monkeypatch):
    from fastapi.testclient import TestClient
    from main import app

    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with TestClient(app) as client:
        yield client


def test_http_learning_journey_and_resume(learning_client):
    response = learning_client.post("/learning/sessions", json={"learner_id": "alice", "sub_skill": "Percentages"})
    assert response.status_code == 200
    session = response.json()
    assert learning_client.get(f"/learning/sessions/{session['session_id']}?learner_id=alice").json() == session
    assert learning_client.get(f"/learning/sessions/{session['session_id']}?learner_id=bob").status_code == 404
    for _ in range(3):
        response = learning_client.post(f"/learning/sessions/{session['session_id']}/answer", json={
            "learner_id": "alice", "question_id": session["question"]["id"], "selected_answer": "B",
        })
        assert response.status_code == 200
        session = response.json()
    tutor = learning_client.post("/learning/tutor", json={"learner_id": "alice", "sub_skill": "Percentages"}).json()
    assert tutor["diagnosis"]["status"] == "observed_errors"
    assert tutor["diagnosis"]["misconception"] is None
    assert tutor["learner_context"]["observed_errors"][0]["selected_answer"] == "B"
    assert tutor["worked_example"] is not None
    assert tutor["generated_by"] == "extractive_fallback"
    verification = learning_client.post("/learning/sessions", json={
        "learner_id": "alice", "sub_skill": "Percentages", "purpose": "verification",
    }).json()
    for _ in range(3):
        verification = learning_client.post(f"/learning/sessions/{verification['session_id']}/answer", json={
            "learner_id": "alice", "question_id": verification["question"]["id"], "selected_answer": "A",
        }).json()
    assert verification["result"]["passed"] is True
    assert learning_client.get("/learning/reviews/alice").json()[0]["interval_days"] == 1
    assert learning_client.get("/learning/reviews/bob").json() == []


def test_http_rejects_invalid_requests_without_exposing_answers(learning_client):
    for payload in [
        {"learner_id": " ", "sub_skill": "Percentages"},
        {"learner_id": "alice", "sub_skill": "Percentages", "purpose": "anything"},
        {"learner_id": "alice", "sub_skill": "Percentages", "count": True},
    ]:
        assert learning_client.post("/learning/sessions", json=payload).status_code == 422
    assert learning_client.post("/learning/tutor", json={"learner_id": "alice", "sub_skill": "missing"}).status_code == 400
    session = learning_client.post("/learning/sessions", json={"learner_id": "alice", "sub_skill": "Percentages"}).json()
    assert "correct_answer" not in session["question"]
    assert learning_client.post(f"/learning/sessions/{session['session_id']}/answer", json={
        "learner_id": "alice", "question_id": "unassigned", "selected_answer": "A",
    }).status_code == 409
    assert learning_client.post(f"/learning/sessions/{session['session_id']}/abandon", json={"learner_id": "bob"}).status_code == 404
    assert learning_client.post(f"/learning/sessions/{session['session_id']}/abandon", json={"learner_id": "alice"}).status_code == 200


def test_tutor_without_history_does_not_invent_diagnosis(learning_client):
    body = learning_client.post("/learning/tutor", json={"learner_id": "new", "sub_skill": "Percentages"}).json()
    assert body["diagnosis"]["status"] == "insufficient_evidence"
    assert body["learner_context"]["observed_errors"] == []
    assert body["teaching_strategy"] == "check_understanding"
