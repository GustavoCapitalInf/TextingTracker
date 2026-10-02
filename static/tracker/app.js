/* Local-only UI enhancements. Authorization and assignment rules live on the server. */
(() => {
  "use strict";

  // Questions that need an answer before work continues open as soon as the
  // page loads. The same choices stay on the page if the dialog is dismissed.
  document.querySelectorAll("dialog[data-auto-open]").forEach((dialog) => {
    if (typeof dialog.showModal === "function" && !dialog.open) dialog.showModal();
  });

  // Template lists: list only the reps who belong to the chosen texting group.
  // Reps hidden by a group change are unticked; the server checks membership too.
  document.querySelectorAll("[data-rep-filter]").forEach((fieldset) => {
    const select = fieldset.closest("form")?.querySelector('select[name$="list_type"]');
    const hint = fieldset.querySelector("[data-rep-hint]");
    const options = [...fieldset.querySelectorAll("input[data-groups]")];
    if (!select) return;
    const update = () => {
      const group = select.value;
      let shown = 0;
      options.forEach((input) => {
        const match = Boolean(group) && input.dataset.groups.split(" ").includes(group);
        (input.closest(".rep-choices > div") || input.parentElement).hidden = !match;
        if (match) shown += 1;
        else input.checked = false;
      });
      if (hint) {
        hint.textContent = group ? "No active reps are on this texting list yet. Add them under Texting lists." : "Choose a texting list to see its reps.";
        hint.hidden = Boolean(group) && shown > 0;
      }
    };
    select.addEventListener("change", update);
    update();
  });

  // Templates: copy a message's exact text. Without script the button stays hidden
  // and the text can still be selected by hand.
  document.querySelectorAll("[data-copy-target]").forEach((button) => {
    const source = document.getElementById(button.dataset.copyTarget);
    if (!source || !navigator.clipboard) return;
    const label = button.textContent;
    let timer;
    button.hidden = false;
    button.addEventListener("click", async () => {
      try {
        await navigator.clipboard.writeText(source.textContent);
        button.dataset.state = "success";
        button.textContent = "Copied";
      } catch {
        button.dataset.state = "error";
        button.textContent = "Couldn’t copy. Select the text instead.";
      }
      clearTimeout(timer);
      timer = setTimeout(() => { delete button.dataset.state; button.textContent = label; }, 2500);
    });
  });

  document.querySelectorAll("[data-dropzone]").forEach((zone) => {
    const input = zone.querySelector('input[type="file"]');
    const feedback = zone.querySelector("[data-file-feedback]");
    if (!input) return;
    const showFile = () => {
      const file = input.files?.[0];
      if (file && feedback) feedback.textContent = `${file.name} · Ready to upload`;
    };
    input.addEventListener("change", showFile);
    ["dragenter", "dragover"].forEach((name) => zone.addEventListener(name, (event) => {
      event.preventDefault();
      zone.classList.add("is-dragging");
    }));
    ["dragleave", "drop"].forEach((name) => zone.addEventListener(name, (event) => {
      event.preventDefault();
      zone.classList.remove("is-dragging");
    }));
    zone.addEventListener("drop", (event) => {
      const files = event.dataTransfer?.files;
      if (!files?.length) return;
      if (files.length !== 1) {
        if (feedback) feedback.textContent = "Choose one spreadsheet at a time.";
        return;
      }
      try { input.files = files; showFile(); } catch {
        if (feedback) feedback.textContent = "Use the file picker to select your spreadsheet.";
      }
    });
  });

  document.querySelectorAll("[data-split-form]").forEach((form) => {
    const dates = form.querySelector('[name="dates"]');
    const today = form.dataset.today;
    if (!dates || !today) return;
    const toISO = (date) => `${date.getUTCFullYear()}-${String(date.getUTCMonth() + 1).padStart(2, "0")}-${String(date.getUTCDate()).padStart(2, "0")}`;
    form.querySelectorAll("[data-date-preset]").forEach((button) => {
      button.addEventListener("click", () => {
        // Use the server's company calendar, independent of browser timezone.
        const date = new Date(`${today}T12:00:00Z`);
        const targetDay = button.dataset.datePreset === "monday" ? 1 : 2;
        date.setUTCDate(date.getUTCDate() + ((targetDay - date.getUTCDay() + 7) % 7));
        const values = [];
        const count = targetDay === 1 ? 1 : 4;
        for (let index = 0; index < count; index += 1) {
          values.push(toISO(date));
          date.setUTCDate(date.getUTCDate() + 1);
        }
        dates.value = values.join("\n");
        dates.dispatchEvent(new Event("input", { bubbles: true }));
        dates.focus();
      });
    });
  });

  // Appearance: apply the choice at once and save it in the background, so the page
  // (and anything typed into it) stays put. Without script the form posts and the
  // server redirects back. If saving fails, fall back to that same plain post.
  document.querySelectorAll("form[data-theme-switch]").forEach((form) => {
    const apply = (theme) => {
      document.documentElement.dataset.theme = theme;
      document.querySelector('meta[name="color-scheme"]')?.setAttribute("content", theme === "system" ? "dark light" : theme);
      document.querySelectorAll('form[data-theme-switch] button[name="theme"]').forEach((button) => {
        button.setAttribute("aria-pressed", String(button.value === theme));
      });
    };
    form.addEventListener("submit", async (event) => {
      const theme = event.submitter?.value;
      if (!theme) return;
      event.preventDefault();
      apply(theme);
      const body = new FormData(form);
      body.set("theme", theme);
      try {
        const response = await fetch(form.action, {
          method: "POST", body, credentials: "same-origin", cache: "no-store",
          headers: { "Accept": "application/json" },
        });
        if (!response.ok) throw new Error("Not saved");
      } catch {
        const field = Object.assign(document.createElement("input"), { type: "hidden", name: "theme", value: theme });
        form.append(field);
        HTMLFormElement.prototype.submit.call(form);
      }
    });
  });

  document.querySelectorAll("form").forEach((form) => {
    if (form.method === "dialog" || form.hasAttribute("data-theme-switch")) return;
    form.addEventListener("submit", (event) => {
      if (form.dataset.submitting === "true") { event.preventDefault(); return; }
      if (form.dataset.confirm && !window.confirm(form.dataset.confirm)) {
        event.preventDefault();
        return;
      }
      // Preserve the clicked submitter's name/value for Preview versus Create.
      form.dataset.submitting = "true";
      form.setAttribute("aria-busy", "true");
      // A failed navigation must not leave the form permanently unusable.
      setTimeout(() => {
        delete form.dataset.submitting;
        form.removeAttribute("aria-busy");
      }, 15000);
    });
  });

  const sensitive = document.querySelector(".sensitive-surface");
  const overlay = document.getElementById("privacy-overlay");
  const resume = document.getElementById("privacy-resume");
  const shell = document.querySelector(".app-shell");
  let previousFocus;
  const cover = () => {
    if (!sensitive || !overlay || overlay.hidden === false) return;
    previousFocus = document.activeElement;
    // A modal dialog would sit above the cover; its choices remain on the page.
    document.querySelectorAll("dialog[open]").forEach((dialog) => dialog.close());
    document.body.classList.add("privacy-active");
    overlay.hidden = false;
    if (shell) shell.inert = true;
  };
  const uncover = () => {
    document.body.classList.remove("privacy-active");
    overlay.hidden = true;
    if (shell) shell.inert = false;
    if (previousFocus instanceof HTMLElement && document.contains(previousFocus)) previousFocus.focus();
  };
  if (sensitive && overlay && resume) {
    window.addEventListener("blur", cover);
    document.addEventListener("visibilitychange", () => {
      if (document.hidden) cover();
      else if (!overlay.hidden) resume.focus();
    });
    window.addEventListener("focus", () => { if (!overlay.hidden) resume.focus(); });
    resume.addEventListener("click", uncover);
    overlay.addEventListener("keydown", (event) => {
      if (event.key === "Tab") { event.preventDefault(); resume.focus(); }
    });
  }

  // A changed batch or unavailable server covers stale data until a fresh page
  // is authorized. This poll never retrieves phone numbers.
  document.querySelectorAll("[data-batch-monitor]").forEach((region) => {
    let checking = false;
    let expired = false;
    const expire = (reason) => {
      expired = true;
      region.classList.add("batch-expired");
      const notice = region.querySelector("[data-batch-expired]");
      const message = region.querySelector("[data-expired-reason]");
      if (notice) notice.hidden = false;
      if (message && reason) message.textContent = reason;
    };
    const check = async () => {
      if (checking || expired || document.hidden) return;
      checking = true;
      const controller = new AbortController();
      const timeout = setTimeout(() => controller.abort(), 7000);
      try {
        const response = await fetch(region.dataset.batchMonitor, {
          credentials: "same-origin", cache: "no-store", redirect: "error",
          headers: { "Accept": "application/json" }, signal: controller.signal,
        });
        if (!response.ok) throw new Error("Access unavailable");
        const state = await response.json();
        if (!state.revision || state.revision !== region.dataset.batchRevision) {
          expire(state.reason || "This batch has changed. Refresh to review its current state.");
        }
      } catch {
        expire("We couldn’t verify this batch. Refresh after your connection is restored.");
      } finally { clearTimeout(timeout); checking = false; }
    };
    setInterval(check, 10000);
    document.addEventListener("visibilitychange", () => { if (!document.hidden) check(); });
    window.addEventListener("online", check);
    window.addEventListener("offline", () => expire("Your connection is offline. Reconnect and refresh before continuing."));
  });

  window.addEventListener("pageshow", (event) => {
    if (event.persisted && sensitive) { cover(); window.location.reload(); }
    document.querySelectorAll('form[aria-busy="true"]').forEach((form) => {
      form.removeAttribute("aria-busy");
      delete form.dataset.submitting;
    });
  });
})();
