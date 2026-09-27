"""Parent-child binding and learning overview APIs."""
from __future__ import annotations

import secrets
import string

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

router = APIRouter(tags=["parents"])


def require_db(mysql_connection):
    connection = mysql_connection()
    if not connection:
        raise HTTPException(status_code=503, detail="MySQL is required for parent features")
    return connection


def require_role(user: dict, *roles: str):
    if user.get("role") not in roles:
        raise HTTPException(status_code=403, detail="insufficient role")


def make_invite(length: int = 8) -> str:
    alphabet = string.ascii_uppercase + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(length))


def rotate_parent_invite(cursor, student_id: int) -> str | None:
    """Invalidate previous invite so unbound parents cannot re-bind with the same code."""
    for _ in range(8):
        code = make_invite()
        try:
            cursor.execute(
                "UPDATE users SET parent_invite_code=%s WHERE id=%s AND role='student'",
                (code, student_id),
            )
            if cursor.rowcount:
                return code
        except Exception:
            continue
    return None


class BindPayload(BaseModel):
    invite_code: str = Field(min_length=4, max_length=16)


def ensure_parent_of(cursor, parent_id: int, student_id: int):
    cursor.execute(
        "SELECT 1 FROM parent_student_links WHERE parent_id=%s AND student_id=%s AND status='active'",
        (parent_id, student_id),
    )
    if not cursor.fetchone():
        raise HTTPException(status_code=403, detail="not linked to this student")


def register_parent_routes(mysql_connection, current_user):
    @router.get("/api/students/me/parent-invite")
    def get_parent_invite(user=Depends(current_user)):
        require_role(user, "student")
        connection = require_db(mysql_connection)
        cursor = connection.cursor(dictionary=True)
        uid = int(user["sub"])
        cursor.execute("SELECT parent_invite_code FROM users WHERE id=%s AND role='student'", (uid,))
        row = cursor.fetchone()
        if not row:
            cursor.close(); connection.close()
            raise HTTPException(status_code=404, detail="student not found")
        code = row.get("parent_invite_code")
        if not code:
            code = make_invite()
            for _ in range(5):
                try:
                    cursor.execute("UPDATE users SET parent_invite_code=%s WHERE id=%s", (code, uid))
                    connection.commit()
                    break
                except Exception:
                    code = make_invite()
            else:
                connection.rollback(); cursor.close(); connection.close()
                raise HTTPException(status_code=500, detail="failed to create invite code")
        cursor.execute(
            "SELECT u.id,u.username,u.display_name,l.created_at "
            "FROM parent_student_links l JOIN users u ON u.id=l.parent_id "
            "WHERE l.student_id=%s AND l.status='active' ORDER BY l.created_at",
            (uid,),
        )
        parents = cursor.fetchall()
        cursor.close(); connection.close()
        return {"invite_code": code, "parents": parents}

    @router.post("/api/students/me/parent-invite/rotate")
    def rotate_parent_invite(user=Depends(current_user)):
        require_role(user, "student")
        connection = require_db(mysql_connection)
        cursor = connection.cursor()
        uid = int(user["sub"])
        code = make_invite()
        try:
            cursor.execute("UPDATE users SET parent_invite_code=%s WHERE id=%s AND role='student'", (code, uid))
            if cursor.rowcount == 0:
                raise HTTPException(status_code=404, detail="student not found")
            connection.commit()
        except HTTPException:
            connection.rollback(); raise
        except Exception:
            connection.rollback(); raise
        finally:
            cursor.close(); connection.close()
        return {"invite_code": code}

    @router.post("/api/parents/bind")
    def bind_child(payload: BindPayload, user=Depends(current_user)):
        require_role(user, "parent")
        code = payload.invite_code.strip().upper()
        connection = require_db(mysql_connection)
        cursor = connection.cursor(dictionary=True)
        cursor.execute(
            "SELECT id,username,display_name FROM users WHERE parent_invite_code=%s AND role='student'",
            (code,),
        )
        student = cursor.fetchone()
        if not student:
            cursor.close(); connection.close()
            raise HTTPException(status_code=404, detail="invalid student invite code")
        parent_id = int(user["sub"])
        cursor.execute(
            "INSERT INTO parent_student_links(parent_id,student_id,status) VALUES(%s,%s,'active') "
            "ON DUPLICATE KEY UPDATE status='active'",
            (parent_id, student["id"]),
        )
        # 绑定成功后轮换邀请码，防止同一码被多人重复绑定
        rotate_parent_invite(cursor, int(student["id"]))
        connection.commit(); cursor.close(); connection.close()
        return {"student_id": student["id"], "display_name": student["display_name"], "bound": True}

    @router.post("/api/parents/children/{student_id}/unbind")
    def unbind_child(student_id: int, user=Depends(current_user)):
        require_role(user, "parent")
        connection = require_db(mysql_connection)
        cursor = connection.cursor()
        cursor.execute(
            "UPDATE parent_student_links SET status='revoked' WHERE parent_id=%s AND student_id=%s",
            (int(user["sub"]), student_id),
        )
        if cursor.rowcount == 0:
            connection.rollback(); cursor.close(); connection.close()
            raise HTTPException(status_code=404, detail="link not found")
        rotate_parent_invite(cursor, student_id)
        connection.commit(); cursor.close(); connection.close()
        return {"student_id": student_id, "unbound": True}

    @router.post("/api/students/me/parents/{parent_id}/revoke")
    def revoke_parent(parent_id: int, user=Depends(current_user)):
        require_role(user, "student")
        connection = require_db(mysql_connection)
        cursor = connection.cursor()
        uid = int(user["sub"])
        cursor.execute(
            "UPDATE parent_student_links SET status='revoked' WHERE parent_id=%s AND student_id=%s",
            (parent_id, uid),
        )
        if cursor.rowcount == 0:
            connection.rollback(); cursor.close(); connection.close()
            raise HTTPException(status_code=404, detail="link not found")
        new_code = rotate_parent_invite(cursor, uid)
        connection.commit(); cursor.close(); connection.close()
        return {"parent_id": parent_id, "revoked": True, "invite_code": new_code}

    @router.get("/api/parents/children")
    def list_children(user=Depends(current_user)):
        require_role(user, "parent")
        connection = require_db(mysql_connection)
        cursor = connection.cursor(dictionary=True)
        cursor.execute("SET time_zone = '+08:00'")
        cursor.execute(
            "UPDATE assignments SET status='closed' "
            "WHERE status='published' AND due_at IS NOT NULL AND due_at < NOW()"
        )
        if cursor.rowcount:
            connection.commit()
        cursor.execute(
            "SELECT u.id,u.username,u.display_name,l.created_at AS linked_at,"
            "(SELECT COUNT(*) FROM assignment_students s "
            " JOIN assignments a ON a.id=s.assignment_id AND a.status IN ('published','closed') "
            " WHERE s.student_id=u.id) AS assignment_count,"
            "(SELECT COUNT(*) FROM assignment_submissions sub "
            " WHERE sub.student_id=u.id AND sub.status IN ('submitted','reviewed')) AS submitted_count "
            "FROM parent_student_links l JOIN users u ON u.id=l.student_id "
            "WHERE l.parent_id=%s AND l.status='active' ORDER BY l.created_at",
            (int(user["sub"]),),
        )
        items = cursor.fetchall()
        cursor.close(); connection.close()
        return {"total": len(items), "items": items}

    @router.get("/api/parents/children/{student_id}/overview")
    def child_overview(student_id: int, user=Depends(current_user)):
        require_role(user, "parent")
        connection = require_db(mysql_connection)
        cursor = connection.cursor(dictionary=True)
        cursor.execute(
            "UPDATE assignments SET status='closed' "
            "WHERE status='published' AND due_at IS NOT NULL AND due_at < NOW()"
        )
        if cursor.rowcount:
            connection.commit()
        ensure_parent_of(cursor, int(user["sub"]), student_id)
        cursor.execute("SELECT id,username,display_name,role FROM users WHERE id=%s AND role='student'", (student_id,))
        student = cursor.fetchone()
        if not student:
            cursor.close(); connection.close()
            raise HTTPException(status_code=404, detail="student not found")

        cursor.execute(
            "SELECT a.id,a.title,a.status,a.due_at,a.class_id,c.name AS class_name,"
            "sub.status AS submission_status,sub.score,sub.teacher_comment,sub.submitted_at,"
            "s.urged_at,s.urge_count "
            "FROM assignment_students s "
            "JOIN assignments a ON a.id=s.assignment_id "
            "LEFT JOIN classes c ON c.id=a.class_id "
            "LEFT JOIN assignment_submissions sub ON sub.assignment_id=a.id AND sub.student_id=s.student_id "
            "WHERE s.student_id=%s AND a.status IN ('published','closed') "
            "ORDER BY a.id DESC LIMIT 50",
            (student_id,),
        )
        assignments = cursor.fetchall()

        cursor.execute(
            "SELECT id,reference_text,overall_score,accuracy_score,fluency_score,completeness_score,"
            "provider,tips,created_at,audio_url "
            "FROM pronunciation_attempts WHERE user_id=%s ORDER BY id DESC LIMIT 10",
            (student_id,),
        )
        pronunciation = cursor.fetchall()

        cursor.execute(
            "SELECT COUNT(*) AS total, "
            "SUM(CASE WHEN mastery>=80 THEN 1 ELSE 0 END) AS mastered "
            "FROM vocab_progress WHERE user_id=%s",
            (student_id,),
        )
        vocab = cursor.fetchone() or {"total": 0, "mastered": 0}

        submitted = sum(1 for a in assignments if a.get("submission_status") in ("submitted", "reviewed"))
        scored = [a["score"] for a in assignments if a.get("score") is not None]
        avg_score = round(sum(scored) / len(scored), 1) if scored else None

        cursor.close(); connection.close()
        return {
            "student": student,
            "summary": {
                "assignment_total": len(assignments),
                "submitted": submitted,
                "avg_score": avg_score,
                "vocab_mastered": int(vocab.get("mastered") or 0),
                "vocab_tracked": int(vocab.get("total") or 0),
            },
            "assignments": assignments,
            "pronunciation": pronunciation,
        }

    return router
