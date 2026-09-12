import base64
import json
import logging
import re
import urllib.parse
from typing import Any, Dict, Optional

logger = logging.getLogger("parser")

def safe_b64decode(s: str) -> str:
    """Safely decode URL-safe or standard Base64 string."""
    s = s.strip()
    # Add padding if needed
    missing_padding = len(s) % 4
    if missing_padding:
        s += "=" * (4 - missing_padding)
    return base64.urlsafe_b64decode(s.encode("utf-8")).decode("utf-8", errors="ignore")

def parse_vless(url: str, tag: str = "proxy") -> Optional[Dict[str, Any]]:
    """Parse vless:// link into sing-box outbound JSON."""
    try:
        parsed = urllib.parse.urlparse(url)
        uuid = parsed.username
        server = parsed.hostname
        port = parsed.port or 443
        query = urllib.parse.parse_qs(parsed.query)

        if not uuid or not server:
            return None

        outbound: Dict[str, Any] = {
            "type": "vless",
            "tag": tag,
            "server": server,
            "server_port": int(port),
            "uuid": uuid,
        }

        flow = query.get("flow", [None])[0]
        if flow:
            outbound["flow"] = flow

        security = query.get("security", ["none"])[0].lower()
        sni = query.get("sni", query.get("serverName", [None]))[0]
        fp = query.get("fp", ["chrome"])[0]

        if security in ("tls", "reality"):
            tls_cfg: Dict[str, Any] = {"enabled": True}
            if sni:
                tls_cfg["server_name"] = sni
            if fp:
                tls_cfg["utls"] = {"enabled": True, "fingerprint": fp}

            insecure = query.get("allowInsecure", query.get("insecure", ["0"]))[0]
            if insecure in ("1", "true"):
                tls_cfg["insecure"] = True

            if security == "reality":
                pbk = query.get("pbk", [None])[0]
                sid = query.get("sid", [None])[0]
                reality_cfg: Dict[str, Any] = {"enabled": True}
                if pbk:
                    reality_cfg["public_key"] = pbk
                if sid:
                    reality_cfg["short_id"] = sid
                tls_cfg["reality"] = reality_cfg

            outbound["tls"] = tls_cfg

        # Transport settings
        net_type = query.get("type", ["tcp"])[0].lower()
        if net_type == "ws":
            ws_cfg: Dict[str, Any] = {"type": "ws"}
            path = query.get("path", [None])[0]
            host = query.get("host", [None])[0]
            if path:
                ws_cfg["path"] = path
            if host:
                ws_cfg["headers"] = {"Host": host}
            outbound["transport"] = ws_cfg
        elif net_type == "grpc":
            grpc_cfg: Dict[str, Any] = {"type": "grpc"}
            service_name = query.get("serviceName", [None])[0]
            if service_name:
                grpc_cfg["service_name"] = service_name
            outbound["transport"] = grpc_cfg

        return outbound
    except Exception as e:
        logger.debug(f"Failed to parse vless url {url}: {e}")
        return None


def parse_trojan(url: str, tag: str = "proxy") -> Optional[Dict[str, Any]]:
    """Parse trojan:// link into sing-box outbound JSON."""
    try:
        parsed = urllib.parse.urlparse(url)
        password = parsed.username or parsed.password
        server = parsed.hostname
        port = parsed.port or 443
        query = urllib.parse.parse_qs(parsed.query)

        if not password or not server:
            return None

        outbound: Dict[str, Any] = {
            "type": "trojan",
            "tag": tag,
            "server": server,
            "server_port": int(port),
            "password": password,
        }

        sni = query.get("sni", query.get("peer", [None]))[0]
        tls_cfg: Dict[str, Any] = {"enabled": True}
        if sni:
            tls_cfg["server_name"] = sni
        insecure = query.get("allowInsecure", query.get("insecure", ["0"]))[0]
        if insecure in ("1", "true"):
            tls_cfg["insecure"] = True

        outbound["tls"] = tls_cfg

        # Transport
        net_type = query.get("type", ["tcp"])[0].lower()
        if net_type == "ws":
            ws_cfg: Dict[str, Any] = {"type": "ws"}
            path = query.get("path", [None])[0]
            host = query.get("host", [None])[0]
            if path:
                ws_cfg["path"] = path
            if host:
                ws_cfg["headers"] = {"Host": host}
            outbound["transport"] = ws_cfg
        elif net_type == "grpc":
            service_name = query.get("serviceName", [None])[0]
            if service_name:
                outbound["transport"] = {"type": "grpc", "service_name": service_name}

        return outbound
    except Exception as e:
        logger.debug(f"Failed to parse trojan url {url}: {e}")
        return None


def parse_shadowsocks(url: str, tag: str = "proxy") -> Optional[Dict[str, Any]]:
    """Parse ss:// link into sing-box shadowsocks outbound JSON."""
    try:
        raw = url.split("://", 1)[1]
        # Remove fragment
        if "#" in raw:
            raw, _ = raw.split("#", 1)

        # SIP002 standard: ss://[base64(method:password)@server:port] or ss://[base64(method:password@server:port)]
        if "@" in raw:
            userinfo, hostport = raw.split("@", 1)
            # userinfo might be base64 encoded
            try:
                decoded = safe_b64decode(userinfo)
                if ":" in decoded:
                    method, password = decoded.split(":", 1)
                else:
                    method, password = userinfo.split(":", 1)
            except Exception:
                method, password = userinfo.split(":", 1)

            # hostport may have query params
            if "?" in hostport:
                hostport, _ = hostport.split("?", 1)
            server, port = hostport.split(":", 1)
        else:
            # Full base64 encoded
            decoded = safe_b64decode(raw)
            if "@" in decoded:
                userinfo, hostport = decoded.split("@", 1)
                method, password = userinfo.split(":", 1)
                server, port = hostport.split(":", 1)
            else:
                return None

        return {
            "type": "shadowsocks",
            "tag": tag,
            "server": server,
            "server_port": int(port),
            "method": method,
            "password": password,
        }
    except Exception as e:
        logger.debug(f"Failed to parse shadowsocks url {url}: {e}")
        return None


def parse_hysteria2(url: str, tag: str = "proxy") -> Optional[Dict[str, Any]]:
    """Parse hysteria2:// or hy2:// link into sing-box outbound JSON."""
    try:
        parsed = urllib.parse.urlparse(url)
        password = parsed.username or parsed.password
        server = parsed.hostname
        port = parsed.port or 443
        query = urllib.parse.parse_qs(parsed.query)

        if not server:
            return None

        outbound: Dict[str, Any] = {
            "type": "hysteria2",
            "tag": tag,
            "server": server,
            "server_port": int(port),
        }
        if password:
            outbound["password"] = password

        sni = query.get("sni", [None])[0]
        tls_cfg: Dict[str, Any] = {"enabled": True}
        if sni:
            tls_cfg["server_name"] = sni
        insecure = query.get("insecure", ["0"])[0]
        if insecure in ("1", "true"):
            tls_cfg["insecure"] = True
        outbound["tls"] = tls_cfg

        obfs_type = query.get("obfs", [None])[0]
        obfs_password = query.get("obfs-password", [None])[0]
        if obfs_type:
            obfs_cfg: Dict[str, Any] = {"type": obfs_type}
            if obfs_password:
                obfs_cfg["password"] = obfs_password
            outbound["obfs"] = obfs_cfg

        return outbound
    except Exception as e:
        logger.debug(f"Failed to parse hysteria2 url {url}: {e}")
        return None


def parse_vmess(url: str, tag: str = "proxy") -> Optional[Dict[str, Any]]:
    """Parse vmess:// link (base64 encoded JSON) into sing-box outbound JSON."""
    try:
        raw = url.split("://", 1)[1]
        decoded = safe_b64decode(raw)
        data = json.loads(decoded)

        server = data.get("add")
        port = data.get("port")
        uuid = data.get("id")
        if not server or not port or not uuid:
            return None

        outbound: Dict[str, Any] = {
            "type": "vmess",
            "tag": tag,
            "server": str(server).strip(),
            "server_port": int(port),
            "uuid": str(uuid).strip(),
            "alter_id": int(data.get("aid", 0)),
            "security": "auto",
        }

        # TLS settings
        tls = str(data.get("tls", "")).lower()
        if tls in ("tls", "1", "true"):
            tls_cfg: Dict[str, Any] = {"enabled": True}
            sni = data.get("sni") or data.get("host")
            if sni:
                tls_cfg["server_name"] = str(sni).strip()
            if data.get("skip-cert-verify"):
                tls_cfg["insecure"] = True
            outbound["tls"] = tls_cfg

        # Transport
        net = str(data.get("net", "tcp")).lower()
        if net == "ws":
            ws_cfg: Dict[str, Any] = {"type": "ws"}
            path = data.get("path")
            host = data.get("host")
            if path:
                ws_cfg["path"] = path
            if host:
                ws_cfg["headers"] = {"Host": host}
            outbound["transport"] = ws_cfg
        elif net == "grpc":
            service_name = data.get("path")
            if service_name:
                outbound["transport"] = {"type": "grpc", "service_name": service_name}

        return outbound
    except Exception as e:
        logger.debug(f"Failed to parse vmess url {url}: {e}")
        return None


def parse_node_url(url: str, tag: str = "proxy") -> Optional[Dict[str, Any]]:
    """Convert any supported proxy URL string into a sing-box Outbound dict."""
    url = url.strip()
    if not url or "://" not in url:
        return None

    scheme = url.split("://", 1)[0].lower()

    if scheme == "vless":
        return parse_vless(url, tag=tag)
    elif scheme == "vmess":
        return parse_vmess(url, tag=tag)
    elif scheme == "trojan":
        return parse_trojan(url, tag=tag)
    elif scheme == "ss":
        return parse_shadowsocks(url, tag=tag)
    elif scheme in ("hysteria2", "hy2"):
        return parse_hysteria2(url, tag=tag)
    else:
        logger.debug(f"Protocol '{scheme}' does not have a direct sing-box builder yet.")
        return None
