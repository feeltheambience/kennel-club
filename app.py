# -*- coding: utf-8 -*-
"""Кинологический центр — учёт клуба и проведение выставок.

FastAPI + SQLite. Один процесс:
  * SPA-панель управления (index.html) — собаки, владельцы, взносы, мероприятия,
    справочники;
  * JSON API /api/* — CRUD;
  * печатные документы /print/* — каталог, ринговые ведомости, дипломы, отчёт
    (открываются отдельной страницей, печать Ctrl+P → PDF).

Вся площадка закрыта одним паролем (по умолчанию "dogs"), кука-сессия.

Порт 8315. Redeploy: python deploy_kennel.py
"""
import hashlib
import json
import os
import sqlite3
import time
from contextlib import closing
from datetime import date, datetime
from html import escape

from fastapi import FastAPI, Request, Form
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

HERE = os.path.dirname(os.path.abspath(__file__))
INDEX = os.path.join(HERE, "index.html")
DB = os.path.join(HERE, "kennel.db")

PASSWORD = os.environ.get("KENNEL_PASSWORD", "dogs")
SALT = "kennel-club-2026"
TOKEN = hashlib.sha256((PASSWORD + SALT).encode()).hexdigest()[:32]
COOKIE = "kennel_auth"

CLUB_NAME = os.environ.get("KENNEL_CLUB_NAME", "Кинологический центр")

app = FastAPI(title="Кинологический центр")


# ------------------------------------------------------------------ DB layer
def db():
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


SCHEMA = """
CREATE TABLE IF NOT EXISTS owners(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    full_name TEXT NOT NULL,
    member_no TEXT,
    address TEXT,
    phone TEXT,
    email TEXT,
    member_since TEXT,
    status TEXT DEFAULT 'active',
    notes TEXT,
    created REAL
);

CREATE TABLE IF NOT EXISTS dogs(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    breed_group TEXT,
    breed TEXT,
    breed_id INTEGER,
    color TEXT,
    name TEXT NOT NULL,
    pedigree_number TEXT,
    chip TEXT,
    tattoo TEXT,
    sex TEXT,
    dob TEXT,
    dam_pedigree TEXT,
    dam_name TEXT,
    sire_pedigree TEXT,
    sire_name TEXT,
    breeder TEXT,
    owner_id INTEGER REFERENCES owners(id) ON DELETE SET NULL,
    notes TEXT,
    created REAL
);

CREATE TABLE IF NOT EXISTS payments(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_id INTEGER REFERENCES owners(id) ON DELETE CASCADE,
    dog_id INTEGER REFERENCES dogs(id) ON DELETE SET NULL,
    event_id INTEGER REFERENCES events(id) ON DELETE SET NULL,
    category TEXT,          -- membership | registration | show_entry | other
    amount REAL DEFAULT 0,
    pay_date TEXT,
    period TEXT,            -- напр. год для членских
    status TEXT DEFAULT 'paid',  -- paid | debt
    note TEXT,
    created REAL
);

CREATE TABLE IF NOT EXISTS events(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    type TEXT DEFAULT 'show',   -- show | test | competition | other
    rank TEXT,
    rank_id INTEGER,
    event_date TEXT,
    place TEXT,
    judges TEXT,
    status TEXT DEFAULT 'draft', -- draft | reg_open | reg_closed | judging | finished
    description TEXT,
    created REAL
);

CREATE TABLE IF NOT EXISTS entries(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id INTEGER REFERENCES events(id) ON DELETE CASCADE,
    dog_id INTEGER REFERENCES dogs(id) ON DELETE CASCADE,
    class_name TEXT,
    catalog_no INTEGER,
    grade TEXT,
    placement INTEGER,
    titles TEXT,
    remark TEXT,
    created REAL
);

CREATE TABLE IF NOT EXISTS refs(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT,   -- classes | grades | titles
    name TEXT,
    sort INTEGER DEFAULT 0
);

-- дерево пород: группы FCI → породы
CREATE TABLE IF NOT EXISTS breed_groups(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT,
    sort INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS breeds(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    group_id INTEGER REFERENCES breed_groups(id) ON DELETE CASCADE,
    name TEXT,
    sort INTEGER DEFAULT 0
);

-- общий справочник адресов (владельцы + места проведения)
CREATE TABLE IF NOT EXISTS addresses(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT,       -- метка: «Клуб Олимп», «г. Тверь» и т.п.
    address TEXT,
    phone TEXT,
    note TEXT,
    sort INTEGER DEFAULT 0
);

-- ранги выставок: у каждого — набор доступных оценок и титулов (JSON-списки имён;
-- пусто = «все»). От ранга зависит, что предлагается в экспертизе и печатается.
CREATE TABLE IF NOT EXISTS ranks(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT,
    grades TEXT DEFAULT '[]',
    titles TEXT DEFAULT '[]',
    sort INTEGER DEFAULT 0
);
"""

# Стандартные справочники РКФ/FCI — засеваются один раз, затем редактируются в UI.
SEED_REFS = {
    "classes": [
        "Беби", "Щенков", "Юниоров", "Промежуточный", "Открытый",
        "Рабочий", "Чемпионов", "Победителей", "Ветеранов",
    ],
    "grades": [
        "Отлично", "Очень хорошо", "Хорошо", "Удовлетворительно",
        "Без оценки", "Дисквалификация",
        # для беби/щенков:
        "Очень перспективный", "Перспективный", "Неперспективный",
    ],
    "titles": [
        "CW (Победитель класса)", "ЮCAC", "CAC", "R.CAC", "CACIB", "R.CACIB",
        "ЮЛПП", "ЛПП (BOB)", "BOS (ЛПП прот. пола)", "ЛПК", "BIG", "BIS",
        "ЮПК", "КЧК", "ПК", "Кандидат в чемпионы",
    ],
}

# Дерево пород — 10 групп FCI с типовым набором пород (заказчик заменит загрузкой файлов).
SEED_BREEDS = {
    "FCI I — Овчарки и скотогонные (кроме швейцарских)": [
        "Немецкая овчарка", "Бельгийская овчарка (малинуа)", "Бордер-колли",
        "Вельш-корги пемброк", "Шелти", "Босерон", "Австралийская овчарка"],
    "FCI II — Пинчеры, шнауцеры, молоссы, швейцарские скотогонные": [
        "Доберман", "Ротвейлер", "Боксёр", "Немецкий дог", "Ризеншнауцер",
        "Цвергшнауцер", "Кане корсо", "Бульмастиф", "Бернский зенненхунд",
        "Среднеазиатская овчарка", "Кавказская овчарка"],
    "FCI III — Терьеры": [
        "Джек-рассел-терьер", "Йоркширский терьер", "Стаффордширский бультерьер",
        "Американский стаффордширский терьер", "Вест-хайленд-уайт-терьер", "Бультерьер"],
    "FCI IV — Таксы": [
        "Такса стандартная", "Такса миниатюрная", "Такса кроличья"],
    "FCI V — Шпицы и примитивные": [
        "Сибирский хаски", "Аляскинский маламут", "Самоедская собака",
        "Померанский шпиц", "Акита-ину", "Сиба-ину", "Чау-чау", "Басенджи"],
    "FCI VI — Гончие и родственные породы": [
        "Бигль", "Бассет-хаунд", "Родезийский риджбек", "Далматин"],
    "FCI VII — Легавые": [
        "Английский сеттер", "Ирландский сеттер", "Пойнтер", "Веймаранер", "Курцхаар"],
    "FCI VIII — Ретриверы, спаниели, водяные собаки": [
        "Лабрадор-ретривер", "Голден-ретривер", "Английский кокер-спаниель",
        "Американский кокер-спаниель", "Английский спрингер-спаниель"],
    "FCI IX — Декоративные и собаки-компаньоны": [
        "Французский бульдог", "Мопс", "Чихуахуа", "Пудель", "Ши-тцу",
        "Мальтезе", "Пекинес", "Кавалер-кинг-чарльз-спаниель", "Бишон фризе"],
    "FCI X — Борзые": [
        "Русская псовая борзая", "Грейхаунд", "Уиппет", "Афганская борзая", "Салюки"],
}

# Ранги выставок: имя + доступные титулы (оценки по умолчанию — все).
SEED_RANKS = [
    ("САС (сертификатная)", ["CW (Победитель класса)", "ЮCAC", "CAC", "R.CAC",
                             "ЛПП (BOB)", "BOS (ЛПП прот. пола)", "ЛПК"]),
    ("CACIB (интернациональная)", ["CW (Победитель класса)", "ЮCAC", "CAC", "R.CAC",
                                    "CACIB", "R.CACIB", "ЛПП (BOB)", "BOS (ЛПП прот. пола)",
                                    "ЛПК", "BIG", "BIS"]),
    ("КЧК (кандидат в чемпионы клуба)", ["CW (Победитель класса)", "КЧК",
                                         "ЛПП (BOB)", "BOS (ЛПП прот. пола)"]),
    ("ПК (победитель клуба)", ["CW (Победитель класса)", "ПК",
                               "ЛПП (BOB)", "BOS (ЛПП прот. пола)"]),
    ("Монопородная", ["CW (Победитель класса)", "ЛПП (BOB)", "BOS (ЛПП прот. пола)"]),
    ("Региональная", ["CW (Победитель класса)", "CAC", "R.CAC", "ЛПП (BOB)"]),
    ("Национальная", ["CW (Победитель класса)", "CAC", "R.CAC", "ЛПП (BOB)", "BOS (ЛПП прот. пола)"]),
    ("Без сертификатов (оценочная)", []),
]


def init_db():
    with closing(db()) as c:
        c.executescript(SCHEMA)
        # миграция: добить недостающие столбцы у существующих БД
        migrations = {
            "owners": {"member_no": "TEXT", "status": "TEXT", "notes": "TEXT"},
            "dogs": {"dob": "TEXT", "notes": "TEXT", "breed_id": "INTEGER"},
            "events": {"type": "TEXT", "description": "TEXT", "rank_id": "INTEGER"},
            "entries": {"titles": "TEXT", "remark": "TEXT", "placement": "INTEGER"},
        }
        for tbl, cols in migrations.items():
            have = {r["name"] for r in c.execute(f"PRAGMA table_info({tbl})")}
            for col, ddl in cols.items():
                if col not in have:
                    try:
                        c.execute(f"ALTER TABLE {tbl} ADD COLUMN {col} {ddl}")
                    except sqlite3.OperationalError:
                        pass
        # засев простых справочников (classes/grades/titles)
        if c.execute("SELECT COUNT(*) FROM refs").fetchone()[0] == 0:
            for kind, names in SEED_REFS.items():
                for i, n in enumerate(names):
                    c.execute("INSERT INTO refs(kind,name,sort) VALUES(?,?,?)", (kind, n, i))
        # чистим устаревший kind='ranks' из refs (ранги переехали в свою таблицу)
        c.execute("DELETE FROM refs WHERE kind='ranks'")
        # засев дерева пород
        if c.execute("SELECT COUNT(*) FROM breed_groups").fetchone()[0] == 0:
            for gi, (gname, breeds) in enumerate(SEED_BREEDS.items()):
                cur = c.execute("INSERT INTO breed_groups(name,sort) VALUES(?,?)", (gname, gi))
                gid = cur.lastrowid
                for bi, b in enumerate(breeds):
                    c.execute("INSERT INTO breeds(group_id,name,sort) VALUES(?,?,?)", (gid, b, bi))
        # засев рангов
        if c.execute("SELECT COUNT(*) FROM ranks").fetchone()[0] == 0:
            for ri, (rname, titles) in enumerate(SEED_RANKS):
                c.execute("INSERT INTO ranks(name,grades,titles,sort) VALUES(?,?,?,?)",
                          (rname, "[]", json.dumps(titles, ensure_ascii=False), ri))
        c.commit()


init_db()


# ------------------------------------------------------------------ auth
def authed(request: Request) -> bool:
    return request.cookies.get(COOKIE) == TOKEN


@app.middleware("http")
async def gate(request: Request, call_next):
    path = request.url.path
    public = (
        path in ("/login", "/health", "/favicon.ico")
        or path.startswith("/static")
    )
    if not public and not authed(request):
        if path.startswith("/api/"):
            return JSONResponse({"error": "auth"}, status_code=401)
        return RedirectResponse("/login", status_code=302)
    return await call_next(request)


LOGIN_HTML = """<!doctype html><html lang=ru><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>Вход — Кинологический центр</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Playfair+Display:wght@600;700&family=Golos+Text:wght@400;500;600&display=swap" rel="stylesheet">
<style>
:root{color-scheme:light}
*{box-sizing:border-box}
body{margin:0;font-family:'Golos Text',-apple-system,Segoe UI,Roboto,sans-serif;
  background:
    radial-gradient(900px 500px at 100% -10%, #efe7d4 0%, transparent 58%),
    radial-gradient(800px 460px at -10% 110%, #e8efe4 0%, transparent 55%),
    #f4f0e8;
  min-height:100vh;display:flex;align-items:center;justify-content:center;color:#2b2722;padding:18px}
.card{position:relative;background:#fffdf8;border:1px solid #e8e1d2;border-radius:22px;
  box-shadow:0 24px 60px rgba(50,40,22,.16);padding:40px 32px 34px;width:min(94vw,372px);text-align:center;overflow:hidden}
.card::before{content:"";position:absolute;left:0;right:0;top:0;height:4px;
  background:linear-gradient(90deg,#2f6b4f,#c39a44,#b0803c)}
.logo{width:64px;height:70px;margin:0 auto 4px;display:block;filter:drop-shadow(0 3px 5px rgba(70,54,30,.2))}
h1{font-family:'Playfair Display',Georgia,serif;font-size:23px;margin:8px 0 2px;font-weight:700;letter-spacing:.2px}
p{color:#8a8374;font-size:13.5px;margin:0 0 22px}
input{width:100%;padding:13px 15px;border:1px solid #e0d8c7;border-radius:12px;
  font-size:16px;margin-bottom:12px;outline:none;background:#fff;font-family:inherit;transition:.15s}
input:focus{border-color:#2f6b4f;box-shadow:0 0 0 3px rgba(47,107,79,.14)}
button{width:100%;padding:13px;border:0;border-radius:12px;background:#2f6b4f;
  color:#fff;font-size:15px;font-weight:600;cursor:pointer;font-family:inherit;
  box-shadow:0 3px 8px rgba(47,107,79,.3);transition:.15s}
button:hover{background:#255a42}
.err{color:#b23a48;font-size:13px;min-height:18px;margin-bottom:8px;font-weight:500}
</style></head><body>
<form class=card method=post action=/login>
  <svg class=logo viewBox="0 0 40 44" aria-hidden="true">
    <path d="M14 29 L11 43 L17.5 38.5 L20 44 L20 29 Z" fill="#b0803c"/>
    <path d="M26 29 L29 43 L22.5 38.5 L20 44 L20 29 Z" fill="#9a6d2f"/>
    <circle cx="20" cy="17" r="15" fill="#2f6b4f"/>
    <circle cx="20" cy="17" r="15" fill="none" stroke="#c39a44" stroke-width="1.2" stroke-dasharray="2.2 2.2"/>
    <circle cx="20" cy="17" r="9.4" fill="#f4f0e8"/>
    <g fill="#2f6b4f"><ellipse cx="20" cy="19.2" rx="3.3" ry="2.7"/>
      <circle cx="15.8" cy="15.2" r="1.5"/><circle cx="20" cy="13.7" r="1.6"/><circle cx="24.2" cy="15.2" r="1.5"/></g>
  </svg>
  <h1>Кинологический центр</h1>
  <p>Учёт клуба и проведение выставок</p>
  <div class=err>__ERR__</div>
  <input type=password name=password placeholder="Пароль" autofocus>
  <button type=submit>Войти</button>
</form></body></html>"""


@app.get("/login", response_class=HTMLResponse)
def login_page():
    return LOGIN_HTML.replace("__ERR__", "")


@app.post("/login")
def login(password: str = Form("")):
    if password == PASSWORD:
        r = RedirectResponse("/", status_code=302)
        r.set_cookie(COOKIE, TOKEN, max_age=60 * 60 * 24 * 30, httponly=True, samesite="lax")
        return r
    return HTMLResponse(LOGIN_HTML.replace("__ERR__", "Неверный пароль"), status_code=401)


@app.get("/logout")
def logout():
    r = RedirectResponse("/login", status_code=302)
    r.delete_cookie(COOKIE)
    return r


@app.get("/health")
def health():
    return {"ok": True}


@app.get("/", response_class=HTMLResponse)
def index():
    with open(INDEX, encoding="utf-8") as f:
        return f.read()


# ------------------------------------------------------------------ helpers
def row(r):
    return dict(r) if r else None


def rows(rs):
    return [dict(r) for r in rs]


async def body(request: Request) -> dict:
    try:
        return await request.json()
    except Exception:
        return {}


# =====================================================================
#  REFS (справочники)
# =====================================================================
REF_KINDS = ("classes", "grades", "titles")


def get_refs():
    with closing(db()) as c:
        out = {k: [] for k in REF_KINDS}
        for r in c.execute("SELECT * FROM refs ORDER BY kind, sort, id"):
            out.setdefault(r["kind"], []).append({"id": r["id"], "name": r["name"]})
        return out


@app.get("/api/refs")
def api_refs():
    return get_refs()


@app.post("/api/refs/{kind}")
async def add_ref(kind: str, request: Request):
    if kind not in REF_KINDS:
        return JSONResponse({"error": "bad kind"}, status_code=400)
    d = await body(request)
    name = (d.get("name") or "").strip()
    if not name:
        return JSONResponse({"error": "empty"}, status_code=400)
    with closing(db()) as c:
        mx = c.execute("SELECT COALESCE(MAX(sort),0)+1 FROM refs WHERE kind=?", (kind,)).fetchone()[0]
        cur = c.execute("INSERT INTO refs(kind,name,sort) VALUES(?,?,?)", (kind, name, mx))
        c.commit()
        return {"id": cur.lastrowid, "name": name}


@app.delete("/api/refs/{kind}/{rid}")
def del_ref(kind: str, rid: int):
    with closing(db()) as c:
        c.execute("DELETE FROM refs WHERE id=? AND kind=?", (rid, kind))
        c.commit()
    return {"ok": True}


@app.post("/api/refs/{kind}/bulk")
async def bulk_ref(kind: str, request: Request):
    """Массовое добавление списком (по строке на значение). Дубли пропускаются."""
    if kind not in REF_KINDS:
        return JSONResponse({"error": "bad kind"}, status_code=400)
    d = await body(request)
    names = [x.strip() for x in (d.get("text") or "").splitlines() if x.strip()]
    added = 0
    with closing(db()) as c:
        have = {r["name"] for r in c.execute("SELECT name FROM refs WHERE kind=?", (kind,))}
        mx = c.execute("SELECT COALESCE(MAX(sort),0) FROM refs WHERE kind=?", (kind,)).fetchone()[0]
        for n in names:
            if n in have:
                continue
            mx += 1
            c.execute("INSERT INTO refs(kind,name,sort) VALUES(?,?,?)", (kind, n, mx))
            have.add(n); added += 1
        c.commit()
    return {"ok": True, "added": added}


# =====================================================================
#  BREEDS (дерево: группы FCI → породы)
# =====================================================================
@app.get("/api/breeds")
def api_breeds():
    with closing(db()) as c:
        groups = rows(c.execute("SELECT * FROM breed_groups ORDER BY sort, id"))
        by_g = {}
        for b in c.execute("SELECT * FROM breeds ORDER BY name COLLATE NOCASE"):
            by_g.setdefault(b["group_id"], []).append({"id": b["id"], "name": b["name"]})
        for g in groups:
            g["breeds"] = by_g.get(g["id"], [])
        return groups


@app.post("/api/breed-groups")
async def save_breed_group(request: Request):
    d = await body(request)
    name = (d.get("name") or "").strip()
    if not name:
        return JSONResponse({"error": "empty"}, status_code=400)
    with closing(db()) as c:
        if d.get("id"):
            c.execute("UPDATE breed_groups SET name=? WHERE id=?", (name, d["id"]))
            gid = d["id"]
        else:
            mx = c.execute("SELECT COALESCE(MAX(sort),0)+1 FROM breed_groups").fetchone()[0]
            gid = c.execute("INSERT INTO breed_groups(name,sort) VALUES(?,?)", (name, mx)).lastrowid
        c.commit()
    return {"id": gid, "name": name}


@app.delete("/api/breed-groups/{gid}")
def del_breed_group(gid: int):
    with closing(db()) as c:
        c.execute("DELETE FROM breeds WHERE group_id=?", (gid,))
        c.execute("DELETE FROM breed_groups WHERE id=?", (gid,))
        c.commit()
    return {"ok": True}


@app.post("/api/breeds")
async def save_breed(request: Request):
    d = await body(request)
    name = (d.get("name") or "").strip()
    gid = d.get("group_id")
    if not name or not gid:
        return JSONResponse({"error": "нужны название и группа"}, status_code=400)
    with closing(db()) as c:
        if d.get("id"):
            c.execute("UPDATE breeds SET name=?, group_id=? WHERE id=?", (name, gid, d["id"]))
            bid = d["id"]
        else:
            bid = c.execute("INSERT INTO breeds(group_id,name) VALUES(?,?)", (gid, name)).lastrowid
        c.commit()
    return {"id": bid, "name": name}


@app.delete("/api/breeds/{bid}")
def del_breed(bid: int):
    with closing(db()) as c:
        c.execute("DELETE FROM breeds WHERE id=?", (bid,))
        c.commit()
    return {"ok": True}


@app.post("/api/breeds/bulk")
async def bulk_breeds(request: Request):
    """Добавить породы списком в группу (строка = порода)."""
    d = await body(request)
    gid = d.get("group_id")
    names = [x.strip() for x in (d.get("text") or "").splitlines() if x.strip()]
    if not gid:
        return JSONResponse({"error": "нет группы"}, status_code=400)
    added = 0
    with closing(db()) as c:
        have = {r["name"] for r in c.execute("SELECT name FROM breeds WHERE group_id=?", (gid,))}
        for n in names:
            if n in have:
                continue
            c.execute("INSERT INTO breeds(group_id,name) VALUES(?,?)", (gid, n))
            have.add(n); added += 1
        c.commit()
    return {"ok": True, "added": added}


# =====================================================================
#  ADDRESSES (общий справочник адресов)
# =====================================================================
ADDR_FIELDS = ["name", "address", "phone", "note"]


@app.get("/api/addresses")
def api_addresses():
    with closing(db()) as c:
        return rows(c.execute("SELECT * FROM addresses ORDER BY name COLLATE NOCASE, id"))


@app.post("/api/addresses")
async def save_address(request: Request):
    d = await body(request)
    vals = {f: (d.get(f) or "").strip() for f in ADDR_FIELDS}
    if not vals["address"] and not vals["name"]:
        return JSONResponse({"error": "пустой адрес"}, status_code=400)
    with closing(db()) as c:
        if d.get("id"):
            sets = ",".join(f"{f}=?" for f in ADDR_FIELDS)
            c.execute(f"UPDATE addresses SET {sets} WHERE id=?",
                      [vals[f] for f in ADDR_FIELDS] + [d["id"]])
            aid = d["id"]
        else:
            cols = ",".join(ADDR_FIELDS)
            ph = ",".join("?" for _ in ADDR_FIELDS)
            aid = c.execute(f"INSERT INTO addresses({cols}) VALUES({ph})",
                            [vals[f] for f in ADDR_FIELDS]).lastrowid
        c.commit()
    return {"id": aid}


@app.delete("/api/addresses/{aid}")
def del_address(aid: int):
    with closing(db()) as c:
        c.execute("DELETE FROM addresses WHERE id=?", (aid,))
        c.commit()
    return {"ok": True}


# =====================================================================
#  RANKS (ранги выставок: имя + доступные оценки и титулы)
# =====================================================================
def _json_list(s):
    try:
        v = json.loads(s or "[]")
        return v if isinstance(v, list) else []
    except Exception:
        return []


@app.get("/api/ranks")
def api_ranks():
    with closing(db()) as c:
        out = []
        for r in c.execute("SELECT * FROM ranks ORDER BY sort, id"):
            out.append({"id": r["id"], "name": r["name"],
                        "grades": _json_list(r["grades"]), "titles": _json_list(r["titles"])})
        return out


@app.post("/api/ranks")
async def save_rank(request: Request):
    d = await body(request)
    name = (d.get("name") or "").strip()
    if not name:
        return JSONResponse({"error": "empty"}, status_code=400)
    grades = json.dumps([x for x in (d.get("grades") or []) if x], ensure_ascii=False)
    titles = json.dumps([x for x in (d.get("titles") or []) if x], ensure_ascii=False)
    with closing(db()) as c:
        if d.get("id"):
            c.execute("UPDATE ranks SET name=?, grades=?, titles=? WHERE id=?",
                      (name, grades, titles, d["id"]))
            rid = d["id"]
        else:
            mx = c.execute("SELECT COALESCE(MAX(sort),0)+1 FROM ranks").fetchone()[0]
            rid = c.execute("INSERT INTO ranks(name,grades,titles,sort) VALUES(?,?,?,?)",
                            (name, grades, titles, mx)).lastrowid
        c.commit()
    return {"id": rid}


@app.delete("/api/ranks/{rid}")
def del_rank(rid: int):
    with closing(db()) as c:
        c.execute("DELETE FROM ranks WHERE id=?", (rid,))
        c.commit()
    return {"ok": True}


def rank_sets(c, rank_id):
    """Вернуть (grades, titles) для ранга; пустые списки = «все» (None-заглушка не нужна)."""
    if not rank_id:
        return [], []
    r = c.execute("SELECT grades,titles FROM ranks WHERE id=?", (rank_id,)).fetchone()
    if not r:
        return [], []
    return _json_list(r["grades"]), _json_list(r["titles"])


# =====================================================================
#  OWNERS (владельцы / члены клуба)
# =====================================================================
OWNER_FIELDS = ["full_name", "member_no", "address", "phone", "email",
                "member_since", "status", "notes"]


@app.get("/api/owners")
def list_owners(q: str = ""):
    with closing(db()) as c:
        sql = """SELECT o.*,
                    (SELECT COUNT(*) FROM dogs d WHERE d.owner_id=o.id) AS dogs_count
                 FROM owners o"""
        args = []
        if q:
            sql += " WHERE o.full_name LIKE ? OR o.phone LIKE ? OR o.member_no LIKE ?"
            args = [f"%{q}%"] * 3
        sql += " ORDER BY o.full_name COLLATE NOCASE"
        return rows(c.execute(sql, args))


@app.post("/api/owners")
async def save_owner(request: Request):
    d = await body(request)
    vals = {f: (d.get(f) or "").strip() if isinstance(d.get(f), str) else d.get(f) for f in OWNER_FIELDS}
    if not vals["full_name"]:
        return JSONResponse({"error": "ФИО обязательно"}, status_code=400)
    with closing(db()) as c:
        if d.get("id"):
            sets = ",".join(f"{f}=?" for f in OWNER_FIELDS)
            c.execute(f"UPDATE owners SET {sets} WHERE id=?",
                      [vals[f] for f in OWNER_FIELDS] + [d["id"]])
            oid = d["id"]
        else:
            cols = ",".join(OWNER_FIELDS)
            ph = ",".join("?" for _ in OWNER_FIELDS)
            cur = c.execute(f"INSERT INTO owners({cols},created) VALUES({ph},?)",
                            [vals[f] for f in OWNER_FIELDS] + [time.time()])
            oid = cur.lastrowid
        c.commit()
    return {"id": oid}


@app.delete("/api/owners/{oid}")
def del_owner(oid: int):
    with closing(db()) as c:
        c.execute("DELETE FROM owners WHERE id=?", (oid,))
        c.commit()
    return {"ok": True}


# =====================================================================
#  DOGS (собаки)
# =====================================================================
DOG_FIELDS = ["breed_group", "breed", "breed_id", "color", "name", "pedigree_number",
              "chip", "tattoo", "sex", "dob", "dam_pedigree", "dam_name", "sire_pedigree",
              "sire_name", "breeder", "owner_id", "notes"]


@app.get("/api/dogs")
def list_dogs(q: str = "", owner_id: int = 0):
    with closing(db()) as c:
        sql = """SELECT d.*, o.full_name AS owner_name, o.phone AS owner_phone,
                        o.email AS owner_email, o.address AS owner_address
                 FROM dogs d LEFT JOIN owners o ON o.id=d.owner_id"""
        cond, args = [], []
        if q:
            cond.append("(d.name LIKE ? OR d.breed LIKE ? OR d.pedigree_number LIKE ? "
                        "OR d.chip LIKE ? OR d.tattoo LIKE ?)")
            args += [f"%{q}%"] * 5
        if owner_id:
            cond.append("d.owner_id=?")
            args.append(owner_id)
        if cond:
            sql += " WHERE " + " AND ".join(cond)
        sql += " ORDER BY d.breed COLLATE NOCASE, d.name COLLATE NOCASE"
        return rows(c.execute(sql, args))


@app.get("/api/dogs/{did}")
def get_dog(did: int):
    with closing(db()) as c:
        d = row(c.execute("SELECT * FROM dogs WHERE id=?", (did,)).fetchone())
        if not d:
            return JSONResponse({"error": "not found"}, status_code=404)
        d["events"] = rows(c.execute(
            """SELECT e.name, e.event_date, e.rank, en.class_name, en.grade,
                      en.placement, en.titles
               FROM entries en JOIN events e ON e.id=en.event_id
               WHERE en.dog_id=? ORDER BY e.event_date DESC""", (did,)))
        return d


@app.post("/api/dogs")
async def save_dog(request: Request):
    d = await body(request)
    vals = {}
    for f in DOG_FIELDS:
        v = d.get(f)
        vals[f] = v.strip() if isinstance(v, str) else v
    if not vals["name"]:
        return JSONResponse({"error": "Кличка обязательна"}, status_code=400)
    if not vals.get("owner_id"):
        vals["owner_id"] = None
    with closing(db()) as c:
        # если выбрана порода из дерева — подтягиваем текст породы и группы (снимок)
        if vals.get("breed_id"):
            b = c.execute("""SELECT b.name AS bn, g.name AS gn
                             FROM breeds b JOIN breed_groups g ON g.id=b.group_id
                             WHERE b.id=?""", (vals["breed_id"],)).fetchone()
            if b:
                vals["breed"] = b["bn"]
                vals["breed_group"] = b["gn"]
        else:
            vals["breed_id"] = None
        if d.get("id"):
            sets = ",".join(f"{f}=?" for f in DOG_FIELDS)
            c.execute(f"UPDATE dogs SET {sets} WHERE id=?",
                      [vals[f] for f in DOG_FIELDS] + [d["id"]])
            did = d["id"]
        else:
            cols = ",".join(DOG_FIELDS)
            ph = ",".join("?" for _ in DOG_FIELDS)
            cur = c.execute(f"INSERT INTO dogs({cols},created) VALUES({ph},?)",
                            [vals[f] for f in DOG_FIELDS] + [time.time()])
            did = cur.lastrowid
        c.commit()
    return {"id": did}


@app.delete("/api/dogs/{did}")
def del_dog(did: int):
    with closing(db()) as c:
        c.execute("DELETE FROM dogs WHERE id=?", (did,))
        c.commit()
    return {"ok": True}


# =====================================================================
#  PAYMENTS (взносы)
# =====================================================================
PAY_FIELDS = ["owner_id", "dog_id", "event_id", "category", "amount",
              "pay_date", "period", "status", "note"]


@app.get("/api/payments")
def list_payments(owner_id: int = 0, category: str = "", status: str = ""):
    with closing(db()) as c:
        sql = """SELECT p.*, o.full_name AS owner_name, d.name AS dog_name,
                        e.name AS event_name
                 FROM payments p
                 LEFT JOIN owners o ON o.id=p.owner_id
                 LEFT JOIN dogs d ON d.id=p.dog_id
                 LEFT JOIN events e ON e.id=p.event_id"""
        cond, args = [], []
        if owner_id:
            cond.append("p.owner_id=?"); args.append(owner_id)
        if category:
            cond.append("p.category=?"); args.append(category)
        if status:
            cond.append("p.status=?"); args.append(status)
        if cond:
            sql += " WHERE " + " AND ".join(cond)
        sql += " ORDER BY p.pay_date DESC, p.id DESC"
        data = rows(c.execute(sql, args))
        tot = c.execute("SELECT COALESCE(SUM(amount),0) FROM payments WHERE status='paid'").fetchone()[0]
        debt = c.execute("SELECT COALESCE(SUM(amount),0) FROM payments WHERE status='debt'").fetchone()[0]
        return {"items": data, "total_paid": tot, "total_debt": debt}


@app.post("/api/payments")
async def save_payment(request: Request):
    d = await body(request)
    vals = {}
    for f in PAY_FIELDS:
        v = d.get(f)
        vals[f] = v.strip() if isinstance(v, str) else v
    for k in ("owner_id", "dog_id", "event_id"):
        if not vals.get(k):
            vals[k] = None
    try:
        vals["amount"] = float(vals.get("amount") or 0)
    except (TypeError, ValueError):
        vals["amount"] = 0
    with closing(db()) as c:
        if d.get("id"):
            sets = ",".join(f"{f}=?" for f in PAY_FIELDS)
            c.execute(f"UPDATE payments SET {sets} WHERE id=?",
                      [vals[f] for f in PAY_FIELDS] + [d["id"]])
            pid = d["id"]
        else:
            cols = ",".join(PAY_FIELDS)
            ph = ",".join("?" for _ in PAY_FIELDS)
            cur = c.execute(f"INSERT INTO payments({cols},created) VALUES({ph},?)",
                            [vals[f] for f in PAY_FIELDS] + [time.time()])
            pid = cur.lastrowid
        c.commit()
    return {"id": pid}


@app.delete("/api/payments/{pid}")
def del_payment(pid: int):
    with closing(db()) as c:
        c.execute("DELETE FROM payments WHERE id=?", (pid,))
        c.commit()
    return {"ok": True}


# =====================================================================
#  EVENTS (мероприятия / выставки)
# =====================================================================
EVENT_FIELDS = ["name", "type", "rank", "rank_id", "event_date", "place", "judges",
                "status", "description"]

STATUS_FLOW = ["draft", "reg_open", "reg_closed", "judging", "finished"]


@app.get("/api/events")
def list_events():
    with closing(db()) as c:
        sql = """SELECT e.*,
                    (SELECT COUNT(*) FROM entries en WHERE en.event_id=e.id) AS entries_count
                 FROM events e ORDER BY e.event_date DESC, e.id DESC"""
        return rows(c.execute(sql))


@app.get("/api/events/{eid}")
def get_event(eid: int):
    with closing(db()) as c:
        e = row(c.execute("SELECT * FROM events WHERE id=?", (eid,)).fetchone())
        if not e:
            return JSONResponse({"error": "not found"}, status_code=404)
        e["entries"] = event_entries(c, eid)
        rg, rt = rank_sets(c, e.get("rank_id"))
        e["rank_grades"] = rg   # пусто = использовать все оценки
        e["rank_titles"] = rt   # пусто = использовать все титулы
        e["stats"] = {
            "total": len(e["entries"]),
            "graded": sum(1 for x in e["entries"] if x.get("grade")),
            "males": sum(1 for x in e["entries"] if x.get("sex") == "male"),
            "females": sum(1 for x in e["entries"] if x.get("sex") == "female"),
        }
        return e


def event_entries(c, eid):
    return rows(c.execute(
        """SELECT en.*, d.name AS dog_name, d.breed, d.breed_group, d.sex, d.color,
                  d.pedigree_number, d.chip, d.tattoo, d.dob, d.breeder,
                  d.dam_name, d.sire_name, d.owner_id,
                  o.full_name AS owner_name
           FROM entries en
           JOIN dogs d ON d.id=en.dog_id
           LEFT JOIN owners o ON o.id=d.owner_id
           WHERE en.event_id=?
           ORDER BY (en.catalog_no IS NULL), en.catalog_no,
                    d.breed COLLATE NOCASE, d.name COLLATE NOCASE""", (eid,)))


@app.post("/api/events")
async def save_event(request: Request):
    d = await body(request)
    vals = {}
    for f in EVENT_FIELDS:
        v = d.get(f)
        vals[f] = v.strip() if isinstance(v, str) else v
    if not vals["name"]:
        return JSONResponse({"error": "Название обязательно"}, status_code=400)
    if not vals.get("status"):
        vals["status"] = "draft"
    if not vals.get("type"):
        vals["type"] = "show"
    with closing(db()) as c:
        # ранг из справочника → подтягиваем имя ранга (снимок)
        if vals.get("rank_id"):
            r = c.execute("SELECT name FROM ranks WHERE id=?", (vals["rank_id"],)).fetchone()
            if r:
                vals["rank"] = r["name"]
        else:
            vals["rank_id"] = None
        if d.get("id"):
            sets = ",".join(f"{f}=?" for f in EVENT_FIELDS)
            c.execute(f"UPDATE events SET {sets} WHERE id=?",
                      [vals[f] for f in EVENT_FIELDS] + [d["id"]])
            eid = d["id"]
        else:
            cols = ",".join(EVENT_FIELDS)
            ph = ",".join("?" for _ in EVENT_FIELDS)
            cur = c.execute(f"INSERT INTO events({cols},created) VALUES({ph},?)",
                            [vals[f] for f in EVENT_FIELDS] + [time.time()])
            eid = cur.lastrowid
        c.commit()
    return {"id": eid}


@app.post("/api/events/{eid}/status")
async def set_status(eid: int, request: Request):
    d = await body(request)
    st = d.get("status")
    if st not in STATUS_FLOW:
        return JSONResponse({"error": "bad status"}, status_code=400)
    with closing(db()) as c:
        c.execute("UPDATE events SET status=? WHERE id=?", (st, eid))
        c.commit()
    return {"ok": True, "status": st}


@app.post("/api/events/{eid}/form-catalog")
def form_catalog(eid: int):
    """Закрыть запись и присвоить каталожные номера.

    Порядок РКФ: по группе пород → породе → полу (кобели раньше сук) →
    порядку класса → кличке. Нумерация сквозная.
    """
    with closing(db()) as c:
        class_order = {r["name"]: r["sort"] for r in
                       c.execute("SELECT name,sort FROM refs WHERE kind='classes'")}
        ent = rows(c.execute(
            """SELECT en.id, en.class_name, d.breed_group, d.breed, d.sex, d.name
               FROM entries en JOIN dogs d ON d.id=en.dog_id
               WHERE en.event_id=?""", (eid,)))

        def key(x):
            sex_rank = 0 if x.get("sex") == "male" else 1
            return (
                (x.get("breed_group") or "").lower(),
                (x.get("breed") or "").lower(),
                sex_rank,
                class_order.get(x.get("class_name"), 99),
                (x.get("name") or "").lower(),
            )

        ent.sort(key=key)
        for i, x in enumerate(ent, start=1):
            c.execute("UPDATE entries SET catalog_no=? WHERE id=?", (i, x["id"]))
        c.execute("UPDATE events SET status='reg_closed' WHERE id=?", (eid,))
        c.commit()
    return {"ok": True, "numbered": len(ent)}


@app.delete("/api/events/{eid}")
def del_event(eid: int):
    with closing(db()) as c:
        c.execute("DELETE FROM events WHERE id=?", (eid,))
        c.commit()
    return {"ok": True}


# --------------------------------------------------------- entries (запись собак)
@app.post("/api/events/{eid}/entries")
async def add_entry(eid: int, request: Request):
    d = await body(request)
    dog_ids = d.get("dog_ids") or ([d["dog_id"]] if d.get("dog_id") else [])
    cls = (d.get("class_name") or "").strip()
    added = 0
    with closing(db()) as c:
        for dog_id in dog_ids:
            exists = c.execute("SELECT 1 FROM entries WHERE event_id=? AND dog_id=?",
                               (eid, dog_id)).fetchone()
            if exists:
                continue
            c.execute("""INSERT INTO entries(event_id,dog_id,class_name,created)
                         VALUES(?,?,?,?)""", (eid, dog_id, cls, time.time()))
            added += 1
        c.commit()
    return {"ok": True, "added": added}


@app.post("/api/entries/{enid}")
async def update_entry(enid: int, request: Request):
    d = await body(request)
    allowed = ["class_name", "grade", "placement", "titles", "remark", "catalog_no"]
    sets, args = [], []
    for f in allowed:
        if f in d:
            v = d[f]
            if f in ("placement", "catalog_no"):
                v = int(v) if str(v).strip() not in ("", "None") else None
            elif isinstance(v, str):
                v = v.strip()
            sets.append(f"{f}=?")
            args.append(v)
    if not sets:
        return {"ok": True}
    args.append(enid)
    with closing(db()) as c:
        c.execute(f"UPDATE entries SET {','.join(sets)} WHERE id=?", args)
        c.commit()
    return {"ok": True}


@app.delete("/api/entries/{enid}")
def del_entry(enid: int):
    with closing(db()) as c:
        c.execute("DELETE FROM entries WHERE id=?", (enid,))
        c.commit()
    return {"ok": True}


# =====================================================================
#  DASHBOARD counts
# =====================================================================
@app.get("/api/dashboard")
def dashboard():
    with closing(db()) as c:
        def one(q):
            return c.execute(q).fetchone()[0]
        upcoming = rows(c.execute(
            """SELECT id,name,event_date,rank,place,status FROM events
               WHERE status!='finished' ORDER BY event_date LIMIT 6"""))
        return {
            "dogs": one("SELECT COUNT(*) FROM dogs"),
            "owners": one("SELECT COUNT(*) FROM owners"),
            "events": one("SELECT COUNT(*) FROM events"),
            "debt": one("SELECT COALESCE(SUM(amount),0) FROM payments WHERE status='debt'"),
            "upcoming": upcoming,
        }


# =====================================================================
#  PRINT DOCUMENTS  (печатные HTML — Ctrl+P → PDF)
# =====================================================================
SEX_RU = {"male": "кобель", "female": "сука", "": "", None: ""}

PRINT_CSS = """
<style>
:root{color-scheme:light}
*{box-sizing:border-box}
body{font-family:'Times New Roman',Georgia,serif;color:#111;margin:0;
  padding:24px 30px;background:#fff;font-size:13px;line-height:1.4}
.toolbar{position:sticky;top:0;background:#f4f7f9;border:1px solid #dde4ea;
  border-radius:10px;padding:10px 14px;margin-bottom:18px;display:flex;gap:10px;
  align-items:center;font-family:Segoe UI,sans-serif;flex-wrap:wrap}
.toolbar button{padding:8px 16px;border:0;border-radius:8px;background:#3f8f78;
  color:#fff;font-weight:600;cursor:pointer;font-size:14px}
.toolbar a{color:#3f8f78;text-decoration:none;font-weight:600;font-size:14px}
.toolbar .muted{color:#7a8794;font-size:13px}
h1{text-align:center;font-size:20px;margin:6px 0 2px}
h2{font-size:16px;margin:20px 0 6px;border-bottom:2px solid #333;padding-bottom:3px}
h3{font-size:14px;margin:14px 0 4px;color:#333}
.head{text-align:center;margin-bottom:16px}
.head .sub{font-size:14px;color:#333;margin:2px 0}
table{width:100%;border-collapse:collapse;margin:6px 0 14px}
th,td{border:1px solid #999;padding:5px 7px;text-align:left;vertical-align:top}
th{background:#eef2f4;font-size:12px}
td.no,th.no{text-align:center;width:34px}
td.c{text-align:center}
.small{font-size:11px;color:#555}
.dog-name{font-weight:bold;text-transform:uppercase}
@media print{
  .toolbar{display:none}
  body{padding:0}
  h2{page-break-after:avoid}
  tr{page-break-inside:avoid}
  .diploma{page-break-after:always}
}
.diploma{border:6px double #6b4f2a;border-radius:8px;padding:34px 40px;margin:0 0 26px;
  text-align:center;min-height:60vh;display:flex;flex-direction:column;justify-content:center}
.diploma .cclub{font-size:15px;letter-spacing:1px;color:#6b4f2a;text-transform:uppercase}
.diploma .dtitle{font-size:34px;margin:10px 0 4px;color:#5a3d1a;font-variant:small-caps}
.diploma .grade{font-size:26px;font-weight:bold;margin:14px 0}
.diploma .breed{font-size:16px;color:#333}
.diploma .dogname{font-size:24px;font-weight:bold;text-transform:uppercase;margin:6px 0}
.diploma .meta{font-size:13px;color:#444;margin-top:auto}
.diploma .titles{font-size:18px;color:#8a5a1a;font-weight:bold;margin:8px 0}
.sign{display:flex;justify-content:space-between;margin-top:26px;font-size:13px}
.sign div{border-top:1px solid #333;padding-top:4px;width:44%;text-align:center}
</style>
"""


def toolbar(back_eid, extra=""):
    return (f'<div class=toolbar><button onclick="window.print()">🖨 Печать / PDF</button>'
            f'<a href="/#event-{back_eid}">← к мероприятию</a>'
            f'<span class=muted>Ctrl+P → «Сохранить как PDF»</span>{extra}</div>')


def fetch_event(eid):
    with closing(db()) as c:
        e = row(c.execute("SELECT * FROM events WHERE id=?", (eid,)).fetchone())
        if not e:
            return None, None
        ent = event_entries(c, eid)
        return e, ent


def event_header(e):
    parts = [f'<div class="sub">{escape(e.get("rank") or "")} {escape(dict(show="выставка", test="тестирование", competition="соревнование", other="мероприятие").get(e.get("type") or "show",""))}</div>']
    line2 = []
    if e.get("event_date"):
        line2.append(escape(e["event_date"]))
    if e.get("place"):
        line2.append(escape(e["place"]))
    if line2:
        parts.append(f'<div class="sub">{" · ".join(line2)}</div>')
    if e.get("judges"):
        parts.append(f'<div class="sub">Судья(и): {escape(e["judges"])}</div>')
    return "".join(parts)


def group_entries(ent):
    """Сгруппировать записи: группа пород → порода → пол → класс."""
    groups = {}
    for x in ent:
        bg = x.get("breed_group") or "Без группы"
        br = x.get("breed") or "Без породы"
        groups.setdefault(bg, {}).setdefault(br, []).append(x)
    return groups


def esc(v):
    return escape(str(v)) if v not in (None, "") else ""


@app.get("/print/catalog/{eid}", response_class=HTMLResponse)
def print_catalog(eid: int):
    e, ent = fetch_event(eid)
    if not e:
        return HTMLResponse("Мероприятие не найдено", status_code=404)
    body_html = [PRINT_CSS, toolbar(eid),
                 f'<div class=head><h1>КАТАЛОГ</h1><div class="sub"><b>{esc(e["name"])}</b></div>',
                 event_header(e), '</div>']
    if not any(x.get("catalog_no") for x in ent):
        body_html.append('<p class=small>⚠ Каталожные номера ещё не присвоены — '
                         'нажмите «Сформировать каталог» в карточке мероприятия.</p>')
    groups = group_entries(ent)
    n_total = 0
    for bg in sorted(groups):
        body_html.append(f'<h2>{esc(bg)}</h2>')
        for br in sorted(groups[bg]):
            body_html.append(f'<h3>{esc(br)}</h3>')
            for sex_key, sex_label in (("male", "Кобели"), ("female", "Суки")):
                rowset = [x for x in groups[bg][br] if x.get("sex") == sex_key]
                if not rowset:
                    continue
                rowset.sort(key=lambda x: (x.get("catalog_no") or 9999))
                body_html.append(f'<div class=small><b>{sex_label}</b></div>')
                body_html.append('<table><tr><th class=no>№</th><th>Кличка</th>'
                                 '<th>Класс</th><th>Родословная</th><th>Клеймо/чип</th>'
                                 '<th>Окрас</th><th>Дата рожд.</th><th>Заводчик</th>'
                                 '<th>Владелец</th></tr>')
                for x in rowset:
                    n_total += 1
                    body_html.append(
                        f'<tr><td class=no>{esc(x.get("catalog_no"))}</td>'
                        f'<td class=dog-name>{esc(x.get("dog_name"))}<br>'
                        f'<span class=small>отец: {esc(x.get("sire_name"))}; мать: {esc(x.get("dam_name"))}</span></td>'
                        f'<td>{esc(x.get("class_name"))}</td>'
                        f'<td>{esc(x.get("pedigree_number"))}</td>'
                        f'<td>{esc(x.get("tattoo"))} / {esc(x.get("chip"))}</td>'
                        f'<td>{esc(x.get("color"))}</td>'
                        f'<td>{esc(x.get("dob"))}</td>'
                        f'<td>{esc(x.get("breeder"))}</td>'
                        f'<td>{esc(x.get("owner_name"))}</td></tr>')
                body_html.append('</table>')
    body_html.append(f'<p class=small>Всего записано: {len(ent)} собак.</p>')
    return HTMLResponse("".join(body_html))


@app.get("/print/ringsheet/{eid}", response_class=HTMLResponse)
def print_ringsheet(eid: int):
    """Оценочный лист судьи / ринговая ведомость — с пустыми графами."""
    e, ent = fetch_event(eid)
    if not e:
        return HTMLResponse("Мероприятие не найдено", status_code=404)
    body_html = [PRINT_CSS, toolbar(eid),
                 f'<div class=head><h1>РИНГОВАЯ ВЕДОМОСТЬ</h1>'
                 f'<div class="sub"><b>{esc(e["name"])}</b></div>',
                 event_header(e), '</div>']
    groups = group_entries(ent)
    for bg in sorted(groups):
        body_html.append(f'<h2>{esc(bg)}</h2>')
        for br in sorted(groups[bg]):
            body_html.append(f'<h3>{esc(br)}</h3>')
            for sex_key, sex_label in (("male", "Кобели"), ("female", "Суки")):
                rowset = sorted([x for x in groups[bg][br] if x.get("sex") == sex_key],
                                key=lambda x: (x.get("catalog_no") or 9999,
                                               (x.get("class_name") or "")))
                if not rowset:
                    continue
                body_html.append(f'<div class=small><b>{sex_label}</b></div>')
                body_html.append('<table><tr><th class=no>№</th><th>Кличка</th>'
                                 '<th>Класс</th><th style="width:90px">Оценка</th>'
                                 '<th style="width:60px">Место</th>'
                                 '<th style="width:140px">Титулы</th>'
                                 '<th>Описание / примечание</th></tr>')
                for x in rowset:
                    body_html.append(
                        f'<tr><td class=no>{esc(x.get("catalog_no"))}</td>'
                        f'<td class=dog-name>{esc(x.get("dog_name"))}</td>'
                        f'<td>{esc(x.get("class_name"))}</td>'
                        f'<td></td><td></td><td></td><td style="height:34px"></td></tr>')
                body_html.append('</table>')
    body_html.append('<div class=sign><div>Судья (подпись)</div>'
                     '<div>Секретарь ринга (подпись)</div></div>')
    return HTMLResponse("".join(body_html))


@app.get("/print/diplomas/{eid}", response_class=HTMLResponse)
def print_diplomas(eid: int, dog: int = 0):
    """Дипломы — по одному на страницу. dog=<id> — один диплом."""
    e, ent = fetch_event(eid)
    if not e:
        return HTMLResponse("Мероприятие не найдено", status_code=404)
    graded = [x for x in ent if x.get("grade")]
    if dog:
        graded = [x for x in graded if x.get("dog_id") == dog]
    body_html = [PRINT_CSS, toolbar(eid)]
    if not graded:
        body_html.append('<p class=small>Нет собак с проставленными оценками — '
                         'сначала внесите оценки в карточке мероприятия.</p>')
    for x in graded:
        titles = esc(x.get("titles"))
        place = f'Место: {esc(x.get("placement"))}' if x.get("placement") else ""
        body_html.append(
            '<div class=diploma>'
            f'<div class=cclub>{esc(CLUB_NAME)}</div>'
            f'<div class=dtitle>Диплом</div>'
            f'<div class="sub">{esc(e.get("rank") or "")} · {esc(e["name"])}</div>'
            f'<div class=breed>{esc(x.get("breed"))} · {esc(SEX_RU.get(x.get("sex"), ""))} · класс {esc(x.get("class_name"))}</div>'
            f'<div class=dogname>{esc(x.get("dog_name"))}</div>'
            f'<div class=small>Родословная {esc(x.get("pedigree_number"))} · '
            f'клеймо {esc(x.get("tattoo"))} · чип {esc(x.get("chip"))}</div>'
            f'<div class=grade>Оценка: {esc(x.get("grade"))}</div>'
            + (f'<div class=titles>{titles}</div>' if titles else '')
            + (f'<div class="sub">{place}</div>' if place else '')
            + f'<div class=meta>Владелец: {esc(x.get("owner_name"))} · '
            f'Заводчик: {esc(x.get("breeder"))}<br>'
            f'{esc(e.get("event_date"))} · {esc(e.get("place"))}<br>'
            f'Судья: {esc(e.get("judges"))}'
            '<div class=sign><div>Судья</div><div>Председатель клуба</div></div>'
            '</div></div>')
    return HTMLResponse("".join(body_html))


@app.get("/print/report/{eid}", response_class=HTMLResponse)
def print_report(eid: int):
    """Пакет документов для отчёта: сводка + полные результаты."""
    e, ent = fetch_event(eid)
    if not e:
        return HTMLResponse("Мероприятие не найдено", status_code=404)
    breeds = {}
    for x in ent:
        breeds[x.get("breed") or "—"] = breeds.get(x.get("breed") or "—", 0) + 1
    titled = [x for x in ent if x.get("titles")]
    body_html = [PRINT_CSS, toolbar(eid),
                 f'<div class=head><h1>ОТЧЁТ О МЕРОПРИЯТИИ</h1>'
                 f'<div class="sub"><b>{esc(e["name"])}</b></div>',
                 event_header(e), '</div>']
    body_html.append('<h2>Сводка</h2><table>'
                     f'<tr><td>Всего собак</td><td>{len(ent)}</td></tr>'
                     f'<tr><td>Кобелей / сук</td><td>{sum(1 for x in ent if x.get("sex")=="male")} / '
                     f'{sum(1 for x in ent if x.get("sex")=="female")}</td></tr>'
                     f'<tr><td>Пород</td><td>{len(breeds)}</td></tr>'
                     f'<tr><td>Оценено</td><td>{sum(1 for x in ent if x.get("grade"))}</td></tr>'
                     f'<tr><td>Присвоено титулов</td><td>{len(titled)}</td></tr></table>')
    body_html.append('<h2>По породам</h2><table><tr><th>Порода</th><th class=no>Кол-во</th></tr>')
    for br in sorted(breeds):
        body_html.append(f'<tr><td>{esc(br)}</td><td class=c>{breeds[br]}</td></tr>')
    body_html.append('</table>')
    body_html.append('<h2>Полные результаты</h2>'
                     '<table><tr><th class=no>№</th><th>Кличка</th><th>Порода</th>'
                     '<th>Класс</th><th>Оценка</th><th>Место</th><th>Титулы</th>'
                     '<th>Владелец</th></tr>')
    for x in sorted(ent, key=lambda x: (x.get("catalog_no") or 9999)):
        body_html.append(
            f'<tr><td class=no>{esc(x.get("catalog_no"))}</td>'
            f'<td class=dog-name>{esc(x.get("dog_name"))}</td>'
            f'<td>{esc(x.get("breed"))}</td>'
            f'<td>{esc(x.get("class_name"))}</td>'
            f'<td>{esc(x.get("grade"))}</td>'
            f'<td class=c>{esc(x.get("placement"))}</td>'
            f'<td>{esc(x.get("titles"))}</td>'
            f'<td>{esc(x.get("owner_name"))}</td></tr>')
    body_html.append('</table>')
    if titled:
        body_html.append('<h2>Присвоенные титулы</h2><table>'
                         '<tr><th>Кличка</th><th>Порода</th><th>Титулы</th></tr>')
        for x in titled:
            body_html.append(f'<tr><td class=dog-name>{esc(x.get("dog_name"))}</td>'
                             f'<td>{esc(x.get("breed"))}</td><td>{esc(x.get("titles"))}</td></tr>')
        body_html.append('</table>')
    body_html.append('<div class=sign><div>Главный эксперт</div>'
                     '<div>Председатель клуба</div></div>')
    return HTMLResponse("".join(body_html))
