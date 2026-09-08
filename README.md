# 🤖 Reminder Bot

Современный Telegram-бот для напоминаний с поддержкой естественного языка, повторяющихся задач и масштабируемой архитектуры.

---

## 🚀 Возможности

### 🕒 Напоминания
- по дате и времени  
- через промежуток времени  
- повторяющиеся по дням недели, рабочим дням, календарным правилам и после выполнения

### 📌 Примеры
напомни завтра в 9 созвон  
напомни через 30 минут выключить духовку  
напомни каждый день в 10 выпить витамины  
напомни каждые 2 часа пить воду  
напомни каждые 10 минут проверить сервер  
напомни 31.03.2026 18:30 купить молоко  
напомни каждый понедельник и четверг в 9 отправить отчёт
напомни по будням в 18 проверить задачи
напомни каждый второй вторник месяца в 10 оплатить счёт
напомни каждый год 15 марта в 9 годовщина
напомни завтра в 9 отчёт, через 3 дня после выполнения

---

### 🔁 Повторения

Поддерживается:

- каждые X минут (минимум 5)
- каждый час / каждые X часов
- каждый день / каждые X дней
- каждую неделю
- каждый месяц
- выбранные дни недели и рабочие дни
- каждые N недель
- N-й или последний день недели в месяце
- ежегодные даты
- повторять до указанной даты
- N дней после фактического выполнения

### 🔔 Важный режим

Добавь `важное`, `important` или `persistent` после команды `напомни`, либо
поставь `[важное]` в конце строки. Бот повторяет доставку до явного действия
пользователя, но всегда соблюдает сохранённые лимиты, тихие часы и cooldown на
сообщения одного пользователя. Кнопка «Выключить повторы» переводит текущий
reminder обратно в обычный режим; «Готово», «Отложить» и «Удалить» также
останавливают текущий persistent-цикл.

Пример:

```text
напомни важное завтра в 9 позвонить
напомни завтра в 18 проверить отчёт [важное]
```

Политика и DST-safe правила описаны в
[docs/persistent_reminders.md](docs/persistent_reminders.md).

### ⏳ Защита дедлайна

Для задачи с конечным сроком используй `/deadline`: бот построит ограниченный
план напоминаний, покажет preview и сохранит его только после подтверждения.
Можно написать и естественно: `Оплатить VPS до 10 сентября`; без времени будет
использовано 23:59 в часовом поясе пользователя.
Точки можно настроить после `|`, например `за день, за час, в срок` или
`просрочено через час`. Детали persistence, DST и действий описаны в
[docs/deadline_protection.md](docs/deadline_protection.md).

Примеры:
каждые 5 минут  
каждый час  
каждые 2 часа  
каждый день  
каждую неделю  
каждый месяц  

Правила календаря хранятся в каноническом виде и пересчитываются в сохранённом
часовом поясе пользователя. Если формулировка неоднозначна (`в пятницу в 8`,
`завтра вечером`, `после обеда`), бот задаёт уточняющий вопрос и хранит его 15 минут;
неоднозначный текст не создаёт напоминание. `/cancel` отменяет уточнение.

---

### 🌍 Часовые пояса
- индивидуально для каждого пользователя  
- формат IANA (Europe/Moscow, Europe/Helsinki и т.д.)  
- хранение времени в UTC  

### 🎙 Голосовые напоминания
- Telegram voice в OGG/Opus проходит bounded download и локальную конвертацию;
- `whisper.cpp` запускается on-demand, только с multilingual/local model;
- транскрипт проходит тот же детерминированный parser;
- reminder сохраняется только после явного подтверждения «Создать»;
- аудио удаляется после обработки, draft хранится в PostgreSQL ограниченное время;
- настройка модели и CPU-only bootstrap описаны в [docs/voice_reminders.md](docs/voice_reminders.md),
  а правила контекстных reminders — в [docs/message_context.md](docs/message_context.md).

### 📎 Контекст Telegram
- reply, forward, пост канала, ссылка, фото и документ сохраняются как bounded snapshot/reference;
- медиа не скачивается: при наличии `file_id` worker отправляет его через Telegram, иначе использует text fallback;
- обычный reminder без Telegram-контекста остаётся без дополнительных данных;
- контекст хранится 30 дней, не логируется raw и удаляется worker-ом по TTL.

---

## 🧠 UX / UI

### `/start`
- короткое описание
- примеры использования
- текущий часовой пояс
- кнопки управления

### Reply-клавиатура
- ➕ Создать напоминание  
- 📋 Мои напоминания  
- 🌍 Часовой пояс  
- ❓ Помощь  

### `/help`
подробная инструкция

### Action cards
- доставленные напоминания имеют кнопки «Готово», «Отложить», «Изменить» и «Удалить»;
- важные напоминания дополнительно показывают «Выключить повторы»;
- для повторяющихся напоминаний доступны «Пауза» и «Продолжить»;
- «Вечером» означает ближайшие 20:00, а «Завтра» — 09:00 в сохранённом часовом поясе пользователя;
- `/list` показывает по одной карточке на активное напоминание с локальным временем и действиями.

---

## ⚙️ Команды

Публичные:
- /start  
- /help  
- /timezone  
- /mytimezone  
- /remind  
- /list  
- /cancel  

Админ:
- /stats  
- /failed  

---

## 🏗 Архитектура

Telegram → Bot (aiogram) → PostgreSQL → Worker

Почему так:
- нет потери задач при рестарте
- масштабируемость
- устойчивость

Доставленные occurrence и многошаговые Edit/Custom Snooze хранятся в PostgreSQL.
Recurring Snooze создаёт связанную one-off child-запись и не меняет canonical series.

---

## ⚙️ Установка

### 1. Клонирование
git clone <repo>
cd reminder_bot

---

### 2. Настройка .env

BOT_TOKEN=your_token  
POSTGRES_DB=reminder_bot  
POSTGRES_USER=postgres  
POSTGRES_PASSWORD=postgres  
BOT_MODE=polling  
DATABASE_URL=postgresql+asyncpg://postgres:postgres@db:5432/reminder_bot  
LOG_LEVEL=INFO  
DEFAULT_TIMEZONE=Europe/Moscow  
POLLING_ALLOWED_UPDATES=message,edited_message,callback_query  
ADMIN_IDS=123456789  

Для локальной разработки установи dev lock без production-секретов:

```bash
python -m venv .venv
python -m pip install -r requirements-dev.txt
```

Production Docker image устанавливает только `requirements.txt`.

---

### 3. Запуск

docker compose build  
docker compose --profile tools run --rm migrate  
docker compose up -d db bot worker  

---

### 4. Логи

docker compose logs -f bot  
docker compose logs -f worker  

---

## 🔁 Управление

Перезапуск:
docker compose restart  

Остановка:
docker compose down  

Обновление:
git pull  
docker compose build  
docker compose up -d  

---

## ⚠️ Важно

- все даты хранятся в UTC  
- пользователю показывается локальное время  
- canonical-правило повторения хранится вместе со следующим UTC-вхождением
- повторения обновляются без создания новых записей (кроме независимого snooze child)
- worker обрабатывает задачи через БД  
- `/healthz` — liveness процесса и не зависит от PostgreSQL
- `/readyz` — bounded PostgreSQL readiness: `200` при `SELECT 1`, `503` при недоступной БД

Bot process слушает `APP_HOST:APP_PORT` в обоих режимах. В polling mode HTTP app содержит только
`/healthz` и `/readyz`; в webhook mode к этим probe routes добавляется Telegram webhook route.

## 🔒 Зависимости и lockfiles

`pyproject.toml` — canonical source прямых runtime-зависимостей и `dev` extra. Для обновления
lockfiles используется `pip-tools`; `requirements.in` не нужен и не дублирует `pyproject.toml`.
Lockfiles генерируются под Linux/Python 3.12 — это target CI и production Docker; на Windows
запускай команды в WSL или Linux-контейнере, чтобы не добавить Windows-only зависимости вроде
`colorama`:

```bash
python -m pip install --upgrade pip-tools
pip-compile --index-url https://pypi.org/simple --no-emit-index-url --strip-extras --output-file requirements.txt pyproject.toml
pip-compile --index-url https://pypi.org/simple --no-emit-index-url --strip-extras --extra dev --output-file requirements-dev.txt pyproject.toml
```

В рамках этого baseline оставлен `pip-tools`, а не `uv`: текущий pip/Docker workflow уже работает,
и compiled runtime/dev lockfiles дают воспроизводимость с меньшим migration risk. Переход на uv
возможен отдельной задачей только при измеримой выгоде.

## ✅ Проверки и CI

GitHub Actions запускается для PR в `master` и push в `master` и содержит стабильные jobs:

- `quality` — Python 3.12, dev lock, Ruff check/format и mypy;
- `tests-postgres` — Python 3.12, PostgreSQL 16 service, полный pytest, миграционный upgrade/
  downgrade/re-upgrade и PostgreSQL concurrency tests;
- `docker-smoke` — production image build, Compose config/migration validation и проверка
  отсутствия dev tools в runtime image без Telegram API.

Lock freshness проверяется пересборкой обоих lockfiles и `git diff --exit-code`. CI не использует
production secrets или production database.

## 🗃️ Миграции и merge policy

Проверить цепочку локально:

```bash
alembic heads
alembic history
alembic upgrade head
```

После Issue #13 self-merge допускается только для внешне одобренного exact head при зелёных
`quality`, `tests-postgres` и `docker-smoke`, mergeable PR и отсутствии blocking review. Auto-merge
предпочтителен, если branch rules и exact-head guards сохранены.

---

## 📈 Возможности для развития

- cron-подобные расписания  
- rate limiting  
- уведомления о сбоях  
- webhook + HTTPS  

---

## 🧠 Концепция

- БД = источник истины  
- worker = обработка  
- bot = интерфейс  

Это даёт:
- стабильность  
- масштабируемость  
- предсказуемость  

---
