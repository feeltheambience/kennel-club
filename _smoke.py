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

# =============== НОВЫЕ СПРАВОЧНИКИ ===============
# --- дерево пород ---
breeds = c.get("/api/breeds").json()
ok(len(breeds)==10, f"засеяно 10 групп FCI ({len(breeds)})")
ok(all("breeds" in g for g in breeds) and sum(len(g["breeds"]) for g in breeds)>40, "в группах есть породы")
# добавить группу и породу
gid = c.post("/api/breed-groups", json={"name":"Тест-группа"}).json()["id"]
bid = c.post("/api/breeds", json={"group_id":gid,"name":"Тест-порода"}).json()["id"]
ok(gid and bid, "группа и порода добавлены")
# переименовать группу
c.post("/api/breed-groups", json={"id":gid,"name":"Тест-группа 2"})
ok(any(g["id"]==gid and g["name"]=="Тест-группа 2" for g in c.get("/api/breeds").json()), "группа переименована")
# bulk пород
r = c.post("/api/breeds/bulk", json={"group_id":gid,"text":"Порода А\nПорода Б\nПорода А"}).json()
ok(r["added"]==2, "bulk пород: 2 добавлено (дубль пропущен)")
# собака с breed_id → резолв текста породы/группы
d4 = c.post("/api/dogs", json={"name":"Тестик","breed_id":bid,"sex":"male"}).json()["id"]
dog4 = c.get(f"/api/dogs/{d4}").json()
ok(dog4["breed"]=="Тест-порода" and dog4["breed_group"]=="Тест-группа 2", f"breed_id → текст ({dog4['breed']}/{dog4['breed_group']})")
# удаление группы каскадит породы
c.delete(f"/api/breed-groups/{gid}")
ok(not any(g["id"]==gid for g in c.get("/api/breeds").json()), "группа удалена с породами")

# --- адреса ---
aid = c.post("/api/addresses", json={"name":"Клуб Олимп","address":"Москва, ул. Спортивная 1","phone":"+7495"}).json()["id"]
ok(any(a["id"]==aid for a in c.get("/api/addresses").json()), "адрес создан")
c.delete(f"/api/addresses/{aid}")
ok(not any(a["id"]==aid for a in c.get("/api/addresses").json()), "адрес удалён")

# --- ранги со связями оценок/титулов ---
ranks = c.get("/api/ranks").json()
ok(len(ranks)>=5, f"ранги засеяны ({len(ranks)})")
sac = next((r for r in ranks if r["name"].startswith("САС")), None)
ok(sac and "CAC" in sac["titles"] and len(sac["grades"])==0, "у ранга САС заданы титулы, оценки=все")
# ранги вычищены из refs
ok("ranks" not in c.get("/api/refs").json(), "ranks убран из общих справочников")
# создать ранг с ограниченными оценками/титулами
rid = c.post("/api/ranks", json={"name":"Тест-ранг","grades":["Отлично","Хорошо"],"titles":["CAC"]}).json()["id"]
# событие с этим рангом → get_event отдаёт rank_grades/rank_titles
eid2 = c.post("/api/events", json={"name":"Событие с рангом","rank_id":rid,"event_date":"2026-10-01"}).json()["id"]
ev2 = c.get(f"/api/events/{eid2}").json()
ok(ev2["rank"]=="Тест-ранг", "имя ранга подтянулось в событие")
ok(ev2["rank_grades"]==["Отлично","Хорошо"] and ev2["rank_titles"]==["CAC"], "событие отдаёт оценки/титулы ранга")
# toggle grades/titles через save
c.post("/api/ranks", json={"id":rid,"name":"Тест-ранг","grades":["Отлично"],"titles":[]})
ev2 = c.get(f"/api/events/{eid2}").json()
ok(ev2["rank_grades"]==["Отлично"] and ev2["rank_titles"]==[], "изменение набора ранга отражается в событии")

# --- bulk импорт списка (титулы) ---
r = c.post("/api/refs/titles/bulk", json={"text":"НОВ-ТИТУЛ-1\nНОВ-ТИТУЛ-2\nCAC"}).json()
ok(r["added"]==2, "bulk титулов: 2 (существующий CAC пропущен)")

print("\nВСЕ ТЕСТЫ ПРОЙДЕНЫ ✅")
