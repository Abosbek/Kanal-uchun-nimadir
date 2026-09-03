"""
handlers/admin.py
Admin uchun barcha buyruqlar, FSM holatlari, inline tugmalar mantiqi va
kanalga chop etish / imzo qo'shish logikasi.
"""

import logging
import os
import re
import tempfile
from datetime import datetime, timedelta, timezone
from html import escape as html_escape
from typing import Optional

from aiogram import Router, F, Bot
from aiogram.filters import Command, CommandObject, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    FSInputFile,
    InputMediaPhoto,
    InputRichMessage,
    InputRichMessageMedia,
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

# Admin qaysi vaqt zonasida yashashini ko'rsatadi (rejalashtirish uchun).
# Toshkent vaqti standart bo'yicha UTC+5.
ADMIN_TZ_OFFSET_HOURS = int(os.getenv("ADMIN_TIMEZONE_OFFSET_HOURS", "5"))


def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


def format_for_telegram_html(text: str) -> str:
    """
    AI tomonidan yaratilgan matnni Telegram HTML rejimi uchun xavfsiz formatlaydi.
    - Avval xavfli HTML belgilarini escape qiladi (<, >, &).
    - Keyin Markdown uslubidagi **qalin** belgilarni <b>qalin</b> ga aylantiradi.
    - Qolgan yakka yulduzcha/pastki chiziqlarni (agar AI baribir qoldirib ketsa) tozalaydi.
    """
    escaped = html_escape(text, quote=False)
    # **qalin matn** -> <b>qalin matn</b>
    bolded = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", escaped, flags=re.DOTALL)
    # Qolgan yakka yulduzcha yoki pastki chiziqlarni olib tashlaymiz
    cleaned = bolded.replace("**", "").replace("__", "")
    return cleaned


# ---------------------------------------------------------------------------
# FSM holatlari
# ---------------------------------------------------------------------------

class EditPost(StatesGroup):
    waiting_for_new_text = State()


class SchedulePost(StatesGroup):
    waiting_for_datetime = State()


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
    builder.button(text="🕒 Rejalashtirish", callback_data=f"post:schedule:{post_id}")
    builder.button(text="📰 Maqola sifatida", callback_data=f"post:richpost:{post_id}")
    builder.button(text="🔄 Qayta yozish", callback_data=f"post:rewrite:{post_id}")
    builder.button(text="🖼 AI Rasm", callback_data=f"post:ai_image:{post_id}")
    builder.button(text="🔍 Google Rasm", callback_data=f"post:real_image:{post_id}")
    builder.button(text="✏️ Tahrirlash", callback_data=f"post:edit:{post_id}")
    builder.button(text="🗑 O'chirish", callback_data=f"post:delete:{post_id}")
    builder.adjust(2, 1, 2, 2, 1)
    return builder.as_markup()


def build_rich_markdown(content: str) -> str:
    """
    Oddiy post matnini Telegram Rich Message (Bot API 10.1+) uchun
    "maqola"ga o'xshash strukturaga aylantiradi: sarlavha + paragraflar + ajratgich.
    """
    lines = [line.strip() for line in content.split("\n") if line.strip()]
    if not lines:
        return content

    heading = lines[0]
    body_lines = lines[1:]
    body = "\n\n".join(body_lines) if body_lines else ""

    md = f"## {heading}"
    if body:
        md += f"\n\n{body}"
    return md


# ---------------------------------------------------------------------------
# /start va yordam
# ---------------------------------------------------------------------------

@router.message(Command("start"))
async def cmd_start(message: Message):
    if not is_admin(message.from_user.id):
        await message.answer("⛔️ Bu bot faqat kanal adminlari uchun mo'ljallangan.")
        return

    text = (
        "👋 <b>Telegram Channel Manager AI</b> botiga xush kelibsiz\n\n"
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
    attachment_file_id: Optional[str] = None,
):
    """Generatsiya qilingan matnni bazaga yozib, rasm tanlash klaviaturasini yuboradi."""
    try:
        post = await db.create_post(
            content=content,
            source_type=source_type,
            source_ref=source_ref,
            attachment_file_id=attachment_file_id,
        )
    except Exception as e:
        await message.answer(f"❌ Bazaga yozishda xatolik: {e}")
        return

    # Agar .apk (yoki boshqa hujjat) biriktirilgan bo'lsa, rasm so'ralmaydi —
    # to'g'ridan-to'g'ri moderatsiya paneli ko'rsatiladi (fayl allaqachon Telegram serverida saqlanadi).
    if attachment_file_id:
        sent = await message.answer(
            f"📝 <b>Qoralama tayyor (.apk fayl bilan):</b>\n\n{format_for_telegram_html(content)}",
            parse_mode="HTML",
        )
        await db.set_admin_message(post.id, sent.chat.id, sent.message_id)
        await _render_moderation_panel(message, db, post.id)
        return

    sent = await message.answer(
        f"📝 <b>Qoralama tayyor:</b>\n\n{format_for_telegram_html(content)}\n\n"
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
# 2) Oddiy matn xabari: link bo'lsa — tahlil qiladi, aks holda mavzu sifatida post yaratadi
# ---------------------------------------------------------------------------

@router.message(StateFilter(None), F.text, ~F.text.startswith("/"))
async def handle_text_message(message: Message, db: Database):
    if not is_admin(message.from_user.id):
        return

    text = message.text.strip()
    if not text:
        return

    match = URL_REGEX.search(text)

    if match:
        url = match.group(0)
        status_msg = await message.answer("🔗 Havola o'qilmoqda va qayta yozilmoqda...")
        try:
            content = await ai_service.fetch_and_summarize_link(url)
        except Exception as e:
            await status_msg.edit_text(f"❌ Havolani qayta ishlashda xatolik: {e}")
            return
        await status_msg.delete()
        await _start_post_flow(message, db, content, source_type="link", source_ref=url)
        return

    # URL topilmadi — matnni mavzu sifatida qabul qilib, post yaratamiz
    status_msg = await message.answer("🤖 AI post tayyorlamoqda, kuting...")
    try:
        content = await ai_service.generate_post_from_topic(text)
    except Exception as e:
        await status_msg.edit_text(f"❌ Post yaratishda xatolik: {e}")
        return

    await status_msg.delete()
    await _start_post_flow(message, db, content, source_type="topic", source_ref=text)


# ---------------------------------------------------------------------------
# 3) .apk fayl yuborilganda (fayl yuklab olinmaydi — Telegram file_id orqali
#    to'g'ridan-to'g'ri kanalga yo'naltiriladi, faqat matn AI orqali yaratiladi)
# ---------------------------------------------------------------------------

@router.message(F.document.file_name.endswith(".apk"))
async def handle_apk_document(message: Message, db: Database, bot: Bot):
    if not is_admin(message.from_user.id):
        return

    status_msg = await message.answer("📦 APK fayl ma'lumotlari asosida post tayyorlanmoqda...")

    document = message.document
    apk_info = {
        "Fayl nomi": document.file_name,
        "Hajmi": image_service_human_size(document.file_size) if document.file_size else "Noma'lum",
    }
    caption = message.caption  # Admin yuborgan izoh — ilova nomi, tavsifi va h.k. shu yerdan olinadi

    try:
        content = await ai_service.generate_post_from_apk(apk_info, caption=caption)
    except Exception as e:
        await status_msg.edit_text(f"❌ Post yaratishda xatolik: {e}")
        return

    await status_msg.delete()
    await _start_post_flow(
        message,
        db,
        content,
        source_type="apk",
        source_ref=document.file_name,
        attachment_file_id=document.file_id,
    )


def image_service_human_size(size_bytes: int) -> str:
    for unit in ["B", "KB", "MB", "GB"]:
        if size_bytes < 1024:
            return f"{size_bytes:.1f} {unit}"
        size_bytes /= 1024
    return f"{size_bytes:.1f} TB"


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
        f"{format_for_telegram_html(post.content)}\n\n⏳ Rasm tayyorlanmoqda, biroz kuting...",
        parse_mode="HTML",
    )

    try:
        if choice == "ai":
            image_bytes = await image_service.generate_ai_image(prompt_seed)
            image_source = ImageSourceType.AI_GENERATED
        elif choice == "real":
            search_query = await ai_service.generate_image_search_query(prompt_seed)
            image_url = await image_service.search_real_image(search_query)
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
            f"{format_for_telegram_html(post.content)}\n\n⚠️ Rasm tayyorlashda xatolik yuz berdi: {html_escape(str(e))}\nRasmsiz davom etamiz.",
            parse_mode="HTML",
        )
        await _render_moderation_panel(callback.message, db, post_id)


async def _render_moderation_panel(
    message: Message, db: Database, post_id: int, photo_path: Optional[str] = None
):
    post = await db.get_post(post_id)
    if not post:
        return

    caption = f"📝 <b>Moderatsiya:</b>\n\n{format_for_telegram_html(post.content)}"
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

    elif action == "richpost":
        await callback.answer("📰 AI maqola shaklida formatlanmoqda...")
        await _publish_rich_post(callback, db, bot, post_id)

    elif action == "schedule":
        await callback.answer()
        await state.update_data(schedule_post_id=post_id)
        await state.set_state(SchedulePost.waiting_for_datetime)
        await callback.message.answer(
            "🕒 Chop etish sanasi va vaqtini quyidagi formatda yuboring "
            "(Toshkent vaqti bo'yicha):\n\n<code>31.12.2026 18:30</code>\n\n"
            "Bekor qilish uchun /cancel yozing.",
            parse_mode="HTML",
        )

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
            search_query = await ai_service.generate_image_search_query(prompt_seed)
            image_url = await image_service.search_real_image(search_query)
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


@router.message(Command("cancel"), SchedulePost.waiting_for_datetime)
async def cancel_schedule(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("❌ Rejalashtirish bekor qilindi.")


@router.message(SchedulePost.waiting_for_datetime)
async def process_schedule_datetime(message: Message, state: FSMContext, db: Database):
    data = await state.get_data()
    post_id = data.get("schedule_post_id")

    if not post_id:
        await state.clear()
        await message.answer("⚠️ Post ID topilmadi, qaytadan urinib ko'ring.")
        return

    text = message.text.strip()
    try:
        local_dt = datetime.strptime(text, "%d.%m.%Y %H:%M")
    except ValueError:
        await message.answer(
            "❌ Format noto'g'ri. Iltimos, aynan shunday yozing:\n<code>31.12.2026 18:30</code>",
            parse_mode="HTML",
        )
        return

    # Admin mahalliy vaqtini (Toshkent, standart UTC+5) UTC'ga aylantiramiz
    admin_tz = timezone(timedelta(hours=ADMIN_TZ_OFFSET_HOURS))
    local_dt = local_dt.replace(tzinfo=admin_tz)
    utc_dt = local_dt.astimezone(timezone.utc)

    if utc_dt <= datetime.now(timezone.utc):
        await message.answer("❌ Kiritilgan vaqt allaqachon o'tib ketgan. Kelajakdagi vaqtni kiriting.")
        return

    await state.clear()

    try:
        await db.schedule_post(post_id, utc_dt)
    except Exception as e:
        await message.answer(f"❌ Rejalashtirishda xatolik: {e}")
        return

    await message.answer(
        f"✅ Post rejalashtirildi!\n🕒 Chop etish vaqti: <b>{text}</b> (Toshkent vaqti)",
        parse_mode="HTML",
    )


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

async def publish_post_to_channel(bot: Bot, db: Database, post_id: int) -> tuple[bool, str]:
    """
    Postni kanalga chop etishning asosiy mantiqi. Bu funksiya callback orqali ham,
    rejalashtirilgan postlar uchun fon vazifasi (scheduler) orqali ham chaqiriladi.
    Natija: (muvaffaqiyatli_boldimi, xabar_matni)
    """
    post = await db.get_post(post_id)
    if not post:
        return False, "⚠️ Post topilmadi."

    if not CHANNEL_ID:
        return False, "❌ CHANNEL_ID .env faylida sozlanmagan."

    final_text = f"{format_for_telegram_html(post.content)}\n\n-------------------\n{html_escape(CHANNEL_FOOTER)}"

    try:
        if post.attachment_file_id:
            # .apk (yoki boshqa hujjat) — hostga yuklanmaydi, Telegram file_id orqali to'g'ridan-to'g'ri forward qilinadi
            caption_text = final_text
            if len(caption_text) > 1024:
                # Telegram caption limiti 1024 belgi — sig'masa, hujjatni izohsiz yuboramiz,
                # to'liq matnni alohida xabar sifatida qo'shamiz.
                sent = await bot.send_document(chat_id=CHANNEL_ID, document=post.attachment_file_id)
                await bot.send_message(chat_id=CHANNEL_ID, text=final_text, parse_mode="HTML")
            else:
                sent = await bot.send_document(
                    chat_id=CHANNEL_ID,
                    document=post.attachment_file_id,
                    caption=caption_text,
                    parse_mode="HTML",
                )
        elif post.image_url and os.path.exists(post.image_url):
            sent = await bot.send_photo(
                chat_id=CHANNEL_ID,
                photo=FSInputFile(post.image_url),
                caption=final_text,
                parse_mode="HTML",
            )
        else:
            sent = await bot.send_message(chat_id=CHANNEL_ID, text=final_text, parse_mode="HTML")

        await db.mark_published(post_id, channel_message_id=sent.message_id)
        return True, "✅ Post muvaffaqiyatli kanalga chop etildi!"

    except Exception as e:
        logger.exception("Kanalga chop etishda xatolik: %s", e)
        return False, f"❌ Kanalga chop etishda xatolik: {e}"


async def send_daily_draft(
    bot: Bot,
    db: Database,
    admin_chat_id: int,
    content: str,
    source_type: str,
    source_ref: Optional[str] = None,
    auto_image_bytes: Optional[bytes] = None,
    auto_image_source: ImageSourceType = ImageSourceType.NONE,
) -> None:
    """
    Kunlik avtomatik generatsiya (scheduler) tomonidan chaqiriladi.
    Admin bilan Message/CallbackQuery kontekstisiz, to'g'ridan-to'g'ri chat_id orqali ishlaydi.
    """
    try:
        post = await db.create_post(content=content, source_type=source_type, source_ref=source_ref)
    except Exception as e:
        logger.exception("Kunlik post yaratishda xatolik: %s", e)
        return

    image_path = None
    if auto_image_bytes:
        try:
            watermark_text = CHANNEL_USERNAME or "@channel"
            final_bytes = image_service.add_text_watermark(auto_image_bytes, watermark_text)
            tmp_path = tempfile.NamedTemporaryFile(delete=False, suffix=".jpg").name
            with open(tmp_path, "wb") as f:
                f.write(final_bytes)
            await db.update_post_image(post.id, image_url=tmp_path, image_source=auto_image_source)
            image_path = tmp_path
        except Exception as e:
            logger.warning("Kunlik post uchun rasm tayyorlashda xatolik: %s", e)

    label = "📰 Kunlik yangilik" if source_type == "rss" else "🤖 Kunlik AI post"
    caption = f"🗓 <b>{label} (tasdiqlashingizni kutmoqda):</b>\n\n{format_for_telegram_html(content)}"
    keyboard = build_moderation_keyboard(post.id)

    try:
        if image_path:
            sent = await bot.send_photo(
                admin_chat_id, FSInputFile(image_path), caption=caption, parse_mode="HTML", reply_markup=keyboard
            )
        else:
            sent = await bot.send_message(admin_chat_id, caption, parse_mode="HTML", reply_markup=keyboard)
        await db.set_admin_message(post.id, sent.chat.id, sent.message_id)
    except Exception as e:
        logger.exception("Kunlik postni adminga yuborishda xatolik: %s", e)


async def _publish_post(callback: CallbackQuery, db: Database, bot: Bot, post_id: int):
    success, info_text = await publish_post_to_channel(bot, db, post_id)
    if success:
        try:
            await callback.message.delete()
        except Exception:
            pass
    await callback.message.answer(info_text)


async def _publish_rich_post(callback: CallbackQuery, db: Database, bot: Bot, post_id: int):
    """
    Postni Telegram Rich Message (Bot API 10.1+, 'Maqola' uslubi) sifatida
    kanalga chop etadi — sarlavha, paragraflar va ajratgichlar bilan.
    Bu funksiya aiogram>=3.31 talab qiladi.
    """
    post = await db.get_post(post_id)
    if not post:
        await callback.message.answer("⚠️ Post topilmadi.")
        return

    if not CHANNEL_ID:
        await callback.message.answer("❌ CHANNEL_ID .env faylida sozlanmagan.")
        return

    body_markdown = await ai_service.enrich_content_for_rich_post(post.content)
    footer_markdown = f"---\n\n{CHANNEL_FOOTER}"

    media_list = None
    if post.image_url and os.path.exists(post.image_url):
        # Rasmni maqola boshiga tg://photo?id=... havolasi orqali bog'laymiz
        image_markdown = "![](tg://photo?id=cover)\n\n"
        full_markdown = f"{image_markdown}{body_markdown}\n\n{footer_markdown}"
        media_list = [
            InputRichMessageMedia(
                id="cover",
                media=InputMediaPhoto(media=FSInputFile(post.image_url)),
            )
        ]
    else:
        full_markdown = f"{body_markdown}\n\n{footer_markdown}"

    try:
        rich_message = InputRichMessage(markdown=full_markdown, media=media_list)
        sent = await bot.send_rich_message(chat_id=CHANNEL_ID, rich_message=rich_message)

        await db.mark_published(post_id, channel_message_id=sent.message_id)

        try:
            await callback.message.delete()
        except Exception:
            pass
        await callback.message.answer("✅ Post 'Maqola' (Rich Message) sifatida kanalga chop etildi!")

    except AttributeError:
        await callback.message.answer(
            "❌ Joriy aiogram versiyasi Rich Message'ni qo'llab-quvvatlamaydi. "
            "requirements.txt faylida aiogram>=3.31.0 ekanini tekshiring va qaytadan deploy qiling."
        )
    except Exception as e:
        logger.exception("Rich Message sifatida chop etishda xatolik: %s", e)
        await callback.message.answer(
            f"❌ Maqola sifatida chop etishda xatolik: {e}\n\n"
            f"Oddiy '✅ Kanalga e'lon qilish' tugmasi orqali urinib ko'ring."
        )

