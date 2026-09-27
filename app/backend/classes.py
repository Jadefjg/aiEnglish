"""Class / classroom management APIs."""
from __future__ import annotations

import secrets
import string

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

router = APIRouter(tags=["classes"])


def require_db(mysql_connection):
    connection = mysql_connection()
    if not connection:
        raise HTTPException(status_code=503, detail="MySQL is required for class features")
    return connection


def require_role(user: dict, *roles: str):
    if user.get("role") not in roles:
        raise HTTPException(status_code=403, detail="insufficient role")


def invite_code(length: int = 6) -> str:
    alphabet = string.ascii_uppercase + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(length))


class ClassCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    description: str | None = Field(default=None, max_length=500)


class ClassUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    description: str | None = Field(default=None, max_length=500)
    status: str | None = Field(default=None, pattern="^(active|archived)$")


class MembersPayload(BaseModel):
    student_ids: list[int] = Field(min_length=1)


class JoinPayload(BaseModel):
    invite_code: str = Field(min_length=4, max_length=16)


def sync_class_homework(cursor, class_id: int, student_ids: list[int]) -> int:
    """把班级已发布/草稿作业同步给新加入学员，避免后加入者看不到作业。"""
    if not student_ids:
        return 0
    cursor.execute(
        "SELECT id FROM assignments WHERE class_id=%s AND status IN ('published','draft')",
        (class_id,),
    )
    linked = 0
    for row in cursor.fetchall():
        for sid in student_ids:
            cursor.execute(
                "INSERT IGNORE INTO assignment_students(assignment_id, student_id) VALUES(%s,%s)",
                (row["id"], int(sid)),
            )
            linked += cursor.rowcount
    return linked


def register_class_routes(mysql_connection, current_user):
    @router.post("/api/classes", status_code=201)
    def create_class(payload: ClassCreate, user=Depends(current_user)):
        require_role(user, "teacher", "admin")
        connection = require_db(mysql_connection)
        cursor = connection.cursor(dictionary=True)
        code = invite_code()
        for _ in range(5):
            cursor.execute("SELECT 1 FROM classes WHERE invite_code=%s", (code,))
            if not cursor.fetchone():
                break
            code = invite_code()
        cursor.execute(
            "INSERT INTO classes(name,description,teacher_id,invite_code) VALUES(%s,%s,%s,%s)",
            (payload.name.strip(), payload.description, int(user["sub"]), code),
        )
        class_id = cursor.lastrowid
        connection.commit()
        cursor.close(); connection.close()
        return {"id": class_id, "name": payload.name.strip(), "invite_code": code, "status": "active"}

    @router.get("/api/classes")
    def list_classes(user=Depends(current_user)):
        connection = require_db(mysql_connection)
        cursor = connection.cursor(dictionary=True)
        uid = int(user["sub"])
        role = user["role"]
        if role in ("teacher", "admin"):
            if role == "admin":
                cursor.execute(
                    "SELECT c.id,c.name,c.description,c.invite_code,c.status,c.teacher_id,c.created_at,"
                    "u.display_name AS teacher_name,"
                    "(SELECT COUNT(*) FROM class_members m WHERE m.class_id=c.id) AS member_count "
                    "FROM classes c JOIN users u ON u.id=c.teacher_id ORDER BY c.id DESC"
                )
            else:
                cursor.execute(
                    "SELECT c.id,c.name,c.description,c.invite_code,c.status,c.teacher_id,c.created_at,"
                    "u.display_name AS teacher_name,"
                    "(SELECT COUNT(*) FROM class_members m WHERE m.class_id=c.id) AS member_count "
                    "FROM classes c JOIN users u ON u.id=c.teacher_id WHERE c.teacher_id=%s ORDER BY c.id DESC",
                    (uid,),
                )
        else:
            cursor.execute(
                "SELECT c.id,c.name,c.description,c.status,c.teacher_id,c.created_at,"
                "u.display_name AS teacher_name,"
                "(SELECT COUNT(*) FROM class_members m WHERE m.class_id=c.id) AS member_count "
                "FROM classes c JOIN class_members cm ON cm.class_id=c.id AND cm.user_id=%s "
                "JOIN users u ON u.id=c.teacher_id WHERE c.status='active' ORDER BY c.id DESC",
                (uid,),
            )
        items = cursor.fetchall()
        cursor.close(); connection.close()
        return {"total": len(items), "items": items}

    @router.get("/api/classes/{class_id}")
    def get_class(class_id: int, user=Depends(current_user)):
        connection = require_db(mysql_connection)
        cursor = connection.cursor(dictionary=True)
        cursor.execute(
            "SELECT c.id,c.name,c.description,c.invite_code,c.status,c.teacher_id,c.created_at,"
            "u.display_name AS teacher_name FROM classes c JOIN users u ON u.id=c.teacher_id WHERE c.id=%s",
            (class_id,),
        )
        item = cursor.fetchone()
        if not item:
            cursor.close(); connection.close()
            raise HTTPException(status_code=404, detail="class not found")
        uid = int(user["sub"])
        is_teacher = user["role"] in ("teacher", "admin") and (user["role"] == "admin" or item["teacher_id"] == uid)
        if not is_teacher:
            cursor.execute("SELECT 1 FROM class_members WHERE class_id=%s AND user_id=%s", (class_id, uid))
            if not cursor.fetchone():
                cursor.close(); connection.close()
                raise HTTPException(status_code=403, detail="not a class member")
        cursor.execute(
            "SELECT u.id,u.username,u.display_name,cm.member_role,cm.joined_at "
            "FROM class_members cm JOIN users u ON u.id=cm.user_id WHERE cm.class_id=%s ORDER BY cm.joined_at",
            (class_id,),
        )
        item["members"] = cursor.fetchall()
        if not is_teacher:
            item.pop("invite_code", None)
        cursor.close(); connection.close()
        return item

    @router.put("/api/classes/{class_id}")
    def update_class(class_id: int, payload: ClassUpdate, user=Depends(current_user)):
        require_role(user, "teacher", "admin")
        connection = require_db(mysql_connection)
        cursor = connection.cursor(dictionary=True)
        cursor.execute("SELECT id,teacher_id FROM classes WHERE id=%s", (class_id,))
        item = cursor.fetchone()
        if not item:
            cursor.close(); connection.close()
            raise HTTPException(status_code=404, detail="class not found")
        if user["role"] != "admin" and item["teacher_id"] != int(user["sub"]):
            cursor.close(); connection.close()
            raise HTTPException(status_code=403, detail="not your class")
        fields, values = [], []
        if payload.name is not None:
            fields.append("name=%s"); values.append(payload.name.strip())
        if payload.description is not None:
            fields.append("description=%s"); values.append(payload.description)
        if payload.status is not None:
            fields.append("status=%s"); values.append(payload.status)
        if not fields:
            cursor.close(); connection.close()
            raise HTTPException(status_code=400, detail="no fields to update")
        values.append(class_id)
        cursor.execute(f"UPDATE classes SET {', '.join(fields)} WHERE id=%s", tuple(values))
        connection.commit(); cursor.close(); connection.close()
        return {"id": class_id, "updated": True}

    @router.post("/api/classes/{class_id}/members", status_code=201)
    def add_members(class_id: int, payload: MembersPayload, user=Depends(current_user)):
        require_role(user, "teacher", "admin")
        connection = require_db(mysql_connection)
        cursor = connection.cursor(dictionary=True)
        cursor.execute("SELECT id,teacher_id FROM classes WHERE id=%s", (class_id,))
        item = cursor.fetchone()
        if not item:
            cursor.close(); connection.close()
            raise HTTPException(status_code=404, detail="class not found")
        if user["role"] != "admin" and item["teacher_id"] != int(user["sub"]):
            cursor.close(); connection.close()
            raise HTTPException(status_code=403, detail="not your class")
        added_ids = []
        for sid in payload.student_ids:
            cursor.execute("SELECT id,role FROM users WHERE id=%s", (int(sid),))
            stu = cursor.fetchone()
            if not stu or stu["role"] != "student":
                continue
            cursor.execute(
                "INSERT IGNORE INTO class_members(class_id,user_id,member_role) VALUES(%s,%s,'student')",
                (class_id, int(sid)),
            )
            if cursor.rowcount:
                added_ids.append(int(sid))
        homework_linked = sync_class_homework(cursor, class_id, added_ids)
        connection.commit(); cursor.close(); connection.close()
        return {"class_id": class_id, "added": len(added_ids), "homework_linked": homework_linked}

    @router.post("/api/classes/{class_id}/members/remove")
    def remove_members(class_id: int, payload: MembersPayload, user=Depends(current_user)):
        require_role(user, "teacher", "admin")
        connection = require_db(mysql_connection)
        cursor = connection.cursor(dictionary=True)
        cursor.execute("SELECT id,teacher_id FROM classes WHERE id=%s", (class_id,))
        item = cursor.fetchone()
        if not item:
            cursor.close(); connection.close()
            raise HTTPException(status_code=404, detail="class not found")
        if user["role"] != "admin" and item["teacher_id"] != int(user["sub"]):
            cursor.close(); connection.close()
            raise HTTPException(status_code=403, detail="not your class")
        removed = 0
        homework_unlinked = 0
        for sid in payload.student_ids:
            cursor.execute("DELETE FROM class_members WHERE class_id=%s AND user_id=%s", (class_id, int(sid)))
            removed += cursor.rowcount
            # 清理进行中的提交，避免孤儿进度
            cursor.execute(
                "DELETE sub FROM assignment_submissions sub "
                "JOIN assignments a ON a.id=sub.assignment_id "
                "WHERE a.class_id=%s AND sub.student_id=%s AND sub.status='in_progress'",
                (class_id, int(sid)),
            )
            # 退班后摘掉未交卷的班级作业（已提交/已批改保留学籍痕迹）
            cursor.execute(
                "DELETE s FROM assignment_students s "
                "JOIN assignments a ON a.id=s.assignment_id "
                "LEFT JOIN assignment_submissions sub "
                "ON sub.assignment_id=s.assignment_id AND sub.student_id=s.student_id "
                "WHERE a.class_id=%s AND s.student_id=%s "
                "AND (sub.id IS NULL OR sub.status='in_progress')",
                (class_id, int(sid)),
            )
            homework_unlinked += cursor.rowcount or 0
        connection.commit(); cursor.close(); connection.close()
        return {"class_id": class_id, "removed": removed, "homework_unlinked": homework_unlinked}

    @router.post("/api/classes/join")
    def join_class(payload: JoinPayload, user=Depends(current_user)):
        require_role(user, "student")
        code = payload.invite_code.strip().upper()
        connection = require_db(mysql_connection)
        cursor = connection.cursor(dictionary=True)
        cursor.execute(
            "SELECT id,name,status FROM classes WHERE invite_code=%s",
            (code,),
        )
        item = cursor.fetchone()
        if not item or item["status"] != "active":
            cursor.close(); connection.close()
            raise HTTPException(status_code=404, detail="invalid invite code")
        uid = int(user["sub"])
        cursor.execute(
            "INSERT IGNORE INTO class_members(class_id,user_id,member_role) VALUES(%s,%s,'student')",
            (item["id"], uid),
        )
        newly_joined = cursor.rowcount > 0
        homework_linked = sync_class_homework(cursor, item["id"], [uid]) if newly_joined else 0
        # 已在班内时也补同步一次，修复历史漏发作业
        if not newly_joined:
            homework_linked = sync_class_homework(cursor, item["id"], [uid])
        connection.commit(); cursor.close(); connection.close()
        return {
            "class_id": item["id"], "name": item["name"], "joined": True,
            "newly_joined": newly_joined, "homework_linked": homework_linked,
        }

    @router.post("/api/classes/{class_id}/regenerate-invite")
    def regenerate_invite(class_id: int, user=Depends(current_user)):
        require_role(user, "teacher", "admin")
        connection = require_db(mysql_connection)
        cursor = connection.cursor(dictionary=True)
        cursor.execute("SELECT id,teacher_id FROM classes WHERE id=%s", (class_id,))
        item = cursor.fetchone()
        if not item:
            cursor.close(); connection.close()
            raise HTTPException(status_code=404, detail="class not found")
        if user["role"] != "admin" and item["teacher_id"] != int(user["sub"]):
            cursor.close(); connection.close()
            raise HTTPException(status_code=403, detail="not your class")
        code = invite_code()
        cursor.execute("UPDATE classes SET invite_code=%s WHERE id=%s", (code, class_id))
        connection.commit(); cursor.close(); connection.close()
        return {"id": class_id, "invite_code": code}

    return router
