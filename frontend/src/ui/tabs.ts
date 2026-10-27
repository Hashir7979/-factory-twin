/** WAI-ARIA tabs: click or arrow keys switch panels; only the active tab is in the tab order. */
export function initTabs(root: HTMLElement, onChange?: (id: string) => void): (id: string) => void {
  const tabs = [...root.querySelectorAll<HTMLButtonElement>('[role="tab"]')];
  const select = (tab: HTMLButtonElement, focus: boolean) => {
    for (const t of tabs) {
      const active = t === tab;
      t.setAttribute("aria-selected", String(active));
      t.tabIndex = active ? 0 : -1;
      const panel = document.getElementById(t.getAttribute("aria-controls") ?? "");
      if (panel) panel.hidden = !active;
    }
    if (focus) tab.focus();
    onChange?.(tab.id);
  };
  tabs.forEach((tab, i) => {
    tab.addEventListener("click", () => select(tab, false));
    tab.addEventListener("keydown", (e) => {
      let next = -1;
      if (e.key === "ArrowRight") next = (i + 1) % tabs.length;
      else if (e.key === "ArrowLeft") next = (i - 1 + tabs.length) % tabs.length;
      else if (e.key === "Home") next = 0;
      else if (e.key === "End") next = tabs.length - 1;
      if (next >= 0) {
        e.preventDefault();
        select(tabs[next]!, true);
      }
    });
  });
  return (id: string) => {
    const tab = tabs.find((t) => t.id === id);
    if (tab) select(tab, false);
  };
}
