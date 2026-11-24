import asyncio
from unittest.mock import AsyncMock, MagicMock, patch
import pytest
from telegram import Chat, Message, Update, User
from telegram.error import BadRequest, TelegramError

import bot


@pytest.fixture
def temp_bot_storage(tmp_path, monkeypatch):
    req_file = tmp_path / "test_requests.json"
    grp_file = tmp_path / "test_groups.json"
    evt_file = tmp_path / "test_events.json"
    srv_file = tmp_path / "test_service.json"

    req_file.write_text("{}", encoding="utf-8")
    grp_file.write_text("{}", encoding="utf-8")
    evt_file.write_text("[]", encoding="utf-8")
    srv_file.write_text("{}", encoding="utf-8")

    monkeypatch.setattr(bot, "REQUESTS_FILE", req_file)
    monkeypatch.setattr(bot, "DATA_FILE", grp_file)
    monkeypatch.setattr(bot, "EVENTS_FILE", evt_file)
    monkeypatch.setattr(bot, "SERVICE_MESSAGES_FILE", srv_file)
    monkeypatch.setattr(bot, "ADMIN_IDS", {12345})

    return {
        "requests": req_file,
        "groups": grp_file,
        "events": evt_file,
        "service": srv_file,
    }


# ===========================================================================
# Test Issue 1 & 5: sync_request_status and Case-Insensitive Error Handling
# ===========================================================================

def test_sync_request_status(temp_bot_storage):
    """Verify sync_request_status updates all pending records for (chat_id, user_id)."""
    requests = {
        "tok1": {"chat_id": 100, "user_id": 200, "status": "requested", "full_name": "Alice"},
        "tok2": {"chat_id": 100, "user_id": 200, "status": "payment_pending", "full_name": "Alice"},
        "tok3": {"chat_id": 100, "user_id": 300, "status": "requested", "full_name": "Bob"},
        "tok4": {"chat_id": 100, "user_id": 200, "status": "approved", "full_name": "Alice"},
    }
    bot.save_requests(requests)

    count = bot.sync_request_status(100, 200, "expired")
    assert count == 2

    updated = bot.load_requests()
    assert updated["tok1"]["status"] == "expired"
    assert updated["tok2"]["status"] == "expired"
    assert updated["tok3"]["status"] == "requested"  # Bob is unaffected
    assert updated["tok4"]["status"] == "approved"   # Already approved, untouched


# ===========================================================================
# Test Issue 2: Safe message edit suppressing "Message is not modified"
# ===========================================================================

@pytest.mark.asyncio
async def test_safe_edit_message_text_suppresses_not_modified():
    """Verify safe_edit_message_text suppresses 'Message is not modified' error."""
    mock_query = AsyncMock()
    mock_query.edit_message_text.side_effect = BadRequest(
        "Bad Request: message is not modified: specified new message content and reply markup are exactly the same"
    )

    result = await bot.safe_edit_message_text(mock_query, "same text")
    assert result is False  # Safely suppressed, returns False


@pytest.mark.asyncio
async def test_safe_edit_message_text_raises_other_errors():
    """Verify safe_edit_message_text re-raises non-not-modified errors."""
    mock_query = AsyncMock()
    mock_query.edit_message_text.side_effect = BadRequest("Bad Request: chat not found")

    with pytest.raises(BadRequest):
        await bot.safe_edit_message_text(mock_query, "some text")


# ===========================================================================
# Test Issue 3: Global error handler registration and handling
# ===========================================================================

@pytest.mark.asyncio
async def test_global_error_handler():
    """Verify global_error_handler ignores 'not modified' and handles errors."""
    # 1. 'Message is not modified' should be ignored silently
    ctx = MagicMock()
    ctx.error = BadRequest("Message is not modified")
    await bot.global_error_handler(None, ctx)

    # 2. Unexpected error with callback query should alert user safely
    ctx.error = RuntimeError("Unexpected crash")
    mock_update = MagicMock(spec=Update)
    mock_query = AsyncMock()
    mock_update.callback_query = mock_query
    mock_update.effective_message = None

    await bot.global_error_handler(mock_update, ctx)
    mock_query.answer.assert_called_once()


# ===========================================================================
# Test Issue 1 & 4: handle_all_pending_action with Hide_requester_missing
# ===========================================================================

@pytest.mark.asyncio
@pytest.mark.parametrize("error_text", [
    "Bad Request: Hide_requester_missing",
    "Bad Request: HIDE_REQUESTER_MISSING",
    "hide_requester_missing",
])
async def test_handle_all_pending_action_hide_requester_missing(temp_bot_storage, error_text):
    """Verify Hide_requester_missing updates status to expired and prevents cascading errors."""
    bot.save_requests({
        "tok_abc": {
            "chat_id": -100123,
            "user_id": 999,
            "full_name": "Test User",
            "status": "requested",
            "request_time": "2026-11-25T00:00:00+00:00",
        }
    })
    bot.save_groups({
        "-100123": {"title": "Test Group", "chat_id": -100123}
    })

    mock_update = MagicMock(spec=Update)
    mock_query = AsyncMock()
    mock_query.edit_message_text.side_effect = BadRequest("Message is not modified")
    mock_update.callback_query = mock_query

    mock_context = MagicMock()
    mock_context.bot = AsyncMock()
    # Simulate Telegram failing with Hide_requester_missing on decline
    mock_context.bot.decline_chat_join_request.side_effect = BadRequest(error_text)

    # Should execute without throwing any cascading exception!
    await bot.handle_all_pending_action(mock_update, mock_context, "apreq:decline:tok_abc:0")

    # Record must be marked expired so it disappears from pending list!
    updated_requests = bot.load_requests()
    assert updated_requests["tok_abc"]["status"] == "expired"
    # Admin was notified with an alert
    mock_query.answer.assert_called()


@pytest.mark.asyncio
async def test_handle_all_pending_action_user_already_participant(temp_bot_storage):
    """Verify USER_ALREADY_PARTICIPANT updates status to approved."""
    bot.save_requests({
        "tok_xyz": {
            "chat_id": -100123,
            "user_id": 888,
            "full_name": "Member User",
            "status": "requested",
            "request_time": "2026-11-25T00:00:00+00:00",
        }
    })
    bot.save_groups({
        "-100123": {"title": "Test Group", "chat_id": -100123}
    })

    mock_update = MagicMock(spec=Update)
    mock_query = AsyncMock()
    mock_update.callback_query = mock_query

    mock_context = MagicMock()
    mock_context.bot = AsyncMock()
    mock_context.bot.decline_chat_join_request.side_effect = BadRequest("Bad Request: USER_ALREADY_PARTICIPANT")

    await bot.handle_all_pending_action(mock_update, mock_context, "apreq:decline:tok_xyz:0")

    updated_requests = bot.load_requests()
    assert updated_requests["tok_xyz"]["status"] == "approved"


# ===========================================================================
# Test Issue 5: Synchronization on Join/Leave
# ===========================================================================

@pytest.mark.asyncio
async def test_member_update_syncs_pending_requests(temp_bot_storage):
    """Verify member_update auto-approves pending request when user joins."""
    bot.save_requests({
        "tok_join": {
            "chat_id": -100555,
            "user_id": 777,
            "full_name": "Joining User",
            "status": "requested",
        }
    })
    bot.save_groups({
        "-100555": {"title": "Join Group", "chat_id": -100555}
    })

    mock_update = MagicMock(spec=Update)
    mock_change = MagicMock()
    mock_change.chat.id = -100555
    mock_change.chat.title = "Join Group"

    # User transitioned from left to member
    mock_change.old_chat_member.status = "left"
    mock_change.new_chat_member.status = "member"
    mock_change.new_chat_member.user.is_bot = False
    mock_change.new_chat_member.user.id = 777
    mock_change.new_chat_member.user.first_name = "Joining"
    mock_change.new_chat_member.user.last_name = "User"
    mock_change.new_chat_member.user.username = "joining_user"

    mock_update.chat_member = mock_change
    mock_context = MagicMock()
    mock_context.bot = AsyncMock()
    mock_context.bot.get_me.return_value = User(id=99999, first_name="Bot", is_bot=True)

    await bot.member_update(mock_update, mock_context)

    updated_requests = bot.load_requests()
    assert updated_requests["tok_join"]["status"] == "approved"


# ===========================================================================
# Test Issue 1 & 4: handle_pending_request_action (Per-Group Pending)
# ===========================================================================

@pytest.mark.asyncio
async def test_handle_pending_request_action_hide_requester_missing(temp_bot_storage):
    """Verify handle_pending_request_action handles Hide_requester_missing."""
    bot.save_requests({
        "tok_grp": {
            "chat_id": -100999,
            "user_id": 123,
            "full_name": "Group User",
            "status": "requested",
            "request_time": "2026-11-25T00:00:00+00:00",
        }
    })
    bot.save_groups({
        "-100999": {"title": "Group 999", "chat_id": -100999}
    })

    mock_update = MagicMock(spec=Update)
    mock_query = AsyncMock()
    mock_update.callback_query = mock_query

    mock_context = MagicMock()
    mock_context.bot = AsyncMock()
    mock_context.bot.decline_chat_join_request.side_effect = BadRequest("Bad Request: Hide_requester_missing")

    await bot.handle_pending_request_action(mock_update, mock_context, "preq:decline:tok_grp")

    updated_requests = bot.load_requests()
    assert updated_requests["tok_grp"]["status"] == "expired"
    mock_query.answer.assert_called()


# ===========================================================================
# Test Issue 1: handle_decision (Admin Private Notification Callback)
# ===========================================================================

@pytest.mark.asyncio
async def test_handle_decision_hide_requester_missing(temp_bot_storage):
    """Verify handle_decision handles Hide_requester_missing without crashing."""
    bot.save_requests({
        "tok_dec": {
            "chat_id": -100777,
            "user_id": 456,
            "full_name": "Dec User",
            "status": "requested",
        }
    })

    mock_update = MagicMock(spec=Update)
    mock_query = AsyncMock()
    mock_query.message.text = "Join request for Dec User"
    mock_update.callback_query = mock_query

    mock_context = MagicMock()
    mock_context.bot = AsyncMock()
    mock_context.bot.decline_chat_join_request.side_effect = BadRequest("Bad Request: Hide_requester_missing")

    await bot.handle_decision(mock_update, mock_context, "request:decline:-100777:456")

    updated_requests = bot.load_requests()
    assert updated_requests["tok_dec"]["status"] == "expired"


# ===========================================================================
# Test Issue 5: syncrequests_command
# ===========================================================================

@pytest.mark.asyncio
async def test_syncrequests_command(temp_bot_storage):
    """Verify /syncrequests synchronizes all pending requests with Telegram."""
    bot.save_requests({
        "tok_p1": {"chat_id": -1001, "user_id": 111, "status": "requested"},
        "tok_p2": {"chat_id": -1001, "user_id": 222, "status": "requested"},
    })

    mock_update = MagicMock(spec=Update)
    mock_update.effective_user.id = 12345  # Admin
    mock_update.effective_message = AsyncMock()

    mock_context = MagicMock()
    mock_context.bot = AsyncMock()

    # User 111 is already a member; user 222 is not
    async def fake_get_chat_member(chat_id, user_id):
        m = MagicMock()
        if user_id == 111:
            m.status = "member"
        else:
            raise BadRequest("Bad Request: USER_NOT_PARTICIPANT")
        return m

    mock_context.bot.get_chat_member.side_effect = fake_get_chat_member

    await bot.syncrequests_command(mock_update, mock_context)

    requests = bot.load_requests()
    assert requests["tok_p1"]["status"] == "approved"
    assert requests["tok_p2"]["status"] == "requested"
