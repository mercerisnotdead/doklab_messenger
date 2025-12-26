from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime
from pathlib import Path

from flask import Flask, send_from_directory, redirect, request, jsonify
from flask_sock import Sock

from db import init_db, AsyncSessionLocal, Appointment
from websocket_logic import handle_ws_message, on_ws_connect, on_ws_disconnect

BASE_DIR = Path(__file__).resolve().parent

app = Flask(
    __name__,
    static_folder=str(BASE_DIR),
    static_url_path="",
)

sock = Sock(app)

# --- INIT DB ---
def _init_db_sync():
    asyncio.run(init_db())

_init_db_sync()


# --- ROUTES ---
@app.route("/")
def root():
    return redirect("/login.html")


@app.route("/health")
def health():
    return {"status": "ok"}


@app.route("/<path:filename>")
def static_files(filename: str):
    return send_from_directory(BASE_DIR, filename)


# --- API (appointments, как у вас было) ---
@app.get("/api/appointments")
async def get_appointments():
    date_from_s = request.args.get("from")
    date_to_s = request.args.get("to")

    date_from = datetime.fromisoformat(date_from_s) if date_from_s else None
    date_to = datetime.fromisoformat(date_to_s) if date_to_s else None

    async with AsyncSessionLocal() as session:
        from sqlalchemy import select

        stmt = select(Appointment)
        if date_from:
            stmt = stmt.where(Appointment.start_time >= date_from)
        if date_to:
            stmt = stmt.where(Appointment.start_time <= date_to)

        res = await session.execute(stmt)
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


# --- WEBSOCKET ---
@sock.route("/ws")
def websocket(ws):
    on_ws_connect(ws)
    try:
        while True:
            data = ws.receive()
            if data is None:
                break
            try:
                obj = json.loads(data)
            except Exception:
                ws.send(json.dumps({"type": "error", "text": "Некорректный JSON"}, ensure_ascii=False))
                continue
            handle_ws_message(ws, obj)
    finally:
        on_ws_disconnect(ws)


# --- START ---
if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5000"))
    app.run(host="0.0.0.0", port=port, debug=False)
