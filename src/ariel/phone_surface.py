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
    .approval-controls { margin-left: 8px; margin-top: 4px; display: flex; gap: 6px; }
    .approval-btn {
      border: 1px solid #30363d;
      border-radius: 6px;
      padding: 4px 8px;
      background: #238636;
      color: #fff;
      font-size: 0.78rem;
      font-weight: 600;
    }
    .approval-deny { background: #da3633; }
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
        .replaceAll(">", "&gt;")
        .replaceAll('"', "&quot;")
        .replaceAll("'", "&#39;");
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

    function safeJsonStringify(value) {
      try {
        return JSON.stringify(value);
      } catch {
        return "(unserializable)";
      }
    }

    function formatSurfaceLifecycleDetails(lifecycleItem) {
      const proposal = (lifecycleItem && typeof lifecycleItem.proposal === "object" && lifecycleItem.proposal !== null)
        ? lifecycleItem.proposal
        : {};
      const policy = (lifecycleItem && typeof lifecycleItem.policy === "object" && lifecycleItem.policy !== null)
        ? lifecycleItem.policy
        : {};
      const approval = (lifecycleItem && typeof lifecycleItem.approval === "object" && lifecycleItem.approval !== null)
        ? lifecycleItem.approval
        : {};
      const execution = (lifecycleItem && typeof lifecycleItem.execution === "object" && lifecycleItem.execution !== null)
        ? lifecycleItem.execution
        : {};

      const parts = [];
      if (proposal.capability_id) parts.push(`proposal=${proposal.capability_id}`);
      if (proposal.input_summary !== undefined) {
        parts.push(`input=${safeJsonStringify(proposal.input_summary)}`);
      }
      if (policy.decision) parts.push(`policy=${policy.decision}`);
      if (policy.reason) parts.push(`policy_reason=${policy.reason}`);
      if (approval.status) parts.push(`approval=${approval.status}`);
      if (approval.reference) parts.push(`approval_ref=${approval.reference}`);
      if (approval.reason) parts.push(`approval_reason=${approval.reason}`);
      if (execution.status) parts.push(`execution=${execution.status}`);
      if (execution.output !== undefined && execution.output !== null) {
        parts.push(`output=${safeJsonStringify(execution.output)}`);
      }
      if (execution.error) parts.push(`error=${execution.error}`);
      return parts.join(" | ");
    }

    function approvalReferenceFromLifecycle(lifecycleItem) {
      const approvalRef = (
        lifecycleItem &&
        lifecycleItem.approval &&
        typeof lifecycleItem.approval.reference === "string" &&
        lifecycleItem.approval.reference
      )
        ? lifecycleItem.approval.reference
        : null;
      return approvalRef;
    }

    function renderApprovalControls(lifecycleItem) {
      const approval = (lifecycleItem && typeof lifecycleItem.approval === "object" && lifecycleItem.approval !== null)
        ? lifecycleItem.approval
        : {};
      if (approval.status !== "pending") return "";
      const approvalRef = approvalReferenceFromLifecycle(lifecycleItem);
      if (!approvalRef) return "";
      return `
        <div class="approval-controls">
          <button class="approval-btn" type="button" data-approval-ref="${escapeHtml(approvalRef)}" data-decision="approve">approve</button>
          <button class="approval-btn approval-deny" type="button" data-approval-ref="${escapeHtml(approvalRef)}" data-decision="deny">deny</button>
        </div>
      `;
    }

    function renderTimeline(turns) {
      if (!turns.length) {
        timelineNode.innerHTML = "<p>no turns yet.</p>";
        return;
      }
      timelineNode.innerHTML = turns.map((turn) => {
        const actionAttempts = (Array.isArray(turn.surface_action_lifecycle) ? turn.surface_action_lifecycle : [])
          .map((lifecycleItem) => {
            const detailText = formatSurfaceLifecycleDetails(lifecycleItem);
            const controls = renderApprovalControls(lifecycleItem);
            return `
              <div class="action">action[${escapeHtml(lifecycleItem.proposal_index)}] ${escapeHtml(detailText)}</div>
              ${controls}
            `;
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

    async function submitApprovalDecision(approvalRef, decision) {
      const response = await fetch("/v1/approvals", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({
          approval_ref: approvalRef,
          decision,
        }),
      });
      const data = await response.json();
      if (!response.ok || !data.ok) {
        throw new Error(data?.error?.message || "approval update failed");
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

    timelineNode.addEventListener("click", async (event) => {
      const target = event.target;
      if (!(target instanceof HTMLElement)) return;
      const button = target.closest("button[data-approval-ref][data-decision]");
      if (!(button instanceof HTMLButtonElement)) return;
      const approvalRef = button.getAttribute("data-approval-ref");
      const decision = button.getAttribute("data-decision");
      if (!approvalRef || (decision !== "approve" && decision !== "deny")) return;
      button.disabled = true;
      setStatus(`submitting ${decision}...`);
      try {
        await submitApprovalDecision(approvalRef, decision);
        await loadTimeline();
        setStatus("approval updated");
      } catch (error) {
        setStatus(error.message);
      } finally {
        button.disabled = false;
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
