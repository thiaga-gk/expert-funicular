"""Code shared by the Databricks products (the publisher and the realizer).

Neutral by construction: nothing here imports either product, and a product's identity (logger, user agent, error
type) is injected by the caller, never hard-coded.
"""
