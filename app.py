from __future__ import annotations

import asyncio
import json
import os
import threading
from datetime import datetime
from pathlib import Path as _Path

from flask import Flask, jsonify, redirect, request, send_from_directory, session
from flask_sock import Sock

from db import AsyncSessionLocal, Appointment, User, init_db
import websocket_logic as wsl

BASE_DIR = _Path(__file__).resolve().parent

app = Flask(__name__, static_folder=str(BASE_DIR))
# В проде задайте SECRET_KEY в Render Environment Variables
app.secret_key = os.getenv("SECRET_KEY", "dev-secret-change-me")

sock = Sock(app)

# -------------------- DB init (once) --------------------
_db_init_lock = threading.Lock()
_db_inited = False


def _ensure_db_inited() -> None:
    global _db_inited
    if _db_inited:
        return
    with _db_init_lock:
        if _db_inited:
            return
        asyncio.run(init_db())
        _db_inited = True


@app.before_request
def _before_any_request():
    # Инициализация БД перед первым запросом (и WS тоже идёт как HTTP upgrade)
    _ensure_db_inited()


# -------------------- Pages / Static --------------------
@app.get("/")
def root():
    # Если пользователь уже авторизован — сразу в чат
    if session.get("user"):
        return redirect("/index.html")
    return redirect("/login.html")


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/<path:filename>")
def static_files(filename: str):
    return send_from_directory(BASE_DIR, filename)


# -------------------- Auth (HTTP) --------------------
@app.get("/api/me")
def api_me():
    u = session.get("user")
    if not u:
        return jsonify({"ok": False}), 401
    return jsonify({"ok": True, "user": u})


@app.post("/api/register")
async def api_register():
    data = request.get_json(force=True) or {}
    username = (data.get("username") or "").strip()
    password = (data.get("password") or "").strip()
    if not username or not password:
        return jsonify({"ok": False, "error": "Имя и пароль обязательны"}), 400

    async with AsyncSessionLocal() as db:
        from sqlalchemy import select
        res = await db.execute(select(User).where(User.username == username))
        if res.scalar_one_or_none() is not None:
            return jsonify({"ok": False, "error": "Имя занято"}), 409

        # ВНИМАНИЕ: в текущей версии проекта пароль хранится как есть (совместимость с WS-логикой).
        db.add(User(username=username, password_hash=password))
        await db.commit()

    session["user"] = username
    return jsonify({"ok": True, "user": username})


@app.post("/api/login")
async def api_login():
    data = request.get_json(force=True) or {}
    username = (data.get("username") or "").strip()
    password = (data.get("password") or "").strip()
    if not username or not password:
        return jsonify({"ok": False, "error": "Имя и пароль обязательны"}), 400

    async with AsyncSessionLocal() as db:
        from sqlalchemy import select
        res = await db.execute(select(User).where(User.username == username))
        u = res.scalar_one_or_none()
        if u is None or (u.password_hash or "") != password:
            return jsonify({"ok": False, "error": "Неверное имя или пароль"}), 401

    session["user"] = username
    return jsonify({"ok": True, "user": username})


@app.post("/api/logout")
def api_logout():
    session.pop("user", None)
    return jsonify({"ok": True})


# -------------------- WebSocket (same origin) --------------------
@sock.route("/ws")
def ws_route(ws):
    """
    WebSocket endpoint used by index.html/login.html (same origin):
    - Auth is taken from Flask session (cookie) if present.
    - If session отсутствует, websocket_logic потребует login/register через WS.
    """
    # Ensure state exists
    wsl.on_ws_connect(ws)

    # If logged in via HTTP, pre-auth WS
    user = session.get("user")
    if user:
        st = wsl.STATE.get(ws) or {"username": None, "current_room": None}
        st["username"] = user
        wsl.STATE[ws] = st

        wsl.ONLINE[ws] = user
        wsl.ONLINE_BY_USER.setdefault(user, set()).add(ws)

        # send auth_ok immediately so frontend can proceed
        try:
            ws.send(json.dumps({"type": "auth_ok", "user": user}))
        except Exception:
            pass

    try:
        while True:
            raw = ws.receive()
            if raw is None:
                break
            try:
                obj = json.loads(raw)
            except Exception:
                try:
                    ws.send(json.dumps({"type": "error", "text": "Некорректный JSON"}))
                except Exception:
                    pass
                continue
            wsl.handle_ws_message(ws, obj)
    finally:
        wsl.on_ws_disconnect(ws)


# -------------------- Calendar API (kept) --------------------
@app.get("/api/appointments")
async def get_appointments():
    date_from_s = request.args.get("from")
    date_to_s = request.args.get("to")

    date_from = datetime.fromisoformat(date_from_s) if date_from_s else None
    date_to = datetime.fromisoformat(date_to_s) if date_to_s else None

    async with AsyncSessionLocal() as db:
        from sqlalchemy import select

        stmt = select(Appointment).order_by(Appointment.start_time.asc())
        if date_from:
            stmt = stmt.where(Appointment.end_time >= date_from)
        if date_to:
            stmt = stmt.where(Appointment.start_time <= date_to)

        res = await db.execute(stmt)
        items = []
        for a in res.scalars():
            items.append(
                {
                    "id": a.id,
                    "patient_name": a.patient_name,
                    "policy_number": a.policy_number,
                    "doctor": a.doctor,
                    "room": a.room,
                    "start_time": a.start_time.isoformat(),
                    "end_time": a.end_time.isoformat(),
                    "category": a.category,
                    "status": a.status,
                    "result_url": a.result_url,
                    "completed_at": a.completed_at.isoformat() if a.completed_at else None,
                    "created_by_id": a.created_by_id,
                }
            )
    return jsonify(items)


@app.post("/api/appointments")
async def create_appointment():
    data = request.get_json(force=True) or {}
    required = ["patient_name", "doctor", "room", "start_time", "end_time"]
    for k in required:
        if not data.get(k):
            return jsonify({"error": f"Field '{k}' required"}), 400

    a = Appointment(
        patient_name=data["patient_name"],
        policy_number=data.get("policy_number"),
        doctor=data["doctor"],
        room=data["room"],
        start_time=datetime.fromisoformat(data["start_time"]),
        end_time=datetime.fromisoformat(data["end_time"]),
        category=data.get("category") or "default",
        status=data.get("status") or "pending",
        result_url=data.get("result_url"),
    )

    async with AsyncSessionLocal() as db:
        db.add(a)
        await db.commit()
        await db.refresh(a)

    return jsonify({"id": a.id}), 201


if __name__ == "__main__":
    host = os.getenv("FLASK_HOST", "127.0.0.1")
    port = int(os.getenv("PORT", os.getenv("FLASK_PORT", "5000")))
    app.run(host=host, port=port, debug=True)
