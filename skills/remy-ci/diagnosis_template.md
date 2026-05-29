# CI Failure Diagnosis Report

> Generated: {timestamp}
> Source: {source_mode} | Branch: {branch} | Run ID: {run_id}

## 1. Failure Summary

| Failure Type | Count | Severity |
| :--- | :--- | :--- |
{failure_summary_rows}

## 2. Error Details

{error_details}

## 3. Affected Files

| File | Line | Error Type | Message |
| :--- | :--- | :--- | :--- |
{affected_files_rows}

## 4. Git Correlation

### Recent Changes in Affected Files

{git_diff_section}

## 5. Impact Analysis

{impact_section}

## 6. Root Cause Assessment

| # | Hypothesis | Confidence | Evidence |
| :--- | :--- | :--- | :--- |
{root_cause_rows}

## 7. Recommended Actions

{recommended_actions}
