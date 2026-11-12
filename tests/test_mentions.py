"""Nombre visible del bot para las instrucciones de uso."""

from types import SimpleNamespace as NS

from mentions import bot_display_name

BOT = "28:bot-id"


def activity(entities, recipient_name="bot-teams-adapter-dev"):
    return NS(recipient=NS(id=BOT, name=recipient_name), entities=entities)


def mention(account_id, name, text=None):
    return NS(type="mention", mentioned=NS(id=account_id, name=name), text=text)


def test_uses_mention_text_of_the_bot():
    act = activity([
        mention("29:otra-persona", "Valentina", "<at>Valentina</at>"),
        mention(BOT, "bot-teams-adapter-dev", "<at>Agentes ICP DEV</at>"),
    ])
    assert bot_display_name(act) == "Agentes ICP DEV"


def test_uses_mentioned_name_when_text_is_missing():
    assert bot_display_name(activity([mention(BOT, "Agentes ICP DEV")])) == "Agentes ICP DEV"


def test_dict_entities_are_supported():
    act = activity([{"type": "mention", "mentioned": {"id": BOT, "name": "x"}, "text": "<AT>Agentes ICP</AT>"}])
    assert bot_display_name(act) == "Agentes ICP"


def test_fallbacks_without_bot_mention():
    act = activity([mention("29:otra", "Ana", "<at>Ana</at>"), NS(type="clientInfo")])
    assert bot_display_name(act, "Agentes ICP DEV") == "Agentes ICP DEV"
    assert bot_display_name(act) == "bot-teams-adapter-dev"
    assert bot_display_name(activity(None)) == "bot-teams-adapter-dev"
