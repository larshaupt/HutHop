// Feedback dialog (💬 in the header): a message to the operator, saved on the server with the app's current URL.
import { $, esc } from "./util.js";

function render(status = "") {
  $("#feedback").innerHTML = `
    <div class="about-head">
      <h2>Feedback</h2>
      <button class="about-close" data-close aria-label="Close">×</button>
    </div>
    <form class="about-body" id="feedbackForm">
      <p>Found a mistake, missing a hut or a feature? Tell me. The view you're on (hut, tour) is sent along.</p>
      <textarea class="input" id="feedbackText" rows="6" maxlength="5000" required autofocus
        aria-label="Your feedback" placeholder="Your feedback"></textarea>
      <p class="hint" role="status">${esc(status)}</p>
      <button class="btn primary" type="submit">Send</button>
    </form>`;
}

async function send(e) {
  e.preventDefault();
  const message = $("#feedbackText").value.trim();
  if (!message) return;
  const button = e.target.querySelector("button[type=submit]");
  button.disabled = true;
  let ok = false;
  try {
    const res = await fetch("/api/feedback", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ message, page: location.href.slice(0, 1000) }) });
    ok = res.ok;
  } catch (err) {
    console.error(err);
  }
  if (ok) {
    $("#feedbackForm").innerHTML = `<p>Thanks, your feedback was sent.</p>`;
  } else {
    $("#feedbackForm [role=status]").textContent = "Could not send it right now. Please try again later.";
    button.disabled = false;
  }
}

export function bindFeedback() {
  $("#feedbackBtn").addEventListener("click", () => {
    render();
    $("#feedback").showModal();
  });
  $("#feedback").addEventListener("submit", send);
  $("#feedback").addEventListener("click", (e) => {
    if (e.target.closest("[data-close]") || e.target === $("#feedback")) $("#feedback").close();
  });
}
