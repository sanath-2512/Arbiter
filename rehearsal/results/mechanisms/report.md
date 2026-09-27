# Mechanism rehearsals (scripted policies on real repositories)

> Mechanism rehearsals: scripted policies drive arbiter, through its real HTTP transport and tools, on real pinned repositories into one controlled failure mode each; every run is judged exactly like a gauntlet run (export -> clean base -> hidden tests). They test the harness's plumbing under that failure mode. They are not evidence of solve rate: the scripted policy already knows the fix.

Harness 1b4f97810f7c · 31 judged runs · expectations met: 25/25

## stuck_loop — flask_ipv6_server_name

Same failure signature, same region, no progress: does the harness force a new hypothesis, and how many repeats happen before and after?

| config | judged solved | valid artifact | failure class | restored | recovery | interventions | fails before / after | advisory | attempts | requests | wall s | expectation |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| A | no | yes | hidden_tests_fail | no | - | 0 | 0 / 0 | 0 | 1 | 25 | 7.24 | met |
| B | yes | yes | solved | no | - | 1 | 2 / 0 | 0 | 1 | 13 | 4.11 | met |
| C | no | yes | hidden_tests_fail | no | - | 0 | 0 / 0 | 0 | 1 | 25 | 7.29 | - |
| D | no | yes | hidden_tests_fail | no | - | 0 | 0 / 0 | 0 | 1 | 25 | 7.25 | - |
| E | no | yes | hidden_tests_fail | no | - | 0 | 0 / 0 | 0 | 1 | 25 | 7.25 | - |
| F | yes | yes | solved | no | - | 1 | 2 / 0 | 0 | 1 | 13 | 4.96 | met |

## late_regression — click_sentinel_copy

Verified candidate A, then an unverified tidy-up B that breaks an existing test: is A restored and exported?

| config | judged solved | valid artifact | failure class | restored | recovery | interventions | fails before / after | advisory | attempts | requests | wall s | expectation |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| A | no | yes | regression | no | - | 0 | 0 / 0 | 0 | 1 | 7 | 3.07 | met |
| B | no | yes | regression | no | - | 0 | 0 / 0 | 0 | 1 | 7 | 2.96 | - |
| C | no | yes | regression | no | - | 0 | 0 / 0 | 0 | 1 | 7 | 2.91 | - |
| D | no | yes | regression | no | - | 0 | 0 / 0 | 0 | 1 | 8 | 5.8 | - |
| E | yes | yes | solved | yes | win | 0 | 0 / 0 | 0 | 1 | 7 | 4.29 | met |
| F | yes | yes | solved | yes | win | 0 | 0 / 0 | 0 | 1 | 8 | 6.15 | met |

## wrong_generated_test — click_sentinel_copy

A registered reproduction with a wrong expectation keeps failing after the correct fix: does the good candidate survive?

| config | judged solved | valid artifact | failure class | restored | recovery | interventions | fails before / after | advisory | attempts | requests | wall s | expectation |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| A | yes | yes | solved | no | - | 0 | 0 / 0 | 0 | 1 | 5 | 2.89 | met |
| B | yes | yes | solved | no | - | 0 | 0 / 0 | 0 | 1 | 5 | 2.94 | met |
| C | yes | yes | solved | no | - | 0 | 0 / 0 | 0 | 1 | 5 | 2.85 | met |
| D | yes | yes | solved | no | - | 0 | 0 / 0 | 1 | 1 | 6 | 4.58 | met |
| E | yes | yes | solved | no | - | 0 | 0 / 0 | 0 | 1 | 5 | 2.91 | met |
| F | yes | yes | solved | no | - | 0 | 0 / 0 | 1 | 3 | 18 | 8.03 | met |

## stale_edit — click_help_parameter

An edit written against text that has since changed: refused without side effects, with a hint exact enough to recover from?

| config | judged solved | valid artifact | failure class | restored | recovery | interventions | fails before / after | advisory | attempts | requests | wall s | expectation |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| A | yes | yes | solved | no | - | 0 | 0 / 0 | 0 | 1 | 12 | 10.15 | met |
| F | yes | yes | solved | no | - | 0 | 0 / 0 | 0 | 1 | 13 | 11.63 | met |

`A`: recovered_from_hint=True, stale_edit_refused=True, fails_fast=False, recovered_offline=False

`F`: recovered_from_hint=True, stale_edit_refused=True, fails_fast=False, recovered_offline=False

## api_faults — click_sentinel_copy

429, 5xx, dropped connection, non-JSON body, output cap, context overflow, unsupported parameter, hung request

| config | judged solved | valid artifact | failure class | restored | recovery | interventions | fails before / after | advisory | attempts | requests | wall s | expectation |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| A | yes | yes | solved | no | - | 0 | 0 / 0 | 0 | 1 | 13 | 46.15 | met |
| F | yes | yes | solved | no | - | 0 | 0 / 0 | 0 | 1 | 13 | 47.77 | met |

## invalid_key — click_sentinel_copy

401 invalid key: stop at once with a clear error instead of retrying

| config | judged solved | valid artifact | failure class | restored | recovery | interventions | fails before / after | advisory | attempts | requests | wall s | expectation |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| F | no | yes | empty_patch | no | - | 0 | 0 / 0 | 0 | 1 | 1 | 2.04 | met |

`F`: fails_fast=True, recovered_offline=False

## missing_key — click_sentinel_copy

AI_API_KEY unset: clear configuration error, no traceback, no model request

| config | judged solved | valid artifact | failure class | restored | recovery | interventions | fails before / after | advisory | attempts | requests | wall s | expectation |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| F | no | yes | configuration_error | no | - | 0 | 0 / 0 | 0 | 0 | None | 0.5 | met |

`F`: fails_fast=True, recovered_offline=False

## sigterm — click_sentinel_copy

SIGTERM while a tool subprocess runs, fix already on disk: graceful stop with a valid artifact

| config | judged solved | valid artifact | failure class | restored | recovery | interventions | fails before / after | advisory | attempts | requests | wall s | expectation |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| A | yes | yes | solved | no | - | 0 | 0 / 0 | 0 | 1 | 4 | 27.2 | met |
| F | yes | yes | solved | no | - | 0 | 0 / 0 | 0 | 1 | 4 | 26.93 | met |

## sigkill — click_sentinel_copy

SIGKILL mid-run (partial state on disk): offline finalize recovers a valid artifact

| config | judged solved | valid artifact | failure class | restored | recovery | interventions | fails before / after | advisory | attempts | requests | wall s | expectation |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| F | yes | yes | solved | no | - | 0 | 0 / 0 | 0 | 1 | 4 | 25.48 | met |

`F`: fails_fast=False, recovered_offline=True

## budget_exhaustion — click_sentinel_copy

Step budget runs out right after the fix was verified: is the verified work exported?

| config | judged solved | valid artifact | failure class | restored | recovery | interventions | fails before / after | advisory | attempts | requests | wall s | expectation |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| A | yes | yes | solved | no | - | 0 | 0 / 0 | 0 | 1 | 4 | 3.07 | met |
| F | yes | yes | solved | no | - | 0 | 0 / 0 | 0 | 1 | 4 | 4.79 | met |

## hostile_tools — click_sentinel_copy

Hung command, 40 MB of stdout, a background sleeper, an ambiguous edit, an edit of a missing file

| config | judged solved | valid artifact | failure class | restored | recovery | interventions | fails before / after | advisory | attempts | requests | wall s | expectation |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| A | yes | yes | solved | no | - | 0 | 0 / 0 | 0 | 1 | 9 | 10.27 | met |
| F | yes | yes | solved | no | - | 0 | 0 / 0 | 0 | 1 | 9 | 11.47 | met |

