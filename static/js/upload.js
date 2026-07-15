// 파일 업로드 → /admin/upload 로 전송 → 반환된 URL을 지정한 입력창에 자동으로 채워줌
function wireUploader(fileInputId, targetFieldId, previewId) {
  const fileInput = document.getElementById(fileInputId);
  const target = document.getElementById(targetFieldId);
  const preview = previewId ? document.getElementById(previewId) : null;
  if (!fileInput || !target) return;

  fileInput.addEventListener("change", () => {
    const file = fileInput.files[0];
    if (!file) return;
    const fd = new FormData();
    fd.append("file", file);
    if (preview) preview.style.opacity = "0.4";
    fetch("/admin/upload", { method: "POST", body: fd })
      .then(r => r.json())
      .then(data => {
        if (data.error) { toast(data.error, "error"); if (preview) preview.style.opacity = "1"; return; }
        target.value = data.url;
        if (preview) { preview.src = data.url; preview.style.display = "block"; preview.style.opacity = "1"; }
        toast("업로드 완료! 저장 버튼을 눌러 반영하세요.", "success");
      })
      .catch(() => { toast("업로드에 실패했습니다.", "error"); if (preview) preview.style.opacity = "1"; });
  });
}
