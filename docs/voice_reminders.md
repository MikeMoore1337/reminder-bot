# Голосовые напоминания

Голосовой сценарий остаётся частью bot-процесса:

`Telegram voice -> bounded download -> ffmpeg -> whisper.cpp CLI -> deterministic parser -> PostgreSQL draft -> explicit confirmation -> reminder`

Аудио скачивается только в уникальный временный каталог. Каталог удаляется в
`finally` после успеха, ошибки или отмены. В PostgreSQL сохраняется только
временный TTL-bound draft с транскриптом и разобранными полями; исходный OGG/Opus
не сохраняется. Draft удаляется после подтверждения, отмены или фоновой TTL-cleanup.

Поддерживаются только Telegram voice messages с OGG/Opus, размером до
`VOICE_MAX_FILE_SIZE_BYTES` и длительностью до `VOICE_MAX_DURATION_SECONDS`.
Загрузка, конвертация, STT-процесс, размер результата и число одновременно
обрабатываемых voice jobs ограничены настройками. Ошибки возвращаются без
вывода путей, stderr, токенов, аудио или транскрипта в логи.

## Почему on-demand CLI

`whisper.cpp` запускается отдельным bounded-процессом на один voice job и
завершается после расшифровки. Это не добавляет постоянно работающий сервис и
сохраняет разделение bot/worker на небольшом CPU-only VPS. По умолчанию voice
STT выключен, пока не задан путь к модели; remote STT, GPU, LLM fallback и
второй polling owner не используются.

Ресурсный профиль: STT запускается только на время одного запроса, по умолчанию
с двумя CPU threads и максимумом одной одновременной job. `tiny`/quantized
модель снижает RAM/CPU ценой качества, а `base` даёт лучший русский результат
ценой большего пикового потребления; постоянного STT-процесса и фоновой загрузки
модели нет. Таймауты, очередь и лимиты медиа не дают voice flow занять worker
без ограничений.

## Модель и bootstrap

Модель и бинарник `whisper.cpp` должны быть установлены владельцем вне Git и
вне runtime temp directory. Конвертер `ffmpeg` входит в immutable application
image и проверяется до rollout. Используйте multilingual
`base` либо конфигурируемый `tiny`/quantized low-resource вариант; `.en` модели
для русского потока не подходят.

Compose поддерживает один лёгкий host runtime bundle, например
`/opt/reminder-bot/voice-runtime`, со структурой:

```text
voice-runtime/
  bin/whisper-cli
  models/ggml-base.bin
```

Он монтируется только в bot-контейнер как read-only в `/opt/reminder-bot/voice`.
Временные voice-каталоги bot и worker используют общий именованный volume в
`/var/lib/reminder-bot/voice-temp`; это позволяет worker удалить orphan после
перезапуска, не давая ему доступ к runtime bundle. Ни бинарники, ни модели не
коммитятся в репозиторий, а provisioning этого bundle остаётся owner-only.
В `.env` задаются container paths:

```text
VOICE_RUNTIME_DIR=/opt/reminder-bot/voice-runtime
VOICE_STT_COMMAND=/opt/reminder-bot/voice/bin/whisper-cli
VOICE_STT_MODEL_PATH=/opt/reminder-bot/voice/models/ggml-base.bin
VOICE_STT_LANGUAGE=ru
VOICE_STT_THREADS=2
VOICE_TEMP_DIR=/var/lib/reminder-bot/voice-temp
```

`VOICE_STT_COMMAND` разбирается как argv и запускается с `shell=False`. Не
вставляйте в него shell pipelines, secrets или пользовательские значения.
Compose задаёт `VOICE_CONVERSION_COMMAND=/usr/bin/ffmpeg` из application image;
значение из production `.env` не используется для converter path. Команда
конвертера разбирается в argv и не проходит через shell. До production rollout
deploy script запускает bounded synthetic OGG/Opus -> mono 16 kHz PCM WAV
preflight; при отключённом STT он пропускается, а при включённом и сломанном
runtime deployment завершается до `ACTIVE`.

Production secrets, model installation, `DEPLOY_ENABLED`, первый deploy и
production database operations остаются owner-only. Этот документ не является
разрешением выполнять эти действия.

## UX и retention

1. Пользователь отправляет voice message.
2. Бот валидирует media boundary, получает транскрипт и прогоняет существующий
   детерминированный parser.
3. При валидном результате бот показывает транскрипт, текст, локальное время,
   повторение и часовой пояс с кнопками «Создать» и «Отмена».
4. Reminder появляется только после «Создать». Повторный или stale callback
   не создаёт вторую запись.
5. Неоднозначный результат переводится в bounded clarification flow с
   сохранением voice-контекста. Ответ пользователя создаёт только новый
   confirmation-gated voice draft; Reminder появляется лишь после точного
   «Создать». `/cancel` отменяет voice draft, clarification и другие активные
   сценарии.

Счётчики и bounded latency buckets доступны администраторской статистике:
download/conversion/STT/parse/confirmation/cancellation/cleanup. В метриках нет
содержимого аудио или текста. Conversion failures дополнительно дают safe log с
`stage=media`, категорией, return code или типом ошибки; raw ffmpeg stderr,
пути, Telegram identifiers и secrets не логируются.
