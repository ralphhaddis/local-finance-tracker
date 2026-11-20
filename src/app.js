(() => {
  const storageKey = "local-finance-tracker.pending-categories.v1";
  const readPending = () => {
    try { return JSON.parse(sessionStorage.getItem(storageKey) || "{}"); }
    catch (_) { return {}; }
  };
  const writePending = (pending) => {
    try { sessionStorage.setItem(storageKey, JSON.stringify(pending)); }
    catch (_) { /* The save action still works when browser storage is unavailable. */ }
  };
  const encodeControls = (form, extra = {}) => {
    const pairs = [];
    form.querySelectorAll("input[name], select[name], textarea[name]").forEach((control) => {
      if (control.disabled || !control.name) return;
      if (control instanceof HTMLSelectElement && control.multiple) {
        Array.from(control.selectedOptions).forEach((option) => pairs.push([control.name, option.value]));
      } else if (control.type !== "submit" && control.type !== "button") {
        pairs.push([control.name, control.value]);
      }
    });
    Object.keys(extra).forEach((key) => pairs.push([key, extra[key]]));
    return pairs.map(([key, value]) => `${encodeURIComponent(key)}=${encodeURIComponent(value)}`).join("&");
  };

  document.querySelectorAll("[data-auto-dismiss]").forEach((notice) => {
    window.setTimeout(() => {
      notice.classList.add("notice-dismissed");
      window.setTimeout(() => notice.remove(), 350);
    }, 6000);
  });

  document.querySelectorAll(".filters input[name='q']").forEach((input) => {
    input.addEventListener("keydown", (event) => {
      if (event.key !== "Enter") return;
      event.preventDefault();
      input.form?.requestSubmit();
    });
  });

  document.querySelectorAll("[data-category-details]").forEach((details) => {
    details.addEventListener("toggle", async () => {
      if (!details.open) return;
      const container = details.querySelector("[data-category-transactions]");
      if (!container || container.dataset.loaded === "true" || container.dataset.loading === "true") return;
      container.dataset.loading = "true";
      container.innerHTML = "<small>Loading transactions…</small>";
      const query = `category_id=${encodeURIComponent(details.dataset.categoryId)}&period=${encodeURIComponent(details.dataset.period)}&date=${encodeURIComponent(details.dataset.date)}`;
      try {
        const response = await fetch(`/category-transactions?${query}`, { headers: { "Accept": "text/html" } });
        const fragment = await response.text();
        if (!response.ok) throw new Error("Could not load category transactions");
        container.innerHTML = fragment;
        container.dataset.loaded = "true";
      } catch (error) {
        container.textContent = error.message;
      } finally {
        delete container.dataset.loading;
      }
    });
  });

  let pending = readPending();
  const applyCategoryColor = (select) => {
    const color = select.selectedOptions[0]?.dataset.color;
    if (color) select.style.setProperty("--category-select-color", color);
    else select.style.removeProperty("--category-select-color");
  };
  document.querySelectorAll("[data-category-select]").forEach((select) => {
    const id = select.closest("form")?.dataset.transactionId;
    if (!id) return;
    if (pending[id] && Array.from(select.options).some((option) => option.value === pending[id])) {
      select.value = pending[id];
    }
    select.addEventListener("change", () => {
      pending[id] = select.value;
      writePending(pending);
    });
  });
  document.querySelectorAll(".group-bulk-change").forEach((form) => {
    form.addEventListener("click", (event) => event.stopPropagation());
    form.addEventListener("change", (event) => {
      event.stopPropagation();
      if (event.target instanceof HTMLSelectElement) form.dataset.groupCategoryEdited = "true";
    });
  });

  const selectedGroupCategory = (form) => {
    const select = form.querySelector("select[name='category_id']");
    return select?.value || form.dataset.predictedCategory ||
      form.querySelector("[data-group-action='accept']")?.dataset.predictedCategory || "";
  };
  document.querySelectorAll("[data-group-row-form]").forEach((form) => {
    const select = form.querySelector("select[name='category_id']");
    if (select && !select.value) select.value = selectedGroupCategory(form);
  });

  document.querySelectorAll("[data-error-category]").forEach((select) => {
    const id = `error:${select.dataset.errorCategory}`;
    if (pending[id] && Array.from(select.options).some((option) => option.value === pending[id])) {
      select.value = pending[id];
    }
    select.addEventListener("change", () => {
      pending[id] = select.value;
      writePending(pending);
    });
  });

  document.querySelectorAll("select.category-select").forEach((select) => {
    applyCategoryColor(select);
    select.addEventListener("change", () => applyCategoryColor(select));
  });

  document.querySelectorAll("form[action='/reclassify']").forEach((form) => {
    form.addEventListener("submit", () => {
      const button = form.querySelector("button[type='submit'], button:not([type])");
      if (!button) return;
      button.disabled = true;
      button.innerHTML = '<span class="loading-spinner" aria-hidden="true"></span> Reclassifying…';
      button.setAttribute("aria-live", "polite");
    });
  });

  const saveCategoryForm = async (form) => {
      const id = form.dataset.transactionId;
      const button = form.querySelector("button");
      const state = form.querySelector(".save-state");
      if (button) button.disabled = true;
      if (state) state.textContent = "saving…";
      try {
        const response = await fetch(form.getAttribute("action") || location.pathname, {
          method: "POST",
          body: encodeControls(form),
          headers: { "Accept": "application/json", "Content-Type": "application/x-www-form-urlencoded" }
        });
        const result = await response.json();
        if (!response.ok || !result.ok) throw new Error(result.error || "Save failed");

        delete pending[id];
        writePending(pending);
        if (location.pathname === "/review") {
          location.reload();
          return;
        } else {
          const row = form.closest("tr");
          const status = row?.querySelector(".tx-status");
          if (status) status.innerHTML = '<span class="pill">manual</span>';
          row?.classList.add("saved-row");
          if (state) state.textContent = "saved";
        }
      } catch (error) {
        if (state) state.textContent = `error: ${error.message}`;
        return false;
      } finally {
        if (button) button.disabled = false;
      }
      return true;
  };

  document.querySelectorAll("[data-category-form]").forEach((form) => {
    form.addEventListener("submit", (event) => {
      event.preventDefault();
      void saveCategoryForm(form);
    });
  });

  document.querySelectorAll("[data-save-all]").forEach((button) => {
    button.addEventListener("click", async () => {
      const forms = Array.from(document.querySelectorAll("[data-category-form]"));
      const state = document.querySelector("[data-save-all-state]");
      button.disabled = true;
      let saved = 0;
      let failed = 0;
      for (const form of forms) {
        if (await saveCategoryForm(form)) saved += 1;
        else failed += 1;
      }
      if (state) state.textContent = failed ? `saved ${saved}; ${failed} failed` : `saved ${saved}`;
      button.disabled = false;
    });
  });

  const appendRecentDecision = (decision) => {
    const body = document.querySelector("#recent-decisions");
    if (!body) return;
    body.querySelector(".no-decisions")?.remove();
    const row = document.createElement("tr");
    row.dataset.decisionRow = decision.decision_id;
    const when = new Date().toLocaleString();
    const amount = decision.amount_pence == null ? "—" : (decision.amount_pence / 100).toFixed(2);
    [when, decision.date || "—", decision.description || "", amount,
      `${decision.previous_category} → ${decision.chosen_category}`].forEach((value) => {
      const cell = document.createElement("td");
      cell.textContent = value;
      row.appendChild(cell);
    });
    const action = document.createElement("td");
    const form = document.createElement("form");
    form.method = "post";
    form.action = "/undo-error-decision";
    form.dataset.undoForm = "";
    const hidden = document.createElement("input");
    hidden.type = "hidden";
    hidden.name = "decision_id";
    hidden.value = decision.decision_id;
    const button = document.createElement("button");
    button.type = "submit";
    button.className = "button";
    button.textContent = "undo";
    form.append(hidden, button);
    action.appendChild(form);
    row.appendChild(action);
    body.prepend(row);
  };

  document.addEventListener("submit", async (event) => {
    const form = event.target;
    if (!(form instanceof HTMLFormElement)) return;
    const isErrorReview = form.matches("[data-error-review-form]");
    const isUndo = form.matches("[data-undo-form]");
    if (!isErrorReview && !isUndo) return;
    event.preventDefault();
    const button = form.querySelector("button");
    const state = form.querySelector(".save-state");
    if (button) button.disabled = true;
    if (state) state.textContent = "saving…";
    try {
      const action = form.dataset.pendingAction || event.submitter?.value || "correct";
      const extra = isErrorReview ? { action } : {};
      const response = await fetch(form.getAttribute("action") || location.pathname, {
        method: "POST",
        body: encodeControls(form, extra),
        headers: { "Accept": "application/json", "Content-Type": "application/x-www-form-urlencoded" }
      });
      const result = await response.json();
      if (!response.ok || !result.ok) throw new Error(result.error || "Save failed");
      if (isUndo) {
        form.closest("tr")?.remove();
        location.reload();
        return;
      }
      delete pending[`error:${result.example_id}`];
      writePending(pending);
      form.closest("tr")?.remove();
      const count = document.querySelector("#error-count");
      const body = document.querySelector("#error-rows");
      const remaining = body?.querySelectorAll("tr[data-error-row]").length || 0;
      if (count) count.textContent = `/${remaining}`;
      if (body && !remaining) body.innerHTML = '<tr id="no-errors"><td colspan="6" class="empty">No model errors to review</td></tr>';
      appendRecentDecision(result);
    } catch (error) {
      if (state) state.textContent = `error: ${error.message}`;
    } finally {
      if (button) button.disabled = false;
    }
  });

  document.addEventListener("click", async (event) => {
    const clicked = event.target instanceof Element ? event.target : event.target?.parentElement;
    const bulkButton = clicked?.closest("[data-group-bulk]");
    if (bulkButton) {
      event.preventDefault();
      event.stopPropagation();
      const button = bulkButton;
      const group = button.closest("[data-review-group]");
      const groupForm = group?.querySelector("[data-group-bulk-change-form]");
      const groupSelect = groupForm?.querySelector("select[name='category_id']");
      const state = groupForm?.querySelector(".save-state");
      if (groupForm?.dataset.groupCategoryEdited === "true") {
        if (!groupSelect?.value) {
          if (state) state.textContent = "choose a category first";
          return;
        }
        group?.querySelectorAll("[data-group-row-form] select[name='category_id']").forEach((select) => {
          select.value = groupSelect.value;
          applyCategoryColor(select);
          const transactionId = select.closest("[data-group-row-form]")?.dataset.transactionId;
          if (transactionId) pending[transactionId] = select.value;
        });
        writePending(pending);
        delete groupForm.dataset.groupCategoryEdited;
        return;
      }
      const rowForms = Array.from(group?.querySelectorAll("[data-group-row-form]") || []);
      if (!rowForms.length || rowForms.some((rowForm) => !selectedGroupCategory(rowForm))) {
        if (state) state.textContent = "choose a category for each transaction first";
        return;
      }
      rowForms.forEach((rowForm) => {
        const select = rowForm.querySelector("select[name='category_id']");
        if (select && !select.value) select.value = selectedGroupCategory(rowForm);
      });
      button.disabled = true;
      if (state) state.textContent = "saving…";
      try {
        const body = new URLSearchParams();
        rowForms.forEach((rowForm) => {
          body.append("transaction_id", rowForm.querySelector("input[name='transaction_id']").value);
          body.append("category_id", selectedGroupCategory(rowForm));
        });
        body.set("mode", "accept");
        const response = await fetch("/categorize-bulk", {
          method: "POST", body,
          headers: { "Accept": "application/json", "Content-Type": "application/x-www-form-urlencoded" }
        });
        const result = await response.json();
        if (!response.ok || !result.ok) throw new Error(result.error || "Could not confirm group");
        rowForms.forEach((rowForm) => delete pending[rowForm.dataset.transactionId]);
        writePending(pending);
        updateReviewCount(-result.count);
        group?.remove();
      } catch (error) {
        button.disabled = false;
        if (state) state.textContent = `error: ${error.message}`;
      }
      return;
    }

    const actionButton = clicked?.closest("[data-group-action]");
    if (!actionButton) return;
    const form = actionButton.closest("[data-group-row-form]");
    if (!form) return;
    const state = form.querySelector(".save-state");
    const select = form.querySelector("select[name='category_id']");
    const action = actionButton.dataset.groupAction;
    if (action !== "accept") return;
    const categoryId = selectedGroupCategory(form);
    if (select && !select.value && categoryId) select.value = categoryId;
    if (!categoryId) {
      if (state) state.textContent = "choose a category first";
      return;
    }
    actionButton.disabled = true;
    if (state) state.textContent = "saving…";
    try {
      const body = new URLSearchParams(new FormData(form));
      const endpoint = "/categorize";
      body.set("category_id", categoryId);
      const response = await fetch(endpoint, {
        method: "POST", body,
        headers: { "Accept": "application/json", "Content-Type": "application/x-www-form-urlencoded" }
      });
      const result = await response.json();
      if (!response.ok || !result.ok) throw new Error(result.error || "Could not save transaction");
      delete pending[form.dataset.transactionId];
      writePending(pending);
      const group = form.closest("[data-review-group]");
      const row = form.closest("tr");
      row?.remove();
      updateReviewCount(-1);
      const remaining = group?.querySelectorAll("tr[data-group-transaction]").length || 0;
      if (!remaining) group?.remove();
      else {
        const count = group.querySelector(".group-count");
        if (count) count.textContent = `${remaining} transaction${remaining === 1 ? "" : "s"}`;
      }
    } catch (error) {
      actionButton.disabled = false;
      if (state) state.textContent = `error: ${error.message}`;
    }
  });

  function updateReviewCount(change) {
    const counter = document.querySelector("#review-count[data-pending-count]");
    if (!counter) return;
    const count = Math.max(0, Number(counter.dataset.pendingCount || 0) + change);
    counter.dataset.pendingCount = String(count);
    counter.textContent = `/${count} waiting`;
  }
})();
