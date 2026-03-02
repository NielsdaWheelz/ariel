from __future__ import annotations


PHONE_SURFACE_HTML = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Ariel Chat</title>
  <style>
    :root { color-scheme: dark; }
    body {
      margin: 0;
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      background: #0f1115;
      color: #e6edf3;
    }
    main { max-width: 760px; margin: 0 auto; padding: 16px; }
    h1 { font-size: 1.1rem; margin: 0 0 12px; }
    #timeline {
      border: 1px solid #30363d;
      border-radius: 10px;
      padding: 12px;
      min-height: 140px;
      margin-bottom: 12px;
      background: #161b22;
    }
    .turn {
      margin-bottom: 10px;
      border-bottom: 1px solid #30363d;
      padding-bottom: 8px;
    }
    .turn:last-child { border-bottom: none; margin-bottom: 0; }
    .meta { color: #8b949e; font-size: 0.8rem; margin-bottom: 4px; }
    .action { font-size: 0.85rem; margin-left: 8px; color: #d2a8ff; }
    .event { font-size: 0.85rem; margin-left: 8px; color: #c9d1d9; }
    form { display: flex; gap: 8px; }
    input {
      flex: 1;
      font-size: 16px;
      border: 1px solid #30363d;
      border-radius: 8px;
      padding: 10px;
      background: #0d1117;
      color: #e6edf3;
    }
    button {
      border: none;
      border-radius: 8px;
      padding: 10px 14px;
      background: #2f81f7;
      color: #fff;
      font-weight: 600;
      font-size: 0.95rem;
    }
    #status { margin: 8px 0; min-height: 18px; color: #8b949e; font-size: 0.85rem; }
  </style>
</head>
<body>
  <main>
    <h1>ariel chat (slice 0)</h1>
    <section id="timeline"></section>
    <div id="status"></div>
    <form id="chat-form">
      <input id="message" name="message" autocomplete="off" placeholder="type a message" required />
      <button type="submit">send</button>
    </form>
  </main>
  <script>
    let sessionId = null;

    const timelineNode = document.getElementById("timeline");
    const statusNode = document.getElementById("status");
    const formNode = document.getElementById("chat-form");
    const messageNode = document.getElementById("message");

    function setStatus(text) {
      statusNode.textContent = text;
    }

    function escapeHtml(value) {
      return String(value ?? "")
        .replaceAll("&", "&amp;")
        .replaceAll("<", "&lt;")
        .replaceAll(">", "&gt;");
    }

    function formatUsage(usage) {
      if (!usage || typeof usage !== "object") return "";
      const fields = [
        ["prompt", usage.prompt_tokens],
        ["completion", usage.completion_tokens],
        ["total", usage.total_tokens],
      ];
      const tokenParts = fields
        .filter(([, value]) => Number.isFinite(Number(value)))
        .map(([label, value]) => `${label}=${value}`);
      return tokenParts.length ? `tokens(${tokenParts.join(", ")})` : "";
    }

    function formatEventDetails(event) {
      const payload = (event && typeof event.payload === "object" && event.payload !== null)
        ? event.payload
        : {};
      const parts = [];
      if (payload.provider) parts.push(`provider=${payload.provider}`);
      if (payload.model) parts.push(`model=${payload.model}`);
      if (typeof payload.duration_ms === "number") parts.push(`duration_ms=${payload.duration_ms}`);
      const usage = formatUsage(payload.usage);
      if (usage) parts.push(usage);
      if (payload.failure_reason) parts.push(`failure_reason=${payload.failure_reason}`);
      return parts.join(" | ");
    }

    function formatActionAttemptDetails(actionAttempt) {
      const parts = [];
      parts.push(`capability=${actionAttempt.capability_id}`);
      parts.push(`status=${actionAttempt.status}`);
      if (actionAttempt.policy_decision) parts.push(`policy=${actionAttempt.policy_decision}`);
      if (actionAttempt.policy_reason) parts.push(`reason=${actionAttempt.policy_reason}`);
      if (actionAttempt.approval && actionAttempt.approval.status) {
        parts.push(`approval=${actionAttempt.approval.status}`);
      }
      if (actionAttempt.execution && actionAttempt.execution.status) {
        parts.push(`execution=${actionAttempt.execution.status}`);
      }
      return parts.join(" | ");
    }

    function renderTimeline(turns) {
      if (!turns.length) {
        timelineNode.innerHTML = "<p>no turns yet.</p>";
        return;
      }
      timelineNode.innerHTML = turns.map((turn) => {
        const actionAttempts = (Array.isArray(turn.action_attempts) ? turn.action_attempts : [])
          .map((actionAttempt) => {
            const detailText = formatActionAttemptDetails(actionAttempt);
            return `<div class="action">action[${escapeHtml(actionAttempt.proposal_index)}] ${escapeHtml(detailText)}</div>`;
          })
          .join("");
        const events = turn.events
          .map((event) => {
            const detailText = formatEventDetails(event);
            const suffix = detailText ? ` - ${escapeHtml(detailText)}` : "";
            return `<div class="event">[${escapeHtml(event.sequence)}] ${escapeHtml(event.event_type)}${suffix}</div>`;
          })
          .join("");
        return `
          <article class="turn">
            <div class="meta">${escapeHtml(turn.id)} · ${escapeHtml(turn.status)}</div>
            <div><strong>user:</strong> ${escapeHtml(turn.user_message)}</div>
            <div><strong>assistant:</strong> ${escapeHtml(turn.assistant_message || "(none)")}</div>
            ${actionAttempts}
            ${events}
          </article>
        `;
      }).join("");
    }

    async function loadTimeline() {
      const response = await fetch(`/v1/sessions/${sessionId}/events`);
      const data = await response.json();
      if (!response.ok || !data.ok) {
        setStatus(data?.error?.message || "timeline load failed");
        return;
      }
      renderTimeline(data.turns);
    }

    async function ensureSession() {
      const response = await fetch("/v1/sessions/active");
      const data = await response.json();
      if (!response.ok || !data.ok) {
        throw new Error("session bootstrap failed");
      }
      sessionId = data.session.id;
    }

    async function sendMessage(text) {
      const response = await fetch(`/v1/sessions/${sessionId}/message`, {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ message: text }),
      });
      const data = await response.json();
      if (!response.ok || !data.ok) {
        throw new Error(data?.error?.message || "send failed");
      }
    }

    formNode.addEventListener("submit", async (event) => {
      event.preventDefault();
      const text = messageNode.value.trim();
      if (!text) return;
      messageNode.value = "";
      setStatus("sending...");
      try {
        await sendMessage(text);
        await loadTimeline();
        setStatus("ok");
      } catch (error) {
        setStatus(error.message);
      }
    });

    (async () => {
      try {
        await ensureSession();
        await loadTimeline();
        setStatus("ready");
      } catch (error) {
        setStatus(error.message);
      }
    })();
  </script>
</body>
</html>
"""
