"""PocketBase multi-match and LEFT JOIN semantics in filters and rules (WS-A3).

The scenarios are the ``multimatch`` and ``buma_rules`` groups of
``tests/pocketbase_parity/relation_multiplicity_probe.py``: the same requests
were sent to the official PocketBase 0.22.34 binary, whose answers are the
expected values below.  ``buma_rules`` reproduces the BUMA ``users``
create/update rules of the frozen schema export::

    @collection.users.id = @request.auth.id
      || @request.auth.role.permission.isAdmin = true
      || @request.auth.role.permission.user.create = true   (user.update)

and the ``buma_pdv`` delete rule ``@request.auth.id != '' && (client.id = ''
&& agent.id = '')``.
"""

from __future__ import annotations

import copy
from collections.abc import AsyncIterator
from typing import Any

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import text

from ppbase.db.engine import get_engine
from tests.pocketbase_parity import relation_multiplicity_probe as probe

pytestmark = pytest.mark.asyncio

_EVERY_OWNER = ["alpha", "beta", "empty", "gamma", "orphan"]

# PocketBase 0.22.34 answers (probe ``multimatch`` group).
POCKETBASE_MULTIMATCH: dict[str, Any] = {
    "back_id_eq_empty": ["empty", "orphan"],
    "back_title_any_eq": ["alpha"],
    "back_title_eq": [],
    "back_title_neq": ["beta", "gamma"],
    "coll_alias_any": _EVERY_OWNER,
    "coll_empty_any_eq": [],
    "coll_empty_eq_empty": _EVERY_OWNER,
    "coll_empty_neq": [],
    "coll_n_any_gt_2": _EVERY_OWNER,
    "coll_n_gt_0": _EVERY_OWNER,
    "coll_n_gt_1": [],
    "coll_name_any_eq": _EVERY_OWNER,
    "coll_name_eq": [],
    "coll_name_neq_zzz": _EVERY_OWNER,
    "coll_path_any_eq": ["l1", "l2", "l3", "l4", "l5"],
    "coll_path_eq": [],
    "coll_self_any_title": _EVERY_OWNER,
    "coll_self_title": [],
    "hop_one_name_eq_alpha": ["l1", "l2"],
    "hop_one_name_eq_empty": ["l5"],
    "hop_rels_n_gt_1": ["l4"],
    "hop_rels_name_any_eq": ["l1", "l2", "l3"],
    "hop_rels_name_eq": ["l1", "l2"],
    "one_id_eq_empty": ["empty", "orphan"],
    "one_id_neq_empty": ["alpha", "beta", "gamma"],
    "one_n_gt_0": ["alpha", "beta", "gamma"],
    "one_name_eq_empty": ["empty", "orphan"],
    "one_name_eq_null": ["empty", "orphan"],
    "one_name_like": ["alpha", "beta", "gamma"],
    "one_name_neq_beta": ["alpha", "empty", "gamma", "orphan"],
    "one_name_neq_empty": ["alpha", "beta", "gamma"],
    "one_name_not_like": ["alpha", "beta", "gamma"],
    "one_name_vs_title": ["alpha", "beta", "gamma"],
    "rels_any_vs_title": ["alpha", "beta", "gamma"],
    "rels_id_eq_empty": ["empty"],
    "rels_id_eq_t1": ["alpha", "orphan"],
    "rels_id_neq_empty": ["alpha", "beta", "gamma"],
    "rels_n_any_gt_1": ["beta", "gamma"],
    "rels_n_gt_1": ["gamma"],
    "rels_n_gte_1": ["alpha", "beta", "gamma"],
    "rels_name_any_eq": ["alpha", "beta", "orphan"],
    "rels_name_any_neq": ["beta", "empty", "gamma", "orphan"],
    "rels_name_eq": ["alpha", "orphan"],
    "rels_name_like": ["alpha", "beta", "gamma"],
    "rels_name_neq": ["gamma"],
    "rels_name_not_like": ["alpha", "beta", "gamma"],
    "rels_or_one": ["alpha", "beta", "orphan"],
    "rels_same_row": ["alpha", "orphan"],
    "rels_split_rows": [],
    "rels_vs_title": ["alpha", "gamma"],
    "uback_id_eq_empty": ["empty", "gamma", "orphan"],
    "uback_title_eq": ["alpha"],
    "uback_title_neq": ["beta", "empty", "gamma", "orphan"],
}

_CREATE_DENIED = {"data": {}, "message": "Failed to create record.", "status": 400}
_NOT_FOUND = {
    "data": {},
    "message": "The requested resource wasn't found.",
    "status": 404,
}

# PocketBase 0.22.34 answers (probe ``buma_rules`` group).
POCKETBASE_BUMA_RULES: dict[str, Any] = {
    # One account: the caller is the only users row.
    "solo_update_self": 200,
    "solo_create": _CREATE_DENIED,  # the created row makes two rows
    "solo_count_after_create": 1,
    "anonymous_create_one_row": _CREATE_DENIED,
    # Two accounts and more.
    "plain_create": _CREATE_DENIED,
    "plain_update_self": _NOT_FOUND,
    "plain_update_other": _NOT_FOUND,
    "plain_escalate_self": _NOT_FOUND,
    "plain_escalate_other": _NOT_FOUND,
    "admin_role_create": 200,
    "admin_role_update_other": 200,
    "admin_role_update_self": 200,
    "creator_create": 200,
    "creator_update_other": _NOT_FOUND,
    "updater_update_other": 200,
    "updater_create": _CREATE_DENIED,
    "anonymous_create": _CREATE_DENIED,
    "anonymous_update": _NOT_FOUND,
    "superuser_create": 200,
    "superuser_update": 200,
    "created_in_matrix": 3,
    "u2_after": {"name": "u2g", "role": "role_plain"},
    "solo_after": {"name": "solo2", "role": "role_plain"},
    # buma_pdv.deleteRule: a missing or orphan client/agent is a NULL leaf.
    "pdv_filter": ["anon_none", "none", "orphan_agent", "orphan_both", "orphan_client"],
    "pdv_filter_rule": {"status": 403},  # @request. in a guest/user filter
    "pdv_delete_anonymous": 404,
    "pdv_delete_none": 204,
    "pdv_delete_client": 404,
    "pdv_delete_agent": 404,
    "pdv_delete_both": 404,
    "pdv_delete_orphan_client": 204,
    "pdv_delete_orphan_agent": 204,
    "pdv_delete_orphan_both": 204,
}

# Known envelope difference (not a rule difference): a create denied by the
# create rule answers ``data: {}`` in PocketBase 0.22.34 and
# ``data: {"rule": {"code": "validation_rule_failed", ...}}`` in PPBase.
_CREATE_RULE_DATA = {"rule": "validation_rule_failed"}

_PROBE_COLLECTIONS = (
    "pm_ulinks",
    "pm_links",
    "pm_owners",
    "pm_empty",
    "pm_targets",
    "pb_pdv",
    "pb_agents",
    "pb_customers",
    "pb_users",
    "pb_roles",
)


async def _inject(table: str, record_id: str, field: str, value: Any) -> None:
    """Store a value as is (an orphan id, like an imported 0.22 record)."""
    async with get_engine().begin() as connection:
        await connection.execute(
            text(f'UPDATE "{table}" SET "{field}" = :value WHERE id = :id'),
            {"value": value, "id": record_id},
        )


@pytest_asyncio.fixture
async def admin(
    app_client: AsyncClient, admin_token: str
) -> AsyncIterator[dict[str, str]]:
    headers = {"Authorization": admin_token}
    try:
        yield headers
    finally:
        for name in _PROBE_COLLECTIONS:
            await app_client.delete(f"/api/collections/{name}", headers=headers)


async def test_multi_match_filters_match_pocketbase(
    app_client: AsyncClient,
    admin: dict[str, str],
) -> None:
    result = await probe.multimatch_scenarios(
        app_client, admin, probe.Labels(), _inject
    )
    assert result == POCKETBASE_MULTIMATCH


async def test_buma_users_and_pdv_rules_match_pocketbase(
    app_client: AsyncClient,
    admin: dict[str, str],
) -> None:
    with probe._auth_path_context() as auth_paths:
        result = await probe.buma_rule_scenarios(
            app_client, admin, probe.Labels(), _inject, auth_paths
        )

    expected = copy.deepcopy(POCKETBASE_BUMA_RULES)
    for value in expected.values():
        if value == _CREATE_DENIED:
            value["data"] = dict(_CREATE_RULE_DATA)
    assert result == expected


async def test_denied_writes_leave_no_trace(
    app_client: AsyncClient,
    admin: dict[str, str],
) -> None:
    """A denied create is rolled back; a denied update changes nothing."""
    with probe._auth_path_context() as auth_paths:
        await probe.buma_rule_scenarios(
            app_client, admin, probe.Labels(), _inject, auth_paths
        )
    response = await app_client.get(
        "/api/collections/pb_users/records",
        params={"perPage": 200, "sort": "name"},
        headers=admin,
    )
    assert response.status_code == 200, response.text
    names = [item["name"] for item in response.json()["items"]]
    # solo/u2/boss/creator/updater, then the three creates PocketBase allows
    # (new4 by boss, new5 by creator, new8 by a superuser); the denied
    # creates (new1-3, new6, new7) left nothing.
    assert names == [
        "boss2",
        "creator",
        "new4",
        "new5",
        "new8",
        "solo2",
        "u2g",
        "updater",
    ]
