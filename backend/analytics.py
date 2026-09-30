"""Teacher / student analytics for English tutoring workflows."""
from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Depends, HTTPException

from backend.homework import apply_overdue_close, parse_json

router = APIRouter(tags=["analytics"])


def require_db(mysql_connection):
    connection = mysql_connection()
    if not connection:
        raise HTTPException(status_code=503, detail="MySQL is required")
    return connection


def require_role(user: dict, *roles: str):
    if user.get("role") not in roles:
        raise HTTPException(status_code=403, detail="insufficient role")


def register_analytics_routes(mysql_connection, current_user):
    @router.get("/api/teacher/dashboard")
    def teacher_dashboard(user=Depends(current_user)):
        """教培看板：班级、待批改、催交、完成率。"""
        require_role(user, "teacher", "admin")
        connection = require_db(mysql_connection)
        cursor = connection.cursor(dictionary=True)
        closed = apply_overdue_close(mysql_connection, cursor, connection)
        uid = int(user["sub"])
        is_admin = user["role"] == "admin"
        teacher_filter = "" if is_admin else " AND a.teacher_id=%s"
        params: list[Any] = [] if is_admin else [uid]

        cursor.execute(
            f"SELECT COUNT(*) AS c FROM classes c WHERE c.status='active'"
            + ("" if is_admin else " AND c.teacher_id=%s"),
            tuple([] if is_admin else [uid]),
        )
        class_count = int((cursor.fetchone() or {}).get("c") or 0)

        cursor.execute(
            f"SELECT COUNT(*) AS c FROM assignments a WHERE a.status='published'{teacher_filter}",
            tuple(params),
        )
        published = int((cursor.fetchone() or {}).get("c") or 0)

        cursor.execute(
            f"SELECT COUNT(*) AS c FROM assignment_submissions sub "
            f"JOIN assignments a ON a.id=sub.assignment_id "
            f"WHERE sub.status='submitted'{teacher_filter}",
            tuple(params),
        )
        pending_review = int((cursor.fetchone() or {}).get("c") or 0)

        cursor.execute(
            f"SELECT COUNT(*) AS c FROM assignment_students s "
            f"JOIN assignments a ON a.id=s.assignment_id AND a.status='published' "
            f"LEFT JOIN assignment_submissions sub "
            f"ON sub.assignment_id=s.assignment_id AND sub.student_id=s.student_id "
            f"WHERE (sub.id IS NULL OR sub.status='in_progress'){teacher_filter}",
            tuple(params),
        )
        unsubmitted = int((cursor.fetchone() or {}).get("c") or 0)

        cursor.execute(
            f"SELECT a.id,a.title,a.status,a.due_at,a.class_id,c.name AS class_name,"
            f"(SELECT COUNT(*) FROM assignment_students s WHERE s.assignment_id=a.id) AS student_count,"
            f"(SELECT COUNT(*) FROM assignment_submissions sub "
            f" WHERE sub.assignment_id=a.id AND sub.status IN ('submitted','reviewed')) AS submitted_count,"
            f"(SELECT ROUND(AVG(sub.score),1) FROM assignment_submissions sub "
            f" WHERE sub.assignment_id=a.id AND sub.score IS NOT NULL) AS avg_score,"
            f"(SELECT COUNT(*) FROM assignment_submissions sub "
            f" WHERE sub.assignment_id=a.id AND sub.status='submitted') AS pending_review "
            f"FROM assignments a LEFT JOIN classes c ON c.id=a.class_id "
            f"WHERE a.status IN ('published','closed')"
            + ("" if is_admin else " AND a.teacher_id=%s")
            + " ORDER BY a.id DESC LIMIT 12",
            tuple(params),
        )
        recent = cursor.fetchall()
        for row in recent:
            total = int(row.get("student_count") or 0)
            done = int(row.get("submitted_count") or 0)
            row["completion_rate"] = round(100 * done / total, 1) if total else 0

        # 续费预警摘要（低课时学员数）
        if is_admin:
            cursor.execute(
                "SELECT COUNT(*) AS c FROM student_hour_packages "
                "WHERE (total_hours - used_hours) <= 3"
            )
        else:
            cursor.execute(
                "SELECT COUNT(DISTINCT p.student_id) AS c FROM student_hour_packages p "
                "JOIN class_members m ON m.user_id=p.student_id "
                "JOIN classes c ON c.id=m.class_id AND c.teacher_id=%s AND c.status='active' "
                "WHERE (p.total_hours - p.used_hours) <= 3",
                (uid,),
            )
        low_hours_students = int((cursor.fetchone() or {}).get("c") or 0)
        cursor.execute(
            "SELECT COUNT(*) AS c FROM tuition_contracts "
            "WHERE status='active' AND end_date IS NOT NULL "
            "AND end_date <= DATE_ADD(CURDATE(), INTERVAL 14 DAY)"
            + ("" if is_admin else " AND teacher_id=%s"),
            tuple([] if is_admin else [uid]),
        )
        expiring_contracts = int((cursor.fetchone() or {}).get("c") or 0)

        cursor.close(); connection.close()
        return {
            "summary": {
                "active_classes": class_count,
                "published_assignments": published,
                "pending_reviews": pending_review,
                "unsubmitted_slots": unsubmitted,
                "auto_closed_now": int(closed or 0),
                "low_hours_students": low_hours_students,
                "expiring_contracts": expiring_contracts,
            },
            "recent_assignments": recent,
        }

    @router.get("/api/teacher/performance")
    def teacher_performance(days: int = 30, user=Depends(current_user)):
        """教师绩效报表：排课、作业、批改、口语预约、收款。"""
        require_role(user, "teacher", "admin")
        days = max(1, min(int(days or 30), 366))
        connection = require_db(mysql_connection)
        cursor = connection.cursor(dictionary=True)
        cursor.execute("SET time_zone = '+08:00'")
        uid = int(user["sub"])
        is_admin = user["role"] == "admin"
        tf = "" if is_admin else " AND teacher_id=%s"
        params: list[Any] = [] if is_admin else [uid]

        cursor.execute(
            f"SELECT COUNT(*) AS c FROM lessons WHERE status='completed' "
            f"AND created_at >= DATE_SUB(NOW(), INTERVAL %s DAY){tf}",
            tuple([days] + params),
        )
        lessons_done = int((cursor.fetchone() or {}).get("c") or 0)

        cursor.execute(
            f"SELECT COALESCE(SUM(hours_cost),0) AS h FROM lessons WHERE status='completed' "
            f"AND created_at >= DATE_SUB(NOW(), INTERVAL %s DAY){tf}",
            tuple([days] + params),
        )
        hours_taught = float((cursor.fetchone() or {}).get("h") or 0)

        cursor.execute(
            f"SELECT COUNT(*) AS c FROM assignments WHERE status IN ('published','closed') "
            f"AND created_at >= DATE_SUB(NOW(), INTERVAL %s DAY)"
            + ("" if is_admin else " AND teacher_id=%s"),
            tuple([days] + ([] if is_admin else [uid])),
        )
        hw_created = int((cursor.fetchone() or {}).get("c") or 0)

        cursor.execute(
            f"SELECT COUNT(*) AS c FROM assignment_submissions sub "
            f"JOIN assignments a ON a.id=sub.assignment_id "
            f"WHERE sub.status='reviewed' AND sub.submitted_at >= DATE_SUB(NOW(), INTERVAL %s DAY)"
            + ("" if is_admin else " AND a.teacher_id=%s"),
            tuple([days] + ([] if is_admin else [uid])),
        )
        reviews = int((cursor.fetchone() or {}).get("c") or 0)

        cursor.execute(
            f"SELECT ROUND(AVG(sub.score),1) AS avg_score FROM assignment_submissions sub "
            f"JOIN assignments a ON a.id=sub.assignment_id "
            f"WHERE sub.score IS NOT NULL AND sub.submitted_at >= DATE_SUB(NOW(), INTERVAL %s DAY)"
            + ("" if is_admin else " AND a.teacher_id=%s"),
            tuple([days] + ([] if is_admin else [uid])),
        )
        avg_score = (cursor.fetchone() or {}).get("avg_score")

        cursor.execute(
            f"SELECT COUNT(*) AS c FROM tutor_bookings b "
            f"JOIN tutor_slots s ON s.id=b.slot_id "
            f"WHERE b.status='completed' AND b.updated_at >= DATE_SUB(NOW(), INTERVAL %s DAY)"
            + ("" if is_admin else " AND s.teacher_id=%s"),
            tuple([days] + ([] if is_admin else [uid])),
        )
        tutor_done = int((cursor.fetchone() or {}).get("c") or 0)

        cursor.execute(
            f"SELECT COALESCE(SUM(p.amount),0) AS amt, COUNT(*) AS n FROM tuition_payments p "
            f"JOIN tuition_contracts c ON c.id=p.contract_id "
            f"WHERE p.paid_at >= DATE_SUB(NOW(), INTERVAL %s DAY)"
            + ("" if is_admin else " AND c.teacher_id=%s"),
            tuple([days] + ([] if is_admin else [uid])),
        )
        pay = cursor.fetchone() or {}
        cursor.close(); connection.close()
        return {
            "days": days,
            "lessons_completed": lessons_done,
            "hours_taught": hours_taught,
            "assignments_created": hw_created,
            "reviews_done": reviews,
            "student_avg_score": avg_score,
            "tutor_sessions_completed": tutor_done,
            "payments_count": int(pay.get("n") or 0),
            "payments_amount": float(pay.get("amt") or 0),
        }

    @router.get("/api/classes/{class_id}/progress")
    def class_progress(class_id: int, user=Depends(current_user)):
        """班级学员学情：作业完成与均分。"""
        require_role(user, "teacher", "admin")
        connection = require_db(mysql_connection)
        cursor = connection.cursor(dictionary=True)
        apply_overdue_close(mysql_connection, cursor, connection)
        cursor.execute("SELECT id,teacher_id,name,status FROM classes WHERE id=%s", (class_id,))
        clazz = cursor.fetchone()
        if not clazz:
            cursor.close(); connection.close()
            raise HTTPException(status_code=404, detail="class not found")
        if user["role"] != "admin" and clazz["teacher_id"] != int(user["sub"]):
            cursor.close(); connection.close()
            raise HTTPException(status_code=403, detail="not your class")

        cursor.execute(
            "SELECT COUNT(*) AS c FROM assignments WHERE class_id=%s AND status IN ('published','closed')",
            (class_id,),
        )
        assignment_total = int((cursor.fetchone() or {}).get("c") or 0)

        cursor.execute(
            "SELECT u.id,u.username,u.display_name,cm.joined_at,"
            "(SELECT COUNT(*) FROM assignment_students s "
            " JOIN assignments a ON a.id=s.assignment_id "
            " WHERE s.student_id=u.id AND a.class_id=%s AND a.status IN ('published','closed')) AS assigned,"
            "(SELECT COUNT(*) FROM assignment_submissions sub "
            " JOIN assignments a ON a.id=sub.assignment_id "
            " WHERE sub.student_id=u.id AND a.class_id=%s "
            " AND sub.status IN ('submitted','reviewed')) AS submitted,"
            "(SELECT ROUND(AVG(sub.score),1) FROM assignment_submissions sub "
            " JOIN assignments a ON a.id=sub.assignment_id "
            " WHERE sub.student_id=u.id AND a.class_id=%s AND sub.score IS NOT NULL) AS avg_score,"
            "(SELECT COUNT(*) FROM pronunciation_attempts p WHERE p.user_id=u.id) AS pron_count,"
            "(SELECT ROUND(AVG(p.overall_score),1) FROM pronunciation_attempts p WHERE p.user_id=u.id) AS pron_avg "
            "FROM class_members cm JOIN users u ON u.id=cm.user_id "
            "WHERE cm.class_id=%s AND cm.member_role='student' ORDER BY u.id",
            (class_id, class_id, class_id, class_id),
        )
        members = cursor.fetchall()
        for m in members:
            assigned = int(m.get("assigned") or 0)
            submitted = int(m.get("submitted") or 0)
            m["completion_rate"] = round(100 * submitted / assigned, 1) if assigned else 0

        cursor.close(); connection.close()
        return {
            "class": {"id": clazz["id"], "name": clazz["name"], "status": clazz["status"]},
            "assignment_total": assignment_total,
            "members": members,
        }

    @router.get("/api/my/wrongbook")
    def my_wrongbook(user=Depends(current_user)):
        """学生错题本：交卷后的选择/听力/听写错题 + 解析。"""
        require_role(user, "student", "admin")
        connection = require_db(mysql_connection)
        cursor = connection.cursor(dictionary=True)
        uid = int(user["sub"])
        cursor.execute(
            "SELECT ta.id,ta.task_id,ta.answer_json,ta.score,ta.is_correct,"
            "a.id AS assignment_id,a.title AS assignment_title,a.status AS assignment_status,"
            "t.title AS task_title,t.task_type,t.config_json,sub.submitted_at "
            "FROM task_answers ta "
            "JOIN assignment_submissions sub ON sub.id=ta.submission_id "
            "JOIN assignments a ON a.id=sub.assignment_id "
            "JOIN assignment_tasks t ON t.id=ta.task_id "
            "WHERE sub.student_id=%s AND t.task_type IN ('choice','dictation') "
            "AND sub.status IN ('submitted','reviewed') "
            "AND (ta.is_correct=0 OR ta.score < 100) "
            "ORDER BY sub.submitted_at DESC, ta.id DESC LIMIT 50",
            (uid,),
        )
        rows = cursor.fetchall()
        items = []
        for row in rows:
            answer = parse_json(row.pop("answer_json")) or {}
            config = parse_json(row.pop("config_json")) or {}
            detail = answer.get("detail") or {}
            questions = []
            if row.get("task_type") == "dictation":
                for iid, d in detail.items():
                    if d.get("is_correct") is True:
                        continue
                    questions.append({
                        "id": iid,
                        "stem": d.get("prompt") or f"听写 {iid}",
                        "selected": d.get("typed"),
                        "correct": d.get("correct"),
                        "explanation": None,
                        "kind": "dictation",
                    })
            else:
                qids = [int(x) for x in (config.get("question_ids") or [])]
                if qids:
                    placeholders = ",".join(["%s"] * len(qids))
                    cursor.execute(
                        f"SELECT id,stem,option_a,option_b,option_c,option_d,answer,explanation "
                        f"FROM questions WHERE id IN ({placeholders})",
                        tuple(qids),
                    )
                    by_id = {q["id"]: q for q in cursor.fetchall()}
                    for qid in qids:
                        q = by_id.get(qid)
                        if not q:
                            continue
                        d = detail.get(str(qid)) or {}
                        if d.get("is_correct") is True:
                            continue
                        questions.append({
                            "id": qid,
                            "stem": q["stem"],
                            "options": {
                                "A": q["option_a"], "B": q["option_b"],
                                "C": q["option_c"], "D": q["option_d"],
                            },
                            "selected": d.get("selected"),
                            "correct": q["answer"],
                            "explanation": q.get("explanation"),
                            "kind": "choice",
                        })
            if not questions:
                continue
            items.append({
                "assignment_id": row["assignment_id"],
                "assignment_title": row["assignment_title"],
                "task_id": row["task_id"],
                "task_title": row["task_title"],
                "task_type": row.get("task_type"),
                "score": row["score"],
                "submitted_at": row["submitted_at"],
                "wrong_questions": questions,
            })
        cursor.close(); connection.close()
        return {"total": len(items), "items": items}

    @router.get("/api/my/learning-summary")
    def my_learning_summary(user=Depends(current_user)):
        """学生个人学情摘要（作业/词汇/纠音）。"""
        require_role(user, "student", "admin")
        connection = require_db(mysql_connection)
        cursor = connection.cursor(dictionary=True)
        uid = int(user["sub"])
        apply_overdue_close(mysql_connection, cursor, connection)
        cursor.execute(
            "SELECT COUNT(*) AS total,"
            "SUM(CASE WHEN sub.status IN ('submitted','reviewed') THEN 1 ELSE 0 END) AS submitted,"
            "ROUND(AVG(CASE WHEN sub.score IS NOT NULL THEN sub.score END),1) AS avg_score "
            "FROM assignment_students s "
            "JOIN assignments a ON a.id=s.assignment_id AND a.status IN ('published','closed') "
            "LEFT JOIN assignment_submissions sub ON sub.assignment_id=s.assignment_id AND sub.student_id=s.student_id "
            "WHERE s.student_id=%s",
            (uid,),
        )
        hw = cursor.fetchone() or {}
        cursor.execute(
            "SELECT COUNT(*) AS tracked, SUM(CASE WHEN mastery>=80 THEN 1 ELSE 0 END) AS mastered "
            "FROM vocab_progress WHERE user_id=%s",
            (uid,),
        )
        vocab = cursor.fetchone() or {}
        cursor.execute(
            "SELECT COUNT(*) AS attempts, ROUND(AVG(overall_score),1) AS avg_score,"
            "MAX(overall_score) AS best_score FROM pronunciation_attempts WHERE user_id=%s",
            (uid,),
        )
        pron = cursor.fetchone() or {}
        cursor.close(); connection.close()
        return {
            "homework": {
                "total": int(hw.get("total") or 0),
                "submitted": int(hw.get("submitted") or 0),
                "avg_score": hw.get("avg_score"),
            },
            "vocab": {
                "tracked": int(vocab.get("tracked") or 0),
                "mastered": int(vocab.get("mastered") or 0),
            },
            "pronunciation": {
                "attempts": int(pron.get("attempts") or 0),
                "avg_score": pron.get("avg_score"),
                "best_score": pron.get("best_score"),
            },
        }

    return router
