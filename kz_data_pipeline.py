#!/usr/bin/env python3
"""Пайплайн данных для симулятора экономики Казахстана.

Опрашивает открытые источники, нормализует значения и собирает единый
артефакт ``kz_model_data.json``, который читает дашборд.

Принцип устойчивости (FR-7): падение любого источника не ломает сборку —
для соответствующего поля подставляется последнее известное значение из
уже существующего ``kz_model_data.json`` (fallback).

Зависимости: только стандартная библиотека Python 3 (urllib, xml, json),
чтобы скрипт запускался без установки пакетов.

Ключи API передаются через переменные окружения:
    COMTRADE_KEY  — UN Comtrade (товарная структура экспорта/импорта)
    EIA_KEY       — EIA (мировые цены на энергоносители)
"""

import json
import os
import sys
import datetime
import urllib.request
import urllib.error
import xml.etree.ElementTree as ET

HERE = os.path.dirname(os.path.abspath(__file__))
DATA_FILE = os.path.join(HERE, "kz_model_data.json")
TIMEOUT = 20

# Базовые значения «по умолчанию» (PRD §7.1) — используются, если файла ещё нет.
DEFAULT_DATA = {
    "updated": "2026-06-04",
    "source": "defaults",
    "fx": {"usdkzt": 490.0},
    "macro": {
        "gdpNominal": 290.0, "growth": 5.5, "inflation": 11.0,
        "nfAssets": 60.0, "govResourceRevenue": 22.0, "nfTransfer": 17.0,
        "baseRate": 18.0, "fedRate": 3.75,
    },
    "prices": {"brent": 95.0, "uranium": 83.0, "copper": 6.0},
    "drivers": {"fdiUsdBn": 0.0, "laborForce": 0},
    "exports": [
        {"id": "oil", "label": "Нефть и нефтепродукты", "base": 39.9, "resource": True, "ref": "Brent ~$95", "price": 95.0, "unit": "барр."},
        {"id": "uranium", "label": "Уран", "base": 4.2, "resource": True, "ref": "U3O8 ~$83/lb", "price": 83.0, "unit": "фунт"},
        {"id": "copper", "label": "Медь (рафин. + руда)", "base": 6.95, "resource": True, "ref": "LME ~$9.3/кг", "price": 9.3, "unit": "кг"},
        {"id": "ferro", "label": "Ферросплавы", "base": 2.05, "resource": True, "ref": "Ферросилиций/хром ~$1.2/кг", "price": 1.2, "unit": "кг"},
        {"id": "zinclead", "label": "Цинк и свинец", "base": 1.6, "resource": True, "ref": "Zn/Pb ~$2.6/кг", "price": 2.6, "unit": "кг"},
        {"id": "grain", "label": "Зерно и масла", "base": 2.4, "resource": True, "ref": "Пшеница ~$230/т", "price": 0.23, "unit": "кг"},
        {"id": "coal", "label": "Уголь", "base": 2.0, "resource": True, "ref": "Энергоуголь ~$115/т", "price": 0.12, "unit": "кг"},
        {"id": "gas", "label": "Газ", "base": 2.5, "resource": True, "ref": "Газ ~$300/тыс. м³", "price": 0.3, "unit": "м³"},
        {"id": "other", "label": "Несырьевой экспорт", "base": 17.4, "resource": False, "ref": "остальное"},
    ],
    "imports": [
        {"id": "mach", "label": "Машины, оборуд., транспорт", "base": 26},
        {"id": "chem", "label": "Химия и пластмассы", "base": 10},
        {"id": "metal", "label": "Металлоизделия", "base": 8},
        {"id": "food", "label": "Продовольствие", "base": 7},
        {"id": "fuel", "label": "Нефтепродукты, топливо", "base": 6},
        {"id": "other", "label": "Прочий импорт", "base": 8},
    ],
    "coeffs": {
        "fxElasticity": -0.15, "fxFedCoeff": 0.06, "fxBaseRateCoeff": 0.05,
        "rentRate": 0.35, "resourceBase": 61.6,
        "gdpPriceCoeff": 0.04, "gdpFiscalCoeff": 0.7,
        "gdpInvest": 0.06, "gdpConsume": 0.06, "gdpExtDemand": 0.04,
        "gdpTfp": 0.05, "gdpLabor": 0.04, "gdpBaseRate": -0.15, "gdpFedRate": -0.10,
        "fxPass": 0.15, "demandPush": 0.5,
        "infConsume": 0.04, "infInvest": 0.02, "infBaseRate": -0.12, "infTfp": -0.03,
        "nfInvestYield": 0.05, "importIncomeElast": 1.5, "importFxElast": 0.3,
    },
}

# Коды HS для агрегирования экспорта (PRD §8) — задел для Comtrade.
HS_GROUPS = {
    "oil": ["2709", "2710"], "uranium": ["2844"],
    "copper": ["7402", "7403", "7404", "2603"], "ferro": ["7202"],
    "zinclead": ["7901", "7801", "2607", "2608"],
    "grain": ["1001", "1101", "1205", "1512"],
    "coal": ["2701"], "gas": ["2711"],
}

WB_INDICATORS = {
    "gdpNominal": "NY.GDP.MKTP.CD",   # ВВП, текущие USD
    "growth": "NY.GDP.MKTP.KD.ZG",    # рост ВВП, %
    "inflation": "FP.CPI.TOTL.ZG",    # инфляция CPI, %
}

# Драйверы роста (PRD v1.1 §8): FDI и рабочая сила -> блок data["drivers"].
WB_DRIVERS = {
    "fdiUsdBn": "BX.KLT.DINV.CD.WD",  # чистый приток ПИИ, текущие USD
    "laborForce": "SL.TLF.TOTL.IN",   # рабочая сила, всего (чел.)
}

OK, FAIL = [], []


def log_ok(name, msg):
    OK.append(name)
    print("  [ok]   %-22s %s" % (name, msg))


def log_fail(name, err):
    FAIL.append(name)
    print("  [fail] %-22s %s -> fallback на последнее значение" % (name, err))


def http_get(url, headers=None):
    req = urllib.request.Request(url, headers=headers or {"User-Agent": "kz-pipeline/1.0"})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        return r.read()


def load_existing():
    """Последний известный артефакт = база для fallback."""
    if os.path.exists(DATA_FILE):
        try:
            with open(DATA_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            pass
    return json.loads(json.dumps(DEFAULT_DATA))


# ---------------------------------------------------------------- источники

def fetch_fx(data):
    """Курс USD/KZT из RSS Нацбанка РК (XML)."""
    name = "НБ РК · курс"
    try:
        raw = http_get("https://nationalbank.kz/rss/rates.xml")
        root = ET.fromstring(raw)
        for item in root.iter("item"):
            title = (item.findtext("title") or "").upper()
            if "USD" in title:
                val = float((item.findtext("description") or "").replace(",", ".").strip())
                if val > 0:
                    data["fx"]["usdkzt"] = round(val, 2)
                    log_ok(name, "USD/KZT = %.2f" % val)
                    return
        raise ValueError("USD не найден в ленте")
    except Exception as e:  # noqa: BLE001 — любой сбой = fallback
        log_fail(name, repr(e))


def fetch_worldbank(data):
    """ВВП, рост, инфляция из World Bank API (JSON, без ключа)."""
    name = "World Bank · макро"
    got = []
    try:
        for key, code in WB_INDICATORS.items():
            url = ("https://api.worldbank.org/v2/country/KAZ/indicator/%s"
                   "?format=json&per_page=60" % code)
            payload = json.loads(http_get(url))
            series = payload[1] if isinstance(payload, list) and len(payload) > 1 else []
            latest = next((row["value"] for row in series if row.get("value") is not None), None)
            if latest is None:
                continue
            if key == "gdpNominal":
                data["macro"][key] = round(latest / 1e9, 1)  # USD -> млрд
            else:
                data["macro"][key] = round(latest, 1)
            got.append(key)
        if not got:
            raise ValueError("нет значений")
        log_ok(name, "обновлено: " + ", ".join(got))
    except Exception as e:  # noqa: BLE001
        log_fail(name, repr(e))


def fetch_wb_drivers(data):
    """ПИИ и рабочая сила из World Bank API -> data["drivers"]."""
    name = "World Bank · драйверы"
    got = []
    try:
        for key, code in WB_DRIVERS.items():
            url = ("https://api.worldbank.org/v2/country/KAZ/indicator/%s"
                   "?format=json&per_page=60" % code)
            payload = json.loads(http_get(url))
            series = payload[1] if isinstance(payload, list) and len(payload) > 1 else []
            latest = next((row["value"] for row in series if row.get("value") is not None), None)
            if latest is None:
                continue
            if key == "fdiUsdBn":
                data["drivers"][key] = round(latest / 1e9, 1)  # USD -> млрд
            else:
                data["drivers"][key] = int(round(latest))
            got.append(key)
        if not got:
            raise ValueError("нет значений")
        log_ok(name, "обновлено: " + ", ".join(got))
    except Exception as e:  # noqa: BLE001
        log_fail(name, repr(e))


def fetch_fred(data):
    """Верхняя граница целевого диапазона ставки ФРС из FRED (CSV, без ключа).

    Ряд DFEDTARU (Federal Funds Target Range — Upper Limit) отдаётся как CSV,
    поэтому ключ не нужен. Берём последнее непустое значение.
    """
    name = "FRED · ставка ФРС"
    try:
        raw = http_get("https://fred.stlouisfed.org/graph/fredgraph.csv?id=DFEDTARU")
        lines = raw.decode("utf-8", "ignore").strip().splitlines()
        val = None
        for line in reversed(lines[1:]):  # пропускаем заголовок
            parts = line.split(",")
            if len(parts) >= 2 and parts[1].strip() not in ("", "."):
                val = float(parts[1])
                break
        if val is None:
            raise ValueError("нет значений в ряду")
        data["macro"]["fedRate"] = round(val, 2)
        log_ok(name, "fedRate = %.2f" % val)
    except Exception as e:  # noqa: BLE001
        log_fail(name, repr(e))


COMTRADE_YEAR = 2024  # последний полный год


def fetch_comtrade(data):
    """Агрегирует товарный экспорт РК из UN Comtrade по группам HS_GROUPS.

    Защита калибровки: новые значения применяются только если они проходят
    проверку правдоподобия (итог и сырьевая часть в разумных границах, заполнено
    >= 4 групп). Иначе — fallback на текущие base. При успехе перекалибровывается
    ``resourceBase`` = сумма сырьевых групп, чтобы база модели (все idx=1)
    по-прежнему давала windfall = 0.
    """
    name = "UN Comtrade · торговля"
    key = os.environ.get("COMTRADE_KEY")
    if not key:
        log_fail(name, "COMTRADE_KEY не задан")
        return
    try:
        url = ("https://comtradeapi.un.org/data/v1/get/C/A/HS"
               "?reporterCode=398&flowCode=X&period=%d&cmdCode=AG4"
               "&subscription-key=%s" % (COMTRADE_YEAR, key))
        rows = json.loads(http_get(url)).get("data", [])
        if not rows:
            raise ValueError("пустой ответ по экспорту")

        group_val = {gid: 0.0 for gid in HS_GROUPS}
        total = 0.0
        for r in rows:
            code = str(r.get("cmdCode", ""))
            val = float(r.get("primaryValue") or 0)
            total += val
            for gid, prefixes in HS_GROUPS.items():
                if any(code.startswith(p) for p in prefixes):
                    group_val[gid] += val
                    break

        total_bn = total / 1e9
        resource_bn = sum(group_val.values()) / 1e9
        filled = sum(1 for v in group_val.values() if v > 0)

        # Проверка правдоподобия — иначе не трогаем калибровку.
        if not (30 <= total_bn <= 200 and 15 <= resource_bn <= 150 and filled >= 4):
            raise ValueError("значения вне диапазона: total=%.1f, res=%.1f, групп=%d"
                             % (total_bn, resource_bn, filled))

        for e in data["exports"]:
            if e["id"] in group_val and group_val[e["id"]] > 0:
                e["base"] = round(group_val[e["id"]] / 1e9, 2)
            elif e["id"] == "other":
                e["base"] = round(max(total_bn - resource_bn, 0.0), 2)
        data["coeffs"]["resourceBase"] = round(resource_bn, 1)
        log_ok(name, "экспорт агрегирован: всего $%.1f млрд, сырьё $%.1f млрд (%d групп)"
               % (total_bn, resource_bn, filled))
    except Exception as e:  # noqa: BLE001
        log_fail(name, repr(e))


def fetch_eia(data):
    """Цена Brent из EIA (нужен ключ)."""
    name = "EIA · цены"
    key = os.environ.get("EIA_KEY")
    if not key:
        log_fail(name, "EIA_KEY не задан")
        return
    try:
        url = ("https://api.eia.gov/v2/petroleum/pri/spt/data/"
               "?frequency=daily&data[0]=value"
               "&facets[product][]=EPCBRENT&sort[0][column]=period"
               "&sort[0][direction]=desc&length=1&api_key=%s" % key)
        payload = json.loads(http_get(url))
        rows = payload.get("response", {}).get("data", [])
        if rows:
            brent = round(float(rows[0]["value"]), 2)
            data["prices"]["brent"] = brent
            # Отражаем цену Brent и в карточке нефтяного экспорта (для UI цен сырья).
            for e in data["exports"]:
                if e["id"] == "oil":
                    e["price"] = brent
            log_ok(name, "Brent = %.2f" % brent)
        else:
            raise ValueError("пустой ответ")
    except Exception as e:  # noqa: BLE001
        log_fail(name, repr(e))


# ---------------------------------------------------------------------- main

def main():
    print("KZ data pipeline — сборка kz_model_data.json")
    data = load_existing()

    # Гарантируем наличие v1.1-ключей даже при загрузке старого артефакта.
    data.setdefault("drivers", {"fdiUsdBn": 0.0, "laborForce": 0})
    data["macro"].setdefault("baseRate", 18.0)
    data["macro"].setdefault("fedRate", 3.75)

    fetch_fx(data)
    fetch_worldbank(data)
    fetch_wb_drivers(data)
    fetch_fred(data)
    fetch_comtrade(data)
    fetch_eia(data)

    updated_any = len(OK) > 0
    data["updated"] = datetime.date.today().isoformat()
    data["source"] = ("live: %d/%d источников" % (len(OK), len(OK) + len(FAIL))
                      if updated_any else "fallback (источники недоступны)")

    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    print("\nИтог: обновлено %d, fallback %d. Записан %s"
          % (len(OK), len(FAIL), os.path.basename(DATA_FILE)))
    # Успех = собран валидный артефакт (даже если часть источников упала).
    return 0


if __name__ == "__main__":
    sys.exit(main())
