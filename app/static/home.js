// Case selection: start an investigation through the API, then open its page.
(() => {
  "use strict";

  const errorLine = document.getElementById("open-error");

  document.addEventListener("click", async (event) => {
    const button = event.target.closest("[data-open-case]");
    if (!button) return;
    button.disabled = true;
    errorLine.hidden = true;
    try {
      const response = await fetch("/api/investigations", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ instance_id: button.dataset.openCase }),
      });
      if (!response.ok) throw new Error(`server returned ${response.status}`);
      const body = await response.json();
      window.location.href = `/investigations/${body.investigation_id}`;
    } catch (err) {
      errorLine.textContent = `Could not start the investigation: ${err.message}`;
      errorLine.hidden = false;
      button.disabled = false;
    }
  });
})();
