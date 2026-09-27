"""In-memory persistence. Swapped for adapters/supabase at migration step 1."""

from slidequote.adapters.memory.repositories import InMemoryRepositories

__all__ = ["InMemoryRepositories"]
