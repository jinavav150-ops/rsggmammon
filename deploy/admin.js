// 관리자 전용 화면 — **서버가 관리자에게만 index.html에 끼워 넣는다.**
// ⚠️ 정적 화이트리스트(ALLOWED)에 넣지 말 것. 넣으면 /admin.js 로 아무나 받아간다.
// 일반 방문자가 받는 index.html에는 이 코드가 한 글자도 들어가지 않는다.
//
// 하는 일: ① 상단 탭에 '개명' 추가  ② #/renamed 화면  ③ /api/renamed 에서 데이터
// 기존 함수를 감싸는(monkey-patch) 방식이라 index.html 본문엔 흔적이 없다.
(function(){
  var _tabs = tabsHtml, _render = render;

  tabsHtml = function(){
    var h = _tabs();
    var on = (location.hash || '').indexOf('#/renamed') === 0;
    var tab = '<div class="tab' + (on ? ' on' : '') + '" onclick="goRenamed()">개명 ' +
      '<span style="font-size:9px;color:var(--acc2);vertical-align:2px">관리자</span></div>';
    // ⚠️ replace('</div>')를 쓰면 **첫 번째** </div>(= 첫 탭의 닫는 태그)에 걸려
    //    새 탭이 그 탭 안으로 들어가 버린다. 맨 끝(.tabs를 닫는 것)에만 넣는다.
    return h.replace(/<\/div>\s*$/, tab + '</div>');
  };

  window.goRenamed = function(){ goHash('#/renamed'); };

  render = function(){
    var h = location.hash || '';
    try { h = decodeURIComponent(h); } catch(e){}
    if (h.indexOf('#/renamed') === 0) return renderRenamed();
    return _render();
  };
  // ⚠️ addEventListener는 **등록 당시의 함수 참조**를 붙들고 있다. 전역 render를 바꿔도
  //    리스너는 옛 함수를 계속 부른다(그래서 #/renamed로 가도 랭킹이 떴다). 갈아끼운다.
  window.removeEventListener('hashchange', _render);
  window.addEventListener('hashchange', render);

  var CACHE = null;

  function renderRenamed(){
    hidePerkTip();
    if (CACHE) return paint(CACHE);
    app.innerHTML = tabsHtml() + '<div class="card"><div class="empty">불러오는 중…</div></div>';
    fetch('/api/renamed')
      .then(function(r){ if(!r.ok) throw new Error(r.status); return r.json(); })
      .then(function(j){ CACHE = j; if ((location.hash||'').indexOf('#/renamed')===0) paint(j); })
      .catch(function(e){
        app.innerHTML = tabsHtml() +
          '<div class="card"><div class="empty">불러오기 실패 (' + esc(String(e.message||e)) + ')<br>' +
          '<span class="muted" style="font-size:12px">관리자 쿠키가 만료됐을 수 있습니다. ' +
          '주소 뒤에 ?preview=&lt;키&gt; 를 붙여 다시 들어오세요.</span></div></div>';
      });
  }

  // 로컬시간 기준 "N일 전". ts가 없으면(옛 기록·경기에서 주운 이름) 빈칸.
  function ago(ts){
    if (!ts) return '';
    var s = Math.floor(Date.now()/1000 - ts);
    if (s < 3600) return Math.floor(s/60) + '분 전';
    if (s < 86400) return Math.floor(s/3600) + '시간 전';
    return Math.floor(s/86400) + '일 전';
  }

  function paint(j){
    var rows = j.players || [];
    var h = tabsHtml();
    h += '<div class="sect-t">개명 이력 ' + rows.length + '명' +
         '<span class="muted" style="font-size:12px;font-weight:400"> · 이름 ' + (j.names||0) + '개 추적 중' +
         (j.hist_bytes ? ' · 저장 ' + (j.hist_bytes/1024).toFixed(1) + 'KB' : '') + '</span></div>';
    h += '<div class="card" style="overflow-x:auto"><table><thead><tr>' +
         '<th>현재 닉네임</th><th>이전 닉네임 (최신순)</th>' +
         '<th class="num">횟수</th><th class="hcol">지역</th><th class="hcol">마지막 개명</th>' +
         '</tr></thead><tbody>';
    if (!rows.length) h += '<tr><td colspan="5" class="empty">아직 개명한 유저가 없습니다</td></tr>';
    for (var i=0; i<rows.length; i++){
      var p = rows[i];
      var chips = (p.prev||[]).map(function(pn){
        return '<span style="display:inline-block;background:var(--panel2);border:1px solid var(--line);' +
               'border-radius:6px;padding:1px 8px;margin:2px 4px 2px 0;color:var(--acc2);font-weight:600">' +
               esc(pn) + '</span>';
      }).join('');
      h += '<tr' + (p.id ? ' onclick="goPlayer(\'' + p.id + '\')" style="cursor:pointer"' : '') + '>' +
           '<td><span class="name">' + esc(p.n||'') + '</span></td>' +
           '<td style="font-size:12px">' + chips + '</td>' +
           '<td class="num">' + (p.prev||[]).length + '</td>' +
           '<td class="muted hcol">' + esc(regionName(p.r)) + '</td>' +
           '<td class="muted hcol" style="font-size:12px">' + ago(p.ts) + '</td></tr>';
    }
    app.innerHTML = h + '</tbody></table></div>';
  }
})();
