// Magic-link landing page. The token arrives in the URL fragment (never sent to servers). Opening the
// page only checks the link; signing in takes a button press, so email link scanners that open links
// automatically can't use up the one-time token.
"use strict";

(async function () {
  const $ = (id) => document.getElementById(id);
  const params = new URLSearchParams(location.hash.slice(1));
  const token = params.get("token") || "";
  const next = params.get("next") === "/" ? "/" : "/alerts";
  // Drop the token from the address bar and history right away.
  history.replaceState(null, "", location.pathname);

  function fail(text) {
    $("status").textContent = text;
    $("confirm").hidden = true;
    $("retry").hidden = false;
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
    if (!res.ok) throw new Error(data.error || "Something went wrong. Please try again.");
    return data;
  }

  if (!/^[A-Za-z0-9_-]{43}$/.test(token)) return fail("This sign-in link is invalid, expired, or already used. Request a new one.");
  try {
    const peek = await verify(false);
    $("status").textContent = "Sign in as " + peek.email + "?";
    $("confirm").hidden = false;
  } catch (err) {
    return fail(err.message);
  }

  $("confirm").addEventListener("click", async () => {
    $("confirm").disabled = true;
    try {
      const out = await verify(true);
      location.replace(out.next === "/" ? "/" : "/alerts");
    } catch (err) {
      fail(err.message);
    }
  });
})();
