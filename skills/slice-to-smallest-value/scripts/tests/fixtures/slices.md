# Slices: CSV export

Thinnest path first: one button, one format, current page only.

```json
{
  "parent": {"id": "P2", "title": "Export the orders table", "criteria": [{"id": "AC1", "text": "User can download orders as CSV"}]},
  "slices": [
    {
      "id": "S1",
      "title": "Download current page of orders as CSV",
      "value": "Finance can pull orders into a spreadsheet",
      "scope": ["Export button", "CSV endpoint for current filter"],
      "files": ["src/orders/export.ts"],
      "criteria": [{"id": "S1.1", "text": "Clicking Export downloads a CSV with the visible rows", "proof": "OrdersExportTests.DownloadsVisibleRows", "covers": ["AC1"]}],
      "depends_on": []
    }
  ]
}
```
