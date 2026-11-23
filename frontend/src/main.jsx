import React, { useCallback, useEffect, useState } from "react";
import { createRoot } from "react-dom/client";

// --- Sign-in --- Cognito hosted UI sign-in: authorization code flow with PKCE (no client secret in the browser).
const cfg = {
  domain: import.meta.env.VITE_COGNITO_DOMAIN,      // e.g. https://momentum.auth.eu-west-1.amazoncognito.com
  clientId: import.meta.env.VITE_COGNITO_CLIENT_ID,
  redirectUri: import.meta.env.VITE_REDIRECT_URI || window.location.origin + "/",
};

const b64url = (bytes) =>
  btoa(String.fromCharCode(...bytes)).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");

async function challengeFor(verifier) {
  const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(verifier));
  return b64url(new Uint8Array(digest));
}

let idToken = null;   // kept in memory only; a page reload signs in again through the hosted UI session

export const isConfigured = () => Boolean(cfg.domain && cfg.clientId);
export const getToken = () => idToken;
export const signOut = () => {
  idToken = null;
  window.location.assign(`${cfg.domain}/logout?client_id=${cfg.clientId}&logout_uri=${encodeURIComponent(cfg.redirectUri)}`);
};

export async function signIn() {
  const verifier = b64url(crypto.getRandomValues(new Uint8Array(32)));
  const state = b64url(crypto.getRandomValues(new Uint8Array(16)));
  sessionStorage.setItem("pkce_verifier", verifier);
  sessionStorage.setItem("oauth_state", state);
  const q = new URLSearchParams({
    response_type: "code", client_id: cfg.clientId, redirect_uri: cfg.redirectUri,
    scope: "openid email profile", state,
    code_challenge_method: "S256", code_challenge: await challengeFor(verifier),
  });
  window.location.assign(`${cfg.domain}/oauth2/authorize?${q}`);
}

// Call once on page load. Returns true when a token is available.
export async function completeSignIn() {
  const params = new URLSearchParams(window.location.search);
  const code = params.get("code");
  if (!code) return false;
  const expected = sessionStorage.getItem("oauth_state");
  const verifier = sessionStorage.getItem("pkce_verifier");
  sessionStorage.removeItem("oauth_state");
  sessionStorage.removeItem("pkce_verifier");
  window.history.replaceState({}, "", window.location.pathname);   // remove the code from the address bar
  if (!verifier || params.get("state") !== expected) throw new Error("Sign-in response did not match this browser session");
  const res = await fetch(`${cfg.domain}/oauth2/token`, {
    method: "POST",
    headers: { "Content-Type": "application/x-www-form-urlencoded" },
    body: new URLSearchParams({
      grant_type: "authorization_code", client_id: cfg.clientId, code,
      redirect_uri: cfg.redirectUri, code_verifier: verifier,
    }),
  });
  if (!res.ok) throw new Error("Sign-in failed");
  idToken = (await res.json()).id_token;   // the Cognito authorizer on API Gateway validates the ID token
  return true;
}

// --- API client ---
const BASE = import.meta.env.VITE_API_URL || "/api";   // CloudFront forwards /api/* to API Gateway

async function call(method, path, body) {
  const res = await fetch(BASE + path, {
    method,
    headers: { Authorization: getToken(), ...(body ? { "Content-Type": "application/json" } : {}) },
    body: body ? JSON.stringify(body) : undefined,
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.message || `Request failed (${res.status})`);
  return data;
}

export const api = {
  tasks: () => call("GET", "/tasks"),
  createTask: (title, estimateMin) => call("POST", "/tasks", { title, estimateMin }),
  startTask: (id) => call("POST", `/tasks/${id}/start`),
  completeTask: (id) => call("POST", `/tasks/${id}/complete`),
  deleteTask: (id) => call("DELETE", `/tasks/${id}`),
  leaderboard: () => call("GET", "/leaderboard"),
  me: () => call("GET", "/leaderboard/me"),
  // Two steps: ask for a short-lived pre-signed POST, then send the photo straight to S3.
  async uploadProof(id, file) {
    const { url, fields } = await call("POST", `/tasks/${id}/proof-url`);
    const form = new FormData();
    Object.entries(fields).forEach(([k, v]) => form.append(k, v));
    form.append("file", file);   // the file field must come last
    const res = await fetch(url, { method: "POST", body: form });
    if (!res.ok) throw new Error("Photo upload was rejected (JPEG, 1 KB to 5 MB)");
  },
};

// --- Screens ---
function Tasks({ tasks, reload, run }) {
  const [title, setTitle] = useState("");
  const [estimate, setEstimate] = useState(25);

  const add = (e) => {
    e.preventDefault();
    run(async () => { await api.createTask(title, Number(estimate)); setTitle(""); });
  };
  const upload = (id, file) => file && run(() => api.uploadProof(id, file),
    "Photo sent. Verification takes a few seconds, then reload the list.");

  return (
    <section>
      <form onSubmit={add}>
        <input name="title" placeholder="What will you do?" value={title} maxLength={120}
               onChange={(e) => setTitle(e.target.value)} required />
        <input name="estimate" type="number" min="1" max="480" value={estimate}
               onChange={(e) => setEstimate(e.target.value)} aria-label="Estimate in minutes" required />
        <button type="submit">Add task</button>
      </form>
      <ul>
        {tasks.length === 0 && <li className="meta">No tasks yet.</li>}
        {tasks.map((t) => (
          <li key={t.taskId}>
            <div>
              <strong>{t.title}</strong>
              <div className="meta">
                {t.status === "todo" && `Estimate ${t.estimateMin} min`}
                {t.status === "in_progress" && `In progress, estimate ${t.estimateMin} min (locked)`}
                {t.status === "done" && `Done in ${t.actualMin} min (estimate ${t.estimateMin})`
                  + (t.scoreEligible ? "" : ", not scored")
                  + (t.proofStatus === "approved" ? ", proof approved" : t.proofStatus === "rejected" ? ", proof rejected" : "")}
              </div>
            </div>
            <div className="actions">
              {t.status === "todo" && <button onClick={() => run(() => api.startTask(t.taskId))}>Start</button>}
              {t.status === "in_progress" && <button onClick={() => run(() => api.completeTask(t.taskId))}>Complete</button>}
              {t.status !== "todo" && t.proofStatus !== "approved" && (
                <label className="secondary">
                  <input type="file" accept="image/jpeg" hidden onChange={(e) => upload(t.taskId, e.target.files[0])} />
                  <button type="button" className="secondary" onClick={(e) => e.currentTarget.previousSibling.click()}>Add proof</button>
                </label>
              )}
              {t.status !== "done" && <button className="secondary" onClick={() => run(() => api.deleteTask(t.taskId))}>Delete</button>}
            </div>
          </li>
        ))}
      </ul>
      <button className="secondary" onClick={reload}>Reload</button>
    </section>
  );
}

function Leaderboard({ board, me }) {
  return (
    <section>
      <div className="meta">Week {board.week}{me && me.rank ? `, you are #${me.rank} with ${me.points} points` : ", you have no points yet"}</div>
      <ul>
        {board.top.length === 0 && <li className="meta">Nobody has scored this week.</li>}
        {board.top.map((p) => (
          <li key={p.rank}><span>#{p.rank} {p.displayName}</span><span>{p.points} pts, {p.tasksCompleted} tasks</span></li>
        ))}
      </ul>
    </section>
  );
}

function App() {
  const [ready, setReady] = useState(false);
  const [signedIn, setSignedIn] = useState(false);
  const [tasks, setTasks] = useState([]);
  const [board, setBoard] = useState({ week: "", top: [] });
  const [me, setMe] = useState(null);
  const [error, setError] = useState("");
  const [note, setNote] = useState("");

  const load = useCallback(async () => {
    const [t, b, m] = await Promise.all([api.tasks(), api.leaderboard(), api.me()]);
    setTasks(t); setBoard(b); setMe(m);
  }, []);

  const run = useCallback(async (action, message = "") => {
    setError(""); setNote("");
    try { await action(); await load(); setNote(message); } catch (e) { setError(e.message); }
  }, [load]);

  useEffect(() => {
    completeSignIn()
      .then((ok) => { setSignedIn(ok); return ok && load(); })
      .catch((e) => setError(e.message))
      .finally(() => setReady(true));
  }, [load]);

  if (!ready) return <main className="center">Loading...</main>;
  if (!isConfigured()) return <main><div className="error">Set VITE_COGNITO_DOMAIN and VITE_COGNITO_CLIENT_ID (see frontend/README.md).</div></main>;
  if (!signedIn) return (
    <main className="center">
      <h1>Momentum</h1>
      <p>Finish your tasks honestly and climb the weekly leaderboard.</p>
      {error && <div className="error">{error}</div>}
      <button onClick={signIn}>Sign in</button>
    </main>
  );
  return (
    <main>
      <header><h1>Momentum</h1><button className="secondary" onClick={signOut}>Sign out</button></header>
      {error && <div className="error">{error}</div>}
      {note && <div className="note">{note}</div>}
      <h2>My tasks</h2>
      <Tasks tasks={tasks} reload={() => run(() => Promise.resolve())} run={run} />
      <h2>Weekly leaderboard</h2>
      <Leaderboard board={board} me={me} />
    </main>
  );
}

createRoot(document.getElementById("root")).render(<App />);
