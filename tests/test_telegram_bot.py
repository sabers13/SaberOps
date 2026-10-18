"""Telegram bot interaction surface (TG-1, Step 018).

Hermetic: the Bot API transport is always a recording fake, every question
store lives in ``tmp_path``, and every clock read is an injected ``now`` —
the tests never touch the network, the owner's real state, or the clock.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from saberops.questions import (
    AnswerAuthority,
    AskRole,
    DecisionClass,
    Escalation,
    OwnerQuestion,
    OwnerQuestionQueue,
    QuestionRequest,
)
from saberops.telegram import (
    AnswerRouter,
    BotApiClient,
    BotDaemon,
    GateCompletion,
    NotificationService,
    QuestionStore,
    StatusReader,
    TelegramApiError,
    TelegramConfig,
    TelegramTransportError,
    WaveMilestone,
    format_gate_completion,
    format_question_notification,
    format_wave_milestone,
    is_status_query,
    parse_answer_command,
    parse_incoming_message,
)
from saberops.telegram.answers import IncomingMessage


def _escalation() -> Escalation:
    return Escalation(
        requirement="The lane cannot pass the gate without editing a protected test",
        proposed_expansion="Allow editing the blocked test in this lane",
        smallest_compliant="Add one assertion to the existing test",
        consequences="An accepted test contract changes",
        recommendation="Owner decides; the Orchestrator cannot",
    )


def _request(key: str = "wave3/gate-block") -> QuestionRequest:
    return QuestionRequest(
        key=key,
        subject="May the lane touch the blocked test to fix the failure?",
        kind="gate-block",
        decision_class=DecisionClass.TEST_CHANGE_AGAINST_ADR,
        raised_by_role=AskRole.WORKER,
        raised_by="worker:wave3",
        escalation=_escalation(),
        run_id="wave3",
        blocks=("wave3/lane-a",),
        options=("yes-edit", "no-defer"),
        default_option="no-defer",
    )


@pytest.fixture()
def queue(tmp_path: Path) -> OwnerQuestionQueue:
    return OwnerQuestionQueue(tmp_path / "questions.json")


@pytest.fixture()
def question(queue: OwnerQuestionQueue) -> OwnerQuestion:
    return queue.ask(_request(), now="2026-10-18T10:00:00+00:00")


@pytest.fixture()
def config() -> TelegramConfig:
    return TelegramConfig(
        bot_token="test-token",
        allowed_chat_ids=(111,),
        allowed_user_ids=(222,),
    )


class _FakeApi:
    """Recording Bot API fake: ``transport(method, params) -> result``."""

    def __init__(self, updates: list[dict[str, Any]] | None = None) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.updates = updates or []

    def __call__(self, method: str, params: dict[str, Any]) -> Any:
        self.calls.append((method, dict(params)))
        if method == "sendMessage":
            return {"message_id": len(self.calls), "chat": {"id": params["chat_id"]}}
        if method == "getUpdates":
            return list(self.updates)
        raise AssertionError(f"unexpected method {method!r}")

    def send(self, chat_id: int, text: str) -> Any:
        """The ``(chat_id, text)`` send contract used by NotificationService."""
        return self.__call__("sendMessage", {"chat_id": chat_id, "text": text})


def _now() -> datetime:
    return datetime(2026, 10, 18, 12, 0, 0)


def _message(
    text: str,
    *,
    update_id: int = 7,
    message_id: int = 42,
    chat_id: int = 111,
    user_id: int | None = 222,
) -> IncomingMessage:
    return IncomingMessage(
        update_id=update_id,
        message_id=message_id,
        chat_id=chat_id,
        user_id=user_id,
        text=text,
    )


# ---------------------------------------------------------------------------
# notification formatting
# ---------------------------------------------------------------------------


def test_question_notification_names_key_options_and_answer_hint(
    question: OwnerQuestion,
) -> None:
    """Covers: prompt § Outcomes 1 — Q-1 notification formatting."""
    text = format_question_notification(question)
    assert "wave3/gate-block" in text
    assert "May the lane touch the blocked test" in text
    assert "yes-edit" in text and "no-defer" in text
    assert "requirement" in text and "recommendation" in text
    assert "/answer wave3/gate-block" in text


def test_gate_and_wave_formatting_name_outcome_and_status() -> None:
    """Covers: prompt § Outcomes 1 — gate/wave milestone formatting."""
    gate = format_gate_completion(
        GateCompletion(
            project="p", lane="lane-a", gate="G1", outcome="PASS", head_sha="abc123"
        )
    )
    assert "G1" in gate and "PASS" in gate and "lane-a" in gate
    wave = format_wave_milestone(
        WaveMilestone(wave="wave3", status="complete", detail="all lanes merged")
    )
    assert "wave3" in wave and "complete" in wave and "all lanes merged" in wave


# ---------------------------------------------------------------------------
# notification posture: quiet, DND, rate limiting, dedup, digest
# ---------------------------------------------------------------------------


def test_quiet_mode_mutes_question_notification(
    config: TelegramConfig, question: OwnerQuestion
) -> None:
    """Covers: prompt § Outcomes 1 — quiet/away posture mutes notifications."""
    fake = _FakeApi()
    service = NotificationService(replace(config, quiet=True), fake.send)
    result = service.notify_question(question, now=_now())
    assert result.sent is False and result.reason == "quiet"
    assert fake.calls == []


def test_dnd_window_mutes_gate_notification(config: TelegramConfig) -> None:
    """Covers: prompt § Outcomes 1 — do-not-disturb windows mute notifications."""
    fake = _FakeApi()
    dnd = replace(config, dnd_start=(22, 0), dnd_end=(7, 0))
    service = NotificationService(dnd, fake.send)
    night = datetime(2026, 10, 18, 23, 30, 0)
    result = service.notify_gate_completion(
        GateCompletion(project="p", lane="l", gate="G1", outcome="PASS"), now=night
    )
    assert result.sent is False and result.reason == "dnd"
    assert fake.calls == []
    day = datetime(2026, 10, 18, 12, 0, 0)
    result = service.notify_gate_completion(
        GateCompletion(project="p", lane="l", gate="G1", outcome="PASS"), now=day
    )
    assert result.sent is True


def test_question_notification_is_deduplicated(
    config: TelegramConfig, question: OwnerQuestion
) -> None:
    """Covers: prompt § Outcomes 1 — a question is notified once unless forced."""
    fake = _FakeApi()
    service = NotificationService(config, fake.send)
    first = service.notify_question(question, now=_now())
    second = service.notify_question(question, now=_now())
    assert first.sent is True and second.sent is False
    assert second.reason == "duplicate"
    assert len(fake.calls) == 1


def test_rate_limiting_suppresses_bursts(config: TelegramConfig) -> None:
    """Covers: prompt § Outcomes 1 — configurable notification frequency."""
    fake = _FakeApi()
    limited = replace(config, min_interval_s=60.0)
    service = NotificationService(limited, fake.send)
    base = _now()
    first = service.notify_wave_milestone(
        WaveMilestone(wave="w", status="started"), now=base, now_ts=1000.0
    )
    second = service.notify_wave_milestone(
        WaveMilestone(wave="w", status="progress"), now=base, now_ts=1030.0
    )
    assert first.sent is True
    assert second.sent is False and second.reason == "rate-limited"


def test_digest_mode_buffers_and_flushes(config: TelegramConfig) -> None:
    """Covers: prompt § Outcomes 1 — digest batching for away posture."""
    fake = _FakeApi()
    digest = replace(config, digest=True)
    service = NotificationService(digest, fake.send)
    result = service.notify_wave_milestone(
        WaveMilestone(wave="w", status="started"), now=_now()
    )
    assert result.sent is False and result.reason == "digested"
    assert fake.calls == []
    flushed = service.flush_digest()
    assert flushed.sent is True
    assert fake.calls and fake.calls[0][0] == "sendMessage"


# ---------------------------------------------------------------------------
# answer routing: authorization, verbatim answers, idempotency
# ---------------------------------------------------------------------------


def test_question_store_alias_is_the_q1_queue() -> None:
    """Covers: prompt § Outcomes 2 — QuestionStore contract name."""
    assert QuestionStore is OwnerQuestionQueue


def test_answer_routing_stores_exact_owner_words(
    config: TelegramConfig, question: OwnerQuestion, queue: OwnerQuestionQueue
) -> None:
    """Covers: prompt § Outcomes 2 — keyed answers feed QuestionStore.answer."""
    router = AnswerRouter(config, queue)
    words = "Yes — edit ONLY tests/test_gate_scope.py,  nothing else."
    outcome = router.route(_message(f"/answer wave3/gate-block {words}"))
    assert outcome.kind == "answered"
    stored = queue.get("wave3/gate-block")
    assert stored is not None and stored.answer is not None
    assert stored.answer.words == words
    assert stored.answer.authority is AnswerAuthority.OWNER


def test_duplicate_update_never_reanswers(
    config: TelegramConfig, question: OwnerQuestion, queue: OwnerQuestionQueue
) -> None:
    """Covers: prompt § Outcomes 2 — idempotent interaction IDs."""
    router = AnswerRouter(config, queue)
    message = _message("/answer wave3/gate-block first words")
    first = router.route(message)
    second = router.route(message)
    assert first.kind == "answered"
    assert second.kind == "duplicate"
    stored = queue.get("wave3/gate-block")
    assert stored is not None
    assert stored.answer is not None and stored.answer.words == "first words"
    assert len(stored.history) == 2  # ask + one answer; the duplicate added nothing


def test_redelivered_message_id_is_also_duplicate(
    config: TelegramConfig, queue: OwnerQuestionQueue
) -> None:
    """Covers: prompt § Outcomes 2 — (chat, message) dedup across update ids."""
    queue.ask(_request(), now="2026-10-18T10:00:00+00:00")
    router = AnswerRouter(config, queue)
    assert router.route(_message("/answer wave3/gate-block yes", update_id=1)).kind == "answered"
    retry = router.route(_message("/answer wave3/gate-block yes", update_id=2))
    assert retry.kind == "duplicate"


def test_unauthorized_sender_is_refused_without_touching_queue(
    config: TelegramConfig, question: OwnerQuestion, queue: OwnerQuestionQueue
) -> None:
    """Covers: prompt § Outcomes 2 — strict chat/user whitelist refusal."""
    router = AnswerRouter(config, queue)
    outcome = router.route(
        _message("/answer wave3/gate-block hostile words", chat_id=999, user_id=888)
    )
    assert outcome.kind == "refused"
    stored = queue.get("wave3/gate-block")
    assert stored is not None and stored.answer is None


def test_empty_whitelist_authorizes_nobody(queue: OwnerQuestionQueue) -> None:
    """Covers: prompt § Outcomes 2 — fail-closed authorization default."""
    queue.ask(_request(), now="2026-10-18T10:00:00+00:00")
    router = AnswerRouter(TelegramConfig(bot_token="t"), queue)
    outcome = router.route(_message("/answer wave3/gate-block hi"))
    assert outcome.kind == "refused"


def test_unknown_question_reference_is_reported(
    config: TelegramConfig, queue: OwnerQuestionQueue
) -> None:
    """Covers: prompt § Outcomes 2 — unknown refs never create questions."""
    router = AnswerRouter(config, queue)
    outcome = router.route(_message("/answer no-such-key some words"))
    assert outcome.kind == "unknown"
    assert queue.get("no-such-key") is None


def test_answer_command_grammar() -> None:
    """Covers: prompt § Outcomes 2 — /answer grammar, @Bot suffix accepted."""
    assert parse_answer_command("/answer key words here") == ("key", "words here")
    assert parse_answer_command("/answer@OwnerBot key words") == ("key", "words")
    assert parse_answer_command("/answer key") is None
    assert parse_answer_command("/answer") is None
    assert parse_answer_command("just chatting") is None
    assert parse_answer_command("/status") is None


def test_update_parsing_rejects_non_messages() -> None:
    """Covers: prompt § Outcomes 2 — non-text updates are ignored safely."""
    assert parse_incoming_message({}) is None
    assert parse_incoming_message({"update_id": 1}) is None
    assert (
        parse_incoming_message(
            {"update_id": 1, "message": {"message_id": 2, "chat": {"id": 111}}}
        )
        is None
    )
    parsed = parse_incoming_message(
        {
            "update_id": 3,
            "message": {
                "message_id": 4,
                "chat": {"id": 111},
                "from": {"id": 222},
                "text": "/status",
            },
        }
    )
    assert parsed is not None and parsed.chat_id == 111 and parsed.user_id == 222


# ---------------------------------------------------------------------------
# conversational status from stored records (no orchestrator wake)
# ---------------------------------------------------------------------------


def test_status_query_lists_pending_from_store(
    config: TelegramConfig, question: OwnerQuestion, queue: OwnerQuestionQueue
) -> None:
    """Covers: prompt § Outcomes 1 — status answered from stored records."""
    reader = StatusReader()
    reply = reader.answer_query("/status", queue, as_of=datetime(2026, 10, 18).date())
    assert reply is not None
    assert "wave3/gate-block" in reply
    assert "1 question(s)" in reply


def test_question_detail_renders_escalation(
    question: OwnerQuestion, queue: OwnerQuestionQueue
) -> None:
    """Covers: prompt § Outcomes 1 — per-question detail from stored records."""
    reader = StatusReader()
    reply = reader.answer_query("/question wave3/gate-block", queue)
    assert reply is not None
    assert "smallest_compliant" in reply
    assert "May the lane touch" in reply
    assert reader.answer_query("/question missing", queue) == "Unknown question: 'missing'"


def test_non_status_text_is_not_a_status_query() -> None:
    """Covers: prompt § Outcomes 1 — only status commands take the read path."""
    assert is_status_query("/status") is True
    assert is_status_query("/answer k words") is False
    assert is_status_query("hello") is False
    reader = StatusReader()
    assert reader.answer_query("hello", OwnerQuestionQueue("/nonexistent.json")) is None


def test_help_lists_commands(queue: OwnerQuestionQueue) -> None:
    """Covers: prompt § Outcomes 1 — discoverable conversational commands."""
    reply = StatusReader().answer_query("/help", queue)
    assert reply is not None and "/answer" in reply and "/status" in reply


# ---------------------------------------------------------------------------
# daemon dispatch
# ---------------------------------------------------------------------------


def _update(
    text: str, *, update_id: int = 7, chat_id: int = 111, user_id: int = 222
) -> dict[str, Any]:
    return {
        "update_id": update_id,
        "message": {
            "message_id": 42,
            "chat": {"id": chat_id},
            "from": {"id": user_id},
            "text": text,
        },
    }


def _daemon(
    config: TelegramConfig,
    queue: OwnerQuestionQueue,
    fake: _FakeApi,
) -> BotDaemon:
    return BotDaemon(
        config=config,
        client=BotApiClient(bot_token="test-token", transport=fake),
        queue=queue,
    )


def test_daemon_answers_status_without_writing(
    config: TelegramConfig, question: OwnerQuestion, queue: OwnerQuestionQueue
) -> None:
    """Covers: prompt § Outcomes 1 — status replies read the store, write nothing."""
    fake = _FakeApi()
    daemon = _daemon(config, queue, fake)
    result = daemon.handle_update(_update("/status"), as_of=datetime(2026, 10, 18).date())
    assert result.replied is True and result.outcome == "status"
    assert "wave3/gate-block" in result.reply_text
    assert fake.calls and fake.calls[0][0] == "sendMessage"
    assert queue.get("wave3/gate-block") is not None


def test_daemon_refuses_unauthorized_chat_silently(
    config: TelegramConfig, question: OwnerQuestion, queue: OwnerQuestionQueue
) -> None:
    """Covers: prompt § Outcomes 2 — refused senders get no reply, no record."""
    fake = _FakeApi()
    daemon = _daemon(config, queue, fake)
    result = daemon.handle_update(_update("/status", chat_id=999, user_id=888))
    assert result.replied is False and result.outcome == "refused"
    assert fake.calls == []


def test_daemon_routes_answer_and_dedupes_redelivery(
    config: TelegramConfig, question: OwnerQuestion, queue: OwnerQuestionQueue
) -> None:
    """Covers: prompt § Outcomes 2 — daemon answer routing with idempotency."""
    fake = _FakeApi()
    daemon = _daemon(config, queue, fake)
    first = daemon.handle_update(_update("/answer wave3/gate-block proceed"))
    assert first.replied is True and first.outcome.startswith("answered:")
    stored = queue.get("wave3/gate-block")
    assert stored is not None and stored.answer is not None
    assert stored.answer.words == "proceed"
    sends = len(fake.calls)
    second = daemon.handle_update(_update("/answer wave3/gate-block proceed"))
    assert second.replied is False and second.outcome == "duplicate"
    assert len(fake.calls) == sends  # no second reply, no second answer


def test_daemon_hints_on_unknown_text(
    config: TelegramConfig, queue: OwnerQuestionQueue
) -> None:
    """Covers: prompt § Outcomes 1 — unknown chatter gets the /help hint."""
    fake = _FakeApi()
    daemon = _daemon(config, queue, fake)
    result = daemon.handle_update(_update("hello there"))
    assert result.replied is True and "/help" in result.reply_text


def test_daemon_poll_once_advances_offset(
    config: TelegramConfig, queue: OwnerQuestionQueue
) -> None:
    """Covers: prompt § Outcomes 1 — polling advances the update offset."""
    fake = _FakeApi(updates=[_update("/help", update_id=10), _update("/help", update_id=11)])
    daemon = _daemon(config, queue, fake)
    results = daemon.poll_once()
    assert len(results) == 2
    assert daemon.next_offset == 12
    assert daemon.updates_snapshot()["configured"] is True


# ---------------------------------------------------------------------------
# client transport behaviour (fake-backed)
# ---------------------------------------------------------------------------


def test_client_send_message_posts_expected_shape() -> None:
    """Covers: prompt § Outcomes 1 — Bot API sendMessage contract."""
    fake = _FakeApi()
    client = BotApiClient(bot_token="token", transport=fake)
    client.send_message(111, "hello")
    assert fake.calls == [("sendMessage", {"chat_id": 111, "text": "hello"})]


def test_client_rejects_empty_token_and_text() -> None:
    """Covers: prompt § Outcomes 1 — fail-closed client construction."""
    with pytest.raises(TelegramTransportError):
        BotApiClient(bot_token="  ", transport=_FakeApi())
    client = BotApiClient(bot_token="token", transport=_FakeApi())
    with pytest.raises(TelegramTransportError):
        client.send_message(111, "   ")


def test_client_surfaces_api_failure() -> None:
    """Covers: prompt § Outcomes 1 — Bot API errors are typed, token-free."""

    def failing(method: str, params: dict[str, Any]) -> Any:
        raise TelegramApiError(method, "FORBIDDEN: bot was blocked")

    client = BotApiClient(bot_token="secret-xyz", transport=failing)
    with pytest.raises(TelegramApiError, match="FORBIDDEN"):
        client.send_message(111, "hello")
    assert "secret-xyz" not in repr(client)


def test_client_get_updates_keeps_only_mappings() -> None:
    """Covers: prompt § Outcomes 1 — malformed updates never reach dispatch."""

    def mixed(method: str, params: dict[str, Any]) -> Any:
        assert method == "getUpdates"
        return [{"update_id": 1}, "junk", 42]

    client = BotApiClient(bot_token="token", transport=mixed)
    assert client.get_updates() == [{"update_id": 1}]


# ---------------------------------------------------------------------------
# configuration behaviour
# ---------------------------------------------------------------------------


def test_config_from_env_parses_whitelist_and_posture() -> None:
    """Covers: prompt § Outcomes 1 — env-driven authorization and quiet posture."""
    config = TelegramConfig.from_env(
        {
            "SABEROPS_TELEGRAM_BOT_TOKEN": "abc",
            "SABEROPS_TELEGRAM_ALLOW_CHAT_IDS": "111, 222",
            "SABEROPS_TELEGRAM_ALLOW_USER_IDS": "333",
            "SABEROPS_TELEGRAM_QUIET": "true",
            "SABEROPS_TELEGRAM_DND_START": "22:00",
            "SABEROPS_TELEGRAM_DND_END": "07:00",
            "SABEROPS_TELEGRAM_MIN_INTERVAL_S": "5",
            "SABEROPS_TELEGRAM_DIGEST": "1",
        }
    )
    assert config.is_configured is True
    assert config.allowed_chat_ids == (111, 222)
    assert config.quiet is True and config.digest is True
    assert config.in_dnd(23, 0) is True and config.in_dnd(12, 0) is False
    assert "abc" not in repr(config)


def test_config_rejects_bad_ids_and_times() -> None:
    """Covers: prompt § Outcomes 2 — fail-closed configuration parsing."""
    with pytest.raises(ValueError):
        TelegramConfig.from_env({"SABEROPS_TELEGRAM_ALLOW_CHAT_IDS": "abc"})
    with pytest.raises(ValueError):
        TelegramConfig.from_env({"SABEROPS_TELEGRAM_DND_START": "25:00"})
    with pytest.raises(ValueError):
        TelegramConfig.from_env({"SABEROPS_TELEGRAM_MIN_INTERVAL_S": "soon"})
    assert TelegramConfig(bot_token="t").is_configured is False
