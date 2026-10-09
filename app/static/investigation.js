// Live investigation view. The server renders the full page from persisted state;
// this script follows the SSE stream and swaps in server-rendered fragments.
(() => {
  "use strict";

  const root = document.querySelector("[data-investigation]");
  if (!root) return;

  const id = root.dataset.investigation;
  const timeline = document.getElementById("timeline");
  const timelineEmpty = document.getElementById("timeline-empty");
  const streamState = document.getElementById("stream-state");
  let cursor = Number(root.dataset.lastSeq);

  const setStreamState = (text) => { streamState.textContent = text; };

  const post = (path, body) => fetch(`/api/investigations/${id}/${path}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: body ? JSON.stringify(body) : undefined,
  });

  root.addEventListener("click", async (event) => {
    const cancel = event.target.closest("[data-cancel]");
    const decision = event.target.closest("[data-decision]");
    if (!cancel && !decision) return;
    const buttons = decision ? root.querySelectorAll("[data-decision]") : [cancel];
    buttons.forEach((b) => { b.disabled = true; });
    const response = cancel
      ? await post("cancel")
      : await post("decision", { decision: decision.dataset.decision });
    if (!response.ok) {
      buttons.forEach((b) => { b.disabled = false; });
      setStreamState(`Request failed (${response.status})`);
    }
    // The stream delivers the resulting state from persistence.
  });

  if (root.dataset.terminal === "true" || !("EventSource" in window)) return;

  const source = new EventSource(`/api/investigations/${id}/events?after=${cursor}`);

  source.addEventListener("open", () => setStreamState("Live"));

  source.addEventListener("timeline", (message) => {
    const data = JSON.parse(message.data);
    if (data.seq <= cursor) return; // already rendered (refresh/reconnect overlap)
    timeline.insertAdjacentHTML("beforeend", data.html);
    timelineEmpty.hidden = true;
    cursor = data.seq;
    timeline.lastElementChild.scrollIntoView({ block: "nearest" });
  });

  source.addEventListener("state", (message) => {
    const data = JSON.parse(message.data);
    for (const [name, html] of Object.entries(data.panels)) {
      const panel = root.querySelector(`[data-panel="${name}"]`);
      if (panel) panel.innerHTML = html;
    }
    root.dataset.terminal = String(data.terminal);
  });

  source.addEventListener("end", () => {
    source.close();
    setStreamState("Final state");
  });

  source.addEventListener("error", () => {
    // EventSource retries on its own and resumes from the last event id.
    setStreamState(source.readyState === EventSource.CLOSED
      ? "Disconnected. Refresh to reload the saved state."
      : "Reconnecting…");
  });
})();
