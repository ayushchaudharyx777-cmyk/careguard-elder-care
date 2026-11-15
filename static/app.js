/* CareGuard Accessible Vanilla JavaScript Application */

(function () {
  "use strict";

  const appRoot = document.getElementById("app-root");
  const userBar = document.getElementById("user-bar");
  const statusRegion = document.getElementById("status-region");

  const state = {
    token: sessionStorage.getItem("careguard_token") || null,
    me: null,
    activeTab: "today",
  };

  function announce(message) {
    if (statusRegion) {
      statusRegion.textContent = message;
    }
  }

  function clearElement(element) {
    while (element.firstChild) {
      element.removeChild(element.firstChild);
    }
  }

  async function api(path, options = {}) {
    const headers = options.headers || {};
    if (state.token) {
      headers["Authorization"] = "Bearer " + state.token;
    }
    if (options.body && typeof options.body === "object") {
      headers["Content-Type"] = "application/json";
      options.body = JSON.stringify(options.body);
    }
    options.headers = headers;

    const res = await fetch(path, options);
    if (res.status === 401) {
      sessionStorage.removeItem("careguard_token");
      state.token = null;
      state.me = null;
      initApp();
      throw new Error("Session expired. Please sign in again.");
    }
    const contentType = res.headers.get("content-type");
    let data = null;
    if (contentType && contentType.includes("application/json")) {
      data = await res.json();
    }
    if (!res.ok) {
      const detail = data && data.detail ? data.detail : "Request failed with status " + res.status;
      throw new Error(detail);
    }
    return data;
  }

  async function initApp() {
    clearElement(appRoot);
    clearElement(userBar);

    try {
      if (state.token) {
        const [setupStatus, meData] = await Promise.all([
          api("/api/setup/status"),
          api("/api/me"),
        ]);
        if (!setupStatus.setup_complete) {
          renderSetupView();
          return;
        }
        state.me = meData;
        renderAppShell();
      } else {
        const setupStatus = await api("/api/setup/status");
        if (!setupStatus.setup_complete) {
          renderSetupView();
          return;
        }
        renderLoginView();
      }
    } catch (err) {
      if (!state.token) {
        renderLoginView(err.message);
      } else {
        sessionStorage.removeItem("careguard_token");
        state.token = null;
        renderLoginView("Session invalid: " + err.message);
      }
    }
  }

  function renderSetupView(errorMessage = "") {
    clearElement(appRoot);

    const card = document.createElement("section");
    card.className = "card";

    const title = document.createElement("h2");
    title.textContent = "First-Run Setup";
    card.appendChild(title);

    const desc = document.createElement("p");
    desc.textContent = "Welcome to CareGuard! Please configure the patient and the primary caregiver account.";
    card.appendChild(desc);

    if (errorMessage) {
      const alert = document.createElement("div");
      alert.className = "alert-box alert-error";
      alert.textContent = errorMessage;
      card.appendChild(alert);
    }

    const form = document.createElement("form");

    function addInput(id, labelText, type = "text", val = "", placeholder = "", required = true) {
      const group = document.createElement("div");
      group.className = "form-group";

      const label = document.createElement("label");
      label.setAttribute("for", id);
      label.textContent = labelText;
      group.appendChild(label);

      const input = document.createElement("input");
      input.id = id;
      input.type = type;
      input.value = val;
      input.placeholder = placeholder;
      input.required = required;
      group.appendChild(input);

      form.appendChild(group);
      return input;
    }

    const patientNameIn = addInput("setup_p_name", "Patient Full Name", "text", "", "e.g. Eleanor Vance");
    const patientTzIn = addInput("setup_p_tz", "Patient Time Zone", "text", "Asia/Kolkata", "e.g. Asia/Kolkata or UTC");
    const caregiverNameIn = addInput("setup_cg_name", "Caregiver Full Name", "text", "", "e.g. Sarah Vance");
    const caregiverEmailIn = addInput("setup_cg_email", "Caregiver Email", "email", "", "caregiver@example.com");
    const caregiverPasscodeIn = addInput("setup_cg_pass", "Caregiver Passcode", "password", "", "Enter secure passcode");

    const btn = document.createElement("button");
    btn.type = "submit";
    btn.className = "btn btn-primary";
    btn.textContent = "Complete Setup & Sign In";
    form.appendChild(btn);

    form.addEventListener("submit", async function (e) {
      e.preventDefault();
      try {
        announce("Submitting setup...");
        const payload = {
          patient_name: patientNameIn.value.trim(),
          patient_timezone: patientTzIn.value.trim(),
          caregiver_name: caregiverNameIn.value.trim(),
          caregiver_email: caregiverEmailIn.value.trim(),
          caregiver_passcode: caregiverPasscodeIn.value,
        };
        const res = await api("/api/setup", {
          method: "POST",
          body: payload,
        });
        if (res.token) {
          sessionStorage.setItem("careguard_token", res.token);
          state.token = res.token;
          announce("Setup completed successfully.");
          initApp();
        }
      } catch (err) {
        announce("Setup failed: " + err.message);
        renderSetupView(err.message);
      }
    });

    card.appendChild(form);
    appRoot.appendChild(card);
  }

  function renderLoginView(errorMessage = "") {
    clearElement(appRoot);

    const card = document.createElement("section");
    card.className = "card";

    const title = document.createElement("h2");
    title.textContent = "Sign In to CareGuard";
    card.appendChild(title);

    if (errorMessage) {
      const alert = document.createElement("div");
      alert.className = "alert-box alert-error";
      alert.textContent = errorMessage;
      card.appendChild(alert);
    }

    const form = document.createElement("form");

    const group1 = document.createElement("div");
    group1.className = "form-group";
    const label1 = document.createElement("label");
    label1.setAttribute("for", "login_email");
    label1.textContent = "Email Address";
    const input1 = document.createElement("input");
    input1.id = "login_email";
    input1.type = "email";
    input1.required = true;
    group1.appendChild(label1);
    group1.appendChild(input1);
    form.appendChild(group1);

    const group2 = document.createElement("div");
    group2.className = "form-group";
    const label2 = document.createElement("label");
    label2.setAttribute("for", "login_passcode");
    label2.textContent = "Passcode";
    const input2 = document.createElement("input");
    input2.id = "login_passcode";
    input2.type = "password";
    input2.required = true;
    group2.appendChild(label2);
    group2.appendChild(input2);
    form.appendChild(group2);

    const btn = document.createElement("button");
    btn.type = "submit";
    btn.className = "btn btn-primary";
    btn.textContent = "Sign In";
    form.appendChild(btn);

    form.addEventListener("submit", async function (e) {
      e.preventDefault();
      try {
        announce("Signing in...");
        const res = await api("/api/auth/login", {
          method: "POST",
          body: {
            email: input1.value.trim(),
            passcode: input2.value,
          },
        });
        if (res.token) {
          sessionStorage.setItem("careguard_token", res.token);
          state.token = res.token;
          announce("Signed in successfully.");
          initApp();
        }
      } catch (err) {
        announce("Login failed: " + err.message);
        renderLoginView(err.message);
      }
    });

    card.appendChild(form);
    appRoot.appendChild(card);
  }

  function renderAppShell() {
    clearElement(userBar);

    const userSpan = document.createElement("span");
    const roleText = state.me.member.role ? state.me.member.role.toUpperCase() : "MEMBER";
    userSpan.textContent = state.me.member.name + " (" + roleText + ") | Patient: " + (state.me.patient.name || "Patient");
    userBar.appendChild(userSpan);

    const logoutBtn = document.createElement("button");
    logoutBtn.className = "btn btn-secondary";
    logoutBtn.textContent = "Sign Out";
    logoutBtn.addEventListener("click", async function () {
      try {
        await api("/api/auth/logout", { method: "POST" });
      } catch (e) {
        // ignore error on logout
      }
      sessionStorage.removeItem("careguard_token");
      state.token = null;
      state.me = null;
      announce("Signed out.");
      initApp();
    });
    userBar.appendChild(logoutBtn);

    clearElement(appRoot);

    const nav = document.createElement("nav");
    nav.className = "tabs-nav";
    nav.setAttribute("aria-label", "Care navigation tabs");

    const tabs = [
      { id: "today", label: "Today" },
      { id: "medicines", label: "Medicines" },
      { id: "appointments", label: "Appointments" },
      { id: "family", label: "Family" },
      { id: "summary", label: "Summary" },
    ];

    const tabButtons = {};
    const contentArea = document.createElement("div");
    contentArea.id = "tab-content-area";

    tabs.forEach(function (tab) {
      const btn = document.createElement("button");
      btn.className = "tab-btn";
      btn.textContent = tab.label;
      btn.id = "tab-btn-" + tab.id;
      btn.setAttribute("aria-pressed", state.activeTab === tab.id ? "true" : "false");

      btn.addEventListener("click", function () {
        state.activeTab = tab.id;
        tabs.forEach(function (t) {
          tabButtons[t.id].setAttribute("aria-pressed", t.id === tab.id ? "true" : "false");
        });
        announce("Switched to " + tab.label + " tab");
        renderCurrentTab(contentArea);
      });

      tabButtons[tab.id] = btn;
      nav.appendChild(btn);
    });

    appRoot.appendChild(nav);
    appRoot.appendChild(contentArea);

    renderCurrentTab(contentArea);
  }

  function renderCurrentTab(container) {
    clearElement(container);
    if (state.activeTab === "today") {
      renderTodayTab(container);
    } else if (state.activeTab === "medicines") {
      renderMedicinesTab(container);
    } else if (state.activeTab === "appointments") {
      renderAppointmentsTab(container);
    } else if (state.activeTab === "family") {
      renderFamilyTab(container);
    } else if (state.activeTab === "summary") {
      renderSummaryTab(container);
    }
  }

  /* TODAY TAB */
  async function renderTodayTab(container) {
    const header = document.createElement("h2");
    header.textContent = "Today's Schedule & Dose Logs";
    container.appendChild(header);

    const loadingP = document.createElement("p");
    loadingP.textContent = "Loading doses...";
    container.appendChild(loadingP);

    try {
      const doses = await api("/api/doses?days=2");
      container.removeChild(loadingP);

      if (doses.length === 0) {
        const emptyP = document.createElement("p");
        emptyP.textContent = "No scheduled doses recorded for today or recent days.";
        container.appendChild(emptyP);
        return;
      }

      const list = document.createElement("ul");
      list.className = "item-list";

      doses.forEach(function (dose) {
        const li = document.createElement("li");
        li.className = "item-card";

        const info = document.createElement("div");
        info.className = "item-info";

        const title = document.createElement("h3");
        title.className = "item-title";
        title.textContent = dose.medication_name + " (" + dose.dosage + ")";
        info.appendChild(title);

        const timeP = document.createElement("p");
        timeP.className = "item-meta";
        timeP.textContent = "Scheduled (UTC): " + dose.scheduled_for;
        info.appendChild(timeP);

        if (dose.instructions) {
          const instP = document.createElement("p");
          instP.className = "item-meta";
          instP.textContent = "Instructions: " + dose.instructions;
          info.appendChild(instP);
        }

        const badge = document.createElement("span");
        badge.className = "status-badge status-" + dose.status;
        badge.textContent = dose.status;
        info.appendChild(badge);

        li.appendChild(info);

        const actions = document.createElement("div");
        actions.className = "item-actions";

        if (dose.status === "pending") {
          const takenBtn = document.createElement("button");
          takenBtn.className = "btn btn-success";
          takenBtn.textContent = "Mark Taken";
          takenBtn.addEventListener("click", async function () {
            try {
              announce("Marking dose taken...");
              await api("/api/doses/" + dose.id, {
                method: "PATCH",
                body: { status: "taken" },
              });
              announce("Dose marked as taken.");
              renderTodayTab(container);
            } catch (err) {
              announce("Error updating dose: " + err.message);
              alert(err.message);
            }
          });
          actions.appendChild(takenBtn);

          const skipBtn = document.createElement("button");
          skipBtn.className = "btn btn-secondary";
          skipBtn.textContent = "Skip";
          skipBtn.addEventListener("click", async function () {
            try {
              announce("Skipping dose...");
              await api("/api/doses/" + dose.id, {
                method: "PATCH",
                body: { status: "skipped" },
              });
              announce("Dose marked as skipped.");
              renderTodayTab(container);
            } catch (err) {
              announce("Error updating dose: " + err.message);
              alert(err.message);
            }
          });
          actions.appendChild(skipBtn);
        }

        li.appendChild(actions);
        list.appendChild(li);
      });

      container.appendChild(list);
    } catch (err) {
      if (container.contains(loadingP)) {
        container.removeChild(loadingP);
      }
      const errP = document.createElement("p");
      errP.className = "alert-box alert-error";
      errP.textContent = "Failed to load doses: " + err.message;
      container.appendChild(errP);
    }
  }

  /* MEDICINES TAB */
  async function renderMedicinesTab(container) {
    const card = document.createElement("section");
    card.className = "card";

    const h2 = document.createElement("h2");
    h2.textContent = "Add New Medication";
    card.appendChild(h2);

    const form = document.createElement("form");

    function createField(id, labelText, type = "text", val = "", placeholder = "", required = true) {
      const g = document.createElement("div");
      g.className = "form-group";
      const l = document.createElement("label");
      l.setAttribute("for", id);
      l.textContent = labelText;
      const inp = document.createElement("input");
      inp.id = id;
      inp.type = type;
      inp.value = val;
      inp.placeholder = placeholder;
      inp.required = required;
      g.appendChild(l);
      g.appendChild(inp);
      form.appendChild(g);
      return inp;
    }

    const nameIn = createField("med_name", "Medicine Name", "text", "", "e.g. Metformin");
    const dosageIn = createField("med_dosage", "Dosage", "text", "", "e.g. 500mg");
    const instructionsIn = createField("med_inst", "Instructions", "text", "", "e.g. Take with dinner", false);
    const timesIn = createField("med_times", "Times (comma-separated 24-hr HH:MM)", "text", "08:00, 20:00", "08:00, 20:00");

    const todayStr = new Date().toISOString().split("T")[0];
    const startIn = createField("med_start", "Start Date", "date", todayStr);
    const endIn = createField("med_end", "End Date (Optional)", "date", "", "", false);

    const checkG = document.createElement("div");
    checkG.className = "form-group checkbox-group";
    const checkIn = document.createElement("input");
    checkIn.id = "med_active";
    checkIn.type = "checkbox";
    checkIn.checked = true;
    const checkL = document.createElement("label");
    checkL.setAttribute("for", "med_active");
    checkL.textContent = "Active Medication";
    checkG.appendChild(checkIn);
    checkG.appendChild(checkL);
    form.appendChild(checkG);

    const addBtn = document.createElement("button");
    addBtn.type = "submit";
    addBtn.className = "btn btn-primary";
    addBtn.textContent = "Add Medicine";
    form.appendChild(addBtn);

    form.addEventListener("submit", async function (e) {
      e.preventDefault();
      const rawTimes = timesIn.value
        .split(",")
        .map(function (s) { return s.trim(); })
        .filter(Boolean);

      try {
        announce("Adding medication...");
        await api("/api/medications", {
          method: "POST",
          body: {
            name: nameIn.value.trim(),
            dosage: dosageIn.value.trim(),
            instructions: instructionsIn.value.trim(),
            times: rawTimes,
            start_date: startIn.value,
            end_date: endIn.value || null,
            active: checkIn.checked,
          },
        });
        announce("Medication added successfully.");
        renderMedicinesTab(container);
      } catch (err) {
        announce("Error: " + err.message);
        alert(err.message);
      }
    });

    card.appendChild(form);
    container.appendChild(card);

    const listH2 = document.createElement("h2");
    listH2.textContent = "Current Medications";
    container.appendChild(listH2);

    try {
      const meds = await api("/api/medications");
      if (meds.length === 0) {
        const emptyP = document.createElement("p");
        emptyP.textContent = "No medications configured yet.";
        container.appendChild(emptyP);
        return;
      }

      const list = document.createElement("ul");
      list.className = "item-list";

      meds.forEach(function (med) {
        const li = document.createElement("li");
        li.className = "item-card";

        const info = document.createElement("div");
        info.className = "item-info";

        const title = document.createElement("h3");
        title.className = "item-title";
        title.textContent = med.name + " (" + med.dosage + ")";
        info.appendChild(title);

        const timesP = document.createElement("p");
        timesP.className = "item-meta";
        timesP.textContent = "Daily Times: " + med.times.join(", ");
        info.appendChild(timesP);

        const dateP = document.createElement("p");
        dateP.className = "item-meta";
        dateP.textContent = "Dates: " + med.start_date + (med.end_date ? " to " + med.end_date : " (ongoing)");
        info.appendChild(dateP);

        if (med.instructions) {
          const instP = document.createElement("p");
          instP.className = "item-meta";
          instP.textContent = "Instructions: " + med.instructions;
          info.appendChild(instP);
        }

        const statusSpan = document.createElement("span");
        statusSpan.className = "status-badge " + (med.active ? "status-taken" : "status-skipped");
        statusSpan.textContent = med.active ? "ACTIVE" : "INACTIVE";
        info.appendChild(statusSpan);

        li.appendChild(info);

        const actions = document.createElement("div");
        actions.className = "item-actions";

        const delBtn = document.createElement("button");
        delBtn.className = "btn btn-danger";
        delBtn.textContent = "Delete";
        delBtn.addEventListener("click", async function () {
          if (confirm("Delete " + med.name + " and all its dose history?")) {
            try {
              announce("Deleting medication...");
              await api("/api/medications/" + med.id, { method: "DELETE" });
              announce("Medication deleted.");
              renderMedicinesTab(container);
            } catch (err) {
              announce("Error: " + err.message);
              alert(err.message);
            }
          }
        });
        actions.appendChild(delBtn);

        li.appendChild(actions);
        list.appendChild(li);
      });

      container.appendChild(list);
    } catch (err) {
      const errP = document.createElement("p");
      errP.className = "alert-box alert-error";
      errP.textContent = "Failed to load medications: " + err.message;
      container.appendChild(errP);
    }
  }

  /* APPOINTMENTS TAB */
  async function renderAppointmentsTab(container) {
    const card = document.createElement("section");
    card.className = "card";

    const h2 = document.createElement("h2");
    h2.textContent = "Schedule Doctor Appointment";
    card.appendChild(h2);

    const form = document.createElement("form");

    function createField(id, labelText, type = "text", val = "", placeholder = "", required = true) {
      const g = document.createElement("div");
      g.className = "form-group";
      const l = document.createElement("label");
      l.setAttribute("for", id);
      l.textContent = labelText;
      const inp = document.createElement("input");
      inp.id = id;
      inp.type = type;
      inp.value = val;
      inp.placeholder = placeholder;
      inp.required = required;
      g.appendChild(l);
      g.appendChild(inp);
      form.appendChild(g);
      return inp;
    }

    const docNameIn = createField("appt_doc", "Doctor Name", "text", "", "e.g. Dr. Jane Smith");
    const specIn = createField("appt_spec", "Specialty", "text", "", "e.g. Cardiologist", false);
    const locIn = createField("appt_loc", "Location", "text", "", "e.g. City Health Clinic, Room 302", false);
    const startsIn = createField("appt_start", "Date & Time", "datetime-local", "");
    const notesIn = createField("appt_notes", "Notes", "text", "", "e.g. Bring recent blood test results", false);

    const btn = document.createElement("button");
    btn.type = "submit";
    btn.className = "btn btn-primary";
    btn.textContent = "Schedule Appointment";
    form.appendChild(btn);

    form.addEventListener("submit", async function (e) {
      e.preventDefault();
      try {
        announce("Scheduling appointment...");
        await api("/api/appointments", {
          method: "POST",
          body: {
            doctor_name: docNameIn.value.trim(),
            specialty: specIn.value.trim(),
            location: locIn.value.trim(),
            notes: notesIn.value.trim(),
            starts_at: startsIn.value,
          },
        });
        announce("Appointment scheduled successfully.");
        renderAppointmentsTab(container);
      } catch (err) {
        announce("Error: " + err.message);
        alert(err.message);
      }
    });

    card.appendChild(form);
    container.appendChild(card);

    const listH2 = document.createElement("h2");
    listH2.textContent = "Upcoming & Past Appointments";
    container.appendChild(listH2);

    try {
      const appts = await api("/api/appointments");
      if (appts.length === 0) {
        const emptyP = document.createElement("p");
        emptyP.textContent = "No appointments scheduled.";
        container.appendChild(emptyP);
        return;
      }

      const list = document.createElement("ul");
      list.className = "item-list";

      appts.forEach(function (appt) {
        const li = document.createElement("li");
        li.className = "item-card";

        const info = document.createElement("div");
        info.className = "item-info";

        const title = document.createElement("h3");
        title.className = "item-title";
        title.textContent = appt.doctor_name + (appt.specialty ? " - " + appt.specialty : "");
        info.appendChild(title);

        const timeP = document.createElement("p");
        timeP.className = "item-meta";
        timeP.textContent = "Scheduled (UTC): " + appt.starts_at;
        info.appendChild(timeP);

        if (appt.location) {
          const locP = document.createElement("p");
          locP.className = "item-meta";
          locP.textContent = "Location: " + appt.location;
          info.appendChild(locP);
        }

        if (appt.notes) {
          const notesP = document.createElement("p");
          notesP.className = "item-meta";
          notesP.textContent = "Notes: " + appt.notes;
          info.appendChild(notesP);
        }

        const reminderBadge = document.createElement("span");
        reminderBadge.className = "status-badge " + (appt.reminder_sent ? "status-taken" : "status-pending");
        reminderBadge.textContent = appt.reminder_sent ? "REMINDER SENT" : "REMINDER PENDING";
        info.appendChild(reminderBadge);

        li.appendChild(info);

        const actions = document.createElement("div");
        actions.className = "item-actions";

        const delBtn = document.createElement("button");
        delBtn.className = "btn btn-danger";
        delBtn.textContent = "Cancel";
        delBtn.addEventListener("click", async function () {
          if (confirm("Cancel this appointment?")) {
            try {
              announce("Cancelling appointment...");
              await api("/api/appointments/" + appt.id, { method: "DELETE" });
              announce("Appointment cancelled.");
              renderAppointmentsTab(container);
            } catch (err) {
              announce("Error: " + err.message);
              alert(err.message);
            }
          }
        });
        actions.appendChild(delBtn);

        li.appendChild(actions);
        list.appendChild(li);
      });

      container.appendChild(list);
    } catch (err) {
      const errP = document.createElement("p");
      errP.className = "alert-box alert-error";
      errP.textContent = "Failed to load appointments: " + err.message;
      container.appendChild(errP);
    }
  }

  /* FAMILY TAB */
  async function renderFamilyTab(container) {
    const isCaregiver = state.me.member.role === "caregiver";

    if (isCaregiver) {
      const card = document.createElement("section");
      card.className = "card";

      const h2 = document.createElement("h2");
      h2.textContent = "Add Care Circle Member";
      card.appendChild(h2);

      const form = document.createElement("form");

      function createField(id, labelText, type = "text", val = "", placeholder = "", required = true) {
        const g = document.createElement("div");
        g.className = "form-group";
        const l = document.createElement("label");
        l.setAttribute("for", id);
        l.textContent = labelText;
        const inp = document.createElement("input");
        inp.id = id;
        inp.type = type;
        inp.value = val;
        inp.placeholder = placeholder;
        inp.required = required;
        g.appendChild(l);
        g.appendChild(inp);
        form.appendChild(g);
        return inp;
      }

      const nameIn = createField("mem_name", "Full Name", "text", "", "e.g. John Doe");
      const emailIn = createField("mem_email", "Email Address", "email", "", "john@example.com");
      const passIn = createField("mem_pass", "Initial Passcode", "password", "", "Enter passcode");

      const roleG = document.createElement("div");
      roleG.className = "form-group";
      const roleL = document.createElement("label");
      roleL.setAttribute("for", "mem_role");
      roleL.textContent = "Care Role";
      const roleSelect = document.createElement("select");
      roleSelect.id = "mem_role";

      const opt1 = document.createElement("option");
      opt1.value = "family";
      opt1.textContent = "Family Member";
      roleSelect.appendChild(opt1);

      const opt2 = document.createElement("option");
      opt2.value = "caregiver";
      opt2.textContent = "Caregiver";
      roleSelect.appendChild(opt2);

      roleG.appendChild(roleL);
      roleG.appendChild(roleSelect);
      form.appendChild(roleG);

      const btn = document.createElement("button");
      btn.type = "submit";
      btn.className = "btn btn-primary";
      btn.textContent = "Add Member";
      form.appendChild(btn);

      form.addEventListener("submit", async function (e) {
        e.preventDefault();
        try {
          announce("Adding member...");
          await api("/api/members", {
            method: "POST",
            body: {
              name: nameIn.value.trim(),
              email: emailIn.value.trim(),
              passcode: passIn.value,
              role: roleSelect.value,
            },
          });
          announce("Member added successfully.");
          renderFamilyTab(container);
        } catch (err) {
          announce("Error: " + err.message);
          alert(err.message);
        }
      });

      card.appendChild(form);
      container.appendChild(card);
    }

    const listH2 = document.createElement("h2");
    listH2.textContent = "Care Circle Members";
    container.appendChild(listH2);

    try {
      const members = await api("/api/members");
      const list = document.createElement("ul");
      list.className = "item-list";

      members.forEach(function (mem) {
        const li = document.createElement("li");
        li.className = "item-card";

        const info = document.createElement("div");
        info.className = "item-info";

        const title = document.createElement("h3");
        title.className = "item-title";
        title.textContent = mem.name + (mem.id === state.me.member.id ? " (You)" : "");
        info.appendChild(title);

        const emailP = document.createElement("p");
        emailP.className = "item-meta";
        emailP.textContent = "Email: " + mem.email;
        info.appendChild(emailP);

        const roleBadge = document.createElement("span");
        roleBadge.className = "status-badge status-pending";
        roleBadge.textContent = mem.role.toUpperCase();
        info.appendChild(roleBadge);

        li.appendChild(info);

        const actions = document.createElement("div");
        actions.className = "item-actions";

        if (isCaregiver && mem.id !== state.me.member.id) {
          const remBtn = document.createElement("button");
          remBtn.className = "btn btn-danger";
          remBtn.textContent = "Remove";
          remBtn.addEventListener("click", async function () {
            if (confirm("Remove " + mem.name + " from the care circle?")) {
              try {
                announce("Removing member...");
                await api("/api/members/" + mem.id, { method: "DELETE" });
                announce("Member removed.");
                renderFamilyTab(container);
              } catch (err) {
                announce("Error: " + err.message);
                alert(err.message);
              }
            }
          });
          actions.appendChild(remBtn);
        }

        li.appendChild(actions);
        list.appendChild(li);
      });

      container.appendChild(list);
    } catch (err) {
      const errP = document.createElement("p");
      errP.className = "alert-box alert-error";
      errP.textContent = "Failed to load members: " + err.message;
      container.appendChild(errP);
    }
  }

  /* SUMMARY TAB */
  async function renderSummaryTab(container) {
    const h2 = document.createElement("h2");
    h2.textContent = "7-Day Medication Adherence Summary";
    container.appendChild(h2);

    try {
      const stats = await api("/api/adherence?days=7");

      const card = document.createElement("section");
      card.className = "card";

      const adhHeading = document.createElement("h3");
      adhHeading.textContent = "Adherence Rate (Last 7 Days)";
      card.appendChild(adhHeading);

      const rateDisplay = document.createElement("div");
      rateDisplay.className = "stat-number";
      if (stats.adherence !== null && stats.adherence !== undefined) {
        rateDisplay.textContent = stats.adherence + "%";
      } else {
        rateDisplay.textContent = "No Doses Due";
      }
      card.appendChild(rateDisplay);

      const grid = document.createElement("div");
      grid.className = "stat-grid";

      function addStat(label, num) {
        const item = document.createElement("div");
        item.className = "stat-card";
        const l = document.createElement("div");
        l.textContent = label;
        const n = document.createElement("div");
        n.className = "stat-number";
        n.textContent = String(num);
        item.appendChild(l);
        item.appendChild(n);
        grid.appendChild(item);
      }

      addStat("Doses Taken", stats.taken);
      addStat("Doses Skipped", stats.skipped);
      addStat("Doses Missed", stats.missed);
      addStat("Total Doses Due", stats.total_due);

      card.appendChild(grid);
      container.appendChild(card);
    } catch (err) {
      const errP = document.createElement("p");
      errP.className = "alert-box alert-error";
      errP.textContent = "Failed to load adherence summary: " + err.message;
      container.appendChild(errP);
    }
  }

  // Initialize
  window.addEventListener("DOMContentLoaded", initApp);
})();

