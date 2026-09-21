import json
import os
import subprocess
import base64
import hashlib
import hmac
import time
import threading
from pathlib import Path
from urllib.parse import quote

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

ROOT = Path(__file__).resolve().parents[2]
ASSET_DIR = Path(os.getenv("ASSET_DIR", ROOT / "asset"))
FRONTEND_DIR = ROOT / "app" / "frontend"
CATALOG_PATH = Path(__file__).with_name("catalog.json")
AUDIT_PATH = Path(__file__).with_name("reports") / "asset_audit.json"

ENVIRONMENT = os.getenv("ENVIRONMENT", "development")
JWT_SECRET = os.getenv("JWT_SECRET", "dev-only-change-this-secret")
if ENVIRONMENT == "production" and (len(JWT_SECRET) < 32 or JWT_SECRET == "dev-only-change-this-secret"):
    raise RuntimeError("JWT_SECRET must be a strong external secret in production")
_rate_lock = threading.Lock()
_rate_buckets: dict[str, list[float]] = {}
_failed_logins: dict[str, list[float]] = {}
bearer = HTTPBearer(auto_error=False)
app = FastAPI(title="领智云英语教程中心", version="2.1.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["GET", "POST", "PUT"], allow_headers=["*"])
app.mount("/assets", StaticFiles(directory=ASSET_DIR), name="assets")
VUE_VENDOR_DIR = FRONTEND_DIR / "node_modules" / "vue" / "dist"
if VUE_VENDOR_DIR.exists():
    app.mount("/vendor", StaticFiles(directory=VUE_VENDOR_DIR), name="vendor")


def load_catalog():
    return json.loads(CATALOG_PATH.read_text(encoding="utf-8"))


def load_audit():
    return json.loads(AUDIT_PATH.read_text(encoding="utf-8"))


def media_items():
    metadata = {item["filename"]: item for item in load_catalog().get("media", [])}
    items = []
    for index, path in enumerate(sorted(ASSET_DIR.iterdir()), 1):
        suffix = path.suffix.lower()
        if suffix not in {".mp4", ".pdf"}:
            continue
        duration = None
        if suffix == ".mp4":
            try:
                value = subprocess.check_output(
                    ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=nw=1:nk=1", str(path)],
                    text=True, timeout=2,
                )
                duration = round(float(value.strip()))
            except (OSError, ValueError, subprocess.SubprocessError):
                pass
        meta = metadata.get(path.name, {})
        role = meta.get("role", "student" if "学生" in path.name else "teacher" if "老师" in path.name else "general")
        items.append({"id": index, "title": meta.get("title", path.stem), "filename": path.name, "type": suffix[1:],
                      "role": role, "duration": duration, "size": path.stat().st_size, "feature_id": meta.get("feature_id"),
                      "summary": meta.get("summary", ""), "duplicate_of": meta.get("duplicate_of"),
                      "url": f"/assets/{quote(path.name)}"})
    return items


def sync_mysql():
    """Synchronize the file catalog when MySQL is configured; local development can run without it."""
    host = os.getenv("MYSQL_HOST")
    if not host:
        return False
    import mysql.connector
    connection = mysql.connector.connect(
        host=host, port=int(os.getenv("MYSQL_PORT", "3306")),
        user=os.getenv("MYSQL_USER", "root"), password=os.getenv("MYSQL_PASSWORD", ""),
        database=os.getenv("MYSQL_DATABASE", "aienglish"), charset="utf8mb4",
    )
    cursor = connection.cursor()
    try:
      for item in load_catalog()["features"]:
        cursor.execute(
            "INSERT INTO features (id,role,title,description,manual_pages,sort_order) VALUES (%s,%s,%s,%s,%s,%s) "
            "ON DUPLICATE KEY UPDATE role=VALUES(role),title=VALUES(title),description=VALUES(description),manual_pages=VALUES(manual_pages),sort_order=VALUES(sort_order)",
            (item["id"], item["role"], item["title"], item["description"], item["pages"], item["id"]),
        )
      for item in media_items():
        cursor.execute(
            "INSERT INTO resources (title,role,type,filename,url,duration_seconds,size_bytes) VALUES (%s,%s,%s,%s,%s,%s,%s) "
            "ON DUPLICATE KEY UPDATE title=VALUES(title),role=VALUES(role),type=VALUES(type),url=VALUES(url),duration_seconds=VALUES(duration_seconds),size_bytes=VALUES(size_bytes)",
            (item["title"], item["role"], item["type"], item["filename"], item["url"], item["duration"], item["size"]),
        )
        cursor.execute("SELECT id FROM resources WHERE filename=%s", (item["filename"],))
        resource_id = cursor.fetchone()[0]
        if item.get("feature_id") and not item.get("duplicate_of"):
            cursor.execute(
                "INSERT IGNORE INTO feature_resources (feature_id,resource_id) VALUES (%s,%s)",
                (item["feature_id"], resource_id),
            )
      admin_username = os.getenv("BOOTSTRAP_ADMIN_USERNAME")
      admin_password = os.getenv("BOOTSTRAP_ADMIN_PASSWORD")
      if admin_username and admin_password:
        if len(admin_password) < 12:
            raise RuntimeError("BOOTSTRAP_ADMIN_PASSWORD must be at least 12 characters")
        cursor.execute("SELECT id FROM users WHERE username=%s", (admin_username,))
        if not cursor.fetchone():
            cursor.execute("INSERT INTO users(username,password_hash,display_name,role) VALUES(%s,%s,%s,'admin')",
                           (admin_username, password_hash(admin_password), "系统管理员"))
      connection.commit()
    except Exception:
      connection.rollback()
      raise
    finally:
      cursor.close()
      connection.close()
    return True


def mysql_connection():
    if not os.getenv("MYSQL_HOST"):
        return None
    import mysql.connector
    return mysql.connector.connect(
        host=os.getenv("MYSQL_HOST"), port=int(os.getenv("MYSQL_PORT", "3306")),
        user=os.getenv("MYSQL_USER", "root"), password=os.getenv("MYSQL_PASSWORD", ""),
        database=os.getenv("MYSQL_DATABASE", "aienglish"), charset="utf8mb4",
    )


class ProgressUpdate(BaseModel):
    progress: int = Field(ge=0, le=100)


class UserCreate(BaseModel):
    username: str = Field(min_length=3, max_length=50, pattern="^[A-Za-z0-9_.-]+$")
    password: str = Field(min_length=8, max_length=128)
    display_name: str = Field(min_length=1, max_length=100)
    role: str = Field(pattern="^(student|teacher|parent)$")


class AdminUserCreate(BaseModel):
    username: str = Field(min_length=3, max_length=50, pattern="^[A-Za-z0-9_.-]+$")
    password: str = Field(min_length=8, max_length=128)
    display_name: str = Field(min_length=1, max_length=100)
    role: str = Field(pattern="^(student|teacher|parent|admin)$")


class LoginRequest(BaseModel):
    username: str
    password: str


def guard_request(request: Request, bucket: str, limit: int, window: int = 60):
    now = time.time(); key = bucket + ":" + client_ip(request)
    with _rate_lock:
        values = [t for t in _rate_buckets.get(key, []) if now - t < window]
        if len(values) >= limit: raise HTTPException(status_code=429, detail="too many requests; retry later")
        values.append(now); _rate_buckets[key] = values


def client_ip(request: Request) -> str:
    if ENVIRONMENT == "production":
        forwarded = request.headers.get("x-real-ip", "").strip()
        if forwarded: return forwarded
    return request.client.host if request.client else "unknown"


def failed_login(key: str):
    now = time.time()
    with _rate_lock:
        values = [t for t in _failed_logins.get(key, []) if now - t < 900]
        values.append(now); _failed_logins[key] = values
        return len(values) >= 5


def login_locked(key: str):
    now = time.time()
    with _rate_lock:
        return len([t for t in _failed_logins.get(key, []) if now - t < 900]) >= 5


def password_hash(password: str, salt: bytes | None = None) -> str:
    salt = salt or os.urandom(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 180000)
    return base64.urlsafe_b64encode(salt).decode() + "." + base64.urlsafe_b64encode(digest).decode()


def password_ok(password: str, encoded: str) -> bool:
    try:
        salt, expected = encoded.split(".")
        actual = password_hash(password, base64.urlsafe_b64decode(salt + "==")).split(".", 1)[1]
        return hmac.compare_digest(actual, expected)
    except (ValueError, TypeError):
        return False


def make_token(user: dict) -> str:
    header = {"alg": "HS256", "typ": "JWT"}
    payload = {"sub": str(user["id"]), "username": user["username"], "role": user["role"], "exp": int(time.time()) + 86400}
    def enc(value): return base64.urlsafe_b64encode(json.dumps(value, separators=(",", ":")).encode()).rstrip(b"=").decode()
    signing = enc(header) + "." + enc(payload)
    sig = hmac.new(JWT_SECRET.encode(), signing.encode(), hashlib.sha256).digest()
    return signing + "." + base64.urlsafe_b64encode(sig).rstrip(b"=").decode()


def current_user(credentials: HTTPAuthorizationCredentials | None = Depends(bearer)):
    if not credentials or credentials.scheme.lower() != "bearer":
        raise HTTPException(status_code=401, detail="authentication required")
    try:
        head, body, signature = credentials.credentials.split(".")
        signing = head + "." + body
        expected = base64.urlsafe_b64encode(hmac.new(JWT_SECRET.encode(), signing.encode(), hashlib.sha256).digest()).rstrip(b"=").decode()
        if not hmac.compare_digest(signature, expected): raise ValueError
        payload = json.loads(base64.urlsafe_b64decode(body + "=="))
        if int(payload["exp"]) < int(time.time()): raise ValueError
        return payload
    except (ValueError, KeyError, json.JSONDecodeError):
        raise HTTPException(status_code=401, detail="invalid or expired token")


@app.on_event("startup")
def startup_sync():
    app.state.database_ready = False
    try:
        app.state.database_ready = sync_mysql()
    except Exception as exc:
        app.state.database_error = str(exc)


@app.get("/api/health")
def health():
    configured = bool(os.getenv("MYSQL_HOST"))
    ready = getattr(app.state, "database_ready", False)
    return {"status": "ok" if ready or not configured else "degraded", "service": "aienglish", "asset_count": len(media_items()),
            "database": "connected" if getattr(app.state, "database_ready", False) else "file-fallback"}


@app.get("/api/audit")
def audit():
    return load_audit()


@app.post("/api/auth/register", status_code=201)
def register(payload: UserCreate, request: Request):
    guard_request(request, "register", 5)
    connection = mysql_connection()
    if not connection: raise HTTPException(status_code=503, detail="MySQL is required for authentication")
    cursor = connection.cursor(dictionary=True)
    try:
        cursor.execute("INSERT INTO users(username,password_hash,display_name,role) VALUES(%s,%s,%s,%s)",
                       (payload.username, password_hash(payload.password), payload.display_name.strip(), payload.role))
        user_id = cursor.lastrowid; connection.commit()
    except Exception as exc:
        connection.rollback()
        if "Duplicate" in str(exc): raise HTTPException(status_code=409, detail="username already exists")
        raise
    finally:
        cursor.close(); connection.close()
    return {"id": user_id, "username": payload.username, "display_name": payload.display_name.strip(), "role": payload.role}


@app.post("/api/auth/login")
def login(payload: LoginRequest, request: Request):
    guard_request(request, "login", 10)
    ip = client_ip(request)
    lock_key = ip + ":" + payload.username.casefold()
    if login_locked(lock_key): raise HTTPException(status_code=429, detail="login temporarily locked")
    connection = mysql_connection()
    if not connection: raise HTTPException(status_code=503, detail="MySQL is required for authentication")
    cursor = connection.cursor(dictionary=True)
    cursor.execute("SELECT id,username,password_hash,display_name,role FROM users WHERE username=%s", (payload.username,))
    user = cursor.fetchone(); cursor.close(); connection.close()
    if not user or not password_ok(payload.password, user["password_hash"]):
        failed_login(lock_key)
        raise HTTPException(status_code=401, detail="invalid username or password")
    with _rate_lock: _failed_logins.pop(lock_key, None)
    user.pop("password_hash", None)
    return {"access_token": make_token(user), "token_type": "bearer", "user": user}


@app.get("/api/auth/me")
def me(user=Depends(current_user)):
    return user


@app.get("/api/features")
def features(role: str = Query("all"), q: str = Query("")):
    role = role if isinstance(role, str) else "all"
    q = q if isinstance(q, str) else ""
    data = load_catalog()["features"]
    if role != "all":
        data = [item for item in data if item["role"] == role]
    if q.strip():
        needle = q.strip().casefold()
        data = [item for item in data if needle in (item["title"] + item["description"]).casefold()]
    media = media_items()
    data = [{**item, "resources": [x for x in media if x.get("feature_id") == item["id"] and not x.get("duplicate_of")]} for item in data]
    return {"total": len(data), "items": data}


@app.get("/api/features/{feature_id}")
def feature(feature_id: int):
    item = next((x for x in load_catalog()["features"] if x["id"] == feature_id), None)
    if not item: raise HTTPException(status_code=404, detail="feature not found")
    return {**item, "resources": [x for x in media_items() if x.get("feature_id") == feature_id and not x.get("duplicate_of")]}


@app.get("/api/resources")
def resources(role: str = Query("all"), kind: str = Query("all"), q: str = Query("")):
    role = role if isinstance(role, str) else "all"
    kind = kind if isinstance(kind, str) else "all"
    q = q if isinstance(q, str) else ""
    data = media_items()
    if role != "all": data = [item for item in data if item["role"] == role]
    if kind != "all": data = [item for item in data if item["type"] == kind]
    if q.strip(): data = [item for item in data if q.strip().casefold() in item["title"].casefold()]
    return {"total": len(data), "items": data}


@app.get("/api/resources/{resource_id}")
def resource(resource_id: int):
    item = next((item for item in media_items() if item["id"] == resource_id), None)
    if not item: raise HTTPException(status_code=404, detail="resource not found")
    return item


@app.get("/api/users/{user_id}/progress")
def get_progress(user_id: int, user=Depends(current_user)):
    if int(user["sub"]) != user_id and user["role"] != "admin":
        raise HTTPException(status_code=403, detail="cannot access another user's progress")
    connection = mysql_connection()
    if not connection: raise HTTPException(status_code=503, detail="MySQL is required for progress tracking")
    cursor = connection.cursor(dictionary=True)
    cursor.execute("SELECT feature_id,progress,completed_at FROM learning_progress WHERE user_id=%s", (user_id,))
    rows = cursor.fetchall(); cursor.close(); connection.close()
    return {"user_id": user_id, "items": rows}


@app.post("/api/admin/users", status_code=201)
def create_user(payload: AdminUserCreate, request: Request, user=Depends(current_user)):
    if user["role"] != "admin": raise HTTPException(status_code=403, detail="admin required")
    connection = mysql_connection()
    if not connection: raise HTTPException(status_code=503, detail="MySQL is required for user management")
    cursor = connection.cursor()
    try:
        cursor.execute("INSERT INTO users(username,password_hash,display_name,role) VALUES(%s,%s,%s,%s)",
                       (payload.username, password_hash(payload.password), payload.display_name.strip(), payload.role))
        user_id = cursor.lastrowid; connection.commit()
    except Exception as exc:
        connection.rollback()
        if "Duplicate" in str(exc): raise HTTPException(status_code=409, detail="username already exists")
        raise
    finally:
        cursor.close(); connection.close()
    return {"id": user_id, "username": payload.username, "display_name": payload.display_name.strip(), "role": payload.role}


@app.put("/api/users/{user_id}/progress/{feature_id}")
def put_progress(user_id: int, feature_id: int, payload: ProgressUpdate, user=Depends(current_user)):
    if int(user["sub"]) != user_id and user["role"] != "admin":
        raise HTTPException(status_code=403, detail="cannot modify another user's progress")
    if not any(x["id"] == feature_id for x in load_catalog()["features"]):
        raise HTTPException(status_code=404, detail="feature not found")
    connection = mysql_connection()
    if not connection: raise HTTPException(status_code=503, detail="MySQL is required for progress tracking")
    cursor = connection.cursor()
    cursor.execute("SELECT 1 FROM users WHERE id=%s", (user_id,))
    if not cursor.fetchone():
        cursor.close(); connection.close()
        raise HTTPException(status_code=404, detail="user not found")
    cursor.execute(
        "INSERT INTO learning_progress(user_id,feature_id,progress,completed_at) VALUES(%s,%s,%s,IF(%s=100,NOW(),NULL)) "
        "ON DUPLICATE KEY UPDATE progress=VALUES(progress),completed_at=IF(VALUES(progress)=100,NOW(),NULL)",
        (user_id, feature_id, payload.progress, payload.progress),
    )
    connection.commit(); cursor.close(); connection.close()
    return {"user_id": user_id, "feature_id": feature_id, "progress": payload.progress}


@app.get("/api/stats")
def stats():
    media, feature_data = media_items(), load_catalog()["features"]
    return {"features": len(feature_data), "student_features": sum(x["role"] == "student" for x in feature_data),
            "teacher_features": sum(x["role"] == "teacher" for x in feature_data),
            "videos": sum(x["type"] == "mp4" for x in media), "manuals": sum(x["type"] == "pdf" for x in media)}


@app.get("/")
def index():
    return FileResponse(FRONTEND_DIR / "index.html")


@app.get("/auth.js")
def auth_script():
    return FileResponse(FRONTEND_DIR / "auth.js", media_type="application/javascript")
