// Unsubscribe page. The signed link arrives in the URL fragment. Opening the page does nothing on its
// own; the button sends the request, so link scanners can't unsubscribe anyone.
"use strict";

(function () {
  const $ = (id) => document.getElementById(id);
  const params = new URLSearchParams(location.hash.slice(1));
  const u = params.get("u") || "";
  const t = params.get("t") || "";
  history.replaceState(null, "", location.pathname);

  function say(text, kind) {
    $("msg").textContent = text;
    $("msg").className = "msg " + kind;
  }

  if (!/^[A-Za-z0-9_-]{22}$/.test(u) || !/^[A-Za-z0-9_-]{43}$/.test(t)) {
    $("confirm").hidden = true;
    say("This unsubscribe link is incomplete. Sign in to manage your alerts instead.", "err");
    return;
  }

  $("confirm").addEventListener("click", async () => {
    $("confirm").disabled = true;
    try {
      const res = await fetch("/api/unsubscribe", {
        method: "POST",
        credentials: "omit",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ u, t }),
      });
      let data = {};
      try { data = await res.json(); } catch (e) { /* empty body */ }
      if (!res.ok) throw new Error(data.error || "Something went wrong. Please try again.");
      $("confirm").hidden = true;
      $("status").textContent = "You're unsubscribed.";
      say("You won't get any more alert emails. This took effect immediately.", "ok");
    } catch (err) {
      $("confirm").disabled = false;
      say(err.message, "err");
    }
  });
})();
