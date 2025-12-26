from __future__ import annotations

import os
from datetime import datetime

from sqlalchemy import (
    Column,
    Integer,
    String,
    Text,
    Boolean,
    DateTime,
    ForeignKey,
    UniqueConstraint,
)
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from sqlalchemy.orm import declarative_base, relationship
from sqlalchemy.sql import func

Base = declarative_base()


def _normalize_database_url(url: str | None) -> str:
    """
    Render часто отдаёт URL вида:
      postgresql://user:pass@host/db
    А SQLAlchemy async требует:
      postgresql+asyncpg://user:pass@host/db

    Также иногда встречается postgres://
    """
    if not url:
        return "postgresql+asyncpg://chat:chat@localhost:5432/chatdb"

    url = url.strip()

    # Render может дать postgres://
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://") :]

    # Приводим к asyncpg
    if url.startswith("postgresql+asyncpg://"):
        return url

    if url.startswith("postgresql://"):
        return "postgresql+asyncpg://" + url[len("postgresql://") :]

    # Если вдруг уже другой драйвер — оставим как есть
    return url


DATABASE_URL = _normalize_database_url(os.getenv("DATABASE_URL"))

# Можно выключить шумные SQL-логи на Render:
SQL_ECHO = os.getenv("SQL_ECHO", "0") == "1"

engine = create_async_engine(DATABASE_URL, echo=SQL_ECHO, future=True)
AsyncSessionLocal = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True)
    username = Column(String(64), unique=True, nullable=False)
    password = Column(String(128), nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class Room(Base):
    __tablename__ = "rooms"
    id = Column(Integer, primary_key=True)
    name = Column(String(255), nullable=False)
    is_direct = Column(Boolean, default=False, nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class RoomMember(Base):
    __tablename__ = "room_members"
    id = Column(Integer, primary_key=True)
    room_id = Column(Integer, ForeignKey("rooms.id", ondelete="CASCADE"), nullable=False)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    role = Column(String(32), default="member", nullable=False)  # member/admin
    joined_at = Column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (UniqueConstraint("room_id", "user_id", name="uq_room_user"),)


class Message(Base):
    __tablename__ = "messages"
    id = Column(Integer, primary_key=True)
    room_id = Column(Integer, ForeignKey("rooms.id", ondelete="CASCADE"), nullable=False)
    sender_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)

    content = Column(Text, nullable=True)
    media_type = Column(String(32), nullable=True)   # image/video/file/link
    media_url = Column(Text, nullable=True)          # если храните ссылки/пути
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    edited_at = Column(DateTime(timezone=True), nullable=True)
    is_deleted = Column(Boolean, default=False, nullable=False)


class MessageRead(Base):
    __tablename__ = "message_reads"
    id = Column(Integer, primary_key=True)
    message_id = Column(Integer, ForeignKey("messages.id", ondelete="CASCADE"), nullable=False)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    read_at = Column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (UniqueConstraint("message_id", "user_id", name="uq_msg_user_read"),)


async def init_db() -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
