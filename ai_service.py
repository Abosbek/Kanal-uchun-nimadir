"""
ai_service.py
Google Gemini (google-genai) orqali matn generatsiyasi, web-sahifa scraping,
APK fayl tahlili va RSS feed'larni o'qish uchun xizmatlar.
"""

import asyncio
import logging
import os
import random
import re
import zipfile
from dataclasses import dataclass
from datetime import date
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
    "MUHIM: hech qanday Markdown belgisidan foydalanma — ya'ni **qalin**, __tagizza__, "
    "# sarlavha yoki shunga o'xshash belgilarni ishlatma. Faqat oddiy matn va emoji ishlat. "
    "Faqat tayyor post matnini qaytar, hech qanday qo'shimcha izoh yozma."
)


async def _generate(prompt: str, temperature: float = 0.8, max_retries: int = 3) -> str:
    """
    Gemini API'ga so'rov yuborish (blocking chaqiruvni thread'da bajaramiz).
    Google tomonidan vaqtinchalik xatolar (503 UNAVAILABLE, 429 RATE_LIMIT) qaytarilsa,
    eksponensial kutish bilan avtomatik qayta urinadi.
    """

    def _call() -> str:
        client = _get_client()
        response = client.models.generate_content(
            model=GEMINI_MODEL,
            contents=prompt,
            config={"temperature": temperature},
        )
        return (response.text or "").strip()

    last_error: Optional[Exception] = None
    for attempt in range(1, max_retries + 1):
        try:
            return await asyncio.to_thread(_call)
        except Exception as e:
            last_error = e
            error_text = str(e)
            is_temporary = any(
                marker in error_text
                for marker in ("503", "UNAVAILABLE", "429", "RESOURCE_EXHAUSTED", "overloaded")
            )
            if is_temporary and attempt < max_retries:
                wait_seconds = 2 ** attempt  # 2s, 4s, 8s...
                logger.warning(
                    "Gemini vaqtinchalik band (urinish %s/%s), %s soniyadan keyin qayta urinamiz: %s",
                    attempt, max_retries, wait_seconds, e,
                )
                await asyncio.sleep(wait_seconds)
                continue
            logger.exception("Gemini API xatoligi: %s", e)
            raise RuntimeError(f"AI xizmatida xatolik yuz berdi: {e}") from e

    raise RuntimeError(f"AI xizmatida xatolik yuz berdi: {last_error}")


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

def _fetch_page_text_sync(url: str, timeout: int = 20) -> str:
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "uz,ru;q=0.9,en;q=0.8",
    }
    try:
        resp = requests.get(url, headers=headers, timeout=timeout, allow_redirects=True)
        resp.raise_for_status()
    except requests.exceptions.SSLError as e:
        raise RuntimeError(f"SSL sertifikat xatosi: {e}") from e
    except requests.exceptions.Timeout as e:
        raise RuntimeError("Sahifa javob bermadi (timeout). Sayt sekin ishlayotgan bo'lishi mumkin.") from e
    except requests.exceptions.HTTPError as e:
        raise RuntimeError(f"Sayt xatolik qaytardi: {e}") from e
    except requests.exceptions.RequestException as e:
        raise RuntimeError(f"Sahifaga ulanib bo'lmadi: {e}") from e

    resp.encoding = resp.apparent_encoding or resp.encoding
    soup = BeautifulSoup(resp.text, "lxml")

    for tag in soup(["script", "style", "nav", "footer", "header", "noscript", "svg", "iframe"]):
        tag.decompose()

    title = soup.title.string.strip() if soup.title and soup.title.string else ""
    paragraphs = [p.get_text(" ", strip=True) for p in soup.find_all("p")]
    body_text = " ".join(t for t in paragraphs if len(t) > 20)

    # Ba'zi saytlarda matn <p> teglarida emas, boshqa strukturada bo'ladi.
    # Shunday holatda butun sahifa matnidan foydalanamiz (fallback).
    if len(body_text) < 200:
        fallback_text = soup.get_text(separator=" ", strip=True)
        if len(fallback_text) > len(body_text):
            body_text = fallback_text

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


# ---------------------------------------------------------------------------
# Rasm qidiruvi uchun optimallashtirilgan (ingliz tilida) kalit so'z yaratish
# ---------------------------------------------------------------------------

async def generate_image_search_query(post_content: str) -> str:
    """
    Rasm qidiruv tizimlari (Google/Bing/DuckDuckGo) odatda ingliz tilidagi qisqa
    so'zlarga yaxshiroq javob beradi. Shuning uchun o'zbekcha post matnidan
    3-5 so'zdan iborat, vizual jihatdan aniq ingliz qidiruv so'zini yasaymiz.
    """
    prompt = (
        "Quyidagi post matni asosida, rasm qidiruv tizimi uchun 3-5 so'zdan iborat, "
        "aniq va vizual jihatdan tasvirlanadigan INGLIZ TILIDA qidiruv so'zini yoz. "
        "Faqat qidiruv so'zini qaytar, boshqa hech narsa yozma, tirnoq belgisiz:\n\n"
        f"{post_content[:500]}"
    )
    try:
        result = await _generate(prompt, temperature=0.3)
        return result.strip().strip('"').strip("'")
    except Exception as e:
        logger.warning("Qidiruv so'zi generatsiyasida xatolik, xom matn ishlatiladi: %s", e)
        return post_content[:60]


# ---------------------------------------------------------------------------
# Har kuni avtomatik post generatsiyasi uchun mavzular hovuzi
# ---------------------------------------------------------------------------

TOPIC_POOL = [
    "Sun'iy intellekt sohasidagi eng so'nggi startaplar",
    "Foydali va kam odam biladigan Chrome kengaytmalari",
    "Dasturchilar uchun hayotni osonlashtiruvchi lifehacklar",
    "Kiberxavfsizlik bo'yicha amaliy maslahatlar",
    "Hozir juda mashhur bo‘layotgan ai orqali o‘zini rasmini har xil rasmga aylantirish uchun promplar ",
    "Qiziqarli va foydali veb-saytlar to'plami",
    "Ochiq manbali (open-source) loyihalarning eng qizig'lari",
    "Robototexnika sohasidagi so'nggi yutuqlar",
    "Kvant kompyuterlar haqida sodda tilda",
    "Mobil ilovalar dunyosidagi eng qiziqarli yangiliklar",
    "Googledagi qiziqarli natija beradigan qidiruv so‘zlari yangilari",
    "Kosmik texnologiyalar va xususiy kosmik kompaniyalar",
    "Virtual va kengaytirilgan reallik (VR/AR) yangiliklari",
    "Elektromobillar sohasidagi so'nggi yangiliklar",
    "Gadjetlar olamidan qiziqarli faktlar",
    "Dasturlash tillari reytingi va tendensiyalari",
    "Blokcheyn va kriptovalyutalar dunyosidagi yangiliklar",
    "IT sohasida martaba qurish bo'yicha maslahatlar",
    "Foydali AI vositalari (ChatGPT, Midjourney va boshqalar)",
    "Internet xavfsizligi va shaxsiy ma'lumotlarni himoya qilish",
    "Video o'yinlar sanoatidagi so'nggi yangiliklar",
    "Ijtimoiy tarmoqlar algoritmlari qanday ishlaydi",
    "Big Data va ma'lumotlar tahlili dunyosi",
    "Texnologik gigantlarning (Google, Apple, Microsoft) so'nggi qadamlari",
    "Startap ekotizimi va venchur investitsiyalar",
    "Smart uy (Smart Home) texnologiyalari",
    "5G va kelajakdagi aloqa texnologiyalari",
    "Ta'limda texnologiyalar (EdTech) yangiliklari",
    "Sog'liqni saqlashda sun'iy intellekt qo'llanilishi",
    "Iqtisodiyot va texnologiya kesishmasidagi qiziqarli voqealar",
]


def get_daily_ai_topics(count: int = 5, seed_date: Optional[date] = None) -> list[str]:
    """
    Har kuni boshqacha, lekin bir kun ichida barqaror (deterministik) mavzular
    to'plamini qaytaradi — shu sababli qayta ishga tushirilsa ham bir xil kun
    uchun bir xil mavzular tanlanadi, lekin har kuni boshqa kombinatsiya chiqadi.
    """
    seed_date = seed_date or date.today()
    rng = random.Random(seed_date.isoformat())
    pool_copy = TOPIC_POOL.copy()
    rng.shuffle(pool_copy)
    return pool_copy[:count]


# ---------------------------------------------------------------------------
# Har kuni bir nechta RSS manbadan eng so'nggi yangiliklarni yig'ish
# ---------------------------------------------------------------------------

DAILY_RSS_SOURCES = [
    os.getenv("PRODUCTHUNT_RSS", "https://www.producthunt.com/feed"),
    os.getenv("REDDIT_RSS", "https://www.reddit.com/r/technology/.rss"),
    "https://hnrss.org/frontpage",
]


async def get_daily_news_items(count: int = 5) -> list[RssItem]:
    """Bir nechta RSS manbadan yangiliklarni yig'ib, birlashtirib, top N tasini qaytaradi."""
    all_items: list[RssItem] = []
    per_source_limit = max(2, (count // max(1, len(DAILY_RSS_SOURCES))) + 2)

    for feed_url in DAILY_RSS_SOURCES:
        items = await fetch_rss_items(feed_url, limit=per_source_limit)
        all_items.extend(items)

    # Sarlavha bo'yicha takrorlanishlarni olib tashlaymiz
    seen_titles = set()
    unique_items = []
    for item in all_items:
        key = item.title.strip().lower()
        if key and key not in seen_titles:
            seen_titles.add(key)
            unique_items.append(item)

    random.shuffle(unique_items)
    return unique_items[:count]


# ---------------------------------------------------------------------------
# Maqola sahifasidan asosiy rasmni (og:image) topish
# ---------------------------------------------------------------------------

def _fetch_og_image_sync(url: str, timeout: int = 15) -> Optional[str]:
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        ),
    }
    try:
        resp = requests.get(url, headers=headers, timeout=timeout)
        resp.raise_for_status()
    except Exception as e:
        logger.warning("og:image uchun sahifani ochib bo'lmadi (%s): %s", url, e)
        return None

    soup = BeautifulSoup(resp.text, "lxml")
    og_image = soup.find("meta", property="og:image") or soup.find("meta", attrs={"name": "twitter:image"})
    if og_image and og_image.get("content"):
        return og_image["content"]
    return None


async def fetch_og_image(url: str) -> Optional[str]:
    """Maqola/havola sahifasidan asosiy (og:image) rasm URL manzilini qaytaradi, topilmasa None."""
    try:
        return await asyncio.to_thread(_fetch_og_image_sync, url)
    except Exception as e:
        logger.warning("og:image olishda xatolik: %s", e)
        return None
