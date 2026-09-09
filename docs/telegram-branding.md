# Telegram branding

Reminder Bot позиционируется как умная напоминалка без сложных форм: пользователь
пишет или говорит обычной фразой, бот разбирает время и показывает понятное
подтверждение перед созданием напоминания.

## Что синхронизируется автоматически

При каждом старте bot-процесса `app.bot_commands.setup_bot_commands` вызывает
идемпотентный Bot API sync из `app.telegram_metadata`:

- name, short description/About и description;
- public commands в default scope;
- полный набор команд только для chat scopes, перечисленных в `ADMIN_IDS`.

Один canonical source — `app/telegram_metadata.py`. Не вводите эти значения
вручную в BotFather: runtime повторяет sync после временной ошибки и не делает
metadata failure причиной restart loop. Username, token, ownership, production
deployment, БД и `mtproxy` этим документом не меняются.

## Assets

| Surface | Telegram-ready file | Reproducible source |
| --- | --- | --- |
| Avatar/profile picture | `assets/telegram/avatar.png` | `assets/telegram/avatar.svg` + `scripts/export_telegram_assets.py` |
| Welcome / Description Picture | `assets/telegram/welcome.png` | `assets/telegram/welcome.svg` + `scripts/export_telegram_assets.py` |

PNG-файлы уже закоммичены и подходят для загрузки. SVG и генератор не используют
AI-generated raster text, системный шрифт или сетевой сервис. Для повторной
генерации из корня репозитория:

```text
python scripts/export_telegram_assets.py
```

Аватар — простой символ часов и выполненного действия, без мелкого текста и с
безопасной зоной для circular crop. Welcome visual показывает тот же символ и
три ключевые возможности: текст, голос и повторы. Основной текст остаётся в
caption `/start`, поэтому он доступен для копирования и локализации.

## Минимальный owner checkpoint после merge

Эти действия не выполняются кодом автоматически, чтобы не менять публичный
профиль повторной загрузкой media при каждом рестарте:

1. Откройте `@BotFather`, отправьте `/mybots` и выберите существующего бота.
   Не выбирайте `/newbot`, `/token`, transfer ownership или удаление бота.
2. Откройте `Bot Settings` → `Edit Botpic` (название пункта может немного
   отличаться в клиенте) и загрузите готовый файл
   `assets/telegram/avatar.png`.
3. Если в этом BotFather/client доступен пункт `Edit Description Picture`,
   загрузите туда `assets/telegram/welcome.png`. Если пункта нет, этот шаг
   можно пропустить: welcome visual уже используется в `/start`.
4. На телефоне откройте профиль и новый/пустой чат с ботом. Проверьте, что
   avatar читается в маленьком круге, а welcome image и подпись объясняют
   назначение бота. Не меняйте username, token и ownership.

Name, About, description и commands после этого должны оставаться значениями,
которые выставляет runtime Bot API sync. Для справки по BotFather и описаниям:
[официальная документация Telegram Bot Features](https://core.telegram.org/bots/features).
