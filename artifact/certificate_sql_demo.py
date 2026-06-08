#!/usr/bin/env python3
"""SQLite certificate database demo.

This complements `certificate_demo.py` by materializing accepted/rejected
certificate rows into relational tables and running audit/pricing queries.
"""

from __future__ import annotations

import json
import shutil
import sqlite3
import tempfile
from pathlib import Path

from certificate_demo import run_demo


DB = Path(__file__).with_name("certificate_audit_demo.sqlite")
OUT = Path(__file__).with_name("certificate_sql_queries.json")


SCHEMA = """
DROP TABLE IF EXISTS certificates;
DROP TABLE IF EXISTS rejections;
DROP TABLE IF EXISTS source_tokens;
DROP TABLE IF EXISTS price_table;

CREATE TABLE certificates (
  rid TEXT NOT NULL,
  seller TEXT NOT NULL,
  session TEXT NOT NULL,
  source TEXT NOT NULL,
  workload TEXT NOT NULL,
  algorithm TEXT NOT NULL,
  policy TEXT NOT NULL,
  pcr TEXT NOT NULL,
  price_id TEXT NOT NULL,
  quality_class TEXT NOT NULL,
  counter INTEGER NOT NULL,
  nonce TEXT NOT NULL,
  commitment TEXT NOT NULL,
  residual_bucket TEXT NOT NULL,
  price REAL NOT NULL,
  accepted INTEGER NOT NULL,
  PRIMARY KEY(rid, nonce),
  UNIQUE(source),
  UNIQUE(seller, session, counter),
  UNIQUE(seller, session, nonce)
);

CREATE INDEX idx_cert_policy_workload ON certificates(policy, workload);
CREATE INDEX idx_cert_seller_counter ON certificates(seller, session, counter);
CREATE INDEX idx_cert_price ON certificates(price_id);
CREATE INDEX idx_cert_quality ON certificates(quality_class);

CREATE TABLE rejections (
  rid TEXT NOT NULL,
  nonce TEXT NOT NULL,
  seller TEXT,
  session TEXT,
  source TEXT,
  counter INTEGER,
  reason TEXT NOT NULL,
  PRIMARY KEY(rid, nonce)
);

CREATE INDEX idx_reject_reason ON rejections(reason);

CREATE TABLE source_tokens (
  source TEXT PRIMARY KEY,
  seller TEXT NOT NULL,
  cohort TEXT NOT NULL,
  sampling_contract TEXT NOT NULL,
  active INTEGER NOT NULL
);

CREATE TABLE price_table (
  price_id TEXT PRIMARY KEY,
  quality_class TEXT NOT NULL,
  unit_price REAL NOT NULL
);
"""


def main() -> None:
    result = run_demo()
    workdir = Path(tempfile.mkdtemp(prefix="cert_sql_demo_"))
    tmp_db = workdir / DB.name
    conn = sqlite3.connect(tmp_db)
    conn.executescript(SCHEMA)
    conn.executemany(
        "INSERT INTO source_tokens VALUES (?, ?, ?, ?, ?)",
        [
            ("src-marketing-cohort-7", "seller-a", "cohort-7", "sampling-contract-2026", 1),
            ("src-fork-a", "seller-a", "cohort-7", "sampling-contract-2026", 1),
            ("src-reuse", "seller-a", "cohort-7", "sampling-contract-2026", 1),
        ],
    )
    conn.executemany(
        "INSERT INTO price_table VALUES (?, ?, ?)",
        [("price-silver-v4", "silver", 12.0), ("price-gold-v4", "gold", 20.0)],
    )
    for row in result["accepted_rows"]:
        conn.execute(
            """
            INSERT INTO certificates
            (rid, seller, session, source, workload, algorithm, policy, pcr,
             price_id, quality_class, counter, nonce, commitment, residual_bucket, price, accepted)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                row["rid"],
                row["seller"],
                row["session"],
                row["source"],
                row["workload"],
                row["algorithm"],
                row["policy"],
                row["pcr"],
                row["price_id"],
                row["quality_class"],
                row["counter"],
                row["nonce"],
                row["commitment"],
                row["residual_bucket"],
                row["price"],
                1,
            ),
        )
    for row in result["rejected_rows"]:
        conn.execute(
            "INSERT OR IGNORE INTO rejections (rid, nonce, seller, session, source, counter, reason) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (row["rid"], row["nonce"], row["seller"], row["session"], row["source"], row["counter"], row["reason"]),
        )
    conn.commit()

    queries = {
        "accepted_by_policy": """
            SELECT policy, workload, quality_class, COUNT(*) AS reports
            FROM certificates
            WHERE accepted = 1
            GROUP BY policy, workload, quality_class
        """,
        "price_join": """
            SELECT c.seller, c.workload, c.price_id, SUM(p.unit_price) AS payment
            FROM certificates c
            JOIN price_table p USING (price_id)
            WHERE p.quality_class = c.quality_class
            GROUP BY c.seller, c.workload, c.price_id
        """,
        "source_join": """
            SELECT c.rid, c.seller, c.price_id, s.cohort, s.sampling_contract
            FROM certificates c
            JOIN source_tokens s USING (source)
            WHERE s.active = 1
        """,
        "rejection_audit": """
            SELECT reason, COUNT(*) AS failures
            FROM rejections
            GROUP BY reason
            ORDER BY failures DESC, reason
        """,
        "ledger_key_probe": """
            SELECT rid, accepted
            FROM certificates
            WHERE source = 'src-marketing-cohort-7'
              AND seller = 'seller-a'
              AND session = 's-001'
              AND policy = 'policy-v4'
              AND price_id = 'price-gold-v4'
        """,
    }
    output = {}
    for name, sql in queries.items():
        cur = conn.execute(sql)
        cols = [c[0] for c in cur.description]
        output[name] = [dict(zip(cols, row)) for row in cur.fetchall()]
    conn.close()
    shutil.copy2(tmp_db, DB)
    OUT.write_text(json.dumps(output, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(output, indent=2, sort_keys=True))
    print(f"wrote {DB}")
    print(f"wrote {OUT}")


if __name__ == "__main__":
    main()
