// SPDX-License-Identifier: Apache-2.0
// "Copy" buttons next to share links: <button data-copy="<input id>">.
document.addEventListener("click", (event) => {
  const button = event.target.closest("button[data-copy]");
  if (!button) return;
  const input = document.getElementById(button.dataset.copy);
  if (!input) return;
  input.select();
  const done = () => { button.textContent = "Copied"; setTimeout(() => { button.textContent = "Copy"; }, 2000); };
  if (navigator.clipboard && window.isSecureContext) {
    navigator.clipboard.writeText(input.value).then(done, () => { document.execCommand("copy"); done(); });
  } else {
    document.execCommand("copy");
    done();
  }
});
for (const button of document.querySelectorAll("button[data-copy]")) button.hidden = false;
