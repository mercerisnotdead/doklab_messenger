# websocket_server.py
# WS chat server (in-memory rooms/history) + PostgreSQL auth.
# Supports:
# - public/group rooms + invites
# - DM (личные чаты) without manual room creation: dm_open + auto-create on first message
# - room profile: info/avatar/rename/delete (admin)
# - messages: text + file (image/video), replies, edit/delete (admin can delete чужие)
# - presence list in client-friendly format
# - basic read receipts

import asyncio
import argparse
import base64
import json
import logging
import time
from typing import Dict, Any, Optional

import websockets
from sqlalchemy import select

from db import init_db, AsyncSessionLocal, User

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

# 100 MB media limit (decoded bytes)
MAX_BYTES = 100 * 1024 * 1024
ALLOWED_PREFIXES = ("image/", "video/")

# ws -> username
ONLINE: Dict[websockets.WebSocketServerProtocol, str] = {}

# room -> info
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

HISTORY_LIMIT = 400  # per room, kept in RAM only


async def send(ws, obj: Any):
    await ws.send(json.dumps(obj, ensure_ascii=False))


async def broadcast_to_room(room: str, obj: dict, exclude: Optional[websockets.WebSocketServerProtocol] = None):
    data = json.dumps(obj, ensure_ascii=False)
    info = ROOMS.get(room)
    if not info:
        return

    dead = []
    for w, u in list(ONLINE.items()):
        if w is exclude:
            continue
        if u not in info["members"]:
            continue
        try:
            await w.send(data)
        except Exception:
            dead.append(w)

    for w in dead:
        ONLINE.pop(w, None)


async def send_to_user(user: str, payload: dict):
    """Send payload to all online connections of a specific user."""
    data = json.dumps(payload, ensure_ascii=False)
    dead = []
    for w, u in list(ONLINE.items()):
        if u != user:
            continue
        try:
            await w.send(data)
        except Exception:
            dead.append(w)
    for w in dead:
        ONLINE.pop(w, None)


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


def add_history(room: str, record: dict):
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

    # client renders only type='text'/'file'
    if not out.get("type"):
        out["type"] = out.get("kind") or "text"

    # replyTo compatibility
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
    """
    Room list item that the client uses in sidebar.
    For DM rooms, title is the other username and dm=true.
    For group rooms, title is the room name and dm=false.
    """
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


async def push_presence(room: Optional[str] = None):
    """
    Client expects:
      {type:'presence', room, users:[{name, status}]}
    """
    if room:
        info = ROOMS.get(room)
        if not info:
            return
        online_set = set(ONLINE.values())
        users = [{"name": u, "status": ("online" if u in online_set else "offline")} for u in sorted(info["members"])]
        await broadcast_to_room(room, {"type": "presence", "room": room, "users": users})
        return

    online_set = set(ONLINE.values())
    users = [{"name": u, "status": "online"} for u in sorted(online_set)]
    data = json.dumps({"type": "presence", "room": None, "users": users}, ensure_ascii=False)
    dead = []
    for w in list(ONLINE.keys()):
        try:
            await w.send(data)
        except Exception:
            dead.append(w)
    for w in dead:
        ONLINE.pop(w, None)


def _safe_b64_len(data_b64: str) -> int:
    # approximate decoded size without decoding full content
    # 4 b64 chars -> 3 bytes
    return (len(data_b64) * 3) // 4


async def ensure_dm_membership(room: str, sender: str):
    """For DM rooms: ensure both participants are in members and notify recipient to show chat."""
    if not is_dm_room(room):
        return
    info = ROOMS.get(room)
    if info is None:
        info = ensure_room(room, owner="system")
        info["public"] = False

    # add sender
    info["members"].add(sender)
    info["last_read"].setdefault(sender, 0)

    other = dm_other(room, sender)
    if other:
        info["members"].add(other)
        info["last_read"].setdefault(other, 0)
        # notify recipient so chat appears immediately (if online)
        try:
            item = room_item_for(other, room, info)
        except Exception:
            item = {"name": room, "room": room, "title": other, "dm": True, "public": False, "owner": info.get("owner"), "avatar": info.get("avatar")}
        await send_to_user(other, {"type": "room_created", **item})


async def handle(ws: websockets.WebSocketServerProtocol, *args):
    logging.info("New WS connection from %s", ws.remote_address)
    username = None
    current_room = None

    try:
        async for raw in ws:
            try:
                obj = json.loads(raw)
            except json.JSONDecodeError:
                await send(ws, {"type": "error", "text": "Некорректный JSON"})
                continue

            t = obj.get("type")

            # ---------------- AUTH ----------------
            if username is None:
                if t in ("register", "login"):
                    u = (obj.get("username") or "").strip()
                    p = (obj.get("password") or "").strip()
                    if not u or not p:
                        await send(ws, {"type": "error", "text": "Имя и пароль обязательны"})
                        continue

                    async with AsyncSessionLocal() as session:
                        result = await session.execute(select(User).where(User.username == u))
                        user_obj = result.scalar_one_or_none()

                        if t == "register":
                            if user_obj is not None:
                                await send(ws, {"type": "error", "text": "Имя занято"})
                                continue
                            user_obj = User(username=u, password_hash=p)
                            session.add(user_obj)
                            await session.commit()
                        else:
                            if user_obj is None or (user_obj.password_hash or "") != p:
                                await send(ws, {"type": "error", "text": "Неверное имя или пароль"})
                                continue

                    ONLINE[ws] = u
                    username = u
                    await send(ws, {"type": "auth_ok", "user": u})
                    continue

                await send(ws, {"type": "error", "text": "Сначала нужно /login или /register"})
                continue

            # ---------------- ROOMS ----------------
            if t == "list_rooms":
                visible = []
                for r, info in ROOMS.items():
                    # group rooms: public or member/invited
                    if info["public"] or username in info["members"] or username in info["invited"]:
                        # DM rooms only visible to members
                        if is_dm_room(r) and username not in info["members"]:
                            continue
                        visible.append(room_item_for(username, r, info))
                await send(ws, {"type": "rooms", "items": visible})
                continue

            # DM open (no manual "create room")
            if t == "dm_open":
                target = (obj.get("user") or obj.get("username") or obj.get("to") or "").strip()
                if not target:
                    await send(ws, {"type": "error", "text": "Не указан пользователь"})
                    continue
                if target == username:
                    await send(ws, {"type": "error", "text": "Нельзя писать самому себе"})
                    continue

                room = dm_room_id(username, target)
                info = ROOMS.get(room)
                if info is None:
                    info = ensure_room(room, owner="system")
                    info["public"] = False

                info["members"].add(username)
                info["members"].add(target)
                info["last_read"].setdefault(username, 0)
                info["last_read"].setdefault(target, 0)

                current_room = room

                # show immediately to requester
                await send(ws, {"type": "room_created", **room_item_for(username, room, info)})
                await send(ws, {"type": "joined", "room": room})
                await send(ws, {"type": "history", "room": room, "items": get_history_items(room)})
                await send(ws, {"type": "room_info", **room_item_for(username, room, info)})
                await push_presence(room)

                # show immediately to target if online
                await send_to_user(target, {"type": "room_created", **room_item_for(target, room, info)})
                continue

            if t == "create_room":
                room = (obj.get("room") or "").strip()
                is_public = bool(obj.get("public", True))
                if not room:
                    await send(ws, {"type": "error", "text": "Название комнаты обязательно"})
                    continue
                if room in ROOMS:
                    await send(ws, {"type": "error", "text": "Комната уже существует"})
                    continue

                info = ensure_room(room, owner=username)
                info["public"] = is_public
                info["members"].add(username)
                info["last_read"].setdefault(username, 0)

                await send(ws, {"type": "room_created", **room_item_for(username, room, info)})
                await push_presence(room)
                continue

            if t == "join":
                room = (obj.get("room") or "").strip()
                info = ROOMS.get(room)
                if not info:
                    await send(ws, {"type": "error", "text": "Нет такой комнаты"})
                    continue

                if is_dm_room(room):
                    # DMs only for participants
                    other = dm_other(room, username)
                    if other is None:
                        await send(ws, {"type": "error", "text": "Нет доступа к личному чату"})
                        continue
                    info["members"].add(other)
                    info["members"].add(username)
                    info["last_read"].setdefault(other, 0)
                    info["last_read"].setdefault(username, 0)
                else:
                    if (not info["public"]) and (username not in info["invited"]) and (username != info["owner"]):
                        await send(ws, {"type": "error", "text": "Комната приватная, нужен инвайт"})
                        continue
                    info["members"].add(username)
                    info["last_read"].setdefault(username, 0)

                current_room = room
                await send(ws, {"type": "joined", "room": room})
                await send(ws, {"type": "history", "room": room, "items": get_history_items(room)})
                await send(ws, {"type": "room_info", **room_item_for(username, room, info)})
                await push_presence(room)
                continue

            # client requests room_info to refresh profile
            if t == "room_info":
                room = (obj.get("room") or "").strip()
                info = ROOMS.get(room)
                if not info:
                    await send(ws, {"type": "error", "text": "Нет такой комнаты"})
                    continue
                if is_dm_room(room):
                    if dm_other(room, username) is None:
                        await send(ws, {"type": "error", "text": "Нет доступа к личному чату"})
                        continue
                else:
                    if not info["public"] and username not in info["members"] and username not in info["invited"]:
                        await send(ws, {"type": "error", "text": "Нет доступа к комнате"})
                        continue
                await send(ws, {"type": "room_info", **room_item_for(username, room, info)})
                continue

            if t == "invite":
                room = (obj.get("room") or "").strip()
                target = (obj.get("user") or obj.get("username") or "").strip()
                info = ROOMS.get(room)
                if not info:
                    await send(ws, {"type": "error", "text": "Нет такой комнаты"})
                    continue
                if is_dm_room(room):
                    await send(ws, {"type": "error", "text": "В личные чаты нельзя приглашать"})
                    continue
                if info["owner"] != username:
                    await send(ws, {"type": "error", "text": "Только администратор может приглашать"})
                    continue
                if not target:
                    await send(ws, {"type": "error", "text": "Кого приглашать?"})
                    continue
                info["invited"].add(target)
                await send(ws, {"type": "invited", "room": room, "user": target})
                continue

            if t == "rename_room":
                room = (obj.get("room") or "").strip()
                new_title = (obj.get("title") or obj.get("new_name") or "").strip()
                if not room or not new_title:
                    await send(ws, {"type": "error", "text": "room и title обязательны"})
                    continue
                if room not in ROOMS:
                    await send(ws, {"type": "error", "text": "Нет такой комнаты"})
                    continue
                if is_dm_room(room):
                    await send(ws, {"type": "error", "text": "Личный чат нельзя переименовать"})
                    continue
                if new_title in ROOMS:
                    await send(ws, {"type": "error", "text": "Комната с таким именем уже существует"})
                    continue
                if not is_admin(room, username):
                    await send(ws, {"type": "error", "text": "Только администратор может менять название"})
                    continue

                info = ROOMS.pop(room)
                ROOMS[new_title] = info
                for r in info["history"]:
                    r["room"] = new_title

                await broadcast_to_room(new_title, {"type": "room_renamed", "old": room, "new": new_title, **room_item_for(username, new_title, info)})
                continue

            if t == "set_room_avatar":
                room = (obj.get("room") or "").strip()
                avatar = obj.get("avatar")  # dataURL or null
                info = ROOMS.get(room)
                if not info:
                    await send(ws, {"type": "error", "text": "Нет такой комнаты"})
                    continue
                if is_dm_room(room):
                    await send(ws, {"type": "error", "text": "В личном чате нет аватара беседы"})
                    continue
                if not is_admin(room, username):
                    await send(ws, {"type": "error", "text": "Только администратор может менять аватар"})
                    continue
                if avatar is not None and (not isinstance(avatar, str) or not avatar.startswith("data:")):
                    await send(ws, {"type": "error", "text": "avatar должен быть dataURL или null"})
                    continue
                info["avatar"] = avatar
                await broadcast_to_room(room, {"type": "room_avatar", "room": room, "avatar": info["avatar"]})
                continue

            if t == "delete_room":
                room = (obj.get("room") or "").strip()
                info = ROOMS.get(room)
                if not info:
                    await send(ws, {"type": "error", "text": "Нет такой комнаты"})
                    continue
                if is_dm_room(room):
                    await send(ws, {"type": "error", "text": "Личный чат нельзя удалить как беседу"})
                    continue
                if not is_admin(room, username):
                    await send(ws, {"type": "error", "text": "Только администратор может удалить беседу"})
                    continue
                await broadcast_to_room(room, {"type": "room_deleted", "room": room})
                ROOMS.pop(room, None)
                continue

            # ---------------- READ RECEIPTS ----------------
            if t == "mark_read":
                room = (obj.get("room") or "").strip()
                up_to = obj.get("up_to")
                info = ROOMS.get(room)
                if not info or username not in info["members"]:
                    continue
                try:
                    up_to = int(up_to or 0)
                except Exception:
                    up_to = 0

                prev = int(info["last_read"].get(username, 0))
                if up_to > prev:
                    info["last_read"][username] = up_to
                    await broadcast_to_room(room, {"type": "read", "room": room, "user": username, "up_to": up_to})
                continue

            # ---------------- MESSAGES ----------------
            if t == "text":
                room = (obj.get("room") or "").strip()
                text = (obj.get("text") or "").strip()
                reply_to = obj.get("reply_to")
                if reply_to is None:
                    reply_to = obj.get("replyTo")

                if not room or not text:
                    await send(ws, {"type": "error", "text": "Комната и текст обязательны"})
                    continue

                # DM fallback: if client sends room=<username> (legacy), convert to DM room id
                if (not is_dm_room(room)) and (room not in ROOMS):
                    # treat as direct message to that username
                    target_user = room
                    if target_user and target_user != username:
                        room = dm_room_id(username, target_user)
                        # ensure room exists + both members + notify recipient so chat appears
                        await ensure_dm_membership(room, username)
                        # also ensure sender sees this DM in sidebar immediately
                        info_dm = ROOMS.get(room)
                        if info_dm:
                            await send(ws, {"type": "room_created", **room_item_for(username, room, info_dm)})
                    # else: keep as-is (will error below if no such room)

                # DM auto membership + notify recipient
                if is_dm_room(room):
                    await ensure_dm_membership(room, username)

                info = ROOMS.get(room)
                if not info or username not in info["members"]:
                    await send(ws, {"type": "error", "text": "Нет доступа к комнате"})
                    continue

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
                await broadcast_to_room(room, normalize_record_for_client(rec))
                continue

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
                    await send(ws, {"type": "error", "text": "room, mime, data обязательны для файла"})
                    continue

                # DM fallback: if client sends room=<username> (legacy), convert to DM room id
                if (not is_dm_room(room)) and (room not in ROOMS):
                    target_user = room
                    if target_user and target_user != username:
                        room = dm_room_id(username, target_user)
                        await ensure_dm_membership(room, username)
                        info_dm = ROOMS.get(room)
                        if info_dm:
                            await send(ws, {"type": "room_created", **room_item_for(username, room, info_dm)})

                if is_dm_room(room):
                    await ensure_dm_membership(room, username)

                info = ROOMS.get(room)
                if not info or username not in info["members"]:
                    await send(ws, {"type": "error", "text": "Нет доступа к комнате"})
                    continue

                if not any(mime.startswith(p) for p in ALLOWED_PREFIXES):
                    await send(ws, {"type": "error", "text": "Разрешены только изображения и видео"})
                    continue

                # validate size
                try:
                    declared = int(size) if size is not None else None
                except Exception:
                    declared = None
                decoded_est = _safe_b64_len(data_b64)
                real_size = declared if declared is not None else decoded_est
                if real_size > MAX_BYTES or decoded_est > MAX_BYTES:
                    await send(ws, {"type": "error", "text": "Файл больше 100 МБ"})
                    continue

                # verify base64
                try:
                    base64.b64decode(data_b64, validate=True)
                except Exception:
                    await send(ws, {"type": "error", "text": "Некорректные данные файла"})
                    continue

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
                await broadcast_to_room(room, normalize_record_for_client(rec))
                continue

            if t == "typing":
                room = (obj.get("room") or "").strip()
                info = ROOMS.get(room)
                if info and username in info["members"]:
                    await broadcast_to_room(room, {"type": "typing", "room": room, "from": username})
                continue

            if t == "edit_msg":
                room = (obj.get("room") or "").strip()
                msg_id = obj.get("id")
                new_text = (obj.get("text") or "").strip()

                if not room or msg_id is None:
                    await send(ws, {"type": "error", "text": "Не указан room или id"})
                    continue
                info = ROOMS.get(room)
                if not info or username not in info["members"]:
                    await send(ws, {"type": "error", "text": "Нет доступа к комнате"})
                    continue

                rec = find_message(room, int(msg_id))
                if not rec:
                    await send(ws, {"type": "error", "text": "Сообщение не найдено"})
                    continue
                if rec.get("from") != username:
                    await send(ws, {"type": "error", "text": "Можно редактировать только свои сообщения"})
                    continue
                if rec.get("deleted"):
                    await send(ws, {"type": "error", "text": "Нельзя редактировать удалённое сообщение"})
                    continue
                if rec.get("type") != "text":
                    await send(ws, {"type": "error", "text": "Редактировать можно только текстовые сообщения"})
                    continue

                rec["text"] = new_text
                rec["edited"] = True
                await broadcast_to_room(room, {"type": "edited", "room": room, "id": int(msg_id), "text": new_text, "edited": True})
                continue

            if t == "delete_msg":
                room = (obj.get("room") or "").strip()
                msg_id = obj.get("id")

                if not room or msg_id is None:
                    await send(ws, {"type": "error", "text": "Не указан room или id"})
                    continue
                info = ROOMS.get(room)
                if not info or username not in info["members"]:
                    await send(ws, {"type": "error", "text": "Нет доступа к комнате"})
                    continue

                rec = find_message(room, int(msg_id))
                if not rec:
                    await send(ws, {"type": "error", "text": "Сообщение не найдено"})
                    continue

                if rec.get("from") != username and not is_admin(room, username):
                    await send(ws, {"type": "error", "text": "Можно удалять только свои сообщения (или быть админом беседы)"})
                    continue

                rec["deleted"] = True
                rec["text"] = ""
                rec["name"] = ""
                rec["data"] = ""
                await broadcast_to_room(room, {"type": "deleted", "room": room, "id": int(msg_id)})
                continue

            await send(ws, {"type": "error", "text": "Неизвестный тип сообщения"})

    except websockets.exceptions.ConnectionClosedError:
        pass
    finally:
        u = ONLINE.pop(ws, None)
        if u:
            for info in ROOMS.values():
                info["members"].discard(u)
            if current_room:
                await push_presence(current_room)


async def main(host: str, port: int):
    await init_db()
    # JSON+base64 overhead is large; allow up to ~350MB frames
    async with websockets.serve(handle, host, port, max_size=350 * 1024 * 1024):
        logging.info("WS server on ws://%s:%d", host, port)
        await asyncio.Future()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=int(os.getenv("WS_PORT","8765")))
    args = ap.parse_args()
    try:
        asyncio.run(main(args.host, args.port))
    except KeyboardInterrupt:
        print("\nStopping...")
