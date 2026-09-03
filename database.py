"""
database.py
Supabase (PostgreSQL) yoki lokal SQLite bilan ishlash uchun SQLAlchemy async qatlami.

- Production: DATABASE_URL = postgresql+asyncpg://...   (Supabase)
- Lokal test:  DATABASE_URL = sqlite+aiosqlite:///./bot_local.db
"""

import enum
import logging
from datetime import datetime
from typing import Optional, AsyncGenerator

from sqlalchemy import String, Text, Integer, DateTime, Enum as SAEnum, func
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
    AsyncEngine,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

logger = logging.getLogger(__name__)


class Base(DeclarativeBase):
    pass


class PostStatus(str, enum.Enum):
    DRAFT = "draft"          # Admin chatida ko'rib chiqilmoqda
    PUBLISHED = "published"  # Kanalga chop etilgan
    DELETED = "deleted"      # Admin tomonidan bekor qilingan


class ImageSourceType(str, enum.Enum):
    NONE = "none"
    AI_GENERATED = "ai_generated"   # Pollinations.ai
    REAL_SEARCH = "real_search"     # DuckDuckGo/Google Image Search


class Post(Base):
    """Har bir tayyorlanayotgan yoki chop etilgan post uchun jadval."""

    __tablename__ = "posts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    # Postning asosiy matni (Gemini tomonidan generatsiya qilingan / tahrirlangan)
    content: Mapped[str] = mapped_column(Text, nullable=False)

    # Post qanday manbadan kelgan: link, apk, topic, rss
    source_type: Mapped[str] = mapped_column(String(32), default="topic")
    source_ref: Mapped[Optional[str]] = mapped_column(Text, nullable=True)  # original link/nomi

    # Rasm bilan bog'liq maydonlar
    image_url: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    image_source: Mapped[str] = mapped_column(
        SAEnum(ImageSourceType, native_enum=False), default=ImageSourceType.NONE
    )

    # Holat va admin bog'liqligi
    status: Mapped[str] = mapped_column(
        SAEnum(PostStatus, native_enum=False), default=PostStatus.DRAFT
    )
    admin_chat_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    admin_message_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)

    # Kanalga chop etilgandan keyingi xabar ID (kerak bo'lsa tahrirlash uchun)
    channel_message_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    def __repr__(self) -> str:
        return f"<Post id={self.id} status={self.status} source={self.source_type}>"


class Database:
    """Engine va session'larni boshqaruvchi yordamchi klass."""

    def __init__(self, database_url: str):
        self.database_url = database_url
        self.engine: AsyncEngine = create_async_engine(
            database_url,
            echo=False,
            pool_pre_ping=True,
            future=True,
        )
        self.session_factory = async_sessionmaker(
            bind=self.engine,
            expire_on_commit=False,
            class_=AsyncSession,
        )

    async def init_models(self) -> None:
        """Jadvallarni yaratish (agar mavjud bo'lmasa)."""
        try:
            async with self.engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            logger.info("Ma'lumotlar bazasi jadvallari muvaffaqiyatli tayyorlandi.")
        except Exception as e:
            logger.exception("Bazani initsializatsiya qilishda xatolik: %s", e)
            raise

    async def get_session(self) -> AsyncGenerator[AsyncSession, None]:
        async with self.session_factory() as session:
            yield session

    async def close(self) -> None:
        await self.engine.dispose()

    # ---------- Yuqori darajadagi yordamchi metodlar (CRUD) ----------

    async def create_post(
        self,
        content: str,
        source_type: str = "topic",
        source_ref: Optional[str] = None,
        image_url: Optional[str] = None,
        image_source: ImageSourceType = ImageSourceType.NONE,
    ) -> Post:
        async with self.session_factory() as session:
            post = Post(
                content=content,
                source_type=source_type,
                source_ref=source_ref,
                image_url=image_url,
                image_source=image_source,
                status=PostStatus.DRAFT,
            )
            session.add(post)
            try:
                await session.commit()
                await session.refresh(post)
            except Exception as e:
                await session.rollback()
                logger.exception("Post yaratishda xatolik: %s", e)
                raise
            return post

    async def get_post(self, post_id: int) -> Optional[Post]:
        async with self.session_factory() as session:
            return await session.get(Post, post_id)

    async def update_post_content(self, post_id: int, content: str) -> Optional[Post]:
        async with self.session_factory() as session:
            post = await session.get(Post, post_id)
            if not post:
                return None
            post.content = content
            try:
                await session.commit()
                await session.refresh(post)
            except Exception as e:
                await session.rollback()
                logger.exception("Post matnini yangilashda xatolik: %s", e)
                raise
            return post

    async def update_post_image(
        self, post_id: int, image_url: str, image_source: ImageSourceType
    ) -> Optional[Post]:
        async with self.session_factory() as session:
            post = await session.get(Post, post_id)
            if not post:
                return None
            post.image_url = image_url
            post.image_source = image_source
            try:
                await session.commit()
                await session.refresh(post)
            except Exception as e:
                await session.rollback()
                logger.exception("Post rasmini yangilashda xatolik: %s", e)
                raise
            return post

    async def set_admin_message(self, post_id: int, chat_id: int, message_id: int) -> None:
        async with self.session_factory() as session:
            post = await session.get(Post, post_id)
            if not post:
                return
            post.admin_chat_id = chat_id
            post.admin_message_id = message_id
            try:
                await session.commit()
            except Exception as e:
                await session.rollback()
                logger.exception("Admin xabar ID saqlashda xatolik: %s", e)
                raise

    async def mark_published(self, post_id: int, channel_message_id: int) -> None:
        async with self.session_factory() as session:
            post = await session.get(Post, post_id)
            if not post:
                return
            post.status = PostStatus.PUBLISHED
            post.channel_message_id = channel_message_id
            try:
                await session.commit()
            except Exception as e:
                await session.rollback()
                logger.exception("Postni 'published' deb belgilashda xatolik: %s", e)
                raise

    async def mark_deleted(self, post_id: int) -> None:
        async with self.session_factory() as session:
            post = await session.get(Post, post_id)
            if not post:
                return
            post.status = PostStatus.DELETED
            try:
                await session.commit()
            except Exception as e:
                await session.rollback()
                logger.exception("Postni o'chirishda xatolik: %s", e)
                raise
