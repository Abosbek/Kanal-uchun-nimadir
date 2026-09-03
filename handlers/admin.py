"""
handlers/admin.py
Admin uchun barcha buyruqlar, FSM holatlari, inline tugmalar mantiqi va
kanalga chop etish / imzo qo'shish logikasi.
"""

import logging
import os
import re
import tempfile
from typing import Optional

from aiogram import Router, F, Bot
from aiogram.filters import Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    FSInputFile,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder

import ai_service
import image_service
from database import Database, ImageSourceType

logger = logging.getLogger(__name__)

router = Router(name="admin_router")

# ---------------------------------------------------------------------------
# Sozlamalar
# ---------------------------------------------------------------------------

ADMIN_IDS = {int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip().isdigit()}
CHANNEL_ID = os.getenv("CHANNEL_ID", "")
CHANNEL_USERNAME = os.getenv("CHANNEL_USERNAME", "")
CHANNEL_FOOTER = os.getenv(
    "CHANNEL_FOOTER",
    "✅ Admin tomondan ko'rib chiqildi va tasdiqlandi\n📢 Bosh sahifa: @kanal_username",
)

URL_REGEX = re.compile(r"https?://\S+")


def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


# ---------------------------------------------------------------------------
# FSM holatlari
# ---------------------------------------------------------------------------

class EditPost(StatesGroup):
    waiting_for_new_text = State()


# ---------------------------------------------------------------------------
# Yordamchi: klaviaturalar
# ---------------------------------------------------------------------------

def build_image_choice_keyboard(post_id: int) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="🖼 AI Rasm chizish", callback_data=f"imgchoice:ai:{post_id}")
    builder.button(text="🔍 Haqiqiy rasm topish", callback_data=f"imgchoice:real:{post_id}")
    builder.button(text="⏭ Rasmsiz davom etish", callback_data=f"imgchoice:skip:{post_id}")
    builder.adjust(1)
    return builder.as_markup()


def build_moderation_keyboard(post_id: int) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="✅ Kanalga e'lon qilish", callback_data=f"post:publish:{post_id}")
    builder.button(text="🔄 Qayta yozish", callback_data=f"post:rewrite:{post_id}")
    builder.button(text="🖼 AI Rasm", callback_data=f"post:ai_image:{post_id}")
    builder.button(text="🔍 Google Rasm", callback_data=f"post:real_image:{post_id}")
    builder.button(text="✏️ Tahrirlash", callback_data=f"post:edit:{post_id}")
    builder.button(text="🗑 O'chirish", callback_data=f"post:delete:{post_id}")
    builder.adjust(2, 2, 2)
    return builder.as_markup()


# ---------------------------------------------------------------------------
# /start va yordam
# ---------------------------------------------------------------------------

@router.message(Command("start"))
async def cmd_start(message: Message):
    if not is_admin(message.from_user.id):
        await message.answer("⛔️ Bu bot faqat kanal adminlari uchun mo'ljallangan.")
        return

    text = (
        "👋 <b>Telegram Channel Manager AI</b> botiga xush kelibsiz!\n\n"
        "Quyidagilardan birini yuboring:\n"
        "• 🔗 Havola (link) — maqolani o'qib, post tayyorlayman\n"
        "• 📦 .apk fayl — ilova haqida post tayyorlayman\n"
        "• <code>/post Mavzu nomi</code> — ixtiyoriy mavzu bo'yicha post\n"
        "• <code>/rss</code> — Product Hunt/Reddit'dan yangi loyihalar\n\n"
        "Har bir post avval sizga yuboriladi va siz uni tasdiqlaysiz ✅"
    )
    await message.answer(text, parse_mode="HTML")


# ---------------------------------------------------------------------------
# Umumiy: postni admin chatiga (draft sifatida) yuborish oqimi
# ---------------------------------------------------------------------------

async def _start_post_flow(
    message: Message,
    db: Database,
    content: str,
    source_type: str,
    source_ref: Optional[str] = None,
):
    """Generatsiya qilingan matnni bazaga yozib, rasm tanlash klaviaturasini yuboradi."""
    try:
        post = await db.create_post(content=content, source_type=source_type, source_ref=source_ref)
    except Exception as e:
        await message.answer(f"❌ Bazaga yozishda xatolik: {e}")
        return

    sent = await message.answer(
        f"📝 <b>Qoralama tayyor:</b>\n\n{content}\n\n"
        f"Rasm bilan bog'liq variantni tanlang:",
        parse_mode="HTML",
        reply_markup=build_image_choice_keyboard(post.id),
    )
    await db.set_admin_message(post.id, sent.chat.id, sent.message_id)


# ---------------------------------------------------------------------------
# 1) Mavzu asosida post: /post <mavzu>
# ---------------------------------------------------------------------------

@router.message(Command("post"))
async def cmd_post_topic(message: Message, command: CommandObject, db: Database):
    if not is_admin(message.from_user.id):
        return

    topic = (command.args or "").strip()
    if not topic:
        await message.answer("✏️ Iltimos, mavzuni ham yozing. Masalan:\n<code>/post Bugungi IT yangiliklari</code>", parse_mode="HTML")
        return

    status_msg = await message.answer("🤖 AI post tayyorlamoqda, kuting...")
    try:
        content = await ai_service.generate_post_from_topic(topic)
    except Exception as e:
        await status_msg.edit_text(f"❌ Post yaratishda xatolik: {e}")
        return

    await status_msg.delete()
    await _start_post_flow(message, db, content, source_type="topic", source_ref=topic)


# ---------------------------------------------------------------------------
# 2) Link yuborilganda avtomatik tahlil
# ---------------------------------------------------------------------------

@router.message(F.text.regexp(URL_REGEX.pattern))
async def handle_link_message(message: Message, db: Database):
    if not is_admin(message.from_user.id):
        return

    match = URL_REGEX.search(message.text)
    if not match:
        return
    url = match.group(0)

    status_msg = await message.answer("🔗 Havola o'qilmoqda va qayta yozilmoqda...")
    try:
        content = await ai_service.fetch_and_summarize_link(url)
    except Exception as e:
        await status_msg.edit_text(f"❌ Havolani qayta ishlashda xatolik: {e}")
        return

    await status_msg.delete()
    await _start_post_flow(message, db, content, source_type="link", source_ref=url)


# ---------------------------------------------------------------------------
# 3) .apk fayl yuborilganda
# ---------------------------------------------------------------------------

@router.message(F.document.file_name.endswith(".apk"))
async def handle_apk_document(message: Message, db: Database, bot: Bot):
    if not is_admin(message.from_user.id):
        return

    status_msg = await message.answer("📦 APK fayl tahlil qilinmoqda...")

    tmp_path = None
    try:
        file_info = await bot.get_file(message.document.file_id)
        with tempfile.NamedTemporaryFile(delete=False, suffix=".apk") as tmp:
            tmp_path = tmp.name
        await bot.download_file(file_info.file_path, destination=tmp_path)

        apk_info = ai_service.extract_apk_info(tmp_path)
        caption = message.caption
        content = await ai_service.generate_post_from_apk(apk_info, caption=caption)
    except Exception as e:
        await status_msg.edit_text(f"❌ APK faylni qayta ishlashda xatolik: {e}")
        return
    finally:
        if tmp_path and os.path.exists(tmp_path):
            os.remove(tmp_path)

    await status_msg.delete()
    await _start_post_flow(message, db, content, source_type="apk", source_ref=message.document.file_name)


# ---------------------------------------------------------------------------
# 4) RSS orqali yangi loyihalarni topish
# ---------------------------------------------------------------------------

@router.message(Command("rss"))
async def cmd_rss(message: Message, db: Database):
    if not is_admin(message.from_user.id):
        return

    status_msg = await message.answer("📡 RSS manbalar tekshirilmoqda...")

    feed_url = os.getenv("PRODUCTHUNT_RSS", "https://www.producthunt.com/feed")
    items = await ai_service.fetch_rss_items(feed_url, limit=3)

    if not items:
        await status_msg.edit_text("⚠️ Hozircha yangi loyihalar topilmadi yoki RSS o'qib bo'lmadi.")
        return

    await status_msg.delete()
    for item in items:
        try:
            content = await ai_service.generate_post_from_rss_item(item)
        except Exception as e:
            logger.exception("RSS elementidan post yaratishda xatolik: %s", e)
            continue
        await _start_post_flow(message, db, content, source_type="rss", source_ref=item.link)


# ---------------------------------------------------------------------------
# Rasm tanlash bosqichi (imgchoice:*)
# ---------------------------------------------------------------------------

@router.callback_query(F.data.startswith("imgchoice:"))
async def handle_image_choice(callback: CallbackQuery, db: Database):
    _, choice, post_id_str = callback.data.split(":")
    post_id = int(post_id_str)

    post = await db.get_post(post_id)
    if not post:
        await callback.answer("⚠️ Post topilmadi (o'chirilgan bo'lishi mumkin).", show_alert=True)
        return

    await callback.answer()

    if choice == "skip":
        await _render_moderation_panel(callback.message, db, post_id)
        return

    prompt_seed = post.source_ref or post.content[:200]

    await callback.message.edit_text(
        f"{post.content}\n\n⏳ Rasm tayyorlanmoqda, biroz kuting...",
    )

    try:
        if choice == "ai":
            image_bytes = await image_service.generate_ai_image(prompt_seed)
            image_source = ImageSourceType.AI_GENERATED
        elif choice == "real":
            image_url = await image_service.search_real_image(prompt_seed)
            if not image_url:
                raise RuntimeError("Mos rasm topilmadi.")
            image_bytes = await image_service.download_image_bytes(image_url)
            image_source = ImageSourceType.REAL_SEARCH
        else:
            await _render_moderation_panel(callback.message, db, post_id)
            return

        watermark_text = CHANNEL_USERNAME or "@channel"
        final_bytes = image_service.add_text_watermark(image_bytes, watermark_text)

        tmp_path = tempfile.NamedTemporaryFile(delete=False, suffix=".jpg").name
        with open(tmp_path, "wb") as f:
            f.write(final_bytes)

        await db.update_post_image(post_id, image_url=tmp_path, image_source=image_source)
        await _render_moderation_panel(callback.message, db, post_id, photo_path=tmp_path)

    except Exception as e:
        logger.exception("Rasm tayyorlashda xatolik: %s", e)
        await callback.message.edit_text(
            f"{post.content}\n\n⚠️ Rasm tayyorlashda xatolik yuz berdi: {e}\nRasmsiz davom etamiz.",
        )
        await _render_moderation_panel(callback.message, db, post_id)


async def _render_moderation_panel(
    message: Message, db: Database, post_id: int, photo_path: Optional[str] = None
):
    post = await db.get_post(post_id)
    if not post:
        return

    caption = f"📝 <b>Moderatsiya:</b>\n\n{post.content}"
    keyboard = build_moderation_keyboard(post_id)

    photo_to_use = photo_path or (post.image_url if post.image_url and os.path.exists(post.image_url) else None)

    try:
        if photo_to_use:
            await message.answer_photo(
                FSInputFile(photo_to_use),
                caption=caption,
                parse_mode="HTML",
                reply_markup=keyboard,
            )
        else:
            await message.answer(caption, parse_mode="HTML", reply_markup=keyboard)
    except Exception as e:
        logger.exception("Moderatsiya panelini ko'rsatishda xatolik: %s", e)
        await message.answer(caption, parse_mode="HTML", reply_markup=keyboard)


# ---------------------------------------------------------------------------
# Moderatsiya tugmalari (post:*)
# ---------------------------------------------------------------------------

@router.callback_query(F.data.startswith("post:"))
async def handle_moderation_action(callback: CallbackQuery, db: Database, bot: Bot, state: FSMContext):
    _, action, post_id_str = callback.data.split(":")
    post_id = int(post_id_str)

    post = await db.get_post(post_id)
    if not post:
        await callback.answer("⚠️ Post topilmadi.", show_alert=True)
        return

    if action == "publish":
        await callback.answer("📢 Kanalga chop etilmoqda...")
        await _publish_post(callback, db, bot, post_id)

    elif action == "rewrite":
        await callback.answer("🔄 Qayta yozilmoqda...")
        try:
            new_content = await ai_service.rewrite_post(post.content)
            await db.update_post_content(post_id, new_content)
        except Exception as e:
            await callback.message.answer(f"❌ Qayta yozishda xatolik: {e}")
            return
        await _refresh_moderation_message(callback, db, post_id)

    elif action == "ai_image":
        await callback.answer("🖼 AI rasm tayyorlanmoqda...")
        await _regenerate_image(callback, db, post_id, mode="ai")

    elif action == "real_image":
        await callback.answer("🔍 Haqiqiy rasm qidirilmoqda...")
        await _regenerate_image(callback, db, post_id, mode="real")

    elif action == "edit":
        await callback.answer()
        await state.update_data(edit_post_id=post_id)
        await state.set_state(EditPost.waiting_for_new_text)
        await callback.message.answer(
            "✏️ Yangi matnni yuboring. Bekor qilish uchun /cancel yozing."
        )

    elif action == "delete":
        await callback.answer("🗑 O'chirilmoqda...")
        await db.mark_deleted(post_id)
        try:
            await callback.message.delete()
        except Exception:
            pass
        await callback.message.answer("🗑 Qoralama o'chirildi.")

    else:
        await callback.answer("Noma'lum amal.")


async def _regenerate_image(callback: CallbackQuery, db: Database, post_id: int, mode: str):
    post = await db.get_post(post_id)
    if not post:
        return
    prompt_seed = post.source_ref or post.content[:200]

    try:
        if mode == "ai":
            image_bytes = await image_service.generate_ai_image(prompt_seed)
            image_source = ImageSourceType.AI_GENERATED
        else:
            image_url = await image_service.search_real_image(prompt_seed)
            if not image_url:
                raise RuntimeError("Mos rasm topilmadi.")
            image_bytes = await image_service.download_image_bytes(image_url)
            image_source = ImageSourceType.REAL_SEARCH

        watermark_text = CHANNEL_USERNAME or "@channel"
        final_bytes = image_service.add_text_watermark(image_bytes, watermark_text)

        tmp_path = tempfile.NamedTemporaryFile(delete=False, suffix=".jpg").name
        with open(tmp_path, "wb") as f:
            f.write(final_bytes)

        await db.update_post_image(post_id, image_url=tmp_path, image_source=image_source)
    except Exception as e:
        logger.exception("Rasmni qayta generatsiya qilishda xatolik: %s", e)
        await callback.message.answer(f"❌ Rasm tayyorlashda xatolik: {e}")
        return

    await _refresh_moderation_message(callback, db, post_id)


async def _refresh_moderation_message(callback: CallbackQuery, db: Database, post_id: int):
    """Eski moderatsiya xabarini o'chirib, yangilangan holatini qayta chizadi."""
    try:
        await callback.message.delete()
    except Exception:
        pass
    await _render_moderation_panel(callback.message, db, post_id)


# ---------------------------------------------------------------------------
# Qo'lda tahrirlash (FSM)
# ---------------------------------------------------------------------------

@router.message(Command("cancel"), EditPost.waiting_for_new_text)
async def cancel_edit(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("❌ Tahrirlash bekor qilindi.")


@router.message(EditPost.waiting_for_new_text)
async def process_edit_text(message: Message, state: FSMContext, db: Database):
    data = await state.get_data()
    post_id = data.get("edit_post_id")
    await state.clear()

    if not post_id:
        await message.answer("⚠️ Post ID topilmadi, qaytadan urinib ko'ring.")
        return

    try:
        await db.update_post_content(post_id, message.text)
    except Exception as e:
        await message.answer(f"❌ Matnni yangilashda xatolik: {e}")
        return

    await message.answer("✅ Matn yangilandi.")
    await _render_moderation_panel(message, db, post_id)


# ---------------------------------------------------------------------------
# Kanalga chop etish (imzo/footer bilan)
# ---------------------------------------------------------------------------

async def _publish_post(callback: CallbackQuery, db: Database, bot: Bot, post_id: int):
    post = await db.get_post(post_id)
    if not post:
        await callback.message.answer("⚠️ Post topilmadi.")
        return

    if not CHANNEL_ID:
        await callback.message.answer("❌ CHANNEL_ID .env faylida sozlanmagan.")
        return

    final_text = f"{post.content}\n\n-------------------\n{CHANNEL_FOOTER}"

    try:
        if post.image_url and os.path.exists(post.image_url):
            sent = await bot.send_photo(
                chat_id=CHANNEL_ID,
                photo=FSInputFile(post.image_url),
                caption=final_text,
            )
        else:
            sent = await bot.send_message(chat_id=CHANNEL_ID, text=final_text)

        await db.mark_published(post_id, channel_message_id=sent.message_id)

        try:
            await callback.message.delete()
        except Exception:
            pass
        await callback.message.answer("✅ Post muvaffaqiyatli kanalga chop etildi!")

    except Exception as e:
        logger.exception("Kanalga chop etishda xatolik: %s", e)
        await callback.message.answer(f"❌ Kanalga chop etishda xatolik: {e}")
