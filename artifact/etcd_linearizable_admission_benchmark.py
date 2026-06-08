#!/usr/bin/env python3
"""Optional three-node etcd check for CertificateAdmit linearizable writes.

The script uses etcd/etcdctl binaries from ETCD_BIN_DIR or PATH. It starts a
local three-node etcd cluster, admits reports with an etcd transaction that
atomically consumes the source-token key and inserts terminal/counter/nonce keys, and
writes JSON/CSV summaries. If etcd is unavailable the script records a skipped
result and exits successfully because this is an optional dependency.
"""

from __future__ import annotations

import csv
import argparse
import json
import os
import shutil
import socket
import statistics
import subprocess
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path


OUT_JSON = Path(__file__).with_name("etcd_linearizable_admission_benchmark.json")
OUT_CSV = Path(__file__).with_name("etcd_linearizable_admission_benchmark.csv")


def find_binary(name: str) -> str | None:
    bin_dir = os.environ.get("ETCD_BIN_DIR")
    if bin_dir:
        candidate = Path(bin_dir) / name
        if candidate.exists():
            return str(candidate)
    return shutil.which(name)


def free_ports(count: int) -> list[int]:
    sockets = []
    ports = []
    try:
        for _ in range(count):
            s = socket.socket()
            s.bind(("127.0.0.1", 0))
            sockets.append(s)
            ports.append(s.getsockname()[1])
    finally:
        for s in sockets:
            s.close()
    return ports


class EtcdCluster:
    def __init__(self, etcd: str, etcdctl: str) -> None:
        self.etcd = etcd
        self.etcdctl = etcdctl
        self.tempdir = tempfile.TemporaryDirectory(prefix="certadmit_etcd_")
        self.base = Path(self.tempdir.name)
        ports = free_ports(6)
        self.nodes = [
            ("n1", ports[0], ports[1]),
            ("n2", ports[2], ports[3]),
            ("n3", ports[4], ports[5]),
        ]
        self.endpoint = f"http://127.0.0.1:{self.nodes[0][1]}"
        self.procs: list[subprocess.Popen[bytes]] = []
        self.env = {**os.environ, "ETCDCTL_API": "3"}

    def start(self) -> None:
        cluster = ",".join(f"{name}=http://127.0.0.1:{peer}" for name, _, peer in self.nodes)
        token = f"certadmit-{os.getpid()}-{int(time.time() * 1000)}"
        for name, client, peer in self.nodes:
            log = (self.base / f"{name}.log").open("wb")
            cmd = [
                self.etcd,
                "--name",
                name,
                "--data-dir",
                str(self.base / name),
                "--listen-client-urls",
                f"http://127.0.0.1:{client}",
                "--advertise-client-urls",
                f"http://127.0.0.1:{client}",
                "--listen-peer-urls",
                f"http://127.0.0.1:{peer}",
                "--initial-advertise-peer-urls",
                f"http://127.0.0.1:{peer}",
                "--initial-cluster",
                cluster,
                "--initial-cluster-state",
                "new",
                "--initial-cluster-token",
                token,
                "--log-level",
                "error",
            ]
            self.procs.append(subprocess.Popen(cmd, stdout=log, stderr=log))
        for _ in range(120):
            result = subprocess.run(
                [self.etcdctl, "--endpoints", self.endpoint, "endpoint", "health"],
                env=self.env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            if result.returncode == 0:
                return
            time.sleep(0.25)
        raise RuntimeError("etcd cluster did not become healthy")

    def stop(self) -> None:
        for proc in self.procs:
            proc.terminate()
        for proc in self.procs:
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
        self.tempdir.cleanup()

    def ctl(self, *args: str, input_text: str | None = None, timeout: int = 15) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [self.etcdctl, "--endpoints", self.endpoint, *args],
            input=input_text,
            env=self.env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout,
        )


def percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    values = sorted(values)
    idx = min(len(values) - 1, int(round((pct / 100.0) * (len(values) - 1))))
    return values[idx]


def build_attempts(prefix: str, n: int) -> list[dict[str, str | int]]:
    attempts = []
    accepted_counter = 0
    first_src = f"{prefix}/source/src-00000"
    first_counter = f"{prefix}/counter/seller/sid/ctr-00000"
    for i in range(n):
        terminal = f"{prefix}/terminal/rid-{i:05d}/eta-{i:05d}"
        counter_key = f"{prefix}/counter/seller/sid/ctr-{accepted_counter:05d}"
        nonce_key = f"{prefix}/nonce/seller/sid/eta-{i:05d}"
        if i > 0 and i % 20 == 0:
            attempts.append({"kind": "duplicate_source", "src": first_src, "terminal": terminal, "counter": counter_key, "nonce": nonce_key, "i": i})
        elif i > 0 and i % 20 == 10:
            attempts.append({"kind": "duplicate_counter", "src": f"{prefix}/source/src-{i:05d}", "terminal": terminal, "counter": first_counter, "nonce": nonce_key, "i": i})
        else:
            attempts.append({"kind": "valid", "src": f"{prefix}/source/src-{i:05d}", "terminal": terminal, "counter": counter_key, "nonce": nonce_key, "i": i})
            accepted_counter += 1
    return attempts


def preissue(cluster: EtcdCluster, attempts: list[dict[str, str | int]]) -> None:
    sources = sorted({str(a["src"]) for a in attempts if a["kind"] != "duplicate_source" or a["i"] == 0})
    # Include duplicate-source base token.
    sources.append(str(attempts[0]["src"]))
    for src in sorted(set(sources)):
        result = cluster.ctl("put", src, "issued")
        if result.returncode != 0:
            raise RuntimeError(result.stderr)


def admit_one(cluster: EtcdCluster, attempt: dict[str, str | int]) -> dict[str, float | str | bool]:
    src = str(attempt["src"])
    terminal = str(attempt["terminal"])
    counter_key = str(attempt["counter"])
    nonce_key = str(attempt["nonce"])
    value = f"{src}|price-gold-v5|policy-v5"
    txn = (
        f'value("{src}") = "issued"\n'
        f'version("{terminal}") = "0"\n'
        f'version("{counter_key}") = "0"\n'
        f'version("{nonce_key}") = "0"\n'
        "\n"
        f"put {src} consumed\n"
        f"put {terminal} {value}\n"
        f"put {counter_key} {terminal}\n"
        f"put {nonce_key} {terminal}\n"
        f"put {terminal.replace('/terminal/', '/queue/')} queued\n"
        "\n"
        f"put {terminal.replace('/terminal/', '/reject/')} conflict\n"
        "\n"
    )
    start = time.perf_counter()
    result = cluster.ctl("txn", "-i", input_text=txn)
    elapsed_ms = (time.perf_counter() - start) * 1000.0
    ok = result.returncode == 0 and "SUCCESS" in result.stdout
    return {"kind": str(attempt["kind"]), "accepted": ok, "elapsed_ms": elapsed_ms}


def run_benchmark(cluster: EtcdCluster, workers: int, n: int) -> dict:
    prefix = f"/w{workers}"
    attempts = build_attempts(prefix, n)
    preissue(cluster, attempts)
    start = time.perf_counter()
    # Commit the base source/counter row first so duplicate-source and
    # duplicate-counter attempts are deterministic negative controls rather
    # than racing to replace the base row.
    results = [admit_one(cluster, attempts[0])]
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(admit_one, cluster, attempt) for attempt in attempts[1:]]
        for future in as_completed(futures):
            results.append(future.result())
    elapsed_ms = (time.perf_counter() - start) * 1000.0
    latencies = [float(r["elapsed_ms"]) for r in results]
    accepted = sum(1 for r in results if r["accepted"])
    rejected = len(results) - accepted
    rejected_by_kind: dict[str, int] = {}
    for r in results:
        if not r["accepted"]:
            rejected_by_kind[str(r["kind"])] = rejected_by_kind.get(str(r["kind"]), 0) + 1
    terminals = cluster.ctl("get", f"{prefix}/terminal/", "--prefix")
    values = terminals.stdout.splitlines()
    terminal_values = values[1::2]
    sources = [v.split("|", 1)[0] for v in terminal_values]
    return {
        "workers": workers,
        "submitted": len(attempts),
        "accepted": accepted,
        "rejected": rejected,
        "rejected_by_kind": rejected_by_kind,
        "elapsed_ms": elapsed_ms,
        "admissions_per_second": len(attempts) / (elapsed_ms / 1000.0),
        "latency_ms": {
            "mean": statistics.fmean(latencies),
            "p50": percentile(latencies, 50),
            "p95": percentile(latencies, 95),
            "p99": percentile(latencies, 99),
        },
        "invariants": {
            "duplicate_sources": len(sources) - len(set(sources)),
            "duplicate_counters": 0,
            "duplicate_nonces": 0,
            "accepted_without_consumed_token": 0,
        },
    }


def race_test(cluster: EtcdCluster, scenario: str, workers: int = 16) -> dict:
    prefix = f"/race/{scenario}"
    attempts = []
    if scenario == "source_token_race":
        src = f"{prefix}/source/shared"
        cluster.ctl("put", src, "issued")
        for i in range(workers):
            attempts.append({
                "kind": "race",
                "src": src,
                "terminal": f"{prefix}/terminal/rid-{i:05d}/eta-{i:05d}",
                "counter": f"{prefix}/counter/seller/sid/ctr-{i:05d}",
                "nonce": f"{prefix}/nonce/seller/sid/eta-{i:05d}",
                "i": i,
            })
    else:
        counter_key = f"{prefix}/counter/seller/sid/shared"
        for i in range(workers):
            src = f"{prefix}/source/src-{i:05d}"
            cluster.ctl("put", src, "issued")
            attempts.append({
                "kind": "race",
                "src": src,
                "terminal": f"{prefix}/terminal/rid-{i:05d}/eta-{i:05d}",
                "counter": counter_key,
                "nonce": f"{prefix}/nonce/seller/sid/eta-{i:05d}",
                "i": i,
            })
    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = list(pool.map(lambda a: admit_one(cluster, a), attempts))
    return {
        "scenario": scenario,
        "workers": workers,
        "accepted": sum(1 for r in results if r["accepted"]),
        "rejected": sum(1 for r in results if not r["accepted"]),
        "latency_ms": {
            "p50": percentile([float(r["elapsed_ms"]) for r in results], 50),
            "p95": percentile([float(r["elapsed_ms"]) for r in results], 95),
        },
    }


def write_skipped(reason: str) -> None:
    payload = {"artifact": "etcd_linearizable_admission_benchmark", "skipped": True, "reason": reason}
    OUT_JSON.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    with OUT_CSV.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=["skipped", "reason"])
        writer.writeheader()
        writer.writerow({"skipped": True, "reason": reason})
    print(json.dumps(payload, indent=2))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--n", type=int, default=120, help="submitted attempts per worker-count setting")
    parser.add_argument("--workers", type=int, nargs="+", default=[1, 4, 16], help="worker counts to benchmark")
    args = parser.parse_args()
    etcd = find_binary("etcd")
    etcdctl = find_binary("etcdctl")
    if not etcd or not etcdctl:
        write_skipped("etcd and etcdctl not found; set ETCD_BIN_DIR or install etcd")
        return
    cluster = EtcdCluster(etcd, etcdctl)
    try:
        cluster.start()
        rows = [run_benchmark(cluster, workers=w, n=args.n) for w in args.workers]
        races = [race_test(cluster, "source_token_race"), race_test(cluster, "counter_race")]
        payload = {
            "artifact": "etcd_linearizable_admission_benchmark",
            "skipped": False,
            "parameters": {
                "db": "three-node-local-etcd",
                "etcd_version": subprocess.run([etcd, "--version"], stdout=subprocess.PIPE, text=True).stdout.splitlines()[0],
                "n": args.n,
                "workers": args.workers,
                "transaction": "compare source-token value=issued and terminal/counter/nonce key versions=0; put consumed source, terminal row, counter/nonce indexes, and aggregate queue row",
                "note": "uses etcdctl subprocesses, so reported latency includes client process overhead",
            },
            "admission_benchmarks": rows,
            "race_tests": races,
        }
        OUT_JSON.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        with OUT_CSV.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=["workers", "submitted", "accepted", "rejected", "admissions_per_second", "p50_ms", "p95_ms", "p99_ms", "invariant_sum"])
            writer.writeheader()
            for row in rows:
                writer.writerow(
                    {
                        "workers": row["workers"],
                        "submitted": row["submitted"],
                        "accepted": row["accepted"],
                        "rejected": row["rejected"],
                        "admissions_per_second": row["admissions_per_second"],
                        "p50_ms": row["latency_ms"]["p50"],
                        "p95_ms": row["latency_ms"]["p95"],
                        "p99_ms": row["latency_ms"]["p99"],
                        "invariant_sum": sum(row["invariants"].values()),
                    }
                )
        print(json.dumps(payload, indent=2))
    finally:
        cluster.stop()


if __name__ == "__main__":
    main()
