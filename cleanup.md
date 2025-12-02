# 🧹 **Codebase Cleanup & DRY Guidelines — Python 2026**

## ✅ **Ensure the Code Is**

- **Docstrings:** All shortened to 1-4 lines (mostly 1-liners).
- **DRY & Deduplicated:** Remove repeated literals—strings, numbers, booleans, or other constants—by centralizing them into **enums**, constants, or configuration objects.
- **Reusable:** Abstract repeated logic into well-named **helper functions**, utility classes, or mixins for modularity and composability.
- **Concise:** Write code with minimal lines without changing behavior. Prefer expressive, compact patterns over verbose repetition.
- **Up to Date:** Leverage modern Python 3.13+ features — structural pattern matching, type hints, f-strings, `dataclasses`, `asyncio`, and `contextlib` improvements.
- **KISS & YAGNI:** Keep solutions simple; avoid unnecessary abstractions or speculative extensibility.
- **Separated by Concern (SoC):** Each module, class, and function should have a single, clear responsibility.
- **Convention Over Configuration:** Follow Python ecosystem norms — directory structure, naming, virtual environments, and style guides.
- **Readable & Self-Documenting:** Use descriptive names; code should read like prose. Comments only clarify **why**, not **what**.
- **Testable by Design:** Extract helper functions and reusable units so they can be tested independently.
- **Composable & Evolvable:** Helpers, enums, and utilities should integrate predictably, supporting future refactors without breaking existing logic.
- **Idiomatic & Pythonic:** Follow **PEP 8/484**, favor explicitness, clarity, and consistent Pythonic patterns.

---

## 🔍 **Look For**

- **Repeated Values:** Strings, numbers, booleans, or configuration-like constants used in multiple places. Deduplicate using enums, constants, or a central configuration module.
- **Repeated Logic:** Common patterns or calculations implemented in multiple places. Extract into **helper functions** or small utility modules.
- **Tight Coupling:** Avoid hard-coded dependencies on specific modules or literals. Favor dependency injection, helper reuse, and modular boundaries.
- **Inefficient Patterns:**

  - Expensive operations repeated inside loops
  - Recomputing immutable results (use memoization/caching)
  - Redundant type checks or conditionals

- **Unclear Abstractions:** Avoid mixing literal values and logic; ensure enums and helpers clearly represent their domain.
- **Inconsistent Style:** Violations of `black`, `ruff`, or `flake8`. Enforce uniform formatting and import order.

---

## ⚙️ **Best Practices for DRY & Reusability**

- **Enums & Constants:**

  ```python
  from enum import Enum, auto

  class UserRole(Enum):
      ADMIN = auto()
      MODERATOR = auto()

  MAX_RETRIES = 5
  ```

- **Helper Functions:** Encapsulate reusable logic:

  ```python
  def is_active(user):
      return user.status == Status.ACTIVE

  def retry_operation(func, retries=MAX_RETRIES):
      for _ in range(retries):
          if func():
              return True
      return False
  ```

- **Use Across Codebase:** Replace all literals and repeated logic with these enums/constants/helpers to reduce boilerplate, minimize errors, and keep code concise.
- **DRY Loops & Collections:** Prefer comprehensions, generator expressions, or helper utilities over repeated loops.
- **Centralize Domain Knowledge:** Enums, constants, and helpers should reflect domain-specific concepts, not ad-hoc magic values.

---

## ⚡ **Performance Considerations**

- Algorithmic efficiency: avoid O(n²) patterns where O(n log n) is possible.
- Memory & I/O: cache results, batch operations, and avoid redundant allocations.
- Reusable helpers can reduce overhead and improve maintainability without changing runtime performance.
