"""Homework & practice APIs: choice / video / vocab / voice."""
from __future__ import annotations

import json
import os
import tempfile
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from urllib.parse import quote

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from pydantic import BaseModel, Field

from backend.notify import (
    create_notification,
    notify_assignment_published,
    notify_assignment_urged,
    notify_parents_of_student,
    notify_users,
)
from backend.pronunciation import assess_audio, provider_status
from backend.storage import UPLOAD_DIR, ensure_local_dirs, save_bytes

# 业务默认按中国时区理解截止时间（与 MySQL session time_zone 对齐）
from datetime import timezone, timedelta
CST = timezone(timedelta(hours=8))

ensure_local_dirs()
VOICE_DIR = UPLOAD_DIR / "voice"

router = APIRouter(tags=["homework"])

TASK_TYPES = {"choice", "video", "vocab", "voice", "listen", "dictation"}


def require_db(mysql_connection):
    connection = mysql_connection()
    if not connection:
        raise HTTPException(status_code=503, detail="MySQL is required for homework features")
    return connection


def parse_json(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, (dict, list)):
        return value
    return json.loads(value)


def require_role(user: dict, *roles: str):
    if user.get("role") not in roles:
        raise HTTPException(status_code=403, detail="insufficient role")


def close_overdue_assignments(cursor) -> int:
    """将已过截止时间的已发布作业自动关闭。"""
    try:
        cursor.execute("SET time_zone = '+08:00'")
    except Exception:
        pass
    cursor.execute(
        "UPDATE assignments SET status='closed' "
        "WHERE status='published' AND due_at IS NOT NULL AND due_at < NOW()"
    )
    return cursor.rowcount or 0


def apply_overdue_close(mysql_connection, cursor, connection) -> int:
    """Analytics/curriculum helper: close overdue and commit if any rows changed."""
    n = close_overdue_assignments(cursor)
    if n and connection is not None:
        connection.commit()
    return n


def assert_assignment_open(assignment: dict | None):
    if not assignment:
        raise HTTPException(status_code=404, detail="assignment not available")
    if assignment["status"] == "closed":
        raise HTTPException(status_code=400, detail="assignment closed (past due date)")
    if assignment["status"] != "published":
        raise HTTPException(status_code=404, detail="assignment not available")


def now_cst_naive() -> datetime:
    return datetime.now(CST).replace(tzinfo=None)


def sync_assignment_class_members(cursor, assignment_id: int, class_id: int) -> int:
    """发布/补同步时，把班级当前学员写入作业指派表。"""
    cursor.execute(
        "SELECT user_id AS id FROM class_members WHERE class_id=%s AND member_role='student'",
        (class_id,),
    )
    linked = 0
    for row in cursor.fetchall():
        cursor.execute(
            "INSERT IGNORE INTO assignment_students(assignment_id, student_id) VALUES(%s,%s)",
            (assignment_id, int(row["id"])),
        )
        linked += cursor.rowcount
    return linked


class WordCreate(BaseModel):
    word: str = Field(min_length=1, max_length=100)
    phonetic: str | None = Field(default=None, max_length=100)
    meaning: str = Field(min_length=1, max_length=255)
    example_sentence: str | None = Field(default=None, max_length=500)


class QuestionCreate(BaseModel):
    stem: str = Field(min_length=1)
    option_a: str = Field(min_length=1, max_length=255)
    option_b: str = Field(min_length=1, max_length=255)
    option_c: str = Field(min_length=1, max_length=255)
    option_d: str = Field(min_length=1, max_length=255)
    answer: str = Field(pattern="^[ABCD]$")
    explanation: str | None = Field(default=None, max_length=500)


class TaskCreate(BaseModel):
    task_type: str = Field(pattern="^(choice|video|vocab|voice|listen|dictation)$")
    title: str = Field(min_length=1, max_length=200)
    config: dict[str, Any] = Field(default_factory=dict)


class AssignmentCreate(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    description: str | None = None
    due_at: datetime | None = None
    class_id: int | None = None
    student_ids: list[int] = Field(default_factory=list)
    assign_all_students: bool = False
    tasks: list[TaskCreate] = Field(min_length=1)
    publish: bool = True


class AssignmentUpdate(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = None
    due_at: datetime | None = None
    clear_due_at: bool = False
    # 重开时是否把已交卷（未批改）打回进行中，便于补做
    reset_submitted: bool = False


class TaskAnswerPayload(BaseModel):
    answer: dict[str, Any] = Field(default_factory=dict)


class VocabReviewPayload(BaseModel):
    mastery: int = Field(ge=0, le=100, default=100)


class ReviewPayload(BaseModel):
    score: int = Field(ge=0, le=100)
    teacher_comment: str | None = Field(default=None, max_length=500)


def validate_task_config(task_type: str, config: dict[str, Any]):
    if task_type == "choice":
        ids = config.get("question_ids") or []
        if not isinstance(ids, list) or not ids:
            raise HTTPException(status_code=400, detail="choice task requires question_ids")
    elif task_type in {"video", "listen"}:
        if not config.get("resource_filename") and not config.get("video_url") and not config.get("audio_url"):
            raise HTTPException(
                status_code=400,
                detail=f"{task_type} task requires resource_filename or media url",
            )
    elif task_type in {"vocab", "dictation"}:
        ids = config.get("word_ids") or []
        if not isinstance(ids, list) or not ids:
            raise HTTPException(status_code=400, detail=f"{task_type} task requires word_ids")
    elif task_type == "voice":
        if not (config.get("prompt") or "").strip():
            raise HTTPException(status_code=400, detail="voice task requires prompt")


def _normalize_words(text: str) -> list[str]:
    import re
    return [w for w in re.findall(r"[A-Za-z']+", (text or "").lower()) if w]


def sanitize_answer_for_student(task_type: str, answer: Any, reveal: bool) -> Any:
    """Strip keys that leak answers / per-item correctness before final submit / close."""
    if reveal or not isinstance(answer, dict):
        return answer
    if task_type not in {"choice", "dictation"}:
        return answer
    out = dict(answer)
    detail = out.get("detail")
    if isinstance(detail, dict):
        cleaned = {}
        leak_keys = {"correct", "is_correct"}
        if task_type == "dictation":
            leak_keys |= {"prompt"}
        for k, v in detail.items():
            if isinstance(v, dict):
                cleaned[k] = {kk: vv for kk, vv in v.items() if kk not in leak_keys}
            else:
                cleaned[k] = v
        out["detail"] = cleaned
    # 顶层 correct 计数也会辅助试探，交卷前隐藏
    out.pop("correct", None)
    out.pop("hit", None)
    out.pop("expected_words", None)
    out.pop("expected_count", None)
    return out


def grade_task(task_type: str, config: dict[str, Any], answer: dict[str, Any], questions_by_id: dict[int, dict] | None = None):
    """Return (score 0-100, is_correct 0/1/None, normalized_answer)."""
    if task_type == "choice":
        qids = [int(x) for x in (config.get("question_ids") or [])]
        answers = answer.get("answers") or {}
        correct = 0
        detail = {}
        for qid in qids:
            selected = str(answers.get(str(qid)) or answers.get(qid) or "").upper()
            expected = (questions_by_id or {}).get(qid, {}).get("answer", "")
            ok = selected == expected
            if ok:
                correct += 1
            detail[str(qid)] = {"selected": selected or None, "correct": expected, "is_correct": ok}
        total = max(len(qids), 1)
        score = round(100 * correct / total)
        return score, 1 if correct == total else 0, {"answers": answers, "detail": detail, "correct": correct, "total": total}

    if task_type in {"video", "listen"}:
        min_seconds = int(config.get("min_seconds") or 0)
        watched = int(answer.get("watched_seconds") or 0)
        completed = bool(answer.get("completed")) or watched >= min_seconds
        score = 100 if completed else min(99, int(100 * watched / max(min_seconds, 1))) if min_seconds else (100 if completed else 0)
        return score, 1 if completed else 0, {
            "watched_seconds": watched, "completed": completed, "min_seconds": min_seconds,
            "media_session_id": answer.get("media_session_id"),
        }

    if task_type == "vocab":
        word_ids = {int(x) for x in (config.get("word_ids") or [])}
        learned = {int(x) for x in (answer.get("learned_word_ids") or [])}
        done = learned & word_ids
        total = max(len(word_ids), 1)
        completed = len(done) >= len(word_ids)
        score = round(100 * len(done) / total)
        return score, 1 if completed else 0, {"learned_word_ids": sorted(done), "completed": completed, "total": len(word_ids)}

    if task_type == "dictation":
        expected = [str(x).strip().lower() for x in (config.get("expected_words") or []) if str(x).strip()]
        typed_raw = answer.get("text") or " ".join(answer.get("words") or [])
        typed = _normalize_words(str(typed_raw))
        if expected:
            detail = {}
            hit = 0
            pool = list(typed)
            for idx, w in enumerate(expected):
                ok = w in pool
                if ok:
                    pool.remove(w)
                    hit += 1
                detail[str(idx)] = {
                    "prompt": w, "typed": w if ok else None, "correct": w, "is_correct": ok,
                }
            total = max(len(expected), 1)
            score = round(100 * hit / total)
            completed = hit >= total
            return score, 1 if completed else 0, {
                "text": typed_raw, "words": typed, "expected_count": total, "hit": hit,
                "completed": completed, "detail": detail, "expected_words": expected,
            }
        completed = bool(typed)
        return (100 if completed else 0), (1 if completed else 0), {
            "text": typed_raw, "words": typed, "completed": completed,
        }

    if task_type == "voice":
        audio_url = answer.get("audio_url")
        duration = int(answer.get("duration_seconds") or 0)
        min_seconds = int(config.get("min_seconds") or 1)
        pass_score = int(config.get("pass_score") or 60)
        assessment = answer.get("assessment") or {}
        completed = bool(audio_url) and duration >= min_seconds
        provider = str(assessment.get("provider") or "").lower()
        # 本地/浏览器引擎上限约 49–96，默认及格 60 会导致合法录音永远无法交卷
        if provider in {"local", "browser-asr", "browser"}:
            pass_score = min(pass_score, 50)
        if assessment.get("overall_score") is not None:
            score = int(round(float(assessment["overall_score"])))
            return score, (1 if score >= pass_score and completed else 0), {
                "audio_url": audio_url,
                "duration_seconds": duration,
                "completed": completed,
                "assessment": assessment,
                "pass_score": pass_score,
                "transcript": assessment.get("recognized_text") or answer.get("transcript"),
            }
        # 无服务端评分时不可凭 URL 直接满分
        return (0 if not completed else 40), (0), {
            "audio_url": audio_url, "duration_seconds": duration, "completed": completed,
            "transcript": answer.get("transcript"), "pass_score": pass_score,
            "note": "awaiting server assessment via /voice",
        }

    raise HTTPException(status_code=400, detail="unknown task type")


def ensure_submission(cursor, connection, assignment_id: int, student_id: int) -> dict:
    cursor.execute(
        "SELECT id,status,score,teacher_comment,submitted_at FROM assignment_submissions "
        "WHERE assignment_id=%s AND student_id=%s",
        (assignment_id, student_id),
    )
    row = cursor.fetchone()
    if row:
        return row
    cursor.execute(
        "INSERT INTO assignment_submissions(assignment_id,student_id,status) VALUES(%s,%s,'in_progress')",
        (assignment_id, student_id),
    )
    connection.commit()
    cursor.execute(
        "SELECT id,status,score,teacher_comment,submitted_at FROM assignment_submissions "
        "WHERE assignment_id=%s AND student_id=%s",
        (assignment_id, student_id),
    )
    return cursor.fetchone()


def load_tasks(cursor, assignment_id: int) -> list[dict]:
    cursor.execute(
        "SELECT id,task_type,title,sort_order,config_json FROM assignment_tasks WHERE assignment_id=%s ORDER BY sort_order,id",
        (assignment_id,),
    )
    rows = cursor.fetchall()
    for row in rows:
        row["config"] = parse_json(row.pop("config_json"))
    return rows


def enrich_tasks(
    cursor,
    tasks: list[dict],
    hide_answers: bool = False,
    hide_vocab_spelling: bool = False,
) -> list[dict]:
    for task in tasks:
        cfg = dict(task.get("config") or {})
        if hide_answers:
            # 交卷前不把标准答案写进 config（听写 expected_words / 题目 answer 等）
            cfg.pop("expected_words", None)
            task["config"] = cfg
        if task["task_type"] == "choice":
            ids = cfg.get("question_ids") or []
            if ids:
                placeholders = ",".join(["%s"] * len(ids))
                cursor.execute(
                    f"SELECT id,stem,option_a,option_b,option_c,option_d,answer,explanation FROM questions WHERE id IN ({placeholders})",
                    tuple(ids),
                )
                questions = cursor.fetchall()
                by_id = {q["id"]: q for q in questions}
                ordered = [by_id[i] for i in ids if i in by_id]
                if hide_answers:
                    for q in ordered:
                        q.pop("answer", None)
                        q.pop("explanation", None)
                task["questions"] = ordered
        elif task["task_type"] in {"vocab", "dictation"}:
            ids = cfg.get("word_ids") or []
            if ids:
                placeholders = ",".join(["%s"] * len(ids))
                cursor.execute(
                    f"SELECT id,word,phonetic,meaning,example_sentence FROM words WHERE id IN ({placeholders})",
                    tuple(ids),
                )
                words = cursor.fetchall()
                by_id = {w["id"]: w for w in words}
                ordered = [by_id[i] for i in ids if i in by_id]
                task["words"] = ordered
                if task["task_type"] == "dictation" and hide_answers:
                    # 听写答题前不直接展示目标单词拼写
                    task["prompt_words"] = [
                        {"id": w["id"], "hint": (w.get("meaning") or ""), "phonetic": w.get("phonetic")}
                        for w in ordered
                    ]
                    task.pop("words", None)
                elif task["task_type"] == "vocab" and (hide_answers and hide_vocab_spelling):
                    # 同作业含听写时，背单词只给释义/音标，避免串题
                    task["words"] = [
                        {
                            "id": w["id"],
                            "word": "••••",
                            "phonetic": w.get("phonetic"),
                            "meaning": w.get("meaning"),
                            "example_sentence": None,
                            "spelling_hidden": True,
                        }
                        for w in ordered
                    ]
        elif task["task_type"] in {"video", "listen"}:
            filename = cfg.get("resource_filename")
            if filename:
                url = f"/assets/{quote(filename)}"
                task["video_url" if task["task_type"] == "video" else "audio_url"] = url
            else:
                task["video_url"] = cfg.get("video_url")
                task["audio_url"] = cfg.get("audio_url") or cfg.get("video_url")
    return tasks


def register_homework_routes(mysql_connection, current_user):
    @router.get("/api/words")
    def list_words(user=Depends(current_user)):
        # 全库拼写仅教师可见，避免听写任务被 /api/words 泄题
        require_role(user, "teacher", "admin")
        connection = require_db(mysql_connection)
        cursor = connection.cursor(dictionary=True)
        cursor.execute("SELECT id,word,phonetic,meaning,example_sentence FROM words ORDER BY id")
        items = cursor.fetchall()
        cursor.close(); connection.close()
        return {"total": len(items), "items": items}

    @router.post("/api/words", status_code=201)
    def create_word(payload: WordCreate, user=Depends(current_user)):
        require_role(user, "teacher", "admin")
        connection = require_db(mysql_connection)
        cursor = connection.cursor()
        try:
            cursor.execute(
                "INSERT INTO words(word,phonetic,meaning,example_sentence,created_by) VALUES(%s,%s,%s,%s,%s)",
                (payload.word.strip(), payload.phonetic, payload.meaning.strip(), payload.example_sentence, int(user["sub"])),
            )
            word_id = cursor.lastrowid
            connection.commit()
        except Exception as exc:
            connection.rollback()
            if "Duplicate" in str(exc):
                raise HTTPException(status_code=409, detail="word already exists") from exc
            raise
        finally:
            cursor.close(); connection.close()
        return {"id": word_id, **payload.model_dump()}

    @router.get("/api/questions")
    def list_questions(user=Depends(current_user)):
        require_role(user, "teacher", "admin")
        connection = require_db(mysql_connection)
        cursor = connection.cursor(dictionary=True)
        cursor.execute(
            "SELECT id,stem,option_a,option_b,option_c,option_d,answer,explanation FROM questions ORDER BY id"
        )
        items = cursor.fetchall()
        cursor.close(); connection.close()
        return {"total": len(items), "items": items}

    @router.post("/api/questions", status_code=201)
    def create_question(payload: QuestionCreate, user=Depends(current_user)):
        require_role(user, "teacher", "admin")
        connection = require_db(mysql_connection)
        cursor = connection.cursor()
        cursor.execute(
            "INSERT INTO questions(stem,option_a,option_b,option_c,option_d,answer,explanation,created_by) "
            "VALUES(%s,%s,%s,%s,%s,%s,%s,%s)",
            (payload.stem.strip(), payload.option_a, payload.option_b, payload.option_c, payload.option_d,
             payload.answer, payload.explanation, int(user["sub"])),
        )
        qid = cursor.lastrowid
        connection.commit(); cursor.close(); connection.close()
        return {"id": qid, **payload.model_dump()}

    @router.get("/api/students")
    def list_students(user=Depends(current_user)):
        require_role(user, "teacher", "admin")
        connection = require_db(mysql_connection)
        cursor = connection.cursor(dictionary=True)
        if user["role"] == "admin":
            cursor.execute("SELECT id,username,display_name,role FROM users WHERE role='student' ORDER BY id")
        else:
            cursor.execute(
                "SELECT DISTINCT u.id,u.username,u.display_name,u.role FROM users u "
                "JOIN class_members cm ON cm.user_id=u.id AND cm.member_role='student' "
                "JOIN classes c ON c.id=cm.class_id AND c.teacher_id=%s AND c.status='active' "
                "WHERE u.role='student' ORDER BY u.id",
                (int(user["sub"]),),
            )
        items = cursor.fetchall()
        cursor.close(); connection.close()
        return {"total": len(items), "items": items}

    @router.post("/api/assignments", status_code=201)
    def create_assignment(payload: AssignmentCreate, user=Depends(current_user)):
        require_role(user, "teacher", "admin")
        if not payload.tasks:
            raise HTTPException(status_code=400, detail="at least one task required")
        for task in payload.tasks:
            validate_task_config(task.task_type, task.config)

        connection = require_db(mysql_connection)
        cursor = connection.cursor(dictionary=True)
        teacher_id = int(user["sub"])
        try:
            class_id = payload.class_id
            if class_id:
                cursor.execute("SELECT id,teacher_id,status FROM classes WHERE id=%s", (class_id,))
                clazz = cursor.fetchone()
                if not clazz or clazz["status"] != "active":
                    raise HTTPException(status_code=404, detail="class not found")
                if user["role"] != "admin" and clazz["teacher_id"] != teacher_id:
                    raise HTTPException(status_code=403, detail="not your class")
                cursor.execute(
                    "SELECT user_id AS id FROM class_members WHERE class_id=%s AND member_role='student'",
                    (class_id,),
                )
                student_ids = [r["id"] for r in cursor.fetchall()]
            else:
                student_ids = list(payload.student_ids)
                if payload.assign_all_students:
                    # 仅本教师班级内学员，避免跨校全库派发
                    if user["role"] == "admin":
                        cursor.execute("SELECT id FROM users WHERE role='student'")
                    else:
                        cursor.execute(
                            "SELECT DISTINCT cm.user_id AS id FROM class_members cm "
                            "JOIN classes c ON c.id=cm.class_id AND c.teacher_id=%s AND c.status='active' "
                            "WHERE cm.member_role='student'",
                            (teacher_id,),
                        )
                    student_ids = [r["id"] for r in cursor.fetchall()]
                elif user["role"] != "admin" and student_ids:
                    # 校验点名学员确属本教师班级
                    placeholders = ",".join(["%s"] * len(student_ids))
                    cursor.execute(
                        f"SELECT DISTINCT cm.user_id AS id FROM class_members cm "
                        f"JOIN classes c ON c.id=cm.class_id AND c.teacher_id=%s AND c.status='active' "
                        f"WHERE cm.member_role='student' AND cm.user_id IN ({placeholders})",
                        (teacher_id, *student_ids),
                    )
                    allowed = {int(r["id"]) for r in cursor.fetchall()}
                    student_ids = [sid for sid in student_ids if int(sid) in allowed]
            # 仅学生角色可被指派
            if student_ids:
                placeholders = ",".join(["%s"] * len(student_ids))
                cursor.execute(
                    f"SELECT id FROM users WHERE role='student' AND id IN ({placeholders})",
                    tuple(int(x) for x in student_ids),
                )
                student_ids = [r["id"] for r in cursor.fetchall()]
            if not student_ids:
                raise HTTPException(
                    status_code=400,
                    detail="select a class with members, students, or assign_all_students",
                )

            due_at = payload.due_at
            if due_at is not None and due_at.tzinfo is not None:
                due_at = due_at.astimezone(CST).replace(tzinfo=None)
            if payload.publish and due_at is not None and due_at <= now_cst_naive():
                raise HTTPException(status_code=400, detail="due_at must be in the future when publishing")

            status = "published" if payload.publish else "draft"
            cursor.execute("SET time_zone = '+08:00'")
            cursor.execute(
                "INSERT INTO assignments(title,description,teacher_id,class_id,status,due_at,published_at) "
                "VALUES(%s,%s,%s,%s,%s,%s,IF(%s='published',NOW(),NULL))",
                (payload.title.strip(), payload.description, teacher_id, class_id, status, due_at, status),
            )
            assignment_id = cursor.lastrowid
            for index, task in enumerate(payload.tasks):
                cursor.execute(
                    "INSERT INTO assignment_tasks(assignment_id,task_type,title,sort_order,config_json) "
                    "VALUES(%s,%s,%s,%s,%s)",
                    (assignment_id, task.task_type, task.title.strip(), index, json.dumps(task.config, ensure_ascii=False)),
                )
            for sid in student_ids:
                cursor.execute(
                    "INSERT INTO assignment_students(assignment_id,student_id) VALUES(%s,%s)",
                    (assignment_id, int(sid)),
                )
            connection.commit()
        except HTTPException:
            connection.rollback()
            raise
        except Exception:
            connection.rollback()
            raise
        finally:
            cursor.close(); connection.close()
        notified = {"students": 0, "parents": 0}
        if status == "published":
            notified = notify_assignment_published(
                mysql_connection, assignment_id, payload.title.strip(), student_ids,
            )
        return {
            "id": assignment_id, "status": status, "class_id": class_id,
            "student_count": len(student_ids), "task_count": len(payload.tasks),
            "notified": notified,
        }

    @router.get("/api/assignments")
    def list_assignments(user=Depends(current_user)):
        connection = require_db(mysql_connection)
        cursor = connection.cursor(dictionary=True)
        closed_n = close_overdue_assignments(cursor)
        if closed_n:
            connection.commit()
        role = user["role"]
        uid = int(user["sub"])
        if role in ("teacher", "admin"):
            if role == "admin":
                cursor.execute(
                    "SELECT a.id,a.title,a.description,a.status,a.due_at,a.created_at,a.published_at,a.teacher_id,a.class_id,"
                    "a.last_urged_at,c.name AS class_name,"
                    "(SELECT COUNT(*) FROM assignment_students s WHERE s.assignment_id=a.id) AS student_count,"
                    "(SELECT COUNT(*) FROM assignment_submissions sub WHERE sub.assignment_id=a.id AND sub.status!='in_progress') AS submitted_count "
                    "FROM assignments a LEFT JOIN classes c ON c.id=a.class_id ORDER BY a.id DESC"
                )
            else:
                cursor.execute(
                    "SELECT a.id,a.title,a.description,a.status,a.due_at,a.created_at,a.published_at,a.teacher_id,a.class_id,"
                    "a.last_urged_at,c.name AS class_name,"
                    "(SELECT COUNT(*) FROM assignment_students s WHERE s.assignment_id=a.id) AS student_count,"
                    "(SELECT COUNT(*) FROM assignment_submissions sub WHERE sub.assignment_id=a.id AND sub.status!='in_progress') AS submitted_count "
                    "FROM assignments a LEFT JOIN classes c ON c.id=a.class_id WHERE a.teacher_id=%s ORDER BY a.id DESC",
                    (uid,),
                )
        else:
            cursor.execute(
                "SELECT a.id,a.title,a.description,a.status,a.due_at,a.created_at,a.published_at,a.teacher_id,a.class_id,"
                "a.last_urged_at,c.name AS class_name,"
                "sub.status AS submission_status,sub.score AS submission_score,"
                "s.urged_at,s.urge_count "
                "FROM assignments a "
                "JOIN assignment_students s ON s.assignment_id=a.id AND s.student_id=%s "
                "LEFT JOIN assignment_submissions sub ON sub.assignment_id=a.id AND sub.student_id=%s "
                "LEFT JOIN classes c ON c.id=a.class_id "
                "WHERE a.status IN ('published','closed') ORDER BY a.id DESC",
                (uid, uid),
            )
        items = cursor.fetchall()
        cursor.close(); connection.close()
        return {"total": len(items), "items": items, "auto_closed": closed_n}

    @router.get("/api/assignments/{assignment_id}")
    def get_assignment(assignment_id: int, user=Depends(current_user)):
        connection = require_db(mysql_connection)
        cursor = connection.cursor(dictionary=True)
        if close_overdue_assignments(cursor):
            connection.commit()
        cursor.execute(
            "SELECT a.id,a.title,a.description,a.teacher_id,a.class_id,a.status,a.due_at,a.created_at,a.published_at,"
            "a.last_urged_at,c.name AS class_name FROM assignments a LEFT JOIN classes c ON c.id=a.class_id WHERE a.id=%s",
            (assignment_id,),
        )
        item = cursor.fetchone()
        if not item:
            cursor.close(); connection.close()
            raise HTTPException(status_code=404, detail="assignment not found")

        uid = int(user["sub"])
        role = user["role"]
        is_owner = role in ("teacher", "admin") and (role == "admin" or item["teacher_id"] == uid)
        submission = None
        if not is_owner:
            if item["status"] not in ("published", "closed"):
                cursor.close(); connection.close()
                raise HTTPException(status_code=404, detail="assignment not found")
            cursor.execute(
                "SELECT urged_at,urge_count FROM assignment_students WHERE assignment_id=%s AND student_id=%s",
                (assignment_id, uid),
            )
            link = cursor.fetchone()
            if not link:
                cursor.close(); connection.close()
                raise HTTPException(status_code=403, detail="not assigned to this homework")
            item["urged_at"] = link.get("urged_at")
            item["urge_count"] = link.get("urge_count") or 0
            if item["status"] == "published":
                submission = ensure_submission(cursor, connection, assignment_id, uid)
            else:
                cursor.execute(
                    "SELECT id,status,score,teacher_comment,submitted_at FROM assignment_submissions "
                    "WHERE assignment_id=%s AND student_id=%s",
                    (assignment_id, uid),
                )
                submission = cursor.fetchone()

        # 仅交卷后揭晓；关闭作业对未交卷学生仍隐藏答案，避免关闭→偷看→重开作弊
        submitted = bool(submission and submission.get("status") in ("submitted", "reviewed"))
        reveal_answers = is_owner or submitted
        loaded_tasks = load_tasks(cursor, assignment_id)
        # 同作业同时有听写时，背单词也不下发拼写（防串题）
        has_dictation = any(t.get("task_type") == "dictation" for t in loaded_tasks)
        tasks = enrich_tasks(
            cursor, loaded_tasks,
            hide_answers=not reveal_answers,
            hide_vocab_spelling=(not reveal_answers and has_dictation),
        )
        result = {**item, "tasks": tasks, "accepting_answers": item["status"] == "published"}
        task_types = {t["id"]: t["task_type"] for t in loaded_tasks}

        if is_owner:
            cursor.execute(
                "SELECT u.id,u.username,u.display_name,sub.id AS submission_id,sub.status AS submission_status,"
                "sub.score,sub.teacher_comment,sub.submitted_at "
                "FROM assignment_students s JOIN users u ON u.id=s.student_id "
                "LEFT JOIN assignment_submissions sub ON sub.assignment_id=s.assignment_id AND sub.student_id=s.student_id "
                "WHERE s.assignment_id=%s ORDER BY u.id",
                (assignment_id,),
            )
            result["students"] = cursor.fetchall()
        else:
            if submission:
                cursor.execute(
                    "SELECT task_id,answer_json,is_correct,score,completed_at FROM task_answers WHERE submission_id=%s",
                    (submission["id"],),
                )
                answers = cursor.fetchall()
                for row in answers:
                    parsed = parse_json(row.pop("answer_json"))
                    ttype = task_types.get(row["task_id"], "")
                    row["answer"] = sanitize_answer_for_student(ttype, parsed, reveal_answers)
                    if not reveal_answers and ttype in {"choice", "dictation"}:
                        row["score"] = None
                        row["is_correct"] = None
                result["submission"] = {**submission, "answers": answers}
            else:
                result["submission"] = None
            result["answers_revealed"] = bool(reveal_answers)

        cursor.close(); connection.close()
        return result

    @router.post("/api/assignments/{assignment_id}/urge")
    def urge_assignment(assignment_id: int, user=Depends(current_user)):
        require_role(user, "teacher", "admin")
        connection = require_db(mysql_connection)
        cursor = connection.cursor(dictionary=True)
        close_overdue_assignments(cursor)
        cursor.execute("SELECT id,teacher_id,status,title FROM assignments WHERE id=%s", (assignment_id,))
        item = cursor.fetchone()
        if not item:
            cursor.close(); connection.close()
            raise HTTPException(status_code=404, detail="assignment not found")
        if user["role"] != "admin" and item["teacher_id"] != int(user["sub"]):
            cursor.close(); connection.close()
            raise HTTPException(status_code=403, detail="not your assignment")
        if item["status"] != "published":
            connection.commit(); cursor.close(); connection.close()
            raise HTTPException(status_code=400, detail="only published assignments can be urged")
        cursor.execute(
            "SELECT s.student_id FROM assignment_students AS s "
            "LEFT JOIN assignment_submissions AS sub "
            "ON sub.assignment_id=s.assignment_id AND sub.student_id=s.student_id "
            "WHERE s.assignment_id=%s AND (sub.id IS NULL OR sub.status='in_progress')",
            (assignment_id,),
        )
        target_ids = [int(r["student_id"]) for r in cursor.fetchall()]
        cursor.execute(
            "UPDATE assignment_students AS s "
            "LEFT JOIN assignment_submissions AS sub "
            "ON sub.assignment_id=s.assignment_id AND sub.student_id=s.student_id "
            "SET s.urged_at=NOW(), s.urge_count=COALESCE(s.urge_count,0)+1 "
            "WHERE s.assignment_id=%s AND (sub.id IS NULL OR sub.status='in_progress')",
            (assignment_id,),
        )
        urged = cursor.rowcount or 0
        cursor.execute("UPDATE assignments SET last_urged_at=NOW() WHERE id=%s", (assignment_id,))
        connection.commit(); cursor.close(); connection.close()
        notified = notify_assignment_urged(
            mysql_connection, assignment_id, item.get("title") or "作业", target_ids,
        )
        return {
            "id": assignment_id, "urged_students": urged, "last_urged_at": "now",
            "notified": notified,
        }

    @router.post("/api/assignments/{assignment_id}/publish")
    def publish_assignment(assignment_id: int, user=Depends(current_user)):
        require_role(user, "teacher", "admin")
        connection = require_db(mysql_connection)
        cursor = connection.cursor(dictionary=True)
        cursor.execute("SET time_zone = '+08:00'")
        close_overdue_assignments(cursor)
        cursor.execute(
            "SELECT id,teacher_id,status,class_id,due_at,title FROM assignments WHERE id=%s",
            (assignment_id,),
        )
        item = cursor.fetchone()
        if not item:
            cursor.close(); connection.close()
            raise HTTPException(status_code=404, detail="assignment not found")
        if user["role"] != "admin" and item["teacher_id"] != int(user["sub"]):
            cursor.close(); connection.close()
            raise HTTPException(status_code=403, detail="not your assignment")
        if item["status"] == "closed":
            cursor.close(); connection.close()
            raise HTTPException(status_code=400, detail="closed assignment cannot be published")
        if item.get("due_at") is not None and item["due_at"] <= now_cst_naive():
            cursor.close(); connection.close()
            raise HTTPException(status_code=400, detail="due_at already passed; update due date or close assignment")
        cursor.execute(
            "UPDATE assignments SET status='published', published_at=COALESCE(published_at,NOW()) WHERE id=%s",
            (assignment_id,),
        )
        linked = 0
        if item.get("class_id"):
            # 发布时补同步：草稿期间新加入班级的学员也要收到作业
            linked = sync_assignment_class_members(cursor, assignment_id, int(item["class_id"]))
        cursor.execute(
            "SELECT student_id FROM assignment_students WHERE assignment_id=%s",
            (assignment_id,),
        )
        student_ids = [int(r["student_id"]) for r in cursor.fetchall()]
        connection.commit(); cursor.close(); connection.close()
        notified = notify_assignment_published(
            mysql_connection, assignment_id, item.get("title") or "作业", student_ids,
        )
        return {
            "id": assignment_id, "status": "published", "members_synced": linked,
            "notified": notified,
        }

    @router.post("/api/assignments/{assignment_id}/close")
    def close_assignment(assignment_id: int, user=Depends(current_user)):
        require_role(user, "teacher", "admin")
        connection = require_db(mysql_connection)
        cursor = connection.cursor(dictionary=True)
        cursor.execute("SELECT id,teacher_id,status,title FROM assignments WHERE id=%s", (assignment_id,))
        item = cursor.fetchone()
        if not item:
            cursor.close(); connection.close()
            raise HTTPException(status_code=404, detail="assignment not found")
        if user["role"] != "admin" and item["teacher_id"] != int(user["sub"]):
            cursor.close(); connection.close()
            raise HTTPException(status_code=403, detail="not your assignment")
        if item["status"] == "closed":
            cursor.close(); connection.close()
            return {"id": assignment_id, "status": "closed", "already": True}
        cursor.execute("UPDATE assignments SET status='closed' WHERE id=%s", (assignment_id,))
        cursor.execute(
            "SELECT student_id FROM assignment_students WHERE assignment_id=%s",
            (assignment_id,),
        )
        student_ids = [int(r["student_id"]) for r in cursor.fetchall()]
        connection.commit(); cursor.close(); connection.close()
        title = item.get("title") or "作业"
        body = f"作业「{title}」已关闭，无法继续作答。"
        students_n = notify_users(
            mysql_connection, student_ids, "作业已关闭", body, "assignment_closed",
            "/learn.html", {"assignment_id": assignment_id},
        )
        parents_n = 0
        for sid in student_ids:
            parents_n += notify_parents_of_student(
                mysql_connection, sid, "子女作业已关闭", body, "assignment_closed",
                "/learn.html", {"assignment_id": assignment_id, "student_id": sid},
            )
        return {
            "id": assignment_id, "status": "closed",
            "notified": {"students": students_n, "parents": parents_n},
        }

    @router.put("/api/assignments/{assignment_id}")
    def update_assignment(assignment_id: int, payload: AssignmentUpdate, user=Depends(current_user)):
        """改标题/说明/截止时间（草稿与已发布均可；已关闭需先 reopen）。"""
        require_role(user, "teacher", "admin")
        connection = require_db(mysql_connection)
        cursor = connection.cursor(dictionary=True)
        cursor.execute("SET time_zone = '+08:00'")
        cursor.execute("SELECT id,teacher_id,status FROM assignments WHERE id=%s", (assignment_id,))
        item = cursor.fetchone()
        if not item:
            cursor.close(); connection.close()
            raise HTTPException(status_code=404, detail="assignment not found")
        if user["role"] != "admin" and item["teacher_id"] != int(user["sub"]):
            cursor.close(); connection.close()
            raise HTTPException(status_code=403, detail="not your assignment")
        if item["status"] == "closed":
            cursor.close(); connection.close()
            raise HTTPException(status_code=400, detail="closed assignment; call /reopen first")
        fields, values = [], []
        if payload.title is not None:
            fields.append("title=%s"); values.append(payload.title.strip())
        if payload.description is not None:
            fields.append("description=%s"); values.append(payload.description)
        if payload.clear_due_at:
            fields.append("due_at=NULL")
        elif payload.due_at is not None:
            due_at = payload.due_at
            if due_at.tzinfo is not None:
                due_at = due_at.astimezone(CST).replace(tzinfo=None)
            if item["status"] == "published" and due_at <= now_cst_naive():
                cursor.close(); connection.close()
                raise HTTPException(status_code=400, detail="due_at must be in the future for published assignment")
            fields.append("due_at=%s"); values.append(due_at)
        if not fields:
            cursor.close(); connection.close()
            raise HTTPException(status_code=400, detail="no fields to update")
        values.append(assignment_id)
        cursor.execute(f"UPDATE assignments SET {', '.join(fields)} WHERE id=%s", tuple(values))
        connection.commit(); cursor.close(); connection.close()
        return {"id": assignment_id, "updated": True}

    @router.post("/api/assignments/{assignment_id}/reopen")
    def reopen_assignment(assignment_id: int, payload: AssignmentUpdate, user=Depends(current_user)):
        """关闭后重开：必须提供未来 due_at（或 clear 后无截止）。"""
        require_role(user, "teacher", "admin")
        connection = require_db(mysql_connection)
        cursor = connection.cursor(dictionary=True)
        cursor.execute("SET time_zone = '+08:00'")
        cursor.execute(
            "SELECT id,teacher_id,status,class_id,title FROM assignments WHERE id=%s",
            (assignment_id,),
        )
        item = cursor.fetchone()
        if not item:
            cursor.close(); connection.close()
            raise HTTPException(status_code=404, detail="assignment not found")
        if user["role"] != "admin" and item["teacher_id"] != int(user["sub"]):
            cursor.close(); connection.close()
            raise HTTPException(status_code=403, detail="not your assignment")
        if item["status"] != "closed":
            cursor.close(); connection.close()
            raise HTTPException(status_code=400, detail="only closed assignments can be reopened")
        due_at = None
        if payload.clear_due_at:
            due_at = None
        elif payload.due_at is not None:
            due_at = payload.due_at
            if due_at.tzinfo is not None:
                due_at = due_at.astimezone(CST).replace(tzinfo=None)
            if due_at <= now_cst_naive():
                cursor.close(); connection.close()
                raise HTTPException(status_code=400, detail="due_at must be in the future when reopening")
        else:
            cursor.close(); connection.close()
            raise HTTPException(status_code=400, detail="provide due_at or clear_due_at=true")
        cursor.execute(
            "UPDATE assignments SET status='published', due_at=%s, published_at=COALESCE(published_at,NOW()) WHERE id=%s",
            (due_at, assignment_id),
        )
        reset_n = 0
        if payload.reset_submitted:
            cursor.execute(
                "UPDATE assignment_submissions SET status='in_progress', submitted_at=NULL "
                "WHERE assignment_id=%s AND status='submitted'",
                (assignment_id,),
            )
            reset_n = cursor.rowcount or 0
        linked = 0
        if item.get("class_id"):
            linked = sync_assignment_class_members(cursor, assignment_id, int(item["class_id"]))
        cursor.execute(
            "SELECT student_id FROM assignment_students WHERE assignment_id=%s",
            (assignment_id,),
        )
        student_ids = [int(r["student_id"]) for r in cursor.fetchall()]
        connection.commit(); cursor.close(); connection.close()
        notified = notify_assignment_published(
            mysql_connection, assignment_id, item.get("title") or "作业", student_ids,
        )
        return {
            "id": assignment_id, "status": "published", "due_at": due_at,
            "members_synced": linked, "notified": notified, "reset_submitted": reset_n,
        }

    @router.post("/api/assignments/{assignment_id}/tasks/{task_id}/answer")
    def submit_task_answer(assignment_id: int, task_id: int, payload: TaskAnswerPayload, user=Depends(current_user)):
        require_role(user, "student", "admin")
        uid = int(user["sub"])
        connection = require_db(mysql_connection)
        cursor = connection.cursor(dictionary=True)
        if close_overdue_assignments(cursor):
            connection.commit()
        cursor.execute("SELECT id,status FROM assignments WHERE id=%s", (assignment_id,))
        assignment = cursor.fetchone()
        try:
            assert_assignment_open(assignment)
        except HTTPException:
            cursor.close(); connection.close()
            raise
        if user["role"] == "student":
            cursor.execute(
                "SELECT 1 FROM assignment_students WHERE assignment_id=%s AND student_id=%s",
                (assignment_id, uid),
            )
            if not cursor.fetchone():
                cursor.close(); connection.close()
                raise HTTPException(status_code=403, detail="not assigned")

        cursor.execute(
            "SELECT id,task_type,config_json FROM assignment_tasks WHERE id=%s AND assignment_id=%s",
            (task_id, assignment_id),
        )
        task = cursor.fetchone()
        if not task:
            cursor.close(); connection.close()
            raise HTTPException(status_code=404, detail="task not found")
        if task["task_type"] == "voice":
            cursor.close(); connection.close()
            raise HTTPException(
                status_code=400,
                detail="voice tasks must use /voice endpoint; client assessment is not accepted",
            )

        config = parse_json(task["config_json"])
        answer = dict(payload.answer or {})
        questions_by_id = {}
        if task["task_type"] == "choice":
            ids = config.get("question_ids") or []
            if ids:
                placeholders = ",".join(["%s"] * len(ids))
                cursor.execute(f"SELECT id,answer FROM questions WHERE id IN ({placeholders})", tuple(ids))
                questions_by_id = {r["id"]: r for r in cursor.fetchall()}
        if task["task_type"] in {"video", "listen"}:
            from backend.media_progress import verify_media_progress
            claimed = int(answer.get("watched_seconds") or 0)
            trusted = verify_media_progress(
                cursor, uid, assignment_id, task_id,
                answer.get("media_session_id"), answer.get("media_token"), claimed,
            )
            min_seconds = int(config.get("min_seconds") or 0)
            answer["watched_seconds"] = trusted
            answer["completed"] = trusted >= min_seconds if min_seconds else trusted > 0
        if task["task_type"] == "dictation" and not config.get("expected_words"):
            ids = config.get("word_ids") or []
            if ids:
                placeholders = ",".join(["%s"] * len(ids))
                cursor.execute(f"SELECT id,word FROM words WHERE id IN ({placeholders})", tuple(ids))
                by_id = {r["id"]: r["word"] for r in cursor.fetchall()}
                config = {
                    **config,
                    "expected_words": [str(by_id[i]).lower() for i in ids if i in by_id],
                }

        submission = ensure_submission(cursor, connection, assignment_id, uid)
        if submission["status"] in ("submitted", "reviewed") and user["role"] != "admin":
            cursor.close(); connection.close()
            raise HTTPException(status_code=400, detail="assignment already submitted")

        # 背单词：每次最多新增 1 个词，禁止一次上报全会
        if task["task_type"] == "vocab":
            allowed = {int(x) for x in (config.get("word_ids") or [])}
            incoming = {int(x) for x in (answer.get("learned_word_ids") or []) if int(x) in allowed}
            cursor.execute(
                "SELECT answer_json FROM task_answers WHERE submission_id=%s AND task_id=%s",
                (submission["id"], task_id),
            )
            prev_row = cursor.fetchone()
            prev_learned = set()
            if prev_row:
                prev_learned = {
                    int(x) for x in (parse_json(prev_row.get("answer_json")) or {}).get("learned_word_ids") or []
                }
            new_ids = incoming - prev_learned
            if len(new_ids) > 1:
                cursor.close(); connection.close()
                raise HTTPException(
                    status_code=400,
                    detail="vocab allows at most one new word per submit",
                )
            if not new_ids and not incoming:
                cursor.close(); connection.close()
                raise HTTPException(status_code=400, detail="learned_word_ids required")
            answer["learned_word_ids"] = sorted(prev_learned | new_ids)

        score, is_correct, normalized = grade_task(task["task_type"], config, answer, questions_by_id)

        cursor.execute(
            "INSERT INTO task_answers(submission_id,task_id,answer_json,is_correct,score,completed_at) "
            "VALUES(%s,%s,%s,%s,%s,IF(%s=100,NOW(),NULL)) "
            "ON DUPLICATE KEY UPDATE answer_json=VALUES(answer_json),is_correct=VALUES(is_correct),"
            "score=VALUES(score),completed_at=IF(VALUES(score)=100,NOW(),completed_at)",
            (submission["id"], task_id, json.dumps(normalized, ensure_ascii=False), is_correct, score, score),
        )
        if task["task_type"] == "vocab":
            for wid in normalized.get("learned_word_ids") or []:
                cursor.execute(
                    "INSERT INTO vocab_progress(user_id,word_id,mastery,last_reviewed_at) VALUES(%s,%s,100,NOW()) "
                    "ON DUPLICATE KEY UPDATE mastery=GREATEST(mastery,VALUES(mastery)), last_reviewed_at=NOW()",
                    (uid, int(wid)),
                )
        connection.commit()
        cursor.close(); connection.close()
        public_answer = sanitize_answer_for_student(task["task_type"], normalized, reveal=False)
        # 交卷前不回传 score/is_correct，避免选择题逐题试探
        if task["task_type"] in {"choice", "dictation"}:
            return {
                "task_id": task_id,
                "saved": True,
                "score": None,
                "is_correct": None,
                "answer": public_answer,
                "note": "已保存作答，交卷后公布对错与得分",
            }
        return {"task_id": task_id, "score": score, "is_correct": is_correct, "answer": public_answer}

    @router.post("/api/assignments/{assignment_id}/tasks/{task_id}/voice")
    async def upload_voice(
        assignment_id: int,
        task_id: int,
        duration_seconds: int = Form(0),
        transcript: str = Form(""),
        file: UploadFile = File(...),
        user=Depends(current_user),
    ):
        require_role(user, "student", "admin")
        uid = int(user["sub"])
        connection = require_db(mysql_connection)
        cursor = connection.cursor(dictionary=True)
        if close_overdue_assignments(cursor):
            connection.commit()
        cursor.execute("SELECT id,status FROM assignments WHERE id=%s", (assignment_id,))
        assignment = cursor.fetchone()
        try:
            assert_assignment_open(assignment)
        except HTTPException:
            cursor.close(); connection.close()
            raise
        cursor.execute(
            "SELECT id,task_type,config_json FROM assignment_tasks WHERE id=%s AND assignment_id=%s",
            (task_id, assignment_id),
        )
        task = cursor.fetchone()
        if not task or task["task_type"] != "voice":
            cursor.close(); connection.close()
            raise HTTPException(status_code=404, detail="voice task not found")
        if user["role"] == "student":
            cursor.execute(
                "SELECT 1 FROM assignment_students WHERE assignment_id=%s AND student_id=%s",
                (assignment_id, uid),
            )
            if not cursor.fetchone():
                cursor.close(); connection.close()
                raise HTTPException(status_code=403, detail="not assigned")

        submission = ensure_submission(cursor, connection, assignment_id, uid)
        if submission["status"] in ("submitted", "reviewed") and user["role"] != "admin":
            cursor.close(); connection.close()
            raise HTTPException(status_code=400, detail="assignment already submitted")

        content = await file.read()
        if not content or len(content) > 8 * 1024 * 1024:
            cursor.close(); connection.close()
            raise HTTPException(status_code=400, detail="audio must be non-empty and under 8MB")
        ext = Path(file.filename or "audio.webm").suffix.lower() or ".webm"
        if ext not in {".webm", ".mp3", ".wav", ".m4a", ".ogg"}:
            ext = ".webm"
        key = f"voice/{assignment_id}_{task_id}_{uid}_{uuid.uuid4().hex}{ext}"
        try:
            audio_url = save_bytes(key, content, content_type=file.content_type or "audio/webm")
        except Exception as exc:
            cursor.close(); connection.close()
            raise HTTPException(status_code=500, detail=f"audio storage failed: {exc}") from exc
        config = parse_json(task["config_json"])
        reference = (config.get("prompt") or "").strip()
        fd, tmp_name = tempfile.mkstemp(suffix=ext)
        os.close(fd)
        tmp_path = Path(tmp_name)
        try:
            tmp_path.write_bytes(content)
            from backend.pronunciation import probe_duration
            probed = probe_duration(tmp_path) or 0.0
            # 作业场景：时长以探测为准；无 Azure/Whisper 时不采信客户端 transcript（防作弊）
            trust_browser = os.getenv("PRONUNCIATION_TRUST_BROWSER_ASR", "0").strip().lower() in {
                "1", "true", "yes",
            }
            assessment = assess_audio(
                tmp_path,
                reference,
                float(probed or duration_seconds or 0),
                transcript=(transcript or "") if trust_browser else "",
                allow_client_transcript=trust_browser,
            )
            duration_seconds = int(round(probed or duration_seconds or 0))
        except Exception as exc:
            cursor.close(); connection.close()
            raise HTTPException(status_code=500, detail=f"pronunciation assessment failed: {exc}") from exc
        finally:
            tmp_path.unlink(missing_ok=True)
        score, is_correct, normalized = grade_task(
            "voice",
            config,
            {
                "audio_url": audio_url,
                "duration_seconds": max(0, int(duration_seconds)),
                "assessment": assessment,
            },
        )
        cursor.execute(
            "INSERT INTO task_answers(submission_id,task_id,answer_json,is_correct,score,completed_at) "
            "VALUES(%s,%s,%s,%s,%s,IF(%s>=%s,NOW(),NULL)) "
            "ON DUPLICATE KEY UPDATE answer_json=VALUES(answer_json),is_correct=VALUES(is_correct),"
            "score=VALUES(score),completed_at=IF(VALUES(score)>=%s,NOW(),completed_at)",
            (
                submission["id"], task_id, json.dumps(normalized, ensure_ascii=False), is_correct, score,
                score, int(config.get("pass_score") or 60), int(config.get("pass_score") or 60),
            ),
        )
        cursor.execute(
            "INSERT INTO pronunciation_attempts("
            "user_id,assignment_id,task_id,reference_text,audio_url,overall_score,accuracy_score,"
            "fluency_score,completeness_score,prosody_score,recognized_text,word_feedback_json,provider,tips) "
            "VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (
                uid, assignment_id, task_id, reference, audio_url,
                assessment.get("overall_score") or 0,
                assessment.get("accuracy_score"),
                assessment.get("fluency_score"),
                assessment.get("completeness_score"),
                assessment.get("prosody_score"),
                assessment.get("recognized_text"),
                json.dumps(assessment.get("words") or [], ensure_ascii=False),
                assessment.get("provider") or "local",
                assessment.get("tips"),
            ),
        )
        connection.commit(); cursor.close(); connection.close()
        return {"task_id": task_id, "audio_url": audio_url, "score": score, "answer": normalized, "assessment": assessment}

    @router.post("/api/assignments/{assignment_id}/submit")
    def finalize_submission(assignment_id: int, user=Depends(current_user)):
        require_role(user, "student", "admin")
        uid = int(user["sub"])
        connection = require_db(mysql_connection)
        cursor = connection.cursor(dictionary=True)
        if close_overdue_assignments(cursor):
            connection.commit()
        cursor.execute("SELECT id,status,title,teacher_id FROM assignments WHERE id=%s", (assignment_id,))
        assignment = cursor.fetchone()
        try:
            assert_assignment_open(assignment)
        except HTTPException:
            cursor.close(); connection.close()
            raise
        if user["role"] == "student":
            cursor.execute(
                "SELECT 1 FROM assignment_students WHERE assignment_id=%s AND student_id=%s",
                (assignment_id, uid),
            )
            if not cursor.fetchone():
                cursor.close(); connection.close()
                raise HTTPException(status_code=403, detail="not assigned")
        submission = ensure_submission(cursor, connection, assignment_id, uid)
        if submission["status"] in ("submitted", "reviewed"):
            cursor.close(); connection.close()
            raise HTTPException(status_code=400, detail="assignment already submitted")
        tasks = load_tasks(cursor, assignment_id)
        cursor.execute("SELECT task_id,score FROM task_answers WHERE submission_id=%s", (submission["id"],))
        answered = {r["task_id"]: r["score"] for r in cursor.fetchall()}
        missing = [t["id"] for t in tasks if t["id"] not in answered]
        incomplete = []
        for t in tasks:
            if t["id"] not in answered:
                continue
            score = int(answered[t["id"]] or 0)
            # choice/dictation：有作答即可交卷（对错交卷后公布，避免被迫刷满分试探）
            if t["task_type"] in {"choice", "dictation"}:
                continue
            pass_score = 100
            if t["task_type"] == "voice":
                pass_score = int((t.get("config") or {}).get("pass_score") or 60)
            if score < pass_score:
                incomplete.append(t["id"])
        if missing:
            cursor.close(); connection.close()
            raise HTTPException(status_code=400, detail=f"please complete tasks first: {missing}")
        if incomplete:
            cursor.close(); connection.close()
            raise HTTPException(
                status_code=400,
                detail=f"tasks not meeting pass score: {incomplete}",
            )
        scores = [int(answered[t["id"]] or 0) for t in tasks]
        total_score = round(sum(scores) / max(len(scores), 1))
        cursor.execute(
            "UPDATE assignment_submissions SET status='submitted', score=%s, submitted_at=NOW() WHERE id=%s",
            (total_score, submission["id"]),
        )
        cursor.execute("SELECT display_name FROM users WHERE id=%s", (uid,))
        student_name = (cursor.fetchone() or {}).get("display_name") or f"学员{uid}"
        teacher_id = int(assignment["teacher_id"])
        title = assignment.get("title") or "作业"
        connection.commit(); cursor.close(); connection.close()
        create_notification(
            mysql_connection, teacher_id, "学生已交作业",
            f"{student_name} 已提交「{title}」，得分 {total_score}。",
            "assignment_submitted", "/learn.html",
            {"assignment_id": assignment_id, "student_id": uid, "score": total_score},
        )
        notify_parents_of_student(
            mysql_connection, uid, "子女已交作业",
            f"{student_name} 已提交「{title}」，得分 {total_score}。",
            "assignment_submitted", "/learn.html",
            {"assignment_id": assignment_id, "student_id": uid, "score": total_score},
        )
        return {"assignment_id": assignment_id, "status": "submitted", "score": total_score, "incomplete_tasks": incomplete}

    @router.get("/api/assignments/{assignment_id}/submissions")
    def list_submissions(assignment_id: int, user=Depends(current_user)):
        require_role(user, "teacher", "admin")
        connection = require_db(mysql_connection)
        cursor = connection.cursor(dictionary=True)
        cursor.execute("SELECT id,teacher_id FROM assignments WHERE id=%s", (assignment_id,))
        item = cursor.fetchone()
        if not item:
            cursor.close(); connection.close()
            raise HTTPException(status_code=404, detail="assignment not found")
        if user["role"] != "admin" and item["teacher_id"] != int(user["sub"]):
            cursor.close(); connection.close()
            raise HTTPException(status_code=403, detail="not your assignment")
        cursor.execute(
            "SELECT sub.id,sub.student_id,u.display_name,u.username,sub.status,sub.score,sub.teacher_comment,sub.submitted_at "
            "FROM assignment_submissions sub JOIN users u ON u.id=sub.student_id "
            "WHERE sub.assignment_id=%s ORDER BY sub.submitted_at DESC",
            (assignment_id,),
        )
        items = cursor.fetchall()
        for row in items:
            cursor.execute(
                "SELECT task_id,answer_json,is_correct,score,completed_at FROM task_answers WHERE submission_id=%s",
                (row["id"],),
            )
            answers = cursor.fetchall()
            for a in answers:
                a["answer"] = parse_json(a.pop("answer_json"))
            row["answers"] = answers
        cursor.close(); connection.close()
        return {"total": len(items), "items": items}

    @router.post("/api/assignments/{assignment_id}/submissions/{submission_id}/review")
    def review_submission(assignment_id: int, submission_id: int, payload: ReviewPayload, user=Depends(current_user)):
        require_role(user, "teacher", "admin")
        connection = require_db(mysql_connection)
        cursor = connection.cursor(dictionary=True)
        cursor.execute("SELECT id,teacher_id FROM assignments WHERE id=%s", (assignment_id,))
        item = cursor.fetchone()
        if not item:
            cursor.close(); connection.close()
            raise HTTPException(status_code=404, detail="assignment not found")
        if user["role"] != "admin" and item["teacher_id"] != int(user["sub"]):
            cursor.close(); connection.close()
            raise HTTPException(status_code=403, detail="not your assignment")
        cursor.execute(
            "SELECT student_id,status FROM assignment_submissions WHERE id=%s AND assignment_id=%s",
            (submission_id, assignment_id),
        )
        sub_row = cursor.fetchone()
        if not sub_row:
            cursor.close(); connection.close()
            raise HTTPException(status_code=404, detail="submission not found")
        if sub_row["status"] not in ("submitted", "reviewed"):
            cursor.close(); connection.close()
            raise HTTPException(status_code=400, detail="only submitted submissions can be reviewed")
        cursor.execute(
            "UPDATE assignment_submissions SET status='reviewed', score=%s, teacher_comment=%s "
            "WHERE id=%s AND assignment_id=%s AND status IN ('submitted','reviewed')",
            (payload.score, payload.teacher_comment, submission_id, assignment_id),
        )
        if cursor.rowcount == 0:
            connection.rollback(); cursor.close(); connection.close()
            raise HTTPException(status_code=404, detail="submission not found")
        cursor.execute("SELECT title FROM assignments WHERE id=%s", (assignment_id,))
        asg_title = (cursor.fetchone() or {}).get("title") or "作业"
        student_id = int(sub_row["student_id"])
        comment = (payload.teacher_comment or "").strip()
        body = f"作业「{asg_title}」已批改，得分 {payload.score}。"
        if comment:
            body += f" 评语：{comment[:80]}"
        connection.commit(); cursor.close(); connection.close()
        create_notification(
            mysql_connection, student_id, "作业已批改", body, "assignment_reviewed",
            "/learn.html", {"assignment_id": assignment_id, "score": payload.score},
        )
        notify_parents_of_student(
            mysql_connection, student_id, "子女作业已批改", body, "assignment_reviewed",
            "/learn.html", {"assignment_id": assignment_id, "student_id": student_id, "score": payload.score},
        )
        return {"id": submission_id, "status": "reviewed", "score": payload.score}

    @router.post("/api/my/vocab/{word_id}/review")
    def review_vocab(word_id: int, payload: VocabReviewPayload, user=Depends(current_user)):
        require_role(user, "student", "teacher", "admin")
        connection = require_db(mysql_connection)
        cursor = connection.cursor(dictionary=True)
        cursor.execute("SELECT id FROM words WHERE id=%s", (word_id,))
        if not cursor.fetchone():
            cursor.close(); connection.close()
            raise HTTPException(status_code=404, detail="word not found")
        # 学生只能复习自己可见词库中的词，避免借 review 枚举全库
        if user.get("role") == "student":
            uid = int(user["sub"])
            cursor.execute(
                "SELECT 1 FROM vocab_progress WHERE user_id=%s AND word_id=%s",
                (uid, word_id),
            )
            allowed = bool(cursor.fetchone())
            if not allowed:
                cursor.execute(
                    "SELECT t.config_json FROM assignment_tasks t "
                    "JOIN assignments a ON a.id=t.assignment_id "
                    "JOIN assignment_students s ON s.assignment_id=a.id AND s.student_id=%s "
                    "WHERE t.task_type='vocab' AND a.status IN ('published','closed')",
                    (uid,),
                )
                for row in cursor.fetchall():
                    cfg = parse_json(row.get("config_json")) or {}
                    if word_id in {int(x) for x in (cfg.get("word_ids") or []) if str(x).isdigit() or isinstance(x, int)}:
                        allowed = True
                        break
            if not allowed:
                cursor.close(); connection.close()
                raise HTTPException(status_code=403, detail="word not in your practice set")
        cursor.execute(
            "INSERT INTO vocab_progress(user_id,word_id,mastery,last_reviewed_at) VALUES(%s,%s,%s,NOW()) "
            "ON DUPLICATE KEY UPDATE mastery=VALUES(mastery), last_reviewed_at=NOW()",
            (int(user["sub"]), word_id, payload.mastery),
        )
        connection.commit(); cursor.close(); connection.close()
        return {"word_id": word_id, "mastery": payload.mastery}

    @router.get("/api/my/vocab")
    def my_vocab(user=Depends(current_user)):
        """学生可见词库：已掌握进度 ∪ 已布置的「背单词」任务词（不含听写题词，防泄题）。"""
        connection = require_db(mysql_connection)
        cursor = connection.cursor(dictionary=True)
        uid = int(user["sub"])
        role = user.get("role")
        if role in ("teacher", "admin"):
            cursor.execute(
                "SELECT w.id,w.word,w.phonetic,w.meaning,w.example_sentence,"
                "COALESCE(p.mastery,0) AS mastery,p.last_reviewed_at "
                "FROM words w LEFT JOIN vocab_progress p ON p.word_id=w.id AND p.user_id=%s ORDER BY w.id",
                (uid,),
            )
            items = cursor.fetchall()
            cursor.close(); connection.close()
            return {"total": len(items), "items": items}

        allowed_ids: set[int] = set()
        cursor.execute(
            "SELECT word_id FROM vocab_progress WHERE user_id=%s",
            (uid,),
        )
        allowed_ids.update(int(r["word_id"]) for r in cursor.fetchall())
        # 仅 vocab 任务可暴露拼写；dictation 任务的 word_ids 不进入自由练习词库
        cursor.execute(
            "SELECT t.config_json FROM assignment_tasks t "
            "JOIN assignments a ON a.id=t.assignment_id "
            "JOIN assignment_students s ON s.assignment_id=a.id AND s.student_id=%s "
            "WHERE t.task_type='vocab' AND a.status IN ('published','closed')",
            (uid,),
        )
        for row in cursor.fetchall():
            cfg = parse_json(row.get("config_json")) or {}
            for wid in cfg.get("word_ids") or []:
                try:
                    allowed_ids.add(int(wid))
                except (TypeError, ValueError):
                    continue
        if not allowed_ids:
            cursor.close(); connection.close()
            return {"total": 0, "items": []}
        placeholders = ",".join(["%s"] * len(allowed_ids))
        cursor.execute(
            f"SELECT w.id,w.word,w.phonetic,w.meaning,w.example_sentence,"
            f"COALESCE(p.mastery,0) AS mastery,p.last_reviewed_at "
            f"FROM words w LEFT JOIN vocab_progress p ON p.word_id=w.id AND p.user_id=%s "
            f"WHERE w.id IN ({placeholders}) ORDER BY w.id",
            (uid, *sorted(allowed_ids)),
        )
        items = cursor.fetchall()
        cursor.close(); connection.close()
        return {"total": len(items), "items": items}

    @router.get("/api/pronunciation/status")
    def pronunciation_status(user=Depends(current_user)):
        return provider_status()

    @router.post("/api/pronunciation/assess")
    async def practice_pronunciation(
        reference_text: str = Form(...),
        duration_seconds: int = Form(0),
        transcript: str = Form(""),
        file: UploadFile = File(...),
        user=Depends(current_user),
    ):
        require_role(user, "student", "teacher", "admin", "parent")
        content = await file.read()
        if not content or len(content) > 8 * 1024 * 1024:
            raise HTTPException(status_code=400, detail="audio must be non-empty and under 8MB")
        reference = (reference_text or "").strip()
        if not reference:
            raise HTTPException(status_code=400, detail="reference_text required")
        ext = Path(file.filename or "audio.webm").suffix.lower() or ".webm"
        if ext not in {".webm", ".mp3", ".wav", ".m4a", ".ogg"}:
            ext = ".webm"
        key = f"voice/practice_{user['sub']}_{uuid.uuid4().hex}{ext}"
        try:
            audio_url = save_bytes(key, content, content_type=file.content_type or "audio/webm")
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"audio storage failed: {exc}") from exc
        fd, tmp_name = tempfile.mkstemp(suffix=ext)
        os.close(fd)
        tmp_path = Path(tmp_name)
        try:
            tmp_path.write_bytes(content)
            trust_browser = os.getenv("PRONUNCIATION_TRUST_BROWSER_ASR", "0").strip().lower() in {
                "1", "true", "yes",
            }
            assessment = assess_audio(
                tmp_path,
                reference,
                float(duration_seconds or 0),
                transcript=(transcript or "") if trust_browser else "",
                allow_client_transcript=trust_browser,
            )
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"pronunciation assessment failed: {exc}") from exc
        finally:
            tmp_path.unlink(missing_ok=True)

        connection = require_db(mysql_connection)
        cursor = connection.cursor()
        cursor.execute(
            "INSERT INTO pronunciation_attempts("
            "user_id,assignment_id,task_id,reference_text,audio_url,overall_score,accuracy_score,"
            "fluency_score,completeness_score,prosody_score,recognized_text,word_feedback_json,provider,tips) "
            "VALUES(%s,NULL,NULL,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (
                int(user["sub"]), reference, audio_url,
                assessment.get("overall_score") or 0,
                assessment.get("accuracy_score"),
                assessment.get("fluency_score"),
                assessment.get("completeness_score"),
                assessment.get("prosody_score"),
                assessment.get("recognized_text"),
                json.dumps(assessment.get("words") or [], ensure_ascii=False),
                assessment.get("provider") or "local",
                assessment.get("tips"),
            ),
        )
        attempt_id = cursor.lastrowid
        connection.commit(); cursor.close(); connection.close()
        return {"id": attempt_id, "audio_url": audio_url, "assessment": assessment}

    @router.get("/api/pronunciation/history")
    def pronunciation_history(user=Depends(current_user)):
        connection = require_db(mysql_connection)
        cursor = connection.cursor(dictionary=True)
        cursor.execute(
            "SELECT id,assignment_id,task_id,reference_text,audio_url,overall_score,accuracy_score,"
            "fluency_score,completeness_score,prosody_score,recognized_text,word_feedback_json,provider,tips,created_at "
            "FROM pronunciation_attempts WHERE user_id=%s ORDER BY id DESC LIMIT 30",
            (int(user["sub"]),),
        )
        items = cursor.fetchall()
        for row in items:
            row["words"] = parse_json(row.pop("word_feedback_json")) or []
        cursor.close(); connection.close()
        return {"total": len(items), "items": items}

    return router
