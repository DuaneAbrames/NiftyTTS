// ==UserScript==
// @name         NiftyTTS Link Injector
// @namespace    https://duane.example/niftytts
// @version      1.0.0
// @description  Add "TTS" links after story links on nifty.org to send them to your local NiftyTTS app
// @match        http*://nifty.org/*
// @match        http*://www.nifty.org/*
// @run-at       document-idle
// @grant        none
// ==/UserScript==

(function () {
  "use strict";
//update this to match your environment.  If running locally, it will probably be http://localhost:8000/
  const APP_BASE = "http://niftytts.dragon.local:7230/?u="; // FastAPI app expects ?u=<URL>
  const processed = new WeakSet();

  function isEligibleAnchor(a) {
    // Must have href and not be inside a <nav>
    if (!a || !a.href || a.closest("nav")) return false;

    try {
      const u = new URL(a.href, location.href);

      // Only http(s)
      if (!(u.protocol === "http:" || u.protocol === "https:")) return false;

      // Skip hash-only, mailto, javascript, tel
      if (u.href.startsWith("javascript:")) return false;
      if (a.getAttribute("href")?.trim().startsWith("#")) return false;
      const scheme = a.getAttribute("href")?.split(":")[0]?.toLowerCase();
      if (scheme === "mailto" || scheme === "tel") return false;

      // Skip paths that end with a slash (directory links)
      if (u.pathname.endsWith("/")) return false;

      // Already injected?
      if (a.nextSibling && a.nextSibling.classList?.contains("nifty-tts-link")) return false;

      return true;
    } catch {
      return false;
    }
  }

  function makeTTSLink(targetUrl) {
    const link = document.createElement("a");
    link.textContent = "TTS";
    link.className = "nifty-tts-link";
    link.href = APP_BASE + encodeURIComponent(targetUrl);
    link.target = "_blank";
    link.rel = "noopener";
    link.style.marginLeft = "0.5em";
    link.style.padding = "0.05em 0.35em";
    link.style.border = "1px solid currentColor";
    link.style.borderRadius = "0.35em";
    link.style.textDecoration = "none";
    link.style.font = "inherit";
    link.title = "Send to local NiftyTTS";
    return link;
  }

  function processAnchor(a) {
    if (processed.has(a)) return;
    if (!isEligibleAnchor(a)) {
      processed.add(a);
      return;
    }
    const abs = new URL(a.href, location.href).href;

    // Insert a thin spacer if not already whitespace
    const prev = a.nextSibling;
    const spacerNeeded = !(prev && prev.nodeType === Node.TEXT_NODE && /\s/.test(prev.nodeValue || ""));
    if (spacerNeeded) a.parentNode.insertBefore(document.createTextNode(" "), a.nextSibling);

    a.parentNode.insertBefore(makeTTSLink(abs), a.nextSibling?.nextSibling || a.nextSibling);
    processed.add(a);
  }

  function scanAll() {
    document.querySelectorAll("a[href]").forEach(processAnchor);
  }

  // Initial pass
  scanAll();

  // Handle dynamically added content
  const obs = new MutationObserver((mutations) => {
    for (const m of mutations) {
      for (const node of m.addedNodes) {
        if (node.nodeType !== 1) continue;
        if (node.matches?.("a[href]")) processAnchor(node);
        node.querySelectorAll?.("a[href]").forEach(processAnchor);
      }
    }
  });
  obs.observe(document.documentElement, { childList: true, subtree: true });
})();
