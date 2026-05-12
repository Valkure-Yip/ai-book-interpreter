"""AI Book Interpreter — translate academic books with sliding-window context."""

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

__version__ = "0.1.0"
