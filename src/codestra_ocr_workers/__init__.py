"""Codestra OCR worker runtime.

Internal-only worker: accepts bounded document images, runs a pluggable OCR engine and
returns transient structured extraction results. It never persists images or owns
client/business records.
"""

__version__ = "0.1.0"
