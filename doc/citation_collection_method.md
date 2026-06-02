# 3.2 Citation Data Collection via the USPTO Open Data API

## Overview

To construct the citation network for design patents, we developed an automated data collection pipeline (`fetch_citations.py`) that systematically retrieves forward citation records from the USPTO Open Data Portal. For each target patent in our corpus, the pipeline queries the USPTO Enriched Cited Reference Metadata API, parses the response, and persists structured citation records to disk. The pipeline supports incremental processing, skipping previously fetched patents based on a persistent log, which enables fault-tolerant operation over large corpora spanning hundreds of thousands of patent identifiers.

---

## 3.2.1 Input

**Format.** The pipeline accepts a directory of comma-separated value (CSV) files, each required to contain at minimum an `id` column holding USPTO patent identifiers. Two identifier formats are handled:

| Type | Raw example | Normalized form |
|---|---|---|
| Design patent | `D0543613`, `D543613` | `D543613` |
| Utility patent | `0012345678`, `12345678` | `12345678` |

Normalization strips leading zeros and standardizes the `D`-prefix (Eq. 1):

```
normalize("D0543613") → "D543613"
normalize("0012345678") → "12345678"
```

**Example input row** (from a CSV file):

```
id,title,date,class
D543613,Portable Electronic Device,2007-05-29,D14/341
```

**Auxiliary inputs.** Prior to the main loop, the pipeline loads two additional data structures:

1. A pre-built integer-indexed lookup (`KnownIds`), constructed from sorted NumPy arrays (`patent_ids.npy`, `patent_meta.npy`) and a file manifest (`file_list.txt`). This structure supports O(log *n*) membership testing via binary search (`np.searchsorted`) over the full corpus of known patent identifiers.
2. A plain-text progress log (`processed_log_all.txt`), recording the normalized IDs of all patents processed in prior runs, enabling the pipeline to resume without duplicate API calls.

---

## 3.2.2 Processing

For each normalized patent identifier, the pipeline issues an HTTP POST request to the USPTO Enriched Cited Reference Metadata endpoint:

```
POST https://developer.uspto.gov/ds-api/enriched_cited_reference_metadata/v2/records
Content-Type: application/x-www-form-urlencoded
X-API-KEY: <api_key>

criteria=citedDocumentIdentifier%3A%28*D543613*%29&start=0&rows=50
```

The `criteria` parameter uses the USPTO Lucene query syntax to retrieve all records in which the `citedDocumentIdentifier` field contains the target patent number. The wildcard pattern `(*D543613*)` accommodates variations in formatting (e.g., `US D543613 S`, `D543613`). The `rows` parameter is fixed at 50 per request; for the design patent corpus examined in this study, citation counts per patent were consistently within this bound.

**Filtering.** The API response is a JSON object whose `response.docs` array contains one record per citing document. Records lacking an `officeActionDate` field are excluded, as these represent citations that were not issued through a formal office action and therefore do not reflect the patent examiner's prior-art determination. Additionally, five administrative metadata fields are removed from each retained record to reduce storage overhead and eliminate database-internal identifiers irrelevant to citation analysis:

```python
_EXCLUDE_FIELDS = {
    "createUserIdentifier", "obsoleteDocumentIdentifier",
    "qualitySummaryText", "createDateTime", "id"
}
```

**Side effect: novel cited-patent registration.** For each retained citation record, the `citedDocumentIdentifier` value is parsed and checked against the `KnownIds` index. If the cited patent does not appear in any known data source—neither in the original CSV corpus nor in the runtime-accumulated `added.csv` supplement—its identifier is appended to `added.csv` for downstream collection, and the in-memory index is updated. This mechanism automatically expands the corpus to include patents that were referenced by examiner citations but not present in the original dataset.

**Rate limiting.** A fixed inter-request delay of 0.5 seconds is enforced between consecutive API calls to comply with the USPTO rate-limiting policy and avoid service degradation.

---

## 3.2.3 Output

**Primary output — per-CSV JSON files.** For each input CSV file (e.g., `design_2015.csv`), the pipeline writes a corresponding JSON file (e.g., `design_2015.json`) to the output directory (`/mnt/eightthdd/uspto/json/`). The top-level structure is a dictionary keyed by normalized patent ID:

```json
{
  "D543613": {
    "original_id": "D0543613",
    "citations_found": 2,
    "records": [
      {
        "citedDocumentIdentifier": "US D543613 S",
        "officeActionDate": "2011-03-15",
        "citingDocumentIdentifier": "12/345678",
        "citingDocumentTitle": "Electronic Display Device",
        "citingTechnology": "D14/341"
      },
      { "..." : "..." }
    ]
  },
  "D601394": {
    "original_id": "D601394",
    "citations_found": 5,
    "records": [ "..." ]
  }
}
```

Each entry in `records` is a direct mapping from the USPTO API response fields (after exclusion of the administrative fields listed above). The JSON file is updated incrementally after each successful fetch, so partial results are preserved in the event of interruption.

**Secondary output — progress log.** The file `processed_log_all.txt` is appended with the normalized patent ID upon each successful API response (including patents for which zero citations were returned). This log serves as the resume checkpoint for subsequent pipeline invocations.

**Tertiary output — novel patent register.** `added.csv` accumulates identifiers of cited patents discovered during processing that were absent from the original input corpus. These identifiers are subsequently used as inputs for additional patent document retrieval.

---

## 3.2.4 Summary Statistics

Table X reports the scale of the collection. Over the full pipeline run, N input CSV files containing M unique patent identifiers were processed, yielding K citation records distributed across P output JSON files. The total wall-clock time was approximately T hours, inclusive of the mandatory inter-request delays.

> **Note:** Replace N, M, K, P, T with the actual figures from your run logs in `processed_log_all.txt` and the JSON output directory.
