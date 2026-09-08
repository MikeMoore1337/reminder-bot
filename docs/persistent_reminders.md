# Важные (persistent) напоминания

Важный режим — это ограниченный повтор одной текущей доставки. Он не создаёт
новую независимую задачу и не изменяет каноническое расписание. В PostgreSQL
сохраняются `mode`, следующий `delivery_at_utc`, выбранный часовой пояс,
лимиты и счётчики цикла, поэтому worker может продолжить работу после
перезапуска.

## Ввод и состояние

Режим включается явным маркером:

```text
напомни важное завтра в 9 позвонить
напомни завтра в 18 проверить отчёт [важное]
/remind persistent 2026-09-10 09:00 принять лекарство
```

В базе хранится каноническое значение `normal` или `persistent`; `important`
и русские формы `важное`/`постоянное` — только пользовательские алиасы.
Обычная задача сохраняет прежнее поведение.

После успешной отправки persistent reminder остаётся в состоянии `scheduled`:
`remind_at_utc` сохраняет канонический occurrence, а `delivery_at_utc` получает
следующий bounded repeat. Пока цикл активен, worker переиспользует persisted
identity этого occurrence и обновляет его revision; это не позволяет двум
валидным worker leases выполнить одну переходную операцию.

Цикл завершается при первом из событий:

- `Готово` — occurrence выполнен; для recurring series вычисляется следующий
  канонический occurrence;
- `Отложить` — для one-off создаётся одна обычная отложенная доставка без
  дальнейших persistent repeats; для recurring series snooze остаётся локальным
  child occurrence, а следующий канонический occurrence серии не теряется;
- `Выключить повторы` — mode становится `normal`, текущая доставка сохраняется
  actionable;
- `Удалить` или terminal delivery failure;
- достижение лимита доставок/повторов.

Все действия проверяют owner/chat scope, revision и при наличии callback —
точную occurrence identity и Telegram message ID. Повторный или устаревший
callback становится no-op.

## Bounded policy

Настройки приложения:

- `PERSISTENT_REPEAT_INTERVAL_MINUTES`: 5–1440 минут, default `60`;
- `PERSISTENT_MAX_DELIVERIES`: 1–100, default `6`;
- `PERSISTENT_MAX_ESCALATIONS`: 0–99, default `5`;
- `PERSISTENT_QUIET_HOURS_START` и `PERSISTENT_QUIET_HOURS_END`: `HH:MM`,
  default `22:00`–`08:00`;
- `PERSISTENT_USER_COOLDOWN_MINUTES`: 0–60, default `1`.

Эффективный предел цикла — `min(max_deliveries, 1 + max_escalations)`. Поэтому
даже ошибочная конфигурация с большим одним лимитом не делает цикл
unbounded. `WORKER_MAX_ATTEMPTS` отдельно ограничивает retry конкретной
попытки доставки; retry не увеличивает persistent delivery count.

Перед claim worker переводит due delivery за локальные quiet hours на конец
окна и увеличивает `persistent_deferred_count`, не меняя `remind_at_utc`.
Затем он резервирует cooldown в строке пользователя под PostgreSQL row lock,
чтобы batch/concurrent workers не отправили несколько persistent сообщений
одному пользователю одновременно. В логах и метриках используются только
безопасные идентификаторы, состояния и bounded counters.

Тихие часы рассчитываются в сохранённом IANA `schedule_timezone`. Переходы DST
используют общий scheduling policy проекта: nonexistent local time сдвигается
через gap, ambiguous time использует fold 0. В памяти worker не хранит
расписание как source of truth.
