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

  root.addEventListener("click", async (event) => {
    const button = event.target.closest("[data-cancel]");
    if (!button) return;
    button.disabled = true;
    const response = await fetch(`/api/investigations/${id}/cancel`, { method: "POST" });
    if (!response.ok) {
      button.disabled = false;
      setStreamState(`Cancel failed (${response.status})`);
    }
    // The stream delivers the CANCELLED state from persistence.
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
