"""Textbook / chapter curriculum system."""
from __future__ import annotations

import json
from datetime import timedelta
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

router = APIRouter(tags=["curriculum"])


def require_db(mysql_connection):
    connection = mysql_connection()
    if not connection:
        raise HTTPException(status_code=503, detail="MySQL is required")
    return connection


def require_role(user: dict, *roles: str):
    if user.get("role") not in roles:
        raise HTTPException(status_code=403, detail="insufficient role")


def parse_json(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value)
    except Exception:
        return None


class TextbookCreate(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    level: str | None = Field(default=None, max_length=40)
    description: str | None = Field(default=None, max_length=500)


class ChapterCreate(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    objectives: str | None = Field(default=None, max_length=500)
    sort_order: int = Field(default=0, ge=0, le=9999)
    word_ids: list[int] = Field(default_factory=list)
    question_ids: list[int] = Field(default_factory=list)
    resource_filenames: list[str] = Field(default_factory=list)


class ClassTextbookPayload(BaseModel):
    textbook_id: int


class ChapterAssignPayload(BaseModel):
    class_id: int | None = None
    publish: bool = False
    due_days: int = Field(default=3, ge=1, le=60)
    include_voice: bool = False
    include_dictation: bool = False
    voice_prompt: str | None = Field(default=None, max_length=1000)


def register_curriculum_routes(mysql_connection, current_user):
    @router.post("/api/textbooks", status_code=201)
    def create_textbook(payload: TextbookCreate, user=Depends(current_user)):
        require_role(user, "teacher", "admin")
        connection = require_db(mysql_connection)
        cursor = connection.cursor()
        cursor.execute(
            "INSERT INTO textbooks(title,level,description,created_by) VALUES(%s,%s,%s,%s)",
            (payload.title.strip(), payload.level, payload.description, int(user["sub"])),
        )
        tid = cursor.lastrowid
        connection.commit(); cursor.close(); connection.close()
        return {"id": tid, "title": payload.title.strip()}

    @router.get("/api/textbooks")
    def list_textbooks(user=Depends(current_user)):
        connection = require_db(mysql_connection)
        cursor = connection.cursor(dictionary=True)
        cursor.execute(
            "SELECT t.*, "
            "(SELECT COUNT(*) FROM chapters c WHERE c.textbook_id=t.id) AS chapter_count "
            "FROM textbooks t WHERE t.status='active' ORDER BY t.id DESC"
        )
        items = cursor.fetchall()
        cursor.close(); connection.close()
        return {"total": len(items), "items": items}

    @router.post("/api/textbooks/{textbook_id}/chapters", status_code=201)
    def create_chapter(textbook_id: int, payload: ChapterCreate, user=Depends(current_user)):
        require_role(user, "teacher", "admin")
        connection = require_db(mysql_connection)
        cursor = connection.cursor(dictionary=True)
        cursor.execute("SELECT id FROM textbooks WHERE id=%s AND status='active'", (textbook_id,))
        if not cursor.fetchone():
            cursor.close(); connection.close()
            raise HTTPException(status_code=404, detail="textbook not found")
        cursor.execute(
            "INSERT INTO chapters(textbook_id,title,objectives,sort_order,word_ids_json,question_ids_json,resource_filenames_json) "
            "VALUES(%s,%s,%s,%s,%s,%s,%s)",
            (
                textbook_id, payload.title.strip(), payload.objectives, payload.sort_order,
                json.dumps(payload.word_ids), json.dumps(payload.question_ids),
                json.dumps(payload.resource_filenames),
            ),
        )
        cid = cursor.lastrowid
        connection.commit(); cursor.close(); connection.close()
        return {"id": cid, "textbook_id": textbook_id}

    @router.get("/api/textbooks/{textbook_id}/chapters")
    def list_chapters(textbook_id: int, user=Depends(current_user)):
        connection = require_db(mysql_connection)
        cursor = connection.cursor(dictionary=True)
        cursor.execute("SELECT id,title,level FROM textbooks WHERE id=%s", (textbook_id,))
        book = cursor.fetchone()
        if not book:
            cursor.close(); connection.close()
            raise HTTPException(status_code=404, detail="textbook not found")
        cursor.execute(
            "SELECT * FROM chapters WHERE textbook_id=%s ORDER BY sort_order,id",
            (textbook_id,),
        )
        chapters = cursor.fetchall()
        for ch in chapters:
            ch["word_ids"] = parse_json(ch.pop("word_ids_json")) or []
            ch["question_ids"] = parse_json(ch.pop("question_ids_json")) or []
            ch["resource_filenames"] = parse_json(ch.pop("resource_filenames_json")) or []
        cursor.close(); connection.close()
        return {"textbook": book, "total": len(chapters), "items": chapters}

    @router.post("/api/classes/{class_id}/textbooks")
    def assign_textbook(class_id: int, payload: ClassTextbookPayload, user=Depends(current_user)):
        require_role(user, "teacher", "admin")
        connection = require_db(mysql_connection)
        cursor = connection.cursor(dictionary=True)
        cursor.execute("SELECT id,teacher_id FROM classes WHERE id=%s", (class_id,))
        clazz = cursor.fetchone()
        if not clazz:
            cursor.close(); connection.close()
            raise HTTPException(status_code=404, detail="class not found")
        if user["role"] != "admin" and clazz["teacher_id"] != int(user["sub"]):
            cursor.close(); connection.close()
            raise HTTPException(status_code=403, detail="not your class")
        cursor.execute("SELECT id FROM textbooks WHERE id=%s AND status='active'", (payload.textbook_id,))
        if not cursor.fetchone():
            cursor.close(); connection.close()
            raise HTTPException(status_code=404, detail="textbook not found")
        cursor.execute(
            "INSERT IGNORE INTO class_textbooks(class_id,textbook_id) VALUES(%s,%s)",
            (class_id, payload.textbook_id),
        )
        connection.commit(); cursor.close(); connection.close()
        return {"class_id": class_id, "textbook_id": payload.textbook_id, "assigned": True}

    @router.get("/api/classes/{class_id}/textbooks")
    def class_textbooks(class_id: int, user=Depends(current_user)):
        connection = require_db(mysql_connection)
        cursor = connection.cursor(dictionary=True)
        uid = int(user["sub"])
        cursor.execute("SELECT id,teacher_id FROM classes WHERE id=%s", (class_id,))
        clazz = cursor.fetchone()
        if not clazz:
            cursor.close(); connection.close()
            raise HTTPException(status_code=404, detail="class not found")
        if user["role"] not in ("teacher", "admin") or (
            user["role"] == "teacher" and clazz["teacher_id"] != uid
        ):
            cursor.execute(
                "SELECT 1 FROM class_members WHERE class_id=%s AND user_id=%s",
                (class_id, uid),
            )
            if not cursor.fetchone() and user["role"] != "admin":
                cursor.close(); connection.close()
                raise HTTPException(status_code=403, detail="forbidden")
        cursor.execute(
            "SELECT t.*,ct.assigned_at FROM class_textbooks ct "
            "JOIN textbooks t ON t.id=ct.textbook_id WHERE ct.class_id=%s ORDER BY t.id",
            (class_id,),
        )
        items = cursor.fetchall()
        cursor.close(); connection.close()
        return {"total": len(items), "items": items}

    @router.post("/api/chapters/{chapter_id}/assign-homework", status_code=201)
    def assign_chapter_homework(
        chapter_id: int,
        payload: ChapterAssignPayload | None = None,
        user=Depends(current_user),
    ):
        """Quick-create assignment from chapter packs; optional class_id + publish."""
        require_role(user, "teacher", "admin")
        from backend.homework import now_cst_naive, sync_assignment_class_members
        from backend.notify import notify_assignment_published

        payload = payload or ChapterAssignPayload()
        connection = require_db(mysql_connection)
        cursor = connection.cursor(dictionary=True)
        cursor.execute(
            "SELECT c.*,t.title AS book_title FROM chapters c "
            "JOIN textbooks t ON t.id=c.textbook_id WHERE c.id=%s",
            (chapter_id,),
        )
        chapter = cursor.fetchone()
        if not chapter:
            cursor.close(); connection.close()
            raise HTTPException(status_code=404, detail="chapter not found")
        class_id = payload.class_id
        if class_id:
            cursor.execute("SELECT id,teacher_id,status FROM classes WHERE id=%s", (class_id,))
            clazz = cursor.fetchone()
            if not clazz or clazz["status"] != "active":
                cursor.close(); connection.close()
                raise HTTPException(status_code=404, detail="class not found")
            if user["role"] != "admin" and clazz["teacher_id"] != int(user["sub"]):
                cursor.close(); connection.close()
                raise HTTPException(status_code=403, detail="not your class")
        word_ids = parse_json(chapter.get("word_ids_json")) or []
        question_ids = parse_json(chapter.get("question_ids_json")) or []
        resources = parse_json(chapter.get("resource_filenames_json")) or []
        tasks = []
        if question_ids:
            tasks.append(("choice", "章节选择题", {"question_ids": [int(x) for x in question_ids]}))
        if word_ids:
            tasks.append(("vocab", "章节单词", {"word_ids": [int(x) for x in word_ids]}))
            if payload.include_dictation:
                tasks.append(("dictation", "章节听写", {"word_ids": [int(x) for x in word_ids]}))
            if payload.include_voice:
                sample = ""
                if word_ids:
                    cursor.execute(
                        "SELECT word FROM words WHERE id=%s",
                        (int(word_ids[0]),),
                    )
                    wrow = cursor.fetchone()
                    sample = (wrow or {}).get("word") or ""
                prompt = (payload.voice_prompt or "").strip() or (
                    f"Please read aloud: {sample}" if sample else "Please read aloud clearly."
                )
                tasks.append(("voice", "章节纠音", {"prompt": prompt, "pass_score": 60, "min_seconds": 2}))
        if resources:
            tasks.append((
                "video", "章节视频",
                {"resource_filename": resources[0], "min_seconds": 10},
            ))
        if not tasks:
            cursor.close(); connection.close()
            raise HTTPException(status_code=400, detail="chapter has no content packs")
        title = f"{chapter['book_title']} · {chapter['title']}"
        status = "published" if payload.publish and class_id else "draft"
        due_at = (now_cst_naive() + timedelta(days=int(payload.due_days or 3))) if status == "published" else None
        cursor.execute(
            "INSERT INTO assignments(title,description,teacher_id,class_id,status,due_at,published_at) "
            "VALUES(%s,%s,%s,%s,%s,%s,IF(%s='published',NOW(),NULL))",
            (title, chapter.get("objectives"), int(user["sub"]), class_id, status, due_at, status),
        )
        aid = cursor.lastrowid
        for idx, (ttype, ttitle, cfg) in enumerate(tasks):
            cursor.execute(
                "INSERT INTO assignment_tasks(assignment_id,task_type,title,sort_order,config_json) "
                "VALUES(%s,%s,%s,%s,%s)",
                (aid, ttype, ttitle, idx, json.dumps(cfg, ensure_ascii=False)),
            )
        student_ids: list[int] = []
        if class_id:
            linked = sync_assignment_class_members(cursor, aid, int(class_id))
            cursor.execute(
                "SELECT student_id FROM assignment_students WHERE assignment_id=%s",
                (aid,),
            )
            student_ids = [int(r["student_id"]) for r in cursor.fetchall()]
        else:
            linked = 0
        connection.commit(); cursor.close(); connection.close()
        notified = {"students": 0, "parents": 0}
        if status == "published" and student_ids:
            notified = notify_assignment_published(mysql_connection, aid, title, student_ids)
        return {
            "assignment_id": aid, "status": status, "task_count": len(tasks),
            "title": title, "class_id": class_id, "members_synced": linked,
            "notified": notified, "created_at_ref": str(now_cst_naive()),
        }

    return router
