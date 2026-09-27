> Offline scripted replay behind the deepseek emulator: each run's model replays the task's reference fix through the unchanged harness (real tools, toolchains, evidence, export, judge). It checks the harness end to end and measures its overhead; it says nothing about a model's ability.

# Evaluation summary

Records: 46 (every scheduled run is listed; failures included)

Model calls live: [True] — fake/scripted runs are pipeline checks, not capability results.

| task | ours |
|---|---|
| calc-divide | PASS |
| g-go-initials | PASS |
| g-go-max | PASS |
| g-go-reverse | PASS |
| g-go-sum-positive | PASS |
| g-js-cents | PASS |
| g-js-chunk | PASS |
| g-js-deep-equal | PASS |
| g-js-format-time | PASS |
| g-js-get-path | PASS |
| g-js-numeric-sort | PASS |
| g-js-query | PASS |
| g-js-range | PASS |
| g-js-retry | PASS |
| g-js-title-case | PASS |
| g-js-truncate | PASS |
| g-js-unique-by | PASS |
| g-py-chunks | PASS |
| g-py-flatten | PASS |
| g-py-median | PASS |
| g-py-merge-intervals | PASS |
| g-py-percent | PASS |
| g-py-safe-int | PASS |
| g-rb-average | PASS |
| g-rb-pluralize | PASS |
| g-rs-bank | PASS |
| g-rs-checked-ratio | PASS |
| g-rs-clamp | PASS |
| g-rs-duration | PASS |
| g-rs-fahrenheit | PASS |
| g-rs-fields | PASS |
| g-rs-fizzbuzz | PASS |
| g-rs-leap | PASS |
| g-rs-median | PASS |
| g-rs-roman | PASS |
| g-rs-slug | PASS |
| g-rs-stack-pop | PASS |
| g-rs-transpose | PASS |
| g-rs-unique | PASS |
| g-rs-version | PASS |
| g-rs-word-count | PASS |
| go-wordfreq | PASS |
| js-duration | PASS |
| py-deep-merge | PASS |
| py-slugify | PASS |
| py-ttl-cache | PASS |

| system | judged pass | runs | submission_ready | tokens (sum) | wall s (sum) | audit flags |
|---|---|---|---|---|---|---|
| ours | 46 | 46 | 46 | 261837 | 94 | 0 |

| system | pass@1 | pass^k (k = repeats) | tokens / solved | wall s / solved | cost / solved |
|---|---|---|---|---|---|
| ours | 1.000 | 1.000 (k=1) | 5692 | 2 | n/a |

Proof level vs hidden-test outcome (does the harness's own evidence predict correctness?):

| system | proof level | runs | judged pass |
|---|---|---|---|
| ours | passing | 46 | 46 |
