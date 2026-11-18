// @ts-check

/** @type import("../../../../frontend/src/extensions").ExtensionModule */
export default {
  onExtensionPageLoad(ctx) {
    // Page settings (see the Settings card at the bottom). Neither
    // setting persists across page reloads.
    const settings = {
      // Open file links in a new tab: on by default on desktop, off
      // on touch devices.
      newTab: !window.matchMedia("(pointer: coarse)").matches,
      // Allow multiple cards to be expanded at once: off by default,
      // so expanding one card collapses the others.
      multi: false,
    };
    const newTabBox = document.getElementById("br-set-newtab");
    if (newTabBox instanceof HTMLInputElement) {
      newTabBox.checked = settings.newTab;
      newTabBox.addEventListener("change", () => {
        settings.newTab = newTabBox.checked;
      });
    }
    const multiBox = document.getElementById("br-set-multi");
    if (multiBox instanceof HTMLInputElement) {
      multiBox.checked = settings.multi;
      multiBox.addEventListener("change", () => {
        settings.multi = multiBox.checked;
      });
    }

    // Fava's frontend intercepts link clicks for its router, so open
    // document links explicitly: in a new tab or the current tab,
    // per the setting above.
    for (const link of document.querySelectorAll(".br-doclink")) {
      link.addEventListener(
        "click",
        (event) => {
          event.preventDefault();
          event.stopPropagation();
          if (link instanceof HTMLAnchorElement) {
            if (settings.newTab) {
              window.open(link.href, "_blank", "noopener");
            } else {
              window.location.href = link.href;
            }
          }
        },
        true,
      );
    }

    const progress = document.getElementById("br-progress");
    if (!progress) return;
    const branch = progress.dataset.branch ?? "";
    const cards = [...document.querySelectorAll(".br-card[data-key]")];

    // The status lives on the card element itself; the button label
    // and the header color are rendered from it by refresh().
    const getStatus = (card) => card.dataset.status ?? "none";

    const refresh = () => {
      let done = 0;
      for (const card of cards) {
        const status = getStatus(card);
        if (status === "approved") done += 1;
        card.classList.toggle("br-approved", status === "approved");
        card.classList.toggle("br-needs", status === "needs-work");
        for (const btn of card.querySelectorAll(".br-state-btn")) {
          btn.classList.toggle("br-active", btn.dataset.state === status);
        }
      }
      progress.textContent =
        cards.length === 0
          ? ""
          : done === cards.length
            ? ` - ✓ All ${cards.length} approved`
            : ` - ${done} of ${cards.length} approved`;
    };

    // iOS Safari has no textarea resize handle, so grow the comment
    // box to fit its content as the user types (desktop keeps the
    // manual resize handle as well).
    const autogrow = (ta) => {
      ta.style.height = "auto";
      ta.style.height = `${ta.scrollHeight + 2}px`;
    };

    const save = (card) => {
      const key = card.dataset.key ?? "";
      const ta = card.querySelector(".br-comment");
      void ctx.api.post("save", {
        branch,
        key,
        status: getStatus(card),
        comment: ta instanceof HTMLTextAreaElement ? ta.value : "",
        collapsed: card.classList.contains("br-collapsed"),
      });
      refresh();
    };

    const setCollapsed = (card, collapsed) => {
      if (!collapsed && !settings.multi && cards.includes(card)) {
        for (const other of cards) {
          if (other !== card) setCollapsed(other, true);
        }
      }
      card.classList.toggle("br-collapsed", collapsed);
      if (!collapsed) {
        // A textarea that was hidden (display: none) when its height
        // was last measured has a stale, too-small height - re-grow it
        // now that it is visible again.
        const ta = card.querySelector(".br-comment");
        if (ta instanceof HTMLTextAreaElement) autogrow(ta);
        // Lazy-load this card's documents on first expansion.
        for (const el of card.querySelectorAll("[data-src]")) {
          if (el instanceof HTMLElement && !el.getAttribute("src")) {
            el.setAttribute("src", el.dataset.src ?? "");
          }
        }
      }
    };

    for (const card of cards) {
      const ta = card.querySelector(".br-comment");
      const head = card.querySelector(".br-head");
      // Picking a state sets it and collapses the card.
      for (const btn of card.querySelectorAll(".br-state-btn")) {
        btn.addEventListener("click", () => {
          card.dataset.status = btn.dataset.state ?? "none";
          setCollapsed(card, true);
          save(card);
        });
      }
      let timer = 0;
      ta?.addEventListener("input", () => {
        if (ta instanceof HTMLTextAreaElement) autogrow(ta);
        window.clearTimeout(timer);
        timer = window.setTimeout(() => save(card), 500);
      });
      head?.addEventListener("click", (event) => {
        // Clicks on the controls themselves (checkbox, label, comment
        // box) act on those controls; anything else in the header
        // toggles the card.
        if (
          event.target instanceof Element &&
          event.target.closest(".br-controls, a")
        ) {
          return;
        }
        setCollapsed(card, !card.classList.contains("br-collapsed"));
        save(card);
      });
    }

    // The Settings card collapses like the transaction cards, but
    // is not one of them: no review state, no accordion.
    const settingsCard = document.querySelector(".br-settings");
    if (settingsCard instanceof HTMLElement) {
      settingsCard
        .querySelector(".br-head")
        ?.addEventListener("click", () => {
          setCollapsed(
            settingsCard,
            !settingsCard.classList.contains("br-collapsed"),
          );
        });
    }

    // Cards are rendered collapsed by the server, so there is no
    // expand-then-collapse shuffle on page load.

    void ctx.api.get("state", { branch }).then((state) => {
      if (state && typeof state === "object") {
        for (const card of cards) {
          const s = state[card.dataset.key ?? ""];
          if (!s) continue;
          const ta = card.querySelector(".br-comment");
          // Older state files stored a boolean "approved" instead.
          card.dataset.status =
            s.status ?? (s.approved ? "approved" : "none");
          if (ta instanceof HTMLTextAreaElement) {
            ta.value = s.comment ?? "";
            autogrow(ta);
          }
        }
      }
      refresh();
    });

    refresh();
  },
};
