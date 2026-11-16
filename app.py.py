"""우리끼리 TRIP · 설정 없이 시작하는 가족용 Streamlit 앱
배포: app.py와 requirements.txt를 GitHub에 업로드하고 Streamlit Community Cloud에서
app.py를 선택해 배포하면 끝. 토큰, Secrets, 외부 데이터베이스 설정 불필요.
첫 접속부터 홈을 바로 표시. 기본 가족은 엄마·아빠·나·동생이며 운영 > 가족 설정에서 변경.
같은 앱 주소를 쓰는 모든 사람이 함께 사용한다.
보안: 로그인 없는 공용 가족 공간이다. 앱 주소를 가족끼리만 공유하고 민감정보 입력 금지.
저장: 서버의 SQLite 파일 사용. 같은 서버의 동시 사용자에게 공유되고 새로고침 후 유지.
Community Cloud 재부팅/절전 복귀/재배포/서버 교체 때 영구 보존은 보장되지 않는다.
여러 서버 복제본 간 동기화는 지원하지 않는다. 개인별 인증/자동 push 동기화 없음.
스마트폰 기본 글자 크기에 맞춘 컴팩트 디자인이며 아주 작은 화면에서는 스크롤 허용.
"""
from __future__ import annotations

import base64
import calendar
import copy
import hashlib
import hmac
import html
import io
import json
import secrets
from datetime import date, datetime, timedelta, timezone

import qrcode
import sqlite3
from pathlib import Path
import streamlit as st
import streamlit.components.v1 as components

# Streamlit の初期テーマ。既存環境で設定不可でも CSS で統一する。
try:
    from streamlit import config as _theme
    _theme.set_option("theme.primaryColor", "#23644d")
    _theme.set_option("theme.backgroundColor", "#f5f4ef")
    _theme.set_option("theme.secondaryBackgroundColor", "#ffffff")
    _theme.set_option("theme.textColor", "#182a23")
except Exception:
    pass
st.set_page_config(page_title="우리끼리 · 가족 여행 뽑기", page_icon="🎒", layout="centered", initial_sidebar_state="collapsed")
KST = timezone(timedelta(hours=9))
KINDS = {"소모임": (1, "☕", "외식 · 피크닉"), "중모임": (2, "🚗", "당일치기 여행"), "대모임": (6, "🏡", "숙박 여행")}


def today():
    return datetime.now(KST).date()


def add_months(d, months):
    y, m = divmod(d.year * 12 + d.month - 1 + months, 12)
    return date(y, m + 1, min(d.day, calendar.monthrange(y, m + 1)[1]))


def candidates(submissions, members, nights, floor):
    """모든 구성원이 제출해야 확정. 숙박은 체크아웃일까지 연속 날짜 필요."""
    if not all(name in submissions for name in members):
        return []
    common = set.intersection(*(set(submissions[name]["dates"]) for name in members))
    result = []
    for value in sorted(common):
        start = date.fromisoformat(value)
        if start >= floor and all((start + timedelta(days=i)).isoformat() in common for i in range(nights + 1)):
            result.append(value)
    return result


def digest_pin(pin, salt):
    return hashlib.pbkdf2_hmac("sha256", pin.encode(), salt.encode(), 200_000).hex()


def verify_admin(room, pin):
    # 별도 PIN 없이 사용하는 신뢰 기반 가족 공용 공간입니다.
    return True


def tokens(value):
    return list(dict.fromkeys(v.strip()[:45] for v in value.replace("，", ",").split(",") if v.strip()))[:3]


def escaped(value):
    return html.escape(str(value))


class StoreError(Exception):
    pass


class RuleError(Exception):
    pass


class LocalStore:
    """동일 서버 접속자들이 공유하는 트랜잭션 기반 SQLite 저장소."""
    def __init__(self):
        self.path = str(Path(__file__).resolve().parent / "family_trip.sqlite3")
        try:
            with sqlite3.connect(self.path, timeout=20) as conn:
                conn.execute("CREATE TABLE IF NOT EXISTS family (id INTEGER PRIMARY KEY, data TEXT NOT NULL)")
        except sqlite3.Error as exc:
            raise StoreError("가족 저장 공간을 열 수 없습니다. 잠시 후 다시 시도하세요.") from exc

    def load(self):
        try:
            with sqlite3.connect(self.path, timeout=20) as conn:
                row = conn.execute("SELECT data FROM family WHERE id=1").fetchone()
            return (json.loads(row[0]) if row else None), None
        except (sqlite3.Error, ValueError) as exc:
            raise StoreError("가족 데이터를 읽을 수 없습니다. 잠시 후 다시 시도하세요.") from exc

    def change(self, operation):
        conn = sqlite3.connect(self.path, timeout=20)
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT data FROM family WHERE id=1").fetchone()
            room = json.loads(row[0]) if row else None
            updated = operation(copy.deepcopy(room))
            conn.execute("INSERT INTO family(id,data) VALUES(1,?) ON CONFLICT(id) DO UPDATE SET data=excluded.data",
                         (json.dumps(updated, ensure_ascii=False),))
            conn.commit()
            return updated
        except sqlite3.Error as exc:
            conn.rollback()
            raise StoreError("저장하지 못했습니다. 새로고침 후 다시 시도하세요.") from exc
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()


def active(room, round_id):
    if room is None or room["round"]["id"] != round_id:
        raise RuleError("회차가 바뀌었습니다. 새로고침 후 다시 입력해 주세요.")
    return room["round"]


def new_round(kind, nights, start, days=21):
    return {"id": secrets.token_hex(8), "kind": kind, "nights": nights,
            "window_start": start.isoformat(), "window_end": (start + timedelta(days=days-1)).isoformat(),
            "submissions": {}, "confirmed": None, "draw": None, "completed": False}


def save_submission(room, round_id, member, entry, floor):
    r = active(room, round_id)
    if r["confirmed"] or r["completed"]:
        raise RuleError("이미 확정된 회차입니다. 날짜와 취향을 변경할 수 없습니다.")
    if member not in room["members"]:
        raise RuleError("가족 구성원을 확인해 주세요.")
    if any(not (r["window_start"] <= d <= r["window_end"]) or date.fromisoformat(d) < floor for d in entry["dates"]):
        raise RuleError("지난 날짜 또는 모집 범위 밖 날짜가 있습니다.")
    r["submissions"][member] = entry
    options = candidates(r["submissions"], room["members"], r["nights"], floor)
    if options:
        r["confirmed"] = options[0]
    return room


def make_ladder(pool):
    # 무작위 출발선은 각 후보에 정확히 1:1 연결되어 같은 당첨 확률을 가진다.
    n = len(pool)
    rows = []
    for _ in range(12):
        row = []
        j = 0
        while j < n-1:
            if secrets.randbelow(2):
                row.append(j)
                j += 2
            else:
                j += 1
        rows.append(row)
    start = secrets.randbelow(n)
    pos = start
    route = []
    for row in rows:
        old = pos
        if pos in row:
            pos += 1
        elif pos-1 in row:
            pos -= 1
        route.append([old, pos])
    return {"pool": pool, "count": n, "rows": rows, "start": start,
            "route": route, "end": pos, "winner": pool[pos]}


def draw_options(room, round_id, pin=""):
    r = active(room, round_id)
    if not r["confirmed"]:
        raise RuleError("날짜 확정 후 시작하세요.")
    if r["draw"]:
        raise RuleError("이미 결정된 회차입니다. 재추첨할 수 없어요.")
    result = {}
    for field in ("concept", "place", "food"):
        pool = sorted({v for entry in r["submissions"].values() for v in entry[field]})
        if field == "place":
            pool = sorted(set(pool) | set(r.get("old_candidates", [])))
        if not pool:
            raise RuleError("각 항목에 후보가 필요해요.")
        result[field] = make_ladder(pool)
    r["draw"] = result
    saved = room.setdefault("wishlist", [])
    for place in result["place"]["pool"]:
        if place not in saved:
            saved.append(place)
    return room


def encode_photo(upload):
    try:
        from PIL import Image, ImageOps
        if upload.size > 10*1024*1024: raise ValueError("사진은 10MB 이하로 선택하세요.")
        im=Image.open(upload)
        if im.width*im.height>30_000_000: raise ValueError("사진 크기가 너무 커요.")
        im=ImageOps.exif_transpose(im).convert("RGB"); im.thumbnail((1280,1280))
        buf=io.BytesIO(); im.save(buf,format="JPEG",quality=82)
        return base64.b64encode(buf.getvalue()).decode()
    except Exception as exc:
        raise ValueError("사진을 읽을 수 없어요. JPG 또는 PNG로 다시 시도하세요.") from exc


def ladder_view(data):
    if "rows" not in data:
        st.info("이전 버전 결과: " + data["winner"])
        return
    # Canvas: 사다리와 저장된 경로만 애니메이션. 재생은 재추첨하지 않는다.
    payload = json.dumps(data, ensure_ascii=True).replace("<", "\\u003c")
    components.html('''<canvas id="c" style="width:100%;height:270px"></canvas>
<button id="play" style="border:0;border-radius:10px;padding:8px 16px;background:#23644d;color:white">경로 다시 보기</button>
<script>
const d=PAYLOAD,c=document.getElementById('c'),x=c.getContext('2d');
const w=1200,h=540;c.width=w;c.height=h;
let timer;const n=d.pool.length, X=i=>n===1?w/2:55+i*(w-110)/(n-1), Y=i=>65+i*30;
function base(){x.clearRect(0,0,w,h);x.lineWidth=3;x.strokeStyle='#bccfc2';
for(let i=0;i<n;i++){x.beginPath();x.moveTo(X(i),30);x.lineTo(X(i),450);x.stroke();}
d.rows.forEach((r,k)=>r.forEach(j=>{x.beginPath();x.moveTo(X(j),Y(k));x.lineTo(X(j+1),Y(k));x.stroke();}));
x.font='22px sans-serif';x.textAlign='center';x.fillStyle='#173e32';
for(let i=0;i<n;i++){x.fillText(i===d.start?'▼ 출발':String(i+1),X(i),23);x.fillText(String(i+1),X(i),490);}}
function play(){clearInterval(timer);base();let k=0,pos=d.start,prev=30;
x.strokeStyle='#ed8853';x.lineWidth=7;
timer=setInterval(()=>{x.beginPath();x.moveTo(X(pos),prev);
if(k<d.route.length){let [a,b]=d.route[k];x.lineTo(X(a),Y(k));x.lineTo(X(b),Y(k));pos=b;prev=Y(k);k++;}
else{x.lineTo(X(pos),450);clearInterval(timer);}x.stroke();},180);}
document.getElementById('play').onclick=play;play();
</script>'''.replace("PAYLOAD", payload), height=315)
    st.caption(" · ".join(f"{i+1}: {v}" for i,v in enumerate(data["pool"])))
    st.success("도착 결과: " + data["winner"])


def month_calendar(room, r, member=None):
    key = f"month_{r['id']}_{member or 'shared'}"
    base = date.fromisoformat(r["window_start"]).replace(day=1)
    if key not in st.session_state:
        st.session_state[key] = base.isoformat()
    month = date.fromisoformat(st.session_state[key])
    left, middle, right = st.columns([1,3,1])
    if left.button("‹", key=key+"prev", use_container_width=True):
        st.session_state[key] = add_months(month,-1).isoformat()
        st.rerun()
    middle.markdown(f"**{month.year}년 {month.month}월**")
    if right.button("›", key=key+"next", use_container_width=True):
        st.session_state[key] = add_months(month,1).isoformat()
        st.rerun()
    selection_key = f"dates_{r['id']}_{member}"
    if member and selection_key not in st.session_state:
        st.session_state[selection_key] = r["submissions"].get(member,{}).get("dates",[]).copy()
    headers=st.columns(7)
    for col, label in zip(headers,["월","화","수","목","금","토","일"]):
        col.markdown(f"<div style='text-align:center;font-size:11px'>{label}</div>",unsafe_allow_html=True)
    for week in calendar.Calendar().monthdatescalendar(month.year,month.month):
        cols=st.columns(7, gap="small")
        for col, day in zip(cols,week):
            ds=day.isoformat()
            count=sum(ds in e["dates"] for e in r["submissions"].values())
            selected=member and ds in st.session_state[selection_key]
            confirmed=bool(r["confirmed"] and date.fromisoformat(r["confirmed"]) <= day <= date.fromisoformat(r["confirmed"])+timedelta(days=r["nights"]))
            text=f"{day.day}" + ("★" if confirmed else ("✓" if selected else (("●" if count==len(room["members"]) else f"·{count}") if not member and count else "")))
            disabled=day.month != month.month or (bool(member) and (r["confirmed"] is not None or ds < r["window_start"] or ds > r["window_end"] or day < today()))
            if col.button(text,key=f"{key}_{ds}",disabled=disabled,use_container_width=True,type="primary" if selected or confirmed else "secondary"):
                if member:
                    chosen=st.session_state[selection_key]
                    if ds in chosen: chosen.remove(ds)
                    else: chosen.append(ds)
                else:
                    st.session_state["inspect_date"] = ds
                st.rerun()
    if member:
        return sorted(st.session_state[selection_key])
    ds=st.session_state.get("inspect_date",today().isoformat())
    yes=[m for m in room["members"] if ds in r["submissions"].get(m,{}).get("dates",[])]
    no=[m for m in room["members"] if m in r["submissions"] and m not in yes]
    missing=[m for m in room["members"] if m not in r["submissions"]]
    st.caption("날짜·숫자 = 가능 인원 / 전원 가능은 ● / ★ = 확정 일정")
    st.markdown(f'<div class="card"><b>{escaped(ds)} · {len(yes)}/{len(room["members"])}명 가능</b><br>가능: {escaped(", ".join(yes) or "없음")}<br>불가: {escaped(", ".join(no) or "없음")}<br>미제출: {escaped(", ".join(missing) or "없음")}</div>',unsafe_allow_html=True)
    if r["confirmed"]:
        st.caption(f"확정: {r['confirmed']} · {r['nights']}박 {r['nights']+1}일" + (" · "+r["draw"]["place"]["winner"]+" / "+r["draw"]["food"]["winner"] if r["draw"] else ""))


st.markdown("""<style>
:root {color-scheme:light;}
[data-testid="stHorizontalBlock"] {gap:0.25rem;}
[data-testid="stHorizontalBlock"] [data-testid="stColumn"] {min-width:0!important;}
[data-testid="stHorizontalBlock"] button {padding:1px;min-height:32px;font-size:11px;}
.stApp {background:#f5f4ef;color:#182a23;}
header[data-testid="stHeader"] {background:transparent;height:0;}
.block-container {max-width:460px;padding:0.7rem 0.85rem 1rem;}
[data-testid="stToolbar"], #MainMenu, footer {display:none;}
h1,h2,h3 {line-height:1.4;} p {line-height:1.65;} .brand {font-size:23px;font-weight:850;letter-spacing:-1px;}
.sub {font-size:12px;color:#56675e;margin:2px 0 9px;}
.hero {background:#173e32;color:#fff;border-radius:20px;padding:15px 18px;margin:8px 0;}
.hero small {color:#d9e9df;} .big {font-size:35px;font-weight:850;line-height:1.2;}
.hero .line {font-size:13px;margin-top:5px;color:#e8f2ec;}
.card {background:white;border:1px solid #e1e5dd;border-radius:16px;padding:12px 14px;margin:7px 0;}
.card b {font-size:15px;} .card small {font-size:12px;color:#526259;}
.triplet {display:grid;grid-template-columns:repeat(3,1fr);gap:7px;margin:9px 0;}
.cool {background:#fff;border:1px solid #e1e5dd;border-radius:14px;text-align:center;padding:10px 2px;font-size:12px;}
.cool strong {display:block;font-size:17px;color:#1e6148;margin:3px 0;}
.chips {font-size:12px;line-height:1.7;color:#405a4a;}
.rule {font-size:11px;color:#5d6c63;line-height:1.5;margin:7px 0;}
.stButton button,.stFormSubmitButton button,.stLinkButton a {border-radius:12px;min-height:38px;font-weight:650;}
.stButton button[kind="primary"],.stFormSubmitButton button[kind="primary"] {background:#23644d;border:0;}
[data-testid="stVerticalBlock"] {gap:0.4rem;}
[data-testid="stWidgetLabel"] p {font-size:12px;}
[data-testid="stForm"] {border:0;padding:0;}
.stTextInput input {min-height:37px;} [data-testid="stCaptionContainer"] {font-size:11px;}
[data-testid="stAlert"] {padding:0.5rem;font-size:12px;}
[data-testid="stPills"] button {padding:5px 10px;font-size:12px;}
@media (max-width:380px) {.block-container{padding-left:0.6rem;padding-right:0.6rem;}.hero{padding:11px 14px;}.big{font-size:29px;}}
/* Readable compact layout: no fixed-height text or clipped content. */
.block-container {padding-top:1rem;padding-bottom:2rem;}
[data-testid="stVerticalBlock"] {gap:0.8rem;}
[data-testid="stCaptionContainer"] p {line-height:1.65!important;margin-bottom:0.3rem!important;overflow-wrap:anywhere;}
[data-testid="stMarkdownContainer"] p {line-height:1.65;overflow-wrap:anywhere;}
[data-testid="stHorizontalBlock"] {align-items:center;}
[data-testid="stWidgetLabel"] p {font-size:13px;line-height:1.55;}
.card {line-height:1.7;padding:14px 16px;overflow-wrap:anywhere;}
.rule,.chips {line-height:1.8;}
[data-testid="stPills"] button[aria-pressed="true"] {background:#23644d!important;color:white!important;border-color:#23644d!important;}
[data-testid="stBaseButton-primary"],button[kind="primaryFormSubmit"] {background:#23644d!important;border-color:#23644d!important;color:white!important;}
[data-testid="stCheckbox"] {accent-color:#23644d;}
[data-baseweb="slider"] {accent-color:#23644d;}
[data-testid="stPills"] button {font-size:12px;padding:5px 8px;}
@media(max-width:420px){.block-container {padding-left:0.75rem;padding-right:0.75rem;}.big{font-size:28px;}}
</style>""", unsafe_allow_html=True)
st.markdown('<div class="brand">🎒 우리끼리 <span style="color:#438360">TRIP</span></div><div class="sub">날짜는 함께 맞추고, 취향은 공정하게 뽑아요.</div>', unsafe_allow_html=True)

try:
    store = LocalStore()
    room, _ = store.load()
except StoreError as exc:
    st.error(str(exc))
    st.stop()

if room is None:
    # 첫 방문도 시작/설정 화면 없이 곧바로 홈으로 진입한다.
    def initialize(old):
        if old is not None:
            return old
        return {"version": 4, "members": ["엄마", "아빠", "나", "동생"],
                "last": {}, "history": [],
                "round": new_round("소모임", 0, today())}
    try:
        room = store.change(initialize)
    except StoreError as exc:
        st.error(str(exc))
        st.stop()

r = room["round"]
round_id = r["id"]
now = today()
if "next_nav" in st.session_state:
    st.session_state["nav"]=st.session_state.pop("next_nav")
nav = st.pills("메뉴", ["홈", "달력", "사다리", "추억", "관리", "QR"], default="홈", label_visibility="collapsed", key="nav")


def mutate(operation, message):
    try:
        store.change(operation)
        st.session_state.flash = message
        st.rerun()
    except (StoreError, RuleError) as exc:
        st.error(str(exc))


if st.session_state.get("flash"):
    st.success(st.session_state.pop("flash"))

if nav == "홈":
    home_mode=st.pills("홈 보기",["여행 현황","오늘의 여행","준비물"],default="여행 현황",label_visibility="collapsed")
    if home_mode=="준비물":
        items=r.get("packing",[])
        st.caption(f"🧳 {sum(bool(x['done']) for x in items)}/{len(items)}개 준비 완료")
        person=st.selectbox("나는",room["members"],key="packing_person")
        page_count=max(1,(len(items)+3)//4)
        page=st.number_input("준비물 페이지",min_value=1,max_value=page_count,value=1,step=1) if page_count>1 else 1
        for item in items[(page-1)*4:page*4]:
            left,middle,right=st.columns([1,3,2])
            checked=left.checkbox("완료",value=item["done"],key="done_"+round_id+item["id"],label_visibility="collapsed")
            middle.markdown(f"**{escaped(item['name'])}**")
            middle.caption(item.get("owner") or "담당 없음")
            if checked != item["done"]:
                def set_done(doc, iid=item["id"], value=checked):
                    for x in active(doc,round_id).get("packing",[]):
                        if x["id"]==iid: x["done"]=value
                    return doc
                mutate(set_done,"준비 상태를 저장했어요.")
            if right.button("내가 챙길게",key="claim_"+item["id"],use_container_width=True):
                def claim(doc,iid=item["id"]):
                    for x in active(doc,round_id).get("packing",[]):
                        if x["id"]==iid: x["owner"]=person
                    return doc
                mutate(claim,"담당자를 저장했어요.")
        with st.form("pack_add"):
            value=st.text_input("준비물 추가",max_chars=45)
            if st.form_submit_button("추가",use_container_width=True):
                def add_pack(doc):
                    if not value.strip(): raise RuleError("준비물을 입력하세요.")
                    items=active(doc,round_id).setdefault("packing",[])
                    if not any(x["name"]==value.strip() for x in items):
                        items.append({"id":secrets.token_hex(5),"name":value.strip(),"owner":"","done":False})
                    return doc
                mutate(add_pack,"추가했어요.")
        if room["history"] and st.button("이전 여행 준비물 불러오기",use_container_width=True):
            def import_pack(doc):
                current=active(doc,round_id); items=current.setdefault("packing",[])
                for old in doc["history"][-1].get("packing",[]):
                    if not any(x["name"]==old["name"] for x in items):
                        items.append({"id":secrets.token_hex(5),"name":old["name"],"owner":"","done":False})
                return doc
            mutate(import_pack,"이전 준비물을 불러왔어요.")
    elif home_mode=="오늘의 여행":
        info=r.get("itinerary",{})
        st.markdown("### 🗺️ 오늘의 여행 카드")
        if r["draw"]:
            st.write("📍 "+r["draw"]["place"]["winner"]+" · 🍴 "+r["draw"]["food"]["winner"])
        st.caption("일정: "+str(r["confirmed"] or "미확정"))
        st.write("출발: "+info.get("time","미정")+" · 모이는 곳: "+info.get("meeting","미정"))
        st.caption(f"준비물 {sum(bool(x['done']) for x in r.get('packing',[]))}/{len(r.get('packing',[]))}개 완료")
        if r["draw"]:
            from urllib.parse import quote
            st.link_button("목적지 지도 열기","https://map.naver.com/p/search/"+quote(r["draw"]["place"]["winner"]),use_container_width=True)
        if not r["confirmed"]:
            st.info("날짜 확정 후 여행 정보를 준비하세요.")
        elif st.toggle("여행 정보 수정",value=False):
            with st.form("itinerary"):
                time=st.text_input("출발 시간",value=info.get("time","09:00"),max_chars=20)
                meeting=st.text_input("모이는 장소",value=info.get("meeting",""),max_chars=80)
                if st.form_submit_button("여행 카드 저장",use_container_width=True):
                    def itinerary(doc):
                        active(doc,round_id)["itinerary"]={"time":time,"meeting":meeting}
                        return doc
                    mutate(itinerary,"여행 카드를 저장했어요.")
    else:
        if r["confirmed"]:
            start = date.fromisoformat(r["confirmed"])
            end = start + timedelta(days=r["nights"])
            diff = (start - now).days
            dday = f"D-{diff}" if diff > 0 else ("D-DAY" if diff == 0 else f"D+{abs(diff)}")
            label = "완료한 만남" if r["completed"] else ("진행일이 지났어요 · 관리에서 완료 처리" if now > end else "우리 가족의 약속 · 자동 확정")
            text = f'{start:%m.%d} ~ {end:%m.%d}' if r["nights"] else f'{start:%m.%d} ({["월","화","수","목","금","토","일"][start.weekday()]})'
        else:
            dday = f'일정 제출 {len(r["submissions"])}/{len(room["members"])}명'
            label = "가능한 날짜를 모으고 있어요"
            text = "전원 제출 → 가장 빠른 공통 날짜 자동 확정"
        st.markdown(f'<div class="hero"><small>{label}</small><div class="big">{dday}</div><div class="line">{escaped(r["kind"])} · {text}</div></div>', unsafe_allow_html=True)
        if r["confirmed"] and not r["completed"] and start <= now <= end:
            info=r.get("itinerary",{})
            st.markdown(f'<div class="card"><b>🗺️ 오늘의 여행</b><div class="chips">출발 {escaped(info.get("time","미정"))} · {escaped(info.get("meeting","모이는 장소 미정"))}<br>위 ‘오늘의 여행’에서 지도와 준비물을 확인하세요.</div></div>',unsafe_allow_html=True)
        cards = []
        for kind, (months, icon, _) in KINDS.items():
            last = room["last"].get(kind)
            if last:
                due = add_months(date.fromisoformat(last), months)
                left = max(0, (due - now).days)
                status = "지금 만나요" if left == 0 else f"{left}일 남음"
                foot = f"{due:%m.%d} · {left}일 남음"
            else:
                status, foot = "첫 만남 대기", "완료 이력 없음"
            cards.append(f'<div class="cool">{icon} {kind}<strong>{status}</strong>{foot}</div>')
        st.markdown('<div class="triplet">'+''.join(cards)+'</div>', unsafe_allow_html=True)
        if r["draw"]:
            d = r["draw"]
            st.markdown(f'<div class="card"><b>📍 {escaped(d["place"]["winner"])}</b><div class="chips">✨ {escaped(d["concept"]["winner"])} &nbsp; 🍴 {escaped(d["food"]["winner"])}</div></div>', unsafe_allow_html=True)
        else:
            pending = [name for name in room["members"] if name not in r["submissions"]]
            line = "미제출: " + ", ".join(pending) if pending else ("날짜 확정! 사다리에서 취향을 결정해요." if r["confirmed"] else "공통 날짜 없음 · 입력에서 날짜를 조정해요.")
            st.markdown(f'<div class="card"><b>{KINDS[r["kind"]][1]} {KINDS[r["kind"]][2]}</b><div class="chips">{escaped(line)}</div></div>', unsafe_allow_html=True)
        st.markdown('<div class="rule">처음이라면 관리 → 가족 설정에서 이름을 바꿔 주세요.<br>약속 원칙: 전원 가능한 날짜가 생기면 자동 확정, 이후 날짜 변경·재추첨 불가.<br>주기: 소 1개월 · 중 2개월 · 대 6개월 / 마지막 실제 완료일부터 계산</div>', unsafe_allow_html=True)
        if st.button("↻ 가족 입력 새로고침", use_container_width=True):
            st.rerun()

elif nav == "달력":
    mode = st.pills("달력 보기", ["내 날짜", "가족 일정"], default="가족 일정", label_visibility="collapsed")
    if mode == "가족 일정":
        month_calendar(room,r)
        if st.button("↻ 가족 일정 새로고침",use_container_width=True): st.rerun()
    else:
        member=st.selectbox("가족",room["members"])
        step=st.pills("입력",["날짜 선택","취향 · 저장"],default="날짜 선택",label_visibility="collapsed")
        if step=="날짜 선택":
            st.caption("날짜를 누르면 선택·해제됩니다. 숙박은 귀가일까지 모두 선택하세요.")
            month_calendar(room,r,member)
            chosen=st.session_state.get(f"dates_{r['id']}_{member}",[])
            st.caption(f"선택 {len(chosen)}일 · " + ("제출 완료 · 수정 후 다시 저장 가능" if member in r["submissions"] and not r["confirmed"] else ("확정 · 수정 불가" if r["confirmed"] else "아직 미제출")))
            st.caption("‘취향 · 저장’에서 최종 제출하세요.")
        elif r["confirmed"]:
            st.success("이미 확정된 일정이에요. 입력은 잠겼어요.")
        else:
            prior=r["submissions"].get(member,{})
            chosen=st.session_state.get(f"dates_{r['id']}_{member}",prior.get("dates",[]))
            st.caption(f"선택한 날짜 {len(chosen)}일 · 전원 교집합이 생기면 가장 빠른 일정 자동 확정")
            with st.form("prefs_"+r["id"]+member):
                concept=st.text_input("컨셉 · 쉼표로 최대 3개",value=", ".join(prior.get("concept",[])),placeholder="힐링, 자연",max_chars=140)
                place=st.text_input("장소 · 지역",value=", ".join(prior.get("place",[])),placeholder="서울숲, 부산",max_chars=140)
                food=st.text_input("먹고 싶은 음식",value=", ".join(prior.get("food",[])),placeholder="파스타, 바비큐",max_chars=140)
                if st.form_submit_button("일정 · 취향 저장",type="primary",use_container_width=True):
                    entry={"dates":sorted(chosen),"concept":tokens(concept),"place":tokens(place),"food":tokens(food)}
                    if not all(entry[f] for f in ("concept","place","food")):
                        st.error("컨셉·장소·음식을 각각 입력하세요.")
                    else:
                        mutate(lambda doc:save_submission(doc,round_id,member,entry,today()),"저장했어요!")

elif nav == "사다리":
    st.caption("후보별 동일 확률 · 회차당 한 번 결정 · 경로 재생은 결과를 바꾸지 않아요.")
    if r["draw"]:
        field=st.pills("종류",["place","food","concept"],format_func=lambda x:{"place":"📍 장소","food":"🍴 음식","concept":"✨ 컨셉"}[x],default="place",label_visibility="collapsed")
        ladder_view(r["draw"][field or "place"])
    elif not r["confirmed"]:
        st.info("전원 일정 제출로 날짜를 확정한 후 사다리를 시작해요.")
        st.caption("미제출: "+(", ".join(m for m in room["members"] if m not in r["submissions"]) or "없음 · 공통 날짜를 확인하세요"))
        if st.button("내 일정 입력하러 가기",use_container_width=True):
            st.session_state["next_nav"]="달력"
            st.rerun()
    else:
        st.markdown("### 🪜 우리 가족 사다리")
        st.caption("컨셉·장소·음식의 사다리가 함께 생성되고 저장됩니다. 많은 후보는 번호로 표시하고 아래에서 이름을 확인해요.")
        if st.button("사다리 시작",type="primary",use_container_width=True):
            mutate(lambda doc:draw_options(doc,round_id),"사다리가 결정됐어요! 경로를 확인하세요.")

elif nav == "추억":
    section=st.pills("추억 메뉴",["여행 앨범","사진 미션","가고 싶은 곳"],default="여행 앨범",label_visibility="collapsed")
    if section=="가고 싶은 곳":
        visited={x["draw"]["place"]["winner"] for x in room["history"] if x.get("draw")}
        saved=room.get("wishlist",[])
        if saved:
            place=st.selectbox("보관한 장소",saved,format_func=lambda p:p+(" · 방문 완료" if p in visited else " · 다음에 가요"))
        else: st.info("장소를 추가하거나 사다리를 진행하면 후보들이 보관돼요.")
        with st.form("wishlist_add"):
            place=st.text_input("가고 싶은 곳",max_chars=45)
            if st.form_submit_button("보관함에 추가",use_container_width=True):
                def add(doc):
                    if not place.strip(): raise RuleError("장소 이름을 적어 주세요.")
                    saved=doc.setdefault("wishlist",[])
                    if place.strip() not in saved: saved.append(place.strip())
                    return doc
                mutate(add,"보관했어요.")
    elif section=="사진 미션":
        choices=[r]+[x for x in reversed(room["history"]) if x["id"]!=r["id"]]
        target=st.selectbox("미션 여행",choices,format_func=lambda x:f"{x.get('confirmed') or '준비 중'} · {x['kind']}")
        tid=target["id"]
        mission=st.selectbox("사진 미션",["가족 모두 같은 포즈","가장 맛있었던 음식","여행 전후 표정"])
        st.caption("선택 참여 · 가족별로 사진을 추가하며 다른 가족 사진을 덮어쓰지 않아요.")
        who=st.selectbox("참여 가족",room["members"])
        upload=st.file_uploader("미션 사진",type=["jpg","jpeg","png","webp"])
        if st.button("미션 사진 추가",type="primary",use_container_width=True):
            if not upload: st.error("사진을 선택하세요.")
            else:
                try:
                    encoded=encode_photo(upload)
                    def save_mission(doc):
                        if tid!=doc["round"]["id"] and not any(x["id"]==tid for x in doc["history"]): raise RuleError("여행을 찾을 수 없어요.")
                        doc.setdefault("mission_photos",{}).setdefault(tid,[]).append({"id":secrets.token_hex(6),"author":who,"mission":mission,"photo":encoded})
                        return doc
                    mutate(save_mission,"미션 완료! 사진을 추가했어요.")
                except ValueError as exc: st.error(str(exc))
        photos=room.get("mission_photos",{}).get(tid,[])
        st.caption(f"미션 사진 {len(photos)}장")
        if photos:
            chosen=st.selectbox("모아 보기",list(range(len(photos))),format_func=lambda i:photos[i]["author"]+" · "+photos[i]["mission"]+f" ({i+1})")
            st.image(base64.b64decode(photos[chosen]["photo"]),use_container_width=True)
    else:
        options=room["history"]
        if not options:
            st.info("모임을 완료하면 여행 앨범을 작성할 수 있어요.")
        else:
            target=st.selectbox("여행",options,format_func=lambda x:f"{x.get('confirmed') or '준비 중'} · {x['kind']}")
            tid=target["id"]
            memories=room.get("memories",{}).get(tid,{})
            page=st.pills("보기",["보기","작성"],default="보기",label_visibility="collapsed")
            if section=="사진 미션":
                st.info("📸 선택 미션: 가족 같은 포즈 / 가장 맛있는 음식 / 여행 전후 표정")
            if page=="보기":
                gallery=memories.get("photos",[]).copy()
                if memories.get("photo") and not any(x["photo"]==memories["photo"] for x in gallery): gallery.insert(0,{"author":"대표 사진","photo":memories["photo"]})
                gallery += room.get("mission_photos",{}).get(tid,[])
                if gallery:
                    pi=st.selectbox("여행 사진",list(range(len(gallery))),format_func=lambda i:f"{i+1}/{len(gallery)} · {gallery[i]['author']}")
                    st.image(base64.b64decode(gallery[pi]["photo"]),use_container_width=True)
                st.caption(str(target.get("confirmed") or "날짜 미정")+" · "+target["kind"])

                st.markdown("**"+escaped(memories.get("title","우리 가족 여행"))+"**")
                for name,review in memories.get("reviews",{}).items():
                    st.markdown(f'<div class="card"><b>{escaped(name)} · {"★"*review["rating"]}</b><br>{escaped(review["text"])}<br>{"♥ 다시 가고 싶어요" if review.get("again") else ""}</div>',unsafe_allow_html=True)
                if memories.get("mission"): st.caption("사진 미션: "+memories["mission"])
                if not memories: st.caption("작성 버튼에서 사진과 후기를 남겨 보세요.")
            else:
                author=st.selectbox("작성자",room["members"])
                with st.form("memory_"+tid+author):
                    title=st.text_input("여행 제목",value=memories.get("title","우리 가족 여행"),max_chars=60)
                    review=st.text_input("나의 한 줄 후기",value=memories.get("reviews",{}).get(author,{}).get("text",""),max_chars=160)
                    rating=st.select_slider("만족도",options=[1,2,3,4,5],value=memories.get("reviews",{}).get(author,{}).get("rating",5))
                    again=st.checkbox("다시 가고 싶어요 ♥",value=memories.get("reviews",{}).get(author,{}).get("again",False))
                    mission=memories.get("mission", "")
                    photo=st.file_uploader("여행 사진 추가 · 이전 사진은 유지",type=["jpg","jpeg","png","webp"])
                    if st.form_submit_button("추억 저장",type="primary",use_container_width=True):
                        photo_data=None
                        if photo:
                            try:
                                from PIL import Image, ImageOps
                                if photo.size>10*1024*1024: raise ValueError("사진은 10MB 이하로 선택하세요.")
                                image=Image.open(photo)
                                if image.width*image.height>30_000_000: raise ValueError("사진 크기가 너무 큽니다.")
                                image=ImageOps.exif_transpose(image).convert("RGB")
                                image.thumbnail((1280,1280)); buf=io.BytesIO(); image.save(buf,format="JPEG",quality=82)
                                photo_data=base64.b64encode(buf.getvalue()).decode()
                            except Exception as exc:
                                st.error("사진을 읽을 수 없어요: "+str(exc)); st.stop()
                        def save_memory(doc):
                            if tid != doc["round"]["id"] and not any(x["id"]==tid for x in doc["history"]):
                                raise RuleError("여행 기록을 찾을 수 없어요.")
                            m=doc.setdefault("memories",{}).setdefault(tid,{})
                            m["title"]=title; m["mission"]=mission
                            m.setdefault("reviews",{})[author]={"text":review,"rating":rating,"again":again}
                            if photo_data:
                                m.setdefault("photos",[]).append({"author":author,"photo":photo_data})
                                if not m.get("photo"): m["photo"]=photo_data
                            return doc
                        mutate(save_memory,"추억을 저장했어요.")

elif nav == "관리":
    action = st.pills("운영 메뉴", ["새 여행", "모임 완료", "가족 설정"], default="새 여행", label_visibility="collapsed")
    if action == "가족 설정":
        st.caption("확정 전 또는 완료 후에만 가족을 변경할 수 있어요. 변경하면 현재 회차의 미확정 입력이 초기화됩니다.")
        if "family_draft" not in st.session_state:
            st.session_state.family_draft=room["members"].copy()
        for i,name in enumerate(st.session_state.family_draft):
            left,right=st.columns([4,1])
            left.markdown(f"**{escaped(name)}**")
            if right.button("×",key=f"remove_family_{i}"):
                st.session_state.family_draft.pop(i); st.rerun()
        with st.form("family_add"):
            new_name=st.text_input("추가할 가족 이름",max_chars=12)
            if st.form_submit_button("가족 추가"):
                name=new_name.strip()
                if not name or name in st.session_state.family_draft or len(st.session_state.family_draft)>=8:
                    st.error("중복되지 않는 이름을 입력하세요. 최대 8명이에요.")
                else:
                    st.session_state.family_draft.append(name);st.rerun()
        with st.form("members"):
            agree=st.checkbox("변경 시 현재 미확정 입력이 초기화되는 것에 동의")
            if st.form_submit_button("가족 구성 저장",use_container_width=True):
                members=st.session_state.family_draft.copy()
                def change_members(doc):
                    current = active(doc, round_id)
                    if not agree or not 2 <= len(members) <= 8 or any(len(n)>12 for n in members):
                        raise RuleError("초기화 동의, 가족 2~8명, 별명 12자 이하를 확인하세요.")
                    if current["confirmed"] and not current["completed"]:
                        raise RuleError("확정된 모임을 완료한 후 가족을 변경하세요.")
                    doc["members"] = members
                    if not current["completed"]:
                        doc["round"] = new_round(current["kind"], current["nights"], today())
                    return doc
                mutate(change_members, "가족 구성을 변경했어요.")
    elif action == "모임 완료":
        st.markdown(f'<div class="card"><b>완료할 여행 · {escaped(r["kind"])}</b><br>{escaped(r.get("confirmed") or "날짜 미확정")} · {escaped((r.get("draw") or {}).get("place",{}).get("winner","장소 미정"))}<br>완료하면 추억에 보관되고 다음 쿨타임이 시작돼요.</div>',unsafe_allow_html=True)
        can_complete=bool(r["confirmed"] and not r["completed"] and date.fromisoformat(r["confirmed"])+timedelta(days=r["nights"])<=today())
        if r["completed"]:
            st.success("완료한 여행이에요.")
            if st.button("추억 작성하러 가기",use_container_width=True):
                st.session_state["next_nav"]="추억"; st.rerun()
        st.caption("여행 마지막 날부터 완료 처리 가능. 실제 완료일로 주기를 계산하고 기록에 보관합니다.")
        with st.form("complete"):
            done_day = st.date_input("실제 완료일", value=now, max_value=now)
            pin = ""
            if st.form_submit_button("✅ 함께 다녀왔어요", type="primary", use_container_width=True, disabled=not can_complete):
                def complete(doc):
                    current = active(doc, round_id)
                    if not verify_admin(doc, pin):
                        raise RuleError("운영 PIN이 맞지 않습니다.")
                    if not current["confirmed"] or current["completed"]:
                        raise RuleError("확정 전이거나 이미 완료한 회차입니다.")
                    end_day = date.fromisoformat(current["confirmed"]) + timedelta(days=current["nights"])
                    if not end_day <= done_day <= today():
                        raise RuleError("예정된 마지막 날 이후, 오늘까지의 날짜만 가능합니다.")
                    current["completed"] = True
                    doc["last"][current["kind"]] = done_day.isoformat()
                    doc["history"].append(copy.deepcopy(current))
                    return doc
                mutate(complete, "모임 완료! 추억 메뉴에서 사진과 후기를 남겨 주세요.")
    else:
        st.caption("확정된 회차는 완료해야 다음 회차를 열 수 있어요. 미확정 회차는 새로 모집하면 기존 입력이 초기화됩니다.")
        with st.container():
            c1, c2 = st.columns(2)
            kind = c1.selectbox("모임 규모", list(KINDS))
            if kind=="대모임":
                nights = c2.selectbox("여행 기간", [1, 2, 3], format_func=lambda n: f"{n}박 {n+1}일")
            else:
                nights=0
                c2.caption("여행 기간 · 당일 일정")
            start = st.date_input("가능한 날짜를 고를 시작일", value=now, min_value=now, max_value=now+timedelta(days=365))
            days=st.selectbox("날짜 선택 기간",[14,21,30,60],index=2,format_func=lambda n:f"{n}일")
            pin = ""
            reuse = st.radio("이전에 나왔던 후보지도 넣을까요?", ["이번엔 새 후보만", "골라서 넣기"], horizontal=True)
            old = st.multiselect("보관함에서 선택", room.get("wishlist", []), disabled=reuse != "골라서 넣기")
            agree = st.checkbox("미확정 입력 초기화 또는 완료 후 새 모집에 동의")
            if st.button("새 여행 계획 시작", type="primary", use_container_width=True):
                def restart(doc):
                    current = active(doc, round_id)
                    if not verify_admin(doc, pin):
                        raise RuleError("운영 PIN이 맞지 않습니다.")
                    if not agree:
                        raise RuleError("초기화 동의를 체크해 주세요.")
                    if current["confirmed"] and not current["completed"]:
                        raise RuleError("확정된 일정은 변경 불가. 모임을 완료한 뒤 새로 열어 주세요.")
                    doc["round"] = new_round(kind, nights if kind == "대모임" else 0, start, days)
                    doc["round"]["old_candidates"] = old if reuse == "골라서 넣기" else []
                    return doc
                mutate(restart, "새 여행을 시작했어요. 달력에서 날짜와 취향을 입력하세요.")
        st.caption("주기는 권장 간격이며, 쿨타임 중에도 새 모임은 모집할 수 있습니다.")

elif nav == "QR":
    st.markdown('<div class="card"><b>📱 가족에게 앱 공유</b><div class="chips">같은 주소로 접속하면 바로 함께 사용해요.<br>가족방 코드나 비밀번호는 필요 없어요.</div></div>', unsafe_allow_html=True)
    app_url = st.text_input("현재 앱 주소를 붙여넣으세요", placeholder="https://내앱.streamlit.app", key="share_url").strip()
    if app_url.startswith("https://"):
        from urllib.parse import urlsplit
        parsed = urlsplit(app_url)
        if not parsed.hostname or parsed.username or parsed.password:
            st.warning("올바른 https 앱 주소를 입력해 주세요.")
        else:
            image = qrcode.make(app_url)
            stream = io.BytesIO()
            image.save(stream, format="PNG")
            st.image(stream.getvalue(), width=170)
            st.download_button("QR 이미지 내려받기", stream.getvalue(), "family-trip-qr.png", "image/png", use_container_width=True)
    elif app_url:
        st.warning("https://로 시작하는 앱 주소를 넣어 주세요.")
    else:
        st.caption("주소창의 앱 주소만 복사해 넣으면 QR이 나옵니다. 앱 사용 자체에는 필요하지 않아요.")
    st.caption("주소를 아는 누구나 내용을 보고 수정할 수 있어요. 가족끼리만 공유하고 개인정보는 적지 마세요.")
    st.caption("서버 재시작·재배포 시 저장 기록이 사라질 수 있습니다.")
