"""Development helpers for the Spreadsheet Tools.

These scripts exist so the tools can be built and demoed before the Payments and
KG population tools are finished (guide § "Developing before other tools are
ready"):

* `seed`  — writes sample transactions, obligations and counterparties straight
            into the KG, covering paid, partial, overdue and disputed cases.
* `emit`  — fires `transaction.created` and `obligation.status_changed` events
            so `watch` and live refresh can be tested.

Nothing here is production code. Both operate on the in-memory KG stub and print
what they did.
"""
