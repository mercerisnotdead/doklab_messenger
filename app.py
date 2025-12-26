from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime
from pathlib import Path

from flask import Flask, jsonify, redirect, request, send_from_directory
from flask_sock import Sock
from sqlalchemy import select

from db import AsyncSessionLocal, Appointment, init_db
from websocket_logic import handle_ws_message, on_ws_connect, on_ws_disconnect

BASE_DIR = Path(__file__).resolve().parent

app = Flask(__name__, static_folder=str(BASE_DIR))
sock = Sock(app)


def _init_db_sync() -> None:
    """
    Create tables on startup.

    On Render, the service is restarted automatically on deploy; keeping this at import
    time is acceptable and simplifies first-run bootstrap.
    """
    try:
        asyncio.run(init_db())
    except RuntimeError:
        # In rare cases an event loop may already exist (local dev in some environments).
        loop = asyncio.new_event_loop()
        loop.run_until_complete(init_db())
        loop.close()


# Initialize DB unless explicitly disabled
if os.getenv("SKIP_DB_INIT", "0") != "1":
    _init_db_sync()


# --------------------
# Static pages / files
# --------------------
@app.get("/")
def root():
    return redirect("/login.html")


@app.get("/health")
def health():
    return jsonify({"ok": True})


@app.get("/<path:filename>")
def static_files(filename: str):
    # Serves index.html, login.html, and any other static assets in repo root.
    return send_from_directory(BASE_DIR, filename)


# --------------------
# WebSocket endpoint
# --------------------
@sock.route("/ws")
def ws_route(ws):
    """
    Single WS endpoint for the whole app.

    The frontend is expected to connect to:
      wss://<host>/ws  (or ws:// for local http)
    """
    on_ws_connect(ws)
    try:
        while True:
            raw = ws.receive()
            if raw is None:
                break
            try:
                obj = json.loads(raw)
            except Exception:
                ws.send(json.dumps({"type": "error", "text": "Некорректный JSON"}, ensure_ascii=False))
                continue
            try:
                handle_ws_message(ws, obj)
            except Exception as e:
                # Never crash the WS handler; return a readable error to the client.
                ws.send(json.dumps({"type": "error", "text": f"Ошибка сервера: {e}"}, ensure_ascii=False))
    finally:
        on_ws_disconnect(ws)


# --------------------
# Optional HTTP API (appointments)
# --------------------
@app.get("/api/appointments")
async def get_appointments():
    """
    Kept for compatibility with your existing app.py.
    """
    date_from_s = request.args.get("from")
    date_to_s = request.args.get("to")
    date_from = datetime.fromisoformat(date_from_s) if date_from_s else None
    date_to = datetime.fromisoformat(date_to_s) if date_to_s else None

    async with AsyncSessionLocal() as session:
        stmt = select(Appointment).order_by(Appointment.start_time.asc())
        if date_from:
            stmt = stmt.where(Appointment.end_time >= date_from)
        if date_to:
            stmt = stmt.where(Appointment.start_time <= date_to)

        res = await session.execute(stmt)
        items = []
        for a in res.scalars().all():
            items.append(
                {
                    "id": a.id,
                    "patient_name": a.patient_name,
                    "policy_number": a.policy_number,
                    "doctor": a.doctor,
                    "room": a.room,
                    "start_time": a.start_time.isoformat() if a.start_time else None,
                    "end_time": a.end_time.isoformat() if a.end_time else None,
                    "status": a.status,
                    "result_url": a.result_url,
                }
            )
    return jsonify(items)


@app.post("/api/appointments")
async def create_appointment():
    data = request.get_json(force=True, silent=True) or {}

    def _dt(v):
        return datetime.fromisoformat(v) if v else None

    a = Appointment(
        patient_name=(data.get("patient_name") or "").strip(),
        policy_number=(data.get("policy_number") or "").strip(),
        doctor=(data.get("doctor") or "").strip(),
        room=(data.get("room") or "").strip(),
        start_time=_dt(data.get("start_time")),
        end_time=_dt(data.get("end_time")),
        status=data.get("status") or "pending",
        result_url=data.get("result_url"),
    )

    async with AsyncSessionLocal() as session:
        session.add(a)
        await session.commit()
        await session.refresh(a)

    return jsonify({"id": a.id}), 201


# --------------------
# Local dev run
# --------------------
if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5000"))
    app.run(host="0.0.0.0", port=port, debug=False)
