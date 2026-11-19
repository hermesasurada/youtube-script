/* 유튜브 스크립트 PC 화면 — 시작 코드. 반드시 마지막에 읽힌다.
   전역 범위를 함께 쓰는 일반 스크립트다. templates/index.html이 index.js → index-history → index-queue → index-run →
   index-urlqueue → index-summary → index-boot 순서로 읽는다(2026-11-17 index.js에서 분리). */
// 페이지가 열릴 때 바로 도는 호출 — 여러 파일로 나뉜 뒤에는 앞 파일이 모두 읽힌 다음에 돌아야 한다
// (한 파일일 때는 함수 선언이 끌어올려져 어디서 불러도 됐다). 순서는 원래 파일에서 나오던 순서 그대로.
const _prefetchChannels = () => { _fetchChannels(false).catch(() => {}); };
if ('requestIdleCallback' in window) requestIdleCallback(_prefetchChannels, {timeout: 1800});
else setTimeout(_prefetchChannels, 700);
if (!IS_REMOTE) loadSavedPrompt();   // /prompt 는 원격 비허용
if (!IS_REMOTE) { _pollKfStatus(); setInterval(_pollKfStatus, 4000); }
// 영상 모음에서 왔으면(?open=<id>) 목록을 받은 뒤 그 요약을 바로 연다 — 닫으면 모음으로(closeSummaryModal)
const _deepOpen = YS.collectionDeepLink();
loadHistory().then(() => {
  if (!_deepOpen) return;
  const it = _historyItems.find(i => Number(i.item_id) === _deepOpen.id);
  _collectionReturn = _deepOpen.returnable;
  openSummaryModal(_deepOpen.id, it ? it.title : '');
});
renderQueue();

/* ── 색상 모드(시스템·라이트·다크) — 공용 hermes-theme(2026-10-24, 예전 2단 토글 대체) ── */
HermesTheme.mount(document.getElementById('theme-slot'), { className: 'theme-btn' });

/* reader font init */
_applyReaderFont();
