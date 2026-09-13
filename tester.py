from concurrent.futures import ThreadPoolExecutor, as_completed
import json
import logging
import os
import socket
import subprocess
import tempfile
import time
from typing import Any, Dict, List, Optional, Tuple
import requests

from config import (
    FAST_TCP_PRECHECK,
    MAX_FAIL_COUNT,
    SING_BOX_PATH,
    SPEED_TEST_URL,
    TCP_PING_TIMEOUT,
    TEST_TIMEOUT,
    TEST_URL,
)
from database import Database
from parser import parse_node_url

logger = logging.getLogger("tester")

def check_tcp_reachable(host: str, port: int, timeout: float = TCP_PING_TIMEOUT) -> bool:
    """Fast pre-flight TCP handshake check to weed out dead hosts without spawning sing-box."""
    try:
        with socket.create_connection((host, int(port)), timeout=timeout):
            return True
    except Exception:
        return False

def get_free_port() -> int:
    """Find a free local TCP port."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]



def build_singbox_test_config(outbound_cfg: Dict[str, Any], port: int) -> Dict[str, Any]:
    """Assemble an isolated sing-box config for testing."""
    return {
        "log": {"level": "warn"},
        "inbounds": [
            {
                "type": "mixed",
                "tag": "mixed-in",
                "listen": "127.0.0.1",
                "listen_port": port,
            }
        ],
        "outbounds": [
            outbound_cfg,
            {"type": "direct", "tag": "direct"},
        ],
    }


def probe_node_delay(port: int, timeout: float = TEST_TIMEOUT) -> Optional[int]:
    """
    Send an HTTP probe request via local proxy port to test latency.
    Returns delay in milliseconds, or None on failure.
    """
    proxies = {
        "http": f"http://127.0.0.1:{port}",
        "https": f"http://127.0.0.1:{port}",
    }
    start = time.perf_counter()
    try:
        resp = requests.get(
            TEST_URL,
            proxies=proxies,
            timeout=timeout,
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"},
        )
        elapsed_ms = int((time.perf_counter() - start) * 1000)
        if resp.status_code in (200, 204):
            return max(1, elapsed_ms)
    except Exception as e:
        logger.debug(f"Probe failed via port {port}: {e}")
    return None


def probe_download_speed(port: int, sample_duration: float = 2.5) -> float:
    """
    Measure download speed (Mbps) via the proxy port.
    Returns 0.0 if failed.
    """
    proxies = {
        "http": f"http://127.0.0.1:{port}",
        "https": f"http://127.0.0.1:{port}",
    }
    try:
        start = time.perf_counter()
        resp = requests.get(
            SPEED_TEST_URL,
            proxies=proxies,
            stream=True,
            timeout=sample_duration + 1.0,
        )
        if resp.status_code != 200:
            return 0.0

        downloaded_bytes = 0
        for chunk in resp.iter_content(chunk_size=65536):
            downloaded_bytes += len(chunk)
            if time.perf_counter() - start >= sample_duration:
                break

        elapsed = time.perf_counter() - start
        if elapsed > 0:
            # bytes to bits -> Mbps
            speed_mbps = round((downloaded_bytes * 8) / (elapsed * 1000 * 1000), 2)
            return speed_mbps
    except Exception as e:
        logger.debug(f"Speed test failed via port {port}: {e}")
    return 0.0


def test_single_node(
    node: Dict[str, Any], enable_speed_test: bool = True
) -> Tuple[int, str, int, float, int]:
    """
    Execute an isolated sing-box test for a single node.
    Returns (node_id, new_status, delay_ms, speed_mbps, fail_count)
    """
    node_id = node["id"]
    node_url = node["node_url"]
    current_fail = node.get("fail_count", 0)

    outbound_cfg = parse_node_url(node_url, tag="proxy")
    if not outbound_cfg:
        logger.warning(f"Node {node_id} could not be parsed into sing-box config")
        return node_id, "dead", -1, 0.0, MAX_FAIL_COUNT

    server = outbound_cfg.get("server")
    server_port = outbound_cfg.get("server_port")
    proto_type = outbound_cfg.get("type", "")

    # Fast pre-flight TCP connectivity check (for TCP-based protocols)
    if FAST_TCP_PRECHECK and proto_type not in ("hysteria2", "tuic") and server and server_port:
        if not check_tcp_reachable(server, server_port, timeout=TCP_PING_TIMEOUT):
            new_fail = current_fail + 1
            new_status = "dead" if new_fail >= MAX_FAIL_COUNT else "untested"
            logger.info(
                f"Node {node_id} [TCP UNREACHABLE] {server}:{server_port} | fail_count: {new_fail}/{MAX_FAIL_COUNT} -> {new_status}"
            )
            return node_id, new_status, -1, 0.0, new_fail

    port = get_free_port()
    config_dict = build_singbox_test_config(outbound_cfg, port)


    temp_cfg = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
    proc: Optional[subprocess.Popen] = None

    try:
        json.dump(config_dict, temp_cfg)
        temp_cfg.close()

        # Launch sing-box in background
        proc = subprocess.Popen(
            [SING_BOX_PATH, "run", "-c", temp_cfg.name],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        # Allow sing-box to initialize listener
        time.sleep(0.3)

        if proc.poll() is not None:
            # Process died prematurely
            logger.warning(f"sing-box failed to start for node {node_id}")
            new_fail = current_fail + 1
            new_status = "dead" if new_fail >= MAX_FAIL_COUNT else "untested"
            return node_id, new_status, -1, 0.0, new_fail

        delay_ms = probe_node_delay(port)
        if delay_ms is not None:
            speed_mbps = 0.0
            if enable_speed_test and delay_ms < 1500:
                speed_mbps = probe_download_speed(port)
            logger.info(
                f"Node {node_id} [ACTIVE] | Delay: {delay_ms}ms | Speed: {speed_mbps}Mbps"
            )
            return node_id, "active", delay_ms, speed_mbps, 0
        else:
            new_fail = current_fail + 1
            new_status = "dead" if new_fail >= MAX_FAIL_COUNT else "untested"
            logger.info(
                f"Node {node_id} [FAILED] | fail_count: {new_fail}/{MAX_FAIL_COUNT} -> {new_status}"
            )
            return node_id, new_status, -1, 0.0, new_fail

    finally:
        if proc:
            try:
                proc.terminate()
                proc.wait(timeout=1.0)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
        if os.path.exists(temp_cfg.name):
            try:
                os.remove(temp_cfg.name)
            except Exception:
                pass


def test_nodes_batch_singbox_clash(
    nodes: list,
    concurrency: int = 50,
    test_url: str = TEST_URL,
    timeout_sec: float = TEST_TIMEOUT,
) -> Optional[List[Tuple[int, str, int, float, int]]]:
    """
    High-performance batch tester using a single sing-box process with Clash API.
    All outbounds are evaluated asynchronously by the sing-box core in Go.
    Returns list of (node_id, status, delay_ms, speed_mbps, fail_count), or None if fallback needed.
    """
    results: List[Tuple[int, str, int, float, int]] = []
    valid_outbounds: List[Dict[str, Any]] = [{"type": "direct", "tag": "direct"}]
    testable_nodes: Dict[str, Dict[str, Any]] = {}

    for node in nodes:
        node_id = node["id"]
        node_url = node["node_url"]
        current_fail = node.get("fail_count", 0)
        tag = f"node_{node_id}"

        outbound_cfg = parse_node_url(node_url, tag=tag)
        if not outbound_cfg:
            new_fail = current_fail + 1
            new_status = "dead" if new_fail >= MAX_FAIL_COUNT else "untested"
            results.append((node_id, new_status, -1, 0.0, new_fail))
            continue

        server = outbound_cfg.get("server")
        server_port = outbound_cfg.get("server_port")
        proto_type = outbound_cfg.get("type", "")

        # Optional fast TCP precheck
        if FAST_TCP_PRECHECK and proto_type not in ("hysteria2", "tuic") and server and server_port:
            if not check_tcp_reachable(server, server_port, timeout=TCP_PING_TIMEOUT):
                new_fail = current_fail + 1
                new_status = "dead" if new_fail >= MAX_FAIL_COUNT else "untested"
                results.append((node_id, new_status, -1, 0.0, new_fail))
                continue

        valid_outbounds.append(outbound_cfg)
        testable_nodes[tag] = node

    if not testable_nodes:
        return results

    clash_port = get_free_port()
    mixed_port = get_free_port()

    cfg = {
        "log": {"level": "warn"},
        "experimental": {
            "clash_api": {
                "external_controller": f"127.0.0.1:{clash_port}"
            }
        },
        "inbounds": [
            {"type": "mixed", "tag": "mixed-in", "listen": "127.0.0.1", "listen_port": mixed_port}
        ],
        "outbounds": valid_outbounds,
    }

    temp_cfg = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
    proc: Optional[subprocess.Popen] = None

    try:
        json.dump(cfg, temp_cfg)
        temp_cfg.close()

        proc = subprocess.Popen(
            [SING_BOX_PATH, "run", "-c", temp_cfg.name],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

        # Wait for Clash API to become ready
        ready = False
        for _ in range(30):
            try:
                r = requests.get(f"http://127.0.0.1:{clash_port}/version", timeout=0.2)
                if r.status_code == 200:
                    ready = True
                    break
            except Exception:
                time.sleep(0.05)

        if not ready or proc.poll() is not None:
            logger.warning("Batch sing-box process failed to initialize Clash API, falling back.")
            return None

        # Concurrently query each outbound's delay via Clash API
        session = requests.Session()
        timeout_ms = int(timeout_sec * 1000)

        def query_delay(item: Tuple[str, Dict[str, Any]]) -> Tuple[int, str, int, float, int]:
            tag, n = item
            nid = n["id"]
            cf = n.get("fail_count", 0)
            try:
                url = f"http://127.0.0.1:{clash_port}/proxies/{tag}/delay?url={test_url}&timeout={timeout_ms}"
                resp = session.get(url, timeout=timeout_sec + 1.0)
                if resp.status_code == 200:
                    delay = resp.json().get("delay", -1)
                    if delay > 0:
                        logger.info(f"Node {nid} [ACTIVE] | Delay: {delay}ms")
                        return (nid, "active", delay, 0.0, 0)
            except Exception:
                pass

            nf = cf + 1
            ns = "dead" if nf >= MAX_FAIL_COUNT else "untested"
            return (nid, ns, -1, 0.0, nf)

        with ThreadPoolExecutor(max_workers=concurrency) as executor:
            batch_tested = list(executor.map(query_delay, testable_nodes.items()))

        results.extend(batch_tested)
        return results

    except Exception as e:
        logger.warning(f"Error in batch sing-box test: {e}, falling back to single tests.")
        return None

    finally:
        if proc:
            try:
                proc.terminate()
                proc.wait(timeout=1.0)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
        if os.path.exists(temp_cfg.name):
            try:
                os.remove(temp_cfg.name)
            except Exception:
                pass


class NodeTester:
    """Batch tester coordinating sing-box testing with concurrency."""

    def __init__(self, db: Database, concurrency: int = 5):
        self.db = db
        self.concurrency = concurrency

    def _test_batch(
        self, nodes: list, enable_speed_test: bool = True, region: str = "cn"
    ) -> Dict[str, int]:
        stats = {"tested": 0, "active": 0, "dead": 0}
        results = None

        # If speed test is disabled, use the ultra-fast sing-box Clash API batch engine
        if not enable_speed_test:
            results = test_nodes_batch_singbox_clash(
                nodes, concurrency=self.concurrency, test_url=TEST_URL, timeout_sec=TEST_TIMEOUT
            )

        # Fallback to single node executor if batch engine was skipped or failed
        if results is None:
            results = []
            with ThreadPoolExecutor(max_workers=self.concurrency) as executor:
                futures = {
                    executor.submit(test_single_node, node, enable_speed_test): node
                    for node in nodes
                }
                for future in as_completed(futures):
                    try:
                        results.append(future.result())
                    except Exception as e:
                        logger.error(f"Error executing test task: {e}")

        # High-speed batch update to database
        self.db.update_test_results_batch(results, region=region)

        for nid, status, delay_ms, speed_mbps, fail_count in results:
            stats["tested"] += 1
            if status == "active":
                stats["active"] += 1
            elif status == "dead":
                stats["dead"] += 1

        return stats

    def run(
        self,
        limit: int = 50,
        enable_speed_test: bool = True,
        region: str = "cn",
        all_nodes: bool = False,
    ) -> Dict[str, int]:
        """
        Run test cycle on candidate nodes from the database.
        If all_nodes is True or limit <= 0: runs in continuous batches until all un-tested nodes in the region are processed.
        Returns statistics: {'tested': count, 'active': count, 'dead': count}
        """
        if all_nodes or limit <= 0:
            logger.info(
                f"Starting FULL test on all candidate nodes (region={region}) with concurrency={self.concurrency}"
            )
            total_stats = {"tested": 0, "active": 0, "dead": 0}
            batch_size = max(self.concurrency * 10, 500)

            logger.info(f"Loading candidate nodes in a single read to minimize database queries...")
            all_candidates = self.db.get_nodes_for_testing(limit=15000, region=region)
            if not all_candidates:
                logger.info(f"No candidate nodes available for testing in region '{region}'.")
                return total_stats

            # Prioritize untested nodes first, then active/retest nodes
            untested = [
                n for n in all_candidates
                if (region == "global" and n.get("global_is_active") is None)
                or (region == "cn" and n.get("cn_is_active") is None)
            ]
            candidates = untested if untested else all_candidates

            total_count = len(candidates)
            total_batches = (total_count + batch_size - 1) // batch_size
            logger.info(f"Loaded {total_count} nodes for testing ({total_batches} batches of {batch_size}).")

            for i in range(0, total_count, batch_size):
                batch = candidates[i : i + batch_size]
                batch_num = i // batch_size + 1
                logger.info(f"Processing batch {batch_num}/{total_batches} ({len(batch)} nodes)...")
                b_stats = self._test_batch(batch, enable_speed_test=enable_speed_test, region=region)
                total_stats["tested"] += b_stats["tested"]
                total_stats["active"] += b_stats["active"]
                total_stats["dead"] += b_stats["dead"]
                logger.info(
                    f"Cumulative progress: Tested {total_stats['tested']}/{total_count} | Active {total_stats['active']} | Dead {total_stats['dead']}"
                )

            logger.info(f"Full test finished. Stats: {total_stats}")
            return total_stats

        nodes = self.db.get_nodes_for_testing(limit=limit, region=region)
        if not nodes:
            logger.info("No nodes available for testing.")
            return {"tested": 0, "active": 0, "dead": 0}

        logger.info(
            f"Starting test on {len(nodes)} nodes (region={region}) with concurrency={self.concurrency}"
        )
        stats = self._test_batch(nodes, enable_speed_test=enable_speed_test, region=region)
        logger.info(f"Testing finished. Stats: {stats}")
        return stats
