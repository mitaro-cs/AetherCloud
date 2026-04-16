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
    render_template,
    request,
    send_file,
    url_for,
    session,
)
from werkzeug.exceptions import RequestEntityTooLarge
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.utils import secure_filename

ADMIN_EMAIL = "miri.saro@bk.ru"
ROOT_FOLDER_NAME = "Aether Space"
VALID_VIEWS = {"home", "storage", "sync", "stats", "settings"}
VALID_TABS = {"folders", "tags"}
FOLDER_THEMES = ("aqua", "violet", "sunset", "mint", "amber")
VIEW_META = {
    "home": (
        "Command Center",
        "Everything connected to your self-hosted storage node in one view.",
    ),
    "storage": (
        None,
        "Browse folders, upload files and keep your library under your control.",
    ),
    "sync": (
        "Sync Center",
        "Run consistency checks between SQLite metadata and the real disk.",
    ),
    "stats": (
        "Usage Analytics",
        "See capacity, file mix and workspace growth without leaving your node.",
    ),
    "settings": (
        "Workspace Settings",
        "Update your identity, password and deployment-facing details.",
    ),
}


def create_app():
    app = Flask(__name__, template_folder="templates", static_folder="static")

    project_dir = Path(__file__).resolve().parents[1]
    db_path = Path(os.getenv("AETHER_DB_PATH", str(project_dir / "users.db")))
    storage_dir = Path(os.getenv("AETHER_STORAGE_DIR", str(project_dir / "storage")))
    total_storage_gb = int(os.getenv("AETHER_TOTAL_STORAGE_GB", "280"))
    max_upload_mb = int(os.getenv("AETHER_MAX_UPLOAD_MB", "250"))
    app_host = os.getenv("AETHER_HOST", "127.0.0.1")
    app_port = int(os.getenv("AETHER_PORT", "5000"))
    tuna_url = os.getenv("AETHER_TUNA_URL", "").strip()
    password_hash_method = os.getenv(
        "AETHER_PASSWORD_HASH_METHOD", "pbkdf2:sha256"
    )

    app.config.update(
        SECRET_KEY=os.getenv("AETHER_SECRET_KEY", "super-secret-key-change-me"),
        PROJECT_DIR=project_dir,
        DB_PATH=db_path,
        STORAGE_DIR=storage_dir,
        TOTAL_SHARED_STORAGE_GB=total_storage_gb,
        TOTAL_SHARED_STORAGE_BYTES=total_storage_gb * 1024 * 1024 * 1024,
        MAX_UPLOAD_MB=max_upload_mb,
        MAX_UPLOAD_BYTES=max_upload_mb * 1024 * 1024,
        PASSWORD_HASH_METHOD=password_hash_method,
        APP_HOST=app_host,
        APP_PORT=app_port,
        TUNA_PUBLIC_URL=tuna_url,
    )
    app.config["MAX_CONTENT_LENGTH"] = app.config["MAX_UPLOAD_BYTES"]

    def ensure_runtime_paths():
        app.config["DB_PATH"].parent.mkdir(parents=True, exist_ok=True)
        app.config["STORAGE_DIR"].mkdir(parents=True, exist_ok=True)

    def utc_now():
        return datetime.now(timezone.utc).isoformat(timespec="seconds")

    def db():
        con = sqlite3.connect(app.config["DB_PATH"])
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA foreign_keys = ON")
        return con

    def init_db():
        with db() as con:

            def table_columns(table_name):
                rows = con.execute(f"PRAGMA table_info({table_name})").fetchall()
                return {row["name"] for row in rows}

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

                CREATE INDEX IF NOT EXISTS idx_folders_user_parent
                ON folders(user_id, parent_id);

                CREATE INDEX IF NOT EXISTS idx_files_user_folder
                ON files(user_id, folder_id);
                """
            )

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

    def hash_password(password):
        return generate_password_hash(password, method=app.config["PASSWORD_HASH_METHOD"])

    def verify_password(stored_hash, password):
        try:
            return check_password_hash(stored_hash, password), None
        except (AttributeError, ValueError) as exc:
            message = str(exc).lower()
            if "hashlib" in message and "scrypt" in message:
                return (
                    False,
                    "Password hash is not supported in this Python build. "
                    "Use Forgot password to reset it.",
                )
            return False, "Unable to verify password. Use Forgot password to reset it."

    def normalized_public_url(raw_url):
        value = (raw_url or "").strip().rstrip("/")
        if not value:
            return ""
        if not value.startswith(("http://", "https://")):
            value = f"https://{value}"
        return value

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
        return max(app.config["TOTAL_SHARED_STORAGE_BYTES"] // total_users(), 1)

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
                """
                SELECT id, user_id, parent_id, name, created
                FROM folders
                WHERE id=? AND user_id=?
                """,
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
                """
                SELECT id, name
                FROM folders
                WHERE user_id=? AND parent_id IS NULL
                ORDER BY id
                LIMIT 1
                """,
                (user_id,),
            ).fetchone()
            if root:
                if root_name and root["name"] != root_name:
                    con.execute(
                        "UPDATE folders SET name=? WHERE id=? AND user_id=?",
                        (root_name, root["id"], user_id),
                    )
                return int(root["id"])

            cur = con.execute(
                "INSERT INTO folders(user_id, parent_id, name, created) VALUES(?,?,?,?)",
                (user_id, None, root_name, utc_now()),
            )
            return int(cur.lastrowid)

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
            SELECT id, folder_id, original_name, size, mime_type, uploaded, stored_name
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
                SELECT id, folder_id, original_name, size, mime_type, uploaded, stored_name
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
            return con.execute(
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
        return app.config["STORAGE_DIR"] / f"{safe}_{user_id}"

    def user_blob_path(user_id, folder_id, stored_name, fullname=None):
        return user_storage_root(user_id, fullname) / str(folder_id) / stored_name

    def legacy_blob_path(user_id, folder_id, stored_name):
        return app.config["STORAGE_DIR"] / str(user_id) / str(folder_id) / stored_name

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
<stop offset="0%" stop-color="#171d2e"/>
<stop offset="100%" stop-color="#0b101d"/>
</linearGradient>
</defs>
<rect x="6" y="6" width="84" height="84" rx="18" fill="url(#bg)" stroke="#303754"/>
<rect x="22" y="20" width="52" height="56" rx="12" fill="#111827" stroke="#495270"/>
<text x="48" y="54" fill="#f8fafc" font-size="14" font-family="Arial, sans-serif" text-anchor="middle">{token}</text>
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

    def folder_theme(seed_text):
        index = sum(ord(char) for char in str(seed_text)) % len(FOLDER_THEMES)
        return FOLDER_THEMES[index]

    def serialize_folder(row):
        return {
            "id": int(row["id"]),
            "name": row["name"],
            "file_count": int(row["file_count"]),
            "created_short": short_time(row["created"]),
            "theme": folder_theme(f"folder-{row['id']}-{row['name']}"),
        }

    def serialize_file(row):
        ext = file_extension(row["original_name"])
        return {
            "id": int(row["id"]),
            "folder_id": int(row["folder_id"]),
            "original_name": row["original_name"],
            "ext": ext,
            "size_human": human_size(row["size"]),
            "uploaded_short": short_time(row["uploaded"]),
            "thumb_url": url_for("file_preview", file_id=row["id"]),
            "download_url": url_for("download_file", file_id=row["id"]),
            "theme": folder_theme(f"file-{row['id']}-{ext}"),
        }

    def view_title(active_view, current_folder_name):
        title, subtitle = VIEW_META[active_view]
        if active_view == "storage":
            return current_folder_name, subtitle
        return title, subtitle

    ensure_runtime_paths()
    init_db()

    @app.context_processor
    def inject_globals():
        return {
            "public_url": normalized_public_url(app.config["TUNA_PUBLIC_URL"]),
        }

    @app.get("/")
    def landing():
        viewer = current_user()
        return render_template(
            "landing.html",
            title="AetherCloud",
            viewer=viewer,
            total_storage_gb=app.config["TOTAL_SHARED_STORAGE_GB"],
            docker_volume="./data:/data",
            windows_disk_example="E:/AetherCloudData:/data",
            public_url=normalized_public_url(app.config["TUNA_PUBLIC_URL"]),
        )

    @app.get("/manifest.webmanifest")
    def manifest_file():
        return send_file(
            Path(app.static_folder) / "manifest.webmanifest",
            mimetype="application/manifest+json",
            max_age=0,
        )

    @app.get("/favicon.svg")
    def favicon():
        return send_file(
            Path(app.static_folder) / "icons" / "aether-icon.svg",
            mimetype="image/svg+xml",
            max_age=3600,
        )

    @app.get("/sw.js")
    def service_worker():
        return send_file(
            Path(app.static_folder) / "sw.js",
            mimetype="application/javascript",
            max_age=0,
        )

    @app.get("/health")
    def health():
        return Response("ok", mimetype="text/plain")

    @app.route("/register", methods=["GET", "POST"])
    @app.route("/signup", methods=["GET", "POST"])
    def register():
        if current_user():
            return redirect(url_for("cloud"))

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
                error = "Passwords do not match."
            elif not valid_password(password):
                error = "Password must include 8 characters, a number and a symbol."
            else:
                try:
                    with db() as con:
                        cur = con.execute(
                            "INSERT INTO users(fullname, email, password, created) VALUES(?,?,?,?)",
                            (fullname, email, hash_password(password), utc_now()),
                        )
                        user_id = int(cur.lastrowid)
                    ensure_default_workspace(user_id, fullname)
                    flash("Account created. Sign in to continue.", "ok")
                    return redirect(url_for("login"))
                except sqlite3.IntegrityError:
                    error = "User with this email already exists."

        return render_template(
            "auth.html",
            mode="register",
            error=error,
            message=None,
            form=form,
            title="Create Account",
        )

    @app.route("/login", methods=["GET", "POST"])
    def login():
        if current_user():
            return redirect(url_for("cloud"))

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

            if row:
                is_valid, hash_error = verify_password(row["password"], password)
                if hash_error:
                    error = hash_error
                elif is_valid:
                    session["user_id"] = row["id"]
                    ensure_default_workspace(row["id"], row["fullname"])
                    return redirect(url_for("cloud"))
                else:
                    error = "Password is not correct."
            else:
                error = "User not found."

        return render_template(
            "auth.html",
            mode="login",
            error=error,
            message=None,
            form=form,
            title="Sign In",
        )

    @app.route("/forgot", methods=["GET", "POST"])
    def forgot():
        if current_user():
            return redirect(url_for("cloud"))

        error = None
        message = None
        form = {"email": "", "new_password": ""}

        if request.method == "POST":
            email = request.form.get("email", "").strip().lower()
            new_pw = request.form.get("new_password", "")
            form = {"email": email, "new_password": new_pw}

            if not valid_password(new_pw):
                error = "New password must include 8 characters, a number and a symbol."
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
                            (hash_password(new_pw), email),
                        )
                        message = "Password successfully updated."

        return render_template(
            "auth.html",
            mode="forgot",
            error=error,
            message=message,
            form=form,
            title="Reset Access",
        )

    @app.route("/logout")
    def logout():
        session.clear()
        return redirect(url_for("landing"))

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

        child_folders = [
            serialize_folder(item)
            for item in get_child_folders(user["id"], current_folder["id"], search)
        ]
        files = [
            serialize_file(item)
            for item in get_files_in_folder(user["id"], current_folder["id"], search)
        ]
        recent_files = [
            serialize_file(item) for item in get_recent_files(user["id"], limit=6)
        ]

        used_bytes = user_used_space(user["id"])
        quota_bytes = per_user_quota_bytes()
        users_count = total_users()
        free_bytes = max(quota_bytes - used_bytes, 0)
        used_percent = (
            min(round((used_bytes / quota_bytes) * 100, 2), 100) if quota_bytes else 0
        )
        stats = get_user_metrics(user["id"])
        stats["bytes_label"] = human_size(stats["bytes_total"])
        tags = get_tag_summary(user["id"], search if active_tab == "tags" else "")
        latest_file = files[0] if files else (recent_files[0] if recent_files else None)
        title, subtitle = view_title(active_view, current_folder["name"])

        return render_template(
            "cloud.html",
            title="AetherCloud Workspace",
            user=user,
            is_admin=is_admin_user(user),
            nick=user_nick(user["fullname"]),
            current_folder=current_folder,
            current_folder_name=current_folder["name"],
            child_folders=child_folders,
            files=files,
            recent_files=recent_files,
            latest_file=latest_file,
            stats=stats,
            tree=get_folder_tree(user["id"], root_id=root_id, include_root=False),
            breadcrumbs=get_breadcrumbs(user["id"], current_folder["id"]),
            tags=tags,
            active_view=active_view,
            active_tab=active_tab,
            used_label=human_size(used_bytes),
            max_label=human_size(quota_bytes),
            free_label=human_size(free_bytes),
            total_label=human_size(app.config["TOTAL_SHARED_STORAGE_BYTES"]),
            users_count=users_count,
            used_percent=used_percent,
            search=search,
            root_id=root_id,
            view_title=title,
            view_subtitle=subtitle,
            public_url=normalized_public_url(app.config["TUNA_PUBLIC_URL"]),
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

        return render_template(
            "admin.html",
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
        legacy_root = app.config["STORAGE_DIR"] / str(user["id"])
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

        if not row:
            flash("Current password is not correct.", "error")
            return cloud_redirect(folder_id, "settings", "folders")

        current_valid, hash_error = verify_password(row["password"], current_password)
        if hash_error:
            flash(hash_error, "error")
            return cloud_redirect(folder_id, "settings", "folders")
        if not current_valid:
            flash("Current password is not correct.", "error")
            return cloud_redirect(folder_id, "settings", "folders")
        if new_password != confirm_password:
            flash("Passwords do not match.", "error")
            return cloud_redirect(folder_id, "settings", "folders")
        if not valid_password(new_password):
            flash("Password must include 8 characters, a number and a symbol.", "error")
            return cloud_redirect(folder_id, "settings", "folders")

        with db() as con:
            con.execute(
                "UPDATE users SET password=? WHERE id=?",
                (hash_password(new_password), user["id"]),
            )

        flash("Password changed.", "ok")
        return cloud_redirect(folder_id, "settings", "folders")

    @app.errorhandler(RequestEntityTooLarge)
    def too_large(_error):
        flash(
            f"File too large. Max upload size is {app.config['MAX_UPLOAD_MB']} MB.",
            "error",
        )
        if session.get("user_id"):
            return cloud_redirect(None, "storage", "folders")
        return redirect(url_for("login"))

    return app
