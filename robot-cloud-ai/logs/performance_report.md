# Phase 1 — Cloud AI prototype performance report

- Generated: 2026-10-31T16:28:15Z
- Log source: `C:\Users\Prakash\EdgeGrip\robot-cloud-ai\logs\results.csv`
- Records in log: 1

## Headline metrics

| Metric | Value |
|---|---|
| Total requests | 1 |
| Successful requests | 0 |
| Failed requests | 1 |
| Success rate | 0.0% |
| Average latency | n/a |
| Minimum latency | n/a |
| Maximum latency | n/a |
| Prediction accuracy | not measured (no labelled records) |

## Results by test case

| Test case | Attempts | Successful | Failed | Average latency |
|---|---:|---:|---:|---:|
| TC1.4 | 1 | 0 | 1 | n/a |

## How to read this report

- Latency is the complete client-side round trip measured with `time.perf_counter()` around the HTTP call. It includes TLS setup, network transit, provider queueing and model execution — not model inference time alone.
- Latency statistics use successful requests only.
- Success rate measures whether a usable prediction came back. It is not prediction quality.
- Accuracy is computed only from records that carry an expected label. Failed requests are excluded from the accuracy denominator but still count as attempts.
- The 5,000 ms response target is a prototype benchmark, not a promised service level from the inference provider.
- Phase 1 neither retries nor buffers requests. Failures are recorded, not recovered; that is Phase 2 scope.
