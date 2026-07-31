#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""RS.GG 로컬 백엔드 서버.
- 정적 사이트 제공 (index.html, site_data.js)
- 리더보드 30분마다 자동 갱신
- /api/player/<id> : 그 유저를 게임서버에서 실시간 조회
실행:  python3 server.py   →  브라우저에서 http://localhost:8000
"""
import socket, struct, json, os, time, threading, http.server, urllib.parse, urllib.request

# ── 설정 ─────────────────────────────────────────────
HOST="frontend-a76415741fc3480f.elb.us-east-1.amazonaws.com"; IP="35.153.75.130"; PORT=21300
PID=os.environ.get("RS_PID",""); SEC=os.environ.get("RS_SEC","")  # 환경변수에서 (코드에 secret 없음)
SEASON="202607"
LB_REFRESH_SEC=1800   # 30분
WEB_PORT=int(os.environ.get("PORT","8000"))
HERE=os.path.dirname(os.path.abspath(__file__))
DATA=os.path.join(HERE,"data")
HEROES={"108140568":"Dread","80931949":"Khan","61223451":"Vector","631049":"Oni","631047":"Remedy",
"97251807":"Sejin","631045":"Twinkle","631042":"Jagger","631040":"Calibri","631036":"Leo",
"45850136":"Magnus","65893852":"Nova","91887043":"Fury"}
REGION_KR={"ap-northeast-2":"한국","eu-central-1":"유럽","us-east-1":"미국동부",
"us-west-2":"미국서부","ap-southeast-1":"싱가포르","ap-south-1":"인도"}
LEVEL_KR={"85682979":"퍼시피카","34624828":"항구","57585175":"하늘공원","36077481":"균열 관측소","41172989":"용광로"}
TN=json.load(open(os.path.join(DATA,"tech_names.json"),encoding="utf-8"))
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
def connect():
    try: s=socket.create_connection((HOST,PORT),timeout=10)
    except Exception: s=socket.create_connection((IP,PORT),timeout=10)
    s.settimeout(20)
    s.sendall(fr({"request_id":1,"type":"authenticate_account"},{"player_id":PID,"authentication_secret":SEC})); rd(s)
    return s
def skin_name(v):
    if v is None: return None
    nm=TN.get(str(v))
    if not nm: return None
    p=nm.split("_"); return " ".join(p[2:]) if len(p)>2 else nm
def parse_acc(acc):
    cc=(acc.get("counters_state") or {}).get("counter_collection",{})
    g=lambda k:(cc.get(k,{}) or {}).get("value")
    wins=g(C["wins"]); m=g(C["matches"]); k=g(C["kills"]); d=g(C["deaths"]); pt=g(C["playtime"])
    heroes={}
    for hid,hv in (acc.get("heroes_state") or {}).get("hero_collection",{}).items():
        hcc=hv.get("counter_collection",{}); hg=lambda kk:(hcc.get(kk,{}) or {}).get("value")
        hpt=hg(H_PT)
        heroes[HEROES.get(hid,hid)]={"lv":hv.get("level"),"pt":round(hpt/3600,1) if isinstance(hpt,int) else None,
            "rt":hg(H_RT),"pre":hg(H_PRE),"sk":skin_name(hg(H_SKIN))}
    regs=acc.get("regions") or []
    return {"n":(acc.get("player_state") or {}).get("name",""),"r":REGION_KR.get(regs[0],regs[0]) if regs else "",
        "lv":g(C["level"]),"wr":round(wins/m*100,1) if (isinstance(wins,int) and isinstance(m,int) and m) else None,
        "mvp":g(C["mvp"]),"m":m,"w":wins,"kd":round(k/d,2) if (isinstance(k,int) and isinstance(d,int) and d) else None,
        "k":k,"d":d,"dmg":g(C["damage"]),"heal":g(C["heal"]),"db":g(C["double"]),"tr":g(C["triple"]),
        "fh":g(C["final"]),"pt":round(pt/3600) if isinstance(pt,int) else None,"pm":g(C["pro_matches"]),
        "rm":(acc.get("match_state") or {}).get("match_history",[])[:10],"h":heroes}

# ── 캐시 ─────────────────────────────────────────────
LOCK=threading.Lock()
CACHE={"boards":{},"board_meta":{},"board_labels":[],"hero_labels":[],
       "players":{},"player_ranks":{},"season":SEASON,"last_refresh":0}
NAME_HIST={}
# ── 닉네임 이력 (Upstash Redis 단일키 저장 + 백그라운드 전원검사) ──
UPSTASH_URL=os.environ.get("UPSTASH_URL","").rstrip("/")
UPSTASH_TOKEN=os.environ.get("UPSTASH_TOKEN","")
HIST_KEY="rsgg:namehist"
SWEEP_SEC=int(os.environ.get("SWEEP_SEC","21600"))  # 전원 이름검사 주기(기본 6시간)
def redis_cmd(*args):
    if not UPSTASH_URL or not UPSTASH_TOKEN: return None
    try:
        req=urllib.request.Request(UPSTASH_URL+"/",data=json.dumps(list(args)).encode(),
            headers={"Authorization":"Bearer "+UPSTASH_TOKEN,"Content-Type":"application/json"})
        with urllib.request.urlopen(req,timeout=8) as r:
            return json.loads(r.read().decode()).get("result")
    except Exception: return None
def hist_load():
    raw=redis_cmd("GET",HIST_KEY)
    if raw:
        try:
            d=json.loads(raw); NAME_HIST.clear(); NAME_HIST.update(d)
            print(f"이름이력 Redis 로드 {len(NAME_HIST)}명"); return
        except Exception: pass
    try:
        d=json.load(open(os.path.join(DATA,"name_history.json"),encoding="utf-8"))
        NAME_HIST.clear(); NAME_HIST.update(d); print(f"이름이력 baseline 로드 {len(NAME_HIST)}명")
    except Exception as e: print("이름이력 로드 실패:",e)
def hist_save():
    if UPSTASH_URL and UPSTASH_TOKEN:
        redis_cmd("SET",HIST_KEY,json.dumps(NAME_HIST,ensure_ascii=False))
def hist_observe(pid,name):
    # 이름 관측 → 개명 감지시 기록. 반환 (prev리스트, 변경여부)
    if not name: return NAME_HIST.get(pid,{}).get("prev",[]), False
    rec=NAME_HIST.get(pid)
    if rec is None:
        NAME_HIST[pid]={"cur":name,"prev":[]}; return [], False
    if rec.get("cur")!=name:
        old=rec.get("cur")
        if old and old not in rec["prev"]: rec["prev"].insert(0,old)
        rec["cur"]=name; return rec["prev"], True
    return rec.get("prev",[]), False

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
    hist_load()
    with LOCK:
        for pid,rec in NAME_HIST.items():
            if pid in CACHE["players"]: CACHE["players"][pid]["prev"]=rec.get("prev",[])

def to_compact_from_raw(v):
    heroes={}
    for hn,hd in (v.get("heroes") or {}).items():
        heroes[hn]={"lv":hd.get("lv"),"pt":hd.get("pt"),"rt":hd.get("rt"),"pre":hd.get("pre"),"sk":hd.get("skin")}
    return {"n":v.get("name",""),"r":v.get("region",""),"lv":v.get("level"),"wr":v.get("winrate"),
        "mvp":v.get("mvp"),"m":v.get("matches"),"w":v.get("wins"),"kd":v.get("kd"),"k":v.get("kills"),
        "d":v.get("deaths"),"dmg":v.get("damage"),"heal":v.get("heal"),"db":v.get("double"),
        "tr":v.get("triple"),"fh":v.get("final"),"pt":v.get("playtime_h"),"pm":v.get("pro_matches"),
        "rm":v.get("recent_matches",[]),"h":heroes}

def label_of(kind,label): return label if kind=="hero" else ("프로" if kind=="pro" else "캐주얼")

def apply_leaderboards(lb):
    boards={}; meta={}; ranks={}; blabels=[]; hlabels=[]
    for name,mb in lb["boards"].items():
        lab=label_of(mb["kind"],mb["label"]); blabels.append(lab)
        meta[lab]={"size":mb["size"],"kind":mb["kind"]}
        if mb["kind"]=="hero": hlabels.append(lab)
        ordered=[]
        for e in mb["entries"]:
            pid=e["player_id"]; ordered.append(pid); ranks.setdefault(pid,{})[lab]=[e["rank"],e["score"]]
        boards[lab]=ordered
    with LOCK:
        CACHE.update({"boards":boards,"board_meta":meta,"board_labels":blabels,
            "hero_labels":hlabels,"player_ranks":ranks,"last_refresh":int(time.time())})

def build_site_data():
    with LOCK:
        players={}
        for pid,p in CACHE["players"].items():
            q=dict(p); q["rk"]=CACHE["player_ranks"].get(pid,{}); players[pid]=q
        out={"season":CACHE["season"],"board_labels":CACHE["board_labels"],"hero_labels":CACHE["hero_labels"],
            "board_meta":CACHE["board_meta"],"boards":CACHE["boards"],"players":players,
            "generated_count":len(players),"last_refresh":CACHE["last_refresh"]}
    with open(os.path.join(HERE,"site_data.js"),"w",encoding="utf-8") as f:
        f.write("window.DATA="); json.dump(out,f,ensure_ascii=False,separators=(",",":")); f.write(";")

def refresh_leaderboards():
    print("[갱신] 리더보드 수집...")
    s=connect(); rid=500; result={"season":SEASON,"boards":{}}; allids=set()
    boards=[("pro","pro",f"rating_pro_0_{SEASON}"),("casual","casual",f"rating_casual_0_{SEASON}")]
    boards+=[("hero",HEROES[h],f"rating_heroes_{h}_{SEASON}") for h in HEROES]
    for kind,label,name in boards:
        rid+=1; s.sendall(fr({"request_id":rid,"type":"get_leaderboard"},{"leaderboard_name":name,"top_size":20000}))
        body=None
        for _ in range(50):
            env,b=rd(s)
            if env.get("type")=="get_leaderboard" and env.get("request_id")==rid: body=b; break
        if not body: continue
        ids=body.get("top_player_ids",[]); sc=body.get("top_player_scores",[])
        result["boards"][name]={"kind":kind,"label":label,"size":body.get("leaderboard_size"),
            "entries":[{"rank":i+1,"player_id":pid,"score":s2} for i,(pid,s2) in enumerate(zip(ids,sc))]}
        allids.update(ids)
    s.close()
    result["unique_player_ids"]=sorted(allids)
    json.dump(result,open(os.path.join(DATA,"leaderboards.json"),"w",encoding="utf-8"),ensure_ascii=False)
    apply_leaderboards(result); build_site_data()
    print(f"[갱신] 완료 · 고유유저 {len(allids)} · {time.strftime('%H:%M:%S')}")

def scheduler():
    while True:
        time.sleep(LB_REFRESH_SEC)
        try: refresh_leaderboards()
        except Exception as e: print("[갱신] 오류:",e)

def sweep_names():
    ids=list(CACHE["players"].keys())
    if not ids: return
    print(f"[이름검사] {len(ids)}명 이름 수집...")
    s=connect(); rid=8000; changed=0; i=0
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
                            _,ch=hist_observe(pid,nm)
                            if ch:
                                changed+=1
                                if pid in CACHE["players"]: CACHE["players"][pid]["n"]=nm
                    break
            i+=60
        except (ConnectionError, socket.timeout, OSError):
            try: s.close()
            except: pass
            time.sleep(1); s=connect(); continue
        time.sleep(0.1)
    s.close()
    with LOCK:
        for pid,rec in NAME_HIST.items():
            if pid in CACHE["players"]: CACHE["players"][pid]["prev"]=rec.get("prev",[])
    hist_save(); build_site_data()
    print(f"[이름검사] 완료 · 개명 {changed}건")
def sweep_scheduler():
    while True:
        time.sleep(SWEEP_SEC)
        try: sweep_names()
        except Exception as e: print("[이름검사] 오류:",e)

def hero_name(hid): return HEROES.get(str(hid),f"#{hid}")

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
                  "mv":p.get("PlayerId")==m.get("MVPId"),"me":p.get("PlayerId")==pid}
                 for p in prs]
        out.append({"ts":m.get("Timestamp"),"type":m.get("MatchType"),"t1":m.get("Team1Score"),"t2":m.get("Team2Score"),"dur":m.get("MatchTime"),
            "win":me.get("Team")==m.get("WinnerTeam"),"myhero":hero_name(me.get("LastUsedHeroId")),
            "mymvp":int(me.get("MvpPoints",0)),"myrt":me.get("Rating"),"myk":me.get("Eliminations"),"myd":me.get("Deaths"),"mys":(m.get("Team1Score") if me.get("Team")==1 else m.get("Team2Score")) or 0,"ens":(m.get("Team2Score") if me.get("Team")==1 else m.get("Team1Score")) or 0,"mvpme":me.get("PlayerId")==m.get("MVPId"),"map":LEVEL_KR.get(str(m.get("LevelId")),""),"dmg":me.get("Damage"),"players":players})
    out.sort(key=lambda x:-(x["ts"] or 0))
    return out[:20]

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
        except Exception as e:
            comp2["matches"]=[]
        return comp2
    finally:
        s.close()

# ── 웹서버 ───────────────────────────────────────────
class H(http.server.BaseHTTPRequestHandler):
    def log_message(self,*a): pass
    def _send(self,code,body,ctype="application/json"):
        self.send_response(code); self.send_header("Content-Type",ctype)
        self.send_header("Access-Control-Allow-Origin","*"); self.end_headers()
        self.wfile.write(body if isinstance(body,bytes) else body.encode("utf-8"))
    def do_GET(self):
        u=urllib.parse.urlparse(self.path); path=u.path
        if path=="/api/status":
            with LOCK: st={"live":True,"last_refresh":CACHE["last_refresh"],
                "players":len(CACHE["players"]),"boards":len(CACHE["boards"]),"season":CACHE["season"]}
            return self._send(200,json.dumps(st))
        if path=="/api/search":
            qs=urllib.parse.parse_qs(u.query); q=(qs.get("q",[""])[0] or "").strip().lower()
            res=[]
            if q:
                with LOCK:
                    for pid,rec in NAME_HIST.items():
                        cur=rec.get("cur") or ""; prevs=rec.get("prev",[])
                        if q in cur.lower(): mp=None
                        else:
                            mp=next((pn for pn in prevs if pn and q in pn.lower()),None)
                            if mp is None: continue
                        p=CACHE["players"].get(pid,{})
                        res.append({"id":pid,"n":cur or p.get("n",""),"r":p.get("r",""),
                            "wr":p.get("wr"),"lv":p.get("lv"),"prev":prevs,"pm":mp,"rk":p.get("rk",{})})
                        if len(res)>=300: break
            return self._send(200,json.dumps({"results":res}))
        if path.startswith("/api/player/"):
            pid=urllib.parse.unquote(path[len("/api/player/"):])
            try:
                p=live_player(pid)
                return self._send(200 if p else 404,json.dumps({"ok":bool(p),"player":p,"ts":int(time.time())}))
            except Exception as e:
                return self._send(500,json.dumps({"ok":False,"error":str(e)}))
        # 정적 파일
        fn="index.html" if path in ("/","") else path.lstrip("/")
        fp=os.path.join(HERE,fn)
        if os.path.isfile(fp) and os.path.abspath(fp).startswith(HERE):
            ct="text/html" if fn.endswith(".html") else "application/javascript" if fn.endswith(".js") else "text/plain"
            return self._send(200,open(fp,"rb").read(),ct)
        self._send(404,"not found","text/plain")

def main():
    if not PID or not SEC:
        print("!! 환경변수 RS_PID / RS_SEC 가 설정되지 않았습니다. (클라우드 대시보드에서 설정)")
    print("데이터 로드 중...")
    load_disk(); build_site_data()
    threading.Thread(target=scheduler,daemon=True).start()
    threading.Thread(target=sweep_scheduler,daemon=True).start()
    srv=http.server.ThreadingHTTPServer(("0.0.0.0",WEB_PORT),H)
    print(f"\n✅ RS.GG 서버 실행 중 →  http://localhost:{WEB_PORT}")
    print(f"   리더보드 {LB_REFRESH_SEC//60}분마다 자동 갱신 · 프로필은 새로고침 버튼으로 실시간")
    print("   (종료: Ctrl+C)\n")
    try: srv.serve_forever()
    except KeyboardInterrupt: print("\n종료")

if __name__=="__main__": main()
