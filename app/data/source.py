"""Raw SWE-bench row fetching via the Hugging Face datasets-server API.

Raw rows contain gold answers. Only the adapter (`app.data.swebench`) and the
evaluator (`app.evaluation`) may use this module; agent code must not.
"""

import itertools
import json
import urllib.parse
import urllib.request
from collections.abc import Iterator
from typing import Any

DATASETS_SERVER_URL = "https://datasets-server.huggingface.co/rows"
MAX_PAGE_SIZE = 100
TIMEOUT_SECONDS = 30

RawRow = dict[str, Any]


class DatasetFetchError(RuntimeError):
    pass


def iter_raw_rows(dataset_id: str, split: str) -> Iterator[RawRow]:
    """Yield raw rows from the start of `split`, one page request at a time."""
    offset = 0
    while True:
        query = urllib.parse.urlencode(
            {
                "dataset": dataset_id,
                "config": "default",
                "split": split,
                "offset": offset,
                "length": MAX_PAGE_SIZE,
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
            return
        for item in page:
            if item.get("truncated_cells"):
                raise DatasetFetchError(
                    f"row {item.get('row_idx')} has truncated cells: {item['truncated_cells']}"
                )
            yield item["row"]
        offset += len(page)


def fetch_raw_rows(dataset_id: str, split: str, limit: int) -> list[RawRow]:
    """Return up to `limit` raw rows from the start of `split`."""
    return list(itertools.islice(iter_raw_rows(dataset_id, split), limit))


def find_raw_row(dataset_id: str, split: str, instance_id: str) -> RawRow:
    """Return the raw row for one instance (evaluator use only)."""
    for row in iter_raw_rows(dataset_id, split):
        if row.get("instance_id") == instance_id:
            return row
    raise DatasetFetchError(f"{instance_id} not found in {dataset_id}:{split}")
