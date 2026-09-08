# Подсказки и дайджесты

Issue #7 добавляет две независимые, opt-in функции. Обе настройки пользователя
выключены по умолчанию и хранятся в PostgreSQL.

## Подсказки по переносам

`/suggestions on` включает запись минимальной истории переносов: user/chat,
reminder, время исходного occurrence, целевое время и IANA timezone. Текст
напоминания и raw Telegram payload в историю не попадают. История ограничена
окном `SUGGESTION_WINDOW_DAYS` и максимумом 32 событий на reminder.

Для календарного повторения подсказка создаётся только когда минимум
`SUGGESTION_SNOOZE_THRESHOLD` переносов попали в один кластер с допуском
`SUGGESTION_TARGET_TOLERANCE_MINUTES`, а предлагаемое время отличается от
исходного минимум на `SUGGESTION_MIN_SCHEDULE_SHIFT_MINUTES`. One-off, minute/hourly,
deadline и completion-relative reminders не участвуют.

Созданная подсказка ничего не меняет автоматически. Только кнопка «Перенести»
принимает подсказку, проверяет owner/chat, revision и актуальность reminder, а
затем атомарно обновляет календарное правило. «Оставить» и «Скрыть подсказку»
не меняют расписание. Повторный callback идемпотентен; stale callback не
применяется к новому состоянию. Pending-подсказка истекает, если её evidence
или сама запись вышли за retention window; такая подсказка не показывается, а
старый callback безопасно становится идемпотентным `expired`.

## Дайджесты

`/digest on` включает максимум один утренний и один вечерний digest на local date
пользователя. По умолчанию расписание — 09:00 и 20:00, тихие часы — 22:00–08:00.
Слоты и lease хранятся в `reminder_digest_deliveries`; текущий и следующий слоты
ставятся в очередь при opt-in и после завершения доставки. Уникальность
`(user_id, period, local_date)` предотвращает дубли после рестарта или параллельного
worker, а idle-poll читает только индексированную очередь due-доставок и не
пересматривает всех opt-in пользователей.

В дайджест попадают активные незавершённые reminders в сохранённом часовом поясе.
Обычные reminders ограничены `DIGEST_MAX_ITEMS`, а persistent/важные reminders
добавляются сверх этого лимита и не скрываются: если полный текст важных
reminders не помещается, для каждого сохраняется компактное представление с
его идентификатором. Просроченный слот старше `DIGEST_MAX_DELAY_MINUTES` и слот
в тихих часах подавляется, а временный сбой Telegram проходит через bounded
retry/lease policy с учётом полного `RetryAfter`. Lease каждой delivery
обновляется непосредственно перед внешним send.
Терминальная ошибка Telegram (например, заблокированный чат) помечает текущую
доставку failed, отключает digest opt-in и подавляет будущие queued slots до
нового явного включения. Если `DIGEST_LEASE_DURATION_SECONDS`
не задан, digest lease наследует `WORKER_LEASE_DURATION_SECONDS`; явный override
должен учитывать timeout и safety margin worker.

Отключение функции отзывает pending подсказки или queued digest slots; уже
идущая отправка не прерывается задним числом. Worker периодически удаляет
истёкшую историю и завершённые delivery records по retention policy.

Настройки времени являются server-side defaults/configuration. Production
секреты, deploy gate и live Telegram smoke этим Issue не изменяются.
