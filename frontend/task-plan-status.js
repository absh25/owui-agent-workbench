/* Show only observed task-plan text in Open WebUI's existing status element. */
(() => {
  "use strict";
  if (window.__owuiTaskPlanStatusLoaded) return;
  window.__owuiTaskPlanStatusLoaded = true;

  const prefix = "任务计划 ·";
  const marker = "data-owui-task-plan-status";
  const markerClass = "owui-task-plan-status";
  let scheduled = false;

  function scan() {
    for (const element of document.querySelectorAll(`.line-clamp-1,[${marker}]`)) {
      const matches = element.classList.contains("line-clamp-1")
        && (element.textContent || "").startsWith(prefix);
      if (matches) {
        element.setAttribute(marker, "true");
        element.classList.add(markerClass);
      } else if (element.hasAttribute(marker)) {
        element.removeAttribute(marker);
        element.classList.remove(markerClass);
      }
    }
  }

  function scheduleScan() {
    if (scheduled) return;
    scheduled = true;
    window.requestAnimationFrame(() => {
      scheduled = false;
      scan();
    });
  }

  function start() {
    // Marker attributes are intentionally not observed: our own styling
    // changes cannot schedule another mutation scan.
    new MutationObserver(scheduleScan).observe(document.body, {
      childList: true,
      characterData: true,
      subtree: true,
    });
    scheduleScan();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", start, { once: true });
  } else {
    start();
  }
})();
