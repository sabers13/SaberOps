# Telegram bot interaction (TG-1, Step 018, optional)

The Telegram surface notifies the owner about `Q-1` owner questions, gate
completions and wave milestones, answers conversational status queries from
stored records, and routes keyed `/answer` replies into the question queue.
It is optional: with no bot token configured, the package stays inert.

The module is `saberops.telegram`. Like the rest of the engine it never
calls a paid/LLM provider; the only network use is the Telegram Bot HTTPS
API. Status answers never wake the Orchestrator — they read the durable
question document and nothing else.

## Setup

1. Create a bot with `@BotFather` and keep the token secret.
2. Find your numeric chat/user id (e.g. via `@userinfobot`).
3. Export the environment before starting the daemon:

```bash
export SABEROPS_TELEGRAM_BOT_TOKEN="<token>"
export SABEROPS_TELEGRAM_ALLOW_CHAT_IDS="<chat id>"
# optional second identity:
export SABEROPS_TELEGRAM_ALLOW_USER_IDS="<user id>"
```

| Variable | Meaning | Default |
|---|---|---|
| `SABEROPS_TELEGRAM_BOT_TOKEN` | Bot API token (never logged, never stored) | unset (disabled) |
| `SABEROPS_TELEGRAM_ALLOW_CHAT_IDS` | comma/space separated chat ids that may interact | empty (refuse all) |
| `SABEROPS_TELEGRAM_ALLOW_USER_IDS` | comma/space separated user ids that may interact | empty (refuse all) |
| `SABEROPS_TELEGRAM_QUIET` | `1`/`true` mutes all notifications until cleared | off |
| `SABEROPS_TELEGRAM_DND_START` / `_END` | do-not-disturb window as `HH:MM` (24h) | unset |
| `SABEROPS_TELEGRAM_MIN_INTERVAL_S` | minimum seconds between notification sends | `0` |
| `SABEROPS_TELEGRAM_DIGEST` | `1`/`true` buffers notifications; send one digest via `flush_digest()` | off |

Authorization is fail-closed: with both whitelists empty, every incoming
update is refused and the queue is never touched. A refused sender gets no
reply and leaves no record.

## Owner commands

| Command | Effect |
|---|---|
| `/status` | counts plus divergences, and every question awaiting an answer |
| `/pending` | every open (or due deferred) question |
| `/question <key-or-id>` | one question with its five escalation parts |
| `/answer <key-or-id> <exact words>` | record an owner answer (stored verbatim) |
| `/help` | the command list |

Answers go through `OwnerQuestionQueue.answer` (exported as
`QuestionStore` for the TG-1 contract) with `OWNER` authority. A later
conflicting answer is kept as a divergent record; the earlier answer
stands. Each Telegram `update_id` and each `(chat, message)` pair is
processed at most once: duplicate webhook deliveries and retried polls
never re-answer and never duplicate a record.

## Quiet/away posture

* `SABEROPS_TELEGRAM_QUIET=1` mutes everything (e.g. set when away).
* The DND window mutes nights/weekends on a schedule; overnight windows
  such as `22:00`–`07:00` are supported.
* `SABEROPS_TELEGRAM_MIN_INTERVAL_S` rate-limits bursts.
* Digest mode collects notifications; one combined message goes out on
  `flush_digest()`.
* A question is notified once; re-notification needs an explicit `force`.

## Security notes

* The token lives only in the environment and in Bot API request bodies;
  `repr`, errors and diagnostics render it as `<redacted>`.
* Only whitelisted chats/users can read status or answer questions.
* The daemon holds no Orchestrator handle and imports no engine modules:
  there is no path from a Telegram message to waking a run.
