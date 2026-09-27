use g_rs_bank::*;

#[test]
fn judge_overdraw() {
    let mut a = Account { balance: 50 };
    assert_eq!(a.withdraw(80), Err(Error::InsufficientFunds));
    assert_eq!(a.balance, 50);
    assert_eq!(a.withdraw(50), Ok(0));
}
