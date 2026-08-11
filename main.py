import json
import os
import random
import time
from datetime import datetime, timedelta

import psycopg2
import psycopg2.extras
import requests
from dotenv import load_dotenv

load_dotenv()

BASE_URL = "https://pass.rzd.ru/timetable/public/ru"
RAW_DIR = "rzd_prices_raw"

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) Gecko/20100101 Firefox/125.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.4 Safari/605.1.15",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36 Edg/124.0.0.0",
]

BASE_HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Referer": "https://pass.rzd.ru/",
}

ROUTES = [
    {"from": "2000000", "to": "2004000", "name": "Москва -> Санкт-Петербург"},
    {"from": "2000000", "to": "2060000", "name": "Москва -> Нижний Новгород"},
    {"from": "2000000", "to": "2060500", "name": "Москва -> Казань"},
    {"from": "2000000", "to": "2024000", "name": "Москва -> Самара"},
    {"from": "2000000", "to": "2064000", "name": "Москва -> Ростов-на-Дону"},
]

DAYS_AHEAD_LIST = [1, 3, 7, 14, 30]

MIN_DELAY = 4
MAX_DELAY = 9 
POLL_ATTEMPTS = 6
POLL_DELAY = 2

DB_CONFIG = {
    "host": os.environ.get("PGHOST", "localhost"),
    "port": os.environ.get("PGPORT", "5432"),
    "dbname": os.environ.get("PGDATABASE", "rzd_prices"),
    "user": os.environ.get("PGUSER", "postgres"),
    "password": os.environ.get("PGPASSWORD", ""),
}

CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS rzd_prices (
    id SERIAL PRIMARY KEY,
    fetched_at TIMESTAMP NOT NULL,
    days_ahead INTEGER NOT NULL,
    from_city TEXT,
    to_city TEXT,
    date TEXT,
    departure_time TEXT,
    arrival_time TEXT,
    duration TEXT,
    train_number TEXT,
    brand TEXT,
    carrier TEXT,
    car_type TEXT,
    price NUMERIC,
    free_seats INTEGER,
    is_firm BOOLEAN,
    is_electronic BOOLEAN,
    is_refundable BOOLEAN,
    food BOOLEAN,
    goods BOOLEAN,
    luggage BOOLEAN,
    hand_luggage BOOLEAN,
    disabled_access BOOLEAN
);
"""

INSERT_SQL = """
INSERT INTO rzd_prices (
    fetched_at, days_ahead, from_city, to_city, date, departure_time,
    arrival_time, duration, train_number, brand, carrier, car_type, price,
    free_seats, is_firm, is_electronic, is_refundable, food, goods,
    luggage, hand_luggage, disabled_access
) VALUES %s
"""

ROW_COLUMNS = [
    "fetched_at",
    "days_ahead",
    "from_city",
    "to_city",
    "date",
    "departure_time",
    "arrival_time",
    "duration",
    "train_number",
    "brand",
    "carrier",
    "car_type",
    "price",
    "free_seats",
    "is_firm",
    "is_electronic",
    "is_refundable",
    "food",
    "goods",
    "luggage",
    "hand_luggage",
    "disabled_access",
]


def warm_up_session(session: requests.Session) -> None:
    session.get(
        BASE_URL, params={"STRUCTURE_ID": 735}, headers=session.headers, timeout=15
    )


def fetch_route(session: requests.Session, route: dict, date_str: str) -> dict:
    params = {
        "layer_id": 5827,
        "dir": 0,
        "tfl": 3,
        "checkSeats": 1,
        "code0": route["from"],
        "code1": route["to"],
        "dt0": date_str,
        "dt1": date_str,
    }

    resp = session.get(BASE_URL, params=params, headers=session.headers, timeout=15)
    resp.raise_for_status()
    data = resp.json()

    rid = data.get("rid") or data.get("RID")
    if not rid:
        return data

    for _ in range(POLL_ATTEMPTS):
        time.sleep(POLL_DELAY)
        poll = session.get(
            BASE_URL,
            params={"layer_id": 5827, "rid": rid},
            headers=session.headers,
            timeout=15,
        )
        poll.raise_for_status()
        data = poll.json()
        if data.get("result") == "RID":
            continue
        break

    return data


def _row_from_car(tp_from, tp_to, train, car, fetched_at, days_ahead, is_refundable):
    return {
        "fetched_at": fetched_at,
        "days_ahead": days_ahead,
        "from_city": tp_from,
        "to_city": tp_to,
        "date": train.get("date0"),
        "departure_time": train.get("time0"),
        "arrival_time": train.get("time1"),
        "duration": train.get("timeInWay"),
        "train_number": train.get("number"),
        "brand": train.get("brand") or None,
        "carrier": train.get("carrier"),
        "car_type": car.get("type"),
        "price": car.get("tariff"),
        "free_seats": car.get("freeSeats"),
        "is_firm": bool(train.get("bFirm", False)),
        "is_electronic": bool(train.get("elReg", False)),
        "is_refundable": is_refundable,
        "food": bool(train.get("addFood", False)),
        "goods": bool(train.get("addGoods", False)),
        "luggage": bool(train.get("addCompLuggage", False)),
        "hand_luggage": bool(train.get("addHandLuggage", False)),
        "disabled_access": bool(
            car.get("disabledPerson", False) or train.get("disabledType", False)
        ),
    }


def parse_response(data: dict, fetched_at: str, days_ahead: int) -> list:
    """Parses the raw pass.rzd.ru response into a flat list of rows —
    one row per (train, car type/price) pair."""
    rows = []
    if data.get("result") != "OK":
        return rows

    for tp in data.get("tp", []):
        from_city = tp.get("from")
        to_city = tp.get("where")

        for train in tp.get("list", []):
            is_refundable = not bool(train.get("nonRefundable", False))

            for car in train.get("cars", []):
                rows.append(
                    _row_from_car(
                        from_city,
                        to_city,
                        train,
                        car,
                        fetched_at,
                        days_ahead,
                        is_refundable,
                    )
                )

            # For some "Lastochka" trains with seat selection, prices are in seatCars, not cars
            for car in train.get("seatCars", []):
                rows.append(
                    _row_from_car(
                        from_city,
                        to_city,
                        train,
                        car,
                        fetched_at,
                        days_ahead,
                        is_refundable,
                    )
                )

    return rows


def ensure_table(conn) -> None:
    with conn.cursor() as cur:
        cur.execute(CREATE_TABLE_SQL)
    conn.commit()


def insert_rows(conn, rows: list) -> None:
    if not rows:
        return
    values = [tuple(row[col] for col in ROW_COLUMNS) for row in rows]
    with conn.cursor() as cur:
        psycopg2.extras.execute_values(cur, INSERT_SQL, values)
    conn.commit()


def main():
    os.makedirs(RAW_DIR, exist_ok=True)
    fetched_at = datetime.now()
    fetched_at_str = fetched_at.strftime("%Y-%m-%d %H:%M:%S")

    conn = psycopg2.connect(**DB_CONFIG)
    ensure_table(conn)

    session = requests.Session()
    session.headers.update(BASE_HEADERS)
    session.headers["User-Agent"] = random.choice(USER_AGENTS)
    warm_up_session(session)

    raw_results_on_error = {}
    all_rows = []

    tasks = [(route, days_ahead) for days_ahead in DAYS_AHEAD_LIST for route in ROUTES]

    for i, (route, days_ahead) in enumerate(tasks):
        date_str = (fetched_at + timedelta(days=days_ahead)).strftime("%d.%m.%Y")
        label = f"{route['name']} (+{days_ahead}д, {date_str})"
        print(f"[{datetime.now()}] Запрашиваю: {label}")

        had_exception = False
        try:
            data = fetch_route(session, route, date_str)
        except requests.RequestException as e:
            print(f"  ОШИБКА: {e}")
            data = {"error": str(e)}
            had_exception = True

        rows = parse_response(data, fetched_at_str, days_ahead)
        all_rows.extend(rows)
        print(f"  Найдено вариантов цены: {len(rows)}")

        # Raw JSON is saved not every time, only when something looks like
        # a problem: a network error or zero prices found — either the day
        # is genuinely empty, or the parser/codes broke, and then it's
        # useful to see what RZD actually sent back.
        if had_exception or not rows:
            raw_results_on_error[label] = data

        if i < len(tasks) - 1:
            time.sleep(random.uniform(MIN_DELAY, MAX_DELAY))

    if raw_results_on_error:
        raw_path = os.path.join(
            RAW_DIR, fetched_at.strftime("%Y-%m-%dT%H-%M-%S") + ".json"
        )
        with open(raw_path, "w", encoding="utf-8") as f:
            json.dump(
                raw_results_on_error, f, ensure_ascii=False, indent=2, sort_keys=True
            )
        print(f"  Сохранён сырой JSON по проблемным направлениям: {raw_path}")
    else:
        raw_path = None

    insert_rows(conn, all_rows)
    conn.close()

    print(
        f"[{datetime.now()}] Готово. Записано строк в БД: {len(all_rows)}."
        f" Проблемных направлений: {len(raw_results_on_error)}."
    )


if __name__ == "__main__":
    main()
