# Пайплайн данных: BigQuery + TimesFM → база симулятора

Подключение экономических рядов к симулятору **без отдельного бэкенда** (Вариант B).
Прогноз считается офлайн и записывается в артефакт `kz_model_data.json`, который
дашборд уже читает напрямую — с готовым фолбэком, если артефакт недоступен.

## Поток данных

```
источники → ingest.py → BigQuery (kz_econ.series)
                                  │
                          AI.FORECAST (TimesFM)   ← forecast_baseline.sql
                                  │
                  kz_data_pipeline.py: fetch_bigquery_forecast()
                                  │  (проверка диапазонов, маппинг)
                                  ▼
                        kz_model_data.json  →  index.html (симулятор)
```

Почему так, а не сервер с `/api/baseline`: сайт — статика на GitHub Pages.
Прогноз меняется медленно (макроряды), поэтому считать его на каждый запрос не нужно.
BigQuery **никогда не вызывается из браузера**, ключ живёт только в окружении/секретах CI.

## Состав

| Файл | Назначение |
|------|------------|
| `ingest.py` | Загрузка рядов в `kz_econ.series` (добавляет пользователь). Повторный запуск не плодит дубли. |
| `forecast.sql` | Запрос прогноза пользователя (опц.; путь в `BQ_FORECAST_SQL`). |
| `forecast_baseline.sql` | Запрос прогноза по умолчанию (TimesFM `AI.FORECAST`). |
| `requirements.txt` | Зависимости шага прогноза (`google-cloud-bigquery`). |
| `.env.example` | Шаблон переменных окружения. |

## Запуск

```bash
# 1. зависимости шага прогноза
pip install -r data_pipeline/requirements.txt

# 2. переменные окружения (см. .env.example)
export GCP_PROJECT=...
export GOOGLE_APPLICATION_CREDENTIALS=/путь/к/key.json
export BQ_DATASET=kz_econ

# 3. (один раз / по расписанию) наполнить таблицу рядов
python data_pipeline/ingest.py --backfill-days 365

# 4. собрать артефакт: прогноз + прочие источники → kz_model_data.json
python3 kz_data_pipeline.py
```

Если `GCP_PROJECT` не задан или `google-cloud-bigquery` не установлен — шаг прогноза
**мягко пропускается** (база остаётся на последних значениях), пайплайн не падает.

## Какие ряды становятся базой

`fetch_bigquery_forecast()` берёт последнюю точку горизонта по ряду и, если значение
проходит проверку диапазона, кладёт его в базу симулятора:

| `series_id` в BigQuery | Поле в `kz_model_data.json` | Диапазон-страховка |
|---|---|---|
| `usdkzt` | `fx.usdkzt` (курс ₸/$) | 300–800 |
| `brent` | `prices.brent` (+ цена нефти в экспорте) | 20–200 |
| `inflation` | `macro.inflation` (%) | 0–40 |
| `gdp_growth` | `macro.growth` (%) | −10…15 |

Маппинг и страховки — в `kz_data_pipeline.py` (`BQ_SERIES_MAP`, `BQ_GUARDS`).
Прогноз вне диапазона не применяется, чтобы испорченный ряд не сломал базу.

## Расписание

Обновление по cron делает GitHub Actions: `.github/workflows/refresh-data.yml`
(ежедневно + ручной запуск). Ключ сервис-аккаунта — в секрете репозитория `GCP_SA_KEY`,
в код/артефакт не попадает.
