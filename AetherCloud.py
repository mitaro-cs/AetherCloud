import os
import re
import shutil
import sqlite3
import uuid
from datetime import datetime, timezone
from functools import wraps
from pathlib import Path

from flask import (
    Flask,
    Response,
    abort,
    flash,
    redirect,
    render_template_string,
    request,
    send_file,
    session,
    url_for,
)
from werkzeug.exceptions import RequestEntityTooLarge
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.utils import secure_filename

app = Flask(__name__)
app.secret_key = os.getenv("AETHER_SECRET_KEY", "super-secret-key-change-me")

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = Path(os.getenv("AETHER_DB_PATH", str(BASE_DIR / "users.db")))
STORAGE_DIR = Path(os.getenv("AETHER_STORAGE_DIR", str(BASE_DIR / "storage")))

TOTAL_SHARED_STORAGE_GB = int(os.getenv("AETHER_TOTAL_STORAGE_GB", "280"))
TOTAL_SHARED_STORAGE_BYTES = TOTAL_SHARED_STORAGE_GB * 1024 * 1024 * 1024

MAX_UPLOAD_MB = int(os.getenv("AETHER_MAX_UPLOAD_MB", "250"))
MAX_UPLOAD_BYTES = MAX_UPLOAD_MB * 1024 * 1024

BACKGROUND_IMAGE = (
    "https://i.pinimg.com/736x/f9/ad/c2/f9adc243ecb36023bf238bff0f7712d1.jpg"
)
ROOT_FOLDER_NAME = "Workspace"
ADMIN_EMAIL = "miri.saro@bk.ru"
VALID_VIEWS = {"home", "storage", "sync", "stats", "settings"}
VALID_TABS = {"folders", "tags"}

app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_BYTES


def ensure_runtime_paths():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    STORAGE_DIR.mkdir(parents=True, exist_ok=True)


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def db():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    return con


def init_db():
    with db() as con:

        def table_columns(table_name):
            rows = con.execute(f"PRAGMA table_info({table_name})").fetchall()
            return {row["name"] for row in rows}

        # Legacy compatibility: old builds used a different files schema.
        files_cols = table_columns("files")
        required_files_cols = {
            "user_id",
            "folder_id",
            "original_name",
            "stored_name",
            "size",
            "mime_type",
            "uploaded",
        }
        if files_cols and not required_files_cols.issubset(files_cols):
            con.execute("DROP TABLE files")

        con.executescript(
            """
            CREATE TABLE IF NOT EXISTS users(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                fullname TEXT NOT NULL,
                email TEXT UNIQUE NOT NULL,
                password TEXT NOT NULL,
                created DATETIME NOT NULL
            );

            CREATE TABLE IF NOT EXISTS folders(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                parent_id INTEGER,
                name TEXT NOT NULL,
                created DATETIME NOT NULL,
                UNIQUE(user_id, parent_id, name),
                FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE,
                FOREIGN KEY(parent_id) REFERENCES folders(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS files(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                folder_id INTEGER NOT NULL,
                original_name TEXT NOT NULL,
                stored_name TEXT NOT NULL,
                size INTEGER NOT NULL,
                mime_type TEXT,
                uploaded DATETIME NOT NULL,
                FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE,
                FOREIGN KEY(folder_id) REFERENCES folders(id) ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS idx_folders_user_parent ON folders(user_id, parent_id);
            CREATE INDEX IF NOT EXISTS idx_files_user_folder ON files(user_id, folder_id);
            """
        )


ensure_runtime_paths()
init_db()


# ================== VALIDATION ==================


def valid_email(email):
    return bool(re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email or ""))


def valid_password(password):
    if not password:
        return False
    return (
        len(password) >= 8
        and bool(re.search(r"[0-9]", password))
        and bool(re.search(r"[!@#$%^&*()_+\-=\[\]{};':\"\\|,.<>/?]", password))
    )


# ================== HELPERS ==================


def login_required(view):
    @wraps(view)
    def wrapper(*args, **kwargs):
        if "user_id" not in session:
            return redirect(url_for("login"))
        return view(*args, **kwargs)

    return wrapper


def current_user():
    uid = session.get("user_id")
    if not uid:
        return None
    with db() as con:
        return con.execute(
            "SELECT id, fullname, email FROM users WHERE id=?",
            (uid,),
        ).fetchone()


def total_users():
    with db() as con:
        row = con.execute("SELECT COUNT(*) AS total FROM users").fetchone()
    return max(int(row["total"]), 1)


def per_user_quota_bytes():
    return max(TOTAL_SHARED_STORAGE_BYTES // total_users(), 1)


def normalize_view(value):
    value = (value or "storage").lower()
    return value if value in VALID_VIEWS else "storage"


def normalize_tab(value):
    value = (value or "folders").lower()
    return value if value in VALID_TABS else "folders"


def file_extension(filename):
    ext = Path(filename or "").suffix.lower().lstrip(".")
    return ext or "file"


def user_nick(fullname):
    parts = (fullname or "").strip().split()
    return parts[0] if parts else "User"


def workspace_name(fullname):
    value = (fullname or "").strip()
    if not value:
        return ROOT_FOLDER_NAME
    return value[:80]


def is_admin_user(user_row):
    return bool(user_row and user_row["email"].strip().lower() == ADMIN_EMAIL.lower())


def admin_required(view):
    @wraps(view)
    def wrapper(*args, **kwargs):
        user = current_user()
        if not user:
            return redirect(url_for("login"))
        if not is_admin_user(user):
            flash("Admin access only.", "error")
            return cloud_redirect()
        return view(*args, **kwargs)

    return wrapper


def user_used_space(user_id):
    with db() as con:
        row = con.execute(
            "SELECT COALESCE(SUM(size), 0) AS used FROM files WHERE user_id=?",
            (user_id,),
        ).fetchone()
    return int(row["used"])


def human_size(size_bytes):
    value = float(size_bytes)
    units = ["B", "KB", "MB", "GB", "TB"]
    for unit in units:
        if value < 1024 or unit == units[-1]:
            if unit == "B":
                return f"{int(value)} {unit}"
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{int(size_bytes)} B"


def short_time(iso_text):
    try:
        dt = datetime.fromisoformat(iso_text)
    except (ValueError, TypeError):
        return iso_text
    return dt.strftime("%Y-%m-%d %H:%M")


def get_folder(user_id, folder_id):
    if folder_id is None:
        return None
    with db() as con:
        return con.execute(
            "SELECT id, user_id, parent_id, name, created FROM folders WHERE id=? AND user_id=?",
            (folder_id, user_id),
        ).fetchone()


def split_relative_parts(raw_path):
    text = (raw_path or "").replace("\\", "/")
    parts = []
    for part in text.split("/"):
        item = part.strip()
        if not item or item in {".", ".."}:
            continue
        parts.append(item)
    return parts


def normalize_folder_name(name):
    value = (name or "").strip()
    if not value:
        return ""
    value = value.replace("/", "").replace("\\", "")
    value = re.sub(r"\s+", " ", value)
    return value[:60]


def get_or_create_folder(user_id, parent_id, folder_name):
    name = normalize_folder_name(folder_name)
    if not name:
        return parent_id

    with db() as con:
        row = con.execute(
            "SELECT id FROM folders WHERE user_id=? AND parent_id=? AND name=?",
            (user_id, parent_id, name),
        ).fetchone()
        if row:
            return int(row["id"])

        try:
            cur = con.execute(
                "INSERT INTO folders(user_id, parent_id, name, created) VALUES(?,?,?,?)",
                (user_id, parent_id, name, utc_now()),
            )
            return int(cur.lastrowid)
        except sqlite3.IntegrityError:
            row = con.execute(
                "SELECT id FROM folders WHERE user_id=? AND parent_id=? AND name=?",
                (user_id, parent_id, name),
            ).fetchone()
            return int(row["id"]) if row else parent_id


def ensure_folder_chain(user_id, root_parent_id, parts):
    current = root_parent_id
    for part in parts:
        current = get_or_create_folder(user_id, current, part)
    return current


def ensure_default_workspace(user_id, preferred_name=None):
    root_name = workspace_name(preferred_name)
    with db() as con:
        root = con.execute(
            "SELECT id, name FROM folders WHERE user_id=? AND parent_id IS NULL ORDER BY id LIMIT 1",
            (user_id,),
        ).fetchone()
        if root:
            if root_name and root["name"] != root_name:
                con.execute(
                    "UPDATE folders SET name=? WHERE id=? AND user_id=?",
                    (root_name, root["id"], user_id),
                )
            return int(root["id"])

        created = utc_now()
        cur = con.execute(
            "INSERT INTO folders(user_id, parent_id, name, created) VALUES(?,?,?,?)",
            (user_id, None, root_name, created),
        )
        root_id = int(cur.lastrowid)

    return root_id


def get_child_folders(user_id, parent_id, search=""):
    params = [user_id, parent_id]
    sql = """
        SELECT
            f.id,
            f.name,
            f.parent_id,
            f.created,
            COALESCE(COUNT(fi.id), 0) AS file_count
        FROM folders f
        LEFT JOIN files fi ON fi.folder_id = f.id
        WHERE f.user_id = ?
          AND f.parent_id = ?
    """
    if search:
        sql += " AND LOWER(f.name) LIKE ?"
        params.append(f"%{search.lower()}%")

    sql += " GROUP BY f.id ORDER BY f.name COLLATE NOCASE"

    with db() as con:
        return con.execute(sql, params).fetchall()


def get_files_in_folder(user_id, folder_id, search=""):
    params = [user_id, folder_id]
    sql = """
        SELECT
            id,
            folder_id,
            original_name,
            size,
            mime_type,
            uploaded
        FROM files
        WHERE user_id = ?
          AND folder_id = ?
    """
    if search:
        sql += " AND LOWER(original_name) LIKE ?"
        params.append(f"%{search.lower()}%")

    sql += " ORDER BY uploaded DESC"

    with db() as con:
        return con.execute(sql, params).fetchall()


def get_recent_files(user_id, limit=6):
    with db() as con:
        return con.execute(
            """
            SELECT id, folder_id, original_name, size, mime_type, uploaded
            FROM files
            WHERE user_id=?
            ORDER BY uploaded DESC
            LIMIT ?
            """,
            (user_id, limit),
        ).fetchall()


def get_tag_summary(user_id, search=""):
    with db() as con:
        rows = con.execute(
            "SELECT original_name FROM files WHERE user_id=?",
            (user_id,),
        ).fetchall()

    needle = (search or "").strip().lower()
    counters = {}
    for row in rows:
        ext_name = file_extension(row["original_name"]).upper()
        if needle and needle not in ext_name.lower():
            continue
        counters[ext_name] = counters.get(ext_name, 0) + 1

    tags = [
        {"name": name, "count": count, "query": name.lower()}
        for name, count in counters.items()
    ]
    tags.sort(key=lambda item: (-item["count"], item["name"]))
    return tags


def get_user_metrics(user_id):
    with db() as con:
        files_row = con.execute(
            """
            SELECT COUNT(*) AS files_total, COALESCE(SUM(size), 0) AS bytes_total
            FROM files
            WHERE user_id=?
            """,
            (user_id,),
        ).fetchone()
        folders_row = con.execute(
            """
            SELECT COUNT(*) AS folders_total
            FROM folders
            WHERE user_id=? AND parent_id IS NOT NULL
            """,
            (user_id,),
        ).fetchone()

    return {
        "files_total": int(files_row["files_total"]),
        "folders_total": int(folders_row["folders_total"]),
        "bytes_total": int(files_row["bytes_total"]),
    }


def get_admin_metrics():
    with db() as con:
        users_row = con.execute("SELECT COUNT(*) AS total FROM users").fetchone()
        files_row = con.execute(
            "SELECT COUNT(*) AS total, COALESCE(SUM(size), 0) AS bytes_total FROM files"
        ).fetchone()
        folders_row = con.execute(
            "SELECT COUNT(*) AS total FROM folders WHERE parent_id IS NOT NULL"
        ).fetchone()

    return {
        "users_total": int(users_row["total"]),
        "files_total": int(files_row["total"]),
        "folders_total": int(folders_row["total"]),
        "bytes_total": int(files_row["bytes_total"]),
    }


def get_admin_users():
    with db() as con:
        rows = con.execute(
            """
            SELECT
                u.id,
                u.fullname,
                u.email,
                u.created,
                (SELECT COUNT(*) FROM folders f WHERE f.user_id=u.id AND f.parent_id IS NOT NULL) AS folders_total,
                (SELECT COUNT(*) FROM files fi WHERE fi.user_id=u.id) AS files_total,
                (SELECT COALESCE(SUM(size), 0) FROM files fi WHERE fi.user_id=u.id) AS used_bytes
            FROM users u
            ORDER BY u.created DESC
            """
        ).fetchall()

    return rows


def get_file_record(user_id, file_id):
    with db() as con:
        return con.execute(
            """
            SELECT id, folder_id, original_name, stored_name, mime_type, size, uploaded
            FROM files
            WHERE id=? AND user_id=?
            """,
            (file_id, user_id),
        ).fetchone()


def get_user_fullname(user_id):
    with db() as con:
        row = con.execute(
            "SELECT fullname FROM users WHERE id=?",
            (user_id,),
        ).fetchone()
    return row["fullname"] if row else "user"


def user_storage_root(user_id, fullname=None):
    real_name = fullname if fullname is not None else get_user_fullname(user_id)
    safe = secure_filename(real_name or "") or "user"
    return STORAGE_DIR / f"{safe}_{user_id}"


def user_blob_path(user_id, folder_id, stored_name, fullname=None):
    return user_storage_root(user_id, fullname) / str(folder_id) / stored_name


def legacy_blob_path(user_id, folder_id, stored_name):
    return STORAGE_DIR / str(user_id) / str(folder_id) / stored_name


def resolve_blob_path(user_id, folder_id, stored_name, fullname=None):
    preferred = user_blob_path(user_id, folder_id, stored_name, fullname)
    if preferred.exists() and preferred.is_file():
        return preferred
    legacy = legacy_blob_path(user_id, folder_id, stored_name)
    if legacy.exists() and legacy.is_file():
        return legacy
    return preferred


def rename_user_storage_root(user_id, old_name, new_name):
    old_root = user_storage_root(user_id, old_name)
    new_root = user_storage_root(user_id, new_name)
    if old_root == new_root:
        return
    if old_root.exists() and old_root.is_dir() and not new_root.exists():
        old_root.rename(new_root)


def build_preview_svg(ext):
    token = re.sub(r"[^A-Za-z0-9]", "", (ext or "file").upper())[:4] or "FILE"
    return f"""<svg xmlns="http://www.w3.org/2000/svg" width="96" height="96" viewBox="0 0 96 96">
<defs>
<linearGradient id="bg" x1="0" y1="0" x2="1" y2="1">
<stop offset="0%" stop-color="#1a1a1a"/>
<stop offset="100%" stop-color="#0a0a0a"/>
</linearGradient>
</defs>
<rect x="6" y="6" width="84" height="84" rx="16" fill="url(#bg)" stroke="#2c2c2c"/>
<rect x="22" y="20" width="52" height="56" rx="10" fill="#111" stroke="#333"/>
<text x="48" y="54" fill="#f2f2f2" font-size="14" font-family="Arial, sans-serif" text-anchor="middle">{token}</text>
</svg>"""


def find_missing_file_ids(user_id):
    with db() as con:
        rows = con.execute(
            "SELECT id, folder_id, stored_name FROM files WHERE user_id=?",
            (user_id,),
        ).fetchall()

    missing_ids = []
    fullname = get_user_fullname(user_id)
    for row in rows:
        blob = resolve_blob_path(
            user_id, row["folder_id"], row["stored_name"], fullname
        )
        if not blob.exists() or not blob.is_file():
            missing_ids.append(int(row["id"]))

    return missing_ids, len(rows)


def cloud_redirect(folder_id=None, view="storage", tab="folders", search=""):
    args = {"view": normalize_view(view), "tab": normalize_tab(tab)}
    if folder_id:
        args["folder"] = folder_id
    if search:
        args["q"] = search
    return redirect(url_for("cloud", **args))


def get_folder_tree(user_id, root_id=None, include_root=False):
    with db() as con:
        rows = con.execute(
            """
            SELECT
                f.id,
                f.parent_id,
                f.name,
                COALESCE(COUNT(fi.id), 0) AS file_count
            FROM folders f
            LEFT JOIN files fi ON fi.folder_id = f.id
            WHERE f.user_id = ?
            GROUP BY f.id
            """,
            (user_id,),
        ).fetchall()

    grouped = {}
    for row in rows:
        grouped.setdefault(row["parent_id"], []).append(row)

    for key in grouped:
        grouped[key].sort(key=lambda item: item["name"].lower())

    flat = []

    def walk(parent_id, depth):
        for item in grouped.get(parent_id, []):
            flat.append(
                {
                    "id": item["id"],
                    "name": item["name"],
                    "depth": depth,
                    "file_count": item["file_count"],
                }
            )
            walk(item["id"], depth + 1)

    if root_id is not None and not include_root:
        walk(root_id, 0)
    else:
        walk(None, 0)
    return flat


def get_breadcrumbs(user_id, folder_id):
    crumbs = []
    visited = set()
    current = folder_id

    with db() as con:
        while current is not None and current not in visited:
            visited.add(current)
            row = con.execute(
                "SELECT id, parent_id, name FROM folders WHERE id=? AND user_id=?",
                (current, user_id),
            ).fetchone()
            if not row:
                break
            crumbs.append({"id": row["id"], "name": row["name"]})
            current = row["parent_id"]

    crumbs.reverse()
    return crumbs


def get_folder_tree_ids(user_id, folder_id):
    with db() as con:
        rows = con.execute(
            """
            WITH RECURSIVE tree(id) AS (
                SELECT id
                FROM folders
                WHERE id = ? AND user_id = ?
                UNION ALL
                SELECT f.id
                FROM folders f
                JOIN tree t ON f.parent_id = t.id
                WHERE f.user_id = ?
            )
            SELECT id FROM tree
            """,
            (folder_id, user_id, user_id),
        ).fetchall()

    return [int(row["id"]) for row in rows]


def store_uploaded_file(user_row, folder_id, file_storage, display_name=None):
    user_id = user_row["id"]
    original_name = (display_name or file_storage.filename or "").strip()
    safe_name = secure_filename(original_name)
    if not safe_name:
        raise ValueError("File name is not valid.")

    stored_name = f"{uuid.uuid4().hex}_{safe_name}"
    target_dir = user_storage_root(user_id, user_row["fullname"]) / str(folder_id)
    target_dir.mkdir(parents=True, exist_ok=True)

    target_file = target_dir / stored_name
    file_storage.save(target_file)

    size = target_file.stat().st_size if target_file.exists() else 0
    if size <= 0:
        target_file.unlink(missing_ok=True)
        raise ValueError("Empty file cannot be uploaded.")

    used = user_used_space(user_id)
    quota = per_user_quota_bytes()
    if used + size > quota:
        target_file.unlink(missing_ok=True)
        raise ValueError(f"Storage limit exceeded. Your limit is {human_size(quota)}.")

    with db() as con:
        con.execute(
            """
            INSERT INTO files(user_id, folder_id, original_name, stored_name, size, mime_type, uploaded)
            VALUES(?,?,?,?,?,?,?)
            """,
            (
                user_id,
                folder_id,
                original_name,
                stored_name,
                size,
                file_storage.mimetype,
                utc_now(),
            ),
        )


def delete_file_blob(user_id, folder_id, stored_name):
    path = user_blob_path(user_id, folder_id, stored_name)
    if path.exists() and path.is_file():
        path.unlink(missing_ok=True)
    old_path = legacy_blob_path(user_id, folder_id, stored_name)
    if old_path.exists() and old_path.is_file():
        old_path.unlink(missing_ok=True)
    folder_path = path.parent
    if folder_path.exists():
        try:
            if not any(folder_path.iterdir()):
                folder_path.rmdir()
        except OSError:
            pass
    old_folder_path = old_path.parent
    if old_folder_path.exists():
        try:
            if not any(old_folder_path.iterdir()):
                old_folder_path.rmdir()
        except OSError:
            pass


# ================== HTML ==================

AUTH_HTML = """
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{{title}}</title>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500&display=swap" rel="stylesheet">
<style>
*{box-sizing:border-box}
html,body{margin:0;height:100%}
body{background:#000;font-family:Inter,sans-serif;color:#fff}
.shell{display:flex;height:100%}

.left{
    flex:1.1;
    margin:32px;
    border-radius:24px;
    background:url("{{ bg_image }}") center/cover;
    position:relative;
    overflow:hidden;
}
.left::after{
    content:"";
    position:absolute;
    inset:0;
    background:rgba(0,0,0,.25);
}
.left-text{
    position:absolute;
    bottom:40px;
    left:40px;
    max-width:420px;
    z-index:2;
}
.left-text h1{font-size:26px;font-weight:400;margin-bottom:14px}
.left-text p{font-size:14px;opacity:.95}

.right{flex:1;padding:88px;overflow:auto}

h2{font-size:22px;font-weight:400;margin-bottom:8px}
.sub{font-size:14px;color:#9a9a9a;margin-bottom:24px}

label{font-size:13px;color:#8e8e8e;margin-bottom:8px;display:block}
.field{margin-bottom:22px}

input{
    width:100%;
    padding:15px;
    background:#0f0f0f;
    border:1px solid #1f1f1f;
    border-radius:12px;
    color:#fff;
    font-size:15px;
}

button{
    width:100%;
    padding:16px;
    background:#fff;
    color:#000;
    border:none;
    border-radius:14px;
    font-weight:500;
    cursor:pointer;
}

.msg{font-size:13px;margin-bottom:12px;padding:12px;border-radius:10px;background:#141414}
.err{color:#ff7d7d;border:1px solid #3a1f1f}
.ok{color:#7ee787;border:1px solid #1f3a2a}

.bottom{text-align:center;font-size:13px;color:#6f6f6f;margin-top:26px}
.bottom a{color:#fff;text-decoration:none}

@media (max-width: 980px){
  .shell{flex-direction:column}
  .left{min-height:240px;margin:18px}
  .right{padding:24px}
}
</style>
</head>
<body>
<div class="shell">
<div class="left">
<div class="left-text">
<h1>Secure cloud storage<br>for your digital life.</h1>
<p>Store, sync and protect your files across all devices.</p>
</div>
</div>

<div class="right">

{% with messages = get_flashed_messages(with_categories=true) %}
  {% if messages %}
    {% for category, message in messages %}
      <div class="msg {{ 'err' if category == 'error' else 'ok' }}">{{message}}</div>
    {% endfor %}
  {% endif %}
{% endwith %}

{% if mode == "register" %}
<h2>Create your account</h2>
<div class="sub">Access your private cloud workspace</div>
{% if error %}<div class="msg err">{{error}}</div>{% endif %}
<form method="POST">
<label>Full name</label>
<div class="field"><input name="fullname" value="{{ form.fullname|e }}" required></div>
<label>Email</label>
<div class="field"><input name="email" value="{{ form.email|e }}" required></div>
<label>Password</label>
<div class="field"><input type="password" name="password" value="{{ form.password|e }}" required></div>
<label>Repeat password</label>
<div class="field"><input type="password" name="confirm_password" value="{{ form.confirm_password|e }}" required></div>
<button>Create account</button>
</form>
<div class="bottom">Already have an account? <a href="/login">Sign in</a></div>
{% endif %}

{% if mode == "login" %}
<h2>Sign in</h2>
<div class="sub">Welcome back</div>
{% if error %}<div class="msg err">{{error}}</div>{% endif %}
<form method="POST">
<label>Email</label>
<div class="field"><input name="email" value="{{ form.email|e }}" required></div>
<label>Password</label>
<div class="field"><input type="password" name="password" value="{{ form.password|e }}" required></div>
<button>Sign in</button>
</form>
<div class="bottom"><a href="/">Create account</a> | <a href="/forgot">Forgot password?</a></div>
{% endif %}

{% if mode == "forgot" %}
<h2>Reset password</h2>
<div class="sub">Enter your email and new password</div>
{% if error %}<div class="msg err">{{error}}</div>{% endif %}
{% if message %}<div class="msg ok">{{message}}</div>{% endif %}
<form method="POST">
<label>Email</label>
<div class="field"><input name="email" value="{{ form.email|e }}" required></div>
<label>New password</label>
<div class="field"><input type="password" name="new_password" value="{{ form.new_password|e }}" required></div>
<button>Reset password</button>
</form>
<div class="bottom"><a href="/login">Back to login</a></div>
{% endif %}

</div>
</div>
</body>
</html>
"""

CLOUD_HTML = """
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{{title}}</title>
<link href="https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@400;500;600;700&display=swap" rel="stylesheet">
<style>
:root{
    --bg:#000000;
    --panel:#050505;
    --panel-soft:#0a0a0a;
    --line:#1b1b1b;
    --line-strong:#2a2a2a;
    --text:#f5f5f5;
    --muted:#999;
    --accent:#ffffff;
    --accent-soft:#cfcfcf;
    --ok:#97d7aa;
    --danger:#ff9f9f;
}
*{box-sizing:border-box}
html,body{margin:0;min-height:100%}
body{
    font-family:"Space Grotesk",sans-serif;
    color:var(--text);
    background:
      radial-gradient(1000px 650px at -10% -10%, #181818 0%, transparent 50%),
      radial-gradient(900px 500px at 120% 120%, #111111 0%, transparent 50%),
      radial-gradient(400px 200px at 70% 0%, rgba(255,255,255,.08) 0%, transparent 70%),
      var(--bg);
}
a{color:inherit;text-decoration:none}

.layout{
    min-height:100vh;
    padding:24px;
    display:grid;
    grid-template-columns:84px 360px 1fr;
    gap:16px;
}

.rail,
.sidebar,
.workspace{
    border:1px solid var(--line);
    background:linear-gradient(180deg, rgba(255,255,255,.02), rgba(255,255,255,.005));
    backdrop-filter: blur(8px);
    border-radius:22px;
}

.rail{
    padding:16px 12px;
    display:flex;
    flex-direction:column;
    align-items:center;
    justify-content:space-between;
}
.logo{
    width:44px;
    height:44px;
    border-radius:14px;
    background:linear-gradient(135deg, #ffffff, #bfbfbf);
    color:#111;
    display:grid;
    place-items:center;
    font-weight:700;
    letter-spacing:.04em;
}
.rail-nav{
    display:flex;
    flex-direction:column;
    gap:12px;
}
.rail-btn{
    width:42px;
    height:42px;
    border-radius:12px;
    display:grid;
    place-items:center;
    border:1px solid transparent;
    color:var(--muted);
    background:transparent;
    font-size:14px;
    font-weight:600;
}
.rail-btn:hover,
.rail-btn.active{
    color:var(--text);
    border-color:var(--line-strong);
    background:#121212;
}

.sidebar{padding:18px;display:flex;flex-direction:column;gap:14px;overflow:auto}
.sidebar-head{
    display:flex;
    align-items:center;
    justify-content:space-between;
    gap:10px;
}
.sidebar-title{font-size:30px;font-weight:600;letter-spacing:-.02em}
.ghost-btn{
    width:36px;
    height:36px;
    border-radius:10px;
    border:1px solid var(--line-strong);
    background:#121212;
    color:var(--text);
    font-size:22px;
    cursor:pointer;
    line-height:1;
}
.search-form input{
    width:100%;
    border:1px solid var(--line-strong);
    border-radius:12px;
    background:#070707;
    color:var(--text);
    padding:12px 14px;
    font-size:14px;
}
.tabs{
    display:grid;
    grid-template-columns:1fr 1fr;
    background:#070707;
    border:1px solid var(--line-strong);
    border-radius:12px;
    padding:4px;
    gap:4px;
}
.tab{
    text-align:center;
    font-size:13px;
    color:var(--muted);
    padding:8px;
    border-radius:9px;
    border:1px solid transparent;
}
.tab.active{
    background:linear-gradient(145deg, #f5f5f5, #bdbdbd);
    color:#0b0b0b;
    border-color:#fafafa;
}
.tree{display:flex;flex-direction:column;gap:6px}
.tree-row{
    display:flex;
    align-items:center;
    justify-content:space-between;
    gap:10px;
    border:1px solid transparent;
    border-radius:10px;
    padding:8px 10px;
    padding-left:calc(12px + (var(--depth) * 18px));
}
.tree-row:hover{background:#111;border-color:#2a2a2a}
.tree-row.active{background:#171717;border-color:#3a3a3a}
.tree-name{font-size:14px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.count{
    min-width:24px;
    padding:2px 8px;
    border-radius:999px;
    background:#171717;
    font-size:12px;
    color:#f0f0f0;
    text-align:center;
}

.workspace{padding:24px;overflow:auto}
.workspace-head{display:flex;align-items:flex-start;justify-content:space-between;gap:16px;flex-wrap:wrap}
.crumbs{display:flex;align-items:center;gap:8px;color:var(--muted);font-size:13px;flex-wrap:wrap}
.crumbs span{opacity:.5}
.workspace h1{margin:8px 0 0;font-size:38px;letter-spacing:-.02em}
.top-right{display:flex;gap:12px;align-items:flex-start;flex-wrap:wrap;justify-content:flex-end}
.top-card{
    width:320px;
    min-height:118px;
}
.user-chip{
    border:1px solid var(--line-strong);
    background:#0b0b0b;
    border-radius:14px;
    padding:10px 12px;
}
.user-nick{font-size:16px;font-weight:600;line-height:1.2}
.user-mail{font-size:12px;color:var(--muted);margin-top:3px}
.user-actions{margin-top:8px;display:flex;gap:8px}
.chip-btn{
    padding:6px 10px;
    border-radius:8px;
    border:1px solid var(--line-strong);
    font-size:12px;
    background:#141414;
}

.storage-box{
    border:1px solid var(--line-strong);
    border-radius:14px;
    padding:12px;
    background:#0b0b0b;
}
.storage-top{display:flex;justify-content:space-between;align-items:center;font-size:13px;color:var(--muted);margin-bottom:8px}
.storage-top strong{color:var(--text);font-size:14px}
.meter{height:8px;background:#060606;border-radius:999px;overflow:hidden;border:1px solid #222}
.meter span{display:block;height:100%;background:linear-gradient(90deg,var(--accent-soft),var(--accent))}
.storage-note{margin-top:8px;font-size:12px;color:var(--muted)}

.action-row{display:grid;grid-template-columns:repeat(3, minmax(0, 1fr));gap:12px;margin:18px 0 8px}
.action-form{
    display:flex;
    gap:8px;
    border:1px solid var(--line-strong);
    border-radius:14px;
    padding:10px;
    background:#0b0b0b;
}
.action-form input[type="text"],
.action-form input[type="file"]{
    flex:1;
    background:#050505;
    color:var(--text);
    border:1px solid #252525;
    border-radius:10px;
    padding:10px;
    font-size:13px;
    height:46px;
}
.action-form button{
    border:1px solid #efefef;
    border-radius:10px;
    background:linear-gradient(135deg,var(--accent),var(--accent-soft));
    color:#080808;
    padding:10px 14px;
    font-weight:600;
    cursor:pointer;
    white-space:nowrap;
    height:46px;
    min-width:120px;
}
.action-form input[type="file"]{
    padding:0 10px;
    display:flex;
    align-items:center;
}
.action-form input[type="file"]::file-selector-button{
    border:none;
    border-right:1px solid #2b2b2b;
    border-radius:8px;
    margin-right:10px;
    padding:0 14px;
    height:44px;
    background:linear-gradient(135deg,#fff,#cfcfcf);
    color:#0c0c0c;
    font-weight:600;
    cursor:pointer;
}

.alerts{display:flex;flex-direction:column;gap:8px;margin:12px 0}
.alert{padding:11px 12px;border-radius:10px;font-size:13px;border:1px solid}
.alert.ok{color:var(--ok);border-color:#264230;background:#0e1712}
.alert.error{color:var(--danger);border-color:#4d2a2a;background:#1c1111}

.section{margin-top:20px}
.section h2{font-size:34px;margin:0 0 14px;letter-spacing:-.01em}
.folder-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(220px,1fr));gap:14px}
.folder-card{
    border:1px solid var(--line-strong);
    border-radius:18px;
    padding:12px;
    background:#0a0a0a;
    display:flex;
    flex-direction:column;
    gap:10px;
}
.folder-link{display:block}
.folder-visual{
    height:126px;
    border-radius:14px;
    background:linear-gradient(165deg, #2b2b2b, #141414 65%);
    position:relative;
    overflow:hidden;
    border:1px solid #333;
}
.folder-tab{
    width:90px;
    height:24px;
    border-radius:12px 12px 0 0;
    background:#555;
    position:absolute;
    top:14px;
    left:18px;
    opacity:.6;
}
.folder-body{
    position:absolute;
    inset:28px 14px 14px;
    border-radius:14px;
    background:linear-gradient(180deg, #2d2d2d, #191919);
    border:1px solid #3a3a3a;
}
.folder-card h3{margin:2px 0 0;font-size:23px;letter-spacing:-.01em}
.folder-card p{margin:2px 0 0;color:var(--muted);font-size:13px}
.delete-btn{
    width:100%;
    border:1px solid #4d2a2a;
    border-radius:10px;
    background:#1b1111;
    color:var(--danger);
    padding:8px;
    cursor:pointer;
    font-size:13px;
}
.empty{
    border:1px dashed var(--line);
    border-radius:14px;
    padding:18px;
    color:var(--muted);
    background:#0a0a0a;
}

.table-wrap{border:1px solid var(--line-strong);border-radius:16px;overflow:auto;background:#090909}
table{width:100%;border-collapse:collapse;min-width:900px}
thead th{
    text-align:left;
    padding:14px;
    font-size:12px;
    letter-spacing:.03em;
    color:var(--muted);
    border-bottom:1px solid #222;
    text-transform:uppercase;
}
tbody td{padding:14px;border-bottom:1px solid #1b1b1b;font-size:14px}
tbody tr:hover{background:#111}
.file-name a{font-weight:500}
.file-cell{display:flex;align-items:center;gap:10px}
.thumb{
    width:42px;
    height:42px;
    border-radius:10px;
    border:1px solid #2a2a2a;
    object-fit:cover;
    background:#121212;
    flex-shrink:0;
}
.file-meta{display:flex;flex-direction:column;gap:2px}
.file-ext{font-size:11px;color:var(--muted);letter-spacing:.03em;text-transform:uppercase}
.table-actions{display:flex;gap:8px;align-items:center}
.mini-btn{
    border:1px solid #2a2a2a;
    background:#141414;
    color:#e7e7e7;
    border-radius:8px;
    padding:7px 10px;
    font-size:12px;
    cursor:pointer;
}
.mini-btn.light{
    border-color:#ececec;
    background:linear-gradient(135deg,var(--accent),var(--accent-soft));
    color:#111;
}
.mini-btn.danger{border-color:#4e2b2b;background:#241618;color:#ffb3b3}
.empty-cell{color:var(--muted);text-align:center;padding:28px}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:12px}
.card{
    border:1px solid var(--line-strong);
    border-radius:14px;
    padding:14px;
    background:#0a0a0a;
}
.card .label{color:var(--muted);font-size:12px}
.card .value{margin-top:8px;font-size:26px;font-weight:600}
.card .sub{margin-top:6px;color:var(--muted);font-size:12px}
.panel{
    border:1px solid var(--line-strong);
    border-radius:16px;
    background:#0a0a0a;
    padding:16px;
    margin-top:16px;
}
.panel h3{margin:0 0 10px;font-size:22px}
.list{display:flex;flex-direction:column;gap:8px}
.list-row{
    display:flex;
    align-items:center;
    justify-content:space-between;
    gap:10px;
    padding:9px 10px;
    border:1px solid #1e1e1e;
    border-radius:10px;
    background:#0f0f0f;
}
.settings-grid{display:grid;grid-template-columns:1fr 1fr;gap:14px}
.settings-form{
    border:1px solid var(--line-strong);
    border-radius:16px;
    padding:16px;
    background:#0a0a0a;
    display:flex;
    flex-direction:column;
    gap:12px;
}
.settings-form h3{margin:0;font-size:22px}
.settings-form label{font-size:13px;color:var(--muted)}
.settings-form input{
    width:100%;
    border:1px solid #262626;
    background:#050505;
    color:var(--text);
    border-radius:10px;
    padding:10px;
}
.settings-form button{
    width:auto;
    border:1px solid #efefef;
    border-radius:10px;
    background:linear-gradient(135deg,var(--accent),var(--accent-soft));
    color:#111;
    padding:10px 14px;
    font-weight:600;
}
.settings-help{color:var(--muted);font-size:13px}

@media (max-width: 1400px){
  .sidebar{padding:14px}
  .sidebar-title{font-size:25px}
  .workspace h1{font-size:30px}
}

@media (max-width: 1100px){
  .layout{grid-template-columns:84px 320px 1fr;padding:16px}
  .action-row{grid-template-columns:1fr}
  .settings-grid{grid-template-columns:1fr}
}

@media (max-width: 920px){
  .layout{grid-template-columns:1fr;grid-template-rows:auto auto 1fr}
  .rail{flex-direction:row;padding:10px 14px}
  .rail-nav{flex-direction:row}
  .sidebar{max-height:350px}
}
</style>
</head>
<body>
<div class="layout">
    <aside class="rail">
        <div class="logo">AC</div>
        <nav class="rail-nav">
            <a class="rail-btn {% if active_view == 'home' %}active{% endif %}" href="{{ url_for('cloud', folder=current_folder.id, view='home', tab=active_tab, q=search) }}" title="Home">&#8962;</a>
            <a class="rail-btn {% if active_view == 'storage' %}active{% endif %}" href="{{ url_for('cloud', folder=current_folder.id, view='storage', tab=active_tab, q=search) }}" title="Storage">&#9635;</a>
            <a class="rail-btn {% if active_view == 'sync' %}active{% endif %}" href="{{ url_for('cloud', folder=current_folder.id, view='sync', tab=active_tab, q=search) }}" title="Sync">&#8646;</a>
            <a class="rail-btn {% if active_view == 'stats' %}active{% endif %}" href="{{ url_for('cloud', folder=current_folder.id, view='stats', tab=active_tab, q=search) }}" title="Stats">&#8801;</a>
            <a class="rail-btn {% if active_view == 'settings' %}active{% endif %}" href="{{ url_for('cloud', folder=current_folder.id, view='settings', tab=active_tab, q=search) }}" title="Settings">&#9881;</a>
        </nav>
        <a class="rail-btn" href="{{ url_for('logout') }}" title="Log out">&#10142;</a>
    </aside>

    <aside class="sidebar">
        <div class="sidebar-head">
            <div class="sidebar-title">Aether Cloud</div>
            <form method="POST" action="{{ url_for('create_folder') }}">
                <input type="hidden" name="parent_id" value="{{ current_folder.id }}">
                <input type="hidden" name="next_view" value="{{ active_view }}">
                <input type="hidden" name="next_tab" value="{{ active_tab }}">
                <input type="hidden" name="name" value="New Folder">
                <button class="ghost-btn" type="submit" title="Quick add folder">+</button>
            </form>
        </div>

        <form class="search-form" method="GET" action="{{ url_for('cloud') }}">
            <input type="hidden" name="folder" value="{{ current_folder.id }}">
            <input type="hidden" name="view" value="{{ active_view }}">
            <input type="hidden" name="tab" value="{{ active_tab }}">
            <input type="text" name="q" value="{{ search }}" placeholder="Search...">
        </form>

        <div class="tabs">
            <a class="tab {% if active_tab == 'folders' %}active{% endif %}" href="{{ url_for('cloud', folder=current_folder.id, view=active_view, tab='folders', q=search) }}">Folders</a>
            <a class="tab {% if active_tab == 'tags' %}active{% endif %}" href="{{ url_for('cloud', folder=current_folder.id, view=active_view, tab='tags', q=search) }}">Tags</a>
        </div>

        {% if active_tab == "folders" %}
            <div class="tree">
                {% for node in tree %}
                  <a
                    class="tree-row {% if node.id == current_folder.id %}active{% endif %}"
                    style="--depth:{{ node.depth }}"
                    href="{{ url_for('cloud', folder=node.id, view=active_view, tab=active_tab, q=search) }}"
                  >
                      <span class="tree-name">{{ node.name }}</span>
                      <span class="count">{{ node.file_count }}</span>
                  </a>
                {% endfor %}
            </div>
        {% else %}
            <div class="tree">
                {% if tags %}
                    {% for tag in tags %}
                    <a class="tree-row" style="--depth:0" href="{{ url_for('cloud', folder=current_folder.id, view='storage', tab='tags', q=tag.query) }}">
                        <span class="tree-name">{{ tag.name }}</span>
                        <span class="count">{{ tag.count }}</span>
                    </a>
                    {% endfor %}
                {% else %}
                    <div class="empty">No tags yet.</div>
                {% endif %}
            </div>
        {% endif %}
    </aside>

    <main class="workspace">
        <header class="workspace-head">
            <div>
                <div class="crumbs">
                    {% for item in breadcrumbs %}
                        <a href="{{ url_for('cloud', folder=item.id, view=active_view, tab=active_tab, q=search) }}">{{ item.name }}</a>
                        {% if not loop.last %}<span>/</span>{% endif %}
                    {% endfor %}
                </div>
                <h1>{% if active_view == 'settings' %}Settings{% elif active_view == 'stats' %}Statistics{% elif active_view == 'sync' %}Synchronization{% elif active_view == 'home' %}Dashboard{% else %}{{ current_folder.name }}{% endif %}</h1>
            </div>

            <div class="top-right">
                <div class="user-chip top-card">
                    <div class="user-nick">{{ nick }}</div>
                    <div class="user-mail">{{ user.email }}</div>
                    <div class="user-actions">
                        <a class="chip-btn" href="{{ url_for('cloud', folder=current_folder.id, view='settings', tab=active_tab, q=search) }}">Settings</a>
                        {% if is_admin %}
                        <a class="chip-btn" href="{{ url_for('admin_console') }}">Admin</a>
                        {% endif %}
                        <a class="chip-btn" href="{{ url_for('logout') }}">Logout</a>
                    </div>
                </div>
                <div class="storage-box top-card">
                    <div class="storage-top">
                        <span>Storage</span>
                        <strong>{{ used_label }} / {{ max_label }}</strong>
                    </div>
                    <div class="meter"><span style="width: {{ used_percent }}%;"></span></div>
                    <div class="storage-note">Total: {{ total_label }} for {{ users_count }} users</div>
                </div>
            </div>
        </header>

        {% if active_view in ['home', 'storage'] %}
            <div class="action-row">
                <form class="action-form" method="POST" action="{{ url_for('create_folder') }}">
                    <input type="hidden" name="parent_id" value="{{ current_folder.id }}">
                    <input type="hidden" name="next_view" value="{{ active_view }}">
                    <input type="hidden" name="next_tab" value="{{ active_tab }}">
                    <input type="text" name="name" placeholder="New folder name" required>
                    <button type="submit">Create folder</button>
                </form>

                <form class="action-form" method="POST" action="{{ url_for('upload_file') }}" enctype="multipart/form-data">
                    <input type="hidden" name="folder_id" value="{{ current_folder.id }}">
                    <input type="hidden" name="next_view" value="{{ active_view }}">
                    <input type="hidden" name="next_tab" value="{{ active_tab }}">
                    <input type="file" name="files" multiple required>
                    <button type="submit">Upload files</button>
                </form>

                <form class="action-form" method="POST" action="{{ url_for('upload_file') }}" enctype="multipart/form-data">
                    <input type="hidden" name="folder_id" value="{{ current_folder.id }}">
                    <input type="hidden" name="next_view" value="{{ active_view }}">
                    <input type="hidden" name="next_tab" value="{{ active_tab }}">
                    <input type="file" name="folder_files" webkitdirectory directory multiple required>
                    <button type="submit">Upload folder</button>
                </form>
            </div>
        {% endif %}

        {% with messages = get_flashed_messages(with_categories=true) %}
          {% if messages %}
            <div class="alerts">
            {% for category, message in messages %}
              <div class="alert {{ 'error' if category == 'error' else 'ok' }}">{{ message }}</div>
            {% endfor %}
            </div>
          {% endif %}
        {% endwith %}

        {% if active_view == "home" %}
            <section class="section">
                <h2>Overview</h2>
                <div class="cards">
                    <article class="card">
                        <div class="label">Folders</div>
                        <div class="value">{{ stats.folders_total }}</div>
                        <div class="sub">inside your workspace</div>
                    </article>
                    <article class="card">
                        <div class="label">Files</div>
                        <div class="value">{{ stats.files_total }}</div>
                        <div class="sub">stored in cloud</div>
                    </article>
                    <article class="card">
                        <div class="label">Used</div>
                        <div class="value">{{ used_label }}</div>
                        <div class="sub">out of {{ max_label }}</div>
                    </article>
                    <article class="card">
                        <div class="label">Free</div>
                        <div class="value">{{ free_label }}</div>
                        <div class="sub">available now</div>
                    </article>
                </div>
            </section>
            <section class="section">
                <h2>Recent Files</h2>
                <div class="table-wrap">
                    <table>
                        <thead>
                            <tr>
                                <th>Preview</th>
                                <th>Name</th>
                                <th>Size</th>
                                <th>Uploaded</th>
                                <th>Actions</th>
                            </tr>
                        </thead>
                        <tbody>
                            {% if recent_files %}
                                {% for file in recent_files %}
                                    <tr>
                                        <td><img class="thumb" src="{{ file.thumb_url }}" alt="preview"></td>
                                        <td class="file-name"><a href="{{ url_for('download_file', file_id=file.id) }}">{{ file.original_name }}</a></td>
                                        <td>{{ file.size_human }}</td>
                                        <td>{{ file.uploaded_short }}</td>
                                        <td><a class="mini-btn light" href="{{ url_for('cloud', folder=file.folder_id, view='storage', tab=active_tab) }}">Open folder</a></td>
                                    </tr>
                                {% endfor %}
                            {% else %}
                                <tr><td class="empty-cell" colspan="5">No recent files yet.</td></tr>
                            {% endif %}
                        </tbody>
                    </table>
                </div>
            </section>
        {% elif active_view == "storage" %}
            <section class="section">
                <h2>Folders</h2>
                <div class="folder-grid">
                    {% if child_folders %}
                        {% for folder in child_folders %}
                            <article class="folder-card">
                                <a class="folder-link" href="{{ url_for('cloud', folder=folder.id, view='storage', tab=active_tab, q=search) }}">
                                    <div class="folder-visual">
                                        <div class="folder-tab"></div>
                                        <div class="folder-body"></div>
                                    </div>
                                    <h3>{{ folder.name }}</h3>
                                    <p>{{ folder.file_count }} files</p>
                                </a>
                                <form method="POST" action="{{ url_for('delete_folder', folder_id=folder.id) }}">
                                    <input type="hidden" name="next_view" value="{{ active_view }}">
                                    <input type="hidden" name="next_tab" value="{{ active_tab }}">
                                    <input type="hidden" name="folder_id" value="{{ current_folder.id }}">
                                    <button type="submit" class="delete-btn">Delete folder</button>
                                </form>
                            </article>
                        {% endfor %}
                    {% else %}
                        <div class="empty">No folders in this directory.</div>
                    {% endif %}
                </div>
            </section>

            <section class="section">
                <h2>Files</h2>
                <div class="table-wrap">
                    <table>
                        <thead>
                            <tr>
                                <th>Name</th>
                                <th>Size</th>
                                <th>Added By</th>
                                <th>Uploaded</th>
                                <th>Actions</th>
                            </tr>
                        </thead>
                        <tbody>
                            {% if files %}
                                {% for file in files %}
                                    <tr>
                                        <td class="file-name">
                                            <div class="file-cell">
                                                <img class="thumb" src="{{ file.thumb_url }}" alt="preview">
                                                <div class="file-meta">
                                                    <a href="{{ url_for('download_file', file_id=file.id) }}">{{ file.original_name }}</a>
                                                    <span class="file-ext">{{ file.ext }}</span>
                                                </div>
                                            </div>
                                        </td>
                                        <td>{{ file.size_human }}</td>
                                        <td>{{ user.email }}</td>
                                        <td>{{ file.uploaded_short }}</td>
                                        <td>
                                            <div class="table-actions">
                                                <a class="mini-btn light" href="{{ url_for('download_file', file_id=file.id) }}">Download</a>
                                                <form method="POST" action="{{ url_for('delete_file', file_id=file.id) }}">
                                                    <input type="hidden" name="folder_id" value="{{ current_folder.id }}">
                                                    <input type="hidden" name="next_view" value="{{ active_view }}">
                                                    <input type="hidden" name="next_tab" value="{{ active_tab }}">
                                                    <button type="submit" class="mini-btn danger">Delete</button>
                                                </form>
                                            </div>
                                        </td>
                                    </tr>
                                {% endfor %}
                            {% else %}
                                <tr>
                                    <td class="empty-cell" colspan="5">No files uploaded to this folder.</td>
                                </tr>
                            {% endif %}
                        </tbody>
                    </table>
                </div>
            </section>
        {% elif active_view == "sync" %}
            <section class="section">
                <h2>Sync Center</h2>
                <div class="panel">
                    <h3>Consistency check</h3>
                    <p class="settings-help">This check scans metadata and real disk files. Missing records will be cleaned automatically.</p>
                    <form method="POST" action="{{ url_for('sync_check') }}">
                        <input type="hidden" name="folder_id" value="{{ current_folder.id }}">
                        <button class="mini-btn light" type="submit">Run sync check</button>
                    </form>
                </div>
            </section>
        {% elif active_view == "stats" %}
            <section class="section">
                <h2>Usage Stats</h2>
                <div class="cards">
                    <article class="card">
                        <div class="label">Total folders</div>
                        <div class="value">{{ stats.folders_total }}</div>
                    </article>
                    <article class="card">
                        <div class="label">Total files</div>
                        <div class="value">{{ stats.files_total }}</div>
                    </article>
                    <article class="card">
                        <div class="label">Used space</div>
                        <div class="value">{{ used_label }}</div>
                    </article>
                    <article class="card">
                        <div class="label">Free space</div>
                        <div class="value">{{ free_label }}</div>
                    </article>
                </div>
                <div class="panel">
                    <h3>File Types</h3>
                    <div class="list">
                        {% if tags %}
                            {% for tag in tags[:10] %}
                                <div class="list-row">
                                    <span>{{ tag.name }}</span>
                                    <span class="count">{{ tag.count }}</span>
                                </div>
                            {% endfor %}
                        {% else %}
                            <div class="empty">No file type stats yet.</div>
                        {% endif %}
                    </div>
                </div>
            </section>
        {% elif active_view == "settings" %}
            <section class="section">
                <h2>User Settings</h2>
                <div class="settings-grid">
                    <form class="settings-form" method="POST" action="{{ url_for('update_profile') }}">
                        <h3>Profile</h3>
                        <input type="hidden" name="folder_id" value="{{ current_folder.id }}">
                        <label>Display name</label>
                        <input type="text" name="fullname" value="{{ user.fullname }}" required>
                        <label>Email (read only)</label>
                        <input type="text" value="{{ user.email }}" readonly>
                        <button type="submit">Save profile</button>
                    </form>

                    <form class="settings-form" method="POST" action="{{ url_for('update_password') }}">
                        <h3>Password</h3>
                        <input type="hidden" name="folder_id" value="{{ current_folder.id }}">
                        <label>Current password</label>
                        <input type="password" name="current_password" required>
                        <label>New password</label>
                        <input type="password" name="new_password" required>
                        <label>Repeat new password</label>
                        <input type="password" name="confirm_password" required>
                        <button type="submit">Change password</button>
                    </form>
                </div>
            </section>
        {% endif %}
    </main>
</div>
</body>
</html>
"""

ADMIN_HTML = """
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{{ title }}</title>
<link href="https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@400;500;600;700&display=swap" rel="stylesheet">
<style>
:root{
  --bg:#000;
  --panel:#090909;
  --line:#222;
  --text:#f5f5f5;
  --muted:#a6a6a6;
}
*{box-sizing:border-box}
html,body{margin:0;min-height:100%}
body{
  font-family:"Space Grotesk",sans-serif;
  background:
    radial-gradient(900px 400px at 5% 0%, #171717 0%, transparent 55%),
    radial-gradient(900px 500px at 100% 120%, #111 0%, transparent 55%),
    var(--bg);
  color:var(--text);
}
.wrap{max-width:1280px;margin:0 auto;padding:24px}
.top{display:flex;justify-content:space-between;align-items:center;gap:12px;flex-wrap:wrap}
.title{font-size:40px;font-weight:700;letter-spacing:-.02em}
.subtitle{color:var(--muted);margin-top:6px}
.btn{
  display:inline-block;
  border:1px solid #efefef;
  border-radius:12px;
  padding:10px 14px;
  color:#0c0c0c;
  background:linear-gradient(135deg,#fff,#cfcfcf);
  text-decoration:none;
  font-weight:600;
}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:12px;margin-top:18px}
.card{
  border:1px solid var(--line);
  background:var(--panel);
  border-radius:16px;
  padding:14px;
}
.card .label{font-size:12px;color:var(--muted)}
.card .value{font-size:34px;font-weight:700;margin-top:8px}
.panel{
  margin-top:18px;
  border:1px solid var(--line);
  background:var(--panel);
  border-radius:16px;
  overflow:auto;
}
table{width:100%;border-collapse:collapse;min-width:900px}
th,td{padding:14px;border-bottom:1px solid #1d1d1d;text-align:left}
th{font-size:12px;letter-spacing:.03em;text-transform:uppercase;color:var(--muted)}
tbody tr:hover{background:#111}
.pill{padding:3px 10px;border-radius:999px;border:1px solid #2a2a2a;background:#151515;font-size:12px}
</style>
</head>
<body>
<div class="wrap">
  <header class="top">
    <div>
      <div class="title">Admin Console</div>
      <div class="subtitle">Access for {{ admin_email }} only. Logged in as {{ user.email }}</div>
    </div>
    <a class="btn" href="{{ url_for('cloud', view='storage') }}">Back To Cloud</a>
  </header>

  <section class="grid">
    <article class="card">
      <div class="label">Users</div>
      <div class="value">{{ metrics.users_total }}</div>
    </article>
    <article class="card">
      <div class="label">Folders</div>
      <div class="value">{{ metrics.folders_total }}</div>
    </article>
    <article class="card">
      <div class="label">Files</div>
      <div class="value">{{ metrics.files_total }}</div>
    </article>
    <article class="card">
      <div class="label">Used Space</div>
      <div class="value">{{ metrics.bytes_label }}</div>
    </article>
  </section>

  <section class="panel">
    <table>
      <thead>
        <tr>
          <th>User</th>
          <th>Email</th>
          <th>Created</th>
          <th>Folders</th>
          <th>Files</th>
          <th>Used</th>
          <th>Quota</th>
        </tr>
      </thead>
      <tbody>
        {% for item in users %}
        <tr>
          <td>{{ item.fullname }}</td>
          <td>{{ item.email }}</td>
          <td>{{ item.created_short }}</td>
          <td><span class="pill">{{ item.folders_total }}</span></td>
          <td><span class="pill">{{ item.files_total }}</span></td>
          <td>{{ item.used_label }}</td>
          <td>{{ item.quota_label }}</td>
        </tr>
        {% endfor %}
      </tbody>
    </table>
  </section>
</div>
</body>
</html>
"""


# ================== ROUTES ==================


@app.route("/", methods=["GET", "POST"])
def register():
    error = None
    form = {
        "fullname": "",
        "email": "",
        "password": "",
        "confirm_password": "",
    }

    if request.method == "POST":
        fullname = request.form.get("fullname", "").strip()
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        confirm_password = request.form.get("confirm_password", "")

        form = {
            "fullname": fullname,
            "email": email,
            "password": password,
            "confirm_password": confirm_password,
        }

        if len(fullname) < 2:
            error = "Full name must contain at least 2 characters."
        elif not valid_email(email):
            error = "Email is not valid."
        elif password != confirm_password:
            error = "Пароль не подходит."
        elif not valid_password(password):
            error = "Пароль не подходит."
        else:
            try:
                with db() as con:
                    cur = con.execute(
                        "INSERT INTO users(fullname, email, password, created) VALUES(?,?,?,?)",
                        (fullname, email, generate_password_hash(password), utc_now()),
                    )
                    user_id = int(cur.lastrowid)
                ensure_default_workspace(user_id, fullname)
                flash("Account created. Sign in to continue.", "ok")
                return redirect(url_for("login"))
            except sqlite3.IntegrityError:
                error = "User with this email already exists."

    return render_template_string(
        AUTH_HTML,
        mode="register",
        error=error,
        form=form,
        title="Register",
        bg_image=BACKGROUND_IMAGE,
    )


@app.route("/login", methods=["GET", "POST"])
def login():
    error = None
    form = {"email": "", "password": ""}

    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        form = {"email": email, "password": password}

        with db() as con:
            row = con.execute(
                "SELECT id, fullname, password FROM users WHERE email=?",
                (email,),
            ).fetchone()

        if row and check_password_hash(row["password"], password):
            session["user_id"] = row["id"]
            ensure_default_workspace(row["id"], row["fullname"])
            return redirect(url_for("cloud"))
        if row:
            error = "Пароль не подходит."
        else:
            error = "User not found."

    return render_template_string(
        AUTH_HTML,
        mode="login",
        error=error,
        form=form,
        title="Login",
        bg_image=BACKGROUND_IMAGE,
    )


@app.route("/forgot", methods=["GET", "POST"])
def forgot():
    error = None
    message = None
    form = {"email": "", "new_password": ""}

    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        new_pw = request.form.get("new_password", "")
        form = {"email": email, "new_password": new_pw}

        if not valid_password(new_pw):
            error = "New password must include 8 chars, number and special symbol."
        else:
            with db() as con:
                user = con.execute(
                    "SELECT id FROM users WHERE email=?",
                    (email,),
                ).fetchone()

                if not user:
                    error = "User not found."
                else:
                    con.execute(
                        "UPDATE users SET password=? WHERE email=?",
                        (generate_password_hash(new_pw), email),
                    )
                    message = "Password successfully updated."

    return render_template_string(
        AUTH_HTML,
        mode="forgot",
        error=error,
        message=message,
        form=form,
        title="Reset",
        bg_image=BACKGROUND_IMAGE,
    )


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/cloud")
@login_required
def cloud():
    user = current_user()
    if not user:
        return redirect(url_for("login"))

    root_id = ensure_default_workspace(user["id"], user["fullname"])
    requested_folder = request.args.get("folder", type=int)
    active_view = normalize_view(request.args.get("view", "storage"))
    active_tab = normalize_tab(request.args.get("tab", "folders"))
    search = request.args.get("q", "").strip()

    target_folder_id = requested_folder if requested_folder else root_id
    current_folder = get_folder(user["id"], target_folder_id)
    if not current_folder:
        current_folder = get_folder(user["id"], root_id)

    child_folders = get_child_folders(user["id"], current_folder["id"], search)
    files_raw = get_files_in_folder(user["id"], current_folder["id"], search)

    files = []
    for item in files_raw:
        files.append(
            {
                "id": item["id"],
                "folder_id": item["folder_id"],
                "original_name": item["original_name"],
                "ext": file_extension(item["original_name"]),
                "size_human": human_size(item["size"]),
                "uploaded_short": short_time(item["uploaded"]),
                "thumb_url": url_for("file_preview", file_id=item["id"]),
            }
        )

    recent_files = []
    for item in get_recent_files(user["id"], limit=8):
        recent_files.append(
            {
                "id": item["id"],
                "folder_id": item["folder_id"],
                "original_name": item["original_name"],
                "size_human": human_size(item["size"]),
                "uploaded_short": short_time(item["uploaded"]),
                "thumb_url": url_for("file_preview", file_id=item["id"]),
            }
        )

    used_bytes = user_used_space(user["id"])
    quota_bytes = per_user_quota_bytes()
    users_count = total_users()
    free_bytes = max(quota_bytes - used_bytes, 0)
    used_percent = (
        min(round((used_bytes / quota_bytes) * 100, 2), 100) if quota_bytes else 0
    )
    stats = get_user_metrics(user["id"])
    tags = get_tag_summary(user["id"], search if active_tab == "tags" else "")

    return render_template_string(
        CLOUD_HTML,
        title="AetherCloud",
        user=user,
        is_admin=is_admin_user(user),
        nick=user_nick(user["fullname"]),
        current_folder=current_folder,
        child_folders=child_folders,
        files=files,
        recent_files=recent_files,
        stats=stats,
        tree=get_folder_tree(user["id"], root_id=root_id, include_root=False),
        breadcrumbs=get_breadcrumbs(user["id"], current_folder["id"]),
        tags=tags,
        active_view=active_view,
        active_tab=active_tab,
        used_label=human_size(used_bytes),
        max_label=human_size(quota_bytes),
        free_label=human_size(free_bytes),
        total_label=human_size(TOTAL_SHARED_STORAGE_BYTES),
        users_count=users_count,
        used_percent=used_percent,
        search=search,
    )


@app.get("/admin")
@login_required
@admin_required
def admin_console():
    user = current_user()
    metrics = get_admin_metrics()
    users_rows = get_admin_users()
    quota_bytes = per_user_quota_bytes()

    users = []
    for row in users_rows:
        users.append(
            {
                "id": row["id"],
                "fullname": row["fullname"],
                "email": row["email"],
                "created_short": short_time(row["created"]),
                "folders_total": int(row["folders_total"]),
                "files_total": int(row["files_total"]),
                "used_label": human_size(int(row["used_bytes"])),
                "quota_label": human_size(quota_bytes),
            }
        )

    metrics["bytes_label"] = human_size(metrics["bytes_total"])

    return render_template_string(
        ADMIN_HTML,
        title="AetherCloud Admin",
        user=user,
        admin_email=ADMIN_EMAIL,
        metrics=metrics,
        users=users,
    )


@app.post("/cloud/folders")
@login_required
def create_folder():
    user = current_user()
    if not user:
        return redirect(url_for("login"))

    next_view = normalize_view(request.form.get("next_view", "storage"))
    next_tab = normalize_tab(request.form.get("next_tab", "folders"))
    root_id = ensure_default_workspace(user["id"], user["fullname"])
    parent_id = request.form.get("parent_id", type=int) or root_id
    parent = get_folder(user["id"], parent_id)
    if not parent:
        abort(403)

    name = request.form.get("name", "").strip()
    if not name:
        flash("Folder name is required.", "error")
        return cloud_redirect(parent_id, next_view, next_tab)

    if len(name) > 60 or "/" in name or "\\" in name:
        flash("Folder name contains invalid characters.", "error")
        return cloud_redirect(parent_id, next_view, next_tab)

    try:
        with db() as con:
            con.execute(
                "INSERT INTO folders(user_id, parent_id, name, created) VALUES(?,?,?,?)",
                (user["id"], parent_id, name, utc_now()),
            )
        flash("Folder created.", "ok")
    except sqlite3.IntegrityError:
        flash("Folder with this name already exists here.", "error")

    return cloud_redirect(parent_id, next_view, next_tab)


@app.post("/cloud/files/upload")
@login_required
def upload_file():
    user = current_user()
    if not user:
        return redirect(url_for("login"))

    next_view = normalize_view(request.form.get("next_view", "storage"))
    next_tab = normalize_tab(request.form.get("next_tab", "folders"))
    folder_id = request.form.get("folder_id", type=int)
    folder = get_folder(user["id"], folder_id)
    if not folder:
        abort(403)

    regular_files = [
        item
        for item in request.files.getlist("files")
        if item and (item.filename or "").strip()
    ]
    folder_files = [
        item
        for item in request.files.getlist("folder_files")
        if item and (item.filename or "").strip()
    ]

    if not regular_files and not folder_files:
        flash("Select files or a folder to upload.", "error")
        return cloud_redirect(folder_id, next_view, next_tab)

    uploaded = 0
    failed = 0

    for file_storage in regular_files:
        parts = split_relative_parts(file_storage.filename)
        file_name = parts[-1] if parts else ""
        if not file_name:
            failed += 1
            continue
        try:
            store_uploaded_file(user, folder_id, file_storage, display_name=file_name)
            uploaded += 1
        except ValueError as exc:
            failed += 1
            flash(str(exc), "error")

    for file_storage in folder_files:
        parts = split_relative_parts(file_storage.filename)
        if not parts:
            failed += 1
            continue

        file_name = parts[-1]
        target_folder_id = ensure_folder_chain(user["id"], folder_id, parts[:-1])
        try:
            store_uploaded_file(
                user,
                target_folder_id,
                file_storage,
                display_name=file_name,
            )
            uploaded += 1
        except ValueError as exc:
            failed += 1
            flash(str(exc), "error")

    if uploaded:
        flash(f"Uploaded: {uploaded} file(s).", "ok")
    if failed and not uploaded:
        flash("Upload failed.", "error")
    elif failed:
        flash(f"Skipped: {failed} file(s).", "error")

    return cloud_redirect(folder_id, next_view, next_tab)


@app.get("/cloud/files/<int:file_id>/download")
@login_required
def download_file(file_id):
    user = current_user()
    if not user:
        return redirect(url_for("login"))

    row = get_file_record(user["id"], file_id)
    if not row:
        abort(404)

    blob = resolve_blob_path(
        user["id"],
        row["folder_id"],
        row["stored_name"],
        user["fullname"],
    )
    if not blob.exists() or not blob.is_file():
        flash("File missing on disk.", "error")
        return cloud_redirect(row["folder_id"], "storage", "folders")

    return send_file(blob, as_attachment=True, download_name=row["original_name"])


@app.get("/cloud/files/<int:file_id>/preview")
@login_required
def file_preview(file_id):
    user = current_user()
    if not user:
        abort(401)

    row = get_file_record(user["id"], file_id)
    if not row:
        abort(404)

    blob = resolve_blob_path(
        user["id"],
        row["folder_id"],
        row["stored_name"],
        user["fullname"],
    )
    mime_type = row["mime_type"] or ""
    if blob.exists() and blob.is_file() and mime_type.startswith("image/"):
        return send_file(blob, as_attachment=False, mimetype=mime_type)

    return Response(
        build_preview_svg(file_extension(row["original_name"])),
        mimetype="image/svg+xml",
    )


@app.post("/cloud/files/<int:file_id>/delete")
@login_required
def delete_file(file_id):
    user = current_user()
    if not user:
        return redirect(url_for("login"))

    redirect_folder = request.form.get("folder_id", type=int)
    next_view = normalize_view(request.form.get("next_view", "storage"))
    next_tab = normalize_tab(request.form.get("next_tab", "folders"))

    with db() as con:
        row = con.execute(
            """
            SELECT id, folder_id, stored_name
            FROM files
            WHERE id=? AND user_id=?
            """,
            (file_id, user["id"]),
        ).fetchone()

        if not row:
            abort(404)

        con.execute(
            "DELETE FROM files WHERE id=? AND user_id=?",
            (file_id, user["id"]),
        )

    delete_file_blob(user["id"], row["folder_id"], row["stored_name"])
    flash("File deleted.", "ok")

    back_folder = redirect_folder or row["folder_id"]
    return cloud_redirect(back_folder, next_view, next_tab)


@app.post("/cloud/folders/<int:folder_id>/delete")
@login_required
def delete_folder(folder_id):
    user = current_user()
    if not user:
        return redirect(url_for("login"))

    next_view = normalize_view(request.form.get("next_view", "storage"))
    next_tab = normalize_tab(request.form.get("next_tab", "folders"))
    current_folder_id = request.form.get("folder_id", type=int)
    root_id = ensure_default_workspace(user["id"], user["fullname"])
    if folder_id == root_id:
        flash("Root folder cannot be deleted.", "error")
        return cloud_redirect(current_folder_id or root_id, next_view, next_tab)

    folder = get_folder(user["id"], folder_id)
    if not folder:
        abort(404)

    folder_ids = get_folder_tree_ids(user["id"], folder_id)
    parent_id = folder["parent_id"] or root_id

    with db() as con:
        con.execute(
            "DELETE FROM folders WHERE id=? AND user_id=?",
            (folder_id, user["id"]),
        )

    user_root = user_storage_root(user["id"], user["fullname"])
    legacy_root = STORAGE_DIR / str(user["id"])
    for fid in folder_ids:
        shutil.rmtree(user_root / str(fid), ignore_errors=True)
        shutil.rmtree(legacy_root / str(fid), ignore_errors=True)

    flash("Folder deleted.", "ok")
    return cloud_redirect(parent_id, next_view, next_tab)


@app.post("/cloud/sync/check")
@login_required
def sync_check():
    user = current_user()
    if not user:
        return redirect(url_for("login"))

    folder_id = request.form.get("folder_id", type=int)
    if not folder_id or not get_folder(user["id"], folder_id):
        folder_id = ensure_default_workspace(user["id"], user["fullname"])

    missing_ids, checked_total = find_missing_file_ids(user["id"])
    if missing_ids:
        with db() as con:
            con.executemany(
                "DELETE FROM files WHERE id=? AND user_id=?",
                [(file_id, user["id"]) for file_id in missing_ids],
            )
        flash(
            f"Sync finished. Removed {len(missing_ids)} missing file records out of {checked_total}.",
            "ok",
        )
    else:
        flash(f"Sync finished. Checked {checked_total} files, no issues found.", "ok")

    return cloud_redirect(folder_id, "sync", "folders")


@app.post("/cloud/settings/profile")
@login_required
def update_profile():
    user = current_user()
    if not user:
        return redirect(url_for("login"))

    folder_id = request.form.get("folder_id", type=int)
    if not folder_id or not get_folder(user["id"], folder_id):
        folder_id = ensure_default_workspace(user["id"], user["fullname"])

    fullname = request.form.get("fullname", "").strip()
    if len(fullname) < 2:
        flash("Display name must contain at least 2 characters.", "error")
        return cloud_redirect(folder_id, "settings", "folders")

    old_fullname = user["fullname"]
    with db() as con:
        con.execute(
            "UPDATE users SET fullname=? WHERE id=?",
            (fullname, user["id"]),
        )
    rename_user_storage_root(user["id"], old_fullname, fullname)
    ensure_default_workspace(user["id"], fullname)

    flash("Profile updated.", "ok")
    return cloud_redirect(folder_id, "settings", "folders")


@app.post("/cloud/settings/password")
@login_required
def update_password():
    user = current_user()
    if not user:
        return redirect(url_for("login"))

    folder_id = request.form.get("folder_id", type=int)
    if not folder_id or not get_folder(user["id"], folder_id):
        folder_id = ensure_default_workspace(user["id"], user["fullname"])

    current_password = request.form.get("current_password", "")
    new_password = request.form.get("new_password", "")
    confirm_password = request.form.get("confirm_password", "")

    with db() as con:
        row = con.execute(
            "SELECT password FROM users WHERE id=?",
            (user["id"],),
        ).fetchone()

    if not row or not check_password_hash(row["password"], current_password):
        flash("Текущий пароль не подходит.", "error")
        return cloud_redirect(folder_id, "settings", "folders")
    if new_password != confirm_password:
        flash("Пароль не подходит.", "error")
        return cloud_redirect(folder_id, "settings", "folders")
    if not valid_password(new_password):
        flash("Пароль не подходит.", "error")
        return cloud_redirect(folder_id, "settings", "folders")

    with db() as con:
        con.execute(
            "UPDATE users SET password=? WHERE id=?",
            (generate_password_hash(new_password), user["id"]),
        )

    flash("Password changed.", "ok")
    return cloud_redirect(folder_id, "settings", "folders")


@app.errorhandler(RequestEntityTooLarge)
def too_large(_error):
    flash(f"File too large. Max upload size is {MAX_UPLOAD_MB} MB.", "error")
    return cloud_redirect(None, "storage", "folders")


if __name__ == "__main__":
    app.run(debug=True)
