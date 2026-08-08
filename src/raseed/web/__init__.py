"""The read-only web dashboard.

Separate from `adapters/` on purpose. An adapter is a way a receipt gets IN;
this is a way the ledger gets LOOKED AT, and it never writes. Nothing in this
package imports `db.ledger`.
"""
