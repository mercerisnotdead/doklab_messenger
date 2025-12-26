"""
websocket_logic.py

Sync (WSGI-friendly) WebSocket message handler for Flask-Sock.

This module is adapted from websocket_server_fixed_v7.py:
- In-memory rooms/history
- Auth via PostgreSQL (Async SQLAlchemy) using AsyncSessionLocal from db.py
- Group rooms + DM rooms (личные чаты)
- Media (image/video) base64 with 100MB limit
- Edit/Delete messages (admin can delete чужие)
- Read receipts (mark_read -> broadcast read)
- Room profile actions: rename, set avatar, delete (admin)

How to use (in app.py):

    from flask_sock import Sock
    from websocket_logic import on_ws_connect, on_ws_disconnect, handle_ws_message

    sock = Sock(app)

    @sock.route("/ws")
    def ws_route(ws):
        on_ws_connect(ws)
        try:
            while True:
                raw = ws.receive()
                if raw is None:
                    break
                try:
                    obj = json.loads(raw)
                except Exception:
                    ws.send(json.dumps({"type":"error","text":"Некорректный JSON"}, ensure_ascii=False))
                    continue
                handle_ws_message(ws, obj)
        finally:
            on_ws_disconnect(ws)

Notes:
- This module keeps state in RAM. Restarting the process clears rooms/history.
- For Render/WSGI, prefer running Flask with a WebSocket-capable server (Render Web Service supports it).
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import time
from typing import Any, Dict, Optional, Set

from sqlalchemy import select

from db import AsyncSessionLocal, User

log = logging.getLogger(__name__)

# 100 MB media limit (decoded bytes)
MAX_BYTES = 100 * 1024 * 1024
ALLOWED_PREFIXES = ("image/", "video/")

HISTORY_LIMIT = 400  # per room, kept in RAM only

# ws -> username
ONLINE: Dict[Any, str] = {}
# username -> set(ws)
ONLINE_BY_USER: Dict[str, Set[Any]] = {}
# ws -> connection state
STATE: Dict[Any, Dict[str, Any]] = {}

# room -> info (in-memory)
# info = {
#   "owner": str,
#   "public": bool,
#   "avatar": Optional[str],  # dataURL or None
#   "members": set[str],
#   "invited": set[str],
#   "history": list[dict],
#   "seq": int,
#   "last_read": dict[str,int],
# }
ROOMS: Dict[str, dict] = {}


# --------------------------- helpers: async bridge ---------------------------

def _run(coro):
    """
    Run an async coroutine from sync context safely.
    WSGI threads typically have no running event loop.
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None

    if loop and loop.is_running():
        # If running inside an existing loop (rare here), create a task and wait.
        fut = asyncio.run_coroutine_threadsafe(coro, loop)  # type: ignore
        return fut.result()
    return asyncio.run(coro)


# --------------------------- helpers: sending -------------------------------

def _send(ws, obj: Any) -> None:
    ws.send(json.dumps(obj, ensure_ascii=False))

def _safe_send(ws, obj: Any) -> bool:
    try:
        _send(ws, obj)
        return True
    except Exception:
        return False

def _send_to_user(user: str, payload: dict) -> None:
    conns = ONLINE_BY_USER.get(user) or set()
    dead = []
    for w in list(conns):
        if not _safe_send(w, payload):
            dead.append(w)
    for w in dead:
        _drop_ws(w)

def _broadcast_to_room(room: str, obj: dict, exclude: Optional[Any] = None) -> None:
    info = ROOMS.get(room)
    if not info:
        return

    for w, u in list(ONLINE.items()):
        if exclude is not None and w is exclude:
            continue
        if u not in info["members"]:
            continue
        if not _safe_send(w, obj):
            _drop_ws(w)


# --------------------------- rooms/history ----------------------------------

def ensure_room(name: str, owner: str) -> dict:
    info = ROOMS.get(name)
    if info is None:
        info = {
            "owner": owner,
            "public": True,
            "avatar": None,
            "members": set(),
            "invited": set(),
            "history": [],
            "seq": 0,
            "last_read": {},
        }
        ROOMS[name] = info
    return info

def add_history(room: str, record: dict) -> None:
    info = ROOMS.get(room)
    if not info:
        return
    info["history"].append(record)
    if len(info["history"]) > HISTORY_LIMIT:
        del info["history"][: len(info["history"]) - HISTORY_LIMIT]

def next_id(room: str) -> int:
    info = ROOMS.get(room)
    if not info:
        return 0
    info["seq"] += 1
    return info["seq"]

def normalize_record_for_client(rec: dict) -> dict:
    out = dict(rec)

    if not out.get("type"):
        out["type"] = out.get("kind") or "text"

    if "replyTo" not in out:
        out["replyTo"] = out.get("reply_to")
    if "reply_to" not in out:
        out["reply_to"] = out.get("replyTo")

    if out.get("reactions") is None:
        out["reactions"] = {}

    if not out.get("ts"):
        out["ts"] = int(time.time())

    return out

def get_history_items(room: str) -> list[dict]:
    info = ROOMS.get(room)
    if not info:
        return []
    return [normalize_record_for_client(r) for r in info["history"]]

def find_message(room: str, msg_id: int) -> Optional[dict]:
    info = ROOMS.get(room)
    if not info:
        return None
    for r in info["history"]:
        if int(r.get("id", -1)) == int(msg_id):
            return r
    return None

def is_admin(room: str, username: str) -> bool:
    info = ROOMS.get(room)
    return bool(info and info.get("owner") == username)

def is_dm_room(room: str) -> bool:
    return isinstance(room, str) and room.startswith("dm:")

def dm_room_id(a: str, b: str) -> str:
    a = (a or "").strip()
    b = (b or "").strip()
    x, y = sorted([a, b])
    return f"dm:{x}|{y}"

def dm_other(room: str, me: str) -> Optional[str]:
    if not is_dm_room(room):
        return None
    try:
        _, pair = room.split(":", 1)
        u1, u2 = pair.split("|", 1)
        return u2 if me == u1 else (u1 if me == u2 else None)
    except Exception:
        return None

def room_item_for(requester: str, room: str, info: dict) -> dict:
    base = {
        "name": room,
        "room": room,
        "public": bool(info.get("public", True)),
        "owner": info.get("owner"),
        "avatar": info.get("avatar"),
    }
    if is_dm_room(room):
        other = dm_other(room, requester) or room
        base["title"] = other
        base["dm"] = True
        base["public"] = False
    else:
        base["title"] = room
        base["dm"] = False
    return base

def _safe_b64_len(data_b64: str) -> int:
    return (len(data_b64) * 3) // 4

def ensure_dm_membership(room: str, sender: str) -> None:
    if not is_dm_room(room):
        return
    info = ROOMS.get(room)
    if info is None:
        info = ensure_room(room, owner="system")
        info["public"] = False

    info["members"].add(sender)
    info["last_read"].setdefault(sender, 0)

    other = dm_other(room, sender)
    if other:
        info["members"].add(other)
        info["last_read"].setdefault(other, 0)
        # notify recipient so DM appears immediately if online
        try:
            item = room_item_for(other, room, info)
        except Exception:
            item = {"name": room, "room": room, "title": other, "dm": True, "public": False, "owner": info.get("owner"), "avatar": info.get("avatar")}
        _send_to_user(other, {"type": "room_created", **item})


def push_presence(room: Optional[str] = None) -> None:
    """
    Client expects:
      {type:'presence', room, users:[{name, status}]}
    """
    online_set = set(ONLINE.values())

    if room:
        info = ROOMS.get(room)
        if not info:
            return
        users = [{"name": u, "status": ("online" if u in online_set else "offline")} for u in sorted(info["members"])]
        _broadcast_to_room(room, {"type": "presence", "room": room, "users": users})
        return

    users = [{"name": u, "status": "online"} for u in sorted(online_set)]
    for w in list(ONLINE.keys()):
        if not _safe_send(w, {"type": "presence", "room": None, "users": users}):
            _drop_ws(w)


# --------------------------- connection lifecycle ----------------------------

def on_ws_connect(ws) -> None:
    STATE[ws] = {"username": None, "current_room": None}

def _drop_ws(ws) -> None:
    st = STATE.get(ws) or {}
    u = ONLINE.pop(ws, None)
    if u:
        s = ONLINE_BY_USER.get(u)
        if s:
            s.discard(ws)
            if not s:
                ONLINE_BY_USER.pop(u, None)

    STATE.pop(ws, None)

    # If this was the last connection of the user, we only update presence,
    # membership is not removed (membership is a room property).
    cur = st.get("current_room")
    if cur:
        push_presence(cur)

def on_ws_disconnect(ws) -> None:
    _drop_ws(ws)


# --------------------------- auth (PostgreSQL) -------------------------------

async def _db_get_user(username: str) -> Optional[User]:
    async with AsyncSessionLocal() as session:
        res = await session.execute(select(User).where(User.username == username))
        return res.scalar_one_or_none()

async def _db_register(username: str, password: str) -> bool:
    async with AsyncSessionLocal() as session:
        res = await session.execute(select(User).where(User.username == username))
        user_obj = res.scalar_one_or_none()
        if user_obj is not None:
            return False
        user_obj = User(username=username, password_hash=password)
        session.add(user_obj)
        await session.commit()
        return True

async def _db_login(username: str, password: str) -> bool:
    u = await _db_get_user(username)
    if u is None:
        return False
    return (u.password_hash or "") == password


# --------------------------- main message handler ----------------------------

def handle_ws_message(ws, obj: dict) -> None:
    st = STATE.get(ws)
    if st is None:
        on_ws_connect(ws)
        st = STATE[ws]

    username = st.get("username")
    current_room = st.get("current_room")

    t = obj.get("type")

    # ---------------- AUTH ----------------
    if username is None:
        if t in ("register", "login"):
            u = (obj.get("username") or "").strip()
            p = (obj.get("password") or "").strip()
            if not u or not p:
                _send(ws, {"type": "error", "text": "Имя и пароль обязательны"})
                return

            if t == "register":
                ok = _run(_db_register(u, p))
                if not ok:
                    _send(ws, {"type": "error", "text": "Имя занято"})
                    return
            else:
                ok = _run(_db_login(u, p))
                if not ok:
                    _send(ws, {"type": "error", "text": "Неверное имя или пароль"})
                    return

            ONLINE[ws] = u
            ONLINE_BY_USER.setdefault(u, set()).add(ws)
            st["username"] = u
            _send(ws, {"type": "auth_ok", "user": u})
            return

        _send(ws, {"type": "error", "text": "Сначала нужно /login или /register"})
        return

    # ---------------- ROOMS ----------------
    if t == "list_rooms":
        visible = []
        for r, info in ROOMS.items():
            if info["public"] or username in info["members"] or username in info["invited"]:
                if is_dm_room(r) and username not in info["members"]:
                    continue
                visible.append(room_item_for(username, r, info))
        _send(ws, {"type": "rooms", "items": visible})
        return

    if t == "dm_open":
        target = (obj.get("user") or obj.get("username") or obj.get("to") or "").strip()
        if not target:
            _send(ws, {"type": "error", "text": "Не указан пользователь"})
            return
        if target == username:
            _send(ws, {"type": "error", "text": "Нельзя писать самому себе"})
            return

        room = dm_room_id(username, target)
        info = ROOMS.get(room)
        if info is None:
            info = ensure_room(room, owner="system")
            info["public"] = False

        info["members"].add(username)
        info["members"].add(target)
        info["last_read"].setdefault(username, 0)
        info["last_read"].setdefault(target, 0)

        st["current_room"] = room
        _send(ws, {"type": "room_created", **room_item_for(username, room, info)})
        _send(ws, {"type": "joined", "room": room})
        _send(ws, {"type": "history", "room": room, "items": get_history_items(room)})
        _send(ws, {"type": "room_info", **room_item_for(username, room, info)})
        push_presence(room)

        _send_to_user(target, {"type": "room_created", **room_item_for(target, room, info)})
        return

    if t == "create_room":
        room = (obj.get("room") or "").strip()
        is_public = bool(obj.get("public", True))
        if not room:
            _send(ws, {"type": "error", "text": "Название комнаты обязательно"})
            return
        if room in ROOMS:
            _send(ws, {"type": "error", "text": "Комната уже существует"})
            return

        info = ensure_room(room, owner=username)
        info["public"] = is_public
        info["members"].add(username)
        info["last_read"].setdefault(username, 0)

        _send(ws, {"type": "room_created", **room_item_for(username, room, info)})
        push_presence(room)
        return

    if t == "join":
        room = (obj.get("room") or "").strip()
        info = ROOMS.get(room)
        if not info:
            _send(ws, {"type": "error", "text": "Нет такой комнаты"})
            return

        if is_dm_room(room):
            other = dm_other(room, username)
            if other is None:
                _send(ws, {"type": "error", "text": "Нет доступа к личному чату"})
                return
            info["members"].add(other)
            info["members"].add(username)
            info["last_read"].setdefault(other, 0)
            info["last_read"].setdefault(username, 0)
        else:
            if (not info["public"]) and (username not in info["invited"]) and (username != info["owner"]):
                _send(ws, {"type": "error", "text": "Комната приватная, нужен инвайт"})
                return
            info["members"].add(username)
            info["last_read"].setdefault(username, 0)

        st["current_room"] = room
        _send(ws, {"type": "joined", "room": room})
        _send(ws, {"type": "history", "room": room, "items": get_history_items(room)})
        _send(ws, {"type": "room_info", **room_item_for(username, room, info)})
        push_presence(room)
        return

    if t == "room_info":
        room = (obj.get("room") or "").strip()
        info = ROOMS.get(room)
        if not info:
            _send(ws, {"type": "error", "text": "Нет такой комнаты"})
            return
        if is_dm_room(room):
            if dm_other(room, username) is None:
                _send(ws, {"type": "error", "text": "Нет доступа к личному чату"})
                return
        else:
            if not info["public"] and username not in info["members"] and username not in info["invited"]:
                _send(ws, {"type": "error", "text": "Нет доступа к комнате"})
                return
        _send(ws, {"type": "room_info", **room_item_for(username, room, info)})
        return

    if t == "invite":
        room = (obj.get("room") or "").strip()
        target = (obj.get("user") or obj.get("username") or "").strip()
        info = ROOMS.get(room)
        if not info:
            _send(ws, {"type": "error", "text": "Нет такой комнаты"})
            return
        if is_dm_room(room):
            _send(ws, {"type": "error", "text": "В личные чаты нельзя приглашать"})
            return
        if info["owner"] != username:
            _send(ws, {"type": "error", "text": "Только администратор может приглашать"})
            return
        if not target:
            _send(ws, {"type": "error", "text": "Кого приглашать?"})
            return
        info["invited"].add(target)
        _send(ws, {"type": "invited", "room": room, "user": target})
        return

    if t == "rename_room":
        room = (obj.get("room") or "").strip()
        new_title = (obj.get("title") or obj.get("new_name") or "").strip()
        if not room or not new_title:
            _send(ws, {"type": "error", "text": "room и title обязательны"})
            return
        if room not in ROOMS:
            _send(ws, {"type": "error", "text": "Нет такой комнаты"})
            return
        if is_dm_room(room):
            _send(ws, {"type": "error", "text": "Личный чат нельзя переименовать"})
            return
        if new_title in ROOMS:
            _send(ws, {"type": "error", "text": "Комната с таким именем уже существует"})
            return
        if not is_admin(room, username):
            _send(ws, {"type": "error", "text": "Только администратор может менять название"})
            return

        info = ROOMS.pop(room)
        ROOMS[new_title] = info
        for r in info["history"]:
            r["room"] = new_title

        _broadcast_to_room(new_title, {"type": "room_renamed", "old": room, "new": new_title, **room_item_for(username, new_title, info)})
        return

    if t == "set_room_avatar":
        room = (obj.get("room") or "").strip()
        avatar = obj.get("avatar")
        info = ROOMS.get(room)
        if not info:
            _send(ws, {"type": "error", "text": "Нет такой комнаты"})
            return
        if is_dm_room(room):
            _send(ws, {"type": "error", "text": "В личном чате нет аватара беседы"})
            return
        if not is_admin(room, username):
            _send(ws, {"type": "error", "text": "Только администратор может менять аватар"})
            return
        if avatar is not None and (not isinstance(avatar, str) or not avatar.startswith("data:")):
            _send(ws, {"type": "error", "text": "avatar должен быть dataURL или null"})
            return
        info["avatar"] = avatar
        _broadcast_to_room(room, {"type": "room_avatar", "room": room, "avatar": info["avatar"]})
        return

    if t == "delete_room":
        room = (obj.get("room") or "").strip()
        info = ROOMS.get(room)
        if not info:
            _send(ws, {"type": "error", "text": "Нет такой комнаты"})
            return
        if is_dm_room(room):
            _send(ws, {"type": "error", "text": "Личный чат нельзя удалить как беседу"})
            return
        if not is_admin(room, username):
            _send(ws, {"type": "error", "text": "Только администратор может удалить беседу"})
            return
        _broadcast_to_room(room, {"type": "room_deleted", "room": room})
        ROOMS.pop(room, None)
        return

    # ---------------- READ RECEIPTS ----------------
    if t == "mark_read":
        room = (obj.get("room") or "").strip()
        up_to = obj.get("up_to")
        info = ROOMS.get(room)
        if not info or username not in info["members"]:
            return
        try:
            up_to = int(up_to or 0)
        except Exception:
            up_to = 0

        prev = int(info["last_read"].get(username, 0))
        if up_to > prev:
            info["last_read"][username] = up_to
            _broadcast_to_room(room, {"type": "read", "room": room, "user": username, "up_to": up_to})
        return

    # ---------------- MESSAGES ----------------
    if t == "text":
        room = (obj.get("room") or "").strip()
        text = (obj.get("text") or "").strip()
        reply_to = obj.get("reply_to")
        if reply_to is None:
            reply_to = obj.get("replyTo")

        if not room or not text:
            _send(ws, {"type": "error", "text": "Комната и текст обязательны"})
            return

        # DM fallback: if client sends room=<username> (legacy), convert to DM room id
        if (not is_dm_room(room)) and (room not in ROOMS):
            target_user = room
            if target_user and target_user != username:
                room = dm_room_id(username, target_user)
                ensure_dm_membership(room, username)
                info_dm = ROOMS.get(room)
                if info_dm:
                    _send(ws, {"type": "room_created", **room_item_for(username, room, info_dm)})

        if is_dm_room(room):
            ensure_dm_membership(room, username)

        info = ROOMS.get(room)
        if not info or username not in info["members"]:
            _send(ws, {"type": "error", "text": "Нет доступа к комнате"})
            return

        mid = next_id(room)
        rec = {
            "id": mid,
            "room": room,
            "from": username,
            "text": text,
            "ts": int(time.time()),
            "reply_to": reply_to,
            "replyTo": reply_to,
            "edited": False,
            "deleted": False,
            "reactions": {},
            "kind": "text",
            "type": "text",
        }
        add_history(room, rec)
        _broadcast_to_room(room, normalize_record_for_client(rec))
        return

    if t == "file":
        room = (obj.get("room") or "").strip()
        name = (obj.get("name") or "file").strip()
        mime = (obj.get("mime") or "").strip()
        data_b64 = obj.get("data") or ""
        size = obj.get("size")

        reply_to = obj.get("reply_to")
        if reply_to is None:
            reply_to = obj.get("replyTo")

        if not room or not mime or not data_b64:
            _send(ws, {"type": "error", "text": "room, mime, data обязательны для файла"})
            return

        # DM fallback: if client sends room=<username> (legacy), convert to DM room id
        if (not is_dm_room(room)) and (room not in ROOMS):
            target_user = room
            if target_user and target_user != username:
                room = dm_room_id(username, target_user)
                ensure_dm_membership(room, username)
                info_dm = ROOMS.get(room)
                if info_dm:
                    _send(ws, {"type": "room_created", **room_item_for(username, room, info_dm)})

        if is_dm_room(room):
            ensure_dm_membership(room, username)

        info = ROOMS.get(room)
        if not info or username not in info["members"]:
            _send(ws, {"type": "error", "text": "Нет доступа к комнате"})
            return

        if not any(mime.startswith(p) for p in ALLOWED_PREFIXES):
            _send(ws, {"type": "error", "text": "Разрешены только изображения и видео"})
            return

        try:
            declared = int(size) if size is not None else None
        except Exception:
            declared = None
        decoded_est = _safe_b64_len(data_b64)
        real_size = declared if declared is not None else decoded_est
        if real_size > MAX_BYTES or decoded_est > MAX_BYTES:
            _send(ws, {"type": "error", "text": "Файл больше 100 МБ"})
            return

        try:
            base64.b64decode(data_b64, validate=True)
        except Exception:
            _send(ws, {"type": "error", "text": "Некорректные данные файла"})
            return

        mid = next_id(room)
        rec = {
            "id": mid,
            "room": room,
            "from": username,
            "name": name,
            "mime": mime,
            "size": real_size,
            "data": data_b64,
            "ts": int(time.time()),
            "reply_to": reply_to,
            "replyTo": reply_to,
            "edited": False,
            "deleted": False,
            "reactions": {},
            "kind": "file",
            "type": "file",
        }
        add_history(room, rec)
        _broadcast_to_room(room, normalize_record_for_client(rec))
        return

    if t == "typing":
        room = (obj.get("room") or "").strip()
        info = ROOMS.get(room)
        if info and username in info["members"]:
            _broadcast_to_room(room, {"type": "typing", "room": room, "from": username})
        return

    if t == "edit_msg":
        room = (obj.get("room") or "").strip()
        msg_id = obj.get("id")
        new_text = (obj.get("text") or "").strip()

        if not room or msg_id is None:
            _send(ws, {"type": "error", "text": "Не указан room или id"})
            return
        info = ROOMS.get(room)
        if not info or username not in info["members"]:
            _send(ws, {"type": "error", "text": "Нет доступа к комнате"})
            return

        rec = find_message(room, int(msg_id))
        if not rec:
            _send(ws, {"type": "error", "text": "Сообщение не найдено"})
            return
        if rec.get("from") != username:
            _send(ws, {"type": "error", "text": "Можно редактировать только свои сообщения"})
            return
        if rec.get("deleted"):
            _send(ws, {"type": "error", "text": "Нельзя редактировать удалённое сообщение"})
            return
        if rec.get("type") != "text":
            _send(ws, {"type": "error", "text": "Редактировать можно только текстовые сообщения"})
            return

        rec["text"] = new_text
        rec["edited"] = True
        _broadcast_to_room(room, {"type": "edited", "room": room, "id": int(msg_id), "text": new_text, "edited": True})
        return

    if t == "delete_msg":
        room = (obj.get("room") or "").strip()
        msg_id = obj.get("id")

        if not room or msg_id is None:
            _send(ws, {"type": "error", "text": "Не указан room или id"})
            return
        info = ROOMS.get(room)
        if not info or username not in info["members"]:
            _send(ws, {"type": "error", "text": "Нет доступа к комнате"})
            return

        rec = find_message(room, int(msg_id))
        if not rec:
            _send(ws, {"type": "error", "text": "Сообщение не найдено"})
            return

        if rec.get("from") != username and not is_admin(room, username):
            _send(ws, {"type": "error", "text": "Можно удалять только свои сообщения (или быть админом беседы)"})
            return

        rec["deleted"] = True
        rec["text"] = ""
        rec["name"] = ""
        rec["data"] = ""
        _broadcast_to_room(room, {"type": "deleted", "room": room, "id": int(msg_id)})
        return

    _send(ws, {"type": "error", "text": "Неизвестный тип сообщения"})
