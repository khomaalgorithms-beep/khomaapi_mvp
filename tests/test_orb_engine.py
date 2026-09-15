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


# ---- EngineManager lifecycle: ONE shared feed dispatched to all accounts ----
class FakeAP:
    def __init__(self, cfg):
        self.cfg = cfg
        self.bars = []
        self.backfilled = []
    def on_bar(self, root, bar):
        self.bars.append((root, bar)); return {"action": "seen", "root": root}
    def backfill(self, root, bars):
        self.backfilled.append((root, tuple(bars))); return {"root": root, "action": "backfill_ready"}
    def poll(self):
        return [{"account_id": self.cfg.account_id, "action": "tick"}]


class FakeFeed:
    def __init__(self, subs, on_bar):
        self.subs = subs; self.on_bar = on_bar; self.stopped = False
    def run(self):
        return None
    def stop(self):
        self.stopped = True


def _cfg(acct_id, roots=("MNQ",)):
    return AccountConfig(env="demo", account_spec="D", account_id=acct_id, account_name="D",
                         instruments={r: r + "Z6" for r in roots}, qty={r: 6 for r in roots})


def _item(acct_id, sig, roots=("MNQ",)):
    return {"cfg": _cfg(acct_id, roots), "sig": sig, "token_provider": lambda: "t"}


def _mgr():
    made = {}
    feeds = []
    def apf(cfg, tp):
        ap = FakeAP(cfg); made[cfg.account_id] = ap; return ap
    def ff(subs, on_bar):
        f = FakeFeed(subs, on_bar); feeds.append(f); return f
    m = eng.EngineManager(feed_factory=ff, autopilot_factory=apf, start_feed=True)
    return m, made, feeds


def test_one_shared_feed_for_all_accounts():
    m, made, feeds = _mgr()
    m.sync([_item(1, "a"), _item(2, "a")])
    assert sorted(m.active_ids()) == [1, 2]
    # exactly ONE feed for both accounts, subscribed to the NQ data ticker -> MNQ
    assert len(feeds) == 1 and list(feeds[0].subs.values()) == ["MNQ"]
    assert m.feed_roots() == ("MNQ",)


def test_bar_dispatched_to_every_account():
    m, made, feeds = _mgr()
    m.sync([_item(1, "a"), _item(2, "a")])
    from app.orb_selective import Bar
    from datetime import datetime
    out = m.on_bar("MNQ", Bar(datetime(2026, 1, 2, 9, 50), 1, 2, 0, 1, 5))
    assert len(out) == 2                          # both accounts saw the one bar
    assert made[1].bars and made[2].bars


def test_feed_resubscribes_when_instrument_set_changes():
    m, made, feeds = _mgr()
    m.sync([_item(1, "a", roots=("MNQ",))])
    assert m.feed_roots() == ("MNQ",)
    # add an account that also trades RTY -> feed must resubscribe to both
    m.sync([_item(1, "a", roots=("MNQ",)), _item(2, "b", roots=("MNQ", "M2K"))])
    assert m.feed_roots() == ("M2K", "MNQ")
    assert sorted(feeds[-1].subs.values()) == ["M2K", "MNQ"]   # newest feed has both


def test_feed_stops_when_all_disarmed():
    m, made, feeds = _mgr()
    m.sync([_item(1, "a")])
    m.sync([])                                    # all disarmed
    assert m.active_ids() == [] and m.feed_roots() == () and feeds[0].stopped


def test_poll_all_accounts():
    m, made, feeds = _mgr()
    m.sync([_item(1, "a"), _item(2, "a")])
    assert len(m.poll()) == 2
    m.stop_all()
    assert m.active_ids() == [] and feeds[-1].stopped


def test_run_backfills_once_per_root_then_cleared():
    m, made, feeds = _mgr()
    m.sync([_item(1, "a", roots=("MNQ", "M2K")), _item(2, "a", roots=("MNQ",))])
    fetched = []
    def fetch(root):
        fetched.append(root); return [f"bar-{root}"]
    out = m.run_backfills(fetch)
    assert sorted(set(fetched)) == ["M2K", "MNQ"]              # one REST fetch per unique root
    assert {r for r, _ in made[1].backfilled} == {"MNQ", "M2K"}
    assert [r for r, _ in made[2].backfilled] == ["MNQ"]
    assert any(a["action"] == "backfill_ready" for a in out)
    assert m.run_backfills(fetch) == []                        # needs_backfill cleared -> no-op
