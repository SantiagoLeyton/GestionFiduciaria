(function () {
  const storageKey = "pagosfiducia-theme";
  const root = document.documentElement;

  function preferredTheme() {
    const stored = localStorage.getItem(storageKey);
    if (stored === "light" || stored === "dark") return stored;
    return window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
  }

  function applyTheme(theme) {
    root.dataset.theme = theme;
    document.querySelectorAll("[data-theme-toggle]").forEach((button) => {
      const isDark = theme === "dark";
      const nextLabel = isDark ? "Modo claro" : "Modo oscuro";
      button.setAttribute("aria-label", `Cambiar a ${nextLabel.toLowerCase()}`);
      const icon = button.querySelector("[data-theme-icon]");
      const label = button.querySelector("[data-theme-label]");
      if (icon) icon.textContent = isDark ? "☾" : "☀";
      if (label) label.textContent = nextLabel;
    });
  }

  applyTheme(preferredTheme());

  document.addEventListener("click", (event) => {
    const button = event.target.closest("[data-theme-toggle]");
    if (!button) return;
    const nextTheme = root.dataset.theme === "dark" ? "light" : "dark";
    localStorage.setItem(storageKey, nextTheme);
    applyTheme(nextTheme);
  });
})();
