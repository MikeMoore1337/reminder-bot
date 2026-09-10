# Оффлайн benchmark русского voice STT

Этот инструмент сравнивает локальные модели `whisper.cpp` на разрешённом
корпусе голосовых сообщений. Он не обращается к Telegram, remote STT или LLM
и не меняет production runtime.

## Подготовка private real-user corpus

В репозитории нет реальных voice recordings. Каталог `benchmarks/audio/` и
рабочий manifest `voice-benchmark.json` намеренно игнорируются Git.

1. Получите явное разрешение на использование каждой записи для benchmark.
2. Запишите 10–20 фраз реальным пользователем в Telegram voice формате OGG/Opus
   или подготовьте WAV-копии локально. Не кладите в Git исходные записи,
   Telegram identifiers, private message text или другие лишние данные.
3. Скопируйте `benchmarks/voice-benchmark.example.json` в private
   `voice-benchmark.json` и укажите реальные относительные имена файлов.
4. Проверьте `expected` дословно по сказанной фразе. Поле `expected_product`
   заполните вручную по требуемому поведению продукта: ожидаемые локальные
   дата/время, семантика времени и body. Это ground truth, а не результат
   текущего parser.
5. Укажите фиксированный `now_local` с offset. Он нужен для воспроизводимого
   сравнения relative schedule.

В example manifest уже есть 12 строк, включая relative intervals, spoken
number, absolute time, короткую фразу с числом, negative case без schedule и
фразу длиной 8–15 слов. Audio к нему не прилагается. Synthetic TTS не является
real-user benchmark; его результат можно помечать только как
`synthetic infrastructure smoke`.

## Запуск

Нужны Python 3.12 с development dependencies, `ffmpeg`, собранный
`whisper-cli` и модели вне Git. Для каждого запуска передавайте одну и ту же
команду whisper.cpp и несколько model paths:

```text
python scripts/benchmark_voice_stt.py --manifest voice-benchmark.json --samples-dir benchmarks --model /models/ggml-base-q5_1.bin --model /models/ggml-small-q5_1.bin --command /opt/whisper/bin/whisper-cli --language ru --threads 2 --timeout-seconds 90 --measure-rss --output benchmarks/results/voice-stt.json
```

`--samples-dir` — корень для относительных `file` из manifest. Поддерживаются
`.wav`, `.ogg` и `.opus`; каждый файл и model должен существовать, быть
непустым, а sample path не может выйти за пределы этого каталога.

Каждый sample сначала проходит тот же production
`convert_voice_to_wav`: mono, 16 kHz, signed 16-bit PCM WAV, с теми же
bounded process/size limits. Затем запускается тот же
`WhisperCppSpeechToTextProvider` с `language=ru`, явным argv и timeout.
Модели обрабатываются последовательно, поэтому benchmark не создаёт
параллельную нагрузку на VPS.

## Метрики

Для каждой пары sample/model JSON report содержит:

- production-normalized transcript и normalized exact-match;
- WER и CER после NFKC, case-fold, `ё -> е`, удаления punctuation и
  нормализации whitespace;
- результат `parse_voice_transcript()` — `parsed`, `deadline`, `clarification`
  или failure;
- `schedule_correct`, `body_correct` и
  `fully_correct_reminder_interpretation` относительно явного
  `expected_product`;
- wall-clock latency нормализации и STT, timeout/crash/error category;
- optional `peak_rss_bytes`. На Linux `--measure-rss` снимает high-water
  `VmHWM` процесса через `/proc`; метод и отсутствие измерения явно записываются
  в report. Это best-effort process measurement, а не выдуманная оценка RAM.

В summary по модели считаются mean, median и nearest-rank p95 STT latency,
parser success rate, schedule/body rates, fully-correct rate, fail-closed rate,
timeout/crash/error counts и максимальный измеренный RSS. Fully-correct rate
имеет denominator только у samples с ожидаемым parsed/deadline reminder;
negative/clarification samples отдельно входят в `safety_fail_closed_rate`.
Ошибки STT не считаются успешной интерпретацией.

## Официальные multilingual small candidates

В официальном `models/download-ggml-model.sh` для whisper.cpp v1.9.2 есть
точные имена `small-q5_1` и `small-q8_0`, которые скачиваются как
`ggml-small-q5_1.bin` и `ggml-small-q8_0.bin`. `.en` варианты не подходят для
русского потока. В том же скрипте нет официальных `small-q4_*` или
`small-q5_0`; не следует угадывать такие имена.

| Модель | Официальный размер на диске | Практический trade-off |
| --- | ---: | --- |
| `ggml-small-q5_1.bin` | 181 MiB (около 190 MB) | Первый кандидат: заметно точнее класса base за счёт примерно 3.2× большего файла против текущего base Q5; умереннее RAM, чем Q8 |
| `ggml-small-q8_0.bin` | 252 MiB (около 264 MB) | Меньше quantization loss, но больше RAM/IO; запасной кандидат для сравнения |
| `ggml-small.bin` | 466 MiB | Не quantized; для host с 2 vCPU и 2 GB RAM не первый кандидат |

Размер файла не равен peak RSS: модель требует буферы и память процесса.
Ориентир для планирования — small Q5 добавляет к текущему model file около
124 MB, small Q8 — около 204 MB; окончательное решение принимается только по
измеренному RSS, свободной RAM и отсутствию swap/OOM.

## Acceptance criteria для будущего switch

Численные пороги не зашиты в tool до фактических measurements. Для owner
review после capacity audit разумно начинать с такого набора:

- fully-correct rate small заметно выше base на том же real-user corpus, с
  заранее согласованным минимальным улучшением; отдельно проверить каждую
  известную semantic ошибку вроде `голосовое` → `глазовой`;
- ни одного ухудшения fail-closed поведения, safety mismatch, OOM, crash или
  timeout; negative samples должны оставаться без создания reminder;
- p95 STT укладывается в Telegram UX target (начальный review target — не более
  30 секунд, но owner подтверждает его по фактическому UX);
- измеренный peak RSS вместе с обычным потреблением bot/worker/db оставляет
  подтверждённый запас физической RAM (начальный operational target — не менее
  256 MiB, не считать swap запасом);
- threads остаются `2`, concurrency остаётся `1`, пока measurements на 2-vCPU
  host не докажут безопасное и полезное изменение.

Текущий capacity snapshot — 2 vCPU, около 2.06 GB RAM, около 1.02 GB available
RAM, около 2.15 GB swap и около 22.3 GiB свободного места на filesystem с
voice-runtime. Это ещё не peak-RSS measurement small model и не разрешение на
production swap.

## Возможность model swap

Текущий deployment-owned bundle монтируется в контейнер read-only. Runtime
читает модель через `VOICE_STT_MODEL_PATH`; code migration для замены файла не
нужна. По upstream v1.9.2 small Q5/Q8 входят в список моделей того же
`whisper-cli`, но перед любым rollout нужен offline load/transcription smoke с
тем же binary.

Для будущей owner-approved замены потребуется отдельно provision новый файл в
bundle, проверить checksum/load/latency/RSS и изменить только
`VOICE_STT_MODEL_PATH` (плюс обычный контролируемый restart/deploy). В этой
задаче model file, production `.env`, binary, containers и database не
изменяются. Adaptive base → small fallback пока не реализуется.
