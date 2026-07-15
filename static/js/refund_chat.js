// 환불 요청 채팅 위젯 — 선수 화면과 관리자 화면에서 공용으로 사용
let _refundSocket = null;

function escapeHtml(str) {
  const div = document.createElement("div");
  div.textContent = str;
  return div.innerHTML;
}

function ensureRefundSocket() {
  if (!_refundSocket) _refundSocket = io();
  return _refundSocket;
}

function openRefundChat(refundId, containerId) {
  const container = document.getElementById(containerId);
  if (!container) return;
  container.style.display = "block";
  container.dataset.refundId = refundId;
  loadRefundMessages(refundId, containerId);

  const socket = ensureRefundSocket();
  socket.emit("join_refund", {refund_id: refundId});
  socket.off("refund_message");
  socket.on("refund_message", (m) => {
    if (parseInt(container.dataset.refundId) === refundId) appendRefundMessage(containerId, m);
  });
  socket.off("refund_status");
  socket.on("refund_status", (d) => {
    const statusEl = container.querySelector(".refund-status-label");
    if (statusEl) statusEl.textContent = d.status;
  });
  socket.off("refund_ended");
  socket.on("refund_ended", () => {
    const list = container.querySelector(".refund-messages");
    if (list) list.insertAdjacentHTML("beforeend",
      `<p style="color:var(--ink-faint);font-size:13px;text-align:center;margin:12px 0;">주최자가 이 문의를 종료했습니다.</p>`);
    const input = container.querySelector(".refund-input");
    if (input) input.disabled = true;
  });
}

function loadRefundMessages(refundId, containerId) {
  fetch(`/api/refunds/${refundId}/messages`).then(r => r.json()).then(data => {
    const list = document.querySelector(`#${containerId} .refund-messages`);
    if (!list) return;
    list.innerHTML = data.messages.map(renderRefundMsg).join("") ||
      `<p style="color:var(--ink-faint);font-size:13px;text-align:center;margin:20px 0;">아직 메시지가 없습니다. 아래에서 첫 메시지를 남겨보세요.</p>`;
    list.scrollTop = list.scrollHeight;
    const statusEl = document.querySelector(`#${containerId} .refund-status-label`);
    if (statusEl) statusEl.textContent = data.status;
  });
}

function renderRefundMsg(m) {
  return `<div class="refund-msg ${m.is_admin ? 'from-admin' : 'from-player'}">
    <span class="refund-msg-meta">${escapeHtml(m.sender)} · ${m.created_at}</span>
    <div class="refund-msg-bubble">${escapeHtml(m.message)}</div>
  </div>`;
}

function appendRefundMessage(containerId, m) {
  const list = document.querySelector(`#${containerId} .refund-messages`);
  if (!list) return;
  const empty = list.querySelector("p");
  if (empty) list.innerHTML = "";
  list.insertAdjacentHTML("beforeend", renderRefundMsg(m));
  list.scrollTop = list.scrollHeight;
}

function sendRefundMessage(containerId) {
  const container = document.getElementById(containerId);
  const refundId = container.dataset.refundId;
  const input = container.querySelector(".refund-input");
  const text = input.value.trim();
  if (!text) return;
  fetch(`/api/refunds/${refundId}/messages`, {
    method: "POST", headers: {"Content-Type": "application/json"},
    body: JSON.stringify({message: text})
  }).then(r => r.json()).then(d => {
    if (d.error) { toast(d.error, "error"); return; }
    input.value = "";
  }).catch(() => toast("전송에 실패했습니다.", "error"));
}
