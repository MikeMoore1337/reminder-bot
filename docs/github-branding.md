# GitHub branding · Reminder Bot

Этот набор оформляет **публичную страницу репозитория**, а не профиль Telegram-бота. Он согласован по палитре и мотиву циферблата с [Telegram branding](telegram-branding.md).

## Готовые ресурсы

| Файл | Назначение |
| --- | --- |
| [`assets/branding/hero.svg`](../assets/branding/hero.svg) | Обложка README (1200 × 440), подключена автоматически |
| [`assets/branding/social-preview.svg`](../assets/branding/social-preview.svg) | Векторный оригинал карточки для социальных сетей (1280 × 640) |
| [`assets/telegram/avatar.png`](../assets/telegram/avatar.png) | Аватар Telegram, не подменяется GitHub-обложкой |
| [`assets/telegram/welcome.png`](../assets/telegram/welcome.png) | Welcome-визуал Telegram, не подменяется GitHub-обложкой |

SVG используют системные шрифты как fallback и не обращаются к внешним файлам. Палитра: тёмный синий `#09162D`, основной синий `#2869B7`, голубой `#8AE9FF`, белый `#F9FCFF`. Контрастная текстовая версия содержится в README, поэтому иллюстрация не является единственным источником информации.

## GitHub About · рекомендуемые значения

**Description:**

> Telegram-бот напоминаний: естественный язык, голос, гибкие повторы, дедлайны и общие задачи. Python · aiogram · PostgreSQL.

**Topics:**

`telegram-bot`, `reminder-bot`, `python`, `aiogram`, `postgresql`, `docker-compose`, `task-management`, `voice-recognition`, `scheduling`.

**Website:** оставлять пустым, пока не известен подтверждённый публичный username или официальный сайт. Не публиковать примерный `t.me/...`.

Настройки About/Topics задаются через блок **About → шестерёнка** в правой части страницы репозитория. Это metadata GitHub и оно не меняется коммитом README.

## Social preview

GitHub принимает растровую картинку для карточки ссылки. Чтобы подготовить PNG на основе векторного исходника:

```bash
inkscape assets/branding/social-preview.svg \
  --export-filename=assets/branding/social-preview.png \
  --export-width=1280 --export-height=640
```

Команда требует установленный Inkscape и выполняется **локально**, не в CI/production. Перед загрузкой проверьте, что кириллица отображается корректно и надписи не обрезаны. Загрузить PNG нужно в настройках репозитория **Settings → General → Social preview** (если у владельца есть доступ к разделу). Сам SVG напрямую не активирует social preview.

Не изменять BotFather, Telegram username, токены, production-конфигурацию и deployment ради оформления GitHub.

## Редакционные правила

- Тезисы на обложке должны соответствовать реализованным функциям, а не планируемому backlog.
- Никаких фиктивных метрик, скачиваний, статусов релиза и ссылок на несуществующего публичного бота.
- Не добавлять badge лицензии, пока лицензия явно не оформлена в репозитории.
- Сохранять читаемость на мобильных устройствах, а у важных картинок — осмысленный `alt`.
