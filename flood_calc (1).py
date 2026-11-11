#!/usr/bin/env python3
"""ตัวคำนวณความเสี่ยงน้ำท่วม รองรับหลายพื้นที่ (แต่ละพื้นที่ใช้ topic ของตัวเอง)
อ่านรายงานจาก ESP32 -> คำนวณทั้งตาราง -> แจ้งเตือนเสียงเข้าโทรศัพท์ + อัปเดตหน้าเว็บ
ใช้ Python 3 ล้วน ไม่ต้องติดตั้งอะไรเพิ่ม รัน:  python flood_calc.py
"""
import json, math, time, urllib.request

# ===== ตั้งค่า =====
WEB = "https://radin4469-collab.github.io/my-live-site/"
STALE_MIN = 45      # เซนเซอร์เงียบเกินกี่นาทีถึงเตือนว่าหาย (ควรมากกว่ารอบ heartbeat 30 นาที)
POLL_SEC = 60

# แต่ละพื้นที่: topic ต้องไม่ซ้ำกัน และตรงกับ AREAS ใน live.html + TOPIC ใน ESP32 ของพื้นที่นั้น
AREAS = [
    {
        "name": "โรงเรียนไชยวานวิทยา",
        "topic": "flood-chaiyawan-7k2x9q",    # เปลี่ยนเป็นชื่อสุ่มของคุณเอง (อังกฤษ/ตัวเลข/ขีด)
        "lat": 17.2867, "lng": 103.2233,       # *** ตัวอย่าง = กลางอำเภอไชยวาน ให้แทนด้วยพิกัดจุดกลางโรงเรียนจริงจาก Google Maps ***
        "n": 5, "m": 40,                       # ตาราง 5x5 ช่องละ 40 เมตร (คลุม 200x200 ม.)
        "nodes": ["C3"],                       # ช่องที่มี ESP32 วางอยู่ เพิ่มได้เช่น ["C3", "A1", "E5"]
        # ความสูงแต่ละช่อง (เมตร) แถวบน = ทิศเหนือ ซ้ายไปขวา ต้องมี n*n ค่า *** ตัวอย่าง ให้วัดจริงหน้างาน ***
        "el": [5.00, 5.05, 4.95, 5.00, 5.05,
               4.85, 4.90, 4.80, 4.85, 4.90,
               4.70, 4.75, 4.65, 4.70, 4.75,
               4.55, 4.60, 4.50, 4.55, 4.60,
               4.40, 4.45, 4.35, 4.40, 4.45],
    },
    # {"name": "พื้นที่ถัดไป", "topic": "flood-xxxx-ชื่อสุ่ม", "lat": 0, "lng": 0, "n": 5, "m": 40, "nodes": ["C3"], "el": [...]},
]
# ===================
NTFY = "https://ntfy.sh/"


def name(a, i): return chr(65 + i % a["n"]) + str(i // a["n"] + 1)


def cell_index(a, c):
    try:
        col, row = ord(c[0]) - 65, int(c[1:]) - 1
        return row * a["n"] + col if 0 <= col < a["n"] and 0 <= row < a["n"] else None
    except Exception:
        return None


def get_reports(a):
    """คืนค่า {ช่อง: [(เวลา, ความเปียก 0-1), ...]} จากรายงานย้อนหลัง 12 ชม."""
    with urllib.request.urlopen(f"{NTFY}{a['topic']}-in/json?poll=1&since=12h", timeout=20) as r:
        lines = r.read().decode("utf-8").splitlines()
    nodes = {}
    for ln in lines:
        try:
            e = json.loads(ln)
            if e.get("event") != "message":
                continue
            cell, s = e["message"].strip().split("|")[:2]
            i = cell_index(a, cell.upper())
            if i is not None:
                nodes.setdefault(i, []).append((e["time"], int(s) / 100))
        except Exception:
            pass
    return nodes


def calc(a, nodes):
    """ให้ระดับแต่ละช่อง: 0 ปกติ, 1 เหลือง, 2 ส้ม, 3 แดง"""
    n, el = a["n"], a["el"]
    last = {i: v[-1] for i, v in nodes.items()}   # ตัวที่เงียบหายยังใช้ค่าสุดท้าย เผื่อโดนน้ำจนดับ
    wet = {i: s for i, (t, s) in last.items() if s >= 0.25}
    sustained = any(len(v) >= 3 and all(s >= 0.25 for _, s in v[-3:]) for v in nodes.values())
    rn = max(wet.values(), default=0)
    if sustained:
        rn = max(rn, 0.8)                          # เปียกต่อเนื่อง = ฝนหนัก น้ำแผ่ไกลขึ้น
    R = 2 + 3 * rn
    lv = [0] * (n * n)
    for i in range(n * n):
        if i in wet:                               # ช่องที่มีเซนเซอร์เปียก = ข้อมูลจริง
            lv[i] = 3 if wet[i] >= 0.6 else 2
            continue
        p = 1.0                                    # ช่องอื่น: รวมความเสี่ยงจากทุกจุดที่เปียก
        for j, s in wet.items():
            d = math.hypot(i % n - j % n, i // n - j // n)
            dh = el[j] - el[i]                     # บวก = ช่องนี้ต่ำกว่าจุดที่เปียก
            prox = max(0, 1 - (d - 1) / R)
            ef = 1 if dh >= 0 else max(0, 1 + dh / 0.8)
            p *= 1 - prox * ef * (0.4 + 0.6 * s)
        risk = 1 - p                               # หลายจุดเปียกพร้อมกัน ความเสี่ยงยิ่งสูง
        lv[i] = 2 if risk >= 0.5 else 1 if risk >= 0.15 else 0
    return lv, last


def post(topic, msg, title=None, pri=3, tags=None, click=None):
    body = {"topic": topic, "message": msg, "priority": pri}
    if title: body["title"] = title
    if tags: body["tags"] = tags
    if click: body["click"] = click
    req = urllib.request.Request(NTFY, json.dumps(body).encode("utf-8"),
                                 {"Content-Type": "application/json"})
    urllib.request.urlopen(req, timeout=20).read()


def summary(a, lv):
    g = lambda L: " ".join(name(a, i) for i in range(len(lv)) if lv[i] == L) or "-"
    return f"แดง: {g(3)}\nส้ม: {g(2)}\nเหลือง: {g(1)}\nแตะเพื่อดูรายละเอียด"


def state_msg(a, lv):   # รูปแบบที่ live.html อ่าน
    return "|".join([str(a["n"]), str(a["m"]), f"{a['lat']:.5f}", f"{a['lng']:.5f}",
                     "".join(map(str, lv)), ",".join(f"{e:.2f}" for e in a["el"])])


def check(a, st):
    nodes = get_reports(a)
    lv, last = calc(a, nodes)
    prev, T = st["prev"], a["topic"]
    up = [i for i in range(len(lv)) if lv[i] >= 2 and lv[i] > prev[i]]
    if up:   # มีช่องใหม่ขึ้นเป็นส้ม/แดง -> แจ้งเตือนเสียงก่อน
        top = max(lv[i] for i in up)
        post(T, summary(a, lv), f"{'🔴 น้ำท่วมแล้ว' if top == 3 else '🟠 เสี่ยงน้ำท่วมสูง'} · {a['name']}",
             5 if top == 3 else 4, ["rotating_light"], f"{WEB}?t={T}")
    elif max(prev) > 0 and max(lv) == 0:
        post(T, "ไม่พบน้ำที่เซนเซอร์ใดเลย", f"🟢 กลับสู่ปกติ · {a['name']}", 2, ["white_check_mark"])
    if lv != prev:
        post(T + "-state", state_msg(a, lv))      # อัปเดตหน้าเว็บ
    st["prev"] = lv
    now = time.time()
    for c in a["nodes"]:                           # เช็กเซนเซอร์ที่เงียบหาย
        i = cell_index(a, c)
        t = last[i][0] if i in last else 0
        if now - t > STALE_MIN * 60:
            if c not in st["warned"]:
                st["warned"].add(c)
                post(T, f"ไม่มีรายงานเกิน {STALE_MIN} นาที (แบตหมดหรือหลุด WiFi) ควรไปตรวจ",
                     f"⚠️ เซนเซอร์ {c} เงียบหาย · {a['name']}", 3, ["warning"])
        else:
            st["warned"].discard(c)
    print(time.strftime("%H:%M:%S"), a["name"], "".join(map(str, lv)))


def main():
    topics = [a["topic"] for a in AREAS]
    assert len(set(topics)) == len(topics), "topic ของแต่ละพื้นที่ต้องไม่ซ้ำกัน"
    for a in AREAS:
        assert len(a["el"]) == a["n"] ** 2, f"el ของ {a['name']} ต้องมี n*n ค่า"
    state = {a["topic"]: {"prev": [0] * a["n"] ** 2, "warned": set()} for a in AREAS}
    print("เริ่มทำงาน (Ctrl+C เพื่อหยุด)")
    for a in AREAS:
        print(" -", a["name"], "| topic:", a["topic"])
    while True:
        for a in AREAS:
            try:
                check(a, state[a["topic"]])
            except Exception as e:
                print("error", a["name"], ":", e)
        time.sleep(POLL_SEC)


if __name__ == "__main__":
    main()
