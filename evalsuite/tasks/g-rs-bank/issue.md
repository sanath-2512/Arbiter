`Account::withdraw` lets the balance go negative. Withdrawing more than the balance must return `Err(Error::InsufficientFunds)` and leave the balance unchanged.
