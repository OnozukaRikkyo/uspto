# 特許IDインデックス仕様

## 概要

全CSVの特許IDを事前にnumpy int64配列としてインデックス化し、APIレスポンス内の
`citedDocumentIdentifier` が既知の特許かどうかを高速に判定する。
未知の特許は `added.csv` に自動追記される。

## ファイル構成

| ファイル | 役割 |
|----------|------|
| `build_id_index.py` | インデックス構築スクリプト（初回・CSV追加時に実行） |
| `numpy_data/patent_ids.npy` | ソート済み int64 特許ID配列 shape(N,) |
| `numpy_data/patent_meta.npy` | メタデータ配列 shape(N,2) int32 — `[file_idx, row_idx]` |
| `numpy_data/file_list.txt` | file_idx → ファイルパスの対応リスト |
| `added.csv` | 未知特許の追記先（data/CSVと同フォーマット） |
| `api_utils.py` | `KnownIds` クラス・検索ロジックの実装 |

## IDエンコード規則

特許IDを整数に変換して配列に格納する。

| 元のID | 変換後 | 計算式 |
|--------|--------|--------|
| `D543613` | `10_000_543_613` | `DESIGN_OFFSET(10_000_000_000) + 543613` |
| `D0949851` | `10_000_949_851` | 先頭ゼロは除去してから加算 |
| `12345678` | `12_345_678` | そのまま整数化 |

`DESIGN_OFFSET = 10_000_000_000` により、デザイン特許と数値特許が整数空間で衝突しない。

## KnownIds クラス

```
KnownIds
├── _arr            : shape(N,)   int64  ソート済み特許ID
│                     → np.searchsorted で O(log n) 検索
├── _meta           : shape(N,2)  int32  [file_idx, row_idx]（_arr と同順）
├── _added          : dict[int, list[int,int]]  {patent_int: [file_idx, row_idx]}
│                     added.csv 由来 + 実行中追記分  → O(1) 検索
└── _added_file_idx : int  added.csv のファイルインデックス（= len(file_list)）
```

### メソッド一覧

| メソッド | 説明 |
|----------|------|
| `__contains__(int \| str)` | int または str を受け取り整数で直接比較する |
| `__len__()` | `len(_arr) + len(_added)` を返す |
| `add(patent_id, row=0)` | `_added` に登録する。`row` は added.csv の 0 ベース行インデックス |
| `update_row(patent_id, row)` | `_added` の行番号を後から更新する |
| `get_location(patent_id)` | `(file_idx, row_idx)` を返す。未知なら `None` |

### get_location の使い方

```python
loc = known_ids.get_location('D543613')
# → (2, 481)  file_idx=2, row_idx=481

# ファイルパスは file_list.txt の 2 行目
# 行データは pd.read_csv(path).iloc[481]
```

## 3段階チェックフロー（check_and_register_cited）

`citedDocumentIdentifier` を受け取ってから追記までの判定フロー：

```
cited_str = 'US D543613 S'
     ↓ parse_cited_id()
patent_id = 'D543613'
     ↓ id_to_int()
patent_int = 10_000_543_613

① patent_int in known_ids   numpy _arr + in-memory _added を整数で検索
       ↓ 見つからない
② _check_added_csv()         added.csv ファイルを直接読んで確認
                              見つかれば _added に同期して return
       ↓ 見つからない
③ added.csv に追記 + known_ids.add(patent_id, row=row_idx)
```

②のファイル直接確認により、起動後に別プロセスや別実行で書き込まれた分も検出できる。
見つかった場合は `_added` に同期するため、次回以降は①で即ヒットする。

## インデックスの構築

### 実行（初回・CSV追加時）

```bash
python build_id_index.py
```

出力例：
```
対象CSVファイル: 16件

  [ 0] 2007.csv:  4,832行  (+4,832件)
  [ 1] 2008.csv:  5,120行  (+5,120件)
  ...
✅ インデックス構築完了: 76,543件
   /mnt/eightthdd/uspto/numpy_data/patent_ids.npy   shape=(76543,)  dtype=int64
   /mnt/eightthdd/uspto/numpy_data/patent_meta.npy  shape=(76543,2) dtype=int32
   /mnt/eightthdd/uspto/numpy_data/file_list.txt    (16ファイル)
```

### 重複の扱い

同じIDが複数のCSVに存在する場合、最初に出現したファイル・行が `patent_meta` に記録される。

### 通常実行

`ustpo.py` / `uspto_2.py` 起動時に自動で3ファイルを読み込む。
いずれかが存在しない場合はエラーメッセージを表示して終了する。

```bash
python ustpo.py       # または
python uspto_2.py
```

## 未知特許の追記（added.csv）

- フォーマットは `data/` 配下のCSVと同一
- `id` 列のみ埋まり、他の列は空白（APIレスポンスから取得不可）
- 追記時に `print` で patent_id・整数値・行番号・埋められない列名を出力する
- 追記と同時に `known_ids.add(patent_id, row=row_idx)` で `_added` に登録
- 次回起動時は `added.csv` も `_added` に読み込まれるため実行跨ぎでも重複しない

### 埋められない列

`title`, `claim`, `date`, `class`, `class_search`, `inv_country`,
`no_figs`, `sheets`, `file_names`, `fig_desc`, `caption`

## 複数引用がある場合の動作

APIレスポンスの `docs` は引用レコードの配列。各 `doc` に対してループ内で
`check_and_register_cited` が1回ずつ呼ばれる。

### 重複防止

同一 `citedDocumentIdentifier` が複数 `doc` に現れても重複追記されない。

| doc | citedDocumentIdentifier | 状態 | 動作 |
|-----|------------------------|------|------|
| doc1 | US D543613 S | 未登録 | added.csv に追記 → `_added` に登録 |
| doc2 | US D543613 S | `_added` に存在（①でヒット） | スキップ |
| doc3 | US D714742 S | 未登録 | added.csv に追記 → `_added` に登録 |