import base64
import json
import logging
from pathlib import Path
from typing import Any, Dict, List

from config import DEFAULT_EXPORT_LIMIT, OUTPUT_DIR
from database import Database
from parser import parse_node_url

logger = logging.getLogger("exporter")

def generate_subscription_base64(node_urls: List[str]) -> str:
    """Generate Base64 encoded string from a list of node URLs."""
    joined = "\n".join(node_urls)
    return base64.b64encode(joined.encode("utf-8")).decode("utf-8")


def generate_singbox_client_config(
    nodes: List[Dict[str, Any]], mixed_port: int = 7890
) -> Dict[str, Any]:
    """Generate a complete, ready-to-use sing-box client configuration."""
    node_outbounds: List[Dict[str, Any]] = []
    tags: List[str] = []

    for idx, node in enumerate(nodes, start=1):
        url = node["node_url"]
        delay = node.get("delay_ms", 0)
        proto = node.get("protocol", "proxy")
        tag = f"node-{idx}-{proto}-{delay}ms"
        parsed = parse_node_url(url, tag=tag)
        if parsed:
            node_outbounds.append(parsed)
            tags.append(tag)

    if not tags:
        tags = ["direct"]

    config: Dict[str, Any] = {
        "log": {"level": "info", "timestamp": True},
        "dns": {
            "servers": [
                {"tag": "remote-dns", "type": "udp", "server": "1.1.1.1"},
                {"tag": "local-dns", "type": "udp", "server": "223.5.5.5", "detour": "direct"},
            ]
        },
        "inbounds": [
            {
                "type": "mixed",
                "tag": "mixed-in",
                "listen": "127.0.0.1",
                "listen_port": mixed_port,
            }
        ],
        "outbounds": [
            {
                "type": "selector",
                "tag": "select",
                "outbounds": ["auto-urltest"] + tags + ["direct"],
                "default": "auto-urltest",
            },
            {
                "type": "urltest",
                "tag": "auto-urltest",
                "outbounds": tags,
                "url": "http://cp.cloudflare.com/generate_204",
                "interval": "3m",
                "tolerance": 50,
            },
            *node_outbounds,
            {"type": "direct", "tag": "direct"},
            {"type": "block", "tag": "block"},
        ],
        "route": {
            "default_domain_resolver": "local-dns",
            "rules": [
                {"protocol": "dns", "action": "hijack-dns"},
                {"ip_is_private": True, "outbound": "direct"},
            ],
            "auto_detect_interface": True,
        },
    }
    return config


class Exporter:
    """Export active nodes to Base64 subscription text and sing-box JSON configuration."""

    def __init__(self, db: Database, output_dir: str = OUTPUT_DIR):
        self.db = db
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)

    def export(self, limit: int = DEFAULT_EXPORT_LIMIT, region: str = "cn") -> Dict[str, str]:
        """
        Query active nodes from the database and export them.
        Returns a dict with paths of generated files.
        """
        active_nodes = self.db.get_active_nodes(limit=limit, region=region)
        logger.info(f"Found {len(active_nodes)} active nodes (region={region}) for export.")


        node_urls = [node["node_url"] for node in active_nodes]

        # 1. Base64 subscription file
        b64_content = generate_subscription_base64(node_urls)
        sub_file = self.output_dir / "subscription.txt"
        sub_file.write_text(b64_content, encoding="utf-8")
        logger.info(f"Generated subscription file at: {sub_file}")

        # 2. sing-box JSON config
        client_cfg = generate_singbox_client_config(active_nodes)
        cfg_file = self.output_dir / "config.json"
        cfg_file.write_text(json.dumps(client_cfg, indent=2, ensure_ascii=False), encoding="utf-8")
        logger.info(f"Generated sing-box config at: {cfg_file}")

        # 3. Raw URLs list for convenience
        raw_file = self.output_dir / "nodes_raw.txt"
        raw_file.write_text("\n".join(node_urls), encoding="utf-8")

        return {
            "subscription_txt": str(sub_file),
            "config_json": str(cfg_file),
            "nodes_raw_txt": str(raw_file),
        }
