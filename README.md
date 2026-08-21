# Great Opportunities — Telegram AI Administrator

MASTER-пакет проекта №4.

## Уже реализовано
- AI через Kie.ai
- память 15 сообщений
- запись на урок по свободным слотам
- 7 услуг
- отзывы
- уведомления администратору
- работа только в личных чатах
- SQLite
- admin takeover на 24 часа

## Команды администратора
/addslot 25.08.2026 17:00 — добавить окно
/slots — показать слоты
/delslot 12 — удалить слот
/bookings — показать записи
/reply USER_ID текст — ответить клиенту и поставить AI на паузу 24 ч
/pause USER_ID [часы] — пауза AI
/resume USER_ID — вернуть AI

## Перед запуском
1. Создать бота у @BotFather.
2. В хостинге задать BOT_TOKEN, ADMIN_ID, KIE_API_KEY.
3. Заполнить реальные цены и правила в knowledge_base.json.
4. При желании добавить assets/welcome.jpg и 3–5 отзывов в assets/reviews/.

Секретные ключи в файлы проекта не записывать.

## AI endpoint

По умолчанию используется документированный Kie.ai endpoint:
`https://api.kie.ai/gemini-2.5-flash/v1/chat/completions`
Ключ хранится только в переменной окружения `KIE_API_KEY`.
