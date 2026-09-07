# 🤖 Reminder Bot

Современный Telegram-бот для напоминаний с поддержкой естественного языка, повторяющихся задач и масштабируемой архитектуры.

---

## 🚀 Возможности

### 🕒 Напоминания
- по дате и времени  
- через промежуток времени  
- повторяющиеся  

### 📌 Примеры
напомни завтра в 9 созвон  
напомни через 30 минут выключить духовку  
напомни каждый день в 10 выпить витамины  
напомни каждые 2 часа пить воду  
напомни каждые 10 минут проверить сервер  
напомни 31.03.2026 18:30 купить молоко  

---

### 🔁 Повторения

Поддерживается:

- каждые X минут (минимум 5)
- каждый час / каждые X часов
- каждый день / каждые X дней
- каждую неделю
- каждый месяц

Примеры:
каждые 5 минут  
каждый час  
каждые 2 часа  
каждый день  
каждую неделю  
каждый месяц  

---

### 🌍 Часовые пояса
- индивидуально для каждого пользователя  
- формат IANA (Europe/Moscow, Europe/Helsinki и т.д.)  
- хранение времени в UTC  

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
- повторения обновляются без создания новых записей  
- worker обрабатывает задачи через БД  
- `/healthz` — liveness процесса и не зависит от PostgreSQL
- `/readyz` — bounded PostgreSQL readiness: `200` при `SELECT 1`, `503` при недоступной БД

## 🔒 Зависимости и lockfiles

`pyproject.toml` — canonical source прямых runtime-зависимостей и `dev` extra. Для обновления
lockfiles используется `pip-tools`; `requirements.in` не нужен и не дублирует `pyproject.toml`:

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

- inline-кнопки управления  
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
