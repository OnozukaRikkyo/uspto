# API エラーハンドリング仕様

## 概要

USPTO ODP API へのリクエスト結果は `api_utils.py` の `check_status()` で一元管理する。
エラーログは `/mnt/eightthdd/uspto/log/` に日付付きファイルで記録される。

## HTTPステータスコードの処理方針

| コード | 分類 | 処理 |
|--------|------|------|
| 200 | 正常 | 処理続行 |
| 4xx（全て） | クライアントエラー | **即座に致命的終了** |
| 5xx（全て） | サーバーエラー | **即座に致命的終了** |

### 4xx を致命的とする理由

4xx はサーバーではなく**こちら側のリクエストに問題がある**ことを示す。

- `401 Unauthorized` — APIキーが無効
- `403 Forbidden` — アクセス権がない
- `429 Too Many Requests` — レート制限超過
- `400 Bad Request` — クエリの形式が不正（コードのバグの可能性）
- その他の4xx — 同様にリクエスト起因のエラー

いずれもプログラムの設定やコードを修正しない限り解決しないため、続行しても無意味。

### 404 を致命的とする理由

USPTO API は**特定の特許が存在しない場合、`200 OK` + 空の `docs` 配列**を返す。
そのため `404` はエンドポイントURL自体が存在しない（APIのパスが間違っている）ことを意味し、致命的エラーとして扱う。

「特許が存在しない」ケースは `len(docs) == 0` の確認で処理される。

### 5xx を致命的とする理由

サーバー側の障害が発生している状態でリトライしても復旧の見込みがなく、大量リクエストを継続することでさらに負荷をかけるリスクがある。確認・再実行は手動で行う。

## ネットワーク例外の処理

`requests.exceptions.RequestException`（タイムアウト、接続エラー等）はHTTPステータスコードとは別に処理される。

- エラー内容を `logger.error` でログに記録
- そのレコードをスキップして次のレコードへ進む
- 次回実行時に未処理レコードとして再試行される（スキップフラグが有効な場合）

## ログファイル

| パス | 内容 |
|------|------|
| `/mnt/eightthdd/uspto/log/ustpo_YYYYMMDD.log` | `ustpo.py` の実行ログ |
| `/mnt/eightthdd/uspto/log/uspto_2_YYYYMMDD.log` | `uspto_2.py` の実行ログ |

ログレベル：
- `DEBUG` — ファイルに記録（全レベル）
- `WARNING` / `ERROR` / `CRITICAL` — コンソールにも出力

## 実装ファイル

- `api_utils.py` — `setup_logger()` と `check_status()` の定義
- `ustpo.py` — `extract_examiner_rejections()` で使用
- `uspto_2.py` — `extract_layer1_candidates()` / `verify_layer2_strict()` で使用