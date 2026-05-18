# Expanded Type Checking Exposes Test Annotation Debt

When adding recently touched tests to `basedpyright --level error`, old bare `dict` annotations surfaced as `reportMissingTypeArgument` errors. These were not business logic failures, but they block a clean static gate once the files enter the checked set.

Lesson: when refactoring tests around core ledger modules, either keep static checking focused to already-clean files or fix local annotations as part of the same cut. Prefer the latter for core position/trade paths so the validation envelope grows instead of shrinking.
