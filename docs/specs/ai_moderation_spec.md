# AI Moderation — Feature Spec

LLM-assisted moderation backed by a local model. The `/watch` relay below reads live messages as they arrive; nothing here reads stored text, which the bot no longer keeps.

There are no on-demand `/ai review | scan | channel | query` commands or guild-wide message query, because each answered questions from the stored message archive (`docs/plans/remove-message-storage.md`).

- **`/watch add | remove | list`**: opt-in per-user subscription. Every public message from a watched member is evaluated by the LLM; only messages tagged as rule violations are DM'd to the watcher. When the LLM is unavailable, every public message is relayed unfiltered.

## Commands

| Command | Type | Permission | Purpose |
|---|---|---|---|
| `/watch add user:<member>` | Slash | Mod | Subscribe yourself to that member's posts |
| `/watch remove user:<member>` | Slash | Mod | Unsubscribe yourself |
| `/watch list` | Slash | Mod | Show everyone you're currently watching |
| AI config (prompts / clear) | Web | Moderator to view; Admin to edit | Read or override the bot-wide system prompt for the watch check and wellness note |
| AI prompt test | Web | Admin | Run the current prompt against arbitrary input |
| Model status / source / reload | Web | Moderator to view status; Admin to change source/reload | Inspect or change the loaded model file |

---

## Behavior

### `/watch add | remove | list`
- **add**: rejects bots, self, and DM-context invocations. Multiple mods can independently watch the same member. The reply explicitly warns when the LLM is unavailable.
- **remove**: drops the (watched, watcher) pair.
- **list**: shows every member the caller is currently watching; departed members appear as bare IDs.

On every public guild message from a watched member:

- **LLM available**: the message is evaluated by the watch-check prompt (400-character truncation, newlines collapsed, channel name and NSFW tag included). Only messages the model tags as a rule violation are DM'd. Any error during the check falls through to "treat as violation": better to over-notify than silently drop a flag.
- **LLM unavailable**: every public message is relayed unconditionally.

The DM carries the message content, attachment URLs, an optional `⚠️ Rule concern: {reason}` line, and a footer with author, guild, channel, and jump URL. Watchers with closed DMs are silently dropped. The watch-check prompt has explicit high-threshold guidance because every public post from a watched member is evaluated; a chatty false-positive rate would spam DMs.

---

## LLM lifetime

Two backends, selected by whether `LLAMA_SERVER_URL` is set.

**In-process** (default). Model loading is asynchronous and singleton: at most one model is loaded at a time. Phases are `idle → downloading → loading → ready` (or `error`). The model file is downloaded from HuggingFace on first start and cached locally. Inference is serialized through a single worker thread; concurrent calls queue rather than race, so watch checks share that queue with the wellness note.

**Remote.** `LLAMA_SERVER_URL` points at a llama.cpp `llama-server` on another machine, typically one with a GPU. Nothing is downloaded or loaded locally; the phase goes straight to `ready`. Requests use the server's OpenAI-compatible `POST /v1/chat/completions`. Concurrency is the server's business (continuous batching), so watch checks never queue behind the wellness note. `status()` reports which backend is active.

### Endpoint privacy gate

The model sees raw conversation windows, which is exactly the content the local-inference design exists to keep off third-party services. So the remote backend **only accepts an endpoint it can prove is local**: loopback, RFC1918 private, link-local, or unique-local v6 addresses; `localhost`; or a hostname ending in `.local` / `.lan` / `.home` / `.internal` / `.localdomain`. A bare hostname cannot be classified without a DNS lookup and is refused rather than guessed at.

A non-private URL is **refused with an error log and the bot falls back to in-process inference**; it does not silently send content off-network. `LLAMA_SERVER_ALLOW_PUBLIC=1` overrides this deliberately; unrecognized values for that flag fail closed.

The `model` argument threaded through `chat()` remains ignored on both backends and is **never forwarded to the remote server**. Some guild rows still carry hosted model IDs from an abandoned cloud switch; honoring them would route moderation content off-box.

---

## Permissions

- `/watch *` requires the bot's **mod** check; add/remove reject DMs.
- The DM relay needs each watcher to allow DMs from server members. Failures are logged and dropped.
- Dashboard endpoints require **admin** to change anything (prompt save/reset, model source, model reload); viewing the AI config panel and model status require **moderator**.

---

## User-visible errors

| When | The user sees |
|---|---|
| Non-mod runs `/watch *` | "You don't have permission to use this command." |
| `/watch add` while the AI is unavailable | "Now watching {user}. AI filtering is not available — all their public posts will be DM'd to you." |
| `/watch add` on a bot | "You cannot watch bots." |
| `/watch add` on self | "You cannot watch yourself." |
| `/watch add | remove` in DMs | "This command must be used in a server." |
| Dashboard prompt update, unknown key | "Unknown prompt key: {key}" |
| Dashboard model reload, no source set | "No model source configured — set model path and HuggingFace details first." |

---

## Non-goals

- **No history.** Nothing here reads past messages from storage: the watch check sees one live message.
- **No multi-model serving.** Exactly one model is loaded at a time, chosen by the model-source settings. There is no per-command or per-guild model dial: `ollama_client.chat` takes no model argument, and the remote backend is always asked for `"local"` so a stray config row can never route moderation content at a hosted API.
- **No streaming.** Each inference call returns the whole completion.
- **No prompt-injection mitigation** beyond 400-character per-message truncation.
- **No DM inspection.** The watch check reads public-chat messages only.
- **No auto-action.** Every output goes to a human. Nothing is kicked, banned, or muted automatically.
- **No Rule 5 enforcement.** DM-consent violations are handled entirely by human-reviewed user reports.
- **No per-channel watch scoping.** A `/watch` subscription fires across every guild channel.
- **No conversational memory.** Each AI call is one-shot; no follow-ups, no transcripts.

---

## Configuration

Keys an admin sets via the dashboard:

- **Per-job system prompt**: watch check and wellness encouragement. Empty falls through to the hard-coded default. These are **bot-wide**: the AI panel is primary-guild-only, so it writes them at `guild_id=0` and every guild's reader resolves them through `get_config_value`'s legacy fallback. Saving or restoring also clears any per-guild row an older build wrote, so nothing can shadow the value the panel shows. "Primary-guild-only" is enforced on the server, not just hidden in the nav: every write on this panel (prompt save, Restore Original, Try It Out, model source, model reload) returns **403** when the caller's active guild is not `ctx.guild_id`, so an admin of a secondary guild cannot rewrite the prompts every other guild runs.
Global-only (host-level, not per-guild):

- **Model file path**: the local GGUF file.
- **HuggingFace repo + filename**: used to download the model on first start if local file is missing.

Runtime tuning (context length, GPU layers, threads, batch size) is set once via environment variables, not user-tunable through the dashboard.

Backend selection is deployment topology, so it lives in the environment alongside `LAVALINK_HOST` rather than on the dashboard:

| Variable | Meaning |
|---|---|
| `LLAMA_SERVER_URL` | Base URL of a llama.cpp `llama-server`, e.g. `http://192.0.2.20:8080`. Unset ⇒ in-process. |
| `LLAMA_SERVER_TIMEOUT` | Per-request timeout in seconds (default 120). |
| `LLAMA_SERVER_ALLOW_PUBLIC` | `1` (or `true` / `yes`) to permit a non-private endpoint. Off by default; see the privacy gate above. |

Note that `LLAMA_N_CTX` must accommodate the largest prompt any job can build. The code default is 4096; a 32768 setting was sized for the removed `/ai scan` (up to 200 messages at 400 characters each); the surviving prompts are far smaller, so it has headroom. On the remote backend this is the *server's* `-c` flag, not the bot's env var.

---

## Stored data

**Per guild:**

| Table | Contents |
|---|---|
| `watched_users` | (guild_id, watched_user_id, watcher_user_id): one row per mod/member pair. Persists when watchers leave the guild. |

**Global (`guild_id=0`), in the shared config table:** every `ai_prompt_*` override, the model file path, and the HuggingFace source. The wellness prompt entry is co-owned with [[wellness-guardian-spec]].

**Read-only consumers:** none of the message tables. The AI cluster reads live messages only.

**On disk:** the model file, downloaded from HuggingFace on first start and cached across restarts.

**In-memory only:** the watched-users map (hot-updated by `/watch add | remove`); the loaded model singleton.
