"""The serving layer: an HTTP API over the weekly predictions table.

Nothing here fits a model. The weekly job scores player-weeks and publishes a
predictions file with a manifest; this package loads that file, answers squad and
player questions from it, reports its own freshness, and reloads when a new one
is published. The Streamlit page is a second reader of the same files.
"""
