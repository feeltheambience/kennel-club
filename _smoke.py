# -*- coding: utf-8 -*-
"""Локальный смоук-тест API через ASGI TestClient (без сети)."""
import os, sys
os.environ["KENNEL_PASSWORD"] = "dogs"
# свежая БД для теста
if os.path.exists("kennel.db"):
    os.remove("kennel.db")
from fastapi.testclient import TestClient
import app as A
c = TestClient(A.app, follow_redirects=False)

def ok(cond, msg):
    print(("  OK  " if cond else " FAIL ") + msg)
    if not cond: sys.exit(1)

# --- gate ---
r = c.get("/")
ok(r.status_code == 302 and "/login" in r.headers.get("location",""), "неавторизованный редиректится на /login")
r = c.get("/api/dogs")
ok(r.status_code == 401, "API без куки → 401")

# --- login ---
r = c.post("/login", data={"password":"wrong"})
ok(r.status_code == 401, "неверный пароль → 401")
r = c.post("/login", data={"password":"dogs"})
ok(r.status_code == 302, "верный пароль → редирект")
token = r.cookies.get("kennel_auth")
ok(bool(token), "кука выдана")
c.cookies.set("kennel_auth", token)

# --- refs seeded ---
refs = c.get("/api/refs").json()
ok(len(refs["classes"])>=8 and len(refs["grades"])>=4 and len(refs["titles"])>=5, "справочники засеяны")

# --- owner ---
oid = c.post("/api/owners", json={"full_name":"Иванова Мария","phone":"+79001112233","email":"m@i.ru","member_no":"12","member_since":"2020-03-01"}).json()["id"]
ok(oid>0, "владелец создан")
owners = c.get("/api/owners").json()
ok(any(o["id"]==oid and o["dogs_count"]==0 for o in owners), "владелец в списке, 0 собак")

# --- dogs ---
d1 = c.post("/api/dogs", json={"name":"Граф","breed":"Немецкая овчарка","breed_group":"FCI I Овчарки","sex":"male","color":"чепрачный","pedigree_number":"RKF-100","tattoo":"ABC1","chip":"643001","owner_id":oid,"sire_name":"Рекс","dam_name":"Лада","breeder":"Петров","dob":"2022-05-10"}).json()["id"]
d2 = c.post("/api/dogs", json={"name":"Айра","breed":"Немецкая овчарка","breed_group":"FCI I Овчарки","sex":"female","owner_id":oid}).json()["id"]
d3 = c.post("/api/dogs", json={"name":"Барон","breed":"Ротвейлер","breed_group":"FCI II","sex":"male","owner_id":oid}).json()["id"]
ok(all([d1,d2,d3]), "3 собаки созданы")
dogs = c.get("/api/dogs?q=овчарк").json()
ok(len(dogs)==2, "поиск по породе даёт 2")
card = c.get(f"/api/dogs/{d1}").json()
ok(card["name"]=="Граф" and "events" in card, "карточка собаки")

# --- payments ---
p1 = c.post("/api/payments", json={"owner_id":oid,"category":"membership","amount":2000,"pay_date":"2026-01-10","period":"2026","status":"paid"}).json()["id"]
p2 = c.post("/api/payments", json={"owner_id":oid,"category":"show_entry","amount":1500,"status":"debt"}).json()["id"]
pay = c.get("/api/payments").json()
ok(pay["total_paid"]==2000 and pay["total_debt"]==1500, f"суммы платежей ({pay['total_paid']}/{pay['total_debt']})")

# --- event ---
eid = c.post("/api/events", json={"name":"Региональная выставка 2026","type":"show","rank":"САС","event_date":"2026-09-20","place":"Москва, СК Олимп","judges":"Сидоров А.П."}).json()["id"]
ok(eid>0, "выставка создана")
c.post(f"/api/events/{eid}/status", json={"status":"reg_open"})
# запись собак
r = c.post(f"/api/events/{eid}/entries", json={"dog_ids":[d1,d2,d3],"class_name":"Открытый"}).json()
ok(r["added"]==3, "3 собаки записаны")
# дубль не добавляется
r = c.post(f"/api/events/{eid}/entries", json={"dog_ids":[d1],"class_name":"Открытый"}).json()
ok(r["added"]==0, "повторная запись игнорируется")

# form catalog
r = c.post(f"/api/events/{eid}/form-catalog").json()
ok(r["numbered"]==3, "каталог: 3 номера")
ev = c.get(f"/api/events/{eid}").json()
ok(ev["status"]=="reg_closed", "статус → reg_closed")
# проверка порядка: кобели раньше сук в одной породе; овчарки (2) + ротвейлер (1)
by_no = {x["catalog_no"]: x["dog_name"] for x in ev["entries"]}
ok(all(by_no.get(i) for i in (1,2,3)), f"номера присвоены: {by_no}")

# judging: grades
c.post(f"/api/events/{eid}/status", json={"status":"judging"})
en0 = ev["entries"][0]["id"]
c.post(f"/api/entries/{en0}", json={"grade":"Отлично","placement":1,"titles":"CW, CAC","remark":"отличный экстерьер"})
ev = c.get(f"/api/events/{eid}").json()
graded = [x for x in ev["entries"] if x["grade"]]
ok(len(graded)==1 and graded[0]["placement"]==1, "оценка проставлена")

# --- print docs ---
for path in [f"/print/catalog/{eid}", f"/print/ringsheet/{eid}", f"/print/diplomas/{eid}", f"/print/report/{eid}"]:
    r = c.get(path)
    ok(r.status_code==200 and "html" in r.headers.get("content-type",""), f"печать {path}")
cat = c.get(f"/print/catalog/{eid}").text
ok("Граф" in cat and "КАТАЛОГ" in cat, "каталог содержит собаку")
dip = c.get(f"/print/diplomas/{eid}").text
ok("Диплом" in dip and "Отлично" in dip, "диплом с оценкой")

# --- ref add/del ---
rid = c.post("/api/refs/titles", json={"name":"ТЕСТ-ТИТУЛ"}).json()["id"]
ok(any(x["name"]=="ТЕСТ-ТИТУЛ" for x in c.get("/api/refs").json()["titles"]), "справочник: добавлено")
c.delete(f"/api/refs/titles/{rid}")
ok(not any(x["name"]=="ТЕСТ-ТИТУЛ" for x in c.get("/api/refs").json()["titles"]), "справочник: удалено")

# --- dashboard ---
dash = c.get("/api/dashboard").json()
ok(dash["dogs"]==3 and dash["owners"]==1 and dash["debt"]==1500, "дашборд считает корректно")

print("\nВСЕ ТЕСТЫ ПРОЙДЕНЫ ✅")
