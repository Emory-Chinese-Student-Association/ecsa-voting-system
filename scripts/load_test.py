#!/usr/bin/env python3
import argparse
import concurrent.futures
import socket
import statistics
import sys
import tempfile
import threading
import time
from collections import Counter
from pathlib import Path

import requests
from werkzeug.serving import make_server

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import app as vote_app


REQUEST_TIMEOUT_SECONDS = 20


def find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def percentile(values, ratio: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(round((len(ordered) - 1) * ratio))))
    return ordered[index]


def build_ballot_payload(token: str, voter_index: int) -> dict:
    payload = {"token": token}
    for category in vote_app.CONFIG.BALLOT_CATEGORIES:
        start = voter_index % len(category.candidates)
        choices = [
            category.candidates[(start + offset) % len(category.candidates)]
            for offset in range(category.max_choices)
        ]
        payload[vote_app.category_field_name(category)] = choices
    return payload


def wait_for_server(base_url: str) -> None:
    deadline = time.time() + 10
    while time.time() < deadline:
        try:
            response = requests.get(f"{base_url}/votes", timeout=2)
            if response.status_code == 200:
                return
        except requests.RequestException:
            pass
        time.sleep(0.1)
    raise RuntimeError("server did not start in time")


def create_token(role: str, index: int) -> str:
    prefix = {"chair": "C", "minister": "M", "member": "U"}[role]
    return f"{prefix}-LOAD{index:06d}"


def role_for_index(index: int) -> str:
    if index % 10 == 0:
        return "chair"
    if index % 4 == 0:
        return "minister"
    return "member"


def prepare_isolated_app(token_count: int):
    tempdir = tempfile.TemporaryDirectory(prefix="ecsa-load-test-")
    db_path = Path(tempdir.name) / "load_test.db"
    export_dir = Path(tempdir.name) / "exports"
    export_dir.mkdir(parents=True, exist_ok=True)

    vote_app.CONFIG.DB_PATH = str(db_path)
    vote_app.CONFIG.EXPORT_DIR = str(export_dir)
    vote_app.CONFIG.AUTO_OPEN_ADMIN = False
    vote_app.CONFIG.PUBLIC_BASE_URL = "http://127.0.0.1"

    vote_app.db_init()

    tokens = []
    for index in range(token_count):
        role = role_for_index(index)
        token = create_token(role, index)
        vote_app.insert_token(token, role, vote_app.default_role_weight(role), note=f"load_test_{index}")
        tokens.append(token)

    vote_app.set_state("open")
    return tempdir, tokens


def get_db_counts() -> tuple[int, int]:
    conn = vote_app.db_connect()
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) AS count FROM votes")
    vote_count = int(cur.fetchone()["count"])
    cur.execute("SELECT COUNT(*) AS count FROM tokens WHERE used=1")
    used_token_count = int(cur.fetchone()["count"])
    conn.close()
    return vote_count, used_token_count


def classify_submission_response(response: requests.Response) -> str:
    if response.status_code >= 500:
        return "http_5xx"
    if "投票成功" in response.text:
        return "success"
    if "系统当前较忙" in response.text:
        return "busy_retry"
    if "提交失败" in response.text:
        return "submit_error"
    return "unexpected"


def run_voter_flow(base_url: str, token: str, voter_index: int) -> dict:
    session = requests.Session()
    start = time.perf_counter()
    try:
        ballot_response = session.get(
            f"{base_url}/votes/ballot",
            params={"token": token},
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
        if ballot_response.status_code >= 500:
            return {
                "outcome": "http_5xx",
                "latency_ms": (time.perf_counter() - start) * 1000,
                "status_code": ballot_response.status_code,
            }
        if "按类别完成整张选票" not in ballot_response.text:
            if "该投票码已使用" in ballot_response.text or "投票码无效" in ballot_response.text:
                return {
                    "outcome": "ballot_rejected",
                    "latency_ms": (time.perf_counter() - start) * 1000,
                    "status_code": ballot_response.status_code,
                }
            return {
                "outcome": "ballot_error",
                "latency_ms": (time.perf_counter() - start) * 1000,
                "status_code": ballot_response.status_code,
            }

        submit_response = session.post(
            f"{base_url}/votes/submit",
            data=build_ballot_payload(token, voter_index),
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
        return {
            "outcome": classify_submission_response(submit_response),
            "latency_ms": (time.perf_counter() - start) * 1000,
            "status_code": submit_response.status_code,
        }
    except requests.RequestException as exc:
        return {
            "outcome": "request_exception",
            "latency_ms": (time.perf_counter() - start) * 1000,
            "status_code": 0,
            "error": str(exc),
        }
    finally:
        session.close()


def run_concurrent_flows(base_url: str, tokens: list[str], concurrency: int) -> list[dict]:
    with concurrent.futures.ThreadPoolExecutor(max_workers=concurrency) as executor:
        futures = [
            executor.submit(run_voter_flow, base_url, token, index)
            for index, token in enumerate(tokens)
        ]
        return [future.result() for future in concurrent.futures.as_completed(futures)]


def run_duplicate_race(base_url: str, token: str, concurrency: int) -> list[dict]:
    with concurrent.futures.ThreadPoolExecutor(max_workers=concurrency) as executor:
        futures = [
            executor.submit(run_voter_flow, base_url, token, index)
            for index in range(concurrency)
        ]
        return [future.result() for future in concurrent.futures.as_completed(futures)]


def summarize_results(name: str, results: list[dict]) -> dict:
    outcomes = Counter(item["outcome"] for item in results)
    latencies = [item["latency_ms"] for item in results]
    summary = {
        "name": name,
        "requests": len(results),
        "outcomes": dict(outcomes),
        "avg_ms": round(statistics.mean(latencies), 2) if latencies else 0.0,
        "p95_ms": round(percentile(latencies, 0.95), 2) if latencies else 0.0,
        "max_ms": round(max(latencies), 2) if latencies else 0.0,
    }
    return summary


def print_summary(summary: dict, vote_count: int, used_token_count: int) -> None:
    print(f"\nScenario: {summary['name']}")
    print(f"  Requests: {summary['requests']}")
    print(f"  Outcomes: {summary['outcomes']}")
    print(f"  Avg latency: {summary['avg_ms']} ms")
    print(f"  P95 latency: {summary['p95_ms']} ms")
    print(f"  Max latency: {summary['max_ms']} ms")
    print(f"  Votes recorded: {vote_count}")
    print(f"  Used tokens: {used_token_count}")


def run_server_and_test(tokens: list[str], test_runner):
    host = "127.0.0.1"
    port = find_free_port()
    base_url = f"http://{host}:{port}"
    vote_app.CONFIG.PUBLIC_BASE_URL = f"{base_url}/votes"

    server = make_server(host, port, vote_app.app, threaded=True)
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    try:
        wait_for_server(base_url)
        return test_runner(base_url)
    finally:
        server.shutdown()
        server_thread.join(timeout=5)


def run_unique_scenario(total_voters: int, concurrency: int) -> int:
    tempdir, tokens = prepare_isolated_app(total_voters)
    try:
        results = run_server_and_test(
            tokens,
            lambda base_url: run_concurrent_flows(base_url, tokens, concurrency),
        )
        vote_count, used_token_count = get_db_counts()
        summary = summarize_results(f"unique_{total_voters}_c{concurrency}", results)
        print_summary(summary, vote_count, used_token_count)

        success_count = summary["outcomes"].get("success", 0)
        expected_votes_per_ballot = sum(category.max_choices for category in vote_app.CONFIG.BALLOT_CATEGORIES)
        if success_count != total_voters:
            print("  FAIL: not every voter completed successfully.")
            return 1
        if vote_count != success_count * expected_votes_per_ballot:
            print("  FAIL: recorded vote rows do not match expected ballot size.")
            return 1
        if used_token_count != success_count:
            print("  FAIL: used token count does not match successful ballots.")
            return 1
        if summary["outcomes"].get("http_5xx", 0) > 0 or summary["outcomes"].get("request_exception", 0) > 0:
            print("  FAIL: server produced 5xx responses or request exceptions.")
            return 1
        print("  PASS")
        return 0
    finally:
        tempdir.cleanup()


def run_duplicate_scenario(concurrency: int) -> int:
    tempdir, tokens = prepare_isolated_app(1)
    token = tokens[0]
    try:
        results = run_server_and_test(
            tokens,
            lambda base_url: run_duplicate_race(base_url, token, concurrency),
        )
        vote_count, used_token_count = get_db_counts()
        summary = summarize_results(f"duplicate_race_c{concurrency}", results)
        print_summary(summary, vote_count, used_token_count)

        success_count = summary["outcomes"].get("success", 0)
        rejected_count = summary["outcomes"].get("submit_error", 0) + summary["outcomes"].get("ballot_rejected", 0)
        expected_votes_per_ballot = sum(category.max_choices for category in vote_app.CONFIG.BALLOT_CATEGORIES)
        if success_count != 1:
            print("  FAIL: duplicate race should allow exactly one success.")
            return 1
        if rejected_count != concurrency - 1:
            print("  FAIL: duplicate race should reject all later submissions cleanly.")
            return 1
        if vote_count != expected_votes_per_ballot or used_token_count != 1:
            print("  FAIL: duplicate race wrote unexpected database state.")
            return 1
        if summary["outcomes"].get("http_5xx", 0) > 0 or summary["outcomes"].get("request_exception", 0) > 0:
            print("  FAIL: server produced 5xx responses or request exceptions.")
            return 1
        print("  PASS")
        return 0
    finally:
        tempdir.cleanup()


def main() -> int:
    parser = argparse.ArgumentParser(description="Run concurrent load tests against the local voting app.")
    parser.add_argument("--scenario", choices=["unique", "duplicate"], required=True)
    parser.add_argument("--voters", type=int, default=50, help="Total unique voters for the unique scenario.")
    parser.add_argument("--concurrency", type=int, default=50, help="Concurrent worker count.")
    args = parser.parse_args()

    if args.scenario == "unique":
        return run_unique_scenario(args.voters, args.concurrency)
    return run_duplicate_scenario(args.concurrency)


if __name__ == "__main__":
    raise SystemExit(main())
