"""Chat-only recipient contract and notification retry tests; no database or network."""

from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import httpx
import pytest

from roo.bridge_mentions import BridgeRecipient, VerifiedBridgeContext
from roo.clients.mlai_backend import MLAIBackendClient
from roo import points_awards
from roo.skills.executor import SkillExecutor


ACTOR = "00000000-0000-4000-8000-000000000001"
TARGET = "00000000-0000-4000-8000-000000000002"
OTHER = "00000000-0000-4000-8000-000000000003"
CONTEXT = VerifiedBridgeContext("ab" * 32, ACTOR, "member", (BridgeRecipient(TARGET, "Chat member"),))
TEXT = "<@UROO> please give @Chat member 4 points for self intro"


def confirmation(*, profile_id=TARGET, created=True, slack_user_id=None):
    return {"success": True, "ledger_id": 42, "new_balance": 88,
            "created": created, "replayed": not created,
            "recipient": {"kind": "mlai_profile", "profile_id": profile_id,
                          "display_name": "Chat member", "slack_user_id": slack_user_id}}


async def award(client, *, context=CONTEXT, text=TEXT, params=None):
    return await points_awards.award_bridge_points(
        client=client, context=context, text=text, params=params or {},
        admin_slack_id="UADMIN", channel_id="CSTART", thread_ts="111.222",
        bot_user_id="UROO",
    )


@pytest.fixture(autouse=True)
def forbid_external_sends(monkeypatch):
    monkeypatch.setattr(points_awards, "send_dm", Mock(side_effect=AssertionError("Unexpected Slack DM")))


@pytest.mark.asyncio
async def test_chat_only_award_uses_verified_profile_and_stable_source_not_model_ids():
    client = SimpleNamespace(award_points=AsyncMock(return_value=confirmation()))
    result = await award(client, params={"target_profile_id": OTHER, "target_user": "UFABRICATED", "points": 900})
    client.award_points.assert_awaited_once_with(
        "UADMIN", None, 4, "self intro", target_profile_id=TARGET,
        idempotency_key=f"roo-chat-award:{CONTEXT.source_event_id}:{TARGET}",
    )
    assert "Awarded 4 points to Chat member" in result
    assert "88" not in result and "balance" not in result.lower()
    points_awards.send_dm.assert_not_called()


@pytest.mark.asyncio
async def test_missing_explicit_target_never_falls_back_to_names_or_model_params():
    client = SimpleNamespace(award_points=AsyncMock())
    result = await award(client, context=replace(CONTEXT, recipient_targets=()), params={"target_profile_id": TARGET})
    assert "mention picker" in result
    client.award_points.assert_not_awaited()


@pytest.mark.asyncio
async def test_distinct_mixed_chat_and_slack_recipients_ask_for_separate_commands_before_credit():
    client = SimpleNamespace(award_points=AsyncMock())
    result = await award(client, text="<@UROO> give @Chat member and <@USLACK> 4 points for helping")
    assert "separate award commands" in result
    client.award_points.assert_not_awaited()


@pytest.mark.asyncio
async def test_same_canonical_recipient_native_and_slack_alias_tags_award_once():
    client = SimpleNamespace(award_points=AsyncMock(return_value=confirmation()))
    context = replace(CONTEXT, recipient_targets=(BridgeRecipient(TARGET, "Member", ("Member",), "UMEMBER"),))
    result = await award(client, context=context, text="<@UROO> give <@UMEMBER> 4 points for helping")
    assert "Awarded 4 points to Member" in result
    client.award_points.assert_awaited_once()
    assert client.award_points.call_args.kwargs["target_profile_id"] == TARGET


@pytest.mark.asyncio
async def test_slack_mentions_in_code_do_not_create_a_mixed_recipient_command():
    client = SimpleNamespace(award_points=AsyncMock(return_value=confirmation()))
    result = await award(client, text=TEXT + " using `give <@USLACK> 4 points` as an example")
    assert "Awarded 4 points to Chat member" in result
    client.award_points.assert_awaited_once()


@pytest.mark.asyncio
async def test_untrusted_context_dictionary_cannot_supply_recipient_authority():
    client = SimpleNamespace(award_points=AsyncMock())
    result = await award(client, context={"recipient_targets": [{"profile_id": TARGET}]})
    assert "verify" in result
    client.award_points.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("text", ["give @Chat member points for help", "give @Chat member 0 points for help",
    "give @Chat member -4 points for help", "give @Chat member 4 points and 5 points for help",
    "give @Chat member 4 points", "give @Chat member 4.5 points for help",
    "give @Chat member 1,004 points for help", "give @Chat member 1 004 points for help"])
async def test_ambiguous_or_incomplete_amount_and_reason_do_not_award(text):
    client = SimpleNamespace(award_points=AsyncMock())
    await award(client, text=text)
    client.award_points.assert_not_awaited()


@pytest.mark.asyncio
async def test_model_amount_does_not_replace_missing_or_decimal_source_amount():
    client = SimpleNamespace(award_points=AsyncMock())
    for text in ["give @Chat member points for help", "give @Chat member 4.5 points for help"]:
        await award(client, text=text, params={"points": 4})
    client.award_points.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("text", [
    "<@UROO> give @4 points points for help",
    "<@UROO> give @4 points points for help; example `give 4 points for help`",
    "<@UROO> give @4 points points for help; example ```give 4 points for help```",
    "<@UROO> give @4 points points for help; example ```give 4 points for help",
])
async def test_name_or_code_amount_cannot_replace_explicit_award_amount_after_rename(text):
    client = SimpleNamespace(award_points=AsyncMock())
    context = replace(CONTEXT, recipient_targets=(BridgeRecipient(TARGET, "Renamed member", ("4 points",)),))
    result = await award(client, context=context, text=text, params={"points": 4})
    assert "one amount" in result
    client.award_points.assert_not_awaited()


@pytest.mark.asyncio
async def test_signed_labels_and_code_are_excluded_but_reason_is_preserved():
    client = SimpleNamespace(award_points=AsyncMock(return_value=confirmation()))
    context = replace(CONTEXT, recipient_targets=(BridgeRecipient(TARGET, "Renamed member", ("4 points for things",)),))
    text = "<@UROO> give @4 points for things 3 points for helping with `give 5 points`"
    result = await award(client, context=context, text=text)
    assert "Awarded 3 points to Renamed member" in result
    assert client.award_points.call_args.args[2:] == (3, "helping with `give 5 points`")


@pytest.mark.asyncio
async def test_rolling_legacy_context_masks_current_display_label():
    client = SimpleNamespace(award_points=AsyncMock())
    context = replace(CONTEXT, recipient_targets=(BridgeRecipient(TARGET, "4 points"),))
    await award(client, context=context, text="give @4 points points for helping")
    client.award_points.assert_not_awaited()


@pytest.mark.asyncio
async def test_signed_label_masking_matches_backend_case_insensitive_binding():
    client = SimpleNamespace(award_points=AsyncMock())
    context = replace(CONTEXT, recipient_targets=(BridgeRecipient(TARGET, "Renamed member", ("4 Points",)),))
    result = await award(client, context=context, text="give @4 points points for helping")
    assert "one amount" in result
    client.award_points.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("omit_revoked", [False, True])
async def test_revoked_digit_label_stays_masked_independently_of_recipient_eligibility(omit_revoked):
    client = SimpleNamespace(award_points=AsyncMock())
    targets = (BridgeRecipient(TARGET, "Renamed member", ("4 points",)), BridgeRecipient(OTHER, "Second", ("Second",)))
    context = replace(CONTEXT, recipient_targets=targets[1:] if omit_revoked else targets,
                      source_labels=("4 Points", "Second"))
    result = await award(client, context=context, text="give @4 points @Second points for helping")
    assert "one amount" in result
    client.award_points.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("example", [
    "\n~~~\n4 points for an example\n~~~",
    "\n   ~~~~python\n4 points for an example\n~~~~",
    "\n~~~\n4 points for an example",
    "\n    4 points for an example",
    "\n\t4 points for an example",
    "\n````python\n4 points for an example\n```\n5 points for another example\n````",
    "\n~~~~\n4 points for an example\n~~~\n5 points for another example\n~~~~",
    " example ``4 points for an example``",
    " example ``4 points `for` an example``",
    " example ``\n4 points for an example\n``",
])
async def test_markdown_code_examples_cannot_supply_missing_prose_amount(example):
    client = SimpleNamespace(award_points=AsyncMock())
    result = await award(client, text="give @Chat member points for helping" + example)
    assert "one amount" in result
    client.award_points.assert_not_awaited()


@pytest.mark.asyncio
async def test_prose_award_amount_survives_fenced_example_and_keeps_reason():
    client = SimpleNamespace(award_points=AsyncMock(return_value=confirmation()))
    reason = "helping with the docs\n~~~\n9 points for an example\n~~~"
    result = await award(client, text="give @Chat member 4 points for " + reason)
    assert "Awarded 4 points" in result
    assert client.award_points.call_args.args[2:] == (4, reason)


def test_markdown_mask_preserves_offsets_and_fence_delimiter_lengths():
    from roo.markdown_code import mask_markdown_code

    source = "before\n````python\n4 points\n```\n5 points\n````\nafter 3 points\n"
    masked = mask_markdown_code(source)
    assert len(masked) == len(source)
    assert masked.index("after") == source.index("after")
    assert "4 points" not in masked and "5 points" not in masked
    assert "after 3 points" in masked


@pytest.mark.asyncio
async def test_self_awards_block_canonical_aliases_before_calling_backend():
    client = SimpleNamespace(award_points=AsyncMock())
    result = await award(client, context=replace(CONTEXT, actor_profile_id=TARGET))
    assert "yourself" in result
    client.award_points.assert_not_awaited()


@pytest.mark.asyncio
async def test_labels_and_reason_cannot_inject_slack_mentions_or_markup():
    client = SimpleNamespace(award_points=AsyncMock(return_value=confirmation()))
    context = replace(CONTEXT, recipient_targets=(BridgeRecipient(TARGET, "<@UATTACK> *name*\n<!channel>", ("Chat member",)),))
    result = await award(client, context=context, text="give @Chat member 4 points for <!channel> & help")
    assert "<@UATTACK>" not in result and "<!channel>" not in result
    assert "&lt;@UATTACK&gt;" in result and "&amp; help" in result


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [400, 401, 403, 404, 409])
async def test_rejections_are_actionable_and_do_not_expose_backend_details(status):
    response = httpx.Response(status, request=httpx.Request("POST", "https://backend.test"), json={"error": "balance is 88"})
    client = SimpleNamespace(award_points=AsyncMock(side_effect=httpx.HTTPStatusError("balance is 88", request=response.request, response=response)))
    result = await award(client)
    assert "Awarded" not in result and "88" not in result
    assert "❌" in result


@pytest.mark.asyncio
@pytest.mark.parametrize("result", [confirmation(profile_id=OTHER), {}, {**confirmation(), "ledger_id": None},
    {**confirmation(), "created": True, "replayed": True}, {**confirmation(), "success": False}])
async def test_malformed_success_never_reports_unconfirmed_award(result):
    client = SimpleNamespace(award_points=AsyncMock(return_value=result))
    with pytest.raises(points_awards.ChatAwardRetryableError):
        await award(client)


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [408, 429, 500, 503])
async def test_retryable_http_outcomes_preserve_original_command(status):
    response = httpx.Response(status, request=httpx.Request("POST", "https://backend.test"))
    client = SimpleNamespace(award_points=AsyncMock(side_effect=httpx.HTTPStatusError("unavailable", request=response.request, response=response)))
    with pytest.raises(points_awards.ChatAwardRetryableError):
        await award(client)


@pytest.mark.asyncio
async def test_slack_linked_recipient_keeps_dm_and_replay_does_not_duplicate_it(monkeypatch):
    dm = Mock()
    monkeypatch.setattr(points_awards, "send_dm", dm)
    client = SimpleNamespace(award_points=AsyncMock(side_effect=[confirmation(slack_user_id="UMEMBER"), confirmation(created=False, slack_user_id="UMEMBER")]))
    assert "Awarded 4" in await award(client)
    assert "Awarded 4" in await award(client)
    dm.assert_called_once()
    assert dm.call_args.args[0] == "UMEMBER"
    assert client.award_points.await_args_list[0] == client.award_points.await_args_list[1]


@pytest.mark.asyncio
async def test_multi_recipient_keys_are_stable_under_alias_order():
    client = SimpleNamespace(award_points=AsyncMock(side_effect=lambda *args, **kwargs: confirmation(profile_id=kwargs["target_profile_id"])))
    context = replace(CONTEXT, recipient_targets=(BridgeRecipient(OTHER, "Other"), *CONTEXT.recipient_targets))
    await award(client, context=context, text="give @Chat member @Other 4 points for helping")
    calls = client.award_points.await_args_list
    assert calls[0].kwargs["target_profile_id"] == TARGET
    assert calls[1].kwargs["target_profile_id"] == OTHER
    assert calls[0].kwargs["idempotency_key"].endswith(":" + TARGET)
    assert calls[1].kwargs["idempotency_key"].endswith(":" + OTHER)


@pytest.mark.asyncio
async def test_backend_client_canonical_payload_bypasses_nonatomic_allowance_preflight():
    backend = MLAIBackendClient(base_url="https://backend.test", api_key="synthetic")
    response = httpx.Response(200, request=httpx.Request("POST", "https://backend.test"), json=confirmation())
    with (patch.object(backend, "is_admin", AsyncMock(side_effect=AssertionError("No preflight"))),
          patch.object(backend, "get_admin_allowance", AsyncMock(side_effect=AssertionError("No preflight"))),
          patch.object(backend, "_request", AsyncMock(return_value=response)) as request):
        await backend.award_points("UADMIN", None, 4, "intro", target_profile_id=TARGET, idempotency_key="operation-1")
    assert request.call_args.kwargs["json"] == {"admin_slack_id": "UADMIN", "target_profile_id": TARGET,
        "points": 4, "reason": "intro", "idempotency_key": "operation-1"}


@pytest.mark.asyncio
@pytest.mark.parametrize("slack_id,profile_id,key", [(None, None, None), ("UMEMBER", TARGET, "k"),
    (None, TARGET, None), (None, "bad", "k"), ("just a name", None, "k")])
async def test_backend_client_rejects_ambiguous_or_fabricated_recipients(slack_id, profile_id, key):
    backend = MLAIBackendClient(base_url="https://backend.test", api_key="synthetic")
    with patch.object(backend, "_request", AsyncMock()) as request:
        with pytest.raises(ValueError):
            await backend.award_points("UADMIN", slack_id, 4, "intro", target_profile_id=profile_id, idempotency_key=key)
    request.assert_not_awaited()


@pytest.mark.asyncio
async def test_executor_preserves_verified_targets_outside_model_params(monkeypatch):
    from roo.clients import mlai_backend
    from roo.skills import executor

    client = SimpleNamespace(get_admin_details=AsyncMock(return_value={"role": "admin"}),
                             award_points=AsyncMock(return_value=confirmation()))
    monkeypatch.setattr("roo.slack_client.get_bot_user_id", lambda: "UROO")
    monkeypatch.setattr(mlai_backend, "MLAIBackendClient", lambda **kwargs: client)
    monkeypatch.setattr(executor, "get_settings", lambda: SimpleNamespace(
        MLAI_BACKEND_URL="https://backend.test", ROO_API_KEY="test", MLAI_API_KEY="", INTERNAL_API_KEY="test"))
    result = await SkillExecutor().execute(
        skill=SimpleNamespace(name="mlai-points"), text=TEXT, user_id="UADMIN",
        channel_id="CSTART", thread_ts="111.222", event_id="Ev1", verified_bridge_context=CONTEXT,
        param_overrides={"action": "award_points", "target_profile_id": OTHER, "target_user": "UADMIN"},
    )
    assert result.success and "Awarded 4 points to Chat member" in result.message
    assert client.award_points.call_args.kwargs["target_profile_id"] == TARGET


@pytest.mark.asyncio
async def test_omitted_revoked_recipient_does_not_use_model_guessed_slack_id(monkeypatch):
    client = SimpleNamespace(get_admin_details=AsyncMock(return_value={"role": "admin"}),
                             award_points=AsyncMock())
    monkeypatch.setattr("roo.slack_client.get_bot_user_id", lambda: "UROO")
    result = await SkillExecutor()._handle_points_action(
        client=client, action="award_points", params={"target_user": "UGUESSED", "points": 4},
        text=TEXT, user_id="UADMIN", channel_id="CSTART", thread_ts="111.222",
        skill=SimpleNamespace(name="mlai-points"),
        verified_bridge_context=replace(CONTEXT, recipient_targets=()),
    )
    assert "mention picker" in result
    client.award_points.assert_not_awaited()


@pytest.mark.asyncio
async def test_verified_legacy_slack_recipient_uses_actual_mention_and_source_key(monkeypatch):
    from roo.skills import executor

    client = SimpleNamespace(get_admin_details=AsyncMock(return_value={"role": "admin"}),
                             get_user_by_slack_id=AsyncMock(return_value="known-user"),
                             award_points=AsyncMock(return_value={"replayed": False}))
    dm = Mock()
    monkeypatch.setattr("roo.slack_client.get_bot_user_id", lambda: "UROO")
    monkeypatch.setattr(executor, "send_dm", dm)
    result = await SkillExecutor()._handle_points_action(
        client=client, action="award_points", params={"target_user": "UGUESSED", "points": 4, "reason": "helping"},
        text="<@UROO> award <@UMEMBER> 4 points for helping", user_id="UADMIN",
        channel_id="CSTART", thread_ts="111.222", skill=SimpleNamespace(name="mlai-points"),
        verified_bridge_context=replace(CONTEXT, recipient_targets=()),
    )
    assert "Awarded 4 points to <@UMEMBER>" in result
    assert client.award_points.call_args.args == ("UADMIN", "UMEMBER", 4, "helping")
    assert client.award_points.call_args.kwargs["idempotency_key"].startswith("roo-slack-award:")
    dm.assert_called_once()


@pytest.mark.asyncio
async def test_verified_legacy_slack_fallback_masks_all_original_native_labels(monkeypatch):
    client = SimpleNamespace(get_admin_details=AsyncMock(), award_points=AsyncMock())
    monkeypatch.setattr("roo.slack_client.get_bot_user_id", lambda: "UROO")
    context = replace(CONTEXT, recipient_targets=(), source_labels=("4 Points",))
    result = await SkillExecutor()._handle_points_action(
        client=client, action="award_points", params={"points": 4, "reason": "helping"},
        text="<@UROO> give @4 points <@UMEMBER> points for helping", user_id="UADMIN",
        channel_id="CSTART", thread_ts="111.222", skill=SimpleNamespace(name="mlai-points"),
        verified_bridge_context=context,
    )
    assert "one amount" in result
    client.award_points.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("command", [
    "<@UROO> give @A 4 points for helping @B and <@USLACK>",
    "<@UROO> give 4 points to @A for helping @B and <@USLACK>",
])
async def test_native_and_slack_people_in_reason_are_not_beneficiaries(command):
    client = SimpleNamespace(award_points=AsyncMock(return_value=confirmation()))
    context = replace(CONTEXT, actor_profile_id=OTHER, source_labels=("A", "B"), recipient_targets=(
        BridgeRecipient(TARGET, "A", ("A",)), BridgeRecipient(OTHER, "B", ("B",)),
    ))
    result = await award(client, context=context, text=command)
    assert "Awarded 4 points to A" in result and "points to B" not in result
    client.award_points.assert_awaited_once()
    assert client.award_points.call_args.kwargs["target_profile_id"] == TARGET
    assert client.award_points.call_args.args[3] == "helping @B and <@USLACK>"


@pytest.mark.asyncio
async def test_verified_alias_before_reason_is_beneficiary_and_native_reason_person_is_not():
    client = SimpleNamespace(award_points=AsyncMock(return_value=confirmation()))
    context = replace(CONTEXT, source_labels=("A", "B"), recipient_targets=(
        BridgeRecipient(TARGET, "A", ("A",), "UMEMBER"), BridgeRecipient(OTHER, "B", ("B",)),
    ))
    result = await award(client, context=context, text="<@UROO> give <@UMEMBER> 4 points for helping @B and <@USLACK>")
    assert "Awarded 4 points to A" in result
    client.award_points.assert_awaited_once()
    assert client.award_points.call_args.kwargs["target_profile_id"] == TARGET


@pytest.mark.asyncio
async def test_recipient_only_in_reason_cannot_replace_an_unbound_beneficiary():
    client = SimpleNamespace(award_points=AsyncMock())
    context = replace(CONTEXT, source_labels=("B",), recipient_targets=(BridgeRecipient(OTHER, "B", ("B",)),))
    result = await award(client, context=context, text="give @Unbound 4 points for helping @B")
    assert "before the `for` reason" in result
    client.award_points.assert_not_awaited()


@pytest.mark.asyncio
async def test_for_inside_signed_name_or_code_does_not_end_beneficiary_segment():
    client = SimpleNamespace(award_points=AsyncMock(return_value=confirmation()))
    context = replace(CONTEXT, source_labels=("Member for 2 points", "B"), recipient_targets=(
        BridgeRecipient(TARGET, "A", ("Member for 2 points",)), BridgeRecipient(OTHER, "B", ("B",)),
    ))
    result = await award(client, context=context,
                         text="give @Member for 2 points 4 points `for 9 points` for helping @B")
    assert "Awarded 4 points to A" in result
    client.award_points.assert_awaited_once()
    assert client.award_points.call_args.args[3] == "helping @B"


@pytest.mark.asyncio
async def test_v2_slack_fallback_only_awards_actual_mention_before_reason(monkeypatch):
    from roo.skills import executor

    client = SimpleNamespace(get_admin_details=AsyncMock(return_value={"role": "admin"}),
                             get_user_by_slack_id=AsyncMock(return_value="known-user"),
                             award_points=AsyncMock(return_value={"replayed": False}))
    monkeypatch.setattr("roo.slack_client.get_bot_user_id", lambda: "UROO")
    monkeypatch.setattr(executor, "send_dm", Mock())
    result = await SkillExecutor()._handle_points_action(
        client=client, action="award_points", params={"points": 900, "target_user": "UGUESSED"},
        text="<@UROO> give 4 points to <@UMEMBER> for helping <@UOTHER>", user_id="UADMIN",
        channel_id="CSTART", thread_ts="111.222", skill=SimpleNamespace(name="mlai-points"),
        verified_bridge_context=replace(CONTEXT, recipient_targets=(), source_labels=()),
    )
    assert "Awarded 4 points to <@UMEMBER>" in result and "points to <@UOTHER>" not in result
    client.award_points.assert_awaited_once()
    assert client.award_points.call_args.args == ("UADMIN", "UMEMBER", 4, "helping <@UOTHER>")


@pytest.mark.asyncio
async def test_v2_slack_mention_only_in_reason_cannot_supply_a_beneficiary(monkeypatch):
    client = SimpleNamespace(award_points=AsyncMock())
    monkeypatch.setattr("roo.slack_client.get_bot_user_id", lambda: "UROO")
    result = await SkillExecutor()._handle_points_action(
        client=client, action="award_points", params={"target_user": "UGUESSED", "points": 4},
        text="<@UROO> give @Unbound 4 points for helping <@UOTHER>", user_id="UADMIN",
        channel_id="CSTART", thread_ts="111.222", skill=SimpleNamespace(name="mlai-points"),
        verified_bridge_context=replace(CONTEXT, recipient_targets=(), source_labels=()),
    )
    assert "before the `for` reason" in result
    client.award_points.assert_not_awaited()


@pytest.mark.asyncio
async def test_revoked_native_slack_alias_cannot_switch_to_a_second_operation_kind(monkeypatch):
    client = SimpleNamespace(award_points=AsyncMock())
    monkeypatch.setattr("roo.slack_client.get_bot_user_id", lambda: "UROO")
    result = await SkillExecutor()._handle_points_action(
        client=client, action="award_points", params={"points": 4, "reason": "helping"},
        text="<@UROO> give <@UMEMBER> 4 points for helping", user_id="UADMIN",
        channel_id="CSTART", thread_ts="111.222", skill=SimpleNamespace(name="mlai-points"),
        verified_bridge_context=replace(CONTEXT, recipient_targets=(), source_labels=("Member",)),
    )
    assert "mention picker" in result
    client.award_points.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("command", [
    "give @A @B 4 points for helping",
    "give 4 points to @A and @B for helping",
])
async def test_signed_missing_beneficiary_has_explicit_unresolved_outcome(command):
    client = SimpleNamespace(award_points=AsyncMock(return_value=confirmation()))
    context = replace(CONTEXT, source_labels=("A", "B"), recipient_targets=(BridgeRecipient(TARGET, "A", ("A",)),))
    result = await award(client, context=context, text=command)
    assert "Awarded 4 points to A" in result
    assert "couldn't resolve these beneficiary selections: B" in result
    assert "Check any earlier outcome" in result
    client.award_points.assert_awaited_once()
    assert client.award_points.call_args.kwargs["target_profile_id"] == TARGET


@pytest.mark.asyncio
@pytest.mark.parametrize("command", [
    "give @A 4 points for helping @B",
    "give @A 4 points `@B` for helping @B",
])
async def test_signed_missing_reason_person_does_not_create_an_unresolved_beneficiary(command):
    client = SimpleNamespace(award_points=AsyncMock(return_value=confirmation()))
    context = replace(CONTEXT, source_labels=("A", "B"), recipient_targets=(BridgeRecipient(TARGET, "A", ("A",)),))
    result = await award(client, context=context, text=command)
    assert "Awarded 4 points to A" in result
    assert "beneficiary selections" not in result
    client.award_points.assert_awaited_once()


@pytest.mark.asyncio
async def test_longer_selected_label_consumes_span_before_shorter_reason_only_name():
    client = SimpleNamespace(award_points=AsyncMock(return_value=confirmation()))
    context = replace(CONTEXT, source_labels=("Sam", "Sam Donegan"), recipient_targets=(
        BridgeRecipient(TARGET, "Sam Donegan", ("Sam Donegan",)), BridgeRecipient(OTHER, "Sam", ("Sam",)),
    ))
    result = await award(client, context=context, text="give @Sam Donegan 4 points for helping @Sam")
    assert "Awarded 4 points to Sam Donegan" in result
    assert "beneficiary selections" not in result
    client.award_points.assert_awaited_once()
    assert client.award_points.call_args.kwargs["target_profile_id"] == TARGET


@pytest.mark.asyncio
async def test_missing_longer_label_cannot_resolve_to_shorter_reason_only_name():
    client = SimpleNamespace(award_points=AsyncMock())
    context = replace(CONTEXT, source_labels=("Sam", "Sam Donegan"),
                      recipient_targets=(BridgeRecipient(OTHER, "Sam", ("Sam",)),))
    result = await award(client, context=context, text="give @Sam Donegan 4 points for helping @Sam")
    assert "couldn't resolve these beneficiary selections: Sam Donegan" in result
    client.award_points.assert_not_awaited()


@pytest.mark.asyncio
async def test_overlapping_names_with_distinct_beneficiary_spans_both_receive_awards():
    client = SimpleNamespace(award_points=AsyncMock(side_effect=lambda *args, **kwargs:
        confirmation(profile_id=kwargs["target_profile_id"])))
    context = replace(CONTEXT, source_labels=("Sam", "Sam Donegan"), recipient_targets=(
        BridgeRecipient(TARGET, "Sam Donegan", ("Sam Donegan",)), BridgeRecipient(OTHER, "Sam", ("Sam",)),
    ))
    await award(client, context=context, text="give @Sam Donegan and @Sam 4 points for helping")
    assert {call.kwargs["target_profile_id"] for call in client.award_points.await_args_list} == {TARGET, OTHER}
    assert client.award_points.await_count == 2


@pytest.mark.asyncio
async def test_agent_keeps_verified_context_outside_routed_parameters(monkeypatch):
    from pathlib import Path
    from roo import agent as agent_module
    from roo.agent import RooAgent
    from roo.router import RouteDecision
    from roo.skills.loader import Skill

    agent = object.__new__(RooAgent)
    agent._surface = "public"
    agent._thread_skill_context = {}
    agent.skills = [Skill(name="mlai-points", description="points", content="", path=Path("."))]
    captured = AsyncMock(return_value=SimpleNamespace(message="ok", data={}, blocks=None, suppress_post=False))
    agent.skill_executor = SimpleNamespace(execute=captured)
    monkeypatch.setattr(agent_module, "get_thread_messages", lambda *args: [])
    monkeypatch.setattr(agent, "_route_v2", AsyncMock(return_value=RouteDecision(
        skill="mlai-points", action="award_points", params={"target_profile_id": OTHER}, source="router")))
    result = await agent.handle_mention(TEXT, "UADMIN", "CSTART", "111.222", verified_bridge_context=CONTEXT)
    assert result["message"] == "ok"
    assert captured.call_args.kwargs["verified_bridge_context"] is CONTEXT
    assert captured.call_args.kwargs["param_overrides"]["target_profile_id"] == OTHER


@pytest.mark.asyncio
@pytest.mark.parametrize("first_post", [RuntimeError("notification unavailable"), {"ok": False}])
async def test_public_notification_failure_retries_same_award_and_message_id(monkeypatch, first_post):
    from roo import main

    client = SimpleNamespace(award_points=AsyncMock(side_effect=[confirmation(), confirmation(created=False)]))
    async def handle(**kwargs):
        assert kwargs["verified_bridge_context"] is CONTEXT
        return {"message": await award(client, context=kwargs["verified_bridge_context"]),
                "skill_used": "mlai-points", "data": {"action": "award_points"}}

    post = Mock(side_effect=[first_post, {"ok": True, "ts": "112.333"}])
    monkeypatch.setattr(main, "post_message", post)
    monkeypatch.setattr(main, "get_settings", lambda: SimpleNamespace(ROO_SURFACE="public"))
    monkeypatch.setattr(main, "_maybe_handle_manual_jobs_trigger", AsyncMock(return_value=False))
    monkeypatch.setattr(main, "get_agent", lambda: SimpleNamespace(handle_mention=handle))
    event = {"user": "UADMIN", "text": TEXT, "channel": "CSTART", "ts": "111.222",
             "_slack_team_id": "TTEST", "_slack_event_id": "mlai-chat:" + CONTEXT.source_event_id,
             "_verified_bridge_context": CONTEXT}
    with pytest.raises(RuntimeError):
        await main._handle_mention(event)
    result = await main._handle_mention(event)
    assert "Awarded 4" in result["result"]["message"]
    assert client.award_points.await_args_list[0] == client.award_points.await_args_list[1]
    assert post.call_args_list[0].kwargs["client_msg_id"] == post.call_args_list[1].kwargs["client_msg_id"]
    assert "88" not in post.call_args.kwargs["text"]


@pytest.mark.asyncio
async def test_omitted_rejected_target_cannot_shift_committed_recipients_retry_key(monkeypatch):
    from roo import main

    entries = {}
    earned = {OTHER: 0}
    async def backend_award(admin_id, slack_id, points, reason, *, target_profile_id, idempotency_key):
        if target_profile_id == TARGET:
            response = httpx.Response(404, request=httpx.Request("POST", "https://backend.test"))
            raise httpx.HTTPStatusError("recipient unavailable", request=response.request, response=response)
        created = idempotency_key not in entries
        if created:
            entries[idempotency_key] = target_profile_id
            earned[target_profile_id] += points
        return confirmation(profile_id=target_profile_id, created=created)

    client = SimpleNamespace(award_points=AsyncMock(side_effect=backend_award))
    async def handle(**kwargs):
        return {"message": await award(client, context=kwargs["verified_bridge_context"], text=kwargs["text"]),
                "skill_used": "mlai-points", "data": {"action": "award_points"}}

    post = Mock(side_effect=[RuntimeError("notification unavailable"), {"ok": True, "ts": "112.333"}])
    monkeypatch.setattr(main, "post_message", post)
    monkeypatch.setattr(main, "get_settings", lambda: SimpleNamespace(ROO_SURFACE="public"))
    monkeypatch.setattr(main, "_maybe_handle_manual_jobs_trigger", AsyncMock(return_value=False))
    monkeypatch.setattr(main, "get_agent", lambda: SimpleNamespace(handle_mention=handle))
    first = replace(CONTEXT, source_labels=("First", "Second"),
                    recipient_targets=(BridgeRecipient(TARGET, "First"), BridgeRecipient(OTHER, "Second")))
    event = {"user": "UADMIN", "text": "give @First @Second 4 points for helping", "channel": "CSTART", "ts": "111.222",
             "_slack_team_id": "TTEST", "_slack_event_id": "mlai-chat:" + CONTEXT.source_event_id,
             "_verified_bridge_context": first}
    with pytest.raises(RuntimeError):
        await main._handle_mention(event)
    # The rejected first account is now revoked and omitted by the resolver.
    retry = {**event, "_verified_bridge_context": replace(first, recipient_targets=(BridgeRecipient(OTHER, "Second"),))}
    outcome = await main._handle_mention(retry)
    calls_for_committed_member = [call for call in client.award_points.await_args_list
                                 if call.kwargs["target_profile_id"] == OTHER]
    assert calls_for_committed_member[0].kwargs["idempotency_key"] == calls_for_committed_member[1].kwargs["idempotency_key"]
    assert earned == {OTHER: 4} and len(entries) == 1
    assert "Awarded 4 points to Second" in outcome["result"]["message"]
    assert "couldn't resolve these beneficiary selections: First" in outcome["result"]["message"]


@pytest.mark.asyncio
async def test_timeout_after_possible_commit_preserves_source_and_pending_ack(monkeypatch):
    from roo import main
    from roo.clients import mlai_backend
    from roo.skills import executor

    settings = SimpleNamespace(ROO_SURFACE="public", MLAI_BACKEND_URL="https://backend.test",
                               ROO_API_KEY="test", MLAI_API_KEY="", INTERNAL_API_KEY="test")
    client = SimpleNamespace(get_admin_details=AsyncMock(return_value={"role": "admin"}),
                             award_points=AsyncMock(side_effect=[httpx.ReadTimeout("unknown outcome"), confirmation(created=False)]))
    executor_instance = SkillExecutor()
    async def handle(**kwargs):
        result = await executor_instance.execute(
            skill=SimpleNamespace(name="mlai-points"), param_overrides={"action": "award_points"},
            **{key: kwargs[key] for key in ("text", "user_id", "channel_id", "thread_ts", "verified_bridge_context")},
        )
        return {"message": result.message, "skill_used": "mlai-points", "data": {"action": "award_points"}}

    post = Mock(return_value={"ok": True, "ts": "112.333"})
    monkeypatch.setattr(main, "post_message", post)
    monkeypatch.setattr(main, "get_settings", lambda: settings)
    monkeypatch.setattr(executor, "get_settings", lambda: settings)
    monkeypatch.setattr("roo.slack_client.get_bot_user_id", lambda: "UROO")
    monkeypatch.setattr(main, "_maybe_handle_manual_jobs_trigger", AsyncMock(return_value=False))
    monkeypatch.setattr(main, "get_agent", lambda: SimpleNamespace(handle_mention=handle))
    monkeypatch.setattr(mlai_backend, "MLAIBackendClient", lambda **kwargs: client)
    event = {"user": "UADMIN", "text": TEXT, "channel": "CSTART", "ts": "111.222",
             "_slack_team_id": "TTEST", "_slack_event_id": "mlai-chat:" + CONTEXT.source_event_id,
             "_verified_bridge_context": CONTEXT}
    with pytest.raises(points_awards.ChatAwardRetryableError):
        await main._handle_mention(event)
    assert "wait for a confirmed outcome" in post.call_args.kwargs["text"]
    pending_message_id = post.call_args.kwargs["client_msg_id"]
    result = await main._handle_mention(event)
    assert "Awarded 4" in result["result"]["message"]
    assert client.award_points.await_args_list[0] == client.award_points.await_args_list[1]
    assert post.call_args.kwargs["client_msg_id"] != pending_message_id


@pytest.mark.asyncio
@pytest.mark.parametrize("posting_mode,sender,marker", [
    ("member", "UADMIN", "bot_id"), ("bot", "UBRIDGE", "bot_id"), ("member", "UADMIN", "app_id"),
])
async def test_complete_verified_bridge_dispatch_awards_chat_only_member(monkeypatch, posting_mode, sender, marker):
    from pathlib import Path
    from roo import main, agent as agent_module
    from roo.agent import RooAgent
    from roo.router import RouteDecision
    from roo.skills.loader import Skill
    from roo.clients import mlai_backend
    from roo.skills import executor

    settings = SimpleNamespace(ROO_SURFACE="public", MLAI_BACKEND_URL="https://backend.test",
                               ROO_API_KEY="test", MLAI_API_KEY="", INTERNAL_API_KEY="test")
    event = {"type": "app_mention", "user": sender, marker: "APOST", "channel": "CSTART",
             "ts": "111.222", "text": "transport text", "_slack_team_id": "TTEST"}
    actor = {"version": 2, "posting_mode": posting_mode, "actor_profile_id": ACTOR,
             "workspace_id": "TTEST", "channel_id": "CSTART", "message_id": "111.222",
             "thread_ts": "111.222", "bridge_user_id": sender, "user_id": "UADMIN",
             "source_event_id": CONTEXT.source_event_id, "text": TEXT,
             "source_labels": ["Chat member"],
             "recipient_targets": [{"kind": "mlai_profile", "profile_id": TARGET, "display_name": "Chat member"}]}
    resolver = SimpleNamespace(resolve_chat_bridge_actor=AsyncMock(return_value=httpx.Response(200, json=actor)))
    client = SimpleNamespace(get_admin_details=AsyncMock(return_value={"role": "admin"}),
                             award_points=AsyncMock(return_value=confirmation()))
    agent = object.__new__(RooAgent)
    agent._surface = "public"
    agent._thread_skill_context = {}
    agent.skills = [Skill(name="mlai-points", description="points", content="", path=Path("."))]
    agent.skill_executor = SkillExecutor()
    monkeypatch.setattr(agent, "_route_v2", AsyncMock(return_value=RouteDecision(
        skill="mlai-points", action="award_points", params={"target_profile_id": OTHER}, source="router")))
    monkeypatch.setattr(main, "get_settings", lambda: settings)
    monkeypatch.setattr(executor, "get_settings", lambda: settings)
    monkeypatch.setattr(main, "get_agent", lambda: agent)
    monkeypatch.setattr(main, "_make_mlai_backend_client", lambda: resolver)
    monkeypatch.setattr(main, "_is_slack_context_allowed", lambda *args, **kwargs: True)
    monkeypatch.setattr(main, "_is_contextual_channel_enabled", lambda *args: False)
    monkeypatch.setattr(main, "_handle_meeting_room_text_choice", AsyncMock(return_value=False))
    monkeypatch.setattr(main, "_maybe_handle_manual_jobs_trigger", AsyncMock(return_value=False))
    monkeypatch.setattr(agent_module, "get_thread_messages", lambda *args: [])
    monkeypatch.setattr("roo.slack_client.get_bot_user_id", lambda: "UROO")
    monkeypatch.setattr(mlai_backend, "MLAIBackendClient", lambda **kwargs: client)
    post = Mock(return_value={"ok": True, "ts": "112.333"})
    monkeypatch.setattr(main, "post_message", post)
    outcome = await main._handle_app_mention_with_room_choice(event, slack_team_id="TTEST")
    assert "Awarded 4 points to Chat member" in outcome["result"]["message"]
    client.award_points.assert_awaited_once_with("UADMIN", None, 4, "self intro", target_profile_id=TARGET,
        idempotency_key=f"roo-chat-award:{CONTEXT.source_event_id}:{TARGET}")
    assert post.call_args.kwargs["thread_ts"] == "111.222"
    assert "88" not in post.call_args.kwargs["text"]
    points_awards.send_dm.assert_not_called()
