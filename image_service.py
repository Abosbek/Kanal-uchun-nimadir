"""
image_service.py
Pollinations.ai orqali AI rasm generatsiyasi, DuckDuckGo orqali haqiqiy rasm
qidirish va Pillow yordamida watermark/logotip bosish xizmatlari.
"""

import asyncio
import io
import logging
import os
import time
import urllib.parse
from typing import Optional

import requests
from PIL import Image, ImageDraw, ImageFont
from ddgs import DDGS
from ddgs.exceptions import DDGSException, RatelimitException, TimeoutException
from google import genai

logger = logging.getLogger(__name__)

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
GEMINI_IMAGE_MODEL = os.getenv("GEMINI_IMAGE_MODEL", "gemini-2.5-flash-image")

_genai_client: Optional[genai.Client] = None


def _get_genai_client() -> genai.Client:
    global _genai_client
    if _genai_client is None:
        if not GEMINI_API_KEY:
            raise RuntimeError("GEMINI_API_KEY .env faylida topilmadi.")
        _genai_client = genai.Client(api_key=GEMINI_API_KEY)
    return _genai_client

POLLINATIONS_MODEL = os.getenv("POLLINATIONS_MODEL", "flux")
POLLINATIONS_BASE_URL = "https://image.pollinations.ai/prompt"


# ---------------------------------------------------------------------------
# 1) AI orqali noldan rasm generatsiya qilish (Pollinations.ai)
# ---------------------------------------------------------------------------

def _build_pollinations_url(prompt: str, width: int = 1024, height: int = 768, seed: Optional[int] = None) -> str:
    encoded_prompt = urllib.parse.quote(prompt)
    url = (
        f"{POLLINATIONS_BASE_URL}/{encoded_prompt}"
        f"?width={width}&height={height}&model={POLLINATIONS_MODEL}&nologo=true"
    )
    if seed is not None:
        url += f"&seed={seed}"
    return url


async def generate_ai_image(prompt: str, width: int = 1024, height: int = 768) -> bytes:
    """
    Rasmni AVVAL Gemini (gemini-2.5-flash-image, "Nano Banana") orqali generatsiya qilishga
    urinadi — sifat ancha yuqori. Agar bu ishlamasa (masalan kvota tugagan bo'lsa),
    zaxira sifatida Pollinations.ai ga o'tadi.
    """
    try:
        return await _generate_ai_image_gemini(prompt)
    except Exception as e:
        logger.warning("Gemini rasm generatsiyasi ishlamadi, Pollinations.ai ga o'tilmoqda: %s", e)
        return await _generate_ai_image_pollinations(prompt, width, height)


async def _generate_ai_image_gemini(prompt: str) -> bytes:
    def _call() -> bytes:
        client = _get_genai_client()
        full_prompt = (
            f"Create a high-quality, visually appealing illustration for a technology "
            f"Telegram channel post about: {prompt}. Style: modern, clean, professional, "
            f"vibrant colors, no text or watermarks in the image."
        )
        response = client.models.generate_content(
            model=GEMINI_IMAGE_MODEL,
            contents=full_prompt,
        )
        for part in response.candidates[0].content.parts:
            if getattr(part, "inline_data", None) is not None:
                return part.inline_data.data
        raise RuntimeError("Gemini javobida rasm topilmadi.")

    return await asyncio.to_thread(_call)


async def _generate_ai_image_pollinations(prompt: str, width: int = 1024, height: int = 768) -> bytes:
    url = _build_pollinations_url(prompt, width, height)

    def _download() -> bytes:
        resp = requests.get(url, timeout=60)
        resp.raise_for_status()
        return resp.content

    try:
        return await asyncio.to_thread(_download)
    except Exception as e:
        logger.exception("Pollinations.ai orqali rasm yaratishda xatolik: %s", e)
        raise RuntimeError(f"AI rasm generatsiyasida xatolik: {e}") from e


def get_ai_image_url(prompt: str, width: int = 1024, height: int = 768) -> str:
    """Faylni yuklamasdan, faqat public URL'ni qaytaradi (kerak bo'lsa saqlash uchun)."""
    return _build_pollinations_url(prompt, width, height)


# ---------------------------------------------------------------------------
# 2) Internetdan haqiqiy rasm qidirish (DuckDuckGo Image Search)
# ---------------------------------------------------------------------------

async def search_real_image(query: str, max_results: int = 8) -> Optional[str]:
    """
    Rasm qidiruvi (Google/Bing/DuckDuckGo — ddgs kutubxonasi orqali).
    Bir nechta backend'ni ketma-ket sinaydi, rate-limitga uchraganda qayta urinadi.
    """

    def _search() -> Optional[str]:
        backends_to_try = ["auto", "bing", "google", "duckduckgo"]
        last_error: Optional[Exception] = None

        for backend in backends_to_try:
            for attempt in range(2):
                try:
                    with DDGS() as ddgs:
                        results = list(
                            ddgs.images(
                                query=query,
                                region="us-en",
                                safesearch="moderate",
                                max_results=max_results,
                                backend=backend,
                            )
                        )
                    for r in results:
                        image_url = r.get("image") or r.get("image_url") or r.get("url")
                        if image_url:
                            return image_url
                    break  # bu backend bo'sh natija berdi, keyingi backendga o'tamiz
                except RatelimitException as e:
                    last_error = e
                    logger.warning("Rate-limit (%s backend), qayta urinish %s/2...", backend, attempt + 1)
                    time.sleep(2 * (attempt + 1))
                except TimeoutException as e:
                    last_error = e
                    logger.warning("Timeout (%s backend), qayta urinish %s/2...", backend, attempt + 1)
                    time.sleep(1)
                except DDGSException as e:
                    last_error = e
                    logger.warning("Qidiruvda xatolik (%s backend): %s", backend, e)
                    break
                except Exception as e:
                    last_error = e
                    logger.exception("Kutilmagan xatolik rasm qidirishda (%s backend): %s", backend, e)
                    break

        if last_error:
            logger.warning("Barcha backendlar sinab ko'rildi, natija topilmadi: %s", last_error)
        return None

    return await asyncio.to_thread(_search)


async def download_image_bytes(image_url: str) -> bytes:
    def _download() -> bytes:
        headers = {"User-Agent": "Mozilla/5.0 (compatible; ChannelManagerBot/1.0)"}
        resp = requests.get(image_url, headers=headers, timeout=30)
        resp.raise_for_status()
        return resp.content

    try:
        return await asyncio.to_thread(_download)
    except Exception as e:
        logger.exception("Rasmni yuklab olishda xatolik (%s): %s", image_url, e)
        raise RuntimeError(f"Rasmni yuklab bo'lmadi: {e}") from e


# ---------------------------------------------------------------------------
# 3) Pillow orqali watermark / logotip bosish
# ---------------------------------------------------------------------------

def add_text_watermark(
    image_bytes: bytes,
    text: str,
    opacity: int = 160,
    font_size: int = 32,
    margin: int = 20,
) -> bytes:
    """
    Rasm pastki o'ng burchagiga shaffof matnli watermark qo'shadi.
    Masalan: "@kanal_username"
    """
    try:
        base_image = Image.open(io.BytesIO(image_bytes)).convert("RGBA")
        txt_layer = Image.new("RGBA", base_image.size, (255, 255, 255, 0))
        draw = ImageDraw.Draw(txt_layer)

        try:
            font = ImageFont.truetype("DejaVuSans-Bold.ttf", font_size)
        except Exception:
            font = ImageFont.load_default()

        bbox = draw.textbbox((0, 0), text, font=font)
        text_w, text_h = bbox[2] - bbox[0], bbox[3] - bbox[1]

        x = base_image.width - text_w - margin
        y = base_image.height - text_h - margin

        # Fon uchun yarim shaffof to'rtburchak (o'qilishini yengillashtirish uchun)
        draw.rectangle(
            [x - 10, y - 6, x + text_w + 10, y + text_h + 10],
            fill=(0, 0, 0, 90),
        )
        draw.text((x, y), text, font=font, fill=(255, 255, 255, opacity + 60))

        watermarked = Image.alpha_composite(base_image, txt_layer).convert("RGB")

        output = io.BytesIO()
        watermarked.save(output, format="JPEG", quality=92)
        return output.getvalue()
    except Exception as e:
        logger.exception("Watermark qo'shishda xatolik: %s", e)
        raise RuntimeError(f"Watermark qo'shib bo'lmadi: {e}") from e


def add_logo_watermark(
    image_bytes: bytes,
    logo_path: str,
    scale: float = 0.15,
    margin: int = 20,
) -> bytes:
    """Rasm ustiga logotip (PNG, shaffof fon bilan) joylashtiradi."""
    try:
        base_image = Image.open(io.BytesIO(image_bytes)).convert("RGBA")
        logo = Image.open(logo_path).convert("RGBA")

        logo_width = int(base_image.width * scale)
        ratio = logo_width / logo.width
        logo_height = int(logo.height * ratio)
        logo = logo.resize((logo_width, logo_height))

        position = (
            base_image.width - logo_width - margin,
            base_image.height - logo_height - margin,
        )
        base_image.paste(logo, position, logo)

        output = io.BytesIO()
        base_image.convert("RGB").save(output, format="JPEG", quality=92)
        return output.getvalue()
    except Exception as e:
        logger.exception("Logotip watermark qo'shishda xatolik: %s", e)
        raise RuntimeError(f"Logotip qo'shib bo'lmadi: {e}") from e
