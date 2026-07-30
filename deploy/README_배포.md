# RS.GG 클라우드 배포 안내 (Render 무료티어)

이 `deploy` 폴더를 클라우드에 올리면 **24시간 공개 URL**이 나옵니다.
실시간 새로고침 · 최근 경기 · 30분 자동갱신까지 전부 작동합니다.

> 추천: **Render** (무료, GitHub 연동, 클릭 위주라 제일 쉬움)

---

## ⚠️ 먼저 — 보안 규칙 2가지

1. **`server.py`에는 자격증명이 없습니다.** 로그인 secret은 코드가 아니라 **클라우드 환경변수**로 넣습니다 (아래 4단계). 절대 코드에 적지 마세요.
2. **GitHub 저장소는 "비공개(Private)"로** 만드세요. (다른 플레이어들 데이터 파일이 들어있음. 사이트 자체는 URL로 공개되지만, 원본 데이터/저장소는 비공개 유지)

환경변수에 넣을 값 (Render 대시보드에서만 입력):
```
RS_PID = 64711516-b89f-436b-b768-878b366f2d80
RS_SEC = 581c42b7-b642-41ab-9ce1-dcb5801cc5f2
```

---

## 1단계 · GitHub에 올리기

1. github.com 로그인 → 우상단 **＋ → New repository**
2. 이름 예: `rs-gg` → **Private** 선택 → Create
3. 이 `deploy` 폴더를 그 저장소에 올림. 터미널에서:
```bash
cd "/Users/jinhocheol/Downloads/내게임/deploy"
git init
git add .
git commit -m "RS.GG 배포"
git branch -M main
git remote add origin https://github.com/<내아이디>/rs-gg.git
git push -u origin main
```
(비밀번호 대신 GitHub 토큰이 필요할 수 있음 — 안내 뜨면 Personal Access Token 생성해 사용)

---

## 2단계 · Render에 배포

1. **render.com** 접속 → **Get Started** → GitHub 계정으로 로그인
2. 대시보드 → **New +** → **Web Service**
3. 방금 만든 `rs-gg` 저장소 **Connect**
4. 설정 (render.yaml 있으면 대부분 자동으로 채워짐):
   - **Language / Runtime**: Python 3
   - **Build Command**: `pip install -r requirements.txt`
   - **Start Command**: `python3 server.py`
   - **Instance Type**: **Free**

---

## 3단계 · 환경변수 입력 (중요)

배포 설정 화면 아래 **Environment Variables** (또는 Advanced) 에서 **Add Environment Variable**:

| Key | Value |
|-----|-------|
| `RS_PID` | `64711516-b89f-436b-b768-878b366f2d80` |
| `RS_SEC` | `581c42b7-b642-41ab-9ce1-dcb5801cc5f2` |

---

## 4단계 · 배포 완료

- **Create Web Service** 클릭 → 몇 분 뒤 배포 완료
- `https://rs-gg-xxxx.onrender.com` 같은 **공개 URL**이 나옴 → 이 주소를 남들에게 주면 됩니다

---

## 알아둘 점

- **무료티어는 15분간 접속이 없으면 잠듭니다.** 이후 첫 접속 때 30~50초 깨어나는 시간이 걸림(그 사이 자동갱신도 멈춤). 계속 켜두려면 유료(월 $7) 또는 외부 핑 서비스(UptimeRobot 등으로 5분마다 접속)로 깨워두면 됨.
- **데이터 갱신 방식:**
  - 리더보드: 서버가 30분마다 자동 갱신
  - 개별 유저 프로필·최근경기: 프로필 열 때 실시간 조회
  - 프로필 "기본 스냅샷"(players.json)을 최신화하려면, 로컬에서 `collect_leaderboards.py`+`collect_profiles.py`+`build_site.py` 다시 돌린 뒤 `data/` 파일들 교체해서 다시 push
- **서버 부하:** 방문자가 프로필 열 때마다 게임 서버에 실시간 조회가 갑니다. 인기가 많아지면 게임 서버 부하가 커질 수 있으니 감안하세요 (님 소유 서버).
- **자격증명 만료:** 그 계정 secret이 바뀌면 실시간 기능이 멈춥니다 → 다시 캡처해서 환경변수 갱신.

---

## 다른 무료 옵션 (Render가 안 맞으면)

- **Railway** (railway.app): `Procfile` 포함돼 있어 바로 됨. 최초 크레딧 무료, 이후 소액.
- **Fly.io**: CLI 필요, 무료 할당량 있음.
어느 쪽이든 **환경변수 RS_PID / RS_SEC 설정**과 **시작 명령 `python3 server.py`**는 동일합니다.

— Created & owned by MAMMON
