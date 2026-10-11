from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

MODULE_PATH = Path(__file__).parents[1] / "tools" / "local_t2u_server.py"
SPEC = importlib.util.spec_from_file_location("local_t2u_server", MODULE_PATH)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


def test_parse_dh_prefixed_request_and_redact_route() -> None:
    request = MODULE.parse_request(
        b'DHGET /online/p2psrv/PRIVATE-ID HTTP/1.1\r\nCSeq: 7\r\nX-WSSE: secret\r\n\r\n'
    )
    assert request is not None
    assert request.method == "GET"
    assert request.headers["cseq"] == "7"
    assert MODULE.route_name(request.path) == "online_p2psrv"
    assert "PRIVATE-ID" not in MODULE.route_name(request.path)


def test_stun_response_advertises_local_oracle_and_echoes_sequence() -> None:
    request = MODULE.parse_request(
        b'DHGET /p2p/stun/probe HTTP/1.1\r\nCSeq: 3\r\nContent-Length: 30\r\n\r\n<body><seq>9</seq></body>'
    )
    assert request is not None
    response = MODULE.response_for(request, "192.0.2.10", ("192.0.2.20", 45678))
    assert b"HTTP/1.1 200 OK" in response
    assert b"CSeq: 3" in response
    assert b"<seq>9</seq>" in response
    assert b"<ip>192.0.2.20</ip><port>45678</port>" in response


def test_registration_query_is_never_part_of_route_name() -> None:
    request = MODULE.parse_request(
        b"GET /p2pdev/update?devid=secret&password=secret HTTP/1.1\r\nHost: local\r\n\r\n"
    )
    assert request is not None
    assert MODULE.route_name(request.path) == "register_update"
    assert "secret" not in MODULE.route_name(request.path)


def test_unknown_route_is_collapsed() -> None:
    assert MODULE.route_name("/private/identifier?token=secret") == "unknown"
