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

function setupSignin() {
  const form = $("signin-form");
  const msg = $("signin-msg");
  const btn = $("signin-btn");
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const email = $("email").value.trim();
    if (!/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(email)) return say(msg, "Enter a valid email address.", "err");
    const tokenInput = form.querySelector('[name="cf-turnstile-response"]');
    const turnstileToken = tokenInput ? tokenInput.value : "";
    if (!turnstileToken) return say(msg, "Please complete the check above, then try again.", "err");
    btn.disabled = true;
    try {
      const out = await api("POST", "/api/auth/request", { email, turnstileToken, next: "/alerts" });
      say(msg, out.message, "ok");
    } catch (err) {
      say(msg, err.message, "err");
    } finally {
      btn.disabled = false;
      if (window.turnstile) window.turnstile.reset();
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
}

async function loadArtistSuggestions() {
  try {
    const res = await fetch("/alerts.json", { credentials: "omit" });
    if (!res.ok) return;
    const feed = await res.json();
    const names = [...new Set((feed.events || []).map((e) => e.artist).filter((a) => typeof a === "string"))].sort((a, b) => a.localeCompare(b));
    const dl = $("artist-list");
    for (const name of names) {
      const opt = document.createElement("option");
      opt.value = name;
      dl.append(opt);
    }
  } catch (e) { /* suggestions are optional */ }
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
  let me = { signedIn: false };
  try { me = await api("GET", "/api/me"); } catch (e) { /* show sign-in */ }
  $("loading").hidden = true;
  if (me.signedIn) {
    $("settings").hidden = false;
    setupSettings(me);
  } else {
    $("signin").hidden = false;
    setupSignin();
  }
})();
