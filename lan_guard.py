"""맥미니 hermes 웹서비스 공용 접속 제한 — 로컬(루프백)과 Tailscale에서 온 요청만 받는다.

서비스는 0.0.0.0에 바인딩한다. Tailscale은 수동 연결이라 그 주소에 직접 바인딩하면
연결 전 기동 시 실패하기 때문이다. 그 결과 같은 공유기의 모든 기기에 인증 없이
열려 있었다(2026-10-30 LAN 점검). 그래서 바인딩은 그대로 두고 요청 단위로 거른다.

이 파일은 서비스 저장소마다 같은 내용으로 복사해 둔다(서로 의존하지 않게).
고칠 때는 모든 사본을 함께 바꾼다. 원본: ~/projects/hermes-llm-log/lan_guard.py

- 허용 대역 추가: HERMES_LAN_GUARD_ALLOW="192.168.1.162/32,..."
- 비상 해제:     HERMES_LAN_GUARD=0
Python 3.9 호환(포탈이 /usr/bin/python3로 돈다).
"""
from __future__ import annotations

import ipaddress
import os

_DEFAULT_NETS = (
    "127.0.0.0/8", "::1/128",          # 이 맥미니
    "100.64.0.0/10",                   # Tailscale IPv4(CGNAT 대역)
    "fd7a:115c:a1e0::/48",             # Tailscale IPv6
)
_FORBIDDEN_BODY = "Forbidden: 이 서비스는 Tailscale로만 접속할 수 있습니다.\n".encode("utf-8")


def _networks():
    nets = []
    extra = os.environ.get("HERMES_LAN_GUARD_ALLOW", "")
    for cidr in list(_DEFAULT_NETS) + [c.strip() for c in extra.split(",") if c.strip()]:
        try:
            nets.append(ipaddress.ip_network(cidr, strict=False))
        except ValueError:
            pass                       # 잘못 적은 추가 대역은 무시(기본 대역은 유지)
    return tuple(nets)


_NETS = _networks()


def enabled() -> bool:
    return os.environ.get("HERMES_LAN_GUARD", "1").strip().lower() not in ("0", "false", "off", "no")


def allowed(ip) -> bool:
    """요청한 쪽 IP가 허용 대역이면 True. 모르는 값(None·유닉스 소켓 등)은 막는다."""
    if not enabled():
        return True
    try:
        addr = ipaddress.ip_address(str(ip).split("%", 1)[0])
    except ValueError:
        return False
    if addr.version == 6 and addr.ipv4_mapped is not None:   # ::ffff:192.168.x.x 형태
        addr = addr.ipv4_mapped
    return any(addr in net for net in _NETS)


def guard_http_server(server):
    """http.server 계열: 허용 밖 연결에는 403 한 줄을 보내고 닫는다."""
    def verify_request(request, client_address):
        if allowed(client_address[0]):
            return True
        try:
            request.sendall(b"HTTP/1.1 403 Forbidden\r\nContent-Type: text/plain; charset=utf-8\r\n"
                            b"Content-Length: " + str(len(_FORBIDDEN_BODY)).encode() +
                            b"\r\nConnection: close\r\n\r\n" + _FORBIDDEN_BODY)
        except OSError:
            pass
        return False
    server.verify_request = verify_request
    return server


def guard_flask(app):
    """Flask: 다른 before_request보다 먼저 돌도록 맨 앞에 넣는다."""
    from flask import request

    def _lan_guard():
        if not allowed(request.remote_addr):
            return _FORBIDDEN_BODY.decode("utf-8"), 403, {"Content-Type": "text/plain; charset=utf-8"}
        return None
    app.before_request_funcs.setdefault(None, []).insert(0, _lan_guard)
    return app


class ASGIGuard:
    """FastAPI/Starlette: app.add_middleware(lan_guard.ASGIGuard)."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") in ("http", "websocket"):
            client = scope.get("client") or (None,)
            if not allowed(client[0]):
                if scope["type"] == "websocket":
                    await send({"type": "websocket.close", "code": 1008})
                    return
                await send({"type": "http.response.start", "status": 403,
                            "headers": [(b"content-type", b"text/plain; charset=utf-8"),
                                        (b"content-length", str(len(_FORBIDDEN_BODY)).encode())]})
                await send({"type": "http.response.body", "body": _FORBIDDEN_BODY})
                return
        await self.app(scope, receive, send)
