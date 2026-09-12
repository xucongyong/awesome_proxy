import base64
from datetime import datetime, timedelta, timezone
import logging
import re
import time
from typing import List, Optional, Set, Tuple
import requests

from config import GITHUB_TOKEN
from database import Database

logger = logging.getLogger("crawler")

NODE_REGEX = re.compile(
    r"(?:vless|vmess|trojan|ss|ssr|hysteria|hysteria2|hy2|tuic|tuic-v5|socks5|wireguard|snell)://[^\s\"\'\<\>\[\]]+",
    re.IGNORECASE,
)

SEARCH_KEYWORDS = ["vless", "vmess", "trojan", "hysteria2", "ss://"]

def extract_nodes_from_text(text: str) -> List[str]:
    """Extract all supported proxy URLs from arbitrary text or raw files."""
    if not text:
        return []

    clean_text = text.strip()
    try:
        if not re.search(r"://", clean_text[:50]):
            decoded = base64.b64decode(clean_text, validate=True).decode("utf-8", errors="ignore")
            if "://" in decoded:
                clean_text += "\n" + decoded
    except Exception:
        pass

    found = NODE_REGEX.findall(clean_text)
    seen: Set[str] = set()
    cleaned: List[str] = []
    for item in found:
        url = item.strip().rstrip(".,;)\"'`")
        # Strip URL fragment (#remark) to merge identical nodes with different remark labels
        if "#" in url:
            url = url.split("#", 1)[0]
        if url and url not in seen:
            seen.add(url)
            cleaned.append(url)
    return cleaned


def generate_historical_time_windows(
    start_year: int = 2019, until_date: str = "2023-01-01"
) -> List[Tuple[str, str]]:
    """
    Generate 6-month segmented time intervals from start_year up to until_date.
    This slices queries to avoid hitting GitHub Search 1000-result ceiling.
    """
    until_dt = datetime.strptime(until_date, "%Y-%m-%d")
    current = datetime(start_year, 1, 1)
    windows: List[Tuple[str, str]] = []

    while current < until_dt:
        # Half-year step: 6 months ~ 182 days
        next_dt = current + timedelta(days=182)
        if next_dt > until_dt:
            next_dt = until_dt
        start_str = current.strftime("%Y-%m-%d")
        end_str = (next_dt - timedelta(days=1)).strftime("%Y-%m-%d")
        if start_str <= end_str:
            windows.append((start_str, end_str))
        current = next_dt

    # Reverse to search newest historical data first (e.g. late 2022 down to 2019)
    windows.reverse()
    return windows


class GitHubCrawler:
    """Crawler to discover proxy nodes from GitHub using the Search API and direct sources."""

    def __init__(self, db: Database, token: Optional[str] = None):
        self.db = db
        self.token = token or GITHUB_TOKEN
        self.session = requests.Session()
        headers = {
            "Accept": "application/vnd.github.v3+json",
            "User-Agent": "get_all_proxy-crawler/1.0",
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        self.session.headers.update(headers)

    def _check_token(self) -> bool:
        if not self.token:
            logger.error(
                "====================================================================\n"
                "[GitHub 鉴权异常: 缺少 GITHUB_TOKEN]\n"
                "GitHub Code Search API (/search/code) 强制要求携带有效 Token 鉴权，匿名访问将统一返回 401。\n"
                "请通过以下任一方式配置 GITHUB_TOKEN 后重试：\n"
                "  1. 临时环境变量: export GITHUB_TOKEN='ghp_xxxx'\n"
                "  2. CLI 参数注入: python3 main.py --all --github-token 'ghp_xxxx'\n"
                "===================================================================="
            )
            return False
        return True

    def _handle_rate_limit(self, resp: requests.Response) -> None:
        """Handle GitHub rate limiting with dynamic wait."""
        reset_time = resp.headers.get("x-ratelimit-reset")
        if reset_time:
            wait_seconds = max(1, int(reset_time) - int(time.time()) + 2)
            logger.warning(
                f"Rate limit exceeded (HTTP {resp.status_code}). Sleeping {wait_seconds}s until reset..."
            )
            time.sleep(min(wait_seconds, 60))
        else:
            logger.warning(f"Rate limited (HTTP {resp.status_code}). Sleeping 30s...")
            time.sleep(30)

    def get_start_date(self) -> str:
        """Determine starting push date for incremental search."""
        last_date = self.db.get_last_pushed_date()
        if last_date:
            return last_date
        three_days_ago = datetime.now(timezone.utc) - timedelta(days=3)
        return three_days_ago.strftime("%Y-%m-%d")

    def _fetch_blob_content(self, git_url: str) -> str:
        """Fetch file content from GitHub git blob API."""
        try:
            resp = self.session.get(git_url, timeout=15)
            if resp.status_code == 200:
                data = resp.json()
                content = data.get("content", "")
                encoding = data.get("encoding", "")
                if encoding == "base64":
                    return base64.b64decode(content).decode("utf-8", errors="ignore")
                return content
            elif resp.status_code in (403, 429):
                self._handle_rate_limit(resp)
        except Exception as e:
            logger.warning(f"Error fetching blob {git_url}: {e}")
        return ""

    def _fetch_raw_content(self, raw_url: str) -> str:
        """Fetch raw content directly via HTTP."""
        try:
            resp = self.session.get(raw_url, timeout=15)
            if resp.status_code == 200:
                return resp.text
        except Exception as e:
            logger.warning(f"Error fetching raw URL {raw_url}: {e}")
        return ""

    def _execute_search_query(self, query: str, max_pages: int = 2) -> List[Tuple[str, str]]:
        """Execute a single GitHub Code Search query across pages, returning (node_url, source_url)."""
        found_nodes: List[Tuple[str, str]] = []
        for page in range(1, max_pages + 1):
            params = {
                "q": query,
                "per_page": 20,
                "page": page,
                "sort": "indexed",
                "order": "desc",
            }
            try:
                resp = self.session.get(
                    "https://api.github.com/search/code", params=params, timeout=20
                )
                if resp.status_code == 401:
                    logger.error(
                        "GitHub API 401 Unauthorized. Your GITHUB_TOKEN is missing or invalid."
                    )
                    return found_nodes

                if resp.status_code in (403, 429):
                    self._handle_rate_limit(resp)
                    continue

                if resp.status_code != 200:
                    logger.warning(f"GitHub Search API returned {resp.status_code} for query: {query}")
                    break

                data = resp.json()
                items = data.get("items", [])
                if not items:
                    break

                for item in items:
                    git_url = item.get("git_url")
                    html_url = item.get("html_url", "")
                    src = html_url or git_url or "github_search"
                    raw_text = ""

                    if git_url:
                        raw_text = self._fetch_blob_content(git_url)
                    elif html_url:
                        raw_url = html_url.replace("github.com", "raw.githubusercontent.com").replace(
                            "/blob/", "/"
                        )
                        raw_text = self._fetch_raw_content(raw_url)

                    if raw_text:
                        nodes = extract_nodes_from_text(raw_text)
                        if nodes:
                            logger.info(f"Extracted {len(nodes)} nodes from {src}")
                            for node in nodes:
                                found_nodes.append((node, src))

                # GitHub authenticated code search rate limit is strictly 10 req/min.
                # Sleeping 6.5s guarantees staying safely below the quota threshold.
                time.sleep(6.5)

            except Exception as e:
                logger.error(f"Search request failed: {e}")
                break

        return found_nodes

    def crawl(self, max_pages_per_keyword: int = 2) -> int:
        """Execute search for proxy nodes across supported protocols."""
        if not self._check_token():
            return 0

        today_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        all_discovered: List[str] = []
        
        # Search directly by protocol scheme
        schemes = ["vless://", "vmess://", "trojan://", "ss://", "hysteria2://"]
        for scheme in schemes:
            logger.info(f"Searching GitHub Code API for: '{scheme}'")
            nodes = self._execute_search_query(scheme, max_pages=max_pages_per_keyword)
            if nodes:
                batch_added = self.db.insert_nodes_batch(nodes)
                logger.info(f"Scheme '{scheme}': Found {len(nodes)}, Saved {batch_added} to DB.")
                all_discovered.extend(nodes)

        total_found = len(all_discovered)
        stats = self.db.get_stats()
        logger.info(f"Crawl completed. Discovered: {total_found}. Total DB nodes now: {stats['total']}")
        self.db.record_fetch_log(today_date, total_found, total_found)
        return total_found

    def _crawl_repositories(
        self,
        since_date: Optional[str] = None,
        until_date: Optional[str] = None,
        max_repos: int = 20,
    ) -> List[Tuple[str, str]]:
        """Search repositories by date range (since .. until) and extract all proxy node files."""
        discovered: List[Tuple[str, str]] = []

        # Build pushed qualifier
        if since_date and until_date:
            date_filter = f"pushed:{since_date}..{until_date}"
        elif until_date:
            date_filter = f"pushed:<{until_date}"
        elif since_date:
            date_filter = f"pushed:>{since_date}"
        else:
            date_filter = ""

        queries = [
            f"v2ray OR clash OR shadowsocks {date_filter}".strip(),
            f"free-proxy OR proxy-list OR vless {date_filter}".strip(),
        ]
        target_patterns = re.compile(r"\.(?:txt|yaml|yml|json|md|conf)$", re.IGNORECASE)

        for q in queries:
            logger.info(f"Searching repositories with query: '{q}'")
            try:
                resp = self.session.get(
                    "https://api.github.com/search/repositories",
                    params={"q": q, "sort": "stars", "order": "desc", "per_page": min(max_repos, 50)},
                    timeout=20,
                )
                if resp.status_code != 200:
                    logger.warning(f"Repository search returned status {resp.status_code}")
                    continue

                repos = resp.json().get("items", [])
                for repo in repos:
                    full_name = repo.get("full_name")
                    default_branch = repo.get("default_branch", "master")
                    pushed_at = repo.get("pushed_at", "")[:10]
                    logger.info(f"Mining repository: {full_name} (pushed: {pushed_at})")

                    # Step 1: Check standard root files
                    root_files = ["README.md", "readme.md", "nodes.txt", "sub.txt", "config.json", "clash.yaml", "v2.txt", "all.txt"]
                    for fname in root_files:
                        raw_url = f"https://raw.githubusercontent.com/{full_name}/{default_branch}/{fname}"
                        src_url = f"https://github.com/{full_name}/blob/{default_branch}/{fname}"
                        content = self._fetch_raw_content(raw_url)
                        if content:
                            nodes = extract_nodes_from_text(content)
                            if nodes:
                                logger.info(f" -> Found {len(nodes)} nodes in {src_url}")
                                for node in nodes:
                                    discovered.append((node, src_url))

                    # Step 2: Query repository Git tree for nested txt/sub files
                    try:
                        tree_url = f"https://api.github.com/repos/{full_name}/git/trees/{default_branch}?recursive=1"
                        tree_resp = self.session.get(tree_url, timeout=15)
                        if tree_resp.status_code == 200:
                            tree_items = tree_resp.json().get("tree", [])
                            for item in tree_items[:30]:
                                path = item.get("path", "")
                                if target_patterns.search(path) and item.get("type") == "blob":
                                    git_url = item.get("url")
                                    src_url = f"https://github.com/{full_name}/blob/{default_branch}/{path}"
                                    if git_url:
                                        blob_text = self._fetch_blob_content(git_url)
                                        if blob_text:
                                            sub_nodes = extract_nodes_from_text(blob_text)
                                            if sub_nodes:
                                                logger.info(f" -> Extracted {len(sub_nodes)} nodes from tree: {src_url}")
                                                for sub_node in sub_nodes:
                                                    discovered.append((sub_node, src_url))
                    except Exception as e:
                        logger.debug(f"Could not read tree for {full_name}: {e}")

                    time.sleep(1.0)
            except Exception as e:
                logger.error(f"Error querying repositories: {e}")

        return discovered

    def crawl_range(
        self,
        since_date: Optional[str] = None,
        until_date: Optional[str] = None,
        max_pages: int = 3,
        max_repos: int = 20,
    ) -> int:
        """
        Execute deep crawl across any arbitrary date range.
        Examples:
          - 2016 to 2023: since='2016-01-01', until='2023-01-01'
          - 2023 to 2026: since='2023-01-01', until='2026-09-12'
        """
        if not self._check_token():
            return 0

        date_desc = f"{since_date or 'earliest'} -> {until_date or 'latest'}"
        logger.info(f"--- Starting Deep Crawl for Time Window: {date_desc} ---")
        all_discovered: List[str] = []

        # Phase 1: Repository Mining with pushed date range
        logger.info(f"Phase 1: Mining repositories in range ({date_desc})...")
        repo_nodes = self._crawl_repositories(
            since_date=since_date, until_date=until_date, max_repos=max_repos
        )
        if repo_nodes:
            added = self.db.insert_nodes_batch(repo_nodes)
            logger.info(f"Phase 1 finished. Discovered {len(repo_nodes)}, Saved {added} new unique nodes.")
            all_discovered.extend(repo_nodes)

        # Phase 2: Direct Protocol Code Search
        logger.info(f"Phase 2: Code search across proxy protocols (pages={max_pages})...")
        schemes = ["vless://", "vmess://", "trojan://", "ss://", "hysteria2://"]
        for scheme in schemes:
            logger.info(f"Searching code for scheme: '{scheme}'")
            nodes = self._execute_search_query(scheme, max_pages=max_pages)
            if nodes:
                added = self.db.insert_nodes_batch(nodes)
                logger.info(f"Saved {added} new nodes from scheme '{scheme}'.")
                all_discovered.extend(nodes)

        total_found = len(all_discovered)
        stats = self.db.get_stats()
        logger.info(
            f"Range crawl complete ({date_desc}). Found {total_found} nodes. Total DB unique nodes: {stats['total']}"
        )
        return total_found

    def crawl_history(
        self, until_date: str = "2023-01-01", start_year: int = 2016, max_pages_per_window: int = 3
    ) -> int:
        """Convenience method for backward compatibility."""
        return self.crawl_range(
            since_date=f"{start_year}-01-01",
            until_date=until_date,
            max_pages=max_pages_per_window,
            max_repos=25,
        )

    def fetch_from_url(self, url: str) -> int:
        """Directly fetch and import proxy nodes from any raw subscription/file URL."""
        logger.info(f"Directly fetching subscription/nodes from: {url}")
        content = self._fetch_raw_content(url)
        if not content:
            logger.warning(f"No content returned from {url}")
            return 0

        nodes = extract_nodes_from_text(content)
        added = self.db.insert_nodes_batch(nodes)
        logger.info(f"Direct import from {url}: {len(nodes)} found, {added} newly saved to DB.")
        return added
