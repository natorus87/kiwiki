var KiwikiMotionBundle = (() => {
  // frontend/kiwiki-motion.js
  var reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  window.KiwikiMotion = {
    reducedMotion,
    poweredBy: "web-animations"
  };
  function animateNodes(selectorOrNodes, keyframes, options) {
    if (reducedMotion) return;
    const nodes = typeof selectorOrNodes === "string" ? Array.from(document.querySelectorAll(selectorOrNodes)) : Array.from(selectorOrNodes || []);
    nodes.forEach((node, index) => {
      if (!(node instanceof HTMLElement)) return;
      const delay = typeof options.delay === "function" ? options.delay(index) : options.delay || 0;
      node.animate(keyframes, Object.assign({}, options, { delay }));
    });
  }
  function stagger(step) {
    return (index) => index * step;
  }
  function revealShell() {
    animateNodes("header", { opacity: [0, 1], transform: ["translateY(-8px)", "translateY(0)"] }, { duration: 220, easing: "ease-out", fill: "both" });
    if (window.matchMedia("(min-width: 769px)").matches) {
      animateNodes(".sidebar", { opacity: [0, 1] }, { duration: 220, delay: 40, easing: "ease-out", fill: "both" });
    }
    animateNodes(".content-inner, .editor-bar, .login-card", { opacity: [0, 1], transform: ["translateY(8px)", "translateY(0)"] }, { duration: 240, delay: 70, easing: "ease-out", fill: "both" });
  }
  function revealTree(root = document) {
    if (reducedMotion) return;
    const rows = Array.from(root.querySelectorAll(".tree-row")).slice(0, 18);
    animateNodes(rows, { opacity: [0, 1], transform: ["translateX(-4px)", "translateX(0)"] }, { duration: 160, delay: stagger(14), easing: "ease-out", fill: "both" });
  }
  function revealContent(target) {
    if (reducedMotion || !target) return;
    const view = target.querySelector(".file-view, .empty-state, .hero, .markdown-content");
    if (!view) return;
    animateNodes([view], { opacity: [0, 1], transform: ["translateY(6px)", "translateY(0)"] }, { duration: 190, easing: "ease-out", fill: "both" });
  }
  function revealSearch(target) {
    if (reducedMotion || !target || target.id !== "search-results" || !target.children.length) return;
    animateNodes([target], { opacity: [0, 1], transform: ["translateY(-4px)", "translateY(0)"] }, { duration: 140, easing: "ease-out", fill: "both" });
  }
  function bindPressFeedback() {
    document.addEventListener("pointerdown", (event) => {
      if (!event.target || !event.target.closest) return;
      const control = event.target.closest(".btn, .sidebar-btn, .tree-btn, .header-link, .mobile-nav-btn, .submit-btn");
      if (!control || reducedMotion) return;
      animateNodes([control], { transform: ["scale(1)", "scale(0.98)"] }, { duration: 80, easing: "ease-out", fill: "both" });
    });
    document.addEventListener("pointerup", (event) => {
      if (!event.target || !event.target.closest) return;
      const control = event.target.closest(".btn, .sidebar-btn, .tree-btn, .header-link, .mobile-nav-btn, .submit-btn");
      if (!control || reducedMotion) return;
      animateNodes([control], { transform: ["scale(0.98)", "scale(1)"] }, { duration: 120, easing: "ease-out", fill: "both" });
    });
  }
  function watchTransientUi() {
    if (reducedMotion) return;
    const observer = new MutationObserver((records) => {
      records.forEach((record) => {
        record.addedNodes.forEach((node) => {
          if (!(node instanceof HTMLElement)) return;
          if (node.classList.contains("kw-modal-backdrop")) {
            const modal = node.querySelector(".kw-modal");
            if (modal) animateNodes([modal], { opacity: [0, 1], transform: ["translateY(8px)", "translateY(0)"] }, { duration: 180, easing: "ease-out", fill: "both" });
          }
          if (node.classList.contains("kw-toast")) {
            animateNodes([node], { opacity: [0, 1], transform: ["translateX(10px)", "translateX(0)"] }, { duration: 160, easing: "ease-out", fill: "both" });
          }
        });
      });
    });
    observer.observe(document.body, { childList: true, subtree: true });
  }
  document.addEventListener("DOMContentLoaded", () => {
    revealShell();
    revealTree();
    bindPressFeedback();
    watchTransientUi();
  });
  document.addEventListener("htmx:afterSwap", (event) => {
    revealTree(event.target);
    revealContent(event.target);
    revealSearch(event.target);
  });
})();
