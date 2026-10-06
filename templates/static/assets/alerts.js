// My alerts page: sign-in form, or settings for the signed-in user.
// Builds all dynamic content with textContent, never HTML strings.
"use strict";

function $(id) { return document.getElementById(id); }

async function api(method, path, body) {
  const opts = { method, credentials: "same-origin", headers: {} };
  if (body !== undefined) {
    opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(body);
  }
  const res = await fetch(path, opts);
  let data = {};
  try { data = await res.json(); } catch (e) { /* empty body */ }
  if (!res.ok) throw new Error(data.error || "Something went wrong. Please try again.");
  return data;
}

function say(el, text, kind) {
  el.textContent = text;
  el.className = "msg" + (kind ? " " + kind : "");
}

// ---- Sign in ---------------------------------------------------------------

const RESEND_WAIT = 30;

function showSignin() {
  $("inbox").hidden = true;
  $("signin").hidden = false;
  if (window.turnstile) window.turnstile.reset();
}

function showInbox(email) {
  $("signin").hidden = true;
  $("inbox").hidden = false;
  $("inbox-email").textContent = email;
  $("code").value = "";
  say($("code-msg"), "");
  $("code").focus();
  startResendTimer();
}

let resendTimer = null;
function startResendTimer() {
  const btn = $("resend");
  let left = RESEND_WAIT;
  btn.disabled = true;
  btn.textContent = "Resend (" + left + ")";
  clearInterval(resendTimer);
  resendTimer = setInterval(() => {
    left -= 1;
    if (left > 0) {
      btn.textContent = "Resend (" + left + ")";
    } else {
      clearInterval(resendTimer);
      btn.textContent = "Resend";
      btn.disabled = false;
    }
  }, 1000);
}

// The API's error message, plus whether the request is dead and the user must start over.
async function post(path, body) {
  const res = await fetch(path, {
    method: "POST",
    credentials: "same-origin",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  let data = {};
  try { data = await res.json(); } catch (e) { /* empty body */ }
  return { ok: res.ok, data };
}

function setupSignin(follow) {
  const form = $("signin-form");
  const msg = $("signin-msg");
  const btn = $("signin-btn");
  if (follow) {
    $("follow-note").textContent = "You'll get alerts for " + follow + ".";
    $("follow-note").hidden = false;
  }
  if (new URLSearchParams(location.search).get("expired") === "1") {
    $("expired").hidden = false;
    history.replaceState(null, "", location.pathname);
  }
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const email = $("email").value.trim();
    if (!/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(email)) return say(msg, "Enter a valid email address.", "err");
    const tokenInput = form.querySelector('[name="cf-turnstile-response"]');
    const turnstileToken = tokenInput ? tokenInput.value : "";
    if (!turnstileToken) return say(msg, "Please complete the check above, then try again.", "err");
    btn.disabled = true;
    try {
      const out = await api("POST", "/api/auth/request", follow ? { email, turnstileToken, next: "/alerts", follow } : { email, turnstileToken, next: "/alerts" });
      say(msg, "");
      $("expired").hidden = true;
      showInbox(email);
    } catch (err) {
      say(msg, err.message, "err");
    } finally {
      btn.disabled = false;
      if (window.turnstile) window.turnstile.reset();
    }
  });

  $("change-email").addEventListener("click", (e) => {
    e.preventDefault();
    clearInterval(resendTimer);
    showSignin();
    $("email").focus();
  });

  const codeForm = $("code-form");
  const codeMsg = $("code-msg");
  let busy = false;
  async function submitCode() {
    const code = $("code").value;
    if (!/^[0-9]{6}$/.test(code)) return say(codeMsg, "Enter the 6-digit code from the email.", "err");
    if (busy) return;
    busy = true;
    $("code-btn").disabled = true;
    try {
      const out = await post("/api/auth/code", { code, next: "/alerts" });
      if (out.ok) return location.replace(out.data.next === "/" ? "/" : "/alerts");
      say(codeMsg, out.data.error || "Something went wrong. Please try again.", "err");
      $("code").value = "";
      if (out.data.restart) {
        clearInterval(resendTimer);
        $("resend").disabled = true;
      }
    } catch (err) {
      say(codeMsg, "Something went wrong. Please try again.", "err");
    } finally {
      busy = false;
      $("code-btn").disabled = false;
    }
  }
  // Submits by itself once the sixth digit is in (typed or pasted).
  $("code").addEventListener("input", () => {
    const digits = $("code").value.replace(/[^0-9]/g, "").slice(0, 6);
    if (digits !== $("code").value) $("code").value = digits;
    if (digits.length === 6) submitCode();
  });
  codeForm.addEventListener("submit", (e) => {
    e.preventDefault();
    submitCode();
  });

  $("resend").addEventListener("click", async () => {
    $("resend").disabled = true;
    const out = await post("/api/auth/resend", { next: "/alerts" }).catch(() => ({ ok: false, data: {} }));
    if (out.ok) {
      say(codeMsg, "Sent a new code and link. The old ones no longer work.", "ok");
      $("code").value = "";
      startResendTimer();
    } else {
      say(codeMsg, out.data.error || "Something went wrong. Please try again.", "err");
      if (!out.data.restart) startResendTimer();
    }
  });
}

// ---- Settings ----------------------------------------------------------------

function renderFollows(follows) {
  const list = $("follows");
  list.textContent = "";
  for (const f of follows) {
    const li = document.createElement("li");
    const name = document.createElement("span");
    name.textContent = f.artist;
    const remove = document.createElement("button");
    remove.type = "button";
    remove.textContent = "×";
    remove.setAttribute("aria-label", "Unfollow " + f.artist);
    remove.addEventListener("click", async () => {
      remove.disabled = true;
      try {
        await api("DELETE", "/api/follows/" + encodeURIComponent(String(f.id)));
        li.remove();
        $("no-follows").hidden = list.children.length > 0;
      } catch (err) {
        say($("follow-msg"), err.message, "err");
        remove.disabled = false;
      }
    });
    li.append(name, remove);
    list.append(li);
  }
  $("no-follows").hidden = follows.length > 0;
  $("suggestions").hidden = follows.length > 0 || !$("suggested").children.length;
}

// Six artists with presales coming up, highest edge first, for someone who follows nobody yet.
function renderSuggestions(feed) {
  const now = Date.now();
  const best = new Map();
  for (const e of feed.events || []) {
    if (typeof e.artist !== "string" || (e.presale_end && Date.parse(e.presale_end) <= now)) continue;
    const edge = typeof e.edge === "number" ? e.edge : -Infinity;
    if (!best.has(e.artist) || edge > best.get(e.artist)) best.set(e.artist, edge);
  }
  const picks = [...best.entries()].sort((a, b) => b[1] - a[1]).slice(0, 6).map((x) => x[0]);
  const list = $("suggested");
  list.textContent = "";
  for (const name of picks) {
    const li = document.createElement("li");
    const label = document.createElement("span");
    label.textContent = name;
    const add = document.createElement("button");
    add.type = "button";
    add.className = "secondary";
    add.textContent = "Follow";
    add.setAttribute("aria-label", "Follow " + name);
    add.addEventListener("click", async () => {
      add.disabled = true;
      try {
        await api("POST", "/api/follows", { artist: name });
        const out = await api("GET", "/api/follows");
        renderFollows(out.follows);
        say($("follow-msg"), "Following " + name + ".", "ok");
      } catch (err) {
        say($("follow-msg"), err.message, "err");
        add.disabled = false;
      }
    });
    li.append(label, add);
    list.append(li);
  }
}

async function loadArtistSuggestions() {
  try {
    const res = await fetch("/alerts.json", { credentials: "omit" });
    if (!res.ok) return;
    const feed = await res.json();
    renderSuggestions(feed);
    $("suggestions").hidden = $("follows").children.length > 0 || !$("suggested").children.length;
    const names = [...new Set((feed.events || []).map((e) => e.artist).filter((a) => typeof a === "string"))].sort((a, b) => a.localeCompare(b));
    const dl = $("artist-list");
    for (const name of names) {
      const opt = document.createElement("option");
      opt.value = name;
      dl.append(opt);
    }
  } catch (e) { /* suggestions are optional */ }
}

async function followFromCard(follow) {
  try {
    await api("POST", "/api/follows", { artist: follow });
    const out = await api("GET", "/api/follows");
    renderFollows(out.follows);
    say($("follow-msg"), "Following " + follow + ".", "ok");
  } catch (err) {
    say($("follow-msg"), err.message, "err");
  }
}

function setupSettings(me) {
  $("me-email").textContent = me.email;
  $("follow-alerts").checked = me.preferences.followAlerts;
  $("profit-alerts").checked = me.preferences.profitAlerts;
  $("threshold").value = String(me.preferences.profitThreshold);
  renderFollows(me.follows);
  loadArtistSuggestions();

  $("prefs-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const msg = $("prefs-msg");
    const threshold = Number($("threshold").value);
    if (!Number.isInteger(threshold) || threshold < 0 || threshold > 500) return say(msg, "Enter a whole number from 0 to 500.", "err");
    try {
      await api("PUT", "/api/preferences", { followAlerts: $("follow-alerts").checked, profitAlerts: $("profit-alerts").checked, profitThreshold: threshold });
      say(msg, "Saved.", "ok");
    } catch (err) {
      say(msg, err.message, "err");
    }
  });

  $("follow-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const msg = $("follow-msg");
    const artist = $("artist").value.trim();
    if (!artist) return say(msg, "Enter an artist name.", "err");
    try {
      await api("POST", "/api/follows", { artist });
      $("artist").value = "";
      const out = await api("GET", "/api/follows");
      renderFollows(out.follows);
      say(msg, "Following " + artist + ".", "ok");
    } catch (err) {
      say(msg, err.message, "err");
    }
  });

  $("signout").addEventListener("click", async () => {
    try { await api("POST", "/api/auth/logout", {}); } catch (e) { /* signed out either way */ }
    location.replace("/");
  });

  $("delete").addEventListener("click", async () => {
    if (!confirm("Delete your account? This removes your email address, settings, and follows. It can't be undone.")) return;
    try {
      await api("DELETE", "/api/me");
      location.replace("/");
    } catch (err) {
      say($("account-msg"), err.message, "err");
    }
  });
}

(async function init() {
  // ?follow=<artist> comes from an event card's "Alert me" button.
  const params = new URLSearchParams(location.search);
  const follow = (params.get("follow") || "").trim().slice(0, 100) || null;
  if (params.has("follow")) {
    params.delete("follow");
    history.replaceState(null, "", location.pathname + (params.toString() ? "?" + params : ""));
  }
  let me = { signedIn: false };
  try { me = await api("GET", "/api/me"); } catch (e) { /* show sign-in */ }
  $("loading").hidden = true;
  if (me.signedIn) {
    $("settings").hidden = false;
    setupSettings(me);
    if (follow) followFromCard(follow);
  } else {
    $("signin").hidden = false;
    setupSignin(follow);
  }
})();
