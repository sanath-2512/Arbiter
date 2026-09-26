"""Scripted policy (stuck loop) for flask_ipv6_server_name: keeps trying variations of one wrong
hypothesis (stray whitespace in SERVER_NAME) in the same line, each followed by the same failing test,
until the harness's failure memory says to form a new hypothesis; then it makes the historical fix
(':' inside an IPv6 address breaks `partition(':')`). Without an intervention it gives up after
GIVE_UP variations and submits the wrong change. Measures loop detection and intervention on real
test output, not model capability."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _common import tc  # noqa: E402

P = "src/flask/app.py"
GIVE_UP = 10
TEST = "tests/test_ipv6_server_name.py"
TEST_SRC = '''import pytest
import werkzeug.serving

import flask


@pytest.mark.parametrize(("server_name", "host", "port"), [("[::1]:8080", "::1", 8080), ("[::1]", "::1", 5000)])
def test_ipv6_server_name(monkeypatch, server_name, host, port):
    seen = {}
    monkeypatch.setattr(werkzeug.serving, "run_simple", lambda h, p, *a, **k: seen.update(host=h, port=p))
    app = flask.Flask(__name__)
    app.config["SERVER_NAME"] = server_name
    app.run()
    assert seen == {"host": host, "port": port}
'''
RUN = f"python -m pytest -q -p no:cacheprovider {TEST}"
VERIFY = f"python -m pytest -q -p no:cacheprovider {TEST} tests/test_basic.py -k 'ipv6 or run_from_config'"
LINE = "            sn_host, _, sn_port = {}.partition(\":\")\n"
VARIANTS = ["server_name", "server_name.strip()", "server_name.strip(' ')", "str(server_name).strip()",
            "server_name.lstrip().rstrip()", "server_name.strip(' \\t')"]
FIX = [
    ("fix", '            server_url = urlsplit(f"//{server_name}")\n            sn_host = server_url.hostname\n'
            "            sn_port = server_url.port\n"),
    ("import", "from urllib.parse import quote as _url_quote\nfrom urllib.parse import urlsplit\n"),
    ("port", "        elif sn_port is not None:\n            port = sn_port\n"),
]


def calls(messages):
    return [c.get("function", {}) for m in messages if m.get("role") == "assistant" for c in (m.get("tool_calls") or [])]


def respond(messages, tools):
    made = calls(messages)
    names = [c.get("name") for c in made]
    args = [str(c.get("arguments")) for c in made]
    switched = any("NEW HYPOTHESIS NEEDED" in str(m.get("content") or "")
                   for m in messages if m.get("role") == "user")
    wrong = sum(1 for n, a in zip(names, args) if n == "edit_file" and ".partition" in a and "urlsplit" not in a)
    fixed = sum(1 for n, a in zip(names, args) if n == "edit_file" and ("urlsplit" in a or "is not None" in a))
    current = LINE.format(VARIANTS[wrong % len(VARIANTS)])
    if "write_file" not in names:
        return {"content": "reproduce first", "tool_calls": [tc("write_file", path=TEST, content=TEST_SRC)]}
    if not switched:
        if names[-1] != "bash":
            return {"content": "run the reproduction", "tool_calls": [tc("bash", command=RUN)]}
        if wrong >= GIVE_UP:
            return {"content": "giving up", "tool_calls": [tc("submit", summary="strip SERVER_NAME before parsing")]}
        return {"content": f"variation {wrong + 1} of the whitespace idea",
                "tool_calls": [tc("edit_file", path=P, old_str=current,
                                  new_str=LINE.format(VARIANTS[(wrong + 1) % len(VARIANTS)]))]}
    if fixed == 0:
        return {"content": "new hypothesis: partition(':') splits inside the IPv6 address",
                "tool_calls": [tc("edit_file", path=P, old_str=current, new_str=FIX[0][1])]}
    if fixed == 1:
        return {"content": "import", "tool_calls": [tc("edit_file", path=P,
                old_str="from urllib.parse import quote as _url_quote\n", new_str=FIX[1][1])]}
    if fixed == 2:
        return {"content": "the port is already an int", "tool_calls": [tc("edit_file", path=P,
                old_str="        elif sn_port:\n            port = int(sn_port)\n", new_str=FIX[2][1])]}
    if names[-1] != "bash":
        return {"content": "verify", "tool_calls": [tc("bash", command=VERIFY)]}
    return {"content": "done", "tool_calls": [tc("submit", summary="parse SERVER_NAME with urlsplit (IPv6-aware)")]}
