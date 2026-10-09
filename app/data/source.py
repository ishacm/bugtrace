"""Raw SWE-bench row fetching via the Hugging Face datasets-server API.

Raw rows contain gold answers. Only the adapter (`app.data.swebench`) and the
evaluator (`app.evaluation`) may use this module; agent code must not.
"""

import json
import urllib.parse
import urllib.request
from typing import Any

DATASETS_SERVER_URL = "https://datasets-server.huggingface.co/rows"
MAX_PAGE_SIZE = 100
TIMEOUT_SECONDS = 30

RawRow = dict[str, Any]


class DatasetFetchError(RuntimeError):
    pass


def fetch_raw_rows(dataset_id: str, split: str, limit: int) -> list[RawRow]:
    """Return up to `limit` raw rows from the start of `split`."""
    rows: list[RawRow] = []
    offset = 0
    while len(rows) < limit:
        length = min(MAX_PAGE_SIZE, limit - len(rows))
        query = urllib.parse.urlencode(
            {
                "dataset": dataset_id,
                "config": "default",
                "split": split,
                "offset": offset,
                "length": length,
            }
        )
        try:
            with urllib.request.urlopen(  # noqa: S310 - fixed https URL
                f"{DATASETS_SERVER_URL}?{query}", timeout=TIMEOUT_SECONDS
            ) as response:
                payload = json.load(response)
        except (OSError, ValueError) as exc:
            raise DatasetFetchError(f"failed to fetch {dataset_id}: {exc}") from exc

        page = payload.get("rows", [])
        if not page:
            break
        for item in page:
            if item.get("truncated_cells"):
                raise DatasetFetchError(
                    f"row {item.get('row_idx')} has truncated cells: {item['truncated_cells']}"
                )
            rows.append(item["row"])
        offset += len(page)
    return rows[:limit]
