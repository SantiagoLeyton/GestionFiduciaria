(function () {
  const storageKey = "pagosfiducia-theme";
  const root = document.documentElement;

  function preferredTheme() {
    return localStorage.getItem(storageKey)
      || (window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light");
  }

  function applyTheme(theme) {
    root.dataset.theme = theme;
    document.querySelectorAll("[data-theme-toggle]").forEach((button) => {
      const isDark = theme === "dark";
      button.setAttribute("aria-label", isDark ? "Activar modo claro" : "Activar modo oscuro");
      const icon = button.querySelector("[data-theme-icon]");
      if (icon) icon.textContent = isDark ? "☾" : "☀";
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
