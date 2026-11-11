#!/usr/bin/env python3
"""ตัวคำนวณน้ำท่วมแบบตาราง (grid) รันบนคอม ใช้ Python 3 ล้วน ไม่ต้องติดตั้งอะไรเพิ่ม
- แต่ละบอร์ด (ESP32) มี topic ของตัวเอง ส่งรายงานเข้า <topic>-in
- คำนวณทั้งตารางจาก "ระยะ + ความสูง + ประวัติที่เคยท่วม" แล้วเดาช่องที่มีโอกาสท่วม
- จดประวัติน้ำท่วมลงไฟล์ flood_history.json (อยู่ข้างสคริปต์) และส่งสรุปให้หน้าเว็บ
- แจ้งเตือนเสียงเข้าโทรศัพท์: topic ของบอร์ดรับเฉพาะโซนของตัวเอง, topic หลักของพื้นที่รับทุกอย่าง
รัน:  python flood_calc.py   (ต้องเปิดคอมทิ้งไว้)
"""
import json, math, os, time, urllib.request

# ===== ตั้งค่า =====
WEB = "https://radin4469-collab.github.io/my-live-site/"
STALE_MIN = 45     # บอร์ดเงียบเกินกี่นาทีถึงเตือนว่าหาย (ควรมากกว่ารอบ heartbeat 30 นาที)
POLL_SEC = 60
GAP_MIN = 60       # น้ำหายครบเกินกี่นาที ถือว่าเหตุการณ์จบ แล้วจดเป็น "ท่วม 1 ครั้ง"
REPUB_MIN = 30     # ส่งสถานะ/ประวัติซ้ำทุกกี่นาที (ntfy เก็บข้อความแค่ 12 ชม.)
HIST_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "flood_history.json")

AREAS = [
    {
        "name": "โรงเรียนไชยวานวิทยา",
        "topic": "flood-chaiyawan-7k2x9q",    # topic หลักของพื้นที่ (ผู้ดูแลรับทุกแจ้งเตือน) ใช้ชื่อสุ่มของคุณเอง
        "lat": 17.2867, "lng": 103.2233,       # *** ตัวอย่าง ให้แทนด้วยพิกัดจุดกลางตารางจริง ***
        "n": 5, "m": 40,                       # ตาราง 5x5 ช่องละ 40 เมตร
        # บอร์ดแต่ละตัว: ช่องที่วาง + topic ของบอร์ดนั้น (ใส่ topic เดียวกันในโค้ด ESP32 ตัวนั้น) + reach = ดูแลรอบตัวกี่ช่อง
        "boards": [
            {"cell": "C3", "topic": "flood-chaiyawan-7k2x9q-c3", "reach": 2},
            # {"cell": "A1", "topic": "flood-chaiyawan-7k2x9q-a1", "reach": 2},
        ],
        "el": None,   # ไม่ใส่ = ถือว่าพื้นเรียบ ถ้าวัดได้ใส่ลิสต์ n*n ค่า (เมตร แถวบน = เหนือ) จะแม่นขึ้น
        # ประวัติที่รู้อยู่แล้ว (จากความจำคนในพื้นที่) 1 รายการ = ท่วมครั้งนั้นช่องไหนบ้าง ใช้ช่วยเดาช่องที่เสี่ยง
        "past": [
            # ["C3", "C4", "B4"],
            # ["C3", "D3"],
        ],
    },
]
# ===================
NTFY = "https://ntfy.sh/"


def name(a, i): return chr(65 + i % a["n"]) + str(i // a["n"] + 1)


def cell_index(a, c):
    try:
        col, row = ord(c[0].upper()) - 65, int(c[1:]) - 1
        return row * a["n"] + col if 0 <= col < a["n"] and 0 <= row < a["n"] else None
    except Exception:
        return None


def zone(a, b):
    n, i, r = a["n"], cell_index(a, b["cell"]), b.get("reach", 2)
    return {k for k in range(n * n) if max(abs(k % n - i % n), abs(k // n - i // n)) <= r}


def get_reports(a):
    """คืนค่า {ช่อง: [(เวลา, ความเปียก 0-1), ...]} จากทุกบอร์ด ย้อนหลัง 12 ชม."""
    nodes = {}
    for b in a["boards"]:
        i = cell_index(a, b["cell"])
        try:
            with urllib.request.urlopen(f"{NTFY}{b['topic']}-in/json?poll=1&since=12h", timeout=20) as r:
                lines = r.read().decode("utf-8").splitlines()
        except Exception as e:
            print("อ่านบอร์ด", b["cell"], "ไม่ได้:", e)
            continue
        for ln in lines:
            try:
                e = json.loads(ln)
                if e.get("event") == "message":
                    nodes.setdefault(i, []).append((e["time"], int(e["message"].strip().split("|")[-1]) / 100))
            except Exception:
                pass
    return nodes


def stats(a, h):
    """นับจากประวัติ: cnt[ช่อง] = ท่วมกี่ครั้ง, co[(ช่อง,ช่อง)] = ท่วมพร้อมกันกี่ครั้ง"""
    eps = [set(e["cells"]) for e in h["episodes"]] + [{c.upper() for c in x} for x in a.get("past", [])]
    cnt, co = {}, {}
    for s in eps:
        for c in s:
            cnt[c] = cnt.get(c, 0) + 1
            for d in s:
                if c != d:
                    co[(c, d)] = co.get((c, d), 0) + 1
    return cnt, co


def calc(a, nodes, cnt, co):
    """ระดับแต่ละช่อง: 0 ปกติ, 1 เหลือง, 2 ส้ม, 3 แดง + โอกาสท่วม 0-1"""
    n = a["n"]
    el = a.get("el") or [0] * (n * n)
    last = {i: v[-1] for i, v in nodes.items()}   # ตัวที่เงียบหายยังใช้ค่าสุดท้าย เผื่อโดนน้ำจนดับ
    wet = {i: s for i, (t, s) in last.items() if s >= 0.25}
    sustained = any(len(v) >= 3 and all(s >= 0.25 for _, s in v[-3:]) for v in nodes.values())
    rn = max(wet.values(), default=0)
    if sustained:
        rn = max(rn, 0.8)                          # เปียกต่อเนื่อง = ฝนหนัก น้ำแผ่ไกลขึ้น
    R = 1.5 + 2.5 * rn
    lv, risk = [0] * (n * n), [0.0] * (n * n)
    for i in range(n * n):
        if i in wet:                               # ช่องที่มีบอร์ดเปียก = ข้อมูลจริง
            lv[i], risk[i] = (3, 1.0) if wet[i] >= 0.6 else (2, 0.75)
            continue
        if not wet:
            continue
        ni, p, ph = name(a, i), 1.0, 0.0
        for j, s in wet.items():
            d = math.hypot(i % n - j % n, i // n - j // n)
            dh = el[j] - el[i]                     # บวก = ช่องนี้ต่ำกว่าจุดที่เปียก
            prox = max(0, 1 - (d - 1) / R)
            ef = 1 if dh >= 0 else max(0, 1 + dh / 0.8)
            p *= 1 - prox * ef * (0.4 + 0.6 * s)   # 1) ระยะ + ความสูง (ท่วมจุดหนึ่ง จุดใกล้ๆ ก็เสี่ยง)
            nj = name(a, j)
            if cnt.get(nj):                        # 2) ประวัติ: เคยท่วมพร้อมจุดนี้กี่ส่วนของครั้งที่ท่วม
                ph = max(ph, co.get((ni, nj), 0) / (cnt[nj] + 1) * (0.6 + 0.4 * s))
        vul = min(0.25, 0.08 * cnt.get(ni, 0))     # 3) ช่องที่เคยท่วมบ่อย เสี่ยงกว่าปกติ
        r = 1 - p * (1 - ph) * (1 - vul)
        risk[i], lv[i] = r, (2 if r >= 0.5 else 1 if r >= 0.15 else 0)
    return lv, risk, last, wet


def track(a, st, wet, now):
    """จดประวัติ: น้ำเริ่ม = เปิดเหตุการณ์, น้ำหายเกิน GAP_MIN = ปิดและนับเป็น 1 ครั้ง คืน True ถ้ามีอะไรเปลี่ยน"""
    h, ch = st["hist"], False
    wc = {name(a, i): (3 if s >= 0.6 else 2) for i, s in wet.items()}
    op = h.get("open")
    if wc:
        if not op:
            op = h["open"] = {"s": now, "last": now, "cells": {}}
            ch = True
        op["last"] = now
        for c, v in wc.items():
            if op["cells"].get(c, 0) < v:
                op["cells"][c], ch = v, True
    elif op and now - op["last"] >= GAP_MIN * 60:
        h["episodes"].append({"s": op["s"], "e": op["last"], "cells": op["cells"]})
        h["open"], ch = None, True
    return ch


def hist_msg(a, st):
    """สรุปประวัติแบบกะทัดรัด (ไม่เกิน ~4KB) ให้หน้าเว็บอ่าน"""
    h = st["hist"]
    cnt, _ = stats(a, h)
    last = {}
    for e in h["episodes"]:
        for c in e["cells"]:
            last[c] = max(last.get(c, 0), e["e"])
    op = h.get("open")
    if op:
        for c in op["cells"]:
            cnt[c], last[c] = cnt.get(c, 0) + 1, max(last.get(c, 0), op["last"])
    eps = [[int(e["s"]), int(e["e"]), e["cells"]] for e in h["episodes"][-12:]]
    while True:
        out = {"c": {c: [cnt[c], int(last.get(c, 0))] for c in cnt}, "e": eps}
        if op: out["o"] = [int(op["s"]), op["cells"]]
        s = json.dumps(out, separators=(",", ":"), ensure_ascii=False)
        if len(s.encode("utf-8")) < 3800 or not eps:
            return s
        eps = eps[1:]


def state_msg(a, lv, risk):   # n|m|lat|lng|ระดับ|ความสูง|โอกาส% (live.html อ่านรูปแบบนี้)
    el = ",".join(f"{e:.2f}" for e in a["el"]) if a.get("el") else "-"
    return "|".join([str(a["n"]), str(a["m"]), f"{a['lat']:.5f}", f"{a['lng']:.5f}",
                     "".join(map(str, lv)), el, ",".join(str(round(r * 100)) for r in risk)])


def post(topic, msg, title=None, pri=3, tags=None, click=None):
    body = {"topic": topic, "message": msg, "priority": pri}
    if title: body["title"] = title
    if tags: body["tags"] = tags
    if click: body["click"] = click
    req = urllib.request.Request(NTFY, json.dumps(body).encode("utf-8"), {"Content-Type": "application/json"})
    urllib.request.urlopen(req, timeout=20).read()


def summary(a, lv, cells):
    g = lambda L: " ".join(name(a, i) for i in sorted(cells) if lv[i] == L) or "-"
    return f"แดง: {g(3)}\nส้ม: {g(2)}\nเหลือง: {g(1)}\nแตะเพื่อดูรายละเอียด"


def check(a, st):
    now, T = time.time(), a["topic"]
    cnt, co = stats(a, st["hist"])
    lv, risk, last, wet = calc(a, get_reports(a), cnt, co)
    ch = track(a, st, wet, now)
    prev, N2 = st["prev"], len(lv)
    up = [i for i in range(N2) if lv[i] >= 2 and lv[i] > prev[i]]
    ttl = lambda top: "🔴 น้ำท่วมแล้ว" if top == 3 else "🟠 เสี่ยงน้ำท่วมสูง"
    if up:   # มีช่องใหม่ขึ้นเป็นส้ม/แดง -> แจ้งเตือนเสียงก่อน
        top = max(lv[i] for i in up)
        post(T, summary(a, lv, range(N2)), f"{ttl(top)} · {a['name']}", 5 if top == 3 else 4, ["rotating_light"], f"{WEB}?t={T}")
        for b in a["boards"]:   # บอร์ดแต่ละตัวรับเฉพาะโซนของตัวเอง
            z = zone(a, b)
            ub = [i for i in up if i in z]
            if ub:
                tb = max(lv[i] for i in ub)
                post(b["topic"], summary(a, lv, z), f"{ttl(tb)} · โซน {b['cell']}", 5 if tb == 3 else 4, ["rotating_light"], f"{WEB}?t={T}")
    elif max(prev) > 0 and max(lv) == 0:
        for tp in [T] + [b["topic"] for b in a["boards"]]:
            post(tp, "ไม่พบน้ำที่เซนเซอร์ใดเลย", f"🟢 กลับสู่ปกติ · {a['name']}", 2, ["white_check_mark"])
    sm = state_msg(a, lv, risk)
    if sm != st["pstate"] or now - st["tstate"] >= REPUB_MIN * 60:
        post(T + "-state", sm, pri=1)
        st["pstate"], st["tstate"] = sm, now
    if ch or now - st["thist"] >= REPUB_MIN * 60:
        post(T + "-hist", hist_msg(a, st), pri=1)
        st["thist"] = now
    st["prev"] = lv
    for b in a["boards"]:        # เช็กบอร์ดที่เงียบหาย
        i = cell_index(a, b["cell"])
        t = last[i][0] if i in last else 0
        if now - t > STALE_MIN * 60:
            if b["cell"] not in st["warned"]:
                st["warned"].add(b["cell"])
                post(T, f"ไม่มีรายงานเกิน {STALE_MIN} นาที (แบตหมดหรือหลุด WiFi) ควรไปตรวจ",
                     f"⚠️ บอร์ด {b['cell']} เงียบหาย · {a['name']}", 3, ["warning"])
        else:
            st["warned"].discard(b["cell"])
    print(time.strftime("%H:%M:%S"), a["name"], "".join(map(str, lv)))
    return ch


def main():
    topics = [a["topic"] for a in AREAS] + [b["topic"] for a in AREAS for b in a["boards"]]
    assert len(set(topics)) == len(topics), "topic ทุกตัวต้องไม่ซ้ำกัน"
    try:
        with open(HIST_FILE, encoding="utf-8") as f:
            allh = json.load(f)
    except Exception:
        allh = {}
    state = {}
    for a in AREAS:
        assert a.get("el") is None or len(a["el"]) == a["n"] ** 2, f"el ของ {a['name']} ต้องมี n*n ค่า"
        for b in a["boards"]:
            assert cell_index(a, b["cell"]) is not None, f"ช่อง {b['cell']} อยู่นอกตาราง"
        h = allh.setdefault(a["topic"], {})
        h.setdefault("episodes", []); h.setdefault("open", None)
        state[a["topic"]] = {"prev": [0] * a["n"] ** 2, "warned": set(), "hist": h, "pstate": None, "tstate": 0, "thist": 0}
    print("เริ่มทำงาน (Ctrl+C เพื่อหยุด) ประวัติเก็บที่", HIST_FILE)
    for a in AREAS:
        print(" -", a["name"], "| topic หลัก:", a["topic"])
        for b in a["boards"]:
            print("     บอร์ด", b["cell"], "| topic:", b["topic"])
    while True:
        for a in AREAS:
            try:
                if check(a, state[a["topic"]]):
                    with open(HIST_FILE, "w", encoding="utf-8") as f:
                        json.dump(allh, f, ensure_ascii=False)
            except Exception as e:
                print("error", a["name"], ":", e)
        time.sleep(POLL_SEC)


if __name__ == "__main__":
    main()
