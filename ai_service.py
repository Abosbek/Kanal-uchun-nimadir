"""
ai_service.py
Google Gemini (google-genai) orqali matn generatsiyasi, web-sahifa scraping,
APK fayl tahlili va RSS feed'larni o'qish uchun xizmatlar.
"""

import asyncio
import logging
import os
import re
import zipfile
from dataclasses import dataclass
from typing import Optional

import feedparser
import requests
from bs4 import BeautifulSoup
from google import genai

logger = logging.getLogger(__name__)

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-1.5-flash")

_client: Optional[genai.Client] = None


def _get_client() -> genai.Client:
    global _client
    if _client is None:
        if not GEMINI_API_KEY:
            raise RuntimeError("GEMINI_API_KEY .env faylida topilmadi.")
        _client = genai.Client(api_key=GEMINI_API_KEY)
    return _client


BASE_STYLE_PROMPT = (
    "Sen o'zbek tilidagi Telegram texnologiya kanali uchun kontent yozuvchisan. "
    "Uslubing: qisqa, jonli, chiroyli emoji bilan bezatilgan, aniq va professional. "
    "Post oxirida mavzuga mos 3-5 ta hashteg qo'sh. "
    "Faqat tayyor post matnini qaytar, hech qanday qo'shimcha izoh yozma."
)


async def _generate(prompt: str, temperature: float = 0.8) -> str:
    """Gemini API'ga so'rov yuborish (blocking chaqiruvni thread'da bajaramiz)."""

    def _call() -> str:
        client = _get_client()
        response = client.models.generate_content(
            model=GEMINI_MODEL,
            contents=prompt,
            config={"temperature": temperature},
        )
        return (response.text or "").strip()

    try:
        return await asyncio.to_thread(_call)
    except Exception as e:
        logger.exception("Gemini API xatoligi: %s", e)
        raise RuntimeError(f"AI xizmatida xatolik yuz berdi: {e}") from e


async def generate_post_from_topic(topic: str) -> str:
    """Ixtiyoriy mavzu asosida to'liq post yaratish."""
    prompt = (
        f"{BASE_STYLE_PROMPT}\n\n"
        f"Quyidagi mavzu bo'yicha Telegram kanali uchun post yoz:\n"
        f"Mavzu: {topic}"
    )
    return await _generate(prompt)


async def generate_post_from_text_source(raw_text: str, source_label: str = "maqola") -> str:
    """Web-sahifa yoki boshqa manbadan olingan xom matnni qayta yozish (rephrase/summary)."""
    # Juda uzun matnni qisqartiramiz, tokenlarni tejash uchun
    trimmed = raw_text[:6000]
    prompt = (
        f"{BASE_STYLE_PROMPT}\n\n"
        f"Quyida bir {source_label}dan olingan matn berilgan. Uni o'qib, o'zbek tiliga "
        f"moslab, kanal uslubida qisqacha va qiziqarli post shaklida qayta yoz "
        f"(so'zma-so'z tarjima qilma, mazmunini bayon qil):\n\n{trimmed}"
    )
    return await _generate(prompt)


async def rewrite_post(original_text: str, style_hint: Optional[str] = None) -> str:
    """Mavjud post matnini boshqa uslubda qayta generatsiya qilish."""
    style_instruction = f" Uslub: {style_hint}." if style_hint else " Boshqacha, yangi uslubda yoz."
    prompt = (
        f"{BASE_STYLE_PROMPT}{style_instruction}\n\n"
        f"Quyidagi postni qayta yoz (mazmunini saqla, lekin so'zlarni va tuzilishini o'zgartir):\n\n"
        f"{original_text}"
    )
    return await _generate(prompt, temperature=1.0)


async def generate_post_from_apk(apk_info: dict, caption: Optional[str] = None) -> str:
    """APK fayl haqidagi metama'lumotlar asosida post yaratish."""
    info_lines = "\n".join(f"{k}: {v}" for k, v in apk_info.items() if v)
    extra = f"\nAdmin izohi: {caption}" if caption else ""
    prompt = (
        f"{BASE_STYLE_PROMPT}\n\n"
        f"Quyidagi Android ilova (.apk) haqida jozibali tanishtiruv posti yoz. "
        f"Ilova nomi, versiyasi, hajmi va imkoniyatlarini ta'kidla. "
        f"Hashteglar orasida albatta #apk va #app bo'lsin:\n\n{info_lines}{extra}"
    )
    return await _generate(prompt)


# ---------------------------------------------------------------------------
# Web-sahifa (link) scraping
# ---------------------------------------------------------------------------

def _fetch_page_text_sync(url: str, timeout: int = 15) -> str:
    headers = {"User-Agent": "Mozilla/5.0 (compatible; ChannelManagerBot/1.0)"}
    resp = requests.get(url, headers=headers, timeout=timeout)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "lxml")

    # Skript, style va navigatsiya kabi keraksiz bloklarni olib tashlaymiz
    for tag in soup(["script", "style", "nav", "footer", "header", "noscript"]):
        tag.decompose()

    title = soup.title.string.strip() if soup.title and soup.title.string else ""
    paragraphs = [p.get_text(" ", strip=True) for p in soup.find_all("p")]
    body_text = " ".join(paragraphs)

    return f"Sarlavha: {title}\n\n{body_text}".strip()


async def fetch_and_summarize_link(url: str) -> str:
    """Link ochib, matnini o'qib, Gemini orqali kanal uslubida qayta yozish."""
    try:
        raw_text = await asyncio.to_thread(_fetch_page_text_sync, url)
    except Exception as e:
        logger.exception("Sahifani o'qishda xatolik (%s): %s", url, e)
        raise RuntimeError(f"Havolani ochib bo'lmadi: {e}") from e

    if not raw_text or len(raw_text) < 40:
        raise RuntimeError("Sahifadan yetarlicha matn topilmadi.")

    return await generate_post_from_text_source(raw_text, source_label="veb-sahifa")


# ---------------------------------------------------------------------------
# APK fayl tahlili (metama'lumotlar)
# ---------------------------------------------------------------------------

def extract_apk_info(apk_path: str) -> dict:
    """
    APK fayldan asosiy metama'lumotlarni ajratib olish.
    Androguard kabi og'ir kutubxonalarsiz, AndroidManifest.xml va
    fayl hajmidan foydalanib, iloji boricha ma'lumot chiqaramiz.
    Chuqur tahlil kerak bo'lsa, androguard kutubxonasini requirements.txt ga
    qo'shib, shu funksiyani kengaytirish mumkin.
    """
    info = {
        "Fayl nomi": os.path.basename(apk_path),
        "Hajmi": _human_size(os.path.getsize(apk_path)),
        "Versiya": "Noma'lum",
        "Paket nomi": "Noma'lum",
    }
    try:
        with zipfile.ZipFile(apk_path, "r") as z:
            names = z.namelist()
            info["Fayllar soni"] = str(len(names))
            # AndroidManifest.xml binary formatda bo'lgani uchun to'liq parslash
            # androguard kabi maxsus kutubxona talab qiladi. Bu yerda faqat
            # mavjudligini tekshiramiz, aks holda Gemini'ga umumiy ma'lumot beramiz.
            if "AndroidManifest.xml" in names:
                info["Manifest"] = "Topildi (batafsil versiya androguard orqali o'qilishi mumkin)"
    except zipfile.BadZipFile:
        logger.warning("APK fayl to'g'ri ZIP formatida emas: %s", apk_path)
    except Exception as e:
        logger.exception("APK faylni tahlil qilishda xatolik: %s", e)

    return info


def _human_size(size_bytes: int) -> str:
    for unit in ["B", "KB", "MB", "GB"]:
        if size_bytes < 1024:
            return f"{size_bytes:.1f} {unit}"
        size_bytes /= 1024
    return f"{size_bytes:.1f} TB"


# ---------------------------------------------------------------------------
# RSS Feed (Product Hunt / Reddit)
# ---------------------------------------------------------------------------

@dataclass
class RssItem:
    title: str
    link: str
    summary: str


async def fetch_rss_items(feed_url: str, limit: int = 5) -> list[RssItem]:
    def _parse() -> list[RssItem]:
        parsed = feedparser.parse(feed_url)
        items = []
        for entry in parsed.entries[:limit]:
            summary_raw = getattr(entry, "summary", "")
            summary_clean = re.sub("<[^<]+?>", "", summary_raw)[:500]
            items.append(
                RssItem(
                    title=getattr(entry, "title", "Noma'lum sarlavha"),
                    link=getattr(entry, "link", ""),
                    summary=summary_clean,
                )
            )
        return items

    try:
        return await asyncio.to_thread(_parse)
    except Exception as e:
        logger.exception("RSS o'qishda xatolik (%s): %s", feed_url, e)
        return []


async def generate_post_from_rss_item(item: RssItem) -> str:
    prompt = (
        f"{BASE_STYLE_PROMPT}\n\n"
        f"Quyidagi yangi texnologik loyiha/yangilik haqida qisqa tanishtiruv posti yoz:\n\n"
        f"Sarlavha: {item.title}\n"
        f"Tavsif: {item.summary}\n"
        f"Manba: {item.link}"
    )
    return await _generate(prompt)
