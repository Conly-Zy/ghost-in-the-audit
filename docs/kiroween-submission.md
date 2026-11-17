# Ghost in the Audit · Kiroween Development and Submission Guide

The Kiro workspace agent `orchestrator` dispatches four specialist subagents. Evidence comes from the actual file-read results captured by `PostToolUse`, independently of the target application's own logs. `PreToolUse` checks citations when a subagent writes to the Inbox, and Finalize rechecks them against the current files.

| Earlier plan | Current implementation and next step |
| --- | --- |
| Exorcist, Log Investigator, Data Flow Investigator, and Citation Auditor | `orchestrator` dispatches `cartographer` first. After confirming that reads produced evidence, it dispatches the security, dependency, and quality auditors. Python Hooks check citations, and Kiro Specs and steering maintain the rules. |
| Crew DAG, loops, and `kirocrew spawn run` | Workspace agent definitions control the current workflow. `kirocrew spawn run` is an official Crew command. Before using Crew in the demo, verify that it can find the workspace's `orchestrator` and that subagent tool calls trigger both repository Hooks. The README command update depends on that verification. |
| Run the gate at subagent completion and cite target application logs | At read time, `PostToolUse` records the `file:line` location, UTC timestamp, and content hash. At submission time, `PreToolUse` checks the excerpt against the current file. The write tool returns rejection codes, and subagents can make up to two corrections. |
| Pydantic, Hypothesis, and MCP | The implementation uses only the Python standard library. It already has tests for format properties, Hook integration, and report verification. These tests cover the shortest path to a demo. |
| Optional web version, with a GitHub link alone if there is no deployment | AWS's 2026 event announcement requires a URL for a working application. The repository provides a local audit workflow. For this year's submission, the applicable entry point must be confirmed under the rules before preparing an accessible URL. |

## 1. Prepare a reproducible demo

1. The repository is public. When checked on 2026-11-18, the default branch contained only `LICENSE`, and the GitHub license API identified it as MIT. The submission checklist covers the release contents. Confirm that About shows MIT License and that the README steps work in a clean clone. [GitHub repository](https://github.com/Conly-Zy/ghost-in-the-audit)
2. Run `python3 -m unittest discover -s tests -t .` and then `python3 -m unittest tests.test_observed_payload.TestObservedPayloadEndToEnd -v`. The replay has tested the real Hook scripts with simulated client inputs based on the IDE probe. It covers rejection of a write without a citation, reads at a nonzero offset, acceptance of a valid citation into the ledger, and report verification.
3. Live verification is pending for Kiro IDE and Kiro CLI V3. Run the IDE first and then the CLI. Record each client version, run ID, Canary rejection, subagent statuses, and report verification result. Use live results in the recording. [Live runbook](e2e-runbook.md)
4. Before naming **Kiro Crew** in the submission, verify its entry point. It must select the repository's `orchestrator`, dispatch all four subagents, show both Hooks firing, and produce `RESULT: PASS` from `verify_e2e_report.py`. Until then, use the Kiro workspace entry point in the README. [Crew CLI command](https://kiro.dev/docs/crew/interfaces/cli-reference/#kirocrew-spawn-run)

## 2. Demo script under three minutes

| Time | What to show |
| --- | --- |
| 0:00–0:20 | Explain the goal in one sentence. Show the repository root and `.kiro/`, select `orchestrator`, and send `Audit the pinger-ide directory under targets/.` |
| 0:20–0:55 | Show the Canary write returning `CITATION_MISSING`. Open the matching `gate-live.jsonl` entry and compare it with the rejected write. |
| 0:55–1:35 | Show the dispatch of Cartographer and the three specialist auditors. Open an entry in `evidence.jsonl` and point out the line range, timestamp, and hash. |
| 1:35–2:35 | Open the final report, compare one finding's citation with the sample source, and show the verifier's `RESULT: PASS`. Trim waiting time if needed. |
| 2:35–2:55 | Explain the roles of the Kiro agents, Specs, and the two Hooks. |

## 3. 2026 submission checklist

AWS announced a submission window from 2026-10-30 at 09:00 to 2026-12-04 at 14:00 US Pacific time. The earliest local commit is dated 2026-11-14. Check the development records to confirm that the work took place during the window, and disclose any pre-existing code or assets as required. If the earlier plan or implementation came from a previous entry, check eligibility under the new-project rules first. [AWS 2026 event announcement](https://aws.amazon.com/jp/blogs/news/kiroween-2026/) · [Devpost rules](https://kiroween.devpost.com/rules)

- [ ] The public GitHub repository is accessible and contains the source, assets, sample, `README.md`, open-source license, and root `.kiro/` directory. The repository tracks only `.gitkeep` in each of `runs/`, `targets/`, and `control/`.
- [ ] The working application URL opens in a signed-out browser and demonstrates the core functionality.
- [ ] The YouTube video is under three minutes, shows a live run, and explains how the project uses Kiro. Confirm the visibility setting against the Devpost rules.
- [ ] The submission page describes what the project does and how it uses Kiro, and includes the GitHub, application, and video links.

The repository README draft is ready to use. The Quick start section can include the application and video links once they are available and verified. [README draft](../README.md)
