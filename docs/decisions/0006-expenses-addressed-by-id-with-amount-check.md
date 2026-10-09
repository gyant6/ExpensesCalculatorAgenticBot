# 0006. Address expenses by an immutable id, checked against the amount

- **Status:** Accepted
- **Date:** 2026-10-09

## Context

`edit_expense` and `delete_expense` took a 1-based position in the list, resolved against a
fresh query at call time. Reading a September 2026 trip's full chat history against its
exported CSV showed three failure modes:

- **Batched date edits.** A date edit rewrote the sort key, which the list was sorted by,
  so the expense moved and later positions in the same batch pointed elsewhere. Seven rows
  ended up wrong.
- **The neighbouring line.** The model picked the line next to the one meant, twice, with
  no date edit and no second member involved.
- **Every edit renamed.** `summary` was required, so a misdirected edit also relabelled
  its wrong target, and the damage looked like a duplicate rather than an error.

Asked to repair the list, the model then re-added expenses from memory, double-counting
three.

## Decision

- Each expense's sort key is `EXPENSE#<id>`: four random characters from an alphabet
  without look-alikes (`23456789abcdefghjkmnpqrstuvwxyz`), written with
  `attribute_not_exists` so a collision retries instead of overwriting.
- Edit and delete take `expense_id` **and** `expected_amount`, fetch the item with one
  `get_item`, and refuse — naming what the id actually holds — if the amount differs.
- `summary` is optional on edits; a date edit is a plain update, since the key encodes no
  date.
- Lists sort by `date`, then `created_at`.
- The model sees ids in tool output only and never shows them to users. The prompt
  forbids re-adding expenses from memory.

## Consequences

- A target cannot shift during a batch or after a date edit, and a neighbouring-line pick
  is refused rather than applied. Regression tests replay all three failures.
- The amount differed in every observed case; a false refusal costs a retry, never data.
- Ids are not shown, so users keep referring to expenses by name or number.

## Alternatives considered

- **Verify the position only.** Turns silent errors loud but keeps the unstable
  addressing.
- **Address by the old timestamp sort key.** Long to transcribe, and a date edit still
  rewrote it.
- **Ids without the amount check.** Does not catch an id copied from the next line.
