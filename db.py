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
from sqlalchemy.ext.asyncio import (
    create_async_engine,
    async_sessionmaker,
    AsyncSession,
)
from sqlalchemy.orm import declarative_base, relationship
from sqlalchemy.sql import func

# Строка подключения к PostgreSQL
# Можно переопределить через переменную окружения DATABASE_URL
RAW_DATABASE_URL = os.getenv("DATABASE_URL", "postgresql+asyncpg://chat:chat@localhost:5432/chatdb")
# Render и многие хостинги выдают URL в формате postgres:// или postgresql://
if RAW_DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = "postgresql+asyncpg://" + RAW_DATABASE_URL[len("postgres://"):]
elif RAW_DATABASE_URL.startswith("postgresql://") and not RAW_DATABASE_URL.startswith("postgresql+asyncpg://"):
    DATABASE_URL = "postgresql+asyncpg://" + RAW_DATABASE_URL[len("postgresql://"):]
else:
    DATABASE_URL = RAW_DATABASE_URL

# В продакшене SQL echo лучше выключать; при необходимости включите SQL_ECHO=1
SQL_ECHO = os.getenv("SQL_ECHO", "").strip() not in ("", "0", "false", "False")
engine = create_async_engine(DATABASE_URL, echo=SQL_ECHO, future=True)
AsyncSessionLocal = async_sessionmaker(engine, expire_on_commit=False)

Base = declarative_base()


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True)
    username = Column(String(50), unique=True, nullable=False, index=True)
    password_hash = Column(String(255), nullable=False)

    # Профиль
    full_name = Column(String(100), nullable=True)
    position = Column(String(100), nullable=True)
    department = Column(String(100), nullable=True)
    avatar_url = Column(String(255), nullable=True)
    about = Column(Text, nullable=True)

    created_at = Column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    rooms = relationship("RoomMember", back_populates="user")


class Room(Base):
    __tablename__ = "rooms"

    id = Column(Integer, primary_key=True)
    name = Column(String(100), nullable=False)
    # False – обычный групповой чат, True – личный диалог 1:1
    is_direct = Column(Boolean, default=False, nullable=False)

    created_at = Column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    members = relationship("RoomMember", back_populates="room")
    messages = relationship("Message", back_populates="room")


class RoomMember(Base):
    __tablename__ = "room_members"
    __table_args__ = (
        UniqueConstraint("room_id", "user_id", name="uq_room_member"),
    )

    id = Column(Integer, primary_key=True)
    room_id = Column(Integer, ForeignKey("rooms.id"), nullable=False)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)

    joined_at = Column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    room = relationship("Room", back_populates="members")
    user = relationship("User", back_populates="rooms")


class Message(Base):
    __tablename__ = "messages"

    id = Column(Integer, primary_key=True)
    room_id = Column(Integer, ForeignKey("rooms.id"), nullable=False, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    text = Column(Text, nullable=False)
    created_at = Column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    room = relationship("Room", back_populates="messages")
    user = relationship("User")
    reads = relationship("MessageRead", back_populates="message")


class MessageRead(Base):
    __tablename__ = "message_reads"
    __table_args__ = (
        UniqueConstraint("message_id", "user_id", name="uq_message_read"),
    )

    id = Column(Integer, primary_key=True)
    message_id = Column(Integer, ForeignKey("messages.id"), nullable=False)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    read_at = Column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    message = relationship("Message", back_populates="reads")
    user = relationship("User")


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

    # Дополнительные поля для лабораторного интерфейса / статуса результатов
    category = Column(String(100), nullable=True)  # тип анализа / категория
    status = Column(String(50), nullable=False, server_default='pending')
    result_url = Column(String(255), nullable=True)
    completed_at = Column(DateTime(timezone=True), nullable=True)

    created_by_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    created_by = relationship("User")


async def init_db() -> None:
    """Создаёт таблицы, если их ещё нет."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def get_session() -> AsyncSession:
    return AsyncSessionLocal()
