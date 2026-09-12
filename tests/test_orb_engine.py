"""KhomaAPI integration glue — the parts that don't need the DB or a live socket. Proves:
sizing matches the Prop Engine, a config builds from a connected-account row with resolved
front contracts, the change-signature is stable, and the manager starts/stops/polls accounts."""
import app.orb_engine as eng
from app.orb_live import AccountConfig


def test_qty_matches_prop_engine():
    # 50K Normal, both -> 12 total micros -> 6 MNQ + 6 M2K (the chosen first-run config)
    assert eng.qty_for("eval", "50K", "norm", "both") == {"MNQ": 6, "M2K": 6}
    assert eng.qty_for("eval", "25K", "norm", "both") == {"MNQ": 3, "M2K": 3}
    assert eng.qty_for("eval", "50K", "norm", "nq") == {"MNQ": 12}
    assert eng.qty_for("funded", "50K", "aggr", "rty") == {"M2K": 12}


def test_build_account_config_resolves_contracts_and_sizes(monkeypatch):
    monkeypatch.setattr(eng.tvo, "resolve_contract",
                        lambda env, tok, sym: {"MNQ": "MNQZ5", "M2K": "M2KZ5"}[sym])
    monkeypatch.delenv("ORB_OBSERVE", raising=False)
    row = {"env": "demo", "account_name": "DEMO856420", "account_id": "856420",
           "mode": "eval", "acct_size": "50K", "preset": "norm", "asset": "both"}
    cfg = eng.build_account_config(row, token="t")
    assert isinstance(cfg, AccountConfig)
    assert cfg.env == "demo" and cfg.account_id == 856420
    assert cfg.instruments == {"MNQ": "MNQZ5", "M2K": "M2KZ5"}
    assert cfg.qty == {"MNQ": 6, "M2K": 6}
    assert cfg.observe == {"MNQ": False, "M2K": False}       # both live (RTY validated)


def test_observe_env_forces_observe(monkeypatch):
    monkeypatch.setattr(eng.tvo, "resolve_contract", lambda e, t, s: s + "Z5")
    monkeypatch.setenv("ORB_OBSERVE", "M2K")
    row = {"env": "demo", "account_name": "D", "account_id": "1", "mode": "eval",
           "acct_size": "50K", "preset": "norm", "asset": "both"}
    cfg = eng.build_account_config(row, token="t")
    assert cfg.observe == {"MNQ": False, "M2K": True}        # RTY observe-only via env


def test_build_returns_none_on_unresolvable_contract(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("no contract")
    monkeypatch.setattr(eng.tvo, "resolve_contract", boom)
    row = {"env": "demo", "account_name": "D", "account_id": "1", "mode": "eval",
           "acct_size": "50K", "preset": "norm", "asset": "nq"}
    assert eng.build_account_config(row, token="t") is None


def test_config_signature_changes_on_setting_change():
    row = {"env": "demo", "mode": "eval", "acct_size": "50K", "preset": "norm", "asset": "both"}
    s1 = eng.config_signature(row, {"MNQ": "MNQZ5"}, {"MNQ": 6})
    s2 = eng.config_signature(row, {"MNQ": "MNQZ5"}, {"MNQ": 8})   # size changed
    s3 = eng.config_signature(row, {"MNQ": "MNQZ5"}, {"MNQ": 6})
    assert s1 != s2 and s1 == s3


# ---- EngineManager lifecycle (fake factory, no real feeds) ----
class FakeAP:
    def __init__(self, tag):
        self.tag = tag
    def poll(self):
        return [{"account_id": self.tag, "action": "tick"}]


class FakeFeed:
    def __init__(self):
        self.stopped = False
    def run(self):
        return None
    def stop(self):
        self.stopped = True


def _cfg(acct_id):
    return AccountConfig(env="demo", account_spec="D", account_id=acct_id, account_name="D",
                         instruments={"MNQ": "MNQZ5"}, qty={"MNQ": 6})


def _item(acct_id, sig):
    return {"cfg": _cfg(acct_id), "sig": sig, "token_provider": lambda: "t"}


def test_manager_starts_stops_and_polls():
    made = []
    def factory(cfg, get_token):
        ap = FakeAP(cfg.account_id); made.append(ap)
        return ap, FakeFeed()
    m = eng.EngineManager(engine_factory=factory, start_feed=False)

    m.sync([_item(1, "a"), _item(2, "a")])
    assert sorted(m.active_ids()) == [1, 2]
    assert len(m.poll()) == 2                              # both polled

    # account 2 removed -> its feed stopped; account 1 unchanged -> kept (not rebuilt)
    m.sync([_item(1, "a")])
    assert m.active_ids() == [1] and len(made) == 2        # no rebuild of acct 1

    # account 1 settings change (new sig) -> rebuilt
    m.sync([_item(1, "b")])
    assert m.active_ids() == [1] and len(made) == 3

    m.stop_all()
    assert m.active_ids() == []
