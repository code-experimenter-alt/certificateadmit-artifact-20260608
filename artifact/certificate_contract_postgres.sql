CREATE TABLE AttemptInbox (attempt_id TEXT PRIMARY KEY, receive_hash TEXT NOT NULL, state TEXT NOT NULL, received_at REAL NOT NULL);
CREATE TABLE SourceToken (src TEXT PRIMARY KEY, seller TEXT NOT NULL, session TEXT NOT NULL, workload TEXT NOT NULL, state TEXT NOT NULL);
CREATE TABLE CertLedger (rid TEXT NOT NULL, nonce TEXT NOT NULL, src TEXT NOT NULL UNIQUE, seller TEXT NOT NULL, session TEXT NOT NULL, ctr BIGINT NOT NULL, policy TEXT NOT NULL, price_id TEXT NOT NULL, commitment TEXT NOT NULL, status TEXT NOT NULL, PRIMARY KEY(rid, nonce), UNIQUE(seller, session, ctr), UNIQUE(seller, session, nonce));
CREATE TABLE PricePolicy (policy TEXT NOT NULL, price_id TEXT NOT NULL, quality_class TEXT NOT NULL, price REAL NOT NULL, PRIMARY KEY(policy, price_id));
CREATE TABLE RejectLog (reject_key TEXT PRIMARY KEY, rid TEXT, nonce TEXT, reason TEXT NOT NULL, seller TEXT NOT NULL, workload TEXT NOT NULL);
CREATE TABLE AggregateQueue (rid TEXT NOT NULL, nonce TEXT NOT NULL, src TEXT NOT NULL, seller TEXT NOT NULL, session TEXT NOT NULL, workload TEXT NOT NULL, quality_class TEXT NOT NULL, price_id TEXT NOT NULL, commitment TEXT NOT NULL, PRIMARY KEY(rid, nonce));
CREATE INDEX reject_reason_idx ON RejectLog(reason, seller, workload);
CREATE INDEX aggregate_price_idx ON AggregateQueue(price_id, quality_class);

-- CertificateAdmit admission skeleton for postgres.
BEGIN TRANSACTION ISOLATION LEVEL SERIALIZABLE;
-- 0. Durably record receive before parsing, verification, or rejection.
INSERT INTO AttemptInbox
 (attempt_id, receive_hash, state, received_at)
 VALUES (%(attempt_id)s, %(receive_hash)s, 'received', %(now)s) ON CONFLICT DO NOTHING;
-- 1. Verify attestation, commitment, policy version, and residual evidence in host code.
-- 2. Consume exactly one issued source token; abort or reject unless rowcount is 1.
UPDATE SourceToken SET state='consumed'
 WHERE src=%(src)s AND state='issued';
-- 3. Join the attested policy to the posted price row.
SELECT 1 FROM PricePolicy
 WHERE policy=%(policy)s AND price_id=%(price_id)s;
-- 4. Insert the fresh accepted certificate tuple.
INSERT INTO CertLedger
 (rid, nonce, src, seller, session, ctr, policy, price_id, commitment, status)
 VALUES (%(rid)s, %(nonce)s, %(src)s, %(seller)s, %(session)s,
         %(ctr)s, %(policy)s,
         %(price_id)s, %(commitment)s, 'accepted') ON CONFLICT DO NOTHING;
-- 5. Insert accepted-only estimator input only after all checks pass.
INSERT INTO AggregateQueue
 (rid, nonce, src, seller, session, workload, quality_class, price_id, commitment)
 VALUES (%(rid)s, %(nonce)s, %(src)s, %(seller)s, %(session)s,
         %(workload)s, %(quality_class)s,
         %(price_id)s, %(commitment)s) ON CONFLICT DO NOTHING;
COMMIT;
