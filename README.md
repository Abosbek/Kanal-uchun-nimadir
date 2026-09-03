# Telegram Channel Manager AI — Deployment Yo'riqnomasi

## 1. Loyihani tayyorlash

```bash
pip install -r requirements.txt
cp .env.example .env
```

`.env` faylni to'ldiring: `BOT_TOKEN`, `ADMIN_IDS`, `CHANNEL_ID`, `GEMINI_API_KEY` va boshqalar.

Lokal test uchun `DATABASE_URL=sqlite+aiosqlite:///./bot_local.db` qoldirib, ishga tushiring:

```bash
python main.py
```

---

## 2. Supabase (PostgreSQL) sozlash

1. https://supabase.com — yangi loyiha (project) yarating.
2. **Project Settings → Database → Connection string** bo'limidan `URI` ni oling. U taxminan shunday ko'rinishda bo'ladi:
   ```
   postgresql://postgres:[PAROL]@db.xxxxxxxxxxxx.supabase.co:5432/postgres
   ```
3. Buni `asyncpg` drayveriga moslashtiring (prefiksni almashtiring):
   ```
   postgresql+asyncpg://postgres:[PAROL]@db.xxxxxxxxxxxx.supabase.co:5432/postgres
   ```
4. Shu qatorni `.env` faylidagi `DATABASE_URL` ga qo'ying.
5. Bot birinchi marta ishga tushganda (`on_startup` ichida `db.init_models()`) jadvallar avtomatik yaratiladi — Supabase'da qo'lda SQL yozish shart emas.

---

## 3. Google Gemini API kaliti olish

1. https://aistudio.google.com/app/apikey ga kiring.
2. "Create API key" tugmasini bosing.
3. Olingan kalitni `.env` dagi `GEMINI_API_KEY` ga joylashtiring.

---

## 4. GitHub'ga yuklash

```bash
git init
git add .
git commit -m "Telegram Channel Manager AI bot"
git branch -M main
git remote add origin https://github.com/SIZNING_USERNAME/channel-manager-bot.git
git push -u origin main
```

**Muhim:** `.env` faylini hech qachon GitHub'ga yuklamang! `.gitignore` faylga `.env` qatorini qo'shing.

---

## 5. Render.com'da joylashtirish

1. https://render.com — ro'yxatdan o'ting, GitHub akkauntingizni ulang.
2. **New → Web Service** tugmasini bosing va repozitoriyangizni tanlang.
3. Sozlamalar:
   - **Runtime:** Python 3
   - **Build Command:** `pip install -r requirements.txt`
   - **Start Command:** `python main.py`
   - **Instance Type:** Free
4. **Environment** bo'limida `.env` dagi barcha o'zgaruvchilarni bittalab qo'shing (`BOT_TOKEN`, `ADMIN_IDS`, `CHANNEL_ID`, `CHANNEL_FOOTER`, `GEMINI_API_KEY`, `DATABASE_URL`, va h.k.).
5. `RUN_MODE=webhook` qiling va `WEBHOOK_URL` ga Render bergan public manzilni yozing (masalan `https://channel-manager-bot.onrender.com`) — bu manzil birinchi deploy tugagach Render panelida ko'rinadi, keyin uni environment'ga qo'shib qayta deploy qilasiz.
6. `PORT` odatda Render tomonidan avtomatik beriladi — `.env`dagi qiymatni Render ko'rsatgan portga moslashtiring (Render buni `PORT` muhit o'zgaruvchisi orqali o'zi taqdim etadi, kodimiz shuni o'qiydi).
7. **Deploy** tugmasini bosing. Loglarda "Webhook o'rnatildi" yozuvini ko'rsangiz — tayyor.

> Eslatma: Agar webhook sozlash murakkab tuyulsa, `RUN_MODE=polling` qoldirib, oddiy polling rejimida ham ishlatishingiz mumkin — bu variant ham Render Free tarifida ishlaydi, faqat resurs sal ko'proq sarflanadi.

---

## 6. UptimeRobot bilan 24/7 uxlamasligini ta'minlash

Render'ning bepul tarifi 15 daqiqa faoliyatsizlikdan so'ng "uxlab qoladi". Buni oldini olish uchun:

1. https://uptimerobot.com — ro'yxatdan o'ting.
2. **Add New Monitor** → Monitor Type: `HTTP(s)`.
3. **URL:** `https://channel-manager-bot.onrender.com/health` (o'zingizning Render manzilingiz + `/health`).
4. **Monitoring Interval:** 5 daqiqa.
5. Saqlang — endi UptimeRobot har 5 daqiqada botingizni "uyg'otib" turadi va u doim faol qoladi.

---

## 7. Botni sinovdan o'tkazish

1. Bot bilan `/start` yozing (faqat `ADMIN_IDS` da ko'rsatilgan foydalanuvchilar uchun ishlaydi).
2. Havola yuboring — bot avtomatik o'qib, qayta yozadi.
3. `.apk` fayl yuboring — ilova haqida post tayyorlaydi.
4. `/post Sun'iy intellekt yangiliklari` — mavzu bo'yicha post yaratadi.
5. `/rss` — Product Hunt'dan yangi loyihalarni tortib keladi.
6. Har bir qoralama ostidagi tugmalar orqali rasm tanlang, kerak bo'lsa qayta yozdiring yoki tahrirlang, so'ngra **✅ Kanalga e'lon qilish** tugmasini bosing — post avtomatik imzo bilan kanalga chiqadi.

---

## Fayllar tuzilishi

```
telegram_channel_manager/
├── main.py                # Botni ishga tushirish (polling/webhook + health-check)
├── database.py             # SQLAlchemy async modellar (Supabase/SQLite)
├── ai_service.py            # Gemini AI, link scraping, RSS, APK tahlili
├── image_service.py         # Pollinations AI rasm, DuckDuckGo qidiruv, Pillow watermark
├── handlers/
│   └── admin.py             # Admin buyruqlari, FSM, inline tugmalar, kanal imzosi
├── requirements.txt
└── .env.example
```
