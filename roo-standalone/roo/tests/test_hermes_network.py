from copy import deepcopy
import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def network():
    spec = importlib.util.spec_from_file_location("roo_network_check", ROOT / "hermes/network/check.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def rules():
    return {"nftables": [
        {"metainfo": {"version": "test"}},
        {"table": {"family": "inet", "name": "roo_hermes_host", "handle": 12}},
        {"chain": {"family": "inet", "table": "roo_hermes_host", "name": "protect_host", "handle": 13,
                   "type": "filter", "hook": "input", "prio": -10, "policy": "accept"}},
        {"rule": {"family": "inet", "table": "roo_hermes_host", "chain": "protect_host", "handle": 14,
                  "expr": [{"match": {"op": "==", "left": {"meta": {"key": "iifname"}},
                                      "right": {"set": ["br-roo-web", "br-roo-hermes"]}}},
                           {"counter": {"packets": 0, "bytes": 0}}, {"drop": None}]}}
    ]}


def test_exact_input_drop_for_both_bridges(network, rules):
    network.verify_rules(rules)


@pytest.mark.parametrize("change", ["wrong_hook", "wrong_family", "missing_bridge", "accept", "extra_accept", "extra_condition", "named_set", "dormant"])
def test_fail_closed_on_weakened_host_firewall(network, rules, change):
    chain = rules["nftables"][2]["chain"]
    rule = rules["nftables"][3]["rule"]
    if change == "wrong_hook": chain["hook"] = "forward"
    elif change == "wrong_family": chain["family"] = "ip"
    elif change == "missing_bridge": rule["expr"][0]["match"]["right"]["set"] = ["br-roo-web"]
    elif change == "accept": rule["expr"][-1] = {"accept": None}
    elif change == "extra_accept": rules["nftables"].insert(3, {"rule": {**rule, "expr": [{"accept": None}]}})
    elif change == "extra_condition": rule["expr"].insert(1, {"match": {"op": "==", "left": 1, "right": 2}})
    elif change == "dormant": rules["nftables"][1]["table"]["flags"] = ["dormant"]
    else: rule["expr"][0]["match"]["right"] = "@modifiable_set"
    with pytest.raises(ValueError):
        network.verify_rules(rules)
