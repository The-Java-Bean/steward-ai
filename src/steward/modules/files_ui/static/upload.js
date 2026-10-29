// SPDX-License-Identifier: Apache-2.0
// Resumable uploads for the file manager (protocol: /api/v1/uploads, docs/adr/0018).
// Files go up one at a time, in pieces; after a dropped connection the upload carries
// on from where the server's copy ends. Choosing the same file again later (even after
// closing the browser) resumes it.
"use strict";

(() => {
  const root = document.getElementById("uploads");
  if (!root || !window.fetch) return;
  const folder = root.dataset.folder;
  const scope = root.dataset.scope || "normal";
  const csrf = root.dataset.csrf;
  const list = document.getElementById("upload-list");
  const input = document.getElementById("upload-input");
  const zone = document.getElementById("dropzone");
  const STORE = "steward.uploads.v1";
  const MAX_FAILURES = 40;
  const queue = [];
  const listed = new Map(); // upload_id -> row of an unfinished upload shown at load
  let running = false;
  let doneCount = 0;
  let failedCount = 0;
  root.hidden = false;

  // --- remembering upload IDs ---------------------------------------------------------

  const load = () => {
    try {
      return JSON.parse(localStorage.getItem(STORE) || "{}");
    } catch {
      return {};
    }
  };
  const save = (map) => {
    try {
      localStorage.setItem(STORE, JSON.stringify(map));
    } catch {
      /* private window: resuming still works within this page */
    }
  };
  const keyOf = (file, name) => [folder, name, file.size, file.lastModified].join("\u0000");
  const remember = (key, id) => {
    const map = load();
    map[key] = id;
    save(map);
  };
  const forget = (key) => {
    const map = load();
    delete map[key];
    save(map);
  };

  // --- talking to the server ----------------------------------------------------------

  async function api(method, url, body, headers = {}) {
    const init = {
      method,
      credentials: "same-origin",
      headers: { "X-CSRF-Token": csrf, ...headers },
    };
    if (body instanceof Blob) {
      init.body = body;
      init.headers["Content-Type"] = "application/offset+octet-stream";
    } else if (body !== undefined) {
      init.body = JSON.stringify(body);
      init.headers["Content-Type"] = "application/json";
    }
    const response = await fetch(url, init);
    let data = null;
    try {
      data = await response.json();
    } catch {
      data = null;
    }
    return { status: response.status, data };
  }

  const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
  const percent = (part, whole) => (whole ? Math.floor((part / whole) * 100) : 100);
  const sizeText = (n) => {
    const units = ["B", "KB", "MB", "GB", "TB"];
    let i = 0;
    while (n >= 1000 && i < units.length - 1) {
      n /= 1000;
      i += 1;
    }
    return `${n.toFixed(i ? 1 : 0)} ${units[i]}`;
  };
  const errorText = (r, fallback) => {
    if (r && r.status === 401) return "You were logged out. Log in again, then choose the file again to resume.";
    if (r && r.status === 403) return (r.data && r.data.error) || "Not allowed. Reload the page and try again.";
    return (r && r.data && r.data.error) || fallback;
  };

  // --- one row per file ----------------------------------------------------------------

  function row(name, size) {
    const li = document.createElement("li");
    const title = document.createElement("span");
    title.className = "upload-name";
    title.textContent = `${name} (${sizeText(size)})`;
    const bar = document.createElement("progress");
    bar.max = Math.max(size, 1);
    bar.value = 0;
    const state = document.createElement("span");
    state.className = "upload-state hint";
    const actions = document.createElement("span");
    actions.className = "upload-actions";
    li.append(title, bar, state, actions);
    list.append(li);
    return { li, title, bar, state, actions };
  }

  function button(view, label, onClick, secondary = true) {
    const b = document.createElement("button");
    b.type = "button";
    b.className = secondary ? "small secondary" : "small";
    b.textContent = label;
    b.addEventListener("click", onClick);
    view.actions.append(b);
    return b;
  }

  function askKeepBoth(view, name, suggestion) {
    view.state.textContent = `“${name}” already exists here.`;
    return new Promise((resolve) => {
      const clear = () => view.actions.replaceChildren();
      button(view, `Keep both (save as “${suggestion}”)`, () => {
        clear();
        resolve(true);
      }, false);
      button(view, "Skip", () => {
        clear();
        resolve(false);
      });
    });
  }

  // --- uploading ------------------------------------------------------------------------

  async function findOpen(name, size) {
    const r = await api("GET", `/api/v1/uploads?scope=${encodeURIComponent(scope)}`);
    if (r.status !== 200) return null;
    return r.data.uploads.find((u) => u.folder === folder && u.name === name && u.size === size) || null;
  }

  async function upload(item) {
    const { file, view } = item;
    let key = keyOf(file, item.name);
    let id = load()[key] || null;
    let offset = 0;
    let chunk = 8 * 1024 * 1024;
    if (id) {
      const r = await api("GET", `/api/v1/uploads/${id}`);
      if (r.status === 200 && r.data.status === "open" && r.data.size === file.size) {
        offset = r.data.offset;
        chunk = r.data.chunk_bytes;
      } else {
        forget(key);
        id = null;
      }
    }
    if (!id) {
      const open = await findOpen(item.name, file.size);
      if (open) {
        id = open.upload_id;
        offset = open.offset;
        remember(key, id);
      }
    }
    if (!id) {
      const r = await api("POST", "/api/v1/uploads", { folder, name: item.name, size: file.size, scope });
      if (r.status === 409 && r.data && r.data.suggestion) {
        if (!(await askKeepBoth(view, item.name, r.data.suggestion))) return "skipped";
        item.name = r.data.suggestion;
        view.title.textContent = `${item.name} (${sizeText(file.size)})`;
        return upload(item);
      }
      if (r.status !== 201) throw new Error(errorText(r, "The upload couldn't start."));
      id = r.data.upload_id;
      chunk = r.data.chunk_bytes;
      remember(key, id);
    }
    item.id = id;
    if (listed.has(id)) {
      listed.get(id).li.remove();
      listed.delete(id);
    }
    const url = `/api/v1/uploads/${id}`;
    let failures = 0;
    view.bar.value = offset;
    while (offset < file.size) {
      if (item.cancelled) return "cancelled";
      view.state.textContent = `${percent(offset, file.size)}%`;
      const end = Math.min(offset + chunk, file.size);
      let r = null;
      try {
        r = await api("PUT", url, file.slice(offset, end), { "Upload-Offset": String(offset) });
      } catch {
        r = null; // the connection dropped
      }
      if (r && r.status === 200) {
        offset = r.data.offset;
        failures = 0;
        view.bar.value = offset;
        continue;
      }
      if (r && r.status === 409 && r.data && typeof r.data.offset === "number") {
        offset = r.data.offset;
        continue;
      }
      if (r && r.status < 500) {
        if (r.status === 404) forget(key);
        throw new Error(errorText(r, "The upload was refused."));
      }
      failures += 1;
      if (failures > MAX_FAILURES) {
        throw new Error("The connection keeps failing. Choose the file again later to resume.");
      }
      const wait = Math.min(30, 2 ** Math.min(failures, 5));
      view.state.textContent = `Connection lost at ${percent(offset, file.size)}%; trying again in ${wait} s…`;
      await sleep(wait * 1000);
      try {
        const s = await api("GET", url);
        if (s.status === 200) offset = s.data.offset;
      } catch {
        /* still offline; the next try waits longer */
      }
    }
    view.state.textContent = "Finishing…";
    let r = await api("POST", `${url}/finish`, {});
    if (r.status === 409 && r.data && r.data.suggestion) {
      if (!(await askKeepBoth(view, item.name, r.data.suggestion))) {
        await api("DELETE", url);
        forget(key);
        return "skipped";
      }
      r = await api("POST", `${url}/finish`, { name: r.data.suggestion });
    }
    if (r.status !== 201) throw new Error(errorText(r, "The upload couldn't be finished."));
    forget(key);
    return "done";
  }

  async function run() {
    if (running) return;
    running = true;
    while (queue.length) {
      const item = queue.shift();
      item.view.actions.replaceChildren();
      const cancel = button(item.view, "Cancel", async () => {
        item.cancelled = true;
        cancel.disabled = true;
        if (item.id) {
          await api("DELETE", `/api/v1/uploads/${item.id}`);
          forget(keyOf(item.file, item.name));
        }
        item.view.state.textContent = "Cancelled.";
      });
      try {
        const outcome = await upload(item);
        item.view.actions.replaceChildren();
        if (outcome === "done") {
          doneCount += 1;
          item.view.bar.value = item.view.bar.max;
          item.view.state.textContent = "Uploaded.";
          item.view.li.classList.add("done");
        } else if (outcome === "skipped") {
          item.view.state.textContent = "Skipped.";
        }
      } catch (error) {
        failedCount += 1;
        item.view.actions.replaceChildren();
        item.view.state.textContent = error.message;
        item.view.li.classList.add("failed");
      }
    }
    running = false;
    if (doneCount && !failedCount) {
      window.location.reload();
    } else if (doneCount) {
      const p = document.createElement("p");
      const a = document.createElement("a");
      a.href = window.location.href;
      a.textContent = "Refresh the folder to see the new files";
      p.append(a);
      list.after(p);
    }
  }

  function add(files) {
    for (const file of files) {
      queue.push({ file, name: file.name, view: row(file.name, file.size), cancelled: false });
    }
    run();
  }

  input.addEventListener("change", () => {
    add(Array.from(input.files || []));
    input.value = "";
  });
  zone.addEventListener("dragover", (event) => {
    event.preventDefault();
    zone.classList.add("over");
  });
  zone.addEventListener("dragleave", () => zone.classList.remove("over"));
  zone.addEventListener("drop", (event) => {
    event.preventDefault();
    zone.classList.remove("over");
    const items = Array.from(event.dataTransfer.items || []);
    const folders = items.some((i) => i.webkitGetAsEntry && i.webkitGetAsEntry()?.isDirectory);
    if (folders) {
      const note = document.createElement("p");
      note.className = "warning";
      note.textContent = "Folders can't be uploaded yet; drop the files inside them instead.";
      list.before(note);
    }
    add(Array.from(event.dataTransfer.files || []).filter((f, i) => !(items[i]?.webkitGetAsEntry?.()?.isDirectory)));
  });
  window.addEventListener("beforeunload", (event) => {
    if (running) {
      event.preventDefault();
      event.returnValue = "";
    }
  });

  // Unfinished uploads into this folder, from this or another browser.
  api("GET", `/api/v1/uploads?scope=${encodeURIComponent(scope)}`)
    .then((r) => {
      if (r.status !== 200) return;
      for (const u of r.data.uploads.filter((x) => x.folder === folder)) {
        const view = row(u.name, u.size);
        listed.set(u.upload_id, view);
        view.bar.value = u.offset;
        view.state.textContent = `${percent(u.offset, u.size)}% uploaded. Choose the file again to resume.`;
        button(view, "Cancel", async () => {
          await api("DELETE", `/api/v1/uploads/${u.upload_id}`);
          view.li.remove();
          listed.delete(u.upload_id);
        });
      }
    })
    .catch(() => {});
})();
