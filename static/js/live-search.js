/* Live filtering for the cabinet: results narrow as you type.
 *
 * Progressive enhancement, not a dependency — with this file missing or blocked the
 * toolbar form still submits and the server renders exactly the same markup.
 *
 * It re-requests the page it is already on and lifts the #results region out of the
 * response, instead of talking to a JSON API: one rendering path on the server, so there
 * is no second definition of "a result" to keep in sync. In-flight requests are aborted,
 * so a slow early response can never overwrite a newer one.
 */
(() => {
  "use strict";

  const form = document.querySelector("form.toolbar[role=search]");
  const results = document.getElementById("results");
  if (!form || !results || !window.fetch) return;

  const DEBOUNCE_MS = 140;
  let timer = null;
  let inflight = null;

  function target() {
    const url = new URL(form.getAttribute("action") || location.pathname, location.origin);
    for (const [key, value] of new URLSearchParams(new FormData(form))) {
      url.searchParams.set(key, value);
    }
    return url;
  }

  async function refresh() {
    const url = target();
    if (inflight) inflight.abort();
    const controller = new AbortController();
    inflight = controller;
    results.setAttribute("aria-busy", "true");

    try {
      const response = await fetch(url.pathname + url.search, {
        signal: controller.signal,
        credentials: "same-origin",
        headers: { "X-Requested-With": "fetch" },
      });
      if (!response.ok) return;

      const parsed = new DOMParser().parseFromString(await response.text(), "text/html");
      const fresh = parsed.getElementById("results");
      if (!fresh) {
        // We were redirected (an expired session, say), so that page is not the cabinet.
        // Hand back to the browser rather than swapping nothing and looking frozen.
        if (response.redirected) window.location.reload();
        return;
      }

      results.replaceChildren(...fresh.childNodes);
      // Keep the address bar true to what is on screen, so a refresh or a shared link
      // lands on the same filters — without filling history for every keystroke.
      history.replaceState(null, "", url.pathname + url.search);
    } catch (error) {
      if (!error || error.name !== "AbortError") console.warn("live search:", error);
    } finally {
      if (inflight === controller) {
        inflight = null;
        results.removeAttribute("aria-busy");
      }
    }
  }

  function schedule() {
    window.clearTimeout(timer);
    timer = window.setTimeout(refresh, DEBOUNCE_MS);
  }

  form.addEventListener("input", schedule);
  form.addEventListener("change", () => {
    window.clearTimeout(timer);
    refresh();
  });
  form.addEventListener("submit", (event) => {
    event.preventDefault();
    window.clearTimeout(timer);
    refresh();
  });
})();
