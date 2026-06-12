"""AI Book Interpreter — a self-contained autonomous agent that translates
public-domain books into versioned, quality-gated EPUBs."""

from __future__ import annotations

import warnings

# Pydantic >=2.10 warns about any field name that shadows a parent attribute,
# including ``register`` (an inherited method on BaseModel). Our `register`
# fields are semantic (the book's register, e.g. academic-formal) and not a
# method-shadowing risk. Silence the noise globally.
warnings.filterwarnings(
    "ignore",
    message=r"Field name .register. in .* shadows an attribute in parent .*",
    category=UserWarning,
)

__version__ = "0.2.0"
