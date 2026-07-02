"""g-P0 unit tests: connection groups, env-sets, and resolution (no network)."""

from __future__ import annotations

import pytest

from quarry import core, workspace

CONNS = """
[blog]
url = "postgresql://u@127.0.0.1:5432/blog"
group = "acme"
env = "prod"

[shop_dev]
url = "postgresql://u@dev-host/shop"
group = "shop"
db = "shop"
env = "dev"

[shop_prod]
url = "postgresql://u@prod-host/shop"
group = "shop"
db = "shop"
env = "prod"

[shop_jp]
url = "postgresql://u@tokyo-host/shop"
group = "shop"
db = "shop"
env = "jp"
"""


@pytest.fixture()
def ws(tmp_path):
    (tmp_path / "connections.toml").write_text(CONNS)
    workspace.configure_workspace(str(tmp_path))
    yield tmp_path


def test_direct_key_backward_compatible(ws):
    assert core.resolve_connection("blog").key == "blog"


def test_envset_defaults_to_dev(ws):
    # logical db "shop" with no --env -> dev
    assert core.resolve_connection("shop").key == "shop_dev"


def test_envset_explicit_prod(ws):
    assert core.resolve_connection("shop", env="prod").key == "shop_prod"
    assert core.resolve_connection("shop", env="jp").key == "shop_jp"


def test_key_plus_env_resolves_via_envset(ws):
    # legacy query with @db=<connection key> + --env still hits the right env member
    assert core.resolve_connection("shop_dev", env="jp").key == "shop_jp"
    assert core.resolve_connection("shop_jp", env="prod").key == "shop_prod"


def test_envset_unknown_env_errors(ws):
    with pytest.raises(core.QuarryError):
        core.resolve_connection("shop", env="staging")


def test_unknown_db_errors(ws):
    with pytest.raises(core.QuarryError):
        core.resolve_connection("nope")


def test_group_structure(ws):
    tree = core.group_connections()
    groups = {g["group"]: g for g in tree}
    assert set(groups) == {"acme", "shop"}
    # shop folder holds ONE logical db that is an env-set of 3
    shop = groups["shop"]["items"]
    assert len(shop) == 1
    assert shop[0]["db"] == "shop"
    assert shop[0]["is_env_set"] is True
    assert sorted(e["env"] for e in shop[0]["envs"]) == ["dev", "jp", "prod"]
    # acme folder holds blog as a singleton
    assert groups["acme"]["items"][0]["db"] == "blog"


def test_prod_write_is_read_only_by_default():
    # engine-level: write blocked unless allow_write
    with pytest.raises(core.QuarryError):
        core.enforce_safety("delete from t", allow_write=False, max_rows=None)
    sql, _ = core.enforce_safety("delete from t", allow_write=True, max_rows=None)
    assert sql.startswith("delete")
