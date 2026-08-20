#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""RS.GG 로컬 백엔드 서버.
- 정적 사이트 제공 (index.html, site_data.js)
- 리더보드 30분마다 자동 갱신
- /api/player/<id> : 그 유저를 게임서버에서 실시간 조회
실행:  python3 server.py   →  브라우저에서 http://localhost:8000
"""
import socket, struct, json, os, time, threading, http.server, urllib.parse, urllib.request, zlib, base64, gzip, re, hashlib, hmac
import sys
# ⚠️ Render처럼 stdout이 터미널이 아니면 print가 8KB씩 모였다가 한꺼번에 나간다 → 대시보드 로그가
#    며칠씩 비어 보인다(2026-08-19: 30분마다 찍는 [갱신] 로그가 "5일 전"으로 표시됐다). 줄 단위로 바로 내보낸다.
try: sys.stdout.reconfigure(line_buffering=True); sys.stderr.reconfigure(line_buffering=True)
except Exception: pass

# ── 설정 ─────────────────────────────────────────────
HOST="frontend-a76415741fc3480f.elb.us-east-1.amazonaws.com"; IP="35.153.75.130"
# ⚠️ 게임서버 포트는 **업데이트 때 바뀐다.** 2026-08-04 v2.1.4(빌드 380)에서 21300 → 21400 → 21500(2026-08-20).
#    주소·프로토콜(길이헤더 6B + JSON)은 그대로였고 포트만 옮겨갔다.
#    또 바뀌면 코드 수정 없이 Render 환경변수 RS_PORT만 고치면 된다.
#    (Render가 쓰는 PORT 환경변수는 웹 포트라 이름이 겹치면 안 된다 → RS_PORT)
PORT=int(os.environ.get("RS_PORT","21500"))
PID=os.environ.get("RS_PID",""); SEC=os.environ.get("RS_SEC","")  # 환경변수에서 (코드에 secret 없음)
# 시즌은 박아두지 않는다. 서버에 물어서 참가자가 있는 달을 찾는다.
# (예전엔 "202607"이 박혀 있어서 8월이 됐는데 7월 보드만 갱신하는 사고가 났다.)
SEASON_KEEP=2      # 사이트에서 고를 수 있는 시즌 수 (현재 + 직전)
SEASON_LOOKBACK=6  # 몇 달 전까지 찾아볼지
LB_REFRESH_SEC=1800   # 30분
WEB_PORT=int(os.environ.get("PORT","8000"))
HERE=os.path.dirname(os.path.abspath(__file__))
DATA=os.path.join(HERE,"data")
VALID_PID=re.compile(r"^[0-9a-fA-F-]{8,40}$")   # 게임 player_id(UUID) 형식만 통과
# ── 점검(공개 중지) 모드 ─────────────────────────────
# MAINTENANCE=1 이면 방문자에게 "준비 중" 안내만 보여준다.
# ⚠️ 데이터 수집은 그대로 돈다(리더보드 30분 · 이름검사 6시간 · 랭킹 스냅샷 매일).
# ⚠️ /api/status 는 점검 중에도 열어둔다 — UptimeRobot이 서버를 깨워야 스냅샷이 계속 쌓인다.
#    막아버리면 무료 서버가 잠들어 그날치 랭킹 기록이 통째로 비는 사고가 난다.
# 본인 확인용: 주소 뒤에 ?preview=<MAINTENANCE_KEY> 를 붙이면 쿠키가 심겨 정상 화면이 보인다.
def _on(v): return str(v).strip().lower() not in ("","0","false","no","off")
MAINTENANCE=_on(os.environ.get("MAINTENANCE",""))
MAINT_KEY=os.environ.get("MAINTENANCE_KEY","").strip()
# MAINT_BLANK=1 이면 안내 페이지조차 안 보여준다 — 로고·문구 없는 빈 화면(404).
# 안내문에 RS.GG 로고가 있어서 "사이트 윗부분이 보인다"고 느껴지는 걸 없애기 위한 것.
# ⚠️ 404 + **본문 있음**이어야 브라우저가 자기 오류화면 대신 빈 화면을 그린다.
#    본문을 아예 비우면 크롬이 "HTTP ERROR 404"를 대신 띄운다.
MAINT_BLANK=_on(os.environ.get("MAINT_BLANK",""))
BLANK_HTML=('<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">'
            '<meta name="robots" content="noindex,nofollow"><title>Not Found</title>'
            '</head><body></body></html>')
# ── 게스트 초대 (점검 중에 특정인에게 시간제한 열람 허용) ──
# 발급: /api/guest?key=<MAINTENANCE_KEY>&hours=2  →  {"url": "...?guest=<만료>-<서명>"}
# 링크의 서명은 MAINT_KEY에서 파생되므로 만료시각을 손으로 바꿔도 무효.
# 만료가 지나면 링크를 다시 눌러도, 쿠키가 남아 있어도 서버가 거부한다 → 자동 차단.
def guest_sig(exp):
    return hashlib.sha256(f"rsgg-guest:{exp}:{MAINT_KEY}".encode()).hexdigest()[:20]
def guest_ok(tok):
    """'<만료유닉스초>-<서명>' 토큰 검증."""
    if not (MAINT_KEY and tok): return False
    m=re.match(r"^(\d{1,12})-([0-9a-f]{20})$",tok or "")
    if not m: return False
    exp=int(m.group(1))
    return time.time()<exp and hmac.compare_digest(m.group(2),guest_sig(exp))
HEROES={"108140568":"Dread","80931949":"Khan","61223451":"Vector","631049":"Oni","631047":"Remedy",
"97251807":"Sejin","631045":"Twinkle","631042":"Jagger","631040":"Calibri","631036":"Leo",
"45850136":"Magnus","65893852":"Nova","91887043":"Fury"}
REGION_KR={"ap-northeast-2":"한국","eu-central-1":"유럽","us-east-1":"미국동부",
"us-west-2":"미국서부","ap-southeast-1":"싱가포르","ap-south-1":"인도"}
REGION_REV={v:k for k,v in REGION_KR.items()}
LEVEL_KR={"85682979":"퍼시피카","34624828":"항구","57585175":"하늘공원","36077481":"균열 관측소","41172989":"용광로"}
TN=json.load(open(os.path.join(DATA,"tech_names.json"),encoding="utf-8"))
try: PERK_KR=json.load(open(os.path.join(DATA,"perk_names.json"),encoding="utf-8"))
except Exception: PERK_KR={}
try: COMPS=json.load(open(os.path.join(DATA,"comps.json"),encoding="utf-8"))
except Exception: COMPS={}   # 없으면 조합 탭이 안 나올 뿐, 나머지는 정상
try: HEROSTAT=json.load(open(os.path.join(DATA,"heroes.json"),encoding="utf-8"))
except Exception: HEROSTAT={}  # 없으면 티어 탭만 안 나온다 (build_heroes.py로 생성)
try: PERKSTAT=json.load(open(os.path.join(DATA,"perks.json"),encoding="utf-8"))
except Exception: PERKSTAT={} # 없으면 티어 탭의 특성 섹션만 안 나온다 (build_perks.py로 생성)
C={"level":"52072645","mvp":"86875100","wins":"71495125","matches":"32190379","kills":"85762499",
"deaths":"85762505","damage":"85762483","heal":"85762489","double":"85768264","triple":"85768275",
"final":"85762019","fire":"85842040","playtime":"85762515","pro_rating":"111239832",
"casual_rating":"15599528","pro_matches":"113488083"}
H_PT="85762515"; H_RT="15599528"; H_VIC="77974177"; H_PRE="106323940"; H_PERK="55097452"; H_SKIN="56735408"

# ── 프로토콜 ─────────────────────────────────────────
def fr(e,b):
    e=json.dumps(e,separators=(",",":")).encode(); b=json.dumps(b,separators=(",",":")).encode()
    return struct.pack("<H",len(e))+struct.pack("<I",len(b))+e+b
def rd(s):
    def rn(n):
        buf=b""
        while len(buf)<n:
            c=s.recv(n-len(buf))
            if not c: raise ConnectionError("종료")
            buf+=c
        return buf
    h=rn(6); el=struct.unpack("<H",h[:2])[0]; bl=struct.unpack("<I",h[2:6])[0]
    return json.loads(rn(el).decode("utf-8","ignore")),(json.loads(rn(bl).decode("utf-8","ignore")) if bl else {})
GAME_STATE={"ok":None,"err":"미시도","at":0}   # 게임서버 접속 상태 (/api/status 로 확인)
FRESH={"ok":False}   # 이번 프로세스에서 리더보드를 게임서버에서 한 번이라도 받아왔나
# ⚠️ 재배포 직후엔 저장소에 든 옛 씨앗(leaderboards.json)만 메모리에 있다. 그 상태로 "오늘 기준선"을
#    저장하면 그날 랭킹 변동이 전부 엉터리(씨앗 대비)가 된다 → 씨앗으로는 새 기준선을 만들지 않는다.
def connect():
    # ⚠️ 여기서 실패하면 사이트는 멀쩡한데 데이터만 안 쌓인다. UptimeRobot은 /api/status가
    #    200이라 초록으로 보고한다(2026-08-04에 20시간 동안 못 알아챘다). 그래서 상태를 남긴다.
    try:
        try: s=socket.create_connection((HOST,PORT),timeout=10)
        except Exception: s=socket.create_connection((IP,PORT),timeout=10)
    except Exception as e:
        GAME_STATE.update(ok=False,err=f"연결실패 {type(e).__name__} (포트 {PORT})",at=int(time.time()))
        raise
    # ⚠️ 인증 도중 터지면 소켓을 반드시 닫는다. 예전엔 여기서 예외가 나면 소켓이
    #    영영 안 닫혀서, 게임서버가 느릴 때마다 fd가 1개씩 새어 결국 서버가 접속을 못 받았다.
    try:
        s.settimeout(20)
        s.sendall(fr({"request_id":1,"type":"authenticate_account"},{"player_id":PID,"authentication_secret":SEC})); rd(s)
        GAME_STATE.update(ok=True,err="",at=int(time.time()))
        return s
    except Exception as e:
        GAME_STATE.update(ok=False,err=f"인증실패 {type(e).__name__}",at=int(time.time()))
        try: s.close()
        except Exception: pass
        raise
def skin_name(v):
    if v is None: return None
    nm=TN.get(str(v))
    if not nm: return None
    p=nm.split("_"); return " ".join(p[2:]) if len(p)>2 else nm
# 퍽 슬롯 표시 순서: 능력1 → 능력2 → 얼티밋 → 무기 (게임 해금 순서와 같음)
# ⚠️ 슬롯 ID 숫자순으로 정렬하면 무기가 맨 앞으로 와서 게임과 달라진다.
SLOT_ORDER=["48961378","48961386","48961396","48961367"]
PERK_RANK={pid:(v[3] if isinstance(v,list) and len(v)>3 else 9) for pid,v in PERK_KR.items()}
def perk_sorted(ids):
    """퍽 ID 목록을 게임 표시 순서대로."""
    return sorted(ids, key=lambda x: PERK_RANK.get(str(x), 9))
def perk_of(pid):
    """퍽ID → [한글이름, 분류(0~8), 능력아이콘]. 옛 형식(문자열/2칸)도 동작."""
    v=PERK_KR.get(str(pid))
    if isinstance(v,list): return [v[0], v[1] if len(v)>1 else 0, v[2] if len(v)>2 else ""]
    if isinstance(v,str): return [v,0,""]
    return [f"#{pid}",0,""]
def perk_list(hv):
    """장착 퍽을 슬롯 순서대로 한글명 리스트로. (퍽은 counter가 아니라 perk_slots에 있음)"""
    slots=hv.get("perk_slots") or {}
    out=[]
    for sid in sorted(slots, key=lambda x: SLOT_ORDER.index(str(x)) if str(x) in SLOT_ORDER else 9):
        p=(slots.get(sid) or {}).get("selected_perk_id")
        if not p: continue
        out.append(int(p))
    return out
def parse_acc(acc):
    cc=(acc.get("counters_state") or {}).get("counter_collection",{})
    g=lambda k:(cc.get(k,{}) or {}).get("value")
    wins=g(C["wins"]); m=g(C["matches"]); k=g(C["kills"]); d=g(C["deaths"]); pt=g(C["playtime"])
    heroes={}
    for hid,hv in (acc.get("heroes_state") or {}).get("hero_collection",{}).items():
        hcc=hv.get("counter_collection",{}); hg=lambda kk:(hcc.get(kk,{}) or {}).get("value")
        hpt=hg(H_PT)
        heroes[HEROES.get(hid,hid)]={"lv":hv.get("level"),"pt":round(hpt/3600,1) if isinstance(hpt,int) else None,
            "rt":hg(H_RT),"pre":hg(H_PRE),"sk":skin_name(hg(H_SKIN)),"pk":perk_list(hv)}
    regs=acc.get("regions") or []
    # 이름은 정규화해서 담는다 — 게임서버가 주는 이름 끝 공백이 개명으로 잡히면 안 된다(nname 참고)
    return {"n":nname((acc.get("player_state") or {}).get("name","")),"r":regs[0] if regs else "",
        "lv":g(C["level"]),"wr":round(wins/m*100,1) if (isinstance(wins,int) and isinstance(m,int) and m) else None,
        "mvp":g(C["mvp"]),"m":m,"w":wins,"kd":round(k/d,2) if (isinstance(k,int) and isinstance(d,int) and d) else None,
        "k":k,"d":d,"dmg":g(C["damage"]),"heal":g(C["heal"]),"db":g(C["double"]),"tr":g(C["triple"]),
        "fh":g(C["final"]),"pt":round(pt/3600) if isinstance(pt,int) else None,"pm":g(C["pro_matches"]),
        "rm":(acc.get("match_state") or {}).get("match_history",[])[:10],"h":heroes}

# ── 캐시 ─────────────────────────────────────────────
LOCK=threading.Lock()
CACHE={"boards":{},"board_meta":{},"board_labels":[],"hero_labels":[],
       "players":{},"seasons":[],"sdata":{},"player_ranks":{},"last_refresh":0}
NAME_HIST={}
# ── 닉네임 이력 (Upstash Redis 단일키 저장 + 백그라운드 전원검사) ──
# Upstash 콘솔은 UPSTASH_REDIS_REST_URL/TOKEN 이라는 이름으로 알려준다.
# 그 이름 그대로 넣는 실수가 잦아서 둘 다 받는다.
def _env(*names):
    for n in names:
        v=os.environ.get(n)
        if v: return v.strip()
    return ""
UPSTASH_URL=_env("UPSTASH_URL","UPSTASH_REDIS_REST_URL").rstrip("/")
UPSTASH_TOKEN=_env("UPSTASH_TOKEN","UPSTASH_REDIS_REST_TOKEN")
REDIS_STATE={"ok":None,"err":"미시도"}   # /api/status 로 연결 상태 확인용
HIST_STATE={"n":0,"bytes":0}             # 실제 저장한 이력 수/크기 (1MB 한도 감시용)
HIST_KEY="rsgg:namehist"
SWEEP_SEC=int(os.environ.get("SWEEP_SEC","7200"))   # 전원 이름검사 주기(기본 2시간)
# 스윕 1회당 이름을 수확할 경기 수 상한. 첫 스윕은 전원의 최근 경기가 전부 '처음 보는
# 경기'라 수만 건일 수 있다 → 상한을 걸고 나머지는 다음 스윕에 이어서 한다.
SWEEP_MATCH_MAX=int(os.environ.get("SWEEP_MATCH_MAX","15000"))
SEEN_MATCH=set()   # 이름 수확을 마친 경기 id (메모리 — 재시작하면 한 번 다시 훑지만, 수확은 멱등이라 무해)
def redis_cmd(*args):
    if not UPSTASH_URL or not UPSTASH_TOKEN:
        REDIS_STATE.update(ok=False,err="환경변수 없음"); return None
    try:
        req=urllib.request.Request(UPSTASH_URL+"/",data=json.dumps(list(args)).encode(),
            headers={"Authorization":"Bearer "+UPSTASH_TOKEN,"Content-Type":"application/json"})
        with urllib.request.urlopen(req,timeout=8) as r:
            res=json.loads(r.read().decode()).get("result")
        REDIS_STATE.update(ok=True,err="")
        return res
    except Exception as e:
        code=getattr(e,"code",None)
        REDIS_STATE.update(ok=False,err=f"{type(e).__name__}{'/'+str(code) if code else ''}")
        return None
def nname(s):
    """이름 정규화. 게임서버가 주는 이름은 뒤에 공백이 붙어 오는 경우가 있는데
    ("GiGO.91 " vs "GiGO.91"), 그대로 비교하면 공백 하나 차이로 개명이 일어난 걸로
    잡혀 옛 닉네임 목록이 유령 이름으로 더러워진다(수집본 8,180명 중 188명 해당)."""
    return (s or "").strip()

def hist_load():
    """파일(새로 수집한 기준선)과 Redis(누적된 개명 이력)를 **합친다.**
    ⚠️ 예전엔 Redis가 있으면 파일을 통째로 무시했다. 그래서 새로 수집한 유저가
       이름 목록에 안 들어가 검색이 안 되는 사고가 났다(2026-08-01)."""
    base={}
    try:
        base=json.load(open(os.path.join(DATA,"name_history.json"),encoding="utf-8"))
    except Exception as e:
        print("이름이력 파일 로드 실패:",e)
    n_file=len(base)
    n_redis=0
    raw=hist_unpack(redis_cmd("GET",HIST_KEY))
    if raw:
        try:
            d=raw; n_redis=len(d)
            for pid,rec in d.items():
                cur=base.get(pid)
                if not cur:
                    base[pid]=rec               # Redis에만 있는 유저
                    continue
                # 둘 다 있으면: 옛 닉네임은 합치고, 현재 닉네임은 파일(최신 수집) 우선
                merged=list(cur.get("prev") or [])
                for pn in (rec.get("prev") or []):
                    if pn and pn not in merged: merged.append(pn)
                # ⚠️ 파일 cur을 쓰면 Redis의 cur이 그냥 증발한다. 재배포 직전에 개명한
                #    유저가 그 이름으로 검색이 안 되던 원인. 다른 이름이면 옛 이름으로 남긴다
                #    (다음 이름검사에서 실제 이름이 확인되면 cur/prev가 알아서 정리된다).
                rcur=nname(rec.get("cur"))
                if rcur and rcur!=nname(cur.get("cur")) and rcur not in merged: merged.append(rcur)
                cur["prev"]=merged
                if not cur.get("cur"): cur["cur"]=rec.get("cur")
        except Exception as e:
            print("이름이력 Redis 해석 실패:",e)
    # 정규화 + "현재 이름이 옛 이름 목록에도 들어있는" 상태 청소
    for pid,rec in base.items():
        c=nname(rec.get("cur")); rec["cur"]=c
        seen=set(); out=[]
        for pn in (rec.get("prev") or []):
            pn=nname(pn)
            if pn and pn!=c and pn not in seen: seen.add(pn); out.append(pn)
        rec["prev"]=out
    NAME_HIST.clear(); NAME_HIST.update(base)
    keep={pid:rec for pid,rec in NAME_HIST.items() if rec.get("prev")}
    HIST_STATE.update(n=len(keep),bytes=len(hist_pack(keep).encode()))
    print(f"이름이력 {len(NAME_HIST)}명 (파일 {n_file} + Redis {n_redis} 병합) · "
          f"개명 {HIST_STATE['n']}명 / 저장 {HIST_STATE['bytes']}바이트")

def hist_seed():
    """players.json에는 있는데 이름이력에는 없는 유저를 채운다.
    /api/search는 NAME_HIST만 순회하므로, 여기 없으면 **현재 닉네임으로도 검색이 안 된다.**
    지금은 두 파일을 같이 수집해서 개수가 맞지만, 한쪽만 새로 올리면 바로 구멍이 난다."""
    n=0
    with LOCK:
        for pid,p in CACHE["players"].items():
            if pid in NAME_HIST: continue
            NAME_HIST[pid]={"cur":nname(p.get("n")),"prev":[]}; n+=1
    if n: print(f"이름이력 보충 {n}명 (players.json에만 있던 유저)")

# Upstash REST는 요청 1건이 1MB 제한이다. 이름이력을 통째로 넣으면 8,180명에 597KB로
# 이미 절반을 넘겼다(유저가 늘면 조용히 저장 실패 → 개명 이력이 안 쌓인다).
# 그래서 ① 개명 이력이 **있는 유저만** 저장하고(현재 이름은 players.json에서 온다)
#        ② 그래도 커지면 압축한다. 읽기는 옛 형식(생 JSON)도 그대로 받는다.
HIST_ZPREFIX="z:"
HIST_MAX=900_000
def hist_pack(d):
    blob=json.dumps(d,ensure_ascii=False,separators=(",",":"))
    if len(blob.encode())>200_000: blob=HIST_ZPREFIX+_rk_pack(d)
    return blob
def hist_unpack(raw):
    if not raw: return None
    try:
        if raw.startswith(HIST_ZPREFIX): return _rk_unpack(raw[len(HIST_ZPREFIX):])
        return json.loads(raw)
    except Exception as e:
        print("이름이력 해석 실패:",e); return None
def hist_save():
    if not (UPSTASH_URL and UPSTASH_TOKEN): return
    # ⚠️ 복사는 LOCK 안에서 **깊게**(rec는 NAME_HIST와 같은 객체라 얕은 복사만 하면
    #    LOCK 밖 직렬화 중에 다른 스레드가 rec를 고쳐 순회 중 예외가 난다),
    #    직렬화·네트워크 전송은 LOCK 밖에서(락을 쥔 채 통신하면 사이트 전체가 멎는다).
    with LOCK:
        # ⚠️ 필드를 골라 담지 말 것 — ts처럼 나중에 추가한 값이 조용히 사라진다.
        #    dict(rec)로 통째 복사하고 리스트만 새로 만든다(그래야 LOCK 밖에서 안전).
        keep={}
        for pid,rec in NAME_HIST.items():
            if not rec.get("prev"): continue
            q=dict(rec); q["prev"]=list(rec["prev"]); keep[pid]=q
    blob=hist_pack(keep)
    HIST_STATE.update(n=len(keep),bytes=len(blob.encode()))
    if HIST_STATE["bytes"]>HIST_MAX:
        print(f"[이름이력] {HIST_STATE['bytes']}바이트 — Upstash 1MB 한도에 근접, 저장 생략")
        return
    if redis_cmd("SET",HIST_KEY,blob) is None:
        print("[이름이력] Upstash 저장 실패 — 이번 변경분이 저장되지 않았습니다")
def hist_observe(pid,name):
    """이름 관측 → 개명 감지시 기록. 반환 (prev리스트, 변경여부)
    ⚠️ NAME_HIST는 /api/search가 순회하는 dict다. 여기서 락 없이 키를 추가하면
       검색 중이던 요청이 'dictionary changed size during iteration'으로 끊긴다."""
    name=nname(name)
    with LOCK:
        if not name: return NAME_HIST.get(pid,{}).get("prev",[]), False
        rec=NAME_HIST.get(pid)
        if rec is None:
            NAME_HIST[pid]={"cur":name,"prev":[]}; return [], False
        if nname(rec.get("cur"))!=name:
            old=nname(rec.get("cur"))
            # (여기부터는 '지금 이 순간의 이름'이 확실할 때만 — 과거 이름은 hist_note_past로)
            # ⚠️ 기존 리스트를 제자리에서 insert하면 안 된다. 이 리스트는
            #    CACHE["players"]·site_data 빌드·/api 응답이 같은 객체를 참조하는데,
            #    그쪽 json.dumps는 LOCK 밖에서 돈다. **새 리스트를 만들어 재할당**하면
            #    이미 참조 중인 쪽은 옛 리스트를 끝까지 안전하게 읽는다.
            # 되찾은 이름은 옛 이름 목록에서 뺀다 — 안 그러면 현재 닉네임이
            # 자기 프로필의 '이전 닉네임'에도 같이 뜬다.
            prev=[pn for pn in rec.get("prev",[]) if pn!=name]
            if old and old not in prev: prev.insert(0,old)
            rec["prev"]=prev; rec["cur"]=name
            rec["ts"]=int(time.time())   # 관측 시각(= 개명을 확인한 때). 관리자 목록 정렬·표시용
            return list(prev), True
        return list(rec.get("prev",[])), False

def hist_note_past(pid,name):
    """경기 기록에 남은 '그 당시 이름'을 옛 닉네임 목록에 추가한다. 반환: 추가 여부
    ⚠️ hist_observe로 넣으면 안 된다 — 경기 기록의 이름은 **과거형**이라, 옛 경기를
       나중에 열람하면 개명이 거꾸로 기록된다(현재 이름이 옛 이름으로 밀려나는 사고).
       그래서 cur는 절대 건드리지 않고 prev에만 넣는다.
    ⚠️ 모르는 pid는 만들지 않는다 — 경기에는 수집 대상 밖 유저(봇 포함)도 섞여 있어서
       여기서 새 항목을 만들면 이력이 정체불명 유저로 불어난다."""
    name=nname(name)
    if not name: return False
    with LOCK:
        rec=NAME_HIST.get(pid)
        if rec is None: return False
        if name==nname(rec.get("cur")) or name in (rec.get("prev") or []): return False
        # 재할당(제자리 append 금지 — 이 리스트는 LOCK 밖 json.dumps가 참조한다)
        rec["prev"]=list(rec.get("prev") or [])+[name]
        return True

def harvest_match_names(matches):
    """경기 기록 목록에서 6인 전원의 (pid, 당시 이름)을 이력에 반영. 반환: 추가 건수
    스윕(6시간) 사이에 스쳐간 이름도 경기에는 남는다 — 3일에 4번 개명한 유저의
    중간 이름이 검색 안 되던 실제 사례(2026-08-03, baizaRsanma)를 이걸로 잡는다."""
    noted=0
    for m in (matches or []):
        for pl in m.get("players",[]):
            if pl.get("id") and pl.get("n") and hist_note_past(pl["id"],pl["n"]): noted+=1
    if noted:
        with LOCK:
            for m in (matches or []):
                for pl in m.get("players",[]):
                    rid=pl.get("id")
                    if rid in CACHE["players"] and rid in NAME_HIST:
                        CACHE["players"][rid]["prev"]=NAME_HIST[rid].get("prev",[])
        hist_save()
    return noted

# ── 랭킹 변동 추적 (일별 스냅샷) ──────────────────────────────────
# 매일 KST 0시 이후 첫 갱신 때 그날의 랭킹 전체를 "기준선"으로 저장한다.
# 화면의 ▲▼는 [지금 순위 vs 오늘 기준선]이라 뜻이 '오늘 들어 오른/내린 폭'으로 딱 떨어진다.
#
# ⚠️ 저장은 반드시 Upstash. Render 무료는 재배포마다 디스크가 날아가서 파일에만 두면
#    아무리 오래 돌려도 이력이 안 쌓인다(닉네임 이력과 같은 이유). 파일은 로컬 개발용 폴백.
# ⚠️ 하루치를 한 키에 넣는데, player_id(36자)를 그대로 쓰면 압축해도 1.4MB라
#    Upstash 요청 크기 한도(1MB)를 넘긴다. 그래서 id는 별도 사전(rsgg:rank:ids)에 두고
#    스냅샷에는 번호만 넣는다 → 하루 200KB. 60일 보관해도 12MB.
RANK_KEEP_DAYS=int(os.environ.get("RANK_KEEP_DAYS","60"))
RANK_IDS_KEY="rsgg:rank:ids"     # player_id 사전 (번호 = 배열 위치, 추가만 함)
RANK_DAYS_KEY="rsgg:rank:days"   # 보유한 날짜 목록
RANK_DAY_KEY="rsgg:rank:d:"      # + YYYY-MM-DD → 그날의 기준선
RANK_FILE=os.path.join(DATA,"rank_hist.json")   # Upstash 없을 때만 쓰는 폴백
RANK={"ids":[],"idx":{},"days":[],"base":None,"base_day":None,"dirty":False}

def kst_day(ts=None):
    """한국시간 기준 날짜. 하루의 경계를 어디로 잡든 일관되기만 하면 되고,
    사이트 주 사용자가 한국이라 KST로 고정한다."""
    return time.strftime("%Y-%m-%d", time.gmtime((ts or time.time())+9*3600))

def _rk_pack(o):
    return base64.b64encode(zlib.compress(json.dumps(o,ensure_ascii=False,separators=(",",":")).encode(),6)).decode()
def _rk_unpack(s):
    try: return json.loads(zlib.decompress(base64.b64decode(s)).decode())
    except Exception: return None
def _rk_redis(): return bool(UPSTASH_URL and UPSTASH_TOKEN)
def _rk_file():
    try: return json.load(open(RANK_FILE,encoding="utf-8"))
    except Exception: return {}
def _rk_file_save(d):
    try: json.dump(d,open(RANK_FILE,"w",encoding="utf-8"),ensure_ascii=False)
    except Exception as e: print("[랭킹이력] 파일 저장 실패:",e)

def rank_store_load():
    """ids 사전과 날짜 목록을 읽어온다."""
    if _rk_redis():
        ids=_rk_unpack(redis_cmd("GET",RANK_IDS_KEY) or "") or []
        try: days=json.loads(redis_cmd("GET",RANK_DAYS_KEY) or "[]")
        except Exception: days=[]
        return ids,days
    f=_rk_file(); return f.get("ids") or [], f.get("days") or []
def rank_store_day(day):
    if _rk_redis():
        raw=redis_cmd("GET",RANK_DAY_KEY+day)
        return _rk_unpack(raw) if raw else None
    return (_rk_file().get("snaps") or {}).get(day)
def rank_store_save(day,snap,days,save_ids):
    if _rk_redis():
        if save_ids: redis_cmd("SET",RANK_IDS_KEY,_rk_pack(RANK["ids"]))
        redis_cmd("SET",RANK_DAY_KEY+day,_rk_pack(snap))
        redis_cmd("SET",RANK_DAYS_KEY,json.dumps(days))
    else:
        f=_rk_file(); f["ids"]=RANK["ids"]; f["days"]=days
        snaps=f.setdefault("snaps",{}); snaps[day]=snap
        for k in list(snaps):
            if k not in days: snaps.pop(k,None)
        _rk_file_save(f)
def rank_store_drop(day):
    if _rk_redis(): redis_cmd("DEL",RANK_DAY_KEY+day)

def _rk_id(pid):
    i=RANK["idx"].get(pid)
    if i is None:
        i=len(RANK["ids"]); RANK["ids"].append(pid); RANK["idx"][pid]=i; RANK["dirty"]=True
    return i

def build_snapshot():
    """지금 캐시의 랭킹 → {시즌:{보드:{"i":[번호…순위순],"s":[점수…]}}}"""
    out={}
    with LOCK:
        sdata=CACHE["sdata"]; ranks=CACHE["player_ranks"]
        for season,sd in sdata.items():
            b={}
            for label,pids in (sd.get("boards") or {}).items():
                sc=[]
                for pid in pids:
                    e=((ranks.get(pid) or {}).get(season) or {}).get(label)
                    sc.append(e[1] if e else None)
                b[label]={"i":[_rk_id(p) for p in pids],"s":sc}
            out[season]=b
    return out

def snap_lookup(snap):
    """스냅샷 → {시즌:{보드:{player_id: 순위}}} (변동 계산용)"""
    ids=RANK["ids"]; out={}
    for season,bs in (snap or {}).items():
        o={}
        for label,d in (bs or {}).items():
            m={}
            for r,i in enumerate(d.get("i") or []):
                if 0<=i<len(ids): m[ids[i]]=r+1
            o[label]=m
        out[season]=o
    return out

def rank_load():
    ids,days=rank_store_load()
    RANK["ids"]=ids; RANK["idx"]={p:i for i,p in enumerate(ids)}; RANK["days"]=days
    day=kst_day()
    pick=day if day in days else (days[-1] if days else None)
    if pick:
        snap=rank_store_day(pick)
        if snap: RANK["base"]=snap_lookup(snap); RANK["base_day"]=pick
    where="Upstash" if _rk_redis() else "파일(로컬)"
    print(f"랭킹이력 {len(days)}일치 [{where}] · 기준 {RANK['base_day'] or '없음(첫 저장 대기)'}")

def rank_tick():
    """리더보드 갱신 뒤 호출. 날짜가 바뀌었으면 그 시점 랭킹을 그날 기준선으로 저장."""
    try:
        day=kst_day()
        if RANK["base_day"]==day: return          # 오늘 기준선 이미 있음
        if day in RANK["days"]:                   # 재시작 — 저장돼 있던 오늘 기준선을 다시 읽는다
            snap=rank_store_day(day)
            if snap:
                RANK["base"]=snap_lookup(snap); RANK["base_day"]=day; return
        with LOCK: has=bool(CACHE["sdata"])
        if not has: return                        # 랭킹이 아직 안 올라왔으면 저장하지 않는다
        if not FRESH["ok"]: return                # 재배포 직후 씨앗 데이터 — 진짜 갱신 뒤에 잡는다
        snap=build_snapshot()
        days=sorted(set(RANK["days"])|{day})
        old=days[:-RANK_KEEP_DAYS]; days=days[-RANK_KEEP_DAYS:]
        rank_store_save(day,snap,days,RANK["dirty"])
        RANK["dirty"]=False
        for d in old: rank_store_drop(d)
        RANK["days"]=days; RANK["base"]=snap_lookup(snap); RANK["base_day"]=day
        print(f"[랭킹이력] {day} 기준선 저장 · 보유 {len(days)}일" + (f" · 만료 {len(old)}일 삭제" if old else ""))
    except Exception as e:
        print("[랭킹이력] 오류:",e)

def rank_delta():
    """지금 순위 vs 오늘 기준선. {시즌:{보드:{player_id: 변동}}}.
    양수=상승, 음수=하락, "N"=기준선에 없던 신규 진입. 변동 없는 사람은 아예 안 넣는다."""
    base=RANK.get("base")
    if not base: return {}
    out={}
    with LOCK: sdata=CACHE["sdata"]
    for season,sd in sdata.items():
        bb=base.get(season) or {}
        for label,pids in (sd.get("boards") or {}).items():
            prev=bb.get(label)
            if not prev: continue
            d={}
            for i,pid in enumerate(pids):
                p=prev.get(pid)
                if p is None: d[pid]="N"
                elif p!=i+1: d[pid]=p-(i+1)
            if d: out.setdefault(season,{})[label]=d
    return out

def load_disk():
    try:
        lb=json.load(open(os.path.join(DATA,"leaderboards.json"),encoding="utf-8"))
        apply_leaderboards(lb)
    except Exception as e: print("리더보드 로드 실패:",e)
    try:
        pl=json.load(open(os.path.join(DATA,"players.json"),encoding="utf-8"))
        # players.json은 원본 형식 → 컴팩트로 변환
        with LOCK:
            for pid,v in pl.items():
                CACHE["players"][pid]=to_compact_from_raw(v)
        print(f"플레이어 {len(CACHE['players'])}명 로드")
    except Exception as e: print("플레이어 로드 실패:",e)
    if UPSTASH_URL and UPSTASH_TOKEN:
        redis_cmd("PING")
        print("Upstash 연결:", "성공" if REDIS_STATE["ok"] else "실패("+REDIS_STATE["err"]+")")
    else:
        print("Upstash 미설정 — 닉네임 이력은 재배포시 초기화됩니다")
    hist_load()
    hist_seed()
    rank_load()
    with LOCK:
        for pid,rec in NAME_HIST.items():
            if pid in CACHE["players"]:
                CACHE["players"][pid]["prev"]=rec.get("prev",[])
                # 옛 수집본의 이름 끝 공백을 여기서 맞춰둔다(검색·표시 모두 이력 기준)
                if rec.get("cur"): CACHE["players"][pid]["n"]=rec["cur"]

def to_compact_from_raw(v):
    heroes={}
    for hn,hd in (v.get("heroes") or {}).items():
        heroes[hn]={"lv":hd.get("lv"),"pt":hd.get("pt"),"rt":hd.get("rt"),"pre":hd.get("pre"),"sk":hd.get("skin"),
            "pk":perk_sorted(int(x) for x in (hd.get("pk") or []))}
    reg=v.get("region","");  reg=REGION_REV.get(reg,reg)   # 옛 스냅샷은 한글로 저장돼 있음
    return {"n":nname(v.get("name","")),"r":reg,"lv":v.get("level"),"wr":v.get("winrate"),
        "mvp":v.get("mvp"),"m":v.get("matches"),"w":v.get("wins"),"kd":v.get("kd"),"k":v.get("kills"),
        "d":v.get("deaths"),"dmg":v.get("damage"),"heal":v.get("heal"),"db":v.get("double"),
        "tr":v.get("triple"),"fh":v.get("final"),"pt":v.get("playtime_h"),"pm":v.get("pro_matches"),
        "rm":v.get("recent_matches",[]),"h":heroes}

def month_codes(back=SEASON_LOOKBACK):
    """이번 달부터 거슬러 올라간 YYYYMM 목록."""
    import datetime
    d=datetime.date.today(); y,m=d.year,d.month; out=[]
    for _ in range(back):
        out.append(f"{y:04d}{m:02d}")
        m-=1
        if m==0: m=12; y-=1
    return out

def lb_get(s,name,rid,top=20000):
    s.sendall(fr({"request_id":rid,"type":"get_leaderboard"},{"leaderboard_name":name,"top_size":top}))
    for _ in range(50):
        env,b=rd(s)
        if env.get("type")=="get_leaderboard" and env.get("request_id")==rid: return b
    return None

def find_seasons(s):
    """참가자가 있는 시즌만 최신순으로. 프로가 아직 안 열려도 캐주얼이 있으면 유효."""
    found=[]; rid=300
    for code in month_codes():
        total=0
        for nm in (f"rating_casual_0_{code}", f"rating_pro_0_{code}"):
            rid+=1
            try: b=lb_get(s,nm,rid,top=1)
            except Exception: b=None
            total += (b or {}).get("leaderboard_size") or 0
        if total: found.append(code)
        if len(found)>=SEASON_KEEP: break
    return found

def label_of(kind,label): return label if kind=="hero" else ("pro" if kind=="pro" else "casual")
# 보드 키는 언어중립(pro/casual/영문 캐릭터명). 표시 이름은 브라우저가 언어별로 붙인다.

# ── 프로 시즌은 캐주얼과 주기가 다르다 ─────────────────────────
# 2026-08-13 실측(게임서버에 직접 물어봄):
#   RatingCasualSeason_CurrentSuffix = 202608, 다음 시작 9/1 09:00
#   RatingProSeason_CurrentSuffix    = 202607, 다음 시작 8/15 09:00   ← 달이 안 맞는다
#   rating_pro_0_202608 은 **크기 0**이고, rating_pro_0_202607 이 지금도 살아 움직인다
#   (8/5 스냅샷 2,269명 → 8/13 2,453명, 1,456명의 점수가 바뀌었다)
# 그래서 보드 이름의 달로 시즌 탭을 짝지으면 "8월엔 프로가 없다"가 되고,
# 지금 돌아가는 프로 랭킹이 지난달 탭에 숨는다. 살아있는 프로 보드는 현재 시즌 탭에 붙인다.
PRO_SUFFIX_C="79016390"   # RatingProSeason_CurrentSuffix
PRO_NEXT_C="79012751"     # RatingProSeason_NextStartTimestamp
def pro_board(code): return f"rating_pro_0_{code}"
# ⚠️ "비어있지 않은 보드 = 열린 시즌"이 아니다. 시계가 틀린 클라이언트가 **미래 시즌 보드**에
#    박혀 있다(2026-08-19 실측: rating_pro_0_202609 · rating_casual_0_202609 · rating_pro_0_202612 에
#    같은 유저 1명, 그 계정의 시즌 카운터는 202606). 이 떠돌이를 "9월 프로 시즌이 열렸다"로 읽으면
#    9/1에 진행 중인 8월 프로 보드가 9월 탭에 안 붙고, 9월 탭엔 1명짜리 프로 보드가 뜬다.
PRO_MIN_REAL=20           # 이보다 작은 프로 보드는 그 자체로는 "열린 시즌"의 증거로 안 본다
PRO_ASK_TOP=10            # 현재 시즌을 물어볼 유저 수 (가장 큰 보드 상위 N + 나머지 보드 상위 3)

def pro_season_info(s,pids,rid):
    """프로 유저 여러 명에게 물어 '게임이 말하는 현재 프로 시즌'을 고른다 → (시즌코드, 다음시작시각).
    ⚠️ 이 카운터는 **프로를 해 본 계정에만** 있다(수집용 계정엔 없어서 자기 자신에겐 못 묻는다).
    ⚠️ 한 명에게만 물으면 위험하다 — 시즌이 바뀐 뒤 아직 접속 안 한 계정은 **옛 값**을 들고 있고,
       시계가 틀린 계정은 **엉뚱한 값**을 들고 있다(실측 202606). 그래서 여러 명의 **최빈값**을 쓰고,
       동률이면 최신 쪽을 고른다(시즌 코드는 앞으로만 간다).
    실패해도 갱신 전체를 망치면 안 된다 → 예외는 삼키고 (None,None)."""
    pids=[p for p in dict.fromkeys(pids) if p]
    if not pids: return None,None
    votes={}; nxt={}
    try:
        s.sendall(fr({"request_id":rid,"type":"get_accounts_info"},{"player_ids":pids,"rich_info":True}))
        for _ in range(60):
            env,body=rd(s)
            if env.get("type")=="get_accounts_info" and "account_info_jsons" in body:
                for aj in body.get("account_info_jsons") or []:
                    try: a=json.loads(aj)
                    except Exception: continue
                    cc=(a.get("counters_state") or {}).get("counter_collection",{})
                    g=lambda k:(cc.get(k,{}) or {}).get("value")
                    cur=g(PRO_SUFFIX_C)
                    if not cur: continue
                    cur=str(cur); votes[cur]=votes.get(cur,0)+1
                    if g(PRO_NEXT_C): nxt.setdefault(cur,g(PRO_NEXT_C))
                break
    except Exception as e:
        print("[갱신] 프로 시즌 확인 실패:",e)
    if not votes: return None,None
    best=max(votes, key=lambda c:(votes[c],c))     # 최빈값, 동률이면 최신
    if len(votes)>1: print(f"[갱신] 프로 시즌 투표 {votes} → {best}")
    return best, nxt.get(best)

def attach_live_pro(s,seasons,result,rid):
    """진행 중인 프로 보드를 현재 시즌(seasons[0]) 탭으로 옮긴다.
    끝난 프로 시즌은 건드리지 않는다(자기 달 탭에 그대로). 미래 시즌의 떠돌이 보드는 지운다."""
    if not seasons: return
    cur=seasons[0]
    found={c:result["boards"][c][pro_board(c)] for c in seasons if pro_board(c) in (result["boards"].get(c) or {})}
    if not found:
        # 유지 중인 시즌(2개)보다 길게 끄는 프로 시즌 — 더 거슬러 올라가 찾는다.
        for code in month_codes():
            if code in seasons: continue
            rid+=1
            try: body=lb_get(s,pro_board(code),rid)
            except Exception: body=None
            ids=(body or {}).get("top_player_ids") or []
            if not ids: continue
            sc=body.get("top_player_scores") or []
            mb={"kind":"pro","label":"pro","season":code,"size":body.get("leaderboard_size"),
                "entries":[{"rank":i+1,"player_id":p,"score":s2} for i,(p,s2) in enumerate(zip(ids,sc))]}
            result["boards"].setdefault(code,{})[pro_board(code)]=mb
            found={code:mb}; break
    if not found:
        print("[갱신] 프로 보드를 못 찾았다"); return
    # 게임이 말하는 현재 프로 시즌: 가장 큰 보드(=확실히 진짜 프로들)의 상위 N명 + 나머지 보드 상위 3명에게 묻는다
    by_size=sorted(found, key=lambda c:-len(found[c].get("entries") or []))
    ask=[e["player_id"] for e in (found[by_size[0]].get("entries") or [])[:PRO_ASK_TOP]]
    for c in by_size[1:]: ask+=[e["player_id"] for e in (found[c].get("entries") or [])[:3]]
    declared,next_ts=pro_season_info(s,ask,rid+900)
    # 진행 중인 시즌 = 게임이 말한 시즌(그 보드가 있으면). 단, 그보다 최신인데 **충분히 큰** 보드가
    # 있으면 게임 답이 낡은 것(아직 아무도 접속 안 함)이니 보드를 믿는다.
    real=[c for c in found if len(found[c].get("entries") or [])>=PRO_MIN_REAL]
    cands=set(real) | ({declared} if declared in found else set())
    live=max(cands) if cands else max(found)
    if declared and declared!=live and declared in found:
        print(f"[갱신] 프로 시즌: 게임은 {declared}라지만 {live} 보드가 이미 크다 → {live}를 진행 중으로 본다")
    # 진행 중인 것보다 미래인 보드 = 시계 틀린 유저의 떠돌이 → 화면에 안 낸다
    for c in list(found):
        if c>live:
            n=len(found[c].get("entries") or [])
            result["boards"][c].pop(pro_board(c),None)
            print(f"[갱신] 프로 보드 {c}({n}명)는 아직 안 열린 시즌의 떠돌이 → 뺐다")
    mb=found[live]
    mb["live"]=True
    if next_ts and (declared==live): mb["next_ts"]=next_ts
    result["pro_season"]=live
    if live!=cur:
        result["boards"].setdefault(cur,{})[pro_board(live)]=result["boards"][live].pop(pro_board(live))
        print(f"[갱신] 프로 시즌 {live}이 아직 진행 중 → {cur} 탭에 붙였다"
              + (f" (다음 시즌 {time.strftime('%m/%d %H:%M',time.localtime(mb['next_ts']))})" if mb.get("next_ts") else ""))

def apply_leaderboards(lb):
    """시즌별로 보드를 정리한다. ranks는 {유저: {시즌: {보드: [순위, 점수]}}}."""
    seasons=lb.get("seasons") or []
    by_season=lb.get("boards") or {}
    # 옛 단일시즌 형식({"season":..., "boards":{보드명:...}})도 읽을 수 있게
    if seasons and not isinstance(next(iter(by_season.values()),{}), dict):
        by_season={}
    if not seasons:
        seasons=[lb.get("season") or "?"]; by_season={seasons[0]: lb.get("boards") or {}}
    sdata={}; ranks={}
    for season in seasons:
        boards={}; meta={}; blabels=[]; hlabels=[]
        for name,mb in (by_season.get(season) or {}).items():
            lab=label_of(mb["kind"],mb["label"]); blabels.append(lab)
            meta[lab]={"size":mb["size"],"kind":mb["kind"]}
            # 프로 보드는 시즌 주기가 달라 다른 달 탭에 얹혀 있을 수 있다 → 실제 시즌을 같이 알려준다
            for k in ("season","live","next_ts"):
                if mb.get(k) is not None: meta[lab][k]=mb[k]
            if mb["kind"]=="hero": hlabels.append(lab)
            ordered=[]
            for e in mb["entries"]:
                pid=e["player_id"]; ordered.append(pid)
                ranks.setdefault(pid,{}).setdefault(season,{})[lab]=[e["rank"],e["score"]]
            boards[lab]=ordered
        sdata[season]={"boards":boards,"board_meta":meta,
                       "board_labels":blabels,"hero_labels":hlabels}
    with LOCK:
        CACHE.update({"seasons":seasons,"sdata":sdata,"player_ranks":ranks,
                      "last_refresh":int(time.time())})

# site_data.js에 넣을 유저 필드. **목록·검색에 필요한 것만** 넣는다.
# 나머지(캐릭터별 상세 h, 최근경기 rm, 킬·데미지 등)는 프로필을 열 때 /api/detail 로 받는다.
# 전에는 8,180명 전체 프로필을 첫 화면에서 통째로 보냈다 → 14.5MB(gzip 4.1MB).
# 그중 캐릭터별 상세만 9MB인데 목록에선 한 번도 안 쓴다.
SLIM_KEYS=("n","r","lv","wr","mvp","prev")
def slim_player(p):
    q={k:p[k] for k in SLIM_KEYS if k in p and p[k] not in (None,[],"")}
    return q

# site_data.js는 만들자마자 **메모리에 들고** 그걸 서빙한다.
# ⚠️ 예전엔 요청마다 파일을 통째로 read()했다. 3.9MB를 동시에 여러 명이 받으면
#    무료 512MB 인스턴스가 OOM으로 죽는다. 게다가 "파일을 자르고 이어 쓰는" 방식이라
#    쓰는 도중 방문한 사람은 **잘린 파일**을 받아 화면이 백지가 됐다.
# ⚠️ build_site_data는 갱신 스레드와 이름검사 스레드 양쪽에서 부른다.
#    주기가 1800초 · 21600초로 정확히 12배라 6시간마다 동시에 실행된다 → 전용 락으로 직렬화.
BUILD_LOCK=threading.Lock()
SITE_JS={"bytes":b"", "gz":b"", "ts":0}

def build_site_data():
  with BUILD_LOCK:
    with LOCK:
        players={}
        for pid,p in CACHE["players"].items():
            q=slim_player(p); q["rk"]=CACHE["player_ranks"].get(pid,{}); players[pid]=q
        pmeta={}
        for pid,v in PERK_KR.items():
            if isinstance(v,list) and len(v)>2: pmeta[pid]=[v[1],v[2]]
            elif isinstance(v,list) and len(v)>1: pmeta[pid]=[v[1],""]
        out={"perk_meta":pmeta,"comps":COMPS,"hstat":HEROSTAT,"pstat":PERKSTAT,"slim":True,
            "seasons":CACHE["seasons"],"sdata":CACHE["sdata"],"players":players,
            "generated_count":len(players),"last_refresh":CACHE["last_refresh"]}
    # 랭킹 변동(LOCK 밖에서 — rank_delta가 스스로 LOCK을 잡는다)
    out["rdelta"]=rank_delta()
    out["rbase"]={"day":RANK["base_day"],"days":len(RANK["days"])}
    body=("window.DATA="+json.dumps(out,ensure_ascii=False,separators=(",",":"))+";").encode("utf-8")
    # 먼저 메모리에 올린다 — 서빙은 여기서만 하므로 파일이 잘려도 화면이 깨지지 않는다
    SITE_JS.update(bytes=body, gz=gzip.compress(body,6), ts=int(time.time()))
    # 파일은 임시파일에 쓰고 통째로 바꿔치기(원자적). 쓰다 죽어도 이전 파일이 온전히 남는다
    p=os.path.join(HERE,"site_data.js"); tmp=p+".tmp"
    try:
        with open(tmp,"wb") as f: f.write(body)
        os.replace(tmp,p)
    except Exception as e:
        print("[site_data] 파일 저장 실패(서빙은 메모리로 계속):",e)
        try: os.remove(tmp)
        except Exception: pass

def refresh_leaderboards():
    print("[갱신] 리더보드 수집...")
    s=connect(); rid=1000; allids=set()
    try:                              # ⚠️ 중간에 예외가 나도 소켓은 반드시 닫는다(fd 누수 방지)
        seasons=find_seasons(s)      # 달이 바뀌면 여기서 자동으로 새 시즌을 잡는다
        result={"seasons":seasons,"boards":{}}
        for season in seasons:
            sb={}
            boards=[("pro","pro",f"rating_pro_0_{season}"),("casual","casual",f"rating_casual_0_{season}")]
            boards+=[("hero",HEROES[h],f"rating_heroes_{h}_{season}") for h in HEROES]
            for kind,label,name in boards:
                rid+=1
                try: body=lb_get(s,name,rid)
                except Exception: body=None
                if not isinstance(body,dict): continue
                ids=body.get("top_player_ids",[]); sc=body.get("top_player_scores",[])
                if not ids: continue      # 아직 안 열린 보드(예: 시즌 초 프로)
                sb[name]={"kind":kind,"label":label,"season":season,"size":body.get("leaderboard_size"),
                    "entries":[{"rank":i+1,"player_id":pid,"score":s2} for i,(pid,s2) in enumerate(zip(ids,sc))]}
                allids.update(ids)
            result["boards"][season]=sb
        # 프로는 캐주얼과 시즌 주기가 다르다 — 진행 중인 프로 보드를 현재 시즌 탭으로 옮긴다
        try: attach_live_pro(s,seasons,result,rid+500)
        except Exception as e: print("[갱신] 프로 시즌 정리 오류:",e)
        for sb2 in result["boards"].values():
            for mb2 in sb2.values(): allids.update(e["player_id"] for e in mb2.get("entries") or [])
    finally:
        try: s.close()
        except Exception: pass
    result["unique_player_ids"]=sorted(allids)
    try: fetch_new_players(allids)
    except Exception as e: print("[갱신] 신규 유저 수집 오류:",e)
    # 원자적 저장 — 쓰다 죽어도 이전 파일이 온전히 남는다(잘린 JSON이면 다음 부팅 때 랭킹이 비어버림)
    lbp=os.path.join(DATA,"leaderboards.json")
    with open(lbp+".tmp","w",encoding="utf-8") as f: json.dump(result,f,ensure_ascii=False)
    os.replace(lbp+".tmp",lbp)
    apply_leaderboards(result); FRESH["ok"]=True; rank_tick(); build_site_data()
    print(f"[갱신] 완료 · 시즌 {','.join(seasons)} · 고유유저 {len(allids)} · {time.strftime('%H:%M:%S')}")

# ── 게임서버 이름 검색 (search_players) ─────────────────────────
# 우리 DB(수집한 유저)에 없는 사람도 **게임 전체에서** 찾아준다.
# 프로토콜: {"type":"search_players"} {"query":"<닉네임>"} → {"player_ids":[...]}
# ⚠️ **정확한 이름 일치**다. 부분일치는 0건이 온다(실측: 'gg'·'a' → 0, '한태준' → 1).
# ⚠️ 게임서버 왕복이라 1~2초 걸린다. **로컬에서 못 찾았을 때만** 부른다.
GS_TTL=int(os.environ.get("GS_TTL","300"))   # 같은 질의 재조회 방지 시간(초)
GS_CACHE={}                                   # 질의 → (시각, [player_id...])
GS_LOCK=threading.Lock()                      # 게임서버 조회는 한 번에 하나만(폭주 방지)
GS_MAX=20                                     # 한 질의에서 받아올 최대 인원

def search_game(q):
    """로컬에서 못 찾은 이름을 게임서버에 되묻는다. 찾으면 프로필까지 받아
    CACHE·이름이력에 등록해서 **다음부터는 로컬에서 바로** 나오게 한다."""
    now=time.time()
    with GS_LOCK:
        hit=GS_CACHE.get(q)
        if hit and now-hit[0]<GS_TTL: return hit[1]
    ids=[]
    with GS_LOCK:                  # 통신 구간을 직렬화 — 동시에 여러 소켓을 열지 않는다
        hit=GS_CACHE.get(q)        # 기다리는 사이 다른 요청이 채웠을 수 있다
        if hit and time.time()-hit[0]<GS_TTL: return hit[1]
        try:
            s=connect()
            try:
                s.sendall(fr({"request_id":77,"type":"search_players"},{"query":q}))
                for _ in range(30):
                    env,b=rd(s)
                    if env.get("type")=="search_players":
                        ids=[i for i in (b.get("player_ids") or []) if i and VALID_PID.match(i)][:GS_MAX]
                        break
                with LOCK: new=[i for i in ids if i not in CACHE["players"]]
                if new:
                    s.sendall(fr({"request_id":78,"type":"get_accounts_info"},{"player_ids":new,"rich_info":True}))
                    for _ in range(30):
                        env,b=rd(s)
                        if env.get("type")=="get_accounts_info" and "account_info_jsons" in b:
                            for js in b["account_info_jsons"]:
                                try: acc=json.loads(js)
                                except Exception: continue
                                pid=acc.get("player_id")
                                if not pid: continue
                                comp=parse_acc(acc)
                                with LOCK: CACHE["players"][pid]=comp
                                hist_observe(pid, comp.get("n"))   # 이름이력에도 넣어야 이후 검색에 걸린다
                            break
                    print(f"[검색] '{q}' → 게임서버에서 {len(new)}명 새로 등록")
                    hist_save()
            finally:
                try: s.close()
                except Exception: pass
        except Exception as e:
            print(f"[검색] 게임서버 조회 실패 ('{q}'):",e)
        if len(GS_CACHE)>500: GS_CACHE.clear()   # 무한정 쌓이지 않게
        GS_CACHE[q]=(now,ids)
    return ids

NEW_MAX=int(os.environ.get("NEW_MAX","1500"))   # 한 번의 갱신에서 새로 받아올 유저 수 상한
def fetch_new_players(ids):
    """랭킹 보드에 있는데 players 캐시에 없는 유저의 프로필을 받아 채운다.

    ⚠️ 이게 없으면 **화면이 통째로 안 그려진다.** 화면은 P[id]로 이름·승률을 꺼내는데
       그 유저가 없으면 예외가 나고 렌더링이 멈춘다(2026-08-05: 7월 프로 첫 페이지에
       신규 유저가 걸려 "프로를 눌러도 반응 없음"). 화면에도 방어를 넣었지만,
       빈칸으로 두지 않으려면 여기서 채우는 게 맞다.
    ⚠️ players.json은 오프라인 수집 스크립트가 만든다. 시즌이 흐르면 랭킹에 새로 진입한
       유저가 계속 생기므로, 서버가 스스로 메꿔야 한다."""
    with LOCK: missing=[i for i in ids if i not in CACHE["players"]]
    if not missing: return 0
    capped=missing[:NEW_MAX]
    if len(missing)>NEW_MAX:
        print(f"[갱신] 신규 유저 {len(missing)}명 중 {NEW_MAX}명만 이번에 수집 (나머지는 다음 갱신)")
    print(f"[갱신] 신규 유저 {len(capped)}명 프로필 수집...")
    s=connect(); rid=7000; got=0
    try:
        i=0
        while i<len(capped):
            batch=capped[i:i+60]; rid+=1
            try:
                s.sendall(fr({"request_id":rid,"type":"get_accounts_info"},{"player_ids":batch,"rich_info":True}))
                for _ in range(40):
                    env,body=rd(s)
                    if env.get("type")=="get_accounts_info" and "account_info_jsons" in body:
                        for js in body["account_info_jsons"]:
                            try: acc=json.loads(js)
                            except Exception: continue
                            pid=acc.get("player_id")
                            if not pid: continue
                            comp=parse_acc(acc)
                            with LOCK: CACHE["players"][pid]=comp
                            hist_observe(pid, comp.get("n"))   # 이름이력에도 등록(검색되게)
                            got+=1
                        break
                i+=60
            except (ConnectionError, socket.timeout, OSError):
                try: s.close()
                except Exception: pass
                time.sleep(1); s=connect(); continue
            time.sleep(0.1)
    finally:
        try: s.close()
        except Exception: pass
    with LOCK:
        for pid,rec in NAME_HIST.items():
            if pid in CACHE["players"]: CACHE["players"][pid]["prev"]=rec.get("prev",[])
    hist_save()
    print(f"[갱신] 신규 유저 {got}명 추가 (총 {len(CACHE['players'])}명)")
    return got

def boot_refresh():
    """부팅 직후 1회 갱신. 재배포하면 저장소의 옛 씨앗(8/2 리더보드·8,180명)부터 읽는데, 첫 정기 갱신까지
    30분을 그대로 보여줬다(프로 시즌 탭 배치도 옛날 것). 서버는 이미 떠 있으니 뒤에서 바로 채운다."""
    try: refresh_leaderboards()
    except Exception as e: print("[갱신] 부팅 갱신 오류:",e)

def scheduler():
    while True:
        time.sleep(LB_REFRESH_SEC)
        try: refresh_leaderboards()
        except Exception as e: print("[갱신] 오류:",e)

def harvest_match_ids(s, mids):
    """경기 id 목록 중 아직 안 본 경기의 결과를 받아 6인 전원의 '당시 이름'을 수확.
    반환 (수확 건수, 소켓) — 재접속했을 수 있어 소켓을 돌려준다.
    스윕이 이름을 못 본 사이(2시간)에 스쳐간 닉네임도 경기 기록에는 남는다.
    프로필 열람 수확(harvest_match_names)과 달리 **전원**을 커버한다."""
    todo=[m for m in mids if m not in SEEN_MATCH]
    skipped=len(todo)-SWEEP_MATCH_MAX
    if skipped>0:
        todo=todo[:SWEEP_MATCH_MAX]
        print(f"[이름검사] 새 경기 {len(todo)+skipped}건 중 {len(todo)}건만 이번에 수확 (나머지는 다음 스윕)")
    noted=0; rid=9000; j=0
    while j<len(todo):
        batch=todo[j:j+30]; rid+=1
        try:
            s.sendall(fr({"request_id":rid,"type":"get_match_results_info"},{"match_ids":batch}))
            for _ in range(40):
                env,body=rd(s)
                if env.get("type")=="get_match_results_info" and "match_result_info_jsons" in body:
                    for js in body["match_result_info_jsons"]:
                        try: m=json.loads(js)
                        except: continue
                        for p in m.get("PlayerResults",[]):
                            if p.get("PlayerId") and p.get("Name") and hist_note_past(p["PlayerId"],p["Name"]): noted+=1
                    break
            # 응답에 없는 경기(만료 등)도 본 것으로 친다 — 안 그러면 매 스윕 헛되이 재요청한다
            SEEN_MATCH.update(batch); j+=30
        except (ConnectionError, socket.timeout, OSError):
            try: s.close()
            except: pass
            time.sleep(1); s=connect(); continue
        time.sleep(0.1)
    return noted, s

def sweep_names():
    ids=list(CACHE["players"].keys())
    if not ids: return
    print(f"[이름검사] {len(ids)}명 이름 수집...")
    s=connect(); rid=8000; changed=0; i=0
    mseen=set(); match_ids=[]   # 이번 스윕에서 모은 경기 id (rich_info 응답에 이미 있어 추가 요청 없음)
    while i<len(ids):
        batch=ids[i:i+60]; rid+=1
        try:
            s.sendall(fr({"request_id":rid,"type":"get_accounts_info"},{"player_ids":batch,"rich_info":True}))
            for _ in range(40):
                env,body=rd(s)
                if env.get("type")=="get_accounts_info" and "account_info_jsons" in body:
                    for js in body["account_info_jsons"]:
                        try: acc=json.loads(js)
                        except: continue
                        pid=acc.get("player_id"); nm=(acc.get("player_state") or {}).get("name")
                        if pid and nm:
                            nm=nname(nm)
                            _,ch=hist_observe(pid,nm)
                            if ch:
                                changed+=1
                                if pid in CACHE["players"]: CACHE["players"][pid]["n"]=nm
                        for mid in ((acc.get("match_state") or {}).get("match_history") or []):
                            if mid not in mseen: mseen.add(mid); match_ids.append(mid)
                    break
            i+=60
        except (ConnectionError, socket.timeout, OSError):
            try: s.close()
            except: pass
            time.sleep(1); s=connect(); continue
        time.sleep(0.1)
    noted=0
    try:
        noted,s=harvest_match_ids(s, match_ids)
    except Exception as e:
        print("[이름검사] 경기 수확 오류:",e)
    s.close()
    with LOCK:
        for pid,rec in NAME_HIST.items():
            if pid in CACHE["players"]: CACHE["players"][pid]["prev"]=rec.get("prev",[])
    hist_save(); build_site_data()
    print(f"[이름검사] 완료 · 개명 {changed}건 · 경기에서 옛 이름 {noted}건 · 확인한 경기 누적 {len(SEEN_MATCH)}건")
def sweep_scheduler():
    while True:
        time.sleep(SWEEP_SEC)
        try: sweep_names()
        except Exception as e: print("[이름검사] 오류:",e)

def hero_name(hid): return HEROES.get(str(hid),f"#{hid}")

def match_perks(pr):
    """그 경기에 실제로 사용한 퍽. 한 경기에서 캐릭터를 바꿨으면 캐릭터별로 나온다."""
    out=[]
    for hr in (pr.get("HeroResultData") or []):
        names=perk_sorted(int(x) for x in (hr.get("Perks") or []) if x)
        if names: out.append({"h":hero_name(hr.get("HeroId")),"pk":names})
    return out
def parse_matches(jsons, pid):
    out=[]
    for js in jsons:
        try: m=json.loads(js)
        except: continue
        prs=m.get("PlayerResults",[])
        me=next((p for p in prs if p.get("PlayerId")==pid),None)
        if not me: continue
        players=[{"id":p.get("PlayerId"),"n":p.get("Name",""),"tm":p.get("Team"),"h":hero_name(p.get("LastUsedHeroId")),
                  "rt":p.get("Rating"),"mvp":int(p.get("MvpPoints",0)),"k":p.get("Eliminations"),"d":p.get("Deaths"),
                  "dmg":p.get("Damage"),   # 그 경기 그 선수의 딜량 (게임 결과창에도 나오는 값)
                  "mv":p.get("PlayerId")==m.get("MVPId"),"me":p.get("PlayerId")==pid,"hp":match_perks(p)}
                 for p in prs]
        for q,p in zip(players,prs):
            # 파티/봇 구분 (2026-08-03 덤프로 확인한 SquadId 의미):
            #   빈 값 = 솔로 · 'N_M' = 파티 식별자(같은 팀에서 같은 값 = 같이 큐) · bot_squad_* = 봇
            # 봇은 BotArchetype(봇 성격 id)이 비어있지 않은 것으로 판별하는 게 더 확실하다.
            if p.get("BotArchetype"): q["bot"]=1
            else:
                sq=p.get("SquadId") or ""
                if re.match(r"^\d+_\d+$",sq): q["sq"]=sq
        avg=m.get("AvgTeamRating")   # 매치 전체 평균 레이팅 ("이 판의 수준")
        out.append({"ts":m.get("Timestamp"),"type":m.get("MatchType"),"t1":m.get("Team1Score"),"t2":m.get("Team2Score"),"dur":m.get("MatchTime"),
            "avg":(round(avg) if isinstance(avg,(int,float)) else None),
            "win":me.get("Team")==m.get("WinnerTeam"),"myhero":hero_name(me.get("LastUsedHeroId")),
            "mymvp":int(me.get("MvpPoints",0)),"myrt":me.get("Rating"),"myk":me.get("Eliminations"),"myd":me.get("Deaths"),"mys":(m.get("Team1Score") if me.get("Team")==1 else m.get("Team2Score")) or 0,"ens":(m.get("Team2Score") if me.get("Team")==1 else m.get("Team1Score")) or 0,"mvpme":me.get("PlayerId")==m.get("MVPId"),"map":str(m.get("LevelId") or ""),"dmg":me.get("Damage"),"players":players})
    out.sort(key=lambda x:-(x["ts"] or 0))
    return out[:25]

def fetch_matches(s, match_ids, pid):
    if not match_ids: return []
    s.sendall(fr({"request_id":10,"type":"get_match_results_info"},{"match_ids":match_ids[-30:]}))
    for _ in range(30):
        env,body=rd(s)
        if env.get("type")=="get_match_results_info" and "match_result_info_jsons" in body:
            return parse_matches(body["match_result_info_jsons"], pid)
    return []

def live_player(pid):
    """게임서버에서 해당 유저 실시간 조회 (프로필 + 최근 경기)"""
    s=connect()
    try:
        s.sendall(fr({"request_id":9,"type":"get_accounts_info"},{"player_ids":[pid],"rich_info":True}))
        acc=None
        for _ in range(30):
            env,body=rd(s)
            if env.get("type")=="get_accounts_info" and "account_info_jsons" in body:
                for js in body["account_info_jsons"]:
                    a=json.loads(js)
                    if a.get("player_id")==pid: acc=a; break
                if acc: break
        if not acc: return None
        comp=parse_acc(acc)
        with LOCK: CACHE["players"][pid]=comp
        comp2=dict(comp); comp2["rk"]=CACHE["player_ranks"].get(pid,{})
        _prev,_ch=hist_observe(pid, comp.get("n")); comp2["prev"]=_prev
        if _ch:
            if pid in CACHE["players"]: CACHE["players"][pid]["prev"]=_prev
            hist_save()
        try:
            mh=(acc.get("match_state") or {}).get("match_history",[])
            comp2["matches"]=fetch_matches(s, mh, pid)
            # 경기 기록에서 '그 당시 닉네임' 수확 — 본인 포함 6인 전원
            if harvest_match_names(comp2["matches"]):
                with LOCK: comp2["prev"]=list(NAME_HIST.get(pid,{}).get("prev",[]))  # 방금 찾은 이름도 바로 표시
        except Exception as e:
            comp2["matches"]=[]
        return comp2
    finally:
        s.close()

MAINT_HTML="""<!DOCTYPE html><html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="robots" content="noindex">
<title>RS.GG — 준비 중</title>
<style>
:root{--bg:#0e1015;--panel:#171a21;--line:#2a2f3d;--tx:#e8eaf0;--mut:#9aa3b2;--acc:#4a86ff}
*{box-sizing:border-box}
body{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;
  background:var(--bg);color:var(--tx);font-family:-apple-system,BlinkMacSystemFont,"Apple SD Gothic Neo",sans-serif;padding:24px}
.box{max-width:460px;text-align:center;background:var(--panel);border:1px solid var(--line);border-radius:16px;padding:40px 28px}
.logo{font-size:34px;font-weight:800;letter-spacing:-1px;margin-bottom:6px}
.logo span{color:var(--acc)}
h1{font-size:19px;margin:18px 0 10px}
p{color:var(--mut);font-size:14px;line-height:1.7;margin:8px 0}
.en{font-size:12.5px;color:#6d7688;margin-top:18px;padding-top:16px;border-top:1px solid var(--line)}
</style></head><body><div class="box">
<div class="logo">RS<span>.GG</span></div>
<h1>지금은 이용할 수 없습니다</h1>
<p>운영 방침 협의가 끝날 때까지<br>사이트를 잠시 닫아두었습니다.</p>
<p>그동안에도 랭킹 기록은 계속 쌓이고 있으니,<br>다시 열릴 때 이 기간의 데이터도 함께 보실 수 있습니다.</p>
<div class="en">Temporarily closed while we finalize operating policy.<br>Data collection continues in the background.</div>
</div></body></html>"""

# ── 웹서버 ───────────────────────────────────────────
class H(http.server.BaseHTTPRequestHandler):
    def log_message(self,*a): pass
    def _send(self,code,body,ctype="application/json",cache=None,extra=None):
        self.send_response(code); self.send_header("Content-Type",ctype)
        self.send_header("Access-Control-Allow-Origin","*")
        for k,v in (extra or []): self.send_header(k,v)
        if cache: self.send_header("Cache-Control",cache)
        self.end_headers()
        if getattr(self,"_head_only",False): return      # HEAD는 헤더까지만
        self.wfile.write(body if isinstance(body,bytes) else body.encode("utf-8"))
    def do_HEAD(self):
        """일부 링크 미리보기 크롤러·모니터링이 HEAD로 먼저 찔러본다.
        구현이 없으면 501이 떠서 '죽은 사이트'로 오해받는다. 본문 없이 헤더만 돌려준다."""
        self._head_only=True
        try: self.do_GET()
        finally: self._head_only=False
    def admin_ok(self):
        """관리자 본인인가. **게스트 초대는 여기서 통과 못 한다**(관리자 쿠키만 인정).
        관리자 전용 화면·데이터의 유일한 관문."""
        if not MAINT_KEY: return False
        ck=self.headers.get("Cookie") or ""
        return ("rsgg_preview="+MAINT_KEY) in ck

    def maint_ok(self):
        """점검 모드를 통과할 수 있는가 (관리자 미리보기 또는 유효한 게스트 쿠키)."""
        if not MAINT_KEY: return False
        ck=self.headers.get("Cookie") or ""
        if ("rsgg_preview="+MAINT_KEY) in ck: return True
        m=re.search(r"rsgg_guest=(\d{1,12}-[0-9a-f]{20})",ck)
        return bool(m and guest_ok(m.group(1)))   # 매 요청 재검증 — 만료 순간 자동 차단

    def do_GET(self):
        u=urllib.parse.urlparse(self.path); path=u.path
        qs=urllib.parse.parse_qs(u.query)
        # 관리자 미리보기 진입: ?preview=<키> → 쿠키를 심고 원래 주소로 보낸다
        if MAINT_KEY and (qs.get("preview",[""])[0]==MAINT_KEY):
            self.send_response(302)
            self.send_header("Set-Cookie","rsgg_preview="+MAINT_KEY+"; Path=/; Max-Age=5184000; SameSite=Lax")  # 60일
            self.send_header("Location",path or "/"); self.end_headers(); return
        # 게스트 초대 진입: ?guest=<만료-서명> → 유효할 때만 쿠키를 심는다
        g=qs.get("guest",[""])[0]
        if g and guest_ok(g):
            exp=int(g.split("-")[0])
            self.send_response(302)
            self.send_header("Set-Cookie",f"rsgg_guest={g}; Path=/; Max-Age={max(1,exp-int(time.time()))}; SameSite=Lax")
            self.send_header("Location",path or "/"); self.end_headers(); return
        # 게스트 링크 발급 (관리자 전용 — 키가 틀리면 존재 자체를 숨긴다)
        if path=="/api/guest":
            if not (MAINT_KEY and qs.get("key",[""])[0]==MAINT_KEY):
                return self._send(404,"not found","text/plain")
            try: hours=min(168.0,max(0.05,float(qs.get("hours",["2"])[0])))
            except ValueError: hours=2.0
            exp=int(time.time()+hours*3600)
            host=self.headers.get("X-Forwarded-Host") or self.headers.get("Host") or ""
            proto=self.headers.get("X-Forwarded-Proto") or ("https" if ":443" in host else "http")
            url=f"{proto}://{host.split(',')[0].strip()}/?guest={exp}-{guest_sig(exp)}"
            return self._send(200,json.dumps({"url":url,"hours":hours,
                "expires_kst":time.strftime("%Y-%m-%d %H:%M",time.gmtime(exp+9*3600))},ensure_ascii=False))
        # 점검 중: /api/status 만 열어두고(서버가 잠들지 않게) 나머지는 안내 페이지
        # (MAINT_BLANK=1 이면 안내 페이지 대신 빈 화면 — 로고·문구조차 안 보인다)
        if MAINTENANCE and path!="/api/status" and not self.maint_ok():
            if MAINT_BLANK: return self._send(404,BLANK_HTML,"text/html","no-store")
            return self._send(503,MAINT_HTML,"text/html","no-store")
        if path=="/api/status":
            with LOCK: st={"live":True,"last_refresh":CACHE["last_refresh"],
                "players":len(CACHE["players"]),"seasons":CACHE["seasons"],
                "redis":REDIS_STATE["ok"],"redis_err":REDIS_STATE["err"],
                "names":len(NAME_HIST),"renamed":HIST_STATE["n"],"hist_bytes":HIST_STATE["bytes"],
                # 게임서버 접속 상태 — 웹서버가 살아있어도 여기가 죽으면 데이터가 안 쌓인다
                "game":GAME_STATE["ok"],"game_err":GAME_STATE["err"],"game_port":PORT,
                "stale_min":int((time.time()-CACHE["last_refresh"])/60) if CACHE["last_refresh"] else None,
                "rank_days":len(RANK["days"]),"rank_base":RANK["base_day"]}
            return self._send(200,json.dumps(st))
        if path=="/api/renamed":
            # 관리자 전용 — 개명한 유저 전체 목록.
            # ⚠️ 권한 없으면 403이 아니라 **404**를 준다. 있다는 사실 자체를 숨기려는 것
            #    (403이면 "여기 뭔가 있다"는 신호가 된다).
            if not self.admin_ok(): return self._send(404,"not found","text/plain")
            out=[]
            with LOCK:
                for pid,rec in NAME_HIST.items():
                    prev=rec.get("prev")
                    if not prev: continue
                    p=CACHE["players"].get(pid,{})
                    out.append({"id":pid,"n":rec.get("cur") or p.get("n",""),"prev":list(prev),
                                "r":p.get("r",""),"lv":p.get("lv"),"wr":p.get("wr"),"ts":rec.get("ts")})
                st={"names":len(NAME_HIST),"hist_bytes":HIST_STATE["bytes"]}
            # 최근에 바꾼 사람 먼저, 시각을 모르면(옛 기록·경기에서 주운 이름) 뒤로
            out.sort(key=lambda x:(-(x["ts"] or 0), -len(x["prev"])))
            return self._send(200,json.dumps({"players":out,**st},ensure_ascii=False),
                              "application/json","no-store")
        if path=="/api/search":
            qs=urllib.parse.parse_qs(u.query); q=(qs.get("q",[""])[0] or "").strip().lower()
            # 현재 닉네임 매치와 옛 닉네임 매치를 따로 모은다.
            # ⚠️ 예전엔 한 리스트에 담고 300개에서 끊었다. dict 순회 순서상 현재 닉네임
            #    매치가 먼저 300개를 채우면 옛 닉네임으로 검색한 사람이 통째로 잘렸다.
            LIMIT=300; PREV_MIN=100
            hit_cur=[]; hit_prev=[]
            if q:
                with LOCK:
                    for pid,rec in NAME_HIST.items():
                        # 양쪽 다 300이면 그만 — 어차피 아래에서 300으로 자르므로 결과는 같고,
                        # 한 글자 검색어로 수천 건이 걸릴 때 LOCK을 쥔 시간만 줄어든다
                        if len(hit_cur)>=LIMIT and len(hit_prev)>=LIMIT: break
                        cur=rec.get("cur") or ""; prevs=rec.get("prev",[])
                        # 옛 닉네임은 **전부** 훑어서 맞은 것을 모두 돌려준다(6번 바꿨으면 6개 다 대상)
                        mp=[pn for pn in prevs if pn and q in pn.lower()]
                        curhit=q in cur.lower()
                        if not curhit and not mp: continue
                        dst=hit_cur if curhit else hit_prev
                        if len(dst)>=LIMIT: continue
                        p=CACHE["players"].get(pid,{})
                        dst.append({"id":pid,"n":cur or p.get("n",""),"r":p.get("r",""),
                            "wr":p.get("wr"),"lv":p.get("lv"),"prev":list(prevs),"pm":([] if curhit else mp),
                            "rk":CACHE["player_ranks"].get(pid,{})})   # 랭킹은 별도 캐시에 있음
            # 우리 DB에 없으면 게임서버에 되묻는다(정확한 이름 일치).
            # ⚠️ 로컬에서 한 명이라도 나오면 부르지 않는다 — 매 검색마다 1~2초를 쓸 순 없다.
            found_live=0
            if q and not hit_cur and not hit_prev:
                for pid in search_game(q):
                    with LOCK:
                        p=CACHE["players"].get(pid)
                        rec=NAME_HIST.get(pid) or {}
                        rk=CACHE["player_ranks"].get(pid,{})
                    if not p: continue
                    hit_cur.append({"id":pid,"n":rec.get("cur") or p.get("n",""),"r":p.get("r",""),
                        "wr":p.get("wr"),"lv":p.get("lv"),"prev":list(rec.get("prev") or []),
                        "pm":[],"rk":rk})
                    found_live+=1
            # 현재 닉네임 매치가 300을 다 채워도 옛 닉네임 몫 100은 보장한다
            keep=min(len(hit_prev),max(PREV_MIN,LIMIT-len(hit_cur)))
            res=(hit_cur[:LIMIT-keep]+hit_prev[:keep])[:LIMIT]
            if found_live:
                return self._send(200,json.dumps({"results":res,"live":found_live},ensure_ascii=False))
            return self._send(200,json.dumps({"results":res}))
        if path.startswith("/api/detail/"):
            # 프로필 상세를 서버 메모리 캐시에서 즉시 준다(게임서버 접속 없음, 1KB대).
            # site_data.js에서 뺀 부분을 여기로 돌린 것. 실시간 최신값이 필요하면 /api/player/.
            pid=urllib.parse.unquote(path[len("/api/detail/"):])
            if not VALID_PID.match(pid):
                return self._send(404,json.dumps({"ok":False,"player":None}))
            with LOCK:
                p=CACHE["players"].get(pid)
                q=(dict(p) if p else None)
                if q is not None: q["rk"]=CACHE["player_ranks"].get(pid,{})
            return self._send(200 if q else 404,json.dumps({"ok":bool(q),"player":q}))
        if path.startswith("/api/player/"):
            pid=urllib.parse.unquote(path[len("/api/player/"):])
            # ⚠️ 형식부터 본다. 예전엔 아무 문자열이나 그대로 게임서버에 물어봐서,
            #    잘못된 주소로 들어온 사람이 최대 20초를 기다렸다(그동안 연결도 하나 점유).
            if not VALID_PID.match(pid):
                return self._send(404,json.dumps({"ok":False,"player":None}))
            try:
                p=live_player(pid)
                return self._send(200 if p else 404,json.dumps({"ok":bool(p),"player":p,"ts":int(time.time())}))
            except Exception as e:
                return self._send(500,json.dumps({"ok":False,"error":str(e)}))
        # ── 정적 파일 ────────────────────────────────────────────
        # ⚠️ 예전엔 폴더 아래 **모든 파일**을 내줬다. 그래서 /data/players.json(15.7MB),
        #    /data/name_history.json, /server.py 소스까지 전부 다운로드가 됐다.
        #    이제 화면에 실제로 필요한 것만 허용한다(화이트리스트).
        fn="index.html" if path in ("/","") else path.lstrip("/")
        if fn=="site_data.js":
            # 파일이 아니라 메모리에서 준다 (요청마다 3.9MB를 읽지 않는다)
            if not SITE_JS["bytes"]: return self._send(503,"data not ready","text/plain")
            return self._send(200,SITE_JS["bytes"],"application/javascript","no-cache")
        ALLOWED={"index.html":"text/html","i18n.js":"application/javascript",
                 "favicon.ico":"image/x-icon","robots.txt":"text/plain"}
        ct=ALLOWED.get(fn)
        if ct is None and fn.startswith("img/") and not fn.startswith("img/../"):
            ct=("image/png" if fn.endswith(".png") else "image/webp" if fn.endswith(".webp")
                else "image/svg+xml" if fn.endswith(".svg") else None)
        if ct is None:
            return self._send(404,"not found","text/plain")
        fp=os.path.realpath(os.path.join(HERE,fn))
        if not (os.path.isfile(fp) and fp.startswith(os.path.realpath(HERE)+os.sep)):
            return self._send(404,"not found","text/plain")
        # html/js는 항상 새로 받게(수정 후 옛 화면 방지), 이미지는 오래 캐시
        cache="public, max-age=604800" if fn.startswith("img/") else "no-cache"
        with open(fp,"rb") as f: data=f.read()
        extra=None
        if fn=="index.html":
            # OG 태그의 주소를 실제 접속 주소로 맞춘다.
            # ⚠️ 주소를 코드에 박으면 서비스 이름을 바꿀 때마다 공유 카드가 깨진다.
            host=self.headers.get("X-Forwarded-Host") or self.headers.get("Host") or ""
            proto=self.headers.get("X-Forwarded-Proto") or ("https" if ":443" in host else "http")
            if host: data=data.replace(b"__SITE_URL__",(proto+"://"+host.split(",")[0].strip()).encode())
            # 관리자 전용 화면은 **관리자에게만 끼워 넣는다.** 남이 받는 index.html에는
            # 코드가 한 글자도 안 들어가서 소스를 봐도 그런 메뉴가 있는 줄 모른다.
            # ⚠️ admin.js는 ALLOWED에 없다 → /admin.js 로 직접 받아갈 수 없다.
            adm=b""
            if self.admin_ok():
                try:
                    with open(os.path.join(HERE,"admin.js"),"rb") as f: adm=f.read()
                except Exception as e:
                    print("[관리자] admin.js 읽기 실패:",e)
                cache="no-store"     # 관리자용 화면이 캐시에 남지 않게
            # 관리자가 아니면 자리표시자까지 **지운다**. 남겨두면 "여기 뭔가 들어가는구나"가 보인다
            data=data.replace(b"//__X__",adm)
            # ⚠️ 쿠키에 따라 내용이 달라진다. 중간 캐시가 섞어버리지 않도록 알린다.
            extra=[("Vary","Cookie")]
        return self._send(200,data,ct,cache,extra)

def main():
    if not PID or not SEC:
        print("!! 환경변수 RS_PID / RS_SEC 가 설정되지 않았습니다. (클라우드 대시보드에서 설정)")
    print("데이터 로드 중...")
    load_disk()
    rank_tick()          # 재시작이면 저장돼 있던 오늘 기준선을 다시 읽는다(새 기준선은 진짜 갱신 뒤에)
    build_site_data()
    threading.Thread(target=boot_refresh,daemon=True).start()   # 씨앗 → 실데이터 (1~2분)
    threading.Thread(target=scheduler,daemon=True).start()
    threading.Thread(target=sweep_scheduler,daemon=True).start()
    srv=http.server.ThreadingHTTPServer(("0.0.0.0",WEB_PORT),H)
    print(f"\n✅ RS.GG 서버 실행 중 →  http://localhost:{WEB_PORT}")
    print(f"   리더보드 {LB_REFRESH_SEC//60}분마다 자동 갱신 · 프로필은 새로고침 버튼으로 실시간")
    print("   (종료: Ctrl+C)\n")
    try: srv.serve_forever()
    except KeyboardInterrupt: print("\n종료")

if __name__=="__main__": main()
