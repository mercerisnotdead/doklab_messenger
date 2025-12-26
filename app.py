from __future__ import annotations

import asyncio
import os
from datetime import datetime
from pathlib import Path

from flask import Flask, send_from_directory, redirect, request, jsonify
from flask_sock import Sock

from db import init_db, AsyncSessionLocal, Appointment
from websocket_logic import on_ws_connect, on_ws_disconnect, handle_ws_message

BASE_DIR = Path(__file__).resolve().parent

app = Flask(
    __name__,
    static_folder=str(BASE_DIR),
    static_url_path="",
)

sock = Sock(app)


def _init_db_sync():
    asyncio.run(init_db())


# инициализировать БД при старте приложения
_init_db_sync()


@app.route("/")
def root():
    return redirect("/login.html")


@app.route("/health")
def health():
    return {"status": "ok"}


@app.route("/<path:filename>")
def static_files(filename: str):
    """Отдаём index.html, login.html, css, js и т.п."""
    return send_from_directory(BASE_DIR, filename)


@sock.route("/ws")
def ws_route(ws):
    """WebSocket endpoint для чата (один сервис на Render).

    Ожидает JSON-сообщения (формат определён в websocket_logic.py).
    """
    on_ws_connect(ws)
    try:
        while True:
            raw = ws.receive()
            if raw is None:
                break
            # websocket_logic сам валидирует и отвечает ошибками,
            # но мы передаём ему уже распарсенный объект.
            import json
            try:
                obj = json.loads(raw)
            except Exception:
                ws.send(json.dumps({"type": "error", "text": "Некорректный JSON"}, ensure_ascii=False))
                continue
            handle_ws_message(ws, obj)
    finally:
        on_ws_disconnect(ws)



@app.get("/api/appointments")
async def get_appointments():
    """Вернуть список записей для календаря.

    Опциональные query-параметры:
    - from: ISO-дата/дата-время
    - to:   ISO-дата/дата-время
    """
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


@app.post("/api/appointments")
async def create_appointment():
    """Создать новую запись пациента в календаре."""
    data = request.get_json(force=True) or {}

    required = ["patient_name", "policy_number", "start_time", "end_time"]
    missing = [k for k in required if not data.get(k)]
    if missing:
        return jsonify({"error": f"Отсутствуют поля: {', '.join(missing)}"}), 400

    try:
        start_time = datetime.fromisoformat(data["start_time"])
        end_time = datetime.fromisoformat(data["end_time"])
    except ValueError:
        return jsonify({"error": "Неверный формат времени"}), 400

    async with AsyncSessionLocal() as session:
        a = Appointment(
            patient_name=data["patient_name"],
            policy_number=data["policy_number"],
            doctor=data.get("doctor"),
            room=data.get("room"),
            start_time=start_time,
            end_time=end_time,
            category=data.get("category"),
            status=data.get("status") or "pending",
            result_url=data.get("result_url"),
        )
        session.add(a)
        await session.commit()
        await session.refresh(a)

    return jsonify({"id": a.id}), 201


if __name__ == "__main__":
    host = os.getenv("FLASK_HOST", "127.0.0.1")
    port = int(os.getenv("FLASK_PORT", "5000"))
    app.run(host=host, port=port, debug=True)
