---- MODULE CertificateAdmitInvariants ----
EXTENDS FiniteSets, Sequences

\* TLA-style invariant skeleton for the visible-state contract.
\* CommitmentValid and DisclosureValid are abstract predicates implemented by
\* the executable SQL/KV verifier tests.

CONSTANTS AttemptInbox, CertLedger, PricePolicy, RejectLog, AggregateQueue,
          ReceiptLinked, CommitmentValid, DisclosureValid

TerminalKey(c) == <<c.rid, c.nonce>>
Accepted == {c \in CertLedger : c.status = "accepted"}
QueuedKeys == {TerminalKey(q) : q \in AggregateQueue}
CounterKey(c) == <<c.seller, c.session, c.ctr>>
NonceKey(c) == <<c.seller, c.session, c.nonce>>

I0_durable_receipt ==
    /\ \A c \in CertLedger : ReceiptLinked[c]
    /\ \A r \in RejectLog : ReceiptLinked[r]

I1_source_once ==
    \A s \in {q.src : q \in AggregateQueue} :
        Cardinality({q \in AggregateQueue : q.src = s}) <= 1

I2_separate_freshness_keys ==
    \A c1 \in Accepted :
    \A c2 \in Accepted :
        /\ (TerminalKey(c1) = TerminalKey(c2) => c1 = c2)
        /\ (CounterKey(c1) = CounterKey(c2) => c1 = c2)
        /\ (NonceKey(c1) = NonceKey(c2) => c1 = c2)

I3_policy_price_bound ==
    \A c \in Accepted :
        \E p \in PricePolicy : p.policy = c.policy /\ p.priceID = c.priceID

I4_commitment_bound ==
    \A c \in Accepted : CommitmentValid[c]

I5_reject_no_queue ==
    \A r \in RejectLog : TerminalKey(r) \notin QueuedKeys

I6_missing_no_queue ==
    \A c \in CertLedger :
        c.status = "consumed_missing" => TerminalKey(c) \notin QueuedKeys

I7_queue_accepted_only ==
    \A q \in AggregateQueue :
        \E c \in Accepted : TerminalKey(c) = TerminalKey(q)

I8_disclosure_mode ==
    \A c \in Accepted : DisclosureValid[c]

CertificateAdmitInvariant ==
    /\ I0_durable_receipt
    /\ I1_source_once
    /\ I2_separate_freshness_keys
    /\ I3_policy_price_bound
    /\ I4_commitment_bound
    /\ I5_reject_no_queue
    /\ I6_missing_no_queue
    /\ I7_queue_accepted_only
    /\ I8_disclosure_mode

====
