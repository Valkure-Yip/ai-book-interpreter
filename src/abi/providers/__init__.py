"""Cross-cutting providers: LLM, observability, storage.

This is the ONLY layer permitted to import langchain / langfuse / external LLM SDKs.
Business modules (survey/translate/assemble/runtime) must go through these abstractions.
"""
