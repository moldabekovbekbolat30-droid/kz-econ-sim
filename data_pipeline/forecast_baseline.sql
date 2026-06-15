-- Прогноз базовых рядов экономики РК моделью TimesFM через BigQuery AI.FORECAST.
-- Источник — таблица ${BQ_DATASET}.series (наполняется ingest.py): колонки
--   series_id STRING, ts TIMESTAMP|DATE, value FLOAT64.
-- Возвращает по каждому ряду точки горизонта; пайплайн берёт последнюю как
-- форвардную базу симулятора (курс, инфляция, рост ВВП, Brent).
--
-- ВНИМАНИЕ (Задача 3): имена аргументов AI.FORECAST менялись между версиями
-- BigQuery ML. Если запрос падает на сигнатуре — сверьтесь с актуальной справкой
-- AI.FORECAST и поправьте имена (data_col / timestamp_col / id_cols / horizon).
-- Плейсхолдер ${BQ_DATASET} подставляется пайплайном из переменной окружения.

SELECT
  series_id,
  forecast_timestamp,
  forecast_value
FROM
  AI.FORECAST(
    TABLE `${BQ_DATASET}.series`,
    data_col        => 'value',
    timestamp_col   => 'ts',
    id_cols         => ['series_id'],
    horizon         => 12,
    confidence_level => 0.9
  )
ORDER BY series_id, forecast_timestamp;
