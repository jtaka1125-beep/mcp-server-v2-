# V2 再構築設計 — ラッパー + ワーカープール

Status: **Draft** (2026-04-19 着手)
Author: Claude + Jun
Context: P4-2 並列 v2 エンジンの発展版

## 背景

現 V2 (`mcp-server-v2/server.py` + `tools/*`) は、MCP tool の呼出を受けて
直接 subprocess で `claude.EXE` を叩いてる。構造:

```
MCP client
    ↓ HTTP POST /mcp
server.py (tool dispatcher)
    ↓ import + call
tools/task.py · tools/loop.py · tools/pipeline.py · ...
    ↓ subprocess.run
claude.EXE × N (並列数制限なしだった)
```

2026-04-19 に P4-2 第 1 段階として `parallel.py` の CLI_GATE (BoundedSemaphore)
を挟んで並列数を絞ったが、これは**その場凌ぎ**。gate は各 tool モジュール
に散らばって差し込まれているので、将来 worker 実装を差し替えたくなった時
(例: CLI → API 直叩き) に tool 全部を書き直す必要がある。

## 目標

V2 を 2 層化して、**上層 (ラッパー) と下層 (worker) を分離**する:

```
MCP client
    ↓ HTTP POST /mcp
[上層] server.py + tools/*  (MCP 互換の API、入出力 schema、validation)
    ↓ 内部 IPC (queue / socket)
[下層] dispatcher + worker pool
    ↓ 実行バックエンド (差し替え可能)
  ├─ backend_cli.py     (claude.EXE subprocess, 現行)
  ├─ backend_api.py     (Anthropic API 直叩き, 将来)
  └─ backend_mock.py    (テスト用)
```

V1 はこの再構築の対象外。fallback ルートとして制限なしで独立動作を保つ。

## 要件

### 機能要件

1. **互換性**: MCP client から見た tool schema / 応答形式は現在と同一。
2. **バックエンド差し替え**: worker 実装を環境変数一つで切替可能。
3. **並列制御**: `V2_MAX_PARALLEL` 環境変数でキャパシティ制御 (デフォルト 3)。
4. **キュー可視化**: `gate_stats()` 相当を `task_status` / `loop_status` の
   レスポンスに含める。
5. **graceful shutdown**: 進行中 job を待ってから下層を停止 (タイムアウト 60s)。

### 非機能要件

- **V1 に影響しない**: V1 の code path は一切 import しない。
- **冪等性**: V2 再起動で進行中 job が失われても client 側は再送で回復。
- **観測可能性**: logs/v2_dispatcher.log と logs/v2_worker_N.log を分離。

## アーキテクチャ

### 上層 (wrapper)

- `server.py` は MCP protocol handler のみに痩身化。
- `tools/*.py` は job 構造体 (`{job_id, kind, args, timeout}`) を作り、
  dispatcher に `submit(job)` するだけ。結果は `await_result(job_id)`
  または polling (`get_status(job_id)`).
- 現在の `_tasks_lock` / `_jobs_lock` / `_loop_jobs` 相当の state は
  dispatcher が持つ。tools はローカル state を持たない。

### 下層 (dispatcher + worker)

- `dispatcher.py`: 単一インスタンス、in-process `queue.Queue` にジョブを
  受け取り、worker pool に配る。
- `worker.py`: `concurrent.futures.ThreadPoolExecutor(max_workers=N)` または
  `multiprocessing.Pool` を採用。最初は Thread で十分 (claude.EXE は
  subprocess なので GIL 影響なし)。
- `backend_*.py`: 実際の実行方法を実装。`run(prompt, cwd, model,
  timeout) -> Result` の interface だけを満たす。

### IPC

- まず **in-process** (同一プロセス内の Queue) でスタート。
- 将来 worker を別プロセス分離したくなったら `multiprocessing.Queue` or
  local HTTP に切り替え可能なよう、dispatcher I/F は pickle-safe な
  dict で揃える。

## 段階的移行プラン

### 段階 1: gate 分離 (完了・commit XXXXXXX)

- `parallel.py` 追加、CLI_GATE を export
- `tools/task.py` が `_gated_subprocess_run()` 経由で subprocess 呼出
- `tools/loop.py` が `_run_job_gated()` 経由で loop_engine_v2 呼出
- V1 は touch しない

### 段階 2: dispatcher 追加 (次セッション)

- `mcp-server-v2/dispatcher.py` + `worker.py` + `backend_cli.py` を新規追加
- interface 定義のみで実装はスケルトン
- 既存 tools は **まだ直接 subprocess 呼んだまま** (parallel 維持)
- unit test 追加

### 段階 3: tools リルート (段階 2 の後)

- `tools/task.py` と `tools/loop.py` の subprocess 直接呼出を削除
- dispatcher.submit() + await_result() パターンに書き換え
- 並列挙動の equivalence test
- gate は dispatcher が抱える (parallel.py は dispatcher が import)

### 段階 4: backend 切替 (さらに先)

- `backend_api.py` を追加 (Anthropic API 直叩き、要 API key)
- `V2_BACKEND=api` で切替可能に
- CLI との equivalence を回帰テスト

段階 2-4 は段階 1 が安定稼働した実績 (1 週間程度) を見てから着手。

## やらないこと (明示的に対象外)

- **V1 の再構築**: V1 は fallback 専用、hot standby として現状維持。
- **MCP protocol の変更**: 下層が変わっても上位 API は絶対に変えない。
- **graceful online migration**: V2 再起動でよい (Guard が復旧させる)。
- **分散実行**: ワーカーは同一 PC 内のみ。リモート分散は対象外。

## リスク

| リスク | 影響 | 対策 |
|---|---|---|
| dispatcher 導入で latency 増 | 各 tool call に +5-10ms | in-process queue 継続で最小化 |
| worker 死亡時の orphan job | 進行中 job が stuck | heartbeat + 90s タイムアウトで強制 release |
| V2 再起動で queue 失う | 未実行 job が消滅 | client 側の retry 前提、再送で回復 |
| backend 切替で挙動差分 | API と CLI で結果が違う | equivalence test、どちらも通る仕様化 |

## オープンクエスチョン

- dispatcher は Thread vs Process? → Thread で十分な体感、必要なら後から変更可
- per-tool の timeout はどこで enforce? → 上層の schema に timeout_sec を
  追加、dispatcher が `Future.result(timeout=...)` で catch
- CLI_GATE と dispatcher の棲み分けは? → 段階 3 で dispatcher が gate を
  内包、段階 1-2 は並存

## 関連コミット

- `e0b5f32` slim REST API passthrough (V1 → V2)
- `6fd8462` layer2_guard.ps1 watchdog
- 本 PR (段階 1): `parallel.py`, `tools/task.py`, `tools/loop.py`, 本ドキュメント
