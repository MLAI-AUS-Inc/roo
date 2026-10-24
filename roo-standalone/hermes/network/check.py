"""Read-only host firewall verifier. Install root-owned, invoke fixed check only."""
import json
import os
import socket
import subprocess
import sys

BRIDGES = {"roo-hermes-internal": "br-roo-hermes", "roo-hermes-web": "br-roo-web"}


def verify_rules(document):
    entries = document.get("nftables")
    if not isinstance(entries, list):
        raise ValueError("Missing nftables inventory")
    policy = [e for e in entries if set(e) != {"metainfo"}]
    if len(policy) != 3:
        raise ValueError("Unexpected extra rules in dedicated host boundary table")
    tables = [e["table"] for e in policy if set(e) == {"table"}]
    chains = [e["chain"] for e in policy if set(e) == {"chain"}]
    rules = [e["rule"] for e in policy if set(e) == {"rule"}]
    if len(tables) != 1 or len(chains) != 1 or len(rules) != 1:
        raise ValueError("Expected one host boundary rule")
    table, chain, rule = tables[0], chains[0], rules[0]
    if table.get("family") != "inet" or table.get("name") != "roo_hermes_host" or table.get("flags") not in (None, []):
        raise ValueError("Wrong firewall table")
    if (chain.get("family") != "inet" or chain.get("table") != "roo_hermes_host"
            or chain.get("name") != "protect_host" or chain.get("type") != "filter"
            or chain.get("hook") != "input" or chain.get("prio") != -10
            or chain.get("policy") != "accept"):
        raise ValueError("Host boundary chain is not active at the reviewed hook")
    if (rule.get("family") != "inet" or rule.get("table") != "roo_hermes_host"
            or rule.get("chain") != "protect_host"):
        raise ValueError("Wrong host boundary rule")
    # Ignore only an ordinary inline counter. No accepts/jumps, extra conditions,
    # named sets or rules before our drop can weaken this exact dedicated chain.
    expressions = [e for e in rule.get("expr", []) if set(e) != {"counter"}]
    if len(expressions) != 2 or expressions[1] != {"drop": None}:
        raise ValueError("Host boundary must end in unconditional matching drop")
    match = expressions[0].get("match", {})
    expected = {"op": "==", "left": {"meta": {"key": "iifname"}},
                "right": {"set": sorted(BRIDGES.values())}}
    if not isinstance(match.get("right"), dict) or set(match["right"]) != {"set"} or not isinstance(match["right"].get("set"), list):
        raise ValueError("Expected fixed bridge set")
    actual = dict(match)
    actual["right"] = {"set": sorted(match["right"]["set"])}
    if actual != expected:
        raise ValueError("Host boundary does not match the exact sandbox interfaces")


def main():
    try:
        if os.geteuid() != 0 or sys.argv[1:] != ["check"]:
            raise ValueError("Only a privileged read-only check is supported")
        actual = {name for _, name in socket.if_nameindex()}
        if not set(BRIDGES.values()) <= actual:
            raise ValueError("Sandbox bridges do not exist")
        result = subprocess.run(["/usr/sbin/nft", "--json", "--numeric", "list", "table", "inet", "roo_hermes_host"],
                                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                timeout=10, check=True, env={"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LANG": "C", "LC_ALL": "C"})
        if result.stderr or len(result.stdout) > 65536:
            raise ValueError("Unexpected firewall inspection output")
        verify_rules(json.loads(result.stdout))
        print(json.dumps({"ok": True, "networks": BRIDGES, "host_input_drop": True}))
    except Exception:
        print("Hermes host network isolation check failed", file=sys.stderr)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
