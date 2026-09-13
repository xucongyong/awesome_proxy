import argparse
import logging
import os
import sys
from pathlib import Path

from crawler import GitHubCrawler, extract_nodes_from_text
from database import get_database
from exporter import Exporter
from parser import parse_node_url
from tester import NodeTester


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] (%(name)s) %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("main")

def setup_cli() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Automated Proxy Crawler, sing-box Tester & Cloudflare D1 Manager"
    )
    parser.add_argument("--crawl", action="store_true", help="Run GitHub incremental crawl")
    parser.add_argument(
        "--crawl-history",
        action="store_true",
        help="Run historical crawl for data pushed before 2023-01-01 (or --until date)",
    )
    parser.add_argument(
        "--crawl-range",
        action="store_true",
        help="Run crawl across specified time window using --since and --until",
    )
    parser.add_argument(
        "--since",
        type=str,
        default=None,
        help="Start date for crawl (e.g. 2016-01-01 or 2023-01-01)",
    )
    parser.add_argument(
        "--until",
        type=str,
        default=None,
        help="End date for crawl (e.g. 2023-01-01 or 2026-09-12)",
    )
    parser.add_argument(
        "--pages",
        type=int,
        default=3,
        help="Pagination depth for code search per protocol (default: 3)",
    )
    parser.add_argument(
        "--max-repos",
        type=int,
        default=25,
        help="Maximum repositories to mine per query (default: 25)",
    )
    parser.add_argument(
        "--github-token",
        type=str,
        default=None,
        help="GitHub Personal Access Token for authentication",
    )
    parser.add_argument("--test", action="store_true", help="Run sing-box delay/speed tests")
    parser.add_argument(
        "--export", action="store_true", help="Export active nodes to subscription and config"
    )
    parser.add_argument(
        "--all", action="store_true", help="Execute complete pipeline (crawl -> test -> export)"
    )
    parser.add_argument("--init-db", action="store_true", help="Initialize D1 or SQLite database schema")
    parser.add_argument("--stats", action="store_true", help="Display current database node counts")
    parser.add_argument("--limit", type=int, default=50, help="Node limit for testing/export (default: 50)")
    parser.add_argument("--concurrency", type=int, default=5, help="Concurrency for node testing (default: 5)")
    parser.add_argument("--local", action="store_true", help="Force using local SQLite instead of Cloudflare D1")
    parser.add_argument(
        "--import-file", type=str, default=None, help="Import nodes from a local file or subscription"
    )
    parser.add_argument(
        "--import-url", type=str, default=None, help="Import nodes directly from a remote raw URL"
    )
    parser.add_argument(
        "--no-speed-test",
        action="store_true",
        help="Skip download bandwidth speed test (only test delay/connectivity)",
    )
    parser.add_argument(
        "--top",
        type=int,
        nargs="?",
        const=20,
        default=None,
        help="Display top N active nodes ranked by latency and speed (default: 20)",
    )
    parser.add_argument(
        "--region",
        type=str,
        choices=["cn", "global"],
        default="cn",
        help="Testing perspective/target region: 'cn' (China domestic) or 'global' (overseas VPS) (default: cn)",
    )
    parser.add_argument(
        "--clean-dead",
        action="store_true",
        help="Purge old dead nodes that have been inactive for more than --days (default: 30 days)",
    )
    parser.add_argument(
        "--all-nodes",
        action="store_true",
        help="Run continuous testing across ALL un-tested candidate nodes in batches without stopping",
    )
    parser.add_argument(
        "--days",
        type=int,
        default=30,
        help="Days threshold for purging dead nodes (default: 30)",
    )
    return parser








def main() -> None:
    parser = setup_cli()
    args = parser.parse_args()

    if len(sys.argv) == 1:
        parser.print_help()
        sys.exit(0)

    token = args.github_token or os.getenv("GITHUB_TOKEN")

    db = get_database(force_local=args.local)

    if args.init_db:
        logger.info("Initializing database tables...")
        db.init_db()
        logger.info("Database schema initialized successfully.")

    if args.clean_dead:
        logger.info(f"Purging dead nodes inactive for more than {args.days} days...")
        deleted = db.clean_dead_nodes(days=args.days)
        logger.info(f"Purged {deleted} dead nodes from database.")


    if args.import_file:
        file_path = Path(args.import_file)
        if not file_path.exists():
            logger.error(f"Import file does not exist: {file_path}")
            sys.exit(1)
        content = file_path.read_text(encoding="utf-8", errors="ignore")
        nodes = extract_nodes_from_text(content)
        added = db.insert_nodes_batch(nodes)
        logger.info(f"Imported from {file_path}: {len(nodes)} found, {added} newly added.")

    if args.import_url:
        crawler = GitHubCrawler(db=db, token=token)
        crawler.fetch_from_url(args.import_url)

    if args.crawl_range:
        crawler = GitHubCrawler(db=db, token=token)
        crawler.crawl_range(
            since_date=args.since,
            until_date=args.until,
            max_pages=args.pages,
            max_repos=args.max_repos,
        )

    if args.crawl_history:
        until_date = args.until or "2023-01-01"
        logger.info(f"--- Starting Historical Crawl (Until: {until_date}) ---")
        crawler = GitHubCrawler(db=db, token=token)
        crawler.crawl_history(
            until_date=until_date,
            start_year=2016,
            max_pages_per_window=args.pages,
        )

    if args.crawl or args.all:
        logger.info("--- Starting Module 1: GitHub Crawler ---")
        crawler = GitHubCrawler(db=db, token=token)
        crawler.crawl(max_pages_per_keyword=args.pages)

    if args.test or args.all:
        logger.info(f"--- Starting Module 2: sing-box Tester (Region: {args.region}) ---")
        tester = NodeTester(db=db, concurrency=args.concurrency)
        tester.run(
            limit=args.limit,
            enable_speed_test=not args.no_speed_test,
            region=args.region,
            all_nodes=args.all_nodes,
        )

    if args.export or args.all:
        logger.info(f"--- Starting Module 3: Exporter (Region: {args.region}) ---")
        exporter = Exporter(db=db)
        out = exporter.export(limit=args.limit, region=args.region)
        print("\nExport completed:")
        for k, v in out.items():
            print(f"  - {k}: {v}")

    if args.stats:
        stats = db.get_stats()
        print("\n--- Current Database Node Statistics ---")
        print(f"  Total Nodes:    {stats['total']}")
        print(f"  Active Nodes:   {stats['active']}")
        print(f"  Untested Nodes: {stats['untested']}")
        print(f"  Dead Nodes:     {stats['dead']}\n")

    if args.top:
        limit = args.top
        active_nodes = db.get_active_nodes(limit=limit, region=args.region)
        print(f"\n{'='*82}")
        print(f"       TOP {len(active_nodes)} 优质存活节点画像 ({args.region.upper()} 视角: 按延迟升序、带宽降序)")
        print(f"{'='*82}")

        if not active_nodes:
            print("  [提示] 当前库内暂无已测存活的节点。请先运行: python3 main.py --test 进行批量测速。")
        else:
            print(f"{'排名':<4} | {'协议':<10} | {'延迟(ms)':<9} | {'带宽(Mbps)':<11} | {'质量评级':<10} | 节点地址")
            print("-" * 82)
            for idx, node in enumerate(active_nodes, start=1):
                delay = node.get("delay_ms", 0)
                speed = node.get("speed_mbps", 0.0)
                proto = node.get("protocol", "unknown")
                parsed = parse_node_url(node.get("node_url", ""))
                server = parsed.get("server", "unknown") if parsed else "unknown"
                port = parsed.get("server_port", "") if parsed else ""
                host_port = f"{server}:{port}" if port else server

                # 节点质量评级
                if delay < 300 and (speed > 10.0 or speed == 0.0):
                    grade = "⭐⭐⭐⭐⭐ 极优"
                elif delay < 600:
                    grade = "⭐⭐⭐⭐ 良好"
                elif delay < 1200:
                    grade = "⭐⭐⭐ 普通"
                else:
                    grade = "⭐⭐ 较慢"

                print(f"#{idx:<3} | {proto:<10} | {str(delay) + 'ms':<9} | {str(speed) + 'Mbps':<11} | {grade:<10} | {host_port}")
        print(f"{'='*82}\n")



if __name__ == "__main__":
    main()
