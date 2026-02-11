# app.py
import os
import re
import sqlite3
from datetime import datetime

from flask import (
    Flask,
    redirect,
    render_template_string,
    request,
    send_from_directory,
    session,
)
from werkzeug.security import check_password_hash, generate_password_hash

# ================== CONFIG ==================

app = Flask(__name__)
app.secret_key = "super-secret-key"

DB = "users.db"
STORAGE_DIR = "storage"

TOTAL_STORAGE_GB = 10
TOTAL_STORAGE_BYTES = TOTAL_STORAGE_GB * 1024 * 1024 * 1024

BACKGROUND_IMAGE = (
    "https://i.pinimg.com/736x/f9/ad/c2/f9adc243ecb36023bf238bff0f7712d1.jpg"
)

ADMIN_EMAIL = "miri.saro@bk.ru"

os.makedirs(STORAGE_DIR, exist_ok=True)

# ================== DB ==================


def db():
    return sqlite3.connect(DB)


def init_db():
    with db() as con:
        con.execute("""
            CREATE TABLE IF NOT EXISTS users(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                fullname TEXT UNIQUE,
                email TEXT UNIQUE,
                password TEXT,
                created DATETIME
            )
        """)
        con.execute("""
            CREATE TABLE IF NOT EXISTS files(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                filename TEXT,
                size INTEGER,
                uploaded DATETIME
            )
        """)


init_db()

# ================== VALIDATION ==================


def valid_email(email):
    return re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email)


def valid_password(pw):
    return len(pw) >= 8 and re.search(r"[0-9]", pw) and re.search(r"[!@#$%^&*]", pw)


# ================== HELPERS ==================


def current_user():
    if "user_id" not in session:
        return None
    with db() as con:
        return con.execute(
            "SELECT id, fullname, email FROM users WHERE id=?",
            (session["user_id"],),
        ).fetchone()


def is_admin():
    user = current_user()
    return user and user[2] == ADMIN_EMAIL


def total_users():
    with db() as con:
        return con.execute("SELECT COUNT(*) FROM users").fetchone()[0] or 1


def user_used_space(uid):
    with db() as con:
        return con.execute(
            "SELECT COALESCE(SUM(size),0) FROM files WHERE user_id=?",
            (uid,),
        ).fetchone()[0]


# ================== HTML ==================

HTML = """
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
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
.sub{font-size:14px;color:#9a9a9a;margin-bottom:36px}

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

.msg{font-size:13px;margin-bottom:18px}
.err{color:#ff6b6b}

.bottom{text-align:center;font-size:13px;color:#6f6f6f;margin-top:26px}
.bottom a{color:#fff;text-decoration:none}

.file{
    padding:12px;
    background:#111;
    border-radius:10px;
    margin-bottom:10px;
    display:flex;
    justify-content:space-between;
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

{% if mode == "register" %}
<h2>Create your account</h2>
<div class="sub">Access your private cloud workspace</div>
{% if error %}<div class="msg err">{{error}}</div>{% endif %}
<form method="POST">
<label>Full name</label>
<div class="field"><input name="fullname" required></div>
<label>Email</label>
<div class="field"><input name="email" required></div>
<label>Password</label>
<div class="field"><input type="password" name="password" required></div>
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
<div class="field"><input name="email"></div>
<label>Password</label>
<div class="field"><input type="password" name="password"></div>
<button>Sign in</button>
</form>
<div class="bottom"><a href="/">Create account</a> · <a href="/forgot">Forgot password?</a></div>
{% endif %}

{% if mode == "forgot" %}
<h2>Reset password</h2>
<div class="sub">Enter your email and new password</div>
{% if error %}<div class="msg err">{{error}}</div>{% endif %}
{% if message %}<div class="msg" style="color:#00ff9d">{{message}}</div>{% endif %}
<form method="POST">
<label>Email</label>
<div class="field"><input name="email" required></div>
<label>New password</label>
<div class="field"><input type="password" name="new_password" required></div>
<button>Reset password</button>
</form>
<div class="bottom"><a href="/login">Back to login</a></div>
{% endif %}

</div>
</div>
</body>
</html>
"""

# ================== ROUTES ==================


@app.route("/", methods=["GET", "POST"])
def register():
    error = None
    if request.method == "POST":
        try:
            with db() as con:
                con.execute(
                    "INSERT INTO users(fullname,email,password,created) VALUES(?,?,?,?)",
                    (
                        request.form["fullname"],
                        request.form["email"],
                        generate_password_hash(request.form["password"]),
                        datetime.utcnow(),
                    ),
                )
            return redirect("/login")
        except:
            error = "User already exists"

    return render_template_string(
        HTML, mode="register", error=error, title="Register", bg_image=BACKGROUND_IMAGE
    )


@app.route("/login", methods=["GET", "POST"])
def login():
    error = None
    if request.method == "POST":
        with db() as con:
            row = con.execute(
                "SELECT id,password FROM users WHERE email=?",
                (request.form["email"],),
            ).fetchone()
        if row and check_password_hash(row[1], request.form["password"]):
            session["user_id"] = row[0]
            return redirect("/cloud")
        error = "Invalid credentials"

    return render_template_string(
        HTML, mode="login", error=error, title="Login", bg_image=BACKGROUND_IMAGE
    )


@app.route("/forgot", methods=["GET", "POST"])
def forgot():
    error = None
    message = None

    if request.method == "POST":
        email = request.form["email"]
        new_pw = request.form["new_password"]

        with db() as con:
            user = con.execute(
                "SELECT id FROM users WHERE email=?",
                (email,),
            ).fetchone()

            if not user:
                error = "User not found"
            else:
                con.execute(
                    "UPDATE users SET password=? WHERE email=?",
                    (generate_password_hash(new_pw), email),
                )
                message = "Password successfully updated"

    return render_template_string(
        HTML,
        mode="forgot",
        error=error,
        message=message,
        title="Reset",
        bg_image=BACKGROUND_IMAGE,
    )


if __name__ == "__main__":
    app.run(debug=True)
