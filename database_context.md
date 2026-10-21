## Notes on the data

These notes describe what the data means. Edit them freely. Do not paste real values or
personal data here: this text is sent to the model provider with every request, and the
file is refused if it looks like it contains personal data.

The table and column list above is generated from the live database, so it is always
current. Only meaning belongs in this file.

### Business definitions (placeholders: edit to match your organisation)
- transactions.amount: negative means money out (a debit), positive means money in (a credit).
- transactions.flagged: 1 means the transaction was marked for review, 0 means it was not.
- accounts.balance: the current balance in dollars.
- customers.segment: one of retail, premium, business.
- accounts.account_type: one of checking, savings, credit.
- customer_profiles.credit_band, from lowest to highest: poor, fair, good, very_good, excellent.
- customer_profiles.signup_channel: one of web, branch, mobile, referral.

### Formats
- All dates are ISO text, YYYY-MM-DD. For months use substr(date, 1, 7).
- Transactions run from April to October 2026.

### How tables relate
- accounts.customer_id points to customers.id.
- transactions.account_id points to accounts.id.
- customer_profiles.customer_id points to customers.id (one profile per customer).

### Writing good queries here
- Join with ON. JOIN ... USING and NATURAL JOIN are not allowed.
- On customers and customer_profiles, name the columns you want instead of SELECT *.
- Results are capped at 100 rows, so aggregate or filter first.
- Use CREATE TEMP TABLE for scratch work that later queries in the session can reuse.
- Tables you create (temporary or in your workspace) can't be updated, replaced, or dropped.

### Example questions and the shape of a good answer
- Flagged rate by state: join transactions to accounts to customer_profiles with ON, group
  by state, and divide the sum of flagged by the count.
- Spend by credit band: sum negative amounts per credit_band.
- Month over month: group by substr(date, 1, 7) and use LAG in a window function.
