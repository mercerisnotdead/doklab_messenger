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
    Render часто отдаёт DATABASE_URL в формате:
      postgres://...  или  postgresql://...
    Для SQLAlchemy async нужен драйвер asyncpg:
      postgresql+asyncpg://...

    Если переменная не задана, используем локальный дефолт (для разработки).
    """
    if not url:
        return "postgresql+asyncpg://chat:chat@127.0.0.1:5432/chatdb"

    url = url.strip()

    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://") :]

    if url.startswith("postgresql+asyncpg://"):
        return url

    if url.startswith("postgresql://"):
        return "postgresql+asyncpg://" + url[len("postgresql://") :]

    return url


DATABASE_URL = _normalize_database_url(os.getenv("DATABASE_URL"))
SQL_ECHO = os.getenv("SQL_ECHO", "0") == "1"

engine = create_async_engine(DATABASE_URL, echo=SQL_ECHO, future=True)
AsyncSessionLocal = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True)
    username = Column(String(64), unique=True, nullable=False)
    password = Column(String(128), nullable=False)
    avatar = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class Room(Base):
    __tablename__ = "rooms"

    id = Column(Integer, primary_key=True)
    name = Column(String(255), nullable=False)
    is_direct = Column(Boolean, default=False, nullable=False)
    avatar = Column(Text, nullable=True)
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
    media_type = Column(String(32), nullable=True)  # image/video/file/link
    media_url = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    edited_at = Column(DateTime(timezone=True), nullable=True)
    is_deleted = Column(Boolean, default=False, nullable=False)

    sender = relationship("User", lazy="joined")


class MessageRead(Base):
    __tablename__ = "message_reads"

    id = Column(Integer, primary_key=True)
    message_id = Column(Integer, ForeignKey("messages.id", ondelete="CASCADE"), nullable=False)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    read_at = Column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (UniqueConstraint("message_id", "user_id", name="uq_msg_user_read"),)


class Appointment(Base):
    """
    Запись в календаре – приём пациента.
    """
    __tablename__ = "appointments"

    id = Column(Integer, primary_key=True)
    patient_name = Column(String(100), nullable=False)
    policy_number = Column(String(50), nullable=False)
    doctor = Column(String(100), nullable=True)
    room = Column(String(50), nullable=True)

    start_time = Column(DateTime(timezone=True), nullable=False)
    end_time = Column(DateTime(timezone=True), nullable=False)

    category = Column(String(100), nullable=True)
    status = Column(String(50), nullable=False, server_default="pending")
    result_url = Column(String(255), nullable=True)
    completed_at = Column(DateTime(timezone=True), nullable=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now())
    created_by_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    created_by = relationship("User")


async def init_db() -> None:
    """
    Создаёт таблицы, если их ещё нет.
    ВАЖНО: 'Общий чат' больше не создаём автоматически (по вашему требованию).
    """
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
