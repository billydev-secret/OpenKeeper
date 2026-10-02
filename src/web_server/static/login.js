// Login page script. Externalised from login.html so the dashboard can
// send `script-src 'self'` with no 'unsafe-inline' (see _CSP in server.py).
//
// Not part of the js/ module graph: js/ is the dashboard app, and the login
// page runs before any of it exists.
const ERROR_MESSAGES = {
  invalid_state: "Invalid OAuth state. Please try again.",
  denied: "Authorization was denied.",
  token_failed: "Login failed \u2014 could not exchange token.",
  profile_failed: "Login failed \u2014 could not fetch your Discord profile.",
  guilds_failed: "Login failed \u2014 could not verify guild membership.",
  no_shared_guild: "You must share a server with the bot to use this dashboard.",
  session_failed: "Login failed \u2014 the dashboard couldn\u2019t reach its database. Try again in a moment.",
};
const code = new URLSearchParams(window.location.search).get("error");
if (code) {
  const el = document.getElementById("error-block");
  el.textContent = ERROR_MESSAGES[code] || "An unexpected error occurred.";
  el.style.display = "block";
}
