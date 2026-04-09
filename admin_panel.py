# admin_panel.py
import os
import sqlite3
from datetime import datetime, timedelta
from flask import Flask, request, redirect, flash, session, render_template_string

app = Flask(__name__)
# 🔑 Обязательно! SECRET_KEY нужен для подписи сессий
app.secret_key = os.getenv("ADMIN_SECRET_KEY", "supersecretkey123")

DB_PATH = "users.db"

# Логин и пароль из .env
ADMIN_LOGIN = os.getenv("ADMIN_LOGIN", "admin")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "12345")

# ========================
# ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
# ========================
def init_db():
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                is_premium BOOLEAN DEFAULT 0,
                premium_until TEXT
            )
        """)

def activate_premium(user_id, days=30):
    until = datetime.utcnow() + timedelta(days=days)
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""
            INSERT INTO users (user_id, is_premium, premium_until)
            VALUES (?, 1, ?)
            ON CONFLICT(user_id) DO UPDATE SET is_premium = 1, premium_until = ?
        """, (user_id, until.isoformat(), until.isoformat()))

def revoke_premium(user_id):
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("UPDATE users SET is_premium = 0, premium_until = NULL WHERE user_id = ?", (user_id,))

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def require_auth(f):
    """Декоратор для защищённых страниц"""
    def wrapper(*args, **kwargs):
        if "logged_in" not in session or not session["logged_in"]:
            return redirect("/login")
        return f(*args, **kwargs)
    wrapper.__name__ = f.__name__
    return wrapper

# ========================
# МАРШРУТЫ
# ========================
@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        login = request.form.get("login", "")
        password = request.form.get("password", "")
        if login == ADMIN_LOGIN and password == ADMIN_PASSWORD:
            session["logged_in"] = True
            return redirect("/admin")
        else:
            flash("❌ Неверный логин или пароль", "error")
    return render_template_string('''
    <!DOCTYPE html>
    <html>
    <head><title>Вход</title><meta charset="utf-8"></head>
    <body style="font-family: sans-serif; max-width: 400px; margin: 100px auto;">
        <h2>🔐 Админка — Вход</h2>
        {% with messages = get_flashed_messages(with_categories=true) %}
          {% if messages %}
            {% for category, message in messages %}
              <div style="padding:10px; margin:10px 0; background:#fee; color:#c00; border-left:4px solid #c00;">{{ message }}</div>
            {% endfor %}
          {% endif %}
        {% endwith %}
        <form method="post">
            <input name="login" placeholder="Логин" required style="width:100%; padding:8px; margin:5px 0"><br>
            <input name="password" type="password" placeholder="Пароль" required style="width:100%; padding:8px; margin:5px 0"><br>
            <button type="submit" style="width:100%; padding:10px; background:#007bff; color:white; border:none">Войти</button>
        </form>
    </body>
    </html>
    ''')

@app.route("/admin")
@require_auth
def admin():
    conn = get_db()
    users = conn.execute("""
        SELECT user_id, is_premium, premium_until,
               CASE WHEN is_premium = 1 AND datetime(premium_until) > datetime('now') THEN 1 ELSE 0 END as active
        FROM users ORDER BY active DESC, user_id
    """).fetchall()
    conn.close()
    
    active_premium = sum(1 for u in users if u['active'])
    
    return render_template_string('''
    <!DOCTYPE html>
    <html>
    <head><title>Админка</title><meta charset="utf-8"></head>
    <body style="font-family: Arial, sans-serif; max-width: 900px; margin: 20px auto; padding: 0 10px;">
        <h1>🏆 Админ-панель Football Bot</h1>
        <p>Всего пользователей: {{ users|length }} | Premium активен: {{ active_premium }}</p>

        <h3>➕ Выдать Premium вручную</h3>
        <form method="post" action="/admin/grant" style="margin:10px 0">
            <input name="user_id" type="number" placeholder="ID пользователя (из Telegram)" required style="padding:6px; margin-right:5px">
            <input name="days" type="number" value="30" min="1" style="width:60px; padding:6px; margin-right:5px">
            <button type="submit" style="padding:6px 12px; background:#28a745; color:white; border:none">Выдать</button>
        </form>

        <h3>👥 Список пользователей</h3>
        <table border="1" cellpadding="10" style="width:100%; border-collapse: collapse; margin-top:15px;">
            <thead><tr><th>ID</th><th>Premium</th><th>Действие</th></tr></thead>
            <tbody>
            {% for u in users %}
                <tr>
                    <td>{{ u.user_id }}</td>
                    <td>{% if u.active %}✅ Активен до {{ u.premium_until }}{% else %}❌ Нет{% endif %}</td>
                    <td>
                        <form method="post" action="/admin/revoke" style="display:inline;">
                            <input type="hidden" name="user_id" value="{{ u.user_id }}">
                            <button type="submit" style="padding:4px 10px; background:#dc3545; color:white; border:none; cursor:pointer;">Отозвать</button>
                        </form>
                    </td>
                </tr>
            {% endfor %}
            </tbody>
        </table>

        <p style="margin-top:30px;"><a href="/logout" style="color:#666;">🚪 Выйти</a></p>
    </body>
    </html>
    ''', users=users, active_premium=active_premium)

@app.route("/admin/grant", methods=["POST"])
@require_auth
def grant():
    try:
        user_id = int(request.form["user_id"])
        days = int(request.form.get("days", 30))
        activate_premium(user_id, days)
        flash(f"✅ Premium выдан пользователю {user_id} на {days} дней", "success")
    except Exception as e:
        flash(f"❌ Ошибка: {e}", "error")
    return redirect("/admin")

@app.route("/admin/revoke", methods=["POST"])
@require_auth
def revoke():
    try:
        user_id = int(request.form["user_id"])
        revoke_premium(user_id)
        flash(f"✅ Premium отозван у пользователя {user_id}", "success")
    except Exception as e:
        flash(f"❌ Ошибка: {e}", "error")
    return redirect("/admin")

@app.route("/logout")
def logout():
    session.pop("logged_in", None)
    return redirect("/login")

# ========================
# ЗАПУСК
# ========================
if __name__ == "__main__":
    if not os.path.exists(DB_PATH):
        init_db()
        print("✅ База данных создана.")
    else:
        print("📁 База данных уже существует.")
    print("🚀 Админ-панель запущена: http://localhost:5000/login")
    app.run(host="0.0.0.0", port=5000, debug=False)