// Live investigation view. The server renders the full page from persisted state;
// this script swaps in server-rendered fragments as the persisted state changes.
// - stream mode (local): follows the SSE stream.
// - stepwise mode (serverless demo): polls /updates and, while the server reports
//   pending work, keeps one POST /advance in flight. Nothing is rendered client-side.
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
    // The stream/poll delivers the resulting state from persistence.
    if (root.dataset.mode === "stepwise") { pendingAdvance = true; kick(); }
  });

  const appendEvent = (seq, html) => {
    if (seq <= cursor) return; // already rendered (refresh/reconnect overlap)
    timeline.insertAdjacentHTML("beforeend", html);
    timelineEmpty.hidden = true;
    cursor = seq;
    timeline.lastElementChild.scrollIntoView({ block: "nearest" });
  };

  const applyPanels = (panels) => {
    for (const [name, html] of Object.entries(panels)) {
      const panel = root.querySelector(`[data-panel="${name}"]`);
      if (panel) panel.innerHTML = html;
    }
  };

  // --- stepwise (polling) mode ------------------------------------------------
  let pendingAdvance = root.dataset.advance === "true";
  let advancing = false;
  let polling = false;
  let kick = () => {};

  if (root.dataset.mode === "stepwise") {
    const pollMs = Math.max(Number(root.dataset.pollMs) || 1500, 500);

    const advance = async () => {
      if (advancing || !pendingAdvance) return;
      advancing = true;
      try {
        const response = await post("advance");
        if (response.ok) {
          const data = await response.json();
          pendingAdvance = data.pending;
        } else {
          setStreamState(`Step failed (${response.status}); retrying…`);
        }
      } catch {
        setStreamState("Connection lost; retrying…");
      } finally {
        advancing = false;
      }
    };

    const poll = async () => {
      if (polling) return;
      polling = true;
      try {
        const response = await fetch(`/api/investigations/${id}/updates?after=${cursor}`,
          { cache: "no-store" });
        if (!response.ok) throw new Error(String(response.status));
        const data = await response.json();
        data.events.forEach((e) => appendEvent(e.seq, e.html));
        applyPanels(data.panels);
        root.dataset.terminal = String(data.terminal);
        pendingAdvance = data.advance;
        if (data.terminal && !data.advance) {
          setStreamState("Final state");
        } else if (pendingAdvance) {
          setStreamState(advancing ? "Investigating…" : "Live");
        } else {
          setStreamState("Waiting for your decision");
        }
      } catch {
        setStreamState("Reconnecting…");
      } finally {
        polling = false;
      }
    };

    kick = () => { advance(); poll(); };
    const tick = () => {
      kick();
      if (root.dataset.terminal !== "true" || pendingAdvance || advancing) {
        window.setTimeout(tick, pollMs);
      }
    };
    tick();
    return;
  }

  // --- stream (SSE) mode --------------------------------------------------------
  if (root.dataset.terminal === "true" || !("EventSource" in window)) return;

  const source = new EventSource(`/api/investigations/${id}/events?after=${cursor}`);

  source.addEventListener("open", () => setStreamState("Live"));

  source.addEventListener("timeline", (message) => {
    const data = JSON.parse(message.data);
    appendEvent(data.seq, data.html);
  });

  source.addEventListener("state", (message) => {
    const data = JSON.parse(message.data);
    applyPanels(data.panels);
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
