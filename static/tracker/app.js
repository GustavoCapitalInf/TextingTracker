/* Local-only UI enhancements. Authorization and assignment rules live on the server. */
(() => {
  "use strict";

  // Copy batch URLs only. Phone data is never copied or stored by this script.
  document.querySelectorAll("[data-copy-link]").forEach((button) => {
    button.addEventListener("click", async () => {
      const label = [...button.childNodes].map(node => node.cloneNode(true));
      const restoreLabel = () => button.replaceChildren(...label.map(node => node.cloneNode(true)));
      const link = new URL(button.dataset.copyLink, window.location.origin).href;
      try {
        if (!navigator.clipboard?.writeText) throw new Error("Clipboard unavailable");
        await navigator.clipboard.writeText(link);
        button.textContent = "Link copied ✓";
        button.dataset.state = "success";
        setTimeout(() => { restoreLabel(); delete button.dataset.state; }, 2500);
      } catch {
        // Internal HTTP previews may not expose the Clipboard API.
        const field = document.createElement("input");
        field.value = link;
        field.readOnly = true;
        field.setAttribute("aria-label", "Batch link. Select and copy this URL.");
        button.after(field);
        field.focus();
        field.select();
        button.textContent = "Copy the link below";
        button.dataset.state = "error";
        field.addEventListener("blur", () => {
          field.remove();
          restoreLabel();
          delete button.dataset.state;
        }, { once: true });
      }
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

  document.querySelectorAll("form").forEach((form) => {
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
