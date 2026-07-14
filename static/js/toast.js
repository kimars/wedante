// 위단테 공통 UI 유틸: 토스트 알림 + 버튼 로딩 상태
function toast(message, type) {
  const stack = document.getElementById("toast-stack");
  if (!stack) { console.log(message); return; }
  const el = document.createElement("div");
  el.className = "toast" + (type ? " " + type : "");
  el.textContent = message;
  stack.appendChild(el);
  setTimeout(() => {
    el.classList.add("leaving");
    setTimeout(() => el.remove(), 300);
  }, 2600);
}

// 버튼에 로딩 스피너를 걸고, 끝나면 자동으로 원상복구하는 fetch 래퍼
function withLoading(button, fn) {
  if (!button) return fn();
  button.classList.add("loading");
  button.disabled = true;
  return Promise.resolve(fn()).finally(() => {
    button.classList.remove("loading");
    button.disabled = false;
  });
}

// 특정 요소에 잠깐 하이라이트 효과 (실시간 반영 시 어디가 바뀌었는지 표시)
function flashUpdate(el) {
  if (!el) return;
  el.classList.remove("updated");
  void el.offsetWidth; // reflow to restart animation
  el.classList.add("updated");
}
