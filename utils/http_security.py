"""Shared HTTP URL / secret-redaction helpers usable by integrations and agents."""

from __future__ import annotations

import ipaddress
import re
import socket
from urllib.parse import urlparse

SECRET_ASSIGNMENT_RE = re.compile(
    r"(?i)\b(api[_-]?key|access[_-]?token|refresh[_-]?token|token|password|passwd|secret|authorization|cookie)\b"
    r"\s*[:=]\s*([\"']?)[^\s\"',;]+"
)
BEARER_RE = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{12,}")
COMMON_SECRET_VALUE_RE = re.compile(r"\b(?:sk|ak|pk|ghp|gho|github_pat|glpat|xoxb|xoxp|AIza)[A-Za-z0-9_\-]{12,}\b")
_PROXY_FAKE_IP_NETWORKS = (ipaddress.ip_network("198.18.0.0/15"),)


def security_error(message: str) -> dict:
    return {"error": f"安全拦截: {message}"}


def redact_sensitive_text(text: str) -> str:
    if not text:
        return text
    redacted = SECRET_ASSIGNMENT_RE.sub(lambda m: f"{m.group(1)}={m.group(2)}***REDACTED***", text)
    redacted = BEARER_RE.sub("Bearer ***REDACTED***", redacted)
    return COMMON_SECRET_VALUE_RE.sub("***REDACTED***", redacted)


def validate_public_http_url(url: str) -> str | dict:
    raw = str(url or "").strip()
    if not raw:
        return security_error("URL 不能为空")

    parsed = urlparse(raw)
    if parsed.scheme not in {"http", "https"}:
        return security_error("只允许抓取 http/https URL")
    if parsed.username or parsed.password:
        return security_error("URL 中禁止携带用户名或密码")
    if not parsed.hostname:
        return security_error("URL 缺少主机名")
    if parsed.port and parsed.port not in {80, 443}:
        return security_error("禁止抓取非标准端口，避免访问内网服务")

    host = parsed.hostname.strip().lower().rstrip(".")
    if host in {"localhost", "localhost.localdomain"} or host.endswith(".local"):
        return security_error("禁止抓取本机或本地域名")

    try:
        infos = socket.getaddrinfo(
            host, parsed.port or (443 if parsed.scheme == "https" else 80), type=socket.SOCK_STREAM
        )
    except socket.gaierror:
        return security_error("URL 主机无法解析")

    host_is_ip_literal = _parse_ip(host) is not None
    for info in infos:
        ip_result = _validate_public_ip(info[4][0], allow_proxy_fake_ip=not host_is_ip_literal)
        if ip_result:
            return ip_result
    return raw


def _parse_ip(ip_text: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    try:
        return ipaddress.ip_address(ip_text)
    except ValueError:
        return None


def _validate_public_ip(ip_text: str, *, allow_proxy_fake_ip: bool = False) -> dict | None:
    ip = _parse_ip(ip_text)
    if ip is None:
        return security_error("URL 解析到无效地址")
    if allow_proxy_fake_ip and any(ip in network for network in _PROXY_FAKE_IP_NETWORKS):
        return None
    if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_reserved or ip.is_unspecified:
        return security_error("禁止抓取内网、本机、链路本地或保留地址")
    return None
