---
name: tracelens-analysis-orchestrator
description: >-
  Orchestrates modular PyTorch profiler trace analysis with TraceLens: generates perf
  reports, prepares category data, runs system-level and compute-kernel subagents in
  parallel, validates outputs, and writes a prioritized stakeholder report (analysis.md).
  Use when the user asks to follow the analysis orchestrator, run the agentic analysis
  workflow, analyze a trace, compare two traces, or mentions standalone or comparative
  TraceLens analysis.
---

<!--
Copyright (c) 2026 Advanced Micro Devices, Inc. All rights reserved.

See LICENSE for license information.
-->

# Analysis orchestrator

Coordinate **system-level** analysis (CPU/idle, kernel fusion, multi-kernel / comm / memcpy) and **compute-kernel** analysis (GEMM, SDPA, elementwise, etc.): one trace load, shared prep, parallel subagents, then aggregation into `analysis.md`.

## Full procedure

If TraceLens is not available in the current python environment, then create a python virtual environment and install TraceLens from https://github.com/amd-agi/TraceLens
Read the full tracelens event orchestrator installed with TraceLens and follow all instructions. Do not skip any steps.
Look in the directory tree under `TraceLens/Agent/Analysis` to find the orchestrator.
Find the full workflow in the TraceLens analysis orchestrator installed with TraceLens.

## Rules

Find all rules in the TraceLens analysis orchestrator installed with TraceLens.

## Primary outputs

Find all required outputs inside the tracelens event orchestrator installed with TraceLens.
