"""Adaptive practice, fresh verification, and delayed review.

This service produces assessment evidence. It deliberately does not write
mastery_estimates or gaps: the assessment owner decides how evidence changes
mastery. Policy constants below are provisional, not calibrated learning claims.
"""
import json
import math
import uuid
from datetime import datetime, timedelta, timezone

from .database import get_cursor, loads, row_to_question_dict
from .learning_context import learner_context
from .learning_store import init_learning_tables

PASS_FRACTION = 0.6
REVIEW_INTERVALS_DAYS = (1, 3, 7, 14, 30)


class LearningConflict(ValueError):
    pass


def utcnow():
    return datetime.now(timezone.utc)


def _require_topic(cur, sub_skill):
    cur.execute("SELECT 1 FROM sub_skill_graph_nodes WHERE name=?", (sub_skill,))
    if cur.fetchone() is None:
        raise ValueError("Unknown sub_skill")


def _seen_ids(cur, learner_id):
    """Conservatively include assigned legacy items and reserved verification items."""
    cur.execute("SELECT question_id FROM learning_exposures WHERE learner_id=?", (learner_id,))
    seen = {r["question_id"] for r in cur.fetchall()}
    cur.execute("SELECT used_ids FROM diagnostic_sessions WHERE learner_id=?", (learner_id,))
    for row in cur.fetchall():
        seen.update(loads(row["used_ids"]))
    cur.execute("SELECT retest_question_ids FROM tutoring_content WHERE learner_id=?", (learner_id,))
    for row in cur.fetchall():
        seen.update(loads(row["retest_question_ids"]))
    cur.execute("SELECT question_ids FROM learning_sessions WHERE learner_id=?", (learner_id,))
    for row in cur.fetchall():
        seen.update(loads(row["question_ids"]))
    return seen


def _fresh_questions(cur, learner_id, sub_skill, difficulty):
    seen = _seen_ids(cur, learner_id)
    cur.execute("SELECT * FROM questions WHERE sub_skill=? ORDER BY id", (sub_skill,))
    pool = cur.fetchall()
    def normalized(q):
        return " ".join(q["text"].casefold().split())
    seen_texts = {normalized(q) for q in pool if q["id"] in seen}
    # Skip malformed items rather than grading ambiguous or impossible options.
    rows = [r for r in pool if r["id"] not in seen and normalized(r) not in seen_texts
            and r["text"].strip() and all(r[f"option_{c}"].strip() for c in "abcd")
            and len({r[f"option_{c}"] for c in "abcd"}) == 4
            and r["correct_answer"] in {r[f"option_{c}"] for c in "abcd"}]
    unique = []
    for row in sorted(rows, key=lambda q: (abs(q["difficulty"] - difficulty), q["id"])):
        if normalized(row) not in seen_texts:
            unique.append(row)
            seen_texts.add(normalized(row))
    return unique


def _expose(cur, learner_id, question_id, source, at):
    cur.execute("INSERT OR IGNORE INTO learning_exposures VALUES (?,?,?,?)",
                (learner_id, question_id, source, at))


def _session_contract(cur, session):
    result = json.loads(session["result"]) if session["result"] else None
    question = None
    if session["status"] == "in_progress":
        cur.execute("SELECT * FROM questions WHERE id=?", (session["current_question_id"],))
        row = cur.fetchone()
        if row is None:
            raise LearningConflict("Assigned question is no longer available; request content repair.")
        question = row_to_question_dict(row)
        question.pop("correct_answer")
    return {
        "session_id": session["session_id"], "learner_id": session["learner_id"],
        "sub_skill": session["sub_skill"], "purpose": session["purpose"],
        "status": session["status"], "question": question,
        "question_number": len(loads(session["answers"])) + (1 if question else 0),
        "target_count": session["target_count"], "result": result,
    }


def start_learning_session(learner_id, sub_skill, purpose="practice", count=3):
    if not learner_id or not learner_id.strip():
        raise ValueError("learner_id must not be blank")
    if purpose not in ("practice", "verification", "review"):
        raise ValueError("Invalid learning purpose")
    if not isinstance(count, int) or isinstance(count, bool) or not 1 <= count <= 6:
        raise ValueError("count must be between 1 and 6")
    if purpose != "practice" and count != 3:
        raise ValueError("Verification and review require three questions under the provisional policy")
    init_learning_tables()
    context = learner_context(learner_id, sub_skill)
    difficulty = context["suggested_practice_difficulty"] if purpose == "practice" else 3
    now = utcnow()
    with get_cursor(commit=True) as cur:
        cur.execute("BEGIN IMMEDIATE")
        _require_topic(cur, sub_skill)
        cur.execute("SELECT * FROM learning_sessions WHERE learner_id=? AND sub_skill=? AND status='in_progress' ORDER BY rowid DESC LIMIT 1",
                    (learner_id, sub_skill))
        active = cur.fetchone()
        if active:
            if active["purpose"] != purpose:
                raise LearningConflict("Complete or abandon the active learning session first")
            return _session_contract(cur, active)
        if purpose == "review":
            cur.execute("SELECT due_at FROM learning_reviews WHERE learner_id=? AND sub_skill=?", (learner_id, sub_skill))
            review = cur.fetchone()
            if review is None or datetime.fromisoformat(review["due_at"]) > now:
                raise LearningConflict("There is no retention review due for this topic")
        candidates = _fresh_questions(cur, learner_id, sub_skill, difficulty)
        if len(candidates) < count:
            return {
                "status": "insufficient_coverage", "learner_id": learner_id,
                "sub_skill": sub_skill, "purpose": purpose, "session_id": None,
                "required_count": count, "available_fresh_count": len(candidates),
                "message": "Not enough fresh valid questions. Previously exposed questions will not be silently reused.",
            }
        first = candidates[0]
        # Verification/review reserve their whole set before practice can expose it.
        ids = [q["id"] for q in candidates[:count]] if purpose != "practice" else [first["id"]]
        sid = str(uuid.uuid4())
        cur.execute("""INSERT INTO learning_sessions
            (session_id,learner_id,sub_skill,purpose,question_ids,target_count,
             target_difficulty,current_question_id,created_at)
             VALUES (?,?,?,?,?,?,?,?,?)""",
                    (sid, learner_id, sub_skill, purpose, json.dumps(ids), count,
                     first["difficulty"], first["id"], now.isoformat()))
        _expose(cur, learner_id, first["id"], purpose, now.isoformat())
        cur.execute("SELECT * FROM learning_sessions WHERE session_id=?", (sid,))
        return _session_contract(cur, cur.fetchone())


def get_learning_session(learner_id, session_id):
    init_learning_tables()
    with get_cursor() as cur:
        cur.execute("SELECT * FROM learning_sessions WHERE session_id=? AND learner_id=?", (session_id, learner_id))
        session = cur.fetchone()
        if session is None:
            raise LookupError("Learning session not found")
        return _session_contract(cur, session)


def abandon_learning_session(learner_id, session_id):
    init_learning_tables()
    with get_cursor(commit=True) as cur:
        cur.execute("BEGIN IMMEDIATE")
        cur.execute("SELECT * FROM learning_sessions WHERE session_id=? AND learner_id=?", (session_id, learner_id))
        session = cur.fetchone()
        if session is None:
            raise LookupError("Learning session not found")
        if session["status"] != "in_progress":
            raise LearningConflict("This session is no longer active")
        # Reservations remain excluded: abandonment cannot be used to recycle items.
        cur.execute("UPDATE learning_sessions SET status='abandoned', current_question_id=NULL, completed_at=? WHERE session_id=?",
                    (utcnow().isoformat(), session_id))
    return {"session_id": session_id, "status": "abandoned"}


def _schedule_review(cur, learner_id, sub_skill, purpose, passed, now):
    cur.execute("SELECT * FROM learning_reviews WHERE learner_id=? AND sub_skill=?", (learner_id, sub_skill))
    previous = cur.fetchone()
    if purpose == "verification" and passed and previous:
        # Repeating an immediate check must not postpone an already scheduled review.
        return {"due_at": previous["due_at"], "interval_days": previous["interval_days"]}
    successes = previous["successful_reviews"] if previous else 0
    successes = successes + 1 if purpose == "review" and passed else 0
    interval = REVIEW_INTERVALS_DAYS[min(successes, len(REVIEW_INTERVALS_DAYS) - 1)]
    due = (now + timedelta(days=interval)).isoformat()
    cur.execute("""INSERT INTO learning_reviews VALUES (?,?,?,?,?,?,?)
        ON CONFLICT(learner_id,sub_skill) DO UPDATE SET due_at=excluded.due_at,
        interval_days=excluded.interval_days,successful_reviews=excluded.successful_reviews,
        last_outcome=excluded.last_outcome,updated_at=excluded.updated_at""",
                (learner_id, sub_skill, due, interval, successes, "passed" if passed else "failed", now.isoformat()))
    return {"due_at": due, "interval_days": interval}


def submit_learning_answer(learner_id, session_id, question_id, selected_answer):
    init_learning_tables()
    now = utcnow()
    with get_cursor(commit=True) as cur:
        cur.execute("BEGIN IMMEDIATE")
        cur.execute("SELECT * FROM learning_sessions WHERE session_id=? AND learner_id=?", (session_id, learner_id))
        session = cur.fetchone()
        if session is None:
            raise LookupError("Learning session not found")
        if session["status"] != "in_progress":
            raise LearningConflict("Session is already complete or abandoned")
        if question_id != session["current_question_id"]:
            raise LearningConflict("Answer does not match the current assigned question")
        cur.execute("SELECT * FROM questions WHERE id=? AND sub_skill=?", (question_id, session["sub_skill"]))
        q = cur.fetchone()
        if q is None:
            raise LearningConflict("Assigned question is no longer available")
        if selected_answer not in [q[f"option_{c}"] for c in "abcd"]:
            raise ValueError("Selected answer must be one of the assigned question's options")
        correct = selected_answer == q["correct_answer"]
        answers = loads(session["answers"])
        answers.append({"question_id": question_id, "selected_answer": selected_answer,
                        "correct": correct, "difficulty": q["difficulty"], "answered_at": now.isoformat()})
        ids = loads(session["question_ids"])
        next_q = None
        incomplete = False
        if len(answers) < session["target_count"]:
            if session["purpose"] == "practice":
                target = max(1, min(5, q["difficulty"] + (1 if correct else -1)))
                candidates = _fresh_questions(cur, learner_id, session["sub_skill"], target)
                next_q = candidates[0] if candidates else None
                if next_q:
                    ids.append(next_q["id"])
            else:
                cur.execute("SELECT * FROM questions WHERE id=? AND sub_skill=?", (ids[len(answers)], session["sub_skill"]))
                next_q = cur.fetchone()
            incomplete = next_q is None

        feedback = None
        if session["purpose"] == "practice":
            feedback = {"correct": correct, "expected_answer": q["correct_answer"],
                        "next_step": "Try the next question" if correct else "Review the worked example before continuing"}
        result = None
        if next_q is None:
            n_correct = sum(a["correct"] for a in answers)
            passed = None if incomplete else n_correct >= math.ceil(session["target_count"] * PASS_FRACTION)
            cur.execute("SELECT result FROM learning_sessions WHERE learner_id=? AND sub_skill=? AND status='complete' ORDER BY rowid DESC LIMIT 1",
                        (learner_id, session["sub_skill"]))
            prior = cur.fetchone()
            prior_failed = bool(prior and json.loads(prior["result"]).get("passed") is False)
            action = "verify_with_fresh_questions" if session["purpose"] == "practice" and passed else "return_for_review" if passed else "review_explanation"
            if not passed and prior_failed:
                cur.execute("SELECT from_skill FROM sub_skill_graph_edges WHERE to_skill=? ORDER BY from_skill", (session["sub_skill"],))
                prereqs = [r["from_skill"] for r in cur.fetchall()]
                action = "assess_prerequisites" if prereqs else "simpler_worked_example"
            else:
                prereqs = []
            review = None
            if not incomplete and session["purpose"] in ("verification", "review"):
                review = _schedule_review(cur, learner_id, session["sub_skill"], session["purpose"], passed, now)
                if not passed:
                    action = "review_explanation" if not prior_failed else action
            result = {
                "n_correct": n_correct, "n_total": len(answers),
                "accuracy": n_correct / len(answers), "passed": passed,
                "purpose": session["purpose"], "next_action": "request_content" if incomplete else action,
                "prerequisites_to_check": prereqs, "review": review,
                "coverage": "insufficient" if incomplete else "fresh_questions",
                "mastery_updated": False,
                "policy": {"pass_fraction": PASS_FRACTION, "status": "provisional_not_calibrated"},
            }
        status = "in_progress" if next_q else "insufficient_coverage" if incomplete else "complete"
        cur.execute("""UPDATE learning_sessions SET answers=?,question_ids=?,status=?,
            current_question_id=?,target_difficulty=?,completed_at=?,result=? WHERE session_id=?""",
                    (json.dumps(answers), json.dumps(ids), status, next_q["id"] if next_q else None,
                     next_q["difficulty"] if next_q else q["difficulty"], None if next_q else now.isoformat(),
                     json.dumps(result) if result else None, session_id))
        if next_q:
            _expose(cur, learner_id, next_q["id"], session["purpose"], now.isoformat())
        cur.execute("SELECT * FROM learning_sessions WHERE session_id=?", (session_id,))
        response = _session_contract(cur, cur.fetchone())
        response["feedback"] = feedback
        return response


def list_reviews(learner_id):
    init_learning_tables()
    now = utcnow()
    with get_cursor() as cur:
        cur.execute("SELECT * FROM learning_reviews WHERE learner_id=? ORDER BY due_at,sub_skill", (learner_id,))
        return [{**dict(r), "due": datetime.fromisoformat(r["due_at"]) <= now} for r in cur.fetchall()]
