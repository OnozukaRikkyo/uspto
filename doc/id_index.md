# 特許IDインデックス仕様

## 概要

全CSVの特許IDを事前にnumpy int64配列としてインデックス化し、APIレスポンス内の
`citedDocumentIdentifier` が既知の特許かどうかを高速に判定する。

## ファイル構成

| ファイル | 役割 |
|----------|------|
| `build_id_index.py` | インデックス構築スクリプト（初回・CSV追加時に実行） |
| `/mnt/eightthdd/uspto/numpy_data/patent_ids.npy` | ソート済み int64 配列 |
| `/mnt/eightthdd/uspto/added.csv` | 未知特許の追記先（CSVと同フォーマット） |
| `api_utils.py` | `KnownIds` クラス・検索ロジックの実装 |

## IDエンコード規則

特許IDを整数に変換して配列に格納する。

| 元のID | 変換後 | 計算式 |
|--------|--------|--------|
| `D543613` | `10_000_543_613` | `DESIGN_OFFSET(10_000_000_000) + 543613` |
| `D0949851` | `10_000_949_851` | 先頭ゼロは除去してから加算 |
| `12345678` | `12_345_678` | そのまま整数化 |

`DESIGN_OFFSET = 10_000_000_000` により、デザイン特許と数値特許が整数空間で衝突しない。

## 検索アーキテクチャ

```
KnownIds
├── _arr  : ソート済み numpy int64 配列  → np.searchsorted で O(log n) 検索
│           （data/ 配下の全CSV由来）
└── _added: Python set[int]             → O(1) 検索
            （added.csv 由来 + 実行中追記分）
```

1. `patent_id in known_ids` が呼ばれると、まず `_added` を O(1) で確認
2. なければ `np.searchsorted` で `_arr` を O(log n) で確認

## 使い方

### インデックス構築（初回・CSV追加時）

```bash
python build_id_index.py
```

出力例：
```
対象CSVファイル: 16件

  2007.csv:  4,832行  (+4,832件)
  2008.csv:  5,120行  (+5,120件)
  ...
✅ インデックス構築完了: 76,543件 → /mnt/eightthdd/uspto/numpy_data/patent_ids.npy
```

### 通常実行

`ustpo.py` / `uspto_2.py` 起動時に自動で `patent_ids.npy` を読み込む。
インデックスファイルが存在しない場合はエラーメッセージを表示して終了する。

```bash
python ustpo.py       # または
python uspto_2.py
```

## 未知特許の追記（added.csv）

APIレスポンス内の `citedDocumentIdentifier` が既知IDセットに存在しない場合、
`added.csv` に新規行として追記される。

- フォーマットは `data/` 配下のCSVと同一
- `id` 列のみ埋まり、他の列は空白
- 埋められない列名は `print` で標準出力に表示される
- 追記済みIDは実行中に `KnownIds._added` へ登録され、同一実行内での重複追記を防ぐ
- 次回起動時は `added.csv` も読み込まれるため、実行跨ぎでも重複しない

### 埋められない列

`added.csv` への追記時、以下の列は空白になる（APIレスポンスから取得不可）：

`title`, `claim`, `date`, `class`, `class_search`, `inv_country`,
`no_figs`, `sheets`, `file_names`, `fig_desc`, `caption`