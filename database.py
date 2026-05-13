"""
database.py — PostgreSQL setup with SQLAlchemy + psycopg3
"""
import os
from sqlalchemy import (
    create_engine, Column, String, Text, Integer,
    Boolean, DateTime, JSON
)
from sqlalchemy.orm import declarative_base, sessionmaker
from datetime import datetime, timezone

DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql://user:pass@localhost/movies")

# Fix Render's postgres:// prefix + switch to psycopg3 driver
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql+psycopg://", 1)
elif DATABASE_URL.startswith("postgresql://"):
    DATABASE_URL = DATABASE_URL.replace("postgresql://", "postgresql+psycopg://", 1)

engine       = create_engine(DATABASE_URL, pool_pre_ping=True, pool_size=5)
SessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False)
Base         = declarative_base()


def now():
    return datetime.now(timezone.utc)


class Movie(Base):
    __tablename__ = "movies"

    id             = Column(Integer, primary_key=True, index=True)
    title          = Column(String(500), nullable=False, index=True)
    year           = Column(String(10), default="")
    genre          = Column(String(300), default="")
    category       = Column(String(100), default="", index=True)
    description    = Column(Text, default="")
    filesize       = Column(String(50), default="")
    poster         = Column(Text, default="")
    page_url       = Column(Text, unique=True, nullable=False)
    source_site    = Column(String(50), default="")
    download_links = Column(JSON, default=list)
    link_checked_at= Column(DateTime(timezone=True), nullable=True)
    is_active      = Column(Boolean, default=True)
    created_at     = Column(DateTime(timezone=True), default=now)
    updated_at     = Column(DateTime(timezone=True), default=now, onupdate=now)


class Series(Base):
    __tablename__ = "series"

    id          = Column(Integer, primary_key=True, index=True)
    title       = Column(String(500), nullable=False, index=True)
    year        = Column(String(10), default="")
    genre       = Column(String(300), default="")
    category    = Column(String(100), default="", index=True)
    description = Column(Text, default="")
    poster      = Column(Text, default="")
    page_url    = Column(Text, unique=True, nullable=False)
    source_site = Column(String(50), default="")
    seasons     = Column(JSON, default=list)
    is_active   = Column(Boolean, default=True)
    created_at  = Column(DateTime(timezone=True), default=now)
    updated_at  = Column(DateTime(timezone=True), default=now, onupdate=now)


def init_db():
    Base.metadata.create_all(bind=engine)
    print("[db] Tables created/verified")


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
