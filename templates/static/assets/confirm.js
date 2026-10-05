// Sign-in link landing page. The token arrives in the URL fragment (never sent to servers).
// Opening the page only checks the link; signing in takes a press of "Sign in as …", so email
// link scanners that open links automatically can't use up the one-time token.
"use strict";

(async function () {
  const $ = (id) => document.getElementById(id);
  const params = new URLSearchParams(location.hash.slice(1));
  const token = params.get("token") || "";
  const next = params.get("next") === "/" ? "/" : "/alerts";
  // Drop the token from the address bar and history right away.
  history.replaceState(null, "", location.pathname);

  // An expired or used link goes straight to the email form, so a new one is one step away.
  function expired() {
    location.replace("/alerts?expired=1");
  }

  async function verify(confirm) {
    const res = await fetch("/api/auth/verify", {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ token, confirm, next }),
    });
    let data = {};
    try { data = await res.json(); } catch (e) { /* empty body */ }
    if (!res.ok) throw Object.assign(new Error(data.error || "Something went wrong. Please try again."), { status: res.status });
    return data;
  }

  function fail(err) {
    if (err.status === 400) return expired();
    $("status").textContent = err.message;
    $("confirm").hidden = true;
  }

  if (!/^[A-Za-z0-9_-]{43}$/.test(token)) return expired();
  try {
    const peek = await verify(false);
    $("status").textContent = "Signing in to PouchIt.";
    $("confirm").textContent = "Sign in as " + peek.email;
    $("confirm").hidden = false;
  } catch (err) {
    return fail(err);
  }

  $("confirm").addEventListener("click", async () => {
    $("confirm").disabled = true;
    try {
      const out = await verify(true);
      location.replace(out.next === "/" ? "/" : "/alerts");
    } catch (err) {
      fail(err);
    }
  });
})();
