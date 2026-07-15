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

// 서버 에러(400 등)를 진짜 실패로 인식하는 공용 POST 헬퍼.
// 기존 코드처럼 .then(r => r.json()).then(() => toast(success)) 로만 짜면
// 서버가 {"error": "..."} 를 400으로 내려줘도 fetch 자체는 성공했기 때문에
// 무조건 성공 토스트가 뜨는 버그가 생긴다. 이 헬퍼를 쓰면 그 문제가 없다.
function apiPost(url, body) {
  return fetch(url, {
    method: "POST", headers: {"Content-Type": "application/json"},
    body: JSON.stringify(body || {})
  }).then(r => r.json().then(data => {
    if (!r.ok || data.error) {
      throw new Error(data.error || "요청에 실패했습니다.");
    }
    return data;
  }));
}
