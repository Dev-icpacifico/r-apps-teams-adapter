import asyncio

import httpx
import pytest

from conftest import G_BOD, G_FIN, G_LIQUID
from graph_groups import GraphGroupChecker, GroupLookupError

USER = "eaaceec2-3142-4ef3-b995-4905cfa484b0"


def run(coro):
    return asyncio.run(coro)


class FakeCredential:
    class Token:
        token = "tok"

    async def get_token(self, scope):
        return self.Token()


def checker(handler):
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return GraphGroupChecker(FakeCredential(), http_client=client)


def graph(memberships, status=None):
    calls = []

    def handler(request: httpx.Request):
        calls.append(request)
        group = request.url.path.split("/groups/")[1].split("/")[0]
        if status and group in status:
            return httpx.Response(status[group], json={"error": {}})
        members = [{"id": USER}] if group in memberships else []
        return httpx.Response(200, json={"@odata.count": len(members), "value": members})

    return handler, calls


def test_returns_only_groups_with_membership_using_group_side_query():
    handler, calls = graph({G_LIQUID})
    result = run(checker(handler).member_groups(USER, [G_LIQUID, G_FIN]))
    assert result == {G_LIQUID}
    request = calls[0]
    assert "/transitiveMembers/microsoft.graph.user" in request.url.path
    assert request.headers["ConsistencyLevel"] == "eventual"
    assert f"id eq '{USER}'" in request.url.params["$filter"]


def test_results_are_cached():
    handler, calls = graph({G_FIN, G_BOD})
    c = checker(handler)
    assert run(c.member_groups(USER, [G_FIN, G_BOD])) == {G_FIN, G_BOD}
    run(c.member_groups(USER, [G_BOD, G_FIN]))
    assert len(calls) == 2


def test_missing_group_does_not_grant_membership():
    handler, _ = graph({G_LIQUID}, status={G_FIN: 404})
    assert run(checker(handler).member_groups(USER, [G_LIQUID, G_FIN])) == {G_LIQUID}


def test_graph_error_raises_lookup_error():
    handler, _ = graph(set(), status={G_FIN: 403})
    with pytest.raises(GroupLookupError, match="403"):
        run(checker(handler).member_groups(USER, [G_LIQUID, G_FIN]))


def test_invalid_user_id_is_rejected_without_calling_graph():
    handler, calls = graph({G_LIQUID})
    with pytest.raises(GroupLookupError):
        run(checker(handler).member_groups("x' or 1 eq 1", [G_LIQUID]))
    assert calls == []
